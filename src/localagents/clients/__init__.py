"""The client a localagents server serves: the coordinator that calls its tools (Epic 6.1).

Two clients, Claude Code and OpenCode. Each has its own module holding the sentences that are
true of it alone; the text both share lives here with a named slot for each client sentence.
Which client: declared by ``serve --client``; else, on stdio (one client per process), the name
the client gives in its ``initialize`` request (``clientInfo.name``: Claude Code sends
``claude-code``, OpenCode ``opencode`` — captured 2026-11-17, Claude Code 2.1.239, opencode
2.3.1); else Claude Code, every registration made before 6.1. OpenCode does not mark the
environment of the MCP servers it starts (6.1's end-to-end run), so the environment says nothing.

Nothing here is about the harness a job runs in: a job is a headless Claude Code session
whichever client started it, so the sentences saying so are shared text (they move to the
harness modules with Epic 6.3).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import claude_code, opencode

CLIENTS = {claude_code.NAME: claude_code, opencode.NAME: opencode}
DEFAULT = claude_code.NAME

# The server's instructions. Slots: `wait` (how the client lives with a call that blocks until
# the job ends, and how it runs several), `wait_more` (the rest of it, after the shared
# model_unavailable lines), `reviewer` (what the local models are compared with).
INSTRUCTIONS = """\
Delegate work to agents on LOCAL models (llama.cpp / vLLM) on the user's hardware. A `run_agent`
job is a headless Claude Code session (same tools, CLAUDE.md, working tree) on a local model.

Workflow:
  1. `list_models`: live endpoints and what they serve. Nothing is vetted; you judge suitability.
     Prefer instruct/coder ids with `context` >= 128k (`context_warning` = too small). Say which
     served model you use; ask the user about an unfamiliar or chat-looking id.
  2. `run_agent(task, model=<served id>, wait_s=<the job's timeout_s, default 1800>)` blocks until
     the job ends and returns the record. {wait}
     `model_unavailable`: tell the user what is up, ask them to start a model, retry when told.
     `endpoint=` picks between live endpoints.
{wait_more}\
  3. `cancel_job` stops a job; `list_jobs` / `job_status` / `job_log` follow one.
  4. Review the work (`diff_files`: a worktree job's changes, null without one; `files_touched`
     misses shell writes): local models are less reliable than {reviewer}; treat output as a draft.
`local_complete`: cheap one-shot generation, no tools (summaries, drafts, classification).
"""

# run_agent's description, as the raw docstring it was before 6.1 (the indentation is the
# client's to read, so it is kept). Slots: `infer` (what local models are compared with) and
# `wait_s` (the client's half of the wait_s argument).
RUN_AGENT = """Delegate a task to an agent on a local model and (with `wait_s`) wait for the result.

    A headless Claude Code session (same tools, project CLAUDE.md, working tree): brief it
    like a junior engineer (what, where, how to verify).

    Args:
      task: The brief; be explicit, local models infer less than {infer}.
      model: Served id, glob or fuzzy name (list_models), or a router entry's name to route
        (the record names the real endpoint and model, plus `routed_by`).
      endpoint: Force an endpoint (no `model`: whatever it serves). A router entry routes
        alone: a real `model` or `endpoint` beside it is bad_argument.
      cwd: Working directory (default: the server's).
      isolation: none | worktree (git worktree on local-agent/<job id>, kept only if changed).
      wait_s: If > 0, block (through the queue: `queued #n, ~m min`) for the final record,
        else return the job id. Counts from admission, like timeout_s. Pass the job's timeout
        (shorter: `running` while the session works); {wait_s}
      permission_mode: acceptEdits (default) | bypassPermissions | default | plan. max_turns: Turn cap.
      allowed_tools: Override auto-approved tools. system_prompt_append: Appended to the system prompt.
      resume_job: Continue a job's session with `task` on its model, endpoint, cwd and
        isolation unless a real `model` is given. Refused while it runs.
      timeout_s: Session wall-clock cap, from admission. priority: urgent | normal | background.
      admit: queue | now (would_queue, no job, no wait) | force (skip the wait).
      tolerance: Routed: acceptable shortfall distance from the best row.
      avoid_job: A job id (routed or named); routing never picks its model, naming it too is
        bad_argument.
      background_tasks: false: foreground only, the job ends at its result; true (config
        default) allows background commands; a resume keeps its job's.
    """


def from_client_info(params: Mapping[str, Any] | None) -> str | None:
    """The client an ``initialize`` request's ``clientInfo.name`` names, or None for a name that is
    not one of ours (or no clientInfo at all)."""
    info = (params or {}).get("clientInfo") or {}
    name = info.get("name") if isinstance(info, Mapping) else None
    return name if name in CLIENTS else None


def instructions(client: str) -> str:
    c = CLIENTS[client]
    return INSTRUCTIONS.format(wait=c.WAIT, wait_more=c.WAIT_MORE, reviewer=c.REVIEWER)


def run_agent_description(client: str) -> str:
    c = CLIENTS[client]
    return RUN_AGENT.format(infer=c.REVIEWER, wait_s=c.WAIT_S)


def apply(mcp: Any, client: str) -> None:
    """Give a server the client's text: the instructions sent at initialize and run_agent's
    description. Called once, before the server runs (initialize reads the instructions then)."""
    mcp._lowlevel_server.instructions = instructions(client)
    mcp._tool_manager.get_tool("run_agent").description = run_agent_description(client)


def adapt_at_handshake(mcp: Any, on_client: Any) -> None:
    """Answer each ``initialize`` with the text of the client it names (stdio only: one client per
    process, and the tool descriptions are the process's). The SDK builds the result in
    ``ServerRunner._handle_initialize`` from options fixed before the handshake, so the hook sets
    the tools' text, then overwrites the result's instructions. ``on_client(client, info)`` is
    told what was chosen. A test pins the SDK method this wraps."""
    from mcp.server.runner import ServerRunner

    original = ServerRunner._handle_initialize

    def handle(self: Any, params: Mapping[str, Any] | None) -> Any:
        client = from_client_info(params) or DEFAULT
        apply(mcp, client)
        result = original(self, params)
        result.instructions = instructions(client)
        on_client(client, (params or {}).get("clientInfo"))
        return result

    ServerRunner._handle_initialize = handle
