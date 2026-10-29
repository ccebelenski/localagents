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

from .. import db, derive
from ..registry import Endpoint, backend_kind
from . import jobs as monitor_jobs
from . import rates, state
from .rows import COUNTER_COLUMNS, HARDWARE_COLUMNS

EVENTS_LIMIT = 20  # the overview's newest endpoint events
TRANSCRIPT_LINES = 80  # job detail's transcript tail (job_log's default)
JOBS_LIMIT = 500  # /api/jobs: the newest jobs created in the range, at most this many
SERVER_LIVE_S = 60.0  # a server's heartbeat is fresh this long (status.collect's rule)

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


def _running_jobs(conn: sqlite3.Connection, now: float) -> list[tuple[int | None, dict[str, Any]]]:
    """Every queued or running job on the machine, oldest first, as the running-jobs table needs
    it: the status line's entry (status.collect) plus id, name, elapsed, errors and the last
    tool. Not limited to live servers: ``server_live`` says whether its server still beats.
    Each entry is paired with the job's ``endpoint_id`` (the row, not the name: two configs may
    share an endpoint name)."""
    out = []
    for j in conn.execute(
        "SELECT j.*, m.served_id AS model_served, e.name AS endpoint_name,"
        " s.pid AS server_pid, s.owner AS server_owner, s.updated AS server_updated, s.stopped AS server_stopped"
        " FROM jobs j LEFT JOIN models m ON m.id = j.model_id"
        " LEFT JOIN endpoints e ON e.id = j.endpoint_id"
        " LEFT JOIN mcp_servers s ON s.id = j.mcp_server_id"
        " WHERE j.status IN ('queued', 'running')"
        " ORDER BY COALESCE(j.started_at, j.created_at), j.rowid"
    ).fetchall():
        jid = j["id"]
        start = j["started_at"] or j["created_at"]
        ctx = derive.ctx_tokens(conn, jid)
        tps = derive.gen_tps(conn, jid)
        out.append((j["endpoint_id"], {
            "id": jid,
            "name": j["name"],
            "model": j["model_served"],
            "endpoint": j["endpoint_name"],
            "status": j["status"],
            "started_at": start,
            "finished_at": j["finished_at"],
            "elapsed_s": round(now - start, 1) if start is not None else None,
            "num_turns": j["num_turns"],
            "tool_calls": derive.tool_call_count(conn, jid),
            "errors": derive.error_count(conn, jid),
            "last_tool": derive.last_tool(conn, jid),
            "last_activity": j["last_activity"],
            "ctx_tokens": ctx,
            "context": j["context"],
            "ctx_pct": round(100 * ctx / j["context"]) if ctx and j["context"] else None,
            "gen_tps": round(tps, 1) if tps else None,
            "compactions": j["compactions"],
            "files": len(derive.files_touched(conn, jid)),
            "owner": j["server_owner"],
            "server_live": _server_live(j, now),
        }))
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
    last_up = (conn.execute("SELECT MAX(t) FROM probes WHERE endpoint_id = ? AND up = 1", (eid,)).fetchone()[0]
               if eid is not None else None)
    st = state.endpoint_state(last, last_up, now, window_s)
    dropped = None
    if st == "down":
        first_down = conn.execute(
            "SELECT MIN(t) FROM probes WHERE endpoint_id = ? AND up = 0 AND t > ?", (eid, last_up)).fetchone()[0]
        if first_down is not None:
            job = monitor_jobs.running_job_at(conn, eid, first_down)
            dropped = job["name"] if job is not None else None
    up = bool(last["up"]) if last is not None else False
    entry = {
        "name": ep.name,
        "backend": ep.backend,
        "base_url": ep.url,
        "state": st,
        "last_seen": last_up,
        "model": last["model_served"] if last is not None else None,
        "context": last["context"] if last is not None else None,
        "slots": ({"total": last["slots_total"], "busy": last["slots_busy"]}
                  if last is not None and last["slots_total"] is not None else None),
        "requests": ({"running": last["requests_running"], "waiting": last["requests_waiting"]}
                     if last is not None and (last["requests_running"] is not None
                                              or last["requests_waiting"] is not None) else None),
        "kv_cache_usage": last["kv_cache_usage"] if last is not None else None,
        "latency_ms": last["latency_ms"] if last is not None else None,
        "since": last["since"] if last is not None else None,
        "error": last["error"] if last is not None and not up else None,
        "dropped_job": dropped,
        "rates": _pair_rates(prev, last, backend_kind(ep.backend)),
        "gen_tps_live": last["gen_tps"] if last is not None else None,
        "hardware": ({k: last[k] for k in HARDWARE_COLUMNS}
                     if last is not None and last["has_hardware"] is not None else None),
        "jobs_running": sum(1 for job_eid, _ in running if eid is not None and job_eid == eid),
    }
    node_input = {"up": up, "host": last["host"]} if last is not None else None
    return entry, node_input


