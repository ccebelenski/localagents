"""The tool vocabulary every harness shares (Epic 6.5): what a tool call did, by kind, never by
one harness's tool names.

A harness names its tools its own way (Claude Code's ``Bash``/``Edit``/``Read``, OpenCode's
``shell``/``edit``/``read``) and passes their inputs under its own keys (``file_path`` vs
``path``). Each harness keeps its own table in its own light module (claude_code_tools.py,
opencode_tools.py: stdlib only, because derive and the status line read them); this module holds
the kinds, the lookups, and the one write rule of what a ``tool_calls`` row stores, which both
harnesses' message readers call. Readers (derive, the router's feedback, the monitor) ask for a
job's vocabulary by its ``jobs.harness`` (schema 14) and read kinds through it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# what a call did. edit: wrote a file through the harness's own edit tools (files_touched counts
# their paths); read: only looked (a file, a search, a listing); shell: ran a command (its stored
# command is classified on its own: derive.classify_call); web, agent, mcp: as named; other:
# anything else (a todo list, a question, a tool the table does not know)
KINDS = ("edit", "read", "shell", "web", "agent", "mcp", "other")

# the stored cuts (moved here from derive with 6.5: the write rule's own numbers; derive and
# feedback read them back). A shell command is stored whole up to COMMAND_CHARS (the store's
# transcripts, 2026-11-15: 83% of Bash inputs fit 400 chars of JSON); any other tool's input
# JSON up to TOOL_INPUT_CHARS.
TOOL_INPUT_CHARS = 120
COMMAND_CHARS = 2000


@dataclass(frozen=True)
class Vocabulary:
    """One harness's tools: ``kinds`` maps each tool name it has to a kind (a name not in it is
    ``other``, or ``mcp`` under ``mcp_prefix``); ``path_keys`` are the input keys that carry a
    file path, in the order they are tried; ``command_key`` the key of a shell command."""

    harness: str
    kinds: dict[str, str]
    path_keys: tuple[str, ...]
    command_key: str
    mcp_prefix: str | None = None
    _by_kind: dict[str, tuple[str, ...]] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        bad = sorted(k for k in self.kinds.values() if k not in KINDS)
        if bad:
            raise ValueError(f"{self.harness}: unknown tool kinds {bad}")
        by: dict[str, list[str]] = {}
        for name, kind in self.kinds.items():
            by.setdefault(kind, []).append(name)
        self._by_kind.update({k: tuple(v) for k, v in by.items()})

    def kind(self, tool: str | None) -> str:
        if tool is None:
            return "other"
        k = self.kinds.get(tool)
        if k is not None:
            return k
        if self.mcp_prefix and tool.startswith(self.mcp_prefix):
            return "mcp"
        return "other"

    def names(self, kind: str) -> tuple[str, ...]:
        """The harness's tool names of one kind, in the table's order (an SQL ``IN`` list)."""
        if kind not in KINDS:
            raise ValueError(f"unknown tool kind {kind!r}")
        return self._by_kind.get(kind, ())

    def stored_input(self, tool: str, tool_input: Any) -> str | None:
        """The write rule (spec; tests/test_tool_calls_db.py pins it): a shell call's command
        whole up to COMMAND_CHARS (the read share and the tests gate read it); a string under
        the command key of any other tool at the JSON cut; a path-shaped tool's path whole (the
        edit tools and the readers), so a read on the status row names the file; an edit tool
        whose input carries no path NULL; any other input's JSON cut at TOOL_INPUT_CHARS."""
        short = json.dumps(tool_input, default=str)
        if not isinstance(tool_input, dict):
            return short[:TOOL_INPUT_CHARS]
        command = tool_input.get(self.command_key)
        if isinstance(command, str):
            return command[:COMMAND_CHARS if self.kind(tool) == "shell" else TOOL_INPUT_CHARS]
        for key in self.path_keys:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return value
        if self.kind(tool) == "edit":
            return None
        return short[:TOOL_INPUT_CHARS]


def _tables() -> dict[str, Vocabulary]:
    from . import claude_code_tools, opencode_tools

    return {v.harness: v for v in (claude_code_tools.VOCABULARY, opencode_tools.VOCABULARY)}


DEFAULT = "claude-code"


def vocabulary(harness: str | None) -> Vocabulary:
    """A harness's vocabulary; None (a row from before schema 14 read through an old path, a Job
    built by hand) is Claude Code's, the harness every such job ran in. A name this install does
    not know (a newer install's job in a shared store) gets an EMPTY vocabulary: every tool reads
    as ``other``, so no reader guesses another harness's names into edits or reads, and none
    fails (the 6.5 review: the monitor's snapshot would have raised on one such row)."""
    tables = _tables()
    name = harness or DEFAULT
    if name not in tables:
        return Vocabulary(harness=name, kinds={}, path_keys=(), command_key="command")
    return tables[name]
