"""The localagents SQLite database: one file shared by every MCP server process on the machine.

Schema and write rules: ``docs/superpowers/specs/2026-10-29-mcp-sqlite-schema.md``. Standard
library only, because ``localagents statusline`` will read this on every status-bar refresh
(story 1.5a) and must not pull in the server stack (see ``localagents/__init__.py``).

The monitor's tables (``probes``, ``probe_counters``, ``probe_hardware``, ``endpoint_events``,
``host_probes``; schema version 3, extended by version 4 — ``probes.state`` and the
``probe_hardware`` disk columns —, version 5 — ``host_probes``, the node fallback's answers —
and version 6 — ``endpoints.last_active``, the stored last-active witness — per
``docs/superpowers/specs/2026-10-29-monitor-design.md`` §3) are monitor-owned:
only ``localagents.monitor`` writes or prunes them. Their migration lives here because the file
is shared and versioned as one, so the MCP server creates them too (an idle table costs nothing),
and the MCP server never imports the monitor package (HANDOFF "Decided, do not reopen"; the
static guard in ``tests/test_server.py``). A migration step that has arithmetic to do leaves it
to the monitor, which runs it at its start (the version-6 witness backfill, see ``_v6``); the
one step that moves files is version 9's directory rename (``_v9``), a filesystem move the
monitor cannot do before the server opens the file.

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
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


BUSY_TIMEOUT_MS = 5000


class SchemaError(RuntimeError):
    """The file's schema version is one this code cannot open."""


# story 1.15 (the 2026-10-30 22:44 incident: a lane's worktree code migrated the REAL database and
# renamed 78 real job directories — the conftests' redirect is a convention a subprocess can drop):
# the file this package runs from. A checkout under a `worktrees` directory — a lane's
# `~/.local/state/localagents/worktrees/<repo>/<id>` or an Agent's `.claude/worktrees/agent-*` —
# never opens the default state directory; it must be redirected. The operator's install (the main
# checkout, or a wheel) has no such component and is unaffected. Module-level so a test can point it.
PACKAGE_FILE = Path(__file__).resolve()
WORKTREE_MARK = "worktrees"


def _running_from_a_worktree() -> bool:
    return WORKTREE_MARK in PACKAGE_FILE.parts


