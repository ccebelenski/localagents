"""Claude Code's tools as the shared vocabulary reads them (Epic 6.5). Stdlib only: derive and the
status line import it. The edit and read names are the ones derive held before 6.5 (EDIT_TOOLS,
READ_TOOLS); MCP tools are ``mcp__<server>__<tool>``; inputs carry ``file_path``,
``notebook_path`` or ``path`` and a shell ``command``."""

from .tools import Vocabulary

VOCABULARY = Vocabulary(
    harness="claude-code",
    kinds={
        # the tools whose input carries a touched file path: the write rule and files_touched
        "Edit": "edit", "Write": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
        # the tools that only look (read a file, search, list)
        "Read": "read", "Grep": "read", "Glob": "read", "LS": "read", "NotebookRead": "read",
        "Bash": "shell",
        "WebFetch": "web", "WebSearch": "web",
        "Agent": "agent", "Task": "agent",
    },
    path_keys=("file_path", "notebook_path", "path"),
    command_key="command",
    mcp_prefix="mcp__",
)
