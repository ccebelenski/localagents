"""Router task 11: the database queue, atomic admission, stale owners, pools, ``dropped``
(docs/superpowers/specs/2026-11-02-router-design.md §7 "The queue is the database", "Stale
owners", "Directly named jobs", §7 "Accepted (2026-11-04)"; plan Task 11 and its
"Decided 2026-11-04" note).

Every clock is passed in (``now``), so ordering and the liveness window are pinned against
fixed numbers rather than the wall clock. The race test uses two real connections to one
file, never one connection twice."""

import threading
import time

import pytest

from localagents import db, jobs
from localagents.router import queue
from localagents.router.capacity import Capacity

NOW = 100_000.0


def _server(conn, sid, updated=NOW, stopped=None):
    with db.immediate(conn):
        conn.execute("INSERT INTO mcp_servers (id, pid, started, updated, stopped) VALUES (?, ?, ?, ?, ?)",
                     (sid, 1, updated, updated, stopped))


def _endpoint(conn, name):
    return db.lookup_id(conn, "endpoints", name=name, base_url=f"http://{name}:8080", backend="vllm",
                        host=name, now=NOW)


def _job(conn, jid, server="s1", status="queued"):
    with db.immediate(conn):
        conn.execute("INSERT INTO jobs (id, mcp_server_id, status, created_at) VALUES (?, ?, ?, ?)",
                     (jid, server, status, NOW))


def _queued(conn, jid, endpoint_id, *, server="s1", priority="normal", at=NOW, forced=False):
    """A queued job with its entry, enqueued at ``at``."""
    _job(conn, jid, server)
    queue.enqueue(conn, jid, endpoint_id, server, priority, forced=forced, now=at)


def _status(conn, jid):
    return conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()[0]


def _set_status(conn, jid, status):
    with db.immediate(conn):
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, jid))


def _entry(conn, jid):
    return conn.execute("SELECT * FROM queue_entries WHERE job_id = ?", (jid,)).fetchone()


def _cap(sessions, running=0):
    return Capacity("partitioned", sessions, requests_running=running)


@pytest.fixture
def e1(conn):
    _server(conn, "s1")
    return _endpoint(conn, "e1")


def test_order_is_priority_then_arrival(conn, e1):
    """§7: "Order: priority rank, then arrival." An urgent job that arrives last stands first;
    a background job that arrived first stands last; two normals keep their arrival order."""
    assert queue.PRIORITY_RANK == {"urgent": 0, "normal": 1, "background": 2}
    _queued(conn, "bg", e1, priority="background", at=NOW - 40)
    _queued(conn, "n1", e1, priority="normal", at=NOW - 30)
    _queued(conn, "urg", e1, priority="urgent", at=NOW - 10)
    _queued(conn, "n2", e1, priority="normal", at=NOW - 5)
    assert [queue.position(conn, j, NOW) for j in ("urg", "n1", "n2", "bg")] == [1, 2, 3, 4]
    assert [r["job_id"] for r in queue.ahead(conn, "bg", NOW)] == ["urg", "n1", "n2"]
    assert queue.ahead(conn, "urg", NOW) == []
    assert _entry(conn, "urg")["priority_rank"] == 0
    with pytest.raises(ValueError):
        queue.enqueue(conn, "nope", e1, "s1", "asap")


def test_try_admit_admits_only_the_head(conn, e1):
    """Room for three sessions, two entries: the second is refused while the first waits
    (order is kept even with room, §7), and is admitted once the head has been."""
    _queued(conn, "a", e1, at=NOW - 20)
    _queued(conn, "b", e1, at=NOW - 10)
    assert queue.try_admit(conn, "b", _cap(3), NOW) is False
    assert _entry(conn, "b")["admitted"] is None
    assert _entry(conn, "b")["blocked_by"] == "endpoint"
    assert queue.try_admit(conn, "a", _cap(3), NOW) is True
    assert _entry(conn, "a")["admitted"] == NOW and _entry(conn, "a")["blocked_by"] is None
    assert queue.position(conn, "a", NOW) == 0  # admitted: no longer waiting
    assert queue.position(conn, "b", NOW) == 1
    assert queue.try_admit(conn, "b", _cap(3), NOW) is True
    assert queue.try_admit(conn, "b", _cap(3), NOW) is True  # an admitted entry stays admitted
    assert queue.occupancy(conn, [e1], NOW) == 2


