"""The MCP server exposing local-model agent delegation to a coordinator (Claude Code or OpenCode)."""

from __future__ import annotations

import asyncio
import atexit
import json
import math
import os
import shutil
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from mcp.server.mcpserver import Context, MCPServer

from . import clients, db, derive, jobs, status
from .jobs import Job, JobStore, oldest_first, prune, state_dir
from .registry import ModelUnavailable, Registry, config_path, local_path
from .router import dispatch
from .router import queue as router_queue
from .router import scorecard
from .router.capacity import from_probe, prior_curve
from .router.decision import BriefUnresolved, DecisionUnavailable
from .runner import run_job
from .shim import Shim

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

# the server's instructions and run_agent's description are the client's (Epic 6.1: one module
# per client, the shared text in clients/__init__.py); main() applies the declared client's, and
# this is the default's (Claude Code), which every caller importing the module sees
INSTRUCTIONS = clients.instructions(clients.DEFAULT)

# Returned inside tool results too: server instructions are far back in the prompt by the time a
# model is chosen, and the served list is whatever the user left running, vetted by nobody.
GUIDANCE = (
    "Served models are whatever is running, not a curated list. Verify and validate unknown or "
    "novel models before delegating to them, and confirm with the user that using one is appropriate. "
    "Prefer instruct/coder ids with context >= 128k; a context_warning means the window is too small "
    "for an agent session. Say which served model you are using."
)

@asynccontextmanager
async def _mcp_lifespan(_mcp_server: MCPServer):
    """The MCP server's startup hook (the low-level Server enters it around every serve): start
    the load sampler here so the mcp_servers heartbeat runs from the beginning, not from the
    first job. run_agent still calls _ensure_sampler() as a belt for paths that bypass this."""
    _ensure_sampler()
    yield {}


mcp = MCPServer("localagents", instructions=INSTRUCTIONS, lifespan=_mcp_lifespan)
store = JobStore()
shim = Shim(lambda jid: store.jobs.get(jid), endpoint_lookup=lambda name: _reg().endpoints.get(name))


def _reg() -> Registry:
    return Registry.load()


# ---------------------------------------------------------------- models

async def _exposed_endpoints(reg: Registry, conn: sqlite3.Connection, probes: dict[str, dict],
                             now: float) -> tuple[dict[str, dict], dict[str, dict]]:
    """Router 14a (design §7 "Exposed", "Accepted"): every real endpoint's admission facts as
    the queue reads them, from the probes already taken (no second probe, nothing inside a
    transaction), and the declared pools. Per endpoint: ``sessions`` (capacity, ours, foreign;
    the capacity model of its first served id), ``queue`` (the waiting entries and the head's
    priority), ``predicted_speed`` (the curve at the session a new job would be, ours + foreign
    + 1; None when the endpoint is down and no session can start), ``curve_state`` and
    ``pool`` with ``pool_sessions`` when the pool is declared. A pool's ``used`` is the sum
    over its members of ours plus foreign, the count its cap is compared with."""
    per: dict[str, dict] = {}
    used: dict[str, int] = {}
    for n, e in reg.endpoints.items():
        if e.is_router:
            continue
        probe = probes[n]
        served = (probe.get("models") or [""])[0]
        f = await dispatch.endpoint_facts(reg, conn, e, served, now, probes=probes)
        cap = f.capacity
        foreign = max(0, cap.requests_running - f.ours)
        waiting = router_queue.waiting(conn, f.endpoint_id, now) if f.endpoint_id is not None else []
        per[n] = {
            "sessions": {"capacity": cap.sessions_capacity, "ours": f.ours, "foreign": foreign},
            "queue": {"count": len(waiting),
                      "head_priority": (dispatch.queue_priority(waiting[0]["priority_rank"])
                                        if waiting else None)},
            "predicted_speed": (round(cap.curve.predict(f.ours + foreign + 1), 3)
                                if probe.get("up") else None),
            "curve_state": {"state": cap.curve.state, "points": cap.curve.n,
                            "measured": sorted(cap.curve.measured),
                            "holds_from": cap.curve.first_hold()},
            "pool": f.pool,
        }
        if f.pool is not None:
            used[f.pool] = used.get(f.pool, 0) + f.ours + foreign
    pools = {name: {"cap": spec["max_concurrent"],
                    "members": [m.name for m in reg.pool_members(name)],
                    "used": used.get(name, 0)}
             for name, spec in reg.pools.items()}
    for d in per.values():
        if d["pool"] is not None:
            d["pool_sessions"] = {"cap": pools[d["pool"]]["cap"], "used": pools[d["pool"]]["used"]}
    return per, pools


def _exposed_router(conn: sqlite3.Connection, pools: dict[str, dict]) -> dict[str, Any]:
    """Router 14a (design §4, §8 "Notices, not triggers"): the router entry's scorecard facts.
    ``calibrated_rows`` are the live rows with a complete core run; ``notices`` the rows whose
    notices are not empty, each with them; ``pools`` every declared pool. A probe set that
    cannot be read is named as ``probe_set_error``: the set-upgrade notice needs its version,
    so the listing says it could not look rather than reading as no notice."""
    from .router import calibrate

    out: dict[str, Any] = {}
    try:
        installed = calibrate.load_set().version
    except Exception as e:  # noqa: BLE001  the manifest is operator input: named, never raised
        installed = None
        out["probe_set_error"] = f"{type(e).__name__}: {e}"
    live = scorecard.rows(conn)
    out["calibrated_rows"] = [{"endpoint": r.endpoint_name, "served_id": r.served_id}
                              for r in live if scorecard.core_complete(conn, r.id)]
    notices = []
    for r in live:
        got = scorecard.notices(conn, r.id, installed)
        if got:
            notices.append({"endpoint": r.endpoint_name, "served_id": r.served_id, "notices": got})
    out["notices"] = notices
    out["pools"] = pools
    return out


@mcp.tool()
async def list_models() -> dict[str, Any]:
    """List configured endpoints with live health and what each is serving right now.

    Per endpoint: `up`, `models` (served ids), `context` (real per-request window per served id),
    and slot/queue occupancy where the backend exposes it. Anything listed as served is usable
    immediately via `run_agent`; if nothing suitable is up, ask the user to start a model.
    Every real endpoint also carries our queue's view of it: `sessions` (`capacity`, `ours`,
    `foreign`), `queue` (`count` waiting, `head_priority`), `predicted_speed` (the throughput
    curve's fraction of a lone session's speed for the next session there), `curve_state`
    (`state`: prior | probed | refined, `points` its sample count, `measured` the concurrencies
    measured for the served model, `holds_from` where normal work starts waiting for a measured
    bottleneck, or null), and `pool` (its declared pool or null)
    with `pool_sessions` (`cap`, `used`) when it is in one.
    A `router` entry serves only its own name: it carries `router: True` and the fleet's
    `context_range` — the smallest and largest real windows probed right now — instead of a
    window of its own, plus `calibrated_rows` (endpoint x served id with a complete core
    calibration), `notices` (per scorecard row) and `pools` (name: cap, members, used).
    `facts_error` names a database failure that left the queue facts out.
    """
    reg = _reg()
    probes = await reg.probe_all()
    # the fleet's window span over the real endpoints' probed windows; [] when none answered
    windows = [
        w
        for n, e in reg.endpoints.items()
        if not e.is_router
        for w in (probes[n].get("context") or {}).values()
    ]
    context_range = [min(windows), max(windows)] if windows else []
    # router 14a: the queue's facts, read after every probe; a database failure leaves them out
    # and says so, and the live listing stands
    facts_error = None
    exposed: dict[str, dict] = {}
    router_facts: dict[str, Any] = {}
    try:
        conn = store.conn
        exposed, pools = await _exposed_endpoints(reg, conn, probes, time.time())
        router_facts = _exposed_router(conn, pools)
    except (sqlite3.Error, db.SchemaError) as e:
        exposed, router_facts = {}, {}
        facts_error = f"{type(e).__name__}: {e}"
    endpoints = {}
    for n, e in reg.endpoints.items():
        d = {"base_url": e.url, "backend": e.backend, "host": e.host, "notes": e.notes, **probes[n]}
        if e.is_router:
            d["router"] = True
            d["context_range"] = context_range
            d.update(router_facts)
        else:
            d.update(exposed.get(n, {}))
        endpoints[n] = d
    out = {
        "config": str(reg.path),
        "server_cwd": os.getcwd(),
        "default_model": reg.defaults.model,
        "endpoints": endpoints,
        "guidance": GUIDANCE,
    }
    if facts_error is not None:
        out["facts_error"] = facts_error
    return out


