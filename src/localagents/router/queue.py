"""The queue is the database (docs/superpowers/specs/2026-11-02-router-design.md §7).

Several Claude sessions each run a localagents server against one SQLite file, so the queue is
a ``queue_entries`` row per job: its endpoint, priority rank, arrival, owning server, admission
moment, whether it was forced, and (schema v12) why it still waits. Order is priority rank,
then arrival (the row's insertion order breaks a tie). Position and expected wait are computed
on read.

Occupancy is counted in sessions: our admitted entries whose job is ``queued`` (admitted, task
not yet started) or ``running`` and whose owning server's heartbeat is fresh. Foreign sessions
are the probe's business (``Capacity.admits`` adds them); a probe is never read inside a
transaction here, and every write runs in its own ``db.immediate`` (``BEGIN IMMEDIATE``), so
these functions are never called inside another transaction (the store's one shared connection
cannot nest).

Stale owners: an entry whose server stopped, or whose heartbeat is older than the liveness
window ``mark_lost_jobs`` uses (``jobs.SERVER_LIVE_S``), is skipped by every admission, and
the admission that notices marks its still-queued job ``lost`` (§7: "a closed session at the
head of a queue blocks nothing"). A dead owner's running session stops counting but is not
swept here: ``mark_lost_jobs`` decides that, heartbeat AND pid.

Pools (§7 "Accepted" 2026-11-04): when a pool is given, admission also needs the pool's
occupancy, across every member endpoint, under the pool's cap; a wait the pool caused is
written ``blocked_by = 'pool'`` so Task 12 can fill ``routed_by.pool``.

``dropped`` (§7 "Accepted"): a job that leaves the queue without a session; ``error`` names
the cause, ``started_at`` stays null."""

from __future__ import annotations

import sqlite3
import sys
import time

from localagents import db
from localagents.jobs import SERVER_LIVE_S
from localagents.router.capacity import Capacity

PRIORITY_RANK = {"urgent": 0, "normal": 1, "background": 2}

# the error a dropped or lost job's row carries for the causes this module writes (Task 12
# writes ``cancelled while queued``, ``row_down`` and ``model_unavailable`` through mark_dropped)
DROPPED_BY_QUEUE_TOOL = "dropped by the queue tool"
LOST_WHILE_QUEUED = "its server stopped beating while the job was queued"

# a live owner, with ``s`` the mcp_servers row and one parameter: now - SERVER_LIVE_S. Every
# term is a definite boolean (a NULL heartbeat is dead, never unknown), so NOT (...) is too.
_LIVE = "(s.stopped IS NULL AND s.updated IS NOT NULL AND s.updated >= ?)"

# an endpoint's waiting entries in queue order: unadmitted, job still queued, owner live.
# Parameters: endpoint_id, now - SERVER_LIVE_S. tests/test_query_plans.py pins its plan (a seek
# of queue_entries_dispatch that yields the order; design §13).
_WAITING_SQL = (
    "SELECT q.job_id, q.endpoint_id, q.owner_server, q.priority_rank, q.enqueued, q.blocked_by,"
    " q.rowid AS entry_rowid"
    " FROM queue_entries q JOIN jobs j ON j.id = q.job_id JOIN mcp_servers s ON s.id = q.owner_server"
    " WHERE q.endpoint_id = ? AND q.admitted IS NULL AND j.status = 'queued' AND " + _LIVE +
    " ORDER BY q.priority_rank, q.enqueued, q.rowid"
)

_ENTRY_SQL = (
    "SELECT q.*, q.rowid AS entry_rowid, j.status FROM queue_entries q"
    " JOIN jobs j ON j.id = q.job_id WHERE q.job_id = ?"
)


def _marks(ids: list[int]) -> str:
    return ", ".join("?" * len(ids))


def _key(row: sqlite3.Row) -> tuple:
    return (row["priority_rank"], row["enqueued"], row["entry_rowid"])


def live_owner(conn: sqlite3.Connection, server_id: str, now: float) -> bool:
    """The server has not written ``stopped`` and beat within ``SERVER_LIVE_S`` (inclusive)."""
    row = conn.execute(f"SELECT 1 FROM mcp_servers s WHERE s.id = ? AND {_LIVE}",
                       (server_id, now - SERVER_LIVE_S)).fetchone()
    return row is not None


def occupancy(conn: sqlite3.Connection, endpoint_ids: list[int], now: float) -> int:
    """Our admitted sessions across these endpoints (one id: that endpoint's own count): admitted
    entries whose job is queued or running and whose owner is live. Forced entries count."""
    ids = list(dict.fromkeys(endpoint_ids))
    if not ids:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM queue_entries q JOIN jobs j ON j.id = q.job_id"
        " JOIN mcp_servers s ON s.id = q.owner_server"
        f" WHERE q.endpoint_id IN ({_marks(ids)}) AND q.admitted IS NOT NULL"
        f" AND j.status IN ('queued', 'running') AND {_LIVE}",
        (*ids, now - SERVER_LIVE_S),
    ).fetchone()
    return row[0]


