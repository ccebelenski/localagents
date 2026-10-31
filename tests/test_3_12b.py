"""Story 3.12b: two-level table refresh (spec docs/superpowers/specs/2026-10-31-table-refresh-
design.md, approved 2026-10-31: "a table that hasn't changed content needs no refresh - can we
cache a quick hash over the content and not do a refresh if the hash doesn't change? And then we
do your patch in place idea on top. This gives us two levels of content control.").

Level 1 (B1): ``dataTable`` keeps the last draw per id in ``TABLE_DOM``; a call whose container
is still connected and whose rows hash the same returns the SAME container and touches nothing
(``renders.tableSkipped`` counts it). The empty list lives INSIDE the container now (the
``.empty`` sentence, with the "clear filters" link when a filter is set), so a table can empty
and refill without the container (and its scroll frame) ever being replaced.

Level 2 (B2): when the hash differs, the wrapper, thead, header row, filter row, tbody and pager
are patched in place; the body is reconciled row by row (keyed by ``opts.key`` for the jobs
table, positional for the requests table), keeping every tr whose row signature is unchanged.

Level 3 callers (B3): ``mount(parent, els)`` re-inserts only when the children actually differ;
the overview and the endpoint page's sections stop re-inserting the table, so the scroll frame
survives pushes (the operator: "on a refresh the scroll bars appear on the screen briefly in
firefox, then go back dormant").

The behavioural tests run the page scripts under node over tests/fakedom.js, the loader pattern
from tests/test_3_12a_fold.py (rebuilt here rather than importing another test module: ruling
(o), one test file per story).
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
Promise.resolve(out).then(function (o) { process.stdout.write(JSON.stringify(o)); });
"""


def _page_js(tmp_path, body: str):
    script = tmp_path / "page_probe.js"
    script.write_text(_PROLOGUE % {"fakedom": json.dumps(str(ROOT / "tests" / "fakedom.js")),
                                   "static": json.dumps(str(STATIC)), "body": body})
    p = subprocess.run([NODE, str(script)], capture_output=True, text=False, timeout=60)
    assert p.returncode == 0, (p.stderr.decode(), p.stdout.decode())
    return json.loads(p.stdout.decode())


# --- B1: level 1, the content hash ---------------------------------------------------------------

_ONE_COL = """
  var cols = [{ name: "w", title: null,
    cell: function (r) { return M.h("td", { text: r.w }); }, sortKey: function (r) { return r.w; } }];
  var rows = [{ w: "a" }, { w: "b" }, { w: "c" }];
"""


def test_b1_equal_rows_return_the_same_container_and_count_the_skip(tmp_path):
    """Level 1: the second call of the same id with rows that hash the same returns the SAME
    container, touches nothing (the drawn tr is the identical element), and advances
    renders.tableSkipped by exactly one."""
    got = _page_js(tmp_path, _ONE_COL + """
  var el = M.dataTable("t312b-skip", cols, rows, {});
  document.body.appendChild(el); // the isConnected check: a table in no page is rebuilt
  var tr0 = el.querySelector("tbody tr");
  var before = M.renders.tableSkipped || 0;
  var copy = JSON.parse(JSON.stringify(rows)); // equal rows, fresh objects
  var el2 = M.dataTable("t312b-skip", cols, copy, {});
  return {
    same: el2 === el,
    trKept: el.querySelector("tbody tr") === tr0,
    before: before,
    skipped: M.renders.tableSkipped,
    rows: el.querySelectorAll("tbody tr").length,
  };
""")
    assert got["same"] is True, got
    assert got["trKept"] is True, got
    assert got["skipped"] == got["before"] + 1, got
    assert got["rows"] == 3, got


def test_b1_a_hash_change_keeps_the_container_patched(tmp_path):
    """A changed row (one field moved) breaks the hash: the call falls to level 2, but the
    CONTAINER is the same element — it was never discarded, only its content differs."""
    got = _page_js(tmp_path, _ONE_COL + """
  var el = M.dataTable("t312b-hash", cols, rows, {});
  document.body.appendChild(el);
  var changed = JSON.parse(JSON.stringify(rows));
  changed[1].w = "z";
  var el2 = M.dataTable("t312b-hash", cols, changed, {});
  return {
    same: el2 === el,
    texts: el.querySelectorAll("tbody tr td").map(function (td) { return td.textContent; }),
    skipped: (M.renders.tableSkipped || 0),
  };
""")
    assert got["same"] is True, got
    assert got["texts"] == ["a", "z", "c"], got
    assert got["skipped"] == 0, got  # a hash change never counts as a skip


