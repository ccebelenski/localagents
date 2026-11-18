"""Router 13 (router-design spec §9 feedback, §8 what the pass reads, §11 ``verdict``): the
mechanical gates a finished job writes to ``jobs.gates``, its ``speed_samples`` row, and the
coordinator's verdict (``verdicts``, a ticket scored at verdict time for a named job,
``category_corrections``). Both feeds write evidence and never a cell; ``on_finish`` never
raises and never moves the job's status or error.

The gate keys are the ones ``scorecard.observations`` and ``scorecard._ceiling`` read
(scorecard.py's pinned wire shape): ``tests``, ``stop_rule``, ``not_capped``, ``no_tmp``,
``files_within``, ``lint`` (true = clean, false, null = not evaluable) and ``files_n``.
Jobs, tool calls and tickets are SQL rows on conftest's ``conn``; the diff gates run over a
real throwaway git repo and a real worktree."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from localagents.jobs import Job
from localagents.registry import Defaults, Endpoint, Registry, Resolution
from localagents.router import definitions, feedback, scorecard
from localagents.router.decision import BriefScores
from localagents.harnesses import claude_code


# --- helpers ---------------------------------------------------------------------------------

def _endpoint(conn, name="qwen"):
    conn.execute("INSERT OR IGNORE INTO endpoints (name, base_url, backend, host)"
                 " VALUES (?, 'http://host:1/v1', 'llama.cpp', 'host')", (name,))
    return conn.execute("SELECT id FROM endpoints WHERE name = ?", (name,)).fetchone()[0]


def _model(conn, served_id="qwen3.8"):
    conn.execute("INSERT OR IGNORE INTO models (served_id) VALUES (?)", (served_id,))
    return conn.execute("SELECT id FROM models WHERE served_id = ?", (served_id,)).fetchone()[0]


def _row_job(conn, jid, *, endpoint="qwen", served_id="qwen3.8", status="succeeded",
             task="t", cwd="/nowhere", started_at=100.0, finished_at=200.0):
    """A jobs row on endpoint × served id; returns (endpoint_id, model_id)."""
    eid, mid = _endpoint(conn, endpoint), _model(conn, served_id)
    conn.execute(
        "INSERT INTO jobs (id, endpoint_id, model_id, task, cwd, isolation, status, created_at,"
        " started_at, finished_at) VALUES (?, ?, ?, ?, ?, 'none', ?, ?, ?, ?)",
        (jid, eid, mid, task, cwd, status, (started_at or 100.0) - 1.0, started_at, finished_at))
    return eid, mid


def _job(conn, jid, tmp_path, *, task="t", isolation="none", status="succeeded", error=None,
         result="done", session_id="sess", worktree=None, cwd=None, metrics=None):
    """An in-process Job bound to ``conn`` with its row written (the shape on_finish sees)."""
    cwd = str(cwd or tmp_path)
    eid, mid = _row_job(conn, jid, task=task, cwd=cwd, status=status)
    log_dir = tmp_path / "logs" / jid
    log_dir.mkdir(parents=True, exist_ok=True)
    j = Job(id=jid, task=task, model="qwen3.8", endpoint="qwen", base_url="http://host:1/v1",
            cwd=cwd, run_cwd=cwd, isolation=isolation, status=status, error=error, result=result,
            session_id=session_id, worktree=worktree, log_dir=str(log_dir), metrics=metrics,
            started_at=100.0, finished_at=200.0)
    j.endpoint_id, j.model_id = eid, mid
    j._conn = conn
    return j


def _tool(conn, jid, command, is_error, t=1.0, tool="Bash"):
    conn.execute("INSERT INTO tool_calls (job_id, t, tool, tool_use_id, input, is_error, finished)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (jid, t, tool, f"tu-{t}", command, is_error, t + 0.5 if is_error is not None else None))


def _diff(path, added, context_before=(), start=1):
    """A unified diff of one file: optional context lines, then the added lines."""
    body = [f" {line}" for line in context_before] + [f"+{line}" for line in added]
    n_old, n_new = len(context_before), len(context_before) + len(added)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -{start},{n_old} +{start},{n_new} @@\n" + "\n".join(body) + "\n")


def _res():
    ep = Endpoint(name="qwen", base_url="http://host:1/v1", backend="llama.cpp")
    return Resolution(ep, "qwen3.8")


def _router_reg():
    entry = Endpoint(name="auto", base_url="", backend="router",
                     decision_url="http://d3.test:8765", decision_model="d3-test",
                     decision_timeout_s=5.0, decision_max_tokens=100)
    return Registry(path=None, endpoints={"auto": entry}, defaults=Defaults(), raw={}, local={})


def _ticket(conn, jid, values, undetermined=(), defs_version=None):
    d = defs_version if defs_version is not None else definitions.load(conn).version
    conn.execute(
        "INSERT INTO router_tickets (job_id, defs_version, scores, rows, divert)"
        " VALUES (?, ?, ?, '[]', 0)",
        (jid, d, json.dumps({"values": values, "confidences": {},
                             "undetermined": list(undetermined), "truncated": False,
                             "defs_version": d})))


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "init")
    return r


CLEAN_BRIEF = "Edit src/a.py and tests/test_a.py, then run `uv run pytest -q`."


# --- the gates (§9) --------------------------------------------------------------------------

def test_gates_read_the_diff_and_the_record(conn, tmp_path):
    j = _job(conn, "j1", tmp_path, task=CLEAN_BRIEF, isolation="worktree")
    _tool(conn, "j1", "uv run pytest -q", 1, t=1.0)      # red first
    _tool(conn, "j1", "uv run pytest -q", 0, t=2.0)      # then green: the last run is the verdict
    names = ["src/a.py", "tests/test_a.py"]
    text = _diff("src/a.py", ["def f():", "    return 1"])
    brief = feedback.brief_files(CLEAN_BRIEF)

    g = feedback.gates(j, conn, names, text, brief)
    assert set(feedback.GATES) <= set(g)
    assert g == {"tests": True, "stop_rule": None, "not_capped": True, "no_tmp": True,
                 "files_within": True, "lint": True, "lint_detail": [], "files_n": 2}

    # a stray file outside the brief's list
    assert feedback.gates(j, conn, names + ["notes.txt"], text, brief)["files_within"] is False
    # /tmp on an added line
    tmp_text = _diff("src/a.py", ["OUT = '/tmp/x.log'"])
    assert feedback.gates(j, conn, names, tmp_text, brief)["no_tmp"] is False
    # capped: a timeout, or max_turns in the error
    j.status, j.error = "timeout", "exceeded timeout of 60s"
    assert feedback.gates(j, conn, names, text, brief)["not_capped"] is False
    j.status, j.error = "failed", "error_max_turns:"
    assert feedback.gates(j, conn, names, text, brief)["not_capped"] is False
    j.status, j.error = "succeeded", None
    # the report says it stopped: honoured only with an empty diff
    j.result = ("The brief names feedback.sessions, which does not exist, so I stopped rather"
                " than invent it.")
    assert feedback.gates(j, conn, [], "", brief)["stop_rule"] is True
    assert feedback.gates(j, conn, names, text, brief)["stop_rule"] is False
    # a normal end-of-brief report ("stopped after the last commit") claims no stop rule
    j.result = "All five steps committed; stopped after the final commit and reporting."
    assert feedback.gates(j, conn, names, text, brief)["stop_rule"] is None
    j.result = "done"
    # the last test run red; no test run at all; a brief that names no runner
    _tool(conn, "j1", "uv run pytest -q", 1, t=3.0)
    assert feedback.gates(j, conn, names, text, brief)["tests"] is False
    j2 = _job(conn, "j2", tmp_path, task=CLEAN_BRIEF, isolation="worktree")
    assert feedback.gates(j2, conn, names, text, brief)["tests"] is False
    j3 = _job(conn, "j3", tmp_path, task="Edit src/a.py.", isolation="worktree")
    assert feedback.gates(j3, conn, names, text, brief)["tests"] is None
    # a diff the finish could not read: every diff gate is null, never a pass
    g = feedback.gates(j, conn, None, None, brief)
    assert [g[k] for k in ("stop_rule", "no_tmp", "files_within", "lint", "files_n")] == [None] * 5
    # a brief that names no file: the file-set gate cannot be evaluated
    assert feedback.brief_files("Make the tests pass.") is None
    assert feedback.gates(j, conn, names, text, None)["files_within"] is None


def test_tests_gate_is_null_not_false_when_the_stored_command_was_truncated(conn, tmp_path):
    """Before 2026-11-15 the runner stored a Bash command cut to 120 chars
    (``derive.COMMAND_CHARS_BEFORE``), so a row at exactly that length may have lost the runner
    token to the cut (derive.command_may_be_cut; tests/test_command_cut.py pins the new cut). The rule: a tool_calls
    row whose stored command was truncated and shows no runner is NOT evidence of "no test
    run" — the ``tests`` gate is None for a job whose only candidate rows are truncated and
    runner-less (design §9: a gate that cannot be evaluated is null, not false); a truncated
    row that still shows the runner counts as a run. A stored row under the cut that shows no
    runner is the runner's full record: the job ran no test, the gate says False."""
    from localagents.derive import COMMAND_CHARS_BEFORE  # the old rows' cut

    # a 125-char command: the cut ends mid-token, the row reads "... uv run pyte"
    blind = f"cd {'w' * 102} && uv run pytest -q"
    stored = blind[:COMMAND_CHARS_BEFORE]
    assert len(blind) > COMMAND_CHARS_BEFORE
    assert len(stored) == COMMAND_CHARS_BEFORE and stored.endswith("uv run pyte")
    # a 121-char command: still truncated, but the cut keeps the whole runner token
    seen = f"cd {'w' * 98} && uv run pytest -q"
    kept = seen[:COMMAND_CHARS_BEFORE]
    assert len(kept) == COMMAND_CHARS_BEFORE and "pytest" in kept

    # the green run of the blind command: the row was cut, so no run is visible -> no verdict
    j1 = _job(conn, "jcut", tmp_path, task=CLEAN_BRIEF, isolation="worktree")
    _tool(conn, "jcut", stored, 0)
    assert feedback.gates(j1, conn, None, None, None)["tests"] is None

    # a red run of the blind command reads no differently: the row cannot say what ran
    j2 = _job(conn, "jcutred", tmp_path, task=CLEAN_BRIEF, isolation="worktree")
    _tool(conn, "jcutred", stored, 1)
    assert feedback.gates(j2, conn, None, None, None)["tests"] is None

    # the truncated row that still shows the runner: it is a run, and a green one
    j3 = _job(conn, "jseen", tmp_path, task=CLEAN_BRIEF, isolation="worktree")
    _tool(conn, "jseen", kept, 0)
    assert feedback.gates(j3, conn, None, None, None)["tests"] is True

    # unchanged: a complete row with no runner is the full record -> the job ran no test
    j4 = _job(conn, "jnone", tmp_path, task=CLEAN_BRIEF, isolation="worktree")
    _tool(conn, "jnone", "ls src", 0)
    assert feedback.gates(j4, conn, None, None, None)["tests"] is False


