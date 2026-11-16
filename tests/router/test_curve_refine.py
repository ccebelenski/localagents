"""curve-refine: §7's third stage, the throughput curve "refined by every job's observed speed
logged with the occupancy beside it" (router-design spec §7), under §6's bottleneck rule
(True-now 2026-11-15: "The rule holds a slot only on a MEASURED point of the curve: one the
throughput probe wrote, or one refined from speed samples at that concurrency").

The rule, one test (or more) per sentence:
- a speed sample's concurrency is the occupancy its run held for at least STEADY_SHARE of its
  wall time; a run whose occupancy changed for longer writes no sample;
- a concurrency is measured from MIN_SAMPLES_PER_POINT samples there AND as many at 1 (the
  baseline); fewer predict nothing new and hold nothing;
- the samples a fold reads: this endpoint's, the served model's, no calibration probe, inside
  SAMPLE_WINDOW_S, the newest SAMPLES_PER_POINT per concurrency;
- a point the throughput probe measured takes the higher of the probe's ratio and the samples';
- every fold re-derives from the base (the probe's points for this model, else the prior);
- a curve measured on one served model holds no slot for another;
- the fold runs at a sampled job's finish and never fails it; a change is a ``[curve]`` line in
  that job's transcript, and the row names the job."""

import asyncio
import itertools
import json
import time
from pathlib import Path

import pytest

from localagents import db
from localagents.jobs import Job
from localagents.registry import Defaults, Endpoint, Registry, Resolution
from localagents.router import capacity, dispatch, feedback, queue
from localagents.router.capacity import (MIN_SAMPLES_PER_POINT, SAMPLE_WINDOW_S,
                                         SAMPLES_PER_POINT, Curve, fold_samples, load_capacity,
                                         prior_curve, recent_samples, save_capacity)

NOW = time.time()  # the fold reads its window against the wall clock: rows sit a day inside it
T0 = NOW - 86_400.0
_ids = itertools.count()


def _endpoint(conn, name="box"):
    return db.lookup_id(conn, "endpoints", name=name, base_url=f"http://{name}:8080",
                        backend="llama.cpp", host=name)


def _model(conn, served="m-a"):
    return db.lookup_id(conn, "models", served_id=served)


def _sample(conn, eid, mid, sessions, tps, *, created=None, calibration=0):
    """A finished job's row and its speed sample, written as feedback leaves them."""
    jid = f"s{next(_ids)}"
    created = T0 if created is None else created
    conn.execute("INSERT INTO jobs (id, endpoint_id, model_id, status, created_at, started_at,"
                 " finished_at, calibration) VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?)",
                 (jid, eid, mid, created, created, created + 60.0, calibration))
    conn.execute("INSERT INTO speed_samples (job_id, endpoint_id, sessions, generate_tps, at)"
                 " VALUES (?, ?, ?, ?, ?)", (jid, eid, sessions, tps, created + 60.0))


def _samples(conn, eid, mid, sessions, tps, n, **kw):
    for _ in range(n):
        _sample(conn, eid, mid, sessions, tps, **kw)


def _blob(conn, eid):
    return json.loads(conn.execute("SELECT curve FROM endpoint_capacity WHERE endpoint_id = ?",
                                   (eid,)).fetchone()[0])


# --- the rule (Curve.refine) -------------------------------------------------------------------

def test_too_few_samples_measure_nothing_and_hold_nothing():
    """RULE: a concurrency is measured from MIN_SAMPLES_PER_POINT samples there and as many at
    1. Seven samples at 2 far below the line, or seven at 1, leave the prior as it was: point 2
    still predicts the prior's 0.35 and holds nothing."""
    prior = prior_curve("llamacpp")
    m = MIN_SAMPLES_PER_POINT
    short_at_2 = prior.refine([(1, 40.0)] * m + [(2, 4.0)] * (m - 1))
    short_at_1 = prior.refine([(1, 40.0)] * (m - 1) + [(2, 4.0)] * m)
    for curve in (short_at_2, short_at_1):
        assert curve == prior and curve.measured == frozenset()
        assert curve.first_hold() is None
    # one more sample at 2 and the point is measured: 4 / 40 = 0.1, held from 2
    enough = prior.refine([(1, 40.0)] * m + [(2, 4.0)] * m)
    assert enough.points[2] == pytest.approx(0.1) and enough.measured == {1, 2}
    assert enough.first_hold() == 2 and enough.n == 2 * m


