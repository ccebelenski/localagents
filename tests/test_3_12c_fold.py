"""Story 3.12c-fold: the DeepSeek review of 3.12c (EPICS 3.12, job e04f55b1, 2026-10-31) found
three defects, each a rule with its witness.

F1: the picker's facets excluded its own column by OBJECT IDENTITY (`col === except`), and the
persistent time button passed its BUILDING call's `col` object, so after a push the reopened
panel faceted its date counts and hour list by its own time filter (live: "today (60)", hour 11
only, where "today (120)" and hours 10/11/12 are right). The exclusion is now by name, and the
button passes the name so the toggle resolves the CURRENT call's column.

F2: the level-1 hash excluded opts.now, so a push crossing local midnight over an unchanged row
set was skipped and the picker's relative summary kept saying "today" for what was now
yesterday. The hash carries the local day of opts.now now.

F3: the endpoint page's events section kept its `cache.sig` when the events went to [], so the
SAME events returning later reused the old body with its day-stamps; the sig is now cleared
when the events are absent.

The loader is tests/test_3_12c.py's (rebuilt here, not imported: ruling (o)).
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


# --- F1: the faceting excludes by NAME, the button passes the name -------------------------------

# A hand table with a time column and a pick column, five rows on one local day at hours
# 10, 11, 11, 12, 12 (tags x/x/y/y/y). NOW is that day at 15:00, built from local components.
_F1_ROWS = """
  var day = function (y, mo, d, h) { return new Date(y, mo, d, h, 7, 0).getTime() / 1000; };
  var NOW = new Date(2026, 9, 31, 15, 0, 0).getTime() / 1000;
  var TODAY = "2026-10-31";
  var mk = function (id, hour, tag) { return { id: id, v: day(2026, 9, 31, hour), tag: tag }; };
  var rows = [mk("r1", 10, "x"), mk("r2", 11, "x"), mk("r3", 11, "y"),
              mk("r4", 12, "y"), mk("r5", 12, "y")];
  var copyRows = function (rs, changeId) {
    return rs.map(function (r) {
      return { id: r.id === changeId ? r.id + "b" : r.id, v: r.v, tag: r.tag };
    });
  };
  var timeCol = function () { return { name: "started", filter: "time", title: null,
    cell: function (r) { return M.h("td", { text: M.fmt.time(r.v) }); },
    sortKey: function (r) { return r.v; } }; };
  var pickCol = function () { return { name: "tag", filter: "pick", title: null,
    cell: function (r) { return M.h("td", { text: r.tag }); },
    sortKey: function (r) { return r.tag; } }; };
  var draw = function (id, rs) {
    return M.dataTable(id, [timeCol(), pickCol()], rs, { now: NOW });
  };
  var labels = function (sel) {
    return Array.prototype.map.call(sel.children, function (o) { return o.textContent; });
  };
  var values = function (sel) {
    return Array.prototype.map.call(sel.children, function (o) { return o.getAttribute("value"); });
  };
  var setOpt = function (sel, v) { sel.value = v; sel.dispatchEvent(new Event("change")); };
  var panelBtn = function (el, name) {
    return Array.prototype.filter.call(el.querySelectorAll(".filter-panel .pager-btn"),
      function (b) { return b.textContent === name; })[0];
  };
"""


def test_f1_a_reopened_picker_after_a_push_facets_the_other_columns_only(tmp_path):
    """The witness of the review: state "2026-10-31 11-11" set through the panel, then a push
    (new row objects, a changed id -> level 2 makes new columns too), then a click on the
    persistent button. The reopened date option must read "today (5)" — the rows the OTHER
    columns' filters leave, the time filter excluded — and the hour selects must list 10, 11
    and 12; the defect read "today (2)" and hour 11 alone (its own filter faceting itself)."""
    got = _page_js(tmp_path, _F1_ROWS + """
  var el = draw("t312cfold-f1a", rows);
  document.body.appendChild(el); // level 1 and 2 only persist a container that is in the document
  var btn = function () { return el.querySelector('button.filter-pick[data-filter="started"]'); };
  var b0 = btn();
  var dSel = function () { return el.querySelector(".filter-panel select.fp-date"); };
  var fSel = function () { return el.querySelector(".filter-panel select.fp-from"); };
  var tSel = function () { return el.querySelector(".filter-panel select.fp-to"); };
  b0.click(); // the first open is the BUILDING call's own col object
  var firstOpen = { date: labels(dSel()) };
  setOpt(dSel(), TODAY);
  var afterDate = { from: values(fSel()), to: values(tSel()) };
  setOpt(fSel(), "11");
  setOpt(tSel(), "11");
  var state = M.TABLE_STATE["t312cfold-f1a"].filters.started;
  panelBtn(el, "done").click(); // close
  M.dataTable("t312cfold-f1a", [timeCol(), pickCol()], copyRows(rows, "r1"), { now: NOW });
  btn().click(); // REOPEN: the persistent button's listener is the BUILDING call's
  var reopen = { date: labels(dSel()), from: values(fSel()), to: values(tSel()),
    fromText: labels(fSel()), toText: labels(tSel()),
    state: M.TABLE_STATE["t312cfold-f1a"].filters.started,
    sameBtn: btn() === b0 };
  var ev = new Event("keydown"); ev.key = "Escape"; document.dispatchEvent(ev);
  return { firstOpen: firstOpen, afterDate: afterDate, state: state, reopen: reopen };
