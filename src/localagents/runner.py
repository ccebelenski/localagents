"""Run a job: everything around its session that every harness shares (Epic 6.3) — the worktree,
the metrics, the session's clock, the status, the cancel and the finish. The session itself is
the harness's (harnesses/)."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .derive import DIFF_FILES_MAX, background_tasks_named
from . import harnesses
from .harnesses import SessionSpec
from .jobs import Job, prune, state_dir
from .registry import Registry, Resolution, backend_kind, fetch_metrics, metrics_delta
from .router import feedback

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


# ---------------- main entry ----------------

def _ended_after_result(job: Job, what: str) -> str | None:
    """The error a timeout or cancel writes when the session had already reported a result:
    one sentence keeping what happened (when, its error if the result was one, what held it
    open), so the record never reads as work that did not finish. None with no result."""
    if job._result_at is None:
        return None
    at = round(job._result_at - (job.started_at or job._result_at))
    err = f" ({job.error[:200].rstrip(': ')})" if job.error else ""  # "error_max_turns:" reads as the subtype
    held = (f"was still held open by {background_tasks_named(list(job._tasks.values()))}"
            if job._tasks else "was still running")
    return f"the session reported a result at {at} s{err} and {held} when {what} ended it"


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
    background_tasks: bool | None = None,
) -> None:
    d = reg.defaults
    # background_tasks and timeout_s resolve here against the config's defaults (None is the
    # config's; the session's other settings resolve once in harnesses/policy.py, Epic 6.8). The
    # resolved background value is the job's own, kept for a resume_job of it
    background = d.background_tasks if background_tasks is None else background_tasks
    job._background_tasks = background
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
        # Epic 6.4: the job's own harness, recorded at its creation. Inside the try (the 6.3+6.4
        # review): a row naming a harness this install lacks fails here and still settles
        harness = harnesses.get(job.harness)
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

        job.log_text(
            f"[start] endpoint={res.endpoint.name} ({res.endpoint.url}) via shim {shim_base_url} "
            f"model={res.served_model} context={res.context or '?'} ({res.context_source or 'unknown'}) cwd={job.run_cwd}"
            f" background_tasks={'on' if background else 'off'}"
        )
        spec = SessionSpec(
            defaults=d, shim_base_url=shim_base_url, background=background,
            permission_mode=permission_mode, max_turns=max_turns, allowed_tools=allowed_tools,
            system_prompt_append=system_prompt_append, resume_session=resume_session,
            mcp_servers=mcp_servers, thinking=thinking, effort=effort,
            strict_mcp_config=strict_mcp_config, setting_sources=setting_sources,
        )
        session_timeout = timeout_s or d.timeout_s
        # the session's own clock, for the blocking waits' cap rule (jobs.held_at_cap): armed
        # here, after the preamble, so it starts later than a run_agent wait's clock does
        job._session_timeout_s = float(session_timeout)
        job._session_deadline = time.monotonic() + session_timeout
        # Epic 6.3: the session itself is the harness's (harnesses/); everything around it
        # (the worktree, the clock, the status, the finish) is every harness's
        await asyncio.wait_for(harness.run(job, res, spec), timeout=session_timeout)
        job.status = "failed" if job.error else "succeeded"
        if job.status == "succeeded" and job.result is None:
            job.error = "session ended without a result message"
            job.status = "failed"
    except asyncio.TimeoutError:
        job.status = "timeout"
        job.error = (_ended_after_result(job, f"the timeout of {timeout_s or d.timeout_s} s")
                     or f"exceeded timeout of {timeout_s or d.timeout_s}s")
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.error = _ended_after_result(job, "a cancel") or "cancelled"
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