def test_a_counted_point_is_the_median_ratio_and_a_short_one_keeps_its_provenance():
    """RULE: a counted concurrency takes median(k) / median(1); a key with too few samples
    keeps its value and its provenance (the prior's 3 stays a prior point)."""
    samples = ([(1, 40.0)] * 7 + [(1, 400.0)]          # an outlier moves no median
               + [(2, 20.0)] * 8 + [(3, 1.0)] * 3)
    curve = prior_curve("llamacpp").refine(samples)
    assert curve.points == pytest.approx({1: 1.0, 2: 0.5, 3: 0.25})
    assert curve.measured == {1, 2} and curve.state == "refined"
    assert curve.first_hold() is None  # 2 x 0.5 = 1.0; 3 is the prior's (3 x 0.25 holds nothing)


def test_a_probed_point_takes_the_higher_of_probe_and_samples():
    """RULE: the probe is two requests decoding at once, the worst case; samples can lift a
    probed point off the line, never push it below the probe. A point the probe never measured
    (3) is the samples' alone."""
    m = MIN_SAMPLES_PER_POINT
    probed = Curve({1: 1.0, 2: 0.6}, "probed", 2, frozenset({1, 2}))
    lower = probed.refine([(1, 40.0)] * m + [(2, 8.0)] * m)          # samples 0.2
    assert lower.points[2] == pytest.approx(0.6) and lower.first_hold() is None
    assert lower.probe == {1: 1.0, 2: 0.6} and lower.state == "refined"
    holding = Curve({1: 1.0, 2: 0.3}, "probed", 2, frozenset({1, 2}))
    assert holding.first_hold() == 2
    released = holding.refine([(1, 40.0)] * m + [(2, 32.0)] * m)     # samples 0.8
    assert released.points[2] == pytest.approx(0.8) and released.first_hold() is None
    beyond = probed.refine([(1, 40.0)] * m + [(3, 8.0)] * m)         # 3 x 0.2 = 0.6 < 0.75
    assert beyond.points == pytest.approx({1: 1.0, 2: 0.6, 3: 0.2})
    assert beyond.measured == {1, 2, 3} and beyond.first_hold() == 3


# --- which samples count (recent_samples) ------------------------------------------------------

def test_the_fold_reads_this_models_recent_non_calibration_samples(conn):
    """RULE: the samples a fold reads are this endpoint's, from jobs that ran this served model,
    no calibration probe, created inside the window. Every excluded set here would move the
    answer if it were read: another model's fast samples at 2, calibration probes at 1, old
    samples at 2, another endpoint's."""
    eid, other_ep = _endpoint(conn), _endpoint(conn, "other")
    a, b = _model(conn, "m-a"), _model(conn, "m-b")
    m = MIN_SAMPLES_PER_POINT
    _samples(conn, eid, a, 1, 40.0, m)
    _samples(conn, eid, a, 2, 8.0, m)
    _samples(conn, eid, b, 2, 40.0, 3 * m)                         # another served model
    _samples(conn, eid, a, 1, 400.0, 3 * m, calibration=1)         # calibration probes
    _samples(conn, eid, a, 2, 40.0, 3 * m, created=NOW - SAMPLE_WINDOW_S - 3600.0)  # too old
    _samples(conn, other_ep, a, 2, 40.0, 3 * m)                    # another endpoint
    got = recent_samples(conn, eid, a, NOW)
    assert sorted(got) == sorted([(1, 40.0)] * m + [(2, 8.0)] * m)
    _, curve = fold_samples(conn, eid, "m-a", a, "llamacpp", NOW)
    assert curve.points[2] == pytest.approx(0.2) and curve.first_hold() == 2


def test_the_fold_reads_the_newest_samples_per_concurrency(conn):
    """RULE: the newest SAMPLES_PER_POINT per concurrency: thirty slow old samples at 1 and
    twenty fast newer ones read as the twenty."""
    eid, a = _endpoint(conn), _model(conn)
    _samples(conn, eid, a, 1, 10.0, 30, created=T0 - 3600.0)
    _samples(conn, eid, a, 1, 50.0, SAMPLES_PER_POINT, created=T0)
    got = recent_samples(conn, eid, a, NOW)
    assert got == [(1, 50.0)] * SAMPLES_PER_POINT


