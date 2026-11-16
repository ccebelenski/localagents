"""The expected wait read from job history (docs/superpowers/specs/2026-11-02-router-design.md §6
"Expected wait", the True-now block of 2026-11-15).

The oracle, sentence by sentence:
  1. "A session's or a queued job's time is read from JOB HISTORY: the finished non-calibration
     jobs on the same endpoint and served id, at the job's band when enough of them carry that
     band, else across all of them."
  2. "Probe seconds are not a wall-clock prediction; with too little history the band's default
     table is used and the estimate says "default"."
  3. "A session already past its expected time is read from how much longer the past jobs that
     ran at least that long went on to run, in place of the quarter-band floor."
  4. "A slot the bottleneck rule holds from the asking priority counts as occupied."
  5. "The estimate is written on the job's queue entry at every wake, so any server process
     reads the same refreshed number, a named job's included."

Every store here is a real test store (the conftest's LOCALAGENTS_DB redirect); the fleet is a
monkeypatched probe answer, never a network."""

import asyncio
import time

import pytest

from localagents import db, jobs
from localagents.registry import Defaults, Endpoint, Registry
from localagents.router import dispatch, scorecard

MIN = 60.0
# the live case of 2026-11-15 (job 58b7f76c on dgx1-a): the row's band times are calibration
# probe medians, tens of seconds, while the jobs behind them ran tens of minutes
PROBE_SCALE = {"1": 51.84, "2": 74.86, "3": 90.80}
# twelve finished named jobs, minutes: median (30 + 33) / 2 = 31.5
HISTORY_MIN = [12, 18, 22, 25, 28, 30, 33, 38, 45, 55, 65, 75]


@pytest.fixture
def store():
    s = jobs.JobStore()
    yield s
    s.close()


def _reg(slots=1):
    ep = Endpoint(name="box", base_url="http://box:8080", backend="llama.cpp")
    reg = Registry(path=None, endpoints={"box": ep}, defaults=Defaults(), raw={}, local={})
    probe = {"up": True, "models": ["m"], "context": {"m": 131_072},
             "slots": {"total": slots, "busy": 0}}
    return reg, ep, probe


def _ids(conn, ep, served="m"):
    eid = db.lookup_id(conn, "endpoints", name=ep.name, base_url=ep.url, backend=ep.backend,
                       host=ep.host)
    return eid, db.lookup_id(conn, "models", served_id=served)


def _probe_scale_row(conn, ep, served="m"):
    """A scorecard row as a calibration pass leaves it: band times in probe seconds."""
    eid, mid = _ids(conn, ep, served)
    row = scorecard.get_or_create_row(conn, ep.name, served, eid, mid)
    conn.execute("UPDATE scorecard_rows SET band_times = ? WHERE id = ?",
                 ('{"1": 51.84, "2": 74.86, "3": 90.80}', row.id))
    return row


def _finished(conn, ep, minutes, *, now, served="m", status="succeeded", calibration=0,
              scores=None, gap_s=600.0):
    """Finished jobs on the endpoint, newest last in the list (each finishes ``gap_s`` after the
    one before, the last one ``gap_s`` before ``now``); a named job unless ``scores`` gives a
    ticket's scores JSON."""
    eid, mid = _ids(conn, ep, served)
    n = len(minutes)
    for i, m in enumerate(minutes):
        finished = now - gap_s * (n - i)
        jid = db.new_uuid()
        conn.execute("INSERT INTO jobs (id, endpoint_id, model_id, status, created_at, started_at,"
                     " finished_at, updated, calibration) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (jid, eid, mid, status, finished - m * MIN - 1, finished - m * MIN, finished,
                      finished, calibration))
        if scores is not None:
            conn.execute("INSERT INTO dimension_definitions (version, document, created)"
                         " SELECT 1, '{}', 1.0 WHERE NOT EXISTS"
                         " (SELECT 1 FROM dimension_definitions WHERE version = 1)")
            conn.execute("INSERT INTO router_tickets (job_id, defs_version, scores, rows, divert)"
                         " VALUES (?, 1, ?, '[]', 0)", (jid, scores))


