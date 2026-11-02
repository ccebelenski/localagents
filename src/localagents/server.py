"""FastMCP server exposing local-model agent delegation to Claude Code."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import Context, MCPServer

from . import db, derive, jobs, status
from .jobs import Job, JobStore, oldest_first, prune, state_dir
from .registry import ModelUnavailable, Registry, config_path, local_path
from .runner import run_job
from .shim import Shim

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

INSTRUCTIONS = """\
Delegate work to agents on LOCAL models (llama.cpp / vLLM) on the user's hardware. A `run_agent`
job is a headless Claude Code session (same tools, CLAUDE.md, working tree) on a local model.

Workflow:
  1. `list_models`: live endpoints and what they serve. Nothing is vetted; you judge suitability.
     Prefer instruct/coder ids with `context` >= 128k (`context_warning` = too small). Say which
     served model you use; ask the user about an unfamiliar or chat-looking id.
  2. `run_agent(task, model=<served id>, wait_s=<the job's timeout_s, default 1800>)` blocks until
     the job ends and returns the record. Claude Code (v2.1.212+) moves a call running past
     CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS (default 2 min) to a background task: you get a task id,
     keep working, and the record arrives as a task notification.
     Several jobs: one blocking run_agent per job, never wait_s=0 + wait_jobs. Each job then
     notifies on its own finish; nothing to re-arm. Set CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=10000
     (settings env) so each call backgrounds in seconds.
     wait_jobs is a fallback for calls that cannot background; mode="any" must be re-armed
     after every return or the remaining jobs finish unseen.
     `model_unavailable`: tell the user what is up, ask them to start a model, retry when told.
     `endpoint=` picks between live endpoints.
     Subagents and non-interactive runs do not auto-background (unless CLAUDE_AUTO_BACKGROUND_TASKS=1):
     use the `local-agent` subagent if installed (a relay; prompt = `model: <served id>`, optional
     header lines, `---`, the brief verbatim), or `run_agent(wait_s=0)` then `wait_job` in-turn.
  3. `cancel_job` stops a job; `list_jobs` / `job_status` / `job_log` follow one.
  4. Review the work (files_touched, or the worktree diff): local models are less reliable than
     Claude; treat output as a draft to verify.
`local_complete`: cheap one-shot generation, no tools (summaries, drafts, classification).
"""

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

@mcp.tool()
async def list_models() -> dict[str, Any]:
    """List configured endpoints with live health and what each is serving right now.

    Per endpoint: `up`, `models` (served ids), `context` (real per-request window per served id),
    and slot/queue occupancy where the backend exposes it. Anything listed as served is usable
    immediately via `run_agent`; if nothing suitable is up, ask the user to start a model.
    A `router` entry serves only its own name: it carries `router: True` and the fleet's
    `context_range` — the smallest and largest real windows probed right now — instead of a
    window of its own.
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
    endpoints = {}
    for n, e in reg.endpoints.items():
        d = {"base_url": e.url, "backend": e.backend, "host": e.host, "notes": e.notes, **probes[n]}
        if e.is_router:
            d["router"] = True
            d["context_range"] = context_range
        endpoints[n] = d
    return {
        "config": str(reg.path),
        "server_cwd": os.getcwd(),
        "default_model": reg.defaults.model,
        "endpoints": endpoints,
        "guidance": GUIDANCE,
    }


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
            msg += f", {derive.tool_call_count(conn, job.id)} tool calls"
            errors = derive.error_count(conn, job.id)
            if errors:
                msg += f", {errors} errors"
            last = derive.last_tool(conn, job.id)  # what the agent is doing right now, from the rows
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
                parts.append(f"{j.id}: {j.status} {int(_elapsed(j))}s, {j.num_turns} turns")
            elif jid in finished:
                done += 1
            elif rows.get(jid) is None:
                parts.append(f"{jid}: ?")  # the read failed, or the row is gone (the wait answers that)
            else:
                row = rows[jid]
                start = row["started_at"] or row["created_at"]  # summary_from_row's elapsed rule
                elapsed = int(time.time() - start) if start is not None else 0
                parts.append(f"{jid}: {row['status']} {elapsed}s, {row['num_turns']} turns")
        try:
            await ctx.report_progress(done, len(job_ids), "; ".join([f"{done}/{len(job_ids)} done", *parts]))
        except Exception:  # a client without progress support must not break the wait
            pass


