"""Router 10 (router-design spec §6 selection, §5 dimensions, §7 capacity; router plan Task 10 and
its 2026-11-04 amendments: avoid_job, pools, unproven axes, the chosen row's one-line reason).

Pure data: every ``Row``, ``Cell``, ``Capacity`` and ``BriefScores`` is built by hand, no
database. Shortfalls are hand-computed in the comments beside each assertion, from §6:
``(sum over determined matched axes of gap * demand / 4) / determined + penalty(completeness)
+ penalty(adherence)``, ``penalty(c) = (4 - c) / 8`` with an unknown cell read as 0."""

import pytest

from localagents.router.capacity import Capacity, Curve
from localagents.router.decision import BriefScores
from localagents.router.scorecard import Cell, Row
from localagents.router.select import (BAND_TIMES_S, MATCH, Candidate, Session, expected_wait,
                                       gap, penalty, select, shortfall, weight)

LLAMA = Curve({1: 1.0, 2: 0.35, 3: 0.25})   # the llama.cpp prior shape: predicts, holds nothing
# the same shape measured (a probed curve): bottlenecks at 2 (§6: only a measured point holds)
LLAMA_MEASURED = Curve({1: 1.0, 2: 0.35, 3: 0.25}, "probed", 2)
FLAT = Curve({1: 1.0, 2: 1.0, 3: 1.0})      # never bottlenecks, never slows
NOW = 10_000.0


def _row(rid, endpoint="ep", served="m", band_times=None):
    return Row(id=rid, endpoint_name=endpoint, served_id=served, endpoint_id=rid, model_id=rid,
               set_version="2026.11", band_times=band_times or {}, speed_factor=None,
               archived=None, facts=None)


def _cell(dimension, estimate, count=6.0, language=""):
    return Cell(dimension=dimension, language=language, estimate=estimate, count=count,
                probes_n=int(count), jobs_n=0, thin=False, pass_id="p", defs_version=1)


def _cells(sd=4, tu=4, cu=4, cx=4, judgement=4, completeness=4, adherence=4, extra=()):
    """A row's cells; ``None`` leaves the cell out entirely."""
    spec = {"software_dev": sd, "tool_use": tu, "computer_use": cu, "complexity": cx,
            "judgement": judgement, "completeness": completeness, "adherence": adherence}
    out = [_cell(d, v) for d, v in spec.items() if v is not None]
    return out + list(extra)


def _cap(sessions=1, running=0, curve=FLAT, kind="partitioned"):
    return Capacity(kind, sessions, window=131_072, requests_running=running, curve=curve)


def _cand(row, cells, capacity=None, ours=0, queued=0, window=131_072, wait=None,
          excluded_by=None, **kw):
    return Candidate(row=row, cells=cells, capacity=capacity or _cap(), ours_running=ours,
                     queued_ahead=queued, window=window, expected_wait_s=wait,
                     excluded_by=excluded_by, **kw)


def _scores(values=None, undetermined=()):
    return BriefScores(values=dict(values or {}), confidences={}, undetermined=set(undetermined),
                       truncated=False, raw={}, defs_version=1)


def _select(scores, cands, priority="normal", tolerance=0.5, divert_after_s=600.0,
            load_tokens=16_000, calibrated=None, brief_files=None, **kw):
    if calibrated is None:
        calibrated = {c.row.id for c in cands}
    return select(scores, cands, priority, tolerance, divert_after_s, load_tokens, calibrated,
                  brief_files, **kw)


def _by_row(decision):
    return {c.row_id: c for c in decision.considered}


# --- the shortfall arithmetic (§6 step 2) ---------------------------------------------------------

