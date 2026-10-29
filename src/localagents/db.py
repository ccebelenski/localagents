"""The localagents SQLite database: one file shared by every MCP server process on the machine.

Schema and write rules: ``docs/superpowers/specs/2026-10-29-mcp-sqlite-schema.md``. Standard
library only, because ``localagents statusline`` will read this on every status-bar refresh
(story 1.5a) and must not pull in the server stack (see ``localagents/__init__.py``).

Write rules every caller keeps:

- every read-then-write sequence runs inside ``immediate(conn)`` (``BEGIN IMMEDIATE``): a
  deferred transaction that upgrades to a writer can fail with SQLITE_BUSY at once, and
  ``busy_timeout`` does not retry that case;
- lookups (endpoints, models, paths) go through ``lookup_id``: ``INSERT OR IGNORE`` then
  ``SELECT``, never select-then-insert.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


BUSY_TIMEOUT_MS = 5000


class SchemaError(RuntimeError):
    """The file's schema version is one this code cannot open."""


def db_path() -> Path:
    """``LOCALAGENTS_DB`` when set, else ``<state_dir>/localagents.db``."""
    override = os.environ.get("LOCALAGENTS_DB")
    if override:
        return Path(override).expanduser()
    from .jobs import state_dir  # lazy: jobs imports this module at top level (story 1.2)

    return state_dir() / "localagents.db"


# --- schema ----------------------------------------------------------------------------------
# Version 1, exactly as the spec names it. Plain CREATE (no IF NOT EXISTS): a step that runs twice
# is a bug in the version bookkeeping and must fail loudly, not pass silently.
#
# Lookup key columns are NOT NULL beyond the spec's text: SQLite treats NULLs as distinct under
# UNIQUE, so a NULL in a key would mint a new row on every lookup instead of deduplicating.

_V1 = [
    """CREATE TABLE mcp_servers (
        id          TEXT PRIMARY KEY,
        pid         INTEGER,
        owner       INTEGER,
        cwd         TEXT,
        config      TEXT,
        version     TEXT,
        transport   TEXT,
        shim_port   INTEGER,
        started     REAL,
        updated     REAL,
        stopped     REAL
    )""",
    """CREATE TABLE endpoints (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        base_url    TEXT NOT NULL,
        backend     TEXT NOT NULL,
        host        TEXT NOT NULL,
        first_seen  REAL,
        UNIQUE (name, base_url, backend, host)
    )""",
    """CREATE TABLE endpoint_load (
        endpoint_id INTEGER PRIMARY KEY REFERENCES endpoints(id),
        updated     REAL,
        load        TEXT
    )""",
    """CREATE TABLE models (
        id          INTEGER PRIMARY KEY,
        served_id   TEXT NOT NULL UNIQUE
    )""",
    """CREATE TABLE paths (
        id          INTEGER PRIMARY KEY,
        path        TEXT NOT NULL UNIQUE
    )""",
    """CREATE TABLE jobs (
        id            TEXT PRIMARY KEY,
        name          TEXT,
        mcp_server_id TEXT REFERENCES mcp_servers(id),
        endpoint_id   INTEGER REFERENCES endpoints(id),
        model_id      INTEGER REFERENCES models(id),
        task          TEXT,
        cwd           TEXT,
        isolation     TEXT CHECK (isolation IN ('none', 'worktree')),
        status        TEXT CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'timeout', 'lost')),
        created_at    REAL,
        started_at    REAL,
        finished_at   REAL,
        updated       REAL,
        session_id    TEXT,
        result        TEXT,
        error         TEXT,
        last_activity TEXT,
        num_turns     INTEGER,
        context       INTEGER,
        generated_tokens        INTEGER,
        prompt_tokens_processed INTEGER,
        prompt_tokens_cached    INTEGER,
        cache_hit_ratio         REAL,
        prompt_tps              REAL,
        generate_tps            REAL,
        spec_decode_acceptance  REAL,
        worktree      TEXT,
        dump          INTEGER,
        log_events    INTEGER
    )""",
    """CREATE TABLE requests (
        job_id      TEXT REFERENCES jobs(id) ON DELETE CASCADE,
        path_id     INTEGER REFERENCES paths(id),
        t           REAL,
        http_status INTEGER,
        ms          INTEGER,
        gen_ms      INTEGER,
        stream      INTEGER,
        input_tokens          INTEGER,
        output_tokens         INTEGER,
        cache_read_tokens     INTEGER,
        cache_creation_tokens INTEGER,
        messages    INTEGER,
        tools       INTEGER,
        bytes       INTEGER,
        moved_system INTEGER,
        folded_text  INTEGER,
        context_exceeded TEXT,
        error       TEXT
    )""",
    """CREATE TABLE tool_calls (
        job_id      TEXT REFERENCES jobs(id) ON DELETE CASCADE,
        t           REAL,
        finished    REAL,
        tool        TEXT,
        tool_use_id TEXT,
        input       TEXT,
        is_error    INTEGER
    )""",
    "CREATE INDEX jobs_name ON jobs(name)",
    "CREATE INDEX jobs_mcp_server_id ON jobs(mcp_server_id)",
    "CREATE INDEX jobs_endpoint_id ON jobs(endpoint_id)",
    "CREATE INDEX jobs_model_id ON jobs(model_id)",
    "CREATE INDEX jobs_status_finished_at ON jobs(status, finished_at)",
    "CREATE INDEX mcp_servers_updated ON mcp_servers(updated)",
    "CREATE INDEX requests_job_id_t ON requests(job_id, t)",
    "CREATE INDEX tool_calls_job_id_t ON tool_calls(job_id, t)",
]


