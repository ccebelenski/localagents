"""The recording stub MCP server for the calibration set (router 9c;
docs/superpowers/specs/2026-11-02-calibration-set-design.md §3, §11).

The tool-use probes need a `localagents` MCP server the session can call without a fleet
behind it. This module serves the real server's ten tool names — same parameters, same
docstring first lines — and answers from `CANNED`, a module constant checkers can import and
compare against. Nothing dispatches: everything that would put work on a model answers
`{"error": "refused", ...}`. Every call appends one JSON line ``{"t", "tool", "args"}`` to the
file named by $LOCALAGENTS_STUB_LOG; with no such env the answers stay and nothing is written,
so a probe whose checker does not grade adherence still runs the same server.

Spawn it the way the runner spawns the real one — ``python3 -m localagents.router.stubmcp``,
transport stdio. The canned fleet is invented (box-a / box-b, coder-small / coder-large):
nothing from an operator's config reaches a probe."""

from __future__ import annotations

import copy
import json
import os
import time
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

mcp = MCPServer(
    "localagents",
    instructions=(
        "The localagents job tools, as a calibration stub: reading answers comes from a fixed "
        "recording and nothing is dispatched. Treat it exactly like the real server."
    ),
)

# the one job every read tool knows about (§3's canned fleet): probed, answered, and logged
ANSWERED_JOB_ID = "f00dcafe-0000-4000-8000-000000000001"

_JOB: dict[str, Any] = {
    "id": ANSWERED_JOB_ID,
    "status": "succeeded",
    "num_turns": 42,
    "elapsed_s": 613,
    "model": "coder-large",
    "endpoint": "box-b",
    "task": "rewrite the weekly digest script in the report repo",
}

# what the read tools answer; CANNED is the single source of truth checkers compare against
CANNED: dict[str, Any] = {
    "list_models": {
        "endpoints": {
            "box-a": {"up": True, "models": ["coder-small"], "context": {"coder-small": 131072}},
            "box-b": {"up": True, "models": ["coder-large"], "context": {"coder-large": 262144}},
        },
    },
    "job_status": dict(_JOB),
    "job_log": {
        "id": ANSWERED_JOB_ID,
        "lines": [
            "assistant: I will read the digest script and rewrite it.",
            "tool Read: report/digest.py",
            "assistant: rewrote report/digest.py; the test suite passes.",
        ],
    },
    "list_jobs": [dict(_JOB)],
}

_REFUSAL = {"error": "refused", "message": "this is a calibration stub; nothing is dispatched"}
_UNKNOWN_JOB = {"error": "unknown_job"}


def _record(tool: str, args: dict[str, Any]) -> None:
    """One JSON line per call, appended while $LOCALAGENTS_STUB_LOG names a file; a recording
    the mcp-stats checker reads to prove the session obeyed its read-only instruction."""
    path = os.environ.get("LOCALAGENTS_STUB_LOG")
    if not path:
        return
    with open(path, "a") as fh:
        fh.write(json.dumps({"t": time.time(), "tool": tool, "args": args}) + "\n")


@mcp.tool()
async def list_models() -> dict[str, Any]:
    """List configured endpoints with live health and what each is serving right now."""
    _record("list_models", {})
    return copy.deepcopy(CANNED["list_models"])


@mcp.tool()
async def register_endpoint(name: str, base_url: str, backend: str = "other",
                            host: str = "local", notes: str = "") -> dict[str, Any]:
    """Add or update an inference endpoint (persisted to models.local.yaml) and probe it."""
    _record("register_endpoint", {"name": name, "base_url": base_url, "backend": backend,
                                  "host": host, "notes": notes})
    return dict(_REFUSAL)


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
    priority: str = "normal",
    admit: str = "queue",
    tolerance: float | None = None,
    avoid_job: str | None = None,
    background_tasks: bool | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delegate a task to an agent on a local model and (with `wait_s`) wait for the result."""
    _record("run_agent", {"task": task, "model": model, "endpoint": endpoint, "cwd": cwd,
                          "isolation": isolation, "wait_s": wait_s,
                          "permission_mode": permission_mode, "max_turns": max_turns,
                          "allowed_tools": allowed_tools,
                          "system_prompt_append": system_prompt_append,
                          "resume_job": resume_job, "timeout_s": timeout_s,
                          "priority": priority, "admit": admit, "tolerance": tolerance,
                          "avoid_job": avoid_job, "background_tasks": background_tasks})
    return dict(_REFUSAL)


@mcp.tool()
async def wait_job(job_id: str, timeout_s: int = 1800,
                   ctx: Context | None = None) -> dict[str, Any]:
    """Block up to `timeout_s` seconds for a job to finish; returns its status/result either way."""
    _record("wait_job", {"job_id": job_id, "timeout_s": timeout_s})
    return dict(_REFUSAL)


@mcp.tool()
async def wait_jobs(job_ids: list[str], timeout_s: int = 1800, mode: str = "all",
                    ctx: Context | None = None) -> dict[str, Any]:
    """Block for several jobs: all finished (mode="all"), first finished (mode="any"), or timeout_s."""
    _record("wait_jobs", {"job_ids": job_ids, "timeout_s": timeout_s, "mode": mode})
    return dict(_REFUSAL)


@mcp.tool()
async def job_status(job_id: str) -> dict[str, Any]:
    """Current status, last activity, files changed, and result (if finished) of a job."""
    _record("job_status", {"job_id": job_id})
    if job_id == ANSWERED_JOB_ID:
        return copy.deepcopy(CANNED["job_status"])
    return dict(_UNKNOWN_JOB)


@mcp.tool()
async def list_jobs(include_finished: bool = True, all: bool = False,
                    limit: int = 50) -> list[dict[str, Any]]:
    """List jobs (newest last). This server session's; with all=True, every localagents"""
    _record("list_jobs", {"include_finished": include_finished, "all": all, "limit": limit})
    return copy.deepcopy(CANNED["list_jobs"])


@mcp.tool()
async def job_log(job_id: str, tail_lines: int = 80, tools: int = 0) -> dict[str, Any]:
    """Tail of a job's human-readable transcript (assistant text, tool calls, results)."""
    _record("job_log", {"job_id": job_id, "tail_lines": tail_lines, "tools": tools})
    return copy.deepcopy(CANNED["job_log"])


@mcp.tool()
async def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel a running job (kills the headless Claude session)."""
    _record("cancel_job", {"job_id": job_id})
    return dict(_REFUSAL)


@mcp.tool()
async def local_complete(prompt: str, model: str | None = None, endpoint: str | None = None,
                         system: str | None = None, max_tokens: int = 4096,
                         temperature: float | None = None) -> dict[str, Any]:
    """One-shot completion on a local model, no tools, no agent loop. Cheap offload for"""
    _record("local_complete", {"prompt": prompt, "model": model, "endpoint": endpoint,
                               "system": system, "max_tokens": max_tokens,
                               "temperature": temperature})
    return dict(_REFUSAL)


if __name__ == "__main__":
    mcp.run(transport="stdio")
