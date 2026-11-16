"""Read-time derivations over the database (spec "Derivations", docs/superpowers/specs/
2026-10-29-mcp-sqlite-schema.md): each reproduces the value the in-memory job fields or the
runner used to keep, computed from the ``requests``/``tool_calls`` rows instead.

Standard library only: ``localagents statusline`` imports this on every status-bar refresh.
Story 1.5b adds the rest: usage totals, counts by tool, the error count, the last tool, and
``summary_from_row`` (the whole job_status dict for a row read from the database)."""

from __future__ import annotations

import functools
import json
import re
import sqlite3
import time

# the tools whose input carries a touched file path: the spec's write rule (runner.py imports
# this one copy) and files_touched's read rule
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# What the tool_calls row stores (runner.py writes it; tests/test_tool_calls_db.py pins it):
# a Bash call's command up to COMMAND_CHARS; any other tool's input JSON up to
# TOOL_INPUT_CHARS (a path-shaped tool stores its whole path, an edit tool without one NULL).
TOOL_INPUT_CHARS = 120
# bounded because a heredoc can carry a whole file; long enough that a command is stored whole
# (the store's transcripts, 2026-11-15: 83% of Bash inputs fit 400 chars of JSON)
COMMAND_CHARS = 2000
# the cut a Bash command was stored at before 2026-11-15: the rows written then are at most
# this long. History, never changed (command_may_be_cut reads it).
COMMAND_CHARS_BEFORE = 120
# what a reader is SHOWN of a Bash command (last_activity, last_tool and the progress lines,
# job_log's tool list, the monitor's tool_call_log): today's form, kept when the store grew
TOOL_DISPLAY_CHARS = 120


def command_may_be_cut(text: str) -> bool:
    """A stored Bash command that may have lost its tail to the cut (a lost tail may hold a
    write, or a test runner): classify_call and feedback._tests_gate read this one rule. A row
    cannot say when it was written (no schema change), so the rule goes by length: exactly
    COMMAND_CHARS_BEFORE (a row from before 2026-11-15, at the old cut) or COMMAND_CHARS and
    longer. Its cost: a command written later that is exactly 120 characters long reads as
    possibly cut too."""
    return len(text) == COMMAND_CHARS_BEFORE or len(text) >= COMMAND_CHARS


def display_input(tool: str | None, text: str | None) -> str | None:
    """A stored input as a reader is shown it: a Bash command's first TOOL_DISPLAY_CHARS,
    any other tool's input as stored (its path, or its JSON already cut at TOOL_INPUT_CHARS)."""
    return text[:TOOL_DISPLAY_CHARS] if tool == "Bash" and text is not None else text


def activity_line(tool: str, text: str | None) -> str:
    """``"<tool>: <shown input>"``, or just the tool when there is no input: the one shape of
    last_tool and of the runner's last_activity, so job_status's two fields read alike."""
    shown = display_input(tool, text)
    return f"{tool}: {shown}" if shown is not None else tool

# the most names a worktree's recorded file list keeps (runner.finish_worktree writes it,
# diff_files reads it); ``worktree.files_n`` counts them all. A story touches tens of files; a
# tree that grew a vendored directory would otherwise put thousands into every list_jobs row.
DIFF_FILES_MAX = 100

# the jobs columns holding the /metrics delta, rebuilt into Job.metrics at read time
_METRICS_COLUMNS = (
    "generated_tokens", "prompt_tokens_processed", "prompt_tokens_cached",
    "cache_hit_ratio", "prompt_tps", "generate_tps", "spec_decode_acceptance",
)

_PROMPT_SUM = "(COALESCE(r.input_tokens, 0) + COALESCE(r.cache_read_tokens, 0) + COALESCE(r.cache_creation_tokens, 0))"
_MESSAGES = "FROM requests r JOIN paths p ON p.id = r.path_id WHERE r.job_id = ? AND p.path = '/v1/messages'"


def tool_call_count(conn: sqlite3.Connection, job_id: str) -> int:
    """Every ToolUseBlock the runner recorded for the job."""
    return conn.execute("SELECT COUNT(*) FROM tool_calls WHERE job_id = ?", (job_id,)).fetchone()[0]


