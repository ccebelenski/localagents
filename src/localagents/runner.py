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

from .derive import DIFF_FILES_MAX, EDIT_TOOLS  # the write rule's list is the read rule's (files_touched)
from .jobs import Job, prune, state_dir
from .registry import Registry, Resolution, backend_kind, fetch_metrics, metrics_delta
from .router import feedback

# the tool_calls row stores the command (Bash) or the input JSON cut to this length (spec,
# story 1.5b; tests/test_tool_calls_db.py pins it). It is a shared fact, not a local detail:
# router feedback reads a row of exactly this length as truncated, able to have lost a token
# to the cut, and never as the full record (feedback._tests_gate).
TOOL_INPUT_CHARS = 120


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
    """feedback.git, the one git runner (it owns the bound, GIT_TIMEOUT_S, and the decoding): a
    hung call raises subprocess.TimeoutExpired, which finish_worktree names. ``worktree add`` is
    a checkout, whose time grows with the repository: the one call left unbounded."""
    return feedback.git(cwd, *args, bounded=args[:2] != ("worktree", "add"))


def make_worktree(job: Job) -> dict[str, Any]:
    if job.worktree:
        return _restore_worktree(job.worktree)
    top = _git(job.cwd, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise RuntimeError(f"isolation=worktree requires a git repo at {job.cwd}: {top.stderr.strip()}")
    repo = top.stdout.strip()
    branch = f"local-agent/{job.id}"
    wt = state_dir() / "worktrees" / Path(repo).name / job.id
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


# What an agent's tools create as a side effect and recreate on their next run: package
# environments (uv run's .venv, npm's node_modules) and interpreter, test-runner and linter
# caches. A path git ignores is something a removal would lose unless one of its components is
# named here (or ends in a suffix here). Build outputs (build/, dist/, target/) are not listed:
# a job may have been asked for them, and keeping a tree is the fail-closed direction.
REGENERATED = frozenset({".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
                         ".ruff_cache", ".tox", ".nox", ".hypothesis", ".cache"})
_REGENERATED_SUFFIXES = (".pyc", ".pyo", ".egg-info")
IGNORED_NAMED = 10  # the most ignored paths a note names; it counts them all


def _status(out: str) -> tuple[bool, list[str]]:
    """``git status --porcelain -z --ignored=matching``: whether it lists a change (tracked or
    untracked), and the ignored paths that a removal would lose (a directory ends in "/")."""
    changes, lost = False, []
    entries = iter(out.split("\0"))
    for e in entries:
        if not e:
            continue
        xy, path = e[:2], e[3:]
        if xy != "!!":
            changes = True
            if "R" in xy or "C" in xy:
                next(entries, None)  # -z puts a rename's source path in the next entry
        elif not any(p in REGENERATED or p.endswith(_REGENERATED_SUFFIXES) for p in path.split("/")):
            lost.append(path)
    return changes, lost


def _named(paths: list[str]) -> str:
    shown = ", ".join(paths[:IGNORED_NAMED])
    more = len(paths) - IGNORED_NAMED
    return f"{len(paths)} path{'s' if len(paths) != 1 else ''} ({shown}" + (f" and {more} more)" if more > 0 else ")")


def _set_files(wt: dict[str, Any], names: list[str] | None) -> None:
    """Record the diff's file list on the worktree dict: ``files`` (the first DIFF_FILES_MAX
    names, git's path order) and ``files_n`` (all of them). None removes both: a list that
    could not be read is absent, never empty (derive.diff_files reads absent as null)."""
    if names is None:
        wt.pop("files", None)
        wt.pop("files_n", None)
        return
    wt["files"] = names[:DIFF_FILES_MAX]
    wt["files_n"] = len(names)


def _stopped(e: BaseException) -> str:
    """A git step that hung past the bound, or a step that could not run, as the note names it."""
    if isinstance(e, subprocess.TimeoutExpired):
        cmd = e.cmd if isinstance(e.cmd, (list, tuple)) else [e.cmd]
        return f"{' '.join(map(str, cmd))} timed out after {e.timeout:g}s"
    return f"a step could not run ({type(e).__name__}: {e})"


def finish_worktree(job: Job) -> None:
    wt = job.worktree
    if not wt:
        return
    try:
        _finish_tree(job, wt)
    except (subprocess.TimeoutExpired, OSError) as e:
        # every git call here is bounded (D2): a hung one is named, the tree is left as that
        # step left it (a read that could not see never says unchanged) and the job settles
        what, root = _stopped(e), wt["root"]
        kept = Path(root).is_dir()
        wt.update(kept=kept, note=(
            f"{what}; the finish stopped at that step and nothing after it ran: the worktree is "
            f"{'left at' if kept else 'not on disk at'} {root} and branch {wt['branch']} is left in {wt['repo']}"))
        job.log_text(f"[worktree-error] {what}")


def _finish_tree(job: Job, wt: dict[str, Any]) -> None:
    root, repo = wt["root"], wt["repo"]
    # a previous finish's diffstat and file list (a resumed job's tree) are not this tree's
    # truth: each branch below records what it read, and a list it could not read stays absent
    wt.pop("diffstat", None)
    _set_files(wt, None)
    if not Path(root).is_dir():
        # the directory is gone (deleted out from under the job): record what was found instead
        # of raising. git is told to forget the tree; the branch may hold commits the user
        # wants, so removing it stays the user's call (review of 3.9B, finding B2)
        _git(repo, "worktree", "prune")
        wt.update(kept=False, note=f"the worktree directory was removed while the job ran; "
                                   f"it is left out of git (worktree prune); "
                                   f"branch {wt['branch']} is left in {repo}")
        job.log_text(f"[worktree-error] worktree directory missing at {root}")
        return
    # --ignored=matching: a file git ignores is listed too (a review's t/REVIEW.md where t/* is
    # ignored), an ignored directory as one entry (.venv/), never walked
    st = _git(root, "status", "--porcelain", "-z", "--ignored=matching")
    hd = _git(root, "rev-parse", "HEAD")
    if st.returncode != 0 or hd.returncode != 0:
        # git cannot read the tree (a corrupted .git pointer, a broken mount): both calls
        # print nothing, so "" != base would call it changed and the note would send the user
        # to two commands that fail. The tree exists and nothing is removed; record only that
        # (review 3.9D, F2).
        failed = st if st.returncode != 0 else hd
        err = " ".join(failed.stderr.split()) or f"exit {failed.returncode}"  # one line
        wt.update(
            kept=True,
            note=(f"git could not read the worktree at {root} ({err}); it is left in place "
                  f"on branch {wt['branch']} in {repo}; nothing was removed"),
        )
        job.log_text(f"[worktree-error] git could not read the worktree: {err}")
        return
    changes, lost = _status(st.stdout)
    head = hd.stdout.strip()
    changed = changes or head != wt["base"]
    if not changed:
        # a clean status at the base commit: the diff against the base is empty, so the file
        # list is known to be empty whatever the removal below does
        _set_files(wt, [])
        if lost:
            # the tree is removed only when nothing in it would be lost: files git ignores are
            # outside the diff, so they keep the tree and the note is the only place naming them
            wt.update(kept=True, note=(
                f"no changes git tracks, but the worktree is kept: it holds {_named(lost)} that git "
                f"ignores and removing it would lose. It is left at {root} on branch {wt['branch']}; "
                f"when done: `git worktree remove --force {root}` and `git -C {repo} branch -D {wt['branch']}`."))
            return
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
    # the diffstat and the file list (what the record's diff_files shows: every path the job
    # changed against the base, shell writes included; the diffstat cuts long paths to ".../")
    # come from feedback's one copy-index reader, so the tree's own index is never touched.
    # A read that failed or hung is named in the note and a line; the tree is kept either way.
    problem = None
    try:
        names, (stat,) = feedback.tree_diff(root, wt["base"], job.log_dir, ("--stat",))
    except (feedback.DiffUnread, subprocess.TimeoutExpired, OSError) as e:
        problem = f"{e if isinstance(e, feedback.DiffUnread) else _stopped(e)}: no diffstat or file list was taken"
        job.log_text(f"[worktree-error] {problem}")
    else:
        _set_files(wt, names)
        wt["diffstat"] = stat.strip()
    wt.update(
        kept=True,
        note=(
            f"Changes left in worktree {root} on branch {wt['branch']}. "
            f"Review with: git -C {root} status --short && git -C {root} diff {wt['base']}  "
            "(uncommitted work; status also lists files git has not tracked yet) — "
            f"then merge/cherry-pick or `git worktree remove --force {root}`."
            + (f" It also holds {_named(lost)} that git ignores, which status and the diff do not show."
               if lost else "")
            + (f" {problem}." if problem else "")
        ),
    )


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
                        # around it, so the progress line reads "Bash: uv run pytest -q"
                        input_text = command[:TOOL_INPUT_CHARS]
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
        job._session_over = True  # what follows is the CLI's exit, not more work (jobs.held_at_cap)
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
            job._log_safe(
                f"[cancel] {absorbed} cancel(s) arrived during the job's worktree step; "
                "the tree was finished and removed",
            )
        if not made.cancelled() and made.exception() is None:
            job.worktree = made.result()
            # the in-process record must answer run_cwd the way the row does: derive reads
            # worktree.path, so leaving run_cwd at the original cwd made the same job
            # report two different values depending on the reader (review of 3.9B, finding B3)
            job.run_cwd = job.worktree["path"]
        elif not made.cancelled():
            # the cancel wins (the job is `cancelled`), but the thread's own error is not
            # dropped without a word (DeepSeek review of 6005f94)
            e = made.exception()
            job._log_safe(f"[worktree-error] {type(e).__name__}: {e}")
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
    mcp_servers: dict[str, Any] | None = None,
    thinking: bool | None = None,
    effort: str | None = None,
    strict_mcp_config: bool | None = None,
    setting_sources: list[str] | None = None,
) -> None:
    d = reg.defaults
    job.status = "running"
    job.started_at = time.time()
    job.changed()
    # router 13: the sessions on the endpoint as this one starts (its own row included), the
    # speed sample's concurrency; never raises
    sessions_at_start = feedback.note_start(job)
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
            env=build_env(res, shim_base_url),
            cwd=job.run_cwd,
            permission_mode=permission_mode or d.permission_mode,
            allowed_tools=allowed_tools or d.allowed_tools,
            disallowed_tools=d.disallowed_tools,
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
        job.log_text(
            f"[start] endpoint={res.endpoint.name} ({res.endpoint.url}) via shim {shim_base_url} "
            f"model={res.served_model} context={res.context or '?'} ({res.context_source or 'unknown'}) cwd={job.run_cwd}"
        )

        async def consume() -> None:
            async for m in query(prompt=job.task, options=opts):
                _handle_message(job, m)

        session_timeout = timeout_s or d.timeout_s
        # the session's own clock, for the blocking waits' cap rule (jobs.held_at_cap): armed
        # here, after the preamble, so it starts later than a run_agent wait's clock does
        job._session_timeout_s = float(session_timeout)
        job._session_deadline = time.monotonic() + session_timeout
        await asyncio.wait_for(consume(), timeout=session_timeout)
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
        await _to_the_end(job, _finish(job, res, metrics_before, d.probe_timeout_s, sessions_at_start), "finish")
        # story 1.12: the transcript row is written here — after _finish, so the [metrics] and
        # [worktree]/[worktree-error] lines (and any [cancel] line the finish step absorbed) are
        # in it, and before _done.set()/changed(). A terminal row has its transcript row whenever
        # a transcript file existed at completion; a job that never logged a line (cancelled
        # before [start]) has neither, and readers say so — "no file, no row". A lost job never
        # reaches this (the server died): the file is all it has.
        job.record_transcript()
        job._done.set()
        job.changed()
        # retention: this job's own directory is new, so only older ones can go
        await _to_the_end(job, asyncio.to_thread(prune, d.job_retention_days), "prune")


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
        job._log_safe(f"[cancel] {absorbed} cancel(s) arrived during the job's {what} step; it ran to the end")
    if fut.cancelled():
        job._log_safe(f"[{what}-error] cancelled")
    elif fut.exception() is not None:
        e = fut.exception()
        job._log_safe(f"[{what}-error] {type(e).__name__}: {e}")


async def _finish(job: Job, res: Resolution, metrics_before: dict[str, float] | None, probe_timeout_s: float,
                  sessions_at_start: int | None = None) -> None:
    """The finally's work before the tail: the metrics delta, the feedback gates, then the
    worktree. Each is guarded on its own so a metrics failure still finishes the worktree."""
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
                job._log_safe(f"[metrics] {json.dumps(job.metrics)}")
        except Exception as e:  # noqa: BLE001
            job.metrics = None
            job._log_safe(f"[metrics-error] {type(e).__name__}: {e}")
    # router 13 (design §9): gates and the speed sample, evidence only (on_finish never raises
    # and never moves the status or error). After the metrics, whose generate_tps the sample
    # reads; before finish_worktree, which removes an unchanged tree: the diff gates read the
    # tree as the session left it, so "could not read" is never confused with "nothing changed".
    await feedback.on_finish(job, res, sessions=sessions_at_start)
    try:
        await asyncio.to_thread(finish_worktree, job)
    except Exception as e:  # noqa: BLE001
        job._log_safe(f"[worktree-error] {e}")