def test_a_busy_concurrency_never_hides_a_quiet_one(conn):
    """RULE: every concurrency with samples in the window is read, the newest SAMPLES_PER_POINT
    of EACH, however many samples another concurrency has. 520 recent samples at 1 must not hide
    8 samples at 2 three days old (a global row cap of 500 did): the read is 20 at 1 and 8 at 2,
    and the fold measures point 2 = 8 / 40 = 0.2, a hold from 2 (2 x 0.2 < 0.75)."""
    eid, a = _endpoint(conn), _model(conn)
    _samples(conn, eid, a, 1, 40.0, 520, created=NOW - 3600.0)
    _samples(conn, eid, a, 2, 8.0, MIN_SAMPLES_PER_POINT, created=NOW - 3 * 86_400.0)
    got = recent_samples(conn, eid, a, NOW)
    assert sorted(s for s, _ in got) == [1] * SAMPLES_PER_POINT + [2] * MIN_SAMPLES_PER_POINT
    assert got.count((2, 8.0)) == MIN_SAMPLES_PER_POINT
    _, curve = fold_samples(conn, eid, "m-a", a, "llamacpp", NOW)
    assert curve.points[2] == pytest.approx(0.2) and curve.first_hold() == 2


# --- the fold (fold_samples) -------------------------------------------------------------------

def test_an_endpoint_whose_samples_measure_nothing_gets_no_row(conn):
    """RULE: no row for an endpoint with none whose samples measure nothing (the prior stands,
    read by dispatch as the backend's)."""
    eid, a = _endpoint(conn), _model(conn)
    _samples(conn, eid, a, 1, 40.0, 20)
    before, after = fold_samples(conn, eid, "m-a", a, "llamacpp", NOW)
    assert before is None and after is None
    assert load_capacity(conn, eid) is None


def test_a_legacy_probed_row_is_adopted_and_every_fold_rederives_from_it(conn):
    """RULE: a probed row that records no model (the real store's four) is the base of the
    first fold, which stamps the model; every fold re-derives from the probe's points, so a
    sample-measured point whose samples age out of the window goes back to the base."""
    eid, a = _endpoint(conn), _model(conn)
    conn.execute("INSERT INTO endpoint_capacity (endpoint_id, kind, curve, state, points, updated)"
                 " VALUES (?, 'partitioned', ?, 'probed', 2, 1.0)",
                 (eid, json.dumps({"points": {"1": 1.0, "2": 0.6}})))
    m = MIN_SAMPLES_PER_POINT
    _samples(conn, eid, a, 1, 40.0, m)
    _samples(conn, eid, a, 3, 6.0, m)                                # 3 x 0.15 holds from 3
    before, after = fold_samples(conn, eid, "m-a", a, "llamacpp", NOW)
    assert before.served_id is None and before.state == "probed"
    assert after.points == pytest.approx({1: 1.0, 2: 0.6, 3: 0.15})
    assert after.served_id == "m-a" and after.probe == {1: 1.0, 2: 0.6}
    assert after.first_hold() == 3
    again_before, again = fold_samples(conn, eid, "m-a", a, "llamacpp", NOW)
    assert again is again_before and again == after                  # a second fold: no change
    later = NOW + SAMPLE_WINDOW_S + 2 * 86_400.0                     # the samples aged out
    _, aged = fold_samples(conn, eid, "m-a", a, "llamacpp", later)
    assert aged == Curve({1: 1.0, 2: 0.6}, "probed", 2, frozenset({1, 2}), "m-a")
    assert aged.first_hold() is None and _blob(conn, eid)["served_id"] == "m-a"


def test_another_models_curve_is_not_this_models_base(conn):
    """RULE: a curve measured on one served model holds no slot for another. A fold for model
    B over model A's probed-and-refined curve starts from the prior: A's probe points and A's
    sample points are gone, and B's own samples (none at 1 yet) measure nothing."""
    eid, a, b = _endpoint(conn), _model(conn, "m-a"), _model(conn, "m-b")
    save_capacity(conn, eid, "partitioned",
                  Curve({1: 1.0, 2: 0.3}, "probed", 2, frozenset({1, 2}), "m-a"))
    _samples(conn, eid, b, 2, 8.0, MIN_SAMPLES_PER_POINT)
    _, after = fold_samples(conn, eid, "m-b", b, "llamacpp", NOW)
    assert after == Curve({1: 1.0, 2: 0.35, 3: 0.25}, "prior", 0, None, "m-b")
    assert after.measured == frozenset() and after.first_hold() is None
    assert _blob(conn, eid) == {"points": {"1": 1.0, "2": 0.35, "3": 0.25}, "measured": [],
                                "served_id": "m-b"}


