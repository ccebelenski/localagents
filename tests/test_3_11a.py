"""Story 3.11a: the pre-close codebase pass — the folds of the audit (the table is in the story's
record). One test (or a small group) per fold, each written to fail on the code before its fold.
The row numbers are the audit table's."""
import ast
import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from localagents import jobs
from localagents import shim as shim_mod

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "localagents"
STATIC = SRC / "monitor" / "static"


# --- row 1: dead code -------------------------------------------------------------------------


def _module_level_names(path: Path) -> list[tuple[str, int]]:
    out = []
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((node.name, node.lineno))
        elif isinstance(node, ast.Assign):
            out.extend((t.id, node.lineno) for t in node.targets if isinstance(t, ast.Name))
    return out


def test_every_module_level_name_in_src_is_used_somewhere():
    """Row 1: a module-level function, class or constant in src/localagents that nothing else
    names (in src/, tests/ or tools/) is dead code. ``runner._block_to_dict`` was the one such
    name at the audit; ``__all__`` is read by the import system, not by name."""
    here = Path(__file__).resolve()  # this file names the dead function in its own prose
    texts = [p.read_text() for p in (*SRC.rglob("*.py"), *SRC.rglob("*.js"),
                                     *(ROOT / "tests").rglob("*.py"), *(ROOT / "tools").rglob("*.*"))
             if p.resolve() != here]
    # the calibration set's fixture trees ship under src/ but are inert data: their planted
    # names (a checker's constant, a project class) are single-use by design — a fixture is
    # read by its checker and its tests, never imported — so the scan skips the fixture dirs.
    fixtures = SRC / "router" / "sets" / "fixtures"
    dead = []
    for p in sorted(SRC.rglob("*.py")):
        if fixtures in p.parents:
            continue
        for name, line in _module_level_names(p):
            if name.startswith("__") and name.endswith("__"):
                continue
            pat = re.compile(r"\b" + re.escape(name) + r"\b")
            if sum(len(pat.findall(t)) for t in texts) <= 1:  # the definition itself
                dead.append(f"{p.relative_to(ROOT)}:{line} {name}")
    assert dead == []


# --- row 40 (and 40b): unused parameters in the page scripts ------------------------------------


def _js_body(src: str, start: int) -> str:
    """The brace-balanced body of the function whose signature ends at ``start``."""
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
    raise ValueError("unbalanced function body")


def test_every_named_page_function_reads_each_of_its_parameters():
    """Row 40: endpoint.js ``header(name, snap, host, ep, hist, err)`` never read ``hist``; 40b:
    app.js ``neverSeenOn(host, ep)`` stopped reading ``host`` when 3.7y dropped its down-node
    clause. A parameter nothing reads makes every caller pass a value that decides nothing."""
    unused = []
    for f in ("app.js", "endpoint.js", "job.js"):
        src = (STATIC / f).read_text()
        for m in re.finditer(r"function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)", src):
            body = _js_body(src, m.end())
            for p in (p.strip() for p in m.group(2).split(",")):
                if p and not re.search(r"\b" + re.escape(p) + r"\b", body):
                    unused.append(f"{f} {m.group(1)}({p})")
    assert unused == []


# --- shim relay harness (tests/test_3_10h_fold.py's, trimmed) -----------------------------------

MESSAGE_START = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"m","usage":'
    b'{"input_tokens":67000,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,'
    b'"output_tokens":1}}}\n\n'
)
MESSAGE_DELTA_57 = (
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":57}}\n\n'
)
STREAM_BODY = {"model": "m", "messages": [{"role": "user", "content": "hello"}], "stream": True}
PLAIN_BODY = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for c in self._chunks:
            yield c


def _sse_send(chunks):
    async def send(upstream, stream=True):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_Chunks(chunks), request=upstream)
    return send


def _json_send(status, doc):
    async def send(upstream, stream=True):
        return httpx.Response(status, headers={"content-type": "application/json"},
                              content=json.dumps(doc).encode(), request=upstream)
    return send


