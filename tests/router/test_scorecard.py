"""Router 6 (router-design spec §8, §10 "Complete"/"Ends with a pass"; calibration-set spec §7):
scorecard rows and identity, evidence reads, the pass.

Evidence is built here with SQL on the ``conn`` fixture: probe results through
``calibration_runs``/``calibration_results`` (``job_id`` may be NULL), verdicts, tickets and
gates through minimal ``jobs`` rows joined to ``endpoints``/``models`` for the row identity
(name × served id, §8)."""

import json
import time

import pytest

from localagents.router import definitions, scorecard


def _endpoint(conn, name):
    conn.execute("INSERT OR IGNORE INTO endpoints (name, base_url, backend, host)"
                 " VALUES (?, 'http://host:1/v1', 'llama.cpp', 'host')", (name,))
    return conn.execute("SELECT id FROM endpoints WHERE name = ?", (name,)).fetchone()[0]


def _model(conn, served_id):
    return conn.execute("INSERT INTO models (served_id) VALUES (?)", (served_id,)).lastrowid


def _setup(conn, name="qwen", served_id="qwen3.8"):
    """A live row for name × served id, with its endpoint and model rows beside it."""
    return scorecard.get_or_create_row(conn, name, served_id,
                                       _endpoint(conn, name), _model(conn, served_id))


def _job(conn, jid, endpoint_id, model_id, *, started_at=100.0, finished_at=200.0,
         calibration=0, gates=None):
    conn.execute(
        "INSERT INTO jobs (id, endpoint_id, model_id, status, created_at, started_at,"
        " finished_at, calibration, gates)"
        " VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?, ?)",
        (jid, endpoint_id, model_id, started_at - 10.0, started_at, finished_at, calibration,
         json.dumps(gates) if gates is not None else None))


def _probe(conn, row, probe_id, status, *, seconds=None, started=100.0):
    """One probe of one run of this row (job_id NULL: a checker result needs no job row)."""
    conn.execute(
        "INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
        " VALUES (?, ?, 'core', '2026.11', '{}', ?, 'complete')", (f"run-{probe_id}", row.id, started))
    conn.execute("INSERT INTO calibration_results (run_id, probe_id, job_id, status, seconds)"
                 " VALUES (?, ?, NULL, ?, ?)", (f"run-{probe_id}", probe_id, status, seconds))


def _ticket_verdict(conn, jid, values, *, accepted=1, dimensions=None, at=300.0):
    d = definitions.load(conn).version
    conn.execute(
        "INSERT INTO router_tickets (job_id, defs_version, scores, rows, divert)"
        " VALUES (?, ?, ?, '[]', 0)",
        (jid, d, json.dumps({"values": values, "confidences": {}, "undetermined": [],
                             "truncated": False, "defs_version": d})))
    conn.execute("INSERT INTO verdicts (job_id, accepted, dimensions, at) VALUES (?, ?, ?, ?)",
                 (jid, accepted, json.dumps(dimensions) if dimensions is not None else None, at))


def _cell(conn, row_id, dimension, language="", pass_id=None):
    for c in scorecard.cells(conn, row_id, pass_id=pass_id):
        if c.dimension == dimension and c.language == language:
            return c
    raise AssertionError(f"no cell ({dimension}, {language!r})")


def _rw(k):
    """The recency multiplier of the k-th newest observation in a cell."""
    return 0.5 ** (k / scorecard.RECENCY_HALFLIFE_OBS)


CLEAN_GATES = {"tests": True, "stop_rule": True, "not_capped": True,
               "no_tmp": True, "files_within": True, "lint": True, "files_n": 10}


