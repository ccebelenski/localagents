"""router 9c (docs/superpowers/specs/2026-11-02-calibration-set-design.md §3, §11): the
recording stub MCP server the tool-use probes are calibrated against. It serves the real
server's ten tool names with the same parameter names and docstring first lines, answers from
the module's own CANNED constant, refuses anything that would dispatch, and appends one JSON
line per call to $LOCALAGENTS_STUB_LOG when that env names a file. No model and no network."""

import asyncio
import inspect
import json

import pytest

from localagents.router import stubmcp

# the names the probes' sessions must see; §3's tool-use list, exactly as server.py serves them
TOOL_NAMES = ["list_models", "run_agent", "wait_job", "wait_jobs", "job_status", "job_log",
              "list_jobs", "cancel_job", "local_complete", "register_endpoint"]

# every call that would put work onto a model answers this and nothing else
_REFUSED = {"error": "refused", "message": "this is a calibration stub; nothing is dispatched"}

DISPATCH_TOOLS = [("run_agent", {"task": "fix the parser"}),
                  ("wait_job", {"job_id": stubmcp.ANSWERED_JOB_ID}),
                  ("wait_jobs", {"job_ids": [stubmcp.ANSWERED_JOB_ID]}),
                  ("cancel_job", {"job_id": stubmcp.ANSWERED_JOB_ID}),
                  ("local_complete", {"prompt": "say hi"}),
                  ("register_endpoint", {"name": "box-c", "base_url": "http://box-c:8080"})]


def _call(tool_name: str, **kwargs):
    return asyncio.run(getattr(stubmcp, tool_name)(**kwargs))


def test_server_lists_exactly_the_real_servers_tool_names():
    names = [t.name for t in asyncio.run(stubmcp.mcp.list_tools())]
    assert sorted(names) == sorted(TOOL_NAMES)
    assert stubmcp.mcp.name == "localagents"


def test_docstring_first_lines_match_server_py():
    """A session should not notice the stub by its tool descriptions: every first line is
    copied from server.py's same-named tool."""
    from localagents import server

    for name in TOOL_NAMES:
        real = inspect.getdoc(getattr(server, name)).splitlines()[0].strip()
        stub = inspect.getdoc(getattr(stubmcp, name)).splitlines()[0].strip()
        assert stub == real, name


def test_parameter_names_match_server_py():
    """The signatures the probes brief against; ctx is the MCP injection, not a model argument."""
    from localagents import server

    for name in TOOL_NAMES:
        stub = [p for p in inspect.signature(getattr(stubmcp, name)).parameters if p != "ctx"]
        real = [p for p in inspect.signature(getattr(server, name)).parameters if p != "ctx"]
        assert stub == real, name


def test_list_models_answers_the_canned_fleet():
    out = _call("list_models")
    assert out == stubmcp.CANNED["list_models"]
    eps = out["endpoints"]
    assert sorted(eps) == ["box-a", "box-b"]
    assert eps["box-a"] == {"up": True, "models": ["coder-small"], "context": {"coder-small": 131072}}
    assert eps["box-b"] == {"up": True, "models": ["coder-large"], "context": {"coder-large": 262144}}


def test_job_status_answers_the_canned_finished_job():
    out = _call("job_status", job_id=stubmcp.ANSWERED_JOB_ID)
    assert out == stubmcp.CANNED["job_status"]
    assert out["status"] == "succeeded"
    assert out["num_turns"] == 42
    assert out["elapsed_s"] == 613
    assert out["model"] == "coder-large"


def test_job_status_any_other_id_is_unknown_job():
    assert _call("job_status", job_id="cafe0000-0000-4000-8000-000000000002") == {"error": "unknown_job"}
    assert _call("job_status", job_id="") == {"error": "unknown_job"}


def test_job_log_answers_a_three_line_transcript():
    out = _call("job_log", job_id=stubmcp.ANSWERED_JOB_ID)
    assert out == stubmcp.CANNED["job_log"]
    assert len(out["lines"]) == 3
    # a transcript is canned for any id: the probe's fact is that job logs exist and read
    assert _call("job_log", job_id="whatever") == out


def test_list_jobs_answers_the_one_job():
    out = _call("list_jobs")
    assert out == stubmcp.CANNED["list_jobs"]
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["id"] == stubmcp.ANSWERED_JOB_ID


@pytest.mark.parametrize("name,kwargs", DISPATCH_TOOLS)
def test_dispatching_tools_are_refused(name, kwargs):
    assert _call(name, **kwargs) == _REFUSED


def test_canned_answers_are_never_mutated_by_a_caller():
    out = _call("list_models")
    out["endpoints"]["box-a"]["up"] = False
    assert _call("list_models")["endpoints"]["box-a"]["up"] is True


def test_every_call_is_recorded_in_order(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("LOCALAGENTS_STUB_LOG", str(log))
    _call("list_models")
    _call("run_agent", task="fix the parser")
    _call("job_status", job_id=stubmcp.ANSWERED_JOB_ID)
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [rec["tool"] for rec in lines] == ["list_models", "run_agent", "job_status"]
    assert lines[0]["args"] == {}
    assert lines[1]["args"]["task"] == "fix the parser"
    assert lines[2]["args"] == {"job_id": stubmcp.ANSWERED_JOB_ID}
    assert all(isinstance(rec["t"], float) for rec in lines)


def test_recording_appends_across_calls(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("LOCALAGENTS_STUB_LOG", str(log))
    _call("list_models")
    _call("list_models")
    assert len(log.read_text().splitlines()) == 2


def test_no_env_means_no_recording_but_still_answers(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALAGENTS_STUB_LOG", raising=False)
    assert _call("list_models") == stubmcp.CANNED["list_models"]
    assert _call("run_agent", task="x") == _REFUSED
    # nothing recorded: the directory holds only conftest's models.yaml, no jsonl
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".jsonl")]


def test_serves_stdio_when_run_as_main():
    """The runner spawns `python3 -m localagents.router.stubmcp`; the client handshake proves
    the process serves MCP on stdio without any environment setup."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import sys

    async def _handshake():
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "localagents.router.stubmcp"], env={})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return sorted(t.name for t in tools.tools)

    assert asyncio.run(_handshake()) == sorted(TOOL_NAMES)
