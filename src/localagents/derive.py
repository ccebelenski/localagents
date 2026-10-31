"""Read-time derivations over the database (spec "Derivations", docs/superpowers/specs/
2026-10-29-mcp-sqlite-schema.md): each reproduces the value the in-memory job fields or the
runner used to keep, computed from the ``requests``/``tool_calls`` rows instead.

Standard library only: ``localagents statusline`` imports this on every status-bar refresh.
Story 1.5b adds the rest: usage totals, counts by tool, the error count, the last tool, and
``summary_from_row`` (the whole job_status dict for a row read from the database)."""

from __future__ import annotations

import json
import sqlite3
import time

# runner.py EDIT_TOOLS: the tools whose input carries a touched file path (spec write rule).
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# the jobs columns holding the /metrics delta, rebuilt into Job.metrics at read time
_METRICS_COLUMNS = (
    "generated_tokens", "prompt_tokens_processed", "prompt_tokens_cached",
    "cache_hit_ratio", "prompt_tps", "generate_tps", "spec_decode_acceptance",
)

_PROMPT_SUM = "(COALESCE(r.input_tokens, 0) + COALESCE(r.cache_read_tokens, 0) + COALESCE(r.cache_creation_tokens, 0))"
_MESSAGES = "FROM requests r JOIN paths p ON p.id = r.path_id WHERE r.job_id = ? AND p.path = '/v1/messages'"


def tool_call_count(conn: sqlite3.Connection, job_id: str) -> int:
    """Every ToolUseBlock the runner recorded for the job."""
    return conn.execute("SELECT COUNT(*) FROM tool_calls WHERE job_id = ?", (job_id,)).fetchone()[0]


def files_touched(conn: sqlite3.Connection, job_id: str) -> list[str]:
    """DISTINCT edit-tool ``input`` in first-seen order (dedup keeping the earliest row)."""
    marks = ", ".join("?" * len(EDIT_TOOLS))
    rows = conn.execute(
        f"SELECT input FROM tool_calls WHERE job_id = ? AND tool IN ({marks}) AND input IS NOT NULL"
        " GROUP BY input ORDER BY MIN(t)",
        (job_id, *EDIT_TOOLS),
    ).fetchall()
    return [r[0] for r in rows]


def ctx_tokens(conn: sqlite3.Connection, job_id: str) -> int | None:
    """The prompt sum of the latest ``/v1/messages`` row whose prompt sum is non-zero: a null
    usage or a count_tokens call keeps the older figure (the old note_usage rule)."""
    row = conn.execute(
        f"SELECT {_PROMPT_SUM} {_MESSAGES} AND {_PROMPT_SUM} > 0 ORDER BY r.t DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return row[0] if row else None


def usage_totals(conn: sqlite3.Connection, job_id: str) -> dict[str, int]:
    """The four token columns summed over the job's requests rows (NULLs as 0). Replaces the
    SDK's ``ResultMessage.usage`` in ``job_status``: the two count the same traffic from
    different sides and can differ by count_tokens calls and failed requests (spec contract
    note). The keys are the API's names, as ``Job.usage`` carried them."""
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),"
        " COALESCE(SUM(cache_read_tokens), 0), COALESCE(SUM(cache_creation_tokens), 0)"
        " FROM requests WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return {
        "input_tokens": row[0], "output_tokens": row[1],
        "cache_read_input_tokens": row[2], "cache_creation_input_tokens": row[3],
    }


def tool_counts(conn: sqlite3.Connection, job_id: str) -> dict[str, dict[str, int]]:
    """Calls and errors grouped by tool (spec: ``SUM(is_error)``; a pending result counts 0)."""
    rows = conn.execute(
        "SELECT tool, COUNT(*), COALESCE(SUM(is_error), 0) FROM tool_calls WHERE job_id = ? GROUP BY tool",
        (job_id,),
    ).fetchall()
    return {r[0]: {"calls": r[1], "errors": r[2]} for r in rows}


def error_count(conn: sqlite3.Connection, job_id: str) -> int:
    """Tool calls whose matching result carried is_error."""
    return conn.execute("SELECT COALESCE(SUM(is_error), 0) FROM tool_calls WHERE job_id = ?", (job_id,)).fetchone()[0]