def test_lint_gate_finds_the_three_rules(conn, tmp_path):
    j = _job(conn, "j1", tmp_path, task=CLEAN_BRIEF, isolation="worktree")
    names = ["src/a.py"]
    brief = feedback.brief_files(CLEAN_BRIEF)

    def lint(text):
        return feedback.gates(j, conn, names, text, brief)["lint"]

    # constants after use, Python and JS
    assert lint(_diff("src/a.py", ["def f():", "    return LIMIT * 2", "", "LIMIT = 3"])) is False
    assert lint(_diff("src/a.js", ["function f() { return MAX_N; }", "const MAX_N = 3;"])) is False
    # a constant defined before its use is clean
    assert lint(_diff("src/a.py", ["LIMIT = 3", "", "def f():", "    return LIMIT * 2"])) is True
    # try{}catch(e){throw e}, JS and the Python shape
    assert lint(_diff("src/a.js", ["try { go(); } catch (e) { throw e; }"])) is False
    assert lint(_diff("src/a.js", ["try {", "  go();", "} catch (err) {", "  throw err;", "}"])) is False
    assert lint(_diff("src/a.py", ["try:", "    go()", "except ValueError as e:", "    raise e"])) is False
    assert lint(_diff("src/a.py", ["try:", "    go()", "except ValueError:", "    raise"])) is False
    # a bare re-raise that keeps a subclass out of the broader arm below adds something: clean
    assert lint(_diff("src/a.py", ["try:", "    go()", "except KeyError:", "    raise",
                                   "except Exception:", "    log()"])) is True
    # literal tabs; a Makefile's recipe tab is the format's, not a slip
    assert lint(_diff("src/a.py", ["def f():", "\treturn 1"])) is False
    assert lint(_diff("Makefile", ["all:", "\tmake it"])) is True
    # a comment that describes the shape is not the shape
    assert lint(_diff("src/a.js", ["// never write catch (e) { throw e; }"])) is True
    # removed lines are not the job's writing
    removed = ("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
               "@@ -1,1 +0,0 @@\n-\tx = 1\n")
    assert lint(removed) is True