def test_shortfall_gap_weight_and_normalisation():
    assert MATCH == {"software_dev": "software_dev", "tool_use": "tool_use",
                     "computer_use": "computer_use", "complexity": "complexity",
                     "specification": "judgement"}
    assert weight(4) == 1.0 and weight(2) == 0.5 and weight(0) == 0.0
    assert gap(3, _cell("tool_use", 1.0)) == 2.0
    assert gap(2, _cell("tool_use", 3.0)) == 0.0          # over-delivery gains nothing
    scores = _scores({"software_dev": 3, "tool_use": 2, "complexity": 4, "specification": 1})
    cand = _cand(_row(1), _cells(sd=2, tu=3, cx=2, judgement=1))
    # sd 1 * 0.75 + tu 0 + cx 2 * 1.0 + spec 0 = 2.75, over 4 determined axes = 0.6875
    assert shortfall(scores, cand) == pytest.approx(0.6875)
    # the language facet: a python cell with evidence replaces the aggregate (sd gap 0)
    py = _cell("software_dev", 3.0, language="python")
    cand_py = _cand(_row(2), _cells(sd=2, tu=3, cx=2, judgement=1, extra=[py]))
    scores_py = _scores({"software_dev": 3, "language": "python", "tool_use": 2,
                         "complexity": 4, "specification": 1})
    assert shortfall(scores_py, cand_py) == pytest.approx(2.0 / 4)
    # the same brief with no language read uses the aggregate again
    assert shortfall(scores, cand_py) == pytest.approx(0.6875)
    # normalisation: one band short on one axis reads 1.0 whatever else the brief determines
    one_axis = _scores({"complexity": 4})
    many_axes = _scores({"complexity": 4, "software_dev": 0, "tool_use": 0, "specification": 0})
    short = _cand(_row(3), _cells(cx=3))
    # demand 4 against a cell of 3: gap 1 * weight 1.0 = 1.0, over the one determined axis
    assert shortfall(one_axis, short) == pytest.approx(1.0)
    # three more determined axes at demand 0 cost nothing but count in the divisor: 1.0 / 4
    assert shortfall(many_axes, short) == pytest.approx(0.25)


def test_unknown_cell_costs_the_full_demand():
    assert gap(3, None) == 3
    assert gap(3, _cell("tool_use", 4.0, count=0.0)) == 3        # an estimate with no count
    assert gap(3, _cell("tool_use", None, count=2.0)) == 3       # below MIN_COUNT: no estimate
    scores = _scores({"tool_use": 3})
    missing = _cand(_row(1), _cells(tu=None))
    no_estimate = _cand(_row(2), _cells(tu=None, extra=[_cell("tool_use", None, count=2.0)]))
    evidence = _cand(_row(3), _cells(tu=1))
    # unknown: gap 3 * weight 0.75 = 2.25; evidence at 1: gap 2 * 0.75 = 1.5
    assert shortfall(scores, missing) == pytest.approx(2.25)
    assert shortfall(scores, no_estimate) == pytest.approx(2.25)
    assert shortfall(scores, evidence) == pytest.approx(1.5)
    # unknown counts as zero, so it never beats a row that showed something
    d = _select(scores, [missing, evidence], tolerance=0.1)
    assert d.chosen is evidence


def test_select_thin_language_cell_falls_back_to_the_aggregate():
    # a python cell with evidence seen but no estimate yet (count > 0, below MIN_COUNT):
    # selection reads the aggregate's 3 against the demand-4 brief, not the unknown-cell
    # 4.0 the thin cell used to cost
    thin = _cell("software_dev", None, count=1.0, language="python")
    cand = _cand(_row(1), _cells(sd=3, extra=[thin]))
    scores = _scores({"software_dev": 4, "language": "python"})
    # aggregate estimate 3 against demand 4: gap 1 * weight 1.0, the rate cells are clean
    assert shortfall(scores, cand) == pytest.approx(1.0)


def test_penalties_from_completeness_and_adherence():
    assert penalty(None) == 0.5
    assert penalty(_cell("completeness", 4.0)) == 0.0
    assert penalty(_cell("completeness", 2.0)) == 0.25
    assert penalty(_cell("completeness", None, count=2.0)) == 0.5
    assert penalty(_cell("completeness", 3.0, count=0.0)) == 0.5
    # a brief with nothing determined puts the row on its penalties alone: 0.25 + 0.125
    cand = _cand(_row(1), _cells(completeness=2, adherence=3))
    assert shortfall(_scores(), cand) == pytest.approx(0.375)
    # the penalties are row-level, independent of the brief
    assert shortfall(_scores({"tool_use": 4}), cand) == pytest.approx(0.0 + 0.375)
    # no rate cells at all: 0.5 + 0.5
    assert shortfall(_scores(), _cand(_row(2), _cells(completeness=None, adherence=None))) == 1.0


