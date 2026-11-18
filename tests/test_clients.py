"""Epic 6.1: the server's client-facing text, one version per client over a shared core.

Claude Code's text must be exactly what the server said before 6.1 (the snapshot fixture);
OpenCode's carries none of Claude Code's settings or mechanics; only the sentences that differ
are the clients' own. The client is read from the ``initialize`` request's ``clientInfo.name``
(captured fixtures) unless ``--client`` declares it.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.runner import ServerRunner

from localagents import clients, server
from localagents.clients import claude_code, opencode

FIX = Path(__file__).resolve().parent / "fixtures" / "clients"
BEFORE = json.loads((FIX / "claude-code-before-6.1.json").read_text())
CLIENT_INFO = json.loads((FIX / "initialize-client-info.json").read_text())
SLOTS = ("NAME", "WAIT", "WAIT_MORE", "REVIEWER", "WAIT_S")


@pytest.fixture
def restore_default():
    yield
    clients.apply(server.mcp, clients.DEFAULT)


def _runner() -> SimpleNamespace:
    """What ServerRunner._handle_initialize reads off its runner: the options fixed before the
    handshake and the version negotiation (a static method, reached through self)."""
    return SimpleNamespace(init_options=server.mcp._lowlevel_server.create_initialization_options(), server=None,
                           _negotiate_initialize=ServerRunner._negotiate_initialize)


def _tool_text() -> dict[str, str]:
    return {t.name: t.description for t in server.mcp._tool_manager.list_tools()}


# what a later story added to the shared text, on purpose: (story, the anchor it follows, the text)
ADDED_SINCE: list[tuple[str, str, str]] = []


def _before_plus_additions(text: str) -> str:
    for _story, anchor, added in ADDED_SINCE:
        assert text.count(anchor) == 1, anchor
        text = text.replace(anchor, anchor + added)
    return text


def test_claude_code_text_is_exactly_the_text_before_6_1(restore_default):
    """Byte for byte the text before 6.1, plus only the additions listed in ADDED_SINCE."""
    clients.apply(server.mcp, "claude-code")
    assert server.mcp.instructions == BEFORE["instructions"]
    expected = dict(BEFORE["tools"], run_agent=_before_plus_additions(BEFORE["tools"]["run_agent"]))
    assert _tool_text() == expected
    # and the module's import-time default is the same text
    assert server.INSTRUCTIONS == BEFORE["instructions"]


def test_opencode_text_carries_nothing_of_claude_codes_mechanics(restore_default):
    text = clients.instructions("opencode") + clients.run_agent_description("opencode")
    for claude_only in ("CLAUDE_CODE_", "CLAUDE_AUTO_BACKGROUND", "task notification", "task id",
                        "auto-background", "than Claude"):
        assert claude_only not in text, claude_only
    assert "`local-agent` subagent" in clients.instructions("opencode")
    assert "OpenCode" in clients.run_agent_description("opencode")


def test_only_run_agent_and_the_instructions_differ_between_clients(restore_default):
    clients.apply(server.mcp, "claude-code")
    claude = _tool_text()
    clients.apply(server.mcp, "opencode")
    oc = _tool_text()
    assert server.mcp.instructions == clients.instructions("opencode")
    assert {n for n in claude if claude[n] != oc[n]} == {"run_agent"}


def test_every_client_module_fills_every_slot():
    assert set(clients.CLIENTS) == {"claude-code", "opencode"}
    for name, mod in clients.CLIENTS.items():
        assert mod.NAME == name
        for slot in SLOTS:
            assert isinstance(getattr(mod, slot), str), (name, slot)
    # the templates name exactly the slots the modules fill
    assert set(clients.INSTRUCTIONS.count("{" + s + "}") for s in ("wait", "wait_more", "reviewer")) == {1}
    assert set(clients.RUN_AGENT.count("{" + s + "}") for s in ("infer", "wait_s")) == {1}


def test_the_client_name_comes_from_the_captured_client_info():
    assert clients.from_client_info({"clientInfo": CLIENT_INFO["claude-code"]["clientInfo"]}) == "claude-code"
    assert clients.from_client_info({"clientInfo": CLIENT_INFO["opencode"]["clientInfo"]}) == "opencode"
    assert clients.from_client_info({"clientInfo": {"name": "some-other-client"}}) is None
    assert clients.from_client_info({}) is None
    assert clients.from_client_info(None) is None


def test_the_sdk_method_the_handshake_hook_wraps_is_there():
    """An SDK upgrade that renames or reshapes it must fail here, not silently serve the
    default text to every client."""
    import inspect

    assert list(inspect.signature(ServerRunner._handle_initialize).parameters) == ["self", "params"]


@pytest.mark.parametrize("who", ["claude-code", "opencode"])
def test_the_handshake_answers_each_client_with_its_own_text(who, monkeypatch, restore_default):
    monkeypatch.setattr(ServerRunner, "_handle_initialize", ServerRunner._handle_initialize)  # restored after
    seen = []
    clients.adapt_at_handshake(server.mcp, lambda client, info: seen.append((client, info)))
    runner = _runner()
    params = {"protocolVersion": CLIENT_INFO[who]["protocolVersion"], "capabilities": {},
              "clientInfo": CLIENT_INFO[who]["clientInfo"]}
    result = ServerRunner._handle_initialize(runner, params)
    assert result.instructions == clients.instructions(who)
    assert _tool_text()["run_agent"] == clients.run_agent_description(who)
    assert seen == [(who, CLIENT_INFO[who]["clientInfo"])]


def test_an_unknown_client_gets_the_default_text(monkeypatch, restore_default):
    monkeypatch.setattr(ServerRunner, "_handle_initialize", ServerRunner._handle_initialize)
    clients.apply(server.mcp, "opencode")  # whatever was there before
    clients.adapt_at_handshake(server.mcp, lambda client, info: None)
    runner = _runner()
    result = ServerRunner._handle_initialize(runner, {"protocolVersion": "2025-11-25", "capabilities": {},
                                                      "clientInfo": {"name": "unknown", "version": "1"}})
    assert result.instructions == BEFORE["instructions"]
    assert _tool_text()["run_agent"] == _before_plus_additions(BEFORE["tools"]["run_agent"])


def test_client_modules_are_not_shared_text():
    """The two versions really differ where they are the clients' own."""
    for slot in ("WAIT", "REVIEWER", "WAIT_S"):
        assert getattr(claude_code, slot) != getattr(opencode, slot), slot