def test_lint_detail_names_the_rule(conn, tmp_path):
    j = _job(conn, "j1", tmp_path, task=CLEAN_BRIEF, isolation="worktree")
    brief = feedback.brief_files(CLEAN_BRIEF)
    text = (_diff("src/a.py", ["def f():", "\treturn 1"], context_before=["# head"], start=4)
            + _diff("src/b.js", ["try { go(); } catch (e) { throw e; }"])
            + _diff("src/c.py", ["def g():", "    return CAP", "CAP = 1"]))
    g = feedback.gates(j, conn, ["src/a.py", "src/b.js", "src/c.py"], text, brief)
    assert g["lint"] is False
    assert g["lint_detail"] == [
        {"rule": "literal_tab", "file": "src/a.py", "line": 6},
        {"rule": "rethrow_wrapper", "file": "src/b.js", "line": 1},
        {"rule": "constant_after_use", "file": "src/c.py", "line": 3},
    ]
    assert {d["rule"] for d in g["lint_detail"]} == set(feedback.LINT_RULES)


def test_sessions_at_start_counts_running_jobs_on_the_endpoint(conn):
    _row_job(conn, "a1", endpoint="qwen", status="running", started_at=50.0, finished_at=None)
    _row_job(conn, "a2", endpoint="qwen", status="running", started_at=90.0, finished_at=None)
    _row_job(conn, "a3", endpoint="qwen", status="queued", started_at=None, finished_at=None)
    _row_job(conn, "a4", endpoint="qwen", status="succeeded")
    _row_job(conn, "b1", endpoint="deep", status="running", started_at=60.0, finished_at=None)
    qwen = _endpoint(conn, "qwen")
    assert feedback.sessions_at_start(conn, qwen, 100.0) == 2
    assert feedback.sessions_at_start(conn, _endpoint(conn, "deep"), 100.0) == 1
    assert feedback.sessions_at_start(conn, _endpoint(conn, "empty"), 100.0) == 0


