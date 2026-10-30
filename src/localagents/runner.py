"""Run a job as a headless Claude Code session pointed at a local endpoint."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import subprocess
import sys
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

from .jobs import Job, prune, state_dir
from .registry import Registry, Resolution, backend_kind, fetch_metrics, metrics_delta

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
    if job.worktree:
        return _restore_worktree(job.worktree)
    top = _git(job.cwd, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise RuntimeError(f"isolation=worktree requires a git repo at {job.cwd}: {top.stderr.strip()}")
    repo = top.stdout.strip()
    branch = f"local-agent/{job.name}"
    wt = state_dir() / "worktrees" / Path(repo).name / job.name
    wt.parent.mkdir(parents=True, exist_ok=True)
    r = _git(repo, "worktree", "add", "-b", branch, str(wt), "HEAD")
    if r.returncode != 0:
        # git creates the branch before it checks the target, so a failed add (the target
        # directory already exists, say) leaves the branch behind. Delete what this attempt
        # created, and name the branch in the error either way, never silently
        # (story 1.9a row 4).
        err = f"git worktree add failed: {r.stderr.strip()}"
        if _git(repo, "branch", "--list", branch).stdout.strip():
            br = _git(repo, "branch", "-D", branch)
            if br.returncode == 0:
                err += f"; branch {branch} removed"
            else:
                err += f"; branch {branch} left behind: {br.stderr.strip()}"
        raise RuntimeError(err)
    try:
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()
        # keep the relative position inside the repo
        rel = Path(job.cwd).resolve().relative_to(Path(repo).resolve())
        return {"repo": repo, "path": str(wt / rel), "root": str(wt), "branch": branch, "base": base}
    except BaseException as e:
        # the tree and branch exist but job.worktree will never name them, so nothing else
        # would remove them: this does, then re-raises (DeepSeek review of 6005f94). A cleanup
        # step that itself fails is named in the raised error, never silent (story 1.9 item 5).
        rm = _git(repo, "worktree", "remove", "--force", str(wt))
        br = _git(repo, "branch", "-D", branch)
        fails = []
        if rm.returncode != 0:
            fails.append(f"removing the tree also failed ({rm.stderr.strip()}): it is left at {wt}")
        if br.returncode != 0:
            fails.append(f"removing the branch also failed ({br.stderr.strip()}): branch {branch} is left in {repo}")
        if fails:
            raise RuntimeError(
                f"worktree setup failed after git worktree add ({type(e).__name__}: {e}); " + "; ".join(fails)
            ) from e
        raise


def _restore_worktree(wt: dict[str, Any]) -> dict[str, Any]:
    """A resumed job's tree: reuse it if it is still there, else recreate it at the same path.

    finish_worktree removes the tree and branch of a job that changed nothing, so a follow-up to
    a read-only job would otherwise start in a directory that no longer exists. The tree comes
    back at the base commit the session saw (not the repo's current HEAD), on the same branch if
    it still exists, so the paths and contents the session remembers are what it finds.
    """
    repo, root, branch = wt["repo"], wt["root"], wt["branch"]
    if Path(root).is_dir():
        return dict(wt)
    _git(repo, "worktree", "prune")
    if _git(repo, "rev-parse", "--verify", "--quiet", branch).returncode == 0:
        r = _git(repo, "worktree", "add", root, branch)
    else:
        r = _git(repo, "worktree", "add", "-b", branch, root, wt["base"])
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add (restore) failed: {r.stderr.strip()}")
    return {**wt, "recreated": True}


def finish_worktree(job: Job) -> None:
    wt = job.worktree
    if not wt:
        return
    root, repo = wt["root"], wt["repo"]
    status = _git(root, "status", "--porcelain").stdout.strip()
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    changed = bool(status) or head != wt["base"]
    if not changed:
        # each step's return code is read: "removed" is said only when git removed it
        rm = _git(repo, "worktree", "remove", "--force", root)
        if rm.returncode != 0:
            # the branch is checked out in the tree that is still there: it stays too
            failed = f"git worktree remove failed: {rm.stderr.strip()}"
            wt.update(kept=True, note=f"no changes, but the worktree was not removed ({failed}); "
                                      f"it is left at {root} on branch {wt['branch']}")
            job.log_text(f"[worktree-error] {failed}")
            return
        br = _git(repo, "branch", "-D", wt["branch"])
        if br.returncode != 0:
            failed = f"git branch -D failed: {br.stderr.strip()}"
            wt.update(kept=True, note=f"no changes; the worktree was removed but its branch was not "
                                      f"({failed}): branch {wt['branch']} is left in {repo}")
            job.log_text(f"[worktree-error] {failed}")
            return
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
    _apply_message(job, m)
    job.changed()


def _apply_message(job: Job, m: Any) -> None:
    job.log_event(_msg_to_dict(m))
    if isinstance(m, AssistantMessage):
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
                job.log_text(f"[thinking] {b.thinking[:500]}")
            elif isinstance(b, ToolUseBlock):
                short = json.dumps(b.input, default=str)
                job.log_text(f"[tool] {b.name} {short[:400]}")
                input_text = short[:120]
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
                        # around it, so the progress line reads "Bash: uv run pytest -q"
                        input_text = command[:120]
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
                # last_activity shows the same text the row shows, in derive.last_tool's shape:
                # job_status and the progress line read it, so a Bash call says
                # "Bash: uv run pytest -q", never the JSON around the command (review of 1.8)
                job.last_activity = f"{b.name}: {input_text}" if input_text is not None else b.name
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
        if m.subtype == "init":
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
        job.result = m.result
        job.log_text(f"[done] subtype={m.subtype} turns={m.num_turns} duration={m.duration_ms}ms")
        if m.is_error or m.subtype != "success":
            job.error = f"{m.subtype}: {m.result or ''}".strip()


# ---------------- main entry ----------------

async def _make_worktree_shielded(job: Job) -> dict[str, Any]:
    """make_worktree in a thread, safe against a cancel (review of 1.7, finding 2).

    A cancel cannot stop the thread: git goes on to create the tree and branch. So the thread's
    future is shielded, and on a cancel this waits for it (absorbing further cancels; the wait is
    bounded by the git calls), records the tree on the job so run_job's finally removes it, and
    then re-raises the cancel. Every cancel that arrived during the step is counted (the task's
    cancelling() delta, as _to_the_end counts the finish step's) and named in a [cancel] line
    before the re-raise: the [worktree] line is written only after the shield returns, so without
    it a job cancelled during worktree creation had an empty transcript (story 1.9a row 3)."""
    task = asyncio.current_task()
    started = task.cancelling() if task is not None else 0
    made = asyncio.ensure_future(asyncio.to_thread(make_worktree, job))
    try:
        return await asyncio.shield(made)
    except asyncio.CancelledError:
        while not made.done():
            try:
                await asyncio.wait([made])
            except asyncio.CancelledError:
                pass
        absorbed = (task.cancelling() if task is not None else 0) - started
        if absorbed:
            _log_safe(
                job,
                f"[cancel] {absorbed} cancel(s) arrived during the job's worktree step; "
                "the tree was finished and removed",
            )
        if not made.cancelled() and made.exception() is None:
            job.worktree = made.result()
        elif not made.cancelled():
            # the cancel wins (the job is `cancelled`), but the thread's own error is not
            # dropped without a word (DeepSeek review of 6005f94)
            e = made.exception()
            _log_safe(job, f"[worktree-error] {type(e).__name__}: {e}")
        raise


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
    job.changed()
    metrics_before = None
    try:
        # inside the try: a cancel landing in this first await must still run the finally, or
        # the job stays `running` for good (review of 1.7, finding 3)
        metrics_before = await fetch_metrics(res.endpoint, d.probe_timeout_s)
        if job.isolation == "worktree":
            # to_thread: these are blocking git calls (story 1.7 item 7). subprocess.run would
            # stall the event loop that carries this server's heartbeat and the shim's serving;
            # neither function touches the thread-bound sqlite connection.
            job.worktree = await _make_worktree_shielded(job)
            job.run_cwd = job.worktree["path"]
            job.changed()  # another server's job_status sees the tree before the first message
            job.log_text(
                f"[worktree] {job.worktree['root']} branch={job.worktree['branch']}"
                + (" (recreated at base; the previous tree had been removed)" if job.worktree.get("recreated") else "")
            )

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
        # The tail (finished_at, _done.set(), the final changed(), the prune) ALWAYS runs: a job
        # whose finally stops early stays `running` in its row, its worktree unfinished, and
        # every waiter runs to its timeout. So the finally's awaits absorb cancels until their
        # work is done (a second cancel_job lands here: both calls pass `if job.done` before
        # either cancel is processed — DeepSeek review of 6005f94), and an exception from them
        # is a transcript line, never propagated past the tail.
        job.finished_at = time.time()
        await _to_the_end(job, _finish(job, res, metrics_before, d.probe_timeout_s), "finish")
        job._done.set()
        job.changed()
        # retention: this job's own directory is new, so only older ones can go
        await _to_the_end(job, asyncio.to_thread(prune, d.job_retention_days), "prune")


def _log_safe(job: Job, text: str) -> None:
    """A transcript line from the finally, which must not raise: a failing write (ENOSPC, a
    read-only state directory) falls back to stderr."""
    try:
        job.log_text(text)
    except Exception as e:  # noqa: BLE001
        print(f"localagents: job {job.id}: {text} (transcript write failed: {e})", file=sys.stderr)


async def _to_the_end(job: Job, aw: Any, what: str) -> None:
    """Await `aw` to completion whatever happens to the caller: a cancel is absorbed (the work
    goes on in its task or thread and is waited for, like _make_worktree_shielded), and any
    exception or absorbed cancel is a transcript line. Never raises."""
    fut = asyncio.ensure_future(aw)
    # Count the task's cancel requests (cancelling(), 3.11+), not the CancelledErrors this loop
    # sees: two back-to-back Task.cancel() calls fold into one delivery, so the except-based
    # count under-reported — two concurrent cancel_job calls during a step printed 1 (and with
    # both deliveries landing before the step, 0) (story 1.9 item 4, DeepSeek probe).
    task = asyncio.current_task()
    started = task.cancelling() if task is not None else 0
    while not fut.done():
        try:
            await asyncio.wait([fut])  # wait() never cancels fut, unlike await fut
        except asyncio.CancelledError:
            pass
    absorbed = (task.cancelling() if task is not None else 0) - started
    if absorbed:
        _log_safe(job, f"[cancel] {absorbed} cancel(s) arrived during the job's {what} step; it ran to the end")
    if fut.cancelled():
        _log_safe(job, f"[{what}-error] cancelled")
    elif fut.exception() is not None:
        e = fut.exception()
        _log_safe(job, f"[{what}-error] {type(e).__name__}: {e}")


async def _finish(job: Job, res: Resolution, metrics_before: dict[str, float] | None, probe_timeout_s: float) -> None:
    """The finally's work before the tail: the metrics delta, then the worktree. Each is guarded
    on its own so a metrics failure still finishes the worktree."""
    if metrics_before:
        # Server-wide counters: with other jobs on the same endpoint the delta is shared.
        # the backend decides the arithmetic: vLLM's prompt_tokens already includes cached
        # tokens, and its /metrics sometimes lacks the prefix-cache series (review of 1.8).
        # Guarded (review of 1.8, construct-it: a KeyError from metrics_delta stranded a job).
        try:
            job.metrics = metrics_delta(
                metrics_before,
                await fetch_metrics(res.endpoint, probe_timeout_s),
                backend=backend_kind(res.endpoint.backend),
            )
            if job.metrics:
                _log_safe(job, f"[metrics] {json.dumps(job.metrics)}")
        except Exception as e:  # noqa: BLE001
            job.metrics = None
            _log_safe(job, f"[metrics-error] {type(e).__name__}: {e}")
    try:
        await asyncio.to_thread(finish_worktree, job)
    except Exception as e:  # noqa: BLE001
        _log_safe(job, f"[worktree-error] {e}")
