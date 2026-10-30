"""The monitor's web app (story 3.1; routes and JSON shapes: monitor spec §5a).

``make_app`` builds a Starlette app over a connection factory: every route's database work runs
in ``asyncio.to_thread`` with a connection of its own from ``conn_factory`` (WAL readers; a
connection is never shared across threads), closed when the call ends. A ``sqlite3.Error`` or
``db.SchemaError`` answers 503 ``{"error": "database_error", "message": ...}``.

``GET /events`` is the server-sent events stream the page lives on (spec §5: "the server pushes
a state snapshot after every collection tick; the page redraws in place and never polls"): one
``snapshot`` event on connect, then one per refresh period of the fan-out's clock (story 3.7m:
the collector's ticks are coalesced, so a probe round is one push and nothing is sent between
pushes), and a ``: keepalive`` comment after ``keepalive_s`` of silence.
"""

from __future__ import annotations

import asyncio
import contextlib
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


class _RevalidatingStatic(StaticFiles):
    """Story 3.2a row 2 (the 3.3 build saw Chromium reuse an old app.js after an upgrade): every
    answer — 200 or the 304 of its own ETag/Last-Modified revalidation — carries
    ``Cache-Control: no-cache``, so the browser asks on every load and an upgrade never runs
    stale page code; the files are tiny and local, so the round-trip costs nothing."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _error(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code=status)


def _sse(event: str, body: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(body)}\n\n"


def _configured_endpoint_rows(
    conn: sqlite3.Connection, reg: Any
) -> tuple[dict[str, int], dict[int, float | None]]:
    """The configured endpoints' ``endpoints`` row ids, and each id's newest probe ``t``, in
    two statements however many endpoints there are (story 3.7p's history cache keys on the
    newest ``t``): one read of the small ``endpoints`` table, matched to the config by the same
    composite key ``read.endpoint_row_id`` uses (name, base_url, backend, host), and one
    grouped ``MAX(t)`` over the configured ids. An endpoint never probed has no entry in the
    second map (and may have no id in the first)."""
    by_key = {
        (r["name"], r["base_url"], r["backend"], r["host"]): r["id"]
        for r in conn.execute("SELECT id, name, base_url, backend, host FROM endpoints")
    }
    ids = {ep.name: by_key[(ep.name, ep.url, ep.backend, ep.host)]
           for ep in reg.endpoints.values()
           if (ep.name, ep.url, ep.backend, ep.host) in by_key}
    newest: dict[int, float | None] = {}
    if ids:
        marks = ", ".join("?" * len(ids))
        newest = {r[0]: r[1] for r in conn.execute(
            f"SELECT endpoint_id, MAX(t) FROM probes WHERE endpoint_id IN ({marks})"
            " GROUP BY endpoint_id", tuple(ids.values()))}
    return ids, newest


def _fleet_newest(newest: dict[int, float | None]) -> float | None:
    """The fleet cache key: the newest probe t over the configured endpoints."""
    return max((t for t in newest.values() if t is not None), default=None)


class _Fanout:
    """The monitor's one refresh clock (story 3.7m): it COALESCES the collector's ticks. One
    task waits for a tick, clears it, then waits ``refresh_s`` for the probe round to settle;
    ticks that arrive in that window belong to the same round and do not extend it. Then it
    wakes every connected stream once and goes back to waiting for the next tick. Nothing is
    pushed between those wakes, so a stream sees at most one snapshot per ``refresh_s``
    whatever the collector's per-endpoint cadence. The connect snapshot stays immediate (the
    stream builds it itself). ``pushes`` is the monotonic push counter: the shared snapshot
    cache (3.7p) is keyed on it, so every subscriber of one push yields the same bytes.

    Story 3.3a row 1 (the 3.3 review's first server seam): the task starts with the app —
    ``make_app`` calls ``start`` whenever a loop is already running there (serve.py's own
    loop, the SSE tests' loop) — so a tick before any browser connects is consumed here and
    the first page load receives one snapshot, not two. Built outside a loop (a Starlette
    ``TestClient``, whose app runs on a portal loop of its own), the loop of the first
    subscriber gets it instead; ``start`` also re-homes the task if that loop is not the one
    the app was built on."""

    def __init__(self, tick: asyncio.Event, stop: asyncio.Event | None = None, refresh_s: float = 5.0):
        self.tick = tick
        self.stop = stop
        self.refresh_s = refresh_s
        self.subscribers: set[asyncio.Event] = set()
        self.task: asyncio.Task | None = None
        self.closed = False
        self.pushes = 0  # story 3.7p: the current push's counter (the snapshot cache's key)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.closed:  # story 3.2c row 1: close() is final — see close()
            return
        if self.task is None or self.task.done() or self.task.get_loop() is not loop:
            self.task = loop.create_task(self._run())

    def subscribe(self) -> asyncio.Event:
        self.start(asyncio.get_running_loop())
        ev = asyncio.Event()
        self.subscribers.add(ev)
        return ev

    def unsubscribe(self, ev: asyncio.Event) -> None:
        self.subscribers.discard(ev)

    async def _settle(self) -> None:
        """Wait ``refresh_s`` for the probe round, unless the server is stopping: a stop must
        end the streams at once, not after the clock's period. A stop already set returns
        WITHOUT suspending, so ``_run`` sets every subscriber's event in one step as it did
        before the clock — ``serve.stopper`` sets ``stop`` and ``tick``, lets the loop turn
        once, then closes the fan-out, and the drain must have woken the streams by then."""
        if self.stop is None:
            await asyncio.sleep(self.refresh_s)
            return
        if self.stop.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), self.refresh_s)

    async def _run(self) -> None:
        while True:
            await self.tick.wait()
            self.tick.clear()
            await self._settle()
            # ticks that arrived during the settle are this round's: consume them, so the
            # next push waits for a fresh tick (a probe round is one push, not a train)
            self.tick.clear()
            self.pushes += 1
            for ev in list(self.subscribers):
                ev.set()

    async def close(self) -> None:
        """Story 3.2c row 1 (the 3.2 combined review): this had no caller, so a stop on a loop
        that outlives the app left the drain task pending forever — the shipped command only
        masked it with ``asyncio.run`` cancelling the leftovers at exit. The stop path now calls
        this and awaits it: the drain is cancelled and awaited, so after ``stop`` no
        ``_Fanout._run`` task is pending. Idempotent for the races ruling (d) names: the second
        stop and the other open streams all arrive here too — whoever took the task awaits it,
        whoever finds it taken returns. After close, ``start`` (and with it ``subscribe``'s lazy
        fallback) never restarts the drain: a stream connecting after the stop gets its connect
        snapshot and ends at once, resurrecting nothing. A wake this close races does not lose
        a tick that mattered: every subscriber's event is set in one step of ``_run`` before any
        stream wakes, and streams ending on a set ``stop`` want no further tick anyway."""
        self.closed = True
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def make_app(
    conn_factory: Callable[[], sqlite3.Connection],
    reg: Any,
    settings: Any,
    tick: asyncio.Event,
    *,
    now: Callable[[], float] = time.time,
    stop: asyncio.Event | None = None,
    keepalive_s: float = KEEPALIVE_S,
    db_path: Any = None,
) -> Starlette:
    """The monitor app. ``reg`` is the ``Registry`` whose endpoints the overview lists (config
    order); ``settings`` a ``MonitorSettings`` (``attention_hours`` is the attention window);
    ``tick`` is set by the collector after each endpoint's tick (``collect.run``'s ``on_tick``).
    ``now`` is the clock (a test pins it to the seeded world's); ``stop``, when set (and ``tick``
    set to wake the streams), ends every open event stream so the server can shut down;
    ``db_path`` (story 3.2a row 3) is the database file serve opened — the snapshot names it."""
    window_s = float(settings.attention_hours) * 3600.0
    db_path_s = str(db_path) if db_path is not None else None
    fan = _Fanout(tick, stop, float(settings.refresh_s))
    # story 3.7p: ONE snapshot read per push, shared by every stream (and by /api/snapshot
    # while that push is current). Keyed on the fan-out's monotonic push counter; the first
    # caller after a push computes, the rest reuse — including a cached database error, so
    # every subscriber of one push hears the same answer.
    snapshot_cache: dict[str, Any] = {"push": -1, "snap": None, "sse": None, "error": None}
    snapshot_lock = asyncio.Lock()
    try:  # story 3.3a row 1: drain from the app's start (see _Fanout), not the first subscriber
        fan.start(asyncio.get_running_loop())
    except RuntimeError:  # built outside a loop (TestClient's portal): first subscribe starts it
        pass

    async def in_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        def run() -> Any:
            conn = conn_factory()
            try:
                return fn(conn, *args, **kwargs)
            finally:
                conn.close()
        return await asyncio.to_thread(run)

    async def shared_snapshot() -> tuple[dict[str, Any] | None, str | None, str | None]:
        """The current push's ``(snapshot, sse body, error sentence)``, computed at most once.
        The lock spans the compute, so the subscribers woken by one push wait for the first
        caller's read and then take its bytes (3.7p: one read and one serialisation per push)."""
        async with snapshot_lock:
            push = fan.pushes
            if snapshot_cache["push"] != push:
                try:
                    body = await in_thread(read.overview, reg, now(), window_s, db_path_s,
                                           refresh_s=float(settings.refresh_s))
                except (sqlite3.Error, db.SchemaError) as e:
                    snapshot_cache.update(push=push, snap=None, sse=None,
                                          error=f"{type(e).__name__}: {e}")
                else:
                    snapshot_cache.update(push=push, snap=body, sse=_sse("snapshot", body),
                                          error=None)
            return snapshot_cache["snap"], snapshot_cache["sse"], snapshot_cache["error"]

    def guarded(handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
        async def wrapper(request: Request) -> Response:
            try:
                return await handler(request)
            except (sqlite3.Error, db.SchemaError) as e:
                return _error(503, "database_error", f"{type(e).__name__}: {e}")
        return wrapper

    def range_text(request: Request, default: str) -> str:
        return request.query_params.get("range", default)

    def range_s(request: Request, default: str) -> float:
        return parse_range(range_text(request, default))

    # story 3.7p: history bodies are cached per (endpoint name or "fleet", range text) and
    # keyed on the newest probe t, so a request between ticks is a dictionary hit and a
    # request after a new probe recomputes. One entry per key (the newest); a test counts the
    # statements a warm call costs (two for the id/newest-t read).
    history_cache: dict[tuple[str, str], tuple[float | None, dict[str, Any]]] = {}

    def cached_endpoint_history(conn: sqlite3.Connection, name: str, eid: int | None,
                                newest_t: float | None, text: str, span: float,
                                t: float) -> dict[str, Any]:
        key = (name, text)
        hit = history_cache.get(key)
        if hit is not None and hit[0] == newest_t:
            return hit[1]
        body = read.endpoint_history(conn, eid, t - span, t)
        history_cache[key] = (newest_t, body)
        return body

    def cached_fleet_history(conn: sqlite3.Connection, newest_t: float | None, text: str,
                             span: float, t: float,
                             histories: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        key = ("fleet", text)
        hit = history_cache.get(key)
        if hit is not None and hit[0] == newest_t:
            return hit[1]
        body = read.fleet_history(conn, reg, t - span, t, histories=histories)
        history_cache[key] = (newest_t, body)
        return body

    async def index(request: Request) -> Response:
        # story 3.2c row 3 (the 3.2 combined review): the shell answers no-cache like the static
        # mount below — it names the page's scripts, so a shell the browser reused without
        # asking pins a stale build across an upgrade.
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    @guarded
    async def snapshot(request: Request) -> Response:
        body, _sse_body, error = await shared_snapshot()
        if error is not None:
            return _error(503, "database_error", error)
        return JSONResponse(body)

    @guarded
    async def endpoint_history(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in reg.endpoints:
            return _error(404, "unknown_endpoint", f"no endpoint named {name!r} in the config")
        try:
            span = range_s(request, DEFAULT_RANGE)
        except ValueError as e:
            return _error(400, "bad_range", str(e))
        text = range_text(request, DEFAULT_RANGE)
        t = now()

        def body(conn: sqlite3.Connection) -> dict:
            ids, newest = _configured_endpoint_rows(conn, reg)
            eid = ids.get(name)
            return cached_endpoint_history(conn, name, eid, newest.get(eid), text, span, t)
        return JSONResponse(await in_thread(body))

    @guarded
    async def endpoint_requests(request: Request) -> Response:
        name = request.path_params["name"]
        ep = reg.endpoints.get(name)
        if ep is None:
            return _error(404, "unknown_endpoint", f"no endpoint named {name!r} in the config")
        try:
            span = range_s(request, JOBS_DEFAULT_RANGE)  # the requests table spans the job horizons: 24 h
        except ValueError as e:
            return _error(400, "bad_range", str(e))
        t = now()

        def body(conn: sqlite3.Connection) -> dict:
            return read.endpoint_requests(conn, read.endpoint_row_id(conn, ep), t - span, t)
        return JSONResponse(await in_thread(body))

    @guarded
    async def fleet_history(request: Request) -> Response:
        try:
            span = range_s(request, DEFAULT_RANGE)
        except ValueError as e:
            return _error(400, "bad_range", str(e))
        text = range_text(request, DEFAULT_RANGE)
        t = now()

        def body(conn: sqlite3.Connection) -> dict:
            _ids, newest = _configured_endpoint_rows(conn, reg)
            return cached_fleet_history(conn, _fleet_newest(newest), text, span, t)
        return JSONResponse(await in_thread(body))

    @guarded
    async def overview_history(request: Request) -> Response:
        """Story 3.7p's batched route: the fleet grid plus the history of every endpoint the
        overview lists as live at this moment, in one body. The live set is the snapshot's own
        classification (``state == "live"``), never a second rule; a snapshot of the current
        push is normally already cached, so this read starts at the histories."""
        try:
            span = range_s(request, DEFAULT_RANGE)
        except ValueError as e:
            return _error(400, "bad_range", str(e))
        text = range_text(request, DEFAULT_RANGE)
        t = now()
        snap, _sse_body, error = await shared_snapshot()
        if error is not None:
            return _error(503, "database_error", error)
        live = [ep["name"] for host in snap["hosts"] for ep in host["endpoints"]
                if ep["state"] == "live"]

        def body(conn: sqlite3.Connection) -> dict:
            ids, newest = _configured_endpoint_rows(conn, reg)
            endpoints, histories = {}, {}
            for name in live:
                eid = ids.get(name)
                h = cached_endpoint_history(conn, name, eid, newest.get(eid), text, span, t)
                endpoints[name] = h
                histories[name] = h
            fleet = cached_fleet_history(conn, _fleet_newest(newest), text, span, t, histories)
            return {"fleet": fleet, "endpoints": endpoints}
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
        t = now()
        d = await in_thread(read.job_detail, job_id, t)
        if d is None:
            return _error(404, "unknown_job", f"no job with id {job_id!r}")
        return JSONResponse(d)

    async def snapshot_event() -> str:
        _body, sse_body, error = await shared_snapshot()
        if error is not None:
            return _sse("error", {"error": "database_error", "message": error})
        return sse_body

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
                # Story 3.2c row 1 (the 3.2 combined review): reaching here means the loop
                # ended on a set `stop` — the app's stop path, and the review's place where a
                # caller-less `_Fanout.close()` left the drain task pending. The first stream
                # home cancels it and awaits; the streams behind it find it closed and return.
                await fan.close()
            finally:
                fan.unsubscribe(ev)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app = Starlette(routes=[
        Route("/", index),
        Route("/api/snapshot", snapshot),
        Route("/api/endpoints/{name}/history", endpoint_history),
        Route("/api/endpoints/{name}/requests", endpoint_requests),
        Route("/api/fleet/history", fleet_history),
        Route("/api/overview/history", overview_history),
        Route("/api/jobs", jobs),
        Route("/api/jobs/{id}", job),
        Route("/events", events),
        Mount("/static", app=_RevalidatingStatic(directory=STATIC_DIR), name="static"),
    ])
    app.state.fanout = fan
    return app
