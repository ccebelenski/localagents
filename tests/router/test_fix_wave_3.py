"""Router fix wave 3: the phase-3 whole-branch review's findings (docs/superpowers/specs/
2026-11-02-router-design.md §4, §6 step 5 and the bottleneck rule, §7 the queue, "Accepted" and
"Decided" 2026-11-04). Each test's docstring states the RULE its fix satisfies; the reviewer's
probe (t/review/p3/ in the coordinator's checkout) is the RED test it was cut from.

The fleet is a hand-built ``Registry`` over a monkeypatched ``Registry.probe``; d3 is a faked
``dispatch.score``; run_job and the shim are stubbed, so ``server.run_agent`` exercises the real
dispatch, queue and ticket writes against the test's own database."""

import asyncio
import copy
import time

import pytest

from localagents import db, server
from localagents.registry import Defaults, Endpoint, Registry
from localagents.router import definitions, dispatch, queue, scorecard
from localagents.router.capacity import Capacity, prior_curve
from localagents.router.decision import BriefScores

STRONG = {"software_dev": 4, "tool_use": 4, "computer_use": 4, "complexity": 4, "judgement": 4,
          "completeness": 4, "adherence": 4}
NEAR = {**STRONG, "software_dev": 3}   # one band short on software_dev
VALUES = {"software_dev": 3, "complexity": 2, "tool_use": 2}


def _row(conn, endpoint, served, cells):
    """A scorecard row with a complete core run and one pass of these cells."""
    eid = db.lookup_id(conn, "endpoints", name=endpoint, base_url=f"http://{endpoint}:8080",
                       backend="llama.cpp", host="local")
    mid = db.lookup_id(conn, "models", served_id=served)
    row = scorecard.get_or_create_row(conn, endpoint, served, eid, mid)
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
                 " VALUES (?, ?, 'core', '2026.11', '{}', 1.0, 'complete')", (db.new_uuid(), row.id))
    pid = db.new_uuid()
    conn.execute("INSERT INTO scorecard_passes (id, row_id, at, constants, observations)"
                 " VALUES (?, ?, 2.0, '{}', 0)", (pid, row.id))
    v = definitions.load(conn).version
    for d, e in cells.items():
        conn.execute("INSERT INTO scorecard_cells (pass_id, dimension, language, estimate, count,"
                     " probes_n, jobs_n, thin, defs_version) VALUES (?, ?, '', ?, 6, 6, 0, 0, ?)",
                     (pid, d, e, v))
    return row.id


def _fleet(monkeypatch, endpoints: dict[str, Endpoint], facts: dict[str, dict], pools=None):
    eps = dict(endpoints)
    eps["auto"] = Endpoint(name="auto", base_url="", backend="router",
                           decision_url="http://d3.test:8766", divert_after_min=40.0)
    reg = Registry(path=None, endpoints=eps, defaults=Defaults(), raw={}, local={})
    if pools:
        reg.pools = pools

    async def probe(self, ep):
        if ep.is_router:
            return {"up": True, "models": [ep.name], "router": True}
        return copy.deepcopy(facts[ep.name])

    async def fake_score(client, url, model, text, defs, **kw):
        und = {d for d in definitions.DIMENSIONS if d not in VALUES}
        return BriefScores(dict(VALUES), {d: 0.9 for d in VALUES}, und, False, {}, defs.version)

    async def no_run(*a, **kw):
        return None

    async def no_start():
        return 0

    monkeypatch.setattr(Registry, "probe", probe)
    monkeypatch.setattr(server, "_reg", lambda: reg)
    monkeypatch.setattr(server, "run_job", no_run)
    monkeypatch.setattr(server.shim, "start", no_start)
    monkeypatch.setattr(dispatch, "score", fake_score)
    return reg


def _ep(name, **kw):
    return Endpoint(name=name, base_url=f"http://{name}:8080", backend="llama.cpp", **kw)


def _facts(served, total):
    return {"up": True, "models": [served], "context": {served: 131072},
            "slots": {"total": total, "busy": 0}}


def _running(job_id):
    j = server.store.get(job_id)
    j.status, j.started_at = "running", time.time()
    j.changed()
    return j


def _entry(job_id):
    return server.store.conn.execute("SELECT * FROM queue_entries WHERE job_id = ?",
                                     (job_id,)).fetchone()


