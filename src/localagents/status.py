"""Render the Claude Code status line from the shared SQLite tables (story 1.5a).

Every MCP server keeps its ``mcp_servers`` row alive with a heartbeat and writes its jobs,
requests and tool calls to the database; ``render()`` reads the live servers' jobs and prints
one line, e.g. ``local: ⟳ Qwen3.8-Flash-Next 1m18s 4 calls · ✓ Muse-Glimmer-30B 3m02s``.
``render_lines()`` (story 1.11) is the multi-line form: one header per endpoint the session's
jobs run on, one indented row per job (mark, model, elapsed, calls, files, ctx, t/s,
compactions, last activity), a trailing line for other sessions' jobs. The
``localagents statusline`` command runs ``render_lines()`` by default and ``render()`` under
``--one-line``, and nothing else, so it stays fast enough for every status-line refresh:
stdlib plus ``db``/``derive`` only, under 100 ms, and it never creates or fails on the
database (no live servers or no database yet prints the empty line).
"""

from __future__ import annotations

import json
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
    """Pid of the nearest Claude Code process above us (native ``claude``, or an npm install
    running as ``node``): the session whose status line is asking. None when there is none
    (then every job counts as ours). The walk lives in ``db.session_pid`` — the server writes
    the same answer as its row's ``owner``, so a session's own jobs match instead of folding
    into "+N other" (story 1.9 item 9)."""
    return db.session_pid()


def display_activity(text: str | None, root: str | None, limit: int = MAX_ACTIVITY) -> str | None:
    """The status row's view of a job's raw ``last_activity`` (story 1.11b): ONE line — every
    run of whitespace, newlines of a multi-line Bash command included, collapses to a single
    space, then stripped — with paths under the job's root shown relative to it (every
    occurrence of ``root + "/"`` goes, so the 60-char budget reaches the file name instead of
    the worktree prefix), and cut to ``limit`` characters AFTER relativising. None in, None
    out. Pure: the raw text on the job row and in ``derive.last_tool`` is never touched —
    only the row's rendering changes here."""
    if text is None:
        return None
    text = " ".join(text.split())
    if root:
        text = text.replace(root + "/", "")
    return text[:limit]


def _job_root(j: sqlite3.Row) -> str | None:
    """The job's root for relativising activity paths (story 1.11b): the worktree's ``path``
    when the job runs in one, else its cwd. A corrupt worktree column must not blank the
    whole line (render's promise), so a JSON failure falls back to the cwd."""
    if j["worktree"]:
        try:
            worktree = json.loads(j["worktree"])
        except ValueError:  # JSONDecodeError is a ValueError
            return j["cwd"]
        if isinstance(worktree, dict) and isinstance(worktree.get("path"), str) and worktree["path"]:
            return worktree["path"]
    return j["cwd"]


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
                    # router 8: a calibration probe shows its `cal` mark and is not traffic
                    "calibration": bool(j["calibration"]),
                    "started_at": j["started_at"] or j["created_at"],
                    "finished_at": j["finished_at"],
                    "tool_calls": derive.tool_call_count(conn, j["id"]),
                    "num_turns": j["num_turns"],
                    "compactions": j["compactions"],
                    "files": len(derive.files_touched(conn, j["id"])),
                    "ctx_pct": ctx_pct,
                    "gen_tps": round(tps) if tps else None,
                    # raw: the row relativises against the root and cuts (display_activity),
                    # so the cut can no longer happen here (story 1.11b)
                    "last_activity": j["last_activity"],
                    "root": _job_root(j),
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


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _entry_bits(e: dict[str, Any]) -> list[str]:
    """The bits render_entry (one line) and the multi-line row builder share: calls, files,
    ctx, t/s for a running job; files for a succeeded one (story 1.5a's set, unchanged)."""
    if e.get("status") != "running":
        return [_n(e["files"], "file")] if e.get("status") == "succeeded" and e.get("files") else []
    bits: list[str] = []
    if e.get("tool_calls"):
        bits.append(_n(e["tool_calls"], "call"))
    if e.get("files"):
        bits.append(_n(e["files"], "file"))
    if e.get("ctx_pct") is not None:
        bits.append(f"ctx {e['ctx_pct']}%")
    if e.get("gen_tps"):
        bits.append(f"{e['gen_tps']} t/s")
    return bits