LOAD_SAMPLE_S = 10.0
_sampler: asyncio.Task | None = None
_shim_port_recorded = False  # shim.port is the process's: describe_server(shim_port=...) runs once


async def _sample_endpoint_load() -> None:
    """Every LOAD_SAMPLE_S: heartbeat this server's row (jobs or not: the status line reads
    liveness from it), and while jobs run also probe their endpoints and write the compact load
    text to endpoint_load. Idle servers write no load rows: old ones age out by ``updated``."""
    while True:
        try:
            store.heartbeat()
            running = [j for j in store.jobs.values() if not j.done]
            if running:
                reg = _reg()
                loads: dict[str, str] = {}
                for name in dict.fromkeys(j.endpoint for j in running):
                    ep = reg.endpoints.get(name)
                    if ep is None:
                        continue
                    text = status.load_summary(await reg.probe(ep))
                    if text:
                        loads[name] = text
                store.record_endpoint_load(loads)
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
    (review of 1.10, D2). On timeout the row as it stands — status still `running`, the in-process wait's
    timeout answer's equivalent. No guard inside: every read here is the database's, and the
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
    deadline = time.monotonic() + timeout_s
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
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _row_summary(row)
        if time.monotonic() >= next_sweep:
            store.mark_lost_jobs()  # the spec's sweep, this time driven by the wait itself
            next_sweep += sweep_s
        if ctx is not None and time.monotonic() >= next_progress:
            start = row["started_at"] or row["created_at"]  # summary_from_row's elapsed rule
            elapsed = int(time.time() - start) if start is not None else 0
            msg = f"{row['status']}: {elapsed}s, {row['num_turns']} turns"
            try:
                conn = store.conn
                msg += f", {derive.tool_call_count(conn, job_id)} tool calls"
                errors = derive.error_count(conn, job_id)
                if errors:
                    msg += f", {errors} errors"
                last = derive.last_tool(conn, job_id)
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
        await asyncio.sleep(min(remaining, poll_s))


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


