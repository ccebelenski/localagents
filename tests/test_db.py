"""The SQLite schema module (docs/superpowers/specs/2026-10-29-mcp-sqlite-schema.md)."""

import calendar
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from localagents import db

TABLES = {"mcp_servers", "endpoints", "endpoint_load", "models", "paths", "jobs", "requests", "tool_calls"}
INDEXES = {
    "jobs_name", "jobs_mcp_server_id", "jobs_endpoint_id", "jobs_model_id", "jobs_status_finished_at",
    "jobs_created_at",
    "mcp_servers_updated", "requests_job_id_t", "tool_calls_job_id_t",
}
# every column of every table, in the spec's order
COLUMNS = {
    "mcp_servers": ["id", "pid", "owner", "cwd", "config", "version", "transport", "shim_port",
                    "started", "updated", "stopped"],
    "endpoints": ["id", "name", "base_url", "backend", "host", "first_seen"],
    "endpoint_load": ["endpoint_id", "updated", "load"],
    "models": ["id", "served_id"],
    "paths": ["id", "path"],
    "jobs": ["id", "name", "mcp_server_id", "endpoint_id", "model_id", "task", "cwd", "isolation", "status",
             "created_at", "started_at", "finished_at", "updated", "session_id", "result", "error",
             "last_activity", "num_turns", "context", "generated_tokens", "prompt_tokens_processed",
             "prompt_tokens_cached", "cache_hit_ratio", "prompt_tps", "generate_tps",
             "spec_decode_acceptance", "worktree", "dump", "log_events"],
    "requests": ["job_id", "path_id", "t", "http_status", "ms", "gen_ms", "stream", "input_tokens",
                 "output_tokens", "cache_read_tokens", "cache_creation_tokens", "messages", "tools", "bytes",
                 "moved_system", "folded_text", "context_exceeded", "error"],
    "tool_calls": ["job_id", "t", "finished", "tool", "tool_use_id", "input", "is_error"],
}


@pytest.fixture
def dbfile(monkeypatch, tmp_path):
    path = tmp_path / "test.db"
    monkeypatch.setenv("LOCALAGENTS_DB", str(path))
    return path


def _names(conn, kind):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,))
    return {r["name"] for r in rows}


def test_fresh_file_comes_up_at_version_1_with_every_table(dbfile):
    conn = db.open_db()
    assert dbfile.exists()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert _names(conn, "table") == TABLES
    assert _names(conn, "index") == INDEXES
    for table, cols in COLUMNS.items():
        assert [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")] == cols, table
    conn.close()


def test_opening_the_same_file_twice_is_idempotent(dbfile):
    db.open_db().close()
    conn = db.open_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert _names(conn, "table") == TABLES
    conn.close()


def test_a_newer_version_than_the_code_knows_raises(dbfile):
    raw = sqlite3.connect(dbfile)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    with pytest.raises(db.SchemaError, match="99"):
        db.open_db()


def test_journal_mode_is_wal(dbfile):
    conn = db.open_db()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_a_version_1_file_opens_without_rerunning_ddl(dbfile, monkeypatch):
    db.open_db().close()
    raw = sqlite3.connect(dbfile)
    raw.execute("PRAGMA user_version = 1")  # by hand, on the built file
    raw.close()

    def boom(conn):
        raise AssertionError("the version-1 DDL ran again")

    monkeypatch.setattr(db, "MIGRATIONS", [boom])
    conn = db.open_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert _names(conn, "table") == TABLES
    conn.close()


def test_foreign_keys_are_enforced(dbfile):
    conn = db.open_db()
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (id, mcp_server_id) VALUES ('j1', 'no-such-server')")
    conn.close()


def test_status_check_accepts_the_seven_values_and_nothing_else(dbfile):
    conn = db.open_db()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (id, status) VALUES ('bad', 'bogus')")
    for i, s in enumerate(["queued", "running", "succeeded", "failed", "cancelled", "timeout", "lost"]):
        conn.execute("INSERT INTO jobs (id, status) VALUES (?, ?)", (f"j{i}", s))
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 7
    conn.close()


def test_isolation_check_accepts_none_and_worktree_only(dbfile):
    conn = db.open_db()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (id, isolation) VALUES ('bad', 'bogus')")
    conn.execute("INSERT INTO jobs (id, isolation) VALUES ('a', 'none')")
    conn.execute("INSERT INTO jobs (id, isolation) VALUES ('b', 'worktree')")
    conn.close()


def test_db_path_honours_localagents_db_over_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("LOCALAGENTS_DB", raising=False)
    assert db.db_path() == tmp_path / "state" / "localagents" / "localagents.db"
    monkeypatch.setenv("LOCALAGENTS_DB", str(tmp_path / "elsewhere.db"))
    assert db.db_path() == tmp_path / "elsewhere.db"
    conn = db.open_db()
    conn.close()
    assert (tmp_path / "elsewhere.db").exists()
    assert not (tmp_path / "state" / "localagents" / "localagents.db").exists()


EP = dict(name="llamacpp", base_url="http://dgx3:8080", backend="llama.cpp", host="dgx3")


def test_lookup_id_is_stable_and_keyed_by_every_column(dbfile):
    conn = db.open_db()
    ep = db.lookup_id(conn, "endpoints", **EP)
    assert db.lookup_id(conn, "endpoints", **EP) == ep
    for col in EP:
        assert db.lookup_id(conn, "endpoints", **{**EP, col: EP[col] + "-x"}) != ep, col
    assert conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0] == 5
    # first_seen is written on first sight and is not part of the key
    first_seen = conn.execute("SELECT first_seen FROM endpoints WHERE id = ?", (ep,)).fetchone()[0]
    assert isinstance(first_seen, float)
    db.lookup_id(conn, "endpoints", **EP)
    assert conn.execute("SELECT first_seen FROM endpoints WHERE id = ?", (ep,)).fetchone()[0] == first_seen

    m = db.lookup_id(conn, "models", served_id="Qwen3.8-Flash-Next")
    assert db.lookup_id(conn, "models", served_id="Qwen3.8-Flash-Next") == m
    assert db.lookup_id(conn, "models", served_id="Muse-Glimmer-30B") != m

    p = db.lookup_id(conn, "paths", path="/v1/messages")
    assert db.lookup_id(conn, "paths", path="/v1/messages") == p
    assert db.lookup_id(conn, "paths", path="/v1/messages/count_tokens") != p
    conn.close()