def test_load_for_another_served_model_predicts_and_holds_nothing(conn):
    """RULE: read for a served model, a curve that records another keeps its values (it still
    predicts) and has no measured point; read with no model, or a curve that records none, as
    it stands."""
    eid = _endpoint(conn)
    held = Curve({1: 1.0, 2: 0.3}, "refined", 16, frozenset({1, 2}), "m-a")
    save_capacity(conn, eid, "partitioned", held)
    assert load_capacity(conn, eid, "m-a").first_hold() == 2
    assert load_capacity(conn, eid).first_hold() == 2
    other = load_capacity(conn, eid, "m-b")
    assert other.points == held.points and other.predict(2) == pytest.approx(0.3)
    assert other.measured == frozenset() and other.first_hold() is None
    save_capacity(conn, eid, "partitioned", Curve({1: 1.0, 2: 0.3}, "probed", 2))  # no model
    assert load_capacity(conn, eid, "m-b").first_hold() == 2


def test_the_store_round_trip_keeps_provenance(conn):
    """RULE: points, measured keys, the served model and the probe's points survive save and
    load; the fold's ``by`` rides in the row's JSON and is not part of the curve; the probe's
    own write (save_capacity) carries no ``by``."""
    eid, a = _endpoint(conn), _model(conn)
    curve = Curve({1: 1.0, 2: 0.6, 3: 0.2}, "refined", 16, frozenset({1, 2, 3}), "m-a",
                  {1: 1.0, 2: 0.6})
    save_capacity(conn, eid, "partitioned", curve)
    assert _blob(conn, eid) == {"points": {"1": 1.0, "2": 0.6, "3": 0.2}, "measured": [1, 2, 3],
                                "served_id": "m-a", "probe": {"1": 1.0, "2": 0.6}}
    assert load_capacity(conn, eid) == curve
    _samples(conn, eid, a, 1, 40.0, MIN_SAMPLES_PER_POINT)
    _samples(conn, eid, a, 2, 30.0, MIN_SAMPLES_PER_POINT)
    _, folded = fold_samples(conn, eid, "m-a", a, "llamacpp", NOW, by="job-7")
    blob = _blob(conn, eid)
    assert blob["by"] == "job-7" and blob["probe"] == {"1": 1.0, "2": 0.6}
    assert load_capacity(conn, eid) == folded and folded.points[2] == pytest.approx(0.75)
    assert conn.execute("SELECT kind FROM endpoint_capacity").fetchone()[0] == "partitioned"
    save_capacity(conn, eid, "partitioned", folded)
    assert "by" not in _blob(conn, eid)


# --- the sample's concurrency (feedback.run_occupancy) -----------------------------------------

def _neighbour(conn, eid, jid, started, finished):
    conn.execute("INSERT INTO jobs (id, endpoint_id, status, created_at, started_at, finished_at)"
                 " VALUES (?, ?, ?, ?, ?, ?)",
                 (jid, eid, "running" if finished is None else "succeeded", started, started,
                  finished))


def test_a_sample_is_the_occupancy_the_run_held(conn):
    """RULE: the occupancy that held for STEADY_SHARE of the run, the job included; None when
    none did. The real store's one shared pair: a job that started alone and shared 92% of its
    133 s is a concurrency-2 sample, not the 1 its start counted."""
    eid = _endpoint(conn)
    occ = feedback.run_occupancy
    assert occ(conn, "me", eid, 100.0, 233.0) == 1                   # alone
    _neighbour(conn, eid, "pair", 110.0, 242.0)
    assert occ(conn, "me", eid, 100.0, 233.0) == 2                   # 123 of 133 s shared
    assert occ(conn, "me", eid, 100.0, 300.0) is None                # 132 of 200 s: neither
    _neighbour(conn, eid, "before", 0.0, 100.0)                      # ended as this one began
    _neighbour(conn, eid, "live", 1000.0, None)                      # still running
    assert occ(conn, "me", eid, 1000.0, 1100.0) == 2
    assert occ(conn, "me", eid, 50.0, 50.0) is None                  # no length, no reading
    other = _endpoint(conn, "other")
    assert occ(conn, "me", other, 100.0, 233.0) == 1                 # another endpoint's jobs