@mcp.tool()
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
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delegate a task to an agent on a local model and (with `wait_s`) wait for the result.

    The agent is a headless Claude Code session (Read/Edit/Write/Glob/Grep/Bash, project
    CLAUDE.md, same working tree) whose API calls go to the local endpoint. Write the task like a
    brief for a capable but junior engineer: what to do, where, how to verify, what to report.

    Args:
      task: The brief. Be explicit; local models follow less implicit context than Claude.
      model: A served model id, glob or fuzzy name (see list_models). Default: `defaults.model`
        from models.yaml if set, else whatever the first live endpoint is serving.
      endpoint: Force a specific endpoint; with no `model`, uses whatever it is serving.
      cwd: Working directory (default: this server's cwd, i.e. the current project).
      isolation: "none" (work in cwd) or "worktree" (fresh git worktree on branch
        local-agent/<job id>, kept only if the agent changed something; see job.worktree).
      wait_s: If > 0, block up to this many seconds and return the final record, else return
        the job id at once. Pass the job's timeout: Claude Code backgrounds a long call and
        delivers the record as a task notification; progress is reported every minute.
        Use for every job, swarms included.
      permission_mode: acceptEdits (default) | bypassPermissions | default | plan.
      max_turns: Cap on agent turns (default from models.yaml).
      allowed_tools: Override auto-approved tools.
      system_prompt_append: Extra instructions appended to the Claude Code system prompt.
      resume_job: Continue a previous job's session with `task` as the next message (same
        server process only; the store is in memory — job_status, job_log and
        list_jobs(all=True) read any server's jobs instead). Runs on that job's model,
        endpoint, cwd and isolation unless `model` is given; a removed worktree is recreated
        at its base. Refused while that job runs.
      timeout_s: Wall-clock cap for the job.
    """
    if isolation not in ("none", "worktree"):
        return {"error": "bad_argument", "message": "isolation must be 'none' or 'worktree'"}
    work = str(Path(cwd or os.getcwd()).expanduser().resolve())
    if not Path(work).is_dir():
        return {"error": "bad_argument", "message": f"cwd does not exist: {work}"}

    run_cwd = work
    resume_session = None
    worktree: dict[str, Any] | None = None
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
        # Continue on the model that ran the session, not on whatever is the default now.
        if model is None:
            model, endpoint = prev.model, endpoint or prev.endpoint

    reg = _reg()
    # M1: a named router entry answers not_routable before resolve. resolve probes the fleet,
    # and with the d3 server down that probe buries the true answer (routing is not built yet)
    # under a misleading "no endpoint serving 'auto'". The post-resolve guard below stays for
    # the unnamed case: defaults.model naming a router entry is only known after resolve.
    pinned = reg.endpoints.get(endpoint) if endpoint else None
    named_router = pinned if pinned is not None and pinned.is_router else (
        reg.endpoints.get(model) if model and model in reg.endpoints else None)
    if named_router is not None and named_router.is_router:
        return {
            "error": "not_routable",
            "message": f"endpoint '{named_router.name}' is a router; routing is not built yet, name a model",
            "guidance": GUIDANCE,
        }
    try:
        res = await reg.resolve(model=model, endpoint=endpoint)
    except ModelUnavailable as e:
        return {**e.to_dict(), "guidance": GUIDANCE}
    if res.endpoint.is_router:
        # routing is a later story; refuse before the shim starts and before new(), so no job
        # record is left `queued` for good (review of 1.7, finding 4, same reasoning)
        return {
            "error": "not_routable",
            "message": f"endpoint '{res.endpoint.name}' is a router; routing is not built yet, name a model",
            "guidance": GUIDANCE,
        }

    # Everything that can raise runs before new() or cannot raise after it: a job that exists
    # without its task stays `queued` for good (review of 1.7, finding 4). The shim serves every
    # job and does not depend on this one, so it starts first.
    await shim.start()
    try:
        job = store.new(
            task=task,
            model=res.served_model,
            endpoint=res.endpoint.name,
            base_url=res.endpoint.url,
            cwd=work,
            run_cwd=run_cwd,
            isolation=isolation,
            backend=res.endpoint.backend,
            host=res.endpoint.host,
            worktree=worktree,
            dump=reg.defaults.dump_requests,
            log_events=reg.defaults.log_events or os.environ.get("LOCALAGENTS_LOG_EVENTS") == "1",
            context=res.context,
        )
    except (sqlite3.Error, db.SchemaError) as e:
        # a refusal like the others, not a bare tool error: the SDK keeps an exception's text out
        # of the reply, and the caller should see "database is locked" (review of 1.7, finding 8).
        # The store opens lazily, so this new() is often the first use and a newer-schema file
        # raises db.SchemaError (a RuntimeError) here too — the guard must take it (1.9a review)
        return {"error": "database_error", "message": f"the job could not be recorded: {e}"}
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
    job._task = asyncio.create_task(
        run_job(
            job, reg, res,
            permission_mode=permission_mode,
            max_turns=max_turns,
            allowed_tools=allowed_tools,
            system_prompt_append=system_prompt_append,
            resume_session=resume_session,
            timeout_s=timeout_s,
            shim_base_url=shim.base_url_for(job.id),
        )
    )
    if wait_s > 0:
        await store.wait(job.id, wait_s, progress=_progress_reporter(ctx))
    try:
        return job.summary()
    except (sqlite3.Error, db.SchemaError) as e:
        # the final record's derive reads are reads too: their failure is the database's
        # problem, not the caller's traceback (story 1.9a row 1)
        return _database_error(e)


@mcp.tool()
async def wait_job(job_id: str, timeout_s: int = 1800, ctx: Context | None = None) -> dict[str, Any]:
    """Block up to `timeout_s` seconds for a job to finish; returns its status/result either way.

    Any server's job: this server's waits on its settle event, another server's is polled from
    its row in the shared database (story 1.10); only `cancel_job` and `run_agent(resume_job=)`
    still refuse another server's job. Progress is reported every minute while waiting, so a
    long timeout is safe to pass.
    """
    try:
        job = await store.wait(job_id, timeout_s, progress=_progress_reporter(ctx))
        # inside the guard: the summary()'s own derive reads are reads like the wait's
        # (story 1.9 item 6, made true here by 1.9a row 1 — it sat after the try)
        return job.summary()
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
    Fallback only: prefer one blocking run_agent per job (each notifies on its own finish).
    mode="any" must be re-armed after every return or the rest finish unseen.
    Any server's ids mix (story 1.10): this server's jobs wait on their settle events, another
    server's are polled from their rows.
    Returns done (final records) and running (ids, status, elapsed). Progress every minute.
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
            tasks.append(asyncio.ensure_future(store.wait_many(in_process_ids, timeout_s, mode=mode)))
        tasks.extend(asyncio.ensure_future(_wait_foreign(jid, timeout_s, None)) for jid in foreign_ids)
        # mode="any" returns as soon as ANY task finishes; mode="all" waits for all of them.
        # Every task is internally bounded by timeout_s, so the asyncio.wait timeout is a belt.
        await asyncio.wait(
            tasks, timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED if mode == "any" else asyncio.ALL_COMPLETED,
        )
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
                    done.append(j.summary())
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


def _database_error(e: Exception) -> dict[str, Any]:
    """The read tools' answer when the database itself fails: a refusal like run_agent's, so
    the caller sees "database is locked" rather than a bare tool error (review of 1.8)."""
    return {"error": "database_error", "message": f"the database could not be read: {e}"}


def _row_summary(row: Any, include_result: bool = True) -> dict[str, Any]:
    """derive.summary_from_row, but a row it cannot read (another writer's garbled worktree
    JSON, say) answers with what the row does say plus ``row_error``, never an exception: one
    bad row must not break list_jobs(all=True) for every caller."""
    try:
        return derive.summary_from_row(store.conn, row, include_result=include_result)
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
    """Current status, last activity, files touched, and result (if finished) of a job.

    Reads any localagents server's job: an in-process job gets its live summary, otherwise
    the row and its read-time derivations from the shared database."""
    try:
        return store.get(job_id).summary()
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
            return [j.summary(include_result=False) for j in (mine[-limit:] if limit > 0 else mine)]
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
            out["tool_calls"] = [dict(r) for r in reversed(rows)]  # the last N, oldest first
        return out
    except (sqlite3.Error, db.SchemaError) as e:
        # the foreign branch's row SELECT and the tools=N query both read the database;
        # their failure is database_error, not a traceback (story 1.9 item 6)
        return _database_error(e)


@mcp.tool()
async def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel a running job (kills the headless Claude session)."""
    try:
        job = store.get(job_id)
    except KeyError as e:
        return _foreign_or_unknown(job_id, e)
    if not job.done and job._task:
        job._task.cancel()
    # a done job whose finally is still finishing its worktree gets the same wait as a cancelled
    # one: the record handed back is the settled one, with kept/note (DeepSeek review of 6005f94)
    await store.wait(job_id, 15)
    try:
        out = job.summary()
    except (sqlite3.Error, db.SchemaError) as e:
        # the record cancel_job waited for cannot be built: answer database_error, not the
        # exception (story 1.9a row 1)
        return _database_error(e)
    if not job.settled:
        # the 15 s wait timed out: say so — the record is mid-finally and may still move
        # (story 1.9 item 3)
        out["note"] = "cancel requested; the job had not settled after 15 s"
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
    ap.add_argument(
        "command", nargs="?", default="serve", choices=["serve", "statusline"],
        help="serve (default): run the MCP server; statusline: print running local jobs for a status bar",
    )
    ap.add_argument("--config", help="path to models.yaml (or set LOCALAGENTS_CONFIG)")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http", "sse"])
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
    mcp.run(transport=a.transport)
