"""Story 3.12c: the started picker (spec docs/superpowers/specs/2026-10-31-started-picker-
design.md, approved 2026-10-31: "I think if you make it too fine grained the picker gets too
long - maybe a two-level picker, the first being a date (today, yesterday, list of dates) + a
clock filter ... Digital would be two hours choices"). The typed time grammar of 3.12a §6 is
retired; the `time` kind's control becomes a button in the filter cell opening a panel of three
native selects, and the state string narrows to "YYYY-MM-DD" or "YYYY-MM-DD HH-HH".

C1 pins the state string and the match rule: the day alone is the whole local day; the hour
bounds are inclusive over whole hours (09-11 is 09:00:00 through 11:59:59); from > to matches
nothing; a null key never matches; any other shape in the state matches nothing. C2 pins the
button and its summary; C3 the panel: its options, its writes, its close paths, and its survival
across a push.

The behavioural tests run the page scripts under node over tests/fakedom.js, the loader pattern
from tests/test_3_12b.py (rebuilt here rather than importing another test module: ruling (o),
one test file per story).
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


# --- C1: the state string and the match rule (spec §3) ---------------------------------------------

# Hand rows whose keys are built by hand from LOCAL components (never a parsed string): four
# instants on Oct 31 2026 local straddling the 09-11 window's bounds, one on the day before, one
# on the day after, and a null that never matches.
_TIME_ROWS = """
  var day = function (y, mo, d, h, mi, s) { return new Date(y, mo, d, h, mi, s).getTime() / 1000; };
  var rows = [
    { v: day(2026, 9, 31, 9, 0, 0) },    // the window's first included instant
    { v: day(2026, 9, 31, 8, 59, 59) },  // one second before it
    { v: day(2026, 9, 31, 11, 59, 59) }, // the window's last included instant
    { v: day(2026, 9, 31, 12, 0, 0) },   // one second after it
    { v: day(2026, 9, 30, 10, 0, 0) },   // the previous local day
    { v: day(2026, 9, 31, 10, 0, 0) },   // mid-window, same day
    { v: day(2026, 10, 1, 10, 0, 0) },   // the next local day
    { v: null },                         // a null key never matches
  ];
  var cols = [{ name: "started", filter: "time", unit: null, title: null,
    cell: function (r) { return M.h("td", { text: r.v == null ? "" : M.fmt.time(r.v) }); },
    sortKey: function (r) { return r.v; } }];
  // draw with a state string written straight into TABLE_STATE (the smoke can set it too: the
  // state is a string, and the picker is now its only WRITER, not its only reader)
  var countFor = function (id, state) {
    M.TABLE_STATE[id] = { sort: null, filters: state === null ? {} : { started: state }, page: 1 };
    var el = M.dataTable(id, cols, rows, {});
    return el.querySelectorAll("tbody tr").length;
  };
"""


def test_the_hour_window_is_inclusive_over_whole_hours_and_the_day_alone_takes_the_day(tmp_path):
    """§3: "YYYY-MM-DD 09-11" keeps 09:00:00 through 11:59:59 (09:00:00 in, 08:59:59 out,
    11:59:59 in, 12:00:00 out); "YYYY-MM-DD" alone takes the whole local day; from > to matches
    nothing; a null key never matches; a second day's rows are excluded whichever way the state
    reads."""
    got = _page_js(tmp_path, _TIME_ROWS + """
  return {
    window: countFor("t312c-win", "2026-10-31 09-11"),
    dayAlone: countFor("t312c-day", "2026-10-31"),
    fromAfterTo: countFor("t312c-rev", "2026-10-31 11-09"),
    prevDay: countFor("t312c-prev", "2026-10-30"),
    nextDay: countFor("t312c-next", "2026-11-01"),
    prevDayWindow: countFor("t312c-prevw", "2026-10-30 00-23"),
  };
