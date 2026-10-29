"""In-memory job store with on-disk logs."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import db, derive


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    d = Path(base) / "localagents"
    d.mkdir(parents=True, exist_ok=True)
    return d


# A job directory is named ``<YYYYMMDD>-<HHMMSS>-<hex>``: 4 hex under the old ids, 8 hex now
# (Job.name, see JobStore.new); prune touches nothing else.
_JOB_DIR = re.compile(r"^(\d{8}-\d{6})-[0-9a-f]{4,}$")


def prune(days: int, now: float | None = None, dry_run: bool = False) -> list[Path]:
    """Delete job directories whose id dates them more than ``days`` ago; returns what was (or
    with ``dry_run`` would be) removed, oldest first.

    The date in the directory name decides, never mtime, and a name that is not a job id is left
    alone whatever its age. ``days <= 0`` disables pruning. A job can run for at most about 28
    hours, so any sane cutoff is past every live job of every server on the machine.
    """
    if days <= 0:
        return []
    cutoff = (now if now is not None else time.time()) - days * 86400
    root = state_dir() / "jobs"
    out: list[Path] = []
    for p in sorted(root.iterdir()) if root.is_dir() else []:
        m = _JOB_DIR.match(p.name)
        if not m or not p.is_dir():
            continue
        try:
            stamp = time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S"))
        except ValueError:
            continue
        if stamp < cutoff:
            out.append(p)
            if not dry_run:
                shutil.rmtree(p, ignore_errors=True)
    return out


# How often a blocking wait sends a progress notification (well under Claude Code's idle timeouts).
PROGRESS_INTERVAL_S = 60.0


@dataclass
class Job:
    id: str  # uuid4 (db.new_uuid): the job_id every tool takes, including resume_job
    name: str  # db.job_name(created_at, id): the job directory and worktree branch name
    task: str
    model: str
    endpoint: str
    base_url: str
    cwd: str
    run_cwd: str
    isolation: str
    status: str = "queued"  # queued|running|succeeded|failed|cancelled|timeout
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    session_id: str | None = None
    result: str | None = None
    error: str | None = None
    num_turns: int = 0
    worktree: dict[str, Any] | None = None
    last_activity: str = ""
    log_dir: str = ""
    dump: bool = False
    log_events: bool = False  # write events.jsonl (every SDK message); debugging only
    context: int | None = None  # backend context window handed to the session
    metrics: dict[str, Any] | None = None  # llama.cpp /metrics delta over the job (endpoint-wide)
    # usage, tool_calls, files_touched, ctx_tokens, gen_tps were the duplicated values story
    # 1.5b removed: the rows carry them, summary()/derive compute them at read time.
    # database identity (set by JobStore.new; NULL-ish defaults so a Job built directly still works)
    mcp_server_id: str = ""
    endpoint_id: int | None = None
    model_id: int | None = None
    # runtime-only
    _task: asyncio.Task | None = field(default=None, repr=False)
    _done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _conn: sqlite3.Connection | None = field(default=None, repr=False)  # the store's db connection
    _pending_tools: dict[str, int] = field(default_factory=dict, repr=False)  # tool_use_id -> rowid of its tool_calls row (spec write rule)

    # metrics columns written from Job.metrics; the first three decide the NULL-on-reset rule
    _METRIC_COUNTERS = ("generated_tokens", "prompt_tokens_processed", "prompt_tokens_cached")
    _METRIC_COLUMNS = _METRIC_COUNTERS + ("cache_hit_ratio", "prompt_tps", "generate_tps", "spec_decode_acceptance")
    # storage details: summary()/job_status shows the job, and name identifies it; the id columns
    # resolve to model/endpoint/base_url there (DeepSeek review of 1.2)
    _SUMMARY_HIDDEN = ("mcp_server_id", "endpoint_id", "model_id")

    def changed(self) -> None:
        """This job's visible state moved: write the new state to its row. The status line reads
        the table, so there is nothing else to notify."""
        self._row_update()

    def _row_update(self) -> None:
        if self._conn is None:
            return  # a Job built outside JobStore.new has no row
        if self.metrics is None or any(
            isinstance(self.metrics.get(k), (int, float)) and self.metrics[k] < 0 for k in self._METRIC_COUNTERS
        ):
            met: list[Any] = [None] * len(self._METRIC_COLUMNS)  # spec: a reset counter nulls the whole set
        else:
            met = [self.metrics.get(k) for k in self._METRIC_COLUMNS]
        try:
            with db.immediate(self._conn):
                self._conn.execute(
                    """UPDATE jobs SET status = ?, started_at = ?, finished_at = ?, updated = ?,
                       session_id = ?, result = ?, error = ?, last_activity = ?, num_turns = ?,
                       context = ?, generated_tokens = ?, prompt_tokens_processed = ?,
                       prompt_tokens_cached = ?, cache_hit_ratio = ?, prompt_tps = ?,
                       generate_tps = ?, spec_decode_acceptance = ?, worktree = ?
                       WHERE id = ?""",
                    (self.status, self.started_at, self.finished_at, time.time(), self.session_id,
                     self.result, self.error, self.last_activity, self.num_turns, self.context,
                     *met,
                     json.dumps(self.worktree) if self.worktree is not None else None,
                     self.id),
                )
        except sqlite3.Error as e:
            # the database observes the job; a locked or broken db must never fail the job itself
            self.log_text(f"[db-error] job row update: {e}")

    def record_tool_call(self, t: float, tool: str, tool_use_id: str, input_text: str | None) -> None:
        """One ``tool_calls`` row per ToolUseBlock (spec: input is the path for edit tools, else
        the first 120 chars of the input JSON). Remembers the rowid so the matching result can
        set ``finished``/``is_error`` by rowid — no index on ``tool_use_id`` needed."""
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
            self.log_text(f"[db-error] tool call insert: {e}")

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
            self.log_text(f"[db-error] tool result update: {e}")

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
            self.log_text(f"[db-error] record_request: {e}")

    @property
    def done(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled", "timeout", "lost")

    def summary(self, include_result: bool = True) -> dict[str, Any]:
        d = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if not f.name.startswith("_") and f.name not in self._SUMMARY_HIDDEN
        }
        d["elapsed_s"] = round((self.finished_at or time.time()) - (self.started_at or self.created_at), 1)
        d["task"] = self.task if len(self.task) < 300 else self.task[:300] + "…"
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
        else:
            d["usage"] = None
            d["tool_calls"] = 0
            d["tool_counts"] = {}
            d["errors"] = 0
            d["last_tool"] = None
            d["files_touched"] = []
            d["ctx_tokens"] = None
            d["gen_tps"] = None
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
        p = Path(self.log_dir) / "transcript.txt"
        if not p.exists():
            return ""
        lines = p.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n_lines:])


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
        try:
            version = importlib.metadata.version("localagents")
        except importlib.metadata.PackageNotFoundError:  # running from a source tree without metadata
            version = "dev"
        with db.immediate(conn):
            # updated = started: a row with no heartbeat yet must still count as live for its
            # first 60 s (spec Liveness); NULL would make it dead the moment it is written.
            started = time.time()
            conn.execute(
                "INSERT INTO mcp_servers (id, pid, owner, started, updated, cwd, version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (server_id, os.getpid(), os.getppid(), started, started, os.getcwd(), version),
            )
        self._conn, self._server_id = conn, server_id

    # backend and host are required keywords: the endpoints row is keyed on all four values, and
    # a forgotten call site must fail here, not write a wrong endpoints row (DeepSeek review of 1.2)
    def new(self, *, backend: str, host: str, **kw: Any) -> Job:
        jid = db.new_uuid()
        created_at = kw.setdefault("created_at", time.time())
        name = db.job_name(created_at, jid)
        log_dir = state_dir() / "jobs" / name
        log_dir.mkdir(parents=True, exist_ok=False)  # a name collision would be a minting bug; don't hide it
        job = Job(id=jid, name=name, log_dir=str(log_dir), **kw)
        job.mcp_server_id = self.server_id
        self.jobs[jid] = job
        job._conn = self.conn
        # the task itself is the jobs.task column now (task.txt is dropped)
        with db.immediate(self.conn):
            endpoint_id = db.lookup_id(
                self.conn, "endpoints",
                name=job.endpoint, base_url=job.base_url, backend=backend, host=host,
            )
            model_id = db.lookup_id(self.conn, "models", served_id=job.model)
            job.endpoint_id, job.model_id = endpoint_id, model_id
            self.conn.execute(
                """INSERT INTO jobs (id, name, mcp_server_id, endpoint_id, model_id, task, cwd,
                                     isolation, status, created_at, updated, dump, log_events, worktree,
                                     context, num_turns, last_activity)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (jid, name, self.server_id, endpoint_id, model_id, job.task, job.cwd, job.isolation,
                 job.status, created_at, time.time(), 1 if job.dump else 0, 1 if job.log_events else 0,
                 json.dumps(job.worktree) if job.worktree is not None else None,
                 job.context, job.num_turns, job.last_activity),
            )
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
        """The spec's startup rule: a job whose server row is not live (no heartbeat for 60 s,
        or ``stopped`` set) will never finish: mark it ``lost``. This is the only status write to
        another server's jobs; this server's own row was written/heartbeated moments ago, so the
        fresh ``updated`` keeps its jobs out of the sweep."""
        now = now if now is not None else time.time()
        with db.immediate(self.conn):
            cur = self.conn.execute(
                """UPDATE jobs SET status = 'lost', error = 'server exited', finished_at = ?, updated = ?
                   WHERE status IN ('queued', 'running')
                     AND mcp_server_id IN (
                       SELECT id FROM mcp_servers WHERE stopped IS NOT NULL OR updated IS NULL OR updated < ?
                     )""",
                (now, now, now - 60),
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
            # names (newest first), not uuids: they are what a caller can actually match against
            # list_jobs output or a job directory (DeepSeek review of 1.2)
            newest = [j.name for j in sorted(self.jobs.values(), key=lambda j: j.created_at)][-10:][::-1]
            raise KeyError(f"unknown job '{jid}'. Known (newest first): {newest}")
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
        satisfied = (lambda: all(j.done for j in jobs)) if mode == "all" else (lambda: any(j.done for j in jobs))
        deadline = time.monotonic() + timeout_s
        while jobs and not satisfied():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            step = min(remaining, interval_s) if progress else remaining
            pending = [asyncio.ensure_future(j._done.wait()) for j in jobs if not j.done]
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
        while not job.done:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            step = min(remaining, interval_s) if progress else remaining
            try:
                await asyncio.wait_for(job._done.wait(), timeout=step)
            except asyncio.TimeoutError:
                if progress and not job.done and time.monotonic() < deadline:
                    await progress(job)
        return job
