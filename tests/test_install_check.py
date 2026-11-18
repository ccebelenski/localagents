"""Epic 6.11: `localagents check` — the install's state item by item, read-only — and `check
--url`, what serves at an address. INSTALL.md (the brief a coding agent installs from) reads
both; these pin what it relies on.

The operator (2026-11-18): "Let's be creative with the installation - this is an LLM oriented
project, so maybe the installation itself is instructions for a model to install in claude code
or opencode? The user just tells the LLM 'Use these instructions to install'."
"""

import asyncio
import json
import re
import stat
from pathlib import Path

import httpx
import pytest

from localagents import install_check as ic

ROOT = Path(__file__).resolve().parents[1]
REAL_CLIENT = httpx.AsyncClient


def _backend(routes):
    """httpx.AsyncClient with every request answered from ``routes`` (path -> (status, body))."""
    def handler(req):
        if "refused" in str(req.url):
            raise httpx.ConnectError("[Errno 111] Connection refused", request=req)
        status, body = routes.get(req.url.path, (404, {}))
        return httpx.Response(status, json=body)

    def make(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return REAL_CLIENT(*a, **kw)
    return make


def _identify(monkeypatch, routes, url="http://box.lan:8080", **kw):
    monkeypatch.setattr(httpx, "AsyncClient", _backend(routes))
    return asyncio.run(ic.identify(url, **kw))


def test_identify_llamacpp_names_the_missing_flags(monkeypatch):
    out = _identify(monkeypatch, {
        "/v1/models": (200, {"data": [{"id": "qwen", "owned_by": "llamacpp"}]}),
        "/health": (200, {"status": "ok"}),
        "/props": (200, {"default_generation_settings": {"n_ctx": 131072}, "endpoint_slots": True,
                         "endpoint_metrics": False}),
    }, url="http://box.lan:8080/v1/")
    assert out["backend"] == "llama.cpp" and out["models"] == ["qwen"] and out["context"] == {"qwen": 131072}
    assert out["url"] == "http://box.lan:8080" and any("without /v1" in n for n in out["notes"])
    assert any("--metrics" in n and "--slots" not in n for n in out["notes"])
    assert out["entry"] == "  box:\n    base_url: http://box.lan:8080\n    backend: llama.cpp\n    host: box"


def test_identify_strata_vllm_and_other(monkeypatch):
    out = _identify(monkeypatch, {
        "/v1/models": (200, {"data": [{"id": "q", "owned_by": None}]}),
        "/health": (200, {"status": "ok", "service": "strata"}),
        "/props": (200, {"default_generation_settings": {"n_ctx": 65536}}),
    }, url="http://10.0.0.5:8080")
    assert out["backend"] == "strata" and out["entry"].startswith("  strata:\n")  # an IP: no host line
    assert "host:" not in out["entry"] and any("under 128k" in n for n in out["notes"])
    out = _identify(monkeypatch, {"/v1/models": (200, {"data": [{"id": "ds", "owned_by": "vllm", "max_model_len": 300000}]})})
    assert out["backend"] == "vllm" and out["context"] == {"ds": 300000}
    assert any("--enable-prompt-tokens-details" in n for n in out["notes"])
    out = _identify(monkeypatch, {"/v1/models": (200, {"data": [{"id": "x", "owned_by": "someone"}]})})
    assert out["backend"] == "other" and out["up"]


def test_identify_a_key_and_a_dead_port(monkeypatch):
    out = _identify(monkeypatch, {"/v1/models": (401, {"error": "no"})})
    assert "api_key" in out["error"]
    out = _identify(monkeypatch, {"/v1/models": (200, {"data": [{"id": "q", "owned_by": "llamacpp"}]}),
                                  "/props": (200, {})}, api_key="sk-x")
    assert out["entry"].endswith("api_key: <the key>") and "sk-x" not in json.dumps(out)
    out = _identify(monkeypatch, {}, url="http://refused:1")
    assert out["up"] is False and out["error"]


# ---------- the client items, in a scratch HOME ----------


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv(ic.AUTO_BACKGROUND, raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    return h, bindir


def _exe(bindir, name, body="exit 0"):
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def _items(items):
    return {i.id: i for i in items}


def test_claude_code_absent_then_each_item(home, tmp_path):
    h, bindir = home
    cfg = tmp_path / "models.yaml"
    assert _items(ic.check_claude_code("local", cfg, tmp_path))["claude-code"].status == "absent"  # no claude
    _exe(bindir, "claude")
    _exe(bindir, "localagents")
    got = _items(ic.check_claude_code("local", cfg, tmp_path))
    assert got["claude-code"].status == "absent" and "4a" in got["claude-code"].fix
    assert got["claude-code.auto-background"].status == "warn"
    assert got["claude-code.relay"].status == "absent" and got["claude-code.statusline"].status == "absent"

    (h / ".claude.json").write_text(json.dumps({"mcpServers": {"local": {
        "type": "stdio", "command": "localagents", "args": ["--config", str(cfg)]}}}))
    (h / ".claude" / "settings.json").write_text(json.dumps({
        "env": {ic.AUTO_BACKGROUND: "10000"}, "statusLine": {"type": "command", "command": str(ROOT / "tools" / "statusline.sh")}}))
    (h / ".claude" / "agents").mkdir()
    (h / ".claude" / "agents" / "local-agent.md").symlink_to(ROOT / "agents" / "claude-code" / "local-agent.md")
    got = _items(ic.check_claude_code("local", cfg, tmp_path))
    assert {k: v.status for k, v in got.items()} == {
        "claude-code": "ok", "claude-code.auto-background": "ok", "claude-code.relay": "ok", "claude-code.statusline": "ok"}
    # the old path (agents/local-agent.md, a symlink kept for earlier installs) is the same relay
    (h / ".claude" / "agents" / "local-agent.md").unlink()
    (h / ".claude" / "agents" / "local-agent.md").symlink_to(ROOT / "agents" / "local-agent.md")
    assert _items(ic.check_claude_code("local", cfg, tmp_path))["claude-code.relay"].status == "ok"


def test_claude_code_registration_problems(home, tmp_path):
    h, bindir = home
    _exe(bindir, "claude")
    cfg = tmp_path / "models.yaml"

    def reg(entry):
        (h / ".claude.json").write_text(json.dumps({"mcpServers": {"local": entry}}))
        return _items(ic.check_claude_code("local", cfg, tmp_path))["claude-code"]
    assert reg({"command": "localagents", "args": ["--config", str(cfg)]}).status == "fail"  # not on PATH
    _exe(bindir, "localagents")
    other = reg({"command": "localagents", "args": ["--config", "/elsewhere/models.yaml"]})
    assert other.status == "warn" and "/elsewhere/models.yaml" in other.detail
    assert reg({"command": "localagents", "args": []}).status == "warn"  # no --config
    _exe(bindir, "someserver")
    assert "not localagents" in reg({"command": "someserver", "args": []}).detail
    (h / ".claude.json").write_text(json.dumps({}))
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"local": {"command": "localagents", "args": ["--config", str(cfg)]}}}))
    got = _items(ic.check_claude_code("local", cfg, tmp_path))["claude-code"]
    assert got.status == "ok" and "project scope" in got.detail


