"""The local-agent subagent file must carry valid YAML frontmatter.

Claude Code tolerates a broken frontmatter by keeping only name and description, which
silently turns the relay into a full-tool agent on the main session's model with CLAUDE.md
loaded (seen with an unquoted description containing `model: <id>`, v0.1.4).
"""

from pathlib import Path

import yaml

AGENT_FILE = Path(__file__).resolve().parents[1] / "agents" / "local-agent.md"
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
