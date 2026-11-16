"""The bottleneck rule holds a slot only on a MEASURED point of the curve (router-design spec §6
"Bottleneck rule", True-now 2026-11-15, the operator's ruling; §7 "Throughput curve" and
"Directly named jobs", True-now 2026-11-15):

    "The rule holds a slot only on a MEASURED point of the curve: one the throughput probe wrote,
    or one refined from speed samples at that concurrency. A point that is still the
    backend-family prior predicts speed and expected wait and holds nothing, including on a curve
    whose other points are measured. A named job obeys a measured bottleneck like a routed one;
    `admit: force` is the override."

The live failure: a two-slot llama.cpp endpoint with no endpoint_capacity row ran on the prior
curve (2 x 0.35 = 0.70 < 0.75) and refused its second slot to every normal job all afternoon."""

import json

import pytest

from localagents import db
from localagents.router import queue
from localagents.router.capacity import (Capacity, Curve, load_capacity, prior_curve,
                                         save_capacity)
from localagents.router.decision import BriefScores
from localagents.router.scorecard import Cell, Row
from localagents.router.select import Candidate, select

# the llama.cpp prior's shape, every point measured (what the throughput probe writes when the
# box really degrades that way): bottlenecks at 2, 2 x 0.35 = 0.70 < 0.75
MEASURED = Curve({1: 1.0, 2: 0.35}, "probed", 2, frozenset({1, 2}))


def test_prior_llamacpp_curve_does_not_hold_the_second_slot():
    """RULE: a prior point predicts and holds nothing. The live box: two slots, one of ours
    running, the curve the llama.cpp prior: a normal job has room for the second slot."""
    cap = Capacity("partitioned", 2, requests_running=1, curve=prior_curve("llamacpp"))
    assert cap.admits(1)
    assert not cap.bottleneck_slot(1)
    assert queue.endpoint_room(cap, 1, "normal")
    assert queue.endpoint_room(cap, 1, "urgent")
    assert queue.room(cap, 1, "normal")
    # the same box seen with the running session foreign (no entry of ours): still room
    assert queue.endpoint_room(cap, 0, "normal")


# --- through admission (try_admit over a real store) and the decision (select) ----------------

NOW = 100_000.0


def _endpoint_and_occupant(conn):
    """An endpoint row, a live server, and one admitted (forced) normal job of ours on it."""
    with db.immediate(conn):
        conn.execute("INSERT INTO mcp_servers (id, pid, started, updated) VALUES ('s1', 1, ?, ?)",
                     (NOW, NOW))
    eid = db.lookup_id(conn, "endpoints", name="e1", base_url="http://e1:8080",
                       backend="llama.cpp", host="e1", now=NOW)
    _enqueue(conn, eid, "occ", "normal", forced=True)
    return eid


def _enqueue(conn, eid, jid, priority, forced=False):
    with db.immediate(conn):
        conn.execute("INSERT INTO jobs (id, mcp_server_id, status, created_at)"
                     " VALUES (?, 's1', 'queued', ?)", (jid, NOW))
    queue.enqueue(conn, jid, eid, "s1", priority, forced=forced, now=NOW)


def _blocked_by(conn, jid):
    return conn.execute("SELECT blocked_by FROM queue_entries WHERE job_id = ?",
                        (jid,)).fetchone()[0]


@pytest.mark.parametrize("priority", ["normal", "urgent"])
def test_prior_curve_never_holds_through_try_admit(conn, priority):
    """RULE: a prior point holds nothing. Two slots, one of ours admitted, the llama.cpp prior:
    a normal or urgent entry takes the second slot in ``try_admit`` (over a real store)."""
    eid = _endpoint_and_occupant(conn)
    _enqueue(conn, eid, "j", priority)
    cap = Capacity("partitioned", 2, requests_running=1, curve=prior_curve("llamacpp"))
    assert queue.try_admit(conn, "j", cap, NOW)
    assert _blocked_by(conn, "j") is None


def test_measured_bottleneck_holds_normal_and_urgent_admits_background_through_try_admit(conn):
    """RULE: a MEASURED bottleneck still holds: the second slot is background's only. A normal
    and an urgent entry wait (``blocked_by = 'endpoint'``, its two values kept); the background
    entry takes the slot once the higher-priority entries ahead of it leave."""
    eid = _endpoint_and_occupant(conn)
    for jid, prio in (("u", "urgent"), ("n", "normal"), ("bg", "background")):
        _enqueue(conn, eid, jid, prio)
    cap = Capacity("partitioned", 2, requests_running=1, curve=MEASURED)
    assert not queue.try_admit(conn, "u", cap, NOW)
    assert _blocked_by(conn, "u") == "endpoint"
    queue.mark_dropped(conn, "u", "gone", NOW)
    assert not queue.try_admit(conn, "n", cap, NOW)
    assert _blocked_by(conn, "n") == "endpoint"
    queue.mark_dropped(conn, "n", "gone", NOW)
    assert queue.try_admit(conn, "bg", cap, NOW)


