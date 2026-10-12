"""In-memory job store with on-disk logs."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Awaitable, Callable


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    d = Path(base) / "localagents"
    d.mkdir(parents=True, exist_ok=True)
    return d


# How often a blocking wait sends a progress notification (well under Claude Code's idle timeouts).
PROGRESS_INTERVAL_S = 60.0


@dataclass
class Job:
    id: str
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
    context: int | None = None  # backend context window handed to the session
    metrics: dict[str, Any] | None = None  # llama.cpp /metrics delta over the job (endpoint-wide)
    # runtime-only
    _task: asyncio.Task | None = field(default=None, repr=False)
    _done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _on_change: Callable[[], None] | None = field(default=None, repr=False)

    def changed(self) -> None:
        """Tell the store this job's visible state moved (it republishes the status file)."""
        if self._on_change:
            self._on_change()

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

    def new(self, **kw: Any) -> Job:
        jid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        log_dir = state_dir() / "jobs" / jid
        log_dir.mkdir(parents=True, exist_ok=True)
        job = Job(id=jid, log_dir=str(log_dir), **kw)
        (log_dir / "task.txt").write_text(job.task)
        self.jobs[jid] = job
        job._on_change = self.publish
        self.publish()
        return job

    def publish(self) -> None:
        """Write the status-line file for this server (see status.py)."""
        from . import status

        status.publish(self.jobs.values())

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
