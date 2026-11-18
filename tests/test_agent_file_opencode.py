"""Epic 6.1: the OpenCode local-agent relay (agents/opencode/local-agent.md).

Its own version, not the Claude Code file with names swapped: OpenCode V2 agent frontmatter
(mode, steps, an ordered permissions list whose last match wins), its tool names (the server
registered as `local`: `local_run_agent`, or `tools.local.*` inside `execute` in code mode), and
one blocking run_agent call, since OpenCode waits on a tool call instead of moving it aside.
What both relays share is the contract with the caller: the header keys and the report.
"""

import re
from pathlib import Path

import yaml

AGENTS = Path(__file__).resolve().parents[1] / "agents"
OC = AGENTS / "opencode" / "local-agent.md"
CC = AGENTS / "claude-code" / "local-agent.md"


def _split(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    assert text.startswith("---\n")
    block, _, body = text[4:].partition("\n---\n")
    return yaml.safe_load(block), body


def test_frontmatter_is_opencode_v2():
    fm, _ = _split(OC)
    assert fm["mode"] == "subagent"
    assert isinstance(fm["steps"], int) and fm["steps"] > 0
    assert isinstance(fm["description"], str) and "list_models" in fm["description"]
    # nothing of Claude Code's agent format, and no pinned model: the relay runs on whatever
    # model the session uses unless the operator pins one
    for claude_field in ("name", "tools", "model", "background", "omitClaudeMd", "maxTurns", "color"):
        assert claude_field not in fm, claude_field


def test_the_relay_may_use_only_the_servers_tools():
    """Last match wins: deny everything first, then allow the server's tools (direct) and
    execute (code mode), and nothing after them re-opens anything."""
    fm, _ = _split(OC)
    rules = [(r["action"], r["resource"], r["effect"]) for r in fm["permissions"]]
    assert rules == [("*", "*", "deny"), ("local_*", "*", "allow"), ("execute", "*", "allow")]


def test_the_body_names_the_tools_both_ways_and_waits_once():
    _, body = _split(OC)
    for tool in ("run_agent", "wait_job", "cancel_job"):
        assert f"`local_{tool}`" in body, tool
        assert f"tools.local.{tool}(" in body, tool
    assert "`wait_s` set to the header's `timeout_s`, or 1800 without one" in body
    assert "mcp__local__" not in body  # Claude Code's tool names


def _header_keys(body: str) -> list[str]:
    m = re.search(r"Header keys are `run_agent` arguments: (.*?)\. Pass", body, re.S)
    assert m, "header key sentence"
    return re.findall(r"`(\w+)`", m.group(1))


def test_both_relays_take_the_same_header_and_report_the_same_way():
    _, oc = _split(OC)
    _, cc = _split(CC)
    assert _header_keys(oc) == _header_keys(cc)
    for shared in ("local job <id>: <status> (<model> on <endpoint>, <elapsed_s>s, <num_turns> turns)",
                   "local job: not started",
                   'no model given - pick one with list_models and put "model: <served id>" in the header',
                   "Never fill in a model yourself, not even one mentioned in the brief."):
        assert shared in oc and shared in cc, shared