@mcp.tool()
async def register_endpoint(
    name: str,
    base_url: str,
    backend: str = "other",
    host: str = "local",
    notes: str = "",
) -> dict[str, Any]:
    """Add or update an inference endpoint (persisted to models.local.yaml) and probe it.

    `base_url` is the server root that exposes `/v1/messages`, e.g. `http://127.0.0.1:8080`.
    `backend` is one of llama.cpp | vllm | strata | ollama | other; it selects how the context
    window and occupancy are probed (strata is llama.cpp-compatible but advertises nothing).
    """
    reg = _reg()
    reg.local.setdefault("endpoints", {})[name] = {
        k: v for k, v in dict(base_url=base_url, backend=backend, host=host, notes=notes).items() if v
    }
    reg.save()
    reg = _reg()
    probe = await reg.probe(reg.endpoints[name])
    return {"ok": True, "endpoint": name, "probe": probe, "config": str(local_path(reg.path))}


def _elapsed(job: Job) -> float:
    """Seconds since the job started: 0.0 when neither started_at nor created_at is set, the
    rule summary() already follows for a hand-built Job (story 1.9 item 2)."""
    start = job.started_at or job.created_at
    return round(time.time() - start, 1) if start is not None else 0.0


def _progress_reporter(ctx: Context | None):
    """Progress callback for a blocking wait: one notification per interval, never fatal."""
    if ctx is None:
        return None

    async def report(job: Job) -> None:
        elapsed = int(_elapsed(job))
        msg = f"{job.status}: {elapsed}s, {job.num_turns} turns"
        try:
            conn = store.conn
            # router 12 (design §7): a job waiting in our queue says where it stands instead
            queued = dispatch.queued_line(conn, job) if job.status == "queued" else None
            if queued is not None:
                try:
                    await ctx.report_progress(elapsed, None, queued)
                except Exception:  # a client without progress support must not break the wait
                    pass
                return
            msg += f", {derive.tool_call_count(conn, job.id)} tool calls"
            errors = derive.error_count(conn, job.id)
            if errors:
                msg += f", {errors} errors"
            # what the agent is doing right now, from the rows; a result held open by background
            # tasks says that instead (the last tool is what started them)
            last = derive.progress_activity(job.last_activity, derive.last_tool(conn, job.id))
            if last:
                msg += f"; {last}"
        except (sqlite3.Error, db.SchemaError) as e:
            # a derive read failing inside store.wait must not abort the wait: the line says
            # what it can without the derived parts, one stderr line names the failure
            # (story 1.9 item 6)
            print(f"localagents: progress line degraded: {type(e).__name__}: {e}", file=sys.stderr)
        try:
            await ctx.report_progress(elapsed, None, msg)
        except Exception:  # a client without progress support must not break the wait
            pass

    return report


def _progress_rows(job_ids: list[str]) -> dict[str, Any]:
    """The rows _report_many's line reads for another server's jobs (None: no such row)."""
    return {
        jid: store.conn.execute(
            "SELECT status, started_at, created_at, num_turns FROM jobs WHERE id = ?", (jid,)
        ).fetchone()
        for jid in job_ids
    }


async def _report_many(ctx: Context, job_ids: list[str]) -> None:
    """wait_jobs' one progress reporter, a task of its own: every jobs.PROGRESS_INTERVAL_S one
    line covering every id in job_ids order, this server's jobs from their Job and another
    server's from their row (review of 1.10, D2: the foreign waits each reported their own
    line on the same token, unnamed, with the progress value regressing). `progress` is the
    done count (settled in-process, terminal row) and never goes down: a row seen terminal
    stays counted even if a later read fails. A failing row read degrades the line (the ids it
    could not read show as `?`, one stderr line), never the wait (story 1.9 item 6's rule)."""
    interval_s = jobs.PROGRESS_INTERVAL_S  # read at call time so tests can shorten it
    finished: set[str] = set()  # foreign ids seen terminal: terminal is final

    def waiting(jid: str, j: Job | None) -> str | None:
        # a queued job's part is the queue's own line (`queued #n, ~m min`), as run_agent's and
        # wait_job's; a failed read degrades the part to the plain one
        try:
            return dispatch.queued_line(store.conn, j) if j is not None else _foreign_queued_line(jid)
        except (sqlite3.Error, db.SchemaError) as e:
            print(f"localagents: progress line degraded: {type(e).__name__}: {e}", file=sys.stderr)
            return None

    while True:
        await asyncio.sleep(interval_s)
        foreign = [jid for jid in dict.fromkeys(job_ids) if jid not in store.jobs and jid not in finished]
        try:
            rows = _progress_rows(foreign)
        except (sqlite3.Error, db.SchemaError) as e:
            rows = {}
            print(f"localagents: progress line degraded: {type(e).__name__}: {e}", file=sys.stderr)
        for jid, row in rows.items():
            if row is not None and row["status"] in _TERMINAL_STATUSES:
                finished.add(jid)

        done = 0
        parts = []
        for jid in job_ids:
            j = store.jobs.get(jid)
            if j is not None:
                if j.settled:
                    done += 1
                    continue
                # the id, the job's one handle since 3.10f-1: the same string list_jobs and the
                # job directory show
                line = waiting(jid, j) if j.status == "queued" else None
                plain = f"{j.status} {int(_elapsed(j))}s, {j.num_turns} turns"
                parts.append(f"{j.id}: {line or plain}")
            elif jid in finished:
                done += 1
            elif rows.get(jid) is None:
                parts.append(f"{jid}: ?")  # the read failed, or the row is gone (the wait answers that)
            else:
                row = rows[jid]
                start = row["started_at"] or row["created_at"]  # summary_from_row's elapsed rule
                elapsed = int(time.time() - start) if start is not None else 0
                line = waiting(jid, None) if row["status"] == "queued" else None
                plain = f"{row['status']} {elapsed}s, {row['num_turns']} turns"
                parts.append(f"{jid}: {line or plain}")
        try:
            await ctx.report_progress(done, len(job_ids), "; ".join([f"{done}/{len(job_ids)} done", *parts]))
        except Exception:  # a client without progress support must not break the wait
            pass


LOAD_SAMPLE_S = 10.0
_sampler: asyncio.Task | None = None
_shim_port_recorded = False  # shim.port is the process's: describe_server(shim_port=...) runs once


async def _sample_tick() -> None:
    """One sampler tick (fold 2): heartbeat this server's row (jobs or not: the status line
    reads liveness from it); probe the endpoints of RUNNING sessions only — a queued job is
    not a running session, its endpoint is probed by its own admission wake, not here — and
    write each one's sample to endpoint_load (``_load_sample``: the session capacity, live
    requests, the server's queue depth and our sessions; idle servers write no load rows: old
    ones age out by ``updated``); run the stall scan here (§7 "Stalled sessions": once per stall
    episode, nothing cancelled), independent of the queue, so a stalled session is flagged
    even while nothing is queued behind it; and repair this process's settled jobs whose drop
    write failed at the drop, before the phantom queue entry blocks its endpoint for good."""
    store.heartbeat()
    running = [j for j in store.jobs.values() if j.status == "running"]
    reg = _reg()
    if running:
        loads: dict[str, str] = {}
        for name in dict.fromkeys(j.endpoint for j in running):
            ep = reg.endpoints.get(name)
            if ep is None:
                continue
            probe = await reg.probe(ep)
            mine = [j for j in running if j.endpoint == name]
            try:
                loads[name] = _load_sample(ep, probe, [j.model for j in mine],
                                           next((j.endpoint_id for j in mine if j.endpoint_id is not None), None))
            except Exception:  # noqa: BLE001 -- a probe value from_probe cannot read (or a locked
                continue       # read) writes no sample: the line shows no capacity, and the stall
                               # scan and the drop repair below still run this tick
        store.record_endpoint_load(loads)
    await dispatch.stall_scan(store, store.conn, reg, time.time())
    _repair_settled_drops()


def _load_sample(ep, probe: dict, served_ids: list[str], endpoint_id: int | None) -> str:
    """One endpoint's endpoint_load text (status-sessions; ``status.load_sample``): the
    session capacity admission gates on (``capacity.from_probe``, the entry's max_concurrent
    applied; a pooled model's count depends on its window, so with several served ids running
    here the smallest, never the largest), None when the probe could not see the endpoint (a
    read that could not see never says free); the live requests ``from_probe`` reads; the
    server's own queue depth; and our admitted sessions now (``queue.occupancy``), so the
    status line's foreign count is this tick's ``Capacity.admits`` arithmetic."""
    caps = [from_probe(ep, probe, sid, prior_curve("other")) for sid in dict.fromkeys(served_ids)]
    up = bool(probe.get("up")) and bool(caps)
    sessions = min(c.sessions_capacity for c in caps) if up else None
    running = caps[0].requests_running if up else 0
    waiting = (probe.get("load") or {}).get("requests_waiting") if up else 0
    ours = router_queue.occupancy(store.conn, [endpoint_id], time.time()) if endpoint_id is not None else 0
    return status.load_sample(sessions, running, int(waiting or 0), ours)


