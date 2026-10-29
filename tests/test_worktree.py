"""Worktree lifecycle against a real throwaway git repo (no model server needed)."""

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