# --- on_finish (§9: worktree jobs only for the diff gates; evidence only) --------------------

def test_on_finish_writes_gates_and_a_speed_sample(conn, tmp_path, repo):
    from localagents.runner import make_worktree

    j = _job(conn, "j1", tmp_path, task="Write src/new.py and b.txt; run pytest -q.",
             isolation="worktree", cwd=repo, metrics={"generate_tps": 31.5})
    j.worktree = make_worktree(j)
    root = Path(j.worktree["root"])
    (root / "src").mkdir()
    (root / "src" / "new.py").write_text("X = 1\n")       # untracked: the diff must still see it
    (root / "b.txt").write_text("b\n")
    git(root, "add", "b.txt")
    git(root, "commit", "-q", "-m", "b")                 # committed: the diff is against base
    _tool(conn, "j1", "pytest -q", 0)

    asyncio.run(feedback.on_finish(j, _res(), sessions=2))

    stored = json.loads(conn.execute("SELECT gates FROM jobs WHERE id = 'j1'").fetchone()[0])
    assert stored == j.gates
    assert stored["files_n"] == 2 and stored["files_within"] is True
    assert stored["tests"] is True and stored["no_tmp"] is True and stored["lint"] is True
    s = conn.execute("SELECT * FROM speed_samples WHERE job_id = 'j1'").fetchone()
    # curve-refine moved this pin: the sample's concurrency is the occupancy the run held (it
    # ran alone), not the count passed in from its start
    assert (s["endpoint_id"], s["sessions"], s["generate_tps"]) == (j.endpoint_id, 1, 31.5)
    # evidence only: the job's own fields are as they were
    assert (j.status, j.error) == ("succeeded", None)
    # the gate read a scratch index: the tree's own index still has src/new.py untracked
    assert "?? src/" in git(root, "status", "--porcelain")
    # and the scorecard reads the shape back: one completeness and one adherence observation
    row = scorecard.get_or_create_row(conn, "qwen", "qwen3.8", j.endpoint_id, j.model_id)
    dims = sorted(o.dimension for o in scorecard.observations(conn, row.id))
    assert dims == ["adherence", "completeness"]


