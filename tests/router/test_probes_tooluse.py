"""router 9c (docs/superpowers/specs/2026-11-02-calibration-set-design.md §5, §11): the five
authored tool-use probes' checker proofs, run without a model. Every checker is the manifest's
own command, run with cwd at a materialized repo: the stub scenario (raw project, no answer)
must fail printing no_answer, and the reference scenario (reference/ layered on top) must exit
0 — the checker_proof the manifest claims, proven by this suite. No model is run anywhere."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SETS = ROOT / "src" / "localagents" / "router" / "sets"
FIXTURES = SETS / "fixtures"

IDS = ["tool-failing-build", "tool-bisect", "tool-log-to-config", "tool-inventory",
       "tool-mcp-stats"]

# the manifest entry fields §5 fixes; every probe carries them
FIELDS = ["id", "tier", "source", "language", "labels", "source_difficulty", "brief", "checker",
          "checker_proof", "nominal_s", "nominal_source", "spread", "needs", "variant"]
LABEL_AXES = ["software_dev", "tool_use", "computer_use", "complexity", "specification",
              "context_load"]


def _manifest():
    doc = yaml.safe_load((SETS / "core.yaml").read_text())
    assert doc["version"] == "2026.11"
    return {p["id"]: p for p in doc["probes"]}


def _git(repo: Path, *args: str) -> None:
    proc = subprocess.run(["git", "-c", "user.name=Ana Novak",
                           "-c", "user.email=ana@example.invalid", *args],
                          cwd=repo, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def _materialise(pid: str, tmp_path: Path) -> Path:
    """project/ becomes the materialized repo's first commit; setup.sh (bisect) then rewrites
    history and records the planted answer the brief must not reveal."""
    fixture = FIXTURES / pid
    repo = tmp_path / "repo"
    shutil.copytree(fixture / "project", repo)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial commit")
    if (fixture / "setup.sh").exists():
        proc = subprocess.run(["bash", str(fixture / "setup.sh")], cwd=repo,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    return repo


def _run_checker(pid: str, repo: Path, stub_log: Path | None = None) -> subprocess.CompletedProcess:
    cmd = _manifest()[pid]["checker"]
    env = dict(os.environ, LOCALAGENTS_PROBE_DIR=str(FIXTURES / pid))
    if stub_log is not None:
        env["LOCALAGENTS_STUB_LOG"] = str(stub_log)
    else:
        env.pop("LOCALAGENTS_STUB_LOG", None)
    return subprocess.run(["bash", "-c", cmd], cwd=repo, env=env,
                          capture_output=True, text=True)


def _read_recording(pid: str) -> str:
    return (FIXTURES / pid / "reference" / "stub_log.jsonl").read_text()


# --- checker_proof: stub fails, reference passes ---------------------------------------

@pytest.mark.parametrize("pid", IDS)
def test_checker_fails_on_the_stub(pid, tmp_path):
    """Raw project, no model, no answer: the checker fails, and says no_answer (§5's
    missing/malformed t/answer.json rule)."""
    repo = _materialise(pid, tmp_path)
    log = None
    if pid == "tool-mcp-stats":
        log = tmp_path / "empty.jsonl"
        log.touch()  # the session read nothing; the recording exists and is empty
    proc = _run_checker(pid, repo, stub_log=log)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "no_answer" in proc.stdout


@pytest.mark.parametrize("pid", IDS)
def test_checker_passes_on_the_reference(pid, tmp_path):
    repo = _materialise(pid, tmp_path)
    shutil.copytree(FIXTURES / pid / "reference", repo, dirs_exist_ok=True)
    log = None
    if pid == "tool-mcp-stats":
        # the reference session's recording: two reads, no dispatch
        log = tmp_path / "reference.jsonl"
        log.write_text(_read_recording(pid))
    proc = _run_checker(pid, repo, stub_log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- per-probe pins of what the scenario is really about -------------------------------

def test_failing_build_stub_fails_readable_on_the_planted_file(tmp_path):
    """§3: the failure is caused by one planted file and the traceback names it; that name is
    the planted fact the brief must withhold."""
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / "tool-failing-build" / "project", repo)
    proc = subprocess.run([sys.executable, "-m", "unittest", "-q", "build_check"],
                          cwd=repo, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "report.py" in proc.stderr
    # and the reference's fix (reference/report.py over the broken one) makes the suite green
    shutil.copytree(FIXTURES / "tool-failing-build" / "reference", repo, dirs_exist_ok=True)
    ok = subprocess.run([sys.executable, "-m", "unittest", "-q", "build_check"],
                        cwd=repo, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr


def test_bisect_setup_records_the_planted_hash_not_in_the_brief(tmp_path):
    repo = _materialise("tool-bisect", tmp_path)
    planted = subprocess.run(["git", "rev-parse", "refs/probe/answer"], cwd=repo,
                             capture_output=True, text=True)
    assert planted.returncode == 0, planted.stderr
    hash_ = planted.stdout.strip()
    assert len(hash_) == 40
    # the reference answer is the same prefix the materialized history planted
    answer = json.loads((FIXTURES / "tool-bisect" / "reference" / "t" / "answer.json").read_text())
    assert hash_.startswith(answer["commit"]) and len(answer["commit"]) >= 7
    # the brief withholds it, and so does every tracked file in the project
    assert answer["commit"] not in (FIXTURES / "tool-bisect" / "brief.md").read_text()
    # eight commits, and the planted one is neither the first nor the last
    count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo,
                           capture_output=True, text=True).stdout.strip()
    assert int(count) >= 8
    # the suite is red at HEAD (the job bisects to find where it went wrong)
    red = subprocess.run([sys.executable, "-m", "unittest", "-q", "build_check"], cwd=repo,
                         capture_output=True, text=True)
    assert red.returncode != 0


def test_bisect_reference_wrong_commit_fails(tmp_path):
    repo = _materialise("tool-bisect", tmp_path)
    shutil.copytree(FIXTURES / "tool-bisect" / "reference", repo, dirs_exist_ok=True)
    (repo / "t" / "answer.json").write_text(json.dumps({"commit": "0" * 40}))
    proc = _run_checker("tool-bisect", repo)
    assert proc.returncode != 0


def test_log_to_config_value_is_derivable_from_the_log_only(tmp_path):
    """The planted value appears in the log's bind line, nowhere in the brief; the config as
    shipped holds a different value (the stub must fail on more than a missing answer)."""
    fixture = FIXTURES / "tool-log-to-config"
    check = _load_check("tool-log-to-config")
    log = (fixture / "project" / "logs" / "gateway.log").read_text()
    config = json.loads((fixture / "project" / "config.json").read_text())
    assert str(check.CORRECT_PORT) in log
    assert config["gateway_port"] != check.CORRECT_PORT
    assert str(check.CORRECT_PORT) not in (fixture / "brief.md").read_text()


def test_inventory_constants_match_the_shipped_tree():
    """§5: the checker compares against the fixture's own constants — they must equal what the
    tree actually holds, or the reference would grade wrong."""
    check = _load_check("tool-inventory")
    tree = FIXTURES / "tool-inventory" / "project" / "tree"
    by_ext: dict[str, int] = {}
    lines = 0
    for p in sorted(tree.rglob("*")):
        if not p.is_file():
            continue
        by_ext[p.suffix.lstrip(".")] = by_ext.get(p.suffix.lstrip("."), 0) + 1
        text = p.read_text()
        lines += sum(1 for ln in text.splitlines() if check.PATTERN in ln)
    assert by_ext == check.BY_EXTENSION
    assert lines == check.MATCHING_LINES
    assert sum(by_ext.values()) == 40


def test_inventory_wrong_counts_fail(tmp_path):
    repo = _materialise("tool-inventory", tmp_path)
    shutil.copytree(FIXTURES / "tool-inventory" / "reference", repo, dirs_exist_ok=True)
    path = repo / "t" / "answer.json"
    answer = json.loads(path.read_text())
    answer["matching_lines"] += 1
    path.write_text(json.dumps(answer))
    assert _run_checker("tool-inventory", repo).returncode != 0


def test_mcp_stats_reference_with_a_dispatch_line_fails(tmp_path):
    """§3: tool-mcp-stats doubles as the instruction-adherence probe — the checker reads the
    stub's recording and refuses a session that dispatched."""
    repo = _materialise("tool-mcp-stats", tmp_path)
    shutil.copytree(FIXTURES / "tool-mcp-stats" / "reference", repo, dirs_exist_ok=True)
    log = tmp_path / "dispatched.jsonl"
    log.write_text(_read_recording("tool-mcp-stats")
                   + json.dumps({"t": 1.0, "tool": "run_agent", "args": {"task": "x"}}) + "\n")
    proc = _run_checker("tool-mcp-stats", repo, stub_log=log)
    assert proc.returncode != 0


