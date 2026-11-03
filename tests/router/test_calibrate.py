"""router 9a (router-design spec §10, calibration-set spec §5-§11): the calibration runner,
the manifest loader, ``localagents calibrate`` and the router's ``calibrate``/``scorecard``
tools.

Nothing here runs a model or dispatches a job: ``run_probe_job`` is the module seam the run
tests replace, the one real-seam test patches ``server.start_job`` with a fake job, and the
checker proofs run the fixtures' shell commands on the stub and the reference solution."""

import asyncio
import dataclasses
import inspect
import json
import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from localagents import db, jobs
from localagents.registry import Defaults, Endpoint, Registry
from localagents.router import calibrate, capacity, scorecard
from localagents.router import mcp as router

SET_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "router" / "probeset" / "set.yaml"


# --- helpers -----------------------------------------------------------------------------------


class FakeRegistry:
    """The runner only reads ``endpoints``, ``defaults`` and awaits ``probe``."""

    def __init__(self, endpoints, probe=None):
        self.endpoints = endpoints
        self.defaults = Defaults()
        self._probe = {"up": True, "models": ["served"]} if probe is None else probe

    async def probe(self, ep):
        return self._probe


def _endpoint(name="ep", backend="llama.cpp", **kw):
    return Endpoint(name=name, base_url=f"http://{name}:1/v1", backend=backend, host="local", **kw)


def _reg(probe=None):
    return FakeRegistry({"ep": _endpoint()}, probe)


def _store(conn):
    """A JobStore whose job map the tests fill and whose reads use the fixture's connection."""
    st = jobs.JobStore()
    st._conn = conn
    st._server_id = "test-server"
    return st


def _ids(conn, name="ep", served="served"):
    eid = db.lookup_id(conn, "endpoints", name=name, base_url=f"http://{name}:1/v1",
                       backend="llama.cpp", host="local")
    mid = db.lookup_id(conn, "models", served_id=served)
    return eid, mid


def _row(conn, name="ep", served="served"):
    eid, mid = _ids(conn, name, served)
    return scorecard.get_or_create_row(conn, name, served, eid, mid)


def _save_capacity(conn, name="ep"):
    eid, _ = _ids(conn, name)
    capacity.save_capacity(conn, eid, "unknown", capacity.Curve({1: 1.0, 2: 0.8}, "probed", 2))
    return eid


def _fake_job(store, jid, transcript, tmp_path, *, status="succeeded",
              started=1.0, finished=3.0, num_turns=1):
    log_dir = tmp_path / "logs" / jid
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "transcript.txt").write_text(transcript)
    job = jobs.Job(id=jid, task="t", model="served", endpoint="ep",
                   base_url="http://ep:1/v1", cwd=str(log_dir), run_cwd=str(log_dir),
                   isolation="none", status=status, started_at=started, finished_at=finished,
                   num_turns=num_turns)
    job.log_dir = str(log_dir)
    store.jobs[jid] = job
    if getattr(store, "_conn", None) is not None:
        # a calibration_results row references its job (the real job row exists in the database)
        store._conn.execute("INSERT OR REPLACE INTO jobs (id, status, created_at)"
                            " VALUES (?, 'succeeded', ?)", (jid, time.time()))
    return job


def _outcome(status="pass", seconds=10.0, reason=None, job_id=None):
    return calibrate.ProbeOutcome(status, seconds, reason, job_id)