def test_b1_the_empty_sentence_lives_in_the_container_with_the_clear_link(tmp_path):
    """The empty-list early return (a bare .empty div with no container) goes: the container
    persists and holds the .empty sentence, and when a filter is set the "clear filters" button
    rides after it — the way out of an empty view stays clickable. Clearing it and refilling
    rows draws the table back in the same container."""
    got = _page_js(tmp_path, _ONE_COL + """
  var el = M.dataTable("t312b-empty", cols, rows, { empty: "no w yet" });
  document.body.appendChild(el);
  var input = el.querySelector('input[data-filter="w"]');
  input.value = "q";
  input.dispatchEvent(new Event("input")); // a filter set (and matched by nothing here)
  var el2 = M.dataTable("t312b-empty", cols, [], { empty: "no w yet" }); // the rows are gone
  var out = { same2: el2 === el };
  var sentence = el2.querySelector(".empty");
  out.empty = sentence ? sentence.textContent : null;
  out.topChildren = el2.childNodes.map(function (n) { return n.nodeType === 3 ? "#text" : n.tagName + "." + n.className; });
  var btn = el2.querySelector(".pager-btn");
  out.cleared = { label: btn ? btn.textContent : null };
  if (btn) btn.dispatchEvent(new Event("click"));
  out.stateFilters = JSON.parse(JSON.stringify(M.TABLE_STATE["t312b-empty"].filters));
  var el3 = M.dataTable("t312b-empty", cols, rows, {}); // refill
  out.same3 = el3 === el;
  out.rowsBack = el3.querySelectorAll("tbody tr").length;
  return out;
""")
    assert got["same2"] is True and got["same3"] is True, got
    assert got["empty"] == "no w yet", got
    assert got["topChildren"] == ["DIV.empty", "BUTTON.pager-btn"], got
    assert got["cleared"]["label"] == "clear filters", got
    assert got["stateFilters"] == {}, got  # the click deleted the filter keys
    assert got["rowsBack"] == 3, got


# --- B2: level 2, the patch -----------------------------------------------------------------------

_JOBS = """
  var mk = function (id, turns) { return { id: id, status: "succeeded", server_live: false,
    started_at: 1000000, model: "m1", endpoint: "ai1", elapsed_s: 5, num_turns: turns,
    tool_calls: 0, errors: 0, compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null,
    last_activity: null }; };
  var jobs = [mk("00000000-1111-2222-3333-444444444444", 1),
              mk("00000001-1111-2222-3333-444444444444", 2),
              mk("00000002-1111-2222-3333-444444444444", 3)];
  var push = function (at, turns) { return jobs.map(function (j, i) {
    var c = JSON.parse(JSON.stringify(j)); if (i === at) c.num_turns = turns; return c; }); };
  var trs = function (el) { return el.querySelectorAll("tbody tr"); };
"""


def test_b2_the_parts_persist_across_a_hash_change(tmp_path):
    """A hash change is level 2, not a rebuild: the wrapper, thead, filter row and tbody are
    the SAME elements (the scroll wrapper is created only on a first build — no new scroll
    frame means no overlay-scrollbar flash, no lost sideways scroll)."""
    got = _page_js(tmp_path, _JOBS + """
  var el = M.jobsTable(jobs, { id: "t312b-identity", now: 1000000 });
  document.body.appendChild(el);
  var wrap = el.querySelector(".table-wrap"), thead = el.querySelector("thead");
  var frow = el.querySelector("tr.table-filters"), tbody = el.querySelector("tbody");
  var el2 = M.jobsTable(push(1, 9), { id: "t312b-identity", now: 1000000 });
  return {
    container: el2 === el,
    wrap: el.querySelector(".table-wrap") === wrap,
    thead: el.querySelector("thead") === thead,
    frow: el.querySelector("tr.table-filters") === frow,
    tbody: el.querySelector("tbody") === tbody,
    turns: trs(el).map(function (tr) { return tr.children[6].textContent; }),
  };
""")
    assert got["container"] and got["wrap"] and got["thead"] and got["frow"] and got["tbody"], got
    assert got["turns"] == ["1", "9", "3"], got


