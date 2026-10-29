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


def _event(conn: sqlite3.Connection, t: float, endpoint_id: int, kind: str, detail: str | None) -> None:
    conn.execute("INSERT INTO endpoint_events (t, endpoint_id, kind, detail) VALUES (?, ?, ?, ?)",
                 (t, endpoint_id, kind, detail))


async def tick(reg: Any, conn: sqlite3.Connection, ep: Endpoint, endpoint_id: int, now: float, backend: str) -> int:
    """Probe ``ep`` once, write its rows, record events; returns the probe id.

    ``/metrics`` is fetched only when the probe is up (a down endpoint would time out a second
    time). Events, compared with the endpoint's previous rows: up -> down writes ``down`` with
    the failure sentence, down -> up writes ``up``; a counter that went backwards since the
    previous counters row (down probes in between are skipped) or a changed ``since`` writes
    ``restarted`` naming it. The first probe of an endpoint writes no event (a cold start is not
    a transition)."""
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
            prev_counters = {k: prev[k] for k in rows.COUNTER_COLUMNS}
            rt = rates.rates(prev_counters, r.counters, now - prev["t"], backend, prev["since"], r.probe["since"])
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
    on a completed connect, ``"refused"`` on a connection refusal (the box is up; sshd is
    not, or the port answers with a refusal), ``"nothing"`` on a timeout or any other
    OSError (the box is off, or a firewall drops the packets). The connector is
    ``asyncio.open_connection`` read through the module global, so tests can inject a fake."""
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_s)
        writer.close()
        return "answered"
    except ConnectionRefusedError:
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
) -> None:
    """Probe every endpoint until ``stop`` is set: one task per endpoint, all concurrent. Each
    task ticks, then sleeps ``state.interval_s`` (5 s within ``window_s`` of its newest up
    probe, 60 s dormant; the window is the attention window, ``monitor:``'s ``attention_hours``
    in hours, story 2.3). A failing tick (any exception: ``Registry.probe`` already turns a
    failed probe into an ``up: False`` dict, so these are database errors) is logged to stderr
    and the loop goes on. One more task prunes the monitor's tables at start and then every
    ``retention_every_s`` (``retention.prune_probes``, ``retention_days``)."""

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
            try:
                if eid is None:
                    eid = endpoint_id(conn, ep)
                await tick(reg, conn, ep, eid, now(), kind)
            except Exception as e:  # noqa: BLE001 — the loop outlives any one tick
                _log(f"{ep.name}: tick failed: {type(e).__name__}: {e}")
            # read apart from the tick: one failed tick must not drop a live endpoint to 60 s
            last_up = None
            try:
                last_up = last_up_t(conn, eid) if eid is not None else None
            except sqlite3.Error as e:
                _log(f"{ep.name}: reading the last up probe failed: {e}")
            await _sleep_or_stop(stop, state.interval_s(last_up, now(), window_s))

    await asyncio.gather(prune(), *(one(ep) for ep in endpoints))
