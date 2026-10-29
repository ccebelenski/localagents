"""Story 1.2: the jobs write path into SQLite (mcp_servers row at store start, jobs row at new(),
UPDATEs from changed()). In-memory Job fields and the status file keep working unchanged."""

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


def test_store_does_not_open_the_database_until_used(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    assert not db.db_path().exists(), "constructing a JobStore must not create the database"
    store.conn  # first use
    assert db.db_path().exists()


def test_mcp_servers_row_at_first_use(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    rows = store.conn.execute("SELECT * FROM mcp_servers").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == store.server_id
    assert r["pid"] == os.getpid() and r["owner"] == os.getppid()
    assert r["version"], "version must be non-empty (importlib.metadata or 'dev')"
    assert r["cwd"] == os.getcwd() and r["started"] <= time.time()
    # story 1.5a fills these; they must exist as columns and start NULL
    for col in ("config", "transport", "shim_port", "updated", "stopped"):
        assert r[col] is None


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