def test_b2_keyed_rows_keep_their_elements_across_a_hash_change(tmp_path):
    """Keyed reconcile (jobsTable passes opts.key = job.id): the tr of the changed row is
    replaced; every unchanged row's tr is the SAME element — its cells were never rebuilt."""
    got = _page_js(tmp_path, _JOBS + """
  var el = M.jobsTable(jobs, { id: "t312b-keyed", now: 1000000 });
  document.body.appendChild(el);
  var before = trs(el);
  var el2 = M.jobsTable(push(1, 9), { id: "t312b-keyed", now: 1000000 });
  var after = trs(el);
  return {
    first: after[0] === before[0],
    last: after[2] === before[2],
    changedIsNew: after[1] !== before[1],
    changedKey: after[1].getAttribute("data-key"),
    changedTurns: after[1].children[6].textContent,
    oldDetached: before[1].parentNode === null,
  };
""")
    assert got["first"] is True and got["last"] is True, got
    assert got["changedIsNew"] is True, got
    assert got["changedKey"] == "00000001-1111-2222-3333-444444444444", got
    assert got["changedTurns"] == "9", got
    assert got["oldDetached"] is True, got


def test_b2_a_kept_tr_is_moved_not_rebuilt_on_a_sort(tmp_path):
    """A sort click (an operator action, level 2 through the same patch): every row keeps its
    element — the trs MOVE into the new order (the sort is an index reordering, not a
    rebuild). The signature is unchanged, so the kept tr shows fresh-placed old cells."""
    got = _page_js(tmp_path, _JOBS + """
  var el = M.jobsTable(jobs, { id: "t312b-move", now: 1000000 });
  document.body.appendChild(el);
  var ths = el.querySelector("thead").children[0].children; // the head row (no :first-child in fakedom)
  var turnsTh = null;
  ths.forEach(function (th) { if (th.textContent === "turns") turnsTh = th; });
  // descending: the id ..0000 row (turns 1) goes from position 0 to position 2
  turnsTh.click(); turnsTh.click();
  var after = trs(el);
  return { ids: after.map(function (tr) { return tr.getAttribute("data-key"); }) };
""")
    # descending: turns 3,2,1 — the ..0000 row moved from 0 to 2 and is the SAME element that
    # was at position 0 before the clicks (captured via the identity chain below)
    assert got["ids"] == ["00000002-1111-2222-3333-444444444444",
                          "00000001-1111-2222-3333-444444444444",
                          "00000000-1111-2222-3333-444444444444"], got


def test_b2_a_kept_tr_moves_on_a_sort_its_element_survives(tmp_path):
    """The same sort as above, read by element identity: the tr that sat first (id ..0000)
    sits last after the two clicks (descending) — the same element, moved."""
    got = _page_js(tmp_path, _JOBS + """
  var el = M.jobsTable(jobs, { id: "t312b-move2", now: 1000000 });
  document.body.appendChild(el);
  var firstTr = trs(el)[0];
  var ths = el.querySelector("thead").children[0].children;
  var turnsTh = null;
  ths.forEach(function (th) { if (th.textContent === "turns") turnsTh = th; });
  turnsTh.click(); turnsTh.click();
  var after = trs(el);
  return { movedTo: after.indexOf(firstTr), count: after.length };
""")
    assert got["movedTo"] == 2, got  # was at 0, now at 2: the element moved, never replaced
    assert got["count"] == 3, got


def test_b2_surplus_trs_are_removed_when_rows_shrink(tmp_path):
    """A push that drops a row: the kept rows' elements survive, the dropped tr is gone from
    the tbody, and the count falls."""
    got = _page_js(tmp_path, _JOBS + """
  var el = M.jobsTable(jobs, { id: "t312b-shrink", now: 1000000 });
  document.body.appendChild(el);
  var before = trs(el);
  var el2 = M.jobsTable([jobs[0], jobs[2]], { id: "t312b-shrink", now: 1000000 });
  var after = trs(el);
  return {
    count: after.length,
    keptFirst: after[0] === before[0],
    keptLast: after[1] === before[2],
    droppedGone: before[1].parentNode === null,
  };
""")
    assert got["count"] == 2, got
    assert got["keptFirst"] is True and got["keptLast"] is True, got
    assert got["droppedGone"] is True, got


