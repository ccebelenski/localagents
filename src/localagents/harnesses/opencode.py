"""OpenCode as the harness a job runs in (Epic 6.7): one headless ``opencode run --standalone
--format json`` per job, in the job's directory, against the shim's OpenAI version (6.6).

Everything here is OpenCode's own, settled by the 6.2 captures and the 6.7 checks
(tests/fixtures/opencode): it works in ``$PWD``, not the process's directory, so ``PWD`` is the
job's; ``XDG_CONFIG_HOME`` replaces the user's whole configuration with the job's generated
``opencode.json`` (one OpenAI-compatible provider at the shim, the model's window, an agent
``job`` with the step cap and the permissions, the instructions); ``OPENCODE_DISABLE_CLAUDE_CODE``
keeps the operator's ``~/.claude`` (its CLAUDE.md and skills) out, as Claude Code jobs keep the
user's settings out, while the project's own CLAUDE.md files ride ``instructions`` (OpenCode reads
a project's AGENTS.md itself); the data and state directories are localagents' own and shared, so
``--session`` finds a job's session to resume; ``--title`` saves the extra title request.

The stream is one JSON object per line: ``step_start`` (a turn), ``text``, ``reasoning``,
``tool_use`` (one per call, in its final state), ``step_finish`` (``reason`` tool-calls or stop),
``retry``. There is no result or start event: the session id is any event's ``sessionID`` and the
result is the final step's text. A step cap is silent (the last step runs without tools and ends
``stop``), so a session that reaches its step limit is recorded as capped (``max_turns``, the
word the router's gates read). Compactions emit no event: the shim's request hook counts each new
``<conversation-checkpoint>`` the session sends.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from ..derive import activity_line
from ..jobs import Job, state_dir
from ..registry import Resolution
from . import SessionSpec
from .opencode_tools import VOCABULARY

NAME = "opencode"

PROVIDER = "localagents"  # the generated provider's id; the model is `localagents/job`
MODEL_KEY = "job"
DEFAULT_CONTEXT = 131072  # a window the probe could not read: the smallest the fleet serves
RETRY_CAP = 6  # consecutive transport retries before the session is ended (OpenCode has no cap)
KILL_GRACE_S = 20.0
CHECKPOINT = "<conversation-checkpoint>"
# the tools a job never gets: interactive (question), its own agents (subagent: Claude Code jobs
# disallow Agent/Task), web search, image generation, skills (the operator's own), code mode (no
# MCP server is configured for a job) and OpenCode's own management tools, which surface directly
# once `execute` is denied — worktree create/remove, session move/rename, models, MCP resources
# (the 6.6+6.7 review saw them offered to a job)
DENIED = ("question", "subagent", "websearch", "experimental_image_generate", "skill", "execute", "opencode_*")
# ...and the built-in plugins behind them, switched off in the job's config so their tools are not
# offered at all: a permission rule does not reach OpenCode's own management tools — the 6.7
# fold's live check called `opencode_worktree_remove` under a deny and it ran (it failed only on
# the nonexistent path it was given)
DISABLED_PLUGINS = ("opencode.tools", "opencode.tools.mcp-resources", "opencode.tool.question",
                    "opencode.tool.subagent", "opencode.tool.websearch", "opencode.tool.image-generate",
                    "opencode.tool.skill")
# the arguments this harness cannot honour yet (the policy translation is 6.8): refused at
# start_job, never silently dropped
UNSUPPORTED = ("permission_mode", "allowed_tools", "mcp_servers", "thinking", "effort", "strict_mcp_config")

FOREGROUND_NOTE = (
    "Background tasks are off in this session: never pass background: true to the shell tool; run "
    "every command in the foreground and wait for it to finish, do not start watchers or background "
    "processes and do not poll with sleep, and give a long command a larger timeout instead."
)


def unsupported(**given: Any) -> list[str]:
    """The run_agent arguments set (not None) that this harness does not translate yet."""
    return [k for k in UNSUPPORTED if given.get(k) is not None]


def data_dirs() -> tuple[Path, Path]:
    """localagents' own OpenCode data and state directories, shared by every job (a resume finds
    the session there) and apart from the operator's own OpenCode."""
    root = state_dir() / "opencode"
    data, state = root / "data", root / "state"
    data.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    return data, state


def _git_root(path: str) -> Path | None:
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def project_instructions(run_cwd: str, sources: list[str]) -> list[str]:
    """The project's CLAUDE.md files a Claude Code job would load with the ``project`` setting
    source: the job's directory's and every ancestor's up to the repository root (none outside a
    repository but the directory's own). Nothing without ``project`` in the sources."""
    if "project" not in sources:
        return []
    here = Path(run_cwd).resolve()
    root = _git_root(str(here)) or here
    out: list[str] = []
    for d in [here, *here.parents]:
        f = d / "CLAUDE.md"
        if f.is_file():
            out.append(str(f))
        if d == root:
            break
    return list(reversed(out))  # outermost first, as a reader would layer them


