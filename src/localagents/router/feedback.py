"""Feedback (docs/superpowers/specs/2026-11-02-router-design.md §9): the two feeds that write
evidence for the pass (§8) and never a cell.

- **Mechanical gates**, always on: ``on_finish`` runs in the runner's guarded finish step and
  writes ``jobs.gates`` from the job's record and, for a worktree job only, its worktree diff
  (a non-isolated job's diff mixes the operator's and other jobs' changes). It also writes the
  job's ``speed_samples`` row, the capacity curve's refinement evidence (§7). It never raises
  and never moves the job's status or error: a failure is a ``[feedback-error]`` line.
- **The verdict**, explicit: ``apply_verdict`` (the ``verdict`` tool, §11) upserts the
  coordinator's accepted/rejected with the per-axis detail, scores a directly named job's brief
  with d3 now so the demand bands exist (a ticket flagged ``scored_at_verdict``), and logs a
  category correction with the ticket's definitions version.

``jobs.gates`` is the shape ``scorecard.observations`` and ``scorecard._ceiling`` read
(scorecard.py's pinned wire shapes): ``tests``, ``stop_rule``, ``not_capped``, ``no_tmp``,
``files_within``, ``lint``, each true (clean), false, or null (could not be evaluated, never
counted as a pass), plus ``files_n`` (the diff's file count, the ceiling's evidence) and
``lint_detail`` (which lint rule fired, where)."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from localagents import db, derive
from localagents.jobs import TERMINAL_STATUSES
from localagents.registry import backend_kind
from localagents.router import capacity, definitions, scorecard
from localagents.router.decision import BriefUnresolved, DecisionUnavailable, resolve_brief, score

# the gate keys scorecard.observations reads (its _RATE_GROUPS); files_n and lint_detail ride beside
GATES = ("tests", "stop_rule", "not_capped", "no_tmp", "files_within", "lint")
# the hints' three lint rules a model ignores, by the name lint_detail records
LINT_RULES = ("constant_after_use", "rethrow_wrapper", "literal_tab")
LINT_DETAIL_MAX = 20  # a tab-indented file would otherwise put one entry per line in the row
GIT_TIMEOUT_S = 60.0

# a test runner named in a brief or run through Bash; the edges keep test_pytest.py and
# pytest-cov from reading as a run
_TEST_RUNNER = re.compile(
    r"(?<![\w-])(?:pytest|py\.test|python3? -m (?:pytest|unittest)|tox|nox"
    r"|(?:npm|pnpm|yarn|bun) (?:run )?test|npx (?:jest|vitest|mocha)|jest|vitest|mocha"
    r"|node --test|cargo test|go test|make (?:test|check)|ctest|mvn test|gradlew? test"
    r"|rspec|phpunit|dotnet test)(?![\w-])")
# the report's claim that the job stopped instead of doing the work (the hints' stop rule:
# "stop and report rather than inventing it; no code change is fine"). Narrow on purpose: an
# ordinary end-of-brief report says "stopped after the last commit", which is not this claim.
_STOP_CLAIM = re.compile(
    r"\bstop(?:ped|ping)?\b[^.\n]{0,60}?\b(?:rather\s+than|instead\s+of)\b"
    r"|\bno\s+code\s+changes?\b|\bmade\s+no\s+(?:code\s+)?changes\b"
    r"|\bdid\s+not\s+(?:change|edit|modify)\s+any", re.IGNORECASE)
# /tmp as a path, not inside a longer word or path (a/tmp, /tmpfs)
_TMP = re.compile(r"(?<![\w.~-])/tmp(?![\w-])")
# a brief's file-like token: has a slash, or a name with a letter-led extension
_TOKEN_SPLIT = re.compile(r"[\s`'\"()\[\]<>{},;:|]+")
_HAS_EXTENSION = re.compile(r"^[\w.-]*\w\.[A-Za-z][A-Za-z0-9]{0,7}$")
# files whose format needs a literal tab
_TAB_FORMATS = ("Makefile", "GNUmakefile", "makefile")
_TAB_SUFFIXES = (".mk", ".go", ".tsv")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# constants after use: an UPPER_CASE name assigned on its own line, in Python, JS/TS, Java/C#
_CONST_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:(?:const|let|var|final|static|public|private|protected|readonly)\s+)*"
    r"(?:[\w.<>\[\]]+\s+)?([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z][A-Z0-9]+)\s*(?::\s*[^=]+?)?\s*=(?!=)(.*)$")
_COMMENT = re.compile(r"^\s*(?:#|//|/\*|\*)")
# try{}catch(e){throw e}: the C-family shape over the added text, and Python's except/raise
_C_RETHROW = re.compile(
    r"\bcatch\s*\(\s*(?:[\w.$<>\[\]|]+\s+)?(\w+)(?:\s*:\s*[\w.$]+)?\s*\)\s*\{\s*throw\s+\1\s*;?\s*\}"
    r"|\bcatch\s*\{\s*throw\s*;\s*\}")
_PY_EXCEPT = re.compile(r"^(\s*)except\b(?:[^:#]*?\bas\s+(\w+))?[^:#]*:\s*"
                        r"(?P<inline>raise(?:\s+(?P<raised>\w+))?\s*)?(?:#.*)?$")
_PY_RAISE = re.compile(r"^\s*raise(?:\s+(\w+))?\s*(?:#.*)?$")


# --- the record's reads ----------------------------------------------------------------------

def sessions_at_start(conn, endpoint_id: int, now: float) -> int:
    """The sessions on the endpoint when a job starts: ``jobs`` rows with ``status='running'``
    on it that had started by ``now`` (the starting job's own row included, since run_job
    writes it running first). This is the module seam router Task 13 owns; Task 12 may rewire
    it to the queue's occupancy (``queue.occupancy(conn, endpoint_id, now)``, Task 11) later."""
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE endpoint_id = ? AND status = 'running'"
        " AND (started_at IS NULL OR started_at <= ?)", (endpoint_id, now)).fetchone()[0]


