"""Story 1.4: the tool_calls write path (INSERT per ToolUseBlock, finished/is_error per matching
ToolResultBlock, tool_use_id -> rowid kept in memory). Story 1.5b removed the in-memory
counters: the rows are the record, and the readers use derive.

There is no captured SDK stream to replay, so the message sequence is built directly from the
claude_agent_sdk dataclasses (AssistantMessage / UserMessage / ToolUseBlock / ToolResultBlock,
the same ones runner.py imports) — the honest substitute for real stream capture.
"""

import sqlite3
from contextlib import contextmanager

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

from localagents import derive, jobs, runner
from localagents.harnesses import claude_code


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
    """claude_code._apply_message per message, with its module's time.time pinned to ``times[i]``."""
    clock = _Clock()
    monkeypatch.setattr(claude_code, "time", clock)
    for m, t in zip(_messages(), times):
        clock.now = t
        claude_code._apply_message(job, m)


def _rows(store, job):
    return store.conn.execute(
        "SELECT * FROM tool_calls WHERE job_id = ? ORDER BY rowid", (job.id,)
    ).fetchall()


def _row_vals(store, job):
    # sqlite3.Row does not compare by value; tuples do, so change tests can compare snapshots
    return [tuple(r) for r in _rows(store, job)]


def test_tool_use_rows(monkeypatch, tmp_path):
    """One row per ToolUseBlock in t order; input per the spec: a tool with a string
    ``command`` (Bash) the command, its first derive.COMMAND_CHARS (the read share fold: the
    stored cut, a fact apart from the 120 chars a reader is shown), any tool whose input carries a path
    (the edit tools and the path-shaped readers, story 1.11b) the path itself (NULL for an
    edit tool without one), every other tool the first derive.TOOL_INPUT_CHARS (120) chars of
    the input JSON."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    _feed(store, monkeypatch, job, [1000.0, 1001.0, 1002.0, 1003.0])
    rows = _rows(store, job)
    assert [r["tool"] for r in rows] == ["Read", "Bash", "Edit", "Write"]
    assert [r["t"] for r in rows] == [1000.0, 1000.0, 1002.0, 1002.0]
    assert [r["tool_use_id"] for r in rows] == ["tu-read", "tu-bash", "tu-edit", "tu-write"]
    # Read is path-shaped: input is its file_path, not the JSON around it (story 1.11b)
    assert rows[0]["input"] == "/a.py"
    # Bash: the command itself, whole under the stored cut (review of 1.5b: not of the input
    # JSON); what a reader is shown of it stays its first 120 chars (derive.display_input)
    assert derive.COMMAND_CHARS == 2000 and derive.TOOL_INPUT_CHARS == 120 == derive.TOOL_DISPLAY_CHARS
    assert rows[1]["input"] == "uv run pytest -q " + "x" * 200
    assert derive.display_input("Bash", rows[1]["input"]) == ("uv run pytest -q " + "x" * 200)[:120]
    # edit tools: the path, or NULL when the input has none
    assert rows[2]["input"] == "/a.py"
    assert rows[3]["input"] is None


def test_last_activity_shows_the_same_text_as_the_row(monkeypatch, tmp_path):
    """``job.last_activity`` for a ToolUseBlock is ``f"{tool}: {input_text}"`` with exactly the
    text the tool_calls row gets — the shape of derive.last_tool, so job_status's two fields
    read alike — so job_status shows ``Bash: uv run pytest -q`` (the command, not the input JSON
    with its description) and a call with nothing to show is just the tool name (review of 1.8;
    one format: the fold of its review, finding 8)."""
    from localagents import derive

    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    clock = _Clock()
    monkeypatch.setattr(runner, "time", clock)

    cases = [
        (ToolUseBlock(id="tu-1", name="Bash", input={"command": "uv run pytest -q", "description": "Run the tests"}),
         "Bash: uv run pytest -q", "uv run pytest -q"),
        (ToolUseBlock(id="tu-2", name="Edit", input={"file_path": "/a.py", "old_string": "a", "new_string": "b"}),
         "Edit: /a.py", "/a.py"),
        (ToolUseBlock(id="tu-3", name="Write", input={"content": "no file_path at all"}), "Write", None),
        # a non-string `command` is not Bash's: the input JSON, as for any other tool
        (ToolUseBlock(id="tu-4", name="Custom", input={"command": ["ls", "-la"]}),
         'Custom: {"command": ["ls", "-la"]}', '{"command": ["ls", "-la"]}'),
    ]
    for block, activity, row_input in cases:
        claude_code._apply_message(job, AssistantMessage(content=[block], model="m"))
        assert job.last_activity == activity
        row = store.conn.execute("SELECT input FROM tool_calls WHERE tool_use_id = ?", (block.id,)).fetchone()
        assert row["input"] == row_input
        assert derive.last_tool(store.conn, job.id) == activity  # the same text as the row's view


def test_path_shaped_tools_store_their_path(monkeypatch, tmp_path):
    """Story 1.11b: the input rule stores the path for ANY path-shaped tool — a dict input
    with no string ``command`` and a string ``file_path``/``notebook_path``/``path`` (the
    first key found, in that order) stores that path, so a Read on the status row names the
    file instead of showing its JSON head. A tool with no path key (a Grep of only a
    pattern) keeps the JSON head; and files_touched is unchanged — it filters on the edit
    tool NAMES, so a Read row whose input is now a bare path never joins it."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    clock = _Clock()
    monkeypatch.setattr(runner, "time", clock)

    cases = [
        # Glob/Grep/Read: the path, not the JSON around it
        (ToolUseBlock(id="tu-glob", name="Glob", input={"pattern": "**/*.py", "path": "/work/src"}),
         "Glob: /work/src", "/work/src"),
        (ToolUseBlock(id="tu-grep", name="Grep", input={"pattern": "needle", "path": "/work"}),
         "Grep: /work", "/work"),
        # the first key found in file_path, notebook_path, path order
        (ToolUseBlock(id="tu-note", name="NotebookEdit",
                      input={"notebook_path": "/n.ipynb", "path": "/decoy"}),
         "NotebookEdit: /n.ipynb", "/n.ipynb"),
        (ToolUseBlock(id="tu-read", name="Read", input={"file_path": "/work/a.py", "limit": 5}),
         "Read: /work/a.py", "/work/a.py"),
        # a Grep with only a pattern: no path key, so the JSON head as today
        (ToolUseBlock(id="tu-pattern", name="Grep", input={"pattern": "needle"}),
         'Grep: {"pattern": "needle"}', '{"pattern": "needle"}'),
    ]
    for block, activity, row_input in cases:
        clock.now += 1.0
        claude_code._apply_message(job, AssistantMessage(content=[block], model="m"))
        assert job.last_activity == activity
        row = store.conn.execute("SELECT input FROM tool_calls WHERE tool_use_id = ?", (block.id,)).fetchone()
        assert row["input"] == row_input
        assert derive.last_tool(store.conn, job.id) == activity
    # files_touched reads only the edit-tool rows: the Glob/Grep/Read rows of paths are not
    # files touched even though their input column now holds a bare path
    assert derive.files_touched(store.conn, job.id) == ["/n.ipynb"]


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
    claude_code._apply_message(job, UserMessage(content=[
        ToolResultBlock(tool_use_id="tu-never-sent", content="who dis", is_error=True),
    ]))
    assert _row_vals(store, job) == before
    assert "[db-error]" not in job.transcript_tail()


