"""finish_worktree's removal rule, its bound, and its one reader of the diff (2026-11-15).

- D1: a worktree is removed only when nothing in it would be lost. ``git status --porcelain``
  does not list ignored files, so a review whose only output was ``t/REVIEW.md`` (``t/*``
  ignored, this project's convention for reports) was judged "no changes" and deleted with the
  tree. What an agent's tools recreate on their next run (``.venv``, ``node_modules``,
  ``__pycache__``, test-runner caches) is not something that would be lost.
- D2: every git call on the finish path is bounded; a timeout is named in the note, the tree is
  kept (a read that could not see never says unchanged), and the job settles.
- D3: the diff's names and diffstat come from feedback's copy-index read, so the tree's own
  index is never touched (what the agent staged stays staged).
"""

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from localagents import runner
from localagents.jobs import Job, JobStore
from localagents.router import feedback
from localagents.runner import finish_worktree, make_worktree
from localagents.harnesses import claude_code

REAL_GIT = shutil.which("git")


def git(cwd, *args):
    return subprocess.run(
        [REAL_GIT, "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n")
    (r / ".gitignore").write_text(
        "t/*\n!t/conftest.py\n.venv/\nnode_modules/\n__pycache__/\n*.pyc\n.pytest_cache/\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def _job(repo, tmp_path, jid="jf1"):
    logs = tmp_path / "logs" / jid
    logs.mkdir(parents=True)
    j = Job(id=jid, task="t", model="m", endpoint="e", base_url="http://e", cwd=str(repo),
            run_cwd=str(repo), isolation="worktree", log_dir=str(logs))
    j.worktree = make_worktree(j)
    return j


def _transcript(j):
    p = Path(j.log_dir) / "transcript.txt"
    return p.read_text() if p.exists() else ""


# --- D1: what would be lost ------------------------------------------------------------------


def test_an_ignored_report_keeps_the_tree(repo, tmp_path):
    """The reported loss: the job's only output is a file git ignores. The tree is kept, the
    report is still there, the note says why and names it, and diff_files keeps its meaning
    (the diff's list: ignored files are not in it)."""
    j = _job(repo, tmp_path)
    report = Path(j.worktree["root"]) / "t" / "REVIEW.md"
    report.parent.mkdir()
    report.write_text("findings\n")
    finish_worktree(j)
    wt = j.worktree
    assert wt["kept"] is True
    assert report.read_text() == "findings\n"
    assert "t/REVIEW.md" in wt["note"] and "ignores" in wt["note"]
    assert wt["files"] == [] and wt["files_n"] == 0
    assert git(repo, "branch", "--list", wt["branch"])  # the branch stays with the tree


def test_what_the_tools_recreate_does_not_keep_the_tree(repo, tmp_path):
    """uv run's .venv, npm's node_modules, interpreter and test-runner caches: an ordinary run
    leaves them, and a tree holding only these (and no change) is removed as before."""
    j = _job(repo, tmp_path)
    root = Path(j.worktree["root"])
    for rel in (".venv/lib/python3/site.py", "node_modules/x/index.js", "pkg/__pycache__/m.cpython-313.pyc",
                "stray.pyc", ".pytest_cache/v/cache/lastfailed"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x\n")
    finish_worktree(j)
    assert j.worktree["kept"] is False and not root.exists()
    assert j.worktree["note"] == "no changes; worktree removed"


def test_an_ignored_file_inside_a_tool_directory_does_not_keep_the_tree(repo, tmp_path):
    """The match is by path component: t/.venv/... is an environment, not a report."""
    j = _job(repo, tmp_path)
    p = Path(j.worktree["root"]) / "t" / ".venv" / "bin" / "python"
    p.parent.mkdir(parents=True)
    p.write_text("x\n")
    finish_worktree(j)
    assert j.worktree["kept"] is False


def test_a_changed_tree_names_its_ignored_files_too(repo, tmp_path):
    """A tree kept for its diff that also holds an ignored report: status and the diff do not
    show the report, so the note does."""
    j = _job(repo, tmp_path)
    root = Path(j.worktree["root"])
    (root / "b.txt").write_text("new\n")
    (root / "t").mkdir()
    (root / "t" / "REVIEW.md").write_text("r\n")
    finish_worktree(j)
    wt = j.worktree
    assert wt["kept"] is True and "Changes left" in wt["note"]
    assert "t/REVIEW.md" in wt["note"]
    assert wt["files"] == ["b.txt"]  # diff_files' meaning is unchanged


def test_the_named_ignored_files_are_bounded(repo, tmp_path):
    j = _job(repo, tmp_path)
    t = Path(j.worktree["root"]) / "t"
    t.mkdir()
    n = runner.IGNORED_NAMED + 5
    for i in range(n):
        (t / f"r{i:02d}.md").write_text("r\n")
    finish_worktree(j)
    note = j.worktree["note"]
    assert j.worktree["kept"] is True
    assert "t/r00.md" in note and f"t/r{runner.IGNORED_NAMED - 1:02d}.md" in note
    assert f"t/r{runner.IGNORED_NAMED:02d}.md" not in note
    assert "and 5 more" in note and f"{n} paths" in note


# --- D2: the bound ---------------------------------------------------------------------------


@pytest.fixture
def hanging_git(tmp_path, monkeypatch):
    """A git on PATH that sleeps before the named subcommand, as a hung git would; its sleep is
    a child of the shell, so it holds the output pipe after the shell is killed (the case a
    plain subprocess.run timeout still blocks on). Returns a setter: hang("status")."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "git"
    stub.write_text(f'#!/bin/sh\ncase "$1 $2" in "$HANG_ON"*) sleep 20;; esac\nexec {REAL_GIT} "$@"\n')
    stub.chmod(0o755)
    monkeypatch.setattr(feedback, "GIT_TIMEOUT_S", 1.0)

    def hang(prefix):
        monkeypatch.setenv("HANG_ON", prefix)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    return hang


@pytest.mark.parametrize("step,changed", [("status", False), ("rev-parse HEAD", False),
                                          ("worktree remove", False), ("add -A", True),
                                          ("worktree prune", None)])
def test_a_hung_git_is_bounded_named_and_keeps_the_tree(repo, tmp_path, hanging_git, step, changed):
    j = _job(repo, tmp_path)
    root = Path(j.worktree["root"])
    if changed:
        (root / "b.txt").write_text("new\n")
    if changed is None:  # the tree vanished under the job: the finish prunes
        shutil.rmtree(root)
    hanging_git(step)
    started = time.monotonic()
    finish_worktree(j)
    assert time.monotonic() - started < 10
    wt = j.worktree
    assert "timed out after 1s" in wt["note"] and f"git {step}" in wt["note"]
    assert "[worktree-error]" in _transcript(j) and "timed out" in _transcript(j)
    if changed is None:
        return
    assert wt["kept"] is True and root.is_dir()
    if changed:
        assert "Changes left" in wt["note"] and "diffstat" not in wt and "files" not in wt
    elif step != "worktree remove":
        assert "files" not in wt  # nothing was read: the list is unknown, never empty


def test_a_hung_git_lets_the_job_settle(repo, tmp_path, hanging_git, monkeypatch):
    """Through run_job: the finish's git hangs, the job still settles with the tree kept."""
    from localagents.registry import Defaults, Endpoint, Registry, Resolution

    async def no_metrics(*a, **kw):
        return None

    def quiet_query(prompt, options):
        async def session():
            return
            yield

        return session()

    monkeypatch.setattr(runner, "fetch_metrics", no_metrics)
    monkeypatch.setattr(claude_code, "query", quiet_query)
    ep = Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")
    reg = Registry(path=None, endpoints={"e": ep}, defaults=Defaults(job_retention_days=0), raw={}, local={})
    store = JobStore()
    j = store.new(task="t", model="served-model", endpoint="e", base_url="http://e:8080", cwd=str(repo),
                  run_cwd=str(repo), isolation="worktree", backend="llama.cpp", host="e")

    hanging_git("status")  # make_worktree never runs status: only the finish hangs

    async def go():
        await asyncio.wait_for(runner.run_job(
            j, reg, Resolution(ep, "served-model"), permission_mode=None, max_turns=None,
            allowed_tools=None, system_prompt_append=None, resume_session=None, timeout_s=10,
            shim_base_url="http://127.0.0.1:1/job/x"), 15)

    asyncio.run(go())
    assert j._done.is_set() and j.worktree["kept"] is True
    assert "timed out" in j.worktree["note"]


# --- D3: one reader, the real index untouched ------------------------------------------------


def test_what_the_agent_staged_stays_staged(repo, tmp_path):
    """The old finish ran ``add -A`` and ``reset -q`` on the tree's own index, unstaging what the
    agent had staged. The copy-index read leaves the index as the session left it."""
    j = _job(repo, tmp_path)
    root = Path(j.worktree["root"])
    (root / "staged.txt").write_text("s\n")
    git(root, "add", "staged.txt")
    (root / "loose.txt").write_text("l\n")
    finish_worktree(j)
    assert git(root, "diff", "--cached", "--name-only") == "staged.txt"
    assert sorted(j.worktree["files"]) == ["loose.txt", "staged.txt"]
    assert "staged.txt" in j.worktree["diffstat"] and "loose.txt" in j.worktree["diffstat"]


def test_the_finish_and_the_gates_read_the_same_names(repo, tmp_path):
    """One reader: finish_worktree's file list is read_diff's name list for the same tree."""
    j = _job(repo, tmp_path)
    root = Path(j.worktree["root"])
    (root / "a.txt").write_text("edited\n")
    (root / "new.txt").write_text("n\n")
    names, _ = feedback.read_diff(j.worktree, j.log_dir)
    finish_worktree(j)
    assert j.worktree["files"] == names == ["a.txt", "new.txt"]


def test_the_copy_index_is_removed_after_the_read(repo, tmp_path):
    j = _job(repo, tmp_path)
    (Path(j.worktree["root"]) / "b.txt").write_text("b\n")
    finish_worktree(j)
    assert j.worktree["files"] == ["b.txt"]
    assert not list(Path(j.log_dir).glob("feedback-*"))


def test_read_diff_says_unreadable_when_git_hangs(repo, tmp_path, hanging_git):
    """The gates' read shares the bound and keeps its contract: None, never a partial diff."""
    j = _job(repo, tmp_path)
    (Path(j.worktree["root"]) / "b.txt").write_text("b\n")
    hanging_git("add -A")
    started = time.monotonic()
    assert feedback.read_diff(j.worktree, j.log_dir) is None
    assert time.monotonic() - started < 10


def test_only_the_checkout_is_left_unbounded(monkeypatch):
    """``worktree add`` is a checkout, whose time grows with the repository; every other call
    runner makes is bounded."""
    seen = []
    monkeypatch.setattr(feedback, "git", lambda cwd, *args, **kw: seen.append((args[:2], kw["bounded"])))
    for args in (("worktree", "add", "-b", "b", "p", "HEAD"), ("worktree", "remove", "--force", "p"),
                 ("status", "--porcelain"), ("branch", "-D", "b")):
        runner._git("/r", *args)
    assert seen == [(("worktree", "add"), False), (("worktree", "remove"), True),
                    (("status", "--porcelain"), True), (("branch", "-D"), True)]


def test_a_tree_that_came_back_clean_drops_the_old_diffstat(repo, tmp_path):
    """A resumed job that undid the first job's change: the tree is removed, and the first
    finish's diffstat and file list are not this tree's truth."""
    j1 = _job(repo, tmp_path, "jr1")
    (Path(j1.worktree["root"]) / "b.txt").write_text("b\n")
    finish_worktree(j1)
    assert "b.txt" in j1.worktree["diffstat"]
    logs = tmp_path / "logs" / "jr2"
    logs.mkdir(parents=True)
    j2 = Job(id="jr2", task="t", model="m", endpoint="e", base_url="http://e", cwd=str(repo),
             run_cwd=str(repo), isolation="worktree", log_dir=str(logs), worktree=dict(j1.worktree))
    j2.worktree = make_worktree(j2)
    (Path(j2.worktree["root"]) / "b.txt").unlink()
    finish_worktree(j2)
    assert j2.worktree["kept"] is False and "diffstat" not in j2.worktree
    assert j2.worktree["files"] == []