def test_pass_capability_cell_is_the_highest_band_with_acceptance_rate(conn):
    row = _setup(conn)
    # the brief's worked example: accepts at 2, 2, 3 and rejects at 4, 4 earn band 3, not 4
    plan = [("pass", 2), ("pass", 2), ("pass", 3), ("fail", 4), ("fail", 4)]
    probes = {}
    for i, (status, band) in enumerate(plan):
        _probe(conn, row, f"c{i}", status, started=100.0 + i)
        probes[f"c{i}"] = {"labels": {"complexity": band}, "nominal_s": 60.0}
    scorecard.run_pass(conn, row.id, probes=probes)
    cell = _cell(conn, row.id, "complexity")
    assert cell.estimate == 3.0
    assert cell.count == 5.0            # source weights, not recency: five probes are five
    assert (cell.probes_n, cell.jobs_n) == (5, 0)
    assert not cell.thin                # five observations is the thin line, not below it


def test_pass_marks_thin_below_min_count(conn):
    row = _setup(conn)
    _probe(conn, row, "c0", "pass")
    scorecard.run_pass(conn, row.id, probes={"c0": {"labels": {"complexity": 3}, "nominal_s": 60.0}})
    cell = _cell(conn, row.id, "complexity")
    assert cell.estimate is None          # weighted count 1 < MIN_COUNT 3
    assert cell.count < scorecard.MIN_COUNT
    assert cell.thin
    untouched = _cell(conn, row.id, "judgement")   # every capability cell is written each pass
    assert (untouched.count, untouched.estimate, untouched.thin) == (0.0, None, True)


def test_three_probes_meet_min_count_exactly(conn):
    row = _setup(conn)
    probes = {}
    for i in range(3):
        _probe(conn, row, f"a{i}", "pass", started=100.0 + i)
        probes[f"a{i}"] = {"labels": {"complexity": 3}, "nominal_s": 60.0}
    scorecard.run_pass(conn, row.id, probes=probes)
    cell = _cell(conn, row.id, "complexity")
    # MIN_COUNT 3 is the point at which a cell gets an estimate; no recency subtraction
    assert cell.count == 3.0
    assert cell.estimate == 3.0


def test_five_probes_are_not_thin(conn):
    row = _setup(conn)
    probes = {}
    for i in range(5):
        _probe(conn, row, f"a{i}", "pass", started=100.0 + i)
        probes[f"a{i}"] = {"labels": {"complexity": 3}, "nominal_s": 60.0}
    scorecard.run_pass(conn, row.id, probes=probes)
    cell = _cell(conn, row.id, "complexity")
    assert cell.count == 5.0
    assert not cell.thin                # thin is below five observations, not at five


def test_all_rejects_above_min_count_estimate_zero(conn):
    row = _setup(conn)
    probes = {}
    for i in range(10):
        _probe(conn, row, f"r{i}", "fail", started=100.0 + i)
        probes[f"r{i}"] = {"labels": {"complexity": 1}, "nominal_s": 60.0}
    scorecard.run_pass(conn, row.id, probes=probes)
    cell = _cell(conn, row.id, "complexity")
    assert cell.count >= scorecard.MIN_COUNT
    assert cell.estimate == 0.0     # enough evidence and no band qualifies: fail-closed


def test_disagreement_between_probe_accepts_and_job_rejects_raises_the_notice(conn):
    row = _setup(conn)
    probes = {}
    for i in range(5):
        _probe(conn, row, f"a{i}", "pass", started=100.0 + i)
        probes[f"a{i}"] = {"labels": {"complexity": 4}, "nominal_s": 60.0}
    for i in range(10):
        _job(conn, f"j{i}", row.endpoint_id, row.model_id, finished_at=200.0 + i)
        _ticket_verdict(conn, f"j{i}", {"complexity": 1}, accepted=0, at=200.0 + i)
    pass_id = scorecard.run_pass(conn, row.id, probes=probes)
    consts = json.loads(conn.execute("SELECT constants FROM scorecard_passes WHERE id = ?",
                                     (pass_id,)).fetchone()[0])
    # probes say 4, traffic says 0: the full meter apart, and the notice names it
    assert consts["disagreement"] == pytest.approx(1.0)
    assert scorecard.notices(conn, row.id, "2026.11") == ["needs recalibration"]


