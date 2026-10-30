"""Worktree lifecycle against a real throwaway git repo (no model server needed)."""

import json
import subprocess
from pathlib import Path

import pytest

from localagents.jobs import Job, state_dir
from localagents.runner import finish_worktree, make_worktree

RESUME_KEYS = ("repo", "path", "root", "branch", "base")


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "init")
    return r


def job(jid, repo, log_dir, worktree=None):
    log_dir.mkdir(parents=True, exist_ok=True)
    # id and name deliberately differ: make_worktree must name the branch and directory after the NAME
    return Job(
        id="e3b0c442-98fc-4c00-9a96-54a3242b1f0e", name=jid, task="t", model="m", endpoint="e",
        base_url="http://e", cwd=str(repo), run_cwd=str(repo),
        isolation="worktree", worktree=worktree, log_dir=str(log_dir),
    )


def test_no_change_job_removes_tree_and_branch(repo, tmp_path):
    j = job("j1", repo, tmp_path / "logs" / "j1")
    j.worktree = make_worktree(j)
    assert Path(j.worktree["root"]).is_dir()
    # the branch and directory carry the job NAME, not the uuid id
    assert j.worktree["branch"] == "local-agent/j1"
    assert j.worktree["root"] == str(state_dir() / "worktrees" / "repo" / "j1")
    finish_worktree(j)
    assert j.worktree["kept"] is False
    assert not Path(j.worktree["root"]).exists()
    assert "local-agent/j1" not in git(repo, "branch", "--list", "local-agent/*")


@pytest.mark.parametrize("fails", ["worktree", "branch"])
def test_a_failed_removal_is_recorded_not_reported_removed(repo, tmp_path, monkeypatch, fails):
    """finish_worktree ignored the return codes of `git worktree remove --force` and
    `git branch -D` and said "no changes; worktree removed" regardless. A failed removal is
    kept=True with a note naming the failure, and a [worktree-error] transcript line."""
    from localagents import runner

    j = job("j9", repo, tmp_path / "logs" / "j9")
    j.worktree = make_worktree(j)
    real = runner._git

    def failing(cwd, *args):
        if args[:2] == (("worktree", "remove") if fails == "worktree" else ("branch", "-D")):
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: simulated failure")
        return real(cwd, *args)

    monkeypatch.setattr(runner, "_git", failing)
    finish_worktree(j)
    assert j.worktree["kept"] is True
    assert "fatal: simulated failure" in j.worktree["note"]
    assert j.worktree["note"] != "no changes; worktree removed"
    log = (tmp_path / "logs" / "j9" / "transcript.txt").read_text()
    assert "[worktree-error]" in log and "fatal: simulated failure" in log


def test_resume_recreates_removed_tree_at_base(repo, tmp_path):
    j1 = job("j1", repo, tmp_path / "logs" / "j1")
    j1.worktree = make_worktree(j1)
    finish_worktree(j1)
    carried = {k: j1.worktree[k] for k in RESUME_KEYS}

    # the repo moves on; the resumed session should still see the tree it remembers
    (repo / "a.txt").write_text("two\n")
    git(repo, "commit", "-q", "-am", "later")

    j2 = job("j2", repo, tmp_path / "logs" / "j2", worktree=carried)
    j2.worktree = make_worktree(j2)
    assert j2.worktree["recreated"] is True
    assert j2.worktree["root"] == carried["root"] and Path(j2.worktree["path"]).is_dir()
    assert git(j2.worktree["root"], "rev-parse", "HEAD") == carried["base"]
    assert git(j2.worktree["root"], "rev-parse", "--abbrev-ref", "HEAD") == carried["branch"]
    assert (Path(j2.worktree["path"]) / "a.txt").read_text() == "one\n"

    # still nothing changed: cleaned up again
    finish_worktree(j2)
    assert j2.worktree["kept"] is False and not Path(j2.worktree["root"]).exists()


