"""The harness a job's session runs in (Epic 6.3): one module per harness, the seam here.

runner.run_job owns everything every harness shares — the worktree, the metrics, the session's
clock, the status, the cancel and the finish — and hands the session itself to the harness:
``await harness.run(job, res, spec)`` runs it to its end, applying what it does to the job
(transcript lines, turns, tool calls and results, the session id, the result or error) through
the Job's own methods, and raises what its session raises. ``spec`` carries the job's resolved
settings; a harness translates the ones it has and states the ones it has not (6.8).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Any

from ..registry import Defaults


@dataclass(frozen=True)
class SessionSpec:
    """A job's session settings as run_job resolved them: ``background`` is resolved against the
    config, the rest are the caller's values (None: the config's, which the harness reads from
    ``defaults``)."""

    defaults: Defaults
    shim_base_url: str
    background: bool
    permission_mode: str | None = None
    max_turns: int | None = None
    allowed_tools: list[str] | None = None
    system_prompt_append: str | None = None
    resume_session: str | None = None
    mcp_servers: dict[str, Any] | None = None
    thinking: bool | None = None
    effort: str | None = None
    strict_mcp_config: bool | None = None
    setting_sources: list[str] | None = None


def _modules() -> dict[str, ModuleType]:
    # imported here, not at the top: a harness module imports SessionSpec from this package
    from . import claude_code

    return {claude_code.NAME: claude_code}


DEFAULT = "claude-code"


def names() -> list[str]:
    return sorted(_modules())


def get(name: str) -> ModuleType:
    mods = _modules()
    if name not in mods:
        raise ValueError(f"unknown harness {name!r}; one of {', '.join(sorted(mods))}")
    return mods[name]