def test_mcp_stats_checker_agrees_with_the_stub_canned_values():
    """The checker's literals are checked against stubmcp.CANNED here so the checker stays
    self-contained (a bare `python3` with no localagents import) yet cannot drift."""
    from localagents.router import stubmcp

    check = _load_check("tool-mcp-stats")
    eps = stubmcp.CANNED["list_models"]["endpoints"]
    up = [e for e in eps.values() if e["up"]]
    assert check.EXPECTED == {
        "endpoints_up": len(up),
        "models": sorted({m for e in up for m in e["models"]}),
        "job_status": stubmcp.CANNED["job_status"]["status"],
        "job_turns": stubmcp.CANNED["job_status"]["num_turns"],
    }
    assert check.FORBIDDEN == {"run_agent", "wait_job", "wait_jobs", "cancel_job", "local_complete"}
    # and the reference answer equals those expectations
    answer = json.loads((FIXTURES / "tool-mcp-stats" / "reference" / "t" /
                         "answer.json").read_text())
    assert all(answer[k] == v for k, v in check.EXPECTED.items())


def test_no_brief_leaks_its_planted_fact():
    """The briefs withhold every planted value; only the checker and the reference hold them."""
    check_fc = _load_check("tool-failing-build")
    assert check_fc.CAUSE_FILE not in (FIXTURES / "tool-failing-build" / "brief.md").read_text()
    inv = _load_check("tool-inventory")
    inv_brief = (FIXTURES / "tool-inventory" / "brief.md").read_text()
    assert str(inv.MATCHING_LINES) not in inv_brief
    for count in inv.BY_EXTENSION.values():
        assert str(count) not in inv_brief


