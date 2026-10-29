"""Story 1.2: the jobs write path into SQLite (mcp_servers row at store start, jobs row at new(),
UPDATEs from changed()). In-memory Job fields and the status file keep working unchanged."""

import dataclasses
import os
import re
import sqlite3
import time
from contextlib import contextmanager

import pytest

from localagents import db, jobs


def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return jobs.JobStore()


def _new(store, **kw):
    kw.setdefault("task", "the brief")
    kw.setdefault("model", "served-model")
    kw.setdefault("endpoint", "llamacpp")
    kw.setdefault("base_url", "http://x:8080")
    kw.setdefault("backend", "llama.cpp")
    kw.setdefault("host", "local")
    kw.setdefault("cwd", "/tmp")
    kw.setdefault("run_cwd", "/tmp")
    kw.setdefault("isolation", "none")
    return store.new(**kw)


def test_new_job_inserts_row(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store, backend="llama.cpp", host="dgx1")
    row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert row is not None, "new() wrote no jobs row"
    assert row["id"] == job.id and row["name"] == job.name
    assert row["status"] == "queued" and row["task"] == "the brief"
    assert row["cwd"] == "/tmp" and row["isolation"] == "none"
    assert row["mcp_server_id"] == store.server_id
    assert row["created_at"] == pytest.approx(job.created_at)
    assert row["updated"] is not None and row["updated"] >= row["created_at"]
    ep = store.conn.execute(
        "SELECT name, base_url, backend, host FROM endpoints WHERE id = ?", (row["endpoint_id"],)
    ).fetchone()
    assert tuple(ep) == ("llamacpp", "http://x:8080", "llama.cpp", "dgx1")
    m = store.conn.execute("SELECT served_id FROM models WHERE id = ?", (row["model_id"],)).fetchone()
    assert m[0] == "served-model"


def test_new_leaves_no_orphan_directory_when_the_database_refuses(monkeypatch, tmp_path):
    """Story 1.7 item 5: new() mints the job directory before the INSERT. If the row cannot be
    written the call fails loudly — but cleanly: no directory is left, and the store keeps no
    job it never recorded."""
    store = _store(monkeypatch, tmp_path)

    def boom(conn, table, **cols):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(jobs.db, "lookup_id", boom)
    with pytest.raises(sqlite3.OperationalError):
        _new(store)
    assert list((jobs.state_dir() / "jobs").iterdir()) == []
    assert store.jobs == {}


def test_new_leaves_no_directory_when_the_first_open_fails(monkeypatch, tmp_path):
    """Review of 1.7, finding 7: new() made the job directory before the store's lazy open, so a
    database that could not be opened on first use (the startup writes swallow that) left an
    orphan directory. The open comes first now."""
    store = _store(monkeypatch, tmp_path)

    def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(jobs.db, "open_db", locked)
    with pytest.raises(sqlite3.OperationalError):
        _new(store)
    jobs_dir = jobs.state_dir() / "jobs"
    assert not jobs_dir.exists() or list(jobs_dir.iterdir()) == []
    assert store.jobs == {}


def test_new_cleans_up_on_any_error_not_only_sqlite(monkeypatch, tmp_path):
    """An unserialisable worktree raises TypeError inside the INSERT's arguments: no directory,
    no in-memory job, and the error re-raised."""
    store = _store(monkeypatch, tmp_path)
    with pytest.raises(TypeError):
        _new(store, isolation="worktree", worktree={"x": object()})
    assert list((jobs.state_dir() / "jobs").iterdir()) == []
    assert store.jobs == {}
    assert not store.conn.in_transaction