async def _settle_tasks():
    """Cancel every job task this test started, inside its own loop, so loop close never
    reaches a queued job's loop (item 7's rule is tested on its own)."""
    tasks = [j._task for j in server.store.jobs.values() if j._task is not None and not j._task.done()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --- item 1 (D1): one predicate for admission and the decision -------------------------------

@pytest.fixture
def bottleneck(monkeypatch):
    """One two-slot llama.cpp endpoint on the prior curve (2 x 0.35 < 0.75: bottlenecks at 2)."""
    reg = _fleet(monkeypatch, {"second": _ep("second")}, {"second": _facts("big-model", 2)})
    _row(server.store.conn, "second", "big-model", STRONG)
    return reg


def test_queue_room_applies_the_bottleneck_rule_by_priority():
    """RULE (D1): admission and the decision share ONE predicate, ``queue.room``: capacity's
    sessions rule, the bottleneck rule (the n-th session only for ``background``), the head of
    the queue, and the pool's answer."""
    cap = Capacity("partitioned", 2, curve=prior_curve("llamacpp"))
    assert queue.room(cap, 0, "normal")
    assert not queue.room(cap, 1, "normal")      # the 2nd session is the bottleneck slot
    assert not queue.room(cap, 1, "urgent")
    assert queue.room(cap, 1, "background")
    assert not queue.room(cap, 2, "background")  # full is full
    assert not queue.room(cap, 0, "normal", ahead=1)
    assert not queue.room(cap, 0, "normal", pool_admits=False)
    assert queue.pool_room(None, 99, True)
    assert queue.pool_room(2, 1, False) and not queue.pool_room(2, 2, False)
    assert not queue.pool_room(2, 0, True)


def test_try_admit_reads_the_entry_priority_for_the_bottleneck(conn):
    """RULE (D1): ``try_admit`` reads the entry's priority rank and applies ``room`` with it: a
    normal entry waits for the bottleneck slot (``blocked_by = 'endpoint'``), a background
    entry takes it."""
    now = 100_000.0
    with db.immediate(conn):
        conn.execute("INSERT INTO mcp_servers (id, pid, started, updated) VALUES ('s1', 1, ?, ?)",
                     (now, now))
    eid = db.lookup_id(conn, "endpoints", name="e1", base_url="http://e1:8080", backend="llama.cpp",
                       host="e1", now=now)
    cap = Capacity("partitioned", 2, curve=prior_curve("llamacpp"))
    for jid, prio in (("occ", "normal"), ("n", "normal"), ("bg", "background")):
        with db.immediate(conn):
            conn.execute("INSERT INTO jobs (id, mcp_server_id, status, created_at)"
                         " VALUES (?, 's1', 'queued', ?)", (jid, now))
        queue.enqueue(conn, jid, eid, "s1", prio, forced=jid == "occ", now=now)
    assert not queue.try_admit(conn, "n", cap, now)
    assert conn.execute("SELECT blocked_by FROM queue_entries WHERE job_id = 'n'").fetchone()[0] == "endpoint"
    # the normal head blocks the background behind it (order is kept) until it leaves
    assert not queue.try_admit(conn, "bg", cap, now)
    queue.mark_dropped(conn, "n", "gone", now)
    assert queue.try_admit(conn, "bg", cap, now)


def test_a_routed_job_queued_for_the_bottleneck_slot_waits(bottleneck, tmp_path):
    """RULE (D1): a record that says "queued (bottleneck slot)" is a job that waited: its entry
    and its ticket are not admitted, and ``admit='now'`` refuses it with no job."""
    async def go():
        occ = await server.run_agent(task="occupy", model="big-model", cwd=str(tmp_path))
        _running(occ["id"])
        out = await server.run_agent(task="build it", model="auto", cwd=str(tmp_path))
        entry = dict(_entry(out["id"]))
        before = len(server.store.jobs)
        now_ = await server.run_agent(task="build it too", model="auto", cwd=str(tmp_path),
                                      admit="now")
        after = len(server.store.jobs)
        await _settle_tasks()
        return out, entry, now_, before, after
    out, entry, now_, before, after = asyncio.run(go())
    assert "queued (bottleneck slot)" in out["routed_by"]["reason"]
    assert entry["admitted"] is None and entry["blocked_by"] == "endpoint"
    assert out["ticket"]["admitted"] is None
    assert now_["error"] == "would_queue" and now_.get("id") is None
    assert before == after


def test_admit_now_refuses_what_queue_would_make_wait_for_a_named_job(bottleneck, tmp_path):
    """RULE (D1): ``named_wait``'s pre-check is ``room``: a named normal job's second session on
    a bottlenecked row is ``would_queue`` with no job; a background one is admitted."""
    async def go():
        occ = await server.run_agent(task="occupy", model="big-model", cwd=str(tmp_path))
        _running(occ["id"])
        named = await server.run_agent(task="named 2nd", model="big-model", cwd=str(tmp_path),
                                       admit="now")
        bg = await server.run_agent(task="bg 2nd", model="big-model", cwd=str(tmp_path),
                                    admit="now", priority="background")
        bg_entry = dict(_entry(bg["id"]))
        await _settle_tasks()
        return named, bg, bg_entry
    named, bg, bg_entry = asyncio.run(go())
    assert named["error"] == "would_queue" and named.get("id") is None
    assert bg.get("error") is None and bg_entry["admitted"] is not None


# --- item 2 (D2): a queued job is never pinned to the lesser row -----------------------------

def _occupant(reg, name, served):
    """Another of our sessions on the endpoint: a named job whose entry is admitted (forced)."""
    ep = reg.endpoints[name]
    job = dispatch.new_job(server.store, priority="normal", forced=True, task="occupy", model=served,
                           endpoint=name, base_url=ep.url, cwd="/w", run_cwd="/w",
                           isolation="none", backend=ep.backend, host=ep.host, context=131_072)
    job.status, job.started_at = "running", time.time()
    job.changed()
    return job


def _routed_job(routed, priority="normal"):
    res = routed.resolution
    job = dispatch.new_job(server.store, priority=priority, routed=routed, task="build it",
                           model=res.served_model, endpoint=res.endpoint.name,
                           base_url=res.endpoint.url, cwd="/w", run_cwd="/w", isolation="none",
                           backend=res.endpoint.backend, host=res.endpoint.host,
                           context=res.context)
    job._route = routed
    return job


@pytest.fixture
def two_rows(monkeypatch):
    """first x model-a (the best row) and second x model-b (one band short), one slot each."""
    reg = _fleet(monkeypatch, {"first": _ep("first"), "second": _ep("second")},
                 {"first": _facts("model-a", 1), "second": _facts("model-b", 1)})
    monkeypatch.setattr(dispatch, "score", _score_software_dev_4)
    best = _row(server.store.conn, "first", "model-a", STRONG)
    near = _row(server.store.conn, "second", "model-b", NEAR)
    return reg, best, near


async def _score_software_dev_4(client, url, model, text, defs, **kw):
    values = {"software_dev": 4, "complexity": 2, "tool_use": 2}
    und = {d for d in definitions.DIMENSIONS if d not in values}
    return BriefScores(values, {d: 0.9 for d in values}, und, False, {}, defs.version)


def test_a_diverted_queued_job_returns_to_the_best_row_when_it_frees(two_rows, monkeypatch):
    """RULE (D2, §6 step 5 re-applied at every wake; §4's no-re-route is about ADMITTED jobs):
    a QUEUED job moves to whichever acceptable row the policy would choose now whenever that
    row can admit it now, including back to the best row; the move rewrites the record and the
    ticket (``flags.moved_from``; ``divert_at_wake`` only for a true divert) and keeps the
    entry's original ``enqueued``."""
    reg, best, near = two_rows
    store = server.store
    occ = _occupant(reg, "first", "model-a")
    real = queue.try_admit
    state = {"stolen": None}

    def racing(conn, job_id, capacity, now, **kw):
        e = conn.execute("SELECT e.name FROM queue_entries q JOIN endpoints e ON e.id = q.endpoint_id"
                         " WHERE q.job_id = ?", (job_id,)).fetchone()
        if e is not None and e["name"] == "second" and state["stolen"] is None:
            # another session takes second's one slot between the divert and the claim
            state["stolen"] = _occupant(reg, "second", "model-b")
        return real(conn, job_id, capacity, now, **kw)

    monkeypatch.setattr(queue, "try_admit", racing)

    async def go():
        router = reg.endpoints["auto"]
        router.divert_after_min = 120.0
        routed = await dispatch.route(reg, store.conn, router, "build it", "/w", "normal")
        job = _routed_job(routed)
        enqueued = _entry(job.id)["enqueued"]
        router.divert_after_min = 10.0   # the next wake diverts to second, where the race waits
        t = asyncio.create_task(dispatch.admit_or_wait(store, store.conn, job, reg, poll_s=0.01,
                                                       routed=routed))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if state["stolen"] is not None and job.endpoint == "second":
                break
        diverted = dispatch.read_ticket(store.conn, job.id)
        occ.status, occ.finished_at = "succeeded", time.time()
        occ.changed()   # the best row frees
        res = await asyncio.wait_for(t, 5)
        return job, res, enqueued, diverted
    job, res, enqueued, diverted = asyncio.run(go())
    assert diverted["flags"].get("divert_at_wake") is True and diverted["chosen_row"] == near
    assert res is not None and res.endpoint.name == "first" and job.endpoint == "first"
    t = dispatch.read_ticket(store.conn, job.id)
    assert t["chosen_row"] == best and t["divert"] is False and t["divert_at"] is None
    assert t["flags"]["moved_from"] == near
    assert "divert_at_wake" not in t["flags"] and "diverted_from" not in t["flags"]
    e = _entry(job.id)
    assert e["admitted"] is not None and e["enqueued"] == enqueued
    assert server.store.conn.execute("SELECT e.name FROM jobs j JOIN endpoints e ON e.id = j.endpoint_id"
                                     " WHERE j.id = ?", (job.id,)).fetchone()[0] == "first"


def test_an_admitted_entry_never_moves(two_rows):
    """RULE (D2): a job that has been admitted never moves: ``_reapply`` re-reads the entry in
    its transaction and leaves an admitted one where it is, even when the best row is free."""
    reg, best, near = two_rows
    store = server.store

    async def go():
        router = reg.endpoints["auto"]
        occ = _occupant(reg, "first", "model-a")
        router.divert_after_min = 0.0   # best busy: the decision diverts to second at once
        routed = await dispatch.route(reg, store.conn, router, "build it", "/w", "normal")
        assert routed.decision.divert and routed.resolution.endpoint.name == "second"
        job = _routed_job(routed)
        with db.immediate(store.conn):   # admitted on second (its wake's return not yet seen)
            store.conn.execute("UPDATE queue_entries SET admitted = ? WHERE job_id = ?",
                               (time.time(), job.id))
        occ.status = "succeeded"
        occ.changed()   # the best row frees
        facts = await dispatch.endpoint_facts(reg, store.conn, reg.endpoints["second"], "model-b",
                                              time.time())
        return job, await dispatch._reapply(store.conn, job, reg, routed, facts, time.time())
    job, moved = asyncio.run(go())
    assert moved is None and job.endpoint == "second"
    assert dispatch.read_ticket(store.conn, job.id)["chosen_row"] == near
    assert store.conn.execute("SELECT e.name FROM jobs j JOIN endpoints e ON e.id = j.endpoint_id"
                              " WHERE j.id = ?", (job.id,)).fetchone()[0] == "second"


# --- item 3 (D5): admit='now' against the cross-pool order rule ------------------------------

@pytest.fixture
def pool_box(monkeypatch):
    """Two two-slot vLLM endpoints in pool ``box`` (max_concurrent 1): the vLLM prior does not
    bottleneck at 2, so only the pool decides."""
    eps = {n: Endpoint(name=n, base_url=f"http://{n}:8080", backend="vllm", pool="box")
           for n in ("A", "B")}
    reg = _fleet(monkeypatch, eps, {n: {"up": True, "models": [f"m{n}"], "slots": {"total": 2, "busy": 0}}
                                    for n in eps}, pools={"box": {"max_concurrent": 1}})
    return reg


def test_admit_now_sees_an_earlier_pool_blocked_entry(pool_box, tmp_path):
    """RULE (D5): the ``now`` pre-check uses try_admit's predicate including the strict-order
    clause (an earlier entry anywhere in the pool waits on the pool): it answers
    ``would_queue`` with a position and creates no job, never a drop about a race."""
    async def go():
        holder = await server.run_agent(task="holds the pool", model="mB", cwd=str(tmp_path))
        x = await server.run_agent(task="waits on the pool", model="mA", cwd=str(tmp_path))
        blocked = _entry(x["id"])["blocked_by"]
        h = server.store.get(holder["id"])
        h.status, h.finished_at = "succeeded", time.time()
        h.changed()   # the pool frees; X's mark stands until X's own next wake
        before = set(server.store.jobs)
        y = await server.run_agent(task="y", model="mB", cwd=str(tmp_path), admit="now")
        after = set(server.store.jobs)
        await _settle_tasks()
        return blocked, y, before, after
    blocked, y, before, after = asyncio.run(go())
    assert blocked == "pool"
    assert y["error"] == "would_queue" and y["position"] == 1
    assert "between the check and the claim" not in y["message"]
    assert before == after


def test_a_true_lost_race_is_dropped_with_its_cause(pool_box, tmp_path, monkeypatch):
    """RULE (D5): when the pre-check said yes and the claim said no, the dropped record stays
    and its words say what happened: error ``dispatch.ADMIT_NOW_LOST_RACE``, the reply's
    message "another admission took the slot between the check and the claim"."""
    monkeypatch.setattr(queue, "try_admit", lambda *a, **kw: False)

    async def go():
        y = await server.run_agent(task="y", model="mB", cwd=str(tmp_path), admit="now")
        await _settle_tasks()
        return y
    y = asyncio.run(go())
    assert y["error"] == "would_queue"
    assert "another admission took the slot between the check and the claim" in y["message"]
    row = server.store.conn.execute("SELECT status, error, started_at FROM jobs").fetchone()
    assert (row["status"], row["error"], row["started_at"]) == ("dropped", dispatch.ADMIT_NOW_LOST_RACE, None)


# --- item 4 (D3): no verdict without a session -----------------------------------------------

@pytest.fixture
def one_slot(monkeypatch):
    reg = _fleet(monkeypatch, {"second": _ep("second")}, {"second": _facts("big-model", 1)})
    row_id = _row(server.store.conn, "second", "big-model", STRONG)
    return reg, row_id


def test_a_dropped_job_takes_no_verdict(one_slot, tmp_path):
    """RULE (D3): a job that never had a session (``started_at IS NULL``, whatever its terminal
    status) takes no verdict: ``apply_verdict`` answers ``no_session`` and writes nothing."""
    from localagents.router import feedback
    reg, row_id = one_slot

    async def go():
        occ = await server.run_agent(task="occupy", model="big-model", cwd=str(tmp_path))
        _running(occ["id"])
        q = await server.run_agent(task="build it", model="auto", cwd=str(tmp_path))
        c = await server.cancel_job(q["id"])
        v = await feedback.apply_verdict(server.store.conn, reg, q["id"], False,
                                         {"software_dev": -1})
        await _settle_tasks()
        return c, v
    c, v = asyncio.run(go())
    assert (c["status"], c["started_at"]) == ("dropped", None)
    assert v["error"] == "no_session"
    assert server.store.conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0
    assert scorecard.observations(server.store.conn, row_id) == []


def test_observations_ignore_a_verdict_on_a_sessionless_job(one_slot, tmp_path):
    """RULE (D3, fail-closed for rows written before the fix): ``observations`` ignores verdicts
    whose job has a null ``started_at``; a started job's verdict still reads."""
    reg, row_id = one_slot
    conn = server.store.conn

    async def go():
        a = await server.run_agent(task="build it", model="auto", cwd=str(tmp_path))
        await _settle_tasks()
        return a
    a = asyncio.run(go())
    with db.immediate(conn):
        conn.execute("UPDATE jobs SET status = 'dropped', started_at = NULL, finished_at = 5.0"
                     " WHERE id = ?", (a["id"],))
        conn.execute("INSERT INTO verdicts (job_id, accepted, dimensions, at) VALUES (?, 0, NULL, 6.0)",
                     (a["id"],))
    assert scorecard.observations(conn, row_id) == []
    with db.immediate(conn):
        conn.execute("UPDATE jobs SET status = 'succeeded', started_at = 4.0 WHERE id = ?", (a["id"],))
    assert len(scorecard.observations(conn, row_id)) == 3


# --- item 5 (D4): a clean resumed ticket ------------------------------------------------------

def test_a_resumed_ticket_keeps_the_decision_and_drops_the_previous_jobs_marks(monkeypatch, tmp_path):
    """RULE (D4): a resumed ticket keeps the scores, rows, chosen row, pass and defs version
    (the decision it inherits) and ``resumed``/``resumed_from``; it DROPS ``stalled``,
    ``forced``, ``row_down``, ``diverted_from``, ``divert_at_wake``, ``position``,
    ``admitted``; ``priority`` and ``forced`` come from the NEW call, and ``explain`` (the
    ticket read whole) and the summary agree."""
    _fleet(monkeypatch, {"second": _ep("second")}, {"second": _facts("big-model", 4)})
    _row(server.store.conn, "second", "big-model", STRONG)
    conn = server.store.conn

    async def go():
        first = await server.run_agent(task="build", model="auto", cwd=str(tmp_path), admit="force")
        j = server.store.get(first["id"])
        dispatch._merge_flags(conn, j.id, stalled={"at": 1.0, "notice": "stalled"},
                              row_down={"why": "x", "guidance": "y"}, diverted_from=7,
                              divert_at_wake=True)
        j.status, j.session_id, j.started_at, j.finished_at = "succeeded", "sess", time.time(), time.time()
        j._done.set()
        j.changed()
        again = await server.run_agent(task="continue", resume_job=j.id, cwd=str(tmp_path),
                                       priority="urgent")
        forced = await server.run_agent(task="continue 2", resume_job=j.id, cwd=str(tmp_path),
                                        admit="force")
        await _settle_tasks()
        return first, again, forced
    first, again, forced = asyncio.run(go())
    prev = dispatch.read_ticket(conn, first["id"])
    t = dispatch.read_ticket(conn, again["id"])
    for k in ("scores", "rows", "chosen_row", "pass_id", "defs_version"):
        assert t[k] == prev[k], k
    assert t["flags"]["resumed"] is True and t["flags"]["resumed_from"] == first["id"]
    for k in ("stalled", "forced", "row_down", "diverted_from", "divert_at_wake", "position"):
        assert k not in t["flags"], k
    assert t["flags"]["priority"] == "urgent"
    assert again["ticket"]["priority"] == "urgent" and "stalled" not in again["ticket"]
    assert again["ticket"]["resumed"] is True and again["forced"] is False
    assert dispatch.read_ticket(conn, forced["id"])["flags"]["forced"] is True
    assert forced["forced"] is True


# --- item 7 (N1): a server exit is not a cancel -----------------------------------------------

def test_loop_close_leaves_a_queued_row_for_the_stale_owner_rule(one_slot, tmp_path):
    """RULE (N1): ``cancelled while queued`` is written only when ``cancel_job`` asked for it;
    any other CancelledError in the queue loop (loop close, server exit) leaves the row
    ``queued`` with its entry for the stale-owner rule, and settles the job in memory as
    ``dropped``, ``server exited while queued``."""
    async def go():
        a = await server.run_agent(task="a", model="big-model", cwd=str(tmp_path))
        _running(a["id"])
        b = await server.run_agent(task="b", model="big-model", cwd=str(tmp_path))
        await asyncio.sleep(0.05)
        return b
    b = asyncio.run(go())   # asyncio.run cancels the remaining tasks at loop close
    job = server.store.get(b["id"])
    assert (job.status, job.error, job.settled) == ("dropped", dispatch.SERVER_EXITED_WHILE_QUEUED, True)
    row = server.store.conn.execute("SELECT status, error, started_at FROM jobs WHERE id = ?",
                                    (b["id"],)).fetchone()
    assert tuple(row) == ("queued", None, None)
    assert _entry(b["id"]) is not None


def test_the_stale_sweep_marks_an_exited_servers_queued_job_lost(one_slot, tmp_path):
    """RULE (N1): the row left ``queued`` by a server exit is settled by the stale-owner rule
    with its existing words once that server stops (its row ``stopped``)."""
    async def go():
        a = await server.run_agent(task="a", model="big-model", cwd=str(tmp_path))
        _running(a["id"])
        b = await server.run_agent(task="b", model="big-model", cwd=str(tmp_path))
        await asyncio.sleep(0.05)
        return b
    b = asyncio.run(go())
    conn = server.store.conn
    eid = _entry(b["id"])["endpoint_id"]
    with db.immediate(conn):
        conn.execute("UPDATE mcp_servers SET stopped = ? WHERE id = ?", (time.time(), server.store.server_id))
        queue._mark_stale(conn, [eid], time.time())
    row = conn.execute("SELECT status, error FROM jobs WHERE id = ?", (b["id"],)).fetchone()
    assert tuple(row) == ("lost", queue.LOST_WHILE_QUEUED)


def test_cancel_job_still_writes_cancelled_while_queued(one_slot, tmp_path):
    """RULE (N1): the operator's cancel (``cancel_job``) keeps its words in the row."""
    async def go():
        a = await server.run_agent(task="a", model="big-model", cwd=str(tmp_path))
        _running(a["id"])
        b = await server.run_agent(task="b", model="big-model", cwd=str(tmp_path))
        await asyncio.sleep(0.05)
        out = await server.cancel_job(b["id"])
        await _settle_tasks()
        return b, out
    b, out = asyncio.run(go())
    assert (out["status"], out["error"]) == ("dropped", dispatch.CANCELLED_WHILE_QUEUED)
    row = server.store.conn.execute("SELECT status, error FROM jobs WHERE id = ?", (b["id"],)).fetchone()
    assert tuple(row) == ("dropped", dispatch.CANCELLED_WHILE_QUEUED)


# --- item 8 (N2): the queue tool's drop leaves a finished job's record -----------------------

def test_drop_acts_only_on_a_queued_or_running_job(conn):
    """RULE (N2): ``drop`` acts only on a job whose status is ``queued`` or ``running``; on a
    terminal job it answers False and deletes nothing (a finished forced job still reads
    ``forced``)."""
    now = 100_000.0
    with db.immediate(conn):
        conn.execute("INSERT INTO mcp_servers (id, pid, started, updated) VALUES ('s1', 1, ?, ?)",
                     (now, now))
    eid = db.lookup_id(conn, "endpoints", name="e1", base_url="http://e1:8080", backend="vllm",
                       host="e1", now=now)
    for jid, status in (("done", "succeeded"), ("run", "running"), ("wait", "queued")):
        with db.immediate(conn):
            conn.execute("INSERT INTO jobs (id, mcp_server_id, status, created_at) VALUES (?, 's1', 'queued', ?)",
                         (jid, now))
        queue.enqueue(conn, jid, eid, "s1", "normal", forced=jid != "wait", now=now)
        with db.immediate(conn):
            conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, jid))
    assert queue.drop(conn, "done", now=now) is False
    assert conn.execute("SELECT forced FROM queue_entries WHERE job_id = 'done'").fetchone()[0] == 1
    assert queue.drop(conn, "run", now=now) is True
    assert conn.execute("SELECT status FROM jobs WHERE id = 'run'").fetchone()[0] == "running"
    assert queue.drop(conn, "wait", now=now) is True
    assert tuple(conn.execute("SELECT status, error FROM jobs WHERE id = 'wait'").fetchone()) == (
        "dropped", queue.DROPPED_BY_QUEUE_TOOL)
    assert queue.drop(conn, "nobody", now=now) is False