def test_two_connections_race_for_one_session(tmp_path, monkeypatch):
    """Two server processes, each on its own connection to one file (opened in its own
    thread), each the head of its own endpoint, one pool with one session between them. The
    first is held inside its transaction after its first count; the second starts its
    admission meanwhile. ``BEGIN IMMEDIATE`` makes the second wait for the first's commit and
    then count the session the first took, so exactly one is admitted. A deferred
    read-then-write would let both count zero (or fail the second with SQLITE_BUSY at its
    upgrade)."""
    path = tmp_path / "race.db"
    setup = db.open_db(path)
    _server(setup, "s1")
    _server(setup, "s2")
    ea, eb = _endpoint(setup, "ea"), _endpoint(setup, "eb")
    _queued(setup, "a", ea, server="s1", at=NOW - 2)
    _queued(setup, "b", eb, server="s2", at=NOW - 1)
    setup.close()

    inside, second_started = threading.Event(), threading.Event()
    real = queue.occupancy
    held = []

    def occupancy(conn, endpoint_ids, now):
        n = real(conn, endpoint_ids, now)
        if threading.current_thread().name == "first" and not held:
            held.append(True)
            inside.set()
            second_started.wait(5)
            time.sleep(0.3)  # the second is now blocked in BEGIN IMMEDIATE
        return n

    monkeypatch.setattr(queue, "occupancy", occupancy)
    results, errors = {}, []

    def admit(jid, ep, wait_for=None):
        conn = db.open_db(path)
        try:
            if wait_for is not None:
                wait_for.wait(5)
                second_started.set()
            results[jid] = queue.try_admit(conn, jid, _cap(1), NOW, pool_cap=1,
                                           pool_endpoint_ids=[ea, eb])
        except Exception as e:  # noqa: BLE001  reported by the assert below
            errors.append(e)
        finally:
            conn.close()

    t1 = threading.Thread(target=admit, args=("a", ea), name="first")
    t2 = threading.Thread(target=admit, args=("b", eb, inside), name="second")
    t1.start()
    t2.start()
    t1.join(10)
    t2.join(10)
    assert errors == []
    assert held == [True]  # the hold really happened inside the first's transaction
    assert results == {"a": True, "b": False}
    check = db.open_db(path)
    try:
        assert real(check, [ea, eb], NOW) == 1
        assert _entry(check, "b")["blocked_by"] == "pool"
    finally:
        check.close()


def test_admitted_counts_until_the_job_finishes(conn, e1):
    """§7 occupancy: an admitted entry counts while its job is queued (admitted, task not yet
    started) or running, and stops counting the moment the job is over."""
    _queued(conn, "a", e1, at=NOW - 20)
    _queued(conn, "b", e1, at=NOW - 10)
    assert queue.try_admit(conn, "a", _cap(1), NOW) is True
    assert queue.occupancy(conn, [e1], NOW) == 1  # admitted, still 'queued'
    assert queue.try_admit(conn, "b", _cap(1), NOW) is False
    _set_status(conn, "a", "running")
    assert queue.occupancy(conn, [e1], NOW) == 1
    assert queue.try_admit(conn, "b", _cap(1), NOW) is False
    assert _entry(conn, "b")["blocked_by"] == "endpoint"
    _set_status(conn, "a", "succeeded")
    assert queue.occupancy(conn, [e1], NOW) == 0
    assert queue.try_admit(conn, "b", _cap(1), NOW) is True


def test_stale_owner_entry_is_skipped_and_marked_lost(conn, e1, capsys):
    """§7 stale owners: an entry whose server's heartbeat is older than the store's liveness
    window (``jobs.SERVER_LIVE_S``, the one ``mark_lost_jobs`` uses) blocks nothing: the head
    of a closed session is skipped and its job marked ``lost`` by the process that noticed,
    with a line on stderr. A dead owner's admitted running session stops counting, but its
    job is not swept: ``mark_lost_jobs`` owns that call (heartbeat AND pid)."""
    _server(conn, "dead", updated=NOW - jobs.SERVER_LIVE_S - 1)
    _queued(conn, "run", e1, server="dead", at=NOW - 50, forced=True)
    _set_status(conn, "run", "running")
    _queued(conn, "head", e1, server="dead", at=NOW - 40)
    _queued(conn, "mine", e1, at=NOW - 10)
    assert queue.position(conn, "mine", NOW) == 1  # a dead owner's entry is not ahead
    assert queue.occupancy(conn, [e1], NOW) == 0  # nor counted
    assert queue.try_admit(conn, "mine", _cap(1), NOW) is True
    row = conn.execute("SELECT status, error, finished_at, started_at FROM jobs WHERE id = 'head'").fetchone()
    assert row["status"] == "lost" and row["finished_at"] == NOW and row["started_at"] is None
    assert row["error"]
    assert _status(conn, "run") == "running"
    assert "head" in capsys.readouterr().err