def test_a_run_that_held_one_count_for_the_exact_ninety_percent_boundary_is_a_sample(conn):
    """RULE: a run that held one count for exactly STEADY_SHARE (90%) of its wall time is a
    sample at that count; rounding error never decides it. On real epoch floats a neighbour
    starting at ``start + 0.1 * span`` leaves the run 90% shared, but the summed durations fall
    one float ulp short of ``0.9 * span`` (7626.903769731522 against 7626.903769744732), so an
    exact comparison labels the run "changed occupancy". A run genuinely 0.1% short of the
    boundary is still no sample: the tolerance is far below any real difference."""
    eid, other = _endpoint(conn), _endpoint(conn, "other")
    start, span = 1640309273.2337203, 8474.33752193859
    end = start + span
    _neighbour(conn, eid, "n", start + 0.1 * span, end)
    assert feedback.run_occupancy(conn, "me", eid, start, end) == 2
    _neighbour(conn, other, "short", start + 0.101 * span, end)      # 89.9%: 85 s short
    assert feedback.run_occupancy(conn, "me", other, start, end) is None


# --- the trigger (feedback.on_finish) ----------------------------------------------------------

def _job(conn, tmp_path, eid, mid, start, end, tps, served="m-a"):
    jid = f"j{next(_ids)}"
    conn.execute("INSERT INTO jobs (id, endpoint_id, model_id, task, cwd, isolation, status,"
                 " created_at, started_at, finished_at) VALUES (?, ?, ?, 't', ?, 'none',"
                 " 'succeeded', ?, ?, ?)", (jid, eid, mid, str(tmp_path), start, start, end))
    log_dir = tmp_path / "logs" / jid
    log_dir.mkdir(parents=True)
    j = Job(id=jid, task="t", model=served, endpoint="box", base_url="http://box:8080",
            cwd=str(tmp_path), run_cwd=str(tmp_path), isolation="none", status="succeeded",
            result="done", session_id="sess", log_dir=str(log_dir),
            metrics={"generate_tps": tps}, started_at=start, finished_at=end)
    j.endpoint_id, j.model_id, j._conn = eid, mid, conn
    return j


def _res(served="m-a"):
    return Resolution(Endpoint(name="box", base_url="http://box:8080", backend="llama.cpp"), served)


def _transcript(j):
    return (Path(j.log_dir) / "transcript.txt").read_text()


def test_a_run_whose_occupancy_changed_writes_no_sample(conn, tmp_path):
    """RULE: a run that held no one occupancy writes no sample, and says why in its log."""
    eid, a = _endpoint(conn), _model(conn)
    j = _job(conn, tmp_path, eid, a, T0, T0 + 200.0, 30.0)
    _neighbour(conn, eid, "half", T0 + 100.0, T0 + 400.0)
    asyncio.run(feedback.on_finish(j, _res()))
    assert conn.execute("SELECT 1 FROM speed_samples WHERE job_id = ?", (j.id,)).fetchone() is None
    assert "[speed-sample] none" in _transcript(j)


def test_a_failing_fold_never_fails_the_finish(conn, tmp_path, monkeypatch):
    """RULE: the fold is evidence: one that raises is a ``[feedback-error] curve`` line, the
    sample stays written and the job's status and error are as they were."""
    eid, a = _endpoint(conn), _model(conn)
    j = _job(conn, tmp_path, eid, a, T0, T0 + 200.0, 30.0)

    def boom(*args, **kw):
        raise RuntimeError("fold broke")

    monkeypatch.setattr(capacity, "fold_samples", boom)
    asyncio.run(feedback.on_finish(j, _res()))
    assert (j.status, j.error) == ("succeeded", None)
    assert conn.execute("SELECT sessions FROM speed_samples WHERE job_id = ?",
                        (j.id,)).fetchone()[0] == 1
    assert "[feedback-error] curve: RuntimeError: fold broke" in _transcript(j)