def _running(store, ep, started_at, served="m", priority="normal"):
    """One of our sessions on the endpoint: a named job admitted (forced) and running."""
    job = dispatch.new_job(store, priority=priority, forced=True, task="occupy", model=served,
                           endpoint=ep.name, base_url=ep.url, cwd="/w", run_cwd="/w",
                           isolation="none", backend=ep.backend, host=ep.host, context=131_072)
    job.status, job.started_at = "running", started_at
    job.changed()
    return job


def _facts(store, reg, ep, probe, now):
    return asyncio.run(dispatch.endpoint_facts(reg, store.conn, ep, "m", now,
                                               probes={ep.name: probe}))


# --- step 1: the live case, reproduced --------------------------------------------------------

def test_named_job_wait_reads_job_history_not_probe_seconds(store):
    """The 18.7 s estimate of 2026-11-15 (58b7f76c, admitted after 775 s): a row whose band
    times are probe seconds (band 2 = 74.86 s), twelve long finished named jobs on the
    endpoint, one session running 40 min (past the history's 31.5 min median), one named job
    asking. Main read the row's 74.86 s floored at a quarter: 18.7 s. The oracle reads
    history: the past jobs that ran at least 40 min ran 45, 55, 65 and 75, so 5, 15, 25 and
    35 more; the median of those, 20 min."""
    reg, ep, probe = _reg(slots=1)
    now = time.time()
    conn = store.conn
    _probe_scale_row(conn, ep)
    _finished(conn, ep, HISTORY_MIN, now=now)
    _running(store, ep, now - 40 * MIN)
    facts = _facts(store, reg, ep, probe, now)
    admits, position, wait = dispatch.named_wait(conn, facts, "normal", now)
    assert (admits, position) == (False, 1)
    assert wait == pytest.approx(20 * MIN, abs=0.1)


# --- step 2: the history reading (oracle sentences 1-3) ----------------------------------------

from localagents.router.capacity import Capacity, Curve  # noqa: E402
from localagents.router.decision import BriefScores  # noqa: E402
from localagents.router.select import (BAND_TIMES_S, HISTORY_N, MIN_AT_BAND, MIN_HISTORY,  # noqa: E402
                                       OVERRUN_FLOOR, Candidate, History, Session,
                                       expected_wait, job_time, remaining, wait_default)

NOW = 100_000.0
FLAT = Curve({1: 1.0, 2: 1.0, 3: 1.0})


def _scores(values=None):
    return BriefScores(dict(values or {}), {}, set(), False, {}, 1)


def _cand(history, *, ours=1, slots=1, running=None, ahead=(), curve=FLAT, requests=None):
    row = scorecard.Row(id=1, endpoint_name="box", served_id="m", endpoint_id=1, model_id=1,
                        set_version=None, band_times={2: 74.86}, speed_factor=None,
                        archived=None, facts=None)
    cap = Capacity("partitioned", slots, window=131_072,
                   requests_running=ours if requests is None else requests, curve=curve)
    return Candidate(row=row, cells=[], capacity=cap, ours_running=ours, queued_ahead=len(ahead),
                     window=None, expected_wait_s=None, excluded_by=None,
                     running=list(running or []), ahead_bands=list(ahead), history=history)


def _hist(minutes, band=None):
    return History([(m * MIN, band) for m in minutes])


def test_design_constants():
    """The choices the oracle leaves open, pinned (the report names the reasons): the newest
    20 finished jobs, at least 5 for history at all, 5 at a band to read that band alone."""
    assert (HISTORY_N, MIN_HISTORY, MIN_AT_BAND) == (20, 5, 5)


def test_time_reads_the_jobs_band_when_enough_carry_it_else_all():
    """Sentence 1: at the job's band when enough of them carry that band, else across all of
    them. Five band-3 runs of 60 min among seven named runs of 10 min: band 3 reads 60 min;
    band 1 (no run carries it) and a band of None (a named job) read all twelve, median 10."""
    runs = History([(60 * MIN, 3)] * 5 + [(10 * MIN, None)] * 7)
    assert job_time(runs, 3) == (60 * MIN, False)
    assert job_time(runs, 1) == (10 * MIN, False)
    assert job_time(runs, None) == (10 * MIN, False)
    # four at the band is not enough: all of them
    four = History([(60 * MIN, 3)] * 4 + [(10 * MIN, None)] * 7)
    assert job_time(four, 3) == (10 * MIN, False)