# --- the filters (§6 step 1, plan Task 10 order, amendment 1) --------------------------------------

def test_uncalibrated_filter_and_no_calibrated_row():
    a, b = _cand(_row(1), _cells()), _cand(_row(2), _cells())
    d = _select(_scores(), [a, b], calibrated={2})
    assert d.chosen is b and d.reason is None
    assert _by_row(d)[1].filtered == "uncalibrated" and _by_row(d)[1].shortfall is None
    none = _select(_scores(), [a, b], calibrated=set())
    assert none.chosen is None and none.queue_for is None
    assert none.reason == "no_calibrated_row"
    assert none.chosen_reason is None
    assert [c.filtered for c in none.considered] == ["uncalibrated", "uncalibrated"]


def test_window_and_ceiling_filters():
    small = _cand(_row(1), _cells(), window=32_768)
    shallow = _cand(_row(2), _cells(extra=[_cell("ceiling", 10.0)]))
    unknown_window = _cand(_row(3), _cells(), window=None)
    ok = _cand(_row(4), _cells(extra=[_cell("ceiling", 20.0)]))
    d = _select(_scores(), [small, shallow, unknown_window, ok], load_tokens=64_000,
                brief_files=12)
    seen = _by_row(d)
    assert seen[1].filtered == "window"
    assert seen[2].filtered == "ceiling"
    assert seen[3].filtered is None      # an unreported window is not "below" the load
    assert seen[4].filtered is None
    # ceiling filters only when both are known: no file count, or no ceiling evidence
    d2 = _select(_scores(), [shallow], brief_files=None)
    assert d2.chosen is shallow
    no_evidence = _cand(_row(5), _cells(extra=[_cell("ceiling", None, count=0.0)]))
    assert _select(_scores(), [no_evidence], brief_files=50).chosen is no_evidence
    # facts_unavailable (a down probe reads as zero sessions) and excluded follow
    down = _cand(_row(6), _cells(), capacity=Capacity("unknown", 0, curve=FLAT))
    barred = _cand(_row(7), _cells(), excluded_by="complexity >= 3")
    d3 = _select(_scores(), [down, barred, small], load_tokens=64_000)
    assert [c.filtered for c in d3.considered] == ["facts_unavailable", "excluded", "window"]
    assert d3.chosen is None and d3.reason == "no_acceptable_row"


# --- the priority policy (§6 step 5) --------------------------------------------------------------

def _best_and_lesser(best_ours=1, lesser_ours=0, best_wait=None, **lesser_kw):
    """Best row A and a lesser row B, one session each. Under SCORES_TU4 (tool_use 4 the only
    determined axis) A's shortfall is 0 and B's (tool_use 3) is gap 1 * weight 1.0 / 1 axis =
    1.0: acceptable at tolerance 1.0, not at 0.5."""
    a = _cand(_row(1, "big", "best"), _cells(), capacity=_cap(1), ours=best_ours, wait=best_wait)
    b = _cand(_row(2, "small", "lesser"), _cells(tu=3), capacity=_cap(1), ours=lesser_ours,
              **lesser_kw)
    return a, b


SCORES_TU4 = _scores({"tool_use": 4})


def test_urgent_diverts():
    a, b = _best_and_lesser()
    d = _select(SCORES_TU4, [a, b], priority="urgent", tolerance=1.0)
    assert d.chosen is b and d.divert is True and d.queue_for is None
    assert d.wait_cause == "endpoint"
    # B outside the tolerance is not acceptable: urgent queues at the head of A's queue
    d2 = _select(SCORES_TU4, [a, b], priority="urgent", tolerance=0.5)
    assert d2.chosen is a and d2.queue_for is a and d2.divert is False
    # nothing acceptable can admit: queue for best
    a2, b2 = _best_and_lesser(lesser_ours=1)
    d3 = _select(SCORES_TU4, [a2, b2], priority="urgent", tolerance=1.0)
    assert d3.chosen is a2 and d3.queue_for is a2 and not d3.divert
    # the best can admit: no policy question at all
    a3, b3 = _best_and_lesser(best_ours=0)
    d4 = _select(SCORES_TU4, [a3, b3], priority="urgent", tolerance=1.0)
    assert d4.chosen is a3 and d4.queue_for is None and d4.wait_cause is None