def state_dir() -> Path:
    """``$XDG_STATE_HOME/localagents`` (or ``~/.local/state/localagents``), created on first use.
    Lives here because both the database's own path and the job directories derive from it, and
    a version-9 migration step (``_v9``) needs it too; ``jobs.state_dir`` re-exports the name.
    Story 1.15: with no ``XDG_STATE_HOME`` set, a package running from a worktree checkout refuses
    rather than touch the real state directory — the one database and the real job directories
    belong to the operator's install alone."""
    base = os.environ.get("XDG_STATE_HOME")
    if not base:
        if _running_from_a_worktree():
            raise RuntimeError(
                "refusing to use the default state directory from a worktree checkout "
                f"({PACKAGE_FILE}): a lane never opens the real database or job directories — "
                "set XDG_STATE_HOME (and LOCALAGENTS_DB) to a directory of its own"
            )
        base = str(Path.home() / ".local" / "state")
    d = Path(base) / "localagents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    """``LOCALAGENTS_DB`` when set, else ``<state_dir>/localagents.db``."""
    override = os.environ.get("LOCALAGENTS_DB")
    if override:
        return Path(override).expanduser()
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
    # list_jobs(all=True) takes the newest N rows: ORDER BY created_at DESC, rowid DESC scans
    # this index backwards instead of sorting the whole history (review of 1.8)
    "CREATE INDEX jobs_created_at ON jobs(created_at)",
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


# Version 2 (story 1.11): the job's compaction count, written by the runner when the harness
# sends its compact_boundary system message. One appended step, never a rewritten one: a
# version-1 file runs it on open, and MIGRATIONS only ever grows.
_V2 = [
    "ALTER TABLE jobs ADD COLUMN compactions INTEGER NOT NULL DEFAULT 0",
]

# Version 3 (story 2.2): the monitor's tables, exactly the block approved 2026-10-29 in the
# monitor-design spec §3 "Storage". Monitor-owned: the MCP server creates them by migrating the
# shared file but never reads or writes them. Rates are computed between consecutive
# probe_counters rows at read time, never stored.
_V3 = [
    """CREATE TABLE probes (
        id               INTEGER PRIMARY KEY,
        t                REAL NOT NULL,
        endpoint_id      INTEGER NOT NULL REFERENCES endpoints(id),
        model_id         INTEGER REFERENCES models(id),
        up               INTEGER NOT NULL,
        host             TEXT,
        error            TEXT,
        latency_ms       INTEGER,
        context          INTEGER,
        slots_total      INTEGER,
        slots_busy       INTEGER,
        requests_running INTEGER,
        requests_waiting INTEGER,
        kv_cache_usage   REAL,
        gen_tps          REAL,
        since            REAL
    )""",
    "CREATE INDEX probes_endpoint_t ON probes(endpoint_id, t)",
    """CREATE TABLE probe_counters (
        probe_id             INTEGER PRIMARY KEY REFERENCES probes(id),
        prompt_tokens        REAL,
        prompt_tokens_cached REAL,
        prompt_seconds       REAL,
        generated_tokens     REAL,
        generated_seconds    REAL,
        spec_draft_tokens    REAL,
        spec_accepted_tokens REAL,
        prefix_queries       REAL,
        prefix_hits          REAL,
        preemptions          REAL
    )""",
    """CREATE TABLE probe_hardware (
        probe_id        INTEGER PRIMARY KEY REFERENCES probes(id),
        gpu_util        REAL,
        gpu_mem_used    REAL,
        gpu_mem_total   REAL,
        gpu_temp        REAL,
        gpu_power       REAL,
        gpu_power_limit REAL,
        gpu_pcie_rx_mb  REAL,
        gpu_pcie_tx_mb  REAL,
        cpu             REAL,
        ram_used        REAL,
        ram_total       REAL
    )""",
    """CREATE TABLE endpoint_events (
        id          INTEGER PRIMARY KEY,
        t           REAL NOT NULL,
        endpoint_id INTEGER NOT NULL REFERENCES endpoints(id),
        kind        TEXT NOT NULL CHECK (kind IN ('up', 'down', 'restarted')),
        detail      TEXT
    )""",
    "CREATE INDEX endpoint_events_endpoint_t ON endpoint_events(endpoint_id, t)",
]

# Version 4 (story 3.1a, ruling A of the 3.1 review): the live-state string and the two disk
# counters, appended through one step. probes.state is Strata's live.state verbatim; other
# backends get "busy" when requests_running or slots_busy is positive else "idle"; NULL when
# the probe is down. Nothing backfills: pre-v4 probes read NULL, like any unread figure.
_V4 = [
    "ALTER TABLE probes ADD COLUMN state TEXT",
    "ALTER TABLE probe_hardware ADD COLUMN disk_read_mb REAL",
    "ALTER TABLE probe_hardware ADD COLUMN disk_write_mb REAL",
]

# Version 5 (story 3.1b): the node fallback's answers, appended through one step. One row per
# port-22 ask (at most one per host per minute: asked only when every endpoint on the host
# timed out); the reader resolves an all-timeout node from the newest fresh row, so a restart
# reconstructs the node state from rows. Monitor-owned, pruned with the probes (retention).
_V5 = [
    """CREATE TABLE host_probes (
        id          INTEGER PRIMARY KEY,
        t           REAL NOT NULL,
        host        TEXT NOT NULL,
        address     TEXT NOT NULL,
        answer      TEXT NOT NULL CHECK (answer IN ('answered', 'refused', 'nothing'))
    )""",
    "CREATE INDEX host_probes_host_t ON host_probes (host, t)",
]

# Version 6 (story 3.7-fold-2, the 3.7 server-batch review's D5): the stored last-active
# witness. ``endpoints.last_active`` holds the ``t`` of the collector's newest advancing counters
# interval, written at the tick that advanced it, so ``read._last_active`` reads one value
# instead of walking the endpoint's retained counters rows on every snapshot (the review measured
# a 7-day idle endpoint's walk at 745 ms per overview). The column's contract: NULL = never
# walked; 0.0 = walked, no advancing interval within retention (the walk's memo; read maps it to
# null); ``t`` > 0 = the witness. This step adds the column ONLY: pairing the counters rows to
# backfill old files is the monitor's arithmetic, and the MCP server never imports the monitor
# package, so ``read.backfill_last_active`` runs at the monitor's start instead (once per
# endpoint with no witness), and the seed writes its world's witnesses itself.
def _v6(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE endpoints ADD COLUMN last_active REAL")


# Version 7 (3.9E query-plan audit, finding E1): the monitor snapshot's finished-jobs tail
# (read.py's ``_FINISHED_TAIL``: ``ORDER BY finished_at DESC, rowid DESC LIMIT 10`` over every
# terminal job) planned as a bare ``SCAN j`` plus ``USE TEMP B-TREE FOR ORDER BY`` — the sort
# walks the whole job history on every snapshot. One index fixes it: the plan becomes
# ``SCAN j USING INDEX jobs_finished_at`` (walked backwards) with no sort, and
# ``jobs_overlapping``'s OR terms gain index seeks too (finding E2). ``IF NOT EXISTS``, unlike
# the version-1 DDL: the audit verified the fix by creating this index by hand on files, so the
# step must tolerate one already present. The audit's E3 (``list_jobs``' live-only branch) gets
# no index here: with a provably-implying predicate SQLite still prefers
# ``jobs_status_finished_at``, so E3 is a predicate rewrite in server.py, not a schema change
# (tests/test_query_plans.py pins all three plans).
_V7 = [
    "CREATE INDEX IF NOT EXISTS jobs_finished_at ON jobs(finished_at)",
]


# Version 8 (story 1.12, the operator's word 2026-10-30): the finished job's transcript text,
# copied once at job completion (runner.run_job's finally, after the last transcript line and
# before the row turns terminal). One row per finished job that logged a line: a terminal row
# has its transcript row whenever a transcript file existed at completion; a job that never
# logged a line (cancelled before [start]) has neither, and readers say so. The file keeps being
# written line by line while the job runs (real-time reading) and keeps being pruned by `prune` —
# the row is what survives the file's retention. Readers take the row when present, else the
# file. No index: the primary key is the lookup (job_id). Nothing deletes these rows: database
# purge is its own open item (EPICS 1.12), and ON DELETE CASCADE fires only if a jobs row ever
# goes.
_V8 = [
    """CREATE TABLE transcripts (
        -- job_id is NOT NULL beyond the design's text: a TEXT PRIMARY KEY admits NULLs (only an
        -- INTEGER PRIMARY KEY is a rowid alias), and a NULL key skips the foreign key entirely
        job_id  TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        text    TEXT NOT NULL,
        bytes   INTEGER NOT NULL,
        written REAL NOT NULL
    )""",
]


# Version 9 (story 3.10f-1, ruling (n) of the 3.10 discussion): the job's one key is its uuid.
# ``jobs.name`` (``YYYYMMDD-HHMMSS-<first 8 hex of the id>``) was a second unique key whose stamp
# was only a proxy for ``created_at``; the directory, worktree and branch are named by the id
# now. A step is a callable, and this one has filesystem work to do before the column goes:
# every terminal job's ``jobs/<name>`` directory is renamed to ``jobs/<id>``. A live job
# (queued/running) keeps its directory: its server holds that path in memory and keeps writing
# there, and the name is still its handle until that server exits; the skip is logged with the
# id. A row whose old directory is already gone (pruned) has nothing to move; one whose id
# directory already exists (a retried rename) is left for the next step's release. An OSError
# is logged, never raised: a read-only state directory must not wedge every open of the file.
# Then the index the column carried and the column itself go (DROP COLUMN needs SQLite 3.35+).
def _v9(conn: sqlite3.Connection) -> None:
    if sqlite3.sqlite_version_info < (3, 35):
        raise SchemaError(
            f"schema version 9 drops jobs.name and needs SQLite >= 3.35; this is {sqlite3.sqlite_version}"
        )
    root: Path | None = None  # 1.15: the jobs root only when a row needs renaming (a fresh or empty file never touches it)
    for jid, name, status in conn.execute("SELECT id, name, status FROM jobs WHERE name IS NOT NULL").fetchall():
        if root is None:
            root = state_dir() / "jobs"
        old, new = root / name, root / jid
        try:
            if not old.is_dir():
                continue
            if status in ("queued", "running"):
                print(f"localagents: job {jid}: live job, leaving its directory {old} in place", file=sys.stderr)
                continue
            if new.exists():
                print(f"localagents: job {jid}: {new} already exists, leaving {old} in place", file=sys.stderr)
                continue
            old.rename(new)
        except OSError as e:
            print(f"localagents: job {jid}: could not rename {old}: {e}", file=sys.stderr)
    conn.execute("DROP INDEX jobs_name")
    conn.execute("ALTER TABLE jobs DROP COLUMN name")


# Version 10 (router 1, router-design spec §13, 2026-11-02): the router's tables and the three
# jobs columns. The DDL is §13's, with the spec preamble's rules written out: plain CREATE (a
# re-run must fail loudly, not half-create), every key column NOT NULL (a TEXT PRIMARY KEY
# admits NULLs and a NULL key skips a foreign key and splits a UNIQUE, per the version-8 note),
# every reference declared, a CHECK on every status column. ``scorecard_events`` is NOT here:
# the review's pass-generation ruling replaced the event log with ``scorecard_passes`` rows.
# ``jobs.priority`` is the caller's, never the brief's scoring (spec §5: priority is not a
# dimension); ``jobs.calibration`` marks a calibration probe job; ``jobs.gates`` is the gate
# feed's verdicts as JSON, NULL until the router's build writes one.
_V10 = [
    """CREATE TABLE dimension_definitions (
        -- the versioned judgement document; the text is a draft owned by the calibration
        -- set spec (2026-11-02-calibration-set-design.md Appendix A)
        version  INTEGER PRIMARY KEY NOT NULL,
        document TEXT NOT NULL,
        created  REAL NOT NULL
    )""",
    """CREATE TABLE scorecard_rows (
        -- one endpoint/model serving a served_id; an archived row leaves its identity
        -- (endpoint_name, served_id) behind, so its replacement can insert
        id             INTEGER PRIMARY KEY NOT NULL,
        endpoint_name  TEXT NOT NULL,
        served_id      TEXT NOT NULL,
        endpoint_id    INTEGER NOT NULL REFERENCES endpoints(id),
        model_id       INTEGER NOT NULL REFERENCES models(id),
        created        REAL NOT NULL,
        archived       REAL,
        archive_reason TEXT,
        set_version    TEXT,
        band_times     TEXT,
        speed_factor   REAL,
        facts          TEXT
    )""",
    "CREATE UNIQUE INDEX scorecard_rows_identity ON scorecard_rows(endpoint_name, served_id)"
    " WHERE archived IS NULL",
    """CREATE TABLE scorecard_passes (
        -- one estimation pass over a row; the newest per row is the live answer
        id           TEXT PRIMARY KEY NOT NULL,
        row_id       INTEGER NOT NULL REFERENCES scorecard_rows(id),
        at           REAL NOT NULL,
        constants    TEXT NOT NULL,
        observations INTEGER NOT NULL
    )""",
    "CREATE INDEX scorecard_passes_row_at ON scorecard_passes(row_id, at)",
    """CREATE TABLE scorecard_cells (
        -- one (dimension, language) cell of a pass; NULL estimate = no answer yet
        pass_id      TEXT NOT NULL REFERENCES scorecard_passes(id),
        dimension    TEXT NOT NULL,
        language     TEXT NOT NULL DEFAULT '',
        estimate     REAL,
        count        REAL NOT NULL,
        probes_n     INTEGER,
        jobs_n       INTEGER,
        thin         INTEGER NOT NULL,
        defs_version INTEGER NOT NULL,
        PRIMARY KEY (pass_id, dimension, language)
    )""",
    """CREATE TABLE endpoint_capacity (
        -- the lane's capacity curve; points is the curve's sample count
        endpoint_id INTEGER PRIMARY KEY NOT NULL REFERENCES endpoints(id),
        kind        TEXT NOT NULL CHECK (kind IN ('partitioned', 'pooled', 'unknown')),
        curve       TEXT NOT NULL,
        state       TEXT NOT NULL CHECK (state IN ('prior', 'probed', 'refined')),
        points      INTEGER NOT NULL,
        updated     REAL NOT NULL
    )""",
    """CREATE TABLE queue_entries (
        -- a job waiting at an endpoint; admitted NULL = still queued; forced is a manual pin
        job_id        TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
        endpoint_id   INTEGER NOT NULL REFERENCES endpoints(id),
        owner_server  TEXT NOT NULL REFERENCES mcp_servers(id),
        priority_rank INTEGER NOT NULL,
        enqueued      REAL NOT NULL,
        admitted      REAL,
        forced        INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX queue_entries_dispatch ON queue_entries(endpoint_id, admitted, priority_rank, enqueued)",
    """CREATE TABLE router_tickets (
        -- the routing decision's record; chosen_row NULL until the choice, then the row taken;
        -- divert / divert_at record the queued-divert flag and its moment
        job_id       TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
        defs_version INTEGER NOT NULL,
        pass_id      TEXT,
        scores       TEXT NOT NULL,
        rows         TEXT NOT NULL,
        chosen_row   INTEGER REFERENCES scorecard_rows(id),
        divert       INTEGER NOT NULL,
        divert_at    REAL,
        est_wait_s   REAL,
        admitted     REAL,
        flags        TEXT
    )""",
    """CREATE TABLE verdicts (
        -- the caller's judgement of the finished job; dimensions is the per-dimension detail
        job_id     TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
        accepted   INTEGER NOT NULL,
        dimensions TEXT,
        at         REAL NOT NULL
    )""",
    """CREATE TABLE category_corrections (
        -- an operator's correction of a mis-scored brief: what it said, what it scored, what
        -- it should have been, at which definitions version
        id           INTEGER PRIMARY KEY NOT NULL,
        job_id       TEXT NOT NULL REFERENCES jobs(id),
        brief        TEXT NOT NULL,
        scored       TEXT NOT NULL,
        corrected    TEXT NOT NULL,
        defs_version INTEGER NOT NULL,
        at           REAL NOT NULL
    )""",
    """CREATE TABLE calibration_runs (
        -- one tier's run of the calibration set against one row; tier carries no CHECK: the
        -- set spec adds tiers, the status column is the closed list (§13)
        id          TEXT PRIMARY KEY NOT NULL,
        row_id      INTEGER NOT NULL REFERENCES scorecard_rows(id),
        tier        TEXT NOT NULL,
        set_version TEXT NOT NULL,
        plan        TEXT NOT NULL,
        estimate_s  REAL,
        actual_s    REAL,
        started     REAL NOT NULL,
        finished    REAL,
        status      TEXT NOT NULL CHECK (status IN ('running', 'complete', 'incomplete', 'stopped'))
    )""",
    """CREATE TABLE speed_samples (
        -- one job's measured generation speed at a session count; the capacity curve's fuel
        job_id       TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
        endpoint_id  INTEGER NOT NULL REFERENCES endpoints(id),
        sessions     INTEGER NOT NULL,
        generate_tps REAL NOT NULL,
        at           REAL NOT NULL
    )""",
    """CREATE TABLE calibration_results (
        -- one probe of one run; job_id NULL for a probe that never ran (skipped)
        run_id   TEXT NOT NULL REFERENCES calibration_runs(id),
        probe_id TEXT NOT NULL,
        job_id   TEXT REFERENCES jobs(id),
        status   TEXT NOT NULL CHECK (status IN ('pass', 'fail', 'skipped', 'unreliable')),
        seconds  REAL,
        reason   TEXT,
        PRIMARY KEY (run_id, probe_id)
    )""",
    "ALTER TABLE jobs ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'"
    " CHECK (priority IN ('urgent', 'normal', 'background'))",
    "ALTER TABLE jobs ADD COLUMN calibration INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN gates TEXT",
]