def test_on_finish_skips_diff_gates_for_non_worktree_jobs(conn, tmp_path):
    j = _job(conn, "j1", tmp_path, task=CLEAN_BRIEF, isolation="none",
             metrics={"generate_tps": 12.0})
    _tool(conn, "j1", "uv run pytest -q", 0)
    asyncio.run(feedback.on_finish(j, _res(), sessions=1))
    g = json.loads(conn.execute("SELECT gates FROM jobs WHERE id = 'j1'").fetchone()[0])
    assert g["tests"] is True and g["not_capped"] is True
    assert [g[k] for k in ("stop_rule", "no_tmp", "files_within", "lint", "files_n")] == [None] * 5
    assert conn.execute("SELECT sessions FROM speed_samples WHERE job_id = 'j1'").fetchone()[0] == 1

    # a job that never reached a session is no evidence about the row: no gates
    k = _job(conn, "k1", tmp_path, task=CLEAN_BRIEF, session_id=None, status="failed",
             error="ConnectError: refused")
    asyncio.run(feedback.on_finish(k, _res(), sessions=1))
    assert conn.execute("SELECT gates FROM jobs WHERE id = 'k1'").fetchone()[0] is None
    # no start count, no measured speed: no sample is guessed
    m = _job(conn, "m1", tmp_path, task=CLEAN_BRIEF, metrics=None)
    asyncio.run(feedback.on_finish(m, _res(), sessions=None))
    assert conn.execute("SELECT 1 FROM speed_samples WHERE job_id = 'm1'").fetchone() is None


def test_on_finish_failure_never_fails_the_job(conn, tmp_path, monkeypatch):
    # a broken store under on_finish: logged, returned, the job untouched
    j = _job(conn, "j1", tmp_path, task=CLEAN_BRIEF, metrics={"generate_tps": 5.0})
    broken = __import__("sqlite3").connect(":memory:")
    broken.close()
    j._conn = broken
    asyncio.run(feedback.on_finish(j, _res(), sessions=1))
    assert (j.status, j.error) == ("succeeded", None)
    assert "[feedback-error]" in (Path(j.log_dir) / "transcript.txt").read_text()

    # wired through run_job: an on_finish that raises leaves the status and error as they were
    from localagents import runner
    from localagents.jobs import JobStore

    async def no_metrics(*a, **kw):
        return None

    def fake_query(prompt, options):
        async def nothing():
            return
            yield

        return nothing()

    async def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "fetch_metrics", no_metrics)
    monkeypatch.setattr(claude_code, "query", fake_query)
    monkeypatch.setattr(feedback, "_on_finish", boom)
    ep = Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")
    reg = Registry(path=None, endpoints={"e": ep}, defaults=Defaults(job_retention_days=0),
                   raw={}, local={})
    store = JobStore()
    sj = store.new(task="t", model="served-model", endpoint="e", base_url="http://e:8080",
                   cwd=str(tmp_path), run_cwd=str(tmp_path), isolation="none",
                   backend="llama.cpp", host="e")
    asyncio.run(runner.run_job(
        sj, reg, Resolution(ep, "served-model"), permission_mode=None, max_turns=None,
        allowed_tools=None, system_prompt_append=None, resume_session=None, timeout_s=10,
        shim_base_url="http://127.0.0.1:1/job/x"))
    assert sj.status == "failed" and sj.error == "session ended without a result message"
    row = store.conn.execute("SELECT status, error FROM jobs WHERE id = ?", (sj.id,)).fetchone()
    assert (row["status"], row["error"]) == ("failed", "session ended without a result message")
    assert "[feedback-error] RuntimeError: boom" in sj.transcript_tail(0)
    store.close()


