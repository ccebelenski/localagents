"""The read share on the job record (consumer coordinator, 2026-11-15: "I would use read share
and output tokens per tool call on the job record ... compare briefs"): derive.classify_call
places each tool call as read / edit / other / unclassified from its stored input, and
derive.tool_mix carries the counts, ``read_share`` and ``output_tokens_per_call`` in both
summaries.

The rule's sentences, each pinned here: a field never claims more than it knows (a command
that may have been cut is never a read; what the classifier cannot place is counted and
shown, never folded into a bucket); the classifier is one function with a closed rule, tested
against CAPTURED command lines (tests/fixtures/bash_reads.json, pulled read-only from the real
store and hand-labelled), never typed ones; a job with no tool calls shows null ratios; a
running job carries the mix; both summaries agree."""

import json
import time
from pathlib import Path

import pytest

from localagents import derive, jobs, runner

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "bash_reads.json").read_text())
ROWS = FIXTURE["rows"]

# hand label vs classifier, accepted with the reason (the only disagreement in the fixture):
# the line is cut, so a reader cannot tell whether it wrote; the classifier still says other
# because `sqlite3` without -readonly is a program that can write and its SQL is not parsed.
ACCEPTED = {
    "sqlite3 t/plan.db \"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;\" && echo --- && for t in $(sqlite3 t":
        ("unclassified", "other"),
}


def _by_edge(edge):
    rows = [r for r in ROWS if r["edge"] == edge]
    assert rows, f"the fixture has no captured row for edge {edge!r}"
    return rows


def test_the_classifier_agrees_with_every_hand_label_but_the_accepted_one():
    assert len(ROWS) >= 60
    misses = {}
    for r in ROWS:
        got = derive.classify_call("Bash", r["input"])
        if got != r["label"]:
            misses[r["input"]] = (r["label"], got)
    assert misses == ACCEPTED
    assert sum(1 for r in ROWS if r["label"] == "unclassified") >= 20  # the cut edge is covered


@pytest.mark.parametrize("edge,label", [
    ("cd-and", "read"),               # compound `cd x && grep ...`: read when whole, unclassified when cut
    ("pipe-write", "other"),          # a pipeline ending in a write (`> t/s`, even cut mid-name)
    ("tee", "other"),
    ("short-redirect-file", "other"),
    ("cat-heredoc-write", "other"),   # `cat > file <<EOF` is a WRITE
    ("sed-i", "other"),
    ("export-prefix", "other"),       # `export TMPDIR=...; npm test`
    ("env-assign-prefix", "other"),
    ("var-semicolon-prefix", "read"), # `R=...; grep ...`: the assignment is neutral
    ("test-run", "other"),
    ("python-heredoc", "other"),      # a program ran, even one that only read
    ("cmd-subst", "read"),            # echo "$(git log ...)": the substitution is judged too
    ("backtick", "read"),             # a backtick inside quotes is not a substitution to run
    ("awk", "read"),                  # `>` inside awk's quoted program is not a redirection
    ("git-write", "other"),
    ("git-read", "read"),
    ("git-branch", "read"),
    ("xargs", "read"),                # `git merge-base ... | xargs git log`
    ("find-exec", "other"),
    ("for-loop", "read"),             # for/do/done around grep in $(...)
    ("dev-null", "read"),
    ("truncated-read-prefix", "unclassified"),  # a cut read; a cut line showing python3 is other
])
def test_each_edge_classifies_as_hand_labelled(edge, label):
    """Every captured row of the edge gets its hand label; where the edge names a label, some
    row of the edge carries it (a check that the fixture holds what the edge is about)."""
    rows = _by_edge(edge)
    if label is not None:
        assert any(r["label"] == label for r in rows), rows
    for r in rows:
        assert derive.classify_call("Bash", r["input"]) == r["label"], r


def test_a_command_that_may_have_been_cut_is_never_a_read():
    """A stored command at the old cut (derive.COMMAND_CHARS_BEFORE, every row written before
    2026-11-15) may have lost a write past the cut: never read, over every captured line in the
    fixture (labelled and unlabelled)."""
    cut = [s for s in [r["input"] for r in ROWS] + FIXTURE["unlabelled"] if len(s) == derive.COMMAND_CHARS_BEFORE]
    assert len(cut) >= 150
    assert [s for s in cut if derive.classify_call("Bash", s) == "read"] == []
    # a cut line that visibly runs a program is other: the cut hides only what follows it
    assert {derive.classify_call("Bash", r["input"]) for r in _by_edge("truncated-read-prefix")} == {"unclassified", "other"}