def test_lookup_id_refuses_a_partial_key_an_unknown_table_and_a_null(dbfile):
    conn = db.open_db()
    with pytest.raises(ValueError):
        db.lookup_id(conn, "endpoints", name="llamacpp")  # the key is all four columns
    with pytest.raises(ValueError):
        db.lookup_id(conn, "jobs", id="x")
    with pytest.raises(ValueError):
        db.lookup_id(conn, "endpoints", **{**EP, "host": None})  # NULLs never deduplicate under UNIQUE
    assert conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0] == 0
    conn.close()


def test_new_uuid_is_uuid4_text():
    a, b = db.new_uuid(), db.new_uuid()
    assert a != b
    assert str(uuid.UUID(a)) == a and uuid.UUID(a).version == 4


@pytest.fixture
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


_WRITER = r"""
import os, sys, time
from pathlib import Path
from localagents import db

path, go = Path(sys.argv[1]), Path(sys.argv[2])
deadline = time.time() + 30
while not go.exists():  # start together, so the fresh-file migrate is contended too
    if time.time() > deadline:
        sys.exit("no go file")
    time.sleep(0.002)
conn = db.open_db(path)
sid = db.new_uuid()
now = time.time()
with db.immediate(conn):
    conn.execute("INSERT INTO mcp_servers (id, pid, started, updated) VALUES (?, ?, ?, ?)", (sid, os.getpid(), now, now))
for i in range(100):
    p = db.lookup_id(conn, "paths", path="/v1/messages")  # autocommit: no surrounding transaction
    with db.immediate(conn):
        ep = db.lookup_id(conn, "endpoints", name="llamacpp", base_url="http://dgx3:8080", backend="llama.cpp", host="dgx3")
        m = db.lookup_id(conn, "models", served_id="Qwen3.8-Flash-Next")
        jid = db.new_uuid()
        t = time.time()
        conn.execute(
            "INSERT INTO jobs (id, name, mcp_server_id, endpoint_id, model_id, status, created_at, updated) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
            (jid, db.job_name(t, jid), sid, ep, m, t, t),
        )
        conn.execute("INSERT INTO requests (job_id, path_id, t) VALUES (?, ?, ?)", (jid, p, t))
conn.close()
"""