def test_b2_the_unkeyed_requests_table_reconciles_positionally(tmp_path):
    """requestsTable passes no opts.key (request rows carry no id): the tr at position i is
    kept when its signature matches, else replaced — the row below an insert rebuilds, as
    today, no worse."""
    got = _page_js(tmp_path, """
  var mk = function (t) { return { t: t, path: "/v1/messages", http_status: 200, error: "",
    ms: 800, job_id: null, tokens: { input: 10, output: 1, cache_read: 0, cache_creation: 0 } }; };
  var reqs = [mk(3), mk(2), mk(1)];
  var el = M.requestsTable(reqs, { job: false, now: 1000 });
  document.body.appendChild(el);
  var before = el.querySelectorAll("tbody tr");
  var push2 = [mk(3), mk(20), mk(1)]; // the middle row's figure moved
  var el2 = M.requestsTable(push2, { job: false, now: 1000 });
  var after = el.querySelectorAll("tbody tr");
  return {
    container: el2 === el,
    first: after[0] === before[0],
    middleReplaced: after[1] !== before[1],
    last: after[2] === before[2],
    oldDetached: before[1].parentNode === null,
  };
""")
    assert got["container"] is True, got
    assert got["first"] is True and got["last"] is True, got
    assert got["middleReplaced"] is True, got
    assert got["oldDetached"] is True, got


def test_b2_an_inputs_value_is_never_written_by_a_push(tmp_path):
    """The box's text IS the state (3.12a §2): a push keeps the input element and never
    touches its value — the caret and any half-typed bound survive."""
    got = _page_js(tmp_path, _ONE_COL + """
  var el = M.dataTable("t312b-input", cols, rows, {});
  document.body.appendChild(el);
  var input = el.querySelector('input[data-filter="w"]');
  input.value = "a";
  input.dispatchEvent(new Event("input")); // typing updates the state
  var changed = JSON.parse(JSON.stringify(rows)); changed[2].w = "cc";
  var el2 = M.dataTable("t312b-input", cols, changed, {});
  var input2 = el.querySelector('input[data-filter="w"]');
  return {
    sameInput: input2 === input,
    value: input2.value,
    state: M.TABLE_STATE["t312b-input"].filters.w,
    rowsShown: el.querySelectorAll("tbody tr").length,
  };
""")
    assert got["sameInput"] is True, got
    assert got["value"] == "a", got
    assert got["state"] == "a", got
    assert got["rowsShown"] == 1, got  # the filter stays in force through the push


def test_b2_a_focused_select_is_untouched_until_focusout(tmp_path):
    """A select the operator holds open is never rebuilt by a push (the open dropdown and the
    highlighted option would vanish): the draw marks it stale, and its FOCUSOUT — the row's one
    listener — rebuilds the options with the new counts."""
    got = _page_js(tmp_path, """
  var cols = [{ name: "s", filter: "pick", title: null,
    cell: function (r) { return M.h("td", { text: r.s }); }, sortKey: function (r) { return r.s; } }];
  var rows = [{ s: "x" }, { s: "x" }, { s: "y" }];
  var el = M.dataTable("t312b-sel", cols, rows, {});
  document.body.appendChild(el);
  var sel = el.querySelector('select[data-filter="s"]');
  var labels = function () { return sel.children.map(function (o) { return o.textContent; }); };
  var before = labels();
  sel.focus(); // the operator opens the list
  var pushRows = [{ s: "x" }, { s: "x" }, { s: "x" }, { s: "x" }, { s: "y" }]; // four x now
  var el2 = M.dataTable("t312b-sel", cols, pushRows, {});
  var during = labels();
  document.activeElement = document.body; // focus leaves (the browser fires focusout first)
  var ev = new Event("focusout");
  ev.target = sel;
  el.querySelector("tr.table-filters").dispatchEvent(ev);
  var after = labels();
  return { before: before, during: during, after: after,
    sameSel: el.querySelector('select[data-filter="s"]') === sel };
""")
    assert got["before"] == ["any", "x (2)", "y (1)"], got
    assert got["during"] == got["before"], got  # untouched while focused
    assert got["sameSel"] is True, got
    assert got["after"] == ["any", "x (4)", "y (1)"], got


