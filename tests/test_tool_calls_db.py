"""Story 1.4: the tool_calls write path (INSERT per ToolUseBlock, finished/is_error per matching
ToolResultBlock, tool_use_id -> rowid kept in memory). Story 1.5b removed the in-memory
counters: the rows are the record, and the readers use derive.

There is no captured SDK stream to replay, so the message sequence is built directly from the
claude_agent_sdk dataclasses (AssistantMessage / UserMessage / ToolUseBlock / ToolResultBlock,
the same ones runner.py imports) — the honest substitute for real stream capture.
"""

import json
import sqlite3
from contextlib import contextmanager

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

from localagents import derive, jobs, runner


def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return jobs.JobStore()


def _new(store, **kw):
    kw.setdefault("task", "the brief")
    kw.setdefault("model", "served-model")
    kw.setdefault("endpoint", "llamacpp")
    kw.setdefault("base_url", "http://x:8080")
    kw.setdefault("backend", "llama.cpp")
    kw.setdefault("host", "local")
    kw.setdefault("cwd", "/tmp")
    kw.setdefault("run_cwd", "/tmp")
    kw.setdefault("isolation", "none")
    return store.new(**kw)


class _Clock:
    """Stand-in for the time module inside runner: one readable value per message."""

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now


def _messages():
    bash_input = {"command": "uv run pytest -q " + "x" * 200}
    return [
        AssistantMessage(content=[
            TextBlock(text="working"),
            ToolUseBlock(id="tu-read", name="Read", input={"file_path": "/a.py"}),
            ToolUseBlock(id="tu-bash", name="Bash", input=bash_input),
        ], model="m"),
        UserMessage(content=[
            ToolResultBlock(tool_use_id="tu-read", content="ok"),
            ToolResultBlock(tool_use_id="tu-bash", content="boom", is_error=True),
        ]),
        AssistantMessage(content=[
            ToolUseBlock(id="tu-edit", name="Edit",
                         input={"file_path": "/a.py", "old_string": "a", "new_string": "b"}),
            ToolUseBlock(id="tu-write", name="Write", input={"content": "no file_path at all"}),
        ], model="m"),
        UserMessage(content=[
            ToolResultBlock(tool_use_id="tu-edit", content="ok"),
            ToolResultBlock(tool_use_id="tu-write", content="ok"),
        ]),
    ]


def _feed(store, monkeypatch, job, times):
    """runner._apply_message per message, with runner's time.time pinned to ``times[i]``."""
    clock = _Clock()
    monkeypatch.setattr(runner, "time", clock)
    for m, t in zip(_messages(), times):
        clock.now = t
        runner._apply_message(job, m)


def _rows(store, job):
    return store.conn.execute(
        "SELECT * FROM tool_calls WHERE job_id = ? ORDER BY rowid", (job.id,)
    ).fetchall()


def _row_vals(store, job):
    # sqlite3.Row does not compare by value; tuples do, so change tests can compare snapshots
    return [tuple(r) for r in _rows(store, job)]


def test_tool_use_rows(monkeypatch, tmp_path):
    """One row per ToolUseBlock in t order; input per the spec: edit tools store the path
    (NULL when absent), every other tool the first 120 chars of the input JSON."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    _feed(store, monkeypatch, job, [1000.0, 1001.0, 1002.0, 1003.0])
    rows = _rows(store, job)
    assert [r["tool"] for r in rows] == ["Read", "Bash", "Edit", "Write"]
    assert [r["t"] for r in rows] == [1000.0, 1000.0, 1002.0, 1002.0]
    assert [r["tool_use_id"] for r in rows] == ["tu-read", "tu-bash", "tu-edit", "tu-write"]
    # Read is not an edit tool: input is its JSON (shorter than 120 chars, so the whole thing)
    assert rows[0]["input"] == json.dumps({"file_path": "/a.py"})[:120]
    # Bash: exactly the first 120 chars of its input JSON
    assert rows[1]["input"] == json.dumps({"command": "uv run pytest -q " + "x" * 200})[:120]
    assert len(rows[1]["input"]) == 120
    # edit tools: the path, or NULL when the input has none
    assert rows[2]["input"] == "/a.py"
    assert rows[3]["input"] is None


def test_results_set_finished_and_is_error(monkeypatch, tmp_path):
    """The matching ToolResultBlock sets finished/is_error on its call's row, by the rowid
    remembered when the call was inserted."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    _feed(store, monkeypatch, job, [1000.0, 1001.0, 1002.0, 1003.0])
    rows = {r["tool"]: r for r in _rows(store, job)}
    assert rows["Read"]["finished"] == 1001.0 and rows["Read"]["is_error"] == 0
    assert rows["Bash"]["finished"] == 1001.0 and rows["Bash"]["is_error"] == 1
    assert rows["Edit"]["finished"] == 1003.0 and rows["Edit"]["is_error"] == 0
    assert rows["Write"]["finished"] == 1003.0 and rows["Write"]["is_error"] == 0


def test_orphan_result_is_ignored(monkeypatch, tmp_path):
    """A result for a call we never saw changes nothing: no row, no touch of existing rows,
    no exception, no [db-error] line."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    _feed(store, monkeypatch, job, [1000.0, 1001.0, 1002.0, 1003.0])
    before = _row_vals(store, job)
    runner._apply_message(job, UserMessage(content=[
        ToolResultBlock(tool_use_id="tu-never-sent", content="who dis", is_error=True),
    ]))
    assert _row_vals(store, job) == before
    assert "[db-error]" not in job.transcript_tail()


def test_result_message_usage_stays_off_the_job(monkeypatch, tmp_path):
    """Story 1.5b: usage is the sum of the request rows; the SDK ResultMessage usage is no
    longer parked on the Job (it counts the same traffic from the other side and differs)."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    runner._apply_message(job, ResultMessage(
        subtype="success", duration_ms=5, duration_api_ms=4, is_error=False, num_turns=2,
        session_id="sess-1", usage={"input_tokens": 999, "output_tokens": 7}, result="done",
    ))
    assert not hasattr(job, "usage")
    assert job.result == "done" and job.num_turns == 2 and job.session_id == "sess-1"


def test_counts_and_files_touched_come_from_the_rows(monkeypatch, tmp_path):
    """Story 1.5b: the in-memory counters are gone; the rows are the record and derive
    reproduces exactly the values they held."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    _feed(store, monkeypatch, job, [1000.0, 1001.0, 1002.0, 1003.0])
    assert derive.tool_call_count(store.conn, job.id) == 4
    assert derive.files_touched(store.conn, job.id) == ["/a.py"]  # Edit repeats the path; dedup keeps first seen


def test_db_error_does_not_break_tool_recording(monkeypatch, tmp_path):
    """A locked or broken database must never fail the job: same approach as story 1.2's
    test_db_error_does_not_break_changed — db.immediate raises, the message is handled
    normally and only a [db-error] line appears in the transcript."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)

    @contextmanager
    def boom(conn):
        raise sqlite3.OperationalError("database is locked")
        yield  # unreachable; makes this a context manager

    monkeypatch.setattr(jobs.db, "immediate", boom)
    runner._apply_message(job, AssistantMessage(content=[
        ToolUseBlock(id="tu-boom", name="Bash", input={"command": "ls"}),
    ], model="m"))
    assert _row_vals(store, job) == []  # nothing inserted, no pending rowid remembered
    assert derive.tool_call_count(store.conn, job.id) == 0  # and the readers see nothing
    assert "[db-error]" in job.transcript_tail()
