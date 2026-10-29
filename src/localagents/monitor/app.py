"""The monitor's web app (story 3.1; routes and JSON shapes: monitor spec §5a).

``make_app`` builds a Starlette app over a connection factory: every route's database work runs
in ``asyncio.to_thread`` with a connection of its own from ``conn_factory`` (WAL readers; a
connection is never shared across threads), closed when the call ends. A ``sqlite3.Error`` or
``db.SchemaError`` answers 503 ``{"error": "database_error", "message": ...}``.

``GET /events`` is the server-sent events stream the page lives on (spec §5: "the server pushes
a state snapshot after every collection tick; the page redraws in place and never polls"): one
``snapshot`` event on connect, one more each time the collector sets ``tick``, a ``: keepalive``
comment after ``keepalive_s`` of silence.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import db
from . import read
from .ranges import DEFAULT_RANGE, parse_range

STATIC_DIR = Path(__file__).resolve().parent / "static"
KEEPALIVE_S = 15.0
JOBS_DEFAULT_RANGE = "24h"


def _error(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code=status)


def _sse(event: str, body: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(body)}\n\n"


class _Fanout:
    """Wakes every connected stream when the collector sets ``tick``: one task waits on it,
    clears it and sets each subscriber's own event, so no stream can miss a tick another
    stream cleared. Started on the first subscriber, on the serving loop."""

    def __init__(self, tick: asyncio.Event):
        self.tick = tick
        self.subscribers: set[asyncio.Event] = set()
        self.task: asyncio.Task | None = None

    def subscribe(self) -> asyncio.Event:
        loop = asyncio.get_running_loop()
        if self.task is None or self.task.done() or self.task.get_loop() is not loop:
            self.task = loop.create_task(self._run())
        ev = asyncio.Event()
        self.subscribers.add(ev)
        return ev

    def unsubscribe(self, ev: asyncio.Event) -> None:
        self.subscribers.discard(ev)

    async def _run(self) -> None:
        while True:
            await self.tick.wait()
            self.tick.clear()
            for ev in list(self.subscribers):
                ev.set()

    def close(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()


def make_app(
    conn_factory: Callable[[], sqlite3.Connection],
    reg: Any,
    settings: Any,
    tick: asyncio.Event,
    *,
    now: Callable[[], float] = time.time,
    stop: asyncio.Event | None = None,
    keepalive_s: float = KEEPALIVE_S,
) -> Starlette:
    """The monitor app. ``reg`` is the ``Registry`` whose endpoints the overview lists (config
    order); ``settings`` a ``MonitorSettings`` (``attention_hours`` is the attention window);
    ``tick`` is set by the collector after each endpoint's tick (``collect.run``'s ``on_tick``).
    ``now`` is the clock (a test pins it to the seeded world's); ``stop``, when set (and ``tick``
    set to wake the streams), ends every open event stream so the server can shut down."""
    window_s = float(settings.attention_hours) * 3600.0
    fan = _Fanout(tick)

    async def in_thread(fn: Callable[..., Any], *args: Any) -> Any:
        def run() -> Any:
            conn = conn_factory()
            try:
                return fn(conn, *args)
            finally:
                conn.close()
        return await asyncio.to_thread(run)

    def guarded(handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
        async def wrapper(request: Request) -> Response:
            try:
                return await handler(request)
            except (sqlite3.Error, db.SchemaError) as e:
                return _error(503, "database_error", f"{type(e).__name__}: {e}")
        return wrapper

    def range_s(request: Request, default: str) -> float:
        return parse_range(request.query_params.get("range", default))

    async def index(request: Request) -> Response:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @guarded
    async def snapshot(request: Request) -> Response:
        return JSONResponse(await in_thread(read.overview, reg, now(), window_s))

    @guarded
    async def endpoint_history(request: Request) -> Response:
        name = request.path_params["name"]
        ep = reg.endpoints.get(name)
        if ep is None:
            return _error(404, "unknown_endpoint", f"no endpoint named {name!r} in the config")
        try:
            span = range_s(request, DEFAULT_RANGE)
        except ValueError as e:
            return _error(400, "bad_range", str(e))
        t = now()

        def body(conn: sqlite3.Connection) -> dict:
            return read.endpoint_history(conn, read.endpoint_row_id(conn, ep), t - span, t)
        return JSONResponse(await in_thread(body))

    @guarded
    async def jobs(request: Request) -> Response:
        try:
            span = range_s(request, JOBS_DEFAULT_RANGE)
        except ValueError as e:
            return _error(400, "bad_range", str(e))
        t = now()
        return JSONResponse(await in_thread(read.jobs_in_range, t - span, t))

    @guarded
    async def job(request: Request) -> Response:
        job_id = request.path_params["id"]
        d = await in_thread(read.job_detail, job_id)
        if d is None:
            return _error(404, "unknown_job", f"no job with id {job_id!r}")
        return JSONResponse(d)

    async def snapshot_event() -> str:
        try:
            return _sse("snapshot", await in_thread(read.overview, reg, now(), window_s))
        except (sqlite3.Error, db.SchemaError) as e:
            return _sse("error", {"error": "database_error", "message": f"{type(e).__name__}: {e}"})

    async def events(request: Request) -> Response:
        async def stream() -> AsyncIterator[str]:
            ev = fan.subscribe()
            try:
                yield await snapshot_event()
                while stop is None or not stop.is_set():
                    try:
                        await asyncio.wait_for(ev.wait(), keepalive_s)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    ev.clear()
                    if stop is not None and stop.is_set():
                        break
                    yield await snapshot_event()
            finally:
                fan.unsubscribe(ev)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app = Starlette(routes=[
        Route("/", index),
        Route("/api/snapshot", snapshot),
        Route("/api/endpoints/{name}/history", endpoint_history),
        Route("/api/jobs", jobs),
        Route("/api/jobs/{id}", job),
        Route("/events", events),
        Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
    ])
    app.state.fanout = fan
    return app
