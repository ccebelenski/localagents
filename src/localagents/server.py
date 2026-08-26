"""FastMCP server exposing local-model agent delegation to Claude Code."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from .jobs import JobStore
from .registry import ModelUnavailable, Registry, config_path, local_path
from .runner import run_job
from .shim import Shim

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

INSTRUCTIONS = """\
Delegate work to agents running on LOCAL models (llama.cpp / vLLM / Ollama) hosted on the user's
own infrastructure. Each `run_agent` job is a full headless Claude Code session (same tools, same
CLAUDE.md, same working tree) but backed by a local model instead of Anthropic's API.

Workflow:
  1. `list_models` to see which endpoints are up and what they currently serve.
  2. `run_agent(task, model=...)` -> returns a job id immediately (or the result if wait_s > 0).
     If the model is not up anywhere you get `model_unavailable`: ask the user to bring that
     model up (by pool name, on its `host`), then retry once they say it's running.
  3. `wait_job` / `job_status` / `job_log` to follow progress; `cancel_job` to stop.
  4. Review what the local agent did (files_touched, or the worktree diff if isolation=worktree)
     — local models are less reliable than Claude, treat their output as a draft to verify.
Use `local_complete` for cheap one-shot generation with no tools (summaries, drafts, classification).
"""

mcp = MCPServer("localagents", instructions=INSTRUCTIONS)
store = JobStore()
shim = Shim(lambda jid: store.jobs.get(jid))


def _reg() -> Registry:
    return Registry.load()


# ---------------------------------------------------------------- models

@mcp.tool()
async def list_models() -> dict[str, Any]:
    """List configured endpoints (with live health + what they're serving now) and the model pool.

    Pool entries that are currently reachable are marked `available: true` with the endpoint name
    and the served id. Unavailable ones are the menu to ask the user for (by name, on `host`).
    """
    reg = _reg()
    probes = await reg.probe_all()
    endpoints = {
        n: {"base_url": e.url, "backend": e.backend, "host": e.host, "notes": e.notes, **probes[n]}
        for n, e in reg.endpoints.items()
    }
    pool = {}
    for name, spec in reg.models.items():
        where, live_ctx, sid = None, None, None
        for en, pr in probes.items():
            if pr["up"] and (sid := reg.match(spec, pr["models"])):
                where = en
                live_ctx = (pr.get("context") or {}).get(sid)
                break
        entry: dict[str, Any] = {"available": where is not None}
        if where:
            entry["endpoint"] = where
            entry["served_model"] = sid
        for k, v in (("host", spec.host), ("notes", spec.notes), ("tags", spec.tags),
                     ("context", live_ctx or spec.context), ("bring_up", spec.bring_up if not where else "")):
            if v:
                entry[k] = v
        pool[name] = entry
    return {
        "config": str(reg.path),
        "server_cwd": os.getcwd(),
        "default_model": reg.defaults.model,
        "endpoints": endpoints,
        "models": pool,
    }


@mcp.tool()
async def request_model(model: str) -> dict[str, Any]:
    """What to relay to the user to bring up a model that is not currently running.

    Returns the pool entry's `host`/`notes` (and a start command only if one is on file). If the
    model is already up, says so instead.
    """
    reg = _reg()
    try:
        res = await reg.resolve(model=model)
        return {"available": True, "endpoint": res.endpoint.name, "served_model": res.served_model}
    except ModelUnavailable as e:
        return e.to_dict()


@mcp.tool()
async def register_model(
    name: str,
    host: str = "",
    notes: str = "",
    tags: list[str] | None = None,
    served_name: str = "",
    endpoint: str | None = None,
    context: int | None = None,
    bring_up: str = "",
) -> dict[str, Any]:
    """Add or update a model in the pool (persisted to models.yaml).

    Only `name` is required: it is fuzzy-matched against served ids (alphanumerics, case-folded,
    substring), so `qwen3.8-27b` finds `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL`. `host` and `notes`
    are what gets relayed when asking the user to bring it up. The rest are overrides:
    `served_name` (exact id or glob) when fuzzy matching is ambiguous, `endpoint` to prefer one,
    `context` as a fallback window when the backend doesn't report one, `bring_up` a start command
    (goes stale quickly; usually leave empty).
    """
    reg = _reg()
    entry = {
        "host": host,
        "notes": notes,
        "tags": tags or [],
        "served_name": served_name,
        "endpoint": endpoint,
        "context": context,
        "bring_up": bring_up.strip(),
    }
    reg.local.setdefault("models", {})[name] = {k: v for k, v in entry.items() if v not in (None, "", [])}
    reg.save()
    return {"ok": True, "model": name, "entry": reg.local["models"][name], "config": str(local_path(reg.path))}


@mcp.tool()
async def register_endpoint(
    name: str,
    base_url: str,
    backend: str = "other",
    host: str = "local",
    notes: str = "",
) -> dict[str, Any]:
    """Add or update an inference endpoint (persisted to models.yaml) and probe it.

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
) -> dict[str, Any]:
    """Delegate a task to an agent on a local model. Returns a job id immediately.

    The agent is a headless Claude Code session (Read/Edit/Write/Glob/Grep/Bash, project
    CLAUDE.md, same working tree) whose API calls go to the local endpoint. Write the task like a
    brief for a capable but junior engineer: what to do, where, how to verify, and what to report.

    Args:
      task: The brief. Be explicit; local models follow less implicit context than Claude.
      model: Pool name (see list_models) or a served model id/glob. Default from models.yaml.
      endpoint: Force a specific endpoint; with no `model`, uses whatever it is serving.
      cwd: Working directory (default: this server's cwd, i.e. the current project).
      isolation: "none" (work in cwd, like a normal subagent) or "worktree" (fresh git worktree
        on branch local-agent/<job>; kept only if the agent changed something, reported in
        job.worktree with a diffstat). Use worktree for risky/large edits you want to review as a diff.
      wait_s: If > 0, block up to this many seconds and return the result when done (else job id).
      permission_mode: acceptEdits (default) | bypassPermissions | default | plan.
      max_turns: Cap on agent turns (default from models.yaml).
      allowed_tools: Override auto-approved tools.
      system_prompt_append: Extra instructions appended to the Claude Code system prompt.
      resume_job: Continue a previous job's session (same model) with `task` as the next message.
      timeout_s: Wall-clock cap for the job.
    """
    reg = _reg()
    try:
        res = await reg.resolve(model=model, endpoint=endpoint)
    except ModelUnavailable as e:
        return e.to_dict()

    if isolation not in ("none", "worktree"):
        return {"error": "bad_argument", "message": "isolation must be 'none' or 'worktree'"}
    work = str(Path(cwd or os.getcwd()).expanduser().resolve())
    if not Path(work).is_dir():
        return {"error": "bad_argument", "message": f"cwd does not exist: {work}"}

    resume_session = None
    if resume_job:
        try:
            prev = store.get(resume_job)
        except KeyError as e:
            return {"error": "bad_argument", "message": str(e)}
        if not prev.session_id:
            return {"error": "bad_argument", "message": f"job {resume_job} has no session to resume"}
        resume_session = prev.session_id
        work = prev.run_cwd
        isolation = "none"

    job = store.new(
        task=task,
        model=res.served_model,
        endpoint=res.endpoint.name,
        base_url=res.endpoint.url,
        cwd=work,
        run_cwd=work,
        isolation=isolation,
        dump=reg.defaults.dump_requests,
        context=res.context,
    )
    await shim.start()
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
        await store.wait(job.id, wait_s)
    return job.summary()


@mcp.tool()
async def wait_job(job_id: str, timeout_s: int = 120) -> dict[str, Any]:
    """Block up to `timeout_s` seconds for a job to finish; returns its status/result either way."""
    try:
        job = await store.wait(job_id, timeout_s)
    except KeyError as e:
        return {"error": "unknown_job", "message": str(e)}
    return job.summary()


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
    async with httpx.AsyncClient(timeout=reg.defaults.timeout_s) as c:
        r = await c.post(f"{res.endpoint.url}/v1/messages", json=body, headers=headers)
    if r.status_code != 200:
        return {"error": "backend_error", "status": r.status_code, "body": r.text[:2000]}
    data = r.json()
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
    ap.add_argument("--config", help="path to models.yaml (or set LOCALAGENTS_CONFIG)")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http", "sse"])
    a = ap.parse_args()
    if a.config:
        os.environ["LOCALAGENTS_CONFIG"] = str(Path(a.config).expanduser().resolve())
    mcp.run(transport=a.transport)
