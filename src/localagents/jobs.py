"""In-memory job store with on-disk logs."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    d = Path(base) / "localagents"
    d.mkdir(parents=True, exist_ok=True)
    return d


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
        return job

    def get(self, jid: str) -> Job:
        if jid not in self.jobs:
            raise KeyError(f"unknown job '{jid}'. Known: {sorted(self.jobs)[-10:]}")
        return self.jobs[jid]

    async def wait(self, jid: str, timeout_s: float) -> Job:
        job = self.get(jid)
        try:
            await asyncio.wait_for(job._done.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            pass
        return job
