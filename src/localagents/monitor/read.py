"""The monitor's read model (story 3.1; JSON shapes: monitor spec §5a): pure functions over a
connection and ``now`` that build the bodies of the routes in ``app.py``. Nothing here writes:
an endpoint named in models.yaml that has no ``endpoints`` row yet is looked up, never created
(``collect.endpoint_id`` creates it on the first tick).

Every function runs on a worker thread (the routes call them through ``asyncio.to_thread`` with
a connection of their own), never on the event loop.
"""

from __future__ import annotations

import math
import sqlite3
from collections import deque
from typing import Any

from .. import db, derive, status
from ..registry import Endpoint, backend_kind
from . import jobs as monitor_jobs
from . import rates, state
from .rows import COUNTER_COLUMNS, HARDWARE_COLUMNS

EVENTS_LIMIT = 20  # the overview's newest endpoint events
TRANSCRIPT_LINES = 80  # job detail's transcript tail (job_log's default)
JOBS_LIMIT = 500  # /api/jobs: the newest jobs created in the range, at most this many
SERVER_LIVE_S = 60.0  # a server's heartbeat is fresh this long (status.collect's rule)
# story 3.2a row 5: the snapshot's running-job rows carry the status bar's display form of the
# tool text, on the page's wider row than the status line's 60 (status.MAX_ACTIVITY)
SNAPSHOT_ACTIVITY_LIMIT = 120
# The node fallback's answer is fresh this long (story 3.1b): the collector asks port 22 at
# most once a minute, so a row up to two ask-intervals old still says what the box is doing.
FALLBACK_FRESH_S = 120.0
_FALLBACK_ROW = (
    "SELECT answer, t FROM host_probes WHERE host = ? AND t >= ? ORDER BY t DESC, id DESC LIMIT 1"
)

# one probe with its counters and hardware rows (LEFT JOIN: either may be absent) and its model
_PROBE_SELECT = (
    "SELECT p.*, m.served_id AS model_served, c.probe_id AS has_counters, h.probe_id AS has_hardware, "
    + ", ".join("c." + k for k in COUNTER_COLUMNS) + ", "
    + ", ".join("h." + k for k in HARDWARE_COLUMNS)
    + " FROM probes p LEFT JOIN probe_counters c ON c.probe_id = p.id"
    " LEFT JOIN probe_hardware h ON h.probe_id = p.id"
    " LEFT JOIN models m ON m.id = p.model_id"
)

_NULL_RATES = {"prompt_tps": None, "gen_tps": None, "cached_tps": None, "spec_acceptance": None}

# story 3.7s: the backends whose collectors write probe_counters rows (the ones rates.py's
# COUNTER_COLUMNS describe); an ollama/other endpoint has no counters to watch, so its
# "last active" witness is the requests table.
COUNTER_BACKENDS = ("llamacpp", "vllm", "strata")

# The endpoint's counters rows, newest first: one indexed walk (probes_endpoint_t, the counters
# row by its primary key). Story 3.7-fold-2 (D5) moved ``_last_active`` off this walk onto the
# collector's stored witness, so it now serves ONE purpose: ``backfill_last_active`` pairs
# consecutive rows with rates.rates — the exact function the history's rate derivation uses —
# and stops at the first interval that advanced; the monitor pays it once per endpoint with no
# witness at its start (serve.backfill_at_start), and the seed pays it for its own world.
_WITNESS_BACKFILL_ROWS = (
    "SELECT p.t AS t, p.since AS since, "
    + ", ".join(f"c.{k} AS {k}" for k in COUNTER_COLUMNS)
    + " FROM probes p JOIN probe_counters c ON c.probe_id = p.id"
    " WHERE p.endpoint_id = ? ORDER BY p.t DESC, p.id DESC"
)

# the stored witness (story 3.7-fold-2 D5): written by collect.tick at the tick whose counters
# pair advanced; read as one value here
_LAST_ACTIVE_READ = "SELECT last_active FROM endpoints WHERE id = ?"

# the counterless backend's witness: the newest request row that ran on this endpoint's jobs
_NEWEST_REQUEST_SQL = (
    "SELECT MAX(r.t) FROM requests r JOIN jobs j ON j.id = r.job_id WHERE j.endpoint_id = ?"
)


def _walk_last_active(conn: sqlite3.Connection, endpoint_id: int, kind: str) -> float | None:
    """The 3.7s walk: newest-first over the endpoint's counters rows until an interval advanced.
    Story 3.7-fold-2 (D5) took this off the read path (the idle-endpoint walk cost the WHOLE
    retained history per snapshot — 745 ms measured over 7 days of 5 s probes); it stays for
    ``backfill_last_active``, which pays it once per endpoint with no witness (see there)."""
    newest = None  # the newer row of the interval being judged
    for row in conn.execute(_WITNESS_BACKFILL_ROWS, (endpoint_id,)):
        if newest is not None:
            rt = rates.rates(_counters(row), _counters(newest), newest["t"] - row["t"], kind,
                             row["since"], newest["since"])
            if not rt.reset and ((rt.gen_tps or 0) > 0 or (rt.prompt_tps or 0) > 0):
                return newest["t"]
        newest = row
    return None


def backfill_last_active(conn: sqlite3.Connection, only_null: bool = False) -> None:
    """The backfill of ``endpoints.last_active`` (story 3.7-fold-2 D5, moved out of the version-6
    migration by its follow-up: the MCP server never imports the monitor package, so the monitor
    runs this at its own start — ``serve.backfill_at_start`` — and the seed runs it on its own
    world). For every counter-backend endpoint, the newest advancing interval's ``t`` from its
    retained rows by the 3.7s pairing.

    The walked-and-found-nothing case is MEMOIZED, not left null: the endpoint gets ``0.0``, which
    ``_last_active`` reads as null — an epoch timestamp can never be 0, and the collector's first
    advancing tick overwrites it with a real ``t``. Without the memo an idle-for-a-week endpoint
    would pay its walk (the review measured 745 ms) at EVERY start, because ``last_active IS
    NULL`` says "never walked" and a walk to null walks again; with it, one walk per endpoint ever.

    ``only_null`` (the start's call) restricts the walk to endpoints with a NULL witness: an
    endpoint already holding a witness or the memo costs one SELECT row, no walk. The seed leaves
    it False (its rows are fresh either way). The counterless backends stay NULL — their witness
    is the requests table, read live — so the start re-lists them every time; the listing costs
    nothing (the backend check skips them before any walk). Runs inside the caller's transaction."""
    rows_ = conn.execute("SELECT id, backend FROM endpoints"
                         + (" WHERE last_active IS NULL" if only_null else "")).fetchall()
    for eid, backend in rows_:
        kind = backend_kind(backend)
        if kind not in COUNTER_BACKENDS:
            continue
        t = _walk_last_active(conn, eid, kind)
        conn.execute("UPDATE endpoints SET last_active = ? WHERE id = ?", (t if t is not None else 0.0, eid))