# Version 11 (router 1's whole-branch review, M7, router-design spec §13): three references
# version 10 left undeclared — a ticket's defs_version and pass_id, a scorecard cell's and a
# correction's defs_version. SQLite has no ALTER TABLE ... ADD CONSTRAINT, so the documented
# rebuild: create the new shape beside the old, copy every row, drop the old, rename. Rebuilding
# is safe for exactly these three tables because nothing references them: each name is handed
# back inside the same migration transaction, so no foreign key elsewhere could catch a swap
# mid-flight. The new shapes are v10's DDL with the REFERENCES added, column for column
# (tests/test_db.py pins the column order against the §13 listing).
_V11 = [
    """CREATE TABLE router_tickets_new (
        -- the routing decision's record; chosen_row NULL until the choice, then the row taken;
        -- divert / divert_at record the queued-divert flag and its moment
        job_id       TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
        defs_version INTEGER NOT NULL REFERENCES dimension_definitions(version),
        pass_id      TEXT REFERENCES scorecard_passes(id),
        scores       TEXT NOT NULL,
        rows         TEXT NOT NULL,
        chosen_row   INTEGER REFERENCES scorecard_rows(id),
        divert       INTEGER NOT NULL,
        divert_at    REAL,
        est_wait_s   REAL,
        admitted     REAL,
        flags        TEXT
    )""",
    "INSERT INTO router_tickets_new (job_id, defs_version, pass_id, scores, rows, chosen_row,"
    " divert, divert_at, est_wait_s, admitted, flags)"
    " SELECT job_id, defs_version, pass_id, scores, rows, chosen_row, divert, divert_at,"
    " est_wait_s, admitted, flags FROM router_tickets",
    "DROP TABLE router_tickets",
    "ALTER TABLE router_tickets_new RENAME TO router_tickets",
    """CREATE TABLE scorecard_cells_new (
        -- one (dimension, language) cell of a pass; NULL estimate = no answer yet
        pass_id      TEXT NOT NULL REFERENCES scorecard_passes(id),
        dimension    TEXT NOT NULL,
        language     TEXT NOT NULL DEFAULT '',
        estimate     REAL,
        count        REAL NOT NULL,
        probes_n     INTEGER,
        jobs_n       INTEGER,
        thin         INTEGER NOT NULL,
        defs_version INTEGER NOT NULL REFERENCES dimension_definitions(version),
        PRIMARY KEY (pass_id, dimension, language)
    )""",
    "INSERT INTO scorecard_cells_new (pass_id, dimension, language, estimate, count, probes_n,"
    " jobs_n, thin, defs_version)"
    " SELECT pass_id, dimension, language, estimate, count, probes_n, jobs_n, thin, defs_version"
    " FROM scorecard_cells",
    "DROP TABLE scorecard_cells",
    "ALTER TABLE scorecard_cells_new RENAME TO scorecard_cells",
    """CREATE TABLE category_corrections_new (
        -- an operator's correction of a mis-scored brief: what it said, what it scored, what
        -- it should have been, at which definitions version
        id           INTEGER PRIMARY KEY NOT NULL,
        job_id       TEXT NOT NULL REFERENCES jobs(id),
        brief        TEXT NOT NULL,
        scored       TEXT NOT NULL,
        corrected    TEXT NOT NULL,
        defs_version INTEGER NOT NULL REFERENCES dimension_definitions(version),
        at           REAL NOT NULL
    )""",
    "INSERT INTO category_corrections_new (id, job_id, brief, scored, corrected, defs_version, at)"
    " SELECT id, job_id, brief, scored, corrected, defs_version, at FROM category_corrections",
    "DROP TABLE category_corrections",
    "ALTER TABLE category_corrections_new RENAME TO category_corrections",
]