def _cand(curve):
    """One row, two slots, one of ours running, cells strong on every axis."""
    row = Row(id=1, endpoint_name="ep", served_id="m", endpoint_id=1, model_id=1,
              set_version="2026.11", band_times={}, speed_factor=None, archived=None, facts=None)
    cells = [Cell(dimension=d, language="", estimate=4.0, count=6.0, probes_n=6, jobs_n=0,
                  thin=False, pass_id="p", defs_version=1)
             for d in ("software_dev", "tool_use", "computer_use", "complexity", "judgement",
                       "completeness", "adherence")]
    cap = Capacity("partitioned", 2, window=131_072, requests_running=1, curve=curve)
    return Candidate(row=row, cells=cells, capacity=cap, ours_running=1, queued_ahead=0,
                     window=131_072, expected_wait_s=None, excluded_by=None)


def _decide(cand, priority):
    scores = BriefScores(values={}, confidences={}, undetermined=set(), truncated=False, raw={},
                         defs_version=1)
    return select(scores, [cand], priority, 0.5, 600.0, 16_000, {1}, None)


@pytest.mark.parametrize("priority", ["normal", "urgent"])
def test_prior_curve_never_holds_through_select(priority):
    """RULE: the decision shares admission's predicate: on the prior curve the row admits a
    normal or urgent job now (no queue_for), and the prior still predicts its speed."""
    cand = _cand(prior_curve("llamacpp"))
    d = _decide(cand, priority)
    assert d.chosen is cand and d.queue_for is None
    assert d.considered[0].admits is True
    assert d.considered[0].predicted_speed == pytest.approx(0.35)  # prediction unchanged


def test_measured_bottleneck_holds_through_select():
    """RULE: on a measured bottleneck the decision queues normal and urgent work for the
    bottleneck slot (the reason says so) and admits background."""
    for priority in ("normal", "urgent"):
        cand = _cand(MEASURED)
        d = _decide(cand, priority)
        assert d.queue_for is cand and d.wait_cause == "endpoint"
        assert "queued (bottleneck slot)" in d.chosen_reason
        assert d.considered[0].admits is False
    cand = _cand(MEASURED)
    d = _decide(cand, "background")
    assert d.chosen is cand and d.queue_for is None


# --- provenance: refine, the gap/clamp rule, the store ------------------------------------------

def test_refined_with_only_concurrency_1_samples_does_not_hold_at_2():
    """RULE: a box that only ever ran one session at a time refines nothing; its point 2
    is still the prior's and holds nothing, at 2 or above. (curve-refine moved this pin: the
    curve comes back unchanged, still the prior, rather than ``refined`` with 1 measured.)"""
    refined = prior_curve("llamacpp").refine([(1, 40.0), (1, 41.0), (1, 39.0)] * 3)
    assert refined.state == "prior" and refined.measured == frozenset()
    assert not any(refined.bottleneck_at(n) for n in range(2, 6))
    cap = Capacity("partitioned", 2, requests_running=1, curve=refined)
    assert queue.endpoint_room(cap, 1, "normal")


def test_refined_with_concurrency_2_samples_that_bottleneck_holds():
    """RULE: speed samples at concurrency 2 make point 2 measured; when they bottleneck
    (median 12 over median 40 = 0.3, 2 x 0.3 < 0.75) the second slot is background's only.
    Samples at 2 that do not bottleneck (0.45: 0.9) hold nothing. (curve-refine moved this
    pin: eight samples per concurrency, MIN_SAMPLES_PER_POINT.)"""
    refined = prior_curve("llamacpp").refine([(1, 40.0), (2, 12.0)] * 8)
    assert refined.measured == {1, 2}
    assert refined.points[2] == pytest.approx(0.3)
    assert refined.bottleneck_at(2) and refined.bottleneck_at(3)
    cap = Capacity("partitioned", 2, requests_running=1, curve=refined)
    assert not queue.endpoint_room(cap, 1, "normal")
    assert queue.endpoint_room(cap, 1, "background")
    fine = prior_curve("llamacpp").refine([(1, 40.0), (2, 18.0)] * 8)
    assert fine.measured == {1, 2} and not fine.bottleneck_at(2)
    # point 3 kept the prior's 0.25 (3 x 0.25 = 0.75 is not below the line anyway); it stays
    # unmeasured, and a later refine keeps what was measured
    again = refined.refine([(1, 40.0)] * 8)
    assert again.measured == {1, 2} and again.bottleneck_at(2)


