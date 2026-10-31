"""Fold of the page-batch review (2026-10-30, DeepSeek-V4.1-Flash): F1 and F2, the two 3.10c
helper rules the story's pins could not see.

F1 (app.js ``cellText``): the filter text is the cell's text PLUS its title whenever the title
adds characters — the full UUID behind the short-id cell and the full date-time behind the
clock-time cell both match a pasted needle. The old guard (``title.indexOf(own) < 0``) dropped
the title exactly when it was a superset, so a pasted UUID and today's date matched nothing.

F2 (app.js ``sortRows``): a missing value is absent, not small — it sorts past every present
value in BOTH directions, and a text column's null/blank is missing too, so ``last activity``
blanks sit at the end whether the arrow points up or down. The old sentinel multiplied by
``dir`` and sent text blanks through ``localeCompare``.

Ruling (o): regex/substring pins over the page source, like the neighbours in
tests/test_3_10c.py. The runtime witness is tools/smoke/tables.js's ``f``: a filter on the
started cell's date title and on a full UUID, then ``prefill tok/s`` descending and ``last
activity`` ascending with the blanks counted on page 1 and at the end.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "localagents" / "monitor" / "static"


def _app_js():
    return (STATIC / "app.js").read_text()


def _nested_function(js, name):
    """A function nested inside ``dataTable`` (four-space indented), to its closing brace."""
    m = re.search(r"\n    function " + re.escape(name) + r"\([^\n]*\n.*?\n    \}\n", js, re.S)
    assert m is not None, name
    return m.group(0)


# --- F1: the filter text includes a title that adds characters ----------------------------------


def test_f1_cell_text_appends_the_title_that_extends_the_cell():
    """Both halves of the P1 probe's case. (1) A superset title is exactly the case the
    comment names (the started cell's full date-time behind the clock); the old
    ``t.indexOf(own) < 0`` guard dropped it. A title equal to the cell adds nothing, so ``t ?``
    alone is the rule. (2) The actionable title is often on a CHILD, not the td — jobLink's
    anchor carries the full UUID behind the short id, the requests pill's span the numeric
    code behind "ok" — so the cell's titles are its own and its descendants'."""
    body = _nested_function(_app_js(), "cellText")
    # 3.11a row 38: the same expression, now stored lower-cased in the per-table cache
    # (the cache's behaviour, title matches included: tests/test_3_11a.py)
    assert '(t ? own + " " + t : own)' in body, body
    assert "indexOf(own)" not in body, body
    assert 'td.querySelectorAll("[title]")' in body, body
    assert 'titles.join(" ")' in body, body


# --- F2: a missing value sorts last in both directions -----------------------------------------


def test_f2_missing_sorts_last_in_both_directions():
    """The sentinel branch carries no direction: a missing value is absent, not small, so it
    sorts past every present value whether the arrow points up or down (the old
    ``(xn ? -1 : 1) * dir`` flipped it). A text column's null/blank is missing under the same
    branch — ``last activity``'s blanks are ``""`` — so the present test must admit a
    non-empty text, not only a number."""
    body = _nested_function(_app_js(), "sortRows")
    sentinel = [ln.strip() for ln in body.splitlines() if "xn !== yn" in ln]
    assert len(sentinel) == 1, sentinel
    assert "* dir" not in sentinel[0], sentinel[0]
    assert "return xn ? -1 : 1;" in sentinel[0], sentinel[0]
    # the present test treats a text column's null/blank as missing too
    assert '(x != null && x !== "")' in body, body
    assert '(y != null && y !== "")' in body, body


# --- F3 (operator, 2026-10-31): "every job says 'null' below the requests table" ----------------

import json
import shutil
import subprocess

NODE = shutil.which("node") or "/home/linuxbrew/.linuxbrew/bin/node"
ROOT = Path(__file__).resolve().parent.parent

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


def test_f3_a_table_without_a_note_draws_no_null_text(tmp_path):
    """The operator (2026-10-31, the job page): "every job says 'null' below the requests table
    and row count." dataTable handed ``opts.note ? h(...) : null`` to ``replaceChildren``, and the
    browser inserts a non-Node argument as the text of its string — "null". Every table whose
    caller passes no note drew it (both requests tables; the endpoint page's jobs table). The
    stand-in used to skip a null, kinder than the browser, so no node test could see it; it now
    prints "null" as the browser does, and this test walks every text node of a drawn table block
    for the word. The overview's jobs table, which always carries a note, is the control: its note
    is the last child and the only text outside the table and the pager."""
    got = _page_js(tmp_path, """
  function texts(el, acc) {
    el.childNodes.forEach(function (n) {
      if (n.nodeType === 3) acc.push(n.data); else texts(n, acc);
    });
    return acc;
  }
  var req = { t: 1, path: "/v1/messages", http_status: 200, error: "", ms: 800, job_id: null,
    tokens: { input: 100, output: 10, cache_read: 0, cache_creation: 0 } };
  var job = { id: "00000000-1111-2222-3333-444444444444", status: "succeeded", server_live: false,
    started_at: 1, model: "m", endpoint: "e", elapsed_s: 5, num_turns: 1, tool_calls: 0, errors: 0,
    compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null, last_activity: null };
  var noNote = M.requestsTable([req], { job: false, now: 100 });
  var withNote = M.jobsTable([job], { now: 100, note: "newest first" });
  var last = withNote.childNodes[withNote.childNodes.length - 1];
  return {
    requestsNull: texts(noNote, []).indexOf("null") >= 0,
    requestsTopChildren: noNote.childNodes.map(function (n) { return n.nodeType === 3 ? "#text:" + n.data : n.tagName + "." + n.className; }),
    jobsNull: texts(withNote, []).indexOf("null") >= 0,
    jobsLast: last.nodeType === 3 ? "#text:" + last.data : last.className,
  };
""")
    assert got["requestsNull"] is False, got["requestsTopChildren"]
    assert got["requestsTopChildren"] == ["DIV.table-wrap", "DIV.table-pager muted"]
    assert got["jobsNull"] is False
    assert got["jobsLast"] == "jobs-note muted"
