"""Router 12 (docs/superpowers/specs/2026-11-02-router-design.md §4 the dispatch path and its
"Consumer requirement", §6 selection, §7 queues, "Accepted" and "Decided" 2026-11-04, §14):
``dispatch.route`` resolves, scores, reads the fleet's facts and selects; ``admit_or_wait`` is
the queued job's loop (the admission re-probe, the policy re-applied at a wake, a wake that
fails and retries).

d3 is a faked ``dispatch.score`` (or a MockTransport for the transport failure); the fleet is a
monkeypatched ``Registry.probe`` over a hand-built ``Registry``; scorecard rows are written by
hand with a complete core run and one pass of cells, so ``core_complete`` and ``cells`` read
them exactly as a calibration would leave them."""

import asyncio
import copy
import json
import time

import httpx
import pytest

from localagents import db, jobs
from localagents.registry import Defaults, Endpoint, Registry
from localagents.router import definitions, dispatch, queue, scorecard
from localagents.router.decision import BriefScores, DecisionUnavailable

STRONG = {"software_dev": 4, "tool_use": 4, "computer_use": 4, "complexity": 4, "judgement": 4,
          "completeness": 4, "adherence": 4}
WEAK = {"software_dev": 1, "tool_use": 1, "computer_use": 1, "complexity": 1, "judgement": 1,
        "completeness": 4, "adherence": 4}
NEAR = {**STRONG, "software_dev": 3}   # one band short on software_dev


def _fleet(names=("first", "second")):
    """Real endpoints (one slot each) plus the router entry ``auto``; the facts dict is what
    the fake probe answers, mutable by the test."""
    endpoints = {n: Endpoint(name=n, base_url=f"http://{n}:8080", backend="llama.cpp") for n in names}
    endpoints["auto"] = Endpoint(name="auto", base_url="", backend="router",
                                 decision_url="http://d3.test:8766", divert_after_min=40.0)
    reg = Registry(path=None, endpoints=endpoints, defaults=Defaults(), raw={}, local={})
    served = {"first": "model-a", "second": "model-b"}
    facts = {n: {"up": True, "models": [served[n]], "context": {served[n]: 131_072},
                 "slots": {"total": 1, "busy": 0}} for n in names}
    return reg, facts


def _patch_probe(monkeypatch, facts, calls=None):
    async def probe(self, ep):
        if calls is not None:
            calls.append(ep.name)
        if ep.is_router:
            return {"up": True, "models": [ep.name], "router": True}
        got = facts[ep.name]
        if isinstance(got, Exception):
            raise got
        return copy.deepcopy(got)

    monkeypatch.setattr(Registry, "probe", probe)


def _patch_score(monkeypatch, values, seen=None):
    async def fake(client, url, model, text, defs, **kw):
        if seen is not None:
            seen.append(text)
        undetermined = {d for d in definitions.DIMENSIONS if d not in values}
        return BriefScores(dict(values), {d: 0.9 for d in values}, undetermined, False,
                           {"complexity": {"type": "score"}}, defs.version)

    monkeypatch.setattr(dispatch, "score", fake)


def _calibrated(conn, endpoint, served, cells, *, complete=True):
    """A scorecard row with (by default) a complete core run and one pass of these cells."""
    eid = db.lookup_id(conn, "endpoints", name=endpoint, base_url=f"http://{endpoint}:8080",
                       backend="llama.cpp", host="local")
    mid = db.lookup_id(conn, "models", served_id=served)
    row = scorecard.get_or_create_row(conn, endpoint, served, eid, mid)
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
                 " VALUES (?, ?, 'core', '2026.11', '{}', 1.0, ?)",
                 (db.new_uuid(), row.id, "complete" if complete else "incomplete"))
    pass_id = db.new_uuid()
    conn.execute("INSERT INTO scorecard_passes (id, row_id, at, constants, observations)"
                 " VALUES (?, ?, 2.0, '{}', 0)", (pass_id, row.id))
    version = definitions.load(conn).version
    for dimension, estimate in cells.items():
        conn.execute("INSERT INTO scorecard_cells (pass_id, dimension, language, estimate, count,"
                     " probes_n, jobs_n, thin, defs_version) VALUES (?, ?, '', ?, 6, 6, 0, 0, ?)",
                     (pass_id, dimension, estimate, version))
    return row.id


@pytest.fixture
def store():
    s = jobs.JobStore()
    yield s
    s.close()


def _occupant(store, reg, name, served, status="running"):
    """Another of our sessions on the endpoint: a named job whose entry is admitted."""
    ep = reg.endpoints[name]
    job = dispatch.new_job(store, priority="normal", forced=True, task="occupy", model=served,
                           endpoint=name, base_url=ep.url, cwd="/w", run_cwd="/w",
                           isolation="none", backend=ep.backend, host=ep.host, context=131_072)
    job.status, job.started_at = status, time.time()
    job.changed()
    return job