def test_run_job_offloads_worktree_git_to_a_thread(repo, tmp_path, monkeypatch):
    """Story 1.7 item 7: make_worktree/finish_worktree are blocking subprocess.run calls; run_job
    must run them through asyncio.to_thread, or a slow git status on a big repo stalls the
    heartbeat and the shim that share the event loop."""
    import asyncio
    import threading

    from localagents import runner
    from localagents.registry import Defaults, Endpoint, Registry, Resolution

    j = job("j7", repo, tmp_path / "logs" / "j7")
    where = {}

    def record(what, fn):
        def wrapper(*a, **kw):
            where[what] = threading.current_thread()
            return fn(*a, **kw)
        return wrapper

    async def no_metrics(*a, **kw):
        return None

    def fake_query(prompt, options):
        async def nothing():
            return
            yield  # an async generator that yields no message

        return nothing()

    monkeypatch.setattr(runner, "make_worktree", record("make", runner.make_worktree))
    monkeypatch.setattr(runner, "finish_worktree", record("finish", runner.finish_worktree))
    monkeypatch.setattr(runner, "fetch_metrics", no_metrics)
    monkeypatch.setattr(runner, "query", fake_query)
    ep = Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")
    reg = Registry(path=None, endpoints={"e": ep}, defaults=Defaults(job_retention_days=0), raw={}, local={})
    asyncio.run(runner.run_job(
        j, reg, Resolution(ep, "served-model"), permission_mode=None, max_turns=None, allowed_tools=None,
        system_prompt_append=None, resume_session=None, timeout_s=5, shim_base_url="http://127.0.0.1:1/job/x",
    ))
    assert j.worktree is not None and j.worktree["branch"] == "local-agent/j7"
    assert where["make"] is not None and where["make"] is not threading.main_thread()
    assert where["finish"] is not None and where["finish"] is not threading.main_thread()


def test_resume_reuses_kept_tree_and_reports_its_diff(repo, tmp_path):
    j1 = job("j1", repo, tmp_path / "logs" / "j1")
    j1.worktree = make_worktree(j1)
    (Path(j1.worktree["path"]) / "b.txt").write_text("new\n")
    finish_worktree(j1)
    assert j1.worktree["kept"] is True and "b.txt" in j1.worktree["diffstat"]

    j2 = job("j2", repo, tmp_path / "logs" / "j2", worktree={k: j1.worktree[k] for k in RESUME_KEYS})
    j2.worktree = make_worktree(j2)
    assert "recreated" not in j2.worktree and j2.worktree["root"] == j1.worktree["root"]
    (Path(j2.worktree["path"]) / "c.txt").write_text("more\n")
    finish_worktree(j2)
    assert j2.worktree["kept"] is True
    assert "b.txt" in j2.worktree["diffstat"] and "c.txt" in j2.worktree["diffstat"]


# ---- run_job driven against a real store and repo (review of 1.7: the wait/cancel windows) ----


def _drive_setup(monkeypatch, *, metrics=None):
    """Stub the model side of run_job: query yields no message, fetch_metrics is `metrics` (an
    async callable) or answers None. Returns (registry, resolution, run_job keyword arguments)."""
    from localagents import runner
    from localagents.registry import Defaults, Endpoint, Registry, Resolution

    async def no_metrics(*a, **kw):
        return None

    def fake_query(prompt, options):
        async def nothing():
            return
            yield

        return nothing()

    monkeypatch.setattr(runner, "fetch_metrics", metrics or no_metrics)
    monkeypatch.setattr(runner, "query", fake_query)
    ep = Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")
    reg = Registry(path=None, endpoints={"e": ep}, defaults=Defaults(job_retention_days=0), raw={}, local={})
    kw = dict(permission_mode=None, max_turns=None, allowed_tools=None, system_prompt_append=None,
              resume_session=None, timeout_s=10, shim_base_url="http://127.0.0.1:1/job/x")
    return reg, Resolution(ep, "served-model"), kw


def _store_job(repo, isolation="worktree"):
    from localagents.jobs import JobStore

    store = JobStore()
    j = store.new(task="t", model="served-model", endpoint="e", base_url="http://e:8080", cwd=str(repo),
                  run_cwd=str(repo), isolation=isolation, backend="llama.cpp", host="e")
    return store, j


def _branch_exists(repo, branch):
    return bool(git(repo, "branch", "--list", branch))


def _slow(monkeypatch, name, seconds):
    """Make runner.<name> (a function run_job calls in a worker thread) sleep first."""
    import time

    from localagents import runner

    real = getattr(runner, name)

    def slow(*a, **kw):
        time.sleep(seconds)
        return real(*a, **kw)

    monkeypatch.setattr(runner, name, slow)