def test_live_owner_reads_the_heartbeat_window(conn):
    """The window is ``jobs.SERVER_LIVE_S`` (imported, not redefined), inclusive at its edge;
    a server that wrote ``stopped`` is gone whatever its last beat; an unknown id is not live."""
    assert queue.SERVER_LIVE_S is jobs.SERVER_LIVE_S
    _server(conn, "edge", updated=NOW - jobs.SERVER_LIVE_S)
    _server(conn, "old", updated=NOW - jobs.SERVER_LIVE_S - 0.001)
    _server(conn, "stopped", updated=NOW, stopped=NOW)
    assert queue.live_owner(conn, "edge", NOW) is True
    assert queue.live_owner(conn, "old", NOW) is False
    assert queue.live_owner(conn, "stopped", NOW) is False
    assert queue.live_owner(conn, "never", NOW) is False


def test_forced_entry_is_counted(conn, e1):
    """§7 ``admit: force`` "skips the wait but still writes an admitted entry, so it is
    counted": a forced entry is admitted at enqueue, past a full endpoint, and the entry
    behind it reads the honest wait."""
    _queued(conn, "a", e1, at=NOW - 20)
    assert queue.try_admit(conn, "a", _cap(1), NOW) is True
    _queued(conn, "f", e1, at=NOW - 10, forced=True)
    assert _entry(conn, "f")["admitted"] == NOW - 10 and _entry(conn, "f")["forced"] == 1
    assert queue.occupancy(conn, [e1], NOW) == 2
    _queued(conn, "b", e1, at=NOW - 5)
    _set_status(conn, "a", "succeeded")
    assert queue.try_admit(conn, "b", _cap(1), NOW) is False
    assert _entry(conn, "b")["blocked_by"] == "endpoint"


def test_drop_removes_an_entry(conn, e1):
    """The ``queue`` tool's ``drop`` (§11) removes the entry; a second drop finds nothing; the
    entry behind it moves up."""
    _queued(conn, "a", e1, at=NOW - 20)
    _queued(conn, "b", e1, at=NOW - 10)
    assert queue.drop(conn, "a", now=NOW) is True
    assert _entry(conn, "a") is None
    assert queue.drop(conn, "a", now=NOW) is False
    assert queue.position(conn, "b", NOW) == 1
    with pytest.raises(KeyError):
        queue.position(conn, "a", NOW)
    queue.remove(conn, "b")
    assert _entry(conn, "b") is None


def test_pool_cap_counts_sessions_across_its_endpoints(conn, e1):
    """Design §7 "Accepted": admission counts occupancy across every endpoint in the pool and
    admits only under both the endpoint's capacity and the pool's cap. Two sessions on two
    endpoints fill a pool of two; a third entry with room on its own endpoint waits, marked
    ``pool``; a session ending anywhere in the pool lets it in."""
    e2 = _endpoint(conn, "e2")
    pool = dict(pool_cap=2, pool_endpoint_ids=[e1, e2])
    _queued(conn, "a", e1, at=NOW - 30)
    _queued(conn, "b", e2, at=NOW - 20)
    assert queue.try_admit(conn, "a", _cap(5), NOW, **pool) is True
    assert queue.try_admit(conn, "b", _cap(5), NOW, **pool) is True
    assert queue.occupancy(conn, [e1, e2], NOW) == 2
    assert queue.occupancy(conn, [e1], NOW) == 1
    _queued(conn, "c", e1, at=NOW - 10)
    assert queue.try_admit(conn, "c", _cap(5), NOW, **pool) is False
    assert _entry(conn, "c")["blocked_by"] == "pool"
    _set_status(conn, "b", "succeeded")  # the other endpoint's session ends
    assert queue.try_admit(conn, "c", _cap(5), NOW, **pool) is True
    assert _entry(conn, "c")["blocked_by"] is None
    with pytest.raises(ValueError):  # a cap without its members (or members without a cap)
        queue.try_admit(conn, "c", _cap(5), NOW, pool_cap=2)


