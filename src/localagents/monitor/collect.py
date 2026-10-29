"""The collection loop (monitor spec §3 "Collection loop", §6): probe every endpoint, write one
``probes`` row per probe (a failed probe is a row with its sentence, never a gap), and record
``endpoint_events`` on up/down transitions and restarts.

Everything runs on the event loop thread with one connection: each tick's database work is a
single ``immediate()`` block with no ``await`` inside, so ticks of different endpoints never
interleave inside a transaction.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from typing import Any, Callable, Iterable

from .. import db
from ..registry import Endpoint, backend_kind, fetch_metrics_doc
from . import rates, retention, rows, state


def endpoint_id(conn: sqlite3.Connection, ep: Endpoint) -> int:
    """The endpoints row, keyed exactly as ``JobStore.new`` keys it from server.py (name,
    ``ep.url`` — the base URL without a trailing slash — the backend as configured, host), so
    the monitor and the MCP server share one row per endpoint."""
    return db.lookup_id(conn, "endpoints", name=ep.name, base_url=ep.url, backend=ep.backend, host=ep.host)


def last_up_t(conn: sqlite3.Connection, endpoint_id: int) -> float | None:
    """Time of the endpoint's newest up probe, or None when it was never seen up."""
    row = conn.execute("SELECT MAX(t) FROM probes WHERE endpoint_id = ? AND up = 1", (endpoint_id,)).fetchone()
    return row[0]


_PREVIOUS_PROBE = "SELECT up FROM probes WHERE endpoint_id = ? ORDER BY t DESC, id DESC LIMIT 1"
_PREVIOUS_COUNTERS = (
    f"SELECT p.t, p.since, {', '.join('c.' + k for k in rows.COUNTER_COLUMNS)}"
    " FROM probe_counters c JOIN probes p ON p.id = c.probe_id"
    " WHERE p.endpoint_id = ? ORDER BY p.t DESC, p.id DESC LIMIT 1"
)
# Down probes in the interval between the previous counters row and this tick: a rate whose
# dt spans an outage attributes the post-gap growth to the whole wall time (review P3).
_DOWN_PROBES = "SELECT COUNT(*) FROM probes WHERE endpoint_id = ? AND up = 0 AND t > ? AND t <= ?"


def _event(conn: sqlite3.Connection, t: float, endpoint_id: int, kind: str, detail: str | None) -> None:
    conn.execute("INSERT INTO endpoint_events (t, endpoint_id, kind, detail) VALUES (?, ?, ?, ?)",
                 (t, endpoint_id, kind, detail))


async def tick(reg: Any, conn: sqlite3.Connection, ep: Endpoint, endpoint_id: int, now: float, backend: str) -> int:
    """Probe ``ep`` once, write its rows, record events; returns the probe id.

    ``/metrics`` is fetched only when the probe is up (a down endpoint would time out a second
    time). Events, compared with the endpoint's previous rows: up -> down writes ``down`` with
    the failure sentence, down -> up writes ``up``; a counter that went backwards since the
    previous counters row or a changed ``since`` writes ``restarted`` naming it. An interval
    that contains a down probe has NO rate (like a reset, and not a restart: the growth may have
    happened before the outage), so the post-gap tick writes no rate and no event. The first
    probe of an endpoint writes no event (a cold start is not a transition).

    The tick's database work is one transaction: a failure anywhere inside the ``immediate()``
    block (a row, an event) rolls the whole tick back — a probes row is never stored without its
    counters/hardware rows, and no event is stored for a tick whose rows did not commit."""
    probe = await reg.probe(ep)
    up = bool(probe.get("up"))
    doc = await fetch_metrics_doc(ep, reg.defaults.probe_timeout_s) if up else None
    models = probe.get("models") or []
    with db.immediate(conn):
        model_id = db.lookup_id(conn, "models", served_id=models[0]) if up and models else None
        r = rows.probe_to_rows(endpoint_id, model_id, probe, doc, now)
        last = conn.execute(_PREVIOUS_PROBE, (endpoint_id,)).fetchone()
        prev = conn.execute(_PREVIOUS_COUNTERS, (endpoint_id,)).fetchone()
        probe_id = rows.insert(conn, r)
        if last is not None and bool(last["up"]) != up:
            _event(conn, now, endpoint_id, "up" if up else "down", None if up else r.probe["error"])
        if prev is not None and r.counters is not None:
            down = conn.execute(_DOWN_PROBES, (endpoint_id, prev["t"], now)).fetchone()[0]
            if down:
                # ruling B: a rate spanning an outage reads as a cliff; there is no rate, and
                # the interval is not a restart (the counters may have reset during the gap)
                rt = rates.Rates(None, None, None, None, reset=False,
                                 reason=f"gap: {down} down probe(s) in the interval")
            else:
                prev_counters = {k: prev[k] for k in rows.COUNTER_COLUMNS}
                rt = rates.rates(prev_counters, r.counters, now - prev["t"], backend,
                                 prev["since"], r.probe["since"])
            if rt.reset:
                _event(conn, now, endpoint_id, "restarted", rt.reason)
    return probe_id


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