def _relay(send, body, path="v1/messages"):
    """Drive shim.relay against a fake upstream and drain the reply; returns (reply, reply bytes,
    the drain's exception or None, the job). The conftest points the state at the test's tmp."""
    from starlette.requests import Request

    job = jobs.JobStore().new(
        task="t", model="m", endpoint="e", base_url="http://x:8080", backend="llama.cpp", host="local",
        cwd="/srv/3.11a", run_cwd="/srv/3.11a", isolation="none",
    )
    shim = shim_mod.Shim(lambda jid: job)
    shim._client.send = send
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {
        "type": "http", "method": "POST", "headers": [(b"content-type", b"application/json")],
        "path": f"/job/{job.id}/{path}", "query_string": b"", "root_path": "",
        "path_params": {"job_id": job.id, "path": path},
    }

    async def _drive():
        resp = await shim.relay(Request(scope, receive))
        out, err = b"", None
        if hasattr(resp, "body_iterator"):
            try:
                async for chunk in resp.body_iterator:
                    out += chunk
            except Exception as e:  # noqa: BLE001 — the drain's own failure is a result here
                err = e
        return resp, out, err

    resp, out, err = asyncio.run(_drive())
    return resp, out, err, job


def _request_rows(job):
    return job._conn.execute("SELECT * FROM requests WHERE job_id = ?", (job.id,)).fetchall()


# --- row 5: the stream head's cap -----------------------------------------------------------------


def test_the_stream_head_is_capped_at_head_bytes_whatever_the_read_size(monkeypatch):
    """Row 5: ``if len(head) < HEAD_BYTES: head += chunk`` let the head grow by one WHOLE read
    past the cap — a 200 KiB first read kept 200 KiB. The head handed to the message_start
    parser is at most HEAD_BYTES, and the usage at its front is still read."""
    seen = []
    real = shim_mod._usage_from_sse_head

    def spy(chunk):
        seen.append(len(chunk))
        return real(chunk)

    monkeypatch.setattr(shim_mod, "_usage_from_sse_head", spy)
    big = MESSAGE_START + b": " + b"x" * (200 * 1024) + b"\n\n"
    resp, out, err, job = _relay(_sse_send([big, MESSAGE_DELTA_57]), STREAM_BODY)
    assert err is None and out == big + MESSAGE_DELTA_57  # the client gets every byte
    assert seen and max(seen) <= shim_mod.HEAD_BYTES, seen
    (row,) = _request_rows(job)
    assert row["input_tokens"] == 67000 and row["output_tokens"] == 57


# --- row 34: seed --force replaces every table ------------------------------------------------------


