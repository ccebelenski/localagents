"""3.9E query-plan audit: the plan of every jobs-table statement the audit flagged, pinned with
``EXPLAIN QUERY PLAN`` (the 1.11 purge ruling's standing expectation: "every query is scoped by
job/endpoint/time and served by an index; an EXPLAIN QUERY PLAN sweep ... is the cheap audit
that pins it"). These tests may import ``localagents.monitor.read``; the MCP server may not
(the AST guard in tests/test_server.py).

The findings and their dispositions:
- E1: the snapshot's finished-jobs tail (``read._JOB_SELECT + read._FINISHED_TAIL``) sorted the
  whole job history every snapshot: ``SCAN j`` + ``USE TEMP B-TREE FOR ORDER BY``. Schema
  version 7's ``jobs_finished_at`` fixes it.
- E2: ``jobs_overlapping``'s OR terms (``finished_at IS NULL OR finished_at >= ?``) now take
  index seeks (``MULTI-INDEX OR`` over ``jobs_finished_at``); the ``ORDER BY started_at`` sort
  stays — no index serves it, and it now sorts only the window's rows, not the history.
- E3: ``list_jobs(all=True, include_finished=False)`` walked the whole ``jobs_created_at``
  index for a predicate that only two statuses can satisfy. The fix is the predicate rewrite
  (``status IN ('queued','running')``, the same set: server.py's ``_TERMINAL_STATUSES`` is the
  complement within ``_V1``'s status CHECK), which lets the existing ``jobs_status_finished_at``
  serve it; the audit's candidate partial index is NOT added — the plan proves SQLite prefers
  the composite index even with a provably-implying predicate."""

import pytest

from localagents import db, server
from localagents.monitor import read

# the jobs_overlapping statement, verbatim from read.py (a 3.9E pin: if the statement changes,
# the plan pin must be re-read by hand, which is the point)
_OVERLAPPING_SQL = (
    "SELECT j.id, j.id AS name, e.name AS endpoint, j.status, j.started_at, j.finished_at"
    " FROM jobs j LEFT JOIN endpoints e ON e.id = j.endpoint_id"
    " WHERE j.started_at IS NOT NULL AND j.started_at <= ?"
    " AND (j.finished_at IS NULL OR j.finished_at >= ?)"
    " ORDER BY j.started_at ASC, j.rowid ASC"
)

# the form as the audit found it, kept here to pin what the rewrite replaced; the live and
# all forms are pinned straight off server._LIST_JOBS_SQL, not a copy
_LIST_LIVE_SQL_OLD = (
    "SELECT * FROM (SELECT *, rowid AS _rowid FROM jobs"
    " WHERE status NOT IN ('succeeded', 'failed', 'cancelled', 'timeout', 'lost')"
    " ORDER BY created_at DESC, rowid DESC LIMIT ?) ORDER BY created_at, _rowid"
)


@pytest.fixture
def conn(tmp_path):
    """A fresh current-schema file with a handful of jobs rows inserted by hand (the columns
    are _V1's). Row shape per the 3.9E probes: six finished, one running, one queued (and, since
    router 11, one dropped)."""
    c = db.open_db(tmp_path / "plans.db")
    rows = [
        # id, status, created_at, started_at, finished_at
        ("j1", "succeeded", 100.0, 101.0, 120.0),
        ("j2", "failed", 110.0, 111.0, 115.0),
        ("j3", "succeeded", 120.0, 121.0, 160.0),
        ("j4", "succeeded", 130.0, 131.0, 140.0),
        ("j5", "failed", 140.0, 300.0, 320.0),
        ("j6", "cancelled", 150.0, 240.0, 260.0),
        ("j7", "running", 160.0, 200.0, None),
        ("j8", "queued", 170.0, None, None),
        # router 11 (schema v12): left the queue without a session, never started
        ("j9", "dropped", 180.0, None, 181.0),
    ]
    for jid, status, created, started, finished in rows:
        c.execute(
            "INSERT INTO jobs (id, status, created_at, started_at, finished_at, updated)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (jid, status, created, started, finished, started or created),
        )
    yield c
    c.close()


def _plan(c, sql, params=()):
    return [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql, params)]