def test_pass_censoring_an_accept_at_3_also_counts_for_bands_below(conn):
    row = _setup(conn)
    probes = {}
    for i in range(3):
        _probe(conn, row, f"a{i}", "pass", started=100.0 + i)
        probes[f"a{i}"] = {"labels": {"complexity": 3}, "nominal_s": 60.0}
    for i in range(3):
        _probe(conn, row, f"r{i}", "fail", started=103.0 + i)
        probes[f"r{i}"] = {"labels": {"complexity": 3}, "nominal_s": 60.0}
    # band 3 is refused (three accepts against three rejects, 0.5 < 0.6); the accepts at 3
    # still support band 2, where the rejects at 3 do not count against it at all
    scorecard.run_pass(conn, row.id, probes=probes)
    assert _cell(conn, row.id, "complexity").estimate == 2.0


def test_pass_rate_cells_from_gates(conn):
    row = _setup(conn)
    for i in range(7):
        _job(conn, f"j{i}", row.endpoint_id, row.model_id, finished_at=100.0 + i, gates=CLEAN_GATES)
    scorecard.run_pass(conn, row.id)
    comps = _cell(conn, row.id, "completeness")
    assert comps.estimate == pytest.approx(4.0)          # every evaluable gate clean
    assert comps.count == pytest.approx(0.5 * 7)         # job weights, not recency
    assert (comps.probes_n, comps.jobs_n) == (0, 7)      # traffic evidence, so jobs
    assert _cell(conn, row.id, "adherence").estimate == pytest.approx(4.0)
    # one dirty gate, on the newest job, lands on adherence only
    _job(conn, "jd", row.endpoint_id, row.model_id, finished_at=200.0,
         gates=dict(CLEAN_GATES, lint=False))
    scorecard.run_pass(conn, row.id)
    adh = _cell(conn, row.id, "adherence")
    total = sum(_rw(k) for k in range(8))
    assert adh.estimate == pytest.approx(4.0 * (total - _rw(0)) / total)
    assert _cell(conn, row.id, "completeness").estimate == pytest.approx(4.0)


def test_pass_records_constants_and_a_new_generation_each_time(conn):
    row = _setup(conn)
    _probe(conn, row, "p", "pass", seconds=90.0)
    probes = {"p": {"labels": {"complexity": 2}, "nominal_s": 90.0}}
    first = scorecard.run_pass(conn, row.id, probes=probes)
    second = scorecard.run_pass(conn, row.id, probes=probes)
    assert first != second
    assert conn.execute("SELECT COUNT(*) FROM scorecard_passes WHERE row_id = ?",
                        (row.id,)).fetchone()[0] == 2
    consts = json.loads(conn.execute("SELECT constants FROM scorecard_passes WHERE id = ?",
                                     (second,)).fetchone()[0])
    for name in ("ACCEPT_RATE", "MIN_COUNT", "THIN_BELOW", "PROBE_WEIGHT", "JOB_WEIGHT",
                 "RECENCY_HALFLIFE_OBS", "PASS_PENDING_N"):
        assert consts[name] == getattr(scorecard, name)
    assert consts["unlabelled_probes"] == 0
    assert consts["disagreement"] == 0.0
    obs_n = conn.execute("SELECT observations FROM scorecard_passes WHERE id = ?",
                         (second,)).fetchone()[0]
    assert obs_n == len(scorecard.observations(conn, row.id, probes))
    assert all(c.pass_id == second for c in scorecard.cells(conn, row.id))
    older = scorecard.cells(conn, row.id, pass_id=first)
    assert all(c.pass_id == first for c in older)
    assert len(older) == len(scorecard.cells(conn, row.id))