# --- the verdict (§9, §11) -------------------------------------------------------------------

def test_verdict_upserts(conn):
    _row_job(conn, "j1")
    _ticket(conn, "j1", {"complexity": 2, "tool_use": 1})
    out = asyncio.run(feedback.apply_verdict(conn, _router_reg(), "j1", True))
    assert out["recorded"] is True and isinstance(out["notices"], list)
    out = asyncio.run(feedback.apply_verdict(conn, _router_reg(), "j1", False,
                                             dimensions={"complexity": 1}))
    assert out["recorded"] is True
    rows = conn.execute("SELECT accepted, dimensions FROM verdicts WHERE job_id = 'j1'").fetchall()
    assert len(rows) == 1
    assert rows[0]["accepted"] == 0 and json.loads(rows[0]["dimensions"]) == {"complexity": 1}
    # refusals answer a dict and write nothing
    assert asyncio.run(feedback.apply_verdict(conn, _router_reg(), "nope", True))["error"] == "unknown_job"
    bad = asyncio.run(feedback.apply_verdict(conn, _router_reg(), "j1", True,
                                             dimensions={"complexity": 2}))
    assert bad["error"] == "bad_argument"
    bad = asyncio.run(feedback.apply_verdict(conn, _router_reg(), "j1", True,
                                             dimensions={"colour": 1}))
    assert bad["error"] == "bad_argument"
    _row_job(conn, "r1", status="running", finished_at=None)
    _ticket(conn, "r1", {"complexity": 2})
    assert asyncio.run(feedback.apply_verdict(conn, _router_reg(), "r1", True))["error"] == "job_not_finished"
    assert conn.execute("SELECT accepted FROM verdicts WHERE job_id = 'j1'").fetchone()[0] == 0


def test_verdict_on_named_job_scores_now(conn, tmp_path, monkeypatch):
    (tmp_path / "brief.md").write_text("Refactor the parser across three modules.")
    _row_job(conn, "j1", task="cat brief.md and follow it", cwd=str(tmp_path))
    row = scorecard.get_or_create_row(conn, "qwen", "qwen3.8", _endpoint(conn), _model(conn))
    seen = {}

    async def fake_score(client, url, model, text, defs, **kw):
        seen["text"], seen["url"] = text, url
        return BriefScores(values={"complexity": 3, "tool_use": 2},
                           confidences={"complexity": 0.9, "tool_use": 0.8, "computer_use": 0.1},
                           undetermined={"computer_use"}, truncated=False, raw={},
                           defs_version=defs.version)

    monkeypatch.setattr(feedback, "score", fake_score)
    out = asyncio.run(feedback.apply_verdict(conn, _router_reg(), "j1", True))
    assert out["recorded"] is True
    assert seen == {"text": "Refactor the parser across three modules.", "url": "http://d3.test:8765"}
    t = conn.execute("SELECT * FROM router_tickets WHERE job_id = 'j1'").fetchone()
    assert json.loads(t["flags"]) == {"scored_at_verdict": True}
    assert t["chosen_row"] == row.id and t["defs_version"] == definitions.load(conn).version
    scores = json.loads(t["scores"])
    assert scores["values"] == {"complexity": 3, "tool_use": 2}
    assert scores["undetermined"] == ["computer_use"]
    obs = {(o.dimension, o.band, o.accepted) for o in scorecard.observations(conn, row.id)}
    assert obs == {("complexity", 3, True), ("tool_use", 2, True)}

    # a second verdict reads the ticket it wrote: d3 is not asked again
    async def no_score(*a, **kw):
        raise AssertionError("scored twice")

    monkeypatch.setattr(feedback, "score", no_score)
    assert asyncio.run(feedback.apply_verdict(conn, _router_reg(), "j1", False))["recorded"] is True

    # no router entry to score with: refused, nothing written
    _row_job(conn, "j2", task="plain brief")
    empty = Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={})
    assert asyncio.run(feedback.apply_verdict(conn, empty, "j2", True))["error"] == "no_router_entry"
    assert conn.execute("SELECT 1 FROM verdicts WHERE job_id = 'j2'").fetchone() is None


