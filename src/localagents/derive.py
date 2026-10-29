"""Read-time derivations over the database (spec "Derivations", docs/superpowers/specs/
2026-10-29-mcp-sqlite-schema.md): each reproduces the value the in-memory job fields or the
runner used to keep, computed from the ``requests``/``tool_calls`` rows instead.

Standard library only: ``localagents statusline`` imports this on every status-bar refresh.
Story 1.5b adds the rest here (usage totals, counts by tool, last_tool)."""

from __future__ import annotations

import sqlite3

# runner.py EDIT_TOOLS: the tools whose input carries a touched file path (spec write rule).
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

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