def test_replay_under_changed_constants_changes_cells(conn):
    row = _setup(conn)
    probes = {}
    for i in range(4):
        _probe(conn, row, f"a{i}", "pass", started=100.0 + i)
        probes[f"a{i}"] = {"labels": {"complexity": 2}, "nominal_s": 60.0}
    _probe(conn, row, "r0", "fail", started=104.0)
    probes["r0"] = {"labels": {"complexity": 2}, "nominal_s": 60.0}
    default = scorecard.run_pass(conn, row.id, probes=probes)
    assert _cell(conn, row.id, "complexity").estimate == 2.0     # 4 accepts, 1 reject: 0.8 ≥ 0.6
    scorecard.run_pass(conn, row.id, constants={"ACCEPT_RATE": 0.99}, probes=probes)
    assert _cell(conn, row.id, "complexity").estimate == 1.0     # 0.8 fails 0.99; band 1 has no rejects
    # the earlier generation is kept intact (history reads as a before and after)
    assert _cell(conn, row.id, "complexity", pass_id=default).estimate == 2.0


def test_software_dev_for_language_falls_back_to_the_aggregate(conn):
    row = _setup(conn)
    probes = {}
    for i in range(4):   # four accepts: the language cell clears MIN_COUNT
        _probe(conn, row, f"py{i}", "pass", started=100.0 + i)
        probes[f"py{i}"] = {"labels": {"software_dev": 3, "language": "python"},
                            "nominal_s": 60.0}
    scorecard.run_pass(conn, row.id, probes=probes)
    cells = scorecard.cells(conn, row.id)
    assert _cell(conn, row.id, "software_dev", "python").estimate == 3.0
    assert _cell(conn, row.id, "software_dev", "").estimate == 3.0   # feeds both cells
    assert scorecard.software_dev_for(cells, "python").language == "python"
    assert scorecard.software_dev_for(cells, "rust").language == ""  # unseen language: the aggregate


def test_language_none_feeds_only_the_aggregate_cell(conn):
    row = _setup(conn)
    probes = {}
    for i in range(3):
        _probe(conn, row, f"p{i}", "pass", started=100.0 + i)
        probes[f"p{i}"] = {"labels": {"software_dev": 3, "language": "none"},
                           "nominal_s": 60.0}
    scorecard.run_pass(conn, row.id, probes=probes)
    sd = [c for c in scorecard.cells(conn, row.id) if c.dimension == "software_dev"]
    assert [(c.language, c.count, c.estimate) for c in sd] == [("", 3.0, 3.0)]
    assert scorecard.software_dev_for(sd, "none").language == ""


def test_served_id_change_archives_and_return_revives(conn):
    row = _setup(conn, served_id="qwen-a")
    scorecard.archive_stale_rows(conn, "qwen", ["qwen-b"], "served id changed")
    gone = next(r for r in scorecard.rows(conn, include_archived=True) if r.id == row.id)
    assert gone.archived is not None
    reason = conn.execute("SELECT archive_reason FROM scorecard_rows WHERE id = ?",
                          (row.id,)).fetchone()[0]
    assert reason == "served id changed"
    assert scorecard.rows(conn) == []
    back = scorecard.get_or_create_row(conn, "qwen", "qwen-a", row.endpoint_id, row.model_id)
    assert back.id == row.id and back.archived is None
    assert [r.id for r in scorecard.rows(conn)] == [row.id]


def test_multi_model_endpoint_keeps_sibling_rows(conn):
    e = _endpoint(conn, "shared")
    a = scorecard.get_or_create_row(conn, "shared", "model-a", e, _model(conn, "model-a"))
    b = scorecard.get_or_create_row(conn, "shared", "model-b", e, _model(conn, "model-b"))
    assert scorecard.archive_stale_rows(conn, "shared", ["model-a"], "gone") == 1
    assert [r.id for r in scorecard.rows(conn)] == [a.id]
    assert next(r for r in scorecard.rows(conn, include_archived=True)
                if r.id == b.id).archived is not None
    assert scorecard.archive_stale_rows(conn, "shared", [], "retired") == 1
    assert scorecard.rows(conn) == []   # the empty list archives every live row of the name