def _repair_settled_drops() -> None:
    """Fold 2 (Task 12 review, probe 4B): a drop whose writes failed (a locked database at
    the moment of the drop) settled the job `dropped` in memory and told the caller so, but
    left the row `queued` and the queue entry alive — a phantom that heads its endpoint's
    queue and blocks every later entry, capacity freed or not. This process repairs its own
    settled jobs: every job it holds that is `dropped` and settled in memory but whose row
    still reads `queued` (or still has a queue entry) gets its `mark_dropped` retried with
    the error it settled with. A foreign job is the other server's (or a restart's) to
    repair; the retry never raises — the next tick tries again."""
    conn = store.conn
    for j in list(store.jobs.values()):
        if j.status != "dropped" or not j.settled or j._conn is None:
            continue
        try:
            row = conn.execute("SELECT status FROM jobs WHERE id = ?", (j.id,)).fetchone()
            entry = conn.execute("SELECT 1 FROM queue_entries WHERE job_id = ?",
                                 (j.id,)).fetchone()
        except (sqlite3.Error, db.SchemaError):  # the database is having its own bad day
            continue
        if row is None or (row["status"] != "queued" and entry is None):
            continue  # no phantom: the drop's writes landed, or another process ended the row
        try:
            router_queue.mark_dropped(conn, j.id, j.error or "dropped", time.time())
            j._log_safe(f"[queue-repair] retried the failed drop: {j.error or 'dropped'}")
            dispatch.notify()  # a blocked head may have freed: the waiting loops wake now
        except Exception as e:  # noqa: BLE001 -- the tick must not die on a locked database
            j._log_safe(f"[queue-repair] retry failed: {type(e).__name__}: {e}")


async def _sample_endpoint_load() -> None:
    """The LOAD_SAMPLE_S loop around one tick: the tick itself never takes the server down."""
    while True:
        try:
            await _sample_tick()
        except Exception:  # noqa: BLE001  the status bar must never take the server down
            pass
        await asyncio.sleep(LOAD_SAMPLE_S)


def _ensure_sampler() -> None:
    global _sampler
    if _sampler is None or _sampler.done():
        _sampler = asyncio.create_task(_sample_endpoint_load())


# ---------------------------------------------------------------- agents


# Foreign waits (story 1.10): another server's job has no task here, so it is waited on by
# polling its row. The sweep interval is the wait's own dead-server detector (a dead server's
# job turns `lost`); both are module attributes so tests can shorten them.
FOREIGN_POLL_S = 2.0
FOREIGN_SWEEP_S = 30.0

# the row statuses that mean the job is over — Job.done's list itself (jobs.TERMINAL_STATUSES)
_TERMINAL_STATUSES = jobs.TERMINAL_STATUSES

# list_jobs(all=True), keyed by include_finished; module-level so tests/test_query_plans.py
# pins their EXPLAIN QUERY PLAN (3.9E audit). The live-only filter is the status IN form —
# the same set as NOT IN ``_TERMINAL_STATUSES`` under db.py's status CHECK, but the IN form is
# the one whose plan seeks jobs(status, finished_at) instead of walking jobs(created_at).
_LIST_JOBS_SQL = {
    True: "SELECT * FROM (SELECT *, rowid AS _rowid FROM jobs"
          " ORDER BY created_at DESC, rowid DESC LIMIT ?) ORDER BY created_at, _rowid",
    False: "SELECT * FROM (SELECT *, rowid AS _rowid FROM jobs"
           " WHERE status IN ('queued', 'running')"
           " ORDER BY created_at DESC, rowid DESC LIMIT ?) ORDER BY created_at, _rowid",
}


async def _wait_foreign(job_id: str, timeout_s: float, ctx: Context | None) -> dict[str, Any]:
    """Wait on another server's job by polling its row in the shared database: there is no
    in-process task to await, so the row is the only signal (story 1.10 item 1). Every
    FOREIGN_POLL_S re-read the row; a terminal status returns its _row_summary. Every
    FOREIGN_SWEEP_S of waiting calls mark_lost_jobs, so a job whose server died resolves as
    `lost` instead of running out the clock. Every jobs.PROGRESS_INTERVAL_S a line with the
    same shape _progress_reporter's has (a failing derive read degrades the line, story 1.9
    item 6) — wait_job's; wait_jobs passes no ctx and _report_many covers every id in one line
    (review of 1.10, D2); while the job waits in its queue the line is the queue's own
    (_foreign_queued_line) and `timeout_s` has not started: it counts from the admission, as
    the in-process waits' does. On timeout the row as it stands — status still `running`, the
    in-process wait's timeout answer's equivalent. No guard inside: every read here is the database's, and the
    caller turns sqlite3.Error/db.SchemaError into database_error.

    A terminal row is a settled record because run_job writes the terminal status only in its
    last changed() (runner.py, the finally's tail), after _finish and _done.set(): the status
    is set in-process earlier, but no row write carries it until the worktree is finished and
    the record complete. The in-process waits wait on `settled` for the same reason; this
    path relies on that write order instead (pinned by
    test_the_row_turns_terminal_only_after_the_finally_work, review of 1.10 P2)."""
    poll_s = FOREIGN_POLL_S  # read at call time so tests can shorten them
    sweep_s = FOREIGN_SWEEP_S
    progress_s = jobs.PROGRESS_INTERVAL_S
    # the cap counts from the job's admission, as in-process (JobStore.wait's from_admission):
    # armed at the first poll that finds the job not waiting in its queue. The queue phase has
    # no deadline; the sweep below ends it if the owning server dies (its queued job is `lost`)
    deadline: float | None = None
    next_sweep = time.monotonic() + sweep_s
    next_progress = time.monotonic() + progress_s
    while True:
        row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            # the row vanished mid-wait (nothing prunes job rows, so: a database rewritten under
            # us). The honest answer is the same one wait_job gives for an id never seen.
            return {"error": "unknown_job", "message": f"unknown job '{job_id}'"}
        if row["status"] in _TERMINAL_STATUSES:
            return _row_summary(row)
        queued = _foreign_queued_line(job_id) if row["status"] == "queued" else None
        if deadline is None and queued is None:
            deadline = time.monotonic() + timeout_s
        if deadline is not None and deadline - time.monotonic() <= 0:
            return _row_summary(row)
        if time.monotonic() >= next_sweep:
            store.mark_lost_jobs()  # the spec's sweep, this time driven by the wait itself
            next_sweep += sweep_s
        if ctx is not None and time.monotonic() >= next_progress:
            start = row["started_at"] or row["created_at"]  # summary_from_row's elapsed rule
            elapsed = int(time.time() - start) if start is not None else 0
            msg = queued or f"{row['status']}: {elapsed}s, {row['num_turns']} turns"
            try:
                conn = store.conn
                if queued is None:
                    msg += f", {derive.tool_call_count(conn, job_id)} tool calls"
                    errors = derive.error_count(conn, job_id)
                    if errors:
                        msg += f", {errors} errors"
                    last = derive.progress_activity(row["last_activity"], derive.last_tool(conn, job_id))
                    if last:
                        msg += f"; {last}"
            except (sqlite3.Error, db.SchemaError) as e:
                # same degrade rule as the in-process progress line: the line says what it can
                print(f"localagents: progress line degraded: {type(e).__name__}: {e}", file=sys.stderr)
            try:
                await ctx.report_progress(elapsed, None, msg)
            except Exception:  # a client without progress support must not break the wait
                pass
            next_progress += progress_s
        await asyncio.sleep(poll_s if deadline is None else min(deadline - time.monotonic(), poll_s))


def _foreign_queued_line(job_id: str) -> str | None:
    """dispatch.queued_line (the one owner of ``queued #n, ~m min`` and of "is it waiting") for
    another server's job, read from the shared rows: its Job is not in this process, so the
    estimate is the ticket's ``est_wait_s`` (dispatch's own fallback), never an in-process one.
    None when the job is not waiting (admitted, over, or without a queue entry). Raises what its
    reads raise; every caller is inside a database_error guard or the degrade rule."""
    return dispatch.queued_line(store.conn, SimpleNamespace(id=job_id, _queue_estimate=None))


async def _cancel_and_await(futs: list[asyncio.Future[Any]]) -> None:
    """Cancel every future in `futs` that is not done and await them all to the end, so none
    outlives the caller (review of 1.10, D1: a cancelled wait_jobs orphaned its waits until
    timeout_s). A cancel landing on the caller meanwhile is absorbed until they are all done
    (asyncio.wait never cancels what it waits on: runner._to_the_end's idiom) and then
    re-raised: the cancel was meant for the caller and must reach it, never be swallowed."""
    for f in futs:
        if not f.done():
            f.cancel()
    if not futs:
        return
    gathered = asyncio.gather(*futs, return_exceptions=True)
    absorbed = False
    while not gathered.done():
        try:
            await asyncio.wait([gathered])
        except asyncio.CancelledError:
            absorbed = True
    if absorbed:
        raise asyncio.CancelledError


def _foreign_job_error(job_id: str) -> dict[str, str] | None:
    """The id exists in the shared database but not in this process: another server's job.
    cancel_job and run_agent(resume_job=) act on the process and keep refusing it (spec "Tool
    contract decisions", corrected for the waits by story 1.10); job_status, job_log, list_jobs
    and now the waits read any server's rows instead."""
    row = store.conn.execute("SELECT mcp_server_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    return {
        "error": "foreign_job",
        "message": (
            f"job {job_id} belongs to another localagents server ({row['mcp_server_id']}); "
            "cancel_job and run_agent(resume_job=) need the server that started it, while "
            "job_status, job_log, list_jobs, wait_job and wait_jobs work on any job"
        ),
    }