""")
    # 09:00:00 and 10:00:00 and 11:59:59 — three; 08:59:59 and 12:00:00 out, other days out,
    # the null never
    assert got["window"] == 3, got
    assert got["dayAlone"] == 5, got  # the whole Oct 31: the four straddlers plus the mid one
    assert got["fromAfterTo"] == 0, got  # from > to matches nothing, as a number range does
    assert got["prevDay"] == 1 and got["nextDay"] == 1, got  # second days excluded by the day
    assert got["prevDayWindow"] == 1, got  # and by the window

def test_any_other_shape_in_the_state_matches_nothing(tmp_path):
    """The typed grammar is retired: the state is still a string the smoke (or a hand-edited
    view) can set, but anything that is not "YYYY-MM-DD" or "YYYY-MM-DD HH-HH" — the old
    "11:00-11:30", a bare word, a half-typed date — matches nothing rather than falling through
    to a substring hunt (3.12a §6's reading 3 goes with the box)."""
    got = _page_js(tmp_path, _TIME_ROWS + """
  return {
    oldGrammar: countFor("t312c-old", "11:00-11:30"),
    oldLitRange: countFor("t312c-litrange", "2026-10-30 - 2026-10-31"),
    word: countFor("t312c-word", "10/31"),
    halfDate: countFor("t312c-half", "2026-10"),
    oneHourBound: countFor("t312c-partial", "2026-10-31 09-"),
  };
""")
    assert got["oldGrammar"] == 0, got
    assert got["oldLitRange"] == 0, got  # the 3.12a "a - b" reading stops matching
    assert got["word"] == 0, got
    assert got["halfDate"] == 0, got
    assert got["oneHourBound"] == 0, got


# --- C2: the control in the cell (spec §4) ---------------------------------------------------------

_JOBS = """
  var mk = function (id, turns) { return { id: id, status: "succeeded", server_live: false,
    started_at: 1000000, model: "m1", endpoint: "ai1", elapsed_s: 5, num_turns: turns,
    tool_calls: 0, errors: 0, compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null,
    last_activity: null }; };
  var jobs = [mk("00000000-1111-2222-3333-444444444444", 1),
              mk("00000001-1111-2222-3333-444444444444", 2)];
  // NOW's local day and its yesterday, built from components the same way the summary judges
  // them: the state holds the ABSOLUTE date the label stands for (spec §3)
  var NOW = new Date(2026, 9, 31, 15, 0, 0).getTime() / 1000;
  var dayStr = function (d) {
    var mo = d.getMonth() + 1, day = d.getDate();
    return d.getFullYear() + "-" + (mo < 10 ? "0" + mo : mo) + "-" + (day < 10 ? "0" + day : day);
  };
  var TODAY = dayStr(new Date(NOW * 1000));
  var YESTERDAY = dayStr(new Date(2026, 9, 30));
  var OLDER = "2026-10-28";
  var fmtDay = function (dstr) { var p = dstr.split("-");
    return new Date(+p[0], +p[1] - 1, +p[2]).toLocaleDateString(); };
  var pick = function (el) { return el.querySelector('button.filter-pick[data-filter="started"]'); };
"""


def test_the_time_column_draws_a_filter_pick_button_and_no_input(tmp_path):
    """§4: the time kind's control is a `<button type="button" class="filter-pick"
    data-filter data-filter-kind="time">` in the filter cell — no input — and the requests
    table's "time" column draws the same control. Unset it reads the column name (what the
    box's placeholder did)."""
    got = _page_js(tmp_path, _JOBS + """
  var el = M.jobsTable(jobs, { id: "t312c-btn-a", now: NOW });
  document.body.appendChild(el);
  var cell = el.querySelector('.table-filters th[data-filter-kind="time"]');
  var req = { t: 1000, path: "/v1/messages", http_status: 200, error: null, ms: 100,
    job_id: null, tokens: { input: 1, output: 1, cache_read: 0, cache_creation: 0 } };
  var reqEl = M.requestsTable([req], { now: NOW, id: "t312c-btn-req" });
  var reqBtn = reqEl.querySelector('button.filter-pick[data-filter="time"]');
  return {
    tag: cell.children[0] ? cell.children[0].tagName : null,
    cls: cell.children[0] ? cell.children[0].className : null,
    type: cell.children[0] ? cell.children[0].getAttribute("type") : null,
    kind: cell.children[0] ? cell.children[0].getAttribute("data-filter-kind") : null,
    noInput: cell.querySelectorAll("input").length,
    text: pick(el) ? pick(el).textContent : null,
    setClass: pick(el) ? pick(el).classList.contains("set") : null,
    reqBtn: reqBtn ? reqBtn.textContent : null,
  };
""")
    assert got["tag"] == "BUTTON" and got["type"] == "button", got
    assert got["cls"] == "filter-pick" and got["kind"] == "time", got
    assert got["noInput"] == 0, got
    assert got["text"] == "started", got
    assert got["setClass"] is False, got
    assert got["reqBtn"] == "time", got  # the requests table's time column: the same control


def test_the_button_reads_the_summary_of_its_state(tmp_path):
    """The summary is one function's output, read at build and on every draw: unset the column
    name; set "today" / "yesterday" (judged against opts.now's local day — the clock the cells
    use) or the date as the cells' own short date (toLocaleDateString), then " 09 to 11" when
    hours are bound; the `set` class rides with a set state (ink, not muted)."""
    got = _page_js(tmp_path, _JOBS + """
  var draws = function (id, state) {
    M.TABLE_STATE[id] = { sort: null, filters: state === null ? {} : { started: state }, page: 1 };
    var el = M.jobsTable(jobs, { id: id, now: NOW });
    document.body.appendChild(el);
    var b = pick(el);
    return { text: b.textContent, set: b.classList.contains("set") };
  };
  return {
    unset: draws("t312c-sum-a", null),
    todayHours: draws("t312c-sum-b", TODAY + " 09-11"),
    todayAlone: draws("t312c-sum-c", TODAY),
    yesterday: draws("t312c-sum-d", YESTERDAY + " 13-14"),
    olderDate: draws("t312c-sum-e", OLDER),
    olderLabel: fmtDay(OLDER),
  };
""")
    assert got["unset"] == {"text": "started", "set": False}, got["unset"]
    assert got["todayHours"] == {"text": "today 09 to 11", "set": True}, got["todayHours"]
    assert got["todayAlone"] == {"text": "today", "set": True}, got["todayAlone"]
    assert got["yesterday"] == {"text": "yesterday 13 to 14", "set": True}, got["yesterday"]
    assert got["olderDate"] == {"text": got["olderLabel"], "set": True}, got["olderDate"]


def test_a_push_keeps_the_same_button_and_rereads_its_text(tmp_path):
    """3.12b's patch meets the picker: the button element persists across a dataTable call with
    new rows (the summary is re-read by patchFilterRow every draw, not pinned at build) — set a
    state after the build and the push shows it on the SAME element."""
    got = _page_js(tmp_path, _JOBS + """
  var el = M.jobsTable(jobs, { id: "t312c-btn-push", now: NOW });
  document.body.appendChild(el);
  var btn = pick(el);
  M.TABLE_STATE["t312c-btn-push"].filters.started = TODAY + " 09-11";
  var moved = jobs.map(function (j) { var c = JSON.parse(JSON.stringify(j));
    c.num_turns += 1; return c; }); // a hash change: level 2, the row is patched
  var el2 = M.jobsTable(moved, { id: "t312c-btn-push", now: NOW });
  var after = pick(el);
  return {
    sameContainer: el2 === el,
    sameBtn: after === btn,
    text: after.textContent,
    set: after.classList.contains("set"),
  };
""")
    assert got["sameContainer"] is True and got["sameBtn"] is True, got
    assert got["text"] == "today 09 to 11" and got["set"] is True, got


def test_a_panel_opened_after_a_push_fills_from_the_current_draw(tmp_path):
    """The button persists across pushes, so its click reads the CURRENT call's toggle
    through the TABLE_DOM slot (3.12b B2 (a)'s rule): a captured closure would fill the panel
    from the BUILDING call's rows — the smoke caught "today (2)" (the first draw's two seeded
    jobs) on a table showing 120. Build small, push bigger, then open."""
    got = _page_js(tmp_path, _JOBS + """
  var mk3 = function (n, at) { var j = mk("0000000" + n + "-1111-2222-3333-444444444444", n);
    j.started_at = at; return j; };
  var two = [mk3(0, new Date(2026, 9, 31, 9, 0, 0).getTime() / 1000),
             mk3(1, new Date(2026, 9, 31, 10, 0, 0).getTime() / 1000)];
  var el = M.jobsTable(two, { id: "t312c-slot", now: NOW });
  document.body.appendChild(el);
  var five = two.concat([
    mk3(2, new Date(2026, 9, 31, 11, 0, 0).getTime() / 1000),
    mk3(3, new Date(2026, 9, 31, 12, 0, 0).getTime() / 1000),
    mk3(4, new Date(2026, 9, 31, 13, 0, 0).getTime() / 1000)]);
  M.jobsTable(five, { id: "t312c-slot", now: NOW }); // level-2 push: the button element persists
  var btn = el.querySelector('button.filter-pick[data-filter="started"]');
  btn.click(); // opened against the FIVE-row draw, not the building call's two
  var dateSel = el.querySelector(".filter-panel select.fp-date");
  return { dateLabels: dateSel.children.map(function (o) { return o.textContent; }),
    fromLabels: el.querySelector(".filter-panel select.fp-from").children
      .map(function (o) { return o.getAttribute("value"); }) };
""")
    assert got["dateLabels"] == ["any", "today (5)"], got["dateLabels"]


# --- C3: the panel (spec §5) ------------------------------------------------------------------------

# Hand rows over three local days: today carries hours 09/10/11 and both statuses, yesterday
# hours 09/10 with status y only, an older date one row with status x. NOW (2026-10-31 15:00)
# and the day strings come from _JOBS, built from the same local components the code uses.
_PANEL_ROWS = """
  var mk2 = function (n, status, at) { var j = mk("0000000" + n + "-1111-2222-3333-444444444444", n);
    j.status = status; j.started_at = at; return j; };
  var d3 = function (y, mo, dd, h) { return new Date(y, mo, dd, h, 7, 0).getTime() / 1000; };
  var jobs3 = [
    mk2(0, "x", d3(2026, 9, 31, 9)), mk2(1, "x", d3(2026, 9, 31, 10)), mk2(2, "x", d3(2026, 9, 31, 11)),
    mk2(3, "y", d3(2026, 9, 30, 9)), mk2(4, "y", d3(2026, 9, 30, 10)),
    mk2(5, "x", d3(2026, 9, 28, 14)),
  ];
  var el = M.jobsTable(jobs3, { id: "t312c-panel", now: NOW });
  document.body.appendChild(el);
  var btn = pick(el);
  btn.click(); // open
  var panel = function () { return el.querySelector(".filter-panel"); };
  var dateSel = function () { return panel().querySelector("select.fp-date"); };
  var fromSel = function () { return panel().querySelector("select.fp-from"); };
  var toSel = function () { return panel().querySelector("select.fp-to"); };
  var labels = function (sel) { return sel.children.map(function (o) { return o.textContent; }); };
  var setOpt = function (sel, v) { sel.value = v; sel.dispatchEvent(new Event("change")); };
  var state = function () { return M.TABLE_STATE["t312c-panel"].filters.started; };
  var btnEl = function () { return el.querySelector('button.filter-pick[data-filter="started"]'); };
  var panelBtn = function (name) {
    return Array.prototype.filter.call(panel().querySelectorAll(".pager-btn"),
      function (b) { return b.textContent === name; })[0];
  };
"""


def test_the_panel_is_a_child_of_the_block_and_never_of_the_wrap(tmp_path):
    """§5: the panel is appended to the .table-block (position: relative), NOT inside
    .table-wrap — the wrapper's overflow-x: auto would clip an absolutely positioned child
    or scroll it. Its three selects and two pager-btns are present; a second click on the
    control closes it (and the document listeners come off)."""
    got = _page_js(tmp_path, _JOBS + _PANEL_ROWS + """
  var p = panel();
  var out = {
    parentCls: p.parentNode.className,
    isBlock: p.parentNode === el,
    inWrap: !!el.querySelector(".table-wrap .filter-panel"),
    selects: labels(dateSel()).length > 0 && !!fromSel() && !!toSel(),
    btns: Array.prototype.map.call(p.querySelectorAll(".pager-btn"), function (b) { return b.textContent; }),
    docKey: (document.listeners.keydown || []).length,
    docDown: (document.listeners.mousedown || []).length,
  };
  btnEl().click(); // a second click closes
  out.afterSecondClick = {
    panel: el.querySelector(".filter-panel"),
    docKey: (document.listeners.keydown || []).length,
    docDown: (document.listeners.mousedown || []).length,
  };
  return out;
""")
    assert got["parentCls"] == "table-block" and got["isBlock"], got
    assert got["inWrap"] is False, got
    assert got["selects"] is True, got
    assert got["btns"] == ["clear", "done"], got
    assert got["docKey"] == 1 and got["docDown"] == 1, got
    assert got["afterSecondClick"]["panel"] is None, got
    assert got["afterSecondClick"]["docKey"] == 0 and got["afterSecondClick"]["docDown"] == 0, got


def test_the_date_options_are_today_yesterday_then_older_newest_first_with_faceted_counts(tmp_path):
    """§5: "any", then "today (n)" (title the real date), "yesterday (n)", then each older date
    present newest first labelled with the cells' own short date and its count. DATES PRESENT
    are every row's day (§5's last paragraph, "the distinct local YYYY-MM-DD of every row's
    sort key"); the COUNTS facet over the rows the OTHER columns' filters leave — so filtering
    status to "x" reads "today (3)", "yesterday (0)" (present in the rows, faceted to zero:
    the "(0) is noise" sentence answers why today/yesterday are not unconditional options,
    not a call to drop a faceted zero — the pick keeps those), older "(1)". Selecting a date
    writes the state; a date a push drops survives "(0)", as the pick does."""
    got = _page_js(tmp_path, _JOBS + _PANEL_ROWS + """
  var plain = labels(dateSel());
  var todayTitle = dateSel().children[1].getAttribute("title");
  setOpt(el.querySelector('select[data-filter="status"]'), "x"); // the pick facets the dates
  var faceted = labels(dateSel());
  var facetedCount = el.querySelectorAll("tbody tr").length;
  setOpt(el.querySelector('select[data-filter="status"]'), "");
  setOpt(dateSel(), OLDER); // the 2026-10-28 row (status x): one row, one hour
  var olderHours = labels(fromSel());
  // now a push that drops the chosen date: the option survives "(0)", still selected
  var push3 = jobs3.slice(0, 5);
  M.jobsTable(push3, { id: "t312c-panel", now: NOW });
  var afterDrop = { labels: labels(dateSel()), value: dateSel().value,
    panelKept: el.querySelector(".filter-panel") === panel() };
  return { plain: plain, olderLabel: fmtDay(OLDER), todayTitle: todayTitle, faceted: faceted,
    facetedCount: facetedCount, state: state(), olderHours: olderHours, afterDrop: afterDrop };
""")
    # the older date's label is the CELLS' short date, read back through the same
    # toLocaleDateString the implementation uses (the locale is node's, whatever it is)
    assert got["plain"][:3] == ["any", "today (3)", "yesterday (2)"], got["plain"]
    assert got["plain"][3] == got["olderLabel"] + " (1)", got["plain"]
    assert got["todayTitle"] == "2026-10-31", got
    assert got["faceted"] == ["any", "today (3)", "yesterday (0)", got["olderLabel"] + " (1)"], got["faceted"]
    assert got["facetedCount"] == 4, got
    assert got["state"] == "2026-10-28", got
    assert got["olderHours"] == ["any", "14:00"], got["olderHours"]
    assert got["afterDrop"]["value"] == "2026-10-28", got["afterDrop"]
    assert got["olderLabel"] + " (0)" in got["afterDrop"]["labels"], got["afterDrop"]
    assert got["afterDrop"]["panelKept"] is True, got["afterDrop"]


def test_the_hour_selects_wait_for_a_date_and_list_that_dates_hours(tmp_path):
    """§5: from/to are disabled with no date chosen ("any" only); choosing today enables them
    and lists exactly today's hours — "09:00" labels for from, "09:59" for to, values the two
    digits; the writes compose the state: date alone "YYYY-MM-DD", both hours "YYYY-MM-DD
    HH-HH", from alone takes the day's end ("HH-23"), to alone its start ("00-HH")."""
    got = _page_js(tmp_path, _JOBS + _PANEL_ROWS + """
  var before = { fromDisabled: fromSel().hasAttribute("disabled"),
    toDisabled: toSel().hasAttribute("disabled"),
    fromLabels: labels(fromSel()), toLabels: labels(toSel()) };
  setOpt(dateSel(), TODAY);
  var afterDate = { state: state(),
    fromDisabled: fromSel().hasAttribute("disabled"),
    fromLabels: labels(fromSel()), toLabels: labels(toSel()) };
  setOpt(fromSel(), "09");
  var fromAlone = state();
  setOpt(toSel(), "11");
  var both = state();
  setOpt(fromSel(), "");
  var toAlone = state();
  return { before: before, afterDate: afterDate, fromAlone: fromAlone, both: both, toAlone: toAlone };
""")
    before = got["before"]
    assert before["fromDisabled"] is True and before["toDisabled"] is True, got
    assert before["fromLabels"] == ["any"] and before["toLabels"] == ["any"], got
    assert got["afterDate"]["state"] == "2026-10-31", got["afterDate"]
    assert got["afterDate"]["fromDisabled"] is False, got["afterDate"]
    assert got["afterDate"]["fromLabels"] == ["any", "09:00", "10:00", "11:00"], got["afterDate"]
    assert got["afterDate"]["toLabels"] == ["any", "09:59", "10:59", "11:59"], got["afterDate"]
    assert got["fromAlone"] == "2026-10-31 09-23", got  # from alone: the day's end
    assert got["both"] == "2026-10-31 09-11", got
    assert got["toAlone"] == "2026-10-31 00-11", got  # to alone: the day's start


def test_clear_empties_the_state_and_the_close_paths_hold(tmp_path):
    """§5: "clear" empties the state (the key goes, the panel re-reads itself and stands by);
    Escape removes the panel from the block and the document listeners with it (a second
    Escape throws nothing); an outside mousedown closes, a mousedown on the panel or the
    control does not; "done" closes."""
    got = _page_js(tmp_path, _JOBS + _PANEL_ROWS + """
  setOpt(dateSel(), TODAY);
  setOpt(fromSel(), "09");
  setOpt(toSel(), "11");
  var set = state();
  panelBtn("clear").click();
  var cleared = { state: state(), date: dateSel().value, fromDisabled: fromSel().hasAttribute("disabled"),
    open: !!panel(), btnSet: btnEl().classList.contains("set"), btnText: btnEl().textContent };
  var ev = new Event("mousedown"); ev.target = document.body;
  document.dispatchEvent(ev); // outside: close
  var afterDown = { panel: el.querySelector(".filter-panel"),
    docKey: (document.listeners.keydown || []).length };
  var ev2 = new Event("keydown"); ev2.key = "Escape";
  document.dispatchEvent(ev2); // already closed: throws nothing
  var secondEscapeThrew = false;
  try { document.dispatchEvent(ev2); } catch (e) { secondEscapeThrew = true; }
  btnEl().click(); // open again
  var ev3 = new Event("mousedown"); ev3.target = dateSel(); // inside the panel: stays open
  document.dispatchEvent(ev3);
  var stillOpen = !!panel();
  var ev4 = new Event("mousedown"); ev4.target = btnEl(); // on the control: stays open
  document.dispatchEvent(ev4);
  var openOnCtrl = !!panel();
  panelBtn("done").click();
  var afterDone = { panel: el.querySelector(".filter-panel"),
    docKey: (document.listeners.keydown || []).length,
    docDown: (document.listeners.mousedown || []).length };
  return { set: set, cleared: cleared, afterDown: afterDown, secondEscapeThrew: secondEscapeThrew,
    stillOpen: stillOpen, openOnCtrl: openOnCtrl, afterDone: afterDone };
""")
    assert got["set"] == "2026-10-31 09-11", got
    # JSON drops an undefined property: its absence IS the deletion
    assert "state" not in got["cleared"] and got["cleared"]["date"] == "", got["cleared"]
    assert got["cleared"]["fromDisabled"] is True, got["cleared"]
    assert got["cleared"]["open"] is True, got["cleared"]  # clear does not close the panel
    assert got["cleared"]["btnSet"] is False and got["cleared"]["btnText"] == "started", got["cleared"]
    assert got["afterDown"]["panel"] is None and got["afterDown"]["docKey"] == 0, got["afterDown"]
    assert got["secondEscapeThrew"] is False, got
    assert got["stillOpen"] is True and got["openOnCtrl"] is True, got
    assert got["afterDone"]["panel"] is None, got["afterDone"]
    assert got["afterDone"]["docKey"] == 0 and got["afterDone"]["docDown"] == 0, got["afterDone"]


def test_a_push_with_the_panel_open_keeps_it_and_refreshes_the_counts(tmp_path):
    """3.12b's rule meets the panel: a push while open never closes or replaces it — the panel
    element is the same, the date option's count moves, and the button's summary is re-read.
    The panel sits outside the filter row, so its own focusout listener feeds the same
    rec.onFocusOut slot a focused select was marked for."""
    got = _page_js(tmp_path, _JOBS + _PANEL_ROWS + """
  var opened = panel();
  var oneMore = JSON.parse(JSON.stringify(jobs3));
  oneMore.push(mk2(6, "x", d3(2026, 9, 31, 12))); // a fourth row today
  M.jobsTable(oneMore, { id: "t312c-panel", now: NOW });
  var still = el.querySelector(".filter-panel");
  var afterFirst = labels(dateSel()); // read NOW: the JSON snapshot would read the end state
  // focus rule: hold the date select open across a push, then focusout rebuilds it
  dateSel().focus();
  var oneMore2 = JSON.parse(JSON.stringify(oneMore));
  oneMore2.push(mk2(7, "x", d3(2026, 9, 31, 13))); // fifth today
  M.jobsTable(oneMore2, { id: "t312c-panel", now: NOW });
  var during = labels(dateSel()); // untouched: the operator holds the list open
  document.activeElement = document.body; // focus leaves (the browser fires focusout first)
  var fEv = new Event("focusout"); fEv.target = dateSel();
  panel().dispatchEvent(fEv);
  var afterFocusOut = labels(dateSel());
  return {
    samePanel: still === opened,
    counts: afterFirst,
    during: during,
    afterFocusOut: afterFocusOut,
  };
""")
    assert got["samePanel"] is True, got
    assert "today (4)" in got["counts"], got["counts"]
    assert "today (4)" in got["during"], got["during"]  # the focused select was NOT rebuilt twice
    assert "today (5)" in got["afterFocusOut"], got["afterFocusOut"]
