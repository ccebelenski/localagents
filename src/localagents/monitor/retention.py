"""Probe retention (monitor spec §3 "Storage"): seven days of probes. Deletes only the monitor's
tables; job, request and tool-call rows are kept (the MCP server's prune removes job directories,
never rows)."""

from __future__ import annotations

import sqlite3
import time

from .. import db

RETENTION_DAYS = 7

# Per endpoint, so every delete is a search on the schema's (endpoint_id, t) index rather than
# a scan of the whole table (story 2.2a ruling F; 2.2b ruling C extends it to
# endpoint_events and bounds the discovery to the endpoints table, so no statement scans a
# monitor table or its whole index).
_ENDPOINT_IDS = "SELECT id FROM endpoints"
_OLD_PROBE_IDS = "SELECT id FROM probes WHERE endpoint_id = ? AND t < ?"
_DELETE_OLD_PROBES = "DELETE FROM probes WHERE endpoint_id = ? AND t < ?"
_DELETE_OLD_EVENTS = "DELETE FROM endpoint_events WHERE endpoint_id = ? AND t < ?"


def prune_probes(conn: sqlite3.Connection, days: float = RETENTION_DAYS, now: float | None = None) -> int:
    """Delete, per endpoint (iterating the ``endpoints`` table), the ``probe_hardware``,
    ``probe_counters`` and ``probes`` rows and the ``endpoint_events`` rows whose ``t`` is
    older than ``days`` before ``now``, in one ``immediate()`` transaction. Children go
    first: the foreign keys are enforced. Every delete is ``WHERE endpoint_id = ? AND t < ?``
    so it uses the ``(endpoint_id, t)`` indexes (``probes_endpoint_t``,
    ``endpoint_events_endpoint_t``); event rows share the probe horizon. Returns the number
    of probes rows deleted. ``days <= 0`` disables pruning, as ``jobs.prune`` does."""
    if days <= 0:
        return 0
    cutoff = (now if now is not None else time.time()) - days * 86400
    with db.immediate(conn):
        endpoint_ids = [r[0] for r in conn.execute(_ENDPOINT_IDS)]
        n = 0
        for endpoint_id in endpoint_ids:
            conn.execute(f"DELETE FROM probe_hardware WHERE probe_id IN ({_OLD_PROBE_IDS})",
                         (endpoint_id, cutoff))
            conn.execute(f"DELETE FROM probe_counters WHERE probe_id IN ({_OLD_PROBE_IDS})",
                         (endpoint_id, cutoff))
            n += conn.execute(_DELETE_OLD_PROBES, (endpoint_id, cutoff)).rowcount
            conn.execute(_DELETE_OLD_EVENTS, (endpoint_id, cutoff))
    return n