def files_touched(conn: sqlite3.Connection, job_id: str) -> list[str]:
    """DISTINCT edit-tool ``input`` (the path the edit tools were given, as given: relative and
    absolute forms are not merged) in first-seen order (dedup keeping the earliest row). A call
    whose result was an error is left out: the tool reported it wrote nothing (the store's
    failed-only paths are mostly mistyped ones that never existed); a call with no result yet
    (``is_error`` NULL, still running) stays. Shell writes are never here: see diff_files."""
    marks = ", ".join("?" * len(EDIT_TOOLS))
    rows = conn.execute(
        f"SELECT input FROM tool_calls WHERE job_id = ? AND tool IN ({marks}) AND input IS NOT NULL"
        " AND COALESCE(is_error, 0) = 0 GROUP BY input ORDER BY MIN(t)",
        (job_id, *EDIT_TOOLS),
    ).fetchall()
    return [r[0] for r in rows]


def diff_files(worktree: object) -> list[str] | None:
    """The worktree diff's file list as the job's finish recorded it (``worktree.files``: every
    path changed against the base, shell writes included, files git ignores excluded; at most
    DIFF_FILES_MAX names, ``worktree.files_n`` counts them all). None when it is not known: a
    job with no worktree (nothing in a shared directory can be attributed to one job), one not
    finished yet, or one whose diff could not be read. Never an empty list for "unknown"."""
    files = worktree.get("files") if isinstance(worktree, dict) else None
    return list(files) if isinstance(files, list) else None


def summary_worktree(worktree: object) -> object:
    """The worktree as a summary shows it: a copy without ``files``, which the summary carries
    once, as ``diff_files`` (both summaries call this, so they agree)."""
    if not isinstance(worktree, dict):
        return worktree
    return {k: v for k, v in worktree.items() if k != "files"}


def ctx_tokens(conn: sqlite3.Connection, job_id: str) -> int | None:
    """The prompt sum of the latest ``/v1/messages`` row whose prompt sum is non-zero: a null
    usage or a count_tokens call keeps the older figure (the old note_usage rule)."""
    row = conn.execute(
        f"SELECT {_PROMPT_SUM} {_MESSAGES} AND {_PROMPT_SUM} > 0 ORDER BY r.t DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return row[0] if row else None


def usage_totals(conn: sqlite3.Connection, job_id: str) -> dict[str, int]:
    """The four token columns summed over the job's requests rows (NULLs as 0). Replaces the
    SDK's ``ResultMessage.usage`` in ``job_status``: the two count the same traffic from
    different sides and can differ by count_tokens calls and failed requests (spec contract
    note). The keys are the API's names, as ``Job.usage`` carried them."""
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),"
        " COALESCE(SUM(cache_read_tokens), 0), COALESCE(SUM(cache_creation_tokens), 0)"
        " FROM requests WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return {
        "input_tokens": row[0], "output_tokens": row[1],
        "cache_read_input_tokens": row[2], "cache_creation_input_tokens": row[3],
    }


def tool_counts(conn: sqlite3.Connection, job_id: str) -> dict[str, dict[str, int]]:
    """Calls and errors grouped by tool (spec: ``SUM(is_error)``; a pending result counts 0)."""
    rows = conn.execute(
        "SELECT tool, COUNT(*), COALESCE(SUM(is_error), 0) FROM tool_calls WHERE job_id = ? GROUP BY tool",
        (job_id,),
    ).fetchall()
    return {r[0]: {"calls": r[1], "errors": r[2]} for r in rows}


# --- the read share: what each tool call did, by its stored input --------------------------------

# the tools that only look (read a file, search, list)
READ_TOOLS = ("Read", "Grep", "Glob", "LS", "NotebookRead")

