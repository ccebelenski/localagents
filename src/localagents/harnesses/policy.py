"""A job's session policy, read once (Epic 6.8): the run_agent arguments resolved against the
config's defaults, in one place, for every harness to translate in its own module.

The values are in the vocabulary the arguments and models.yaml have always used — Claude Code's
(``permission_mode``: default | acceptEdits | bypassPermissions | plan; tool names ``Read``,
``Bash``, ``mcp__<server>[__<tool>]``; setting sources user | project | local): Claude Code takes
them as they are (harnesses/claude_code.py), OpenCode translates each one and refuses what has no
equivalent (harnesses/opencode_policy.py). The resolution is the one Claude Code's options always
used: the caller's value, else the config's (``allowed_tools``: an empty list is the config's too;
``setting_sources``: an empty list is the caller's own — a calibration job's "none").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import SessionSpec


@dataclass(frozen=True)
class Policy:
    permission_mode: str
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]
    max_turns: int
    config_append: str  # the config's system_prompt_append ("" when none)
    caller_append: str  # the call's ("" when none)
    setting_sources: tuple[str, ...]
    background: bool
    thinking: bool | None
    effort: str | None
    mcp_servers: dict[str, Any]
    strict_mcp_config: bool | None


def resolve(spec: SessionSpec) -> Policy:
    d = spec.defaults
    return Policy(
        permission_mode=spec.permission_mode or d.permission_mode,
        allowed_tools=tuple(spec.allowed_tools or d.allowed_tools),
        disallowed_tools=tuple(d.disallowed_tools),
        max_turns=spec.max_turns or d.max_turns,
        config_append=d.system_prompt_append or "",
        caller_append=spec.system_prompt_append or "",
        setting_sources=tuple(d.setting_sources if spec.setting_sources is None else spec.setting_sources),
        background=spec.background,
        thinking=spec.thinking,
        effort=spec.effort,
        mcp_servers=dict(spec.mcp_servers or {}),
        strict_mcp_config=spec.strict_mcp_config,
    )
