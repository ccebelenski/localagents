"""Run a job as a headless Claude Code session pointed at a local endpoint."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
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

from .jobs import Job, state_dir
from .registry import Registry, Resolution, fetch_metrics, metrics_delta

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def build_env(res: Resolution, base_url: str) -> dict[str, str]:
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
    env.update(res.endpoint.env)
    return env


# ---------------- worktree isolation ----------------

def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def make_worktree(job: Job) -> dict[str, Any]:
    top = _git(job.cwd, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise RuntimeError(f"isolation=worktree requires a git repo at {job.cwd}: {top.stderr.strip()}")
    repo = top.stdout.strip()
    branch = f"local-agent/{job.id}"
    wt = state_dir() / "worktrees" / Path(repo).name / job.id
    wt.parent.mkdir(parents=True, exist_ok=True)
    r = _git(repo, "worktree", "add", "-b", branch, str(wt), "HEAD")
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {r.stderr.strip()}")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # keep the relative position inside the repo
    rel = Path(job.cwd).resolve().relative_to(Path(repo).resolve())
    return {"repo": repo, "path": str(wt / rel), "root": str(wt), "branch": branch, "base": base}


def finish_worktree(job: Job) -> None:
    wt = job.worktree
    if not wt:
        return
    root, repo = wt["root"], wt["repo"]
    status = _git(root, "status", "--porcelain").stdout.strip()
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    changed = bool(status) or head != wt["base"]
    if not changed:
        _git(repo, "worktree", "remove", "--force", root)
        _git(repo, "branch", "-D", wt["branch"])
        wt.update(kept=False, note="no changes; worktree removed")
        return
    _git(root, "add", "-A")
    diffstat = _git(root, "diff", "--cached", "--stat", wt["base"]).stdout.strip()
    _git(root, "reset", "-q")
    wt.update(
        kept=True,
        diffstat=diffstat,
        note=(
            f"Changes left in worktree {root} on branch {wt['branch']}. "
            f"Review with: git -C {root} diff {wt['base']}  (uncommitted work) — "
            f"then merge/cherry-pick or `git worktree remove --force {root}`."
        ),
    )


# ---------------- message handling ----------------

def _block_to_dict(b: Any) -> dict[str, Any]:
    d = dataclasses.asdict(b) if dataclasses.is_dataclass(b) else {"repr": repr(b)}
    d["type"] = type(b).__name__
    return d


def _msg_to_dict(m: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(m):
        d = dataclasses.asdict(m)
        d["type"] = type(m).__name__
        return d
    return {"type": type(m).__name__, "repr": repr(m)}


def _handle_message(job: Job, m: Any) -> None:
    job.log_event(_msg_to_dict(m))
    if isinstance(m, AssistantMessage):
        for b in m.content:
            if isinstance(b, TextBlock) and b.text.strip():
                job.log_text(f"[assistant] {b.text}")
                job.last_activity = b.text.strip()[:200]
            elif isinstance(b, ThinkingBlock):
                job.log_text(f"[thinking] {b.thinking[:500]}")
            elif isinstance(b, ToolUseBlock):
                job.tool_calls += 1
                short = json.dumps(b.input, default=str)
                job.log_text(f"[tool] {b.name} {short[:400]}")
                job.last_activity = f"{b.name} {short[:120]}"
                if b.name in EDIT_TOOLS:
                    fp = b.input.get("file_path") or b.input.get("notebook_path")
                    if fp and fp not in job.files_touched:
                        job.files_touched.append(fp)
    elif isinstance(m, UserMessage):
        content = m.content
        if isinstance(content, list):
            for b in content:
                if isinstance(b, ToolResultBlock):
                    txt = b.content if isinstance(b.content, str) else json.dumps(b.content, default=str)
                    job.log_text(f"[result{' ERROR' if b.is_error else ''}] {str(txt)[:300]}")
    elif isinstance(m, SystemMessage):
        if m.subtype == "init":
            job.session_id = m.data.get("session_id") or job.session_id
            job.log_text(f"[init] session={job.session_id} model={m.data.get('model')} cwd={m.data.get('cwd')}")
    elif isinstance(m, ResultMessage):
        job.session_id = m.session_id or job.session_id
        job.num_turns = m.num_turns
        job.usage = m.usage
        job.result = m.result
        job.log_text(f"[done] subtype={m.subtype} turns={m.num_turns} duration={m.duration_ms}ms")
        if m.is_error or m.subtype != "success":
            job.error = f"{m.subtype}: {m.result or ''}".strip()


# ---------------- main entry ----------------

async def run_job(
    job: Job,
    reg: Registry,
    res: Resolution,
    *,
    permission_mode: str | None,
    max_turns: int | None,
    allowed_tools: list[str] | None,
    system_prompt_append: str | None,
    resume_session: str | None,
    timeout_s: int | None,
    shim_base_url: str,
) -> None:
    d = reg.defaults
    job.status = "running"
    job.started_at = time.time()
    metrics_before = await fetch_metrics(res.endpoint.url, d.probe_timeout_s)
    try:
        if job.isolation == "worktree":
            job.worktree = make_worktree(job)
            job.run_cwd = job.worktree["path"]
            job.log_text(f"[worktree] {job.worktree['root']} branch={job.worktree['branch']}")

        append = "\n\n".join(s for s in (d.system_prompt_append, system_prompt_append) if s)
        opts = ClaudeAgentOptions(
            model=res.served_model,
            env=build_env(res, shim_base_url),
            cwd=job.run_cwd,
            permission_mode=permission_mode or d.permission_mode,
            allowed_tools=allowed_tools or d.allowed_tools,
            disallowed_tools=d.disallowed_tools,
            max_turns=max_turns or d.max_turns,
            setting_sources=d.setting_sources,  # type: ignore[arg-type]
            system_prompt={"type": "preset", "preset": "claude_code", **({"append": append} if append else {})},
            resume=resume_session,
            stderr=lambda line: job.log_text(f"[stderr] {line}"),
        )
        job.log_text(
            f"[start] endpoint={res.endpoint.name} ({res.endpoint.url}) via shim {shim_base_url} "
            f"model={res.served_model} context={res.context or '?'} ({res.context_source or 'unknown'}) cwd={job.run_cwd}"
        )

        async def consume() -> None:
            async for m in query(prompt=job.task, options=opts):
                _handle_message(job, m)

        await asyncio.wait_for(consume(), timeout=timeout_s or d.timeout_s)
        job.status = "failed" if job.error else "succeeded"
        if job.status == "succeeded" and job.result is None:
            job.error = "session ended without a result message"
            job.status = "failed"
    except asyncio.TimeoutError:
        job.status = "timeout"
        job.error = f"exceeded timeout of {timeout_s or d.timeout_s}s"
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.error = "cancelled"
    except Exception as e:  # noqa: BLE001
        job.status = "failed"
        job.error = f"{type(e).__name__}: {e}"
        job.log_text(f"[error] {job.error}")
    finally:
        job.finished_at = time.time()
        if metrics_before:
            # Server-wide counters: with other jobs on the same endpoint the delta is shared.
            job.metrics = metrics_delta(metrics_before, await fetch_metrics(res.endpoint.url, d.probe_timeout_s))
            if job.metrics:
                job.log_text(f"[metrics] {json.dumps(job.metrics)}")
        try:
            finish_worktree(job)
        except Exception as e:  # noqa: BLE001
            job.log_text(f"[worktree-error] {e}")
        job._done.set()