# --- the manifest ----------------------------------------------------------------------

def test_core_yaml_lists_the_five_authored_probes():
    probes = _manifest()
    for pid in IDS:
        assert pid in probes
        p = probes[pid]
        for field in FIELDS:
            assert field in p, f"{pid} missing {field}"
        assert p["tier"] == "core"
        assert p["source"] == {"name": "authored", "revision": None,
                               "path": f"fixtures/{pid}", "licence": "ours"}
        assert set(p["labels"]) == set(LABEL_AXES)
        assert all(isinstance(v, int) and 0 <= v <= 4 for v in p["labels"].values())
        assert p["source_difficulty"] is None
        assert p["brief"] == f"fixtures/{pid}/brief.md"
        assert p["checker"] == 'python3 "$LOCALAGENTS_PROBE_DIR/check.py"'
        assert p["checker_proof"] == {"stub": "fail", "reference": "pass"}
        assert isinstance(p["nominal_s"], int) and p["nominal_s"] > 0
        assert p["nominal_source"] == "estimated"
        assert p["spread"] == {"failed_on": []}
        assert p["variant"] is None
        # every file the entry points at exists
        assert (SETS / p["brief"]).is_file()
        assert (FIXTURES / pid / "check.py").is_file()
        assert (FIXTURES / pid / "project").is_dir()
        assert (FIXTURES / pid / "reference").is_dir()
        for need in p["needs"]:
            assert need in ("python3", "git", "stubmcp")
    assert probes["tool-mcp-stats"]["needs"] == ["python3", "git", "stubmcp"]


def test_fixture_trees_avoid_pytest_collection_names():
    """`uv run pytest -q` has no ini and collects from the repo root, so no fixture file may
    match test_*.py / *_test.py; the projects' runners are named build_check."""
    for path in sorted(FIXTURES.rglob("*.py")):
        assert not (path.name.startswith("test_") or path.name.endswith("_test.py")), path


def _load_check(pid: str):
    import importlib.util

    path = FIXTURES / pid / "check.py"
    spec = importlib.util.spec_from_file_location(f"check_{pid.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