def test_a_stale_relay_copy_and_a_renamed_one(home, tmp_path):
    h, bindir = home
    _exe(bindir, "claude")
    (h / ".claude" / "agents").mkdir()
    relay = h / ".claude" / "agents" / "local-agent.md"
    relay.write_text("an old relay")
    assert _items(ic.check_claude_code("local", tmp_path / "m.yaml", tmp_path))["claude-code.relay"].status == "warn"
    relay.write_text((ROOT / "agents" / "claude-code" / "local-agent.md").read_text().replace("mcp__local__", "mcp__lab__"))
    assert _items(ic.check_claude_code("lab", tmp_path / "m.yaml", tmp_path))["claude-code.relay"].status == "ok"


def test_opencode_reads_opencodes_own_parse(home, tmp_path):
    h, bindir = home
    cfg = tmp_path / "models.yaml"
    assert _items(ic.check_opencode("local", cfg, tmp_path))["opencode"].status == "absent"
    _exe(bindir, "localagents")
    docs = [{"type": "document", "path": str(h / ".config/opencode/opencode.json"),
             "info": {"mcp": {"servers": {"local": {"type": "local", "command": ["localagents", "--config", str(cfg)]}}}}}]
    (tmp_path / "debug.json").write_text(json.dumps(docs))
    # only the standalone reading is this environment's (the service answers from its own)
    _exe(bindir, "opencode", f'case "$*" in *--standalone*) cat "{tmp_path / "debug.json"}";; esac')
    got = _items(ic.check_opencode("local", cfg, tmp_path))
    assert got["opencode"].status == "ok" and "opencode.json" in got["opencode"].detail
    assert got["opencode.relay"].status == "absent"
    agents = h / ".config" / "opencode" / "agents"
    agents.mkdir(parents=True)
    (agents / "local-agent.md").symlink_to(ROOT / "agents" / "opencode" / "local-agent.md")
    assert _items(ic.check_opencode("local", cfg, tmp_path))["opencode.relay"].status == "ok"
    docs[0]["info"]["mcp"]["servers"]["local"]["enabled"] = False
    (tmp_path / "debug.json").write_text(json.dumps(docs))
    assert "disabled" in _items(ic.check_opencode("local", cfg, tmp_path))["opencode"].detail
    _exe(bindir, "opencode", "echo not json")
    assert _items(ic.check_opencode("local", cfg, tmp_path))["opencode"].status == "warn"