def _row_bits(e: dict[str, Any]) -> list[str]:
    """The multi-line row's bits (story 1.11): the shared bits, plus the compaction count
    (when > 0, on finished rows too — a trailing indicator of a tuning problem, ruling on the
    1.11 review) and the last activity, which stays running-only. The one-line form shows
    neither."""
    bits = _entry_bits(e)
    if e.get("compactions"):
        bits.append(_n(e["compactions"], "compaction"))
    if e.get("status") == "running":
        activity = display_activity(e.get("last_activity"), e.get("root"))
        if activity:  # one line, relative, cut to MAX_ACTIVITY (story 1.11b)
            bits.append(activity)
    return bits


def _cal_mark(e: dict[str, Any]) -> str:
    """The `cal` mark beside a calibration probe's status mark (router 8, spec §10)."""
    return "cal " if e.get("calibration") else ""


def render_entry(e: dict[str, Any], now: float) -> str:
    end = e.get("finished_at") or now
    head = (f"{MARK.get(e.get('status', ''), '?')} {_cal_mark(e)}"
            f"{short_model(e.get('model', ''))} {fmt_elapsed(end - (e.get('started_at') or now))}")
    bits = _entry_bits(e)
    if not bits:
        return head
    return f"{head} " + " ".join(bits)


def _job_row(e: dict[str, Any], now: float) -> str:
    """One indented multi-line row (story 1.11): the same head as render_entry, the row's
    bits joined by ` · `."""
    end = e.get("finished_at") or now
    head = (f"  {MARK.get(e.get('status', ''), '?')} {_cal_mark(e)}"
            f"{short_model(e.get('model', ''))} {fmt_elapsed(end - (e.get('started_at') or now))}")
    bits = _row_bits(e)
    if not bits:
        return head
    return f"{head} · " + " · ".join(bits)


def _split_by_owner(entries: list[dict[str, Any]], owner: int | None) -> tuple[list[dict[str, Any]], int]:
    """This session's entries, and the number of other-session jobs that count as traffic.
    A calibration probe is shown like any job when it is ours, but it is not traffic: another
    session's probe adds nothing to the `+N` count (router 8, spec §10)."""
    mine = [e for e in entries if owner is None or e.get("owner") == owner]
    others = sum(
        1 for e in entries
        if not e.get("calibration") and not (owner is None or e.get("owner") == owner)
    )
    return mine, others


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
    mine, others = _split_by_owner(entries, owner)
    parts = [render_entry(e, now) for e in mine]
    for name in dict.fromkeys(e["endpoint"] for e in mine if e.get("status") == "running" and e.get("endpoint")):
        if loads.get(name):
            parts.append(f"{name} {loads[name]}")
    if others:
        parts.append(f"+{others} other")
    return prefix + sep.join(parts)


def render_lines(now: float | None = None, owner: int | None = None) -> list[str]:
    """The multi-line status line (story 1.11; the one-line render() is unchanged): one
    header per endpoint this session's jobs run on — first-seen order of the entries, which
    collect returns sorted by started_at — with that endpoint's load when the map has one,
    one indented row per job, and a trailing line for other sessions' jobs. Jobs with no
    endpoint go under ``local ?``.

    [] when there is nothing to show, and the same exception set as render never raises
    (it yields the empty line, which the operator's script -n-tests; render_lines yields
    [] so the CLI prints exactly that one empty line)."""
    now = now or time.time()
    try:
        entries, loads = collect(now)
    except (db.SchemaError, sqlite3.Error, OSError, OverflowError, ValueError):
        return []
    if not entries:
        return []
    if owner is None:
        owner = my_owner()
    mine, others = _split_by_owner(entries, owner)
    lines: list[str] = []
    for name in dict.fromkeys(e["endpoint"] or "?" for e in mine):
        header = f"local {name}"
        if loads.get(name):
            header += f" · {loads[name]}"
        lines.append(header)
        for e in mine:
            if (e["endpoint"] or "?") == name:
                lines.append(_job_row(e, now))
    if others == 1:
        lines.append("+1 job in another session")
    elif others:
        lines.append(f"+{others} jobs in other sessions")
    return lines