# --- item 9 (N3): the stall flag write is guarded and retried ---------------------------------

def _stalled_pair(reg):
    """Two routed running jobs whose last progress (their start) is 20 minutes ago."""
    store = server.store

    async def go():
        out = []
        for _ in range(2):
            routed = await dispatch.route(reg, store.conn, reg.endpoints["auto"], "build it", "/w",
                                          "normal")
            job = _routed_job(routed)
            job.status, job.started_at = "running", time.time() - 1200
            job.changed()
            out.append(job)
        return out
    return asyncio.run(go())


def test_a_failed_stall_flag_write_is_retried_and_never_stops_the_scan(two_rows, monkeypatch):
    """RULE (N3): the stall flag write is guarded like the row_down one; ``_stall_seen`` is set
    only after the write succeeded (so the next scan retries); one job's failure never stops
    the scan over the others."""
    import sqlite3
    reg, best, near = two_rows
    a, b = _stalled_pair(reg)
    real = dispatch._merge_flags
    fail = {a.id}

    def flaky(conn, job_id, **flags):
        if job_id in fail:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, job_id, **flags)

    monkeypatch.setattr(dispatch, "_merge_flags", flaky)
    conn = server.store.conn
    asyncio.run(dispatch.stall_scan(server.store, conn, reg, time.time()))
    assert a._stall_seen is None and "stalled" not in dispatch.read_ticket(conn, a.id)["flags"]
    assert b._stall_seen is not None and "stalled" in dispatch.read_ticket(conn, b.id)["flags"]
    fail.clear()
    asyncio.run(dispatch.stall_scan(server.store, conn, reg, time.time()))
    assert a._stall_seen is not None and "stalled" in dispatch.read_ticket(conn, a.id)["flags"]
    assert a.last_activity.startswith("stalled: no progress for 20 min")