def test_normal_waits_then_diverts_by_threshold():
    a, b = _best_and_lesser(best_wait=300.0)
    d = _select(SCORES_TU4, [a, b], tolerance=1.0, divert_after_s=600.0)
    assert d.chosen is a and d.queue_for is a and d.divert is False
    assert d.wait_cause == "endpoint"
    a.expected_wait_s = 600.0      # a wake re-applies the policy: the wait crossed the bar
    d2 = _select(SCORES_TU4, [a, b], tolerance=1.0, divert_after_s=600.0)
    assert d2.chosen is b and d2.divert is True and d2.queue_for is None
    # an unknown wait is no evidence the threshold was crossed: queue for best
    a.expected_wait_s = None
    d3 = _select(SCORES_TU4, [a, b], tolerance=1.0, divert_after_s=600.0)
    assert d3.chosen is a and d3.queue_for is a


def test_background_queues_unless_no_higher_priority_waits_on_the_lesser_row():
    a, b = _best_and_lesser(best_wait=99_999.0)
    d = _select(SCORES_TU4, [a, b], priority="background", tolerance=1.0)
    assert d.chosen is b and d.divert is True
    # a normal job queued for the lesser row: background leaves it alone and queues for best
    a2, b2 = _best_and_lesser(best_wait=99_999.0, queued_higher=1)
    d2 = _select(SCORES_TU4, [a2, b2], priority="background", tolerance=1.0)
    assert d2.chosen is a2 and d2.queue_for is a2 and not d2.divert
    # not told how many of the queued are higher: every queued job ahead counts (fail-closed)
    a3, b3 = _best_and_lesser(best_wait=99_999.0)
    b3.queued_ahead = 1
    d3 = _select(SCORES_TU4, [a3, b3], priority="background", tolerance=1.0)
    assert d3.chosen is a3 and d3.queue_for is a3


def test_bottleneck_slot_only_for_background():
    # a two-slot llama.cpp box with one of ours running and its curve measured: the second
    # session is the bottleneck
    cand = _cand(_row(1), _cells(), capacity=_cap(2, running=1, curve=LLAMA_MEASURED), ours=1)
    assert cand.capacity.admits(1) and cand.capacity.bottleneck_slot(1)
    normal = _select(_scores(), [cand])
    assert normal.chosen is cand and normal.queue_for is cand and normal.wait_cause == "endpoint"
    assert _by_row(normal)[1].admits is False
    background = _select(_scores(), [cand], priority="background")
    assert background.chosen is cand and background.queue_for is None
    assert _by_row(background)[1].admits is True
    # a vLLM-shaped curve does not bottleneck at 2: normal admits
    vllm = _cand(_row(2), _cells(), capacity=_cap(2, running=1, curve=Curve({1: 1.0, 2: 0.85})),
                 ours=1)
    assert _select(_scores(), [vllm]).queue_for is None


def test_all_undetermined_falls_to_tie_break():
    # both rows on their penalties alone (0): the tie goes to the faster prediction. A busy
    # llama.cpp box (one of ours on three slots) gives the next session 0.35 of an idle one.
    busy = _cand(_row(1, "busy"), _cells(), capacity=_cap(3, running=1, curve=LLAMA), ours=1)
    idle = _cand(_row(2, "idle"), _cells(), capacity=_cap(3, curve=LLAMA))
    d = _select(_scores(undetermined={"software_dev", "tool_use", "complexity"}), [busy, idle],
                priority="background")
    assert d.tie_break is True and d.chosen is idle
    assert _by_row(d)[1].predicted_speed == pytest.approx(0.35)
    assert _by_row(d)[2].predicted_speed == pytest.approx(1.0)
    assert set(d.undetermined) == {"software_dev", "tool_use", "complexity"}
    # equal speed: fewer queued ahead wins
    q1 = _cand(_row(3, "q1"), _cells(), capacity=_cap(3), queued=2)
    q0 = _cand(_row(4, "q0"), _cells(), capacity=_cap(3), queued=0)
    d2 = _select(_scores(), [q1, q0])
    assert d2.tie_break is True and d2.chosen is q0
    # a strict winner is no tie
    worse = _cand(_row(5), _cells(completeness=3))
    better = _cand(_row(6), _cells())
    assert _select(_scores(), [worse, better]).tie_break is False