def test_too_little_history_reads_the_default_table_and_says_default():
    """Sentence 2: below MIN_HISTORY runs the band's default table answers and the estimate
    says "default"; at MIN_HISTORY it is history and does not. A row's probe-scale band times
    are never read either way."""
    thin = _hist([30, 30, 30, 30])
    assert job_time(thin, 3) == (float(BAND_TIMES_S[3]), True)
    assert job_time(thin, None, fallback=1) == (float(BAND_TIMES_S[1]), True)
    cand = _cand(thin, running=[Session(NOW - 60, band=2)])
    assert wait_default(cand) is True
    assert expected_wait(cand, _scores(), NOW) == pytest.approx(BAND_TIMES_S[2] - 60)
    enough = _hist([30, 30, 30, 30, 30])
    cand = _cand(enough, running=[Session(NOW - 60, band=2)])
    assert wait_default(cand) is False
    assert expected_wait(cand, _scores(), NOW) == pytest.approx(30 * MIN - 60)
    assert wait_default(_cand(None)) is True


def test_session_past_its_time_reads_the_conditional_residual():
    """Sentence 3: a session past its expected time reads how much longer the past jobs that
    ran at least that long went on to run (their median), in place of the quarter floor.
    History 12..75 min, median 31.5: at 40 min the runs of 45, 55, 65, 75 went on 5, 15, 25,
    35 more, median 20 min (the floor would have said 31.5 / 4 = 7.9 min)."""
    h = _hist(HISTORY_MIN)
    assert remaining(h, None, 40 * MIN) == pytest.approx(20 * MIN)
    # at 70 min only the 75-min run went that long: 5 more
    assert remaining(h, None, 70 * MIN) == pytest.approx(5 * MIN)
    # past every run: the fallback is a quarter of the expected time (the old floor's reading)
    assert remaining(h, None, 90 * MIN) == pytest.approx(31.5 * MIN * OVERRUN_FLOOR)
    # through expected_wait: one slot, that session, nobody ahead
    cand = _cand(h, running=[Session(NOW - 40 * MIN, band=None)])
    assert expected_wait(cand, _scores(), NOW) == pytest.approx(20 * MIN)


def test_session_before_its_time_never_reads_less_than_its_time_left():
    """The residual sits where the floor sat, in a max with the time minus elapsed: a session
    early in its run reads at least its expected time minus elapsed (here the residual is the
    larger: the runs that ran 15 min or more went on a median 20 more, against 31.5 - 15), and
    a just-started session reads the median."""
    h = _hist(HISTORY_MIN)
    assert remaining(h, None, 0.0) == pytest.approx(31.5 * MIN)
    early = remaining(h, None, 15 * MIN)
    assert early >= 31.5 * MIN - 15 * MIN
    # runs >= 15: 18 22 25 28 30 33 38 45 55 65 75; minus 15: 3 7 10 13 15 18 23 30 40 50 60;
    # the median of the eleven is the sixth, 18
    assert early == pytest.approx(18 * MIN)


def test_queued_ahead_and_foreign_sessions_read_history():
    """A queued job ahead takes its history time (sentence 1: "a queued job's time"), at its
    band when enough carry it; a foreign session holds a quarter of the history's time."""
    h = History([(60 * MIN, 3)] * 5 + [(10 * MIN, None)] * 7)
    # two slots, one ours just admitted (reads the all-runs median, 10 min), one free; a band-3
    # job ahead takes the free slot for 60 min: ours waits for the 10-min slot
    cand = _cand(h, ours=1, slots=2, running=[Session(NOW, band=None)], ahead=[3])
    assert expected_wait(cand, _scores(), NOW) == pytest.approx(10 * MIN)
    # one slot held by a foreign session (requests 1, ours 0): a quarter of 10 min
    foreign = _cand(h, ours=0, slots=1, requests=1)
    assert expected_wait(foreign, _scores(), NOW) == pytest.approx(10 * MIN * OVERRUN_FLOOR)