# shell commands that only look at files, listings or text on a pipe; the ones with a writing
# flag (sed -i, find -delete/-exec rm, sort -o, uniq IN OUT, awk -i) are refused in _command,
# git and sqlite3 have their own rules there. A program (python, node, uv, bash, a script) is
# never a look, whatever it did inside: its command line does not say.
_LOOKS = frozenset((
    "cat", "head", "tail", "less", "more", "tac", "nl", "grep", "egrep", "fgrep", "rg", "ag",
    "ls", "find", "tree", "wc", "sed", "awk", "gawk", "cut", "sort", "uniq", "tr", "column",
    "fold", "paste", "comm", "join", "diff", "cmp", "stat", "file", "du", "realpath",
    "readlink", "basename", "dirname", "pwd", "which", "jq", "xxd", "od", "hexdump", "strings",
    "md5sum", "sha1sum", "sha256sum", "rev", "expand",
))
# shell words that neither look nor change anything: moving about, setting variables, printing
# a string, testing a condition. A command made only of these looked at nothing ("other").
_NEUTRAL = frozenset((
    "cd", "pushd", "popd", "export", "set", "unset", "shopt", "true", "false", ":", "echo",
    "printf", "test", "[", "[[", "read", "local", "declare", "typeset", "exit", "return",
    "umask", "ulimit", "trap", "hash", "type",
))
# shell keywords that only frame the command after them
_FRAMES = frozenset(("!", "time", "if", "then", "elif", "else", "while", "until", "do", "done", "fi", "esac", "{", "}"))
_GIT_LOOKS = frozenset((
    "show", "diff", "log", "status", "blame", "annotate", "grep", "ls-files", "ls-tree",
    "rev-parse", "merge-base", "cat-file", "describe", "shortlog", "rev-list", "show-ref",
    "for-each-ref", "name-rev", "check-ignore", "diff-tree", "diff-index", "diff-files",
    "count-objects", "whatchanged", "range-diff", "cherry", "show-branch",
))
_GIT_BRANCH_WRITES = frozenset((
    "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force", "-u",
    "--set-upstream-to", "--unset-upstream", "--edit-description", "-t", "--track",
))
_GIT_BRANCH_LISTS = ("--list", "-l", "--contains", "--no-contains", "--merged", "--no-merged",
                     "--points-at", "--format", "--sort")
_NULL_TARGETS = frozenset(("/dev/null", "/dev/stderr", "/dev/stdout", "/dev/tty"))
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=")