def _statements(stmts: list[str]) -> Callable[[sqlite3.Connection], None]:
    # One execute per statement: executescript() would COMMIT the surrounding BEGIN IMMEDIATE.
    def step(conn: sqlite3.Connection) -> None:
        for s in stmts:
            conn.execute(s)
    return step


# MIGRATIONS[n] takes a file from user_version n to n + 1. A version 2 is one appended step.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_statements(_V1)]
SCHEMA_VERSION = len(MIGRATIONS)


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> None:
    """Bring the file to ``SCHEMA_VERSION``; user_version 0 is a fresh file.

    A current file costs one PRAGMA read and takes no write lock. Otherwise the version is
    re-read under ``BEGIN IMMEDIATE`` so two processes opening a fresh file at once apply each
    step exactly once.
    """
    v = _version(conn)
    if v == SCHEMA_VERSION:
        return
    if v > SCHEMA_VERSION:
        raise SchemaError(
            f"database schema version {v} is newer than this localagents knows ({SCHEMA_VERSION}); "
            "upgrade localagents or point LOCALAGENTS_DB at another file"
        )
    with immediate(conn):
        v = _version(conn)
        if v > SCHEMA_VERSION:  # another process upgraded it between the two reads
            raise SchemaError(f"database schema version {v} is newer than this localagents knows ({SCHEMA_VERSION})")
        for n in range(v, SCHEMA_VERSION):
            MIGRATIONS[n](conn)
            conn.execute(f"PRAGMA user_version = {n + 1}")


def _set_wal(conn: sqlite3.Connection) -> None:
    """Switch the file to WAL, retrying a busy refusal for up to the busy timeout.

    Converting a rollback-journal file to WAL takes an exclusive lock and SQLite does not run the
    busy handler for it: processes opening a fresh file at once got "database is locked" here
    (tests/test_db.py, the fresh-file race). WAL is persistent, so a file already in WAL is left
    alone and only the first opens of a new file can contend.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as e:
            busy = e.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)  # 3.11+; the project is 3.12+
            if not busy or time.monotonic() > deadline:
                raise
            time.sleep(0.01)


def open_db(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating) the database, set the per-connection pragmas, migrate to current."""
    p = Path(path) if path is not None else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        _set_wal(conn)
        conn.execute("PRAGMA foreign_keys = ON")  # per connection, never persisted in the file
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


# Lookup tables and their keys. Table and column names are never taken from the caller unchecked.
_LOOKUPS: dict[str, tuple[str, ...]] = {
    "endpoints": ("name", "base_url", "backend", "host"),
    "models": ("served_id",),
    "paths": ("path",),
}


def lookup_id(conn: sqlite3.Connection, table: str, **cols: str) -> int:
    """The id of the row with exactly these key columns, inserting it on first sight.

    ``INSERT OR IGNORE`` then ``SELECT`` (never select-then-insert), so concurrent callers in
    other processes converge on one row. ``endpoints`` also gets ``first_seen`` on insert; it is
    not part of the key. Every key column must be given and non-NULL.
    """
    key = _LOOKUPS.get(table)
    if key is None:
        raise ValueError(f"{table!r} is not a lookup table (one of {', '.join(_LOOKUPS)})")
    if set(cols) != set(key):
        raise ValueError(f"{table} is keyed by exactly ({', '.join(key)}); got ({', '.join(cols)})")
    missing = [c for c in key if cols[c] is None]
    if missing:
        raise ValueError(f"{table} key column(s) {', '.join(missing)} must not be NULL")
    values = [cols[c] for c in key]
    ins_cols = list(key) + (["first_seen"] if table == "endpoints" else [])
    ins_vals = values + ([time.time()] if table == "endpoints" else [])
    conn.execute(
        f"INSERT OR IGNORE INTO {table} ({', '.join(ins_cols)}) VALUES ({', '.join('?' * len(ins_cols))})",
        ins_vals,
    )
    row = conn.execute(f"SELECT id FROM {table} WHERE {' AND '.join(f'{c} = ?' for c in key)}", values).fetchone()
    if row is None:  # unreachable while the key columns are NOT NULL and UNIQUE; never return a guess
        raise RuntimeError(f"{table} lookup inserted nothing and found nothing for {cols}")
    return row[0]


def new_uuid() -> str:
    """A uuid4 as text with dashes: ``mcp_servers.id`` and ``jobs.id``."""
    return str(uuid.uuid4())


def job_name(created_at: float, job_id: str) -> str:
    """``YYYYMMDD-HHMMSS-<first 8 hex of the id>`` in local time: the job directory and branch name."""
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(created_at)) + "-" + job_id.replace("-", "")[:8]


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``; ``ROLLBACK`` when the body (or the commit) raises."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:  # some errors (SQLITE_FULL, ...) have already rolled back
            conn.execute("ROLLBACK")
        raise
