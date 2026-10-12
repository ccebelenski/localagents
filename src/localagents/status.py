"""Publish job state for the Claude Code status line.

The server writes ``<state>/status/<pid>.json`` whenever a job changes (start, every SDK
message, finish). ``render()`` reads every file whose server is still alive and prints one
line, e.g. ``local: ⟳ Qwen3.8-Flash-Next 1m18s 4 calls · ✓ Muse-Glimmer-30B 3m02s``. The
``localagents statusline`` command runs ``render()`` and nothing else, so it stays fast enough
to run on every status line refresh (no server, MCP or HTTP imports here).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .jobs import state_dir

if TYPE_CHECKING:  # pragma: no cover
    from .jobs import Job

KEEP_FINISHED_S = 120.0  # a finished job stays on the line this long
MAX_ACTIVITY = 60


def status_dir() -> Path:
    d = state_dir() / "status"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(pid: int | None = None) -> Path:
    return status_dir() / f"{pid or os.getpid()}.json"


def short_model(served_id: str) -> str:
    """``unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS`` -> ``Qwen3.8-Flash-Next``."""
    name = served_id.rsplit("/", 1)[-1].split(":", 1)[0]
    for suffix in ("-GGUF", "-gguf", "-EXL3", "-exl3"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name or served_id


def fmt_elapsed(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _entry(job: "Job") -> dict[str, Any]:
    return {
        "id": job.id,
        "model": job.model,
        "endpoint": job.endpoint,
        "status": job.status,
        "started_at": job.started_at or job.created_at,
        "finished_at": job.finished_at,
        "tool_calls": job.tool_calls,
        "num_turns": job.num_turns,
        "files": len(job.files_touched),
        "last_activity": job.last_activity[:MAX_ACTIVITY],
    }


def publish(jobs: Iterable["Job"], now: float | None = None) -> None:
    """Write this server's status file atomically. Never raises: the status line is a convenience."""
    now = now or time.time()
    try:
        entries = [
            _entry(j) for j in jobs
            if not j.done or (j.finished_at and now - j.finished_at < KEEP_FINISHED_S)
        ]
        p = _path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "updated": now, "jobs": entries}))
        os.replace(tmp, p)
    except OSError:
        pass


def remove() -> None:
    """Delete this server's status file (registered at exit; a dead pid is also skipped on read)."""
    try:
        _path().unlink(missing_ok=True)
    except OSError:
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def collect(now: float | None = None) -> list[dict[str, Any]]:
    """Job entries from every live server, oldest first. Files of dead servers are removed."""
    now = now or time.time()
    out: list[dict[str, Any]] = []
    d = status_dir()
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text())
            pid = int(data.get("pid") or p.stem)
        except (OSError, ValueError):
            continue
        if not _alive(pid):
            try:
                p.unlink()
            except OSError:
                pass
            continue
        for e in data.get("jobs", []):
            fin = e.get("finished_at")
            if fin and now - fin >= KEEP_FINISHED_S:
                continue
            out.append(e)
    out.sort(key=lambda e: e.get("started_at") or 0)
    return out


MARK = {"queued": "…", "running": "⟳", "succeeded": "✓", "failed": "✗", "timeout": "⏱", "cancelled": "∅"}


def render_entry(e: dict[str, Any], now: float) -> str:
    end = e.get("finished_at") or now
    bits = [MARK.get(e.get("status", ""), "?"), short_model(e.get("model", "")), fmt_elapsed(end - (e.get("started_at") or now))]
    if e.get("status") == "running":
        if e.get("tool_calls"):
            bits.append(_n(e["tool_calls"], "call"))
        if e.get("files"):
            bits.append(_n(e["files"], "file"))
    elif e.get("status") == "succeeded" and e.get("files"):
        bits.append(_n(e["files"], "file"))
    return " ".join(bits)


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def render(now: float | None = None, prefix: str = "local: ", sep: str = " · ") -> str:
    """One line for the status bar, or an empty string when no live server has jobs to show."""
    now = now or time.time()
    entries = collect(now)
    if not entries:
        return ""
    return prefix + sep.join(render_entry(e, now) for e in entries)