def test_turns_count_live_and_the_result_reconciles(monkeypatch, tmp_path):
    """Story 3.7h: each AssistantMessage is one live turn (the Recent jobs log's turns column
    moves while the job runs, it no longer reads 0 until the end), and the ResultMessage's own
    count still overwrites the running figure — the harness's number is the record."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    for i in range(3):
        claude_code._apply_message(job, AssistantMessage(content=[TextBlock(text=f"turn {i}")], model="m"))
    assert job.num_turns == 3  # live, before any result
    claude_code._apply_message(job, ResultMessage(
        subtype="success", duration_ms=5, duration_api_ms=4, is_error=False, num_turns=7,
        session_id="sess-1", usage=None, result="done",
    ))
    assert job.num_turns == 7  # the result's count wins


def test_result_message_usage_stays_off_the_job(monkeypatch, tmp_path):
    """Story 1.5b: usage is the sum of the request rows; the SDK ResultMessage usage is no
    longer parked on the Job (it counts the same traffic from the other side and differs)."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    claude_code._apply_message(job, ResultMessage(
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
    claude_code._apply_message(job, AssistantMessage(content=[
        ToolUseBlock(id="tu-boom", name="Bash", input={"command": "ls"}),
    ], model="m"))
    assert _row_vals(store, job) == []  # nothing inserted, no pending rowid remembered
    assert derive.tool_call_count(store.conn, job.id) == 0  # and the readers see nothing
    assert "[db-error]" in job.transcript_tail()