def test_archive_row_archives_one_live_row_and_keeps_an_archived_moment(conn):
    row = _setup(conn)
    scorecard.archive_row(conn, row.id, "calibrate --reset")
    assert scorecard.rows(conn) == []
    gone = conn.execute("SELECT archived, archive_reason FROM scorecard_rows WHERE id = ?",
                        (row.id,)).fetchone()
    assert gone["archived"] is not None and gone["archive_reason"] == "calibrate --reset"
    # a second call leaves the archived row's moment and reason alone
    scorecard.archive_row(conn, row.id, "a later command")
    kept = conn.execute("SELECT archived, archive_reason FROM scorecard_rows WHERE id = ?",
                        (row.id,)).fetchone()
    assert kept["archived"] == gone["archived"]
    assert kept["archive_reason"] == "calibrate --reset"
    # the pair's return still revives it, history intact
    back = scorecard.get_or_create_row(conn, "qwen", "qwen3.8", row.endpoint_id, row.model_id)
    assert back.id == row.id and back.archived is None


def test_core_complete_reads_the_run_status(conn):
    row = _setup(conn)
    assert scorecard.core_complete(conn, row.id) is False
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
                 " VALUES ('r-med', ?, 'medium', '2026.11', '{}', 1, 'complete')", (row.id,))
    assert scorecard.core_complete(conn, row.id) is False      # a tier other than core is not it
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
                 " VALUES ('r-core', ?, 'core', '2026.11', '{}', 1, 'running')", (row.id,))
    assert scorecard.core_complete(conn, row.id) is False
    conn.execute("UPDATE calibration_runs SET status = 'complete' WHERE id = 'r-core'")
    assert scorecard.core_complete(conn, row.id) is True


def test_notices_pass_pending_and_needs_recalibration(conn):
    row = _setup(conn)
    assert scorecard.notices(conn, row.id, "2026.11") == []
    for i in range(20):   # twenty evidence items (one gated job each), the pending threshold
        _job(conn, f"p{i}", row.endpoint_id, row.model_id, finished_at=100.0 + i, gates=CLEAN_GATES)
    assert scorecard.notices(conn, row.id, "2026.11") == ["pass pending"]   # no pass yet
    pass_id = scorecard.run_pass(conn, row.id)
    assert scorecard.notices(conn, row.id, "2026.11") == []
    conn.execute("UPDATE scorecard_rows SET set_version = '2026.10' WHERE id = ?", (row.id,))
    assert scorecard.notices(conn, row.id, "2026.11") == ["needs recalibration"]
    conn.execute("UPDATE scorecard_rows SET set_version = NULL WHERE id = ?", (row.id,))
    consts = json.loads(conn.execute("SELECT constants FROM scorecard_passes WHERE id = ?",
                                     (pass_id,)).fetchone()[0])
    consts["disagreement"] = 0.5
    conn.execute("UPDATE scorecard_passes SET constants = ? WHERE id = ?", (json.dumps(consts), pass_id))
    assert scorecard.notices(conn, row.id, "2026.11") == ["needs recalibration"]
    now = time.time()
    for i in range(20):   # fresh traffic after the pass: pending returns beside the recalibration
        _job(conn, f"l{i}", row.endpoint_id, row.model_id, finished_at=now + 100.0 + i,
             gates=CLEAN_GATES)
    assert scorecard.notices(conn, row.id, "2026.11") == ["pass pending", "needs recalibration"]


def test_pass_pending_counts_evidence_items_not_axes(conn):
    row = _setup(conn)
    axes = {d: 2 for d in ("software_dev", "tool_use", "computer_use", "complexity",
                           "judgement")}
    for i in range(3):   # three verdicts, each determining five axes: three items, not fifteen
        _job(conn, f"v{i}", row.endpoint_id, row.model_id, finished_at=100.0 + i)
        _ticket_verdict(conn, f"v{i}", dict(axes), at=100.0 + i)
    assert scorecard.notices(conn, row.id, "2026.11") == []
    for i in range(3, 20):
        _job(conn, f"v{i}", row.endpoint_id, row.model_id, finished_at=100.0 + i)
        _ticket_verdict(conn, f"v{i}", dict(axes), at=100.0 + i)
    assert scorecard.notices(conn, row.id, "2026.11") == ["pass pending"]
    # probe results count too, without the set's labels: a probe-only row can ask for a pass
    probe_row = _setup(conn, name="probeonly", served_id="pn")
    for i in range(20):
        _probe(conn, probe_row, f"q{i}", "pass", started=100.0 + i)
    assert scorecard.notices(conn, probe_row.id, "2026.11") == ["pass pending"]