def test_history_reads_finished_noncalibration_runs_of_this_endpoint_and_served_id(store):
    """Sentence 1's set, read by ``dispatch.history`` over a real store: succeeded, failed and
    timeout runs of this endpoint x served id, newest first, at most HISTORY_N; never a
    calibration job, a cancelled, dropped or lost one, a running one, another endpoint's or
    another served id's. A ticketed run carries its band; a named one None."""
    reg, ep, _ = _reg()
    other = Endpoint(name="other", base_url="http://other:8080", backend="llama.cpp")
    now = time.time()
    conn = store.conn
    _finished(conn, ep, [11], now=now - 10_000, status="failed")
    _finished(conn, ep, [12], now=now - 9_000, status="timeout")
    _finished(conn, ep, [13], now=now - 8_000)
    _finished(conn, ep, [14], now=now - 7_000, scores='{"values": {"complexity": 3}}')
    _finished(conn, ep, [1], now=now - 6_000, calibration=1)
    _finished(conn, ep, [2], now=now - 5_000, status="cancelled")
    _finished(conn, ep, [3], now=now - 4_000, status="lost")
    _finished(conn, ep, [4], now=now - 3_000, served="other-model")
    _finished(conn, other, [5], now=now - 2_000)
    conn.execute("INSERT INTO jobs (id, endpoint_id, model_id, status, started_at, finished_at)"
                 " VALUES ('d', ?, ?, 'dropped', NULL, ?)", (*_ids(conn, ep), now - 1_000))
    _running(store, ep, now - 600)
    eid, _ = _ids(conn, ep)
    got = dispatch.history(conn, eid, "m")
    assert got.runs == [(14 * MIN, 3), (13 * MIN, None), (12 * MIN, None), (11 * MIN, None)]
    # at most the newest HISTORY_N
    _finished(conn, ep, [20 + i for i in range(HISTORY_N + 5)], now=now, gap_s=10.0)
    got = dispatch.history(conn, eid, "m")
    assert len(got.runs) == HISTORY_N
    assert got.runs[0] == (20 * MIN + (HISTORY_N + 4) * MIN, None)
    assert dispatch.history(conn, None, "m").runs == []


# --- step 3: a slot the bottleneck rule holds counts as occupied (sentence 4) ------------------

from localagents.router import queue as router_queue  # noqa: E402
from localagents.router.capacity import save_capacity  # noqa: E402
from localagents.router.select import usable_slots  # noqa: E402

# the llama.cpp shape, measured (a probed curve): 2 * 0.35 < 0.75, so it bottlenecks at 2
MEASURED = Curve({1: 1.0, 2: 0.35, 3: 0.25}, "probed", 2)
PRIOR = Curve({1: 1.0, 2: 0.35, 3: 0.25})   # the same shape as a prior: holds nothing


def test_bottleneck_held_slot_is_not_a_free_slot():
    """Sentence 4, the ced4b4ef case (ai9 logged ~0 min): two slots, one ours running, the
    second held by the bottleneck rule for a normal job. Normal waits for the running session
    (20 min left by history); background may take the slot, 0; on the prior curve nothing is
    held, 0."""
    h = _hist(HISTORY_MIN)
    running = [Session(NOW - 40 * MIN, band=None)]
    held = _cand(h, ours=1, slots=2, running=running, curve=MEASURED)
    assert router_queue.endpoint_hold(held.capacity, 1, "normal") == "bottleneck"
    assert expected_wait(held, _scores(), NOW, priority="normal") == pytest.approx(20 * MIN)
    assert expected_wait(held, _scores(), NOW, priority="urgent") == pytest.approx(20 * MIN)
    assert expected_wait(held, _scores(), NOW, priority="background") == 0.0
    # the default priority is the fail-closed reading: held
    assert expected_wait(held, _scores(), NOW) == pytest.approx(20 * MIN)
    prior = _cand(h, ours=1, slots=2, running=running, curve=PRIOR)
    assert expected_wait(prior, _scores(), NOW, priority="normal") == 0.0


def test_bottleneck_holds_every_slot_from_n_on():
    """"Bottlenecks at n" means from n on: on three slots bottlenecking at 2 a normal job has
    one usable slot, so with two of ours running it waits for BOTH to end (the later one), not
    for the first."""
    h = _hist(HISTORY_MIN)
    assert usable_slots(Capacity("partitioned", 3, curve=MEASURED), "normal") == 1
    assert usable_slots(Capacity("partitioned", 3, curve=MEASURED), "background") == 3
    assert usable_slots(Capacity("partitioned", 3, curve=PRIOR), "normal") == 3
    running = [Session(NOW - 40 * MIN, band=None),   # 20 min left
               Session(NOW, band=None)]              # just started: 31.5 min
    cand = _cand(h, ours=2, slots=3, running=running, curve=MEASURED)
    assert expected_wait(cand, _scores(), NOW, priority="normal") == pytest.approx(31.5 * MIN)
    assert expected_wait(cand, _scores(), NOW, priority="background") == 0.0


