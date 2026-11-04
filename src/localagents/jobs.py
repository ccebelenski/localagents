"""In-memory job store with on-disk logs."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import db, derive
from .db import state_dir  # re-exported: jobs.state_dir is the long-standing import (3.10f-1)


# A job directory is named by the job's id (a uuid, since 3.10f-1) or by the old
# ``<YYYYMMDD>-<HHMMSS>-<hex>`` stamp (4 hex under the very old ids, 8 under the retired name);
# prune touches nothing else.
_JOB_DIR = re.compile(r"^(\d{8}-\d{6})-[0-9a-f]{4,}$")
_UUID_DIR = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")  # a structural uuid4, not any 36 hex-or-dash chars (review of 3.10f-1, O4)


def _newest_mtime(p: Path) -> float:
    """When anything in the directory was last written: the newest mtime of the directory
    itself and its direct children (the transcript, a debug dump). A child that cannot be
    stat'ed is skipped; a directory that cannot be stat'ed propagates its OSError."""
    newest = p.stat().st_mtime
    for child in p.iterdir():
        try:
            newest = max(newest, child.stat().st_mtime)
        except OSError:
            continue
    return newest


def prune(days: int, now: float | None = None, dry_run: bool = False) -> list[Path]:
    """Delete job directories with nothing written for more than ``days``; returns what was (or
    with ``dry_run`` would be) removed.

    A directory is a job's when its name is a uuid (the job's id, the one key since 3.10f-1) or
    the old ``YYYYMMDD-HHMMSS-<hex>`` stamp; a name that is neither is left alone whatever its
    age, and so is a plain file. **The age is mtime**, the newest among the directory itself
    and its direct children: the age a prune wants is how long since the last write, and the
    old name's stamp was only a proxy for the directory's creation time (ruling (n) of the 3.10
    discussion — the files have real timestamps). A running job keeps writing its transcript,
    so it never reaches any sane cutoff; a finished job that wrote nothing at all is stamped by
    the directory's creation, which is when the job started.

    The trap: anything outside the job that touches its directory rejuvenates it — opening and
    saving a transcript in an editor, a backup tool that rewrites mtimes, a recursive walk that
    updates atimes on a filesystem mounted with ``noatime`` off and ``strictatime`` on. Prune
    trusts mtime; the operator's own reads are not free.

    ``days <= 0`` disables pruning (a job can run for at most about 28 hours, so any sane cutoff
    is past every live job of every server on the machine)."""
    if days <= 0:
        return []
    cutoff = (now if now is not None else time.time()) - days * 86400
    root = state_dir() / "jobs"
    out: list[Path] = []
    for p in sorted(root.iterdir()) if root.is_dir() else []:
        if not p.is_dir() or not (_JOB_DIR.match(p.name) or _UUID_DIR.match(p.name)):
            continue
        try:
            written = _newest_mtime(p)
        except OSError:  # a directory this process cannot stat is not proven old
            continue
        if written < cutoff:
            out.append(p)
            if not dry_run:
                shutil.rmtree(p, ignore_errors=True)
    return out


# How often a blocking wait sends a progress notification (well under Claude Code's idle timeouts).
PROGRESS_INTERVAL_S = 60.0

# The statuses that mean the job is over: Job.done reads it, and so do the server's row reads
# (server._TERMINAL_STATUSES is this tuple) — one list, so the two can never disagree.
# ``dropped`` (router 11, design §7 "Accepted"): the job left the queue without a session; its
# ``error`` names the cause and ``started_at`` stays null.
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "timeout", "lost", "dropped")

# A server's heartbeat (``mcp_servers.updated``) is fresh this long: the spec's Liveness window.
# ``mark_lost_jobs`` reads it, and so does the router queue's stale-owner rule (design §7: "the
# store's liveness window (the one mark_lost_jobs uses)"), which imports it rather than keep a
# second 60. status.collect and monitor.read keep their own copies of the same number.
SERVER_LIVE_S = 60.0


def oldest_first(job: "Job") -> tuple[bool, float]:
    """Sort key for in-process jobs, oldest first: a None created_at is the oldest, as a NULL
    created_at is in the row listing (review of 1.8, finding 7)."""
    return (job.created_at is not None, job.created_at or 0.0)


@dataclass
class Job:
    id: str  # uuid4 (db.new_uuid): the job_id every tool takes, and the job directory's name
    task: str
    model: str
    endpoint: str
    base_url: str
    cwd: str
    run_cwd: str
    isolation: str
    status: str = "queued"  # queued|running|succeeded|failed|cancelled|timeout|lost|dropped
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    session_id: str | None = None
    result: str | None = None
    error: str | None = None
    num_turns: int = 0
    compactions: int = 0  # the harness's compact_boundary messages (story 1.11)
    worktree: dict[str, Any] | None = None
    last_activity: str = ""
    log_dir: str = ""
    dump: bool = False
    log_events: bool = False  # write events.jsonl (every SDK message); debugging only
    context: int | None = None  # backend context window handed to the session
    metrics: dict[str, Any] | None = None  # llama.cpp /metrics delta over the job (endpoint-wide)
    # router 1 (router-design spec §13): priority comes from the caller, never from the brief's
    # scoring (§5: priority is not a dimension); calibration marks a calibration probe job;
    # gates is the gate feed's verdicts, NULL until the router's build writes one.
    priority: str = "normal"  # urgent|normal|background
    calibration: bool = False
    gates: dict[str, Any] | None = None
    # router 12 (design §4 "Consumer requirement"): {"router": <entry name>, "reason": <the one
    # line>, "pool": <pool name or None>} for a job the router placed; None for a named job.
    # The job's model/endpoint/base_url are always the real row's, never the router entry's.
    routed_by: dict[str, Any] | None = None
    # usage, tool_calls, files_touched, ctx_tokens, gen_tps were the duplicated values story
    # 1.5b removed: the rows carry them, summary()/derive compute them at read time.
    # database identity (set by JobStore.new; NULL-ish defaults so a Job built directly still works)
    mcp_server_id: str = ""
    endpoint_id: int | None = None
    model_id: int | None = None
    # runtime-only
    # router 9d: the caller's thinking/effort levers, read by the shim on every /v1/messages
    # request; not a column (the requests row notes what was applied) and not in summary()
    levers: dict[str, Any] | None = None
    _task: asyncio.Task | None = field(default=None, repr=False)
    # router 12: set once the job's queue entry is admitted (the blocking wait's clock starts
    # there, design §7 "Accepted"); the routing context a queued routed job's wakes re-apply;
    # the latest expected wait a wake computed; the last progress moment a stall notice named
    _admitted: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _route: Any = field(default=None, repr=False)
    _queue_estimate: float | None = field(default=None, repr=False)
    _stall_seen: float | None = field(default=None, repr=False)
    _done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _conn: sqlite3.Connection | None = field(default=None, repr=False)  # the store's db connection
    _pending_tools: dict[str, int] = field(default_factory=dict, repr=False)  # tool_use_id -> rowid of its tool_calls row (spec write rule)

    # metrics columns written from Job.metrics (derive's one list, which reads them back); the
    # first three decide the NULL-on-reset rule
    _METRIC_COLUMNS = derive._METRICS_COLUMNS
    _METRIC_COUNTERS = _METRIC_COLUMNS[:3]  # generated_tokens, prompt_tokens_processed, prompt_tokens_cached
    # storage details: summary()/job_status shows the job, and the id identifies it; the id
    # columns resolve to model/endpoint/base_url there (DeepSeek review of 1.2). gates stays
    # hidden (the scorecard's evidence, not the job's state) and levers is runtime-only.
    # priority, calibration and routed_by are shown since router 12 (the phase-2 deferral:
    # derive.summary_from_row reads them too, so the parity contract
    # test_summary_and_summary_from_row_agree pins still holds).
    _SUMMARY_HIDDEN = ("mcp_server_id", "endpoint_id", "model_id", "gates", "levers")

    def changed(self) -> None:
        """This job's visible state moved: write the new state to its row. The status line reads
        the table, so there is nothing else to notify."""
        self._row_update()

    def _metric_values(self) -> list[Any]:
        """The seven metrics column values, in _METRIC_COLUMNS order: a key metrics_delta omitted
        is None, and a reset counter nulls the whole set (spec). The row and summary() both use
        this, so in-process and row-derived summaries agree.

        ``metrics_delta`` keeps fractional deltas (ruling A of the 2.4 fold); the three counters
        are INTEGER columns in the schema (``db._V1``), so they are rounded here, while the four
        REAL columns (ratios and t/s) keep their fraction."""
        if self.metrics is None or any(
            isinstance(self.metrics.get(k), (int, float)) and self.metrics[k] < 0 for k in self._METRIC_COUNTERS
        ):
            return [None] * len(self._METRIC_COLUMNS)
        out = [self.metrics.get(k) for k in self._METRIC_COLUMNS]
        for i, k in enumerate(self._METRIC_COLUMNS):
            if k in self._METRIC_COUNTERS and out[i] is not None:
                out[i] = int(round(out[i]))
        return out

    def _row_update(self) -> None:
        if self._conn is None:
            return  # a Job built outside JobStore.new has no row
        met = self._metric_values()
        try:
            with db.immediate(self._conn):
                self._conn.execute(
                    """UPDATE jobs SET status = ?, started_at = ?, finished_at = ?, updated = ?,
                       session_id = ?, result = ?, error = ?, last_activity = ?, num_turns = ?,
                       compactions = ?, context = ?, generated_tokens = ?,
                       prompt_tokens_processed = ?,
                       prompt_tokens_cached = ?, cache_hit_ratio = ?, prompt_tps = ?,
                       generate_tps = ?, spec_decode_acceptance = ?, worktree = ?, gates = ?,
                       routed_by = ?
                       WHERE id = ?""",
                    (self.status, self.started_at, self.finished_at, time.time(), self.session_id,
                     self.result, self.error, self.last_activity, self.num_turns,
                     self.compactions, self.context,
                     *met,
                     json.dumps(self.worktree) if self.worktree is not None else None,
                     json.dumps(self.gates) if self.gates is not None else None,
                     json.dumps(self.routed_by) if self.routed_by is not None else None,
                     self.id),
                )
        except sqlite3.Error as e:
            # the database observes the job; a locked or broken db must never fail the job itself
            self._log_safe(f"[db-error] job row update: {e}")

    def record_tool_call(self, t: float, tool: str, tool_use_id: str, input_text: str | None) -> None:
        """One ``tool_calls`` row per ToolUseBlock. The ``input_text`` follows the current input
        rule (spec; runner._apply_message builds it): an edit tool stores the path, or NULL when
        the input has none; a tool whose input carries a string ``command`` (Bash) stores that
        command, first 120 chars; any other tool stores the first 120 chars of the input JSON.
        Remembers the rowid so the matching result can set ``finished``/``is_error`` by rowid —
        no index on ``tool_use_id`` needed."""
        if self._conn is None:
            return  # a Job built outside JobStore.new has no rows to write
        try:
            with db.immediate(self._conn):
                cur = self._conn.execute(
                    "INSERT INTO tool_calls (job_id, t, tool, tool_use_id, input, is_error, finished)"
                    " VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                    (self.id, t, tool, tool_use_id, input_text),
                )
                self._pending_tools[tool_use_id] = cur.lastrowid
        except sqlite3.Error as e:
            self._log_safe(f"[db-error] tool call insert: {e}")

    def record_tool_result(self, tool_use_id: str, t: float, is_error: bool | None) -> None:
        """Set ``finished``/``is_error`` on the call's row by its remembered rowid. A result
        whose call we never saw (or whose insert failed) is not an error: it is ignored."""
        rowid = self._pending_tools.pop(tool_use_id, None)
        if rowid is None or self._conn is None:
            return
        try:
            with db.immediate(self._conn):
                self._conn.execute(
                    "UPDATE tool_calls SET finished = ?, is_error = ? WHERE rowid = ?",
                    (t, 1 if is_error else 0, rowid),
                )
        except sqlite3.Error as e:
            self._log_safe(f"[db-error] tool result update: {e}")

    def record_request(self, rec: dict[str, Any]) -> None:
        """Write one shim request record to the ``requests`` table (shim._log builds ``rec``).

        Column mapping: docs/superpowers/specs/2026-10-29-mcp-sqlite-schema.md. ``model`` is not
        stored: the request's model is the job's. The record's token keys are the API's names."""
        if self._conn is None:
            return  # a Job built outside JobStore.new has no row to reference
        usage = rec.get("usage") or {}
        try:
            with db.immediate(self._conn):
                self._conn.execute(
                    """INSERT INTO requests (job_id, path_id, t, http_status, ms, gen_ms, stream,
                       input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                       messages, tools, bytes, moved_system, folded_text, context_exceeded, error)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (self.id, db.lookup_id(self._conn, "paths", path=rec["path"]), rec["t"],
                     rec.get("status"), rec.get("ms"), rec.get("gen_ms"),
                     (1 if rec["stream"] else 0) if "stream" in rec else None,
                     usage.get("input_tokens"), usage.get("output_tokens"),
                     usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"),
                     rec.get("messages"), rec.get("tools"), rec.get("bytes"),
                     rec.get("moved_system_messages"), rec.get("folded_tool_result_text"),
                     rec.get("context_exceeded"), rec.get("error")),
                )
        except sqlite3.Error as e:
            # same rule as _row_update: the database observes the job and must never fail it
            self._log_safe(f"[db-error] record_request: {e}")

    def record_transcript(self) -> None:
        """Story 1.12: copy the transcript file's text into the ``transcripts`` row, once, at
        completion (run_job's finally, after the last transcript line and before the row turns
        terminal). INSERT OR REPLACE: a re-finish replaces rather than duplicates. A job with no
        transcript file gets no row — the file is what exists, and readers then say so. Any other
        failure, read or write, is one ``[db-error]`` line and nothing else: same rule as
        record_tool_result — the database observes the job and must never fail it or move its
        status. Not just OSError/sqlite3.Error: the finally sits between two shielded steps, so
        an escaping MemoryError or bug would strand the row `running` for good."""
        if self._conn is None:
            return  # a Job built outside JobStore.new has no row to reference
        try:
            text = (Path(self.log_dir) / "transcript.txt").read_text(errors="replace")
        except OSError:
            return  # no file to record: the readers' note says the file is gone
        except Exception as e:  # noqa: BLE001
            self._log_safe(f"[db-error] transcript: {type(e).__name__}: {e}")
            return
        try:
            with db.immediate(self._conn):
                self._conn.execute(
                    "INSERT OR REPLACE INTO transcripts (job_id, text, bytes, written) VALUES (?, ?, ?, ?)",
                    (self.id, text, len(text.encode()), time.time()),
                )
        except Exception as e:  # noqa: BLE001
            self._log_safe(f"[db-error] transcript: {type(e).__name__}: {e}")

    def _log_safe(self, text: str) -> None:
        """A transcript line that must not raise: a failing write (ENOSPC, a read-only state
        directory) falls back to stderr. The one copy (3.11a row 2): the runner's finally
        steps, the server's shim_port line and this store's own error lines all call it."""
        try:
            self.log_text(text)
        except Exception as e:  # noqa: BLE001
            print(f"localagents: job {self.id}: {text} (transcript write failed: {e})", file=sys.stderr)

    @property
    def done(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def settled(self) -> bool:
        """run_job's finally has finished: the worktree is finished, the row is final, the record
        complete. ``done`` turns true earlier (the status is set before the finally awaits
        finish_worktree in a thread), so a wait that hands back the record waits on this
        (review of 1.7, finding 1)."""
        return self._done.is_set()

    def summary(self, include_result: bool = True) -> dict[str, Any]:
        d = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if not f.name.startswith("_") and f.name not in self._SUMMARY_HIDDEN
        }
        if d["worktree"] is not None:  # a copy: the caller must not hold the job's own dict (story 1.9 item 1)
            d["worktree"] = dict(d["worktree"])
        if d["routed_by"] is not None:  # the same copy rule (router 12)
            d["routed_by"] = dict(d["routed_by"])
        start = self.started_at or self.created_at  # None only for a hand-built Job: 0, as summary_from_row
        d["elapsed_s"] = round((self.finished_at or time.time()) - start, 1) if start is not None else 0
        d["task"] = derive._truncate(self.task)  # the row summary's own cut
        met = self._metric_values()  # the row's rule, as derive.summary_from_row reads it back
        d["metrics"] = None if all(v is None for v in met) else dict(zip(self._METRIC_COLUMNS, met))
        if not include_result:
            d.pop("result", None)
        # the duplicated values come from the rows (spec "Derivations"); a Job built outside
        # JobStore.new has no rows: the same keys, empty. derive.summary_from_row builds these
        # same keys for a job read straight from the database.
        conn = self._conn
        if conn is not None:
            d["usage"] = derive.usage_totals(conn, self.id)
            d["tool_calls"] = derive.tool_call_count(conn, self.id)
            d["tool_counts"] = derive.tool_counts(conn, self.id)
            d["errors"] = derive.error_count(conn, self.id)
            d["last_tool"] = derive.last_tool(conn, self.id)
            d["files_touched"] = derive.files_touched(conn, self.id)
            d["ctx_tokens"] = derive.ctx_tokens(conn, self.id)
            d["gen_tps"] = derive.gen_tps(conn, self.id)
            d["prefill_tps"] = derive.prefill_tps(conn, self.id)
            d["server_version"] = derive.server_version(conn, self.mcp_server_id)
        else:
            d["usage"] = None
            d["tool_calls"] = 0
            d["tool_counts"] = {}
            d["errors"] = 0
            d["last_tool"] = None
            d["files_touched"] = []
            d["ctx_tokens"] = None
            d["gen_tps"] = None
            d["prefill_tps"] = None
            d["server_version"] = None
        return d

    # ----- logging -----
    def log_event(self, obj: Any) -> None:
        """Raw SDK message, one JSON line each. Off unless ``log_events`` (debugging: it carries
        every tool result and was 98% of a 6 GB state directory)."""
        if not self.log_events:
            return
        with open(Path(self.log_dir) / "events.jsonl", "a") as f:
            f.write(json.dumps(obj, default=str) + "\n")

    def log_text(self, text: str) -> None:
        with open(Path(self.log_dir) / "transcript.txt", "a") as f:
            f.write(text.rstrip("\n") + "\n")

    def transcript_tail(self, n_lines: int = 80) -> str:
        """The last ``n_lines`` lines; 0 is the whole transcript (job_log's contract,
        story 1.9 item 7 — stated here too so it is never the ``lines[-0:]`` accident).

        Story 1.12 read order: the ``transcripts`` row when present (the finished job: the text
        as of completion, surviving the file's retention), else the file (a running job, or a
        job from before this change); "" when neither exists. A database failure propagates —
        the readers answer database_error, as they do for their other row reads."""
        if self._conn is not None:
            row = self._conn.execute("SELECT text FROM transcripts WHERE job_id = ?", (self.id,)).fetchone()
            if row is not None:
                lines = row["text"].splitlines()
                return "\n".join(lines[-n_lines:] if n_lines else lines)
        p = Path(self.log_dir) / "transcript.txt"
        if not p.exists():
            return ""
        lines = p.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n_lines:] if n_lines else lines)


