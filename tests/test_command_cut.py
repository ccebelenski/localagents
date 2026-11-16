"""The stored cut and the displayed cut are two facts (read share fold, 2026-11-15): a Bash
call's ``tool_calls.input`` stores the command up to ``derive.COMMAND_CHARS``, so the read
share and the tests gate see whole commands; everything that SHOWS that text to a reader
(last_activity, last_tool, the progress lines, job_log's tool list, the monitor's
tool_call_log) keeps today's form, the first ``derive.TOOL_DISPLAY_CHARS`` characters.

"May have been cut" is one rule with one owner (``derive.command_may_be_cut``) and stays true
for the rows already in the store: a row written before the change was cut at
``derive.COMMAND_CHARS_BEFORE`` (120), one written after at ``COMMAND_CHARS``, and a row cannot
say which it is (no schema change), so the rule is by length. Its cost, pinned here: a new
command exactly 120 characters long reads as possibly cut.

The commands are CAPTURED: tests/fixtures/bash_reads.json's ``whole`` rows, recovered whole
from the store's transcripts."""

import asyncio
import json
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ToolUseBlock

from localagents import derive, jobs, runner, server
from localagents.monitor import read
from localagents.router import feedback

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "bash_reads.json").read_text())
WHOLE = FIXTURE["whole"]
LONG_READS = [r["input"] for r in WHOLE if r["label"] == "read" and len(r["input"]) > 200]
# the whole test run whose runner sits past the old cut
LONG_RUN = next(r["input"] for r in WHOLE if "uv run pytest" in r["input"])
# a whole read of exactly the old cut's length
READ_120 = next(r["input"] for r in WHOLE if len(r["input"]) == 120)


def _store():
    return jobs.JobStore()  # conftest points XDG_STATE_HOME at this test's dir


def _new(store):
    return store.new(task="Run uv run pytest -q.", model="served-model", endpoint="llamacpp",
                     base_url="http://x:8080", cwd="/tmp", run_cwd="/tmp", isolation="none",
                     backend="llama.cpp", host="local")


def _bash(job, command, uid):
    """The runner's write path: one Bash ToolUseBlock through runner._apply_message."""
    runner._apply_message(job, AssistantMessage(content=[
        ToolUseBlock(id=uid, name="Bash", input={"command": command, "description": "x"})], model="m"))


def _stored(store, uid):
    return store.conn.execute("SELECT input FROM tool_calls WHERE tool_use_id = ?", (uid,)).fetchone()[0]


def test_the_captures_are_what_the_tests_say():
    assert len(LONG_READS) >= 3 and all(len(c) > 200 for c in LONG_READS)
    assert LONG_RUN.index("pytest") > 120  # the old cut lost the runner token
    assert len(READ_120) == 120


def test_a_long_read_stored_by_the_runner_is_whole_and_classified_read():
    store = _store()
    job = _new(store)
    for k, command in enumerate(LONG_READS):
        _bash(job, command, f"u{k}")
        assert _stored(store, f"u{k}") == command
        assert derive.classify_call("Bash", _stored(store, f"u{k}")) == "read"
    mix = job.summary()["tool_mix"]
    assert mix["reads"] == len(LONG_READS) and mix["unclassified"] == 0


def test_what_a_reader_is_shown_keeps_the_120_character_form():
    """last_activity, last_tool (job_status and the progress lines), job_log's tool list and the
    monitor's tool_call_log show the first 120 characters, exactly as before the store grew."""
    store = _store()
    job = _new(store)
    command = LONG_READS[0]
    _bash(job, command, "u1")
    shown = "Bash: " + command[:120]
    assert job.last_activity == shown
    assert derive.last_tool(store.conn, job.id) == shown
    assert job.summary()["last_tool"] == shown
    job.changed()
    log = asyncio.run(server.job_log(job.id, tools=1))
    assert log["tool_calls"][0]["input"] == command[:120]
    detail = read.job_detail(store.conn, job.id)
    assert detail["tool_call_log"][0]["input"] == command[:120]
    assert detail["last_tool"] == shown


def test_a_stored_command_is_cut_at_the_new_cut_and_reads_as_possibly_cut():
    store = _store()
    job = _new(store)
    command = "; ".join(LONG_READS * 10)  # captured reads chained past the cut
    assert len(command) > derive.COMMAND_CHARS
    _bash(job, command, "u1")
    stored = _stored(store, "u1")
    assert stored == command[:derive.COMMAND_CHARS]
    assert derive.command_may_be_cut(stored)
    assert derive.classify_call("Bash", stored) == "unclassified"
    assert job.last_activity == "Bash: " + command[:120]


def test_an_old_row_at_the_old_cut_classifies_as_it_did():
    """Rows written before the change are cut at 120: every captured one still may be cut, and
    the fixture's hand labels still hold (the agreement test in test_read_share.py too)."""
    old = [r for r in FIXTURE["rows"] if len(r["input"]) == derive.COMMAND_CHARS_BEFORE]
    assert len(old) >= 30
    for r in old:
        assert derive.command_may_be_cut(r["input"])
        assert derive.classify_call("Bash", r["input"]) != "read"  # a cut line is never a read


def test_the_cost_a_new_command_of_exactly_120_reads_as_possibly_cut():
    """A row cannot say when it was written, so a whole command that happens to be exactly
    120 characters long is counted unclassified, never read: the rule's price for staying
    true on the old rows. A command one character shorter or longer is placed."""
    store = _store()
    job = _new(store)
    _bash(job, READ_120, "u1")
    assert _stored(store, "u1") == READ_120
    assert derive.command_may_be_cut(READ_120)
    assert derive.classify_call("Bash", READ_120) == "unclassified"
    assert not derive.command_may_be_cut(READ_120[:119])
    assert not derive.command_may_be_cut(READ_120 + " ")
    assert derive.classify_call("Bash", READ_120 + " ") == "read"


def test_the_tests_gate_sees_a_whole_test_command_past_the_old_cut():
    """feedback._tests_gate reads the same fact through the same owner: a test run whose
    runner sits past character 120 is now a run it can judge (it used to be null: the cut row
    showed no runner); the same command cut at the old 120 still reads null."""
    store = _store()
    job = _new(store)
    _bash(job, LONG_RUN, "u1")
    store.conn.execute("UPDATE tool_calls SET is_error = 0, finished = t WHERE tool_use_id = 'u1'")
    assert feedback._tests_gate(store.conn, job.id, job.task) is True
    old = _new(store)
    old.record_tool_call(1.0, "Bash", "u2", LONG_RUN[:derive.COMMAND_CHARS_BEFORE])
    old.record_tool_result("u2", 2.0, False)
    assert feedback._tests_gate(store.conn, old.id, old.task) is None