def test_idle_rows_tie_break_on_the_measured_speed_factor_not_row_order():
    """The first live routed job (2026-11-04): four idle calibrated rows tied at the same
    shortfall, every curve read 1.0, and row order chose. The row's speed factor (median
    actual/nominal; bigger is slower) divides the curve's prediction, so the slower row listed
    FIRST loses to the faster one, the ticket shows the divided speeds, and the reason names
    predicted speed as the level that decided. A row without a factor keeps the curve."""
    slow = _row(1, "dgx", "deepseek"); slow.speed_factor = 1.84
    fast = _row(2, "llamacpp", "qwen-27b"); fast.speed_factor = 0.94
    bare = _row(3, "strata", "flash")   # speed_factor None
    d = _select(_scores(), [_cand(slow, _cells()), _cand(fast, _cells()), _cand(bare, _cells())])
    assert d.tie_break is True and d.chosen.row is fast
    assert _by_row(d)[1].predicted_speed == pytest.approx(1 / 1.84)
    assert _by_row(d)[2].predicted_speed == pytest.approx(1 / 0.94)
    assert _by_row(d)[3].predicted_speed == pytest.approx(1.0)
    assert d.chosen_reason.endswith("; tie broken by predicted speed")
    # a busy fast row (second slot at 0.68) still beats an idle slow one: 0.68/0.94 > 1/1.84
    busy_fast = _cand(fast, _cells(), capacity=_cap(2, running=1, curve=Curve({1: 1.0, 2: 0.68})), ours=1)
    d2 = _select(_scores(), [_cand(slow, _cells()), busy_fast], priority="background")
    assert d2.chosen.row is fast
    assert _by_row(d2)[2].predicted_speed == pytest.approx(0.68 / 0.94)
    # a zero or negative factor is not a divisor: the curve's reading stands
    odd = _row(4, "odd", "m"); odd.speed_factor = 0.0
    assert _by_row(_select(_scores(), [_cand(odd, _cells())]))[4].predicted_speed == pytest.approx(1.0)


# --- expected wait (§6, "Expected wait") ----------------------------------------------------------

def test_expected_wait_per_slot_and_overrun_floor():
    assert BAND_TIMES_S == {0: 120, 1: 600, 2: 1800, 3: 3600, 4: 5400}
    row = _row(1, band_times={2: 1000.0})
    sessions = [Session(started_at=NOW - 400, band=2),     # 1000 - 400 = 600 left
                Session(started_at=NOW - 1200, band=2)]    # overran: floored at 1000 / 4 = 250
    cand = _cand(row, _cells(), capacity=_cap(2, running=2), ours=2, running=sessions)
    assert expected_wait(cand, _scores(), NOW) == pytest.approx(250.0)
    # one band-2 job queued ahead takes the 250 slot (free again at 1250): ours waits for 600
    ahead = _cand(row, _cells(), capacity=_cap(2, running=2), ours=2, queued=1,
                  running=sessions, ahead_bands=[2])
    assert expected_wait(ahead, _scores(), NOW) == pytest.approx(600.0)
    # a free slot: no wait
    free = _cand(row, _cells(), capacity=_cap(2, running=1), ours=1, running=sessions[:1])
    assert expected_wait(free, _scores(), NOW) == 0.0
    # no history at the band: the band's default (band 3: 3600 - 600 elapsed)
    default = _cand(row, _cells(), capacity=_cap(1, running=1), ours=1,
                    running=[Session(started_at=NOW - 600, band=3)])
    assert expected_wait(default, _scores(), NOW) == pytest.approx(3000.0)
    # a session with no band of its own reads at the brief's complexity band
    unbanded = _cand(row, _cells(), capacity=_cap(1, running=1), ours=1,
                     running=[Session(started_at=NOW - 100, band=None)])
    assert expected_wait(unbanded, _scores({"complexity": 2}), NOW) == pytest.approx(900.0)


# --- the ticket's row list ------------------------------------------------------------------------