# --- item 10 (N4): an open tool call is activity now ------------------------------------------

def test_an_open_tool_call_is_activity_now(two_rows):
    """RULE (N4): an OPEN tool call (``finished IS NULL``) is activity now: ``_last_progress``
    returns ``now`` while any tool call of the job is open, so a long pytest is no stall; once
    it finishes, its finish is the progress moment."""
    reg, best, near = two_rows
    a, b = _stalled_pair(reg)
    conn = server.store.conn
    began = time.time() - 1000
    with db.immediate(conn):
        conn.execute("INSERT INTO tool_calls (job_id, t, finished, tool, tool_use_id) VALUES (?, ?, NULL,"
                     " 'Bash', 'tu1')", (a.id, began))
    now = time.time()
    assert dispatch._last_progress(conn, a, now) == now
    asyncio.run(dispatch.stall_scan(server.store, conn, reg, now))
    assert a._stall_seen is None and "stalled" not in dispatch.read_ticket(conn, a.id)["flags"]
    assert b._stall_seen is not None   # b has no open call: still a stall
    with db.immediate(conn):
        conn.execute("UPDATE tool_calls SET finished = ? WHERE job_id = ?", (began + 10, a.id))
    assert dispatch._last_progress(conn, a, now) == began + 10


# --- item 11 (N6): garbled flags never block the admission path ------------------------------