def test_set_version_compare_is_numeric(conn):
    row = _setup(conn)
    conn.execute("UPDATE scorecard_rows SET set_version = '2026.9' WHERE id = ?", (row.id,))
    assert scorecard.notices(conn, row.id, "2026.10") == ["needs recalibration"]
    conn.execute("UPDATE scorecard_rows SET set_version = '2026.10' WHERE id = ?", (row.id,))
    assert scorecard.notices(conn, row.id, "2026.9") == []


def test_observations_read_the_three_evidence_sources(conn):
    row = _setup(conn)
    _probe(conn, row, "p1", "pass", seconds=100.0, started=100.0)
    _job(conn, "v1", row.endpoint_id, row.model_id, started_at=100.0, finished_at=200.0)
    _ticket_verdict(conn, "v1", {"tool_use": 2}, accepted=1, dimensions={"tool_use": -1}, at=300.0)
    _job(conn, "g1", row.endpoint_id, row.model_id, finished_at=250.0,
         gates={"tests": True, "stop_rule": None, "not_capped": None,
                "no_tmp": None, "files_within": None, "lint": None, "files_n": None})
    probes = {"p1": {"labels": {"tool_use": 3, "complexity": 3}, "nominal_s": 50.0}}
    obs = sorted(scorecard.observations(conn, row.id, probes), key=lambda o: (o.at, o.dimension))
    assert [(o.dimension, o.at) for o in obs] == [("complexity", 100.0), ("tool_use", 100.0),
                                                  ("completeness", 250.0), ("tool_use", 300.0)]
    probe = obs[1]
    assert (probe.band, probe.accepted, probe.weight, probe.source) == (3, True, 1.0, "probe")
    gate = obs[2]
    assert (gate.band, gate.accepted, gate.weight, gate.source) == (0, True, 0.5, "job")
    verdict = obs[3]   # accepted by the caller, but the dimensions detail flips it
    assert (verdict.band, verdict.accepted, verdict.weight, verdict.source) == (2, False, 0.5, "job")
    scorecard.run_pass(conn, row.id, probes=probes)
    r = next(r for r in scorecard.rows(conn) if r.id == row.id)
    assert r.band_times == {3: 100.0}          # the probe's complexity label and its seconds
    assert r.speed_factor == pytest.approx(2.0)  # 100 s against the 50 s nominal


def test_unreadable_rows_are_skipped_and_counted(conn):
    row = _setup(conn)
    _probe(conn, row, "p", "pass", started=100.0)
    probes = {"p": {"labels": {"complexity": 3}, "nominal_s": 60.0}}
    _job(conn, "bt", row.endpoint_id, row.model_id, finished_at=150.0)
    _ticket_verdict(conn, "bt", {"complexity": 2}, at=300.0)
    conn.execute("UPDATE router_tickets SET scores = '{bad' WHERE job_id = 'bt'")
    _job(conn, "bg", row.endpoint_id, row.model_id, finished_at=200.0)
    conn.execute("UPDATE jobs SET gates = '[oops' WHERE id = 'bg'")
    # the readers skip both bad rows; the good probe still speaks
    assert [(o.dimension, o.source) for o in scorecard.observations(conn, row.id, probes)] \
        == [("complexity", "probe")]
    pass_id = scorecard.run_pass(conn, row.id, probes=probes)
    consts = json.loads(conn.execute("SELECT constants FROM scorecard_passes WHERE id = ?",
                                     (pass_id,)).fetchone()[0])
    assert consts["unreadable"] == 2      # the bad ticket and the bad gates, each counted once
    assert _cell(conn, row.id, "complexity").count == 1.0