# Version 12 (router 11, router-design spec §7 "Accepted" 2026-11-04, §4 "Consumer
# requirement", §13): ``jobs.routed_by`` (the routing record Task 12 writes; NULL for a job
# named directly), ``queue_entries.blocked_by`` (why a queued entry waits: ``pool`` when the
# pool's cap held it, ``endpoint`` when the endpoint's own capacity or the queue order did; NULL
# when admitted or never evaluated) and ``'dropped'`` in the ``jobs.status`` CHECK (a job that
# left the queue without a session; ``started_at`` stays NULL).
#
# SQLite cannot alter a CHECK, and jobs is referenced by nine tables (requests, tool_calls and
# transcripts with ON DELETE CASCADE), so the status change is the sqlite.org ALTER TABLE page's
# "Making Other Kinds Of Table Schema Changes" procedure, its twelve steps as followed here:
#   1. "If foreign key constraints are enabled, disable them using PRAGMA foreign_keys=OFF."
#      ``migrate`` does it before its BEGIN (the pragma is a no-op inside a transaction); the
#      step refuses to run with them on, because DROP TABLE jobs would then be an implicit
#      DELETE that cascades the requests, tool_calls and transcripts rows away.
#   2. "Start a transaction." ``migrate``'s ``immediate``.
#   3. "Remember the format of all indexes, triggers, and views associated with table X." The
#      index and trigger SQL whose tbl_name is jobs is read from sqlite_master (any index the
#      operator made by hand comes back too; the 3.9E audit once did). This schema has no views.
#   4. "Use CREATE TABLE to construct a new table "new_X" that is in the desired revised format
#      of table X." ``jobs_new``: v11's columns in v11's order with v11's types, NOT NULLs and
#      defaults, the CHECK gaining 'dropped', and routed_by appended (it lands where an
#      ALTER TABLE ADD COLUMN would put it, so no separate ALTER is needed).
#   5. "Transfer content from X into new_X using a statement like: INSERT INTO new_X SELECT
#      ... FROM X." The rowid is carried too: jobs has a TEXT key, so its rowid is its own
#      column, and list_jobs and the monitor's tail break created_at ties by it.
#   6. "Drop the old table X: DROP TABLE X."
#   7. "Change the name of new_X to X using: ALTER TABLE new_X RENAME TO X." (New to old, never
#      old out of the way first: the children's REFERENCES jobs(id) text never changes.)
#   8. "Use CREATE INDEX, CREATE TRIGGER, and CREATE VIEW to reconstruct indexes, triggers, and
#      views associated with table X." The SQL saved at step 3, replayed.
#   9. "If any views refer to table X in a way that is affected by the schema change, then drop
#      those views using DROP VIEW and recreate them." None exist, and no column was renamed.
#  10. "If foreign key constraints were originally enabled then run PRAGMA foreign_key_check to
#      verify that the schema change did not break any foreign key constraints." Run before the
#      rebuild and after it: a violation the file did not already hold raises, which rolls the
#      whole migration back. A violation an older build left behind is not this change's and
#      does not wedge every open of the operator's file.
#  11. "Commit the transaction started in step 2." ``migrate``'s ``immediate``.
#  12. "If foreign keys constraints were originally enabled, reenable them now." ``migrate``,
#      in a finally.
# ``queue_entries.blocked_by`` is a plain ALTER TABLE ADD COLUMN (a CHECK on an added column is
# allowed; no row holds a value yet).
_JOBS_V11_COLUMNS = (
    "id", "mcp_server_id", "endpoint_id", "model_id", "task", "cwd", "isolation", "status",
    "created_at", "started_at", "finished_at", "updated", "session_id", "result", "error",
    "last_activity", "num_turns", "context", "generated_tokens", "prompt_tokens_processed",
    "prompt_tokens_cached", "cache_hit_ratio", "prompt_tps", "generate_tps",
    "spec_decode_acceptance", "worktree", "dump", "log_events", "compactions", "priority",
    "calibration", "gates",
)
_JOBS_V12 = """CREATE TABLE jobs_new (
        id            TEXT PRIMARY KEY,
        mcp_server_id TEXT REFERENCES mcp_servers(id),
        endpoint_id   INTEGER REFERENCES endpoints(id),
        model_id      INTEGER REFERENCES models(id),
        task          TEXT,
        cwd           TEXT,
        isolation     TEXT CHECK (isolation IN ('none', 'worktree')),
        status        TEXT CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'timeout', 'lost', 'dropped')),
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
        log_events    INTEGER,
        compactions   INTEGER NOT NULL DEFAULT 0,
        priority      TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('urgent', 'normal', 'background')),
        calibration   INTEGER NOT NULL DEFAULT 0,
        gates         TEXT,
        routed_by     TEXT
    )"""