def test_garbled_ticket_flags_never_raise_on_the_admission_path(two_rows):
    """RULE (N6): unreadable ``router_tickets.flags`` read as ``{}`` with an ``unreadable``
    mark (as ``read_ticket`` already does), never an exception on the admission path: the
    wake's re-applied policy still moves the job and it reaches ``try_admit_now``."""
    reg, best, near = two_rows
    store = server.store

    async def go():
        occ = _occupant(reg, "first", "model-a")
        router = reg.endpoints["auto"]
        router.divert_after_min = 120.0
        routed = await dispatch.route(reg, store.conn, router, "build it", "/w", "normal")
        job = _routed_job(routed)
        with db.immediate(store.conn):
            store.conn.execute("UPDATE router_tickets SET flags = '{not json' WHERE job_id = ?", (job.id,))
        router.divert_after_min = 0.0   # the next wake diverts to the free second row
        res = await asyncio.wait_for(dispatch.admit_or_wait(store, store.conn, job, reg, poll_s=0.01,
                                                            routed=routed), 5)
        return job, res, occ
    job, res, occ = asyncio.run(go())
    assert res is not None and res.endpoint.name == "second"
    flags = dispatch.read_ticket(store.conn, job.id)["flags"]
    assert flags["unreadable"] is True and flags["divert_at_wake"] is True
    assert dispatch._flags_of("[1, 2]") == {"unreadable": True} and dispatch._flags_of(None) == {}


def test_read_ticket_reads_non_object_flags_as_unreadable(two_rows):
    """RULE (N6): a flags column holding JSON that is not an object reads as ``{}`` and is
    named in ``unreadable``, so ``ticket_summary`` never calls ``.get`` on a list."""
    reg, best, near = two_rows
    a, _ = _stalled_pair(reg)
    conn = server.store.conn
    with db.immediate(conn):
        conn.execute("UPDATE router_tickets SET flags = '[1, 2]' WHERE job_id = ?", (a.id,))
    t = dispatch.read_ticket(conn, a.id)
    assert t["flags"] == {} and t["unreadable"] == ["flags"]
    assert dispatch.ticket_summary(t)["unreadable"] == ["flags"]