def _close_paren(text: str, i: int) -> int:
    """The index of the ``)`` closing a ``(`` opened just before ``i`` (quotes and nesting
    honoured), or -1 when the text ends first."""
    depth, n = 1, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if c == '"' and text[j] == "\\" else 1
            if j >= n:
                return -1
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _lex(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, bool]], bool]:
    """A shell command line as (tokens, substitutions, closed). A token is (kind, value): ``w``
    a word (quotes removed), ``op`` a control operator (``;`` ``&&`` ``||`` ``|`` ``&``, a
    newline), ``out`` a writing redirection (its target is the next word), ``in`` a reading one
    (likewise), ``dup`` an fd duplication (``2>&1``, no target), ``here`` a heredoc or here
    string (the lex stops there: what follows is text, not commands). A substitution is the
    text of each ``$(...)``, backtick or ``<(...)`` with whether it closed. ``closed`` is False
    when the text ends inside a quote or a substitution; the open word is then left out."""
    tokens: list[tuple[str, str]] = []
    subs: list[tuple[str, bool]] = []
    buf: list[str] = []
    inword = False
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal inword
        if inword:
            tokens.append(("w", "".join(buf)))
        buf.clear()
        inword = False

    def subst(start: int) -> int:
        """A ``$(``/``<(`` substitution whose text starts at ``start``: its end, or -1."""
        j = _close_paren(text, start)
        subs.append((text[start:j] if j >= 0 else text[start:], j >= 0))
        buf.append("$()")
        return j + 1 if j >= 0 else -1

    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 >= n:
                return tokens, subs, False
            if text[i + 1] != "\n":  # a backslash-newline joins the lines
                buf.append(text[i + 1])
                inword = True
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return tokens, subs, False
            buf.append(text[i + 1:j])
            inword, i = True, j + 1
        elif c == '"':
            i += 1
            inword = True
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                elif text.startswith("$((", i):
                    j = _close_paren(text, i + 2)
                    if j < 0:
                        return tokens, subs, False
                    i = j + 1
                elif text.startswith("$(", i):
                    i = subst(i + 2)
                    if i < 0:
                        return tokens, subs, False
                elif text[i] == "`":
                    j = text.find("`", i + 1)
                    subs.append((text[i + 1:j] if j >= 0 else text[i + 1:], j >= 0))
                    if j < 0:
                        return tokens, subs, False
                    i = j + 1
                else:
                    buf.append(text[i])
                    i += 1
            if i >= n:
                return tokens, subs, False
            i += 1
        elif text.startswith("$((", i):  # arithmetic: a word, no command in it
            j = _close_paren(text, i + 2)
            if j < 0:
                return tokens, subs, False
            buf.append(text[i:j + 1])
            inword, i = True, j + 1
        elif text.startswith("$(", i) or (text.startswith("<(", i) and not inword) or (text.startswith(">(", i) and not inword):
            inword = True
            i = subst(i + 2)
            if i < 0:
                return tokens, subs, False
        elif text.startswith("${", i):
            j = text.find("}", i)
            if j < 0:
                return tokens, subs, False
            buf.append(text[i:j + 1])
            inword, i = True, j + 1
        elif c == "`":
            j = text.find("`", i + 1)
            subs.append((text[i + 1:j] if j >= 0 else text[i + 1:], j >= 0))
            if j < 0:
                return tokens, subs, False
            buf.append("``")
            inword, i = True, j + 1
        elif c in " \t":
            flush()
            i += 1
        elif c == "#" and not inword:
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c == "\n" or c == ";":
            flush()
            tokens.append(("op", c))
            i += 1
        elif c == "<" or c == ">":
            if inword and "".join(buf).isdigit():  # an fd number: 2>, 1>>
                buf.clear()
                inword = False
            flush()
            if c == "<":
                if text.startswith("<<", i):
                    tokens.append(("here", "<<"))
                    return tokens, subs, True
                j = i + 1
                if text.startswith("&", j):
                    j += 1
                    while j < n and (text[j].isdigit() or text[j] == "-"):
                        j += 1
                    tokens.append(("dup", text[i:j]))
                else:
                    tokens.append(("in", "<"))
                i = j
            else:
                j = i + 1
                if j < n and text[j] in ">|":
                    j += 1
                if text.startswith("&", j):
                    k = j + 1
                    while k < n and (text[k].isdigit() or text[k] == "-"):
                        k += 1
                    if k > j + 1:
                        tokens.append(("dup", text[i:k]))
                        i = k
                        continue
                    j += 1  # >&file writes both streams to file
                tokens.append(("out", text[i:j]))
                i = j
        elif c == "&":
            flush()
            if text.startswith("&&", i):
                tokens.append(("op", "&&"))
                i += 2
            elif text.startswith("&>", i):
                j = i + 3 if text.startswith("&>>", i) else i + 2
                tokens.append(("out", text[i:j]))
                i = j
            else:
                tokens.append(("op", "&"))
                i += 1
        elif c == "|":
            flush()
            j = i + 2 if text.startswith(("||", "|&"), i) else i + 1
            tokens.append(("op", text[i:j]))
            i = j
        elif c in "()":
            flush()
            tokens.append(("op", c))
            i += 1
        else:
            buf.append(c)
            inword = True
            i += 1
    flush()
    return tokens, subs, True


