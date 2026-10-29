"""The SQLite schema module (docs/superpowers/specs/2026-10-29-mcp-sqlite-schema.md)."""

import calendar
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from localagents import db

TABLES = {"mcp_servers", "endpoints", "endpoint_load", "models", "paths", "jobs", "requests", "tool_calls"}
# version 3 (story 2.2): the monitor's tables, monitor-design spec §3 "Storage"
MONITOR_TABLES = {"probes", "probe_counters", "probe_hardware", "endpoint_events", "host_probes"}
INDEXES = {
    "jobs_name", "jobs_mcp_server_id", "jobs_endpoint_id", "jobs_model_id", "jobs_status_finished_at",
    "jobs_created_at",
    "mcp_servers_updated", "requests_job_id_t", "tool_calls_job_id_t",
}
MONITOR_INDEXES = {"probes_endpoint_t", "endpoint_events_endpoint_t", "host_probes_host_t"}
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
             "spec_decode_acceptance", "worktree", "dump", "log_events", "compactions"],
    "requests": ["job_id", "path_id", "t", "http_status", "ms", "gen_ms", "stream", "input_tokens",
                 "output_tokens", "cache_read_tokens", "cache_creation_tokens", "messages", "tools", "bytes",
                 "moved_system", "folded_text", "context_exceeded", "error"],
    "tool_calls": ["job_id", "t", "finished", "tool", "tool_use_id", "input", "is_error"],
}
# the monitor tables, every column in the spec's order (monitor-design §3, approved 2026-10-29)
MONITOR_COLUMNS = {
    # schema version 4 (story 3.1a, ruling A): probes.state appends after since,
    # probe_hardware's disk columns after ram_total
    "probes": ["id", "t", "endpoint_id", "model_id", "up", "host", "error", "latency_ms", "context",
               "slots_total", "slots_busy", "requests_running", "requests_waiting", "kv_cache_usage",
               "gen_tps", "since", "state"],
    "probe_counters": ["probe_id", "prompt_tokens", "prompt_tokens_cached", "prompt_seconds", "generated_tokens",
                       "generated_seconds", "spec_draft_tokens", "spec_accepted_tokens", "prefix_queries",
                       "prefix_hits", "preemptions"],
    "probe_hardware": ["probe_id", "gpu_util", "gpu_mem_used", "gpu_mem_total", "gpu_temp", "gpu_power",
                       "gpu_power_limit", "gpu_pcie_rx_mb", "gpu_pcie_tx_mb", "cpu", "ram_used", "ram_total",
                       "disk_read_mb", "disk_write_mb"],
    "endpoint_events": ["id", "t", "endpoint_id", "kind", "detail"],
    # schema version 5 (story 3.1b): the node fallback's answers, one row per port-22 ask
    "host_probes": ["id", "t", "host", "address", "answer"],
}


@pytest.fixture
def dbfile(monkeypatch, tmp_path):
    path = tmp_path / "test.db"
    monkeypatch.setenv("LOCALAGENTS_DB", str(path))
    return path


def _names(conn, kind):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,))
    return {r["name"] for r in rows}