def _routed_job(store, routed, priority="normal"):
    """The job record server.start_job writes for a routed decision: the real row's ids, the
    ticket and the queue entry in one transaction, the routed_by record."""
    res = routed.resolution
    job = dispatch.new_job(store, priority=priority, routed=routed, task="build it",
                           model=res.served_model, endpoint=res.endpoint.name,
                           base_url=res.endpoint.url, cwd="/w", run_cwd="/w", isolation="none",
                           backend=res.endpoint.backend, host=res.endpoint.host,
                           context=res.context)
    job._route = routed
    return job


# --- route -----------------------------------------------------------------------------------

def test_route_scores_resolves_and_picks(conn, monkeypatch, tmp_path):
    """§4 steps 2-4: the brief is scored, every real endpoint's facts read, the rows ranked;
    the stronger row wins and the Routed carries the real endpoint, the ticket's table and the
    routed_by reason (the chosen row's shortfall line)."""
    reg, facts = _fleet()
    _patch_probe(monkeypatch, facts)
    _patch_score(monkeypatch, {"software_dev": 3, "complexity": 2, "tool_use": 2})
    weak = _calibrated(conn, "first", "model-a", WEAK)
    strong = _calibrated(conn, "second", "model-b", STRONG)
    routed = asyncio.run(dispatch.route(reg, conn, reg.endpoints["auto"], "build the parser",
                                        str(tmp_path), "normal"))
    assert routed.resolution.endpoint.name == "second"
    assert routed.resolution.served_model == "model-b"
    assert routed.resolution.context == 131_072
    rows = {r["row_id"]: r for r in routed.ticket["rows"]}
    assert set(rows) == {weak, strong}
    assert rows[strong]["shortfall"] == 0.0 and rows[strong]["acceptable"] is True
    # weak: (2 * 3/4 + 1 * 2/4 + 1 * 2/4) / 3 = 0.8333 on the three determined axes
    assert rows[weak]["shortfall"] == pytest.approx(2.5 / 3)
    assert rows[weak]["acceptable"] is False
    assert routed.ticket["chosen_row"] == strong and routed.ticket["divert"] is False
    assert routed.ticket["scores"]["values"] == {"software_dev": 3, "complexity": 2, "tool_use": 2}
    assert routed.ticket["defs_version"] == 1
    assert routed.routed_by == {"router": "auto", "reason": routed.decision.chosen_reason,
                                "pool": None}
    assert routed.routed_by["reason"].startswith("shortfall 0.00 on second x model-b")
    assert routed.ticket["est_wait_s"] == 0.0
    # admitted at once is no queue position at all (None, not 0: 0 would mean "head of queue")
    assert routed.ticket["position"] is None
    # the plan's write_ticket: the ticket row round-trips through read_ticket
    conn.execute("INSERT INTO jobs (id, status, created_at) VALUES ('j1', 'queued', 1.0)")
    dispatch.write_ticket(conn, "j1", routed)
    back = dispatch.read_ticket(conn, "j1")
    assert back["chosen_row"] == strong and back["rows"] == routed.ticket["rows"]
    assert back["scores"]["values"] == routed.ticket["scores"]["values"]
    assert back["position"] is None and back["est_wait_s"] == 0.0
    assert back["flags"]["reason"] == routed.routed_by["reason"] and back["divert"] is False


def test_route_pointer_brief_is_read_before_scoring(tmp_path, monkeypatch):
    """§4 step 1: a pointer brief is read and the FILE's text is what d3 scores; a missing file
    is BriefUnresolved before any scoring."""
    conn = db.open_db(tmp_path / "p.db")
    try:
        reg, facts = _fleet()
        _patch_probe(monkeypatch, facts)
        seen = []
        _patch_score(monkeypatch, {"software_dev": 2}, seen)
        _calibrated(conn, "second", "model-b", STRONG)
        (tmp_path / "brief.md").write_text("Refactor the tokenizer into its own module.\n")
        routed = asyncio.run(dispatch.route(reg, conn, reg.endpoints["auto"],
                                            "cat `brief.md` and follow it", str(tmp_path),
                                            "normal"))
        assert seen == ["Refactor the tokenizer into its own module.\n"]
        assert routed.text == "Refactor the tokenizer into its own module.\n"
        from localagents.router.decision import BriefUnresolved
        with pytest.raises(BriefUnresolved):
            asyncio.run(dispatch.route(reg, conn, reg.endpoints["auto"], "cat gone.md and follow it",
                                       str(tmp_path), "normal"))
        assert len(seen) == 1  # the missing file was never scored
    finally:
        conn.close()


