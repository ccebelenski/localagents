"""The collection loop (monitor spec §3 "Collection loop", §6): probe every endpoint, write one
``probes`` row per probe (a failed probe is a row with its sentence, never a gap), record
``endpoint_events`` on up/down transitions and restarts, and ask the port-22 node fallback
(spec §3 "Node fallback probe") when every endpoint of a host has timed out, storing the
answer as a ``host_probes`` row (schema version 5) so a restart reconstructs the node state
from rows.

Everything runs on the event loop thread with one connection: each tick's database work is a
single ``immediate()`` block with no ``await`` inside, so ticks of different endpoints never
interleave inside a transaction.
"""

from __future__ import annotations

import asyncio
import socket
import sqlite3
import sys
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .. import db
from ..registry import Endpoint, backend_kind, fetch_metrics_doc
from . import rates, retention, rows, state

# The rule (2026-10-29): the fallback is asked at most once per host per minute.
HOST_PROBE_INTERVAL_S = 60


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
    happened before the outage), so the plain post-gap tick writes no rate and no restarted
    event (a gap tick that is also a down -> up transition still writes that transition event
    — the gap test asserts it). When the counters themselves say the engine restarted across
    the gap (a backwards counter, or a changed since), the gap tick writes that restart with
    its reason suffixed by `` (during a gap)`` and still no rate (ruling A, 2026-10-29). The
    first probe of an endpoint writes no event (a cold start is not a transition).

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
                # A rate spanning an outage reads as a cliff: the interval has no rate. But
                # the counters themselves may say the engine restarted across the gap (an
                # engine crash is exactly a down probe plus zeroed counters), so a gap-spanning
                # reset is still recorded — with its timing qualified — and the interval keeps
                # the gap's Rates (ruling A, 2026-10-29).
                prev_counters = {k: prev[k] for k in rows.COUNTER_COLUMNS}
                reset = rates.reset_reason(prev_counters, r.counters, prev["since"], r.probe["since"])
                if reset is not None:
                    _event(conn, now, endpoint_id, "restarted", reset + " (during a gap)")
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
    other OSError (the box is off, or a firewall drops the packets). A name that does not
    resolve re-raises the ``socket.gaierror``: it is not an answer (ruling 2026-10-29, review
    3.1b — the box may be fine and the name wrong), so the ask fails loudly instead of
    answering ``"nothing"``. The connector is ``asyncio.open_connection`` read through the
    module global, so tests can inject a fake. Both refusal classes precede the OSError catch:
    they are OSError subclasses."""
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_s)
        writer.close()
        await writer.wait_closed()
        return "answered"
    except (ConnectionRefusedError, ConnectionResetError):
        return "refused"
    except socket.gaierror:
        raise
    except (TimeoutError, OSError):
        return "nothing"


_NEWEST_PROBE_HOST = "SELECT host FROM probes WHERE endpoint_id = ? ORDER BY t DESC, id DESC LIMIT 1"
_NEWEST_HOST_PROBE = "SELECT t FROM host_probes WHERE host = ? ORDER BY t DESC, id DESC LIMIT 1"
_INSERT_HOST_PROBE = "INSERT INTO host_probes (t, host, address, answer) VALUES (?, ?, ?, ?)"
_PROBE_HOST_BY_ID = "SELECT host FROM probes WHERE id = ?"


async def maybe_probe_host(conn: sqlite3.Connection, eps: list[Endpoint], now: float) -> None:
    """The node fallback ask (spec §3 "Node fallback probe", the rule 2026-10-29): one TCP
    connect to port 22 on the host, stored as a ``host_probes`` row. Asked only when every
    endpoint on the host has its newest probe timed out (``host = "unknown"``, ``up = 0``)
    and the host's newest ``host_probes`` row is at least ``HOST_PROBE_INTERVAL_S`` old (or
    absent); never when any endpoint answered or refused (a refusal already proves the box is
    up). An endpoint without a probe row yet does not count as timed out: the ask waits for
    its first tick.

    The address is the first endpoint's ``base_url`` hostname (config order). A name there
    that does not resolve is one stderr line and no row (ruling 2026-10-29, review 3.1b): the
    box may be fine and the name wrong, so a failed resolution is a failed ask, never a
    ``"nothing"`` answer. The ``probe_port`` await is outside any transaction; the insert is
    one ``immediate()`` that re-checks the recency under the write lock. The round's bound on
    connects is endpoints-per-host: every endpoint task of the host (or a restart's first
    ticks) may get a connect in flight before any row lands — endpoints-per-host minus one
    of them duplicates (measured by the 3.1b review, pinned by the three-endpoint test). The
    one-row invariant the re-check holds: at most one ``host_probes`` row per host per
    minute."""
    host = eps[0].host
    for ep in eps:
        row = conn.execute(_NEWEST_PROBE_HOST, (endpoint_id(conn, ep),)).fetchone()
        if row is None or row[0] != "unknown":
            return
    last = conn.execute(_NEWEST_HOST_PROBE, (host,)).fetchone()
    if last is not None and now - last[0] < HOST_PROBE_INTERVAL_S:
        return
    address = urlsplit(eps[0].base_url).hostname
    try:
        answer = await probe_port(address)
    except socket.gaierror as e:
        _log(f"{eps[0].name}: host probe failed: {type(e).__name__}: {e}")
        return
    with db.immediate(conn):
        last = conn.execute(_NEWEST_HOST_PROBE, (host,)).fetchone()
        if last is not None and now - last[0] < HOST_PROBE_INTERVAL_S:
            return
        conn.execute(_INSERT_HOST_PROBE, (now, host, address, answer))


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
    snapshot); an exception from it is one stderr line, never the loop's end. The node
    fallback (story 3.1b) rides the loop: after each endpoint's tick whose probe timed out
    (``host = "unknown"``), ``maybe_probe_host`` asks the port-22 connect for the host — at
    most once a minute, only while every endpoint on the host has its newest probe timed out
    — and stores the answer as a ``host_probes`` row; a failed ask is one stderr line and the
    loop goes on. ``endpoints`` is the registry's endpoints in config order (``serve`` passes
    ``list(reg.endpoints.values())``), which is the order the host's fallback address (its
    first endpoint's ``base_url`` hostname) is read in."""

    async def prune() -> None:
        while not stop.is_set():
            try:
                retention.prune_probes(conn, days=retention_days, now=now())
            except Exception as e:  # noqa: BLE001 — a failed prune is retried next period
                _log(f"probe retention failed: {type(e).__name__}: {e}")
            await _sleep_or_stop(stop, retention_every_s)

    eps = list(endpoints)
    # host -> its endpoints in config order: the node fallback is asked per host (story 3.1b)
    hosts: dict[str, list[Endpoint]] = {}
    for ep in eps:
        hosts.setdefault(ep.host, []).append(ep)

    async def one(ep: Endpoint) -> None:
        kind = backend_kind(ep.backend)
        eid: int | None = None
        while not stop.is_set():
            seconds = state.DORMANT_S  # what any error sleeps before the next attempt
            try:
                if eid is None:
                    eid = endpoint_id(conn, ep)
                probe_id = await tick(reg, conn, ep, eid, now(), kind)
                if on_tick is not None:
                    try:
                        on_tick()
                    except Exception as e:  # noqa: BLE001 — a listener never stops collection
                        _log(f"{ep.name}: on_tick failed: {type(e).__name__}: {e}")
                # the node fallback (story 3.1b): this probe timed out — ask the port-22
                # connect for the host (at most once a minute, stored as a host_probes row)
                host_class = conn.execute(_PROBE_HOST_BY_ID, (probe_id,)).fetchone()[0]
                if host_class == "unknown":
                    try:
                        await maybe_probe_host(conn, hosts[ep.host], now())
                    except Exception as e:  # noqa: BLE001 — a failed ask is one line; the
                        # loop goes on, as a failed tick does
                        _log(f"{ep.name}: host probe failed: {type(e).__name__}: {e}")
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

    await asyncio.gather(prune(), *(one(ep) for ep in eps))
