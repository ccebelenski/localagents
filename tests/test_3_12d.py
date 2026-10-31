"""Story 3.12d: the number box reads the cell's own unit (EPICS 3.12, the operator: "the filter
on elapsed column - not intuitive for me - the columns display format 00m00s but filter is text
search, so I can't do (for example) <10m for a filter").

The elapsed column is a `number` filter whose sort key is seconds and whose cells print the
duration grammar ("42s", "4m12s", "2h03m", "3d04h"); the comparator read `parseFloat` and so
"<10m" matched jobs under ten SECONDS. One reader, ``readNum(unit, s)``, now parses a bound in
the unit the column holds: "seconds" reads the grammar the cell prints ("10m", "1h30m", "2h",
"1d4h", "90s"), "ms" reads "800ms", "1.2s", "2m", "%" strips one trailing "%"; a bare number is
read only when the WHOLE remainder is one (">10x" no longer reads 10 — it falls to the
substring, the better reading of a typo), and a bare number keeps the sort key's unit ("<600"
on elapsed is still seconds). The range ends go through the same reader, as does the
comparator's operand; either falls through to the substring when unparseable, as today.

The behavioural tests run the page scripts under node over tests/fakedom.js, the loader
pattern from tests/test_3_12a_fold.py (rebuilt here rather than importing another test module:
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


# The count witness: the pager's total is the matched count, never the page's 50.
_COUNT = """
  var typeIn = function (el, v) {
    var input = el.querySelector("input[data-filter]"); // each table here holds one column
    input.value = v;
    input.dispatchEvent(new Event("input"));
    var p = el.querySelector(".table-pager").textContent;
    var m = / of (\\d+)/.exec(p);
    if (m) return +m[1];
    m = /^(\\d+) rows?$/.exec(p);
    return m ? +m[1] : (/^no rows match/.test(p) ? 0 : -1);
  };
"""


# --- D1: one reader, the cell's own unit -------------------------------------------------------


def test_the_seconds_box_reads_the_duration_grammar_the_cell_prints(tmp_path):
    """elapsed: sort key seconds, cells fmt.dur. "<10m" is ten MINUTES (before: parseFloat read
    10 and it matched jobs under ten seconds). "<600" stays bare seconds; a typed "1h30m" with
    no comparator is the substring over the cells, not a bound; "<10x" is unparseable — 0 rows,
    the substring reading of a typo (before: parseFloat("10x") read 10 and matched the 5 row)."""
    got = _page_js(tmp_path, _COUNT + """
  var rows = [5, 599, 600, 601, 5400, 90000].map(function (v) { return { v: v }; });
  var cols = [{ name: "elapsed", filter: "number", unit: "seconds", title: null,
    cell: function (r) { return M.h("td", { text: M.fmt.dur(r.v) }); },
    sortKey: function (r) { return r.v; } }];
  var el = M.dataTable("t312d-sec", cols, rows, {});
  return {
    lt10m: typeIn(el, "<10m"), // 5, 599
    gt10m: typeIn(el, ">10m"), // 601, 5400, 90000
    range5m10m: typeIn(el, "5m-10m"), // inclusive 300..600: 599, 600
    gt1h: typeIn(el, ">1h"), // 5400, 90000
    lt1d4h: typeIn(el, "<1d4h"), // 100800: all six
    typed1h30m: typeIn(el, "1h30m"), // no comparator: the substring, the 1h30m cell
    bare600: typeIn(el, "<600"), // bare seconds: 5, 599
    lt10x: typeIn(el, "<10x"), // not a bound: the substring finds nothing
  };