def test_considered_lists_every_row():
    cands = [
        _cand(_row(1, "a", "m1"), _cells()),
        _cand(_row(2, "b", "m2"), _cells(), window=8_000),
        _cand(_row(3, "c", "m3"), _cells(), excluded_by="computer_use >= 2"),
        _cand(_row(4, "d", "m4"), _cells(completeness=2), queued=3),
        _cand(_row(5, "e", "m5"), _cells()),
    ]
    d = _select(_scores(), cands, calibrated={1, 2, 3, 4}, load_tokens=16_000)
    assert [c.row_id for c in d.considered] == [1, 2, 3, 4, 5]
    assert [c.endpoint for c in d.considered] == ["a", "b", "c", "d", "e"]
    assert [c.served_id for c in d.considered] == ["m1", "m2", "m3", "m4", "m5"]
    assert [c.filtered for c in d.considered] == [None, "window", "excluded", None,
                                                  "uncalibrated"]
    assert [c.shortfall for c in d.considered] == [0.0, None, None, 0.25, None]
    assert d.considered[3].queued == 3
    assert d.considered[0].acceptable and d.considered[3].acceptable
    assert d.chosen is cands[0]


# --- the 2026-11-04 amendments --------------------------------------------------------------------

def test_avoid_served_id_filters_first_and_is_listed():
    # the builder's model on two endpoints (the rule is about the model, not the box), one of
    # them also uncalibrated: avoid_job is the filter named, because it runs first
    builder_a = _cand(_row(1, "box1", "builder"), _cells())
    builder_b = _cand(_row(2, "box2", "builder"), _cells(), window=1_000)
    other = _cand(_row(3, "box3", "reviewer"), _cells(tu=3))
    d = _select(SCORES_TU4, [builder_a, builder_b, other], calibrated={1, 3},
                avoid_served_id="builder", tolerance=0.1)
    assert [c.filtered for c in d.considered] == ["avoid_job", "avoid_job", None]
    assert d.chosen is other
    assert "avoid_job" in d.chosen_reason and "builder" in d.chosen_reason
    # without the filter the builder's row wins outright
    assert _select(SCORES_TU4, [builder_a, other], tolerance=0.1).chosen is builder_a
    # avoiding the only calibrated row is no acceptable row, not "no calibrated row"
    only = _select(SCORES_TU4, [builder_a], avoid_served_id="builder")
    assert only.chosen is None and only.reason == "no_acceptable_row"


def test_reason_names_avoid_job_only_when_it_bound_the_choice():
    """RULE (brief amendment 2, §7 "Decided"): the reason sentence names a filter only when it
    changed the outcome: an avoided row is named only if its shortfall (scored as if not
    filtered) would have ranked at or above the chosen row's, i.e. it would have been chosen or
    tied; otherwise the ticket's ``considered`` table still lists it as filtered but the
    one-line reason stays silent about it."""
    # the avoided row is weak: the other row wins on shortfall whether or not it was filtered
    weak = _cand(_row(1, "box1", "builder"),
                 _cells(sd=1, tu=1, cu=1, cx=1, completeness=1, adherence=1))
    strong = _cand(_row(2, "box2", "reviewer"), _cells())
    d = _select(_scores({"software_dev": 4}), [weak, strong], avoid_served_id="builder",
                tolerance=0.1)
    assert d.chosen is strong
    assert [c.filtered for c in d.considered] == ["avoid_job", None]
    assert "avoid_job" not in d.chosen_reason
    # the avoided row is strong: unfiltered it would have won, so the reason names the filter
    builder = _cand(_row(3, "box3", "builder"), _cells())
    lesser = _cand(_row(4, "box4", "reviewer"), _cells(tu=3))
    d2 = _select(SCORES_TU4, [builder, lesser], avoid_served_id="builder", tolerance=0.1)
    assert d2.chosen is lesser
    assert [c.filtered for c in d2.considered] == ["avoid_job", None]
    assert "avoid_job excluded box3 x builder" in d2.chosen_reason