def _last_active(conn: sqlite3.Connection, endpoint_id: int | None, kind: str) -> float | None:
    """``last_active`` (story 3.7s, stored since 3.7-fold-2 D5): for a counter backend, the
    witness ``collect.tick`` wrote at its newest advancing counters interval — one statement,
    one row; an old file is backfilled at the monitor's start (``serve.backfill_at_start``),
    not by the version-6 migration, which adds the column only. Null when no witness was
    ever written (fresh endpoint, or nothing advanced within retention — the witness is not
    pruned with the probes). The counterless backends (outside ``COUNTER_BACKENDS``) are
    witnessed live by their newest request row's ``t``, else null."""
    if endpoint_id is None:
        return None
    if kind not in COUNTER_BACKENDS:
        row = conn.execute(_NEWEST_REQUEST_SQL, (endpoint_id,)).fetchone()
        return row[0] if row is not None else None
    row = conn.execute(_LAST_ACTIVE_READ, (endpoint_id,)).fetchone()
    t = row[0] if row is not None else None
    return t if t else None  # 0.0 = the backfill's walked-nothing memo: reads as no witness


# --- shared helpers --------------------------------------------------------------------------


def endpoint_row_id(conn: sqlite3.Connection, ep: Endpoint) -> int | None:
    """The ``endpoints`` row of a configured endpoint, keyed exactly as ``collect.endpoint_id``
    and ``JobStore.new`` key it (name, ``ep.url``, backend as configured, host); None when the
    endpoint was never probed nor ran a job. Read-only: never inserts."""
    row = conn.execute(
        "SELECT id FROM endpoints WHERE name = ? AND base_url = ? AND backend = ? AND host = ?",
        (ep.name, ep.url, ep.backend, ep.host),
    ).fetchone()
    return row[0] if row is not None else None


def _counters(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in COUNTER_COLUMNS}


def _pair_rates(prev: sqlite3.Row | None, cur: sqlite3.Row | None, kind: str) -> dict[str, Any]:
    """``rates.rates`` between two probe rows as the §5a ``rates`` object: the four values, and
    ``reason`` — None when they were computed, else the sentence saying why they are null."""
    if cur is None:
        return {**_NULL_RATES, "reason": "no probes"}
    if prev is None:
        return {**_NULL_RATES, "reason": "one probe: no interval yet"}
    if cur["has_counters"] is None:
        return {**_NULL_RATES, "reason": "the last probe has no counters"}
    if prev["has_counters"] is None:
        return {**_NULL_RATES, "reason": "the probe before the last has no counters"}
    rt = rates.rates(_counters(prev), _counters(cur), cur["t"] - prev["t"], kind, prev["since"], cur["since"])
    if rt.reset:
        return {**_NULL_RATES, "reason": rt.reason}
    return {"prompt_tps": rt.prompt_tps, "gen_tps": rt.gen_tps, "cached_tps": rt.cached_tps,
            "spec_acceptance": rt.spec_acceptance, "reason": None}


def _server_live(row: sqlite3.Row, now: float) -> bool:
    """The job's server is alive: not stopped, a heartbeat within 60 s, its pid alive (the
    status line's liveness rule, heartbeat first because pids are reused)."""
    if row["server_stopped"] is not None or row["server_updated"] is None or row["server_pid"] is None:
        return False
    return row["server_updated"] >= now - SERVER_LIVE_S and db.pid_alive(row["server_pid"])


# story 3.7g: the snapshot's jobs list is the page's Recent jobs log — live jobs first, then a
# capped tail of finished ones. Terminal is jobs.py's ``done`` check: every status that is not
# queued or running (succeeded, failed, cancelled, timeout, lost).
LIVE_STATUSES = ("queued", "running")
KEEP_FINISHED = 10  # the log's finished tail (the page's "then the last 10 finished" note)

# the live and finished SELECTs share everything but the WHERE/ORDER: two queries rather than
# one UNION because each side wants its own ordering and only the finished side carries a LIMIT
_JOB_SELECT = (
    "SELECT j.*, m.served_id AS model_served, e.name AS endpoint_name,"
    " s.pid AS server_pid, s.owner AS server_owner, s.updated AS server_updated, s.stopped AS server_stopped,"
    " s.version AS server_version"
    " FROM jobs j LEFT JOIN models m ON m.id = j.model_id"
    " LEFT JOIN endpoints e ON e.id = j.endpoint_id"
    " LEFT JOIN mcp_servers s ON s.id = j.mcp_server_id"
)
_LIVE_TAIL = (" WHERE j.status IN ('queued', 'running')"
              " ORDER BY COALESCE(j.started_at, j.created_at), j.rowid")
_FINISHED_TAIL = (" WHERE j.status NOT IN ('queued', 'running')"
                  " ORDER BY j.finished_at DESC, j.rowid DESC LIMIT ?")

# story 3.10b: the one window predicate — the job spans ``[since_t, now]`` (started at or before
# the window's end, unfinished or finished at or after its start; a job that never started has
# no span to overlap). Factored out of ``jobs_overlapping`` so the history's job SELECTs below
# answer the same question; the params are always ``(now, since_t)``.
_OVERLAP_WHERE = (" j.started_at IS NOT NULL AND j.started_at <= ?"
                  " AND (j.finished_at IS NULL OR j.finished_at >= ?)")

# the history body's job listing (story 3.10b): this endpoint's §5a job rows overlapping the
# range, in the overview's log order — live by start, then the newest finished first. No LIMIT:
# the range bounds the count.
_ENDPOINT_JOBS_LIVE_TAIL = (" WHERE j.endpoint_id = ? AND j.status IN ('queued', 'running') AND"
                            + _OVERLAP_WHERE + " ORDER BY COALESCE(j.started_at, j.created_at), j.rowid")