def insert_entry(conn: sqlite3.Connection, job_id: str, endpoint_id: int, owner_server: str,
                 priority: str, forced: bool = False, *, now: float | None = None) -> None:
    """``enqueue``'s INSERT inside the CALLER's transaction (router 12: ``JobStore.new`` writes
    the job row and its entry in one, so a job never exists without its entry)."""
    if priority not in PRIORITY_RANK:
        raise ValueError(f"priority must be one of {', '.join(PRIORITY_RANK)}; got {priority!r}")
    now = time.time() if now is None else now
    conn.execute(
        "INSERT INTO queue_entries (job_id, endpoint_id, owner_server, priority_rank, enqueued,"
        " admitted, forced) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, endpoint_id, owner_server, PRIORITY_RANK[priority], now,
         now if forced else None, 1 if forced else 0),
    )


def enqueue(conn: sqlite3.Connection, job_id: str, endpoint_id: int, owner_server: str,
            priority: str, forced: bool = False, *, now: float | None = None) -> None:
    """Write the job's entry. ``forced`` (``admit: force``, §7) skips the wait but still writes
    an admitted entry, so it is counted. ``now`` is the arrival (default: the wall clock)."""
    if priority not in PRIORITY_RANK:
        raise ValueError(f"priority must be one of {', '.join(PRIORITY_RANK)}; got {priority!r}")
    with db.immediate(conn):
        insert_entry(conn, job_id, endpoint_id, owner_server, priority, forced, now=now)


def _mark_stale(conn: sqlite3.Connection, endpoint_ids: list[int], now: float) -> None:
    """Inside the caller's transaction: every still-queued job on these endpoints whose owner
    is not live becomes ``lost``, with a line on stderr naming it (§7 stale owners)."""
    rows = conn.execute(
        "SELECT q.job_id, q.owner_server FROM queue_entries q JOIN jobs j ON j.id = q.job_id"
        " LEFT JOIN mcp_servers s ON s.id = q.owner_server"
        f" WHERE q.endpoint_id IN ({_marks(endpoint_ids)}) AND j.status = 'queued'"
        f" AND NOT {_LIVE}",
        (*endpoint_ids, now - SERVER_LIVE_S),
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE jobs SET status = 'lost', error = ?, finished_at = ?, updated = ?"
            " WHERE id = ? AND status = 'queued'",
            (LOST_WHILE_QUEUED, now, now, r["job_id"]),
        )
        print(f"localagents: job {r['job_id']}: its queue owner {r['owner_server']} has no "
              f"heartbeat within {SERVER_LIVE_S:g} s; marked lost", file=sys.stderr)


def try_admit(conn: sqlite3.Connection, job_id: str, capacity: Capacity, now: float, *,
              pool_cap: int | None = None, pool_endpoint_ids: list[int] | None = None) -> bool:
    """Claim a session for this job, or leave it queued with the reason written on its entry.

    One ``BEGIN IMMEDIATE`` (the write lock is taken before anything is read, so two servers
    admitting at once are serialised and the second counts the first's session): stale owners'
    queued jobs on the endpoints concerned are marked ``lost``; then the entry is admitted only
    when it is the head of its endpoint's waiting entries, ``capacity.admits`` the endpoint's
    occupancy, and, with a pool, the pool's occupancy is under ``pool_cap`` and no earlier entry
    elsewhere in the pool is waiting on the pool (order is kept across the pool, §7). Otherwise
    ``blocked_by`` says why: ``endpoint`` when the endpoint itself is full (named first: the
    pool is the cause only when the endpoint had room) or the entry is behind the queue's
    order, ``pool`` when the pool held it. True when the entry is admitted (already, or now)
    and its job is still live; False for no entry, or a job that is over."""
    if (pool_cap is None) != (pool_endpoint_ids is None):
        raise ValueError("pool_cap and pool_endpoint_ids come together: a pool is its cap and its members")
    with db.immediate(conn):
        me = conn.execute(_ENTRY_SQL, (job_id,)).fetchone()
        if me is None:
            return False
        endpoint = me["endpoint_id"]
        pool_ids = None if pool_endpoint_ids is None else sorted({*pool_endpoint_ids, endpoint})
        _mark_stale(conn, pool_ids or [endpoint], now)
        me = conn.execute(_ENTRY_SQL, (job_id,)).fetchone()
        if me["admitted"] is not None:
            return me["status"] in ("queued", "running")
        if me["status"] != "queued":
            return False
        waiting = conn.execute(_WAITING_SQL, (endpoint, now - SERVER_LIVE_S)).fetchall()
        is_head = bool(waiting) and waiting[0]["job_id"] == job_id
        endpoint_room = capacity.admits(occupancy(conn, [endpoint], now))
        pool_held = False
        if pool_ids is not None:
            pool_held = occupancy(conn, pool_ids, now) >= pool_cap or conn.execute(
                "SELECT 1 FROM queue_entries q JOIN jobs j ON j.id = q.job_id"
                " JOIN mcp_servers s ON s.id = q.owner_server"
                f" WHERE q.endpoint_id IN ({_marks(pool_ids)}) AND q.job_id != ?"
                " AND q.admitted IS NULL AND j.status = 'queued' AND q.blocked_by = 'pool'"
                f" AND {_LIVE} AND (q.priority_rank, q.enqueued, q.rowid) < (?, ?, ?) LIMIT 1",
                (*pool_ids, job_id, now - SERVER_LIVE_S, *_key(me)),
            ).fetchone() is not None
        if endpoint_room and not pool_held and is_head:
            conn.execute("UPDATE queue_entries SET admitted = ?, blocked_by = NULL WHERE job_id = ?",
                         (now, job_id))
            return True
        why = "pool" if endpoint_room and pool_held else "endpoint"
        conn.execute("UPDATE queue_entries SET blocked_by = ? WHERE job_id = ?", (why, job_id))
        return False