def config(job: Job, res: Resolution, spec: SessionSpec, instructions: list[str]) -> dict[str, Any]:
    d = spec.defaults
    context = res.context or DEFAULT_CONTEXT
    output = max(8192, min(32768, context // 8))
    permissions = [{"action": "*", "resource": "*", "effect": "allow"}]
    permissions += [{"action": t, "resource": "*", "effect": "deny"} for t in DENIED]
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{PROVIDER}/{MODEL_KEY}",
        "default_agent": "job",
        "update": "disable",
        "providers": {PROVIDER: {
            "name": "localagents",
            "package": "@opencode/ai/providers/openai-compatible",
            # the shim's OpenAI version; the real api_key is swapped in there, never handed here
            "settings": {"baseURL": spec.shim_base_url + "/v1", "apiKey": "local", "headerTimeout": 900000},
            "models": {MODEL_KEY: {
                "modelID": res.served_model,
                "capabilities": {"tools": True, "input": ["text"], "output": ["text"]},
                "limit": {"context": context, "output": output},
            }},
        }},
        "plugins": [f"-{p}" for p in DISABLED_PLUGINS],
        "permissions": permissions,
        "instructions": instructions,
        "agents": {"job": {"mode": "primary", "steps": spec.max_turns or d.max_turns, "permissions": permissions}},
    }


def environment(job: Job, config_home: Path) -> dict[str, str]:
    data, state = data_dirs()
    env = dict(os.environ)
    # the server's own client markers (a server started by OpenCode) must not reach the job
    for k in ("OPENCODE", "OPENCODE_SESSION_ID", "OPENCODE_TERMINAL", "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT"):
        env.pop(k, None)
    env.update({
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data),
        "XDG_STATE_HOME": str(state),
        "PWD": job.run_cwd,  # OpenCode works in $PWD, not the process's directory (6.2)
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    })
    return env


class Reader:
    """The JSON event stream applied to the job: one ``feed`` per line."""

    def __init__(self, job: Job, steps: int):
        self.job = job
        self.steps = steps
        self.text: str | None = None  # the latest assistant text: the final step's is the result
        self.finished: str | None = None  # the latest step_finish reason
        self.retries = 0  # consecutive transport retries

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        job = self.job
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            job.log_text(f"[stdout] {line[:300]}")
            return
        if not isinstance(e, dict):
            return
        sid = e.get("sessionID")
        if isinstance(sid, str) and sid and job.session_id != sid:
            job.session_id = sid
            job.log_text(f"[init] session={sid}")
        kind = e.get("type")
        part = e.get("part") if isinstance(e.get("part"), dict) else {}
        if kind != "retry":
            self.retries = 0
        if kind == "step_start":
            job.num_turns += 1
        elif kind == "text":
            text = str(part.get("text") or "")
            if text.strip():
                self.text = text
                job.log_text(f"[assistant] {text}")
                job.last_activity = text.strip()[:200]
        elif kind == "reasoning":
            text = str(part.get("text") or "")
            job.log_text(f"[thinking] ({len(text)} chars) {text[:500]}")
        elif kind == "tool_use":
            self._tool(part)
        elif kind == "step_finish":
            self.finished = str(part.get("reason") or "")
        elif kind == "retry":
            self.retries += 1
            err = e.get("error") if isinstance(e.get("error"), dict) else {}
            job.log_text(f"[retry] attempt {e.get('attempt')}: {err.get('type')}: {str(err.get('message'))[:200]}")
        else:
            job.log_text(f"[event] {json.dumps(e, default=str)[:400]}")

    def _tool(self, part: dict[str, Any]) -> None:
        job = self.job
        name = str(part.get("tool") or "?")
        call_id = str(part.get("id") or part.get("partID") or "")
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        tool_input = state.get("input")
        times = state.get("time") if isinstance(state.get("time"), dict) else {}
        start, end = times.get("start"), times.get("end")
        t0 = start / 1000 if isinstance(start, (int, float)) else None
        t1 = end / 1000 if isinstance(end, (int, float)) else t0
        job.log_text(f"[tool] {name} {json.dumps(tool_input, default=str)[:400]}")
        stored = VOCABULARY.stored_input(name, tool_input)
        job.last_activity = activity_line(name, stored, NAME)
        job.record_tool_call(t0 if t0 is not None else time.time(), name, call_id, stored)
        status = state.get("status")
        if status in ("completed", "error"):
            failed = status == "error"
            out = state.get("error") if failed else state.get("output")
            job.log_text(f"[result{' ERROR' if failed else ''}] {str(out)[:300]}")
            job.record_tool_result(call_id, t1 if t1 is not None else time.time(), failed)

    def finish(self, returncode: int | None, stderr_tail: str) -> None:
        """The record once the process has exited: the result is the final step's text; a
        session that reached its step limit is capped; a non-zero exit is an error naming it."""
        job = self.job
        if self.text is not None and self.finished == "stop":
            job.result = self.text
        if returncode not in (0, None):
            job.error = (f"opencode exited {returncode}" + (f": {stderr_tail[-300:]}" if stderr_tail else ""))
        elif self.steps and job.num_turns >= self.steps:
            # the last step ran without tools and was told to summarise (OpenCode's cap): the
            # session did not finish its work; `max_turns` is the word the router's gates read
            job.error = f"max_turns: the session reached its step limit ({self.steps} steps)"
        job.log_text(f"[done] reason={self.finished} turns={job.num_turns} exit={returncode}")