def _v12(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise SchemaError(
            "schema version 12 rebuilds jobs and must run with foreign_keys off (DROP TABLE jobs "
            "would cascade-delete its requests, tool_calls and transcripts rows); migrate() turns "
            "them off before its transaction"
        )
    before = {tuple(r) for r in conn.execute("PRAGMA foreign_key_check")}
    saved = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name = 'jobs' AND type IN ('index', 'trigger')"
        " AND sql IS NOT NULL ORDER BY rowid"
    ).fetchall()]
    conn.execute(_JOBS_V12)
    cols = ", ".join(_JOBS_V11_COLUMNS)
    conn.execute(f"INSERT INTO jobs_new (rowid, {cols}) SELECT rowid, {cols} FROM jobs")
    conn.execute("DROP TABLE jobs")
    conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
    for sql in saved:
        conn.execute(sql)
    conn.execute("ALTER TABLE queue_entries ADD COLUMN blocked_by TEXT"
                 " CHECK (blocked_by IN ('pool', 'endpoint'))")
    broken = {tuple(r) for r in conn.execute("PRAGMA foreign_key_check")} - before
    if broken:
        raise SchemaError(f"schema version 12 would break foreign keys: {sorted(broken)[:5]}")


# Version 13 (router-design spec §6 "Expected wait", True-now 2026-11-15, the operator's go):
# ``queue_entries.est_wait_s``, the waiting job's expected wait in seconds, written at its first
# admission attempt and at every wake that leaves it waiting, NULL once admitted (or never
# estimated), so any server process reads the same refreshed number, a named job's included;
# and ``jobs_endpoint_model_finished``, the index the wait's job-history read walks: the newest
# finished jobs of one endpoint x served id, newest first, without walking the endpoint's
# history or the whole table's (``jobs_endpoint_id`` would read and sort every job of the
# endpoint; ``jobs_finished_at`` would walk every endpoint's jobs back to the first match).
# Both plain: a re-run must fail loudly. The ALTER is an append (no CHECK, no default).
_V13 = [
    "ALTER TABLE queue_entries ADD COLUMN est_wait_s REAL",
    "CREATE INDEX jobs_endpoint_model_finished ON jobs(endpoint_id, model_id, finished_at)",
]