_ENDPOINT_JOBS_FINISHED_TAIL = (" WHERE j.endpoint_id = ? AND j.status NOT IN ('queued', 'running') AND"
                                + _OVERLAP_WHERE + " ORDER BY j.finished_at DESC, j.rowid DESC")

# story 3.7-fold-4s: a finished row's derived figures are computed once per process. The store's
# write rules make them static: run_job's finally sets ``finished_at`` and writes the row with the
# job's LAST ``changed()`` (runner.py's tail), and record_request, record_tool_call and
# record_tool_result (jobs.py) all run in the message loop before that UPDATE — after the terminal
# UPDATE nothing writes the job's requests or tool_calls rows, and the row itself is final
# (``mark_lost_jobs``, jobs.py, is the one cross-server write and it only touches live rows, in
# the same transaction that stamps them terminal). ``server_live`` is the exception that leaves
# the cache: the job's ``mcp_servers`` row beats on after the job ends, so ``_recent_jobs``
# recomputes it each push from the joined server columns (already in the row, no statement).
# The key is ``(id, finished_at, updated)``: ids are uuid primary keys, so in a real process the
# id alone identifies the row; ``finished_at`` and ``updated`` are the row's finality stamps —
# static by the rules above — and they keep a scratch database reusing a fixed id in tests from
# answering from an earlier scratch database's figures.
_FINISHED_CACHE: dict[tuple[str, float | None, float | None], dict[str, Any]] = {}


def _job_entry(conn: sqlite3.Connection, j: sqlite3.Row, now: float, frozen: bool) -> dict[str, Any]:
    """One snapshot job row (spec §5a ``running_job``): the status line's entry
    (status.collect) plus id, elapsed, errors and the last tool. ``frozen`` marks a
    finished row: its ``elapsed_s`` reads finished_at - started_at, the run's length, so the
    figure stops moving; a finished row whose ``finished_at`` is NULL (the schema allows it, no
    write path produces one) has ``elapsed_s`` null — no end is known, and a finished row never
    shows a moving number (story 3.7-fold-4s). Every derived figure reads the job's rows as-is
    (static once the job is done). Not limited to live servers: ``server_live`` says whether its
    server still beats."""
    jid = j["id"]
    start = j["started_at"] or j["created_at"]
    if frozen and j["finished_at"] is None:
        elapsed = None
    else:
        clock = j["finished_at"] if frozen else now
        elapsed = round(clock - start, 1) if start is not None else None
    ctx = derive.ctx_tokens(conn, jid)
    tps = derive.gen_tps(conn, jid)
    prefill = derive.prefill_tps(conn, jid)
    # row 5 (3.2 review): the row carries the DISPLAY form the status bar shows — one line,
    # relative to the job's root (1.11b's rule; status.py is stdlib-only, so the monitor may
    # import it). The raw text stays on the job detail and the /api/jobs summaries.
    root = status._job_root(j)
    return {
        "id": jid,  # 3.10f-2: the id is the job's one key; the pages show its first eight chars
        "model": j["model_served"],
        "endpoint": j["endpoint_name"],
        "status": j["status"],
        # router 8: the page marks a calibration probe (`cal`) and the count below excludes it
        "calibration": bool(j["calibration"]),
        "started_at": start,
        "finished_at": j["finished_at"],
        "elapsed_s": elapsed,
        "num_turns": j["num_turns"],
        "tool_calls": derive.tool_call_count(conn, jid),
        "errors": derive.error_count(conn, jid),
        "last_tool": status.display_activity(derive.last_tool(conn, jid), root, limit=SNAPSHOT_ACTIVITY_LIMIT),
        "last_activity": status.display_activity(j["last_activity"], root, limit=SNAPSHOT_ACTIVITY_LIMIT),
        "ctx_tokens": ctx,
        "context": j["context"],
        "ctx_pct": round(100 * ctx / j["context"]) if ctx and j["context"] else None,
        "gen_tps": round(tps, 1) if tps else None,
        "prefill_tps": round(prefill, 1) if prefill else None,
        "compactions": j["compactions"],
        "files": len(derive.files_touched(conn, jid)),
        "owner": j["server_owner"],
        # story 1.14: the build stamp of the server that ran the job (static: written once at
        # that server's start, so a cached finished entry keeps it)
        "server_version": j["server_version"],
        "server_live": _server_live(j, now),
    }


def _recent_jobs(conn: sqlite3.Connection, now: float,
                 keep_finished: int = KEEP_FINISHED) -> list[tuple[int | None, dict[str, Any]]]:
    """The snapshot's jobs list (story 3.7g): every queued or running job oldest first, then the
    last ``keep_finished`` finished jobs (any terminal status) newest-finished first — the page's
    Recent jobs log. Each entry is paired with the job's ``endpoint_id`` (the row, not the name:
    two configs may share an endpoint name).

    Story 3.7-fold-4s: finished entries come from the module-level ``_FINISHED_CACHE`` (per
    process; see there for the write rules that make the figures static) — a job whose status
    moves live-to-finished enters the cache on its first finished push, and a finished row costs
    no derive statements once cached. Live rows are never cached. After the finished loop every
    cached entry whose id left the current tail is evicted, so the dict holds at most
    ``keep_finished`` entries."""
    out = []
    for j in conn.execute(_JOB_SELECT + _LIVE_TAIL):
        out.append((j["endpoint_id"], _job_entry(conn, j, now, frozen=False)))
    tail_ids = set()
    for j in conn.execute(_JOB_SELECT + _FINISHED_TAIL, (keep_finished,)):
        tail_ids.add(j["id"])
        key = (j["id"], j["finished_at"], j["updated"])
        cached = _FINISHED_CACHE.get(key)
        if cached is None:
            entry = _job_entry(conn, j, now, frozen=True)
            kept = dict(entry)
            kept.pop("server_live")  # the server's heartbeat can move after the job: recomputed
            _FINISHED_CACHE[key] = kept
        else:
            entry = dict(cached)  # a fresh dict per push: the caller owns what overview returns
            entry["server_live"] = _server_live(j, now)
        out.append((j["endpoint_id"], entry))
    # evict ids that left the tail; pop's default because worker threads share the dict and a
    # concurrent push may have evicted the same key first
    for key in [k for k in _FINISHED_CACHE if k[0] not in tail_ids]:
        _FINISHED_CACHE.pop(key, None)
    return out