class CompactionCounter:
    """The shim's request hook (6.6 ``Job._on_request``): each NEW checkpoint the session sends is
    one compaction (OpenCode opens every request after a compaction with the same
    ``<conversation-checkpoint>`` until the next one replaces it; tests/fixtures/opencode/wire/
    compaction-signature.json)."""

    def __init__(self, job: Job):
        self.job = job
        self.last: str | None = None

    def __call__(self, path: str, body: dict[str, Any]) -> None:
        for m in body.get("messages") or []:
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            content = m.get("content")
            text = content if isinstance(content, str) else "".join(
                b.get("text", "") for b in content or [] if isinstance(b, dict))
            if text.startswith(CHECKPOINT):
                digest = hashlib.sha256(text.encode()).hexdigest()
                if digest != self.last:
                    self.last = digest
                    self.job.compactions += 1
                    self.job.log_text(f"[compact] #{self.job.compactions} (a new conversation checkpoint)")
            return  # only the first user message can open with the checkpoint


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), KILL_GRACE_S)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


async def run(job: Job, res: Resolution, spec: SessionSpec) -> None:
    """Run the job's OpenCode session to its end; the caller (runner.run_job) owns the clock, the
    status and the finish. A cancel (a timeout, cancel_job) ends the process group."""
    exe = shutil.which("opencode")
    if exe is None:
        raise RuntimeError("opencode is not on PATH")
    d = spec.defaults
    home = Path(job.log_dir) / "opencode-config"
    (home / "opencode").mkdir(parents=True, exist_ok=True)
    parts = [d.system_prompt_append, *(() if spec.background else (FOREGROUND_NOTE,)), spec.system_prompt_append]
    appended = "\n\n".join(p for p in parts if p)
    instructions = project_instructions(job.run_cwd, d.setting_sources if spec.setting_sources is None
                                        else spec.setting_sources)
    if appended:
        f = home / "instructions.md"
        f.write_text(appended + "\n")
        instructions.append(str(f))
    steps = spec.max_turns or d.max_turns
    (home / "opencode" / "opencode.json").write_text(json.dumps(config(job, res, spec, instructions), indent=1))
    argv = [exe, "run", "--standalone", "--format", "json", "--auto", "--archived",
            "--model", f"{PROVIDER}/{MODEL_KEY}", "--agent", "job", "--title", f"localagents {job.id[:8]}"]
    if spec.resume_session:
        argv += ["--session", spec.resume_session]
    argv.append(job.task)
    job.log_text(f"[opencode] {exe} steps={steps} instructions={len(instructions)} cwd={job.run_cwd}"
                 + (f" resume={spec.resume_session}" if spec.resume_session else ""))
    # the 6.6+6.7 review: the config's tool policy is Claude Code's vocabulary and is not applied
    # here until 6.8 translates it; say so on every job rather than drop it without a word
    job.log_text(f"[opencode] the config's permission_mode ({d.permission_mode}), allowed_tools and "
                 "disallowed_tools are Claude Code's and not applied (Epic 6.8); this job's tools: "
                 "edit, glob, grep, read, shell, webfetch, write")
    reader = Reader(job, steps)
    job._on_request = CompactionCounter(job)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=job.run_cwd, env=environment(job, home), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        limit=64 * 1024 * 1024)
    stderr_tail: list[str] = []

    async def errs() -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip("\n")
            if line:
                stderr_tail.append(line)
                del stderr_tail[:-20]
                job.log_text(f"[stderr] {line[:500]}")

    err_task = asyncio.create_task(errs())
    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            reader.feed(raw.decode(errors="replace"))
            job.changed()
            if reader.retries >= RETRY_CAP:
                job.log_text(f"[opencode] {RETRY_CAP} transport retries in a row: ending the session")
                await _kill(proc)
                raise RuntimeError(f"the backend could not be reached ({RETRY_CAP} retries in a row)")
        await proc.wait()
        await asyncio.wait_for(err_task, 5)
    except BaseException:
        await asyncio.shield(_kill(proc))
        err_task.cancel()
        raise
    finally:
        job._on_request = None
    reader.finish(proc.returncode, "\n".join(stderr_tail))