def test_b2_sort_marks_patch_on_the_existing_ths(tmp_path):
    """The sort marks (class + aria-sort) are SET on the existing ths by a push — the th
    elements themselves never change, so any hover/keyboard state on the header survives."""
    got = _page_js(tmp_path, """
  var cols = [{ name: "w", numeric: true, title: null,
    cell: function (r) { return M.h("td", { text: r.w }); }, sortKey: function (r) { return r.w; } }];
  var el = M.dataTable("t312b-th", cols, [{ w: 1 }, { w: 2 }], {});
  document.body.appendChild(el);
  var th = el.querySelector("thead").children[0].children[0];
  th.click(); // sort ascending
  var marksBefore = { cls: th.className, aria: th.getAttribute("aria-sort") };
  var el2 = M.dataTable("t312b-th", cols, [{ w: 1 }, { w: 3 }], {});
  var th2 = el.querySelector("thead").children[0].children[0];
  return {
    sameTh: th2 === th,
    marksBefore: marksBefore,
    cls: th2.className,
    aria: th2.getAttribute("aria-sort"),
    order: el.querySelectorAll("tbody tr td").map(function (td) { return td.textContent; }),
  };
""")
    assert got["sameTh"] is True, got
    assert got["cls"] == "n sortable sort-asc", got
    assert got["aria"] == "ascending", got
    assert got["order"] == ["1", "3"], got


def test_b2_a_disconnected_container_forces_a_full_build(tmp_path):
    """A page change discards the DOM: the old entry's container is no longer connected, so the
    next call of that id builds a NEW container (a level-2 patch into a detached tree would
    draw the table in no page)."""
    got = _page_js(tmp_path, _JOBS + """
  var el = M.jobsTable(jobs, { id: "t312b-gone", now: 1000000 });
  document.body.appendChild(el);
  var el2 = M.jobsTable(push(0, 9), { id: "t312b-gone", now: 1000000 });
  var patchedSame = el2 === el;
  el.remove(); // the page changed: the container left the document
  var el3 = M.jobsTable(push(0, 9), { id: "t312b-gone", now: 1000000 });
  document.body.appendChild(el3);
  return {
    patchedSame: patchedSame,
    rebuilt: el3 !== el,
    rows: el3.querySelectorAll("tbody tr").length,
    turns: el3.querySelectorAll("tbody tr")[0].children[6].textContent,
  };
""")
    assert got["patchedSame"] is True, got  # level 2 keeps the container (RED before B2)
    assert got["rebuilt"] is True, got
    assert got["rows"] == 3, got
    assert got["turns"] == "9", got


# --- B3: the callers and mount ---------------------------------------------------------------------

_IDS = """
  var ids = {};
  ["meta", "conn-label", "hosts", "hosts-hidden", "jobs-body"].forEach(function (id) {
    var d = document.createElement("div");
    d.setAttribute("id", id);
    document.body.appendChild(d);
    ids[id] = d;
  });
  document.getElementById = function (id) { return ids[id] || null; };
  var mkJob = function (id, turns) { return { id: id, status: "succeeded", server_live: false,
    started_at: 1000000, model: "m1", endpoint: "ai1", elapsed_s: 5, num_turns: turns,
    tool_calls: 0, errors: 0, compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null,
    last_activity: null }; };
  var snapA = { now: 1000000, refresh_s: 5, window_s: 3600, hosts: [],
    jobs: [mkJob("00000000-1111-2222-3333-444444444444", 1),
           mkJob("00000001-1111-2222-3333-444444444444", 2)] };
"""


def test_b3_render_twice_keeps_the_overview_jobs_wrap(tmp_path):
    """render(snap) twice, the second with one job's num_turns changed: #jobs-body keeps the
    SAME .table-wrap element (the container returned by the renderer persists and mount()
    inserts it only when it is not already the whole body). The changed row's figure updates."""
    got = _page_js(tmp_path, _IDS + """
  M.render(snapA);
  var wrap1 = document.querySelector('[id="jobs-body"] .table-wrap');
  var trFirst = document.querySelector('[id="jobs-body"] tbody tr');
  var snapB = JSON.parse(JSON.stringify(snapA));
  snapB.now = 1000010;
  snapB.jobs[1].num_turns = 99;
  M.render(snapB);
  var wrap2 = document.querySelector('[id="jobs-body"] .table-wrap');
  var texts = Array.prototype.map.call(document.querySelectorAll('[id="jobs-body"] tbody tr'),
    function (tr) { return tr.children[6].textContent; });
  return {
    wrap: wrap1 !== null && wrap2 === wrap1,
    body: document.querySelector('[id="jobs-body"]').childNodes.length,
    keptRow: document.querySelector('[id="jobs-body"] tbody tr') === trFirst,
    texts: texts,
  };
""")
    assert got["wrap"] is True, got
    assert got["body"] == 1, got  # the container is #jobs-body's only child, once
    assert got["keptRow"] is True, got
    assert got["texts"] == ["1", "99"], got