# --- overview ----------------------------------------------------------------------------------


def _endpoint(conn: sqlite3.Connection, ep: Endpoint, eid: int | None, now: float, window_s: float,
              running: list[tuple[int | None, dict[str, Any]]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """One endpoint's overview entry, and its last probe's ``{"up", "host"}`` for the node state
    (None when it has no probe)."""
    last2 = (conn.execute(_PROBE_SELECT + " WHERE p.endpoint_id = ? ORDER BY p.t DESC, p.id DESC LIMIT 2",
                          (eid,)).fetchall() if eid is not None else [])
    last = last2[0] if last2 else None
    prev = last2[1] if len(last2) > 1 else None
    # story 3.2a row 3: not MAX(t) — the newest UP probe whole, so a down or dormant endpoint
    # can name what it last served (3.3's gap: the card showed nothing after the last probe's
    # gauges went null)
    last_up_row = (conn.execute(_PROBE_SELECT + " WHERE p.endpoint_id = ? AND p.up = 1"
                                " ORDER BY p.t DESC, p.id DESC LIMIT 1", (eid,)).fetchone()
                   if eid is not None else None)
    last_up = last_up_row["t"] if last_up_row is not None else None
    st = state.endpoint_state(last, last_up, now, window_s)
    dropped_id = None
    if st == "down":
        first_down = conn.execute(
            "SELECT MIN(t) FROM probes WHERE endpoint_id = ? AND up = 0 AND t > ?", (eid, last_up)).fetchone()[0]
        if first_down is not None:
            job = monitor_jobs.running_job_at(conn, eid, first_down)
            if job is not None:  # row 3: the sentence clicks through to the job by its id
                dropped_id = job["id"]
    known = last_up_row is not None and st in ("down", "dormant")
    up = bool(last["up"]) if last is not None else False
    entry = {
        "name": ep.name,
        "backend": ep.backend,
        "base_url": ep.url,
        "host": ep.host,  # row 3: which card this endpoint sits under, for a page that re-sorts
        "state": st,
        "last_seen": last_up,
        # story 3.7s: when the engine itself last did work (its own counters), not when we
        # last saw it; null when the retained history holds no advancing interval
        "last_active": _last_active(conn, eid, backend_kind(ep.backend)),
        "model": last["model_served"] if last is not None else None,
        "context": last["context"] if last is not None else None,
        "probe_state": last["state"] if last is not None else None,  # row 3: idle/busy, null on a down probe
        "last_model": last_up_row["model_served"] if known else None,
        "last_context": last_up_row["context"] if known else None,
        "slots": ({"total": last["slots_total"], "busy": last["slots_busy"]}
                  if last is not None and last["slots_total"] is not None else None),
        "requests": ({"running": last["requests_running"], "waiting": last["requests_waiting"]}
                     if last is not None and (last["requests_running"] is not None
                                              or last["requests_waiting"] is not None) else None),
        "kv_cache_usage": last["kv_cache_usage"] if last is not None else None,
        "latency_ms": last["latency_ms"] if last is not None else None,
        "since": last["since"] if last is not None else None,
        "error": last["error"] if last is not None and not up else None,
        "dropped_job_id": dropped_id,
        "rates": _pair_rates(prev, last, backend_kind(ep.backend)),
        "gen_tps_live": last["gen_tps"] if last is not None else None,
        "hardware": ({k: last[k] for k in HARDWARE_COLUMNS}
                     if last is not None and last["has_hardware"] is not None else None),
        # router 8: jobs_running is a traffic figure, so a calibration probe never counts
        "jobs_running": sum(1 for job_eid, job in running
                            if eid is not None and job_eid == eid and not job.get("calibration")),
    }
    node_input = {"up": up, "host": last["host"]} if last is not None else None
    return entry, node_input


def _fallback(conn: sqlite3.Connection, host: str, now: float) -> tuple[str | None, float | None]:
    """The host's node-fallback answer for the overview (story 3.1b): the newest
    ``host_probes`` row within ``FALLBACK_FRESH_S`` of ``now`` — ``(answer, t)``, or
    ``(None, None)`` when there is none, so an all-timeout node stays ``"unknown"``."""
    row = conn.execute(_FALLBACK_ROW, (host, now - FALLBACK_FRESH_S)).fetchone()
    if row is None:
        return None, None
    return row["answer"], row["t"]


def overview(conn: sqlite3.Connection, reg: Any, now: float, window_s: float,
             db_path: str | None = None, *, refresh_s: float = 5.0,
             build: str | None = None) -> dict[str, Any]:
    """The SSE snapshot (spec §5 "Overview"; shape §5a): hosts in models.yaml order (endpoints
    grouped by ``host``, config order within), each with its node state (resolved from the
    newest fresh port-22 fallback row, story 3.1b), its fallback fields and endpoints; the
    recent jobs (story 3.7g: queued and running first, then the last ``KEEP_FINISHED``
    finished); the newest ``EVENTS_LIMIT`` endpoint events. ``db_path`` (story 3.2a row 3) is
    the database file this server opened, passed in by the route (serve knows it: ``--db``, the
    ``monitor:`` section's db, else ``db.db_path()``); null when the app was built without one.
    ``refresh_s`` (story 3.7m) is the fan-out's clock period, carried to the page so it can say
    how often it is told. ``build`` (story 1.14) is the monitor's own build stamp, computed once
    at the app's start (``db.build_stamp``), carried so the job page can say when a job's server
    ran other code; null when the app was built without one. An endpoint in the database but
    not in the config is ignored (its events too)."""
    recent = _recent_jobs(conn, now)
    # the cards' ``jobs_running`` stays a live count (story 3.7e's contract: the card's jobs
    # block is running rows); the log tail is only for the snapshot's ``jobs`` list (3.7g)
    live = [(eid, j) for eid, j in recent if j["status"] in LIVE_STATUSES]
    groups: dict[str, list[Endpoint]] = {}
    for ep in reg.endpoints.values():
        groups.setdefault(ep.host, []).append(ep)
    hosts = []
    configured_ids = []
    for host, eps in groups.items():
        entries, nodes = [], []
        for ep in eps:
            eid = endpoint_row_id(conn, ep)
            if eid is not None:
                configured_ids.append(eid)
            entry, node_input = _endpoint(conn, ep, eid, now, window_s, live)
            entries.append(entry)
            if node_input is not None:
                nodes.append(node_input)
        # the node fallback (story 3.1b): the host's newest host_probes answer within
        # FALLBACK_FRESH_S of now resolves an all-timeout node (spec §4); else unknown stays
        fallback, fallback_t = _fallback(conn, host, now)
        hosts.append({
            "name": host,
            "state": state.node_state(nodes, fallback),
            "dormant": state.node_dormant([e["state"] for e in entries]),
            "fallback": fallback,
            "fallback_t": fallback_t,
            "endpoints": entries,
        })
    events = []
    if configured_ids:
        marks = ", ".join("?" * len(configured_ids))
        events = [
            {"t": r["t"], "endpoint": r["name"], "kind": r["kind"], "detail": r["detail"]}
            for r in conn.execute(
                "SELECT ev.t, e.name, ev.kind, ev.detail FROM endpoint_events ev"
                f" JOIN endpoints e ON e.id = ev.endpoint_id WHERE ev.endpoint_id IN ({marks})"
                " ORDER BY ev.t DESC, ev.id DESC LIMIT ?",
                (*configured_ids, EVENTS_LIMIT),
            )
        ]
    return {"now": now, "window_s": window_s, "refresh_s": refresh_s, "db_path": db_path, "build": build,
            "hosts": hosts, "jobs": [j for _, j in recent], "events": events}


# --- endpoint history --------------------------------------------------------------------------

_GAUGES = ("slots_busy", "requests_running", "requests_waiting", "kv_cache_usage")
# ruling B (review of 3.1): every hardware column but ram_total (a denominator, not a series)
_HW_SERIES = ("gpu_util", "gpu_mem_used", "gpu_temp", "gpu_power",
              "gpu_pcie_rx_mb", "gpu_pcie_tx_mb", "cpu", "ram_used", "disk_read_mb", "disk_write_mb")
# the probe's own tier-0 figures, served as kept-probe series (state holds strings, not numbers)
_PROBE_SERIES = ("latency_ms", "state")
# a rate series exists when the counter it is computed from holds a value in the range
_RATE_SOURCES = {
    "prompt_tps": ("prompt_tokens",),
    "gen_tps": ("generated_tokens",),
    "cache_hit": ("prompt_tokens_cached", "prefix_hits"),
}


def _interval_ok(prev: sqlite3.Row, cur: sqlite3.Row) -> bool:
    """A raw interval between consecutive probes has a rate: both carry counters (a probe
    without, a down one, is a gap) and nothing reset between them. Review 3.9C R1: the bounded
    read reproduces this rule in SQL (``_history_spans_sql``); it stays for the one interval the
    SQL window cannot see — the row before the range paired with the first in-range row."""
    if prev["has_counters"] is None or cur["has_counters"] is None:
        return False
    return rates.reset_reason(_counters(prev), _counters(cur), prev["since"], cur["since"]) is None


def _source_column(col: str) -> str:
    """The qualified probe column a history series' presence is read from."""
    if col in COUNTER_COLUMNS:
        return "c." + col
    if col in HARDWARE_COLUMNS:
        return "h." + col
    return "p." + col


# every column whose presence keeps a series in the body (spec §5: "a tile with no source is
# absent, not empty"): the rate sources, the gauges, the probe's own figures and the hardware
_HISTORY_SOURCES = sorted({c for cols in _RATE_SOURCES.values() for c in cols}
                          | set(_GAUGES) | set(_PROBE_SERIES) | set(_HW_SERIES))


def _history_counts_sql() -> str:
    """The range's row count and each series source's presence, in one row: ``COUNT(*)`` is
    ``n`` (the downsampling needs it) and each ``COUNT(column)`` is the old "any value in the
    range" rule the absent-series decision takes (spec §5: "a tile with no source is absent,
    not empty"). Returns one row whatever the range holds."""
    counts = ", ".join(f"COUNT({_source_column(col)})" for col in _HISTORY_SOURCES)
    return ("SELECT COUNT(*), " + counts + " FROM probes p"
            " LEFT JOIN probe_counters c ON c.probe_id = p.id"
            " LEFT JOIN probe_hardware h ON h.probe_id = p.id"
            " WHERE p.endpoint_id = ? AND p.t >= ? AND p.t <= ?")


def _history_spans_sql(k: int, n: int) -> str:
    """The kept points and their gap counts (review 3.9C R1), one narrow statement.

    Rows are numbered oldest first (``rn``), so the kept rows are the newest and every ``k``-th
    back (``(n - rn) % k == 0``). Each row belongs to the span of the kept point that follows
    it: ``grp = (rn - rn_min + k - 1) / k`` with ``rn_min = 1 + (n - 1) % k`` the oldest kept
    row, so span ``j`` holds the raw intervals between kept point ``j - 1`` and kept point
    ``j``. ``is_bad`` reproduces ``_interval_ok`` in SQL (either side without a counters row, a
    counter down on both sides, or ``since`` changed), and ``SUM(is_bad)`` per span is what the
    old prefix comparison
    (``bad[cur] == bad[previous kept]``) reduces to. ``MAX(rn)`` names the kept row itself, the
    bare ``id`` taking that row's value (SQLite's single-max aggregate rule). One row per kept
    point — at most ``max_points`` — however many rows the range holds."""
    rn_min = 1 + (n - 1) % k
    counters = ", ".join("c." + col for col in COUNTER_COLUMNS)
    # the previous row's counters come from one primary-key join on its probe id, not ten LAGs
    bad = " OR ".join(
        f"(r.{col} IS NOT NULL AND pc2.{col} IS NOT NULL AND r.{col} < pc2.{col})"
        for col in COUNTER_COLUMNS)
    return (
        "WITH r AS (SELECT p.id AS id, p.t AS t, p.since AS since,"
        " c.probe_id AS has_counters, " + counters
        + ", ROW_NUMBER() OVER w AS rn, LAG(c.probe_id) OVER w AS prev_probe_id,"
        " LAG(p.since) OVER w AS prev_since"
        " FROM probes p LEFT JOIN probe_counters c ON c.probe_id = p.id"
        " WHERE p.endpoint_id = ? AND p.t >= ? AND p.t <= ?"
        " WINDOW w AS (ORDER BY p.t, p.id)),"
        " b AS (SELECT r.*, CASE WHEN rn = 1 THEN 0"
        " WHEN has_counters IS NULL OR prev_probe_id IS NULL THEN 1"
        f" WHEN {bad} OR (since IS NOT NULL AND prev_since IS NOT NULL AND since <> prev_since)"
        " THEN 1 ELSE 0 END AS is_bad"
        " FROM r LEFT JOIN probe_counters pc2 ON pc2.probe_id = r.prev_probe_id)"
        f" SELECT (rn - {rn_min} + {k} - 1) / {k} AS grp, SUM(is_bad) AS bad_count,"
        " MAX(rn) AS rn, id FROM b GROUP BY grp ORDER BY rn"
    )


def _history_kept_sql(count: int) -> str:
    """The full rows of ``count`` kept probes, by id: probes, counters, hardware and the model
    one indexed lookup each, so the wide read is O(points) too."""
    return ("SELECT p.*, m.served_id AS model_served, c.probe_id AS has_counters,"
            " h.probe_id AS has_hardware, "
            + ", ".join("c." + col for col in COUNTER_COLUMNS) + ", "
            + ", ".join("h." + col for col in HARDWARE_COLUMNS)
            + " FROM probes p LEFT JOIN probe_counters c ON c.probe_id = p.id"
            " LEFT JOIN probe_hardware h ON h.probe_id = p.id"
            " LEFT JOIN models m ON m.id = p.model_id"
            f" WHERE p.id IN ({', '.join('?' * count)}) ORDER BY p.t, p.id")


def endpoint_history(
    conn: sqlite3.Connection,
    endpoint_id: int | None,
    since_t: float,
    now: float,
    max_points: int = 600,
) -> dict[str, Any]:
    """The endpoint's series over ``[since_t, now]`` (spec §5 "Endpoint detail"; shape §5a).

    One point per probe; when the range holds more than ``max_points`` probes, every k-th
    (``k = ceil(n / max_points)``, counted back from the newest, which is always kept). A
    point's rates are over the interval from the previous kept point (for the earliest kept
    point: the probe before the range, or the range's oldest row), through ``rates.rates``;
    null when any raw interval inside it is a gap or a reset. A series whose source holds no
    value in the range is ABSENT (spec §5: "a tile with no source is absent, not empty").
    ``endpoint_id`` None (configured, never probed) is the empty history.

    Review 3.9C R1: the rows read are bounded by the points answered. The old path loaded every
    in-range probe (120,961 rows for 599 points, 861 ms on the route) and downsampled in
    Python; ``_history_spans_sql`` names the kept points and their gap counts, and
    ``_history_kept_sql`` fetches those rows by id. The body is byte-identical to the old
    algorithm's."""
    out: dict[str, Any] = {"t": [], "step": 1}
    if endpoint_id is None:
        out.update(events=[], jobs=[])
        return out
    b = conn.execute("SELECT backend FROM endpoints WHERE id = ?", (endpoint_id,)).fetchone()
    kind = backend_kind(b[0]) if b is not None else "other"
    pre = conn.execute(_PROBE_SELECT + " WHERE p.endpoint_id = ? AND p.t < ? ORDER BY p.t DESC, p.id DESC LIMIT 1",
                       (endpoint_id, since_t)).fetchone()
    # one row: n and the absent-series presence flags, over the whole range
    counts = conn.execute(_history_counts_sql(), (endpoint_id, since_t, now)).fetchone()
    n = counts[0]
    present = {col: bool(counts[i + 1]) for i, col in enumerate(_HISTORY_SOURCES)}
    k = max(1, math.ceil(n / max_points)) if n else 1
    out["step"] = k
    # the kept points' gap counts (one row per point), then the kept rows themselves by id:
    # O(points) in both statements, however many rows the range holds
    spans = conn.execute(_history_spans_sql(k, n), (endpoint_id, since_t, now)).fetchall() if n else []
    kept = (conn.execute(_history_kept_sql(len(spans)), [row["id"] for row in spans]).fetchall()
            if spans else [])
    span_by_id = {row["id"]: row for row in spans}
    first = (conn.execute(_PROBE_SELECT + " WHERE p.endpoint_id = ? AND p.t >= ? AND p.t <= ?"
                          " ORDER BY p.t, p.id LIMIT 1", (endpoint_id, since_t, now)).fetchone()
             if n else None)
    out["t"] = [row["t"] for row in kept]
    offset = 1 if pre is not None else 0
    # the earliest kept point's reference is the row the old walk clamped to: index 0 of
    # [pre] + in-range, i.e. pre, or the range's oldest row when there is no pre. bad[0] is 0,
    # so its rate also needs no gap or reset anywhere from the range's start to it — including
    # the pre -> first-in-range interval, which is the one interval the SQL window cannot see.
    bad_first = 0 if pre is None or n == 0 else (0 if _interval_ok(pre, first) else 1)
    series: dict[str, list] = {key: [] for key in (*_RATE_SOURCES, *_GAUGES, *_PROBE_SERIES, *_HW_SERIES)}
    for idx, cur in enumerate(kept):
        span = span_by_id[cur["id"]]
        if idx == 0:
            ref = pre if pre is not None else first
            ok = offset + span["rn"] - 1 > 0 and span["bad_count"] == 0 and bad_first == 0
        else:
            ref = kept[idx - 1]  # the previous kept point (k raw rows back)
            ok = span["bad_count"] == 0  # its span holds no gap or reset
        r = None
        if ok:
            r = rates.rates(_counters(ref), _counters(cur), cur["t"] - ref["t"], kind,
                            ref["since"], cur["since"])
        series["prompt_tps"].append(r.prompt_tps if r else None)
        series["gen_tps"].append(r.gen_tps if r else None)
        hit = None
        if r and r.cached_tps is not None and r.prompt_tps is not None and r.prompt_tps + r.cached_tps > 0:
            hit = r.cached_tps / (r.prompt_tps + r.cached_tps)
        series["cache_hit"].append(hit)
        for g in (*_GAUGES, *_PROBE_SERIES):
            series[g].append(cur[g])
        for h in _HW_SERIES:
            series[h].append(cur[h])
    for key, values in series.items():
        sources = _RATE_SOURCES.get(key, (key,))
        # present is the same "any value in the range" count the old walk took over in_range
        if kept and any(present[c] for c in sources):
            out[key] = values
    out["events"] = [
        {"t": r["t"], "kind": r["kind"], "detail": r["detail"]}
        for r in conn.execute(
            "SELECT t, kind, detail FROM endpoint_events WHERE endpoint_id = ? AND t >= ? AND t <= ?"
            " ORDER BY t, id", (endpoint_id, since_t, now))
    ]
    # story 3.10b: full §5a job rows (the five-key listing is gone — the endpoint page draws
    # these through the overview's jobs table). Same SELECT and derivation the snapshot uses;
    # ``frozen=True`` gives a finished row the run's length, never a moving figure. These rows
    # do NOT join ``_FINISHED_CACHE``: that cache's eviction is keyed to the overview's last-10
    # tail, so it would drop a history row between calls (recomputing it anyway) and could be
    # starved by a long range.
    jobs = [_job_entry(conn, j, now, frozen=False)
            for j in conn.execute(_JOB_SELECT + _ENDPOINT_JOBS_LIVE_TAIL, (endpoint_id, now, since_t))]
    jobs += [_job_entry(conn, j, now, frozen=True)
             for j in conn.execute(_JOB_SELECT + _ENDPOINT_JOBS_FINISHED_TAIL, (endpoint_id, now, since_t))]
    out["jobs"] = jobs
    return out


# --- fleet history -----------------------------------------------------------------------------


def fleet_history(conn: sqlite3.Connection, reg: Any, since_t: float, now: float,
                  max_points: int = 600,
                  histories: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """The fleet's speed and occupancy on ONE time grid (review of 3.1, ruling C; shape §5a).

    Each configured endpoint with at least one probe in the range is downsampled by its own
    ``endpoint_history`` rules, and the grid is the union of their kept times — itself kept to
    ``max_points`` by the same every-kth-from-the-newest rule (``step`` is that factor; 1 when
    the union already fits). A series value at a grid time is the endpoint's value at its own
    kept probe nearest that time, and null when no kept probe lies within half a grid step: an
    endpoint that never probes at a grid time (or whose probes fall between the downsampled
    grid's points) reads null, never another endpoint's figure. An endpoint's own absent
    series (null throughout the range) stays absent: it is served as all-null on the shared
    grid. ``occupancy`` is ``slots_busy`` for a backend that reports slots, else
    ``requests_running`` (vLLM). An endpoint with no probe in the range is left out entirely.
    ``histories`` (story 3.7p) lets a caller that has already read an endpoint's history for
    this range hand it in instead of paying for the same read twice; an empty ``t`` means the
    endpoint has no probe in the range, so it is left out exactly as the check below would."""
    per: list[tuple[str, list, dict[str, list | None]]] = []
    for ep in reg.endpoints.values():
        h = histories.get(ep.name) if histories is not None else None
        if h is None:
            eid = endpoint_row_id(conn, ep)
            if eid is None or conn.execute(
                    "SELECT 1 FROM probes WHERE endpoint_id = ? AND t >= ? AND t <= ? LIMIT 1",
                    (eid, since_t, now)).fetchone() is None:
                continue
            h = endpoint_history(conn, eid, since_t, now, max_points)
        elif not h["t"]:
            continue
        occ = next((k for k in ("slots_busy", "requests_running") if k in h), None)
        per.append((ep.name, h["t"], {
            "gen_tps": h.get("gen_tps"), "prompt_tps": h.get("prompt_tps"),
            "occupancy": h[occ] if occ is not None else None,
        }))
    grid = sorted({t for _, ts, _ in per for t in ts})
    step = 1
    if len(grid) > max_points:
        step = math.ceil(len(grid) / max_points)
        grid = [t for i, t in enumerate(grid) if (len(grid) - 1 - i) % step == 0]
    # how far a kept probe may sit from a grid point and still answer for it (the grid can be
    # coarser than the endpoint's own cadence); half the grid's finest interval
    tol = min(b - a for a, b in zip(grid, grid[1:])) / 2.0 if len(grid) > 1 else 0.0
    body: dict[str, Any] = {"t": grid, "step": step, "endpoints": {}}
    for name, ts, entry in per:
        cols: dict[str, list] = {key: [None] * len(grid) for key in ("gen_tps", "prompt_tps", "occupancy")}
        j = 0
        for i, t in enumerate(ts):  # ts and grid both ascend: one walk
            while j < len(grid) and grid[j] < t - tol:
                j += 1
            best, dist = None, math.inf
            for g in (j - 1, j):
                if 0 <= g < len(grid) and abs(grid[g] - t) < dist:
                    best, dist = g, abs(grid[g] - t)
            if best is not None and dist <= tol:
                for key, values in entry.items():
                    if values is not None:
                        cols[key][best] = values[i]
        body["endpoints"][name] = cols
    return body


# --- jobs --------------------------------------------------------------------------------------


def jobs_in_range(conn: sqlite3.Connection, since_t: float, now: float, limit: int = JOBS_LIMIT) -> list[dict]:
    """Jobs created in ``[since_t, now]``, newest first, at most ``limit``, each
    ``derive.summary_from_row(include_result=False)``."""
    return [
        derive.summary_from_row(conn, row, include_result=False, now=now)
        for row in conn.execute(
            "SELECT * FROM jobs WHERE created_at >= ? AND created_at <= ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (since_t, now, limit))
    ]


def jobs_overlapping(conn: sqlite3.Connection, since_t: float, now: float) -> list[dict]:
    """Story 3.7l: the job spans the fleet timeline draws — jobs OVERLAPPING ``[since_t, now]``
    (``started_at`` set and at or before the window's end, unfinished or ``finished_at`` at or
    after its start; a job that never started has no span to draw), oldest start first, each
    ``{id, endpoint, status, started_at, finished_at}`` — ``endpoint`` is the endpoints
    row's name (null when the job has no endpoint row). One statement, no derive calls: a 7 d
    window over the full log would run ``summary_from_row`` once per job for a bar's label."""
    return [
        {"id": r["id"], "endpoint": r["endpoint"], "status": r["status"],
         "started_at": r["started_at"], "finished_at": r["finished_at"]}
        for r in conn.execute(
            "SELECT j.id, e.name AS endpoint, j.status, j.started_at, j.finished_at"
            " FROM jobs j LEFT JOIN endpoints e ON e.id = j.endpoint_id"
            " WHERE" + _OVERLAP_WHERE +
            " ORDER BY j.started_at ASC, j.rowid ASC",
            (now, since_t),
        )
    ]


def endpoint_requests(conn: sqlite3.Connection, endpoint_id: int | None, since_t: float, now: float,
                      limit: int = 1000) -> dict[str, Any]:
    """The endpoint's per-request table (review of 3.1, ruling D; shape §5a): the ``requests``
    rows of jobs that ran on this endpoint (joined through ``jobs.endpoint_id``) with ``t`` in
    the range, newest first, at most ``limit``. Each row is rendered exactly as ``job_detail``
    renders its requests, plus ``job_id`` (3.10f-2: the id is the job's one key). The ``totals`` object sums the
    whole range (not just the capped list): count, the four token columns, the ms sum (NULLs
    skipped) and the number of requests whose error column is set — not null and not the empty
    string the export writes on clean requests (review of 3.3: ``SUM(error IS NOT NULL)``
    counted those as errors)."""
    rows_ = ([] if endpoint_id is None else conn.execute(
        "SELECT p.path, r.*, j.id AS job_id FROM requests r"
        " JOIN jobs j ON j.id = r.job_id LEFT JOIN paths p ON p.id = r.path_id"
        " WHERE j.endpoint_id = ? AND r.t >= ? AND r.t <= ?"
        " ORDER BY r.t DESC, r.rowid DESC LIMIT ?", (endpoint_id, since_t, now, limit)
    ).fetchall())
    out = [
        {"path": r["path"], "t": r["t"], "http_status": r["http_status"], "ms": r["ms"],
         "gen_ms": r["gen_ms"],
         "tokens": {"input": r["input_tokens"], "output": r["output_tokens"],
                    "cache_read": r["cache_read_tokens"], "cache_creation": r["cache_creation_tokens"]},
         "stream": bool(r["stream"]) if r["stream"] is not None else None,
         "context_exceeded": r["context_exceeded"], "error": r["error"],
         "job_id": r["job_id"]}
        for r in rows_
    ]
    sums = ([0] * 7 if endpoint_id is None else conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(r.input_tokens), 0), COALESCE(SUM(r.output_tokens), 0),"
        " COALESCE(SUM(r.cache_read_tokens), 0), COALESCE(SUM(r.cache_creation_tokens), 0),"
        " COALESCE(SUM(r.ms), 0), COALESCE(SUM(r.error IS NOT NULL AND r.error != ''), 0) FROM requests r"
        " JOIN jobs j ON j.id = r.job_id WHERE j.endpoint_id = ? AND r.t >= ? AND r.t <= ?",
        (endpoint_id, since_t, now)).fetchone()
    )
    return {"requests": out, "totals": {
        "count": sums[0], "input_tokens": sums[1], "output_tokens": sums[2],
        "cache_read_tokens": sums[3], "cache_creation_tokens": sums[4], "ms": sums[5],
        "errors": sums[6],
    }}


def _transcript(conn: sqlite3.Connection, job_id: str) -> tuple[str | None, str | None]:
    """(tail, note): the last ``TRANSCRIPT_LINES`` lines of the job's transcript, or None and
    the sentence saying why there is none. Story 1.12 read order: the ``transcripts`` row when
    present (the finished job — its text, surviving the file's retention), else the file (a
    running job, or a job from before this change); the note names both things it looked for.
    3.10f-1: the directory is ``jobs/<id>`` — the id is the job's one key."""
    from ..db import state_dir  # the MCP server's package; the monitor may read it, never the reverse

    row = conn.execute("SELECT text FROM transcripts WHERE job_id = ?", (job_id,)).fetchone()
    if row is not None:
        tail = deque(row["text"].splitlines(), maxlen=TRANSCRIPT_LINES)
        return "\n".join(tail), None
    d = state_dir() / "jobs" / job_id
    if not d.is_dir():
        return None, (
            f"no transcripts row and the job directory {d} is gone "
            "(pruned after the retention days, or never created here)"
        )
    p = d / "transcript.txt"
    if not p.is_file():
        return None, f"no transcripts row, and {d} holds no transcript.txt"
    with open(p, errors="replace") as f:
        tail = deque((line.rstrip("\n") for line in f), maxlen=TRANSCRIPT_LINES)
    return "\n".join(tail), None


def job_detail(conn: sqlite3.Connection, job_id: str, now: float | None = None) -> dict[str, Any] | None:
    """The job detail body (spec §5 "Job detail"; shape §5a): ``derive.summary_from_row`` plus
    the job's requests and tool calls in time order and the transcript tail. ``now`` is the
    route's clock for a running job's ``elapsed_s`` (ruling C); None reads the wall clock. None
    for an unknown id."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    d = derive.summary_from_row(conn, row, now=now)
    # router 8: the job page's `cal` tag. summary_from_row leaves the router columns out until
    # a story adds them to both readers (jobs.Job._SUMMARY_HIDDEN), so the detail body carries
    # the flag itself; the snapshot entry carries it for the live header.
    d["calibration"] = bool(row["calibration"])
    d["requests"] = [
        {"path": r["path"], "t": r["t"], "http_status": r["http_status"], "ms": r["ms"], "gen_ms": r["gen_ms"],
         "tokens": {"input": r["input_tokens"], "output": r["output_tokens"],
                    "cache_read": r["cache_read_tokens"], "cache_creation": r["cache_creation_tokens"]},
         "stream": bool(r["stream"]) if r["stream"] is not None else None,
         "context_exceeded": r["context_exceeded"], "error": r["error"]}
        for r in conn.execute(
            "SELECT p.path, r.* FROM requests r LEFT JOIN paths p ON p.id = r.path_id"
            " WHERE r.job_id = ? ORDER BY r.t, r.rowid", (job_id,))
    ]
    # ruling E (review of 3.1): the list is tool_call_log; tool_calls stays the count the
    # summary carries (its length is len(tool_call_log))
    d["tool_call_log"] = [
        {"t": r["t"], "finished": r["finished"], "tool": r["tool"], "tool_use_id": r["tool_use_id"],
         "input": r["input"], "is_error": bool(r["is_error"]) if r["is_error"] is not None else None}
        for r in conn.execute(
            "SELECT t, finished, tool, tool_use_id, input, is_error FROM tool_calls"
            " WHERE job_id = ? ORDER BY t, rowid", (job_id,))
    ]
    d["transcript_tail"], d["transcript_note"] = _transcript(conn, job_id)
    return d
