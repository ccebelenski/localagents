"""``localagents check``: the state of an install, item by item, read-only (Epic 6.11).

The install is INSTALL.md, a brief for the coding agent the operator asks to install it: the
agent holds the conversation and makes the changes, with the operator's consent, through the
clients' own commands where they have one. This command is what it reads before and after each
step, so the facts are code's and not the agent's guess: whether the server is registered with
Claude Code and OpenCode and against which config, whether each relay is installed and current,
the auto-background setting, the status line, and every endpoint in the config probed as
``list_models`` probes it. Each item is ok, warn, fail, absent (not set up; the operator may not
want it) or info, with the fix in one sentence naming INSTALL.md's step. Re-running the install,
updating and uninstalling all read the same items. It writes nothing anywhere.

``check --url URL`` identifies the server at an address (llama.cpp, vLLM or Strata; what it
serves, its window, the flags it lacks) and prints the ``models.yaml`` entry for it.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .registry import Defaults, Endpoint, Registry, config_path

AUTO_BACKGROUND = "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS"
AUTO_BACKGROUND_MAX_MS = 30_000  # the README's "ten seconds is about right"; two minutes is the default


@dataclass
class Item:
    id: str
    status: str  # ok | warn | fail | absent | info
    detail: str
    fix: str = ""


def checkout() -> Path | None:
    """The source tree this package runs from (an editable install's), or None for a wheel."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").exists() and (root / "agents").is_dir() else None


# ---------- the package and the config ----------


def check_package() -> Item:
    from .db import build_stamp

    on_path = shutil.which("localagents")
    where = checkout()
    detail = f"{build_stamp()} from {where or Path(__file__).resolve().parent}"
    if on_path is None:
        return Item("package", "fail", detail + "; `localagents` is not on PATH, and the clients start it by name",
                    "INSTALL.md step 2: `uv tool install -e .` in the checkout, and uv's bin directory on PATH")
    return Item("package", "ok", f"{detail}; on PATH at {on_path}")


def check_config(path: Path) -> tuple[Item, Registry | None]:
    if not path.exists():
        return Item("config", "fail", f"{path} does not exist",
                    "INSTALL.md step 3: write models.yaml"), None
    try:
        reg = Registry.load(path)
    except Exception as e:  # noqa: BLE001  a YAML error, a bad defaults value: say it, never a traceback
        return Item("config", "fail", f"{path}: {type(e).__name__}: {e}", "INSTALL.md step 3: fix models.yaml"), None
    real = [e for e in reg.endpoints.values() if not e.is_router]
    if not real:
        return Item("config", "fail", f"{path} names no endpoint", "INSTALL.md step 3: add your servers"), reg
    bad = [f"{e.name}: {e.invalid}" for e in reg.endpoints.values() if e.invalid]
    if bad:
        return Item("config", "warn", f"{path}: {len(reg.endpoints)} endpoints, ignored until fixed: " + "; ".join(bad),
                    "INSTALL.md step 3: fix the entries named"), reg
    return Item("config", "ok", f"{path}: {len(reg.endpoints)} endpoint(s), harness {reg.defaults.harness}"), reg


async def check_endpoints(reg: Registry) -> list[Item]:
    probes = await reg.probe_all()
    items = []
    for name, ep in reg.endpoints.items():
        p = probes.get(name) or {}
        iid = f"endpoint:{name}"
        if ep.is_router:
            items.append(Item(iid, "info", f"router entry ({'up' if p.get('up') else 'down'}); see docs/router.md"))
            continue
        if not p.get("up"):
            items.append(Item(iid, "warn", f"{ep.url} ({ep.backend}): down: {p.get('error', '?')}",
                              (p.get("hint") or "start a model there, or fix base_url") + " (a server that is off is fine)"))
            continue
        ctx = p.get("context") or {}
        models = ", ".join(f"{m} ({ctx[m] // 1024}k)" if m in ctx else m for m in p.get("models", [])) or "nothing"
        notes = [n for n in (p.get("context_warning"), p.get("hint")) if n]
        items.append(Item(iid, "warn" if notes else "ok", f"{ep.url} ({ep.backend}): serving {models}",
                          "; ".join(notes)))
    return items


# ---------- the clients ----------


def _same_relay(installed: Path, shipped: Path | None, server_name: str) -> Item | None:
    """None when no relay is installed; else whether it is this checkout's (a link to it, or the
    same text) or a copy renamed for another server name."""
    if installed.is_symlink() and not installed.exists():
        return Item("", "warn", f"{installed} is a link to {os.readlink(installed)}, which is gone (the checkout moved?)",
                    "remove the link and make it again to this checkout's relay")
    if not installed.exists():
        return None
    if shipped is None:
        return Item("", "info", f"{installed} installed (no checkout to compare it with)")
    if installed.resolve() == shipped.resolve():
        return Item("", "ok", f"{installed} -> {shipped}")
    text = installed.read_text(errors="replace")
    if text == shipped.read_text():
        return Item("", "ok", f"{installed} (a copy, identical to {shipped})")
    if server_name != "local" and server_name in text:
        return Item("", "ok", f"{installed} (a copy renamed for server '{server_name}')")
    return Item("", "warn", f"{installed} differs from {shipped}",
                "a stale copy: replace it with a link to the checkout's (INSTALL.md, the client's relay step)")


def _registration(entry: dict[str, Any] | None, where: str, config: Path, command: list[str]) -> Item:
    if entry is None:
        return Item("", "absent", "not registered")
    if command[:1] and shutil.which(command[0]) is None and not Path(command[0]).exists():
        return Item("", "fail", f"{where}: its command {command[0]!r} is not on PATH",
                    "re-register it with `localagents` (INSTALL.md step 2 puts it on PATH)")
    if "localagents" not in " ".join(command):
        return Item("", "warn", f"{where}: its command is {' '.join(command)!r}, not localagents")
    given = None
    for i, arg in enumerate(command):
        if arg == "--config" and i + 1 < len(command):
            given = Path(command[i + 1]).expanduser()
        elif arg.startswith("--config="):
            given = Path(arg.split("=", 1)[1]).expanduser()
    if given is None:
        return Item("", "warn", f"{where}: registered without --config, so the server looks in its working directory",
                    f"re-register it with --config {config}")
    if given.resolve() != config.resolve():
        return Item("", "warn", f"{where}: registered with --config {given}, not {config}",
                    "fine if that is the config you mean; else re-register it")
    return Item("", "ok", f"{where}: localagents --config {given}")


def claude_dirs() -> tuple[Path, Path]:
    """Claude Code's config directory (``~/.claude``, or ``CLAUDE_CONFIG_DIR``) and its state
    file (``~/.claude.json``, inside the directory when that is set)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        d = Path(env).expanduser()
        return d, d / ".claude.json"
    return Path.home() / ".claude", Path.home() / ".claude.json"


def _dict(v: Any) -> dict[str, Any]:
    """A settings value where a mapping belongs, or {}: the files are the operator's, and a
    malformed one is reported as missing, never a traceback out of the check."""
    return v if isinstance(v, dict) else {}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def check_claude_code(name: str, config: Path, cwd: Path) -> list[Item]:
    if shutil.which("claude") is None:
        return [Item("claude-code", "absent", "`claude` is not on PATH")]
    cdir, state = claude_dirs()
    st = _read_json(state)
    found: list[tuple[str, dict[str, Any]]] = []
    for where, servers in (("user scope", _dict(st.get("mcpServers"))),
                           (f"local scope ({cwd})", _dict(_dict(_dict(st.get("projects")).get(str(cwd))).get("mcpServers"))),
                           (f"project scope ({cwd / '.mcp.json'})", _dict(_read_json(cwd / ".mcp.json").get("mcpServers")))):
        if isinstance(servers.get(name), dict):
            found.append((where, servers[name]))
    items = []
    if not found:
        it = _registration(None, "", config, [])
        it.fix = "INSTALL.md step 4a: `claude mcp add --scope user ...`"
    else:
        where, e = found[0]
        it = _registration(e, where + (f" (also {', '.join(w for w, _ in found[1:])})" if found[1:] else ""),
                           config, [str(e.get("command", "")), *[str(a) for a in e.get("args") or []]])
    it.id = "claude-code"
    items.append(it)

    settings = _read_json(cdir / "settings.json")
    raw = _dict(settings.get("env")).get(AUTO_BACKGROUND) or os.environ.get(AUTO_BACKGROUND)
    try:
        ms = int(raw) if raw is not None else None
    except ValueError:
        ms = None
    if ms is None:
        items.append(Item("claude-code.auto-background", "warn",
                          f"{AUTO_BACKGROUND} unset: a blocking run_agent call waits two minutes before Claude Code backgrounds it",
                          f"INSTALL.md step 4a: set it to 10000 in {cdir / 'settings.json'}"))
    else:
        items.append(Item("claude-code.auto-background", "ok" if ms <= AUTO_BACKGROUND_MAX_MS else "warn",
                          f"{AUTO_BACKGROUND}={ms}", "" if ms <= AUTO_BACKGROUND_MAX_MS else "10000 is about right"))

    root = checkout()
    relay = _same_relay(cdir / "agents" / "local-agent.md",
                        root / "agents" / "claude-code" / "local-agent.md" if root else None, name)
    if relay is None:
        relay = Item("", "absent", f"no {cdir / 'agents' / 'local-agent.md'}",
                     "INSTALL.md step 4a: link the relay")
    relay.id = "claude-code.relay"
    items.append(relay)

    cmd = str(_dict(settings.get("statusLine")).get("command") or "")
    if not cmd:
        items.append(Item("claude-code.statusline", "absent", "no statusLine (optional)",
                          "INSTALL.md step 4a, the optional status line"))
    else:
        script = Path(cmd.split()[0]).expanduser()
        try:
            text = cmd + ("\n" + script.read_text(errors="replace") if script.is_file() else "")
        except OSError:
            text = cmd
        if "localagents statusline" in text:
            items.append(Item("claude-code.statusline", "ok", f"{cmd} runs `localagents statusline`"))
        else:
            items.append(Item("claude-code.statusline", "absent", f"{cmd} does not run `localagents statusline` (optional)",
                              "INSTALL.md step 4a, the optional status line"))
    return items


def opencode_config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser() / "opencode"


def opencode_sources(cwd: Path) -> list[dict[str, Any]] | None:
    """OpenCode's own reading of its configuration (``opencode debug config``: every source with
    its parsed body — JSONC is OpenCode's to parse, not ours). ``--standalone``: without it the
    answer is the background service's, read in the service's environment, not this one's (a
    scratch ``XDG_CONFIG_HOME`` still showed the operator's config). None when it cannot be had."""
    try:
        r = subprocess.run(["opencode", "debug", "config", "--standalone"], cwd=cwd, capture_output=True, text=True, timeout=60)
        docs = json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return [d for d in docs if isinstance(d, dict)] if isinstance(docs, list) else None


def check_opencode(name: str, config: Path, cwd: Path) -> list[Item]:
    if shutil.which("opencode") is None:
        return [Item("opencode", "absent", "`opencode` is not on PATH (needed to coordinate from OpenCode or to run OpenCode jobs)")]
    sources = opencode_sources(cwd)
    if sources is None:
        it = Item("opencode", "warn", "`opencode debug config` gave nothing readable; cannot see the registration")
    else:
        found = None
        for doc in sources:
            mcp = _dict(_dict(doc.get("info")).get("mcp"))
            servers = mcp.get("servers") if isinstance(mcp.get("servers"), dict) else mcp  # 2.x, then 1.x
            if isinstance(servers, dict) and isinstance(servers.get(name), dict):
                found = (str(doc.get("path") or doc.get("type")), servers[name])
        if found is None:
            it = _registration(None, "", config, [])
            it.fix = "INSTALL.md step 4b: `opencode mcp add ...`"
        else:
            where, e = found
            command = e.get("command") if isinstance(e.get("command"), list) else [str(e.get("command", ""))]
            it = _registration(e, where, config, [str(c) for c in command])
            if e.get("enabled") is False:
                it.status, it.detail = "warn", it.detail + "; disabled (enabled: false)"
    it.id = "opencode"
    items = [it]
    root = checkout()
    agents = opencode_config_dir() / "agents" / "local-agent.md"
    relay = _same_relay(agents, root / "agents" / "opencode" / "local-agent.md" if root else None, name)
    if relay is None:
        relay = Item("", "absent", f"no {agents}", "INSTALL.md step 4b: link the relay")
    relay.id = "opencode.relay"
    items.append(relay)
    return items


def check_harness(reg: Registry | None) -> Item:
    want = reg.defaults.harness if reg else "claude-code"
    have = shutil.which("opencode")
    if want == "opencode" and not have:
        return Item("harness", "fail", "defaults.harness is opencode but `opencode` is not on PATH: every job would fail",
                    "install OpenCode, or remove defaults.harness")
    return Item("harness", "info", f"jobs run in {want} by default; OpenCode jobs "
                + ("available" if have else "unavailable (`opencode` not on PATH)"))


async def run_checks(config: Path, name: str, cwd: Path) -> list[Item]:
    items = [check_package()]
    cfg, reg = check_config(config)
    items.append(cfg)
    if reg is not None:
        items += await check_endpoints(reg)
    items += check_claude_code(name, config, cwd)
    items += check_opencode(name, config, cwd)
    items.append(check_harness(reg))
    return items


# ---------- check --url: what serves at an address ----------


def _suggest_name(url: str, kind: str) -> tuple[str, str | None]:
    """The entry's name and its ``host:``. A server on this machine needs no host (the default,
    ``local``); every other one does, or the monitor draws it as this machine (the 6.11 install
    rehearsal: two servers on two boxes by IP, both left as ``local``). An IP has no name to
    offer, so the backend's kind stands in for both until the operator names them."""
    host = urlsplit(url).hostname or "server"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if host == "localhost" or (ip is not None and ip.is_loopback):
        return kind.replace(".", ""), None
    label = kind.replace(".", "") if ip is not None else host.split(".")[0]
    return label, label


async def identify(url: str, api_key: str | None = None, timeout_s: float = 5.0) -> dict[str, Any]:
    url = url.rstrip("/")
    notes = []
    if url.endswith("/v1"):
        url = url[:-3]
        notes.append("base_url is the server's root, without /v1")
    probe_ep = Endpoint(name="probe", base_url=url, api_key=api_key)
    try:
        async with httpx.AsyncClient(timeout=timeout_s, headers=probe_ep.auth_headers()) as c:
            r = await c.get(f"{url}/v1/models")
            if r.status_code in (401, 403):
                return {"url": url, "up": True, "error": f"HTTP {r.status_code}: the server wants an api_key",
                        "notes": ["run again with --api-key, and put it in the entry as api_key"]}
            r.raise_for_status()
            entries = [m for m in (r.json().get("data") or []) if isinstance(m, dict)]
            owners = {m.get("owned_by") for m in entries}
            health: dict[str, Any] = {}
            try:
                h = await c.get(f"{url}/health")
                health = h.json() if h.status_code == 200 else {}
            except (httpx.HTTPError, ValueError):
                pass
    except Exception as e:  # noqa: BLE001
        from .registry import classify_connect_failure

        return {"url": url, "up": False, **classify_connect_failure(e, url)}
    if isinstance(health, dict) and health.get("service") == "strata":
        kind = "strata"
    elif "llamacpp" in owners:
        kind = "llama.cpp"
    elif "vllm" in owners:
        kind = "vllm"
    else:
        kind = "other"
        notes.append("not llama.cpp, vLLM or Strata by its answers: no window, occupancy or metrics are read")
    probe_ep.backend = kind
    reg = Registry(path=Path("-"), endpoints={}, defaults=Defaults(probe_timeout_s=timeout_s), raw={}, local={})
    p = await reg.probe(probe_ep)
    if kind == "llama.cpp":
        try:
            async with httpx.AsyncClient(timeout=timeout_s, headers=probe_ep.auth_headers()) as c:
                props = (await c.get(f"{url}/props")).json()
            missing = [f for f, k in (("--slots", "endpoint_slots"), ("--metrics", "endpoint_metrics")) if not props.get(k)]
            if missing:
                notes.append(f"llama-server started without {' and '.join(missing)}: the job still runs, the occupancy "
                             "and per-job figures are missing (README, Backends: llama.cpp)")
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
    if kind == "vllm":
        notes.append("vLLM: if it was not started with --enable-prompt-tokens-details, the cache figures are missing "
                     "(README, Backends: vLLM)")
    notes += [n for n in (p.get("context_warning"), p.get("hint")) if n]
    name, host = _suggest_name(url, kind)
    stanza = [f"  {name}:", f"    base_url: {url}", f"    backend: {kind}"]
    if host:
        stanza.append(f"    host: {host}")
    if api_key:
        stanza.append("    api_key: <the key>")
    return {"url": url, "up": bool(p.get("up")), "backend": kind, "models": p.get("models", []),
            "context": p.get("context") or {}, "slots": p.get("slots"), "notes": notes,
            "entry": "\n".join(stanza)}


# ---------- the command ----------


def _print_items(items: list[Item]) -> None:
    w = max(len(i.id) for i in items)
    for i in items:
        print(f"{i.status:6} {i.id:{w}}  {i.detail}")
        if i.fix and i.status != "ok":
            print(f"{'':6} {'':{w}}  -> {i.fix}")


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(
        prog="localagents check",
        description="the state of this install, item by item (read-only; INSTALL.md reads it), or with "
                    "--url what serves at an address and its models.yaml entry")
    ap.add_argument("--config", help="the models.yaml to check (default: LOCALAGENTS_CONFIG, ./models.yaml, "
                                     "~/.config/localagents/models.yaml)")
    ap.add_argument("--name", default="local", help="the name the server is registered under (default local)")
    ap.add_argument("--url", help="identify the server at this address instead")
    ap.add_argument("--api-key", help="with --url: the server's key")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)
    if a.url:
        out = asyncio.run(identify(a.url, a.api_key))
        if a.json:
            print(json.dumps(out, indent=1))
        elif not out.get("up") or out.get("error"):
            print(f"{out['url']}: {out.get('error', 'down')}" + (f" ({out['hint']})" if out.get("hint") else ""))
            for n in out.get("notes", []):
                print(f"  note: {n}")
        else:
            ctx = out["context"]
            print(f"{out['url']}: {out['backend']}, serving " + (", ".join(
                f"{m} ({ctx[m] // 1024}k)" if m in ctx else m for m in out["models"]) or "nothing"))
            for n in out["notes"]:
                print(f"  note: {n}")
            print("models.yaml entry (under endpoints:):\n" + out["entry"])
        sys.exit(0 if out.get("up") and not out.get("error") else 1)
    config = Path(a.config).expanduser().resolve() if a.config else config_path().resolve()
    items = asyncio.run(run_checks(config, a.name, Path.cwd()))
    if a.json:
        print(json.dumps([asdict(i) for i in items], indent=1))
    else:
        _print_items(items)
    sys.exit(1 if any(i.status == "fail" for i in items) else 0)