def test_verdict_adds_no_observation_for_undetermined_axes(conn):
    _row_job(conn, "j1")
    row = scorecard.get_or_create_row(conn, "qwen", "qwen3.8", _endpoint(conn), _model(conn))
    _ticket(conn, "j1", {"tool_use": 2}, undetermined=["complexity"])
    out = asyncio.run(feedback.apply_verdict(conn, _router_reg(), "j1", True,
                                             dimensions={"complexity": 1, "tool_use": -1}))
    assert out["recorded"] is True and out["no_observation"] == ["complexity"]
    obs = [(o.dimension, o.band, o.accepted) for o in scorecard.observations(conn, row.id)]
    assert obs == [("tool_use", 2, False)]


def test_correction_is_logged_with_defs_version(conn, tmp_path):
    (tmp_path / "brief.md").write_text("Rename one function.")
    _row_job(conn, "j1", task="cat brief.md and follow it", cwd=str(tmp_path))
    definitions.load(conn)                                        # seeds version 1
    _ticket(conn, "j1", {"complexity": 3, "language": "python"}, defs_version=1)
    definitions.save(conn, definitions.DEFAULT_DOCUMENT)          # the current version moves to 2
    out = asyncio.run(feedback.apply_verdict(conn, _router_reg(), "j1", False,
                                             correction={"complexity": 1}))
    assert out["recorded"] is True
    c = conn.execute("SELECT * FROM category_corrections WHERE job_id = 'j1'").fetchone()
    assert c["defs_version"] == 1                                 # the ticket's, not the current
    assert c["brief"] == "Rename one function."
    assert json.loads(c["scored"])["values"] == {"complexity": 3, "language": "python"}
    assert json.loads(c["corrected"]) == {"complexity": 1}
    # a correction outside the document's answers is refused and writes nothing
    for bad in ({"complexity": 7}, {"language": "cobol"}, {"colour": 1}, {"complexity": True}):
        got = asyncio.run(feedback.apply_verdict(conn, _router_reg(), "j1", True, correction=bad))
        assert got["error"] == "bad_argument", bad
    assert conn.execute("SELECT COUNT(*) FROM category_corrections").fetchone()[0] == 1
    assert conn.execute("SELECT accepted FROM verdicts WHERE job_id = 'j1'").fetchone()[0] == 0


def test_verdict_tool_answers_dicts(conn, monkeypatch):
    from localagents.router import mcp as router

    monkeypatch.setattr(router, "_db", lambda: conn)
    reg = _router_reg()
    monkeypatch.setattr(Registry, "load", staticmethod(lambda path=None: reg))
    _row_job(conn, "j1")
    _ticket(conn, "j1", {"complexity": 2})
    out = asyncio.run(router.verdict(job_id="j1", accepted=True, dimensions={"complexity": 1}))
    assert out["recorded"] is True
    assert conn.execute("SELECT accepted FROM verdicts WHERE job_id = 'j1'").fetchone()[0] == 1
    assert asyncio.run(router.verdict(job_id="nope", accepted=True))["error"] == "unknown_job"