def test_b3_mount_touches_nothing_when_the_children_already_match(tmp_path):
    """mount(parent, els): if the parent's child nodes are exactly els in order, do nothing;
    anything else replaceChildren once. Re-inserting the same element recreates its scroll
    frame, and that is the whole Firefox flash."""
    got = _page_js(tmp_path, """
  var p = M.h("div"), a = M.h("div"), b = M.h("div");
  M.mount(p, [a, b]); // first mount: the parent was empty, it replaces
  var calls = 0;
  var orig = p.replaceChildren;
  p.replaceChildren = function () { calls++; return orig.apply(p, arguments); };
  M.mount(p, [a, b]); // same children, same order: untouched
  var sameOrder = p.childNodes.slice();
  M.mount(p, [b, a]); // order differs: one replace
  return { calls: calls, sameOrder: sameOrder.length === 2,
    nowFirst: p.childNodes[0] === b, kids: p.childNodes.length };
""")
    assert got["calls"] == 1, got
    assert got["nowFirst"] is True, got
    assert got["kids"] == 2, got


def test_b3_the_endpoint_page_draws_twice_and_keeps_its_sections(tmp_path):
    """The endpoint page under node with a stubbed fetch: after the first draw, a second
    snapshot (the page's onSnapshot hook -> drawTables again) keeps the SAME jobs section,
    the SAME requests section and the SAME .table-wrap elements — drawTables ends in
    mount(tablesBox, [...]) over persistent elements, not a wholesale replaceChildren."""
    got = _page_js(tmp_path, _IDS + """
  var hist = { t: [1000000], gen_tps: [10.5], state: ["idle"],
    jobs: [mkJob("00000000-1111-2222-3333-444444444444", 1)], events: [] };
  var reqsBody = { requests: [
      { t: 1000000, path: "/v1/messages", http_status: 200, error: "", ms: 800, job_id: null,
        tokens: { input: 10, output: 1, cache_read: 0, cache_creation: 0 } },
      { t: 999000, path: "/api/hello", http_status: 200, error: "", ms: 5, job_id: null,
        tokens: { input: 0, output: 0, cache_read: 0, cache_creation: 0 } }],
    totals: { count: 2, input_tokens: 100, output_tokens: 20, cache_read: 5,
      cache_creation: 1, ms: 1500, errors: 0 } };
  global.fetch = function (url) {
    var body = String(url).indexOf("/requests") >= 0 ? reqsBody : hist;
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(body); } });
  };
  var fakeState = { snap: snapA, range: "1h" };
  var container = document.createElement("div");
  document.body.appendChild(container);
  var teardown = M.pages.endpoint(container, "ai1", fakeState);
  return new Promise(function (resolve) {
    setTimeout(function () { // the two fetch stubs settle
      var jobs1 = container.querySelector("section.ep-jobs-list");
      var wrap1 = jobs1 && jobs1.querySelector(".table-wrap");
      var req1 = container.querySelector("section.requests");
      var reqWrap1 = req1 && req1.querySelector(".table-wrap");
      var realNow = Date.now;
      Date.now = function () { return realNow() + 60000; }; // past REFETCH_MIN_MS: the hook re-fetches
      M.render(snapA); // a snapshot lands: the page's hook -> fetchAll -> draw() -> drawTables again
      setTimeout(function () {
        var jobs2 = container.querySelector("section.ep-jobs-list");
        var req2 = container.querySelector("section.requests");
        var wrap2 = jobs2 && jobs2.querySelector(".table-wrap");
        var out = {
          built: !!wrap1 && !!reqWrap1,
          jobsSection: jobs1 !== null && jobs2 === jobs1,
          reqSection: req1 !== null && req2 === req1,
          jobsWrap: jobs2 && jobs2.querySelector(".table-wrap") === wrap1,
          reqWrap: req2 && req2.querySelector(".table-wrap") === reqWrap1,
          totals: (container.querySelector(".totals") || {}).textContent || null,
          h2: jobs2 && jobs2.children[0].textContent,
          tablesKids: container.querySelector(".endpoint-detail") &&
            container.querySelectorAll("section").length,
        };
        teardown();
        resolve(out);
      }, 60);
    }, 60);
  });
""")
    assert got["built"] is True, got
    assert got["jobsSection"] is True, got
    assert got["reqSection"] is True, got
    assert got["jobsWrap"] is True, got
    assert got["reqWrap"] is True, got
    assert got["tablesKids"] == 2, got  # jobs + requests; no events section
    assert "requests" in (got["totals"] or ""), got
    assert got["h2"] == "Jobs · ai1 · last 1h", got
