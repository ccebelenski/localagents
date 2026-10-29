"""Reads of the MCP server's ``jobs`` table (monitor spec §3 "Jobs"): the monitor reads the
job rows and never writes them (the server's ``JobStore`` is the only writer). The query
helpers here take a connection because the answer is in the table; everything else in the
package is pure over rows and values. The monitor-owned helpers that need the same table in
two places (``collect.last_up_t`` for the cadence, this module for the display) are the only
connection-taking functions the package allows."""

from __future__ import annotations

import sqlite3


def running_job_at(conn: sqlite3.Connection, endpoint_id: int, t: float) -> sqlite3.Row | None:
    """The job that was running on ``endpoint_id`` at time ``t``: ``started_at <= t`` and
    (``finished_at`` is NULL or ``>= t``), newest first — spec §4: a down-recent endpoint
    "names a job that was running on it when it dropped". A ``lost`` job whose ``finished_at``
    is after ``t`` still counts: it was running when the endpoint dropped. None when nothing
    was. A NULL ``started_at`` (a job that never started) is not running at any t."""
    row = conn.execute(
        """SELECT * FROM jobs
           WHERE endpoint_id = ? AND started_at <= ? AND (finished_at IS NULL OR finished_at >= ?)
           ORDER BY started_at DESC, rowid DESC LIMIT 1""",
        (endpoint_id, t, t),
    ).fetchone()
    return row