"""The Claude Code local-agent subagent file must carry valid YAML frontmatter.

Claude Code tolerates a broken frontmatter by keeping only name and description, which
silently turns the relay into a full-tool agent on the main session's model with CLAUDE.md
loaded (seen with an unquoted description containing `model: <id>`, v0.1.4).
"""

from pathlib import Path

import yaml

AGENTS = Path(__file__).resolve().parents[1] / "agents"
AGENT_FILE = AGENTS / "claude-code" / "local-agent.md"
# Epic 6.1: the Claude Code relay moved under agents/claude-code/; the old path stays as a
# relative symlink, because installs link ~/.claude/agents/local-agent.md to it
OLD_PATH = AGENTS / "local-agent.md"
JOB_TOOLS = {"run_agent", "wait_job", "job_status", "job_log", "cancel_job"}


def frontmatter() -> dict:
    text = AGENT_FILE.read_text()
    assert text.startswith("---\n")
    block, _, _ = text[4:].partition("\n---\n")
    return yaml.safe_load(block)


def test_frontmatter_parses_with_every_field():
    fm = frontmatter()
    assert fm["name"] == "local-agent"
    assert fm["model"] == "haiku"
    assert fm["background"] is True
    assert fm["omitClaudeMd"] is True
    assert isinstance(fm["maxTurns"], int)
    assert isinstance(fm["description"], str) and "list_models" in fm["description"]


def test_relay_gets_only_the_job_tools():
    tools = {t.strip() for t in frontmatter()["tools"].split(",")}
    assert tools == {f"mcp__local__{t}" for t in JOB_TOOLS}


def test_relay_waits_long_enough_not_to_cap_long_jobs():
    """100 turns x the wait timeout bounds job length; 100 s capped jobs at ~2.7 h (v0.1.5)."""
    import re

    m = re.search(r"wait_job\(job_id=<id>, timeout_s=(\d+)\)", AGENT_FILE.read_text())
    assert m and int(m.group(1)) >= 900
    assert frontmatter()["maxTurns"] * int(m.group(1)) >= 24 * 3600


def test_the_old_path_still_reaches_the_claude_code_relay():
    assert OLD_PATH.is_symlink()
    assert OLD_PATH.resolve() == AGENT_FILE.resolve()
    assert str(OLD_PATH.readlink()) == "claude-code/local-agent.md"  # relative: survives a moved checkout
