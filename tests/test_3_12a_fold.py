"""Story 3.12a-fold: the four defects the operator agreed from the 3.12a review, each a rule
with its witness (EPICS 3.12, the review record and the operator's dispositions read at the
note 2026-10-31).

F1 A blank pick value is pickable: a job with a null model or endpoint, or a request with a
   null/blank path, sorts on "" and drew a second option of value "" labelled " (1)"; picking
   it deleted the filter instead of matching those rows (the "any" option owns ""). The blank
   key now has its own value, ``PICK_BLANK``, labelled "(blank) (count)", sorted first after
   "any"; the counts and the "(0)" holdover map "" <-> PICK_BLANK so a held blank survives a
   push.

The behavioural tests run the page scripts under node over tests/fakedom.js, the loader
pattern from tests/test_3_10c_fold.py (rebuilt here rather than importing another test module:
ruling (o), one test file per story).
"""

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "localagents" / "monitor" / "static"

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
    script = tmp_path / "page_probe.js"
    script.write_text(_PROLOGUE % {"fakedom": json.dumps(str(ROOT / "tests" / "fakedom.js")),
                                   "static": json.dumps(str(STATIC)), "body": body})
    p = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


# --- F1: a blank pick value is pickable ----------------------------------------------------------

_BLANK_JOBS = """
  var mk = function (id, model) { return {
    id: id, status: "succeeded", server_live: false, started_at: 1000000, model: model,
    endpoint: "ai1", elapsed_s: 5, num_turns: 1, tool_calls: 0, errors: 0, compactions: 0,
    ctx_pct: null, prefill_tps: null, gen_tps: null, last_activity: null }; };
  var jobs = [
    mk("00000000-1111-2222-3333-444444444444", null),
    mk("00000001-1111-2222-3333-444444444444", "m1"),
  ];
  var el = M.jobsTable(jobs, { id: "t312a-fold-blank-jobs", now: 1000000 });
  var options = function (host) {
    return host.querySelector('select[data-filter="model"]').children.map(function (o) {
      return { value: o.getAttribute("value"), label: o.textContent }; });
  };
  var before = options(el);
  // the blank option, found by its label: the value is the fix's own sentinel
  var blankOpt = null;
  before.forEach(function (o) { if (/^\\(blank\\)/.test(o.label)) blankOpt = o; });
  var sel = el.querySelector('select[data-filter="model"]');
  sel.value = blankOpt ? blankOpt.value : "";
  sel.dispatchEvent(new Event("change"));
  return {
    sentinel: typeof M.PICK_BLANK === "string" ? M.PICK_BLANK : null,
    before: before,
    empties: before.filter(function (o) { return o.value === ""; }),
    blankOpt: blankOpt,
    rows: el.querySelectorAll("tbody tr").length,
    cells: el.querySelectorAll("tbody tr td").map(function (td) { return td.textContent; }),
    state: JSON.parse(JSON.stringify(M.TABLE_STATE["t312a-fold-blank-jobs"].filters)),
    after: options(el),
  };
"""


def test_f1_the_blank_pick_value_is_pickable_on_the_jobs_model_column(tmp_path):
    """The review's defect, verbatim: a null model sorts on "" and pickValues listed "" — the
    select drew a second option of value "" labelled " (1)" beside "any", and setFilter
    deletes the key for "", so choosing it cleared the filter instead of matching those rows.
    The blank key gets its own value (PICK_BLANK) and label, sorted first after "any"; choosing
    it keeps exactly the blank rows and stores the sentinel in the state."""
    got = _page_js(tmp_path, _BLANK_JOBS)
    # "any" owns "" alone — no second empty-valued option (the collision)
    assert got["empties"] == [{"value": "", "label": "any"}], got["before"]
    assert got["blankOpt"] is not None, got["before"]
    assert got["blankOpt"]["label"] == "(blank) (1)", got["before"]
    assert got["sentinel"] == "∅", got
    assert got["before"][1] == got["blankOpt"], "the blank option sorts first after any"
    # choosing it keeps the one blank row (the m1 row drops) and holds the sentinel
    assert got["rows"] == 1, got
    assert got["state"] == {"model": got["sentinel"]}, got
    assert got["after"][1] == {"value": got["sentinel"], "label": "(blank) (1)"}, got["after"]


_BLANK_REQUESTS = """
  var mk = function (path) { return { t: 1000, path: path, http_status: 200, error: "", ms: 800,
    job_id: null, tokens: { input: 10, output: 1, cache_read: 0, cache_creation: 0 } }; };
  var el = M.requestsTable([mk(""), mk("/v1/messages")], { job: false, now: 1000000 });
  var sel = el.querySelector('select[data-filter="path"]');
  var before = sel.children.map(function (o) {
    return { value: o.getAttribute("value"), label: o.textContent }; });
  var blankOpt = null;
  before.forEach(function (o) { if (/^\\(blank\\)/.test(o.label)) blankOpt = o; });
  sel.value = blankOpt ? blankOpt.value : "";
  sel.dispatchEvent(new Event("change"));
  return {
    sentinel: typeof M.PICK_BLANK === "string" ? M.PICK_BLANK : null,
    before: before,
    empties: before.filter(function (o) { return o.value === ""; }),
    blankOpt: blankOpt,
    rows: el.querySelectorAll("tbody tr").length,
    state: JSON.parse(JSON.stringify(M.TABLE_STATE["requests-job"].filters)),
  };
"""