def test_store_does_not_open_the_database_until_used(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    assert not db.db_path().exists(), "constructing a JobStore must not create the database"
    store.conn  # first use
    assert db.db_path().exists()


def test_new_row_carries_the_fields_known_at_creation(monkeypatch, tmp_path):
    """DeepSeek review of 1.2, finding 2: context, num_turns and last_activity were NULL until
    the first changed(); a queued or lost job showed no context window."""
    store = _store(monkeypatch, tmp_path)
    job = store.new(task="t", model="m", endpoint="e", base_url="http://x", backend="llama.cpp", host="local",
                    cwd="/tmp", run_cwd="/tmp", isolation="none", context=131072)
    row = store.conn.execute("SELECT context, num_turns, last_activity FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert tuple(row) == (131072, 0, "")


def test_lost_counts_as_done():
    """1.5a writes status='lost'; a waiter must not block on it (DeepSeek review of 1.2)."""
    j = jobs.Job(id="x", name="n", task="t", model="m", endpoint="e", base_url="b", cwd="c", run_cwd="c",
                 isolation="none", status="lost")
    assert j.done


def test_mcp_servers_row_at_first_use(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    rows = store.conn.execute("SELECT * FROM mcp_servers").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == store.server_id
    assert r["pid"] == os.getpid()
    # owner: the Claude Code session pid (the db.session_pid walk), the parent pid when no
    # claude ancestor is above us (story 1.9 item 9)
    assert r["owner"] == (db.session_pid() or os.getppid())
    assert r["version"], "version must be non-empty (importlib.metadata or 'dev')"
    assert r["cwd"] == os.getcwd() and r["started"] <= time.time()
    # story 1.5a fills these; they must exist as columns and start NULL
    for col in ("config", "transport", "shim_port", "stopped"):
        assert r[col] is None
    # Liveness: a row with no heartbeat yet counts as live for its first 60 s, so _open sets
    # updated = started (a NULL updated would make the row dead the moment it is written).
    assert r["updated"] == r["started"]


def test_directory_and_branch_use_name(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    assert re.match(r"^\d{8}-\d{6}-[0-9a-f]{8}$", job.name), job.name
    assert job.id != job.name
    assert job.log_dir == str(jobs.state_dir() / "jobs" / job.name)
    assert not (jobs.state_dir() / "jobs" / job.name / "task.txt").exists()


FULL_METRICS = {
    "generated_tokens": 100, "prompt_tokens_processed": 500, "prompt_tokens_cached": 900,
    "cache_hit_ratio": 0.64, "prompt_tps": 110.5, "generate_tps": 42.0, "spec_decode_acceptance": 0.8,
}


def _row(store, jid):
    return store.conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()


def test_changed_tracks_status_transitions(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    u0 = _row(store, job.id)["updated"]

    job.status, job.started_at = "running", time.time()
    job.changed()
    r = _row(store, job.id)
    assert r["status"] == "running" and r["started_at"] == pytest.approx(job.started_at)
    assert r["finished_at"] is None and r["result"] is None and r["generated_tokens"] is None
    u1 = r["updated"]
    assert u1 > u0

    job.status, job.finished_at = "succeeded", time.time()
    job.result, job.num_turns, job.metrics = "done", 3, dict(FULL_METRICS)
    job.changed()
    r = _row(store, job.id)
    assert r["status"] == "succeeded" and r["finished_at"] == pytest.approx(job.finished_at)
    assert r["result"] == "done" and r["num_turns"] == 3
    assert r["updated"] > u1
    assert r["generated_tokens"] == 100 and r["prompt_tokens_processed"] == 500 and r["prompt_tokens_cached"] == 900
    assert r["cache_hit_ratio"] == pytest.approx(0.64) and r["prompt_tps"] == pytest.approx(110.5)
    assert r["generate_tps"] == pytest.approx(42.0) and r["spec_decode_acceptance"] == pytest.approx(0.8)


def test_negative_metrics_store_null(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    job.metrics = dict(FULL_METRICS, generated_tokens=-5)  # counter went backwards: backend restarted
    job.changed()
    r = _row(store, job.id)
    cols = ("generated_tokens", "prompt_tokens_processed", "prompt_tokens_cached",
            "cache_hit_ratio", "prompt_tps", "generate_tps", "spec_decode_acceptance")
    assert all(r[c] is None for c in cols)
    # a job that never had metrics leaves them NULL as well
    job2 = _new(store)
    assert all(_row(store, job2.id)[c] is None for c in cols)


def test_db_error_does_not_break_changed(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)

    @contextmanager
    def boom(conn):
        raise sqlite3.OperationalError("database is locked")
        yield  # unreachable; makes this a context manager

    monkeypatch.setattr(jobs.db, "immediate", boom)
    job.status = "running"
    job.changed()  # must return normally despite the database
    assert "[db-error]" in job.transcript_tail()


def test_lookup_rows_reused_across_jobs(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    a = _new(store)
    b = _new(store)
    assert a.endpoint_id == b.endpoint_id and a.model_id == b.model_id
    assert store.conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 1


# --- story 1.5b: the in-memory duplicates are gone; summary() reads them from the rows ---


def test_summary_reads_the_duplicated_values_from_rows(monkeypatch, tmp_path):
    """usage, tool_calls, tool_counts, errors, last_tool, files_touched, ctx_tokens, gen_tps
    are derived from the rows at read time; the in-memory fields and note_usage are gone."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    t = time.time()
    job.record_request({
        "t": t, "path": "/v1/messages", "status": 200, "ms": 1000,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90},
    })
    job.record_tool_call(t, "Edit", "u1", "/a.py")
    job.record_tool_result("u1", t + 1, True)
    s = job.summary()
    assert s["usage"] == {
        "input_tokens": 10, "output_tokens": 5,
        "cache_read_input_tokens": 90, "cache_creation_input_tokens": 0,
    }
    assert s["tool_calls"] == 1 and s["errors"] == 1
    assert s["tool_counts"] == {"Edit": {"calls": 1, "errors": 1}}
    assert s["last_tool"] == "Edit: /a.py"
    assert s["files_touched"] == ["/a.py"]
    assert s["ctx_tokens"] == 100 and s["gen_tps"] == pytest.approx(5.0)
    gone = ("usage", "tool_calls", "files_touched", "ctx_tokens", "gen_tps")
    assert all(name not in {f.name for f in dataclasses.fields(job)} for name in gone), gone
    assert not hasattr(job, "note_usage")


def test_summary_of_a_job_outside_the_store_has_empty_derived_fields():
    j = jobs.Job(id="x", name="n", task="t", model="m", endpoint="e", base_url="b", cwd="c",
                 run_cwd="c", isolation="none")
    s = j.summary()
    assert s["usage"] is None
    assert s["tool_calls"] == 0 and s["tool_counts"] == {} and s["errors"] == 0
    assert s["last_tool"] is None and s["files_touched"] == []
    assert s["ctx_tokens"] is None and s["gen_tps"] is None


# --- story 1.5a: server-row lifecycle (heartbeat, describe, close) and the startup lost rule ---


def _srv_row(store, sid=None):
    return store.conn.execute(
        "SELECT * FROM mcp_servers WHERE id = ?", (sid or store.server_id,)
    ).fetchone()


def test_heartbeat_advances_updated(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    u0 = _srv_row(store)["updated"]
    time.sleep(0.01)  # REAL clocks: two time.time() calls a beat apart must differ
    store.heartbeat()
    assert _srv_row(store)["updated"] > u0
    assert _srv_row(store)["stopped"] is None  # heartbeating must not touch stopped


def test_describe_server_updates_only_given_keys(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.describe_server(config="/cfg/models.yaml", transport="stdio")
    r = _srv_row(store)
    assert r["config"] == "/cfg/models.yaml" and r["transport"] == "stdio"
    assert r["shim_port"] is None  # not given: untouched
    store.describe_server(shim_port=43210)
    r = _srv_row(store)
    assert r["shim_port"] == 43210 and r["config"] == "/cfg/models.yaml" and r["transport"] == "stdio"
    with pytest.raises(ValueError):
        store.describe_server(pid=1)  # not a describable column: never interpolated into SQL


def test_close_sets_stopped_and_is_idempotent(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.conn  # opening is what writes the row; close() must not create one if the store never used the db
    store.close()
    r = _srv_row(store)
    assert r["stopped"] is not None and r["stopped"] >= r["started"]
    first = r["stopped"]
    time.sleep(0.01)
    store.close()  # idempotent: a second close must not move the first exit stamp
    assert _srv_row(store)["stopped"] == first


def test_close_on_an_unused_store_is_a_noop(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.close()  # never opened the database: nothing to mark, and it must not create a file
    assert not db.db_path().exists()


def test_close_never_raises(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.conn

    @contextmanager
    def boom(conn):
        raise sqlite3.OperationalError("database is locked")
        yield  # unreachable; makes this a context manager

    monkeypatch.setattr(jobs.db, "immediate", boom)
    store.close()  # runs at exit: must swallow, not raise


# A pid that does not exist (story 1.7 item 2: the sweep needs a *dead* pid, and the tests must
# prove it). pid_max itself is never assigned (pids run 1 .. pid_max - 1); 2**22 - 1 was a legal
# pid on a box whose pid_max is 2**22 (review of 1.7, finding 10). Linux-only, like the suite's
# /proc reads. Asserted dead in test_mark_lost_jobs_sweeps_a_dead_pid. Read in the tests that use
# it, not at import: on a host without /proc only those tests skip, collection does not fail
# (DeepSeek review of 6005f94).
def _dead_pid() -> int:
    try:
        with open("/proc/sys/kernel/pid_max") as f:
            return int(f.read())
    except OSError:
        pytest.skip("needs Linux's /proc/sys/kernel/pid_max for a provably dead pid")


def _stale_store(monkeypatch, tmp_path, now):
    """A second server whose heartbeat is 61 s old *and* whose pid is dead (row in the same
    database). The story-1.7 sweep is heartbeat first, pid second, like the status line: a stale
    heartbeat with a live pid is a stalled server, not a dead one."""
    other = jobs.JobStore()
    other.conn.execute(
        "UPDATE mcp_servers SET updated = ?, pid = ? WHERE id = ?",
        (now - 61, _dead_pid(), other.server_id),
    )
    return other


def test_mark_lost_jobs_marks_other_servers_not_this_one(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    mine = _new(store)
    mine.status, mine.started_at = "running", time.time()
    mine.changed()

    now = time.time()
    other = _stale_store(monkeypatch, tmp_path, now)
    stale_run = _new(other)
    stale_run.status, stale_run.started_at = "running", now - 100
    stale_run.changed()
    stale_queued = _new(other)  # queued needs no changed(): new() inserted it queued
    finished = _new(other)
    finished.status, finished.finished_at = "succeeded", now - 5
    finished.changed()

    assert store.mark_lost_jobs(now) == 2  # running + queued; the finished one is left alone
    for j in (stale_run, stale_queued):
        r = _row(store, j.id)
        assert r["status"] == "lost" and r["error"] == "server exited"
        assert r["finished_at"] == pytest.approx(now) and r["updated"] == pytest.approx(now)
    assert _row(store, finished.id)["status"] == "succeeded"
    assert _row(store, mine.id)["status"] == "running"  # this server's row is fresh: never touched
    # a second call finds nothing left to mark
    assert store.mark_lost_jobs(now + 10) == 0


def test_mark_lost_jobs_marks_a_server_with_no_heartbeat_at_all(monkeypatch, tmp_path):
    """A row written before heartbeats existed has updated NULL; the spec's 'no heartbeat for
    60 s' covers it (flagged by the 1.5a lane; the brief's SQL missed it)."""
    mine = _store(monkeypatch, tmp_path)
    other = jobs.JobStore()
    j = other.new(task="t", model="m", endpoint="e", base_url="http://x", backend="other", host="local",
                  cwd="/tmp", run_cwd="/tmp", isolation="none")
    j.status = "running"
    j.changed()
    other.conn.execute("UPDATE mcp_servers SET updated = NULL, pid = ? WHERE id = ?", (_dead_pid(), other.server_id))
    other.conn.commit()
    assert mine.mark_lost_jobs() == 1
    assert mine.conn.execute("SELECT status, error FROM jobs WHERE id = ?", (j.id,)).fetchone()[0] == "lost"


def test_mark_lost_jobs_spares_a_stale_heartbeat_with_a_live_pid(monkeypatch, tmp_path):
    """Story 1.7 item 2: heartbeat first, pid second, like status.collect. A server that stopped
    beating 61 s ago but whose pid is this process is stalled, not dead: its running job is not
    swept (the spec hides it from the status line, but does not bury it)."""
    store = _store(monkeypatch, tmp_path)
    now = time.time()
    stalled = jobs.JobStore()  # pid = this process: alive
    job = _new(stalled)
    job.status, job.started_at = "running", now - 100
    job.changed()
    stalled.conn.execute("UPDATE mcp_servers SET updated = ? WHERE id = ?", (now - 61, stalled.server_id))
    assert store.mark_lost_jobs(now) == 0
    assert _row(store, job.id)["status"] == "running"
    # and it is swept the moment the pid is gone
    stalled.conn.execute("UPDATE mcp_servers SET pid = ? WHERE id = ?", (_dead_pid(), stalled.server_id))
    assert store.mark_lost_jobs(now + 1) == 1
    assert _row(store, job.id)["status"] == "lost"


def _stale_running_job(pid, now):
    """Another server, heartbeat 61 s old, row pid `pid`, one running job. Returns the job."""
    other = jobs.JobStore()
    job = _new(other)
    job.status, job.started_at = "running", now - 100
    job.changed()
    other.conn.execute("UPDATE mcp_servers SET updated = ?, pid = ? WHERE id = ?", (now - 61, pid, other.server_id))
    return job


def test_mark_lost_jobs_sweeps_a_stale_pid_owned_by_another_user(monkeypatch, tmp_path):
    """Review of 1.7, finding 5: os.kill raising PermissionError means the pid is another user's
    process; this user's database is written only by this user's servers, so the server that
    wrote the row is gone and its pid was reused. The sweep reads that as dead; the status line
    keeps the lenient reading (its heartbeat check comes first)."""
    if os.geteuid() == 0:
        pytest.skip("root can signal every process: no PermissionError to construct")
    with pytest.raises(PermissionError):
        os.kill(1, 0)
    store = _store(monkeypatch, tmp_path)
    now = time.time()
    job = _stale_running_job(1, now)
    assert store.mark_lost_jobs(now) == 1
    assert _row(store, job.id)["status"] == "lost"
    assert db.pid_alive(1) is True and db.pid_alive(1, strict=True) is False


def test_mark_lost_jobs_survives_a_long_server_history(monkeypatch, tmp_path):
    """Review of 1.7, finding 6: mcp_servers rows are history and never deleted. The sweep built
    an IN list of every dead server id ever written, and past SQLite's 32766 variables every
    startup's sweep failed with "too many SQL variables". Candidates come from the servers that
    still have unfinished jobs."""
    store = _store(monkeypatch, tmp_path)
    now = time.time()
    dead = _dead_pid()
    with db.immediate(store.conn):
        store.conn.executemany(
            "INSERT INTO mcp_servers (id, pid, owner, started, updated, stopped, cwd, version)"
            " VALUES (?, ?, 1, ?, ?, ?, '/', 'x')",
            [(db.new_uuid(), dead, now - 1e5, now - 1e5, now - 1e5) for _ in range(40_000)],
        )
    job = _stale_running_job(dead, now)
    assert store.mark_lost_jobs(now) == 1
    assert _row(store, job.id)["status"] == "lost"


def test_mark_lost_jobs_sweeps_a_corrupt_pid(monkeypatch, tmp_path):
    """Review of 1.7, finding 9: a pid above a C int raised OverflowError out of the sweep, past
    _startup_writes' sqlite3.Error guard, and the server died at startup. Not a live process."""
    store = _store(monkeypatch, tmp_path)
    now = time.time()
    job = _stale_running_job(2**40, now)
    assert store.mark_lost_jobs(now) == 1
    assert _row(store, job.id)["status"] == "lost"


@pytest.mark.parametrize("pid", [0, -1])
def test_mark_lost_jobs_sweeps_a_non_positive_pid(monkeypatch, tmp_path, pid):
    """DeepSeek review of 6005f94: os.kill(0, 0) signals this process group and os.kill(-1, 0)
    every process this user may signal, so both succeed and pid_alive called a corrupt row's
    0 or -1 alive: its jobs were never swept. A pid <= 0 is not a live process, either reading."""
    assert db.pid_alive(pid) is False and db.pid_alive(pid, strict=True) is False
    store = _store(monkeypatch, tmp_path)
    now = time.time()
    job = _stale_running_job(pid, now)
    assert store.mark_lost_jobs(now) == 1
    assert _row(store, job.id)["status"] == "lost"


def test_mark_lost_jobs_sweeps_a_dead_pid(monkeypatch, tmp_path):
    """The dead pid the tests use (_dead_pid) is provably gone (os.kill raises ProcessLookupError), and a
    stale server row carrying it has its running job swept."""
    with pytest.raises(ProcessLookupError):
        os.kill(_dead_pid(), 0)
    store = _store(monkeypatch, tmp_path)
    now = time.time()
    job = _stale_running_job(_dead_pid(), now)
    assert store.mark_lost_jobs(now) == 1
    r = _row(store, job.id)
    assert r["status"] == "lost" and r["error"] == "server exited"


def test_mark_lost_jobs_marks_a_stopped_server_despite_a_fresh_heartbeat(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    now = time.time()
    exited = jobs.JobStore()  # clean exit: stopped set, heartbeat still recent
    job = _new(exited)
    job.status, job.started_at = "running", now - 10
    job.changed()
    exited.conn.execute("UPDATE mcp_servers SET stopped = ? WHERE id = ?", (now - 1, exited.server_id))
    assert store.mark_lost_jobs(now) == 1
    assert _row(store, job.id)["status"] == "lost" and _row(store, job.id)["error"] == "server exited"


def test_record_endpoint_load_writes_by_running_jobs_endpoint(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    store.record_endpoint_load({"llamacpp": "1/1 slots"})
    row = store.conn.execute("SELECT * FROM endpoint_load WHERE endpoint_id = ?", (job.endpoint_id,)).fetchone()
    assert row["load"] == "1/1 slots" and row["updated"] <= time.time()
    # a name with no running job here is skipped (the endpoint row would only be a guess by name)
    store.record_endpoint_load({"nosuch": "2 run 1 wait"})
    assert store.conn.execute("SELECT COUNT(*) FROM endpoint_load").fetchone()[0] == 1
    # last writer wins on the same endpoint
    store.record_endpoint_load({"llamacpp": "2/2 slots"})
    row = store.conn.execute("SELECT load FROM endpoint_load WHERE endpoint_id = ?", (job.endpoint_id,)).fetchone()
    assert row[0] == "2/2 slots"
    assert store.conn.execute("SELECT COUNT(*) FROM endpoint_load").fetchone()[0] == 1
    # a finished job no longer resolves its endpoint
    job.status, job.finished_at = "succeeded", time.time()
    job.changed()
    store.record_endpoint_load({"llamacpp": "0/2 slots"})
    assert store.conn.execute("SELECT load FROM endpoint_load").fetchone()[0] == "2/2 slots"
