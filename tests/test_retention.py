"""Job log retention: events.jsonl is opt-in debugging output, and old job directories are pruned."""

import os
import subprocess
import sys
import time
from pathlib import Path

from localagents import jobs
from localagents.registry import Defaults


def _job(log_events=False):
    return jobs.JobStore().new(
        task="t", model="m", endpoint="e", base_url="http://x:8080", backend="llama.cpp", host="local",
        cwd="/tmp", run_cwd="/tmp", isolation="none", log_events=log_events,
    )


def test_log_event_is_a_no_op_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    job = _job()
    job.log_event({"type": "AssistantMessage", "content": "x" * 1000})
    assert not (Path(job.log_dir) / "events.jsonl").exists()
    # the other logs are unaffected by the gate
    job.log_text("[assistant] hello")
    assert (Path(job.log_dir) / "transcript.txt").read_text() == "[assistant] hello\n"


def test_log_event_writes_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    job = _job(log_events=True)
    job.log_event({"type": "AssistantMessage"})
    assert (Path(job.log_dir) / "events.jsonl").read_text() == '{"type": "AssistantMessage"}\n'


def test_defaults_carry_retention_and_events_flags():
    d = Defaults()
    assert d.log_events is False
    assert d.job_retention_days == 7


DAY = 86400
UUID = "11111111-1111-4111-8111-111111111111"
UUID2 = "22222222-2222-4222-8222-222222222222"


def _mkjob(root, name):
    d = root / "localagents" / "jobs" / name
    d.mkdir(parents=True)
    (d / "task.txt").write_text("t")
    return d


def _age(d, seconds_ago, now):
    """Stamp the directory and every direct child as last written ``seconds_ago`` before ``now``:
    prune reads the newest mtime, so the dir and its files must agree for an old job."""
    t = now - seconds_ago
    for p in [d, *d.iterdir()]:
        os.utime(p, (t, t))


def test_prune_removes_job_dirs_by_newest_mtime(monkeypatch, tmp_path):
    """3.10f-1: the name is not a timestamp any more (the id is opaque), so the age is the
    newest mtime of the directory and its children."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = time.time()
    old = _mkjob(tmp_path, "20261021-115959-aaaa")
    _age(old, 8 * DAY, now)  # 7 days + 1 day: gone
    edge = _mkjob(tmp_path, "20261022-120001-bbbb")
    _age(edge, 6.9 * DAY, now)  # just inside 7 days: kept
    recent = _mkjob(tmp_path, UUID)
    _age(recent, DAY, now)
    removed = jobs.prune(days=7, now=now)
    assert removed == [old]
    assert not old.exists() and edge.exists() and recent.exists()


def test_prune_does_not_trust_the_old_stamp_any_more(monkeypatch, tmp_path):
    """An old-shape directory (what every pre-3.10f file leaves behind) ages by mtime too: a
    young one named for 2011 is kept, an old one named for today is pruned."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = time.time()
    young = _mkjob(tmp_path, "20110101-000000-abcd")
    _age(young, DAY, now)
    old = _mkjob(tmp_path, time.strftime("%Y%m%d-%H%M%S") + "-1234abcd")
    _age(old, 30 * DAY, now)
    removed = jobs.prune(days=7, now=now)
    assert removed == [old]
    assert young.exists() and not old.exists()


def test_prune_reads_the_newest_mtime_of_the_directory_and_its_children(monkeypatch, tmp_path):
    """A finished job whose directory itself is old but that wrote a child recently is kept: the
    write is the sign of life, and the newest mtime wins."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = time.time()
    old = _mkjob(tmp_path, UUID)
    _age(old, 10 * DAY, now)
    touched = _mkjob(tmp_path, UUID2)
    _age(touched, 10 * DAY, now)
    fresh = touched / "transcript.txt"
    fresh.write_text("written just now\n")  # a write after the aging: the directory is alive
    os.utime(fresh, None)
    removed = jobs.prune(days=7, now=now)
    assert removed == [old]
    assert not old.exists() and touched.exists() and fresh.exists()


def test_prune_leaves_anything_not_named_like_a_job(monkeypatch, tmp_path):
    """The name still decides whether the directory is a job's at all: a stray name and a file
    at a job's name are never touched, however old their mtimes."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = time.time()
    stray = _mkjob(tmp_path, "notes")
    almost = _mkjob(tmp_path, "20200101-000000-zzzz")  # bad hex suffix
    old_file = tmp_path / "localagents" / "jobs" / "20200101-000000-abcd"
    old_file.write_text("a file, not a directory")
    for p in (stray, almost, old_file):
        os.utime(p, (0, 0))
    assert jobs.prune(days=7, now=now) == []
    assert stray.exists() and almost.exists() and old_file.exists()


def test_prune_dry_run_lists_without_deleting(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = time.time()
    old = _mkjob(tmp_path, UUID)
    _age(old, 8 * DAY, now)
    assert jobs.prune(days=7, now=now, dry_run=True) == [old]
    assert old.exists()


def test_prune_disabled_when_days_is_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = time.time()
    old = _mkjob(tmp_path, UUID)
    _age(old, 100 * DAY, now)
    assert jobs.prune(days=0, now=now) == []
    assert old.exists()


def _cli(*args):
    return [sys.executable, "-c",
            f"import sys; sys.argv=['localagents', *{list(args)!r}]; import localagents; localagents.main(); "
            "print(sorted(m for m in sys.modules if m in ('mcp','httpx','uvicorn','localagents.server')))"]


def test_cli_prune_subcommand(tmp_path):
    # story 1.7 item 8: LOCALAGENTS_DB is pinned alongside XDG_STATE_HOME — an inherited
    # developer override would otherwise point the subprocess at the real database
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path), "LOCALAGENTS_DB": str(tmp_path / "localagents.db")}
    old = _mkjob(tmp_path, "20200101-000000-abcd")
    _age(old, 30 * DAY, time.time())
    keep = _mkjob(tmp_path, time.strftime("%Y%m%d-%H%M%S") + "-ffff")
    r = subprocess.run(_cli("prune", "--dry-run"), env=env, capture_output=True, text=True, check=True)
    assert str(old) in r.stdout and "dry run" in r.stdout and old.exists()
    r = subprocess.run(_cli("prune", "--days", "7"), env=env, capture_output=True, text=True, check=True)
    assert "localagents.server" not in r.stdout  # stays as light as `statusline`: no server imports
    assert str(old) in r.stdout and not old.exists() and keep.exists()