# Version 14 (Epic 6.4, the operator's go 2026-11-17: "The additions seem logical and useful"):
# ``jobs.harness``, the harness the job's session ran in (Epic 6.3's registry names; every job
# before this version ran in Claude Code, so the default fills them truthfully);
# ``jobs.owner_session``, the coordinator's own session id when the client sent one (OpenCode's
# ``_meta["ai.opencode/sessionID"]`` on the tool call), NULL otherwise — the /proc walk's owner
# pid stays the Claude Code key; ``mcp_servers.client``, the client the server answered (6.1:
# declared, or named at initialize), NULL until it is known. Appends only; no row is rewritten.
_V14 = [
    "ALTER TABLE jobs ADD COLUMN harness TEXT NOT NULL DEFAULT 'claude-code'",
    "ALTER TABLE jobs ADD COLUMN owner_session TEXT",
    "ALTER TABLE mcp_servers ADD COLUMN client TEXT",
]

# MIGRATIONS[n] takes a file from user_version n to n + 1; a file comes up at version 14.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _statements(_V1), _statements(_V2), _statements(_V3), _statements(_V4), _statements(_V5), _v6,
    _statements(_V7), _statements(_V8), _v9, _statements(_V10), _statements(_V11), _v12,
    _statements(_V13), _statements(_V14),
]
SCHEMA_VERSION = len(MIGRATIONS)
# the steps that rebuild a table other tables reference: migrate() runs a pending one with
# foreign keys off (the sqlite.org procedure's step 1, which a transaction cannot do)
FOREIGN_KEYS_OFF_STEPS = frozenset({_v12})


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
    # a pending table rebuild needs foreign keys off, and the pragma only takes effect outside
    # a transaction; the version read above can only be older than the one re-read inside, so
    # this turns them off whenever the locked loop might run such a step
    fk_off = any(MIGRATIONS[n] in FOREIGN_KEYS_OFF_STEPS for n in range(v, SCHEMA_VERSION))
    fk_was_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if fk_off and fk_was_on:
        conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with immediate(conn):
            v = _version(conn)
            if v > SCHEMA_VERSION:  # another process upgraded it between the two reads
                raise SchemaError(f"database schema version {v} is newer than this localagents knows ({SCHEMA_VERSION})")
            for n in range(v, SCHEMA_VERSION):
                MIGRATIONS[n](conn)
                conn.execute(f"PRAGMA user_version = {n + 1}")
    finally:
        if fk_off and fk_was_on:
            conn.execute("PRAGMA foreign_keys = ON")


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
        # WAL + NORMAL: a commit survives a process crash without an fsync of its own (the log is
        # synced at checkpoints); only a power loss can lose the last commits. The server writes a
        # row per SDK message on the event loop, so the per-commit fsync of FULL is the wrong trade.
        conn.execute("PRAGMA synchronous = NORMAL")
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