def test_fresh_file_comes_up_at_the_current_version_with_every_table(dbfile):
    """A fresh file runs the version-1 DDL, the version-2 step (story 1.11: jobs.compactions),
    the version-3 step (story 2.2: the monitor's tables), the version-4 step (story 3.1a
    ruling A: probes.state, probe_hardware's disk columns) and the version-5 step (story 3.1b:
    host_probes), so it comes up at version 5 with the table."""
    conn = db.open_db()
    assert dbfile.exists()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert db.SCHEMA_VERSION == 5 == len(db.MIGRATIONS)
    assert _names(conn, "table") == TABLES | MONITOR_TABLES
    assert _names(conn, "index") == INDEXES | MONITOR_INDEXES
    for table, cols in {**COLUMNS, **MONITOR_COLUMNS}.items():
        assert [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")] == cols, table
    conn.close()


def test_monitor_tables_carry_the_spec_types_keys_and_indexes(dbfile):
    """Spec §3: every probes/counters/hardware column is typed as the block says, the child
    tables are keyed by probe_id referencing probes(id), and the two indexes are on (endpoint_id, t)."""
    conn = db.open_db()
    types = {r["name"]: r["type"] for r in conn.execute("PRAGMA table_info(probes)")}
    assert types == {
        "id": "INTEGER", "t": "REAL", "endpoint_id": "INTEGER", "model_id": "INTEGER", "up": "INTEGER",
        "host": "TEXT", "error": "TEXT", "latency_ms": "INTEGER", "context": "INTEGER",
        "slots_total": "INTEGER", "slots_busy": "INTEGER", "requests_running": "INTEGER",
        "requests_waiting": "INTEGER", "kv_cache_usage": "REAL", "gen_tps": "REAL", "since": "REAL",
        "state": "TEXT",
    }
    notnull = {r["name"] for r in conn.execute("PRAGMA table_info(probes)") if r["notnull"]}
    assert notnull == {"t", "endpoint_id", "up"}
    for child in ("probe_counters", "probe_hardware"):
        info = list(conn.execute(f"PRAGMA table_info({child})"))
        assert [(r["name"], r["pk"]) for r in info if r["pk"]] == [("probe_id", 1)], child
        assert {r["type"] for r in info if r["name"] != "probe_id"} == {"REAL"}, child
        fks = [(r["table"], r["from"], r["to"]) for r in conn.execute(f"PRAGMA foreign_key_list({child})")]
        assert fks == [("probes", "probe_id", "id")], child
    fks = {(r["table"], r["from"], r["to"]) for r in conn.execute("PRAGMA foreign_key_list(probes)")}
    assert fks == {("endpoints", "endpoint_id", "id"), ("models", "model_id", "id")}
    for index, table in (("probes_endpoint_t", "probes"), ("endpoint_events_endpoint_t", "endpoint_events")):
        assert [r["name"] for r in conn.execute(f"PRAGMA index_info({index})")] == ["endpoint_id", "t"]
        assert conn.execute("SELECT tbl_name FROM sqlite_master WHERE name = ?", (index,)).fetchone()[0] == table
    conn.close()


def test_endpoint_events_kind_check_accepts_up_down_restarted_only(dbfile):
    conn = db.open_db()
    ep = db.lookup_id(conn, "endpoints", name="ai1", base_url="http://ai1:8080", backend="strata", host="ai1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO endpoint_events (t, endpoint_id, kind) VALUES (1.0, ?, 'bogus')", (ep,))
    with pytest.raises(sqlite3.IntegrityError):  # kind is NOT NULL
        conn.execute("INSERT INTO endpoint_events (t, endpoint_id, kind) VALUES (1.0, ?, NULL)", (ep,))
    for kind in ("up", "down", "restarted"):
        conn.execute("INSERT INTO endpoint_events (t, endpoint_id, kind) VALUES (1.0, ?, ?)", (ep, kind))
    assert conn.execute("SELECT COUNT(*) FROM endpoint_events").fetchone()[0] == 3
    with pytest.raises(sqlite3.IntegrityError):  # foreign keys are on: no probe row for an unknown endpoint
        conn.execute("INSERT INTO probes (t, endpoint_id, up) VALUES (1.0, 999, 1)")
    conn.close()


def test_opening_the_same_file_twice_is_idempotent(dbfile):
    db.open_db().close()
    conn = db.open_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert _names(conn, "table") == TABLES | MONITOR_TABLES
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


def _v1_file(path: Path) -> None:
    """A file as an older localagents would have left it: only the version-1 DDL ran and
    user_version says 1. The version-2 step is a non-idempotent ALTER, so the file must be
    built at v1, not a v2 file with its version rewound."""
    raw = sqlite3.connect(path)
    db.MIGRATIONS[0](raw)
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()


def test_a_version_1_file_opens_without_rerunning_ddl(dbfile, monkeypatch):
    _v1_file(dbfile)

    def boom(conn):
        raise AssertionError("the version-1 DDL ran again")

    monkeypatch.setattr(db, "MIGRATIONS", [boom, *db.MIGRATIONS[1:]])
    conn = db.open_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert _names(conn, "table") == TABLES | MONITOR_TABLES
    assert "compactions" in [r["name"] for r in conn.execute("PRAGMA table_info(jobs)")]
    conn.close()


def test_a_version_1_file_migrates_to_version_2_with_its_rows_intact(dbfile):
    """The version-2 step adds the column and touches nothing else: a v1 file's rows open at
    version 2 unchanged, with compactions 0 on them (ALTER ADD COLUMN DEFAULT)."""
    _v1_file(dbfile)
    raw = sqlite3.connect(dbfile)
    raw.row_factory = sqlite3.Row
    raw.execute("INSERT INTO mcp_servers (id, pid, started, updated) VALUES ('s1', 42, 1.0, 1.0)")
    raw.execute(
        "INSERT INTO jobs (id, name, mcp_server_id, status, created_at, updated)"
        " VALUES ('j1', '20261029-000000-00000000', 's1', 'running', 1.0, 1.0)")
    raw.commit()
    raw.close()
    conn = db.open_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    row = conn.execute("SELECT status, num_turns, compactions FROM jobs WHERE id = 'j1'").fetchone()
    assert row["status"] == "running" and row["num_turns"] is None and row["compactions"] == 0
    assert conn.execute("SELECT pid FROM mcp_servers WHERE id = 's1'").fetchone()["pid"] == 42
    conn.close()


def test_a_version_2_file_migrates_to_version_3_with_its_job_intact(dbfile):
    """Story 2.2: the version-3 step only adds the monitor's tables. A file a 1.11 localagents
    left at version 2 (both earlier steps run, user_version 2, one job row) opens at version 3
    with the job row unchanged and the four monitor tables empty."""
    raw = sqlite3.connect(dbfile)
    db.MIGRATIONS[0](raw)
    db.MIGRATIONS[1](raw)
    raw.execute("PRAGMA user_version = 2")
    raw.execute("INSERT INTO mcp_servers (id, pid, started, updated) VALUES ('s1', 42, 1.0, 1.0)")
    raw.execute(
        "INSERT INTO jobs (id, name, mcp_server_id, status, created_at, updated, num_turns, compactions)"
        " VALUES ('j1', '20261029-000000-00000000', 's1', 'succeeded', 1.0, 2.0, 7, 1)")
    raw.commit()
    assert MONITOR_TABLES.isdisjoint(r[0] for r in raw.execute("SELECT name FROM sqlite_master"))
    raw.close()
    conn = db.open_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert _names(conn, "table") == TABLES | MONITOR_TABLES
    row = conn.execute("SELECT name, status, updated, num_turns, compactions FROM jobs WHERE id = 'j1'").fetchone()
    assert tuple(row) == ("20261029-000000-00000000", "succeeded", 2.0, 7, 1)
    for table in MONITOR_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    conn.close()


def test_a_version_3_file_migrates_to_version_4_with_its_probe_rows_intact(dbfile):
    """Story 3.1a ruling A: the version-4 step only appends columns. A file a 2.2-era
    localagents left at version 3 (user_version 3, one probe with a hardware row) opens at
    the current version with the rows unchanged and the appended columns NULL on them."""
    raw = sqlite3.connect(dbfile)
    for step in db.MIGRATIONS[:3]:
        step(raw)
    raw.execute("PRAGMA user_version = 3")
    raw.execute("INSERT INTO endpoints (name, base_url, backend, host) VALUES ('ai1', 'http://ai1:8080', 'strata', 'ai1')")
    raw.execute("INSERT INTO probes (t, endpoint_id, up, latency_ms) VALUES (1.0, 1, 1, 12)")
    raw.execute("INSERT INTO probe_hardware (probe_id, gpu_util, ram_total) VALUES (1, 55.0, 1024.0)")
    raw.commit()
    assert "state" not in [r[1] for r in raw.execute("PRAGMA table_info(probes)")]  # raw has no Row factory
    raw.close()
    conn = db.open_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert _names(conn, "table") == TABLES | MONITOR_TABLES
    assert [r["name"] for r in conn.execute("PRAGMA table_info(probes)")] == MONITOR_COLUMNS["probes"]
    assert [r["name"] for r in conn.execute("PRAGMA table_info(probe_hardware)")] == MONITOR_COLUMNS["probe_hardware"]
    row = conn.execute("SELECT * FROM probes WHERE id = 1").fetchone()
    assert row["t"] == 1.0 and row["up"] == 1 and row["latency_ms"] == 12 and row["state"] is None
    h = conn.execute("SELECT * FROM probe_hardware WHERE probe_id = 1").fetchone()
    assert h["gpu_util"] == 55.0 and h["ram_total"] == 1024.0
    assert h["disk_read_mb"] is None and h["disk_write_mb"] is None
    conn.close()


def test_a_version_4_file_migrates_to_version_5_with_its_rows_intact(dbfile):
    """Story 3.1b: the version-5 step only adds the host_probes table. A file a 3.1a-era
    localagents left at version 4 (user_version 4, a probe carrying the v4 state string and a
    hardware row with the v4 disk columns) opens at version 5 with the rows unchanged,
    host_probes present and empty, and its index on (host, t)."""
    raw = sqlite3.connect(dbfile)
    for step in db.MIGRATIONS[:4]:
        step(raw)
    raw.execute("PRAGMA user_version = 4")
    raw.execute("INSERT INTO endpoints (name, base_url, backend, host) VALUES ('ai1', 'http://ai1:8080', 'strata', 'ai1')")
    raw.execute("INSERT INTO probes (t, endpoint_id, up, state) VALUES (1.0, 1, 1, 'busy')")
    raw.execute("INSERT INTO probe_hardware (probe_id, disk_read_mb) VALUES (1, 42.0)")
    raw.commit()
    assert "host_probes" not in (r[0] for r in raw.execute("SELECT name FROM sqlite_master"))
    raw.close()
    conn = db.open_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert _names(conn, "table") == TABLES | MONITOR_TABLES
    assert _names(conn, "index") == INDEXES | MONITOR_INDEXES
    row = conn.execute("SELECT * FROM probes WHERE id = 1").fetchone()
    assert row["t"] == 1.0 and row["up"] == 1 and row["state"] == "busy"
    h = conn.execute("SELECT * FROM probe_hardware WHERE probe_id = 1").fetchone()
    assert h["disk_read_mb"] == 42.0
    assert [r["name"] for r in conn.execute("PRAGMA table_info(host_probes)")] == MONITOR_COLUMNS["host_probes"]
    assert [r["name"] for r in conn.execute("PRAGMA index_info(host_probes_host_t)")] == ["host", "t"]
    assert conn.execute("SELECT COUNT(*) FROM host_probes").fetchone()[0] == 0
    conn.close()


def test_host_probes_answer_check_accepts_the_three_answers_and_nothing_else(dbfile):
    """Spec §3: answer is one of the three probe_port results; host and address are NOT NULL
    (the row names the ask: when, on which host's address, with which answer)."""
    conn = db.open_db()
    for answer in ("answered", "refused", "nothing"):
        conn.execute("INSERT INTO host_probes (t, host, address, answer) VALUES (1.0, 'ai1', '10.0.0.50', ?)",
                     (answer,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO host_probes (t, host, address, answer) VALUES (1.0, 'ai1', '10.0.0.50', 'bogus')")
    with pytest.raises(sqlite3.IntegrityError):  # host is NOT NULL
        conn.execute("INSERT INTO host_probes (t, host, address, answer) VALUES (1.0, NULL, '10.0.0.50', 'answered')")
    assert conn.execute("SELECT COUNT(*) FROM host_probes").fetchone()[0] == 3
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


def test_lookup_id_writes_first_seen_from_its_reader_clock(dbfile):
    """F4, ruling B: the endpoint lookup takes the clock to write ``first_seen`` with, so a
    deterministic writer (the seed) is not made nondeterministic by the wall clock."""
    conn = db.open_db()
    ep = db.lookup_id(conn, "endpoints", now=1234.5, **EP)
    assert conn.execute("SELECT first_seen FROM endpoints WHERE id = ?", (ep,)).fetchone()[0] == 1234.5
    # the default is the wall clock, as before
    ep2 = db.lookup_id(conn, "endpoints", **{**EP, "name": "other"})
    first = conn.execute("SELECT first_seen FROM endpoints WHERE id = ?", (ep2,)).fetchone()[0]
    assert first > 1234.5
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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert _names(conn, "table") == TABLES | MONITOR_TABLES
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


def test_session_pid_finds_an_npm_installed_claude_running_as_node(monkeypatch):
    """Story 1.9a row 5 (1.9 review, probe 3): an npm-installed Claude Code is a node process
    whose command line runs the npm shim's entry (``.../bin/claude``), so the walk that looked
    only for comm ``claude`` found nothing and the status line treated every session's jobs as
    its own. A node ancestor whose cmdline has an argument named ``claude`` is the session."""
    _fake_proc(monkeypatch, {
        "/proc/100/stat": "100 (python) S 99 100 100 0 -1 4194304 1 0 0 0",
        "/proc/99/stat": "99 (uv) S 8 99 99 0 -1 4194304 1 0 0 0",
        "/proc/99/comm": "uv\n",
        "/proc/8/stat": "8 (node) S 5 8 8 0 -1 4194304 1 0 0 0",
        "/proc/8/comm": "node\n",
        "/proc/8/cmdline": "/usr/bin/node\0/home/x/.npm/bin/claude\0",
        "/proc/5/comm": "bash\n",
    })
    assert db.session_pid(start_pid=100) == 8


def test_session_pid_finds_modern_node_whose_comm_carries_the_thread_name(monkeypatch):
    """Review of 1.9a (probe 4): node 26 on this machine names its main thread in ``comm``,
    ``node-MainThread`` — the exact ``comm == "node"`` test never fired, so the npm branch of
    the walk was dead on a modern node. The comm test is node-or-node-something."""
    _fake_proc(monkeypatch, {
        "/proc/100/stat": "100 (python) S 8 100 100 0 -1 4194304 1 0 0 0",
        "/proc/8/stat": "8 (node-MainThread) S 1 8 8 0 -1 4194304 1 0 0 0",
        "/proc/8/comm": "node-MainThread\n",
        "/proc/8/cmdline": "node\0/home/x/.npm/bin/claude\0",
        "/proc/1/comm": "systemd\n",
    })
    assert db.session_pid(start_pid=100) == 8


def test_session_pid_ignores_claude_appearing_later_in_a_node_command_line(monkeypatch):
    """Review of 1.9a (probe 4 d): only the interpreter slot (argv[0]) and the script slot
    (argv[1]) name what the process IS — npm's shim lands in one of them. ``node
    something.js --name claude`` is some other program that takes a flag called claude."""
    _fake_proc(monkeypatch, {
        "/proc/100/stat": "100 (python) S 8 100 100 0 -1 4194304 1 0 0 0",
        "/proc/8/stat": "8 (node) S 1 8 8 0 -1 4194304 1 0 0 0",
        "/proc/8/comm": "node\n",
        "/proc/8/cmdline": "node\0/home/x/something.js\0--name\0claude\0",
        "/proc/1/comm": "systemd\n",
    })
    assert db.session_pid(start_pid=100) is None


def test_session_pid_ignores_a_node_running_something_else(monkeypatch):
    """The cmdline rule is the basename ``claude``: a node app (or cli.js by another name) is
    not the session, and the walk goes on past it."""
    _fake_proc(monkeypatch, {
        "/proc/100/stat": "100 (python) S 8 100 100 0 -1 4194304 1 0 0 0",
        "/proc/8/stat": "8 (node) S 1 8 8 0 -1 4194304 1 0 0 0",
        "/proc/8/comm": "node\n",
        "/proc/8/cmdline": "/usr/bin/node\0/home/x/app/server.js\0--claude-ish\0",
        "/proc/1/comm": "systemd\n",
    })
    assert db.session_pid(start_pid=100) is None


def test_my_owner_agrees_with_the_walk_on_a_node_session(monkeypatch):
    """status.my_owner is db.session_pid with no argument: both name the node ancestor."""
    from localagents import status

    me = os.getpid()
    _fake_proc(monkeypatch, {
        f"/proc/{me}/stat": f"{me} (pytest) S 4242 {me} {me} 0 -1 4194304 1 0 0 0",
        "/proc/4242/stat": "4242 (node) S 1 4242 4242 0 -1 4194304 1 0 0 0",
        "/proc/4242/comm": "node\n",
        "/proc/4242/cmdline": "node\0/usr/local/bin/claude\0",
        "/proc/1/comm": "systemd\n",
    })
    assert db.session_pid() == 4242
    assert status.my_owner() == 4242


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