def _git(args: list[str]) -> str:
    """git's verdict: a look for the subcommands that only read the repository (and the list
    forms of branch, worktree, stash, remote, config, tag, reflog), else other."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
    if i >= len(args):
        return "neutral"  # git --version: no subcommand (a cut one is unknown by the cut)
    sub, rest = args[i], args[i + 1:]
    first = rest[0] if rest else None
    if sub in _GIT_LOOKS:
        return "other" if sub == "diff" and any(r.startswith("--output") for r in rest) else "look"
    if sub == "branch":
        if any(r in _GIT_BRANCH_WRITES or r.startswith("--set-upstream-to=") for r in rest):
            return "other"
        named = [r for r in rest if not r.startswith("-")]
        listing = any(r.startswith(_GIT_BRANCH_LISTS) for r in rest)
        return "other" if named and not listing else "look"  # `git branch new` makes one
    looks = {
        "worktree": first == "list",
        "stash": first in ("list", "show"),
        "remote": first in (None, "-v", "--verbose", "show", "get-url"),
        "config": any(r in ("--get", "--get-all", "--get-regexp", "--list", "-l") for r in rest),
        "tag": first in (None, "-l", "--list"),
        "reflog": first not in ("expire", "delete"),
    }
    return "look" if looks.get(sub) else "other"


def _command(words: list[str]) -> str:
    """One simple command's verdict from its words: ``look``, ``neutral``, ``other`` or
    ``unknown`` (a construct this rule does not follow)."""
    while words and (_ASSIGN.match(words[0]) or words[0] in _FRAMES):
        words = words[1:]
    if not words:
        return "neutral"
    w, args = words[0], words[1:]
    if w in ("for", "select"):
        return "neutral"  # the loop's word list; its substitutions are classified on their own
    if w in ("case", "function"):
        return "unknown"
    if w == "command" and args[:1] == ["-v"]:
        return "neutral"
    if w in ("timeout", "env", "nice", "nohup", "command", "exec", "stdbuf", "xargs"):
        rest = list(args)
        takes_value = {"timeout": ("-s", "-k"), "env": ("-u", "-C"), "nice": ("-n",),
                       "xargs": ("-I", "-n", "-P", "-L", "-d", "-s", "-a", "-E")}.get(w, ())
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in takes_value else rest[1:]
        if w == "timeout" and rest:
            rest = rest[1:]  # the duration
        return _command(rest) if rest else "neutral"
    if w in _NEUTRAL:
        return "neutral"
    if w == "git":
        return _git(args)
    if w == "sqlite3":
        return "look" if "-readonly" in args else "other"
    if w not in _LOOKS:
        return "other"
    flags = [a for a in args if a.startswith("-") and not a.startswith("--")]
    if w == "sed" and (any("i" in f for f in flags) or any(a.startswith("--in-place") for a in args)):
        return "other"
    if w in ("awk", "gawk") and ("-i" in args or "--include" in args):
        return "other"  # gawk -i inplace
    if w == "sort" and (any("o" in f for f in flags) or any(a.startswith("--output") for a in args)):
        return "other"
    if w == "uniq":
        named, skip = [], False
        for a in args:
            if skip:
                skip = False
            elif a in ("-f", "-s", "-w"):
                skip = True
            elif not a.startswith("-"):
                named.append(a)
        return "other" if len(named) > 1 else "look"  # uniq IN OUT writes OUT
    if w == "find":
        if any(a in ("-delete", "-fprint", "-fprint0", "-fprintf", "-fls") for a in args):
            return "other"
        verdicts = []
        for k, a in enumerate(args):
            if a in ("-exec", "-execdir", "-ok", "-okdir"):
                inner = []
                for b in args[k + 1:]:
                    if b in (";", "+"):
                        break
                    inner.append(b)
                verdicts.append(_command(inner))
        for v in ("other", "unknown"):
            if v in verdicts:
                return v
    return "look"


def _segment(toks: list[tuple[str, str]]) -> str:
    """A simple command's verdict with its redirections: a write to anything but /dev/null
    (and the std streams) is other; a writing redirection whose target is not visible (the cut
    took it) is unknown unless the command is other already."""
    words: list[str] = []
    blind = False
    k = 0
    while k < len(toks):
        kind, value = toks[k]
        if kind == "here":
            return "other"
        if kind in ("out", "in"):
            target = toks[k + 1][1] if k + 1 < len(toks) and toks[k + 1][0] == "w" else None
            if kind == "out":
                if target is None:
                    blind = True
                elif target not in _NULL_TARGETS:
                    return "other"
            k += 2 if target is not None else 1
            continue
        if kind == "w":
            words.append(value)
        k += 1
    v = _command(words)
    return "unknown" if blind and v != "other" else v


@functools.lru_cache(maxsize=16384)
def _shell_verdict(text: str, cut: bool) -> str:
    """The whole command line's verdict: other when any part visibly runs something that is not
    a look or writes; else unknown when the line was cut (its unseen rest may write) or holds a
    construct the rule does not follow; else look when some part looked; else neutral."""
    tokens, subs, closed = _lex(text)
    if not closed and not cut:
        return "unknown"
    if cut and closed and tokens and tokens[-1][0] != "here":
        # the last token may itself be cut ("gre" of "grep") and is left out, except a
        # redirection target no null target starts with: a cut "t/s" still names a file
        last = tokens[-1]
        written = (last[0] == "w" and len(tokens) > 1 and tokens[-2][0] == "out"
                   and not any(t.startswith(last[1]) for t in _NULL_TARGETS))
        if not written:
            tokens = tokens[:-1]
    verdicts = [_verdict_of_parts(tokens)]
    verdicts += [_shell_verdict(t, cut or not ok) for t, ok in subs]
    if "other" in verdicts:
        return "other"
    if cut or "unknown" in verdicts:
        return "unknown"
    return "look" if "look" in verdicts else "neutral"


def _verdict_of_parts(tokens: list[tuple[str, str]]) -> str:
    """The tokens split at the control operators, each part judged by _segment, combined as
    _shell_verdict combines (other, then unknown, then look, else neutral)."""
    parts: list[list[tuple[str, str]]] = [[]]
    for tok in tokens:
        if tok[0] == "op":
            parts.append([])
        else:
            parts[-1].append(tok)
    verdicts = {_segment(p) for p in parts if p}
    for v in ("other", "unknown", "look"):
        if v in verdicts:
            return v
    return "neutral"


_SHELL_KIND = {"look": "read", "neutral": "other", "other": "other", "unknown": "unclassified"}
# tool_mix's one statement (tests/test_query_plans.py pins its plan: the job_id index)
_TOOL_MIX_SQL = "SELECT tool, input FROM tool_calls WHERE job_id = ?"


def classify_call(tool: str | None, input_text: str | None) -> str:
    """What one tool call did, from its tool name and its stored ``input``: ``read``, ``edit``,
    ``other`` or ``unclassified``. The rule is closed:

    - an edit tool (EDIT_TOOLS) is ``edit``; Read, Grep, Glob, LS, NotebookRead are ``read``;
      every other non-Bash tool (TodoWrite, Task*, MCP tools, web tools) is ``other``.
    - a Bash command is ``read`` when every part of it (split at ``;`` ``&&`` ``||`` ``|``
      ``&`` and newlines, with ``$(...)``/backtick substitutions judged the same way) is a
      look (_LOOKS, git's reading subcommands, ``sqlite3 -readonly``) or neutral (cd, export,
      variable assignments, echo, test, loop and if keywords), at least one part looked, and no
      redirection writes anywhere but /dev/null.
    - a Bash command is ``other`` when some visible part runs anything else (a program, a test
      run, a script, git add/commit, mkdir/cp/rm, kill, sleep), writes (``>`` to a file, tee,
      ``sed -i``, ``find -exec rm``, a heredoc ``cat > f <<EOF``), or when it only moved
      about and printed (cd, echo) and looked at nothing. A shell write is ``other``, never
      ``edit``: ``edit`` counts the edit tools, as files_touched does.
    - ``unclassified`` when the stored command may be cut (command_may_be_cut: the unseen
      rest could write) and nothing visible already makes it ``other``; when it holds a
      construct the rule does not follow (``case``, a function definition, an unclosed quote in
      a command that was not cut); or when no command was stored."""
    if tool in EDIT_TOOLS:
        return "edit"
    if tool in READ_TOOLS:
        return "read"
    if tool != "Bash":
        return "other"
    if not input_text or not input_text.strip():
        return "unclassified"
    return _SHELL_KIND[_shell_verdict(input_text, command_may_be_cut(input_text))]


def tool_mix(conn: sqlite3.Connection | None, job_id: str, output_tokens: int) -> dict:
    """The job's tool calls by what they did (classify_call): ``reads``, ``edits``, ``other``,
    ``unclassified`` (counts that sum to the calls classified, all read in one statement), with
    ``read_share`` (reads over all of them: a floor when some are unclassified, since an
    unclassified call may have been a read) and ``output_tokens_per_call`` (``output_tokens``,
    the caller's usage_totals figure, over the same calls). Both ratios are None for a job with
    no tool calls. ``conn`` None (a Job built outside JobStore.new) reads as no calls."""
    counts = {"read": 0, "edit": 0, "other": 0, "unclassified": 0}
    if conn is not None:
        for row in conn.execute(_TOOL_MIX_SQL, (job_id,)):
            counts[classify_call(row[0], row[1])] += 1
    calls = sum(counts.values())
    return {
        "reads": counts["read"], "edits": counts["edit"], "other": counts["other"],
        "unclassified": counts["unclassified"],
        "read_share": round(counts["read"] / calls, 3) if calls else None,
        "output_tokens_per_call": round(output_tokens / calls, 1) if calls else None,
    }


def error_count(conn: sqlite3.Connection, job_id: str) -> int:
    """Tool calls whose matching result carried is_error."""
    return conn.execute("SELECT COALESCE(SUM(is_error), 0) FROM tool_calls WHERE job_id = ?", (job_id,)).fetchone()[0]


def last_tool(conn: sqlite3.Connection, job_id: str) -> str | None:
    """activity_line of the latest tool_calls row (``"<tool>: <shown input>"``, a Bash command's
    first TOOL_DISPLAY_CHARS; a NULL input leaves just the name): the progress line's view of
    what the job is doing right now."""
    row = conn.execute(
        "SELECT tool, input FROM tool_calls WHERE job_id = ? ORDER BY t DESC, rowid DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return None if row is None else activity_line(row["tool"], row["input"])


def gen_tps(conn: sqlite3.Connection, job_id: str) -> float | None:
    """Replay of the note_usage EMA, ``0.5*prev + 0.5*(output / span)``, over the job's
    ``/v1/messages`` rows in t order; span is gen_ms when set, else ms, and rows without output
    or span are skipped exactly as note_usage skipped them."""
    ema: float | None = None
    for r in conn.execute(f"SELECT r.t, r.ms, r.gen_ms, r.output_tokens {_MESSAGES} ORDER BY r.t", (job_id,)).fetchall():
        out = r["output_tokens"] or 0
        span = r["gen_ms"] if r["gen_ms"] else r["ms"]
        if out <= 0 or not span or span <= 0:
            continue
        tps = out / (span / 1000)
        ema = tps if ema is None else 0.5 * ema + 0.5 * tps
    return ema


def prefill_tps(conn: sqlite3.Connection, job_id: str) -> float | None:
    """Processed prompt tokens (input + cache_creation) per second of prefill (ms − gen_ms),
    summed over the job's ``/v1/messages`` rows — a plain ratio over the job. Rows join when
    ``input_tokens``, ``ms`` and ``gen_ms`` are numbers, ms > gen_ms and the processed tokens
    (input + cache_creation, a NULL cache_creation counting 0) are > 0: the page's own rule
    (app.js prefillRate). A missing cache_creation counts as 0 since story 3.10i — a backend
    with no cache fields in its usage (vLLM's Anthropic shape, until --enable-prompt-tokens-
    details) otherwise computes no figure at all — and the figure is then an UPPER bound: the
    engine's own prefix cache is invisible to us. Not an EMA replay like gen_tps: decode's EMA
    is a replay of the status line's note_usage history, and prefill has no such history to
    match — a ratio is what "tokens processed per second of prefill" means (story 3.10a)."""
    tokens = seconds = 0.0
    for r in conn.execute(
        f"SELECT r.ms, r.gen_ms, r.input_tokens, r.cache_creation_tokens {_MESSAGES} ORDER BY r.t",
        (job_id,),
    ).fetchall():
        inp, cc, ms, gen = r["input_tokens"], r["cache_creation_tokens"], r["ms"], r["gen_ms"]
        if inp is None or ms is None or gen is None or ms <= gen or inp + (cc or 0) <= 0:
            continue
        tokens += inp + (cc or 0)
        seconds += (ms - gen) / 1000
    return tokens / seconds if seconds else None


def server_version(conn: sqlite3.Connection, server_id: str | None) -> str | None:
    """The build stamp of the MCP server that ran the job (story 1.14): its ``mcp_servers``
    row's ``version``, written once at that server's start (``db.build_stamp``). None when the
    job names no server or the row is gone."""
    if not server_id:
        return None
    row = conn.execute("SELECT version FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
    return row[0] if row is not None else None


def _truncate(task: str | None) -> str | None:
    """job_status shows the brief's first 300 chars, as Job.summary always did."""
    return task if task is None or len(task) < 300 else task[:300] + "…"


def summary_from_row(conn: sqlite3.Connection, row: sqlite3.Row, include_result: bool = True,
                     now: float | None = None) -> dict:
    """The full ``job_status`` dict for a ``jobs`` row read from the database — any server's
    job: every column under its Job field name, the lookup ids resolved to model/endpoint/
    base_url, worktree parsed from its JSON, metrics rebuilt from the seven columns or None,
    plus every read-time derivation. Storage details (mcp_server_id, endpoint_id, model_id)
    stay out; the id identifies the job (3.10f-1 dropped ``name``). Same keys as
    ``Job.summary()`` for the same job.

    A running job's ``elapsed_s`` is computed against ``now``, the caller's clock (a route passes
    its own; the seed's world runs on ``--now``), and only against ``time.time()`` when no clock
    is given. A finished job's elapsed is its own span either way."""
    from .db import state_dir  # lazy: the statusline path keeps this module light

    job_id = row["id"]
    model = (
        conn.execute("SELECT served_id FROM models WHERE id = ?", (row["model_id"],)).fetchone()
        if row["model_id"] is not None else None
    )
    ep = (
        conn.execute("SELECT name, base_url FROM endpoints WHERE id = ?", (row["endpoint_id"],)).fetchone()
        if row["endpoint_id"] is not None else None
    )
    worktree = json.loads(row["worktree"]) if row["worktree"] is not None else None
    # the schema has no NOT NULL: name, created_at and worktree.path are read defensively
    wt_path = worktree.get("path") if isinstance(worktree, dict) else None
    start = row["started_at"] or row["created_at"]
    usage = usage_totals(conn, job_id)
    d: dict = {
        "id": job_id,
        "task": _truncate(row["task"]),
        "model": model["served_id"] if model is not None else None,
        "endpoint": ep["name"] if ep is not None else None,
        "base_url": ep["base_url"] if ep is not None else None,
        "cwd": row["cwd"],
        "run_cwd": wt_path or row["cwd"],
        "isolation": row["isolation"],
        "status": row["status"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "session_id": row["session_id"],
        "result": row["result"],
        "error": row["error"],
        "num_turns": row["num_turns"],
        "compactions": row["compactions"],
        "worktree": summary_worktree(worktree),
        "diff_files": diff_files(worktree),
        "last_activity": row["last_activity"],
        "log_dir": str(state_dir() / "jobs" / job_id),
        "dump": bool(row["dump"]),
        "log_events": bool(row["log_events"]),
        "context": row["context"],
        # router 12: shown on both sides (Job._SUMMARY_HIDDEN no longer hides them)
        "priority": row["priority"],
        "calibration": bool(row["calibration"]),
        "routed_by": json.loads(row["routed_by"]) if row["routed_by"] is not None else None,
        "metrics": None if all(row[c] is None for c in _METRICS_COLUMNS) else {c: row[c] for c in _METRICS_COLUMNS},
        # the spec's read-time derivations (the in-memory duplicates story 1.5b removed from Job)
        "usage": usage,
        "tool_calls": tool_call_count(conn, job_id),
        "tool_counts": tool_counts(conn, job_id),
        "tool_mix": tool_mix(conn, job_id, usage["output_tokens"]),
        "errors": error_count(conn, job_id),
        "last_tool": last_tool(conn, job_id),
        "files_touched": files_touched(conn, job_id),
        "ctx_tokens": ctx_tokens(conn, job_id),
        "gen_tps": gen_tps(conn, job_id),
        "prefill_tps": prefill_tps(conn, job_id),
        "server_version": server_version(conn, row["mcp_server_id"]),
    }
    end = row["finished_at"] if row["finished_at"] is not None else (time.time() if now is None else now)
    d["elapsed_s"] = round(end - start, 1) if start is not None else 0
    if not include_result:
        d.pop("result")
    return d