def lookup_id(conn: sqlite3.Connection, table: str, now: float | None = None, **cols: str) -> int:
    """The id of the row with exactly these key columns, inserting it on first sight.

    ``INSERT OR IGNORE`` then ``SELECT`` (never select-then-insert), so concurrent callers in
    other processes converge on one row. ``endpoints`` also gets ``first_seen`` on insert; it is
    not part of the key, and ``now`` is the clock it is written from (the seed passes its world
    clock, so it stays a pure function of ``now``; the default is ``time.time()``). ``now`` can
    never collide with a key column: ``_LOOKUPS`` names all of them, and none is ``now``. Every
    key column must be given and non-NULL.
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
    ins_vals = values + ([now if now is not None else time.time()] if table == "endpoints" else [])
    conn.execute(
        f"INSERT OR IGNORE INTO {table} ({', '.join(ins_cols)}) VALUES ({', '.join('?' * len(ins_cols))})",
        ins_vals,
    )
    row = conn.execute(f"SELECT id FROM {table} WHERE {' AND '.join(f'{c} = ?' for c in key)}", values).fetchone()
    if row is None:  # unreachable while the key columns are NOT NULL and UNIQUE; never return a guess
        raise RuntimeError(f"{table} lookup inserted nothing and found nothing for {cols}")
    return row[0]


def pid_alive(pid: int, strict: bool = False) -> bool:
    """Is this pid still running? The liveness second check (spec Liveness: heartbeat first,
    pid second, because pids are reused). Lives here because both ``status`` (the status line)
    and ``jobs`` (the ``lost`` sweep) need it and neither may import the other.

    ``strict`` is the sweep's reading: a PermissionError means the pid belongs to another user,
    and this user's database is written only by this user's servers, so the server that wrote
    the row is gone and its pid was reused (review of 1.7, finding 5). The status line keeps the
    lenient reading: its heartbeat check comes first.

    A pid os.kill cannot even take (above a C int, not an integer: a corrupt row) is not a live
    process, in either reading (review of 1.7, finding 9). Nor is a pid <= 0: os.kill(0, 0)
    signals this process group and os.kill(-1, 0) every process this user may signal, so both
    succeed and a corrupt row's 0 or -1 read as alive (DeepSeek review of 6005f94)."""
    try:
        if pid <= 0:
            return False
    except TypeError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return not strict
    except (OverflowError, ValueError, TypeError):
        return False
    return True


