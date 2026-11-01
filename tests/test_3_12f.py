"""Story 3.12f: the started picker's hour bounds work without a date (docs/EPICS.md 3.12f, the
operator's go 2026-10-31). The picker spec §5 said the hour selects are "disabled until a date
is chosen" and 3.12c built that faithfully; the operator's hands on the live page showed the
rule wrong — "when the date is not selected ("any") none of the other parts function". The rule
changes: the §3 state grammar gains a THIRD shape, "HH-HH" — every day, hours inclusive over
whole hours — and the hour selects are never disabled. The two existing shapes keep their
exact meaning; an empty string and any other shape still match nothing.

F1 pins the match rule at the new shape's bounds over two different days and the old shapes
unchanged; F2 the panel with the date "any" (from/to enabled, the hours of every faceted day,
the list narrowed by a status pick); F3 the writes (the one-handle edges without a date, the
date/hour combinations each way); F4 the summary ("09 to 11" alone, the `set` class) and the
panel reading the dayless state back.

The behavioural tests run the page scripts under node over tests/fakedom.js, the loader pattern
from tests/test_3_12c.py (copied, never imported: ruling (o), one test file per story).
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


# --- F1: the state string and the match rule (3.12f R1) ---------------------------------------------

# Hand rows whose keys are built by hand from LOCAL components (never a parsed string): the
# 09-11 window's bounds on two DIFFERENT local days (the dayless shape matches every day), the
# seconds just outside them on both days, a mid-window row, and a null that never matches.
_TIME_ROWS = """
  var day = function (y, mo, d, h, mi, s) { return new Date(y, mo, d, h, mi, s).getTime() / 1000; };
  var rows = [
    { v: day(2026, 9, 31, 9, 0, 0) },    // day 1: the window's first included instant
    { v: day(2026, 9, 31, 8, 59, 59) },  // day 1: one second before it
    { v: day(2026, 9, 31, 11, 59, 59) }, // day 1: the window's last included instant
    { v: day(2026, 9, 31, 12, 0, 0) },   // day 1: one second after it
    { v: day(2026, 9, 30, 9, 0, 0) },    // day 2: the window's first included instant
    { v: day(2026, 9, 30, 11, 59, 59) }, // day 2: the window's last included instant
    { v: day(2026, 9, 30, 8, 59, 59) },  // day 2: one second before it
    { v: day(2026, 10, 1, 10, 0, 0) },   // day 3: mid-window, a third day
    { v: null },                         // a null key never matches
  ];
  var cols = [{ name: "started", filter: "time", unit: null, title: null,
    cell: function (r) { return M.h("td", { text: r.v == null ? "" : M.fmt.time(r.v) }); },
    sortKey: function (r) { return r.v; } }];
  // draw with a state string written straight into TABLE_STATE (the state is a string; the
  // panel is its only writer, but the smoke and a hand-set view can still write one)
  var countFor = function (id, state) {
    M.TABLE_STATE[id] = { sort: null, filters: state === null ? {} : { started: state }, page: 1 };
    var el = M.dataTable(id, cols, rows, {});
    return el.querySelectorAll("tbody tr").length;
  };
"""


def test_the_dayless_hour_window_spans_every_day_at_its_bounds(tmp_path):
    """3.12f R1: "HH-HH" matches every local day, hours inclusive over whole hours — 09:00:00
    and 11:59:59 in on TWO different days, 08:59:59 and 12:00:00 out on both, a third day's
    mid-window row in; "11-09" (from > to) matches nothing; "9-11" (not two digits) matches
    nothing; the two old shapes are unchanged ("2026-10-31 09-11" still its three day-1 rows,
    "2026-10-31" still the whole day)."""
    got = _page_js(tmp_path, _TIME_ROWS + """
  return {
    dayless: countFor("t312f-dayless", "09-11"),
    daylessRev: countFor("t312f-dayless-rev", "11-09"),
    oneDigit: countFor("t312f-onedigit", "9-11"),
    dayWindow: countFor("t312f-daywin", "2026-10-31 09-11"),
    dayAlone: countFor("t312f-dayalone", "2026-10-31"),
  };
