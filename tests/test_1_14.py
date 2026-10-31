"""Story 1.14: a build stamp on every MCP server row and job record, so a process running stale
code is visible (EPICS 1.14). ``db.build_stamp`` is ``<version>+<short sha>``; the server row's
``version`` carries it; ``job_status``/``list_jobs`` carry ``server_version``; the monitor
stamps itself (the snapshot's ``build``) and the job page says when the two differ."""

from __future__ import annotations

import importlib.metadata
import subprocess
from pathlib import Path

import pytest

from localagents import db


# --- step 1: db.build_stamp ---------------------------------------------------------------------


def _checkout(tmp_path: Path, version: str = "9.8.7", git: bool = True) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text(f'[project]\nname = "localagents"\nversion = "{version}"\n')
    if git:
        (root / ".git").write_text("gitdir: elsewhere\n")  # a worktree's .git is a file
    return root


class _FakeGit:
    """subprocess.run stand-in: records the argv and answers like `git rev-parse --short HEAD`."""

    def __init__(self, stdout: str = "8ce7d91\n", returncode: int = 0, raises: BaseException | None = None):
        self.stdout, self.returncode, self.raises, self.calls = stdout, returncode, raises, []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


def test_build_stamp_is_the_pyproject_version_plus_the_short_sha(monkeypatch, tmp_path):
    root = _checkout(tmp_path)
    git = _FakeGit()
    monkeypatch.setattr(subprocess, "run", git)
    assert db.build_stamp(root) == "9.8.7+8ce7d91"
    (argv, kw), = git.calls
    assert argv == ["git", "-C", str(root), "rev-parse", "--short", "HEAD"]
    assert kw["timeout"] == 2.0


def test_build_stamp_prefers_the_source_tree_over_stale_metadata(monkeypatch, tmp_path):
    """The operator's case: the dist-info said 0.1.0 while pyproject said 0.2.0 — the source
    tree is the code that runs, so its version wins; matching metadata is the same answer."""
    root = _checkout(tmp_path, version="0.2.0")
    monkeypatch.setattr(subprocess, "run", _FakeGit("abc1234\n"))
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    assert db.build_stamp(root) == "0.2.0+abc1234"


def test_build_stamp_without_a_source_tree_is_the_metadata_version(monkeypatch, tmp_path):
    """A wheel install: no pyproject.toml and no .git at the root — the metadata version alone,
    and git is never asked."""
    git = _FakeGit()
    monkeypatch.setattr(subprocess, "run", git)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.2.3")
    assert db.build_stamp(tmp_path / "nowhere") == "1.2.3"
    assert git.calls == []


def test_build_stamp_without_metadata_or_pyproject_is_dev(monkeypatch, tmp_path):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    monkeypatch.setattr(subprocess, "run", _FakeGit())
    assert db.build_stamp(tmp_path / "nowhere") == "dev"


@pytest.mark.parametrize("git", [
    _FakeGit(raises=subprocess.TimeoutExpired(["git"], 2.0)),
    _FakeGit(raises=FileNotFoundError("git")),
    _FakeGit(raises=PermissionError("git")),
    _FakeGit(stdout="", returncode=128),  # not a repository
    _FakeGit(stdout="fatal: bad\nthing", returncode=0),  # nonsense on stdout
    _FakeGit(stdout="", returncode=0),
], ids=["timeout", "no-git", "denied", "rc128", "garbage", "empty"])
def test_a_failing_git_leaves_the_version_alone(monkeypatch, tmp_path, git):
    root = _checkout(tmp_path)
    monkeypatch.setattr(subprocess, "run", git)
    assert db.build_stamp(root) == "9.8.7"


def test_build_stamp_never_raises(monkeypatch, tmp_path):
    """A pyproject that is not TOML, metadata that raises something odd: still a string."""
    root = _checkout(tmp_path)
    (root / "pyproject.toml").write_text("this is [not toml")

    def broken(name):
        raise RuntimeError("broken dist-info")

    monkeypatch.setattr(importlib.metadata, "version", broken)
    monkeypatch.setattr(subprocess, "run", _FakeGit("1234abc\n"))
    assert db.build_stamp(root) == "dev+1234abc"