def test_every_unlabelled_capture_classifies_without_raising():
    kinds = {derive.classify_call("Bash", s) for s in FIXTURE["unlabelled"]}
    assert kinds == {"read", "other", "unclassified"}


def test_the_tool_level_rule():
    """The closed rule's non-Bash half: the edit tools (derive.EDIT_TOOLS, files_touched's owner)
    are edits, the reading tools reads, every other tool other; a Bash call with no stored
    command is unclassified."""
    for tool in derive.EDIT_TOOLS:
        assert derive.classify_call(tool, "/a.py") == "edit"
        assert derive.classify_call(tool, None) == "edit"
    for tool in ("Read", "Grep", "Glob"):
        assert derive.classify_call(tool, "/a.py") == "read"
    for tool in ("TaskUpdate", "TaskCreate", "TaskOutput", "mcp__localagents__job_status", "ToolSearch"):
        assert derive.classify_call(tool, '{"taskId": "2"}') == "other"
    assert derive.classify_call("Bash", None) == "unclassified"
    assert derive.classify_call("Bash", "") == "unclassified"


def test_the_input_cut_has_one_owner():
    """runner writes the cuts, feedback and classify_call read them back (through
    derive.command_may_be_cut), last_tool and last_activity show them (derive.activity_line):
    the constants are derive's, runner's names are the same objects."""
    assert runner.TOOL_INPUT_CHARS is derive.TOOL_INPUT_CHARS
    assert runner.COMMAND_CHARS is derive.COMMAND_CHARS
    assert runner.activity_line is derive.activity_line


# --- the record --------------------------------------------------------------------------------

def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return jobs.JobStore()


def _new(store):
    return store.new(task="the brief", model="served-model", endpoint="llamacpp",
                     base_url="http://x:8080", cwd="/tmp", run_cwd="/tmp", isolation="none",
                     backend="llama.cpp", host="local")


def _captured(edge, label):
    return next(r["input"] for r in _by_edge(edge) if r["label"] == label)


def _calls(job, t):
    """Five calls from captured inputs: a Read, an Edit, a Bash read, a Bash write, a cut Bash
    line; then a request with 1000 output tokens."""
    rows = [("Read", "/a.py"), ("Edit", "/a.py"), ("Bash", _captured("git-read", "read")),
            ("Bash", _captured("sed-i", "other")), ("Bash", _captured("truncated-read-prefix", "unclassified"))]
    for k, (tool, inp) in enumerate(rows):
        job.record_tool_call(t + k, tool, f"u{k}", inp)
    job.record_request({"t": t + 9, "path": "/v1/messages", "status": 200, "ms": 100,
                        "usage": {"input_tokens": 10, "output_tokens": 1000}})


def _row(store, job):
    return store.conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()


def test_a_job_with_no_tool_calls_shows_null_ratios_in_both_summaries(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    job.record_request({"t": time.time(), "path": "/v1/messages", "status": 200, "ms": 10,
                        "usage": {"output_tokens": 50}})  # output but no calls: no division
    empty = {"reads": 0, "edits": 0, "other": 0, "unclassified": 0,
             "read_share": None, "output_tokens_per_call": None}
    assert job.summary()["tool_mix"] == empty
    assert derive.summary_from_row(store.conn, _row(store, job))["tool_mix"] == empty
    # a Job built outside JobStore.new has no rows: the same shape, empty
    assert jobs.Job(id="x", task="t", model="m", endpoint="e", base_url="u", cwd="/",
                    run_cwd="/", isolation="none").summary()["tool_mix"] == empty


def test_unclassified_calls_are_shown_and_read_share_is_reads_over_all_calls(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    _calls(job, time.time())
    assert job.summary()["tool_mix"] == {
        "reads": 2, "edits": 1, "other": 1, "unclassified": 1,
        "read_share": 0.4,                 # 2 of 5: the cut line is not counted as a read
        "output_tokens_per_call": 200.0,   # 1000 over 5 calls (usage_totals' figure)
    }


def test_a_running_job_carries_the_mix_and_both_summaries_agree(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    job.status, job.started_at = "running", time.time()
    job.changed()
    _calls(job, job.started_at)
    live = job.summary()["tool_mix"]
    assert live["reads"] == 2 and live["read_share"] == 0.4
    assert derive.summary_from_row(store.conn, _row(store, job))["tool_mix"] == live


def test_a_finished_job_both_summaries_agree(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    job.status, job.started_at, job.finished_at = "succeeded", 1000.0, 1060.0
    job.changed()
    _calls(job, 1001.0)
    row = _row(store, job)
    assert job.summary() == derive.summary_from_row(store.conn, row)
    assert job.summary()["tool_mix"]["output_tokens_per_call"] == 200.0