""")
    # the first open (the building call's own col) already facets correctly
    assert got["firstOpen"]["date"] == ["any", "today (5)"], got["firstOpen"]
    assert got["afterDate"]["from"] == ["", "10", "11", "12"], got["afterDate"]
    assert got["state"] == "2026-10-31 11-11", got
    r = got["reopen"]
    assert r["sameBtn"] is True, r
    assert r["state"] == "2026-10-31 11-11", r
    assert r["date"] == ["any", "today (5)"], r  # the defect read "today (2)" — its own filter's rows
    assert r["from"] == ["", "10", "11", "12"], r  # the defect listed hour 11 alone
    assert r["fromText"] == ["any", "10:00", "11:00", "12:00"], r
    assert r["toText"] == ["any", "10:59", "11:59", "12:59"], r


def test_f1_b_the_pick_s_faceting_ignores_its_own_filter_after_a_push(tmp_path):
    """The same exclusion, through the pick's own option counts: with tag "x" set, the "y"
    option still counts its rows (the pick facets over the other columns' filters), and a push
    (level 2: a changed id, same tags) leaves the persistent select's counts reading them."""
    got = _page_js(tmp_path, _F1_ROWS + """
  var el = draw("t312cfold-f1b", rows);
  document.body.appendChild(el);
  var sel = function () { return el.querySelector('select[data-filter="tag"]'); };
  var before = labels(sel());
  setOpt(sel(), "x");
  var set = { state: M.TABLE_STATE["t312cfold-f1b"].filters.tag,
    options: labels(sel()), matched: el.querySelectorAll("tbody tr").length };
  M.dataTable("t312cfold-f1b", [timeCol(), pickCol()], copyRows(rows, "r2"), { now: NOW });
  var after = { options: labels(sel()), matched: el.querySelectorAll("tbody tr").length,
    sameSel: sel() === sel() };
  return { before: before, set: set, after: after };
""")
    assert got["before"] == ["any", "x (2)", "y (3)"], got["before"]
    assert got["set"]["state"] == "x", got["set"]
    assert got["set"]["options"] == ["any", "x (2)", "y (3)"], got["set"]  # y counts through x
    assert got["set"]["matched"] == 2, got["set"]
    assert got["after"]["options"] == ["any", "x (2)", "y (3)"], got["after"]  # after a push too
    assert got["after"]["matched"] == 2, got["after"]


def test_f1_c_the_source_excludes_by_name_and_the_button_passes_the_name():
    """The rule in the source: `filterIdx` compares column NAMES, and the persistent button
    passes `col.name` (the toggle resolves the current call's column)."""
    src = (STATIC / "app.js").read_text()
    assert "col.name === except.name" in src
    assert "toggleTimePanel(col.name, btn)" in src


# --- F2: the level-1 hash carries the local day of the table's clock ----------------------------

# One job and a state naming 2026-10-31: at 23:59:50 that day the button reads "today"; twenty
# seconds later, past local midnight, the same absolute date is "yesterday". Both clocks are
# built from local components (never a parsed string).
_F2_ROWS = """
  var mkJob = function (id) { return { id: id, status: "succeeded", server_live: false,
    started_at: 1000000, model: "m1", endpoint: "ai1", elapsed_s: 5, num_turns: 1,
    tool_calls: 0, errors: 0, compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null,
    last_activity: null }; };
  var JOB = "00000000-1111-2222-3333-444444444444";
  var LATE = new Date(2026, 9, 31, 23, 59, 50).getTime() / 1000;
  var MIDNIGHT = new Date(2026, 10, 1, 0, 0, 10).getTime() / 1000;
  var TEN_LATER = new Date(2026, 10, 1, 0, 0, 20).getTime() / 1000;
"""


def test_f2_a_push_crossing_local_midnight_redraws_the_relative_summary_once(tmp_path):
    """The witness of the review: identical rows (new objects) and a state naming 2026-10-31;
    the first draw at 23:59:50 reads "today", the second twenty seconds later (now 2026-11-01
    00:00:10 local) must NOT be skipped and must re-read the summary as "yesterday" (the defect
    skipped it and kept "today"); a third call ten seconds later, in the same local day, IS
    skipped (one redraw per local day, not one per push)."""
    got = _page_js(tmp_path, _F2_ROWS + """
  M.TABLE_STATE["t312cfold-f2"] = { sort: null, filters: { started: "2026-10-31" }, page: 1 };
  var el = M.jobsTable([mkJob(JOB)], { id: "t312cfold-f2", now: LATE });
  document.body.appendChild(el); // level 1 and 2 only persist a container that is in the document
  var pick = function () { return el.querySelector('button.filter-pick[data-filter="started"]'); };
  var atLate = pick().textContent;
  var beforeMidnight = M.renders.tableSkipped;
  var el2 = M.jobsTable([mkJob(JOB)], { id: "t312cfold-f2", now: MIDNIGHT });
  var afterMidnight = { text: pick().textContent, same: el2 === el,
    skipped: M.renders.tableSkipped - beforeMidnight };
  var beforeTen = M.renders.tableSkipped;
  var el3 = M.jobsTable([mkJob(JOB)], { id: "t312cfold-f2", now: TEN_LATER });
  var tenLater = { text: pick().textContent, same: el3 === el,
    skipped: M.renders.tableSkipped - beforeTen };
  return { atLate: atLate, afterMidnight: afterMidnight, tenLater: tenLater };
""")
    assert got["atLate"] == "today", got
    assert got["afterMidnight"]["same"] is True, got
    assert got["afterMidnight"]["skipped"] == 0, got  # the defect skipped it (skipped 1)
    assert got["afterMidnight"]["text"] == "yesterday", got  # the defect kept "today"
    assert got["tenLater"]["same"] is True, got
    assert got["tenLater"]["skipped"] == 1, got  # the same local day: still skipped
    assert got["tenLater"]["text"] == "yesterday", got


# --- F3: the events section forgets its body when the events go ---------------------------------

# The endpoint page's stub (tests/test_3_12b_fold.py's F1 shape): its five DOM ids, a snapshot,
# and a fetch that serves one history body (the test swaps `hist` between pushes).
_F3_IDS = """
  var ids = {};
  ["meta", "conn-label", "hosts", "hosts-hidden", "jobs-body"].forEach(function (id) {
    var d = document.createElement("div");
    d.setAttribute("id", id);
    document.body.appendChild(d);
    ids[id] = d;
  });
  document.getElementById = function (id) { return ids[id] || null; };
  var mkJob = function (id) { return { id: id, status: "succeeded", server_live: false,
    started_at: 1000000, model: "m1", endpoint: "ai1", elapsed_s: 5, num_turns: 1,
    tool_calls: 0, errors: 0, compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null,
    last_activity: null }; };
  var snapA = { now: 1000000, refresh_s: 5, window_s: 3600, hosts: [],
    jobs: [mkJob("00000000-1111-2222-3333-444444444444")] };
"""


def test_f3_the_events_section_rebuilds_its_body_when_the_events_return_after_empty(tmp_path):
    """The witness of the review: with events in range the section holds their body; the events
    go to [] and the section leaves the box (the accepted re-insert); the SAME events then
    return. The section element itself persists (3.12b fold F1's rule), but its `.table-wrap`
    must be a NEW element — the defect kept `cache.sig` at the last non-empty events and reused
    the old body, with whatever `when()` day-stamps it had at the time."""
    got = _page_js(tmp_path, _F3_IDS + """
  var EV1 = { t: 1000000, kind: "restart", detail: "collector restarted" };
  var hist = { t: [1000000], gen_tps: [10.5], state: ["idle"],
    jobs: [mkJob("00000000-1111-2222-3333-444444444444")], events: [EV1] };
  var reqsBody = { requests: [], totals: { count: 0, input_tokens: 0, output_tokens: 0,
    cache_read: 0, cache_creation: 0, ms: 0, errors: 0 } };
  global.fetch = function (url) {
    var body = String(url).indexOf("/requests") >= 0 ? reqsBody : hist;
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(body); } });
  };
  var container = document.createElement("div");
  document.body.appendChild(container);
  var teardown = M.pages.endpoint(container, "ai1", { snap: snapA, range: "1h" });
  var realNow = Date.now;
  var step = 0;
  var push = function () {
    step++;
    Date.now = function () { return realNow() + step * 60000; };
    M.render(snapA); // the hook re-fetches and draws the tables again
  };
  return new Promise(function (resolve) {
    setTimeout(function () {
      var sec1 = container.querySelector("section.events");
      var wrap1 = sec1 && sec1.querySelector(".table-wrap");
      var first = { section: !!sec1,
        rows: sec1 ? sec1.querySelectorAll("tbody tr").length : null };
      hist = Object.assign({}, hist, { events: [] }); // the events go
      push();
      setTimeout(function () {
        var none = { section: !!container.querySelector("section.events"),
          sections: container.querySelectorAll("section").length };
        hist = Object.assign({}, hist, { events: [EV1] }); // ... and the SAME events return
        push();
        setTimeout(function () {
          var sec3 = container.querySelector("section.events");
          var out = { first: first, none: none,
            sectionSame: sec3 === sec1,
            wrapNew: !!sec3 && sec3.querySelector(".table-wrap") !== wrap1,
            rows: sec3 ? sec3.querySelectorAll("tbody tr").length : null };
          teardown();
          resolve(out);
        }, 60);
      }, 60);
    }, 60);
  });
""")
    assert got["first"]["section"] is True and got["first"]["rows"] == 1, got
    assert got["none"]["section"] is False and got["none"]["sections"] == 2, got
    assert got["sectionSame"] is True, got  # the section element persists for the return
    assert got["wrapNew"] is True, got  # the defect reused the old body (wrapNew false)
    assert got["rows"] == 1, got