def test_a_fold_that_cannot_run_says_why_in_the_log(conn, tmp_path):
    """RULE: a sampled job whose curve was not folded says why in its log, once, in one line
    (``[curve] not folded: <reason>``); it still never raises, and the sample is written. A job
    that records no model and a finish with no resolution are the two silent skips this closes:
    a transcript with a sample and no curve line now always carries the reason."""
    eid, a = _endpoint(conn), _model(conn)
    no_model = _job(conn, tmp_path, eid, a, T0, T0 + 200.0, 30.0)
    no_model.model_id = None
    asyncio.run(feedback.on_finish(no_model, _res()))
    line = _transcript(no_model)
    assert line.count("[curve] not folded:") == 1
    assert "no model" in line
    no_res = _job(conn, tmp_path, eid, a, T0 + 1000.0, T0 + 1200.0, 30.0)
    asyncio.run(feedback.on_finish(no_res, None))
    line = _transcript(no_res)
    assert line.count("[curve] not folded:") == 1
    assert "no endpoint resolution" in line
    assert [r[0] for r in conn.execute("SELECT job_id FROM speed_samples ORDER BY job_id")] \
        == sorted([no_model.id, no_res.id])                           # both samples were written


def test_concurrency_2_samples_below_the_line_hold_normal_work_and_admit_background(conn, tmp_path):
    """End to end: eight jobs that ran alone at 40 t/s, then four pairs that ran together at
    10 t/s, each through ``on_finish``. The eighth concurrency-2 sample measures point 2 (10 /
    40 = 0.25, 2 x 0.25 < 0.75); its job's transcript names the hold and the row names the job.
    Admission over the store, through dispatch's curve read: with one of ours on the two-slot
    box, a normal job waits for the bottleneck slot and a background job takes it. The same
    endpoint read for another served model holds nothing."""
    eid, a = _endpoint(conn), _model(conn)
    t = T0
    for _ in range(8):
        asyncio.run(feedback.on_finish(_job(conn, tmp_path, eid, a, t, t + 300.0, 40.0), _res()))
        t += 400.0
    pairs = []
    for _ in range(4):
        pairs += [_job(conn, tmp_path, eid, a, t, t + 300.0, 10.0),
                  _job(conn, tmp_path, eid, a, t, t + 300.0, 10.0)]
        t += 400.0
    for j in pairs[:-1]:
        asyncio.run(feedback.on_finish(j, _res()))
    assert load_capacity(conn, eid) is None          # seven at 2: no row yet, the prior stands
    last = pairs[-1]
    asyncio.run(feedback.on_finish(last, _res()))
    assert sorted(r[0] for r in conn.execute("SELECT DISTINCT sessions FROM speed_samples")) == [1, 2]
    curve = load_capacity(conn, eid, "m-a")
    assert curve.points[2] == pytest.approx(0.25) and curve.measured >= {2}
    assert curve.first_hold() == 2 and curve.served_id == "m-a"
    assert "[curve] box (m-a): refined" in _transcript(last)
    assert "now holds from 2 sessions" in _transcript(last)
    assert _blob(conn, eid)["by"] == last.id

    # admission: one of ours admitted on the two-slot box, the probe's one busy slot ours
    with db.immediate(conn):
        conn.execute("INSERT INTO mcp_servers (id, pid, started, updated) VALUES ('s1', 1, ?, ?)",
                     (NOW, NOW))
    for jid, prio, forced in (("occ", "normal", True), ("n", "normal", False),
                              ("bg", "background", False)):
        with db.immediate(conn):
            conn.execute("INSERT INTO jobs (id, mcp_server_id, status, created_at)"
                         " VALUES (?, 's1', 'queued', ?)", (jid, NOW))
        queue.enqueue(conn, jid, eid, "s1", prio, forced=forced, now=NOW)
    ep = Endpoint(name="box", base_url="http://box:8080", backend="llama.cpp", host="box")
    reg = Registry(path=None, endpoints={"box": ep}, defaults=Defaults(), raw={}, local={})
    probes = {"box": {"up": True, "slots": {"total": 2, "busy": 1}}}
    facts = dispatch._facts(reg, conn, ep, "m-a", probes, NOW, endpoint_id=eid)
    assert facts.capacity.curve.first_hold() == 2
    assert not queue.try_admit(conn, "n", facts.capacity, NOW)
    assert conn.execute("SELECT blocked_by FROM queue_entries WHERE job_id = 'n'").fetchone()[0] \
        == "endpoint"
    queue.mark_dropped(conn, "n", "gone", NOW)
    assert queue.try_admit(conn, "bg", facts.capacity, NOW)
    # another served model on the same endpoint: the curve predicts and holds nothing
    other = dispatch._facts(reg, conn, ep, "m-b", probes, NOW, endpoint_id=eid)
    assert other.capacity.curve.first_hold() is None
    assert queue.endpoint_room(other.capacity, 1, "normal")
