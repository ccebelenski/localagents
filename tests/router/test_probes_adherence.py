"""router 9d: the authored adherence and completeness probes, and the facts fixture.

Each fixture is proven the way the build proves it: materialise ``project/`` as the repo's first
commit, run the manifest's checker (it must fail on the stub), overlay ``reference/`` (it must
pass). The adherence probe adds a variant whose answer is right but whose script writes under
/tmp: the rule's gate, not the answer's, must fail it. No model runs here.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SETS = ROOT / "src" / "localagents" / "router" / "sets"
FIXTURES = SETS / "fixtures"
PROBES = ("adherence-tmp-temptation", "completeness-tail", "facts-thinking")
FIELDS = ("id", "tier", "source", "language", "labels", "source_difficulty", "brief", "checker",
          "checker_proof", "nominal_s", "nominal_source", "spread", "needs", "variant")
LABELS = {
    "adherence-tmp-temptation": {"tool_use": 2, "software_dev": 1, "complexity": 1,
                                 "specification": 1, "context_load": 0, "computer_use": 0},
    "completeness-tail": {"software_dev": 2, "tool_use": 2, "complexity": 1,
                          "specification": 0, "context_load": 0, "computer_use": 0},
    "facts-thinking": {"software_dev": 1, "tool_use": 1, "complexity": 1,
                       "specification": 0, "context_load": 0, "computer_use": 0},
}
# The completeness solution's last action is a job action, not a file: the proof commits it.
REFERENCE_COMMITS = {"completeness-tail": "step 3: report"}


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _materialise(fixture: Path, dest: Path) -> None:
    """``project/`` as the repo's first commit, exactly as the runner will."""
    shutil.copytree(fixture / "project", dest)
    _git(dest, "init", "-q")
    _git(dest, "config", "user.email", "probe@example.invalid")
    _git(dest, "config", "user.name", "Probe")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "fixture")


def _overlay(src: Path, dest: Path) -> None:
    for path in sorted(src.rglob("*")):
        if path.is_file():
            target = dest / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def _checker(probe_id: str) -> str:
    manifest = yaml.safe_load((SETS / "core.yaml").read_text())
    return next(p["checker"] for p in manifest["probes"] if p["id"] == probe_id)


def _check(fixture: Path, repo: Path):
    env = {**os.environ, "LOCALAGENTS_PROBE_DIR": str(fixture)}
    return subprocess.run(["bash", "-c", _checker(fixture.name)], cwd=repo, env=env,
                          capture_output=True, text=True)


@pytest.mark.parametrize("probe_id", PROBES)
def test_stub_fails_and_reference_passes(probe_id, tmp_path):
    """The manifest's checker proof, run the way the build runs it: no model, real repo."""
    fixture = FIXTURES / probe_id
    repo = tmp_path / "repo"
    _materialise(fixture, repo)
    stub = _check(fixture, repo)
    assert stub.returncode != 0, f"the stub passed: {stub.stdout}{stub.stderr}"
    _overlay(fixture / "reference", repo)
    if probe_id in REFERENCE_COMMITS:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", REFERENCE_COMMITS[probe_id])
    reference = _check(fixture, repo)
    assert reference.returncode == 0, f"the reference failed: {reference.stdout}{reference.stderr}"


def test_tmp_variant_is_refused_even_though_the_answer_is_right(tmp_path):
    """The adherence rule is the gate: correct figures, a /tmp scratch path, a fail."""
    fixture = FIXTURES / "adherence-tmp-temptation"
    repo = tmp_path / "repo"
    _materialise(fixture, repo)
    _overlay(fixture / "variants" / "tmp-mention", repo)
    out = _check(fixture, repo)
    assert out.returncode != 0
    assert "/tmp" in out.stdout


def test_core_manifest_lists_the_three_probes():
    """Every field of 9b's shape, the hand labels, and ``kind: facts`` on the facts probe."""
    manifest = yaml.safe_load((SETS / "core.yaml").read_text())
    assert manifest["version"] == "2026.11"
    entries = {p["id"]: p for p in manifest["probes"]}
    for probe_id in PROBES:
        entry = entries[probe_id]
        for field in FIELDS:
            assert field in entry, f"{probe_id} is missing {field}"
        assert entry["tier"] == "core"
        assert entry["source"] == {"name": "authored", "revision": None,
                                   "path": f"fixtures/{probe_id}", "licence": "ours"}
        assert entry["language"] == "python"
        assert entry["labels"] == LABELS[probe_id]
        assert entry["source_difficulty"] is None
        assert entry["brief"] == f"fixtures/{probe_id}/brief.md"
        assert entry["checker_proof"] == {"stub": "fail", "reference": "pass"}
        assert entry["nominal_source"] in ("estimated", "measured")
        assert entry["spread"] == {"failed_on": []}
        assert "python3" in entry["needs"]
        assert entry["variant"] is None
    assert entries["facts-thinking"].get("kind") == "facts"
    assert all("kind" not in entries[p] for p in PROBES if p != "facts-thinking")
