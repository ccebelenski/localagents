"""Job log retention: events.jsonl is opt-in debugging output, and old job directories are pruned."""

import os
import subprocess
import sys
import time

from localagents import jobs
from localagents.registry import Defaults


def _job(log_events=False):
    return jobs.JobStore().new(
        task="t", model="m", endpoint="e", base_url="http://x:8080", cwd="/tmp", run_cwd="/tmp",
        isolation="none", log_events=log_events,
    )


def test_log_event_is_a_no_op_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    job = _job()
    job.log_event({"type": "AssistantMessage", "content": "x" * 1000})
    assert not (tmp_path / "localagents" / "jobs" / job.id / "events.jsonl").exists()
    # the other logs are unaffected by the gate
    job.log_text("[assistant] hello")
    assert (tmp_path / "localagents" / "jobs" / job.id / "transcript.txt").read_text() == "[assistant] hello\n"


def test_log_event_writes_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    job = _job(log_events=True)
    job.log_event({"type": "AssistantMessage"})
    assert (tmp_path / "localagents" / "jobs" / job.id / "events.jsonl").read_text() == '{"type": "AssistantMessage"}\n'


def test_defaults_carry_retention_and_events_flags():
    d = Defaults()
    assert d.log_events is False
    assert d.job_retention_days == 7


DAY = 86400


def _mkjob(root, name):
    d = root / "localagents" / "jobs" / name
    d.mkdir(parents=True)
    (d / "task.txt").write_text("t")
    return d


def test_prune_removes_only_job_dirs_older_than_cutoff(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = time.mktime(time.strptime("20261029-120000", "%Y%m%d-%H%M%S"))
    old = _mkjob(tmp_path, "20261021-115959-aaaa")      # 7 days + 1 s: gone
    edge = _mkjob(tmp_path, "20261022-120001-bbbb")     # just inside 7 days: kept
    recent = _mkjob(tmp_path, "20261029-000340-cb65")
    removed = jobs.prune(days=7, now=now)
    assert removed == [old]
    assert not old.exists() and edge.exists() and recent.exists()


def test_prune_leaves_anything_not_named_like_a_job(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = time.mktime(time.strptime("20261029-120000", "%Y%m%d-%H%M%S"))
    stray = _mkjob(tmp_path, "notes")
    almost = _mkjob(tmp_path, "20200101-000000-zzzz")  # bad hex suffix
    old_file = tmp_path / "localagents" / "jobs" / "20200101-000000-abcd"
    old_file.write_text("a file, not a directory")
    os.utime(stray, (0, 0))  # ancient mtime must not matter: the name decides
    assert jobs.prune(days=7, now=now) == []
    assert stray.exists() and almost.exists() and old_file.exists()


def test_prune_dry_run_lists_without_deleting(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = time.mktime(time.strptime("20261029-120000", "%Y%m%d-%H%M%S"))
    old = _mkjob(tmp_path, "20260825-215114-5cdc")
    assert jobs.prune(days=7, now=now, dry_run=True) == [old]
    assert old.exists()


def test_prune_disabled_when_days_is_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    old = _mkjob(tmp_path, "20200101-000000-abcd")
    assert jobs.prune(days=0) == []
    assert old.exists()


def _cli(*args):
    return [sys.executable, "-c",
            f"import sys; sys.argv=['localagents', *{list(args)!r}]; import localagents; localagents.main(); "
            "print(sorted(m for m in sys.modules if m in ('mcp','httpx','uvicorn','localagents.server')))"]


def test_cli_prune_subcommand(tmp_path):
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path)}
    old = _mkjob(tmp_path, "20200101-000000-abcd")
    keep = _mkjob(tmp_path, time.strftime("%Y%m%d-%H%M%S") + "-ffff")
    r = subprocess.run(_cli("prune", "--dry-run"), env=env, capture_output=True, text=True, check=True)
    assert str(old) in r.stdout and "dry run" in r.stdout and old.exists()
    r = subprocess.run(_cli("prune", "--days", "7"), env=env, capture_output=True, text=True, check=True)
    assert "localagents.server" not in r.stdout  # stays as light as `statusline`: no server imports
    assert str(old) in r.stdout and not old.exists() and keep.exists()