def test_a_foreign_session_counts_toward_the_bottleneck():
    """The foreign session is an occupant like ours (capacity.bottleneck_slot counts it): on
    two slots bottlenecking at 2 with one foreign session and none of ours, a normal job waits
    for the foreign hold (a quarter of the history's time), not 0."""
    h = _hist(HISTORY_MIN)
    cand = _cand(h, ours=0, slots=2, requests=1, curve=MEASURED)
    assert router_queue.endpoint_hold(cand.capacity, 0, "normal") == "bottleneck"
    assert expected_wait(cand, _scores(), NOW, priority="normal") == pytest.approx(
        31.5 * MIN * OVERRUN_FLOOR)


def test_usable_slots_reads_the_rule_through_queue_endpoint_hold(monkeypatch):
    """No second copy of the rule: the wait asks ``queue.endpoint_hold`` (here a stand-in that
    holds every slot after the first for everyone)."""
    asked = []

    def hold(capacity, ours, priority):
        asked.append((ours, priority))
        return None if ours == 0 else "bottleneck"

    monkeypatch.setattr(router_queue, "endpoint_hold", hold)
    assert usable_slots(Capacity("partitioned", 3, curve=FLAT), "background") == 1
    assert asked == [(0, "background"), (1, "background"), (2, "background")]


def test_named_job_on_a_measured_bottleneck_reads_the_running_session(store):
    """Sentence 4 end to end over a real store: a two-slot llama.cpp box whose stored curve was
    probed (bottleneck at 2), one of our sessions running 40 min, twelve finished runs: a named
    normal job cannot admit (the slot is held) and its wait is the session's 20 min, not 0; a
    named background job admits at once."""
    reg, ep, probe = _reg(slots=2)
    now = time.time()
    conn = store.conn
    _finished(conn, ep, HISTORY_MIN, now=now)
    _running(store, ep, now - 40 * MIN)
    eid, _ = _ids(conn, ep)
    save_capacity(conn, eid, "partitioned", MEASURED)
    probe = {**probe, "slots": {"total": 2, "busy": 1}}
    facts = _facts(store, reg, ep, probe, now)
    admits, position, wait = dispatch.named_wait(conn, facts, "normal", now)
    assert (admits, position) == (False, 1)
    assert wait == pytest.approx(20 * MIN, abs=0.1)
    admits, _, wait = dispatch.named_wait(conn, facts, "background", now)
    assert admits is True and wait == 0.0


# --- step 4: the estimate lives on the queue entry (sentence 5) --------------------------------

from localagents import server  # noqa: E402


def _named(store, ep, served="m", priority="normal"):
    return dispatch.new_job(store, priority=priority, task="wait", model=served, endpoint=ep.name,
                            base_url=ep.url, cwd="/w", run_cwd="/w", isolation="none",
                            backend=ep.backend, host=ep.host, context=131_072)


def _patch_probe(monkeypatch, probe):
    async def fake(self, endpoint):
        return dict(probe)

    monkeypatch.setattr(Registry, "probe", fake)