def waiting(conn: sqlite3.Connection, endpoint_id: int, now: float) -> list[sqlite3.Row]:
    """The endpoint's waiting entries in queue order (rows of ``_WAITING_SQL``): what
    ``list_models`` counts as the endpoint's queue (router 14a)."""
    return conn.execute(_WAITING_SQL, (endpoint_id, now - SERVER_LIVE_S)).fetchall()


def ahead(conn: sqlite3.Connection, job_id: str, now: float) -> list[sqlite3.Row]:
    """The waiting entries in front of this one on its endpoint, in queue order (rows of
    ``_WAITING_SQL``). Empty for an admitted entry or a job that is no longer queued.
    KeyError when the job has no entry."""
    me = conn.execute(_ENTRY_SQL, (job_id,)).fetchone()
    if me is None:
        raise KeyError(f"job {job_id} has no queue entry")
    if me["admitted"] is not None or me["status"] != "queued":
        return []
    waiting = conn.execute(_WAITING_SQL, (me["endpoint_id"], now - SERVER_LIVE_S)).fetchall()
    return [r for r in waiting if _key(r) < _key(me)]


def position(conn: sqlite3.Connection, job_id: str, now: float) -> int:
    """1 for the head of its endpoint's queue, n for the n-th; 0 when the entry is not
    waiting (admitted, or its job is over). KeyError when the job has no entry."""
    me = conn.execute(_ENTRY_SQL, (job_id,)).fetchone()
    if me is None:
        raise KeyError(f"job {job_id} has no queue entry")
    if me["admitted"] is not None or me["status"] != "queued":
        return 0
    return len(ahead(conn, job_id, now)) + 1


def remove(conn: sqlite3.Connection, job_id: str) -> bool:
    """Delete the job's entry (its session is over, or it never needs one). The job's row is
    not touched. True when there was an entry."""
    with db.immediate(conn):
        return conn.execute("DELETE FROM queue_entries WHERE job_id = ?", (job_id,)).rowcount > 0


def _drop_job(conn: sqlite3.Connection, job_id: str, error: str, now: float) -> bool:
    """Inside the caller's transaction: a still-queued job becomes ``dropped`` with ``error``;
    ``started_at`` is not written (it never ran). A job that is running or over is untouched."""
    return conn.execute(
        "UPDATE jobs SET status = 'dropped', error = ?, finished_at = ?, updated = ?"
        " WHERE id = ? AND status = 'queued'",
        (error, now, now, job_id),
    ).rowcount > 0


def mark_dropped(conn: sqlite3.Connection, job_id: str, error: str, now: float) -> bool:
    """The job leaves the queue without a session: its entry (if one was written) is deleted
    and, iff its status is still ``queued``, it becomes ``dropped`` with ``error`` naming the
    cause (Task 12: ``cancelled while queued``, ``row_down``, ``model_unavailable``). True when
    the status was written. One transaction."""
    with db.immediate(conn):
        conn.execute("DELETE FROM queue_entries WHERE job_id = ?", (job_id,))
        return _drop_job(conn, job_id, error, now)


def drop(conn: sqlite3.Connection, job_id: str, *, now: float | None = None) -> bool:
    """The ``queue`` tool's ``drop`` (§11): remove a stale or stuck entry by hand. The entry is
    deleted and, iff its job is still ``queued``, the job is ``dropped`` with
    ``error = 'dropped by the queue tool'``; an admitted running session keeps its status (it
    ran; only its count goes). True when an entry was removed. One transaction."""
    now = time.time() if now is None else now
    with db.immediate(conn):
        removed = conn.execute("DELETE FROM queue_entries WHERE job_id = ?", (job_id,)).rowcount > 0
        if removed:
            _drop_job(conn, job_id, DROPPED_BY_QUEUE_TOOL, now)
        return removed