def test_wait_returns_only_after_the_finally_work(repo, monkeypatch):
    """Review of 1.7, finding 1: run_job sets the final status before its finally awaits
    finish_worktree in a thread. A wait that woke on a progress tick in that window returned a
    record with no `kept`/`note` and a row still `running`; the wait is on the event now."""
    import asyncio

    from localagents import runner

    reg, res, kw = _drive_setup(monkeypatch)
    _slow(monkeypatch, "finish_worktree", 0.5)
    store, j = _store_job(repo)

    async def progress(_):
        pass

    async def go():
        task = asyncio.create_task(runner.run_job(j, reg, res, **kw))
        got = await store.wait(j.id, 30, progress=progress, interval_s=0.05)
        snap = (got._done.is_set(), dict(got.worktree or {}),
                store.conn.execute("SELECT status FROM jobs WHERE id = ?", (j.id,)).fetchone()[0])
        await task
        return snap

    done, wt, row_status = asyncio.run(go())
    assert done
    assert wt.get("kept") is False and wt.get("note") == "no changes; worktree removed"
    assert row_status == "failed"  # the stub session ends without a result message


def test_wait_many_returns_only_after_the_finally_work(repo, monkeypatch):
    import asyncio

    from localagents import runner

    reg, res, kw = _drive_setup(monkeypatch)
    _slow(monkeypatch, "finish_worktree", 0.5)
    store, j = _store_job(repo)

    async def progress(_):
        pass

    async def go():
        task = asyncio.create_task(runner.run_job(j, reg, res, **kw))
        [got] = await store.wait_many([j.id], 30, progress=progress, interval_s=0.05)
        snap = (got._done.is_set(), dict(got.worktree or {}))
        await task
        return snap

    done, wt = asyncio.run(go())
    assert done and wt.get("kept") is False


def test_the_row_turns_terminal_only_after_the_finally_work(repo, monkeypatch):
    """Story 1.10a row 2 (review of 1.10, P2): another server's wait (_wait_foreign) hands back
    the first terminal row it sees, so a terminal row must be a settled record. It is because
    run_job writes the terminal status only in its last changed(), after _finish and
    _done.set(). Pinned through run_job: while finish_worktree is blocked the in-process status
    is already terminal but the row still says `running` and a foreign waiter has not returned;
    released, the row is terminal and the waiter's record is the finished one."""
    import asyncio
    import threading

    from localagents import runner, server

    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.02)
    reg, res, kw = _drive_setup(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    real = runner.finish_worktree

    def blocked(j):
        entered.set()
        release.wait(10)
        return real(j)

    monkeypatch.setattr(runner, "finish_worktree", blocked)
    store, j = _store_job(repo)
    assert j.id not in server.store.jobs  # server's wait on this id takes the foreign path

    def row_status():
        return store.conn.execute("SELECT status FROM jobs WHERE id = ?", (j.id,)).fetchone()[0]

    async def go():
        task = asyncio.create_task(runner.run_job(j, reg, res, **kw))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.01)
            waiter = asyncio.create_task(server.wait_job(j.id, timeout_s=10))
            await asyncio.sleep(0.2)  # ten polls of the row
            during = (j.status, row_status(), waiter.done())
        finally:
            release.set()
        out = await waiter
        await task
        return during, row_status(), out

    (status_during, row_during, waiter_done_during), row_after, out = asyncio.run(go())
    assert status_during == "failed"  # the stub session ends without a result: terminal in-process
    assert row_during == "running" and not waiter_done_during
    assert row_after == "failed"
    assert out["status"] == "failed"
    assert out["worktree"]["kept"] is False and out["worktree"]["note"] == "no changes; worktree removed"


def test_cancel_during_make_worktree_leaves_no_tree(repo, monkeypatch):
    """Review of 1.7, finding 2: a cancel landing while make_worktree runs in its thread used to
    leave the tree and branch the thread went on to create, with job.worktree None, so the finally
    had nothing to remove. The call is shielded: the tree is recorded, then removed."""
    import asyncio

    from localagents import runner

    reg, res, kw = _drive_setup(monkeypatch)
    _slow(monkeypatch, "make_worktree", 0.5)
    store, j = _store_job(repo)

    async def go():
        j._task = asyncio.create_task(runner.run_job(j, reg, res, **kw))
        await asyncio.sleep(0.15)
        j._task.cancel()
        await store.wait(j.id, 10)

    asyncio.run(go())
    assert j.status == "cancelled" and j.settled
    assert j.worktree is not None and j.worktree["kept"] is False
    assert not Path(j.worktree["root"]).exists()
    assert not _branch_exists(repo, f"local-agent/{j.name}")
    assert not (state_dir() / "worktrees" / repo.name / j.name).exists()


