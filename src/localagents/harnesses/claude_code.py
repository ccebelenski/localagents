"""Claude Code as the harness a job runs in (Epic 6.3): a headless Claude Code session through
the Claude Agent SDK, pointed at the job's shim with Anthropic's environment variables.

Everything here is Claude Code's own: its environment (``build_env``), the foreground-only rule
(its Monitor tool, its Bash ``run_in_background``), its options (the ``claude_code`` system
prompt preset, permission modes, setting sources) and its message stream (the SDK's typed
messages: ``init``, ``compact_boundary``, the ``task_*`` lifecycle, ``ResultMessage``). Moved from
runner.py unchanged; runner.py keeps what every harness shares.
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Any

from claude_agent_sdk import (
    TERMINAL_TASK_STATUSES,
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

# the write rule's list is the read rule's (files_touched); the stored cuts and the shown form
# are derive's too (classify_call and feedback read the cut back, last_tool shows the line)
from ..derive import (COMMAND_CHARS, EDIT_TOOLS, TOOL_INPUT_CHARS, activity_line,
                      background_tasks_named, held_open, held_open_line)
from ..jobs import Job
from ..registry import Defaults, Resolution
from . import SessionSpec

NAME = "claude-code"

# A foreground-only session (Defaults.background_tasks, or the run_agent call's own value,
# false): the tools it never gets whatever the config's disallowed_tools says. The documented
# switch (CLAUDE_CODE_DISABLE_BACKGROUND_TASKS) leaves Monitor running in the background, and a
# Monitor watch holds a headless run open past its result.
FOREGROUND_DISALLOWED = ("Monitor",)
# ...and the sentence its system prompt gets: the harness itself tells a model whose foreground
# `sleep` it blocks to "use run_in_background", a parameter this session does not have
FOREGROUND_NOTE = (
    "Background tasks are off in this session: the Bash tool has no run_in_background parameter, "
    "so run every command in the foreground and wait for it to finish; do not start watchers or "
    "background processes and do not poll with sleep, and give a long command a larger timeout "
    "(the Bash tool's timeout parameter, up to the maximum it states) instead."
)


def build_env(res: Resolution, base_url: str, defaults: Defaults, background_tasks: bool) -> dict[str, str]:
    """The session's whole environment. ``background_tasks`` is the job's resolved value
    (run_job resolves it); false adds the foreground switch and raises the Bash timeouts. The
    endpoint's own ``env:`` is applied last and wins over all of it."""
    m = res.served_model
    env = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": "local",
        "ANTHROPIC_API_KEY": "local",
        "ANTHROPIC_MODEL": m,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": m,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": m,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": m,
        "ANTHROPIC_SMALL_FAST_MODEL": m,
        # vLLM docs: per-request attribution hash defeats prefix caching
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
    }
    if res.context:
        # Claude Code assumes a 200k window for models it does not recognise; tell it the real one
        # so usage-driven auto-compact fires before the backend rejects the prompt. Its compact
        # threshold is window - max_output_tokens (32k default), so shrink the output budget on
        # small windows or the threshold would sit at ~0. Floor of 8k: the compact summary itself
        # runs ~7k output tokens. Below ~100k the session still thrashes (20k fixed prompt +
        # summary + re-attached files refill the window) — that is a model-hosting limit.
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(res.context)
        if res.context < 128000:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max(8192, res.context // 8))
    if not background_tasks:
        # documented: removes run_in_background from Bash/PowerShell/subagents and stops the
        # automatic backgrounding of timed-out shell commands; a foreground command is then
        # killed at its timeout, so the default and the ceiling a model may ask for are raised
        env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
        env["BASH_DEFAULT_TIMEOUT_MS"] = str(defaults.bash_timeout_s * 1000)
        env["BASH_MAX_TIMEOUT_MS"] = str(defaults.bash_max_timeout_s * 1000)
    env.update(res.endpoint.env)  # last: the endpoint's own env wins
    return env


async def run(job: Job, res: Resolution, spec: SessionSpec) -> None:
    """Run the job's session to its end: the SDK's ``query`` with the options below, every
    message applied to the job as it arrives. The caller (runner.run_job) owns the clock, the
    status and the finish; this raises what the SDK raises."""
    d = spec.defaults
    background = spec.background
    system_prompt_append, thinking, effort = spec.system_prompt_append, spec.thinking, spec.effort
    permission_mode, max_turns, allowed_tools = spec.permission_mode, spec.max_turns, spec.allowed_tools
    setting_sources, strict_mcp_config = spec.setting_sources, spec.strict_mcp_config
    resume_session, mcp_servers, shim_base_url = spec.resume_session, spec.mcp_servers, spec.shim_base_url
    foreground = () if background else (FOREGROUND_NOTE,)
    append = "\n\n".join(s for s in (d.system_prompt_append, *foreground, system_prompt_append) if s)
    disallowed = list(d.disallowed_tools)
    disallowed += [t for t in (() if background else FOREGROUND_DISALLOWED) if t not in disallowed]
    # router 8: the caller's levers, when it passed one. `thinking` is a bool at that
    # interface and a config object for the SDK; None leaves the field unset, as does a
    # None effort. The shim's translation of either for a local backend is a later story.
    levers: dict[str, Any] = {}
    if thinking is not None:
        levers["thinking"] = {"type": "adaptive" if thinking else "disabled"}
    if effort is not None:
        levers["effort"] = effort
    opts = ClaudeAgentOptions(
        model=res.served_model,
        env=build_env(res, shim_base_url, d, background),
        cwd=job.run_cwd,
        permission_mode=permission_mode or d.permission_mode,
        allowed_tools=allowed_tools or d.allowed_tools,
        disallowed_tools=disallowed,
        max_turns=max_turns or d.max_turns,
        # the caller's setting_sources override the config's (a calibration job passes [],
        # so no ancestor CLAUDE.md or settings reach the session); None keeps the config's
        setting_sources=(d.setting_sources if setting_sources is None else setting_sources),  # type: ignore[arg-type]
        system_prompt={"type": "preset", "preset": "claude_code", **({"append": append} if append else {})},
        resume=resume_session,
        stderr=lambda line: job.log_text(f"[stderr] {line}"),
        mcp_servers=mcp_servers or {},
        **levers,
        # None keeps the SDK's own default (the server's MCP config not filtered out)
        **({} if strict_mcp_config is None else {"strict_mcp_config": strict_mcp_config}),
    )
    async for m in query(prompt=job.task, options=opts):
        _handle_message(job, m)


# ---------------- message handling ----------------

def _msg_to_dict(m: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(m):
        d = dataclasses.asdict(m)
        d["type"] = type(m).__name__
        return d
    return {"type": type(m).__name__, "repr": repr(m)}


def _handle_message(job: Job, m: Any) -> None:
    _apply_message(job, m)
    job.changed()


def _session_goes_on(job: Job) -> None:
    """A message that means more work after a ResultMessage (the harness re-invoking the
    session with a background task's outcome: its [init], an assistant turn): the session is
    not over. Read for every SDK version, task messages or not."""
    job._after_result = False
    job._session_over = False
    if held_open(job.last_activity):
        # the held-open line is no longer true and the progress lines would keep showing it;
        # the turn's own messages replace this one as they come
        job.last_activity = "re-invoked after its result"


def _track_task(job: Job, m: SystemMessage) -> None:
    """The SDK's task lifecycle, read from the raw payload (``m.data``, which the SDK keeps on
    its typed Task*Message subclasses too, so an SDK that emits only a plain SystemMessage is
    read the same way): ``task_started`` adds the task, a terminal status on ``task_notification``
    (completed | failed | stopped) or ``task_updated``'s patch (the SDK's
    TERMINAL_TASK_STATUSES; a stopped task may report only there) removes it, whatever the
    status: a failed or stopped task holds nothing open. ``task_progress`` changes nothing. A
    task is tracked whether or not it was backgrounded: a foreground one ends before the turn's
    result, so a task alive at a result is one the session left running."""
    data = m.data or {}
    tid = str(data.get("task_id") or "")
    if m.subtype == "task_started":
        job._tasks[tid] = str(data.get("description") or "")
        job._session_over = False
        job.log_text(f"[task] started {tid} ({data.get('task_type') or '?'}): {job._tasks[tid][:200]}")
        return
    patch = data.get("patch") if isinstance(data.get("patch"), dict) else {}
    if m.subtype == "task_notification":
        status, summary = data.get("status"), data.get("summary")
    else:  # task_updated
        status, summary = patch.get("status"), patch.get("error")
        if status not in TERMINAL_TASK_STATUSES:
            if tid in job._tasks and isinstance(patch.get("description"), str):
                job._tasks[tid] = patch["description"]
            return
        if tid not in job._tasks:
            return  # its notification already ended it (or its start was never seen): no second line
    job._tasks.pop(tid, None)
    job.log_text(f"[task] {status or 'ended'} {tid}" + (f": {str(summary)[:200]}" if summary else ""))
    if job._after_result:
        job.last_activity = held_open_line(list(job._tasks.values()))


def _apply_message(job: Job, m: Any) -> None:
    job.log_event(_msg_to_dict(m))
    if isinstance(m, AssistantMessage):
        _session_goes_on(job)
        # 3.7h: the live turn count. One AssistantMessage is one turn, so a running job's
        # num_turns moves while it runs and the Recent jobs log can show it (the column used
        # to read 0 until the job's end). The ResultMessage below still overwrites this with
        # the harness's own count: that one is the record, this is only ever the live view.
        job.num_turns += 1
        for b in m.content:
            if isinstance(b, TextBlock) and b.text.strip():
                job.log_text(f"[assistant] {b.text}")
                job.last_activity = b.text.strip()[:200]
            elif isinstance(b, ThinkingBlock):
                # the length the block really had travels with the excerpt: the transcript
                # holds only its first 500 chars, and the facts probe measures thinking by
                # the number, not by the tail (router M4).
                job.log_text(f"[thinking] ({len(b.thinking)} chars) {b.thinking[:500]}")
            elif isinstance(b, ToolUseBlock):
                short = json.dumps(b.input, default=str)
                job.log_text(f"[tool] {b.name} {short[:400]}")
                input_text = short[:TOOL_INPUT_CHARS]
                if isinstance(b.input, dict):
                    command = b.input.get("command")
                    path = None
                    for key in ("file_path", "notebook_path", "path"):
                        value = b.input.get(key)
                        if isinstance(value, str) and value:
                            path = value
                            break
                    if isinstance(command, str):
                        # spec: a string `command` (Bash) stores the command itself, not the JSON
                        # around it, so the progress line reads "Bash: uv run pytest -q"; a Bash
                        # command whole up to COMMAND_CHARS (the read share and the tests gate
                        # read it), any other tool's at the JSON's cut as before
                        input_text = command[:COMMAND_CHARS if b.name == "Bash" else TOOL_INPUT_CHARS]
                    elif path is not None:
                        # spec: any path-shaped tool (the edit tools and the readers with a
                        # file_path/notebook_path/path — Read, Glob, Grep, LS) stores just the
                        # path, so a Read on the status row names the file (story 1.11b); the
                        # count of calls and the files touched are still read back from the
                        # rows by tool name (story 1.5b)
                        input_text = path
                    elif b.name in EDIT_TOOLS:
                        # spec: an edit tool whose input carries no path stores NULL
                        input_text = None
                # last_activity shows the row's text as derive.last_tool shows it (one shape,
                # derive.activity_line: a Bash command's first TOOL_DISPLAY_CHARS): job_status
                # and the progress line read it, so a Bash call says "Bash: uv run pytest -q",
                # never the JSON around the command (review of 1.8)
                job.last_activity = activity_line(b.name, input_text)
                job.record_tool_call(time.time(), b.name, b.id, input_text)
    elif isinstance(m, UserMessage):
        content = m.content
        if isinstance(content, list):
            for b in content:
                if isinstance(b, ToolResultBlock):
                    txt = b.content if isinstance(b.content, str) else json.dumps(b.content, default=str)
                    job.log_text(f"[result{' ERROR' if b.is_error else ''}] {str(txt)[:300]}")
                    job.record_tool_result(b.tool_use_id, time.time(), b.is_error)
    elif isinstance(m, SystemMessage):
        if m.subtype in ("task_started", "task_notification", "task_updated"):
            _track_task(job, m)
        elif m.subtype == "init":
            _session_goes_on(job)
            job.session_id = m.data.get("session_id") or job.session_id
            job.log_text(f"[init] session={job.session_id} model={m.data.get('model')} cwd={m.data.get('cwd')}")
        elif m.subtype == "compact_boundary":
            # A trailing indicator of a context-tuning problem (story 1.11): the harness's
            # compact_boundary message carries compact_metadata = {trigger: manual|auto,
            # pre_tokens, ...} (verified against the claude_agent_sdk's bundled CLI)
            meta = m.data.get("compact_metadata") or {}
            job.compactions += 1
            pre_tokens = meta.get("pre_tokens")
            job.log_text(
                f"[compact] #{job.compactions} trigger={meta.get('trigger') or '?'}"
                f" pre_tokens={pre_tokens if pre_tokens is not None else '?'}"
            )
    elif isinstance(m, ResultMessage):
        # usage is not stored: it is the sum of the request rows (story 1.5b)
        job.session_id = m.session_id or job.session_id
        # reconciliation (3.7h): the harness's count wins over the per-message increment —
        # they count the same turns from two sides and may disagree on the boundaries
        job.num_turns = m.num_turns
        # the record follows the LATEST result (a session re-invoked by a background task's
        # outcome reports again, and its real answer is the last one): its error too
        job.result = m.result
        job.error = f"{m.subtype}: {m.result or ''}".strip() if m.is_error or m.subtype != "success" else None
        job._after_result, job._result_at = True, time.time()
        # over only with no background task alive: the harness waits for those and takes
        # another turn with their outcome (jobs.held_at_cap reads this)
        job._session_over = not job._tasks
        if job._tasks:
            job.last_activity = held_open_line(list(job._tasks.values()))
        job.log_text(f"[done] subtype={m.subtype} turns={m.num_turns} duration={m.duration_ms}ms"
                     + (f"; held open by {background_tasks_named(list(job._tasks.values()))}" if job._tasks else ""))