def test_e1_finished_tail_walks_jobs_finished_at_with_no_sort(conn):
    """E1, as read.py builds it: the plan named ``jobs_finished_at`` on the coordinator's copy of
    the real database (``SCAN j USING INDEX jobs_finished_at``, no sort) where the audit's same
    statement on the version-6 file said ``SCAN j`` + ``USE TEMP B-TREE FOR ORDER BY``."""
    plan = _plan(conn, read._JOB_SELECT + read._FINISHED_TAIL, (10,))
    assert any("SCAN j USING INDEX jobs_finished_at" in d for d in plan), plan
    assert not any("TEMP B-TREE" in d for d in plan), plan


def test_e2_overlapping_or_terms_seek_jobs_finished_at(conn):
    """E2: with ``jobs_finished_at`` both OR branches seek the index (``MULTI-INDEX OR``), where
    the audit's version-6 plan was a bare ``SCAN j`` + ``USE TEMP B-TREE FOR ORDER BY``. The
    sort stays: nothing indexes ``started_at``, and the audit's expectation that the OR
    optimisation "uses jobs_finished_at" is exactly what the plan now says — twice."""
    plan = _plan(conn, _OVERLAPPING_SQL, (250.0, 150.0))
    assert any("MULTI-INDEX OR" in d for d in plan), plan
    seeks = [d for d in plan if "SEARCH j USING INDEX jobs_finished_at" in d]
    assert len(seeks) == 2, plan  # finished_at=? (the IS NULL term) and finished_at>?
    assert any("TEMP B-TREE FOR ORDER BY" in d for d in plan), plan  # started_at, over the window's rows only


def test_e2_overlapping_rows_are_the_window_spanners(conn):
    """The rewritten plan answers the same question: j3 (finished inside the window), j6 (runs
    across its end) and j7 (unfinished, started inside) overlap [150, 250]; j4 ended before it,
    j5 started after it, j8 never started."""
    rows = read.jobs_overlapping(conn, 150.0, 250.0)
    assert [r["id"] for r in rows] == ["j3", "j7", "j6"]  # ordered by started_at


def test_e3_live_only_branch_seeks_the_status_index(conn):
    """E3: the rewritten predicate turns the audit's ``SCAN jobs USING INDEX jobs_created_at``
    (a walk of every job ever run, newest first, filtered) into a seek of
    ``jobs_status_finished_at`` over just the queued and running rows. The outer sort of at most
    ``limit`` rows stays and costs nothing next to the walk it replaced."""
    plan = _plan(conn, server._LIST_JOBS_SQL[False], (50,))
    assert any("SEARCH jobs USING INDEX jobs_status_finished_at" in d for d in plan), plan
    assert not any("jobs_created_at" in d for d in plan), plan
    # what the rewrite replaced, pinned so the contrast cannot rot into a guess
    old_plan = _plan(conn, _LIST_LIVE_SQL_OLD, (50,))
    assert any("SCAN jobs USING INDEX jobs_created_at" in d for d in old_plan), old_plan


def test_e3_rewritten_predicate_selects_the_same_rows(conn):
    """The rewrite is sound: the status CHECK admits exactly eight statuses (``_V1``'s seven,
    and ``dropped`` since schema v12) and ``_TERMINAL_STATUSES`` names six of them, so
    ``status IN ('queued','running')`` and ``status NOT IN (<the six terminal>)`` select the
    same rows (a NULL status passes neither, in both forms)."""
    assert server._TERMINAL_STATUSES == ("succeeded", "failed", "cancelled", "timeout", "lost", "dropped")
    new = {r["id"] for r in conn.execute("SELECT id FROM jobs WHERE status IN ('queued', 'running')")}
    old = {r["id"] for r in conn.execute(
        "SELECT id FROM jobs WHERE status NOT IN ('succeeded', 'failed', 'cancelled', 'timeout', 'lost',"
        " 'dropped')")}
    assert new == old == {"j7", "j8"}


def test_e3_list_all_branch_still_walks_created_at(conn):
    """include_finished=True has no status predicate, so the partial-index idea cannot serve it
    anyway; it keeps the version-1 answer: the newest rows off ``jobs_created_at``, the audit
    left it alone."""
    plan = _plan(conn, server._LIST_JOBS_SQL[True], (50,))
    assert any("SCAN jobs USING INDEX jobs_created_at" in d for d in plan), plan