def test_cancel_during_the_first_metrics_probe_finishes_cancelled(repo, monkeypatch):
    """Review of 1.7, finding 3: the first fetch_metrics await sat before the try, so a cancel
    landing there skipped the finally: the job stayed `running` in memory and in its row, forever."""
    import asyncio

    from localagents import runner

    async def slow_metrics(*a, **kw):
        await asyncio.sleep(1.0)
        return None

    reg, res, kw = _drive_setup(monkeypatch, metrics=slow_metrics)
    store, j = _store_job(repo, isolation="none")

    async def go():
        j._task = asyncio.create_task(runner.run_job(j, reg, res, **kw))
        await asyncio.sleep(0.2)
        j._task.cancel()
        await store.wait(j.id, 5)

    asyncio.run(go())
    assert j.status == "cancelled" and j.settled and j.finished_at is not None
    assert store.conn.execute("SELECT status FROM jobs WHERE id = ?", (j.id,)).fetchone()[0] == "cancelled"


def test_kept_note_shows_untracked_files_to_the_reviewer(repo, tmp_path):
    """Review 3.9B B1: a tree whose only change is an untracked file got a note saying
    `Review with: git -C <root> diff <base>`, which prints nothing for an untracked file while
    the diffstat names it. The review instruction now leads with `status --short`."""
    j = job("j11", repo, tmp_path / "logs" / "j11")
    j.worktree = make_worktree(j)
    (Path(j.worktree["path"]) / "newfile.txt").write_text("brand new\n")
    finish_worktree(j)
    assert j.worktree["kept"] is True
    assert "newfile.txt" in j.worktree["diffstat"]
    assert "status --short" in j.worktree["note"]


def test_finish_over_a_missing_directory_records_it(repo, tmp_path):
    """Review 3.9B B2: if the worktree directory is gone at finish time, `git status` raised
    FileNotFoundError from subprocess's cwd and the worktree JSON never gained kept/note.
    A missing directory is recorded: prune, kept=False, a note naming it, the branch kept."""
    import shutil

    j = job("j12", repo, tmp_path / "logs" / "j12")
    j.worktree = make_worktree(j)
    root = j.worktree["root"]
    shutil.rmtree(root)
    finish_worktree(j)
    assert j.worktree["kept"] is False
    assert "removed while the job ran" in j.worktree["note"]
    # the branch may hold commits the user wants: it is left for them to delete
    assert _branch_exists(repo, "local-agent/j12")
    assert root not in git(repo, "worktree", "list")
    log = (tmp_path / "logs" / "j12" / "transcript.txt").read_text()
    assert f"[worktree-error] worktree directory missing at {root}" in log


def test_cancel_during_make_worktree_sets_run_cwd_as_the_row_does(repo, monkeypatch):
    """Review 3.9B B3: a cancel absorbed during the worktree step recorded the tree on the job
    but left job.run_cwd at the original cwd, while derive's row says the tree's path: the same
    job answered run_cwd two ways depending on the reader."""
    import asyncio

    from localagents import derive, runner

    reg, res, kw = _drive_setup(monkeypatch)
    _slow(monkeypatch, "make_worktree", 0.5)
    store, j = _store_job(repo)

    async def go():
        j._task = asyncio.create_task(runner.run_job(j, reg, res, **kw))
        await asyncio.sleep(0.15)
        j._task.cancel()
        await store.wait(j.id, 10)

    asyncio.run(go())
    assert j.status == "cancelled" and j.worktree is not None
    row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (j.id,)).fetchone()
    assert j.run_cwd == j.worktree["path"]
    assert derive.summary_from_row(store.conn, row)["run_cwd"] == j.worktree["path"]


