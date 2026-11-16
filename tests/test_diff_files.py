"""What a job's record says it changed (coordinator's report, 2026-11-15: "one review job
reported no files touched although it wrote its report through shell redirection, so that
field only reflects the edit tools").

The rule: a field never claims more than it knows. ``files_touched`` is the paths this job's
edit tools were given; it does not see shell writes. For a worktree job the diff's own file list
is the truth and the record carries it (``diff_files``); for a job with no worktree nothing can
be attributed in a shared directory, and the record says so (null, never an empty list)."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from localagents import derive
from localagents.jobs import JobStore
from localagents.runner import finish_worktree, make_worktree

# long enough that `git diff --stat` cuts it to ".../...": the diffstat alone does not name it
LONG = "deeply/nested/directory/structure/for/the/review/REVIEW-of-the-long-story-name.md"


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "init")
    return r


def _job(repo, isolation="worktree"):
    store = JobStore()
    j = store.new(task="t", model="served-model", endpoint="e", base_url="http://e:8080", cwd=str(repo),
                  run_cwd=str(repo), isolation=isolation, backend="llama.cpp", host="e")
    if isolation == "worktree":
        j.worktree = make_worktree(j)
        j.run_cwd = j.worktree["path"]
    return store, j


def _shell_write(j, rel: str, where: str) -> None:
    """What the reported job did: a Bash call whose redirection writes the report. The tool_calls
    row records the command; the edit tools never see the path."""
    target = Path(where) / rel
    cmd = f"mkdir -p {target.parent} && cat > {target} <<'EOF'\nreport\nEOF"
    t = time.time()
    j.record_tool_call(t, "Bash", "u-bash", cmd[:120])
    subprocess.run(["sh", "-c", cmd], check=True)
    j.record_tool_result("u-bash", t + 1, False)


def _finish(store, j):
    if j.worktree:
        finish_worktree(j)
    j.status, j.started_at, j.finished_at = "succeeded", 1000.0, 1060.0
    j.changed()
    row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (j.id,)).fetchone()
    return j.summary(), derive.summary_from_row(store.conn, row)


def test_a_worktree_jobs_shell_write_is_named_in_its_record(repo):
    """The defect: a worktree job whose only write is a Bash redirection had files_touched []
    and no field naming the file in full (the diffstat cuts a long path to ".../")."""
    store, j = _job(repo)
    _shell_write(j, LONG, j.worktree["path"])
    a, b = _finish(store, j)
    for s in (a, b):
        assert s["files_touched"] == []  # the edit tools never saw it: that field's own truth
        assert LONG in json.dumps(s), "nothing in the record names the file the job wrote"
        assert s["diff_files"] == [LONG]
    assert a == b  # the in-process and the row-derived summaries agree, diff_files included


def test_a_job_with_no_worktree_says_its_file_list_is_unknown(repo):
    """Nothing in a shared directory can be attributed to one job: diff_files is null, never
    an empty list, even though the job wrote a file there."""
    store, j = _job(repo, isolation="none")
    _shell_write(j, "t/REVIEW.md", str(repo))
    for s in _finish(store, j):
        assert s["files_touched"] == []
        assert s["diff_files"] is None
        assert s["worktree"] is None


def test_a_worktree_job_not_finished_yet_has_no_file_list(repo):
    store, j = _job(repo)
    _shell_write(j, "x.txt", j.worktree["path"])
    j.status = "running"
    j.changed()
    row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (j.id,)).fetchone()
    assert j.summary()["diff_files"] is None
    assert derive.summary_from_row(store.conn, row)["diff_files"] is None


def test_an_unchanged_worktree_records_an_empty_list(repo):
    """A clean status at the base commit is a diff known to be empty: [] here is the truth."""
    store, j = _job(repo)
    a, b = _finish(store, j)
    assert j.worktree["kept"] is False and j.worktree["files_n"] == 0
    assert a["diff_files"] == [] and b["diff_files"] == []


def test_the_diff_list_names_every_kind_of_change(repo):
    """An edit-tool write, a shell write, a deletion, a rename (both paths) and a file the job
    committed are all in diff_files; files_touched holds only the edit tool's path."""
    (repo / "old.txt").write_text("to be renamed\n")
    (repo / "gone.txt").write_text("to be deleted\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "more")
    store, j = _job(repo)
    wt = Path(j.worktree["path"])
    t = time.time()
    j.record_tool_call(t, "Edit", "u-edit", str(wt / "a.txt"))
    (wt / "a.txt").write_text("edited\n")
    j.record_tool_result("u-edit", t + 1, False)
    _shell_write(j, "report.md", str(wt))
    git(wt, "rm", "-q", "gone.txt")
    git(wt, "mv", "old.txt", "new.txt")
    (wt / "committed.txt").write_text("c\n")
    git(wt, "add", "committed.txt")
    git(wt, "commit", "-q", "-m", "the job's own commit")
    for s in _finish(store, j):
        assert sorted(s["diff_files"]) == ["a.txt", "committed.txt", "gone.txt", "new.txt", "old.txt", "report.md"]
        assert s["files_touched"] == [str(wt / "a.txt")]
        assert s["worktree"]["files_n"] == 6


def test_the_list_is_bounded_and_the_count_is_whole(repo):
    """At most DIFF_FILES_MAX names ride in the record; worktree.files_n counts them all."""
    store, j = _job(repo)
    wt = Path(j.worktree["path"])
    n = derive.DIFF_FILES_MAX + 5
    for i in range(n):
        (wt / f"f{i:04d}.txt").write_text("x\n")
    for s in _finish(store, j):
        assert len(s["diff_files"]) == derive.DIFF_FILES_MAX
        assert s["diff_files"] == [f"f{i:04d}.txt" for i in range(derive.DIFF_FILES_MAX)]
        assert s["worktree"]["files_n"] == n


def test_the_summary_carries_the_list_once(repo):
    """The stored worktree keeps ``files``; the summary's worktree copy leaves it out, since
    diff_files carries it."""
    store, j = _job(repo)
    (Path(j.worktree["path"]) / "n.txt").write_text("n\n")
    for s in _finish(store, j):
        assert "files" not in s["worktree"] and s["worktree"]["files_n"] == 1
        assert s["diff_files"] == ["n.txt"]
    stored = json.loads(store.conn.execute("SELECT worktree FROM jobs WHERE id = ?", (j.id,)).fetchone()[0])
    assert stored["files"] == ["n.txt"]
    assert "files" in j.worktree  # the summary copied; the job's own dict is unchanged


def test_a_diff_git_cannot_read_is_null_not_empty(repo):
    """A corrupted .git pointer: the finish records that git could not read the tree, and a
    file list from an earlier finish of the same tree (a resume) is not this tree's truth."""
    store, j = _job(repo)
    j.worktree["files"], j.worktree["files_n"] = ["stale.txt"], 1
    (Path(j.worktree["root"]) / ".git").write_text("this is not a gitdir pointer\n")
    for s in _finish(store, j):
        assert s["diff_files"] is None
        assert "files_n" not in s["worktree"]


def test_a_tree_removed_under_the_job_is_null_not_empty(repo):
    import shutil

    store, j = _job(repo)
    j.worktree["files"], j.worktree["files_n"] = ["stale.txt"], 1
    shutil.rmtree(j.worktree["root"])
    for s in _finish(store, j):
        assert s["diff_files"] is None


def test_a_failed_name_list_is_null_and_named(repo, monkeypatch):
    """The name-only read failing leaves the list absent (null), says so in the note and the
    transcript, keeps the diffstat, and the reset still runs (nothing left staged)."""
    from localagents import runner

    store, j = _job(repo)
    wt = Path(j.worktree["path"])
    (wt / "n.txt").write_text("n\n")
    monkeypatch.setattr(runner, "_diff_names", lambda root, base: None)
    for s in _finish(store, j):
        assert s["diff_files"] is None
        assert "no file list was taken" in s["worktree"]["note"]
        assert "n.txt" in s["worktree"]["diffstat"]
    assert git(wt, "diff", "--cached", "--name-only") == ""  # the reset ran
    assert "no file list was taken" in j.transcript_tail()


def test_a_non_utf8_name_cannot_strand_the_index(repo):
    """``-z`` prints names unquoted: an undecodable one is replaced, not raised, so the finish
    completes (kept, note) and the reset after the add still runs."""
    import os

    store, j = _job(repo)
    wt = Path(j.worktree["path"])
    with open(os.path.join(os.fsencode(wt), b"caf\xe9.txt"), "wb") as f:
        f.write(b"x\n")
    for s in _finish(store, j):
        assert s["worktree"]["kept"] is True and s["worktree"]["files_n"] == 1
        assert s["diff_files"] == ["caf�.txt"]
    assert git(wt, "diff", "--cached", "--name-only") == ""


def test_files_git_ignores_are_outside_the_list(repo):
    """The list is the diff's (``git add -A``): a file the repo ignores is not in it. This pins
    the boundary the tool text and docs describe, so a reader knows t/ scratch is not listed."""
    (repo / ".gitignore").write_text("t/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore t/")
    store, j = _job(repo)
    _shell_write(j, "t/REVIEW.md", j.worktree["path"])
    a, b = _finish(store, j)
    assert a["diff_files"] == [] and b["diff_files"] == []


def test_a_stored_file_list_that_is_not_a_list_reads_as_unknown():
    assert derive.diff_files({"files": "a.txt"}) is None
    assert derive.diff_files({"files": None}) is None
    assert derive.diff_files("not a dict") is None
    assert derive.diff_files(None) is None
    assert derive.diff_files({"files": []}) == []


def test_files_touched_leaves_out_a_failed_edit_and_keeps_a_pending_one(repo):
    """An edit call whose result was an error wrote nothing (the store's failed-only paths are
    mostly mistyped ones that never existed); a call with no result yet is still in flight."""
    store, j = _job(repo, isolation="none")
    t = time.time()
    j.record_tool_call(t, "Write", "u1", "/home/someone/projcet/typo.md")
    j.record_tool_result("u1", t + 1, True)
    j.record_tool_call(t + 2, "Edit", "u2", "/r/a.py")
    j.record_tool_result("u2", t + 3, False)
    j.record_tool_call(t + 4, "Edit", "u3", "/r/b.py")  # no result yet
    j.record_tool_call(t + 5, "Edit", "u4", "/r/c.py")
    j.record_tool_result("u4", t + 6, True)
    j.record_tool_call(t + 7, "Edit", "u5", "/r/c.py")  # the retry worked
    j.record_tool_result("u5", t + 8, False)
    assert derive.files_touched(store.conn, j.id) == ["/r/a.py", "/r/b.py", "/r/c.py"]


def test_run_job_hands_back_the_list_through_every_reader(repo, monkeypatch):
    """Through run_job: the session's shell write is in the settled record the wait returns,
    in job_status for this server's job, and in the row another server reads."""
    import asyncio

    from localagents import runner, server
    from localagents.registry import Defaults, Endpoint, Registry, Resolution

    async def no_metrics(*a, **kw):
        return None

    def writing_query(prompt, options):
        async def session():
            (Path(options.cwd) / "out.md").write_text("written by the session's shell\n")
            return
            yield

        return session()

    monkeypatch.setattr(runner, "fetch_metrics", no_metrics)
    monkeypatch.setattr(runner, "query", writing_query)
    ep = Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")
    reg = Registry(path=None, endpoints={"e": ep}, defaults=Defaults(job_retention_days=0), raw={}, local={})
    store = JobStore()
    monkeypatch.setattr(server, "store", store)
    j = store.new(task="t", model="served-model", endpoint="e", base_url="http://e:8080", cwd=str(repo),
                  run_cwd=str(repo), isolation="worktree", backend="llama.cpp", host="e")

    async def go():
        task = asyncio.create_task(runner.run_job(
            j, reg, Resolution(ep, "served-model"), permission_mode=None, max_turns=None,
            allowed_tools=None, system_prompt_append=None, resume_session=None, timeout_s=10,
            shim_base_url="http://127.0.0.1:1/job/x"))
        got = await store.wait(j.id, 30)
        await task
        return got.summary(), await server.job_status(j.id)

    waited, status = asyncio.run(go())
    row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (j.id,)).fetchone()
    foreign = derive.summary_from_row(store.conn, row)
    for s in (waited, status, foreign):
        assert s["diff_files"] == ["out.md"] and s["files_touched"] == []


def test_the_tool_text_says_what_each_field_is():
    """The instructions and job_status point at diff_files and say files_touched misses shell
    writes; both stay under Claude Code's 2048-character cap (test_server pins the cap)."""
    from localagents import server

    assert "`diff_files`" in server.INSTRUCTIONS and "misses shell writes" in server.INSTRUCTIONS
    desc = {t.name: t.description or "" for t in server.mcp._tool_manager.list_tools()}
    assert "`diff_files`" in desc["job_status"] and "never sees a file written through the shell" in desc["job_status"]