def test_build_stamp_finds_this_checkout_by_default(monkeypatch):
    """No argument: the tree this module runs from (the suite runs from a checkout), so the
    version is pyproject's and the sha is the fake git's answer for that tree."""
    git = _FakeGit("feedbee\n")
    monkeypatch.setattr(subprocess, "run", git)
    root = Path(db.__file__).resolve().parents[2]
    version = db._pyproject_version(root)
    assert version is not None
    assert db.build_stamp() == f"{version}+feedbee"
    assert git.calls[0][0][2] == str(root)


def test_the_editable_checkout_is_read_from_direct_url(monkeypatch, tmp_path):
    class Dist:
        def __init__(self, text):
            self.text = text

        def read_text(self, name):
            assert name == "direct_url.json"
            return self.text

    url = (tmp_path / "a dir").as_uri()
    monkeypatch.setattr(importlib.metadata, "distribution",
                        lambda name: Dist('{"url": "%s", "dir_info": {"editable": true}}' % url))
    assert db._editable_checkout() == tmp_path / "a dir"
    monkeypatch.setattr(importlib.metadata, "distribution",
                        lambda name: Dist('{"url": "%s", "dir_info": {}}' % url))
    assert db._editable_checkout() is None
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: Dist(None))
    assert db._editable_checkout() is None


# --- step 2: the server row's version is the stamp; the job record carries server_version -----


def _new(store, **kw):
    for k, v in (("task", "the brief"), ("model", "served-model"), ("endpoint", "llamacpp"),
                 ("base_url", "http://x:8080"), ("cwd", "/tmp"), ("run_cwd", "/tmp"),
                 ("isolation", "none"), ("backend", "llama.cpp"), ("host", "local")):
        kw.setdefault(k, v)
    return store.new(**kw)


def _stamp(monkeypatch, value):
    monkeypatch.setattr(db, "build_stamp", lambda package_root=None: value)


def test_the_server_row_version_is_the_build_stamp(monkeypatch, tmp_path):
    from localagents import jobs

    expected = db.build_stamp()
    store = jobs.JobStore()
    (version,), = store.conn.execute("SELECT version FROM mcp_servers WHERE id = ?", (store.server_id,)).fetchall()
    assert version == expected
    _stamp(monkeypatch, "0.2.0+8ce7d91")
    other = jobs.JobStore()
    (version,), = other.conn.execute("SELECT version FROM mcp_servers WHERE id = ?", (other.server_id,)).fetchall()
    assert version == "0.2.0+8ce7d91"


def test_summary_and_summary_from_row_carry_the_same_server_version(monkeypatch, tmp_path):
    from localagents import derive, jobs

    _stamp(monkeypatch, "0.2.0+8ce7d91")
    store = jobs.JobStore()
    job = _new(store)
    job.status, job.started_at, job.finished_at = "succeeded", 1000.0, 1060.0
    job.changed()
    row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert job.summary()["server_version"] == "0.2.0+8ce7d91"
    assert job.summary()["server_version"] == derive.summary_from_row(store.conn, row)["server_version"]
    assert job.summary(include_result=False)["server_version"] == "0.2.0+8ce7d91"


def test_a_job_whose_server_row_is_gone_has_no_server_version(monkeypatch, tmp_path):
    from localagents import derive, jobs

    store = jobs.JobStore()
    job = _new(store)
    store.conn.execute("PRAGMA foreign_keys = OFF")
    store.conn.execute("DELETE FROM mcp_servers WHERE id = ?", (store.server_id,))
    row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert derive.summary_from_row(store.conn, row)["server_version"] is None
    assert job.summary()["server_version"] is None
    hand_built = jobs.Job(id="x", task="t", model="m", endpoint="e", base_url="b", cwd="c",
                          run_cwd="c", isolation="none")
    assert hand_built.summary()["server_version"] is None


def test_job_status_and_list_jobs_carry_each_servers_own_stamp(monkeypatch, tmp_path):
    """This server's job and another server's job, stamped differently: job_status and both
    list_jobs paths answer each job with the stamp of the server that ran it."""
    import asyncio

    from localagents import jobs, server

    _stamp(monkeypatch, "0.2.0+0000new")
    server.store = jobs.JobStore()
    mine = _new(server.store)
    _stamp(monkeypatch, "0.1.0+00stale")
    other = jobs.JobStore()
    foreign = _new(other, task="the stale server's brief")

    assert asyncio.run(server.job_status(mine.id))["server_version"] == "0.2.0+0000new"
    assert asyncio.run(server.job_status(foreign.id))["server_version"] == "0.1.0+00stale"
    assert [j["server_version"] for j in asyncio.run(server.list_jobs())] == ["0.2.0+0000new"]
    every = {j["id"]: j["server_version"] for j in asyncio.run(server.list_jobs(all=True))}
    assert every == {mine.id: "0.2.0+0000new", foreign.id: "0.1.0+00stale"}