def test_make_worktree_removes_its_tree_when_a_step_after_the_add_fails(repo, monkeypatch):
    """DeepSeek review of 6005f94: make_worktree's steps after `git worktree add` (rev-parse,
    the relative_to of the cwd) could raise and leave a tree and branch nothing removes, since
    job.worktree was never set. It removes its own tree and branch, then re-raises; through
    run_job the job fails with the error in its transcript."""
    import asyncio

    from localagents import runner

    class Unrelated(type(Path())):
        def relative_to(self, *a, **kw):
            raise ValueError(f"{self} is not under the repo")

    reg, res, kw = _drive_setup(monkeypatch)
    monkeypatch.setattr(runner, "Path", Unrelated)
    store, j = _store_job(repo)
    asyncio.run(runner.run_job(j, reg, res, **kw))
    assert j.status == "failed" and "is not under the repo" in j.error
    assert "is not under the repo" in j.transcript_tail()
    assert j.worktree is None
    assert not (state_dir() / "worktrees" / repo.name / j.name).exists()
    assert not _branch_exists(repo, f"local-agent/{j.name}")
    assert "local-agent" not in git(repo, "worktree", "list")


def test_make_worktree_names_a_branch_its_cleanup_could_not_delete(repo, tmp_path, monkeypatch):
    """Story 1.9 item 5: the cleanup after a failed post-add step ran `git branch -D` unchecked
    and said nothing when it failed — a leftover branch was never named. A failed branch -D is
    appended to the raised error; the tree half (as before) too when its removal fails."""
    from localagents import runner

    j = job("j5", repo, tmp_path / "logs" / "j5")
    real = runner._git

    def failing(cwd, *args):
        if args[:2] == ("branch", "-D"):
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: simulated branch failure")
        return real(cwd, *args)

    monkeypatch.setattr(runner, "_git", failing)

    class Unrelated(type(Path())):
        def relative_to(self, *a, **kw):
            raise ValueError(f"{self} is not under the repo")

    monkeypatch.setattr(runner, "Path", Unrelated)
    with pytest.raises(RuntimeError) as ei:
        make_worktree(j)
    assert "fatal: simulated branch failure" in str(ei.value)
    assert "local-agent/j5" in str(ei.value)  # the leftover branch is named
    assert _branch_exists(repo, "local-agent/j5")  # and really is left


def test_a_make_worktree_error_during_a_cancel_is_not_dropped(repo, monkeypatch):
    """_make_worktree_shielded's cancel path waited for the thread and dropped a non-cancel
    exception from it without a word. The cancel still wins (the job is `cancelled`), and the
    thread's error is a [worktree-error] transcript line (DeepSeek review of 6005f94)."""
    import asyncio
    import time

    from localagents import runner

    def slow_fail(job):
        time.sleep(0.5)
        raise RuntimeError("git worktree add failed: fatal: simulated")

    reg, res, kw = _drive_setup(monkeypatch)
    monkeypatch.setattr(runner, "make_worktree", slow_fail)
    store, j = _store_job(repo)

    async def go():
        j._task = asyncio.create_task(runner.run_job(j, reg, res, **kw))
        await asyncio.sleep(0.15)
        j._task.cancel()
        await store.wait(j.id, 10)

    asyncio.run(go())
    assert j.status == "cancelled" and j.settled
    assert "[worktree-error] RuntimeError: git worktree add failed: fatal: simulated" in j.transcript_tail()


WORKTREE_CANCEL_LINE = "[cancel] 2 cancel(s) arrived during the job's worktree step; the tree was finished and removed"


def _block_worktree_add(monkeypatch):
    """Block `git worktree add -b` in its thread until the returned Event is set."""
    import threading

    from localagents import runner

    reached, release = threading.Event(), threading.Event()
    real_git = runner._git

    def blocking_git(cwd, *args):
        if args[:3] == ("worktree", "add", "-b"):
            reached.set()
            release.wait(10)
        return real_git(cwd, *args)

    monkeypatch.setattr(runner, "_git", blocking_git)
    return reached, release


def test_a_partially_failed_add_deletes_the_branch_it_created(repo, tmp_path):
    """Story 1.9a row 4 (1.9 review, probe 1g): git creates the branch before it checks the
    target, so `git worktree add -b` against an existing directory fails with the branch left
    behind and an error that never said so. The failed add removes the branch it created and
    the raised error names it."""
    j = job("j8", repo, tmp_path / "logs" / "j8")
    wt = state_dir() / "worktrees" / repo.name / j.name
    wt.mkdir(parents=True)
    (wt / "occupied").write_text("leftover\n")  # the add fails: the target already exists
    with pytest.raises(RuntimeError) as ei:
        make_worktree(j)
    assert "git worktree add failed" in str(ei.value)  # git's own reason is still there
    assert f"branch local-agent/{j.name} removed" in str(ei.value)
    assert not _branch_exists(repo, f"local-agent/{j.name}")
    assert "main" in git(repo, "branch", "--list", "main")  # the clean-up touched nothing else


