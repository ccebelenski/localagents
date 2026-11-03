"""Task 9b (router plan Task 9; the calibration set spec §3, §4, §5, §8, §11): the six authored
judgement and review probes.

Each probe is a fixture under ``src/localagents/router/sets/fixtures/<id>/``: a ``brief.md`` (the
job's brief, written for a capable but junior engineer, which never names the planted fact), a
``project/`` (the tree the job gets, committed as the materialised repo's first commit, shipped
with a ``.gitignore`` holding ``t/``), an optional ``setup.sh`` (run at the repo root after the
first commit), an optional ``reference/`` (files overlaid onto the project to form the reference
solution, ``t/answer.json`` included for report probes), and a ``check.py`` helper. The checker
is the manifest's one shell command, run with ``bash -c`` at the repo root with
``LOCALAGENTS_PROBE_DIR`` set to the fixture directory; exit 0 is a pass. The runner also exports
``LOCALAGENTS_PROBE_BASE`` — the materialised repo's HEAD after the fixture commit and ``setup.sh``,
where one exists — so a checker that wants an untouched tree sees a job's COMMITTED edits too (fix
round 1, item 2: a committed change empties ``git status --porcelain``, which the checkers used to
read alone). With the base unset (a checker run by hand) the checkers fall back to the porcelain
reading and warn ``base_unset`` on stderr.

The suite (spec §11) runs every checker on the stub and on the reference without a model: the
stub must fail, the reference must pass, and the stub's own unittest suite (where the fixture
ships one) must be green. A ``core.yaml`` manifest entry for each probe carries every field of
spec §8. Nothing here runs a model or dispatches a job: the probes are proven by their checkers
alone.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

# The authored fixtures live beside the package (they ship in the wheel under
# ``localagents/router/sets/``). The test resolves them from its own location, so it exercises
# the worktree's copies regardless of how the package is installed.
SETS = Path(__file__).resolve().parents[2] / "src" / "localagents" / "router" / "sets"
FIXTURES = SETS / "fixtures"
CORE = SETS / "core.yaml"

IDS = [
    "judgement-planted-bug",
    "judgement-false-premise",
    "judgement-missing-dependency",
    "judgement-open-decision",
    "review-planted-defect",
    "review-clean",
]

# Every probe's checker is the same one-line shape (spec §5: one shell command, exit 0 = pass,
# run at the repo root with LOCALAGENTS_PROBE_DIR naming the fixture directory).
CHECKER_CMD = "python3 $LOCALAGENTS_PROBE_DIR/check.py"

# spec §8: every manifest entry carries these fields (and source/labels carry their sub-fields).
ENTRY_FIELDS = (
    "id", "tier", "source", "language", "labels", "source_difficulty",
    "brief", "checker", "checker_proof", "nominal_s", "nominal_source",
    "spread", "needs", "variant",
)
SOURCE_FIELDS = ("name", "revision", "path", "licence")
LABEL_AXES = (
    "software_dev", "tool_use", "computer_use", "complexity",
    "specification", "context_load", "judgement",
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _materialize(fixture_dir: Path, dst: Path) -> str:
    """Copy ``project/`` onto ``dst`` as the first commit; run ``setup.sh`` if present.

    The contract (Task 9b brief): ``project/`` is committed as the materialised repo's first
    commit, then ``setup.sh`` (if any) runs at the repo root. Returns the probe base — the
    repo's ``HEAD`` after those steps — which the runner exports as ``LOCALAGENTS_PROBE_BASE``
    (fix round 1, item 2) so the untouched-tree checks judge against the fixture commit rather
    than an empty ``git status --porcelain`` a job's own commit can produce.
    """
    project = fixture_dir / "project"
    shutil.copytree(project, dst, dirs_exist_ok=True)
    _git(dst, "init", "-q")
    for key, value in (("user.name", "probe"), ("user.email", "probe@local"),
                       ("commit.gpgsign", "false")):
        _git(dst, "config", key, value)
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "fixture")
    setup = dst / "setup.sh"
    if setup.exists():
        subprocess.run(["bash", "setup.sh"], cwd=dst, check=True,
                       capture_output=True, text=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=dst, check=True,
                          capture_output=True, text=True).stdout.strip()


def _run_checker(dst: Path, checker: str, fixture_dir: Path,
                 base: str | None = None) -> subprocess.CompletedProcess:
    """Run the manifest's checker with ``bash -c``, cwd = repo root, LOCALAGENTS_PROBE_DIR set;
    LOCALAGENTS_PROBE_BASE set to the probe base when given (unset exercises the hand-run
    fallback)."""
    env = dict(os.environ, LOCALAGENTS_PROBE_DIR=str(fixture_dir))
    if base is not None:
        env["LOCALAGENTS_PROBE_BASE"] = base
    return subprocess.run(["bash", "-c", checker], cwd=dst, env=env,
                          capture_output=True, text=True)


def _entries() -> dict:
    if not CORE.exists():
        return {}
    return {probe["id"]: probe for probe in yaml.safe_load(CORE.read_text()).get("probes", [])}


@pytest.mark.parametrize("probe_id", IDS)
def test_stub_fails_reference_passes(probe_id, tmp_path):
    """The stub (the bare project) fails the checker; the reference overlay passes it."""
    fixture_dir = FIXTURES / probe_id
    if not fixture_dir.is_dir():
        pytest.fail(f"fixture {probe_id} is not built yet (missing {fixture_dir})")
    entries = _entries()
    if probe_id not in entries:
        pytest.fail(f"{probe_id} has no core.yaml entry yet")
    checker = entries[probe_id]["checker"]

    repo = tmp_path / "repo"
    base = _materialize(fixture_dir, repo)

    stub = _run_checker(repo, checker, fixture_dir, base=base)
    assert stub.returncode != 0, (
        f"the stub must fail the checker ({probe_id}):\n"
        f"stdout: {stub.stdout}\nstderr: {stub.stderr}")

    reference = fixture_dir / "reference"
    if reference.is_dir():
        shutil.copytree(reference, repo, dirs_exist_ok=True)

    ref = _run_checker(repo, checker, fixture_dir, base=base)
    assert ref.returncode == 0, (
        f"the reference solution must pass the checker ({probe_id}):\n"
        f"stdout: {ref.stdout}\nstderr: {ref.stderr}")


@pytest.mark.parametrize("probe_id", IDS)
def test_stub_suite_green(probe_id, tmp_path):
    """Where the fixture ships a unittest suite, the stub's own suite is green (spec §5: the stub
    run is the trap-proof; a green suite on the stub is the planted-bug / planted-defect contract).
    """
    fixture_dir = FIXTURES / probe_id
    if not fixture_dir.is_dir():
        pytest.skip(f"fixture {probe_id} is not built yet")
    if not any((fixture_dir / "project").glob("**/test*.py")):
        pytest.skip(f"{probe_id} ships no test suite")
    repo = tmp_path / "repo"
    _materialize(fixture_dir, repo)
    result = subprocess.run(["python3", "-m", "unittest", "discover", "-q"],
                            cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"{probe_id}'s stub suite must be green:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}")


def test_core_manifest_lists_six_with_all_fields():
    """core.yaml lists all six authored probes, each with every field of the spec §8 entry shape."""
    if not CORE.exists():
        pytest.fail(f"core.yaml is not built yet ({CORE})")
    entries = _entries()
    for probe_id in IDS:
        assert probe_id in entries, f"{probe_id} is missing from core.yaml"
        entry = entries[probe_id]
        for field in ENTRY_FIELDS:
            assert field in entry, f"{probe_id} entry is missing field {field!r}"
        assert entry["checker"] == CHECKER_CMD, (
            f"{probe_id} checker is {entry['checker']!r}, expected {CHECKER_CMD!r}")
        assert entry["checker_proof"] == {"stub": "fail", "reference": "pass"}, (
            f"{probe_id} checker_proof is {entry['checker_proof']!r}")
        for field in SOURCE_FIELDS:
            assert field in entry["source"], f"{probe_id} source is missing {field!r}"
        for axis in LABEL_AXES:
            assert axis in entry["labels"], f"{probe_id} labels are missing {axis!r}"


# --- wrong answers: the reference report present, the act wrong (fix round 1) -------------
#
# Each case lays the reference ``t/answer.json`` (the right report) over a wrong act and
# commits or leaves the act; the checker must still fail. The item-1 hole: the missing
# module invented as a PACKAGE (``timetable/holidays/__init__.py``) and committed, which
# the old file-only scan scored as having stopped.

MISSING_MODULE_CASES = {
    "file, committed": ("timetable/holidays.py", True),
    "file, uncommitted": ("timetable/holidays.py", False),
    "package, committed": ("timetable/holidays/__init__.py", True),
    "package, uncommitted": ("timetable/holidays/__init__.py", False),
}


@pytest.mark.parametrize("case", sorted(MISSING_MODULE_CASES))
def test_missing_dependency_invented_module_fails(case, tmp_path):
    """A stopped-task report must not pass when the job invented the module anyway,
    as a file or as a package, committed or not."""
    rel, commit = MISSING_MODULE_CASES[case]
    fixture_dir = FIXTURES / "judgement-missing-dependency"
    repo = tmp_path / "repo"
    base = _materialize(fixture_dir, repo)
    shutil.copytree(fixture_dir / "reference", repo, dirs_exist_ok=True)
    invented = repo / rel
    invented.parent.mkdir(parents=True, exist_ok=True)
    invented.write_text("def is_holiday(d):\n    return False\n")
    if commit:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "invented the missing module")

    result = _run_checker(repo, CHECKER_CMD, fixture_dir, base=base)
    assert result.returncode != 0, (
        f"the checker must fail when the module is invented ({case}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}")
    assert "module_invented" in result.stderr, (
        f"{case}: expected reason module_invented, got stderr: {result.stderr}")


# Item 2: the untouched-tree check judged against LOCALAGENTS_PROBE_BASE. The reference
# report is right but one tracked file was edited AND COMMITTED, so
# ``git status --porcelain`` is empty — only a base-relative reading catches it.

COMMITTED_EDIT_TARGETS = {
    "judgement-missing-dependency": "README.md",
    "review-planted-defect": "budget/summary.py",
    "review-clean": "budget/summary.py",
}


@pytest.mark.parametrize("probe_id, target", sorted(COMMITTED_EDIT_TARGETS.items()))
def test_committed_edit_fails_untouched_tree_check(probe_id, target, tmp_path):
    """A correct report must not pass when the job committed an edit: porcelain is empty,
    the base diff is not."""
    fixture_dir = FIXTURES / probe_id
    repo = tmp_path / "repo"
    base = _materialize(fixture_dir, repo)
    shutil.copytree(fixture_dir / "reference", repo, dirs_exist_ok=True)
    with open(repo / target, "a") as fh:
        fh.write("\n# tampered\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "committed edit")

    result = _run_checker(repo, CHECKER_CMD, fixture_dir, base=base)
    assert result.returncode != 0, (
        f"a committed edit must fail the untouched-tree check ({probe_id}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}")
    assert "diff_not_empty" in result.stderr, (
        f"{probe_id}: expected reason diff_not_empty, got stderr: {result.stderr}")


def test_false_premise_committed_creation_fails(tmp_path):
    """The asserted file created AND committed fails the guard: the base reading names it
    in ``git diff --name-only $BASE``, so the commit does not hide it."""
    fixture_dir = FIXTURES / "judgement-false-premise"
    repo = tmp_path / "repo"
    base = _materialize(fixture_dir, repo)
    shutil.copytree(fixture_dir / "reference", repo, dirs_exist_ok=True)
    (repo / "parser.py").write_text("def parse(s):\n    return s\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "took the brief literally")

    result = _run_checker(repo, CHECKER_CMD, fixture_dir, base=base)
    assert result.returncode != 0, (
        f"a committed parser.py must fail the guard:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}")
    assert "premise_taken_literally" in result.stderr, (
        f"expected reason premise_taken_literally, got stderr: {result.stderr}")


def test_false_premise_gitignore_hidden_creation_fails(tmp_path):
    """An untracked parser.py hidden by editing ``.gitignore`` is invisible to the base
    reading (``--exclude-standard``); the working-tree scan run beside it catches it."""
    fixture_dir = FIXTURES / "judgement-false-premise"
    repo = tmp_path / "repo"
    base = _materialize(fixture_dir, repo)
    shutil.copytree(fixture_dir / "reference", repo, dirs_exist_ok=True)
    (repo / "parser.py").write_text("def parse(s):\n    return s\n")
    with open(repo / ".gitignore", "a") as fh:
        fh.write("parser.py\n")

    result = _run_checker(repo, CHECKER_CMD, fixture_dir, base=base)
    assert result.returncode != 0, (
        f"a hidden untracked parser.py must fail the guard:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}")
    assert "premise_taken_literally" in result.stderr, (
        f"expected reason premise_taken_literally, got stderr: {result.stderr}")


def test_base_unset_warns_and_still_checks(tmp_path):
    """A checker run by hand (no LOCALAGENTS_PROBE_BASE): the reference still passes on
    the porcelain reading, and the weaker reading is warned as ``base_unset`` on stderr."""
    fixture_dir = FIXTURES / "review-clean"
    repo = tmp_path / "repo"
    _materialize(fixture_dir, repo)
    shutil.copytree(fixture_dir / "reference", repo, dirs_exist_ok=True)

    result = _run_checker(repo, CHECKER_CMD, fixture_dir)  # base deliberately unset
    assert result.returncode == 0, (
        f"the reference must pass on the fallback reading:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}")
    assert "base_unset" in result.stderr, (
        f"the fallback must warn base_unset, got stderr: {result.stderr}")
