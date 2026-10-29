"""Render the Claude Code status line from the shared SQLite tables (story 1.5a).

Every MCP server keeps its ``mcp_servers`` row alive with a heartbeat and writes its jobs,
requests and tool calls to the database; ``render()`` reads the live servers' jobs and prints
one line, e.g. ``local: ⟳ Qwen3.8-Flash-Next 1m18s 4 calls · ✓ Muse-Glimmer-30B 3m02s``. The
``localagents statusline`` command runs ``render()`` and nothing else, so it stays fast enough
for every status-line refresh: stdlib plus ``db``/``derive`` only, under 100 ms, and it never
creates or fails on the database (no live servers or no database yet prints the empty line).
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import db, derive

KEEP_FINISHED_S = 120.0  # a finished job stays on the line this long
MAX_ACTIVITY = 60


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


def load_summary(probe: dict[str, Any]) -> str | None:
    """Compact endpoint load from a registry probe: ``1/1 slots`` (llama.cpp; strata adds
    ``2 wait`` when requests are queued) or ``2 run 1 wait kv 34%`` (vLLM). None when the probe
    carries neither."""
    slots = probe.get("slots") or {}
    load = probe.get("load") or {}
    if "busy" in slots and "total" in slots:
        out = f"{slots['busy']}/{slots['total']} slots"
        if load.get("requests_waiting"):  # strata reports its queue depth beside the slots
            out += f" {load['requests_waiting']} wait"
        return out
    if "requests_running" in load:
        parts = [f"{load['requests_running']} run"]
        if load.get("requests_waiting"):
            parts.append(f"{load['requests_waiting']} wait")
        if load.get("kv_cache_usage") is not None:
            parts.append(f"kv {round(100 * load['kv_cache_usage'])}%")
        return " ".join(parts)
    return None


def my_owner() -> int | None:
    """Pid of the nearest ``claude`` process above us: the Claude Code session whose status line
    is asking. None when there is none (then every job counts as ours). The walk lives in
    ``db.session_pid`` — the server writes the same answer as its row's ``owner``, so a
    session's own jobs match instead of folding into "+N other" (story 1.9 item 9)."""
    return db.session_pid()


def collect(now: float | None = None, conn: sqlite3.Connection | None = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Job entries from every live server, oldest first, each tagged with its server's ``owner``,
    plus the merged endpoint load map (spec "Access patterns": the status line's budget).

    Live = ``stopped IS NULL`` and a heartbeat within 60 s, checked before the pid because pids
    are reused. Their jobs are queued/running or finished within KEEP_FINISHED_S; the metrics
    are the spec's read-time derivations. Nothing is deleted here: rows are history. A machine
    with no database yet gets empty maps and no file created.
    """
    now = now if now is not None else time.time()
    own_conn = conn is None
    if own_conn:
        if not db.db_path().exists():
            return [], {}
        conn = db.open_db()
    try:
        servers = [
            s for s in conn.execute(
                "SELECT id, pid, owner FROM mcp_servers"
                " WHERE stopped IS NULL AND updated >= ? AND pid IS NOT NULL",
                (now - 60.0,),
            ).fetchall()
            if db.pid_alive(s["pid"])  # heartbeat first, pid second (spec Liveness)
        ]
        out: list[dict[str, Any]] = []
        for s in servers:
            for j in conn.execute(
                "SELECT j.*, m.served_id AS model_served, e.name AS endpoint_name"
                " FROM jobs j LEFT JOIN models m ON m.id = j.model_id"
                " LEFT JOIN endpoints e ON e.id = j.endpoint_id"
                " WHERE j.mcp_server_id = ? AND (j.status IN ('queued', 'running') OR j.finished_at >= ?)",
                (s["id"], now - KEEP_FINISHED_S),
            ).fetchall():
                ctx = derive.ctx_tokens(conn, j["id"])
                ctx_pct = round(100 * ctx / j["context"]) if ctx and j["context"] else None
                tps = derive.gen_tps(conn, j["id"])
                out.append({
                    "id": j["id"],
                    # LEFT JOIN: a NULL model_id (or endpoint_id) must not drop the job from the
                    # line (story 1.7 item 3); the model renders as ?, the endpoint as no segment
                    "model": j["model_served"] or "?",
                    "endpoint": j["endpoint_name"],
                    "status": j["status"],
                    "started_at": j["started_at"] or j["created_at"],
                    "finished_at": j["finished_at"],
                    "tool_calls": derive.tool_call_count(conn, j["id"]),
                    "num_turns": j["num_turns"],
                    "files": len(derive.files_touched(conn, j["id"])),
                    "ctx_pct": ctx_pct,
                    "gen_tps": round(tps) if tps else None,
                    "last_activity": (j["last_activity"] or "")[:MAX_ACTIVITY],
                    "owner": s["owner"],
                })
        loads = {
            r["name"]: r["load"]
            for r in conn.execute(
                "SELECT e.name, l.load FROM endpoint_load l JOIN endpoints e ON e.id = l.endpoint_id"
                " WHERE l.updated >= ?",
                (now - 60.0,),
            ).fetchall()
            if r["load"]
        }
        out.sort(key=lambda e: e.get("started_at") or 0)
        return out, loads
    finally:
        if own_conn:
            conn.close()


MARK = {"queued": "…", "running": "⟳", "succeeded": "✓", "failed": "✗", "timeout": "⏱", "cancelled": "∅", "lost": "†"}


def render_entry(e: dict[str, Any], now: float) -> str:
    end = e.get("finished_at") or now
    bits = [MARK.get(e.get("status", ""), "?"), short_model(e.get("model", "")), fmt_elapsed(end - (e.get("started_at") or now))]
    if e.get("status") == "running":
        if e.get("tool_calls"):
            bits.append(_n(e["tool_calls"], "call"))
        if e.get("files"):
            bits.append(_n(e["files"], "file"))
        if e.get("ctx_pct") is not None:
            bits.append(f"ctx {e['ctx_pct']}%")
        if e.get("gen_tps"):
            bits.append(f"{e['gen_tps']} t/s")
    elif e.get("status") == "succeeded" and e.get("files"):
        bits.append(_n(e["files"], "file"))
    return " ".join(bits)


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def render(now: float | None = None, prefix: str = "local: ", sep: str = " · ", owner: int | None = None) -> str:
    """One line for the status bar, or an empty string when no live server has jobs to show.

    Jobs of this Claude Code session (same ``owner`` pid) are shown in full, followed by the load
    of the endpoints they run on; other sessions' jobs are folded into ``+N other``, because the
    boxes are shared and knowing one is busy matters even when the job isn't yours.

    It must never print a traceback: the database is somebody else's process's file, so a
    too-new schema, a lock or any sqlite failure renders as the empty line.
    """
    now = now or time.time()
    try:
        entries, loads = collect(now)
    except (db.SchemaError, sqlite3.Error, OSError, OverflowError, ValueError):
        return ""  # Overflow/ValueError: a corrupt row's values (review of 1.7, finding 9)
    if not entries:
        return ""
    if owner is None:
        owner = my_owner()
    mine = [e for e in entries if owner is None or e.get("owner") == owner]
    others = len(entries) - len(mine)
    parts = [render_entry(e, now) for e in mine]
    for name in dict.fromkeys(e["endpoint"] for e in mine if e.get("status") == "running" and e.get("endpoint")):
        if loads.get(name):
            parts.append(f"{name} {loads[name]}")
    if others:
        parts.append(f"+{others} other")
    return prefix + sep.join(parts)