def test_a_cancel_during_make_worktree_prints_a_cancel_line(repo, monkeypatch):
    """Story 1.9a row 3 (1.9 review, probe 1b): a cancel absorbed while the shielded git thread
    runs was counted nowhere and logged nowhere, and the [worktree] line is written only after
    the shield returns — so a job cancelled during worktree creation had an empty transcript.
    Every absorbed cancel is counted and named before the cancel is re-raised."""
    import asyncio

    from localagents import runner

    reg, res, kw = _drive_setup(monkeypatch)
    _blocking_query(monkeypatch)
    store, j = _store_job(repo)
    reached, release = _block_worktree_add(monkeypatch)

    async def go():
        j._task = asyncio.create_task(runner.run_job(j, reg, res, **kw))
        assert await asyncio.get_running_loop().run_in_executor(None, reached.wait, 10)
        await asyncio.sleep(0.1)  # the shield is awaiting the thread
        j._task.cancel()
        await asyncio.sleep(0.05)
        j._task.cancel()  # a second cancel, while the shield absorbs the first
        await asyncio.sleep(0.1)
        release.set()
        await store.wait(j.id, 10)
        await asyncio.wait([j._task], timeout=10)
        return j._task.cancelling()

    cancelling = asyncio.run(go())
    lines = [ln for ln in j.transcript_tail().splitlines() if ln.startswith("[cancel]")]
    assert cancelling == 2
    assert j.settled and j.status == "cancelled"
    assert j.worktree is not None and j.worktree["kept"] is False
    assert not Path(j.worktree["root"]).exists()
    assert not _branch_exists(repo, f"local-agent/{j.name}")
    assert lines == [WORKTREE_CANCEL_LINE]


def test_two_cancel_job_calls_during_make_worktree_print_a_cancel_line(repo, monkeypatch):
    """The tool door for the same window (1.9 review, probe 1e): two cancel_job calls while
    `git worktree add` is in flight both return the cancelled record, and the transcript now
    says what the cancels interrupted instead of being empty."""
    import asyncio

    from localagents import runner, server

    reg, res, kw = _drive_setup(monkeypatch)
    _blocking_query(monkeypatch)
    store, j = _store_job(repo)
    monkeypatch.setattr(server, "store", store)
    reached, release = _block_worktree_add(monkeypatch)

    async def go():
        j._task = asyncio.create_task(runner.run_job(j, reg, res, **kw))
        assert await asyncio.get_running_loop().run_in_executor(None, reached.wait, 10)
        await asyncio.sleep(0.1)
        c1 = asyncio.create_task(server.cancel_job(j.id))
        await asyncio.sleep(0.1)
        c2 = asyncio.create_task(server.cancel_job(j.id))  # job.done is still False: cancels too
        await asyncio.sleep(0.1)
        assert j._task.cancelling() == 2
        release.set()
        out = await asyncio.gather(c1, c2)
        await asyncio.wait([j._task], timeout=10)
        return out

    out1, out2 = asyncio.run(go())
    lines = [ln for ln in j.transcript_tail().splitlines() if ln.startswith("[cancel]")]
    assert out1["status"] == "cancelled" and out2["status"] == "cancelled"
    assert j.settled and j.worktree["kept"] is False
    assert not Path(j.worktree["root"]).exists()
    assert lines == [WORKTREE_CANCEL_LINE]


def test_cancel_job_on_an_unsettled_job_returns_the_settled_record(repo, monkeypatch):
    """DeepSeek review of 6005f94, nit: cancel_job on a job whose status is final but whose
    finally is still finishing the worktree returned at once, without `kept`/`note`. It waits
    for the settle (up to its 15 s) like the waits do."""
    import asyncio

    from localagents import runner, server

    reg, res, kw = _drive_setup(monkeypatch)
    _blocking_query(monkeypatch)
    _slow(monkeypatch, "finish_worktree", 1.0)
    store, j = _store_job(repo)
    monkeypatch.setattr(server, "store", store)

    async def go():
        j._task = asyncio.create_task(runner.run_job(j, reg, res, **kw))
        await asyncio.sleep(0.3)
        j._task.cancel()
        await asyncio.sleep(0.2)
        assert j.done and not j.settled
        # a copy at return time: summary() hands out the live worktree dict, which the finally
        # would go on to fill and hide the defect
        return json.loads(json.dumps(await server.cancel_job(j.id)))

    out = asyncio.run(go())
    assert out["status"] == "cancelled"
    assert out["worktree"]["kept"] is False and out["worktree"]["note"] == "no changes; worktree removed"