def test_pool_blocked_head_keeps_order_and_marks_blocked_by(conn, e1):
    """A head blocked by the pool does not let a later entry pass it (§7: order is kept). The
    endpoint's own capacity is named first when both are full (``routed_by.pool`` is for a
    wait the pool, not the endpoint, caused). Once the pool frees, a later entry on another
    endpoint of the pool still waits for the earlier pool-blocked head."""
    e2 = _endpoint(conn, "e2")
    pool = dict(pool_cap=1, pool_endpoint_ids=[e1, e2])
    _queued(conn, "x", e1, at=NOW - 50)
    assert queue.try_admit(conn, "x", _cap(1), NOW, **pool) is True
    _queued(conn, "full", e1, at=NOW - 45)
    assert queue.try_admit(conn, "full", _cap(1), NOW, **pool) is False
    assert _entry(conn, "full")["blocked_by"] == "endpoint"  # e1 itself is full
    queue.drop(conn, "full", now=NOW)
    _queued(conn, "a", e2, at=NOW - 40)
    _queued(conn, "b", e2, at=NOW - 30)
    assert queue.try_admit(conn, "a", _cap(2), NOW, **pool) is False
    assert _entry(conn, "a")["blocked_by"] == "pool"
    assert queue.try_admit(conn, "b", _cap(2), NOW, **pool) is False
    assert _entry(conn, "b")["blocked_by"] == "pool"
    _set_status(conn, "x", "succeeded")  # the pool frees
    _queued(conn, "c", e1, at=NOW - 20)  # later than a, on the other endpoint, with room
    assert queue.try_admit(conn, "c", _cap(1), NOW, **pool) is False
    assert _entry(conn, "c")["blocked_by"] == "pool"
    assert queue.try_admit(conn, "a", _cap(2), NOW, **pool) is True
    assert _entry(conn, "a")["blocked_by"] is None
    assert queue.try_admit(conn, "c", _cap(1), NOW, **pool) is False  # the pool is full again
    assert queue.try_admit(conn, "b", _cap(2), NOW, **pool) is False


def test_drop_marks_a_queued_job_dropped_with_started_at_null(conn, e1):
    """Design §7 "Accepted": a job that leaves the queue without a session is ``dropped``,
    ``error`` names the cause, ``started_at`` stays null. ``drop`` on an admitted running
    session removes the stuck entry by hand but never rewrites a job that ran."""
    _queued(conn, "a", e1, at=NOW - 20)
    assert queue.drop(conn, "a", now=NOW) is True
    row = conn.execute("SELECT * FROM jobs WHERE id = 'a'").fetchone()
    assert row["status"] == "dropped"
    assert row["error"] == "dropped by the queue tool"
    assert row["finished_at"] == NOW and row["started_at"] is None
    assert "dropped" in jobs.TERMINAL_STATUSES
    _queued(conn, "r", e1, at=NOW - 10)
    assert queue.try_admit(conn, "r", _cap(1), NOW) is True
    _set_status(conn, "r", "running")
    assert queue.drop(conn, "r", now=NOW) is True
    assert _entry(conn, "r") is None and _status(conn, "r") == "running"


def test_mark_dropped_helper(conn, e1):
    """The helper Task 12's two other causes call (``cancelled while queued``, ``row_down`` /
    ``model_unavailable``): the entry goes, the job is dropped with the caller's error — only
    while it is still queued, and whether or not an entry was ever written."""
    _queued(conn, "a", e1, at=NOW - 20)
    assert queue.mark_dropped(conn, "a", "row_down", NOW) is True
    row = conn.execute("SELECT status, error, finished_at, started_at FROM jobs WHERE id = 'a'").fetchone()
    assert tuple(row) == ("dropped", "row_down", NOW, None)
    assert _entry(conn, "a") is None
    _job(conn, "never-enqueued")
    assert queue.mark_dropped(conn, "never-enqueued", "cancelled while queued", NOW) is True
    assert _status(conn, "never-enqueued") == "dropped"
    _job(conn, "ran", status="running")
    assert queue.mark_dropped(conn, "ran", "row_down", NOW) is False
    assert _status(conn, "ran") == "running"