def test_named_estimate_is_on_the_entry_refreshed_at_each_wake_and_read_by_another_process(
        store, monkeypatch):
    """Sentence 5 over a real store: a named job queued behind a session running 40 min gets
    its estimate (20 min by history) written on its queue entry at the first attempt; a SECOND
    connection (another server process: its own store, no Job in memory) reads that number
    through ``waiting_estimate``, ``job_status``'s routing extras, ``queued_line`` and the
    queue tool's view. A wake 30 min later rewrites it (the session is now 70 min in: 5 min);
    the admission nulls it, and the other process then reads no queue at all."""
    reg, ep, probe = _reg(slots=1)
    _patch_probe(monkeypatch, probe)
    now = time.time()
    conn = store.conn
    _finished(conn, ep, HISTORY_MIN, now=now)
    occupant = _running(store, ep, now - 40 * MIN)
    job = _named(store, ep)
    facts = _facts(store, reg, ep, probe, now)
    assert dispatch.try_admit_now(conn, job, facts, now) is False
    dispatch.note_queued(conn, job, facts, now)
    other = db.open_db(db.db_path())
    try:
        assert other is not conn
        assert dispatch.waiting_estimate(other, job.id) == pytest.approx(20 * MIN, abs=1.0)
        assert server.store is not store
        extras = server._routing_extras(job.id, "queued")
        assert extras["queue"]["position"] == 1
        assert extras["queue"]["expected_wait_s"] == pytest.approx(20 * MIN, abs=1.0)
        assert extras["ticket"] is None   # a named job: no ticket, the entry carries the wait
        stranger = jobs.Job(id=job.id, log_dir="/nonexistent", task="", model="m", endpoint="box",
                            base_url=ep.url, cwd="/w", run_cwd="/w", isolation="none")
        assert stranger._queue_estimate is None   # another process: nothing in memory
        assert dispatch.queued_line(other, stranger) == "queued #1, ~20 min"
        view = asyncio.run(dispatch.queue_view(reg, other, ep))
        assert view["queued"][0]["expected_wait_s"] == pytest.approx(20 * MIN, abs=1.0)

        # a wake 30 min on: the session is 70 min in, the 75-min run went 5 more
        occupant.started_at = now - 70 * MIN
        occupant.changed()
        assert asyncio.run(dispatch._wake(store, conn, job, reg, None)) is False
        assert dispatch.waiting_estimate(other, job.id) == pytest.approx(5 * MIN, abs=1.0)
        assert server._routing_extras(job.id, "queued")["queue"]["expected_wait_s"] == pytest.approx(
            5 * MIN, abs=1.0)

        # the session ends: the next wake admits, the entry's estimate is gone
        occupant.status, occupant.finished_at = "succeeded", time.time()
        occupant.changed()
        assert asyncio.run(dispatch._wake(store, conn, job, reg, None)) is not False
        row = other.execute("SELECT admitted, est_wait_s FROM queue_entries WHERE job_id = ?",
                            (job.id,)).fetchone()
        assert row["admitted"] is not None and row["est_wait_s"] is None
        assert dispatch.waiting_estimate(other, job.id) is None
        assert job._queue_estimate is None
        assert server._routing_extras(job.id, "running")["queue"] is None
    finally:
        other.close()


def test_set_estimate_writes_only_a_waiting_entry_and_admission_nulls_it(store):
    """``queue.set_estimate`` writes a waiting entry only; ``try_admit`` nulls the estimate in
    the admission's own write; a forced entry (admitted at insert) never carries one."""
    reg, ep, probe = _reg(slots=1)
    now = time.time()
    conn = store.conn
    job = _named(store, ep)
    assert router_queue.set_estimate(conn, job.id, 123.0) is True
    assert router_queue.estimate_of(conn, job.id) == 123.0
    facts = _facts(store, reg, ep, probe, now)
    assert dispatch.try_admit_now(conn, job, facts, now) is True
    assert conn.execute("SELECT est_wait_s FROM queue_entries WHERE job_id = ?",
                        (job.id,)).fetchone()[0] is None
    assert router_queue.set_estimate(conn, job.id, 456.0) is False
    assert router_queue.estimate_of(conn, job.id) is None
    forced = _running(store, ep, now)
    assert router_queue.set_estimate(conn, forced.id, 1.0) is False
    assert router_queue.set_estimate(conn, "no-such-job", 1.0) is False


def test_an_unestimated_entry_reads_the_tickets_enqueue_wait(store):
    """Before the job's loop writes its entry (or for an entry from before schema v13) the
    reader falls back to the ticket's wait at enqueue; with neither, None."""
    reg, ep, _ = _reg(slots=1)
    conn = store.conn
    job = _named(store, ep)
    assert dispatch.waiting_estimate(conn, job.id) is None
    conn.execute("INSERT INTO dimension_definitions (version, document, created) VALUES (1, '{}', 1.0)")
    conn.execute("INSERT INTO router_tickets (job_id, defs_version, scores, rows, divert, est_wait_s)"
                 " VALUES (?, 1, '{}', '[]', 0, 77.0)", (job.id,))
    assert dispatch.waiting_estimate(conn, job.id) == 77.0
    router_queue.set_estimate(conn, job.id, 900.0)
    assert dispatch.waiting_estimate(conn, job.id) == 900.0