def _foreign_or_unknown(job_id: str, e: KeyError) -> dict[str, Any]:
    """The unknown-job answer shared by cancel/resume (the waits grew their own answer in
    story 1.10): another server's job if the shared row says so, plain unknown_job if it does
    not exist. A row read that fails answers
    database_error — "not found" would be a lie when the finding itself failed, so
    _foreign_job_error's error propagates here and is answered (story 1.9 item 6)."""
    try:
        foreign = _foreign_job_error(job_id)
    except (sqlite3.Error, db.SchemaError) as db_e:
        return _database_error(db_e)
    return foreign or {"error": "unknown_job", "message": e.args[0]}


class JobRefused(Exception):
    """A start_job refusal, carrying the dict run_agent's tool returns: a router guard, a
    resolution failure, a bad argument or a failed job record."""

    def __init__(self, reply: dict[str, Any]) -> None:
        super().__init__(reply.get("message") or reply.get("error") or "job refused")
        self.reply = reply


def _bad(message: str) -> JobRefused:
    return JobRefused({"error": "bad_argument", "message": message})


async def _route(reg: Registry, router_ep: Any, task: str, work: str, priority: str,
                 tolerance: float | None, avoided: str | None) -> "dispatch.Routed":
    """§4 steps 1-4 for a job aimed at the router entry; every refusal is a JobRefused carrying
    the dict run_agent returns (no job exists yet)."""
    client = _d3_client()
    try:
        return await dispatch.route(reg, store.conn, router_ep, task, work, priority, tolerance,
                                    avoid_served_id=avoided, client=client)
    except BriefUnresolved as e:
        raise JobRefused(e.to_dict()) from e
    except DecisionUnavailable as e:
        raise JobRefused(e.to_dict()) from e
    except dispatch.NoRoute as e:
        raise JobRefused(e.to_dict()) from e
    except (sqlite3.Error, db.SchemaError, json.JSONDecodeError) as e:
        raise JobRefused(_database_error(e)) from e
    finally:
        if client is not None:
            await client.aclose()


def _d3_client() -> httpx.AsyncClient | None:
    """The d3 client's transport for a routed run_agent; None lets route() open and close its
    own (a seam for tests, like router.mcp._client)."""
    return None


async def _queue_then_run(job: Job, reg: Registry, res: Any, admitted: bool,
                          run_kw: dict[str, Any]) -> None:
    """The job's task: the queue first (design §7: admission sits before the session, so the
    runner's ``timeout_s`` wraps the session only), then run_job. A job that leaves the queue
    without a session never reaches run_job; admit_or_wait has settled it. Every session that
    ends here wakes this process's queued jobs."""
    try:
        if not admitted:
            res = await dispatch.admit_or_wait(store, store.conn, job, reg)
            if res is None:
                return
        await run_job(job, reg, res, **run_kw)
    finally:
        dispatch.notify()


