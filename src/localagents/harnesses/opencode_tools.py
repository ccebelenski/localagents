"""OpenCode's tools as the shared vocabulary reads them (Epic 6.5). Stdlib only: derive and the
status line import it. The names are the ones opencode v2.3.1 offered a headless session in the
6.2 captures (tests/fixtures/opencode/wire/request-shapes.json): edit, write, read, glob, grep,
shell, webfetch, websearch, subagent, question, skill, execute, experimental_image_generate;
inputs carry ``path`` and a shell ``command`` (events/strata-tools.jsonl). OpenCode names an MCP
tool ``<server>_<tool>`` with no fixed prefix, so an MCP call is ``other`` here unless it went
through ``execute`` (code mode), which is ``mcp``."""

from .tools import Vocabulary

VOCABULARY = Vocabulary(
    harness="opencode",
    kinds={
        "edit": "edit", "write": "edit",
        "read": "read", "glob": "read", "grep": "read",
        "shell": "shell",
        "webfetch": "web", "websearch": "web",
        "subagent": "agent",
        "execute": "mcp",
    },
    path_keys=("path",),
    command_key="command",
)
