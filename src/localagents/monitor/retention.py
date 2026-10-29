"""Probe retention (monitor spec §3 "Storage"): seven days of probes. Deletes only the monitor's
tables; job, request and tool-call rows are kept (the MCP server's prune removes job directories,
never rows)."""

from __future__ import annotations

import sqlite3
import time

from .. import db

RETENTION_DAYS = 7


def prune_probes(conn: sqlite3.Connection, days: float = RETENTION_DAYS, now: float | None = None) -> int:
    """Delete ``probe_hardware``, ``probe_counters`` and ``probes`` rows (children first: the
    foreign keys are enforced) and ``endpoint_events`` rows with ``t`` older than ``days`` before
    ``now``, in one ``immediate()`` transaction. Returns the number of probes rows deleted.
    ``days <= 0`` disables pruning, as ``jobs.prune`` does."""
    if days <= 0:
        return 0
    cutoff = (now if now is not None else time.time()) - days * 86400
    old = "SELECT id FROM probes WHERE t < ?"
    with db.immediate(conn):
        conn.execute(f"DELETE FROM probe_hardware WHERE probe_id IN ({old})", (cutoff,))
        conn.execute(f"DELETE FROM probe_counters WHERE probe_id IN ({old})", (cutoff,))
        n = conn.execute("DELETE FROM probes WHERE t < ?", (cutoff,)).rowcount
        conn.execute("DELETE FROM endpoint_events WHERE t < ?", (cutoff,))
    return n