async def start_job(
    task: str,
    model: str | None = None,
    endpoint: str | None = None,
    cwd: str | None = None,
    isolation: str = "none",
    permission_mode: str | None = None,
    max_turns: int | None = None,
    allowed_tools: list[str] | None = None,
    system_prompt_append: str | None = None,
    timeout_s: int | None = None,
    *,
    priority: str = "normal",
    calibration: bool = False,
    admit: str = "queue",
    tolerance: float | None = None,
    avoid_job: str | None = None,
    copy_ticket_from: str | None = None,
    mcp_servers: dict[str, Any] | None = None,
    thinking: bool | None = None,
    effort: str | None = None,
    strict_mcp_config: bool | None = None,
    setting_sources: list[str] | None = None,
    resume_session: str | None = None,
    run_cwd: str | None = None,
    worktree: dict[str, Any] | None = None,
    background_tasks: bool | None = None,
) -> Job:
    """Record one job, pass it through admission and start its task: run_agent's dispatch
    between the tool's arguments and the running job (router 8, router 12).

    A caller that cannot start a job gets ``JobRefused`` carrying the dict run_agent returns
    for the same refusal. A job aimed at the router entry (``model`` or ``endpoint`` naming it,
    or ``defaults.model``) is routed (design §4): the brief is scored and a REAL row chosen; the
    record names that row's endpoint and served id and carries ``routed_by`` and a ticket. Every
    job, routed or named, gets a queue entry in the transaction that records it, and one
    admission attempt here before its task starts; a job that cannot admit waits in its task
    (``dispatch.admit_or_wait``) and the runner starts only at admission. ``admit``: ``queue``
    waits, ``now`` refuses with ``would_queue`` and creates no job, ``force`` writes an admitted
    entry and skips the wait (§7). ``avoid_job`` filters that job's served id from the routed
    choice and refuses a named model serving it (§7 "Decided"). ``copy_ticket_from`` is a
    resumed routed job's previous job (its ticket is copied, marked resumed). ``priority`` and
    ``calibration`` are the caller's (§4, §10); ``mcp_servers``, ``thinking``, ``effort``,
    ``strict_mcp_config``, ``setting_sources`` and ``background_tasks`` are handed to run_job
    (which resolves a None against the config); ``resume_session``,
    ``run_cwd`` and ``worktree`` carry a resumed job's state, run_agent owns that preamble.
    """
    if priority not in ("urgent", "normal", "background"):
        raise _bad("priority must be 'urgent', 'normal' or 'background'")
    if admit not in ("queue", "now", "force"):
        raise _bad("admit must be 'queue', 'now' or 'force'")
    if tolerance is not None and (isinstance(tolerance, bool) or not isinstance(tolerance, (int, float))
                                  or not math.isfinite(tolerance) or tolerance < 0):
        raise _bad("tolerance must be a number of 0 or more")

    reg = _reg()
    work = str(Path(cwd or os.getcwd()).expanduser().resolve())
    avoided = None
    if avoid_job is not None:
        try:
            avoided = dispatch.served_id_of(store.conn, avoid_job)
        except (sqlite3.Error, db.SchemaError) as e:
            raise JobRefused(_database_error(e)) from e
        if avoided is None:
            raise _bad(f"avoid_job names no job with a recorded model: '{avoid_job}'")

    # A router entry named by model, by endpoint, or by defaults.model routes the job (the M1
    # guard's reading, before resolve: resolve probes the fleet for nothing).
    pinned = reg.endpoints.get(endpoint) if endpoint else None
    # fold 2: a router entry named as `endpoint` routes the job itself, so a real `model`
    # named alongside it was silently ignored (the router won). It is a contradiction: refuse,
    # naming both. The router named as `endpoint` alone routes exactly like `model=<router>`.
    if pinned is not None and pinned.is_router and model and model != pinned.name:
        raise _bad(f"endpoint '{pinned.name}' is a router entry: it routes the job itself, so a "
                   f"real model cannot be named with it (model '{model}'). Pass the router as "
                   "endpoint alone (or as model), or name one model with a real endpoint.")
    wanted = model or getattr(reg.defaults, "model", None)
    named_router = pinned if pinned is not None and pinned.is_router else (
        reg.endpoints.get(wanted) if wanted and wanted in reg.endpoints else None)
    # the consumer's live acceptance (2026-11-04, job ff8db6cb): the mirror case, a router
    # named as `model` with a REAL `endpoint` forced beside it, silently dropped the endpoint
    # and routed. Same contradiction, same answer: refuse, naming both. A steering argument
    # is never lost in silence.
    if (named_router is not None and named_router.is_router and pinned is not None
            and not pinned.is_router):
        raise _bad(f"model '{named_router.name}' is a router entry: it picks the endpoint itself, "
                   f"so a real endpoint cannot be forced with it (endpoint '{pinned.name}'). Route "
                   "with the router alone, or name one model with a real endpoint.")
    routed = None
    if named_router is not None and named_router.is_router:
        if named_router.invalid:
            raise JobRefused({"error": "model_unavailable",
                              "message": f"router entry '{named_router.name}' is not usable: "
                                         f"{named_router.invalid}. Fix models.yaml.",
                              "guidance": GUIDANCE})
        routed = await _route(reg, named_router, task, work, priority, tolerance, avoided)
        res = routed.resolution
    else:
        try:
            res = await reg.resolve(model=model, endpoint=endpoint)
        except ModelUnavailable as e:
            raise JobRefused({**e.to_dict(), "guidance": GUIDANCE}) from e
        if res.endpoint.is_router:
            # resolve answers a router entry only when it was named, which the branch above
            # took: kept as the net, so a router entry never reaches the runner as an endpoint
            raise JobRefused({"error": "not_routable",
                              "message": f"endpoint '{res.endpoint.name}' is a router; name it as "
                                         "model to route a job",
                              "guidance": GUIDANCE})
        if avoided is not None and res.served_model == avoided:
            raise _bad(f"model '{res.served_model}' is the one avoid_job {avoid_job} ran on; name "
                       "another model, or route with model='auto'")

    # the admission facts, read before the record (probes outside any transaction, §7)
    now = time.time()
    try:
        if routed is not None:
            facts = routed.facts
        else:
            facts = await dispatch.endpoint_facts(reg, store.conn, res.endpoint, res.served_model, now)
        copied = None
        routed_by = None
        if copy_ticket_from is not None and routed is None:
            prev = dispatch.read_ticket(store.conn, copy_ticket_from)
            if prev is not None:
                copied = dispatch.resumed_ticket(prev, copy_ticket_from, priority=priority)
                prev_job = store.jobs.get(copy_ticket_from)
                routed_by = prev_job.routed_by if prev_job is not None else None
        if admit == "now":
            if routed is not None and routed.decision.queue_for is not None:
                q = routed.decision.queue_for
                raise JobRefused(dispatch.would_queue(q.queued_ahead + 1, q.expected_wait_s,
                                                      dispatch.alternatives(routed.decision), routed.ticket))
            if routed is None:
                admits, position, wait = dispatch.named_wait(store.conn, facts, priority, now)
                if not admits:
                    raise JobRefused(dispatch.would_queue(position, wait, [], copied))
    except (sqlite3.Error, db.SchemaError, json.JSONDecodeError) as e:
        raise JobRefused(_database_error(e)) from e

    # Everything that can raise runs before new() or cannot raise after it: a job that exists
    # without its task stays `queued` for good (review of 1.7, finding 4). The shim serves every
    # job and does not depend on this one, so it starts first.
    await shim.start()
    try:
        # the jobs row, its queue entry and a routed job's ticket: one transaction (router 12)
        job = dispatch.new_job(
            store,
            priority=priority,
            forced=admit == "force",
            routed=routed,
            copy_ticket=copied,
            task=task,
            model=res.served_model,
            endpoint=res.endpoint.name,
            base_url=res.endpoint.url,
            cwd=work,
            run_cwd=run_cwd or work,
            isolation=isolation,
            backend=res.endpoint.backend,
            host=res.endpoint.host,
            worktree=worktree,
            dump=reg.defaults.dump_requests,
            log_events=reg.defaults.log_events or os.environ.get("LOCALAGENTS_LOG_EVENTS") == "1",
            context=res.context,
            calibration=calibration,
            **({"routed_by": routed_by} if routed is None and routed_by is not None else {}),
        )
    except (sqlite3.Error, db.SchemaError) as e:
        # a refusal like the others, not a bare tool error: the SDK keeps an exception's text out
        # of the reply, and the caller should see "database is locked" (review of 1.7, finding 8).
        # The store opens lazily, so this new() is often the first use and a newer-schema file
        # raises db.SchemaError (a RuntimeError) here too — the guard must take it (1.9a review)
        raise JobRefused({"error": "database_error", "message": f"the job could not be recorded: {e}"}) from e
    job._route = routed
    # Nothing between new() and create_task may raise: each step below is guarded, and a failed
    # first admission leaves the job to its task's loop, never without a task.
    admitted = admit == "force"
    if admitted:
        job._log_safe("[queue] admitted at once: admit='force' (the entry is counted)")
    else:
        try:
            admitted = dispatch.try_admit_now(store.conn, job, facts, time.time())
            if not admitted:
                dispatch.note_queued(store.conn, job, facts, time.time())
        except Exception as e:  # noqa: BLE001 -- the loop retries what this attempt could not do
            job._log_safe(f"[queue-error] first admission: {type(e).__name__}: {e}")
    if not admitted and admit == "now":
        # the check above (try_admit's own predicate, fix wave 3) saw room and another admission
        # took it first: the caller asked not to wait, so the job leaves the queue (dropped,
        # never ran) and the call says would_queue
        try:
            dropped = router_queue.mark_dropped(store.conn, job.id, dispatch.ADMIT_NOW_LOST_RACE,
                                                time.time())
        except Exception as e:  # noqa: BLE001
            job._log_safe(f"[queue-error] admit='now' drop: {type(e).__name__}: {e}")
            dropped = False
        if dropped:
            job.status, job.error, job.finished_at = "dropped", dispatch.ADMIT_NOW_LOST_RACE, time.time()
            job._done.set()
            reply = dispatch.would_queue(1, job._queue_estimate,
                                         dispatch.alternatives(routed.decision) if routed else [],
                                         routed.ticket if routed else copied)
            reply["message"] += (f"; another admission took the slot between the check and the claim"
                                 f" (job {job.id} is dropped)")
            raise JobRefused(reply)
    global _shim_port_recorded
    if not _shim_port_recorded and shim.port:
        # Nothing between new() and create_task may raise: a job without its task stays `queued`
        # for good. So any failure here is a line, and the line itself may fail (a full disk, a
        # read-only state directory): stderr then (DeepSeek review of 6005f94).
        try:
            store.describe_server(shim_port=shim.port)  # once: the port is the process's, not the job's
            _shim_port_recorded = True  # only on success: a dropped write is retried by the next job
        except Exception as e:  # noqa: BLE001
            job._log_safe(f"[db-error] shim_port: {type(e).__name__}: {e}")
    _ensure_sampler()
    # router 9d: the levers the caller set, for the shim to write into the relayed body
    levers = {k: v for k, v in (("thinking", thinking), ("effort", effort)) if v is not None}
    if levers:
        job.levers = levers
    job._task = asyncio.create_task(_queue_then_run(job, reg, res, admitted, dict(
        permission_mode=permission_mode,
        max_turns=max_turns,
        allowed_tools=allowed_tools,
        system_prompt_append=system_prompt_append,
        resume_session=resume_session,
        timeout_s=timeout_s,
        shim_base_url=shim.base_url_for(job.id),
        mcp_servers=mcp_servers or {},
        thinking=thinking,
        effort=effort,
        strict_mcp_config=strict_mcp_config,
        setting_sources=setting_sources,
        background_tasks=background_tasks,
    )))
    return job


async def _wait_admitted(job: Job, ctx: Context | None) -> None:
    """The queue phase alone (jobs.wait_through_queue, its one owner), reporting ``queued #n,
    ~m min``. The wait tools do not call this: store.wait(from_admission=True) runs it before
    their cap. router/calibrate.py is its last caller."""
    await jobs.wait_through_queue(job, _progress_reporter(ctx))


