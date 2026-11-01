"""The 3.12f fold: the started picker's hour-edge read-back (the review of 3.12f, defect 1).
The panel's `change` writes the state, then the draw re-reads that state into the three selects.
The old read-back materialized a held bound into its select (`sel.value = held.from/held.to`), so
a from of "00" — which a from→any change WRITES (timePanelState: from alone is "00-HH") — came
back as "00" in the from select: the next to→any then composed "00-11" again and the key could
never go. The rule of the fold changes ONLY the read-back: "00" from and "23" to read back as
"any", because a single handle already means the day's edge ("00-HH", "HH-23"), so the edge hour
and "any" are the same bound and the state string is unchanged. The grammar and the writes do not
change.

R3's escape (both hours "any" empties the state) was true of `timePanelState` but unreachable
from the panel; F1/F2 witness it now (dayless and dated), F3 the one-bound rule on both edges (an
explicit 00:00 from / 23:59 to pick writes the same state it always did and reads back "any"), F4
that every other hour still reads back as itself.

The behavioural tests run the page scripts under node over tests/fakedom.js, the loader pattern
copied from tests/test_3_12f.py (never imported: ruling (o), one test file per story).
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


# --- the fixture: tests/test_3_12f.py's _JOBS + _PANEL_ROWS, copied (never imported) ------------

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

# Hand rows over three local days (tests/test_3_12f.py's _PANEL_ROWS), plus the fold's two edge
# rows on the older day: hour 00 and hour 23, so the explicit 00:00 / 23:59 picks F3 makes are
# real options in the hour lists (a browser refuses a value the list does not carry). TODAY's
# hours stay 09/10/11 and yesterday's 09/10 — the lists the copied fixture asserts are untouched.
_PANEL_ROWS = """
  var mk2 = function (n, status, at) { var j = mk("0000000" + n + "-1111-2222-3333-444444444444", n);
    j.status = status; j.started_at = at; return j; };
  var d3 = function (y, mo, dd, h) { return new Date(y, mo, dd, h, 7, 0).getTime() / 1000; };
  var jobs3 = [
    mk2(0, "x", d3(2026, 9, 31, 9)), mk2(1, "x", d3(2026, 9, 31, 10)), mk2(2, "x", d3(2026, 9, 31, 11)),
    mk2(3, "y", d3(2026, 9, 30, 9)), mk2(4, "y", d3(2026, 9, 30, 10)),
    mk2(5, "x", d3(2026, 9, 28, 14)),
    mk2(6, "x", d3(2026, 9, 28, 0)), mk2(7, "x", d3(2026, 9, 28, 23)),
  ];
  var el = M.jobsTable(jobs3, { id: "t312f-fold", now: NOW });
  document.body.appendChild(el);
  var btn = pick(el);
  btn.click(); // open
  var panel = function () { return el.querySelector(".filter-panel"); };
  var dateSel = function () { return panel().querySelector("select.fp-date"); };
  var fromSel = function () { return panel().querySelector("select.fp-from"); };
  var toSel = function () { return panel().querySelector("select.fp-to"); };
  var labels = function (sel) { return sel.children.map(function (o) { return o.textContent; }); };
  var setOpt = function (sel, v) { sel.value = v; sel.dispatchEvent(new Event("change")); };
  var state = function () { return M.TABLE_STATE["t312f-fold"].filters.started; };
  var stateOrNull = function () { return state() == null ? null : state(); };
  var keyGone = function () { return !("started" in M.TABLE_STATE["t312f-fold"].filters); };
  var selects = function () {
    return { date: dateSel().value, from: fromSel().value, to: toSel().value };
  };