def test_only_a_measured_key_holds_and_a_borrowed_reading_holds_nothing():
    """The rule for a concurrency ``_at`` answers from another key (step 2's gap/clamp
    question): k holds only when k is itself a measured key, read at its own concurrency. A
    prior point beside measured ones holds nothing; a gap or a reading beyond the curve borrows
    another key's value and holds nothing on its own (k x v grows with k, so it could never
    cross where its source did not), while a measured hold carries on "from n on"."""
    # prior 2 between measured 1 and 3: 2 holds nothing, 3 (3 x 0.2 = 0.6) holds from 3 on
    mixed = Curve({1: 1.0, 2: 0.35, 3: 0.2}, "refined", 5, frozenset({1, 3}))
    assert not mixed.bottleneck_at(2) and mixed.bottleneck_at(3) and mixed.bottleneck_at(6)
    # a gap at 2 reads 1's value (2 x 1.0, no hold); measured 3 holds; beyond 3 carries on
    gap = Curve({1: 1.0, 3: 0.2}, "refined", 4, frozenset({1, 3}))
    assert gap.predict(2) == 1.0 and not gap.bottleneck_at(2)
    assert gap.bottleneck_at(3) and gap.bottleneck_at(5)
    # beyond an UNMEASURED last key: the clamp borrows the prior's 0.1 and holds nothing
    tail = Curve({1: 1.0, 2: 0.1}, "refined", 3, frozenset({1}))
    assert tail.predict(8) == 0.1
    assert not any(tail.bottleneck_at(n) for n in range(2, 9))
    # a prior curve holds nothing whatever it is given; a key not in points is dropped
    assert Curve({1: 1.0, 2: 0.1}, "prior", 0, frozenset({1, 2})).measured == frozenset()
    assert Curve({1: 1.0, 2: 0.1}, "refined", 3, frozenset({1, 2, 7})).measured == {1, 2}


def _endpoint(conn):
    conn.execute("INSERT INTO endpoints (name, base_url, backend, host)"
                 " VALUES ('box', 'http://box:8080', 'llama.cpp', 'box')")
    return conn.execute("SELECT id FROM endpoints").fetchone()[0]


def test_save_and_load_round_trips_provenance(conn):
    """The measured keys ride in the endpoint_capacity.curve JSON beside the points (no
    schema change) and come back as they were written."""
    eid = _endpoint(conn)
    curve = Curve({1: 1.0, 2: 0.35, 3: 0.25}, "refined", 6, frozenset({1, 3}))
    save_capacity(conn, eid, "partitioned", curve)
    blob = json.loads(conn.execute("SELECT curve FROM endpoint_capacity").fetchone()[0])
    assert blob == {"points": {"1": 1.0, "2": 0.35, "3": 0.25}, "measured": [1, 3]}
    got = load_capacity(conn, eid)
    assert got == curve and got.measured == {1, 3}
    save_capacity(conn, eid, "partitioned", MEASURED)
    assert load_capacity(conn, eid).measured == {1, 2} and load_capacity(conn, eid).bottleneck_at(2)
    save_capacity(conn, eid, "partitioned", prior_curve("llamacpp"))
    got = load_capacity(conn, eid)
    assert got.state == "prior" and got.measured == frozenset() and not got.bottleneck_at(2)


@pytest.mark.parametrize("state, measured, holds", [
    ("probed", {1, 2}, True),       # the probe writes only what it measured
    ("refined", frozenset(), False),  # unprovable: holds nothing
])
def test_an_old_shape_stored_row_loads_by_its_state(conn, state, measured, holds):
    """A row written before the field existed (``{"points": ...}`` only): ``probed`` reads as
    every point measured (the real store's four rows, written by the probe with points 1 and
    2), ``refined`` as none (a point nobody can show was measured holds nothing)."""
    eid = _endpoint(conn)
    conn.execute("INSERT INTO endpoint_capacity (endpoint_id, kind, curve, state, points, updated)"
                 " VALUES (?, 'partitioned', ?, ?, 2, 1.0)",
                 (eid, json.dumps({"points": {"1": 1.0, "2": 0.35}}), state))
    got = load_capacity(conn, eid)
    assert got.points == {1: 1.0, 2: 0.35} and got.state == state
    assert got.measured == measured
    assert got.bottleneck_at(2) is holds
