"""Story 3.12d-fold: the DeepSeek review of 3.12d (EPICS 3.12, 2026-10-31) found that the comparator
and range tokens forbade an inner space, so the ms cells' own print ("1.2 s", "800 ms") never
reached readNum; and that decimal hours/minutes ("1.5m") read nothing. The fold: the operand and
the range ends may hold spaces (the surrounding whitespace is trimmed), and every duration part
takes a decimal.

The original story (EPICS 3.12, the operator: "the filter
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


# --- F1: the cells' own print, with its space, is a readable bound ---------------------------


def test_f1_the_ms_box_reads_the_cells_own_print_with_its_space(tmp_path):
    """The duration cells print "8 ms" and "1.2 s"; a bound typed as the cell prints it must read:
    "<1.2 s" strict under 1200, ">1.2 s" strict over, "800 ms-1.2 s" inclusive, "<1 s"."""
    got = _page_js(tmp_path, _COUNT + """
  var rows = [8, 800, 1200, 60000].map(function (v) { return { v: v }; });
  var cols = [{ name: "duration", filter: "number", unit: "ms", title: null,
    cell: function (r) { return M.h("td", { text: r.v < 1000
      ? M.fmt.int(r.v) + " ms" : M.fmt.speed(r.v / 1000) + " s" }); },
    sortKey: function (r) { return r.v; } }];
  var el = M.dataTable("t312dfold-ms", cols, rows, {});
  return {
    lt1p2sSpace: typeIn(el, "<1.2 s"), // 8, 800
    gt1p2sSpace: typeIn(el, ">1.2 s"), // 60000
    rangeSpaces: typeIn(el, "800 ms-1.2 s"), // 800, 1200
    rangeSpacesAround: typeIn(el, "800 ms - 1.2 s"), // the same, spaces around the hyphen
    lt1sSpace: typeIn(el, "<1 s"), // 8, 800
    typedCell: typeIn(el, "1.2 s"), // no comparator: the substring, the one cell
  };
""")
    assert got["lt1p2sSpace"] == 2, got
    assert got["gt1p2sSpace"] == 1, got
    assert got["rangeSpaces"] == 2, got
    assert got["rangeSpacesAround"] == 2, got
    assert got["lt1sSpace"] == 2, got
    assert got["typedCell"] == 1, got


# --- F2: a decimal in any duration part -------------------------------------------------------


def test_f2_a_decimal_hour_or_minute_reads_as_a_duration(tmp_path):
    """"1.5m" is 90 s, "1.5h" 5400 s, "0.5d" 43200 s: the operator's shorthand, not the cell's
    grammar (fmt.dur never prints a decimal), but a bound that reads nothing silently is worse."""
    got = _page_js(tmp_path, _COUNT + """
  var rows = [5, 599, 600, 601, 5400, 90000].map(function (v) { return { v: v }; });
  var cols = [{ name: "elapsed", filter: "number", unit: "seconds", title: null,
    cell: function (r) { return M.h("td", { text: M.fmt.dur(r.v) }); },
    sortKey: function (r) { return r.v; } }];
  var el = M.dataTable("t312dfold-s", cols, rows, {});
  return {
    lt1p5m: typeIn(el, "<1.5m"), // 5
    range1p5h2h: typeIn(el, "1.5h-2h"), // 5400
    lt0p5d: typeIn(el, "<0.5d"), // all but 90000: 5
    gt1p5h: typeIn(el, ">1.5h"), // 90000 (5400 is not over 5400)
    still10m: typeIn(el, "<10m"), // the integer grammar unchanged: 5, 599
    noSpaceInSeconds: typeIn(el, "<10 m"), // not the cell's grammar: the substring, 0
  };
""")
    assert got["lt1p5m"] == 1, got
    assert got["range1p5h2h"] == 1, got
    assert got["lt0p5d"] == 5, got
    assert got["gt1p5h"] == 1, got
    assert got["still10m"] == 2, got
    assert got["noSpaceInSeconds"] == 0, got