# --- step 5: the "default" flag says which source the number came from -------------------------

from localagents.router import definitions  # noqa: E402
from localagents.router import select as select_mod  # noqa: E402
from localagents.router.select import FALLBACK_BAND, brief_band  # noqa: E402

CELLS = {"software_dev": 4, "tool_use": 4, "computer_use": 4, "complexity": 4, "judgement": 4,
         "completeness": 4, "adherence": 4}


def _calibrated(conn, ep, served, cells):
    eid, mid = _ids(conn, ep, served)
    row = scorecard.get_or_create_row(conn, ep.name, served, eid, mid)
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
                 " VALUES (?, ?, 'core', '2026.11', '{}', 1.0, 'complete')", (db.new_uuid(), row.id))
    pass_id = db.new_uuid()
    conn.execute("INSERT INTO scorecard_passes (id, row_id, at, constants, observations)"
                 " VALUES (?, ?, 2.0, '{}', 0)", (pass_id, row.id))
    version = definitions.load(conn).version
    for dimension, estimate in cells.items():
        conn.execute("INSERT INTO scorecard_cells (pass_id, dimension, language, estimate, count,"
                     " probes_n, jobs_n, thin, defs_version) VALUES (?, ?, '', ?, 6, 6, 0, 0, ?)",
                     (pass_id, dimension, estimate, version))
    conn.execute("UPDATE scorecard_rows SET band_times = ? WHERE id = ?",
                 ('{"1": 51.84, "2": 74.86, "3": 90.80}', row.id))
    return row.id


def test_ticket_wait_table_and_default_flag_say_history_or_default(store):
    """The ticket's table (``_row_dicts``) and the queued ticket's ``est_wait_default`` say
    where the number came from: a row with job history reads history (False) whatever bands the
    brief and the sessions carry; a row with too little reads the table (True). Two busy
    one-slot rows; "rich" has twelve finished runs, "thin" two; both rows' band times are probe
    seconds and neither number reads them. The wire shape is unchanged (``wait_default`` and
    ``est_wait_default`` keep their names and types)."""
    rich = Endpoint(name="rich", base_url="http://rich:8080", backend="llama.cpp")
    thin = Endpoint(name="thin", base_url="http://thin:8080", backend="llama.cpp")
    auto = Endpoint(name="auto", base_url="", backend="router", decision_url="http://d3.test:8766")
    reg = Registry(path=None, endpoints={"rich": rich, "thin": thin, "auto": auto},
                   defaults=Defaults(), raw={}, local={})
    probe = {"up": True, "models": ["m"], "context": {"m": 131_072}, "slots": {"total": 1, "busy": 1}}
    now = time.time()
    conn = store.conn
    rows = {"rich": _calibrated(conn, rich, "m", CELLS),
            "thin": _calibrated(conn, thin, "m", {**CELLS, "software_dev": 3})}
    _finished(conn, rich, HISTORY_MIN, now=now)
    _finished(conn, thin, [30, 30], now=now)
    _running(store, rich, now - 40 * MIN)
    _running(store, thin, now - 10 * MIN)
    scores = BriefScores({"software_dev": 4, "complexity": 4}, {}, set(), False, {}, 1)
    view = asyncio.run(dispatch.decide(reg, conn, auto, "build it", scores, "normal",
                                       probes={"rich": probe, "thin": probe}, now=now))
    table = {r["row_id"]: r for r in view.rows}
    assert table[rows["rich"]]["wait_default"] is False
    assert table[rows["rich"]]["expected_wait_s"] == pytest.approx(20 * MIN, abs=0.1)
    assert table[rows["thin"]]["wait_default"] is True
    # the table's band 4 for a named session (the brief's band): 5400 - 600
    assert table[rows["thin"]]["expected_wait_s"] == pytest.approx(BAND_TIMES_S[4] - 10 * MIN, abs=0.1)
    assert view.decision.queue_for is not None and view.decision.queue_for.row.id == rows["rich"]
    ticket = dispatch._ticket(view, auto, "normal", now, None, frozenset())
    assert ticket["flags"]["est_wait_default"] is False
    assert ticket["est_wait_s"] == pytest.approx(20 * MIN, abs=0.1)
    # the thin row best (rich's cells weaker): queued there, the ticket says default
    conn.execute("UPDATE scorecard_cells SET estimate = 1 WHERE dimension = 'software_dev'"
                 " AND pass_id IN (SELECT id FROM scorecard_passes WHERE row_id = ?)", (rows["rich"],))
    view = asyncio.run(dispatch.decide(reg, conn, auto, "build it", scores, "normal",
                                       probes={"rich": probe, "thin": probe}, now=now))
    assert view.decision.queue_for.row.id == rows["thin"]
    ticket = dispatch._ticket(view, auto, "normal", now, None, frozenset())
    assert ticket["flags"]["est_wait_default"] is True