def test_config_items(tmp_path):
    assert ic.check_config(tmp_path / "none.yaml")[0].status == "fail"
    p = tmp_path / "models.yaml"
    p.write_text("endpoints: {}\n")
    assert ic.check_config(p)[0].status == "fail"
    p.write_text("endpoints:\n  a: {base_url: 'http://127.0.0.1:9', backend: llama.cpp}\n")
    item, reg = ic.check_config(p)
    assert item.status == "ok" and reg is not None
    p.write_text("endpoints: [\n")
    assert ic.check_config(p)[0].status == "fail"


def test_the_command_exits_1_on_a_fail(home, tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        ic.main(["--config", str(tmp_path / "none.yaml"), "--json"])
    assert e.value.code == 1
    items = json.loads(capsys.readouterr().out)
    assert {"id": "config", "status": "fail"}.items() <= next(i for i in items if i["id"] == "config").items()


# ---------- INSTALL.md and the code agree ----------


def test_install_md_names_only_items_the_command_reports():
    text = (ROOT / "INSTALL.md").read_text()
    named = set(re.findall(r"`((?:claude-code|opencode)(?:\.[a-z-]+)?|package|config|harness)`", text))
    src = (ROOT / "src" / "localagents" / "install_check.py").read_text()
    reported = set(re.findall(r'Item\("([a-z.:-]+)"', src)) | set(re.findall(r'\.id = "([a-z.-]+)"', src))
    assert named - {"opencode"} <= reported | {"opencode"}, named - reported
    assert {"package", "config", "claude-code", "claude-code.relay", "opencode.relay", "harness"} <= named


def test_install_md_statusline_lines_are_the_shipped_scripts():
    """The two lines INSTALL.md has the agent add to a user's own status line are the ones
    tools/statusline.sh runs, so the two never drift."""
    text = (ROOT / "INSTALL.md").read_text()
    block = re.search(r"add\s+them at its end with their agreement:\n\n   ```bash\n(.*?)   ```", text, re.S).group(1)
    script = (ROOT / "tools" / "statusline.sh").read_text()
    for line in [ln.strip() for ln in block.splitlines() if ln.strip()]:
        assert line in script, line


def test_the_readme_sends_people_to_install_md():
    assert "INSTALL.md" in (ROOT / "README.md").read_text()