def test_f1_the_blank_pick_value_is_pickable_on_the_requests_path_column(tmp_path):
    """The review's "Same `|| ""` fallback ... requests path": an empty path sorts on "" and
    drew the same colliding option. The sentinel is the same rule."""
    got = _page_js(tmp_path, _BLANK_REQUESTS)
    assert got["empties"] == [{"value": "", "label": "any"}], got["before"]
    assert got["blankOpt"] is not None and got["blankOpt"]["label"] == "(blank) (1)", got["before"]
    assert got["sentinel"] == "∅", got
    assert got["rows"] == 1, got
    assert got["state"] == {"path": got["sentinel"]}, got


def test_f1_a_held_blank_survives_a_push_as_a_zero_option(tmp_path):
    """The counts and pickValues' "(0)" holdover use the same "" <-> PICK_BLANK mapping: a
    push that drops every blank row keeps the sentinel selected and listed "(blank) (0)"."""
    got = _page_js(tmp_path, """
  var job = function (id, model) { return {
    id: id, status: "succeeded", server_live: false, started_at: 1000000, model: model,
    endpoint: "ai1", elapsed_s: 5, num_turns: 1, tool_calls: 0, errors: 0, compactions: 0,
    ctx_pct: null, prefill_tps: null, gen_tps: null, last_activity: null }; };
  var el = M.jobsTable([job("00000000-1111-2222-3333-444444444444", null)],
    { id: "t312a-fold-held-blank", now: 1000000 });
  var sel = el.querySelector('select[data-filter="model"]');
  var blankOpt = null;
  sel.children.forEach(function (o) { if (/^\\(blank\\)/.test(o.textContent)) blankOpt = o; });
  sel.value = blankOpt ? blankOpt.getAttribute("value") : "";
  sel.dispatchEvent(new Event("change"));
  // a second call on the SAME id with no blank model left
  var el2 = M.jobsTable([job("00000001-1111-2222-3333-444444444444", "m1")],
    { id: "t312a-fold-held-blank", now: 1000000 });
  var sel2 = el2.querySelector('select[data-filter="model"]');
  return {
    sentinel: typeof M.PICK_BLANK === "string" ? M.PICK_BLANK : null,
    options: sel2.children.map(function (o) {
      return { value: o.getAttribute("value"), label: o.textContent }; }),
    selected: sel2.value,
    state: JSON.parse(JSON.stringify(M.TABLE_STATE["t312a-fold-held-blank"].filters)),
  };
""")
    assert got["sentinel"] == "∅", got
    assert {"value": got["sentinel"], "label": "(blank) (0)"} in got["options"], got["options"]
    assert got["selected"] == got["sentinel"], got
    assert got["state"] == {"model": got["sentinel"]}, got


# --- F2: thousands separators in the number box ------------------------------------------------


def test_f2_the_number_box_strips_thousands_separators_for_ranges_and_comparators(tmp_path):
    """The cells print fmt.int figures ("1,300"), but the number readings parsed the box as
    written: ">1,300" read as ">1" (parseFloat stops at the comma) and "1,000-20,000" failed
    the range regex and fell to a substring hunt that could not match the cell's "1,000-20,000".
    The range and comparator readings strip commas first; the substring reading keeps the text
    as typed (the next test)."""
    got = _page_js(tmp_path, """
  var vals = [999, 1000, 1299, 1300, 1301, 20000, 20001];
  var rows = vals.map(function (v) { return { v: v }; });
  var cols = [{ name: "turns", filter: "number", title: null,
    cell: function (r) { return M.h("td", { text: M.fmt.int(r.v) }); },
    sortKey: function (r) { return r.v; } }];
  var el = M.dataTable("t312a-fold-commas", cols, rows, {});
  var typeIn = function (v) {
    var input = el.querySelector('input[data-filter="turns"]');
    input.value = v;
    input.dispatchEvent(new Event("input"));
    var p = el.querySelector(".table-pager").textContent;
    var m = / of (\\d+)/.exec(p);
    if (m) return +m[1];
    m = /^(\\d+) rows?$/.exec(p);
    return m ? +m[1] : (/^no rows match/.test(p) ? 0 : -1);
  };
  return {
    printed: el.querySelector("tbody tr td").textContent,
    gt: typeIn(">1,300"), // 1301, 20000, 20001
    range: typeIn("1,000-20,000"), // inclusive: 1000 through 20000
    lt: typeIn("<1,301"), // 999, 1000, 1299, 1300
  };
""")
    assert got["printed"] == "999", got
    assert got["gt"] == 3, got  # before: parseFloat("1,300") = 1, every row matched
    assert got["range"] == 5, got  # 1000, 1299, 1300, 1301, 20000
    assert got["lt"] == 4, got