def _blocking_query(monkeypatch):
    """A session that never ends on its own: only a cancel stops it."""
    import asyncio

    from localagents import runner

    def fake_query(prompt, options):
        async def forever():
            await asyncio.sleep(3600)
            yield

        return forever()

    monkeypatch.setattr(runner, "query", fake_query)


@pytest.mark.parametrize("where", ["finish_worktree", "metrics"])
def test_a_second_cancel_inside_the_finally_cannot_strand_the_job(repo, monkeypatch, where):
    """DeepSeek review of 6005f94: two cancel_job calls both pass `if job.done` before either
    cancel lands, so the second cancel arrives inside run_job's finally — at the after-metrics
    await or at finish_worktree's thread. It escaped `except Exception`, skipped _done.set() and
    the final changed(): the row stayed `running` and every waiter ran to its timeout. The
    finally's awaits now absorb cancels until their work is done and its tail always runs."""
    import asyncio
    import time

    from localagents import runner

    calls = {"n": 0}

    async def metrics(*a, **kw):
        calls["n"] += 1
        if calls["n"] > 1 and where == "metrics":
            await asyncio.sleep(1.0)
        return {"generated_tokens": float(calls["n"])}

    reg, res, kw = _drive_setup(monkeypatch, metrics=metrics)
    _blocking_query(monkeypatch)
    if where == "finish_worktree":
        _slow(monkeypatch, "finish_worktree", 1.0)
    store, j = _store_job(repo)
    seen = {}

    async def go():
        j._task = asyncio.create_task(runner.run_job(j, reg, res, **kw))
        await asyncio.sleep(0.3)  # the tree exists and the session is blocked
        j._task.cancel()
        await asyncio.sleep(0.2)  # now inside the finally's slow await
        assert j.done and not j.settled
        t0 = time.monotonic()
        j._task.cancel()  # the second cancel_job
        await store.wait(j.id, 5)
        seen["settle_s"] = time.monotonic() - t0
        await asyncio.wait([j._task], timeout=5)
        seen["task_done"] = j._task.done()

    asyncio.run(go())
    assert j.settled and seen["settle_s"] < 1.5 and seen["task_done"], seen
    assert j.status == "cancelled"
    assert store.conn.execute("SELECT status FROM jobs WHERE id = ?", (j.id,)).fetchone()[0] == "cancelled"
    assert j.worktree["kept"] is False and not Path(j.worktree["root"]).exists()
    assert not _branch_exists(repo, f"local-agent/{j.name}")
    if where == "metrics":
        assert j.metrics == {"generated_tokens": 1}  # the probe was waited for, not abandoned


def test_the_cancel_line_counts_every_absorbed_cancel(repo, monkeypatch):
    """Story 1.9 item 4 (DeepSeek probe): two concurrent cancel_job calls land back to back,
    and CPython folds the second Task.cancel() into the first delivery, so the loop's
    except-based count saw one cancel where two arrived — and when both landed before the
    finally, none. The count comes from the task's cancelling() delta across the step: every
    absorbed cancel is counted."""
    import asyncio

    from localagents import runner

    calls = {"n": 0}

    async def metrics(*a, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            await asyncio.sleep(1.0)  # slow after-metrics: the step the cancels land in
        return {"generated_tokens": float(calls["n"])}

    reg, res, kw = _drive_setup(monkeypatch, metrics=metrics)
    _blocking_query(monkeypatch)
    store, j = _store_job(repo, isolation="none")

    async def go():
        j._task = asyncio.create_task(runner.run_job(j, reg, res, **kw))
        await asyncio.sleep(0.3)
        j._task.cancel()
        await asyncio.sleep(0.2)  # now inside the finally's slow after-metrics await
        assert j.done and not j.settled
        j._task.cancel()
        j._task.cancel()  # two concurrent cancels: the back-to-back pattern
        await store.wait(j.id, 5)
        await asyncio.wait([j._task], timeout=5)

    asyncio.run(go())
    assert j.settled and j.status == "cancelled"
    assert "[cancel] 2 cancel(s) arrived during the job's finish step; it ran to the end" in j.transcript_tail()