def test_notices_never_raise_on_bad_json(conn):
    row = _setup(conn)
    _job(conn, "vt", row.endpoint_id, row.model_id, finished_at=200.0)
    _ticket_verdict(conn, "vt", {"tool_use": 2}, at=300.0)
    # the JSON string "null" is no per-dimension detail, not an unreadable verdict
    conn.execute("UPDATE verdicts SET dimensions = 'null' WHERE job_id = 'vt'")
    _job(conn, "bt", row.endpoint_id, row.model_id, finished_at=250.0)
    _ticket_verdict(conn, "bt", {"complexity": 1}, at=350.0)
    conn.execute("UPDATE router_tickets SET scores = '{bad' WHERE job_id = 'bt'")
    _job(conn, "bg", row.endpoint_id, row.model_id, finished_at=260.0)
    conn.execute("UPDATE jobs SET gates = '[oops' WHERE id = 'bg'")
    assert [(o.dimension, o.accepted) for o in scorecard.observations(conn, row.id)] \
        == [("tool_use", True)]
    assert isinstance(scorecard.notices(conn, row.id, "2026.11"), list)


def test_pass_without_probes_keeps_the_rows_timings(conn):
    row = _setup(conn)
    _probe(conn, row, "p", "pass", seconds=100.0, started=100.0)
    scorecard.run_pass(conn, row.id,
                       probes={"p": {"labels": {"complexity": 3}, "nominal_s": 50.0}})
    r = next(r for r in scorecard.rows(conn) if r.id == row.id)
    assert (r.band_times, r.speed_factor) == ({3: 100.0}, 2.0)
    scorecard.run_pass(conn, row.id)   # no labels in hand: a read that could not see
    r = next(r for r in scorecard.rows(conn) if r.id == row.id)
    assert (r.band_times, r.speed_factor) == ({3: 100.0}, 2.0)


def test_unlabelled_probe_contributes_nothing_and_is_counted(conn):
    row = _setup(conn)
    _probe(conn, row, "mystery", "pass")
    assert scorecard.observations(conn, row.id) == []
    pass_id = scorecard.run_pass(conn, row.id)
    consts = json.loads(conn.execute("SELECT constants FROM scorecard_passes WHERE id = ?",
                                     (pass_id,)).fetchone()[0])
    assert consts["unlabelled_probes"] == 1
    pass_id = scorecard.run_pass(conn, row.id, probes={})
    consts = json.loads(conn.execute("SELECT constants FROM scorecard_passes WHERE id = ?",
                                     (pass_id,)).fetchone()[0])
    assert consts["unlabelled_probes"] == 1


def test_ceiling_lowered_by_a_later_cap(conn):
    a = _setup(conn, name="cap", served_id="ca")
    _job(conn, "a1", a.endpoint_id, a.model_id, finished_at=100.0,
         gates={"tests": None, "stop_rule": None, "not_capped": True,
                "no_tmp": None, "files_within": None, "lint": None, "files_n": 100})
    _job(conn, "a2", a.endpoint_id, a.model_id, finished_at=200.0,
         gates={"tests": None, "stop_rule": None, "not_capped": False,
                "no_tmp": None, "files_within": None, "lint": None, "files_n": 60})
    _job(conn, "a3", a.endpoint_id, a.model_id, finished_at=150.0,   # unknown cap: counts, cannot lower
         gates={"tests": None, "stop_rule": None, "not_capped": None,
                "no_tmp": None, "files_within": None, "lint": None, "files_n": 30})
    scorecard.run_pass(conn, a.id)
    cell = _cell(conn, a.id, "ceiling")
    assert cell.estimate == 60.0                    # the 200 cap lowers the 100 of job 100
    assert (cell.count, cell.thin) == (3.0, True)
    _job(conn, "a4", a.endpoint_id, a.model_id, finished_at=300.0,   # a later success raises it
         gates={"tests": None, "stop_rule": None, "not_capped": True,
                "no_tmp": None, "files_within": None, "lint": None, "files_n": 80})
    scorecard.run_pass(conn, a.id)
    assert _cell(conn, a.id, "ceiling").estimate == 80.0

    b = _setup(conn, name="cap", served_id="cb")
    _job(conn, "b1", b.endpoint_id, b.model_id, finished_at=100.0,
         gates={"not_capped": False, "files_n": 60})
    _job(conn, "b2", b.endpoint_id, b.model_id, finished_at=200.0,
         gates={"not_capped": True, "files_n": 100})
    scorecard.run_pass(conn, b.id)
    assert _cell(conn, b.id, "ceiling").estimate == 100.0   # the cap came before, not since

    c = _setup(conn, name="cap", served_id="cc")
    _job(conn, "c1", c.endpoint_id, c.model_id, gates={"not_capped": False, "files_n": 60})
    scorecard.run_pass(conn, c.id)
    none = _cell(conn, c.id, "ceiling")
    assert none.estimate is None and none.count == 1.0      # no uncapped job: no answer