def _log(msg: str) -> None:
    print(f"localagents monitor: {msg}", file=sys.stderr, flush=True)


async def probe_port(host: str, port: int = 22, timeout_s: float = 2.0) -> str:
    """The node fallback probe (spec §3 "Node fallback probe"): asked for only when every
    endpoint on the host timed out — one TCP connect to the host's port 22. ``"answered"``
    on a completed connect, ``"refused"`` on a connection refusal or a reset — both answer,
    so the box is up (sshd is not, the port answers with a refusal, or something between
    here and it sends a RST; ruling 2026-10-29) — and ``"nothing"`` on a timeout or any
    other OSError (the box is off, or a firewall drops the packets). The connector is
    ``asyncio.open_connection`` read through the module global, so tests can inject a
    fake. Both refusal classes precede the OSError catch: they are OSError subclasses."""
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_s)
        writer.close()
        await writer.wait_closed()
        return "answered"
    except (ConnectionRefusedError, ConnectionResetError):
        return "refused"
    except (TimeoutError, OSError):
        return "nothing"


async def run(
    reg: Any,
    conn: sqlite3.Connection,
    endpoints: Iterable[Endpoint],
    stop: asyncio.Event,
    now: Callable[[], float] = time.time,
    retention_days: float = retention.RETENTION_DAYS,
    retention_every_s: float = 3600,
    window_s: float = state.ATTENTION_WINDOW_S,
    on_tick: Callable[[], None] | None = None,
) -> None:
    """Probe every endpoint until ``stop`` is set: one task per endpoint, all concurrent. Each
    task ticks, then sleeps ``state.interval_s`` (5 s within ``window_s`` of its newest up
    probe, 60 s dormant; the window is the attention window, ``monitor:``'s ``attention_hours``
    in hours, story 2.3). Any error — in the tick or between ticks (``Registry.probe`` already
    turns a failed probe into an ``up: False`` dict, so ticks raise only database errors;
    between ticks it was the cadence computation) — is one stderr line and a dormant back-off;
    the loop outlives it (ruling 2026-10-29). One more task prunes the monitor's tables at
    start and then every ``retention_every_s`` (``retention.prune_probes``,
    ``retention_days``). ``on_tick``, when given, is called after each endpoint's tick that
    wrote its rows (story 3.1: the web app's ``tick`` event, so the page's stream pushes a
    snapshot); an exception from it is one stderr line, never the loop's end."""

    async def prune() -> None:
        while not stop.is_set():
            try:
                retention.prune_probes(conn, days=retention_days, now=now())
            except Exception as e:  # noqa: BLE001 — a failed prune is retried next period
                _log(f"probe retention failed: {type(e).__name__}: {e}")
            await _sleep_or_stop(stop, retention_every_s)

    async def one(ep: Endpoint) -> None:
        kind = backend_kind(ep.backend)
        eid: int | None = None
        while not stop.is_set():
            seconds = state.DORMANT_S  # what any error sleeps before the next attempt
            try:
                if eid is None:
                    eid = endpoint_id(conn, ep)
                await tick(reg, conn, ep, eid, now(), kind)
                if on_tick is not None:
                    try:
                        on_tick()
                    except Exception as e:  # noqa: BLE001 — a listener never stops collection
                        _log(f"{ep.name}: on_tick failed: {type(e).__name__}: {e}")
                # inside the tick's guard (ruling 2026-10-29, review 2.3): nothing between
                # ticks may raise — a TypeError here once escaped the task and gather's
                # return_exceptions=False turned it into the process exiting. The cost of
                # the single guard: a failed tick costs one dormant sleep instead of
                # re-reading the cadence — the loop surviving every error matters more.
                seconds = state.interval_s(last_up_t(conn, eid), now(), window_s)
            except Exception as e:  # noqa: BLE001 — the loop outlives any error, in the
                # tick or between ticks (CancelledError is a BaseException: it passes)
                _log(f"{ep.name}: tick failed: {type(e).__name__}: {e}")
            await _sleep_or_stop(stop, seconds)

    await asyncio.gather(prune(), *(one(ep) for ep in endpoints))
