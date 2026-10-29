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

from . import db


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
    tool_calls: int = 0
    usage: dict[str, Any] | None = None
    files_touched: list[str] = field(default_factory=list)
    worktree: dict[str, Any] | None = None
    last_activity: str = ""
    log_dir: str = ""
    dump: bool = False
    log_events: bool = False  # write events.jsonl (every SDK message); debugging only
    context: int | None = None  # backend context window handed to the session
    ctx_tokens: int | None = None  # prompt size of the last backend request (shim-observed)
    gen_tps: float | None = None  # smoothed output tokens/s of recent backend responses
    metrics: dict[str, Any] | None = None  # llama.cpp /metrics delta over the job (endpoint-wide)
    # database identity (set by JobStore.new; NULL-ish defaults so a Job built directly still works)
    mcp_server_id: str = ""
    endpoint_id: int | None = None
    model_id: int | None = None
    # runtime-only
    _task: asyncio.Task | None = field(default=None, repr=False)
    _done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _on_change: Callable[[], None] | None = field(default=None, repr=False)
    _conn: sqlite3.Connection | None = field(default=None, repr=False)  # the store's db connection

    # metrics columns written from Job.metrics; the first three decide the NULL-on-reset rule
    _METRIC_COUNTERS = ("generated_tokens", "prompt_tokens_processed", "prompt_tokens_cached")
    _METRIC_COLUMNS = _METRIC_COUNTERS + ("cache_hit_ratio", "prompt_tps", "generate_tps", "spec_decode_acceptance")

    def changed(self) -> None:
        """Tell the store this job's visible state moved (it republishes the status file and
        updates the job's row in the database)."""
        if self._on_change:
            self._on_change()
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

    def note_usage(self, usage: dict[str, Any] | None, ms: int, gen_ms: int | None = None) -> None:
        """Record a backend response's usage (called by the shim): live context fill and speed.

        ``gen_ms`` is the time from first streamed byte to end, i.e. generation without prompt
        processing; it is used for the speed figure when known, else the whole request time."""
        if not usage:
            return
        prompt = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if prompt:
            self.ctx_tokens = prompt
        out = int(usage.get("output_tokens") or 0)
        span = gen_ms if gen_ms else ms
        if out and span > 0:
            tps = out / (span / 1000)
            self.gen_tps = tps if self.gen_tps is None else 0.5 * self.gen_tps + 0.5 * tps
        self.changed()

    @property
    def done(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled", "timeout")

    def summary(self, include_result: bool = True) -> dict[str, Any]:
        d = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if not f.name.startswith("_")
        }
        d["elapsed_s"] = round((self.finished_at or time.time()) - (self.started_at or self.created_at), 1)
        d["task"] = self.task if len(self.task) < 300 else self.task[:300] + "…"
        if not include_result:
            d.pop("result", None)
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
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.endpoint_load: dict[str, str] = {}  # endpoint name -> compact load text, while jobs run
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
        # config, transport, shim_port and the updated/stopped timestamps are story 1.5a's: left NULL.
        conn = db.open_db()
        server_id = db.new_uuid()
        try:
            version = importlib.metadata.version("localagents")
        except importlib.metadata.PackageNotFoundError:  # running from a source tree without metadata
            version = "dev"
        with db.immediate(conn):
            conn.execute(
                "INSERT INTO mcp_servers (id, pid, owner, started, cwd, version) VALUES (?, ?, ?, ?, ?, ?)",
                (server_id, os.getpid(), os.getppid(), time.time(), os.getcwd(), version),
            )
        self._conn, self._server_id = conn, server_id

    def new(self, backend: str = "other", host: str = "local", **kw: Any) -> Job:
        jid = db.new_uuid()
        created_at = kw.setdefault("created_at", time.time())
        name = db.job_name(created_at, jid)
        log_dir = state_dir() / "jobs" / name
        log_dir.mkdir(parents=True, exist_ok=False)  # a name collision would be a minting bug; don't hide it
        job = Job(id=jid, name=name, log_dir=str(log_dir), **kw)
        job.mcp_server_id = self.server_id
        self.jobs[jid] = job
        job._on_change = self.publish
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
                                     isolation, status, created_at, updated, dump, log_events, worktree)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (jid, name, self.server_id, endpoint_id, model_id, job.task, job.cwd, job.isolation,
                 job.status, created_at, time.time(), 1 if job.dump else 0, 1 if job.log_events else 0,
                 json.dumps(job.worktree) if job.worktree is not None else None),
            )
        self.publish()
        return job

    def publish(self) -> None:
        """Write the status-line file for this server (see status.py)."""
        from . import status

        status.publish(self.jobs.values(), self.endpoint_load)

    def get(self, jid: str) -> Job:
        if jid not in self.jobs:
            raise KeyError(f"unknown job '{jid}'. Known: {sorted(self.jobs)[-10:]}")
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