class FakeSeam:
    """The replaced ``run_probe_job``: canned outcomes, every call recorded."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    async def __call__(self, reg, store, conn, ep, served_id, probe, run_dir, timeout_s,
                       levers=None):
        self.calls.append({"id": probe.id, "levers": dict(levers or {}), "timeout_s": timeout_s})
        out = self.outcomes[probe.id]
        return out(levers or {}) if callable(out) else out


@pytest.fixture
def probeset():
    return calibrate.load_set(SET_PATH)


@pytest.fixture
def probes(probeset):
    return {p.id: p for p in probeset.probes}


@pytest.fixture
def seam(monkeypatch):
    def install(outcomes):
        fake = FakeSeam(outcomes)
        monkeypatch.setattr(calibrate, "run_probe_job", fake)
        return fake
    return install


@pytest.fixture
def installed(monkeypatch):
    def install(*probe_list, version="2026.11", reference_tps=None):
        s = calibrate.ProbeSet(version, reference_tps, list(probe_list), SET_PATH)
        monkeypatch.setattr(calibrate, "load_set", lambda path=None: s)
        return s
    return install


def _run(conn, *, tier="core", store=None, reg=None, **kw):
    return asyncio.run(calibrate.run(reg or _reg(), store or _store(conn), conn, "ep",
                                     tier=tier, report=lambda line: None, **kw))


def _run_status(conn, run_id):
    r = conn.execute("SELECT status FROM calibration_runs WHERE id = ?", (run_id,)).fetchone()
    return r["status"]


# --- the loader (spec §8, §11) -------------------------------------------------------------------


def test_load_builtin_core_set_validates_every_probe():
    """The shipped core.yaml loads; every probe it lists has its fixture directory. At this
    base the file lists no probes (the fixture stories ship them), so the loop is vacuous."""
    s = calibrate.load_set()
    assert isinstance(s.version, str) and s.version
    for probe in s.probes:
        assert probe.fixture_dir.is_dir(), probe.id
        assert probe.brief_text()


def test_loader_refuses_a_missing_field_naming_the_probe(tmp_path):
    data = yaml.safe_load(SET_PATH.read_text())
    data["probes"] = [data["probes"][0]]
    del data["probes"][0]["checker"]
    path = tmp_path / "set.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError) as exc:
        calibrate.load_set(path)
    assert "alpha" in str(exc.value) and "checker" in str(exc.value)


def test_loader_refuses_a_wrong_checker_proof(tmp_path):
    data = yaml.safe_load(SET_PATH.read_text())
    data["probes"] = [data["probes"][0]]
    data["probes"][0]["checker_proof"] = {"stub": "pass", "reference": "pass"}
    path = tmp_path / "set.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError) as exc:
        calibrate.load_set(path)
    assert "alpha" in str(exc.value) and "checker_proof" in str(exc.value)


def test_loader_refuses_a_duplicate_id_and_a_missing_fixture(tmp_path):
    data = yaml.safe_load(SET_PATH.read_text())
    data["probes"] = [data["probes"][0], data["probes"][0]]
    path = tmp_path / "set.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError) as exc:
        calibrate.load_set(path)
    assert "alpha" in str(exc.value)
    data["probes"] = [data["probes"][0]]
    data["probes"][0]["id"] = "no-such-fixture"
    data["probes"][0]["brief"] = "fixtures/no-such-fixture/brief.md"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError) as exc:
        calibrate.load_set(path)
    assert "no-such-fixture" in str(exc.value)


def test_labels_carry_facts_probes_with_empty_labels(probeset):
    """The facts probe rides in ``labels()`` with empty labels and its nominal_s (final review,
    layout): a known probe whose output is the row's facts rather than a cell earns no
    observations, and naming it keeps its timing able to speak."""
    labels = probeset.labels()
    assert set(labels) == {"alpha", "beta", "facts"}
    assert labels["facts"] == {"labels": {}, "nominal_s": 30.0}
    assert labels["alpha"]["labels"]["language"] == "python"
    assert labels["alpha"]["nominal_s"] == 60
    assert "language" in labels["beta"]["labels"]


# --- plan and estimate (spec §10 "Time, honestly") ---------------------------------------------


def test_plan_skips_probes_whose_tools_are_absent(probes, probeset):
    needs_tool = dataclasses.replace(probes["beta"], id="beta-tool",
                                     needs=["localagents-no-such-tool"])
    s = calibrate.ProbeSet("2026.11", None, [probes["alpha"], needs_tool], SET_PATH)
    p = calibrate.plan(s, "core", calibrate.present_tools(s))
    assert [pr.id for pr in p.probes] == ["alpha"]
    assert [(pr.id, why) for pr, why in p.skipped] == [
        ("beta-tool", "missing tool: localagents-no-such-tool")]
    assert "stubmcp" in calibrate.present_tools(s)


def test_git_is_an_implicit_need(monkeypatch, probeset):
    """Every tree is built with git, so a box without it skips the whole tier with that
    reason (final review M5) rather than planning probes whose materialise would die."""
    monkeypatch.setattr(calibrate.shutil, "which", lambda name: None)
    tools = calibrate.present_tools(probeset)
    assert tools == {"stubmcp"}  # the set's own stub; git, like every real tool, is absent
    p = calibrate.plan(probeset, "core", tools)
    assert p.probes == []
    assert p.skipped  # every core probe, no exception
    assert all(why == "missing tool: git" for _pr, why in p.skipped)
    core = {pr.id for pr in probeset.probes if pr.tier == "core"}
    assert {pr.id for pr, _why in p.skipped} == core
    # with git on the box the implicit need costs nothing
    p2 = calibrate.plan(probeset, "core", tools | {"git"})
    assert {pr.id for pr in p2.probes} == core
    assert p2.skipped == []


def test_estimate_scales_by_speed_factor_with_margin(probes):
    s = calibrate.ProbeSet("2026.11", None, [probes["alpha"], probes["beta"]], SET_PATH)
    p = calibrate.plan(s, "core", calibrate.present_tools(s))
    low, high = calibrate.estimate_s(p, 2.0, margin=0.5)
    assert low == 180.0 and high == 270.0


# --- materialize and check (spec §5; the fixture contract) ---------------------------------------


def test_materialize_commits_the_fixture_and_runs_setup(probes, tmp_path):
    root = calibrate.materialize(probes["alpha"], tmp_path / "m")
    assert (root / "README.md").is_file()
    assert (root / "setup-ran.txt").read_text().strip() == "ran"
    subject = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%s"],
                             capture_output=True, text=True, check=True).stdout.strip()
    assert subject == "fixture"
    branch = subprocess.run(["git", "-C", str(root), "symbolic-ref", "--short", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert branch == "main"  # the probe repo's branch, never the machine's default
    base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert base == calibrate._git_head(root)


def test_materialize_refuses_a_non_empty_dest(probes, tmp_path):
    """No caller inherits a tree by accident: a retried probe cannot materialise into the
    previous attempt's leftovers, clean (git commit would die) or dirty (the leftovers would
    become the fixture base)."""
    root = calibrate.materialize(probes["alpha"], tmp_path / "m")
    with pytest.raises(ValueError) as exc:
        calibrate.materialize(probes["alpha"], root)
    assert "m" in str(exc.value)
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "file.txt").write_text("leftover\n")
    with pytest.raises(ValueError) as exc:
        calibrate.materialize(probes["alpha"], stray)
    assert "stray" in str(exc.value)
    assert (stray / "file.txt").read_text() == "leftover\n"  # the refusal changes nothing


def test_check_runs_the_command_with_the_probe_dir_env(probes, tmp_path):
    root = calibrate.materialize(probes["alpha"], tmp_path / "c")
    ok, reason = calibrate.check(probes["alpha"], root)
    assert ok is False and reason
    shutil.copytree(probes["alpha"].fixture_dir / "reference", root, dirs_exist_ok=True)
    assert calibrate.check(probes["alpha"], root) == (True, None)
    env_probe = dataclasses.replace(
        probes["alpha"], checker='test -f "$LOCALAGENTS_PROBE_DIR/brief.md"')
    assert calibrate.check(env_probe, root) == (True, None)


def test_no_answer_prefix_is_the_reason(probes, tmp_path):
    # checkers add context to their no_answer ("no_answer: the brief named no file"); the
    # reason stays the marker, which is what the scorecard aggregates over
    prefixed = dataclasses.replace(
        probes["alpha"], checker="echo 'no_answer: the brief named no file'; exit 1")
    assert calibrate.check(prefixed, tmp_path) == (False, "no_answer")
    exact = dataclasses.replace(probes["alpha"], checker="echo 'no_answer'; exit 1")
    assert calibrate.check(exact, tmp_path) == (False, "no_answer")
    other = dataclasses.replace(probes["alpha"],
                                checker="echo 'checker: input missing'; exit 1")
    assert calibrate.check(other, tmp_path) == (False, "checker: input missing")


def test_relative_set_path_is_resolved(tmp_path, monkeypatch):
    # a --set path is relative to the shell's directory, and the run dispatches with cwd
    # inside the attempt directory: the loader pins the manifest and everything derived
    # from it to absolute at load time, or a mid-run cwd change loses the fixture
    monkeypatch.chdir(tmp_path)
    rel = os.path.relpath(SET_PATH, tmp_path)
    assert not os.path.isabs(rel)
    s = calibrate.load_set(rel)
    assert s.path.is_absolute()
    for probe in s.probes:
        assert probe.fixture_dir.is_absolute() and probe.brief_path.is_absolute()
    # and from this unrelated directory the set still materializes and checks
    root = calibrate.materialize(s.probes[0], tmp_path / "rel")
    ok, reason = calibrate.check(s.probes[0], root)
    assert ok is False and reason


# --- throughput probe (spec §7, §10) -------------------------------------------------------------


def test_throughput_probe_refuses_a_busy_endpoint(conn, monkeypatch):
    ep = _endpoint()
    for busy in ({"load": {"requests_running": 1}}, {"slots": {"busy": 1}}):
        reg = FakeRegistry({"ep": ep}, probe={"up": True, **busy})
        with pytest.raises(calibrate.EndpointBusy):
            asyncio.run(calibrate.throughput_probe(reg, ep, "served", conn))


def test_throughput_probe_measures_and_saves(conn, monkeypatch):
    ep = _endpoint()
    reg = FakeRegistry({"ep": ep}, probe={"up": True})
    seen = []
    answers = [(32, 0.5), (2000, 2.0), (2000, 2.5), (2000, 2.5)]

    async def completion(ep_, served_id, max_tokens):
        seen.append(max_tokens)
        return answers.pop(0)

    monkeypatch.setattr(calibrate, "_completion", completion)
    curve, tps = asyncio.run(calibrate.throughput_probe(reg, ep, "served", conn))
    assert seen == [32, 2000, 2000, 2000]  # warm-up discarded, then the single, then the pair
    assert tps == 1000.0  # the single is measured warm, never on the discarded first call
    assert curve.points == {1: 1.0, 2: 0.8}
    assert curve.state == "probed" and curve.n == 2
    eid, _ = _ids(conn)
    saved = capacity.load_capacity(conn, eid)
    assert saved.points == {1: 1.0, 2: 0.8} and saved.state == "probed"


def test_throughput_probe_discards_a_warm_up_and_reads_per_session(conn, monkeypatch):
    # the warm-up's own 3.2 tok/s is the cold artifact the single must not inherit: measured,
    # it would drag the baseline and push this curve point to 125. The pair's two sessions run
    # at 500 and 250 tok/s; point 2 is their per-session median over the single's 200, and it
    # is above 1.0 and stays exactly that: the probe records the box, it does not clamp it.
    ep = _endpoint()
    reg = FakeRegistry({"ep": ep}, probe={"up": True})
    answers = [(32, 10.0), (2000, 10.0), (2000, 4.0), (2000, 8.0)]

    async def completion(ep_, served_id, max_tokens):
        return answers.pop(0)

    monkeypatch.setattr(calibrate, "_completion", completion)
    curve, tps = asyncio.run(calibrate.throughput_probe(reg, ep, "served", conn))
    assert tps == 200.0  # the single after the warm-up, not the warm-up itself
    assert curve.points[2] == 1.875  # median(500, 250) / 200, never the batched (4000 / 8) / 2
    assert curve.points[2] > 1.0  # a point above 1.0 is recorded, not clamped to the plausible


# --- run: estimate, resume, completion, stopping --------------------------------------------------


def test_run_estimate_only_runs_nothing(conn, probes, installed, seam):
    _row(conn)
    eid = _save_capacity(conn)
    installed(probes["alpha"])
    fake = seam({"alpha": _outcome()})
    lines = []
    result = asyncio.run(calibrate.run(_reg(), _store(conn), conn, "ep", estimate_only=True,
                                       report=lines.append))
    assert result == "estimate"
    assert fake.calls == []
    assert conn.execute("SELECT COUNT(*) FROM calibration_runs").fetchone()[0] == 0
    assert any("estimate:" in line for line in lines)
    assert not any("check the box" in line for line in lines)  # the saved curve is at 0.8
    assert capacity.load_capacity(conn, eid) is not None  # the throughput reading stays


def test_estimate_line_flags_a_curve_point_above_one(conn, probes, installed, seam):
    # the probe never clamps the point, but a 2-session curve above 1.0 usually says the box
    # was not quiet while it was probed; the estimate line carries that warning onward
    _row(conn)
    eid, _ = _ids(conn)
    capacity.save_capacity(conn, eid, "unknown", capacity.Curve({1: 1.0, 2: 1.4}, "probed", 2))
    installed(probes["alpha"])
    seam({"alpha": _outcome()})
    lines = []
    result = asyncio.run(calibrate.run(_reg(), _store(conn), conn, "ep", estimate_only=True,
                                       report=lines.append))
    assert result == "estimate"
    assert any(line.endswith("(curve point above 1.0 at 2 sessions: check the box)")
               for line in lines)


def test_run_refuses_a_down_model(conn, capsys):
    reg = FakeRegistry({"ep": _endpoint()}, probe={"up": False, "models": []})
    lines = []
    result = asyncio.run(calibrate.run(reg, _store(conn), conn, "ep", report=lines.append))
    assert result == "model_unavailable"
    # a refusal is not a report line: it goes to stderr, so the operator sees why nothing
    # ran even when the report lines are piped somewhere else
    assert lines == []
    assert "model_unavailable" in capsys.readouterr().err


def test_empty_tier_is_refused_before_running(conn, probes, installed, monkeypatch, capsys):
    """A tier with nothing to run has measured nothing: the refusal is no_probes (the CLI's
    exit-2 family) on stderr, and it happens before anything runs or writes -- no throughput
    probe, no scorecard row, no run row that could never complete (final review M6)."""
    installed(probes["alpha"])  # the set has one core probe and nothing under "large"
    seen = []

    async def no_throughput(*a):
        seen.append(a)
        raise AssertionError("the throughput probe ran for an empty tier")

    monkeypatch.setattr(calibrate, "throughput_probe", no_throughput)
    lines = []
    result = asyncio.run(calibrate.run(_reg(), _store(conn), conn, "ep", tier="large",
                                       report=lines.append))
    assert result == "no_probes"
    assert seen == [] and lines == []  # a refusal is stderr, not the piped report
    assert "no_probes" in capsys.readouterr().err
    assert conn.execute("SELECT COUNT(*) FROM calibration_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM scorecard_rows").fetchone()[0] == 0


def test_run_resumes_retrying_skipped_and_unreliable(conn, probes, installed, seam):
    row = _row(conn)
    _save_capacity(conn)
    installed(probes["alpha"], probes["beta"])
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
                 " VALUES ('run1', ?, 'core', '2026.11', '{}', ?, 'running')", (row.id, time.time()))
    conn.execute("INSERT INTO calibration_results (run_id, probe_id, job_id, status, seconds)"
                 " VALUES ('run1', 'alpha', NULL, 'pass', 10.0)")
    conn.execute("INSERT INTO calibration_results (run_id, probe_id, job_id, status, seconds, reason)"
                 " VALUES ('run1', 'beta', NULL, 'unreliable', 5.0, 'foreign load')")
    fake = seam({"alpha": _outcome(seconds=12.0), "beta": _outcome(seconds=7.0)})
    run_id = _run(conn, set_path=SET_PATH)
    assert run_id == "run1"
    assert [c["id"] for c in fake.calls] == ["beta"]  # the pass and the unreliable row above
    alpha = conn.execute("SELECT status, seconds FROM calibration_results"
                         " WHERE run_id = 'run1' AND probe_id = 'alpha'").fetchone()
    beta = conn.execute("SELECT status FROM calibration_results"
                        " WHERE run_id = 'run1' AND probe_id = 'beta'").fetchone()
    assert alpha["status"] == "pass" and alpha["seconds"] == 10.0
    assert beta["status"] == "pass"
    assert _run_status(conn, run_id) == "complete"


def test_run_complete_needs_two_thirds_results(conn, probes, installed, seam):
    _row(conn)
    _save_capacity(conn)
    installed(probes["alpha"], probes["beta"])
    seam({"alpha": _outcome(), "beta": _outcome("skipped", 0.0, "missing tool")})
    run_id = _run(conn)
    assert _run_status(conn, run_id) == "incomplete"


def test_unreliable_probe_is_rerun_once_and_never_counts_as_complete(conn, probes, installed,
                                                                     seam):
    # spec §10 "Reliable": an unreliable probe is rerun once in the same invocation (a fresh
    # attempt), and a status that is not a verdict keeps the run incomplete until it has one
    _row(conn)
    _save_capacity(conn)
    installed(probes["alpha"], probes["beta"])
    counts = {"alpha": 0, "beta": 0}

    def unreliable(probe_id):
        def out(levers):
            counts[probe_id] += 1
            return _outcome("unreliable", 5.0, "foreign load")
        return out

    fake = seam({pid: unreliable(pid) for pid in counts})
    run_id = _run(conn)
    assert _run_status(conn, run_id) == "incomplete"
    assert [c["id"] for c in fake.calls] == ["alpha", "beta", "alpha", "beta"]  # never a 3rd
    statuses = {r["probe_id"]: r["status"] for r in conn.execute(
        "SELECT probe_id, status FROM calibration_results WHERE run_id = ?", (run_id,))}
    assert statuses == {"alpha": "unreliable", "beta": "unreliable"}

    # a resume whose leftover unreliable probe STAYS unreliable: its one rerun is the main
    # loop's; the post-loop rerun must not dispatch it a second time (the wave's re-review)
    fake.outcomes["alpha"] = _outcome()
    assert _run(conn) == run_id
    assert _run_status(conn, run_id) == "incomplete"
    assert [c["id"] for c in fake.calls][4:] == ["alpha", "beta"]  # beta once, not twice

    fake.outcomes["beta"] = _outcome()
    assert _run(conn) == run_id  # resume: the unreliable row goes through the main loop again
    assert _run_status(conn, run_id) == "complete"
    assert len(fake.calls) == 7  # this resume's one run; no further rerun of a verdict


def _open_running_run(conn):
    """A ``running`` run row a fresh launch would resume and its run directory (the lock
    lives at ``<run_dir>/lock``); returns the directory."""
    row = _row(conn)
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started,"
                 " status) VALUES ('run-held', ?, 'core', '2026.11', '{}', ?, 'running')",
                 (row.id, time.time()))
    run_dir = calibrate.state_dir() / "calibration" / "run-held"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def test_second_launch_refuses_while_the_first_runs(conn, probes, installed, seam):
    # a `running` run row whose lock names a live pid is being driven by another process
    _save_capacity(conn)
    installed(probes["alpha"])
    fake = seam({"alpha": _outcome()})
    run_dir = _open_running_run(conn)
    (run_dir / "lock").write_text(f"{os.getpid()} {time.time()}\n")  # this test process: alive
    result = _run(conn)
    assert result == "run_in_progress"
    assert fake.calls == []  # never a second dispatcher on one run
    assert conn.execute("SELECT COUNT(*) FROM calibration_results").fetchone()[0] == 0
    assert (run_dir / "lock").read_text().split()[0] == str(os.getpid())  # the holder's stays


def test_stale_lock_resumes(conn, probes, installed, seam):
    # a lock whose pid is gone is a crashed holder: the resume takes over, works, releases
    _save_capacity(conn)
    installed(probes["alpha"])
    seam({"alpha": _outcome()})
    run_dir = _open_running_run(conn)
    dead = 2 ** 20
    while True:
        try:
            os.kill(dead, 0)
        except ProcessLookupError:
            break
        dead += 1
    (run_dir / "lock").write_text(f"{dead} 1.0\n")
    run_id = _run(conn)
    assert run_id == "run-held"
    assert _run_status(conn, run_id) == "complete"
    assert not (run_dir / "lock").exists()  # released when the run ends


def _four_probes(probes):
    return [dataclasses.replace(probes["alpha"], id=f"p{i}", nominal_s=60.0) for i in range(4)]


def test_run_stops_when_actuals_exceed_twice_the_estimate(conn, probes, monkeypatch):
    _row(conn)
    _save_capacity(conn)
    four = _four_probes(probes)
    s = calibrate.ProbeSet("2026.11", None, four, SET_PATH)
    monkeypatch.setattr(calibrate, "load_set", lambda path=None: s)
    fake = FakeSeam({p.id: _outcome(seconds=500.0) for p in four})
    monkeypatch.setattr(calibrate, "run_probe_job", fake)
    run_id = _run(conn)
    assert _run_status(conn, run_id) == "stopped"
    assert [c["id"] for c in fake.calls] == ["p0", "p1", "p2"]  # stopped after the third
    assert conn.execute("SELECT COUNT(*) FROM calibration_results WHERE run_id = ?",
                        (run_id,)).fetchone()[0] == 3


def test_revised_estimate_excludes_facts(conn, probes, installed):
    """The facts row is five jobs' seconds summed against one nominal_s: it enters neither
    the revised estimate's elapsed, its median ratio nor its remaining projection (M9). Here
    the facts pass at 900s must leave the answer where alpha (120s actual, 60s nominal) and
    beta (30s nominal, undecided) alone put it."""
    row = _row(conn)
    s = installed(probes["alpha"], probes["beta"], probes["facts"])
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
                 " VALUES ('rx', ?, 'core', '2026.11', '{}', ?, 'running')", (row.id, time.time()))
    for pid, secs in (("alpha", 120.0), ("facts", 900.0)):
        conn.execute("INSERT INTO calibration_results (run_id, probe_id, status, seconds)"
                     " VALUES ('rx', ?, 'pass', ?)", (pid, secs))
    plan_obj = calibrate.plan(s, "core", {"stubmcp", "git"})
    # 120 elapsed + beta's 30 nominal * median(120 / 60) = 180; with facts in the figure the
    # median ratio alone would be 16.0
    assert calibrate._revised_estimate(conn, "rx", plan_obj, 1.0) == 180.0


def test_resumed_run_estimates_the_remaining_probes(conn, probes, monkeypatch):
    """A resume has already paid for its finished probes: the estimate line names the run
    and counts what is left, and the twice rule budgets against that remaining estimate, not
    the original whole-plan total (M10). Three 150s results against 180s of remaining
    nominal stop the run; the 300s original total would have let it run on."""
    row = _row(conn)
    _save_capacity(conn)
    five = [dataclasses.replace(probes["alpha"], id=f"p{i}") for i in range(5)]  # nominal 60 each
    s = calibrate.ProbeSet("2026.11", None, five, SET_PATH)
    monkeypatch.setattr(calibrate, "load_set", lambda path=None: s)
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
                 " VALUES ('r', ?, 'core', '2026.11', '{}', ?, 'running')", (row.id, time.time()))
    for pid in ("p0", "p1"):
        conn.execute("INSERT INTO calibration_results (run_id, probe_id, status, seconds)"
                     " VALUES ('r', ?, 'pass', 60.0)", (pid,))
    fake = FakeSeam({f"p{i}": _outcome(seconds=150.0) for i in (2, 3, 4)})
    monkeypatch.setattr(calibrate, "run_probe_job", fake)
    lines = []
    run_id = asyncio.run(calibrate.run(_reg(), _store(conn), conn, "ep", report=lines.append))
    assert run_id == "r"
    est = next(ln for ln in lines if ln.startswith("estimate:"))
    assert "resuming run r: 3 of 5 probes left" in est
    assert "~3.0-4.5 min" in est  # the three left at factor 1.0, not all five
    assert "stopped: actuals exceed twice the estimate (rerun with --keep-going to continue)" in lines
    assert _run_status(conn, run_id) == "stopped"
    assert [c["id"] for c in fake.calls] == ["p2", "p3", "p4"]


def test_run_keeps_going_when_asked(conn, probes, monkeypatch):
    _row(conn)
    _save_capacity(conn)
    four = _four_probes(probes)
    s = calibrate.ProbeSet("2026.11", None, four, SET_PATH)
    monkeypatch.setattr(calibrate, "load_set", lambda path=None: s)
    fake = FakeSeam({p.id: _outcome(seconds=500.0) for p in four})
    monkeypatch.setattr(calibrate, "run_probe_job", fake)
    run_id = _run(conn, keep_going=True)
    assert _run_status(conn, run_id) == "complete"
    assert len(fake.calls) == 4


def test_cap_stops_the_tier(conn, probes, installed, seam):
    _row(conn)
    _save_capacity(conn)
    installed(probes["alpha"], probes["beta"], probes["facts"])
    fake = seam({"alpha": _outcome(), "beta": _outcome("fail", 3.0, "tail_capped"),
                 "facts": _outcome()})
    run_id = _run(conn)
    assert _run_status(conn, run_id) == "stopped"
    assert [c["id"] for c in fake.calls] == ["alpha", "beta"]


def test_complete_run_writes_set_version_speed_factor_and_runs_a_pass(conn, probes, installed, seam):
    row = _row(conn)
    _save_capacity(conn)
    gamma = dataclasses.replace(probes["alpha"], id="gamma", nominal_s=60.0)
    beta = dataclasses.replace(probes["beta"], nominal_s=60.0)
    installed(probes["alpha"], beta, gamma)
    seam({"alpha": _outcome(seconds=120.0), "beta": _outcome(seconds=60.0),
          "gamma": _outcome(seconds=180.0)})
    lines = []
    run_id = asyncio.run(calibrate.run(_reg(), _store(conn), conn, "ep", report=lines.append))
    assert _run_status(conn, run_id) == "complete"
    refreshed = scorecard.get_or_create_row(conn, "ep", "served", row.endpoint_id, row.model_id)
    assert refreshed.set_version == "2026.11"
    assert refreshed.speed_factor == 2.0  # median(120/60, 60/60, 180/60)
    assert conn.execute("SELECT COUNT(*) FROM scorecard_passes WHERE row_id = ?",
                        (row.id,)).fetchone()[0] == 1
    assert scorecard.cells(conn, row.id)
    assert any("speed factor" in line for line in lines)


def test_reset_archives_the_row_first(conn, probes, installed, seam):
    old = _row(conn)
    _save_capacity(conn)
    installed(probes["alpha"])
    seam({"alpha": _outcome()})
    run_id = _run(conn, reset=True)
    archived = conn.execute("SELECT archived, archive_reason FROM scorecard_rows WHERE id = ?",
                            (old.id,)).fetchone()
    assert archived["archived"] is not None and archived["archive_reason"] == "calibrate --reset"
    live = scorecard.rows(conn)
    assert len(live) == 1 and live[0].id != old.id
    assert live[0].set_version == "2026.11"
    assert _run_status(conn, run_id) == "complete"


def test_reset_with_estimate_only_or_busy_leaves_the_row(conn, probes, installed, seam,
                                                         monkeypatch):
    # an estimate or a refusal is no reason to lose a calibration: the archive and the fresh
    # row happen only once the estimate and the busy check are past and a run opens
    old = _row(conn)
    _save_capacity(conn)
    installed(probes["alpha"])
    fake = seam({"alpha": _outcome()})
    result = _run(conn, reset=True, estimate_only=True)
    assert result == "estimate" and fake.calls == []
    archived = conn.execute("SELECT archived FROM scorecard_rows WHERE id = ?",
                            (old.id,)).fetchone()
    assert archived is not None and archived["archived"] is None
    assert len(scorecard.rows(conn)) == 1  # no fresh row opened for an estimate

    async def busy(*a, **k):
        raise calibrate.EndpointBusy("endpoint 'ep' is serving requests; run when quiet")

    monkeypatch.setattr(calibrate, "throughput_probe", busy)
    monkeypatch.setattr(capacity, "load_capacity", lambda conn, eid: None)
    result = _run(conn, reset=True)
    assert result == "endpoint_busy" and fake.calls == []
    archived = conn.execute("SELECT archived FROM scorecard_rows WHERE id = ?",
                            (old.id,)).fetchone()
    assert archived["archived"] is None and len(scorecard.rows(conn)) == 1
    assert conn.execute("SELECT COUNT(*) FROM calibration_runs").fetchone()[0] == 0


# --- the facts probe (spec §6) -------------------------------------------------------------------


def test_facts_probe_runs_five_ways_and_writes_row_facts(conn, probes, installed, monkeypatch,
                                                         tmp_path):
    row = _row(conn)
    _save_capacity(conn)
    installed(probes["facts"])
    store = _store(conn)
    texts = {
        "defaults": "[thinking] short\n[assistant] x",
        "thinking_on": "[thinking] " + "a" * 50,
        "thinking_off": "[assistant] x",
        "effort_low": "[thinking] " + "b" * 20,
        "effort_high": "[thinking] " + "c" * 60,
    }
    seen = []

    def lever_name(levers):
        if not levers:
            return "defaults"
        if levers.get("thinking") is True:
            return "thinking_on"
        if levers.get("thinking") is False:
            return "thinking_off"
        return "effort_low" if levers.get("effort") == "low" else "effort_high"

    async def fake(reg, store_, conn_, ep, served_id, probe, run_dir, timeout_s, levers=None):
        name = lever_name(levers or {})
        seen.append(name)
        jid = f"facts-{name}"
        _fake_job(store_, jid, texts[name], tmp_path)
        return _outcome("pass", 1.0, None, jid)

    monkeypatch.setattr(calibrate, "run_probe_job", fake)
    run_id = _run(conn)
    assert seen == ["defaults", "thinking_on", "thinking_off", "effort_low", "effort_high"]
    refreshed = scorecard.get_or_create_row(conn, "ep", "served", row.endpoint_id, row.model_id)
    assert refreshed.facts == {"default_thinking": "on", "thinking_switch": "honoured",
                               "effort": "honoured"}
    result = conn.execute("SELECT status FROM calibration_results WHERE run_id = ? AND probe_id = ?",
                          (run_id, "facts")).fetchone()
    assert result["status"] == "pass"
    assert _run_status(conn, run_id) == "complete"


def test_capped_facts_lever_fails_facts_without_stopping_the_tier(conn, probes, installed,
                                                                  monkeypatch, tmp_path):
    """A capped lever of the facts probe fails the row with reason tail_capped, but the facts
    probe is not a capability probe: the tier continues (§10's cap rule is about ceilings)."""
    _row(conn)
    _save_capacity(conn)
    installed(probes["facts"], probes["alpha"], probes["beta"])
    store = _store(conn)
    called = []

    async def fake(reg, store_, conn_, ep, served_id, probe, run_dir, timeout_s, levers=None):
        if probe.kind != "facts":
            called.append(probe.id)
            return _outcome("pass", 1.0)
        if levers and levers.get("effort") == "high":  # the capped lever
            jid = "cap-job"
            _fake_job(store_, jid, "[thinking] x", tmp_path)
            called.append("capped-lever")
            return _outcome("fail", 1.0, "tail_capped", jid)
        jid = f"facts-{len(called)}"
        _fake_job(store_, jid, "[assistant] x", tmp_path)
        return _outcome("pass", 1.0, None, jid)

    monkeypatch.setattr(calibrate, "run_probe_job", fake)
    run_id = _run(conn, store=store)
    result = conn.execute("SELECT status, reason FROM calibration_results WHERE run_id = ?"
                          " AND probe_id = 'facts'", (run_id,)).fetchone()
    assert result["status"] == "fail" and result["reason"] == "tail_capped"
    assert [c for c in called if c in ("alpha", "beta")] == ["alpha", "beta"]  # the tier ran on
    assert _run_status(conn, run_id) == "complete"  # decided results all round, not stopped


def test_facts_fail_closed_and_a_skipped_lever_is_retried(conn, probes, installed, monkeypatch,
                                                          tmp_path):
    """Facts are a whole reading or nothing: a lever that skips leaves the row's facts exactly
    as they were, skips the probe (so the unfinished run resumes and reads all five again), and
    a lever that fails with a job of its own is the job the result names."""
    row = _row(conn)
    _save_capacity(conn)
    installed(probes["facts"])
    store = _store(conn)
    broken = {}

    def lever_name(levers):
        if not levers:
            return "defaults"
        if levers.get("thinking") is True:
            return "thinking_on"
        if levers.get("thinking") is False:
            return "thinking_off"
        return "effort_low" if levers.get("effort") == "low" else "effort_high"

    async def fake(reg, store_, conn_, ep, served_id, probe, run_dir, timeout_s, levers=None):
        name = lever_name(levers or {})
        if broken.get(name):
            return _outcome("skipped", 0.0, "endpoint went down", None)
        if broken.get("fail_" + name):
            jid = f"broken-{name}"
            _fake_job(store_, jid, "[thinking] x", tmp_path)
            return _outcome("fail", 1.0, "timeout", jid)
        jid = f"facts-{name}"
        _fake_job(store_, jid, "[thinking] x\n[assistant] y", tmp_path)
        return _outcome("pass", 1.0, None, jid)

    monkeypatch.setattr(calibrate, "run_probe_job", fake)

    broken["effort_high"] = True  # the one lever the reading still needs
    run_id = _run(conn, store=store)
    result = conn.execute("SELECT status, job_id FROM calibration_results WHERE run_id = ?"
                          " AND probe_id = 'facts'", (run_id,)).fetchone()
    assert result["status"] == "skipped"  # not a fail: a resume runs the whole reading again
    assert result["job_id"] == "facts-defaults"  # the skipped lever had no job: the defaults'
    fresh = conn.execute("SELECT facts FROM scorecard_rows WHERE id = ?", (row.id,)).fetchone()
    assert fresh["facts"] is None  # a fact inferred from four readings is not written
    assert _run_status(conn, run_id) == "incomplete"

    broken.clear()
    assert _run(conn, store=store) == run_id  # resume: the skipped probe is retried
    result = conn.execute("SELECT status FROM calibration_results WHERE run_id = ?"
                          " AND probe_id = 'facts'", (run_id,)).fetchone()
    assert result["status"] == "pass"
    fresh = conn.execute("SELECT facts FROM scorecard_rows WHERE id = ?", (row.id,)).fetchone()
    assert json.loads(fresh["facts"])["default_thinking"] == "on"
    assert _run_status(conn, run_id) == "complete"

    # a failing lever with a job of its own names that job, and still writes no facts
    broken["fail_effort_high"] = True
    run2 = _run(conn, store=store)
    assert run2 != run_id  # the completed run is history; this is a fresh reading
    result = conn.execute("SELECT status, reason, job_id FROM calibration_results"
                          " WHERE run_id = ? AND probe_id = 'facts'", (run2,)).fetchone()
    assert (result["status"], result["reason"]) == ("fail", "timeout")
    assert result["job_id"] == "broken-effort_high"  # the job worth pulling is the broken one
    fresh = conn.execute("SELECT facts FROM scorecard_rows WHERE id = ?", (row.id,)).fetchone()
    assert json.loads(fresh["facts"])["default_thinking"] == "on"  # untouched by the failed run


def test_thinking_chars_reads_the_recorded_length():
    """The ``(N chars)`` prefix the runner writes is what gets counted: the logged excerpt is
    the first 500 chars only, so measuring the text would undercount a long block. A line
    without the prefix predates the runner change and counts its logged text, as before."""
    assert calibrate._thinking_chars("[thinking] (1200 chars) " + "x" * 500) == 1200
    assert calibrate._thinking_chars("[thinking] (12 chars) hello there") == 12
    assert calibrate._thinking_chars("[thinking] old style line") == len(" old style line")
    two = ("[thinking] (7 chars) texty\n[assistant] (not thinking)\n[thinking] tail"
           "\n[thinking] (bad chars) x")
    assert calibrate._thinking_chars(two) == 7 + len(" tail") + len(" (bad chars) x")
    assert calibrate._thinking_chars("") == 0


# --- the unreliable seam (spec §10 "Reliable") ----------------------------------------------------


def test_unreliable_when_foreign_load_was_seen(conn, probes, monkeypatch, tmp_path):
    ep = _endpoint()
    reg = FakeRegistry({"ep": ep}, probe={"up": True, "models": ["served"],
                                          "load": {"requests_running": 2}})
    job = _fake_job(_store(conn), "job-1", "", tmp_path)

    async def start_job(**kw):
        return job

    class Store:
        async def wait(self, jid, timeout_s):
            return job

    monkeypatch.setattr(calibrate.server, "start_job", start_job)
    outcome = asyncio.run(calibrate.run_probe_job(
        reg, Store(), conn, ep, "served", probes["alpha"], tmp_path / "run", 30.0))
    assert outcome.status == "unreliable"


def test_unreliable_from_slots_busy_on_llamacpp(conn, probes, monkeypatch, tmp_path):
    """llama.cpp reports ``slots``, not ``load`` (Registry.probe's real shape): the sampler
    reads slots.busy when requests_running is absent, as capacity.from_probe does, or the
    foreign-session guard would never fire on exactly the box it was written for."""
    ep = _endpoint()
    reg = FakeRegistry({"ep": ep}, probe={"up": True, "models": ["served"],
                                          "slots": {"total": 2, "busy": 2, "free": 0}})
    job = _fake_job(_store(conn), "job-1b", "", tmp_path)

    async def start_job(**kw):
        return job

    class Store:
        async def wait(self, jid, timeout_s):
            return job

    monkeypatch.setattr(calibrate.server, "start_job", start_job)
    outcome = asyncio.run(calibrate.run_probe_job(
        reg, Store(), conn, ep, "served", probes["alpha"], tmp_path / "run", 30.0))
    assert outcome.status == "unreliable" and outcome.reason == "foreign load"


def test_environment_failure_is_skipped_not_failed(conn, probes, monkeypatch, tmp_path):
    """A job that ends failed with no result, cancelled or lost measured nothing: skipped
    with the job's error (or status) as the reason, retried on resume. A capability fail
    would be kept and poison the run; timeout and tail_capped stay fail as now."""
    ep = _endpoint()
    reg = FakeRegistry({"ep": ep}, probe={"up": True, "models": ["served"]})
    held = {}

    async def start_job(**kw):
        return held["job"]

    class Store:
        async def wait(self, jid, timeout_s):
            return held["job"]

    monkeypatch.setattr(calibrate.server, "start_job", start_job)

    async def one(status, error=None, result=None):
        job = _fake_job(_store(conn), f"env-{status}", "", tmp_path, status=status)
        job.error, job.result = error, result
        held["job"] = job
        return await calibrate.run_probe_job(
            reg, Store(), conn, ep, "served", probes["alpha"], tmp_path / "run", 30.0)

    for status, error, reason in (("failed", "connection reset", "connection reset"),
                                  ("cancelled", None, "cancelled"),
                                  ("lost", "server exited", "server exited")):
        outcome = asyncio.run(one(status, error=error))
        assert outcome.status == "skipped" and outcome.reason == reason
    assert asyncio.run(one("timeout")).status == "fail"  # stays a fail as now
    # a failed job WITH a result is a real attempt: the checker still decides it
    assert asyncio.run(one("failed", result="some output")).status == "fail"


def test_materialize_failure_is_skipped(conn, probes, installed, monkeypatch, tmp_path):
    """A tree that cannot be built (a broken fixture, a box without git) is an environment
    reason: the probe is skipped with the environment's own words, and the run goes on to the
    next probe instead of dying on the first bad tree (final review M5)."""
    _row(conn)
    _save_capacity(conn)
    installed(probes["alpha"], probes["beta"])
    job = _fake_job(_store(conn), "job-mat", "[assistant] done", tmp_path)
    dispatched = []

    def materialize(probe, dest):
        if probe.id == "alpha":
            raise FileNotFoundError(2, "No such file or directory: 'git'")
        root = Path(dest)
        root.mkdir(parents=True, exist_ok=True)
        (root / "main.py").write_text("print('x')\n")
        return root

    async def start_job(**kw):
        dispatched.append(kw["task"])
        return job

    class Store:
        async def wait(self, jid, timeout_s):
            return job

    monkeypatch.setattr(calibrate, "materialize", materialize)
    monkeypatch.setattr(calibrate.server, "start_job", start_job)
    run_id = asyncio.run(calibrate.run(_reg(), Store(), conn, "ep", tier="core",
                                       report=lambda line: None))
    rows = {r["probe_id"]: r for r in conn.execute(
        "SELECT probe_id, status, reason FROM calibration_results WHERE run_id = ?", (run_id,))}
    assert rows["alpha"]["status"] == "skipped"
    assert "git" in rows["alpha"]["reason"]  # the environment's own words, kept for the report
    assert "beta" in rows  # the run went on: one bad tree never killed it
    assert dispatched  # beta really was dispatched


def test_seconds_come_from_the_job_row(conn, probes, monkeypatch, tmp_path):
    ep = _endpoint()
    reg = FakeRegistry({"ep": ep}, probe={"up": True, "models": ["served"]})
    job = _fake_job(_store(conn), "job-2", "", tmp_path, started=10.0, finished=16.5)
    root = tmp_path / "run" / "alpha" / "1" / "repo"

    async def start_job(**kw):
        assert kw["calibration"] is True and kw["priority"] == "background"
        assert kw["isolation"] == "none" and kw["cwd"] == str(root)
        return job

    class Store:
        async def wait(self, jid, timeout_s):
            return job

    monkeypatch.setattr(calibrate.server, "start_job", start_job)
    outcome = asyncio.run(calibrate.run_probe_job(
        reg, Store(), conn, ep, "served", probes["alpha"], tmp_path / "run", 30.0))
    assert outcome.status == "fail" and outcome.reason  # the stub tree has no answer.txt
    assert outcome.seconds == 6.5


def test_retried_probe_gets_a_fresh_attempt_directory(conn, probes, monkeypatch, tmp_path):
    """A retried probe (resume, unreliable rerun) materialises into a fresh attempt directory:
    two attempts get two roots under ``<n>/repo``, and the first one's leftovers stay out of
    the new base."""
    ep = _endpoint()
    reg = FakeRegistry({"ep": ep}, probe={"up": True, "models": ["served"]})
    store = _store(conn)
    roots = []

    async def start_job(**kw):
        roots.append(Path(kw["cwd"]))
        return _fake_job(store, f"job-{len(roots)}", "", tmp_path)

    class Store:
        async def wait(self, jid, timeout_s):
            return store.jobs[jid]

    monkeypatch.setattr(calibrate.server, "start_job", start_job)
    for _attempt in range(2):
        asyncio.run(calibrate.run_probe_job(
            reg, Store(), conn, ep, "served", probes["alpha"], tmp_path / "run", 30.0))
    assert roots == [tmp_path / "run" / "alpha" / "1" / "repo",
                     tmp_path / "run" / "alpha" / "2" / "repo"]
    assert calibrate._git_head(roots[1], calibrate.PROBE_BASE_REF) is not None
    # the first attempt's work survives as evidence and is not the new attempt's tree
    (roots[0] / "answer.txt").write_text("previous attempt's work\n")
    asyncio.run(calibrate.run_probe_job(
        reg, Store(), conn, ep, "served", probes["alpha"], tmp_path / "run", 30.0))
    assert roots[2] == tmp_path / "run" / "alpha" / "3" / "repo"
    assert (roots[0] / "answer.txt").read_text() == "previous attempt's work\n"
    assert not (roots[2] / "answer.txt").exists()
    n_commits = subprocess.run(["git", "-C", str(roots[0]), "rev-list", "--count", "HEAD"],
                               capture_output=True, text=True, check=True).stdout.strip()
    assert n_commits == "1"  # the leftover did not become a base commit under the retry


def test_stub_log_path_reaches_the_checker(conn, probes, monkeypatch, tmp_path):
    """The checker of a stubmcp probe reads the recording the stub writes: the job's
    LOCALAGENTS_STUB_LOG reaches it with the same value the stub's mcp_servers env carried (the
    file under the attempt directory). Without it the adherence half passes vacuously."""
    ep = _endpoint()
    reg = FakeRegistry({"ep": ep}, probe={"up": True, "models": ["served"]})
    job = _fake_job(_store(conn), "job-stub", "", tmp_path)
    seen = {}

    async def start_job(**kw):
        seen["mcp"] = kw["mcp_servers"]
        return job

    class Store:
        async def wait(self, jid, timeout_s):
            return job

    printed = tmp_path / "stubpath.txt"
    probe = dataclasses.replace(
        probes["alpha"], needs=["stubmcp"],
        checker=f'test -n "$LOCALAGENTS_STUB_LOG" && printf "%s" "$LOCALAGENTS_STUB_LOG"'
                f' > {printed}')
    monkeypatch.setattr(calibrate.server, "start_job", start_job)
    outcome = asyncio.run(calibrate.run_probe_job(
        reg, Store(), conn, ep, "served", probe, tmp_path / "run", 30.0))
    assert outcome.status == "pass"  # the checker sees a path set
    env_path = seen["mcp"]["localagents"]["env"]["LOCALAGENTS_STUB_LOG"]
    assert printed.read_text() == env_path
    assert env_path == str(tmp_path / "run" / "alpha" / "1" / "stub.jsonl")


def test_attempt_layout_keeps_the_stub_log_outside_the_repo(conn, probes, monkeypatch, tmp_path):
    """The attempt directory holds ``repo/`` (the materialised root the job works in) and
    ``stub.jsonl`` beside it (final review, layout): the recording is not a file in the tree
    the checker reads as untouched, so a stubmcp probe's own log never shows up as work."""
    ep = _endpoint()
    reg = FakeRegistry({"ep": ep}, probe={"up": True, "models": ["served"]})
    job = _fake_job(_store(conn), "job-layout", "", tmp_path)
    seen = {}

    async def start_job(**kw):
        seen["cwd"] = Path(kw["cwd"])
        seen["stub"] = Path(kw["mcp_servers"]["localagents"]["env"]["LOCALAGENTS_STUB_LOG"])
        return job

    class Store:
        async def wait(self, jid, timeout_s):
            return job

    probe = dataclasses.replace(probes["alpha"], needs=["stubmcp"])
    monkeypatch.setattr(calibrate.server, "start_job", start_job)
    asyncio.run(calibrate.run_probe_job(
        reg, Store(), conn, ep, "served", probe, tmp_path / "run", 30.0))
    attempt = tmp_path / "run" / "alpha" / "1"
    assert seen["cwd"] == attempt / "repo"
    assert seen["stub"] == attempt / "stub.jsonl"  # beside repo/, not inside it
    assert not (seen["cwd"] / "stub.jsonl").exists()


def test_stubmcp_probe_allows_the_stub_server_and_pins_mcp_config(conn, probes, monkeypatch,
                                                                  tmp_path):
    """C1: a stubmcp probe's session is allowed the stub (the registry defaults plus the
    server name), or every ``mcp__localagents__*`` call is permission-denied and the probe
    measures a refusal, never a dispatch. And every calibration job is pinned: strict MCP
    config (the stub only, never an operator's real localagents server) and no setting
    sources (no ancestor CLAUDE.md or settings reach a probe session)."""
    ep = _endpoint()
    reg = FakeRegistry({"ep": ep}, probe={"up": True, "models": ["served"]})
    job = _fake_job(_store(conn), "job-c1", "", tmp_path)
    seen = {}

    async def start_job(**kw):
        seen.update(kw)
        return job

    class Store:
        async def wait(self, jid, timeout_s):
            return job

    monkeypatch.setattr(calibrate.server, "start_job", start_job)
    stub_probe = dataclasses.replace(probes["alpha"], needs=["stubmcp"])
    asyncio.run(calibrate.run_probe_job(
        reg, Store(), conn, ep, "served", stub_probe, tmp_path / "run", 30.0))
    assert seen["allowed_tools"] == Defaults().allowed_tools + [calibrate.STUB_SERVER_NAME]
    assert seen["strict_mcp_config"] is True
    assert seen["setting_sources"] == []
    # a probe needing no stub keeps the defaults (None) but is pinned the same way
    asyncio.run(calibrate.run_probe_job(
        reg, Store(), conn, ep, "served", probes["beta"], tmp_path / "run", 30.0))
    assert seen["allowed_tools"] is None
    assert seen["strict_mcp_config"] is True and seen["setting_sources"] == []


# --- the CLI and the tools (spec §11) ------------------------------------------------------------


def test_cli_refuses_a_router_endpoint(tmp_path, monkeypatch, capsys):
    auto = _endpoint(name="auto", backend="router", decision_url="http://d3.test:1")
    reg = Registry(path=None, endpoints={"auto": auto}, defaults=Defaults(), raw={}, local={})
    monkeypatch.setattr(calibrate.Registry, "load", staticmethod(lambda path=None: reg))
    cfg = tmp_path / "models.yaml"
    cfg.write_text("")
    with pytest.raises(SystemExit) as exc:
        calibrate.main(["auto", "--config", str(cfg)])
    assert exc.value.code == 2
    assert "router" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        calibrate.main(["nosuch", "--config", str(cfg)])
    assert exc.value.code == 2


def test_cli_flushes_each_report_line():
    # The CLI reporter must not hold lines in a block buffer: a pipe reader (a tee, a log
    # tail) sees every line the moment it is made, not when the process exits -- the live
    # run's log stayed empty until the run ended. A pipe test through main() would drive a
    # registry, a db and the shim for real, so the pin is two pieces: the identity of the
    # reporter run() installs by default, and a real-pipe check of that reporter from a
    # child that pauses after its first line.
    assert inspect.signature(calibrate.run).parameters["report"].default is calibrate.cli_report
    code = (
        "import time\n"
        "from localagents.router import calibrate\n"
        "calibrate.cli_report('estimate: first out')\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    try:
        ready, _, _ = select.select([proc.stdout], [], [], 15.0)
        assert ready, "the first report line never reached the pipe while the writer lived"
        assert proc.stdout.readline() == b"estimate: first out\n"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_recompute_runs_a_pass(conn, tmp_path, monkeypatch, capsys):
    row = _row(conn)
    _ids(conn)
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
                 " VALUES ('old', ?, 'core', '2026.11', '{}', ?, 'complete')", (row.id, time.time()))
    conn.execute("INSERT INTO calibration_results (run_id, probe_id, job_id, status, seconds)"
                 " VALUES ('old', 'alpha', NULL, 'pass', 60.0)")
    s = calibrate.load_set(SET_PATH)
    monkeypatch.setattr(calibrate, "load_set", lambda path=None: s)
    reg = FakeRegistry({"ep": _endpoint()})
    monkeypatch.setattr(calibrate.Registry, "load", staticmethod(lambda path=None: reg))
    real_open = db.open_db
    monkeypatch.setattr(calibrate.db, "open_db", lambda path=None: real_open(tmp_path / "r.db"))
    cfg = tmp_path / "models.yaml"
    cfg.write_text("")
    calibrate.main(["ep", "--recompute", "--config", str(cfg)])
    assert conn.execute("SELECT COUNT(*) FROM scorecard_passes WHERE row_id = ?",
                        (row.id,)).fetchone()[0] == 1
    out = capsys.readouterr().out
    assert "software_dev/python:" in out and "complexity:" in out


def test_recompute_uses_the_named_set(conn, tmp_path, monkeypatch, capsys):
    """``--recompute --set PATH`` runs its passes with the labels of the named set, not the
    default one (final review, --set): the printed cells carry the named manifest's labels."""
    row = _row(conn)
    _ids(conn)
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
                 " VALUES ('old', ?, 'core', '2026.11', '{}', ?, 'complete')", (row.id, time.time()))
    conn.execute("INSERT INTO calibration_results (run_id, probe_id, job_id, status, seconds)"
                 " VALUES ('old', 'alpha', NULL, 'pass', 60.0)")
    named = tmp_path / "named"
    shutil.copytree(SET_PATH.parent, named)  # the manifest resolves its briefs beside itself
    data = yaml.safe_load((named / "set.yaml").read_text())
    data["probes"][0]["language"] = "go"  # the pass's labels speak the named manifest's language
    (named / "set.yaml").write_text(yaml.safe_dump(data))
    reg = FakeRegistry({"ep": _endpoint()})
    monkeypatch.setattr(calibrate.Registry, "load", staticmethod(lambda path=None: reg))
    real_open = db.open_db
    monkeypatch.setattr(calibrate.db, "open_db", lambda path=None: real_open(tmp_path / "r.db"))
    cfg = tmp_path / "models.yaml"
    cfg.write_text("")
    calibrate.main(["ep", "--recompute", "--set", str(named / "set.yaml"), "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "software_dev/go:" in out  # alpha's language cell as the named set defines it
    assert "software_dev/python:" not in out  # the default set's labels were not used


def test_scorecard_tool_lists_rows_cells_and_notices(conn, monkeypatch):
    row = _row(conn)
    conn.execute("UPDATE scorecard_rows SET set_version = '2026.10', facts = ? WHERE id = ?",
                 (json.dumps({"default_thinking": "on"}), row.id))
    s = calibrate.load_set(SET_PATH)
    monkeypatch.setattr(calibrate, "load_set", lambda path=None: s)
    monkeypatch.setattr(router, "_db", lambda: conn)
    scorecard.run_pass(conn, row.id, probes=s.labels())
    out = asyncio.run(router.scorecard(history=True))
    assert len(out["rows"]) == 1
    got = out["rows"][0]
    for key in ("endpoint_name", "served_id", "archived", "set_version", "speed_factor",
                "core_complete", "notices", "facts", "cells", "history"):
        assert key in got
    assert got["set_version"] == "2026.10"
    assert got["facts"] == {"default_thinking": "on"}
    assert "needs recalibration" in got["notices"]
    assert got["core_complete"] is False
    assert any(c["dimension"] == "software_dev" for c in got["cells"])
    assert len(got["history"]) == 1
    # recompute runs another pass before the cells are read
    out = asyncio.run(router.scorecard(endpoint="ep", recompute=True))
    assert len(out["rows"][0]["cells"]) == len(got["cells"])
    assert conn.execute("SELECT COUNT(*) FROM scorecard_passes WHERE row_id = ?",
                        (row.id,)).fetchone()[0] == 2


# --- the shipped set's checker proofs (spec §11) -------------------------------------------------

_SHIPPED = calibrate.load_set()
_SHIPPED_PARAMS = _SHIPPED.probes or [
    pytest.param(None, marks=pytest.mark.skip(reason="the shipped core.yaml lists no probes at this base"))]


@pytest.mark.parametrize("probe", _SHIPPED_PARAMS)
def test_shipped_set_checkers_fail_on_stub_and_pass_on_reference(probe, tmp_path):
    if probe is None:
        return
    root = calibrate.materialize(probe, tmp_path / "stub")
    ok, reason = calibrate.check(probe, root)
    assert ok is False, probe.id
    assert (probe.fixture_dir / "reference").is_dir(), probe.id
    calibrate.apply_reference(probe, root)
    env = {}
    if "stubmcp" in probe.needs:
        log = tmp_path / "stub.jsonl"
        log.write_text("")
        env["LOCALAGENTS_STUB_LOG"] = str(log)
    ok, reason = calibrate.check(probe, root, env)
    assert ok is True, (probe.id, reason)


def test_check_reads_the_recorded_base_not_head(tmp_path):
    """The base a checker compares against is the ref materialize recorded, so a job that
    commits (completeness-tail asks for one) cannot move it; HEAD only when nothing was recorded."""
    probe = calibrate.load_set(SET_PATH).probes[0]
    root = calibrate.materialize(probe, tmp_path / "r")
    base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    (root / "later.txt").write_text("a job's commit\n")
    subprocess.run(["git", "-C", str(root), "-c", "user.name=j", "-c", "user.email=j@j",
                    "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=j", "-c", "user.email=j@j",
                    "commit", "-q", "-m", "job"], check=True)
    assert calibrate._probe_base(root) == base
    seen = tmp_path / "seen"
    checker = dataclasses.replace(probe, checker=f"printf '%s' \"$LOCALAGENTS_PROBE_BASE\" > {seen}")
    assert calibrate.check(checker, root) == (True, None)
    assert seen.read_text() == base
    subprocess.run(["git", "-C", str(root), "update-ref", "-d", calibrate.PROBE_BASE_REF], check=True)
    assert calibrate._probe_base(root) != base  # nothing recorded: HEAD, the job's commit
