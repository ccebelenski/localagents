"""FastMCP server exposing local-model agent delegation to Claude Code."""

from __future__ import annotations

import asyncio
import atexit
import os
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import Context, MCPServer

from . import status
from .jobs import Job, JobStore
from .registry import ModelUnavailable, Registry, config_path, local_path
from .runner import run_job
from .shim import Shim

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

INSTRUCTIONS = """\
Delegate work to agents on LOCAL models (llama.cpp / vLLM) on the user's hardware. A `run_agent`
job is a headless Claude Code session (same tools, CLAUDE.md, working tree) on a local model.

Workflow:
  1. `list_models`: which endpoints are up and what they serve. Nothing is configured or vetted;
     only you judge suitability. Prefer instruct/coder ids with `context` >= 128k
     (`context_warning` = too small for an agent). Say which served model you will use; if an id
     is unfamiliar or looks like a chat model, ask the user.
  2. `run_agent(task, model=<served id>, wait_s=<the job's timeout_s, default 1800>)` blocks until
     the job ends and returns the record. Claude Code moves an MCP call running past
     CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS (default 2 min; v2.1.212+) to a background task: you get a
     task id, keep working, and the record arrives as a task notification. Progress every minute
     keeps the call alive.
     Several jobs: start each with `wait_s=0` (returns the id at once), then ONE
     `wait_jobs([ids], timeout_s=...)` call; `mode="any"` returns on the first finish so you can
     review it and call again on the rest. One call and one notification for all of them.
     `model_unavailable`: nothing suitable is up; tell the user what is, ask them to start a model,
     retry when they say so. `endpoint=` picks between live endpoints.
     In subagents and non-interactive runs auto-backgrounding is off (unless
     CLAUDE_AUTO_BACKGROUND_TASKS=1): use the `local-agent` subagent if installed (a relay; prompt
     = `model: <served id>`, optional header lines, `---`, the brief verbatim), or
     `run_agent(wait_s=0)` then `wait_job`, not ending your turn while the job runs.
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

mcp = MCPServer("localagents", instructions=INSTRUCTIONS)
store = JobStore()
shim = Shim(lambda jid: store.jobs.get(jid))


def _reg() -> Registry:
    return Registry.load()


# ---------------------------------------------------------------- models

@mcp.tool()
async def list_models() -> dict[str, Any]:
    """List configured endpoints with live health and what each is serving right now.

    Per endpoint: `up`, `models` (served ids), `context` (real per-request window per served id),
    and slot/queue occupancy where the backend exposes it. Anything listed as served is usable
    immediately via `run_agent`; if nothing suitable is up, ask the user to start a model.
    """
    reg = _reg()
    probes = await reg.probe_all()
    endpoints = {
        n: {"base_url": e.url, "backend": e.backend, "host": e.host, "notes": e.notes, **probes[n]}
        for n, e in reg.endpoints.items()
    }
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
    `backend` is one of llama.cpp | vllm | ollama | other (informational).
    """
    reg = _reg()
    reg.local.setdefault("endpoints", {})[name] = {
        k: v for k, v in dict(base_url=base_url, backend=backend, host=host, notes=notes).items() if v
    }
    reg.save()
    reg = _reg()
    probe = await reg.probe(reg.endpoints[name])
    return {"ok": True, "endpoint": name, "probe": probe, "config": str(local_path(reg.path))}


def _progress_reporter(ctx: Context | None):
    """Progress callback for a blocking wait: one notification per interval, never fatal."""
    if ctx is None:
        return None

    async def report(job: Job) -> None:
        elapsed = round(time.time() - (job.started_at or job.created_at))
        msg = f"{job.status}: {elapsed}s, {job.num_turns} turns, {job.tool_calls} tool calls"
        if job.last_activity:
            msg += f"; {job.last_activity[:120]}"
        try:
            await ctx.report_progress(elapsed, None, msg)
        except Exception:  # a client without progress support must not break the wait
            pass

    return report


def _progress_reporter_many(ctx: Context | None):
    """Progress callback for `wait_many`: one line per interval covering every job."""
    if ctx is None:
        return None

    async def report(jobs: list[Job]) -> None:
        done = sum(j.done for j in jobs)
        parts = [f"{done}/{len(jobs)} done"]
        for j in jobs:
            if not j.done:
                elapsed = round(time.time() - (j.started_at or j.created_at))
                parts.append(f"{j.id}: {j.status} {elapsed}s, {j.num_turns} turns")
        try:
            await ctx.report_progress(done, len(jobs), "; ".join(parts))
        except Exception:
            pass

    return report


LOAD_SAMPLE_S = 10.0
_sampler: asyncio.Task | None = None


async def _sample_endpoint_load() -> None:
    """While jobs run, probe their endpoints every LOAD_SAMPLE_S and publish a compact load line
    (slots busy, requests queued, KV cache) for the status bar. Idle servers publish nothing."""
    while True:
        try:
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
                store.endpoint_load = loads
                store.publish()
            elif store.endpoint_load:
                store.endpoint_load = {}
                store.publish()
        except Exception:  # noqa: BLE001  the status bar must never take the server down
            pass
        await asyncio.sleep(LOAD_SAMPLE_S)