def test_route_refuses_no_calibrated_row_with_guidance(tmp_path, monkeypatch):
    """§6: with no complete core run on any row, ``auto`` refuses with no_calibrated_row and
    the guidance names the calibrate command; every row is listed with its filter."""
    conn = db.open_db(tmp_path / "n.db")
    try:
        reg, facts = _fleet()
        _patch_probe(monkeypatch, facts)
        _patch_score(monkeypatch, {"software_dev": 2})
        row = _calibrated(conn, "first", "model-a", STRONG, complete=False)
        with pytest.raises(dispatch.NoRoute) as got:
            asyncio.run(dispatch.route(reg, conn, reg.endpoints["auto"], "t", str(tmp_path), "normal"))
        out = got.value.to_dict()
        assert out["error"] == "no_calibrated_row"
        assert "localagents calibrate" in out["guidance"]
        assert [(c["row_id"], c["filtered"]) for c in out["considered"]] == [(row, "uncalibrated")]
    finally:
        conn.close()


def test_route_d3_down_is_model_unavailable(tmp_path, monkeypatch):
    """§4, §14: d3 unreachable is DecisionUnavailable(down), which answers model_unavailable
    with the guidance "name a model, or check the d3 server"."""
    conn = db.open_db(tmp_path / "d.db")
    try:
        reg, facts = _fleet()
        _patch_probe(monkeypatch, facts)
        _calibrated(conn, "second", "model-b", STRONG)

        def down(request):
            raise httpx.ConnectError("connection refused", request=request)

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
                return await dispatch.route(reg, conn, reg.endpoints["auto"], "t", str(tmp_path),
                                            "normal", client=client)

        with pytest.raises(DecisionUnavailable) as got:
            asyncio.run(go())
        out = got.value.to_dict()
        assert out["error"] == "model_unavailable" and out["reason"] == "down"
        assert out["guidance"] == "name a model, or check the d3 server"
    finally:
        conn.close()


# --- admit_or_wait ---------------------------------------------------------------------------