""")
    # day 1: 09:00:00 and 11:59:59 (not 08:59:59, not 12:00:00); day 2: the same two; day 3:
    # the mid-window 10:00:00 — five
    assert got["dayless"] == 5, got
    assert got["daylessRev"] == 0, got  # from > to matches nothing, as a number range does
    assert got["oneDigit"] == 0, got  # two digits each is the shape
    # the old shapes, unchanged: day 1's two in-window rows (the mid-window row sits on day 3),
    # day 1's four whole-day rows
    assert got["dayWindow"] == 2, got
    assert got["dayAlone"] == 4, got  # the four Oct 31 rows (two bound rows, 12:00, 08:59:59)


def test_shapes_outside_the_grammar_still_match_nothing(tmp_path):
    """3.12f R1: the third shape joins the grammar without loosening it — the half-written
    forms ("09-", "-11", "0 9-11") and the old typed literals stay outside and match nothing;
    the empty string is deleted by setFilter as today (F3 witnesses the deletion), and a
    hand-written "" is skipped upstream by filterIdx's falsy check, so the grammar itself is
    witnessed by the shapes above, never by "" falling through to a substring hunt."""
    got = _page_js(tmp_path, _TIME_ROWS + """
  return {
    fromHalf: countFor("t312f-fromhalf", "09-"),
    toHalf: countFor("t312f-tohalf", "-11"),
    loose: countFor("t312f-loose", "9 - 11"),
    oldGrammar: countFor("t312f-old", "11:00-11:30"),
  };
""")
    assert got["fromHalf"] == 0, got
    assert got["toHalf"] == 0, got
    assert got["loose"] == 0, got
    assert got["oldGrammar"] == 0, got


# --- F2-F4: the panel, the writes, the summary (3.12f R2-R4) -----------------------------------------

_JOBS = """
  var mk = function (id, turns) { return { id: id, status: "succeeded", server_live: false,
    started_at: 1000000, model: "m1", endpoint: "ai1", elapsed_s: 5, num_turns: turns,
    tool_calls: 0, errors: 0, compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null,
    last_activity: null }; };
  // NOW's local day and its yesterday, built from components the same way the summary judges
  // them: the state holds the ABSOLUTE date the label stands for (spec §3)
  var NOW = new Date(2026, 9, 31, 15, 0, 0).getTime() / 1000;
  var dayStr = function (d) {
    var mo = d.getMonth() + 1, day = d.getDate();
    return d.getFullYear() + "-" + (mo < 10 ? "0" + mo : mo) + "-" + (day < 10 ? "0" + day : day);
  };
  var TODAY = dayStr(new Date(NOW * 1000));
  var pick = function (el) { return el.querySelector('button.filter-pick[data-filter="started"]'); };
"""

# Hand rows over three local days, copied from tests/test_3_12c.py's _PANEL_ROWS: today carries
# hours 09/10/11 with status x, yesterday hours 09/10 with status y only, an older date one row
# with status x at hour 14. The all-days hour list without a date is 09/10/11/14; a status "y"
# pick facets it to 09/10.
_PANEL_ROWS = """
  var mk2 = function (n, status, at) { var j = mk("0000000" + n + "-1111-2222-3333-444444444444", n);
    j.status = status; j.started_at = at; return j; };
  var d3 = function (y, mo, dd, h) { return new Date(y, mo, dd, h, 7, 0).getTime() / 1000; };
  var jobs3 = [
    mk2(0, "x", d3(2026, 9, 31, 9)), mk2(1, "x", d3(2026, 9, 31, 10)), mk2(2, "x", d3(2026, 9, 31, 11)),
    mk2(3, "y", d3(2026, 9, 30, 9)), mk2(4, "y", d3(2026, 9, 30, 10)),
    mk2(5, "x", d3(2026, 9, 28, 14)),
  ];
  var el = M.jobsTable(jobs3, { id: "t312f-panel", now: NOW });
  document.body.appendChild(el);
  var btn = pick(el);
  btn.click(); // open
  var panel = function () { return el.querySelector(".filter-panel"); };
  var dateSel = function () { return panel().querySelector("select.fp-date"); };
  var fromSel = function () { return panel().querySelector("select.fp-from"); };
  var toSel = function () { return panel().querySelector("select.fp-to"); };
  var labels = function (sel) { return sel.children.map(function (o) { return o.textContent; }); };
  var setOpt = function (sel, v) { sel.value = v; sel.dispatchEvent(new Event("change")); };
  var state = function () { return M.TABLE_STATE["t312f-panel"].filters.started; };