def test_f2_the_substring_reading_keeps_the_typed_comma_text(tmp_path):
    """"1,3" must still match a cell printing "1,300" as text: the cell text itself carries
    the comma, so the substring needle is the box as typed, not the comma-stripped copy the
    range and comparator readings use."""
    got = _page_js(tmp_path, """
  var job = function (id, turns) { return {
    id: id, status: "succeeded", server_live: false, started_at: 1000000, model: "m1",
    endpoint: "ai1", elapsed_s: 5, num_turns: turns, tool_calls: 0, errors: 0, compactions: 0,
    ctx_pct: null, prefill_tps: null, gen_tps: null, last_activity: null }; };
  var el = M.jobsTable([job("00000000-1111-2222-3333-444444444444", 1300),
    job("00000001-1111-2222-3333-444444444444", 5)], { id: "t312a-fold-comma-text", now: 1000000 });
  var idx = el.querySelectorAll("thead tr")[0].children.map(function (th) {
    return th.textContent; }).indexOf("turns");
  var printed = el.querySelectorAll("tbody tr td")[idx].textContent;
  var input = el.querySelector('input[data-filter="turns"]');
  input.value = "1,3";
  input.dispatchEvent(new Event("input"));
  return { printed: printed, rows: el.querySelectorAll("tbody tr").length,
    pager: el.querySelector(".table-pager").textContent };
""")
    assert got["printed"] == "1,300", got
    assert got["rows"] == 1, got
    assert got["pager"] == "1–1 of 1 · previous · next · clear filters", got


# --- F3: a table's view state follows the operator across endpoint pages -----------------------


def test_f3_endpoint_table_ids_keep_separate_view_state(tmp_path):
    """requestsTable ignored ``opts.id``, so every endpoint page's requests table drew through
    the one "requests-endpoint" entry: a filter typed on ai1's page was in force when dgx1-a's
    opened (jobsTable already honoured ``opts.id``). Two ids, one filter on the first, the
    second's state untouched and its rows whole."""
    got = _page_js(tmp_path, """
  var req = function (error) { return { t: 1000, path: "/v1/messages", http_status: 200,
    error: error, ms: 800, job_id: null,
    tokens: { input: 10, output: 1, cache_read: 0, cache_creation: 0 } }; };
  var rowsA = [req("boom"), req("")];
  var rowsB = [req("boom"), req("")];
  var a = M.requestsTable(rowsA, { id: "requests-endpoint-a", job: false, now: 1000000 });
  var input = a.querySelector('input[data-filter="error"]');
  input.value = "boom";
  input.dispatchEvent(new Event("input"));
  var b = M.requestsTable(rowsB, { id: "requests-endpoint-b", job: false, now: 1000000 });
  var stateA = M.TABLE_STATE["requests-endpoint-a"];
  var stateB = M.TABLE_STATE["requests-endpoint-b"];
  return {
    stateA: stateA ? JSON.parse(JSON.stringify(stateA.filters)) : null,
    stateB: stateB ? JSON.parse(JSON.stringify(stateB.filters)) : null,
    distinct: !!stateA && !!stateB && stateA !== stateB,
    pagerA: a.querySelector(".table-pager").textContent,
    pagerB: b.querySelector(".table-pager").textContent,
  };
""")
    assert got["stateA"] == {"error": "boom"}, got
    assert got["stateB"] == {}, got
    assert got["distinct"] is True, got
    assert got["pagerA"] == "1–1 of 1 · previous · next · clear filters", got
    assert got["pagerB"] == "2 rows", got


def test_f3_the_endpoint_page_keys_both_tables_by_the_endpoint_name():
    """endpoint.js draws the one jobs table and the one requests table on every endpoint's
    page; both ids must carry the page's name so the view is per endpoint (the job page keeps
    the plain "requests-job": it has one table)."""
    src = (STATIC / "endpoint.js").read_text()
    assert 'id: "jobs-endpoint-" + name' in src
    assert 'id: "requests-endpoint-" + name' in src


# --- F4: the smoke's full-id witness reads the id link ------------------------------------------


def test_f4_the_full_id_witness_reads_the_id_links_title_and_asserts_it():
    """The (f) witness read the job cell's td title, but jobLink puts the full uuid on its child
    ``a``, so f.jobFullId.title was null in every d1 while the match itself was real. The
    witness reads the child and asserts it EQUALS fullId (d1 lands before the throw, so a
    failed run still hands back the JSON). The runtime reading is the committed smoke's f
    block; this pins the script's shape."""
    src = (ROOT / "tools" / "smoke" / "tables.js").read_text()
    assert 'const childTitle = (col, row) =>' in src
    assert 'title: childTitle("job", jobRow)' in src
    assert 'fJob.ok = fJob.title === fullId' in src
    assert "if (!f.ok) throw new Error(" in src
    # the old read stays only as the defect's own evidence
    assert 'tdTitle: cellAttr("job", "title")' in src