def last_tool(conn: sqlite3.Connection, job_id: str) -> str | None:
    """``"<tool>: <input>"`` of the latest tool_calls row (a NULL input leaves just the name):
    the progress line's view of what the job is doing right now."""
    row = conn.execute(
        "SELECT tool, input FROM tool_calls WHERE job_id = ? ORDER BY t DESC, rowid DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return f"{row['tool']}: {row['input']}" if row["input"] is not None else row["tool"]


def gen_tps(conn: sqlite3.Connection, job_id: str) -> float | None:
    """Replay of the note_usage EMA, ``0.5*prev + 0.5*(output / span)``, over the job's
    ``/v1/messages`` rows in t order; span is gen_ms when set, else ms, and rows without output
    or span are skipped exactly as note_usage skipped them."""
    ema: float | None = None
    for r in conn.execute(f"SELECT r.t, r.ms, r.gen_ms, r.output_tokens {_MESSAGES} ORDER BY r.t", (job_id,)).fetchall():
        out = r["output_tokens"] or 0
        span = r["gen_ms"] if r["gen_ms"] else r["ms"]
        if out <= 0 or not span or span <= 0:
            continue
        tps = out / (span / 1000)
        ema = tps if ema is None else 0.5 * ema + 0.5 * tps
    return ema


def prefill_tps(conn: sqlite3.Connection, job_id: str) -> float | None:
    """Processed prompt tokens (input + cache_creation) per second of prefill (ms − gen_ms),
    summed over the job's ``/v1/messages`` rows — a plain ratio over the job. Rows join only
    when all four fields are numbers, ms > gen_ms and the processed tokens > 0: the page's own
    rule (app.js prefillRate, story 3.9k). Not an EMA replay like gen_tps: decode's EMA is a
    replay of the status line's note_usage history, and prefill has no such history to match —
    a ratio is what "tokens processed per second of prefill" means (story 3.10a)."""
    tokens = seconds = 0.0
    for r in conn.execute(
        f"SELECT r.ms, r.gen_ms, r.input_tokens, r.cache_creation_tokens {_MESSAGES} ORDER BY r.t",
        (job_id,),
    ).fetchall():
        inp, cc, ms, gen = r["input_tokens"], r["cache_creation_tokens"], r["ms"], r["gen_ms"]
        if inp is None or cc is None or ms is None or gen is None or ms <= gen or inp + cc <= 0:
            continue
        tokens += inp + cc
        seconds += (ms - gen) / 1000
    return tokens / seconds if seconds else None


def server_version(conn: sqlite3.Connection, server_id: str | None) -> str | None:
    """The build stamp of the MCP server that ran the job (story 1.14): its ``mcp_servers``
    row's ``version``, written once at that server's start (``db.build_stamp``). None when the
    job names no server or the row is gone."""
    if not server_id:
        return None
    row = conn.execute("SELECT version FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
    return row[0] if row is not None else None


def _truncate(task: str | None) -> str | None:
    """job_status shows the brief's first 300 chars, as Job.summary always did."""
    return task if task is None or len(task) < 300 else task[:300] + "…"


def summary_from_row(conn: sqlite3.Connection, row: sqlite3.Row, include_result: bool = True,
                     now: float | None = None) -> dict:
    """The full ``job_status`` dict for a ``jobs`` row read from the database — any server's
    job: every column under its Job field name, the lookup ids resolved to model/endpoint/
    base_url, worktree parsed from its JSON, metrics rebuilt from the seven columns or None,
    plus every read-time derivation. Storage details (mcp_server_id, endpoint_id, model_id)
    stay out; the id identifies the job (3.10f-1 dropped ``name``). Same keys as
    ``Job.summary()`` for the same job.

    A running job's ``elapsed_s`` is computed against ``now``, the caller's clock (a route passes
    its own; the seed's world runs on ``--now``), and only against ``time.time()`` when no clock
    is given. A finished job's elapsed is its own span either way."""
    from .db import state_dir  # lazy: the statusline path keeps this module light

    job_id = row["id"]
    model = (
        conn.execute("SELECT served_id FROM models WHERE id = ?", (row["model_id"],)).fetchone()
        if row["model_id"] is not None else None
    )
    ep = (
        conn.execute("SELECT name, base_url FROM endpoints WHERE id = ?", (row["endpoint_id"],)).fetchone()
        if row["endpoint_id"] is not None else None
    )
    worktree = json.loads(row["worktree"]) if row["worktree"] is not None else None
    # the schema has no NOT NULL: name, created_at and worktree.path are read defensively
    wt_path = worktree.get("path") if isinstance(worktree, dict) else None
    start = row["started_at"] or row["created_at"]
    d: dict = {
        "id": job_id,
        "task": _truncate(row["task"]),
        "model": model["served_id"] if model is not None else None,
        "endpoint": ep["name"] if ep is not None else None,
        "base_url": ep["base_url"] if ep is not None else None,
        "cwd": row["cwd"],
        "run_cwd": wt_path or row["cwd"],
        "isolation": row["isolation"],
        "status": row["status"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "session_id": row["session_id"],
        "result": row["result"],
        "error": row["error"],
        "num_turns": row["num_turns"],
        "compactions": row["compactions"],
        "worktree": worktree,
        "last_activity": row["last_activity"],
        "log_dir": str(state_dir() / "jobs" / job_id),
        "dump": bool(row["dump"]),
        "log_events": bool(row["log_events"]),
        "context": row["context"],
        "metrics": None if all(row[c] is None for c in _METRICS_COLUMNS) else {c: row[c] for c in _METRICS_COLUMNS},
        # the spec's read-time derivations (the in-memory duplicates story 1.5b removed from Job)
        "usage": usage_totals(conn, job_id),
        "tool_calls": tool_call_count(conn, job_id),
        "tool_counts": tool_counts(conn, job_id),
        "errors": error_count(conn, job_id),
        "last_tool": last_tool(conn, job_id),
        "files_touched": files_touched(conn, job_id),
        "ctx_tokens": ctx_tokens(conn, job_id),
        "gen_tps": gen_tps(conn, job_id),
        "prefill_tps": prefill_tps(conn, job_id),
        "server_version": server_version(conn, row["mcp_server_id"]),
    }
    end = row["finished_at"] if row["finished_at"] is not None else (time.time() if now is None else now)
    d["elapsed_s"] = round(end - start, 1) if start is not None else 0
    if not include_result:
        d.pop("result")
    return d