# --- step 3: the monitor — the snapshot's build, the job rows' server_version, the page ---------


@pytest.fixture
def _empty_finished_cache():
    from localagents.monitor import read

    read._FINISHED_CACHE.clear()
    yield
    read._FINISHED_CACHE.clear()


def test_the_seeded_snapshot_carries_build_and_each_jobs_server_version(monkeypatch, tmp_path,
                                                                         _empty_finished_cache):
    """The seed writes its own server row with the stamp of the tree it runs from; every job
    row of the snapshot (live and finished, the finished one through the cache too) carries
    it, and ``build`` is what the caller gave."""
    from localagents.monitor import read
    from monitor_seeded import NOW, registry, seed_file

    _stamp(monkeypatch, "0.2.0+seeded1")
    path = tmp_path / "seeded.db"
    seed_file(path)
    conn = db.open_db(path)
    try:
        reg = registry(tmp_path)
        for _ in range(2):  # the second read serves the finished row from _FINISHED_CACHE
            ov = read.overview(conn, reg, NOW, 86400.0, build="0.2.0+monitor")
            assert ov["build"] == "0.2.0+monitor"
            assert [j["status"] for j in ov["jobs"]] == ["running", "succeeded"]
            assert [j["server_version"] for j in ov["jobs"]] == ["0.2.0+seeded1"] * 2
        assert read.overview(conn, reg, NOW, 86400.0)["build"] is None
    finally:
        conn.close()


def test_the_app_stamps_itself_once_and_every_snapshot_carries_it(monkeypatch, tmp_path,
                                                                    _empty_finished_cache):
    """make_app computes the monitor's stamp once (app.state.build); /api/snapshot carries it
    as ``build`` beside the jobs' own server_version, and the job detail route carries the
    job's server_version (the summary's key)."""
    import asyncio

    from starlette.testclient import TestClient

    from localagents.monitor import app as app_mod
    from localagents.monitor.config import MonitorSettings
    from monitor_seeded import NOW, registry, seed_file

    _stamp(monkeypatch, "0.1.0+oldsrvr")
    path = tmp_path / "seeded.db"
    seed_file(path)
    calls = []

    def stamp(package_root=None):
        calls.append(package_root)
        return "0.2.0+monitor"

    monkeypatch.setattr(db, "build_stamp", stamp)
    app = app_mod.make_app(lambda: db.open_db(path), registry(tmp_path), MonitorSettings(refresh_s=0.01),
                           asyncio.Event(), now=lambda: NOW, db_path=path)
    assert app.state.build == "0.2.0+monitor"
    with TestClient(app) as c:
        snaps = [c.get("/api/snapshot").json() for _ in range(3)]
        assert [s["build"] for s in snaps] == ["0.2.0+monitor"] * 3
        assert {j["server_version"] for s in snaps for j in s["jobs"]} == {"0.1.0+oldsrvr"}
        jid = snaps[0]["jobs"][0]["id"]
        assert c.get(f"/api/jobs/{jid}").json()["server_version"] == "0.1.0+oldsrvr"
        assert {j["server_version"] for j in c.get("/api/jobs").json()} == {"0.1.0+oldsrvr"}
    assert calls == [None]  # once, at the app's start


def test_job_js_reads_server_version_and_build_and_says_the_note():
    """The static pin (the DOM smoke checks the drawing): the header draws a "server" figure
    from detail.server_version, and the note compares snap.build with it; the comparison runs
    before the jobs[] lookup, so a job past the snapshot log's tail still compares."""
    import re

    js = (Path(db.__file__).resolve().parent / "monitor" / "static" / "job.js").read_text()
    assert 'fig("server", detail.server_version)' in js
    assert re.search(r"snap\.build !== detail\.server_version", js)
    assert "server code differs from the monitor's" in js
    apply_src = js[js.index("function applySnapshot("):js.index("stream = new EventSource")]
    assert apply_src.index("applyBuild(snap)") < apply_src.index("if (!job) return;")
    header_src = js[js.index("function header("):js.index("return function () {")]
    assert "serverFig()" in header_src