def test_ceiling_recovers_after_a_cap(conn):
    row = _setup(conn, name="recover", served_id="r")
    def gates(not_capped, files_n):
        return {"tests": None, "stop_rule": None, "not_capped": not_capped,
                "no_tmp": None, "files_within": None, "lint": None, "files_n": files_n}
    _job(conn, "u12", row.endpoint_id, row.model_id, finished_at=50.0, gates=gates(True, 12))
    _job(conn, "cap0", row.endpoint_id, row.model_id, finished_at=60.0,
         gates=gates(False, 0))          # a timeout that committed nothing
    for i in range(10):
        _job(conn, f"u8{i}", row.endpoint_id, row.model_id, finished_at=100.0 + i,
             gates=gates(True, 8))
    scorecard.run_pass(conn, row.id)
    cell = _cell(conn, row.id, "ceiling")
    assert cell.estimate == 12.0         # the 0-file cap constrains nothing; the 12 stands
    assert cell.count == 12.0            # every job carrying files_n, capped or not


def test_rate_cell_ignores_null_gates(conn):
    row = _setup(conn)
    _job(conn, "n1", row.endpoint_id, row.model_id,
         gates={"tests": None, "stop_rule": None, "not_capped": None,
                "no_tmp": None, "files_within": None, "lint": None, "files_n": None})
    _job(conn, "n2", row.endpoint_id, row.model_id,
         gates={"tests": None, "stop_rule": None, "not_capped": None,
                "no_tmp": True, "files_within": None, "lint": None, "files_n": None})
    # n1: no evaluable gate in either group, so no observation; n2: adherence only
    assert [(o.dimension, o.accepted) for o in scorecard.observations(conn, row.id)] \
        == [("adherence", True)]


def test_a_probe_named_with_empty_labels_is_neither_observed_nor_unlabelled_nor_timed(conn):
    """The facts probe (kind: facts) reaches the pass as ``{"labels": {}, "nominal_s": ...}``:
    it makes no observation, it is not counted unlabelled (only an id ABSENT from ``probes``
    is), and its five-job seconds never enter the speed factor."""
    row = _setup(conn)
    _probe(conn, row, "facts-thinking", "pass", seconds=300.0)
    _probe(conn, row, "real-probe", "pass", seconds=20.0)
    _probe(conn, row, "unknown-probe", "pass", seconds=20.0)
    probes = {"facts-thinking": {"labels": {}, "nominal_s": 60.0},
              "real-probe": {"labels": {"complexity": 1, "language": "python"}, "nominal_s": 10.0}}
    pass_id = scorecard.run_pass(conn, row.id, probes=probes)
    consts = json.loads(conn.execute("SELECT constants FROM scorecard_passes WHERE id = ?",
                                     (pass_id,)).fetchone()["constants"])
    assert consts["unlabelled_probes"] == 1  # unknown-probe only
    assert scorecard.rows(conn)[0].speed_factor == 2.0  # 20 / 10; the facts 300 / 60 excluded