def _ensure_sampler() -> None:
    global _sampler
    if _sampler is None or _sampler.done():
        _sampler = asyncio.create_task(_sample_endpoint_load())


# ---------------------------------------------------------------- agents

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
        local-agent/<job>, kept only if the agent changed something; see job.worktree).
      wait_s: If > 0, block up to this many seconds and return the final record, else return
        the job id at once. Pass the job's timeout: Claude Code backgrounds a long call and
        delivers the record as a task notification; progress is reported every minute.
      permission_mode: acceptEdits (default) | bypassPermissions | default | plan.
      max_turns: Cap on agent turns (default from models.yaml).
      allowed_tools: Override auto-approved tools.
      system_prompt_append: Extra instructions appended to the Claude Code system prompt.
      resume_job: Continue a previous job's session with `task` as the next message (same
        server process only; the store is in memory). Runs on that job's model, endpoint, cwd
        and isolation unless `model` is given; a removed worktree is recreated at its base.
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
            return {"error": "bad_argument", "message": str(e)}
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
    try:
        res = await reg.resolve(model=model, endpoint=endpoint)
    except ModelUnavailable as e:
        return {**e.to_dict(), "guidance": GUIDANCE}

    job = store.new(
        task=task,
        model=res.served_model,
        endpoint=res.endpoint.name,
        base_url=res.endpoint.url,
        cwd=work,
        run_cwd=run_cwd,
        isolation=isolation,
        worktree=worktree,
        dump=reg.defaults.dump_requests,
        context=res.context,
    )
    await shim.start()
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
    return job.summary()


@mcp.tool()
async def wait_job(job_id: str, timeout_s: int = 1800, ctx: Context | None = None) -> dict[str, Any]:
    """Block up to `timeout_s` seconds for a job to finish; returns its status/result either way.

    Progress is reported every minute while waiting, so a long timeout is safe to pass.
    """
    try:
        job = await store.wait(job_id, timeout_s, progress=_progress_reporter(ctx))
    except KeyError as e:
        return {"error": "unknown_job", "message": str(e)}
    return job.summary()


@mcp.tool()
async def wait_jobs(
    job_ids: list[str],
    timeout_s: int = 1800,
    mode: str = "all",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Block for several jobs at once: until all have finished (`mode="all"`), or until the first
    one has (`mode="any"`), or `timeout_s` passes.

    The way to run a swarm: start each job with `run_agent(..., wait_s=0)`, which returns at once,
    then make this one call. It is one tool call however many jobs there are, so it costs one
    auto-background wait instead of one per job, and one notification. Returns `done` (final
    records) and `running` (ids still going, with status and elapsed time) so with `mode="any"`
    you can review finished jobs and call again on the rest. Progress is reported every minute.
    """
    if mode not in ("all", "any"):
        return {"error": "bad_argument", "message": "mode must be 'all' or 'any'"}
    if not job_ids:
        return {"error": "bad_argument", "message": "job_ids is empty"}
    try:
        jobs = await store.wait_many(job_ids, timeout_s, mode=mode, progress=_progress_reporter_many(ctx))
    except KeyError as e:
        return {"error": "unknown_job", "message": str(e)}
    done = [j for j in jobs if j.done]
    running = [j for j in jobs if not j.done]
    if not running:
        outcome = "all_done"
    elif done:
        outcome = "some_done"
    else:
        outcome = "timeout"
    return {
        "outcome": outcome,
        "done": [j.summary() for j in done],
        "running": [
            {"id": j.id, "status": j.status, "elapsed_s": j.summary()["elapsed_s"], "num_turns": j.num_turns}
            for j in running
        ],
    }


@mcp.tool()
async def job_status(job_id: str) -> dict[str, Any]:
    """Current status, last activity, files touched, and result (if finished) of a job."""
    try:
        return store.get(job_id).summary()
    except KeyError as e:
        return {"error": "unknown_job", "message": str(e)}


@mcp.tool()
async def list_jobs(include_finished: bool = True) -> list[dict[str, Any]]:
    """List jobs from this server session (newest last)."""
    return [
        j.summary(include_result=False)
        for j in store.jobs.values()
        if include_finished or not j.done
    ]


@mcp.tool()
async def job_log(job_id: str, tail_lines: int = 80) -> dict[str, Any]:
    """Tail of a job's human-readable transcript (assistant text, tool calls, results)."""
    try:
        job = store.get(job_id)
    except KeyError as e:
        return {"error": "unknown_job", "message": str(e)}
    return {"job_id": job_id, "status": job.status, "log_dir": job.log_dir, "transcript": job.transcript_tail(tail_lines)}


@mcp.tool()
async def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel a running job (kills the headless Claude session)."""
    try:
        job = store.get(job_id)
    except KeyError as e:
        return {"error": "unknown_job", "message": str(e)}
    if job.done:
        return job.summary()
    if job._task:
        job._task.cancel()
    await store.wait(job_id, 15)
    return job.summary()


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
        res = await reg.resolve(model=model, endpoint=endpoint)
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
    headers = {"x-api-key": "local", "authorization": "Bearer local", "anthropic-version": "2023-06-01"}
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
    atexit.register(status.remove)
    mcp.run(transport=a.transport)