@mcp.tool(description=clients.run_agent_description(clients.DEFAULT))
async def run_agent(
    task: str,
    model: str | None = None,
    endpoint: str | None = None,
    cwd: str | None = None,
    isolation: str = "none",
    wait_s: int = 0,
    permission_mode: str | None = None,
    max_turns: int | None = None,
    allowed_tools: list[str] | None = None,
    system_prompt_append: str | None = None,
    resume_job: str | None = None,
    timeout_s: int | None = None,
    priority: str = "normal",
    admit: str = "queue",
    tolerance: float | None = None,
    avoid_job: str | None = None,
    background_tasks: bool | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delegate a task to an agent on a local model and (with `wait_s`) wait for the result.

    The description the client reads (the arguments, the wait) is clients.RUN_AGENT rendered
    for the client this server serves; the first line above is its first line (the calibration
    stub, router/stubmcp.py, copies it)."""
    if isolation not in ("none", "worktree"):
        return {"error": "bad_argument", "message": "isolation must be 'none' or 'worktree'"}
    if background_tasks is not None and not isinstance(background_tasks, bool):
        return {"error": "bad_argument", "message": "background_tasks must be true, false or omitted"}
    work = str(Path(cwd or os.getcwd()).expanduser().resolve())
    if not Path(work).is_dir():
        return {"error": "bad_argument", "message": f"cwd does not exist: {work}"}

    run_cwd = work
    resume_session = None
    worktree: dict[str, Any] | None = None
    copy_from = None
    if resume_job:
        try:
            prev = store.get(resume_job)
        except KeyError as e:
            return _foreign_or_unknown(resume_job, e)
        if not prev.settled:
            # settled, not done: a job still in run_job's finally may be removing the very tree
            # the resume would reuse (review of 1.7)
            return {
                "error": "job_running",
                "message": f"job {prev.id} has not finished; wait_job for it, then resume",
            }
        if not prev.session_id:
            return {"error": "bad_argument", "message": f"job {resume_job} has no session to resume"}
        resume_session = prev.session_id
        # the resumed session runs as its first run did unless the call says otherwise (None:
        # the first run never reached run_job, which resolves it: the config's then)
        if background_tasks is None:
            background_tasks = prev._background_tasks
        # Continue where the session was. A worktree job keeps its tree: if the tree is gone
        # (removed because the job changed nothing) run_job recreates it at the same path, so
        # the paths the session remembers still resolve.
        work, run_cwd = prev.cwd, prev.run_cwd
        if prev.worktree:
            isolation = "worktree"
            worktree = {k: prev.worktree[k] for k in ("repo", "path", "root", "branch", "base")}
        else:
            isolation = "none"
            if not Path(run_cwd).is_dir():
                return {"error": "bad_argument", "message": f"cwd of job {resume_job} no longer exists: {run_cwd}"}
        # design §4: the session's prefix cache lives on the row that ran it, so a router name
        # passed as model is ignored and the job resumes on that row, its ticket copied
        try:
            named = _reg().endpoints.get(model) if model else None
        except Exception:  # noqa: BLE001 -- a config start_job will read again and answer
            named = None
        if named is not None and named.is_router:
            model = None
            endpoint = None if endpoint == named.name else endpoint
        # Continue on the model that ran the session, not on whatever is the default now.
        if model is None:
            model, endpoint = prev.model, endpoint or prev.endpoint
            copy_from = prev.id

    try:
        job = await start_job(
            task, model=model, endpoint=endpoint, cwd=work, isolation=isolation,
            permission_mode=permission_mode, max_turns=max_turns,
            allowed_tools=allowed_tools, system_prompt_append=system_prompt_append,
            timeout_s=timeout_s, resume_session=resume_session, run_cwd=run_cwd,
            worktree=worktree, priority=priority, admit=admit, tolerance=tolerance,
            avoid_job=avoid_job, copy_ticket_from=copy_from, background_tasks=background_tasks,
        )
    except JobRefused as e:
        return e.reply
    if wait_s > 0:
        # wait_s from the admission (design §7 "Accepted"): the queue phase first, unbounded
        await store.wait(job.id, wait_s, progress=_progress_reporter(ctx), settle=True, from_admission=True)
    try:
        return _job_summary(job)
    except (sqlite3.Error, db.SchemaError) as e:
        # the final record's derive reads are reads too: their failure is the database's
        # problem, not the caller's traceback (story 1.9a row 1)
        return _database_error(e)


@mcp.tool()
async def wait_job(job_id: str, timeout_s: int = 1800, ctx: Context | None = None) -> dict[str, Any]:
    """Block up to `timeout_s` seconds for a job to finish; returns its status/result either way.

    `timeout_s` counts from the job's admission, as run_agent's wait_s: a queued job is waited
    for through the queue (progress `queued #n, ~m min`) and a job dropped or cancelled there
    returns at once. Any server's job: this server's waits on its settle event, another server's is polled from
    its row in the shared database (story 1.10); only `cancel_job` and `run_agent(resume_job=)`
    still refuse another server's job. Progress is reported every minute while waiting, so a
    long timeout is safe to pass. At `timeout_s`, this server's job whose session has ended
    or run out of time is waited for until its record is final; one whose session is still
    working comes back `running`.
    """
    try:
        job = await store.wait(job_id, timeout_s, progress=_progress_reporter(ctx), settle=True,
                               from_admission=True)
        # inside the guard: the summary()'s own derive reads are reads like the wait's
        # (story 1.9 item 6, made true here by 1.9a row 1 — it sat after the try)
        return _job_summary(job)
    except KeyError as e:
        return await _wait_or_unknown(job_id, e, timeout_s, ctx)
    except (sqlite3.Error, db.SchemaError) as e:
        return _database_error(e)


async def _wait_or_unknown(job_id: str, e: KeyError, timeout_s: float, ctx: Context | None) -> dict[str, Any]:
    """wait_job's answer when the id is not in this process: the shared database has the row →
    wait on it (`_wait_foreign`, story 1.10 item 1); no row anywhere → the plain unknown_job
    with store.get's message. Every read here is the database's: its failure is
    database_error, never unknown_job (story 1.9 item 6's rule)."""
    try:
        if store.conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
            return {"error": "unknown_job", "message": e.args[0]}
        return await _wait_foreign(job_id, timeout_s, ctx)
    except (sqlite3.Error, db.SchemaError) as db_e:
        return _database_error(db_e)


@mcp.tool()
async def wait_jobs(
    job_ids: list[str],
    timeout_s: int = 1800,
    mode: str = "all",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Block for several jobs: all finished (mode="all"), first finished (mode="any"), or timeout_s.
    Each job's timeout_s counts from its own admission (queue time does not spend it): "all"
    ends when every job is done or past its cap, "any" at the first finish or then.
    Fallback only: prefer one blocking run_agent per job (each notifies on its own finish).
    mode="any" must be re-armed after every return or the rest finish unseen.
    Any server's ids mix (story 1.10): this server's jobs wait on their settle events, another
    server's are polled from their rows.
    Returns done (final records) and running (ids, status, elapsed). Progress every minute.
    At timeout_s, this server's jobs whose session has ended or run out of time are waited
    for until final; jobs whose session is still working come back in running.
    A done job's record is derived from the database: when that read fails, the whole call
    answers database_error — a partial record would be a lie about the jobs it leaves out.
    Likewise a row that disappears mid-wait (nothing deletes job rows; a database rewritten
    under the server would) answers unknown_job for the whole call, not a list entry.
    """
    if mode not in ("all", "any"):
        return {"error": "bad_argument", "message": "mode must be 'all' or 'any'"}
    if not job_ids:
        return {"error": "bad_argument", "message": "job_ids is empty"}
    # an id given twice is one job: dedup once here so the wait, the progress total and the
    # classification below all count it once (review 3.9A, A1)
    job_ids = list(dict.fromkeys(job_ids))
    # split the ids: in-process wait on their events, foreign on their rows; an unknown id
    # anywhere answers unknown_job before any waiting (as today)
    try:
        foreign_ids = [jid for jid in job_ids if jid not in store.jobs]
        for jid in foreign_ids:
            if store.conn.execute("SELECT id FROM jobs WHERE id = ?", (jid,)).fetchone() is None:
                try:
                    store.get(jid)  # never returns: its message names the known jobs
                except KeyError as e:
                    return {"error": "unknown_job", "message": e.args[0]}
    except (sqlite3.Error, db.SchemaError) as e:
        return _database_error(e)  # a failed row read is never an unknown_job (story 1.9 item 6)
    in_process_ids = [jid for jid in job_ids if jid not in foreign_ids]

    tasks: list[asyncio.Future[Any]] = []
    reporter: asyncio.Future[Any] | None = None
    try:
        # the waits report nothing themselves: one reporter covers every id (review of 1.10, D2)
        if ctx is not None:
            reporter = asyncio.ensure_future(_report_many(ctx, job_ids))
        if in_process_ids:
            tasks.append(asyncio.ensure_future(store.wait_many(in_process_ids, timeout_s, mode=mode, settle=True,
                                                               from_admission=True)))
        tasks.extend(asyncio.ensure_future(_wait_foreign(jid, timeout_s, None)) for jid in foreign_ids)
        # Each job's cap counts from its own admission (wait_many's per-job waits, and
        # _wait_foreign's): mode="all" waits for every task; mode="any" until one job is over,
        # or every task has ended without one (wait_many's own rule, across both kinds). No
        # timeout here: a queue phase has no deadline, so no fixed belt can bound the call;
        # every task ends at its job's settle, its cap, or its job leaving the queue.
        pending = set(tasks)
        while pending:
            ended, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED if mode == "any" else asyncio.ALL_COMPLETED)
            if mode == "any" and any(_a_job_is_over(f) for f in ended):
                break
    finally:
        # asyncio.wait never cancels its tasks, and a cancel of this call (the client's Esc or
        # request timeout) lands here: every task is cancelled and awaited to the end on every
        # path, so no wait_many and no _wait_foreign outlives the call (review of 1.10, D1:
        # they polled and swept until timeout_s). wait_many's own finally drops its _done
        # waiters; a cancel landing during this await is re-raised once it is done. The
        # progress reporter goes the same way (row 5 of 1.10a).
        await _cancel_and_await(tasks + ([reporter] if reporter is not None else []))
    try:
        for f in tasks + ([reporter] if reporter is not None else []):
            exc = None if f.cancelled() else f.exception()
            if isinstance(exc, (sqlite3.Error, db.SchemaError)):
                return _database_error(exc)
            if exc is not None:
                raise exc  # not the database's failure and not a refusal: a bug, let it raise
        # classify every id by where it stands now, not by the task returns: a cancelled or
        # still-pending task says nothing about the job. settled as before: a job still in
        # run_job's finally has an incomplete record (review of 1.7, finding 1)
        done, running = [], []
        for jid in job_ids:
            if jid in store.jobs:
                j = store.jobs[jid]
                if j.settled:
                    done.append(_job_summary(j))
                else:
                    # elapsed from the timestamps (summary()'s formula, None-settled 0.0
                    # included — story 1.9 item 2), not a whole summary's db reads
                    running.append({"id": j.id, "status": j.status,
                                    "elapsed_s": _elapsed(j), "num_turns": j.num_turns})
            else:
                row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
                if row is None:
                    return {"error": "unknown_job", "message": f"unknown job '{jid}'"}
                if row["status"] in _TERMINAL_STATUSES:
                    done.append(_row_summary(row))
                else:
                    # the same entry keys an in-process running entry carries, read off the row
                    start = row["started_at"] or row["created_at"]
                    running.append({
                        "id": row["id"], "status": row["status"],
                        "elapsed_s": round(time.time() - start, 1) if start is not None else 0.0,
                        "num_turns": row["num_turns"],
                    })
    except (sqlite3.Error, db.SchemaError) as e:
        # a done job's derive reads failing makes the whole record incomplete: answer
        # database_error for the call rather than hand back a partial `done` (story 1.9a row 1)
        return _database_error(e)
    if not running:
        outcome = "all_done"
    elif done:
        outcome = "some_done"
    else:
        outcome = "timeout"
    return {"outcome": outcome, "done": done, "running": running}


def _a_job_is_over(f: asyncio.Future[Any]) -> bool:
    """wait_jobs mode="any": an ended wait whose job is over (wait_many returned a settled job,
    _wait_foreign a terminal row), or that failed or answered an error (the classification
    after it says which). A wait that only reached its cap is not."""
    if f.cancelled() or f.exception() is not None:
        return True
    got = f.result()
    if isinstance(got, list):
        return any(j.settled for j in got)
    return "status" not in got or got["status"] in _TERMINAL_STATUSES


def _database_error(e: Exception) -> dict[str, Any]:
    """The read tools' answer when the database itself fails: a refusal like run_agent's, so
    the caller sees "database is locked" rather than a bare tool error (review of 1.8)."""
    return {"error": "database_error", "message": f"the database could not be read: {e}"}


def _routing_extras(job_id: str, status: str, job: Job | None = None) -> dict[str, Any]:
    """router 12: ``queue`` {position, expected_wait_s} while the job waits in our queue (None
    once admitted or over), ``forced`` (its queue entry was written by ``admit='force'``, §7
    "the record says it forced"; beside the queue facts so a named job, which has no ticket,
    says it too) and ``ticket``, the routing decision's summary (None for a named job). Read
    from the database for any server's job: the estimate is the one its own loop wrote on the
    queue entry at its latest wake (§6 True-now 2026-11-15, schema v13), the same number in
    every server process, a named job's included; else the ticket's wait at enqueue
    (``dispatch.waiting_estimate``)."""
    conn = store.conn
    ticket = dispatch.read_ticket(conn, job_id)
    entry = conn.execute("SELECT forced FROM queue_entries WHERE job_id = ?", (job_id,)).fetchone()
    waiting = None
    if status == "queued":
        try:
            position = router_queue.position(conn, job_id, time.time())
        except KeyError:
            position = 0  # no entry: a job recorded before router 12, or by hand
        if position:
            waiting = {"position": position,
                       "expected_wait_s": dispatch.waiting_estimate(conn, job_id)}
    return {"queue": waiting, "forced": bool(entry["forced"]) if entry is not None else False,
            "ticket": dispatch.ticket_summary(ticket)}


def _job_summary(job: Job, include_result: bool = True) -> dict[str, Any]:
    """An in-process job's summary() with the routing extras (_row_summary adds the same)."""
    return {**job.summary(include_result=include_result), **_routing_extras(job.id, job.status, job)}


def _row_summary(row: Any, include_result: bool = True) -> dict[str, Any]:
    """derive.summary_from_row, but a row it cannot read (another writer's garbled worktree
    JSON, say) answers with what the row does say plus ``row_error``, never an exception: one
    bad row must not break list_jobs(all=True) for every caller."""
    try:
        return {**derive.summary_from_row(store.conn, row, include_result=include_result),
                **_routing_extras(row["id"], row["status"])}
    except (json.JSONDecodeError, TypeError, ValueError, KeyError) as e:
        # only what a row another writer mangled can raise; anything else is a bug in this
        # code and must propagate, not hide behind row_error (review of 1.8). A sqlite3.Error is
        # the database's problem (locked, I/O, closed), not the row's: it propagates and the
        # tool answers database_error (review of 1.8, finding 5). The line names the row: a
        # silently degraded listing is how a broken writer hides.
        print(f"localagents: row_error {row['id']}: {type(e).__name__}: {e}", file=sys.stderr)
        return {"id": row["id"], "status": row["status"], "row_error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def job_status(job_id: str) -> dict[str, Any]:
    """Current status, last activity, files changed, and result (if finished) of a job.

    `diff_files` is the worktree diff's file list, shell writes included (null for a job with
    no worktree, or before its finish); `files_touched` is only the paths the edit tools were
    given, so it never sees a file written through the shell.

    `tool_mix.read_share` is the share of the job's tool calls that only looked (read, searched,
    listed, inspected git), a classification of each call's stored command line;
    `tool_mix.unclassified` counts the calls it could not place (mostly commands cut in
    storage), so the share is a floor.

    Reads any localagents server's job: an in-process job gets its live summary, otherwise
    the row and its read-time derivations from the shared database."""
    try:
        return _job_summary(store.get(job_id))
    except KeyError as e:
        try:
            row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return {"error": "unknown_job", "message": e.args[0]}
            return _row_summary(row)
        except (sqlite3.Error, db.SchemaError) as db_e:
            # the same guard as list_jobs: the database failing is database_error, and a
            # newer-schema file (SchemaError, a RuntimeError) is one way it fails (review of 1.8)
            return _database_error(db_e)
    except (sqlite3.Error, db.SchemaError) as db_e:
        # an in-process job's summary() derives read the rows too: their failure is the
        # database's problem, not the caller's traceback (story 1.9 item 6)
        return _database_error(db_e)


@mcp.tool()
async def list_jobs(
    include_finished: bool = True, all: bool = False, limit: int = 50
) -> list[dict[str, Any]] | dict[str, Any]:
    """List jobs (newest last). This server session's; with all=True, every localagents
    server's jobs read from the shared database.

    limit: the newest N jobs only (default 50; 0 = no limit). The database keeps every job
    ever run, so all=True without a limit grows with the whole history."""
    if not all:
        try:
            mine = [j for j in sorted(store.jobs.values(), key=oldest_first) if include_finished or not j.done]
            return [_job_summary(j, include_result=False) for j in (mine[-limit:] if limit > 0 else mine)]
        except (sqlite3.Error, db.SchemaError) as e:
            # every in-process summary() read the rows too (story 1.9 item 6)
            return _database_error(e)
    # the newest `limit` rows, returned oldest first (newest last, as the in-process list).
    # A NULL created_at means oldest: SQLite sorts NULLs last in DESC (so the inner LIMIT cuts
    # it first) and first in ASC (so the outer re-sort puts it at the oldest end) — no
    # `created_at IS NULL` term needed, and none wanted: it would break the index scan the
    # inner query relies on (review of 1.8). The live-only branch's predicate (3.9E finding
    # E3) is `status IN ('queued', 'running')`, not the same-set `NOT IN` over
    # ``_TERMINAL_STATUSES``: under ``_V1``'s status CHECK both select the live jobs, but only
    # the IN form's plan is a seek of jobs(status, finished_at) — the NOT IN form walks the
    # whole jobs_created_at index over every job ever run (tests/test_query_plans.py pins both
    # plans).
    try:
        rows = store.conn.execute(
            _LIST_JOBS_SQL[include_finished],
            (limit if limit > 0 else -1,),  # SQLite: a negative LIMIT is no limit
        ).fetchall()
        return [_row_summary(r, include_result=False) for r in rows]
    except (sqlite3.Error, db.SchemaError) as e:
        return _database_error(e)


@mcp.tool()
async def job_log(job_id: str, tail_lines: int = 80, tools: int = 0) -> dict[str, Any]:
    """Tail of a job's human-readable transcript (assistant text, tool calls, results).

    tail_lines=0 is the whole transcript (as list_jobs(limit=0) is); a negative tail_lines is
    bad_argument. Works for any localagents server's job. tools=N also returns the last N
    recorded tool calls (t, finished, tool, input, is_error), oldest first.

    Story 1.12 read order, in-process and foreign jobs alike: the ``transcripts`` row when
    present (the finished job — its text, surviving the file's retention), else the file (a
    running job, or a job from before this change); the note is said only when neither exists."""
    if tail_lines < 0:
        return {"error": "bad_argument", "message": "tail_lines must be >= 0 (0 is the whole transcript)"}
    try:
        try:
            job = store.get(job_id)
            status_, log_dir, transcript = job.status, job.log_dir, job.transcript_tail(tail_lines)
            has_row = store.conn.execute("SELECT 1 FROM transcripts WHERE job_id = ?", (job_id,)).fetchone() is not None
        except KeyError as e:
            row = store.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return {"error": "unknown_job", "message": e.args[0]}
            # another server's job: its log directory is jobs/<id> (spec derivation, 3.10f-1)
            status_ = row["status"]
            log_dir = str(state_dir() / "jobs" / job_id)
            trow = store.conn.execute("SELECT text FROM transcripts WHERE job_id = ?", (job_id,)).fetchone()
            has_row = trow is not None
            if trow is not None:
                lines = trow["text"].splitlines()
            else:
                p = Path(log_dir) / "transcript.txt" if log_dir else None
                lines = p.read_text(errors="replace").splitlines() if p is not None and p.exists() else []
            transcript = "\n".join(lines[-tail_lines:] if tail_lines else lines)
        out: dict[str, Any] = {"job_id": job_id, "status": status_, "log_dir": log_dir, "transcript": transcript}
        if not has_row and (not log_dir or not Path(log_dir).is_dir()):
            # retention removed the directory and there is no row (or it never existed):
            # say so rather than an unexplained ""
            out["transcript"] = ""
            out["note"] = f"transcript pruned or never written ({log_dir})"
        if tools > 0:
            rows = store.conn.execute(
                "SELECT t, finished, tool, input, is_error FROM tool_calls WHERE job_id = ?"
                " ORDER BY t DESC, rowid DESC LIMIT ?",
                (job_id, tools),
            ).fetchall()
            # the last N, oldest first; a Bash command as a reader is shown it (derive.display_input)
            out["tool_calls"] = [{**dict(r), "input": derive.display_input(r["tool"], r["input"])}
                                 for r in reversed(rows)]
        return out
    except (sqlite3.Error, db.SchemaError) as e:
        # the foreign branch's row SELECT and the tools=N query both read the database;
        # their failure is database_error, not a traceback (story 1.9 item 6)
        return _database_error(e)


@mcp.tool()
async def cancel_job(job_id: str, ctx: Context | None = None) -> dict[str, Any]:
    """Cancel a running job (kills the headless Claude session).

    A job still waiting in the queue leaves it without a session: `dropped`, error
    `cancelled while queued`, started_at null.
    Returns the final record once the session's teardown has settled it (seconds, up to
    2 minutes), with progress meanwhile; one not settled by then carries a `note` saying so."""
    try:
        job = store.get(job_id)
    except KeyError as e:
        return _foreign_or_unknown(job_id, e)
    if not job.done and job._task:
        # fix wave 3 (N1): this cancel is the operator's, so the queue loop's CancelledError
        # writes `cancelled while queued`; a cancel without the mark is the server exiting
        job._cancel_requested = True
        if job.status == "queued" and not job._admitted.is_set():
            # router 12: a job waiting in our queue leaves it without a session, here and now
            # (design §7 "Accepted": dropped, "cancelled while queued"): a task cancelled before
            # its first step never runs a line of its loop, so the drop cannot be left to it
            dispatch.end_queued(store.conn, job, dispatch.CANCELLED_WHILE_QUEUED)
        job._task.cancel()
        if job.status == "queued":
            await asyncio.sleep(0)  # let the cancel land
            if job._task.done() and not job.settled:
                # admitted, but the task was cancelled before run_job's first line: no session
                # was ever started, so it is the same never-ran drop
                dispatch.end_queued(store.conn, job, dispatch.CANCELLED_WHILE_QUEUED)
    # a done job whose finally is still finishing its worktree gets the same wait as a cancelled
    # one: the record handed back is the settled one, with kept/note (DeepSeek review of 6005f94).
    # The wait is the settle hold alone (no cap of its own: 0): a cancelled or done job is held
    # (held_at_cap) until it settles or jobs.SETTLE_BACKSTOP_S passes, the one owner of how long
    # a teardown may take (the SDK's close alone can take ~20 s)
    t0 = time.monotonic()
    await store.wait(job_id, 0, progress=_progress_reporter(ctx), settle=True)
    try:
        out = _job_summary(job)
    except (sqlite3.Error, db.SchemaError) as e:
        # the record cancel_job waited for cannot be built: answer database_error, not the
        # exception (story 1.9a row 1)
        return _database_error(e)
    if not job.settled:
        # the settle wait ran out: say so, with the time it waited — the record is mid-finally
        # and may still move (story 1.9 item 3)
        out["note"] = f"cancel requested; the job had not settled after {round(time.monotonic() - t0)} s"
    return out


# ---------------------------------------------------------------- one-shot

@mcp.tool()
async def local_complete(
    prompt: str,
    model: str | None = None,
    endpoint: str | None = None,
    system: str | None = None,
    max_tokens: int = 4096,
    temperature: float | None = None,
) -> dict[str, Any]:
    """One-shot completion on a local model, no tools, no agent loop. Cheap offload for
    summaries, drafts, classification, translation, boilerplate. Returns the text."""
    reg = _reg()
    try:
        # real_only: this posts to /v1/messages itself, and a router entry serves no such
        # route — the routing layer is a run_agent story, not a one-shot.
        res = await reg.resolve(model=model, endpoint=endpoint, real_only=True)
    except ModelUnavailable as e:
        return e.to_dict()
    body: dict[str, Any] = {
        "model": res.served_model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    if temperature is not None:
        body["temperature"] = temperature
    headers = {**res.endpoint.auth_headers(), "anthropic-version": "2023-06-01"}
    try:
        async with httpx.AsyncClient(timeout=reg.defaults.timeout_s) as c:
            r = await c.post(f"{res.endpoint.url}/v1/messages", json=body, headers=headers)
    except httpx.HTTPError as e:
        return {
            "error": "backend_error", "endpoint": res.endpoint.name, "base_url": res.endpoint.url,
            "message": f"{type(e).__name__}: {e}",
        }
    if r.status_code != 200:
        return {"error": "backend_error", "status": r.status_code, "body": r.text[:2000]}
    try:
        data = r.json()
    except ValueError:
        return {"error": "backend_error", "status": r.status_code, "body": r.text[:2000], "message": "response was not JSON"}
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return {
        "model": res.served_model,
        "endpoint": res.endpoint.name,
        "text": text,
        "stop_reason": data.get("stop_reason"),
        "usage": data.get("usage"),
    }


def _startup_writes(store: JobStore, config: str, transport: str) -> None:
    """The two startup writes to this server's row and the crashed servers' jobs: fill the row
    the status line hangs its jobs on, then sweep jobs a crashed server left behind (spec's lost
    rule — the only status write to another server's jobs). A locked or broken database costs
    the write that failed, never the server: the spec accepts a dropped update, not a dropped server (story 1.7
    item 4). Factor of main(), so a test can drive it without mcp.run()."""
    # two tries: the writes are independent, and a failed describe must not skip the sweep
    # (review of 1.7, finding 11)
    # Both writes touch store.conn, which is the store's first use of the database: a file at
    # a newer schema raises db.SchemaError (a RuntimeError, not a sqlite3.Error) out of migrate
    # through it, and that is the same dropped write, not a crash (review of 1.9a, row 1).
    try:
        store.describe_server(config=config, transport=transport)
    except (sqlite3.Error, db.SchemaError) as e:
        print(f"localagents: startup server-row write skipped: {e}", file=sys.stderr)
    try:
        store.mark_lost_jobs()
    except (sqlite3.Error, db.SchemaError) as e:
        print(f"localagents: startup lost-job sweep skipped: {e}", file=sys.stderr)
    # story 1.7 item 6: the pre-1.5a status/<pid>.json files are dead weight — the tables and
    # the heartbeat replaced them, and no code reads or writes that directory any more
    shutil.rmtree(state_dir() / "status", ignore_errors=True)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="localagents")
    # every subcommand localagents.main dispatches by hand before this parser runs, so that
    # `localagents --help` lists what works (operator 2026-11-04: calibrate was missing)
    ap.add_argument(
        "command", nargs="?", default="serve",
        choices=["serve", "statusline", "smoke", "prune", "seed", "monitor", "d3", "router", "calibrate"],
        help="serve (default): run the MCP server; statusline: running local jobs for a status bar; "
             "smoke: the monitor's browser smoke; prune: delete old job directories; "
             "seed: an offline monitor database from the fixtures; monitor: the monitor page; "
             "d3: serve the router's decision model; router: the router's MCP server; "
             "calibrate: run the calibration set on an endpoint (each takes --help)",
    )
    ap.add_argument("--config", help="path to models.yaml (or set LOCALAGENTS_CONFIG)")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http", "sse"])
    ap.add_argument("--client", choices=sorted(clients.CLIENTS),
                    help="the coordinator this server serves: claude-code or opencode; without it "
                         "a stdio server takes the name the client gives at initialize, and "
                         "falls back to claude-code")
    a = ap.parse_args()
    if a.command == "statusline":  # normally short-circuited in __init__ before this module loads
        from .status import render

        print(render())
        return
    if a.config:
        os.environ["LOCALAGENTS_CONFIG"] = str(Path(a.config).expanduser().resolve())
    # the server row is the status line's spine: fill it, sweep jobs a crashed server left
    # behind (spec's lost rule — the only status write to another server's jobs), mark stopped at exit
    _startup_writes(store, str(config_path()), a.transport)
    atexit.register(store.close)
    try:  # retention: old job directories go at startup and after every job (see jobs.prune)
        prune(_reg().defaults.job_retention_days)
    except OSError:
        pass
    if a.client:
        clients.apply(mcp, a.client)
        print(f"localagents: serving {a.client} (declared)", file=sys.stderr)
    elif a.transport == "stdio":
        clients.adapt_at_handshake(mcp, lambda client, info: print(
            f"localagents: serving {client} (initialize clientInfo {info!r})", file=sys.stderr))
    mcp.run(transport=a.transport)