def test_admit_or_wait_fails_row_down_when_the_endpoint_changed(store, monkeypatch):
    """§4 step 6, §7 "Accepted": at its admission the head of the queue finds its endpoint
    serving another id: the routed job is ``dropped`` with error ``row_down``, its entry gone,
    ``started_at`` null, and its ticket carries the guidance; a named job in the same place is
    ``model_unavailable``. Nothing re-routes it."""
    reg, facts = _fleet(("first",))
    _patch_probe(monkeypatch, facts)
    _patch_score(monkeypatch, {"software_dev": 2})
    _calibrated(store.conn, "first", "model-a", STRONG)
    _occupant(store, reg, "first", "model-a")

    async def go():
        routed = await dispatch.route(reg, store.conn, reg.endpoints["auto"], "build it", "/w",
                                      "normal")
        job = _routed_job(store, routed)
        assert routed.decision.queue_for is not None  # one slot, taken: it queues
        facts["first"]["models"] = ["model-z"]  # the operator swapped the model meanwhile
        got = await dispatch.admit_or_wait(store, store.conn, job, reg, poll_s=0.01, routed=routed)
        return job, got

    job, got = asyncio.run(go())
    assert got is None
    assert job.status == "dropped" and job.error == "row_down" and job.started_at is None
    assert job.settled
    row = store.conn.execute("SELECT status, error, started_at FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert tuple(row) == ("dropped", "row_down", None)
    assert store.conn.execute("SELECT 1 FROM queue_entries WHERE job_id = ?", (job.id,)).fetchone() is None
    ticket = dispatch.read_ticket(store.conn, job.id)
    assert ticket["flags"]["row_down"]["guidance"] == "resubmit; the brief is rescored"
    assert dispatch.ticket_summary(ticket)["guidance"] == "resubmit; the brief is rescored"

    # a named job at the head of the same changed row: model_unavailable, as today
    named = dispatch.new_job(store, priority="normal", task="t", model="model-a", endpoint="first",
                             base_url="http://first:8080", cwd="/w", run_cwd="/w",
                             isolation="none", backend="llama.cpp", host="local", context=131_072)
    got = asyncio.run(dispatch.admit_or_wait(store, store.conn, named, reg, poll_s=0.01))
    assert got is None and named.status == "dropped" and named.error == "model_unavailable"
    assert named.started_at is None and dispatch.read_ticket(store.conn, named.id) is None


def test_admit_or_wait_revisits_divert_at_a_wake(store, monkeypatch):
    """§6 step 5: the priority policy is re-applied at every wake. A normal job queued for the
    best row (its wait under divert_after) diverts to the free acceptable row once the wait
    crosses the threshold: the entry moves, the record names the new real endpoint, the ticket
    records the divert and when, and the job is admitted there."""
    reg, facts = _fleet()
    _patch_probe(monkeypatch, facts)
    _patch_score(monkeypatch, {"software_dev": 4, "complexity": 2, "tool_use": 2})
    best = _calibrated(store.conn, "first", "model-a", STRONG)
    near = _calibrated(store.conn, "second", "model-b", NEAR)
    _occupant(store, reg, "first", "model-a")   # the best row's one slot is taken
    # one more job waiting ahead of ours on the best row: the ticket's enqueue position is 2
    dispatch.new_job(store, priority="normal", task="ahead", model="model-a", endpoint="first",
                     base_url="http://first:8080", cwd="/w", run_cwd="/w", isolation="none",
                     backend="llama.cpp", host="local", context=131_072)

    async def go():
        router = reg.endpoints["auto"]
        # the queued job ahead makes the best row's expected wait 2 x 1800 s: above the
        # default 40 min threshold, so the route itself is given a wider one and the test's
        # wake below is the first place the policy asks the divert question
        router.divert_after_min = 120.0
        routed = await dispatch.route(reg, store.conn, router, "build it", "/w", "normal")
        assert routed.decision.queue_for is not None and routed.resolution.endpoint.name == "first"
        assert routed.ticket["position"] == 2   # behind the job already waiting there
        job = _routed_job(store, routed)
        before = time.time()
        router.divert_after_min = 10.0   # the wait now crosses the threshold
        got = await dispatch.admit_or_wait(store, store.conn, job, reg, poll_s=0.01, routed=routed)
        return job, got, before

    job, got, before = asyncio.run(go())
    assert got is not None and got.endpoint.name == "second" and got.served_model == "model-b"
    assert (job.endpoint, job.model, job.base_url) == ("second", "model-b", "http://second:8080")
    entry = store.conn.execute("SELECT endpoint_id, admitted FROM queue_entries WHERE job_id = ?",
                               (job.id,)).fetchone()
    assert entry["endpoint_id"] == job.endpoint_id and entry["admitted"] is not None
    row = store.conn.execute("SELECT e.name, m.served_id, j.routed_by FROM jobs j"
                             " JOIN endpoints e ON e.id = j.endpoint_id"
                             " JOIN models m ON m.id = j.model_id WHERE j.id = ?", (job.id,)).fetchone()
    assert (row["name"], row["served_id"]) == ("second", "model-b")
    assert json.loads(row["routed_by"])["reason"].startswith("divert: second x model-b free")
    ticket = dispatch.read_ticket(store.conn, job.id)
    assert ticket["chosen_row"] == near and ticket["divert"] is True
    assert ticket["divert_at"] >= before and ticket["flags"]["diverted_from"] == best
    assert ticket["admitted"] is not None
    assert ticket["position"] == 1   # the NEW row's position at the wake, not the enqueue's 2


def test_admit_or_wait_survives_an_exception_and_keeps_the_job_queued(store, monkeypatch, capsys):
    """§7, §14: a wake that fails (here the probe raises) is logged, sleeps and retries; the
    job stays queued and its task alive, and the next wake admits it once the slot frees."""
    reg, facts = _fleet(("first",))
    _patch_probe(monkeypatch, facts)
    occupant = _occupant(store, reg, "first", "model-a")
    job = dispatch.new_job(store, priority="normal", task="t", model="model-a", endpoint="first",
                           base_url="http://first:8080", cwd="/w", run_cwd="/w",
                           isolation="none", backend="llama.cpp", host="local", context=131_072)
    wakes = []

    async def go():
        facts["first"] = RuntimeError("probe exploded")
        task = asyncio.create_task(dispatch.admit_or_wait(store, store.conn, job, reg, poll_s=0.01))
        await asyncio.sleep(0.08)
        wakes.append((task.done(), job.status))
        facts["first"] = {"up": True, "models": ["model-a"], "context": {"model-a": 131_072},
                          "slots": {"total": 1, "busy": 0}}
        occupant.status = "succeeded"
        occupant.changed()
        dispatch.notify()
        return await asyncio.wait_for(task, 5)

    got = asyncio.run(go())
    assert wakes == [(False, "queued")]   # alive and still queued through the failing wakes
    assert got is not None and got.endpoint.name == "first"
    assert "probe exploded" in (open(f"{job.log_dir}/transcript.txt").read())
    entry = store.conn.execute("SELECT admitted FROM queue_entries WHERE job_id = ?", (job.id,)).fetchone()
    assert entry["admitted"] is not None