def _is_claude(pid: int, comm: str) -> bool:
    """Is ``pid`` the Claude Code session? The native binary's ``comm`` is ``claude``; an
    npm-installed one runs as ``node`` with the npm shim's entry (``.../bin/claude``) as an
    argument, so its ``cmdline`` is what names it. Modern node names its main thread in
    ``comm`` — ``node-MainThread`` on node 26 — so the comm test accepts ``node`` and
    ``node-…`` alike (review of 1.9a, row 2). Only argv[0] and argv[1] are compared with
    ``claude``: the interpreter slot and the script slot, where npm's shim lands. A
    ``claude`` further along is an argument to some other program (``node something.js
    --name claude``), not this process being the session. A cmdline that cannot be read is
    not the session and the walk goes on (story 1.9a row 5)."""
    if comm == "claude":
        return True
    if not (comm == "node" or comm.startswith("node-")):
        return False
    try:
        with open(f"/proc/{pid}/cmdline") as f:
            args = f.read().split("\0")
    except OSError:
        return False
    return any(os.path.basename(arg) == "claude" for arg in args[:2])


def session_pid(start_pid: int | None = None) -> int | None:
    """Pid of the nearest Claude Code process above ``start_pid`` (default: this process): the
    session this process runs under, native ``claude`` or an npm-installed one running as
    ``node`` (story 1.9a row 5). Lives here because both ``status`` (the walk the status line
    does) and ``jobs`` (the ``mcp_servers.owner`` the line matches against) need the same
    answer — the server wrote its parent pid while the line walked to claude, so a session's own
    jobs rendered as "+N other" (story 1.9 item 9). None when there is no such ancestor. Stdlib
    only; ``open`` goes through the module global so a test can fake /proc."""
    pid = os.getpid() if start_pid is None else start_pid
    for _ in range(12):
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            with open(f"/proc/{ppid}/comm") as f:
                comm = f.read().strip()
        except (OSError, ValueError, IndexError):
            return None
        if _is_claude(ppid, comm):
            return ppid
        if ppid <= 1:
            return None
        pid = ppid
    return None


def new_uuid() -> str:
    """A uuid4 as text with dashes: ``mcp_servers.id`` and ``jobs.id``."""
    return str(uuid.uuid4())


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


# --- the build stamp (story 1.14) --------------------------------------------------------------

BUILD_STAMP_GIT_TIMEOUT_S = 2.0


def _editable_checkout() -> Path | None:
    """The checkout an editable install points at: the dist-info's ``direct_url.json`` with
    ``"dir_info": {"editable": true}`` names it as a ``file://`` URL. None when the install is
    not editable, has no metadata, or the file cannot be read."""
    import importlib.metadata
    import json
    from urllib.parse import unquote, urlparse

    try:
        text = importlib.metadata.distribution("localagents").read_text("direct_url.json")
        info = json.loads(text) if text else None
        if not isinstance(info, dict) or not (info.get("dir_info") or {}).get("editable"):
            return None
        url = urlparse(info.get("url") or "")
        return Path(unquote(url.path)) if url.scheme == "file" and url.path else None
    except Exception:  # noqa: BLE001  best effort: no checkout is an answer, never an error
        return None


def _source_checkout() -> Path | None:
    """The checkout this module's own file lives in (``<root>/src/localagents/db.py``): the
    code that is actually running. None when ``<root>`` holds no pyproject.toml (a wheel in
    site-packages)."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() else None


def _pyproject_version(root: Path) -> str | None:
    import tomllib

    try:
        with open(root / "pyproject.toml", "rb") as f:
            project = tomllib.load(f).get("project") or {}
        if project.get("name") != "localagents":
            return None
        v = project.get("version")
        return v if isinstance(v, str) and v else None
    except Exception:  # noqa: BLE001
        return None


def _git_short_sha(root: Path) -> str | None:
    import subprocess

    try:
        p = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=BUILD_STAMP_GIT_TIMEOUT_S, stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001  no git, a timeout, an unreadable directory: no sha
        return None
    sha = (p.stdout or "").strip()
    if p.returncode != 0 or not sha or any(c.isspace() for c in sha):
        return None
    return sha


def build_stamp(package_root: Path | str | None = None) -> str:
    """``<version>+<short sha>``: which code a process runs (story 1.14). Written once into the
    server row's ``version`` at server start, and computed once by the monitor at its start.

    The version is the installed metadata's when it matches the source tree's pyproject.toml,
    else pyproject's (an editable install's dist-info keeps the version it was installed at;
    the source tree is the code that runs); "dev" when neither says. The sha is the checkout's
    ``git rev-parse --short HEAD`` when the code runs from a checkout (an editable install, or
    a source tree on the path); any failure leaves the version alone. ``package_root`` is the
    checkout (the directory holding pyproject.toml); None finds it — this file's own tree,
    else the editable install's ``direct_url.json``. Never raises; standard library only."""
    import importlib.metadata

    try:
        try:
            meta: str | None = importlib.metadata.version("localagents")
        except Exception:  # noqa: BLE001  PackageNotFoundError, or a broken dist-info
            meta = None
        if package_root is not None:
            root: Path | None = Path(package_root)
        else:
            root = _source_checkout() or _editable_checkout()
        source = _pyproject_version(root) if root is not None and (root / "pyproject.toml").is_file() else None
        version = source or meta or "dev"
        sha = _git_short_sha(root) if root is not None and (root / ".git").exists() else None
        return f"{version}+{sha}" if sha else version
    except Exception:  # noqa: BLE001  the stamp is best effort: a server must start without one
        return "dev"