def note_start(job) -> int | None:
    """``sessions_at_start`` for a job that has just turned running, or None when it cannot be
    read; never raises (the runner calls it before the job's session starts)."""
    try:
        if job._conn is None or job.endpoint_id is None:
            return None
        return sessions_at_start(job._conn, job.endpoint_id, job.started_at or time.time())
    except Exception as e:  # noqa: BLE001 -- the count is evidence; it never fails the job
        job._log_safe(f"[feedback-error] sessions at start: {type(e).__name__}: {e}")
        return None


def _brief_text(job) -> str:
    """The brief the job was given: a pointer brief's file text, else the task itself."""
    try:
        return resolve_brief(job.task or "", job.cwd or os.getcwd())
    except BriefUnresolved:
        return job.task or ""


def brief_files(text: str | None) -> list[str] | None:
    """The file-like tokens a brief names (a path with a slash, or a name with an extension,
    globs kept), or None when it names none: the file-set gate then cannot be evaluated."""
    tokens = []
    for raw in _TOKEN_SPLIT.split(text or ""):
        tok = raw.rstrip(".!?")
        if not tok or "://" in tok:
            continue
        if "/" in tok or _HAS_EXTENSION.match(tok):
            tokens.append(tok)
    return tokens or None


def _parts(path: str) -> list[str]:
    return [p for p in path.split("/") if p not in ("", ".")]


def _within(name: str, tokens: list[str]) -> bool:
    """Is a changed file one the brief names? The same file (either path a suffix of the
    other: a bare name, a relative path, an absolute one), a file under a named directory
    (a token ending in ``/`` or with no extension), or a glob match."""
    np = _parts(name)
    for tok in tokens:
        if any(c in tok for c in "*?"):
            if fnmatch.fnmatch(name, tok.lstrip("./")) or fnmatch.fnmatch(name, "*/" + tok.lstrip("/")):
                return True
            continue
        tp = _parts(tok)
        if not tp:
            continue
        k = min(len(tp), len(np))
        if tp[-k:] == np[-k:]:
            return True
        is_dir = tok.endswith("/") or "." not in tp[-1]
        if is_dir:
            # a suffix of the token's parts is a prefix of the file's: an absolute directory
            # token may match from any depth, a relative one only whole
            depths = range(1, len(tp) + 1) if tok.startswith(("/", "~")) else (len(tp),)
            if any(d < len(np) and tp[-d:] == np[:d] for d in depths):
                return True
    return False