def _queue_history(c, finished=20_000):
    """``finished`` jobs that ran and ended, each with the admitted entry it keeps (entries
    persist after their jobs, ruling 184), a few servers (most stopped), one queued job waiting
    and one running admitted; then ANALYZE, so the planner reads real statistics."""
    eid = db.lookup_id(c, "endpoints", name="e", base_url="http://e", backend="vllm", host="e")
    assert eid == 1
    c.execute("BEGIN")
    for i in range(8):
        c.execute("INSERT INTO mcp_servers (id, pid, started, updated, stopped) VALUES (?, 1, 1.0, 1.0, ?)",
                  (f"s{i}", None if i == 0 else 2.0))
    for i in range(finished):
        jid = f"h{i}"
        c.execute("INSERT INTO jobs (id, status, created_at, started_at, finished_at, updated)"
                  " VALUES (?, 'succeeded', ?, ?, ?, ?)", (jid, i, i, i + 1, i + 1))
        c.execute("INSERT INTO queue_entries (job_id, endpoint_id, owner_server, priority_rank,"
                  " enqueued, admitted) VALUES (?, 1, 's0', 1, ?, ?)", (jid, i, i))
    c.execute("INSERT INTO queue_entries (job_id, endpoint_id, owner_server, priority_rank, enqueued,"
              " admitted) VALUES ('j8', 1, 's0', 1, 170.0, NULL)")
    c.execute("INSERT INTO queue_entries (job_id, endpoint_id, owner_server, priority_rank, enqueued,"
              " admitted) VALUES ('j7', 1, 's0', 1, 160.0, 161.0)")
    c.execute("COMMIT")
    c.execute("ANALYZE")


def test_queue_live_reads_drive_from_job_status(conn):
    """Fix wave 3 (P1; moves router 11's pin, which asserted a seek of queue_entries_dispatch:
    that index's range is the endpoint's whole admitted history once entries persist): every
    read of LIVE queue entries — the waiting head (``_WAITING_SQL``), ``occupancy`` and
    ``_mark_stale`` — drives from ``jobs.status IN ('queued','running')`` through
    ``jobs_status_finished_at`` and joins queue_entries by its primary key: never ``SCAN q``
    and never ``SCAN j``, after ANALYZE over 20k finished entries. The waiting head's sort is
    over the waiting few."""
    from localagents.router import queue

    _queue_history(conn)
    plans = {"waiting": _plan(conn, queue._WAITING_SQL, (1, 0.0)),
             "occupancy": _plan(conn, queue._occupancy_sql(1), (1, 0.0)),
             "mark_stale": _plan(conn, queue._stale_sql(1), (1, 0.0))}
    for name, plan in plans.items():
        assert any("SEARCH j USING INDEX jobs_status_finished_at (status=?)" in d for d in plan), (name, plan)
        assert any("SEARCH q USING INDEX sqlite_autoindex_queue_entries_1 (job_id=?)" in d
                   for d in plan), (name, plan)
        assert not any(d.startswith("SCAN q") or d.startswith("SCAN j") for d in plan), (name, plan)


def test_queue_live_reads_select_the_live_rows(conn):
    """The rewritten statements still answer what they did: the one waiting entry, the one
    admitted running session, and no stale job (s0 is live at now = 0)."""
    from localagents.router import queue

    _queue_history(conn)
    assert [r["job_id"] for r in conn.execute(queue._WAITING_SQL, (1, 0.0))] == ["j8"]
    assert conn.execute(queue._occupancy_sql(1), (1, 0.0)).fetchone()[0] == 1
    assert conn.execute(queue._stale_sql(1), (1, 0.0)).fetchall() == []
    assert [r["job_id"] for r in conn.execute(queue._stale_sql(1), (1, 5.0))] == ["j8"]

def test_status_session_count_drives_from_job_status(conn):
    """status-sessions: the status line's per-endpoint session count (status._SESSIONS_SQL,
    queue.occupancy's set restated and grouped by endpoint name) reads like the queue's own
    live reads: from ``jobs.status IN ('queued','running')`` through ``jobs_status_finished_at``,
    queue_entries by its primary key, never ``SCAN q`` and never ``SCAN j``, after ANALYZE over
    20k finished entries. It answers the one admitted running session."""
    from localagents import status

    _queue_history(conn)
    plan = _plan(conn, status._SESSIONS_SQL, (0.0,))
    assert any("SEARCH j USING INDEX jobs_status_finished_at (status=?)" in d for d in plan), plan
    assert any("SEARCH q USING INDEX sqlite_autoindex_queue_entries_1 (job_id=?)" in d for d in plan), plan
    assert not any(d.startswith("SCAN q") or d.startswith("SCAN j") for d in plan), plan
    assert [tuple(r) for r in conn.execute(status._SESSIONS_SQL, (0.0,))] == [("e", 1)]
