"""Claude Code as the client: the sentences of the server's text that are true of it alone.

Claude Code moves a long blocking MCP call to a background task after
``CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`` and delivers the result as a task notification; its
subagents and non-interactive runs do not, which is what the ``local-agent`` relay
(agents/claude-code/local-agent.md) is for. This text is exactly the server's before Epic 6.1
(tests/fixtures/clients/claude-code-before-6.1.json pins it).
"""

NAME = "claude-code"

WAIT = """\
Claude Code (v2.1.212+) moves a call running past
     CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS (default 2 min) to a background task: you get a task id,
     keep working, and the record arrives as a task notification.
     Several jobs: one blocking run_agent per job, never wait_s=0 + wait_jobs. Each job then
     notifies on its own finish; nothing to re-arm. Set CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=10000
     (settings env) so each call backgrounds in seconds.
     wait_jobs is a fallback for calls that cannot background; mode="any" must be re-armed
     after every return or the remaining jobs finish unseen."""

WAIT_MORE = """\
     Subagents and non-interactive runs do not auto-background (unless CLAUDE_AUTO_BACKGROUND_TASKS=1):
     use the `local-agent` subagent if installed (a relay; prompt = `model: <served id>`, optional
     header lines, `---`, the brief verbatim), or `run_agent(wait_s=0)` then `wait_job` in-turn.
"""

REVIEWER = "Claude"

WAIT_S = """\
Claude Code backgrounds a long call,
        its record a task notification. Use for every job, swarms included."""