def test_four_processes_writing_at_once_lose_nothing(tmp_path, monkeypatch):
    """Real processes, not threads: the write rules are about separate connections in separate processes."""
    path, go = tmp_path / "shared.db", tmp_path / "go"
    monkeypatch.setenv("LOCALAGENTS_DB", str(path))
    src = Path(__file__).resolve().parent.parent / "src"
    procs = [
        subprocess.Popen([sys.executable, "-c", _WRITER, str(path), str(go)], cwd=src,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    go.touch()
    results = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0, 0], [err for _, err in results]

    conn = db.open_db(path)
    count = lambda table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: E731
    assert count("mcp_servers") == 4
    assert count("jobs") == 400
    assert count("requests") == 400
    assert (count("endpoints"), count("models"), count("paths")) == (1, 1, 1)
    per_server = conn.execute("SELECT COUNT(*) FROM jobs GROUP BY mcp_server_id").fetchall()
    assert sorted(r[0] for r in per_server) == [100, 100, 100, 100]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


_RACER = r"""
import sys, time
from pathlib import Path
from localagents import db

go, mode, paths = Path(sys.argv[1]), sys.argv[2], [Path(p) for p in sys.argv[3:]]
deadline = time.time() + 30
while not go.exists():
    if time.time() > deadline:
        sys.exit("no go file")
    time.sleep(0.002)
t0 = float(go.read_text())
for k, path in enumerate(paths):
    while time.time() < t0 + k * 0.04:  # every file's opens start together, not only the first
        time.sleep(0.0005)
    conn = db.open_db(path)
    if mode == "lookups":
        for i in range(200):  # 200 first sightings raced by every process, no surrounding transaction
            db.lookup_id(conn, "paths", path=f"/p/{i}")
    conn.close()
"""


def _race(tmp_path, n, mode, files):
    go = tmp_path / "go"
    src = Path(__file__).resolve().parent.parent / "src"
    procs = [
        subprocess.Popen([sys.executable, "-c", _RACER, str(go), mode, *map(str, files)], cwd=src,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(n)
    ]
    tmp = tmp_path / "go.tmp"
    tmp.write_text(repr(time.time() + 0.5))  # start time, a moment ahead so every process is waiting
    tmp.rename(go)
    results = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0] * n, [err for _, err in results]


def test_processes_opening_fresh_files_at_once_migrate_each_exactly_once(tmp_path):
    files = [tmp_path / f"fresh{i}.db" for i in range(25)]
    _race(tmp_path, 8, "open", files)
    for f in files:
        conn = db.open_db(f)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert _names(conn, "table") == TABLES
        conn.close()


def test_processes_racing_first_sight_lookups_converge_on_one_row_each(tmp_path):
    path = tmp_path / "shared.db"
    db.open_db(path).close()
    _race(tmp_path, 4, "lookups", [path])
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*), COUNT(DISTINCT path) FROM paths").fetchone()[:] == (200, 200)
    conn.close()


def test_job_name_is_local_timestamp_and_first_8_hex(utc):
    created = calendar.timegm((2026, 10, 29, 3, 16, 17, 0, 0, 0)) + 0.75
    assert db.job_name(created, "adf7c0de-1234-4abc-8def-0123456789ab") == "20261029-031617-adf7c0de"


def _fake_proc(monkeypatch, files):
    """/proc files served from memory: session_pid's walk is tested against fixed data."""
    import io

    def fake_open(path, *a, **kw):
        if path not in files:
            raise FileNotFoundError(path)
        return io.StringIO(files[path])

    # raising=False: `open` is a builtin, not a module attribute; setting the module global
    # makes session_pid's unqualified open() resolve to the fake before builtins
    monkeypatch.setattr(db, "open", fake_open, raising=False)


def test_session_pid_walks_to_the_claude_ancestor(monkeypatch):
    """Story 1.9 item 9: the walk (moved from status.my_owner) finds the nearest ``claude``
    ancestor's pid: python -> uv -> claude answers 8, the claude pid."""
    _fake_proc(monkeypatch, {
        "/proc/100/stat": "100 (python) S 99 100 100 0 -1 4194304 1 0 0 0",
        "/proc/99/stat": "99 (uv) S 8 99 99 0 -1 4194304 1 0 0 0",
        "/proc/99/comm": "uv\n",
        "/proc/8/comm": "claude\n",
    })
    assert db.session_pid(start_pid=100) == 8


def test_session_pid_is_none_below_init_or_without_proc(monkeypatch):
    """No ``claude`` before the walk runs out (init reached, or /proc unreadable): None, the
    same answer the old my_owner gave."""
    _fake_proc(monkeypatch, {
        "/proc/100/stat": "100 (pytest) S 1 100 100 0 -1 4194304 1 0 0 0",
        "/proc/1/comm": "systemd\n",
    })
    assert db.session_pid(start_pid=100) is None
    _fake_proc(monkeypatch, {})
    assert db.session_pid(start_pid=12345) is None
