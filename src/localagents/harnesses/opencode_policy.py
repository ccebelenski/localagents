"""OpenCode's translation of a job's policy (Epic 6.8): every setting of harnesses/policy.py in
OpenCode's terms, each mapping stated, and refused where OpenCode has no equivalent — never
silently dropped.

Tools. A Claude Code tool name maps to OpenCode's own (``TOOL_EQUIVALENTS``); an OpenCode name is
taken as it is; ``mcp__<server>`` is the server's ``<server>_*`` tools and ``mcp__<server>__<tool>``
the one tool ``<server>_<tool>``; any other name is refused. The tools a job is not permitted are
switched off by their built-in plugin (so they are not offered at all) and denied by a permission
rule; a few are never offered: ``question`` (a headless run cannot answer), image generation, code
mode (``execute``) and OpenCode's own management tools (worktrees, sessions, models, MCP resources:
a permission rule does not reach them, the 6.6+6.7 review's live check).

Permission modes, for a headless run that cannot stop to ask:
  default           exactly the allowed tools (Claude Code headless denies what it would ask about)
  acceptEdits       the allowed tools plus the edit tools (edits are accepted without asking)
  bypassPermissions every tool but the disallowed ones (nothing is asked)
  plan              the allowed tools minus every editing and shell tool (read-only)
The disallowed tools are taken out after the mode; another mode is refused.

MCP servers. A stdio server (``command``/``args``/``env``) becomes a ``local`` OpenCode server, an
``http``/``sse`` one a ``remote`` server; its tools are listed directly (no code mode). The job's
config is the only configuration an OpenCode job reads, so ``strict_mcp_config`` true (or unset)
is what happens anyway; false — "the operator's own MCP servers too" — is refused: they are never
given to a job.

Setting sources pick the instruction files: ``project`` the CLAUDE.md files from the job's directory
up to the repository root, ``local`` their CLAUDE.local.md siblings, ``user`` the operator's
~/.claude/CLAUDE.md; another source is refused. ``thinking`` and ``effort`` are written into every
request by the shim (6.6's levers, both APIs), so they need no OpenCode setting.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .policy import Policy

# Claude Code's tool names, as models.yaml and run_agent take them, in OpenCode's terms
TOOL_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "Read": ("read",), "LS": ("read",), "NotebookRead": ("read",),
    "Edit": ("edit", "patch"), "MultiEdit": ("edit", "patch"), "NotebookEdit": ("edit", "patch"),
    "Write": ("write",),
    "Glob": ("glob",), "Grep": ("grep",),
    "Bash": ("shell",),
    "WebFetch": ("webfetch",), "WebSearch": ("websearch",),
    "Agent": ("subagent",), "Task": ("subagent",),
    "Skill": ("skill",),
}
# OpenCode's own tools a job may be permitted, each with the built-in plugin that provides it
TOOL_PLUGINS: dict[str, str] = {
    "read": "opencode.tool.read", "edit": "opencode.tool.edit", "patch": "opencode.tool.patch",
    "write": "opencode.tool.write", "glob": "opencode.tool.glob", "grep": "opencode.tool.grep",
    "shell": "opencode.tool.shell", "webfetch": "opencode.tool.webfetch",
    "websearch": "opencode.tool.websearch", "subagent": "opencode.tool.subagent",
    "skill": "opencode.tool.skill",
}
EDIT_TOOLS = ("edit", "patch", "write")
NOT_IN_PLAN = ("edit", "patch", "write", "shell")
# never offered, whatever the policy: (permission actions, plugins)
NEVER_ACTIONS = ("question", "experimental_image_generate", "execute", "opencode_*")
NEVER_PLUGINS = ("opencode.tools", "opencode.tools.mcp-resources", "opencode.tool.question",
                 "opencode.tool.image-generate")
SOURCES = ("user", "project", "local")


@dataclass
class Translation:
    """What the job's OpenCode config takes from its policy, the lines its transcript states, and
    the settings refused (empty for a job that may start)."""

    permitted: tuple[str, ...] = ()
    permissions: list[dict[str, str]] = field(default_factory=list)
    plugins_off: tuple[str, ...] = ()
    mcp: dict[str, Any] = field(default_factory=dict)
    instruction_files: list[str] = field(default_factory=list)
    stated: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)


def _mcp_name(name: str) -> str:
    # OpenCode names a tool <server>_<tool>, other characters than letters, digits, _ and - as _
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def _tools(names: tuple[str, ...], refused: list[str], what: str) -> tuple[set[str], set[str]]:
    """(OpenCode tool names, MCP permission actions) of a list of tool names."""
    tools: set[str] = set()
    mcp: set[str] = set()
    for n in names:
        if n in TOOL_EQUIVALENTS:
            tools.update(TOOL_EQUIVALENTS[n])
        elif n in TOOL_PLUGINS:
            tools.add(n)
        elif n.startswith("mcp__"):
            parts = n[len("mcp__"):].split("__", 1)
            server = _mcp_name(parts[0])
            mcp.add(f"{server}_*" if len(parts) == 1 or not parts[1] else f"{server}_{_mcp_name(parts[1])}")
        else:
            refused.append(f"{what}: {n!r} has no OpenCode equivalent")
    return tools, mcp


def _mcp_server(name: str, cfg: Any, refused: list[str]) -> dict[str, Any] | None:
    if not isinstance(cfg, dict):
        refused.append(f"mcp_servers: {name!r} is not an object")
        return None
    kind = cfg.get("type", "stdio")
    if kind == "stdio" and isinstance(cfg.get("command"), str):
        out: dict[str, Any] = {"type": "local", "command": [cfg["command"], *[str(a) for a in cfg.get("args") or []]],
                               "codemode": False}
        if cfg.get("env"):
            out["environment"] = {str(k): str(v) for k, v in cfg["env"].items()}
        return out
    if kind in ("http", "sse") and isinstance(cfg.get("url"), str):
        out = {"type": "remote", "url": cfg["url"], "oauth": False, "codemode": False}
        if cfg.get("headers"):
            out["headers"] = {str(k): str(v) for k, v in cfg["headers"].items()}
        return out
    refused.append(f"mcp_servers: {name!r} ({kind}) has no OpenCode equivalent")
    return None


def _git_root(path: Path) -> Path | None:
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def instruction_files(run_cwd: str, sources: tuple[str, ...], home: Path | None = None) -> list[str]:
    """The instruction files a Claude Code job with these setting sources would load, outermost
    first: ~/.claude/CLAUDE.md for ``user``; for ``project`` the CLAUDE.md files from the job's
    directory up to the repository root (only the directory's own outside a repository), and for
    ``local`` their CLAUDE.local.md siblings."""
    out: list[str] = []
    if "user" in sources:
        f = (home or Path.home()) / ".claude" / "CLAUDE.md"
        if f.is_file():
            out.append(str(f))
    names = [n for s, n in (("project", "CLAUDE.md"), ("local", "CLAUDE.local.md")) if s in sources]
    if names:
        here = Path(run_cwd).resolve()
        root = _git_root(here) or here
        found: list[str] = []
        for d in [here, *here.parents]:
            for n in reversed(names):
                f = d / n
                if f.is_file():
                    found.append(str(f))
            if d == root:
                break
        out += list(reversed(found))
    return out


def translate(pol: Policy, run_cwd: str | None = None) -> Translation:
    """The policy in OpenCode's terms. ``run_cwd`` None skips the instruction files (the check
    run_agent makes before the job exists)."""
    t = Translation()
    refused = t.refused
    allowed, allowed_mcp = _tools(pol.allowed_tools, refused, "allowed_tools")
    disallowed, disallowed_mcp = _tools(pol.disallowed_tools, refused, "disallowed_tools")
    every = set(TOOL_PLUGINS)
    mode = pol.permission_mode
    if mode == "default":
        permitted = set(allowed)
    elif mode == "acceptEdits":
        permitted = set(allowed) | set(EDIT_TOOLS)
    elif mode == "bypassPermissions":
        permitted = set(every)
    elif mode == "plan":
        permitted = set(allowed) - set(NOT_IN_PLAN)
    else:
        refused.append(f"permission_mode: {mode!r} has no OpenCode equivalent")
        permitted = set()
    permitted -= disallowed
    t.permitted = tuple(sorted(permitted & every))
    for name, cfg in pol.mcp_servers.items():
        server = _mcp_server(name, cfg, refused)
        if server is not None:
            t.mcp[_mcp_name(name)] = server
    if pol.strict_mcp_config is False:
        refused.append("strict_mcp_config: false would give the job the operator's own MCP servers; "
                       "an OpenCode job only ever has its own config's")
    bad_sources = [s for s in pol.setting_sources if s not in SOURCES]
    if bad_sources:
        refused.append(f"setting_sources: {', '.join(map(repr, bad_sources))} has no OpenCode equivalent")
    # the rules, last match wins: everything, then each tool not permitted, then the MCP servers'
    # tools not allowed (bypass: all of them), then what a job never has
    rules = [{"action": "*", "resource": "*", "effect": "allow"}]
    rules += [{"action": n, "resource": "*", "effect": "deny"} for n in sorted(every - permitted)]
    if mode != "bypassPermissions":
        for server in sorted(t.mcp):
            rules.append({"action": f"{server}_*", "resource": "*", "effect": "deny"})
        rules += [{"action": a, "resource": "*", "effect": "allow"} for a in sorted(allowed_mcp)]
    rules += [{"action": a, "resource": "*", "effect": "deny"} for a in sorted(disallowed_mcp)]
    rules += [{"action": a, "resource": "*", "effect": "deny"} for a in NEVER_ACTIONS]
    t.permissions = rules
    t.plugins_off = tuple(NEVER_PLUGINS) + tuple(TOOL_PLUGINS[n] for n in sorted(every - permitted))
    if run_cwd is not None:
        t.instruction_files = instruction_files(run_cwd, pol.setting_sources)
    t.stated = [
        f"permission_mode {mode} -> tools {', '.join(t.permitted) or 'none'}",
        f"setting sources {', '.join(pol.setting_sources) or 'none'} -> {len(t.instruction_files)} instruction file(s)",
    ]
    if t.mcp:
        t.stated.append(f"mcp servers {', '.join(sorted(t.mcp))}")
    if pol.thinking is not None or pol.effort is not None:
        t.stated.append(f"thinking {pol.thinking}, effort {pol.effort}: written into each request by the shim")
    return t