def test_pool_blocked_row_queues_with_wait_cause_pool():
    # the best row has a free session of its own, but its pool is full
    a = _cand(_row(1, "dgx1-a", "best"), _cells(), pool="dgx1", pool_admits=False, wait=100.0)
    d = _select(_scores(), [a])
    assert d.chosen is a and d.queue_for is a and d.wait_cause == "pool"
    assert "pool dgx1" in d.chosen_reason
    assert _by_row(d)[1].admits is False
    # urgent diverts to an acceptable free row, and the divert names the pool
    b = _cand(_row(2, "ai1", "lesser"), _cells(tu=3))
    d2 = _select(SCORES_TU4, [a, b], priority="urgent", tolerance=1.0)
    assert d2.chosen is b and d2.divert and d2.wait_cause == "pool"
    assert d2.chosen_reason.startswith("divert: ai1 x lesser free")
    assert "dgx1-a x best busy (pool dgx1)" in d2.chosen_reason
    # the endpoint's own capacity is the cause when the pool admits
    full = _cand(_row(3, "dgx1-a", "best"), _cells(), ours=1, pool="dgx1")
    assert _select(_scores(), [full]).wait_cause == "endpoint"


def test_unproven_axis_is_always_undetermined():
    scores = _scores({"software_dev": 4, "language": "python", "complexity": 2})
    py = _cell("software_dev", 4.0, language="python")
    cand = _cand(_row(1), _cells(sd=0, cx=1, extra=[py]))
    # all three read: python cell 4 -> sd gap 0; cx gap 1 * 0.5 = 0.5; over 2 axes = 0.25
    assert shortfall(scores, cand) == pytest.approx(0.25)
    # language unproven: the aggregate sd cell (0) is used: (4 * 1.0 + 0.5) / 2 = 2.25
    assert shortfall(scores, cand, frozenset({"language"})) == pytest.approx(2.25)
    # software_dev unproven: only complexity counts: 0.5 / 1
    assert shortfall(scores, cand, frozenset({"software_dev"})) == pytest.approx(0.5)
    d = _select(scores, [cand], unproven=frozenset({"software_dev"}))
    assert "software_dev" in d.undetermined
    assert _by_row(d)[1].shortfall == pytest.approx(0.5)
    # an unproven complexity is not the brief's band in the wait either: an unbanded session
    # reads at the brief's band 4 (no history: 5400 - 100) when proven, at the middle band 2
    # (the row's 1000 - 100) when not
    row = _row(2, band_times={2: 1000.0})
    waiting = _cand(row, _cells(), capacity=_cap(1, running=1), ours=1,
                    running=[Session(started_at=NOW - 100, band=None)])
    proven = expected_wait(waiting, _scores({"complexity": 4}), NOW)
    unproven = expected_wait(waiting, _scores({"complexity": 4}), NOW,
                             frozenset({"complexity"}))
    assert proven == pytest.approx(BAND_TIMES_S[4] - 100)
    assert unproven == pytest.approx(900.0)


def test_chosen_reason_is_one_line():
    a = _cand(_row(1, "llamacpp", "qwen-27b"), _cells(completeness=3))
    b = _cand(_row(2, "vllm", "deepseek"), _cells(completeness=2))
    c = _cand(_row(3, "strata", "qwen-flash"), _cells(completeness=1))
    d = _select(_scores(), [a, b, c], tolerance=2.0)
    assert d.chosen is a
    assert d.chosen_reason == "shortfall 0.12 on llamacpp x qwen-27b; best of 3"
    assert "\n" not in d.chosen_reason
    # queued: the line says so and why
    busy = _cand(_row(4, "llamacpp", "qwen-27b"), _cells(), ours=1)
    q = _select(_scores(), [busy])
    assert q.chosen_reason == "shortfall 0.00 on llamacpp x qwen-27b; best of 1; queued (endpoint busy)"
    # a tie says the tie-break decided
    t = _select(_scores(), [_cand(_row(5, "x", "m"), _cells()), _cand(_row(6, "y", "n"), _cells())])
    assert t.chosen_reason.endswith("; tie broken by predicted speed, then queue, then row order")
    # the level that decided is the one named: equal speed, a queue difference
    t2 = _select(_scores(), [_cand(_row(7, "x", "m"), _cells(), queued=1),
                             _cand(_row(8, "y", "n"), _cells())])
    assert t2.chosen.row.id == 8
    assert t2.chosen_reason.endswith("; tie broken by predicted speed, then queue")
    # a refusal has a reason and no sentence
    r = _select(_scores(), [a], calibrated=set())
    assert r.reason == "no_calibrated_row" and r.chosen_reason is None