class JobStore:
    # describable by describe_server(): server attributes the status line needs that are not known
    # at process start. Names never reach SQL unchecked; pid/owner/started are set by _open alone.
    _SERVER_COLS = ("config", "transport", "shim_port")

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._conn: sqlite3.Connection | None = None
        self._server_id: str | None = None

    # The database is opened on first use, not at construction: server.py builds the store at
    # import time, and importing the server (tests, `localagents --help`) must not create or
    # write the real database.
    @property
    def conn(self) -> sqlite3.Connection:
        self._open()
        return self._conn  # type: ignore[return-value]

    @property
    def server_id(self) -> str:
        self._open()
        return self._server_id  # type: ignore[return-value]

    def _open(self) -> None:
        if self._conn is not None:
            return
        # One row per process (spec: docs/superpowers/specs/2026-10-29-mcp-sqlite-schema.md).
        # config/transport/shim_port are filled by describe_server(), updated beats by
        # heartbeat(), stopped by close(); the story-1.5a sampler and status line drive them.
        conn = db.open_db()
        server_id = db.new_uuid()
        # story 1.14: the build stamp (<version>+<short sha>), so a job run on stale code says
        # so; written once, at the row's birth — the code a process runs never changes under it
        version = db.build_stamp()
        with db.immediate(conn):
            # updated = started: a row with no heartbeat yet must still count as live for its
            # first 60 s (spec Liveness); NULL would make it dead the moment it is written.
            started = time.time()
            # owner: the Claude Code session this server runs under (the same walk the status
            # line does), so its jobs match their session on the line; the parent pid when no
            # claude ancestor exists (story 1.9 item 9)
            conn.execute(
                "INSERT INTO mcp_servers (id, pid, owner, started, updated, cwd, version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (server_id, os.getpid(), db.session_pid() or os.getppid(), started, started, os.getcwd(), version),
            )
        self._conn, self._server_id = conn, server_id

    # backend and host are required keywords: the endpoints row is keyed on all four values, and
    # a forgotten call site must fail here, not write a wrong endpoints row (DeepSeek review of 1.2)
    def new(self, *, backend: str, host: str,
            on_insert: Callable[[sqlite3.Connection, Job], None] | None = None, **kw: Any) -> Job:
        """Record one job. ``on_insert`` (router 12) runs inside the same transaction, after the
        jobs row is written: the queue entry and a routed job's ticket go in with the job or not
        at all, so no job row can exist without its entry (a job without its entry would be
        counted by nothing and admitted by nothing)."""
        jid = db.new_uuid()
        created_at = kw.setdefault("created_at", time.time())
        # open the database (and this server's row) before anything is made on disk: a failed
        # first open must leave no job directory behind (review of 1.7, finding 7)
        server_id, conn = self.server_id, self.conn
        log_dir = state_dir() / "jobs" / jid
        log_dir.mkdir(parents=True, exist_ok=False)  # an id collision would be a minting bug; don't hide it
        # the task itself is the jobs.task column now (task.txt is dropped)
        try:
            job = Job(id=jid, log_dir=str(log_dir), **kw)
            job.mcp_server_id = server_id
            self.jobs[jid] = job
            job._conn = conn
            with db.immediate(self.conn):
                endpoint_id = db.lookup_id(
                    self.conn, "endpoints",
                    name=job.endpoint, base_url=job.base_url, backend=backend, host=host,
                )
                model_id = db.lookup_id(self.conn, "models", served_id=job.model)
                job.endpoint_id, job.model_id = endpoint_id, model_id
                self.conn.execute(
                    """INSERT INTO jobs (id, mcp_server_id, endpoint_id, model_id, task, cwd,
                                         isolation, status, created_at, updated, dump, log_events, worktree,
                                         context, num_turns, last_activity, priority, calibration,
                                         routed_by)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (jid, self.server_id, endpoint_id, model_id, job.task, job.cwd, job.isolation,
                     job.status, created_at, time.time(), 1 if job.dump else 0, 1 if job.log_events else 0,
                     json.dumps(job.worktree) if job.worktree is not None else None,
                     job.context, job.num_turns, job.last_activity,
                     job.priority, 1 if job.calibration else 0,
                     json.dumps(job.routed_by) if job.routed_by is not None else None),
                )
                if on_insert is not None:
                    on_insert(self.conn, job)
        except BaseException:
            # the job cannot be recorded, so the call fails loudly — but cleanly (story 1.7
            # item 5): no orphan directory, and no in-memory job the database never saw. Any
            # error, not only sqlite's: a TypeError from the arguments orphans the same way.
            shutil.rmtree(log_dir, ignore_errors=True)
            self.jobs.pop(jid, None)
            raise
        return job

    # ----- server-row lifecycle (story 1.5a; spec Liveness) -----

    def describe_server(self, **cols: Any) -> None:
        """Update this server's row with any of config, transport, shim_port (only the keys given)."""
        if not cols:
            return
        bad = set(cols) - set(self._SERVER_COLS)
        if bad:
            raise ValueError(f"describable server columns are {', '.join(self._SERVER_COLS)}; got {', '.join(sorted(bad))}")
        with db.immediate(self.conn):
            self.conn.execute(
                f"UPDATE mcp_servers SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                (*cols.values(), self.server_id),
            )

    def heartbeat(self) -> None:
        """Move this row's ``updated`` to now: the liveness signal the status line and the
        ``lost`` rule read. Runs every LOAD_SAMPLE_S on the asyncio thread (prune's thread must
        not touch this connection)."""
        with db.immediate(self.conn):
            self.conn.execute("UPDATE mcp_servers SET updated = ? WHERE id = ?", (time.time(), self.server_id))

    def close(self) -> None:
        """Mark this server's row stopped (clean exit). Idempotent and never raises: it runs at
        exit via atexit, where a locked or broken database must not add noise to the shutdown."""
        if self._conn is None:
            return  # never used the database: there is no row to mark and none to create
        try:
            with db.immediate(self._conn):
                self._conn.execute(
                    "UPDATE mcp_servers SET stopped = ? WHERE id = ? AND stopped IS NULL",
                    (time.time(), self.server_id),
                )
        except sqlite3.Error:
            pass  # a row left unstopped ages out by heartbeat instead

    def mark_lost_jobs(self, now: float | None = None) -> int:
        """The spec's startup rule, heartbeat first and pid second like ``status.collect``: a
        candidate server (``stopped`` set, or no heartbeat for 60 s) is dead — and its unfinished
        jobs become ``lost`` — only when its pid is gone too. A stale heartbeat with a live pid is
        a stalled server (a loop wedged on something), not a dead one: sweeping its running jobs
        would lie about work that may still finish. A ``stopped`` row was written by the server
        itself exiting, so its pid is not consulted. This is the only status write to another
        server's jobs (with the worktree note below, the only write of any kind); this server's
        own row was heartbeated moments ago and stays out."""
        now = now if now is not None else time.time()
        with db.immediate(self.conn):
            # candidates are the not-live servers that still have unfinished jobs, never the whole
            # server history: that grows by a row per server start and is never deleted, and an
            # IN list of it hit SQLite's variable limit (review of 1.7, finding 6)
            dead = [
                (now, now, r["id"])
                for r in self.conn.execute(
                    "SELECT DISTINCT s.id, s.pid, s.stopped FROM mcp_servers s"
                    " JOIN jobs j ON j.mcp_server_id = s.id"
                    " WHERE j.status IN ('queued', 'running')"
                    "   AND (s.stopped IS NOT NULL OR s.updated IS NULL OR s.updated < ?)",
                    (now - SERVER_LIVE_S,),
                ).fetchall()
                if r["stopped"] is not None or r["pid"] is None or not db.pid_alive(r["pid"], strict=True)
            ]
            if not dead:
                return 0
            # a worktree finish_worktree never reached (no `kept`) may still be on disk: the lost
            # record says so and names it, every other key kept (review of 1.10, P3). A JSON
            # this code cannot read is another writer's and left as it is (_row_summary reports it).
            notes = []
            for _now, _updated, sid in dead:
                for r in self.conn.execute(
                    "SELECT id, worktree FROM jobs WHERE status IN ('queued', 'running')"
                    " AND mcp_server_id = ? AND worktree IS NOT NULL",
                    (sid,),
                ).fetchall():
                    try:
                        wt = json.loads(r["worktree"])
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(wt, dict) or "kept" in wt:
                        continue
                    where = wt.get("root") or wt.get("path")
                    wt["note"] = "the server exited before finishing the worktree" + (f"; check {where}" if where else "")
                    notes.append((json.dumps(wt), r["id"]))
            if notes:
                self.conn.executemany("UPDATE jobs SET worktree = ? WHERE id = ?", notes)
            cur = self.conn.executemany(
                """UPDATE jobs SET status = 'lost', error = 'server exited', finished_at = ?, updated = ?
                   WHERE status IN ('queued', 'running') AND mcp_server_id = ?""",
                dead,
            )
            return cur.rowcount

    def record_endpoint_load(self, loads: dict[str, str]) -> None:
        """Last-writer-wins compact load text per endpoint (the sampler's; the status line reads
        it back keyed by name). The endpoint row id comes from this store's running jobs — the
        name alone would not identify the row (endpoints is keyed name+base_url+backend+host) and
        the sampler only reports for endpoints that have running jobs anyway."""
        if not loads:
            return
        ids: dict[str, int] = {}
        for j in self.jobs.values():
            if not j.done and j.endpoint_id is not None:
                ids.setdefault(j.endpoint, j.endpoint_id)
        with db.immediate(self.conn):
            for name, text in loads.items():
                eid = ids.get(name)
                if eid is not None:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO endpoint_load (endpoint_id, updated, load) VALUES (?, ?, ?)",
                        (eid, time.time(), text),
                    )

    def get(self, jid: str) -> Job:
        if jid not in self.jobs:
            # the ids, newest first: the id is the job's one handle now, the one list_jobs and a
            # job directory show (3.10f-1, ruling (n))
            if self.jobs:
                newest = sorted(self.jobs.values(), key=oldest_first)[-10:][::-1]
                known = ", ".join(j.id for j in newest)
            else:
                # this server has run nothing, but the shared database holds every other
                # server's jobs and one of them is probably the id the caller meant
                # (review of 1.8). A broken database must not change the unknown_job answer.
                try:
                    rows = self.conn.execute(
                        "SELECT id FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT 10"
                    ).fetchall()
                    known = ", ".join(r["id"] for r in rows)
                except (sqlite3.Error, db.SchemaError):
                    # any failure of the fallback (a newer-schema file raises SchemaError, a
                    # RuntimeError) leaves the plain message (review of 1.8, finding 6)
                    known = ""
            raise KeyError(f"unknown job '{jid}'. Known (newest first): {known or 'none'}")
        return self.jobs[jid]

    async def wait_many(
        self,
        jids: list[str],
        timeout_s: float,
        mode: str = "all",
        progress: Callable[[list[Job]], Awaitable[None]] | None = None,
        interval_s: float | None = None,
    ) -> list[Job]:
        """Block until every job (`mode="all"`) or at least one (`mode="any"`) finishes, or
        `timeout_s` passes. Returns the jobs in the order given. Progress as in `wait`."""
        jobs = [self.get(j) for j in jids]  # raises KeyError before any waiting
        if interval_s is None:
            interval_s = PROGRESS_INTERVAL_S
        # settled, not done: the record is handed back only once run_job's finally has run
        satisfied = (lambda: all(j.settled for j in jobs)) if mode == "all" else (lambda: any(j.settled for j in jobs))
        deadline = time.monotonic() + timeout_s
        while jobs and not satisfied():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            step = min(remaining, interval_s) if progress else remaining
            pending = [asyncio.ensure_future(j._done.wait()) for j in jobs if not j.settled]
            try:
                await asyncio.wait(pending, timeout=step, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for f in pending:
                    f.cancel()
            if progress and not satisfied() and time.monotonic() < deadline:
                await progress(jobs)
        return jobs

    async def wait(
        self,
        jid: str,
        timeout_s: float,
        progress: Callable[[Job], Awaitable[None]] | None = None,
        interval_s: float | None = None,
    ) -> Job:
        """Block until the job finishes or `timeout_s` passes; returns the job either way.

        With `progress`, it is awaited every `interval_s` while the wait continues. Claude Code
        aborts an MCP tool call that stays silent for its idle window (30 min on stdio, 5 min on
        HTTP), so a long wait has to say something now and then; a progress notification counts.
        """
        job = self.get(jid)
        if interval_s is None:
            interval_s = PROGRESS_INTERVAL_S  # read at call time so tests can shorten it
        deadline = time.monotonic() + timeout_s
        while not job.settled:  # not job.done: see Job.settled
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            step = min(remaining, interval_s) if progress else remaining
            try:
                await asyncio.wait_for(job._done.wait(), timeout=step)
            except asyncio.TimeoutError:
                if progress and not job.settled and time.monotonic() < deadline:
                    await progress(job)
        return job