def overview(conn: sqlite3.Connection, reg: Any, now: float, window_s: float) -> dict[str, Any]:
    """The SSE snapshot (spec §5 "Overview"; shape §5a): hosts in models.yaml order (endpoints
    grouped by ``host``, config order within), each with its node state and endpoints; the
    running jobs; the newest ``EVENTS_LIMIT`` endpoint events. An endpoint in the database but
    not in the config is ignored (its events too)."""
    running = _running_jobs(conn, now)
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
            entry, node_input = _endpoint(conn, ep, eid, now, window_s, running)
            entries.append(entry)
            if node_input is not None:
                nodes.append(node_input)
        hosts.append({
            "name": host,
            "state": state.node_state(nodes),
            "dormant": state.node_dormant([e["state"] for e in entries]),
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
    return {"now": now, "window_s": window_s, "hosts": hosts, "jobs": [j for _, j in running], "events": events}


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
    without, a down one, is a gap) and nothing reset between them."""
    if prev["has_counters"] is None or cur["has_counters"] is None:
        return False
    return rates.reset_reason(_counters(prev), _counters(cur), prev["since"], cur["since"]) is None


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
    point's rates are over the interval from the previous point (the previous kept probe; for
    the first point the probe ``k`` before it, or the newest probe before the range), through
    ``rates.rates``; null when any raw interval inside it is a gap or a reset. A series whose
    source holds no value in the range is ABSENT (spec §5: "a tile with no source is absent,
    not empty"). ``endpoint_id`` None (configured, never probed) is the empty history."""
    out: dict[str, Any] = {"t": [], "step": 1}
    if endpoint_id is None:
        out.update(events=[], jobs=[])
        return out
    b = conn.execute("SELECT backend FROM endpoints WHERE id = ?", (endpoint_id,)).fetchone()
    kind = backend_kind(b[0]) if b is not None else "other"
    pre = conn.execute(_PROBE_SELECT + " WHERE p.endpoint_id = ? AND p.t < ? ORDER BY p.t DESC, p.id DESC LIMIT 1",
                       (endpoint_id, since_t)).fetchone()
    in_range = conn.execute(_PROBE_SELECT + " WHERE p.endpoint_id = ? AND p.t >= ? AND p.t <= ? ORDER BY p.t, p.id",
                            (endpoint_id, since_t, now)).fetchall()
    rows_ = ([pre] if pre is not None else []) + in_range
    offset = len(rows_) - len(in_range)
    n = len(in_range)
    k = max(1, math.ceil(n / max_points)) if n else 1
    # bad[i]: raw intervals (j-1, j) with j <= i that have no rate (prefix count over rows_)
    bad = [0] * len(rows_)
    for i in range(1, len(rows_)):
        bad[i] = bad[i - 1] + (0 if _interval_ok(rows_[i - 1], rows_[i]) else 1)
    picked = [i + offset for i in range(n) if (n - 1 - i) % k == 0]
    out["step"] = k
    out["t"] = [rows_[s]["t"] for s in picked]
    series: dict[str, list] = {key: [] for key in (*_RATE_SOURCES, *_GAUGES, *_PROBE_SERIES, *_HW_SERIES)}
    for s in picked:
        cur = rows_[s]
        ref = max(s - k, 0)
        r = None
        if ref < s and bad[s] == bad[ref]:
            r = rates.rates(_counters(rows_[ref]), _counters(cur), cur["t"] - rows_[ref]["t"], kind,
                            rows_[ref]["since"], cur["since"])
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
        if any(row[c] is not None for row in in_range for c in sources):
            out[key] = values
    out["events"] = [
        {"t": r["t"], "kind": r["kind"], "detail": r["detail"]}
        for r in conn.execute(
            "SELECT t, kind, detail FROM endpoint_events WHERE endpoint_id = ? AND t >= ? AND t <= ?"
            " ORDER BY t, id", (endpoint_id, since_t, now))
    ]
    out["jobs"] = [
        {"name": r["name"], "id": r["id"], "status": r["status"], "started_at": r["started_at"],
         "finished_at": r["finished_at"], "model": r["served_id"]}
        for r in conn.execute(
            "SELECT j.name, j.id, j.status, j.started_at, j.finished_at, m.served_id FROM jobs j"
            " LEFT JOIN models m ON m.id = j.model_id"
            " WHERE j.endpoint_id = ? AND COALESCE(j.started_at, j.created_at) <= ?"
            " AND (j.finished_at IS NULL OR j.finished_at >= ?)"
            " ORDER BY COALESCE(j.started_at, j.created_at), j.rowid", (endpoint_id, now, since_t))
    ]
    return out


# --- fleet history -----------------------------------------------------------------------------


def fleet_history(conn: sqlite3.Connection, reg: Any, since_t: float, now: float,
                  max_points: int = 600) -> dict[str, Any]:
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
    ``requests_running`` (vLLM). An endpoint with no probe in the range is left out entirely."""
    per: list[tuple[str, list, dict[str, list | None]]] = []
    for ep in reg.endpoints.values():
        eid = endpoint_row_id(conn, ep)
        if eid is None or conn.execute(
                "SELECT 1 FROM probes WHERE endpoint_id = ? AND t >= ? AND t <= ? LIMIT 1",
                (eid, since_t, now)).fetchone() is None:
            continue
        h = endpoint_history(conn, eid, since_t, now, max_points)
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
        derive.summary_from_row(conn, row, include_result=False)
        for row in conn.execute(
            "SELECT * FROM jobs WHERE created_at >= ? AND created_at <= ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (since_t, now, limit))
    ]


def endpoint_requests(conn: sqlite3.Connection, endpoint_id: int | None, since_t: float, now: float,
                      limit: int = 1000) -> dict[str, Any]:
    """The endpoint's per-request table (review of 3.1, ruling D; shape §5a): the ``requests``
    rows of jobs that ran on this endpoint (joined through ``jobs.endpoint_id``) with ``t`` in
    the range, newest first, at most ``limit``. Each row is rendered exactly as ``job_detail``
    renders its requests, plus ``job_id`` and ``job_name``. The ``totals`` object sums the
    whole range (not just the capped list): count, the four token columns, the ms sum (NULLs
    skipped) and the number of requests whose error column is not null."""
    rows_ = ([] if endpoint_id is None else conn.execute(
        "SELECT p.path, r.*, j.id AS job_id, j.name AS job_name FROM requests r"
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
         "job_id": r["job_id"], "job_name": r["job_name"]}
        for r in rows_
    ]
    sums = ([0] * 7 if endpoint_id is None else conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(r.input_tokens), 0), COALESCE(SUM(r.output_tokens), 0),"
        " COALESCE(SUM(r.cache_read_tokens), 0), COALESCE(SUM(r.cache_creation_tokens), 0),"
        " COALESCE(SUM(r.ms), 0), COALESCE(SUM(r.error IS NOT NULL), 0) FROM requests r"
        " JOIN jobs j ON j.id = r.job_id WHERE j.endpoint_id = ? AND r.t >= ? AND r.t <= ?",
        (endpoint_id, since_t, now)).fetchone()
    )
    return {"requests": out, "totals": {
        "count": sums[0], "input_tokens": sums[1], "output_tokens": sums[2],
        "cache_read_tokens": sums[3], "cache_creation_tokens": sums[4], "ms": sums[5],
        "errors": sums[6],
    }}


def _transcript(name: str | None) -> tuple[str | None, str | None]:
    """(tail, note): the last ``TRANSCRIPT_LINES`` lines of the job's transcript, or None and
    the sentence saying why there is none."""
    from ..jobs import state_dir  # the MCP server's module; the monitor may read it, never the reverse

    if not name:
        return None, "the job has no name, so no job directory"
    d = state_dir() / "jobs" / name
    if not d.is_dir():
        return None, f"the job directory {d} is gone (pruned after the retention days, or never created here)"
    p = d / "transcript.txt"
    if not p.is_file():
        return None, f"{d} holds no transcript.txt"
    with open(p, errors="replace") as f:
        tail = deque((line.rstrip("\n") for line in f), maxlen=TRANSCRIPT_LINES)
    return "\n".join(tail), None


def job_detail(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    """The job detail body (spec §5 "Job detail"; shape §5a): ``derive.summary_from_row`` plus
    the job's requests and tool calls in time order and the transcript tail. None for an
    unknown id."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    d = derive.summary_from_row(conn, row)
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
    d["transcript_tail"], d["transcript_note"] = _transcript(row["name"])
    return d