""")
    assert got["lt10m"] == 2, got
    assert got["gt10m"] == 3, got
    assert got["range5m10m"] == 2, got
    assert got["gt1h"] == 2, got
    assert got["lt1d4h"] == 6, got
    assert got["typed1h30m"] == 1, got
    assert got["bare600"] == 2, got
    assert got["lt10x"] == 0, got


def test_the_ms_box_reads_ms_s_m_and_h_as_the_duration_cells_print_them(tmp_path):
    """duration (requests table): sort key ms, cells "8 ms" under a second else "1.2 s".
    "<1s", ">1.2s" (strict), "800ms-1.2s" (inclusive), "<1m"; "<1,000" keeps the comma rule."""
    got = _page_js(tmp_path, _COUNT + """
  var rows = [8, 800, 1200, 60000].map(function (v) { return { v: v }; });
  var cols = [{ name: "duration", filter: "number", unit: "ms", title: null,
    cell: function (r) { return M.h("td", { text: r.v < 1000
      ? M.fmt.int(r.v) + " ms" : M.fmt.speed(r.v / 1000) + " s" }); },
    sortKey: function (r) { return r.v; } }];
  var el = M.dataTable("t312d-ms", cols, rows, {});
  return {
    lt1s: typeIn(el, "<1s"), // 8, 800
    gt1p2s: typeIn(el, ">1.2s"), // strict: only 60000
    range800ms1p2s: typeIn(el, "800ms-1.2s"), // inclusive: 800, 1200
    lt1m: typeIn(el, "<1m"), // 60000 excluded: 8, 800, 1200
    lt1000: typeIn(el, "<1,000"), // the comma rule: 8, 800
  };
""")
    assert got["lt1s"] == 2, got
    assert got["gt1p2s"] == 1, got
    assert got["range800ms1p2s"] == 2, got
    assert got["lt1m"] == 3, got
    assert got["lt1000"] == 2, got


def test_the_percent_box_reads_a_trailing_percent_and_a_bare_number(tmp_path):
    """context: sort key 0..100, cells fmt.pct. "<50%" matches one row; "50%-80%" is the
    inclusive range over both ends' "%"; "<50" (bare) reads the same as "<50%"."""
    got = _page_js(tmp_path, _COUNT + """
  var rows = [34, 50, 80].map(function (v) { return { v: v }; });
  var cols = [{ name: "context", filter: "number", unit: "%", title: null,
    cell: function (r) { return M.h("td", { text: M.fmt.pct(r.v) }); },
    sortKey: function (r) { return r.v; } }];
  var el = M.dataTable("t312d-pct", cols, rows, {});
  return {
    lt50pct: typeIn(el, "<50%"), // 34 only
    range50pct80pct: typeIn(el, "50%-80%"), // inclusive: 50, 80
    lt50: typeIn(el, "<50"), // bare: 34 only
  };
""")
    assert got["lt50pct"] == 1, got
    assert got["range50pct80pct"] == 2, got
    assert got["lt50"] == 1, got


# --- D2: the title names the grammar as the cell prints it ---------------------------------------


def test_the_number_title_appends_the_grammar_for_the_units_that_have_one(tmp_path):
    """The box's hover title already names the sort key's unit; now it also shows the shape a
    bound takes in that unit: seconds "; n as 4m12s, 2h03m or seconds", ms "; n as 800ms, 1.2s
    or ms", "%" "; n as 50%". Any other unit and the no-unit title stay as 3.12a wrote them."""
    got = _page_js(tmp_path, """
  var oneCol = function (id, name, unit) {
    var el = M.dataTable(id, [{ name: name, filter: "number", unit: unit, title: null,
      cell: function (r) { return M.h("td", { text: "x" }); },
      sortKey: function (r) { return 1; } }], [{}], {});
    return el.querySelector("input[data-filter]").getAttribute("title");
  };
  return {
    seconds: oneCol("t312d-t-sec", "elapsed", "seconds"),
    ms: oneCol("t312d-t-ms", "duration", "ms"),
    pct: oneCol("t312d-t-pct", "context", "%"),
    count: oneCol("t312d-t-count", "turns", "count"),
    none: oneCol("t312d-t-none", "elapsed", null),
  };
""")
    assert got["seconds"] == "elapsed, seconds — text, x-y, <n, >n; n as 4m12s, 2h03m or seconds", got
    assert got["ms"] == "duration, ms — text, x-y, <n, >n; n as 800ms, 1.2s or ms", got
    assert got["pct"] == "context, % — text, x-y, <n, >n; n as 50%", got
    assert got["count"] == "turns, count — text, x-y, <n, >n", got
    assert got["none"] == "elapsed — text, x-y, <n, >n", got
