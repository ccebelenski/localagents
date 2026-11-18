"""Epic 6.8: a job's policy read once (harnesses/policy.py) and OpenCode's translation of it
(harnesses/opencode_policy.py): each mapping stated, refused where there is no equivalent.

The operator (2026-11-18): "we should do 6.8 next" — after the coordinator's statement that the
refusals of 6.7 were a stopgap and 6.8 makes the same call mean the same thing in either harness.
"""

from localagents.harnesses import SessionSpec, opencode_policy as op, policy
from localagents.registry import Defaults

EVERY = set(op.TOOL_PLUGINS)


def _pol(**kw):
    d = kw.pop("defaults", Defaults())
    return policy.resolve(SessionSpec(defaults=d, shim_base_url="", background=kw.pop("background", True), **kw))


def _denied(t):
    return {r["action"] for r in t.permissions if r["effect"] == "deny"}


def test_the_policy_resolves_as_claude_codes_options_always_did():
    d = Defaults()
    p = _pol(defaults=d)
    assert p.permission_mode == d.permission_mode and p.allowed_tools == tuple(d.allowed_tools)
    assert p.max_turns == d.max_turns and p.setting_sources == tuple(d.setting_sources)
    assert _pol(allowed_tools=[]).allowed_tools == tuple(d.allowed_tools)  # empty: the config's
    assert _pol(setting_sources=[]).setting_sources == ()  # empty: the caller's own "none"
    assert _pol(permission_mode="plan", max_turns=3).permission_mode == "plan"


def test_the_default_policy_is_claude_codes_seven_tools():
    t = op.translate(_pol())  # acceptEdits, Read Edit Write Glob Grep Bash WebFetch
    assert t.permitted == ("edit", "glob", "grep", "patch", "read", "shell", "webfetch", "write")
    assert _denied(t) >= {"subagent", "websearch", "skill"} | set(op.NEVER_ACTIONS)
    assert {"opencode.tool.subagent", "opencode.tool.websearch", "opencode.tool.skill"} <= set(t.plugins_off)
    assert set(op.NEVER_PLUGINS) <= set(t.plugins_off) and not t.refused


def test_each_permission_mode():
    read_only = ["Read", "Glob", "Grep"]
    assert op.translate(_pol(permission_mode="default", allowed_tools=read_only)).permitted == ("glob", "grep", "read")
    assert op.translate(_pol(permission_mode="acceptEdits", allowed_tools=read_only)).permitted == (
        "edit", "glob", "grep", "patch", "read", "write")
    bypass = op.translate(_pol(permission_mode="bypassPermissions", allowed_tools=read_only))
    assert set(bypass.permitted) == EVERY - {"subagent"}  # the config's disallowed Agent/Task still out
    plan = op.translate(_pol(permission_mode="plan"))
    assert not set(plan.permitted) & {"edit", "patch", "write", "shell"} and "read" in plan.permitted
    bad = op.translate(_pol(permission_mode="dontAsk"))
    assert bad.refused and "dontAsk" in bad.refused[0] and bad.permitted == ()


def test_tool_names_map_and_an_unknown_one_is_refused():
    t = op.translate(_pol(permission_mode="default", allowed_tools=["Bash", "read", "WebSearch", "Task"],
                          defaults=Defaults(disallowed_tools=[])))
    assert t.permitted == ("read", "shell", "subagent", "websearch")
    t = op.translate(_pol(permission_mode="default", allowed_tools=["Read", "TodoWrite"]))
    assert t.refused == ["allowed_tools: 'TodoWrite' has no OpenCode equivalent"]
    t = op.translate(_pol(defaults=Defaults(disallowed_tools=["Bash"])))
    assert "shell" not in t.permitted and "shell" in _denied(t)


def test_mcp_servers_and_their_tools():
    servers = {"localagents": {"command": "/usr/bin/python3", "args": ["-m", "x"], "env": {"A": "1"}},
               "web-thing": {"type": "http", "url": "https://h/mcp", "headers": {"X": "y"}}}
    t = op.translate(_pol(mcp_servers=servers, allowed_tools=["Read", "mcp__localagents"], strict_mcp_config=True))
    assert t.mcp["localagents"] == {"type": "local", "command": ["/usr/bin/python3", "-m", "x"], "codemode": False,
                                    "environment": {"A": "1"}}
    assert t.mcp["web-thing"] == {"type": "remote", "url": "https://h/mcp", "oauth": False, "codemode": False,
                                  "headers": {"X": "y"}}
    rules = [(r["action"], r["effect"]) for r in t.permissions]
    # the allowed server's tools come after the per-server deny (last match wins), the other's stay denied
    assert rules.index(("localagents_*", "allow")) > rules.index(("localagents_*", "deny"))
    assert ("web-thing_*", "deny") in rules and ("web-thing_*", "allow") not in rules
    one = op.translate(_pol(mcp_servers=servers, allowed_tools=["mcp__web-thing__search"]))
    assert ("web-thing_search", "allow") in [(r["action"], r["effect"]) for r in one.permissions]


def test_what_has_no_equivalent_is_refused_never_dropped():
    t = op.translate(_pol(strict_mcp_config=False))
    assert t.refused and "strict_mcp_config" in t.refused[0]
    t = op.translate(_pol(mcp_servers={"s": {"type": "websocket", "url": "ws://x"}}))
    assert t.refused == ["mcp_servers: 's' (websocket) has no OpenCode equivalent"]
    t = op.translate(_pol(setting_sources=["project", "enterprise"]))
    assert t.refused and "enterprise" in t.refused[0]


def test_each_mapping_is_stated():
    t = op.translate(_pol(thinking=False, effort="low"))
    assert t.stated[0].startswith("permission_mode acceptEdits -> tools edit, glob")
    assert any("written into each request by the shim" in line for line in t.stated)