def _tests_gate(conn, job_id: str, text: str) -> bool | None:
    """Tests ran and passed where the brief named a runner: the last test-runner Bash call
    that finished decides (red then green is a pass). None when the brief names no runner, or
    when the runs it made never returned a result (could not see, so never a verdict), or when
    no row shows a runner but some row's stored command may have been cut
    (derive.command_may_be_cut): a cut row may have lost the runner token past the cut, so it
    is not evidence of no test run — the gate cannot be evaluated (design §9: null, not
    false). A row that cannot have been cut and shows no runner is the full record: False."""
    if not _TEST_RUNNER.search(text or ""):
        return None
    rows = [r for r in conn.execute(
        "SELECT input, is_error FROM tool_calls WHERE job_id = ? AND tool = 'Bash'"
        " ORDER BY t, rowid", (job_id,)).fetchall() if r["input"]]
    runs = [r for r in rows if _TEST_RUNNER.search(r["input"])]
    if not runs:
        # a row that may have been cut may have lost the runner to the cut
        return None if any(derive.command_may_be_cut(r["input"]) for r in rows) else False
    finished = [r for r in runs if r["is_error"] is not None]
    if not finished:
        return None
    return finished[-1]["is_error"] == 0


def _capped(job) -> bool:
    return job.status == "timeout" or "max_turns" in (job.error or "")


# --- the diff's reads ------------------------------------------------------------------------

def _diff_files(diff_text: str) -> list[tuple[str, list[tuple[str, int, str]]]]:
    """Each file a unified diff changes, with its hunk lines in order: (kind, new line number,
    text), kind ``+`` (added) or `` `` (context). Removed lines and deleted files carry no
    writing of the job's and are left out."""
    files: list[tuple[str, list[tuple[str, int, str]]]] = []
    cur: list[tuple[str, int, str]] | None = None
    in_hunk = False
    line_no = 0
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            cur, in_hunk = None, False
            continue
        if not in_hunk:
            if raw.startswith("+++ "):
                path = raw[4:].strip()
                if path == "/dev/null":
                    cur = None
                else:
                    cur = []
                    files.append((path[2:] if path.startswith("b/") else path, cur))
                continue
            m = _HUNK.match(raw)
            if m:
                in_hunk, line_no = True, int(m.group(1))
            continue
        m = _HUNK.match(raw)
        if m:
            line_no = int(m.group(1))
            continue
        kind = raw[:1]
        if kind == "-" or kind == "\\":
            continue
        if cur is not None and kind in ("+", " ", ""):
            cur.append(("+" if kind == "+" else " ", line_no, raw[1:]))
        line_no += 1
    return files