def test_seed_force_leaves_no_row_of_the_previous_world_in_any_table(tmp_path):
    """Row 34: ``seed(force=True)`` promises "deletes every row of every table and seeds fresh",
    but its list skipped ``host_probes`` (schema v5): a node-fallback answer from the previous
    world (or a real collector's) survived the reseed, so the seeded node state was not a pure
    function of ``now``. After a forced reseed, no table holds a row the seed did not write —
    checked over EVERY table the schema has, so the next table added is covered too."""
    from localagents import db
    from localagents.monitor import seed

    from monitor_seeded import FX, NOW

    conn = db.open_db(tmp_path / "w.db")
    try:
        seed.seed(conn, NOW, FX)
        fresh = tmp_path / "fresh.db"
        ref = db.open_db(fresh)
        seed.seed(ref, NOW, FX)
        tables = [r[0] for r in ref.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        expected = {t: ref.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        ref.close()
        # leftovers of an earlier world: a node-fallback answer and a finished job's transcript
        conn.execute("INSERT INTO host_probes (t, host, address, answer) VALUES (?, 'ai1', '10.0.0.50', 'nothing')",
                     (NOW - 30,))
        jid = conn.execute("SELECT id FROM jobs WHERE status = 'succeeded'").fetchone()[0]
        conn.execute("INSERT INTO transcripts (job_id, text, bytes, written) VALUES (?, 'x', 1, ?)", (jid, NOW))
        seed.seed(conn, NOW, FX, force=True)
        got = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        assert got == expected
    finally:
        conn.close()


# --- row 2: one _log_safe -------------------------------------------------------------------------


def _store_job(**kw):
    return jobs.JobStore().new(
        task="t", model="m", endpoint="e", base_url="http://x:8080", backend="llama.cpp", host="local",
        cwd="/srv/3.11a", run_cwd="/srv/3.11a", isolation="none", **kw)


def test_every_never_raise_line_goes_through_the_one_log_safe(monkeypatch):
    """Row 2: three copies of "write the transcript line, else stderr" — runner._log_safe,
    Job._log_safe (whose docstring said it copied runner's) and an inline try/except in
    run_agent's shim_port step. One remains, ``Job._log_safe``: the runner's finally steps and
    the server's shim_port line both reach it (a recorder in its place hears both)."""
    import sqlite3

    from localagents import runner, server
    from localagents.registry import Defaults, Endpoint, Registry

    heard = []
    monkeypatch.setattr(jobs.Job, "_log_safe", lambda self, text: heard.append(text))

    # the runner: a finish step that raises is one [<what>-error] line
    job = _store_job()

    async def boom():
        raise RuntimeError("kaput")

    asyncio.run(runner._to_the_end(job, boom(), "finish"))
    assert heard == ["[finish-error] RuntimeError: kaput"]

    # the server: a failed shim_port write is one [db-error] line
    ep = Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")
    reg = Registry(path=None, endpoints={"e": ep}, defaults=Defaults(), raw={}, local={})

    async def probe(self, e):
        return {"up": True, "models": ["m"]}

    async def no_run(*a, **kw):
        return None

    async def no_start():
        return 0

    def locked(**cols):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Registry, "probe", probe)
    monkeypatch.setattr(server, "_reg", lambda: reg)
    monkeypatch.setattr(server, "run_job", no_run)
    monkeypatch.setattr(server.shim, "start", no_start)
    monkeypatch.setattr(server.shim, "port", 4242)
    monkeypatch.setattr(server.store, "describe_server", locked)
    heard.clear()
    asyncio.run(server.run_agent(task="t"))
    assert heard == ["[db-error] shim_port: OperationalError: database is locked"]


# --- row 3: the row writers' error line never raises ------------------------------------------------


def test_a_locked_database_on_a_full_disk_never_raises_out_of_a_row_writer(monkeypatch, capsys):
    """Row 3 (DeepSeek F3): ``_row_update``, ``record_tool_call``, ``record_tool_result`` and
    ``record_request`` answered a ``sqlite3.Error`` with ``self.log_text``, which raises OSError
    on a full disk — the second failure then escaped into the message loop and failed the job,
    against "the database observes the job and must never fail it". Each now says it through
    ``_log_safe``: no raise, and the line reaches stderr."""
    import sqlite3
    from contextlib import contextmanager

    job = _store_job()
    job.record_tool_call(1.0, "Bash", "tu-1", "ls")  # a pending call, so the result has a row to set

    @contextmanager
    def locked(conn):
        raise sqlite3.OperationalError("database is locked")
        yield  # pragma: no cover

    def no_disk(self, text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(jobs.db, "immediate", locked)
    monkeypatch.setattr(jobs.Job, "log_text", no_disk)
    job.changed()
    job.record_tool_call(2.0, "Read", "tu-2", "/x")
    job.record_tool_result("tu-1", 3.0, False)
    job.record_request({"t": 4.0, "path": "/v1/messages", "status": 200})
    err = capsys.readouterr().err
    for what in ("job row update", "tool call insert", "tool result update", "record_request"):
        assert f"[db-error] {what}: database is locked" in err, what


# --- row 4: a backend's usage that is not an object ------------------------------------------------


def test_a_json_reply_whose_usage_is_not_an_object_still_reaches_the_client():
    """Row 4: ``{"usage": "x"}`` on a 200 JSON reply made ``record_request`` raise AttributeError
    out of ``relay`` — Claude Code got a 500 for a reply the backend answered. The reply goes
    through untouched and the row is written with no token counts."""
    doc = {"type": "message", "content": [{"type": "text", "text": "hi"}], "usage": "weird"}
    resp, out, err, job = _relay(_json_send(200, doc), PLAIN_BODY)
    assert resp.status_code == 200 and json.loads(resp.body) == doc
    (row,) = _request_rows(job)
    assert row["http_status"] == 200 and row["input_tokens"] is None and row["output_tokens"] is None


def test_a_stream_whose_usage_is_not_an_object_keeps_its_row():
    """Row 4: a ``message_start`` whose usage is a JSON array made ``_merge_usage`` raise
    TypeError in the stream's finally — after the bytes were sent — so the drain failed and the
    row was lost. The row is written; the final count still comes from the message_delta."""
    start = (b'event: message_start\n'
             b'data: {"type":"message_start","message":{"id":"m","usage":[1,2]}}\n\n')
    resp, out, err, job = _relay(_sse_send([start, MESSAGE_DELTA_57]), STREAM_BODY)
    assert err is None and out == start + MESSAGE_DELTA_57
    (row,) = _request_rows(job)
    assert row["output_tokens"] == 57 and row["input_tokens"] is None


# --- row 20: the probe trusts the backend's JSON shapes ----------------------------------------------



@pytest.mark.parametrize("backend,odd", [
    ("llama.cpp", {"/slots": [1, 2]}),  # a /slots list whose entries are not objects
    ("llama.cpp", {"/props": []}),  # a /props body that is an array
    ("strata", {"/metrics": [1]}),  # strata's JSON /metrics as an array (non-empty: [] is falsy)
])
def test_an_odd_json_shape_on_a_side_route_never_reports_an_answering_endpoint_down(monkeypatch, backend, odd):
    """Row 20: ``_probe_context`` read ``x.get(...)`` / ``props.get(...)`` / ``doc.get(...)`` off
    whatever JSON came back, so a non-object raised AttributeError and ``probe()``'s catch-all
    reported ``up: False, host: unknown`` for an endpoint that had just answered ``/v1/models``.
    The odd body is ignored like an absent one: the probe stays up with its served ids."""
    from localagents import registry

    routes = {
        "/v1/models": {"data": [{"id": "m"}]},
        "/props": {"default_generation_settings": {"n_ctx": 4096}, "endpoint_slots": True},
        "/slots": [{"is_processing": True}, {"is_processing": False}],
        "/metrics": {"live": {"queued": 0}},
    }
    routes.update(odd)

    def handler(req):
        if req.url.path in routes:
            return httpx.Response(200, json=routes[req.url.path])
        return httpx.Response(404)

    real = httpx.AsyncClient

    class Mocked(real):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", Mocked)
    reg = registry.Registry(path=None, endpoints={}, defaults=registry.Defaults(), raw={}, local={})
    out = asyncio.run(reg.probe(registry.Endpoint(name="e", base_url="http://x", backend=backend)))
    assert out["up"] is True and out["models"] == ["m"], out


# --- row 7: finish_worktree's changed branch reads its return codes ----------------------------------


@pytest.mark.parametrize("fails", ["add", "diff"])
def test_a_failed_git_step_on_a_changed_tree_is_recorded(tmp_path, monkeypatch, fails):
    """Row 7 (DeepSeek 3.9B): the changed branch ran ``git add -A``, ``git diff --cached --stat``
    and ``git reset -q`` and read none of their return codes — a failed add or diff recorded an
    empty or partial diffstat under the "Changes left" note, and it was not said. Each failure is
    in the note and a [worktree-error] line (the no-change branch's shape); the tree is kept; a
    diffstat is recorded only when the read worked. (2026-11-15: the read is feedback.tree_diff
    on a copy of the index, so there is no reset on the tree's own index left to fail.)"""

    from localagents import runner
    from localagents.jobs import Job
    from localagents.router import feedback

    def git(cwd, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       cwd=cwd, capture_output=True, text=True, check=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "init")
    logs = tmp_path / "logs"
    logs.mkdir()
    j = Job(id="j311", task="t", model="m", endpoint="e", base_url="http://e", cwd=str(repo),
            run_cwd=str(repo), isolation="worktree", log_dir=str(logs))
    j.worktree = runner.make_worktree(j)
    (Path(j.worktree["root"]) / "b.txt").write_text("new\n")  # the job changed something

    real = feedback.git
    step = {"add": ("add", "-A"), "diff": ("diff", "--cached")}[fails]

    def failing(cwd, *args, **kw):
        if args[:2] == step:
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: simulated failure")
        return real(cwd, *args, **kw)

    monkeypatch.setattr(feedback, "git", failing)
    runner.finish_worktree(j)
    wt = j.worktree
    assert wt["kept"] is True and Path(wt["root"]).is_dir()
    assert "fatal: simulated failure" in wt["note"] and "Changes left" in wt["note"]
    log = (logs / "transcript.txt").read_text()
    assert "[worktree-error]" in log and "fatal: simulated failure" in log
    assert wt.get("diffstat") is None


# --- the page scripts under node (tests/fakedom.js: a DOM stand-in, no browser) -------------------


NODE = shutil.which("node") or "/home/linuxbrew/.linuxbrew/bin/node"
_PROLOGUE = """
const path = require("path"), fs = require("fs");
require(%(fakedom)s).install();
for (const f of ["app.js", "job.js", "endpoint.js"]) (0, eval)(fs.readFileSync(path.join(%(static)s, f), "utf8"));
const M = window.localagentsMonitor;
const out = (function () {
%(body)s
})();
process.stdout.write(JSON.stringify(out));
"""


def _page_js(tmp_path, body: str):
    """Run ``body`` (a function body returning a JSON-able value) with the three page scripts
    loaded over the DOM stand-in; returns what it returned."""
    script = tmp_path / "page_probe.js"
    script.write_text(_PROLOGUE % {"fakedom": json.dumps(str(ROOT / "tests" / "fakedom.js")),
                                   "static": json.dumps(str(STATIC)), "body": body})
    p = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


# --- row 36: one request-failure rule --------------------------------------------------------------


def test_the_request_failure_rule_is_one_exported_function_both_pages_use(tmp_path):
    """Row 36: the rule "a status outside 2xx, or a non-empty error — the /api/hello handshake
    excepted" was hand-copied in requestsTable's row mark and job.js's "request errors" figure.
    One function, ``requestFailed``, is exported and IS the row mark (the handshake smoke's
    eight shapes plus a 3xx and an errored 200 agree row for row); job.js counts through it and
    the 2xx comparison is written once across the page scripts."""
    got = _page_js(tmp_path, """
  var row = function (path, status, error, ctx) {
    return { t: 1, path: path, http_status: status, error: error, context_exceeded: ctx || null,
      ms: 800, job_id: null, tokens: { input: 100, output: 10, cache_read: 0, cache_creation: 0 } };
  };
  var rows = [row("/api/hello", 501, ""), row("/api/hello", null, ""), row("/api/hello", 200, ""),
    row("/v1/messages", 500, ""), row("/v1/messages", 0, "backend unreachable"),
    row("/v1/messages", 200, ""), row("/v1/messages", 200, "", "x"), row("/api/hello", 500, "boom"),
    row("/v1/messages", 302, null), row("/v1/messages", 200, "stream ended early")];
  if (typeof M.requestFailed !== "function") return { exported: false };
  var table = M.requestsTable(rows, { job: false, now: 100 });
  var marks = table.querySelectorAll("tbody tr").map(function (tr) { return tr.classList.contains("req-err"); });
  return { exported: true, marks: marks, rule: rows.map(function (r) { return M.requestFailed(r); }) };
""")
    assert got["exported"] is True
    assert got["rule"] == [False, False, False, True, True, False, False, False, True, True]
    assert got["marks"] == got["rule"]
    job_js = (STATIC / "job.js").read_text()
    assert "M.requestFailed" in job_js
    srcs = "".join((STATIC / f).read_text() for f in ("app.js", "job.js", "endpoint.js"))
    assert len(re.findall(r"http_status\s*<\s*200\s*\|\|", srcs)) == 1


# --- row 38: a filter keystroke builds no filter text it built before -------------------------------


def test_a_filter_keystroke_builds_each_rows_filter_text_once_per_table(tmp_path):
    """Row 38 (P1-a: 131 ms per keystroke at 1,000 rows, then 175 ms once titles joined the
    haystack): ``cellText`` built the column's td for EVERY row on EVERY keystroke. The text is
    a function of the row and the table's own opts, so it is built once per row and column for
    the table's life: the second keystroke into a 200-row table builds only the drawn page's
    cells (50), not 200 more; the matches are the same rows either way — text and titles."""
    got = _page_js(tmp_path, """
  var built = 0;
  var rows = [];
  for (var i = 0; i < 200; i++) rows.push({ id: i, word: i % 3 ? "alpha" + i : "beta" + i });
  var cols = [{ name: "word", numeric: false, title: null,
    cell: function (r) { built++; return M.h("td", { title: "full-" + r.word }, [M.h("span", { text: r.word })]); },
    sortKey: function (r) { return r.word; } }];
  var el = M.dataTable("t311-cache", cols, rows, {});
  var type = function (v) {
    var input = el.querySelector('input[data-filter="word"]');
    input.value = v;
    var before = built;
    input.dispatchEvent(new Event("input"));
    return { built: built - before, pager: el.querySelector(".table-pager").textContent };
  };
  var first = type("b"), second = type("be"), titled = type("full-beta1"), cleared = type("");
  return { first: first, second: second, titled: titled, cleared: cleared };
""")
    # the first keystroke fills the cache (200 rows) and draws one page (50)
    assert got["first"]["built"] <= 200 + 50
    assert got["second"]["built"] <= 50, got  # cache hit: only the drawn page's cells
    assert got["titled"]["built"] <= 50, got
    # 3.12a §7: a set filter also carries the "clear filters" link at the end of the line
    assert got["second"]["pager"] == got["first"]["pager"] == "1–50 of 67 · previous · next · clear filters"
    # a title-only needle still matches (81b199a's rule): the beta rows whose number starts with 1
    n_titled = sum(1 for i in range(200) if i % 3 == 0 and str(i).startswith("1"))
    assert got["titled"]["pager"] == f"1–{n_titled} of {n_titled} · previous · next · clear filters", got
    assert got["cleared"]["pager"] == "1–50 of 200 · previous · next"


# --- row 39: a drawn page builds each row's cells once ------------------------------------------------


def test_a_drawn_page_builds_each_rows_cells_once_and_never_shares_one(tmp_path):
    """Row 39: requestsTable's cell(), status sortKey and rowAttrs each called ``reqRow(req)``,
    which builds all twelve cells to return one — a 50-row page built 50 x (12 + 1) x 12 tds;
    jobsTable did the same through ``jobRow(job)[c]``. A drawn page now builds each row's tds
    once (at most rows x columns), and a row object listed twice still gets every cell (no td
    is handed to two rows)."""
    got = _page_js(tmp_path, """
  var made = 0, real = document.createElement;
  document.createElement = function (t) { if (String(t).toLowerCase() === "td") made++; return real(t); };
  var reqs = [];
  for (var i = 0; i < 120; i++) reqs.push({ path: "/v1/messages", t: 1000 - i, http_status: i % 9 ? 200 : 500,
    ms: 400 + i, gen_ms: 200, tokens: { input: i, output: 30, cache_read: 5, cache_creation: 1 },
    error: i % 9 ? null : "boom", job_id: "00000000-1111-2222-3333-44444444444" + (i % 10) });
  var jobs = [];
  for (var k = 0; k < 120; k++) jobs.push({ id: "000000" + (k < 10 ? "0" : "") + k + "-1111-2222-3333-444444444444",
    status: "succeeded", server_live: true, started_at: 900 - k, model: "m", endpoint: "ai1", elapsed_s: k,
    num_turns: k, tool_calls: 1, errors: 0, compactions: 0, ctx_pct: 10, ctx_tokens: 1, context: 10,
    prefill_tps: 1.5, gen_tps: 2.5, last_activity: "a" });
  var nextDraw = function (el) {
    var next = el.querySelectorAll(".pager-btn").filter(function (b) { return b.textContent === "next"; })[0];
    var before = made;
    next.click();
    return { tds: made - before, rows: el.querySelectorAll("tbody tr").length,
      cols: el.querySelectorAll("thead tr")[0].children.length };
  };
  var r = nextDraw(M.requestsTable(reqs, { now: 1000 }));
  var j = nextDraw(M.jobsTable(jobs, { id: "t311-draw", now: 1000 }));
  var twice = M.requestsTable([reqs[0], reqs[0]], { now: 1000 });
  var widths = twice.querySelectorAll("tbody tr").map(function (tr) { return tr.children.length; });
  return { requests: r, jobs: j, twice: widths };
""")
    for name in ("requests", "jobs"):
        t = got[name]
        assert t["rows"] == 50 and t["tds"] <= t["rows"] * t["cols"], (name, t)
    assert got["twice"] == [12, 12], got


# --- row 6: the debug dump never fails a request -----------------------------------------------------


def test_a_failing_request_dump_still_forwards_the_reply(monkeypatch, capsys):
    """Row 6: with ``dump`` on, the relay appended the body to ``requests_full.jsonl`` with a
    bare ``open``: a full disk or a missing job directory raised out of ``relay`` and the
    session's request failed for a debugging side file. The reply is forwarded and its row
    written; the failed dump is one line."""
    import shutil as _sh

    job_doc = {"type": "message", "content": [{"type": "text", "text": "hi"}],
               "usage": {"input_tokens": 3, "output_tokens": 2}}

    async def send(upstream, stream=True):
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=json.dumps(job_doc).encode(), request=upstream)

    real_new = jobs.JobStore.new

    def new_with_dump_and_no_dir(self, **kw):
        j = real_new(self, **kw)
        j.dump = True
        _sh.rmtree(j.log_dir)  # the dump's directory is gone (pruned, unmounted, read-only...)
        return j

    monkeypatch.setattr(jobs.JobStore, "new", new_with_dump_and_no_dir)
    resp, out, err, job = _relay(send, PLAIN_BODY)
    assert resp.status_code == 200 and json.loads(resp.body) == job_doc
    (row,) = _request_rows(job)
    assert row["input_tokens"] == 3
    assert "[dump-error]" in capsys.readouterr().err


# --- rows 9, 10, 12: one copy of each shared constant ---------------------------------------------


def test_the_edit_tools_are_one_constant():
    """Row 9: runner.EDIT_TOOLS (a set) and derive.EDIT_TOOLS (a tuple whose comment pointed at
    runner's) were two lists of the same four tools: the write rule (an edit tool with no path
    stores NULL) and the read rule (files_touched) must name the same tools."""
    from localagents import derive, runner

    assert runner.EDIT_TOOLS is derive.EDIT_TOOLS


def test_the_metrics_columns_and_the_task_cut_are_one_rule(monkeypatch):
    """Row 10: the seven metrics columns lived in Job._METRIC_COLUMNS and
    derive._METRICS_COLUMNS, and the 300-char task cut in Job.summary and derive._truncate —
    the in-process summary and the row summary must be the same function of the same job."""
    from localagents import derive

    assert jobs.Job._METRIC_COLUMNS is derive._METRICS_COLUMNS
    j = jobs.Job(id="x", task="t" * 400, model="m", endpoint="e", base_url="u", cwd="c", run_cwd="c",
                 isolation="none")
    calls = []
    real = derive._truncate

    def spy(task):
        calls.append(task)
        return real(task)

    monkeypatch.setattr(derive, "_truncate", spy)
    assert j.summary()["task"] == "t" * 300 + "…"
    assert calls == ["t" * 400]


def test_the_terminal_statuses_are_one_constant():
    """Row 12: Job.done's list and server._TERMINAL_STATUSES ("Job.done's list") were two
    tuples. One constant, and Job.done reads it for every status the schema's CHECK admits."""
    from localagents import server

    assert server._TERMINAL_STATUSES is jobs.TERMINAL_STATUSES
    for status in ("queued", "running", "succeeded", "failed", "cancelled", "timeout", "lost"):
        j = jobs.Job(id="x", task="t", model="m", endpoint="e", base_url="u", cwd="c", run_cwd="c",
                     isolation="none", status=status)
        assert j.done is (status in jobs.TERMINAL_STATUSES)