"""


def test_f1_the_read_back_leaves_an_escape_from_the_dayless_hour_edges(tmp_path):
    """The defect, dayless: from 09, to 11 write "09-11"; from→any writes the to-alone edge
    "00-11" and the read-back must show from "any" and to "11" — the old read-back
    re-materialized "00", making the next to→any a no-op ("00-11" again). Now to→any composes
    "" and setFilter deletes the key (R3's last clause, reachable), all three selects "any"."""
    got = _page_js(tmp_path, _JOBS + _PANEL_ROWS + """
  setOpt(fromSel(), "09");
  setOpt(toSel(), "11");
  var before = state();
  setOpt(fromSel(), ""); // the escape: from alone is the to-alone edge "00-11"
  var escaped = { state: stateOrNull(), readback: { from: fromSel().value, to: toSel().value } };
  setOpt(toSel(), ""); // both handles "any": the state empties
  var gone = { state: stateOrNull(), keyGone: keyGone(), selects: selects() };
  return { before: before, escaped: escaped, gone: gone };
""")
    assert got["before"] == "09-11", got
    assert got["escaped"]["state"] == "00-11", got  # the write is unchanged
    assert got["escaped"]["readback"] == {"from": "", "to": "11"}, got  # the rule: "00" reads "any"
    assert got["gone"]["state"] is None, got  # the key is gone
    assert got["gone"]["keyGone"] is True, got
    assert got["gone"]["selects"] == {"date": "", "from": "", "to": ""}, got


def test_f2_the_escape_holds_with_a_date_and_lands_on_the_day_alone(tmp_path):
    """The same escape dated: today, from 09, to 11 write "2026-10-31 09-11"; from→any writes
    "2026-10-31 00-11" and reads back from "any", to "11"; to→any writes the date alone
    ("2026-10-31"), which the button summarizes "today"."""
    got = _page_js(tmp_path, _JOBS + _PANEL_ROWS + """
  setOpt(dateSel(), TODAY);
  setOpt(fromSel(), "09");
  setOpt(toSel(), "11");
  var dated = state();
  setOpt(fromSel(), "");
  var escaped = { state: stateOrNull(), readback: { from: fromSel().value, to: toSel().value } };
  setOpt(toSel(), "");
  var dateAlone = { state: stateOrNull(), summary: btn.textContent, selects: selects() };
  return { dated: dated, escaped: escaped, dateAlone: dateAlone };
""")
    assert got["dated"] == "2026-10-31 09-11", got
    assert got["escaped"]["state"] == "2026-10-31 00-11", got
    assert got["escaped"]["readback"] == {"from": "", "to": "11"}, got
    assert got["dateAlone"]["state"] == "2026-10-31", got  # the date alone, the hours gone
    assert got["dateAlone"]["summary"] == "today", got
    assert got["dateAlone"]["selects"] == {"date": "2026-10-31", "from": "", "to": ""}, got


def test_f3_the_edge_picks_are_one_bound_and_read_back_as_any(tmp_path):
    """The rule's other face: an explicit from 00 (with to 11) writes "00-11" as it always did
    and reads back from "any" — "00" from and "any" from are the same bound — while the summary
    still prints "00 to 11" (it reads the state, not the selects); an explicit to 23 (with from
    09) writes "09-23" and reads back to "any". Every other hour is F4."""
    got = _page_js(tmp_path, _JOBS + _PANEL_ROWS + """
  setOpt(fromSel(), "00"); // from alone: "00-23" (both edges)
  setOpt(toSel(), "11");
  var fromZero = { state: stateOrNull(), readback: { from: fromSel().value, to: toSel().value },
    summary: btn.textContent };
  setOpt(fromSel(), "09");
  setOpt(toSel(), "23"); // the explicit 23:59 pick
  var toTwentyThree = { state: stateOrNull(),
    readback: { from: fromSel().value, to: toSel().value }, summary: btn.textContent };
  return { fromZero: fromZero, toTwentyThree: toTwentyThree };
""")
    assert got["fromZero"]["state"] == "00-11", got  # the write is unchanged
    assert got["fromZero"]["readback"] == {"from": "", "to": "11"}, got
    assert got["fromZero"]["summary"] == "00 to 11", got  # the summary is untouched
    assert got["toTwentyThree"]["state"] == "09-23", got  # the write is unchanged
    assert got["toTwentyThree"]["readback"] == {"from": "09", "to": ""}, got
    assert got["toTwentyThree"]["summary"] == "09 to 23", got


def test_f4_every_other_hour_reads_back_as_itself(tmp_path):
    """The rule is exactly the two edges: each hour the fixture carries (09/10/11/14) set as
    from reads back as itself, and the same as to; the from→any / to→any reset empties the state
    (the escape F1 witnesses) each time."""
    got = _page_js(tmp_path, _JOBS + _PANEL_ROWS + """
  var out = { from: {}, to: {} };
  ["09", "10", "11", "14"].forEach(function (h) {
    setOpt(fromSel(), h);
    out.from[h] = fromSel().value;
    setOpt(fromSel(), "");
    setOpt(toSel(), h);
    out.to[h] = toSel().value;
    setOpt(toSel(), "");
  });
  return { out: out };
""")
    assert got["out"]["from"] == {"09": "09", "10": "10", "11": "11", "14": "14"}, got
    assert got["out"]["to"] == {"09": "09", "10": "10", "11": "11", "14": "14"}, got