def test_one_brief_band_reading():
    """The two ``_brief_band`` copies are one (``select.brief_band``; dispatch keeps none): a
    determined integer band, else the middle band, for an undetermined, unproven, bool or
    non-integer value alike."""
    assert not hasattr(dispatch, "_brief_band") and not hasattr(dispatch, "_wait_default")
    assert not hasattr(select_mod, "_brief_band") and not hasattr(select_mod, "band_time")

    def s(values, undetermined=()):
        return BriefScores(values, {}, set(undetermined), False, {}, 1)

    assert brief_band(s({"complexity": 3})) == 3
    assert brief_band(s({"complexity": 3}, ["complexity"])) == FALLBACK_BAND
    assert brief_band(s({"complexity": 3}), frozenset({"complexity"})) == FALLBACK_BAND
    assert brief_band(s({"complexity": True})) == FALLBACK_BAND
    assert brief_band(s({"complexity": "high"})) == FALLBACK_BAND
    assert brief_band(s({})) == FALLBACK_BAND


# --- step 6: a routed job's estimate rides the same entry ---------------------------------------

def test_routed_estimate_is_on_the_entry_at_enqueue_and_matches_the_ticket(store, monkeypatch, tmp_path):
    """Sentence 5 for a routed job: the decision's history-scale wait is the ticket's
    ``est_wait_s`` and, at the first admission attempt, the entry's; another connection reads
    the entry's number. Probe-scale band times on the row are read by neither."""
    box = Endpoint(name="box", base_url="http://box:8080", backend="llama.cpp")
    auto = Endpoint(name="auto", base_url="", backend="router", decision_url="http://d3.test:8766")
    reg = Registry(path=None, endpoints={"box": box, "auto": auto}, defaults=Defaults(), raw={},
                   local={})
    probe = {"up": True, "models": ["m"], "context": {"m": 131_072}, "slots": {"total": 1, "busy": 1}}
    _patch_probe(monkeypatch, probe)

    async def fake_score(client, url, model, text, defs, **kw):
        return BriefScores({"software_dev": 4}, {"software_dev": 0.9},
                           set(definitions.DIMENSIONS) - {"software_dev"}, False, {}, defs.version)

    monkeypatch.setattr(dispatch, "score", fake_score)
    now = time.time()
    conn = store.conn
    _calibrated(conn, box, "m", CELLS)
    _finished(conn, box, HISTORY_MIN, now=now)
    _running(store, box, now - 40 * MIN)
    routed = asyncio.run(dispatch.route(reg, conn, auto, "build it", str(tmp_path), "normal"))
    assert routed.decision.queue_for is not None
    assert routed.ticket["est_wait_s"] == pytest.approx(20 * MIN, abs=5.0)
    assert routed.ticket["flags"]["est_wait_default"] is False
    res = routed.resolution
    job = dispatch.new_job(store, priority="normal", routed=routed, task="build it",
                           model=res.served_model, endpoint=res.endpoint.name,
                           base_url=res.endpoint.url, cwd="/w", run_cwd="/w", isolation="none",
                           backend=res.endpoint.backend, host=res.endpoint.host, context=res.context)
    job._route = routed
    assert dispatch.try_admit_now(conn, job, routed.facts, time.time()) is False
    dispatch.note_queued(conn, job, routed.facts, time.time())
    other = db.open_db(db.db_path())
    try:
        assert router_queue.estimate_of(other, job.id) == routed.ticket["est_wait_s"]
        assert server._routing_extras(job.id, "queued")["queue"]["expected_wait_s"] == (
            routed.ticket["est_wait_s"])
    finally:
        other.close()
