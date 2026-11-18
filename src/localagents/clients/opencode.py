"""OpenCode as the client: the sentences of the server's text that are true of it alone.

OpenCode holds a tool call, and the session making it, until the call returns; its MCP
execution timeout (12 h by default) bounds the wait, so a blocking ``run_agent`` is never moved
aside the way Claude Code moves one. Its subagents always run in the background and report to
the parent when they go idle, so several jobs at once are one ``local-agent`` relay each
(agents/opencode/local-agent.md). The coordinating model may be any model the user runs,
local ones included, so the local models are compared with a frontier model, not with a name.
"""

NAME = "opencode"

WAIT = """\
OpenCode holds the call, and the
     session, until it returns (its MCP execution timeout, 12 h by default, bounds the wait).
     Several jobs: one `local-agent` subagent per job if installed (a relay; prompt =
     `model: <served id>`, optional header lines, `---`, the brief verbatim). Subagents run in
     the background and each reports when its job ends.
     wait_jobs is a fallback for a session without the relay; mode="any" must be re-armed
     after every return or the remaining jobs finish unseen."""

WAIT_MORE = ""

REVIEWER = "a frontier model"

WAIT_S = """\
OpenCode waits on the call
        (and the session with it) until it returns. Use for every job; run several through
        one `local-agent` subagent each."""