def _tab_exempt(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in _TAB_FORMATS or name.endswith(_TAB_SUFFIXES)


def _lint_file(path: str, lines: list[tuple[str, int, str]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def hit(rule: str, line: int) -> None:
        found.append({"rule": rule, "file": path, "line": line})

    # literal tabs on an added line
    if not _tab_exempt(path):
        for kind, no, text in lines:
            if kind == "+" and "\t" in text:
                hit("literal_tab", no)

    # constants after use: an added UPPER_CASE definition with an earlier reference in the file
    for i, (kind, no, text) in enumerate(lines):
        m = _CONST_DEF.match(text)
        if kind != "+" or not m or m.group(2).rstrip().endswith(","):
            continue  # a trailing comma is a keyword argument in a multi-line call
        name = m.group(1)
        ref = re.compile(rf"(?<![\w.]){re.escape(name)}\b")
        earlier = [t for _, _, t in lines[:i] if not _COMMENT.match(t)]
        if any((d := _CONST_DEF.match(t)) and d.group(1) == name for t in earlier):
            continue  # a reassignment: the first definition is the one that counts
        if any(ref.search(t) for t in earlier):
            hit("constant_after_use", no)

    # try{}catch(e){throw e}: the C-family shape over the added text
    added = [(no, text) for kind, no, text in lines if kind == "+" and not _COMMENT.match(text)]
    joined = "\n".join(text for _, text in added)
    starts, offset = [], 0
    for _, text in added:
        starts.append(offset)
        offset += len(text) + 1
    for m in _C_RETHROW.finditer(joined):
        idx = max(i for i, s in enumerate(starts) if s <= m.start())
        hit("rethrow_wrapper", added[idx][0])

    # and Python's: an added except whose whole body re-raises what it caught
    body = [(kind, no, text) for kind, no, text in lines if text.strip() and not _COMMENT.match(text)]
    for i, (kind, no, text) in enumerate(body):
        m = _PY_EXCEPT.match(text)
        if kind != "+" or not m:
            continue
        indent, caught = m.group(1), m.group(2)
        if m.group("inline") is None:
            if i + 1 >= len(body):
                continue
            r = _PY_RAISE.match(body[i + 1][2])
            if not r:
                continue
            raised, after = r.group(1), i + 2
        else:
            raised, after = m.group("raised"), i + 1   # the one-line form: except X: raise
        if raised is not None and raised != caught:
            continue  # raising something else is a translation, not a wrapper
        if after < len(body):
            nxt = re.match(r"^(\s*)except\b", body[after][2])
            if nxt and nxt.group(1) == indent:
                continue  # keeps a subclass out of the broader arm below: it adds something
        hit("rethrow_wrapper", no)

    order = {rule: i for i, rule in enumerate(LINT_RULES)}
    return sorted(found, key=lambda f: (f["line"], order[f["rule"]]))


def lint(diff_text: str) -> list[dict[str, Any]]:
    """The hints' three lint rules over a unified diff's added lines, by pattern across
    languages: constants after use, ``try{}catch(e){throw e}``-shaped wrappers, literal tabs.
    Returns one ``{"rule", "file", "line"}`` per finding, files in diff order, at most
    ``LINT_DETAIL_MAX``."""
    found: list[dict[str, Any]] = []
    for path, lines in _diff_files(diff_text or ""):
        found.extend(_lint_file(path, lines))
    return found[:LINT_DETAIL_MAX]


def git(cwd: str, *args: str, env: dict[str, str] | None = None,
        bounded: bool = True) -> subprocess.CompletedProcess:
    """Every git call a job's reads and its worktree make and finish run (runner._git is this).
    Output is decoded as UTF-8 with ``errors="replace"`` (``-z`` prints names unquoted: a
    non-UTF-8 name never raises), stdin is closed, and the call is bounded by GIT_TIMEOUT_S, read
    at call time. Past the bound git and everything it started are killed (its own process
    group: a hook or filter child holds the output pipes, so killing git alone leaves the read
    blocked) and subprocess.TimeoutExpired is raised for the caller to name. ``bounded=False`` is
    for a step whose time grows with the repository (a checkout)."""
    with subprocess.Popen(["git", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                          errors="replace", start_new_session=True) as p:
        try:
            out, err = p.communicate(timeout=GIT_TIMEOUT_S if bounded else None)
        except BaseException as e:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if isinstance(e, subprocess.TimeoutExpired):
                try:  # reap; a child that left the group keeps a pipe open: stop waiting on it
                    p.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            raise
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)


class DiffUnread(Exception):
    """A git step of ``tree_diff`` exited non-zero; the message names the step and git's error."""


def tree_diff(root: str, base: str, scratch_dir: str, *reads: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """The one reader of a worktree's changes against ``base``, committed and not, untracked
    files included (files git ignores are not: ``add -A`` leaves them out). Returns the changed
    paths (``--no-renames``: a rename is both paths; the record's diff_files and the gates'
    files_n) and the output of ``git diff --cached <read> <base>`` for each further read. The
    tree's own index is never touched: ``git add -A`` runs against a copy of it in
    ``scratch_dir``, so what the agent staged stays staged and a killed git leaves no lock in
    the tree. Raises DiffUnread, subprocess.TimeoutExpired (a step hung past GIT_TIMEOUT_S) or
    OSError; the caller names it."""
    index = Path(scratch_dir) / f"feedback-{db.new_uuid()}.index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))

    def step(*args: str, env: dict[str, str] | None = None) -> str:
        r = git(root, *args, env=env)
        if r.returncode != 0:
            raise DiffUnread(f"git {' '.join(args)} failed ({' '.join(r.stderr.split()) or f'exit {r.returncode}'})")
        return r.stdout

    try:
        src = Path(root) / step("rev-parse", "--git-path", "index").strip()
        if src.is_file():
            shutil.copyfile(src, index)   # the copy keeps the stat cache: add -A rehashes less
        else:
            step("read-tree", "HEAD", env=env)
        step("add", "-A", env=env)
        names = step("diff", "--cached", "--no-renames", "--name-only", "-z", base, env=env)
        return [n for n in names.split("\0") if n], [step("diff", "--cached", *r, base, env=env) for r in reads]
    finally:
        for p in (index, Path(f"{index}.lock")):
            p.unlink(missing_ok=True)


def read_diff(worktree: dict[str, Any], scratch_dir: str) -> tuple[list[str], str] | None:
    """``tree_diff``'s names and unified diff for the gates, or None when they cannot be read."""
    root, base = worktree.get("root"), worktree.get("base")
    if not root or not base or not Path(root).is_dir():
        return None
    try:
        names, (text,) = tree_diff(root, base, scratch_dir, ("--no-renames", "--no-color", "--no-ext-diff"))
    except (DiffUnread, OSError, subprocess.SubprocessError):
        return None
    return names, text


# --- the gates (§9) --------------------------------------------------------------------------

def gates(job, conn, diff_names: list[str] | None, diff_text: str | None,
          brief_files: list[str] | None, *, brief_text: str | None = None) -> dict[str, Any]:
    """The gate results for one finished job. ``tests`` and ``not_capped`` read the record;
    the rest read the diff and are None when ``diff_names``/``diff_text`` is None (a
    non-worktree job, or a diff that could not be read). ``stop_rule`` is None unless the
    report claims a stop, then true only for an empty diff; ``files_within`` is None when
    the brief names no file."""
    text = brief_text if brief_text is not None else _brief_text(job)
    out: dict[str, Any] = {
        "tests": _tests_gate(conn, job.id, text),
        "stop_rule": None,
        "not_capped": not _capped(job),
        "no_tmp": None,
        "files_within": None,
        "lint": None,
        "lint_detail": None,
        "files_n": None,
    }
    if diff_names is None or diff_text is None:
        return out
    names = list(diff_names)
    out["files_n"] = len(names)
    if _STOP_CLAIM.search(job.result or ""):
        out["stop_rule"] = not names
    files = _diff_files(diff_text)
    out["no_tmp"] = not any(kind == "+" and _TMP.search(text)
                            for _, lines in files for kind, _, text in lines)
    if brief_files is not None:
        out["files_within"] = all(_within(n, brief_files) for n in names)
    findings = lint(diff_text)
    out["lint"] = not findings
    out["lint_detail"] = findings
    return out


def _is_evidence(job) -> bool:
    """A job is evidence about its row once its session began and the operator did not end it:
    a cancel is the operator's act, and a job that never reached a session is the endpoint's."""
    return job.session_id is not None and job.status != "cancelled"


async def _write_gates(job, conn) -> None:
    names = text = None
    if job.isolation == "worktree" and job.worktree:
        got = await asyncio.to_thread(read_diff, job.worktree, job.log_dir)
        if got is None:
            job._log_safe("[feedback-error] the worktree diff could not be read; its gates are null")
        else:
            names, text = got
    brief = _brief_text(job)
    result = gates(job, conn, names, text, brief_files(brief), brief_text=brief)
    job.gates = result
    with db.immediate(conn):
        # this column only: run_job's last changed() is what turns the row terminal
        conn.execute("UPDATE jobs SET gates = ? WHERE id = ?", (json.dumps(result), job.id))
    fired = sorted({f["rule"] for f in result["lint_detail"] or ()})
    shown = {k: result[k] for k in (*GATES, "files_n")}
    job._log_safe(f"[gates] {json.dumps(shown)}" + (f" lint: {', '.join(fired)}" if fired else ""))


# --- the speed sample and the curve's fold (§7) ------------------------------------------------

# A sample's concurrency is the occupancy its run HELD: the one that covered this share of the
# run's wall time. generate_tps is the server's counters over the whole run, so a run that
# started alone and shared for most of its life measured the shared speed, whatever the count
# at its start said; a run whose occupancy changed for longer measures no one concurrency.
STEADY_SHARE = 0.9

# our other sessions on the endpoint during a run: the jobs_finished_at seeks (finished since
# the run began, or not finished) that read.py's jobs_overlapping takes, so the read is bounded
# by the run's own span; tests/test_query_plans.py pins the plan
_RUN_NEIGHBOURS_SQL = (
    "SELECT started_at, finished_at FROM jobs WHERE started_at IS NOT NULL AND started_at < ?"
    " AND (finished_at IS NULL OR finished_at > ?) AND +endpoint_id = ? AND id <> ?")


def run_occupancy(conn, job_id: str, endpoint_id: int, start: float, end: float) -> int | None:
    """Our sessions on the endpoint across one job's run [start, end], the job included: the
    occupancy that held for at least ``STEADY_SHARE`` of it, or None when none did (the run
    measures no single concurrency) or the run has no length. Sessions that are not ours
    (another client on the box) are not in the record and are not counted."""
    span = end - start
    if span <= 0:
        return None
    edges: list[tuple[float, int]] = []
    for started, finished in conn.execute(_RUN_NEIGHBOURS_SQL, (end, start, endpoint_id, job_id)):
        a, b = max(start, started), end if finished is None else min(end, finished)
        if b > a:
            edges += [(a, 1), (b, -1)]
    held: dict[int, float] = {}
    level, t = 1, start
    for at, step in sorted(edges):  # an end before a start at the same instant
        held[level] = held.get(level, 0.0) + (at - t)
        level, t = level + step, at
    held[level] = held.get(level, 0.0) + (end - t)
    k, longest = max(held.items(), key=lambda kv: kv[1])
    return k if longest >= STEADY_SHARE * span else None


def _write_speed_sample(job, conn) -> bool:
    """The job's ``speed_samples`` row when its speed was measured and its run held one
    occupancy (``run_occupancy``); True when a row was written."""
    tps = (job.metrics or {}).get("generate_tps")
    if job.endpoint_id is None or job.started_at is None or isinstance(tps, bool):
        return False
    if not isinstance(tps, (int, float)) or tps <= 0:
        return False  # no measured speed: a sample is never guessed
    end = job.finished_at or time.time()
    sessions = run_occupancy(conn, job.id, job.endpoint_id, job.started_at, end)
    if sessions is None:
        job._log_safe(f"[speed-sample] none: our sessions on the endpoint changed for more than"
                      f" {1 - STEADY_SHARE:.0%} of the run, so its speed measures no one concurrency")
        return False
    with db.immediate(conn):
        conn.execute("INSERT OR REPLACE INTO speed_samples (job_id, endpoint_id, sessions,"
                     " generate_tps, at) VALUES (?, ?, ?, ?, ?)",
                     (job.id, job.endpoint_id, sessions, float(tps), end))
    return True


def _fold_curve(job, conn, res) -> None:
    """The endpoint's curve re-derived with this job's sample in (``capacity.fold_samples``).
    A change is a ``[curve]`` line in this job's transcript (``job_log``), naming the hold when
    the rule's first holding concurrency moved; the row's JSON names this job (``by``), so an
    operator goes from a hold to the line, and from the line to the samples."""
    if job.model_id is None or res is None:
        return
    before, after = capacity.fold_samples(conn, job.endpoint_id, job.model, job.model_id,
                                          backend_kind(res.endpoint.backend), by=job.id)
    if after is None or after is before:
        return
    points = ", ".join(f"{k}: {v:.3f}" for k, v in after.points.items())
    line = (f"[curve] {res.endpoint.name} ({job.model}): {after.state}, n={after.n},"
            f" points {{{points}}}, measured {sorted(after.measured)}")
    was, now = (before.first_hold() if before is not None else None), after.first_hold()
    if now != was:
        line += (f"; the bottleneck rule now holds from {now} sessions (normal and urgent work"
                 " waits there, background enters)" if now is not None
                 else "; the bottleneck rule now holds nothing")
    job._log_safe(line)


async def _on_finish(job, res, sessions: int | None) -> None:
    conn = job._conn
    if conn is None:
        return  # a Job built outside JobStore.new has no row to write to
    if _is_evidence(job):
        try:
            await _write_gates(job, conn)
        except Exception as e:  # noqa: BLE001 -- one feed failing must not drop the other
            job._log_safe(f"[feedback-error] gates: {type(e).__name__}: {e}")
    try:
        sampled = _write_speed_sample(job, conn)
    except Exception as e:  # noqa: BLE001
        sampled = False
        job._log_safe(f"[feedback-error] speed sample: {type(e).__name__}: {e}")
    if sampled:
        try:
            _fold_curve(job, conn, res)
        except Exception as e:  # noqa: BLE001 -- the curve is evidence; it never fails the job
            job._log_safe(f"[feedback-error] curve: {type(e).__name__}: {e}")


async def on_finish(job, res, sessions: int | None = None) -> None:
    """The finish hook (runner._finish): the gates on ``jobs.gates``, the speed sample, and
    the curve's fold over the endpoint's recent samples. ``sessions`` (``note_start``'s count
    at the job's start) is no longer read: a sample's concurrency is the occupancy its run held
    (``run_occupancy``). Evidence only: it never raises and never changes the job's status or
    error."""
    try:
        await _on_finish(job, res, sessions)
    except Exception as e:  # noqa: BLE001 -- evidence never fails the job (design §9)
        job._log_safe(f"[feedback-error] {type(e).__name__}: {e}")


# --- the verdict (§9, §11) -------------------------------------------------------------------

def _bad(message: str) -> dict[str, Any]:
    return {"error": "bad_argument", "message": message}


def _check_dimensions(dimensions) -> str | None:
    if dimensions is None:
        return None
    if not isinstance(dimensions, dict):
        return "dimensions must be an object of {axis: -1 | 0 | 1}"
    for axis, value in dimensions.items():
        if axis not in scorecard.CAPABILITY_CELLS:
            return f"unknown axis {axis!r} (one of {', '.join(scorecard.CAPABILITY_CELLS)})"
        if isinstance(value, bool) or value not in (-1, 0, 1):
            return f"dimension {axis!r} must be -1, 0 or 1, not {value!r}"
    return None


def _check_correction(correction, document: dict) -> str | None:
    """A correction is the brief's true answer on any dimension of the ticket's document: a
    band 0-4 for a score, one of the options for a choice."""
    if correction is None:
        return None
    if not isinstance(correction, dict) or not correction:
        return "correction must be a non-empty object of {dimension: value}"
    for dim, value in correction.items():
        entry = document.get(dim)
        if not isinstance(entry, dict):
            return f"unknown dimension {dim!r} (one of {', '.join(document)})"
        if entry.get("type") == "score":
            if isinstance(value, bool) or not isinstance(value, int) or str(value) not in entry["criteria"]:
                return f"correction {dim!r} must be a band 0-4, not {value!r}"
        elif entry.get("type") == "choice":
            if not isinstance(value, str) or value not in entry["criteria"]:
                return f"correction {dim!r} must be one of {', '.join(entry['criteria'])}, not {value!r}"
        else:
            return f"dimension {dim!r} has a type a correction cannot carry"
    return None


def _router_entry(reg):
    for ep in (reg.endpoints.values() if reg is not None else ()):
        if ep.is_router and not ep.invalid:
            return ep
    return None


def _live_row_id(conn, endpoint_name, served_id) -> int | None:
    r = conn.execute("SELECT id FROM scorecard_rows WHERE endpoint_name = ? AND served_id = ?"
                     " AND archived IS NULL", (endpoint_name, served_id)).fetchone()
    return r["id"] if r is not None else None


async def apply_verdict(conn, reg, job_id: str, accepted: bool, dimensions: dict | None = None,
                        correction: dict | None = None, *, client: httpx.AsyncClient | None = None,
                        set_version: str | None = None) -> dict[str, Any]:
    """The coordinator's verdict on a finished job (§9): one per job, a second replaces the
    first. ``dimensions`` is the per-axis accept (1) / reject (-1) / as-the-verdict (0) the
    caller asserts over the job-level verdict; an axis the ticket left undetermined (or never
    scored) adds no observation and is named in ``no_observation``. A job with no ticket (a
    directly named model) has its brief scored by d3 now and a ticket written with
    ``flags.scored_at_verdict``, the row being the one that ran it. ``correction`` logs a
    ``category_corrections`` row at the ticket's definitions version (the next verdict's
    correction replaces it; a verdict without one leaves it). Every refusal is a dict and
    writes nothing. A job that never had a session (``started_at`` null: dropped, lost or
    cancelled while queued) takes no verdict, ``no_session`` (fix wave 3, D3): nothing ran, so
    there is no work to judge and no evidence about its row. Async: the d3 scoring is an HTTP
    call."""
    job = conn.execute(
        "SELECT j.id, j.task, j.cwd, j.status, j.started_at, e.name AS endpoint_name, m.served_id"
        " FROM jobs j LEFT JOIN endpoints e ON e.id = j.endpoint_id"
        " LEFT JOIN models m ON m.id = j.model_id WHERE j.id = ?", (job_id,)).fetchone()
    if job is None:
        return {"error": "unknown_job", "message": f"no job {job_id!r}"}
    if job["status"] not in TERMINAL_STATUSES:
        return {"error": "job_not_finished",
                "message": f"job {job_id} is {job['status']}; a verdict is on a finished job"}
    if job["started_at"] is None:
        return {"error": "no_session",
                "message": f"job {job_id} is {job['status']} and never had a session; a verdict"
                           " judges work a session did, and there was none"}
    if not isinstance(accepted, bool):
        return _bad("accepted must be true or false")
    problem = _check_dimensions(dimensions)
    if problem:
        return _bad(problem)

    ticket = conn.execute("SELECT defs_version, scores FROM router_tickets WHERE job_id = ?",
                          (job_id,)).fetchone()
    if ticket is not None:
        defs_version = ticket["defs_version"]
        stored = conn.execute("SELECT document FROM dimension_definitions WHERE version = ?",
                              (defs_version,)).fetchone()
        document = json.loads(stored["document"]) if stored is not None else {}
    else:
        defs = definitions.load(conn)
        defs_version, document = defs.version, defs.document
    problem = _check_correction(correction, document)
    if problem:
        return _bad(problem)

    brief: str | None = None
    new_scores: dict[str, Any] | None = None
    if ticket is None:
        ep = _router_entry(reg)
        if ep is None:
            return {"error": "no_router_entry",
                    "message": f"job {job_id} has no ticket, and scoring its brief at verdict"
                               " time needs a backend: router entry in models.yaml"}
        try:
            brief = resolve_brief(job["task"] or "", job["cwd"] or os.getcwd())
        except BriefUnresolved as e:
            return e.to_dict()
        own = client is None
        http = client if client is not None else httpx.AsyncClient()
        try:
            got = await score(http, ep.decision_url, ep.decision_model, brief, defs,
                              timeout_s=ep.decision_timeout_s, max_tokens=ep.decision_max_tokens)
        except DecisionUnavailable as e:
            return e.to_dict()
        finally:
            if own:
                await http.aclose()
        defs_version = got.defs_version
        new_scores = {"values": got.values, "confidences": got.confidences,
                      "undetermined": sorted(got.undetermined), "truncated": got.truncated,
                      "defs_version": got.defs_version}

    row_id = _live_row_id(conn, job["endpoint_name"], job["served_id"])
    now = time.time()
    with db.immediate(conn):
        if new_scores is not None:
            # OR IGNORE: a verdict racing this one may have written the ticket first; the
            # first ticket stands and is the one read back below
            conn.execute(
                "INSERT OR IGNORE INTO router_tickets (job_id, defs_version, pass_id, scores, rows,"
                " chosen_row, divert, flags) VALUES (?, ?, NULL, ?, '[]', ?, 0, ?)",
                (job_id, defs_version, json.dumps(new_scores), row_id,
                 json.dumps({"scored_at_verdict": True})))
        current = conn.execute("SELECT defs_version, scores FROM router_tickets WHERE job_id = ?",
                               (job_id,)).fetchone()
        conn.execute(
            "INSERT INTO verdicts (job_id, accepted, dimensions, at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(job_id) DO UPDATE SET accepted = excluded.accepted,"
            " dimensions = excluded.dimensions, at = excluded.at",
            (job_id, 1 if accepted else 0,
             json.dumps(dimensions) if dimensions is not None else None, now))
        if correction is not None:
            if brief is None:
                try:
                    brief = resolve_brief(job["task"] or "", job["cwd"] or os.getcwd())
                except BriefUnresolved:
                    brief = job["task"] or ""  # the pointer is the brief the record still has
            conn.execute("DELETE FROM category_corrections WHERE job_id = ?", (job_id,))
            conn.execute(
                "INSERT INTO category_corrections (job_id, brief, scored, corrected, defs_version,"
                " at) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, brief, current["scores"], json.dumps(correction),
                 current["defs_version"], now))

    try:
        scores = json.loads(current["scores"])
    except (TypeError, ValueError):
        scores = {}
    if not isinstance(scores, dict):
        scores = {}
    values = scores.get("values") or {}
    undetermined = set(scores.get("undetermined") or ())
    no_observation = sorted(axis for axis in (dimensions or {})
                            if axis not in values or axis in undetermined)
    return {
        "recorded": True,
        "job_id": job_id,
        "accepted": accepted,
        "scored_at_verdict": new_scores is not None,
        "no_observation": no_observation,
        "notices": scorecard.notices(conn, row_id, set_version) if row_id is not None else [],
    }