"""


def test_the_hour_selects_stand_enabled_with_no_date_and_list_every_faceted_day(tmp_path):
    """3.12f R2: with the date "any" the hour selects carry no `disabled` and list "any" plus
    the hours present among the rows the OTHER columns' filters leave on EVERY day — the
    fixture's three days give 09/10/11/14 (from "09:00" labels, to "09:59"); a status pick of
    "y" (yesterday's two rows, hours 09/10) narrows the list to 09/10."""
    got = _page_js(tmp_path, _JOBS + _PANEL_ROWS + """
  var before = { fromDisabled: fromSel().hasAttribute("disabled"),
    toDisabled: toSel().hasAttribute("disabled"),
    fromLabels: labels(fromSel()), toLabels: labels(toSel()),
    fromValue: fromSel().value, toValue: toSel().value };
  setOpt(el.querySelector('select[data-filter="status"]'), "y");
  var faceted = { fromLabels: labels(fromSel()), toLabels: labels(toSel()),
    fromDisabled: fromSel().hasAttribute("disabled") };
  return { before: before, faceted: faceted };
""")
    before = got["before"]
    assert before["fromDisabled"] is False and before["toDisabled"] is False, got
    assert before["fromLabels"] == ["any", "09:00", "10:00", "11:00", "14:00"], before
    assert before["toLabels"] == ["any", "09:59", "10:59", "11:59", "14:59"], before
    assert before["fromValue"] == "" and before["toValue"] == "", before
    faceted = got["faceted"]
    assert faceted["fromDisabled"] is False, faceted
    assert faceted["fromLabels"] == ["any", "09:00", "10:00"], faceted
    assert faceted["toLabels"] == ["any", "09:59", "10:59"], faceted


def test_the_writes_compose_the_dayless_shape_and_the_date_edges_hold(tmp_path):
    """3.12f R3, no date chosen: from 09 alone writes "09-23" (the day's end edge, the
    one-handle rule without a day); to 11 completes "09-11"; choosing TODAY keeps the hours
    ("2026-10-31 09-11"); choosing "any" again keeps them ("09-11" — the operator: no
    limiting); then the hours go back to "any": clearing from alone lands on the to-alone
    edge "00-11" (and the read-back materializes "00" into the select, 3.12c's rule kept —
    the same behaviour the dated shape has had since 3.12c), and with BOTH selects at "any"
    the state empties and the key goes (R3's last clause)."""
    got = _page_js(tmp_path, _JOBS + _PANEL_ROWS + """
  setOpt(fromSel(), "09");
  var fromAlone = state();
  setOpt(toSel(), "11");
  var both = state();
  setOpt(dateSel(), TODAY);
  var withDate = state();
  setOpt(dateSel(), "");
  var daylessAgain = state();
  setOpt(fromSel(), "");
  var toAloneEdge = state();
  var reread = { from: fromSel().value, to: toSel().value };
  // both selects at "any", one change: the state empties, setFilter deletes the key
  fromSel().value = "";
  setOpt(toSel(), "");
  var keyGone = ("started" in M.TABLE_STATE["t312f-panel"].filters);
  var afterGone = { from: fromSel().value, to: toSel().value, date: dateSel().value };
  return { fromAlone: fromAlone, both: both, withDate: withDate, daylessAgain: daylessAgain,
    toAloneEdge: toAloneEdge, reread: reread, keyGone: keyGone, afterGone: afterGone };
""")
    assert got["fromAlone"] == "09-23", got  # from alone: the day's end, even with no day
    assert got["both"] == "09-11", got
    assert got["withDate"] == "2026-10-31 09-11", got  # the date joins, the hours stay
    assert got["daylessAgain"] == "09-11", got  # "any" after a date keeps the hours
    assert got["toAloneEdge"] == "00-11", got  # clearing from lands on the to-alone edge
    assert got["reread"] == {"from": "00", "to": "11"}, got  # the read-back materializes it
    assert got["keyGone"] is False, got  # both "any": the state empties, setFilter deletes
    assert got["afterGone"] == {"from": "", "to": "", "date": ""}, got  # the panel stands by


def test_the_dayless_state_summarizes_alone_and_rereads_into_the_panel(tmp_path):
    """3.12f R4: the state "09-11" makes the button read "09 to 11" — no day word, no leading
    space — with the `set` class; reopening the panel reads the dayless state back: date "any",
    from "09", to "11", and the held bounds sit in the option list."""
    got = _page_js(tmp_path, _JOBS + """
  var rows = [
    { id: "00000000-1111-2222-3333-444444444444", status: "x", started_at: new Date(2026, 9, 31, 9, 7, 0).getTime() / 1000 },
    { id: "00000001-1111-2222-3333-444444444444", status: "x", started_at: new Date(2026, 9, 28, 11, 7, 0).getTime() / 1000 },
  ];
  M.TABLE_STATE["t312f-sum"] = { sort: null, filters: { started: "09-11" }, page: 1 };
  var el = M.jobsTable(rows, { id: "t312f-sum", now: NOW });
  document.body.appendChild(el);
  var b = pick(el);
  var summary = { text: b.textContent, set: b.classList.contains("set") };
  b.click(); // reopen: the panel reads the dayless state back
  var ds = el.querySelector(".filter-panel select.fp-date");
  var fs = el.querySelector(".filter-panel select.fp-from");
  var ts = el.querySelector(".filter-panel select.fp-to");
  var labels = function (sel) { return sel.children.map(function (o) { return o.textContent; }); };
  return { summary: summary, matched: el.querySelectorAll("tbody tr").length,
    date: ds.value, dateLabels: labels(ds),
    from: fs.value, fromLabels: labels(fs), to: ts.value,
    toDisabled: ts.hasAttribute("disabled") };
""")
    assert got["summary"] == {"text": "09 to 11", "set": True}, got["summary"]
    assert got["matched"] == 2, got  # the dayless window matches both days' rows
    assert got["date"] == "", got
    assert got["from"] == "09" and got["fromLabels"] == ["any", "09:00", "11:00"], got
    assert got["to"] == "11", got
    assert got["toDisabled"] is False, got


def test_a_held_bound_the_day_lacks_stays_selectable_without_a_date(tmp_path):
    """3.12c's rule kept: a held bound the faceted rows no longer carry stays selectable —
    witness it without a date: pick status "y" (yesterday's hours 09/10) while the dayless
    state "09-14" holds, and "14" stays listed (the state is the truth)."""
    got = _page_js(tmp_path, _JOBS + _PANEL_ROWS + """
  setOpt(fromSel(), "09");
  setOpt(toSel(), "14"); // "14" is the older day's hour; the list has it with no date
  setOpt(el.querySelector('select[data-filter="status"]'), "y"); // facets to yesterday: 09/10
  return { state: state(), fromLabels: labels(fromSel()), toLabels: labels(toSel()) };
""")
    assert got["state"] == "09-14", got
    assert got["fromLabels"] == ["any", "09:00", "10:00", "14:00"], got
    assert got["toLabels"] == ["any", "09:59", "10:59", "14:59"], got
