"""Epic 6.5: one tool vocabulary — each harness's own table (claude_code_tools.py,
opencode_tools.py) over the shared kinds and write rule (harnesses/tools.py). Readers ask for a
job's vocabulary by its ``jobs.harness`` and read kinds, never one harness's names.

OpenCode's names and input keys are checked against the 6.2 captures (tests/fixtures/opencode),
never typed from memory: every tool a captured session called is in the table, and the write
rule stores what the reader needs from each captured input.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from localagents import db, derive
from localagents.harnesses import tools

FIX = Path(__file__).resolve().parent / "fixtures" / "opencode"


def _captured_tool_calls():
    """(tool, input) of every completed or failed tool call in the captured OpenCode events."""
    out = []
    for f in sorted((FIX / "events").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            e = json.loads(line)
            if e.get("type") == "tool_use":
                out.append((e["part"]["tool"], e["part"]["state"].get("input")))
    return out


def test_every_kind_is_known_and_every_table_maps_into_them():
    for name in ("claude-code", "opencode"):
        v = tools.vocabulary(name)
        assert v.harness == name
        assert set(v.kinds.values()) <= set(tools.KINDS)
    with pytest.raises(ValueError, match="unknown tool kinds"):
        tools.Vocabulary(harness="x", kinds={"a": "nope"}, path_keys=(), command_key="c")
    with pytest.raises(ValueError, match="no tool vocabulary"):
        tools.vocabulary("nope")
    assert tools.vocabulary(None) is tools.vocabulary("claude-code")  # a row from before schema 14


def test_claude_codes_table_is_the_names_derive_held_before():
    v = tools.vocabulary("claude-code")
    assert v.names("edit") == ("Edit", "Write", "MultiEdit", "NotebookEdit")
    assert v.names("read") == ("Read", "Grep", "Glob", "LS", "NotebookRead")
    assert v.names("shell") == ("Bash",)
    assert v.kind("mcp__localagents__job_status") == "mcp"
    assert v.kind("TodoWrite") == "other" and v.kind(None) == "other"


def test_opencodes_table_covers_every_tool_a_captured_session_called():
    calls = _captured_tool_calls()
    assert calls, "the 6.2 captures hold tool calls"
    v = tools.vocabulary("opencode")
    seen = {t for t, _ in calls}
    assert seen <= set(v.kinds), sorted(seen - set(v.kinds))
    assert v.kind("shell") == "shell" and v.kind("Bash") == "other"  # names are per harness
    assert v.kind("read") == "read" and v.kind("Read") == "other"


def test_the_write_rule_on_opencodes_captured_inputs():
    v = tools.vocabulary("opencode")
    for tool, inp in _captured_tool_calls():
        stored = v.stored_input(tool, inp)
        if tool == "shell":
            assert stored == inp["command"][:tools.COMMAND_CHARS]
        elif tool in ("read", "edit", "write") and isinstance(inp, dict) and inp.get("path"):
            assert stored == inp["path"]
    assert v.stored_input("edit", {"oldString": "a"}) is None  # an edit without a path: NULL


def test_the_write_rule_is_the_claude_code_reader_s_old_one():
    v = tools.vocabulary("claude-code")
    long = "x" * 3000
    assert v.stored_input("Bash", {"command": long}) == long[:2000]
    assert v.stored_input("Other", {"command": long}) == long[:120]
    assert v.stored_input("Read", {"file_path": "/a.py"}) == "/a.py"
    assert v.stored_input("NotebookEdit", {"notebook_path": "/n.ipynb"}) == "/n.ipynb"
    assert v.stored_input("MultiEdit", {"edits": []}) is None
    assert v.stored_input("TodoWrite", {"todos": [1]}) == json.dumps({"todos": [1]})
    assert v.stored_input("Weird", "a string input") == json.dumps("a string input")


def _job_with_calls(conn, harness, calls):
    with db.immediate(conn):
        conn.execute("INSERT INTO mcp_servers (id, pid, owner) VALUES ('s', 1, 1)")
        conn.execute("INSERT INTO jobs (id, mcp_server_id, status, created_at, harness)"
                     " VALUES ('j', 's', 'succeeded', 1.0, ?)", (harness,))
        for i, (tool, inp, err) in enumerate(calls):
            conn.execute("INSERT INTO tool_calls (job_id, t, tool, tool_use_id, input, is_error)"
                         " VALUES ('j', ?, ?, ?, ?, ?)", (float(i), tool, f"t{i}", inp, err))


def test_the_readers_read_an_opencode_job_by_its_vocabulary(tmp_path, monkeypatch):
    """files_touched, tool_mix and last_tool on an OpenCode job's rows: its edit/write paths are
    the files, its read/glob/grep reads, its shell commands classified as Bash's are."""
    monkeypatch.setenv("LOCALAGENTS_DB", str(tmp_path / "v.db"))
    conn = db.open_db()
    _job_with_calls(conn, "opencode", [
        ("read", "/w/a.py", 0), ("glob", "/w", 0), ("edit", "/w/a.py", 0), ("write", "/w/b.py", 0),
        ("edit", "/w/bad.py", 1), ("shell", "cat a.py | head", 0), ("shell", "python -m pytest -q", 0),
        ("question", '{"q": 1}', 0), ("shell", "ls " + "x" * 300, 0),
    ])
    assert derive.harness_of(conn, "j") == "opencode"
    assert derive.files_touched(conn, "j") == ["/w/a.py", "/w/b.py"]
    mix = derive.tool_mix(conn, "j", 900)
    assert (mix["reads"], mix["edits"], mix["other"]) == (4, 3, 2), mix
    last = derive.last_tool(conn, "j")
    assert last.startswith("shell: ls ") and len(last) == len("shell: ") + derive.TOOL_DISPLAY_CHARS
    # the same rows read as Claude Code's would be none of these: names are per harness
    assert derive.files_touched(conn, "j", "claude-code") == []
    conn.close()


def test_the_vocabulary_stays_out_of_the_sdk():
    """derive and the status line import the vocabularies: no Claude Agent SDK, no registry
    (the status bar renders on every refresh)."""
    code = ("import sys; import localagents.status, localagents.derive, localagents.harnesses.tools as t;"
            " t.vocabulary('opencode');"
            " print(sorted(m for m in sys.modules if m.startswith(('claude_agent_sdk', 'localagents.registry',"
            " 'localagents.harnesses.claude_code')) and m != 'localagents.harnesses.claude_code_tools'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]", out
