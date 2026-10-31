"""Story 3.12b-fold: the DeepSeek review of 3.12b (EPICS 3.12, 2026-10-31) found two defects. (1) The
endpoint page's events section was a NEW element every draw, so with an event in range
drawTables' mount(tablesBox, ...) saw a difference on every push and replaced all three sections,
re-inserting the persistent jobs and requests sections and resetting their sideways scroll (60 -> 0
measured live). The events section now persists like the other two, its body rebuilt only when the
events change. (2) Level 1's hash covered the rows and the column names but not the caller's `note`
or `empty` text, so a range switch over an unchanged row set kept the old note ("jobs ... in the
last 1h" under a 6h table). The hash now carries both.

The loader below is the 3.12b test's (rebuilt here, not imported: ruling (o)).
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


# --- F1: the events section persists ---------------------------------------------------------


def test_f1_the_events_section_persists_so_a_push_with_events_touches_no_section(tmp_path):
    """With an event in range, a second draw must not call replaceChildren on the tables box at
    all: the three sections are the same elements, and the events body is rebuilt only when the
    events themselves change."""
    got = _page_js(tmp_path, _IDS + """
  var hist = { t: [1000000], gen_tps: [10.5], state: ["idle"],
    jobs: [mkJob("00000000-1111-2222-3333-444444444444", 1)],
    events: [{ t: 1000000, kind: "restart", detail: "collector restarted" }] };
  var reqsBody = { requests: [], totals: { count: 0, input_tokens: 0, output_tokens: 0, cache_read: 0,
    cache_creation: 0, ms: 0, errors: 0 } };
  global.fetch = function (url) {
    var body = String(url).indexOf("/requests") >= 0 ? reqsBody : hist;
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(body); } });
  };
  var fakeState = { snap: snapA, range: "1h" };
  var container = document.createElement("div");
  document.body.appendChild(container);
  var teardown = M.pages.endpoint(container, "ai1", fakeState);
  return new Promise(function (resolve) {
    setTimeout(function () {
      var detail = container.querySelector(".endpoint-detail");
      var tablesBox = detail.childNodes[2];
      var sections1 = Array.prototype.slice.call(container.querySelectorAll("section"));
      var ev1 = container.querySelector("section.events");
      var evWrap1 = ev1 && ev1.querySelector(".table-wrap");
      var calls = 0;
      var orig = tablesBox.replaceChildren;
      tablesBox.replaceChildren = function () { calls++; return orig.apply(this, arguments); };
      var realNow = Date.now;
      Date.now = function () { return realNow() + 60000; };
      M.render(snapA); // a push: the hook re-fetches and draws the tables again
      setTimeout(function () {
        var sections2 = Array.prototype.slice.call(container.querySelectorAll("section"));
        var ev2 = container.querySelector("section.events");
        var same = sections1.length === sections2.length && sections1.every(function (s, i) { return s === sections2[i]; });
        var evWrapSame = ev2 && ev2.querySelector(".table-wrap") === evWrap1;
        // now the events change: the body is rebuilt inside the SAME section
        hist = Object.assign({}, hist, { events: [{ t: 1000000, kind: "restart", detail: "collector restarted" },
          { t: 1000500, kind: "down", detail: "10.0.0.50 reachable, port 8080 no response" }] });
        Date.now = function () { return realNow() + 120000; };
        M.render(snapA);
        setTimeout(function () {
          var ev3 = container.querySelector("section.events");
          var out = { sections: sections1.length, same: same, calls: calls, evWrapSame: !!evWrapSame,
            evSectionSame3: ev3 === ev1, evWrapNew3: ev3 && ev3.querySelector(".table-wrap") !== evWrap1,
            evRows3: ev3 ? ev3.querySelectorAll("tbody tr").length : null,
            h2: ev3 && ev3.children[0].textContent };
          teardown();
          resolve(out);
        }, 60);
      }, 60);
    }, 60);
  });
""")
    assert got["sections"] == 3, got
    assert got["same"] is True, got
    assert got["calls"] == 0, got  # the defect: 1 (the new events section forced a replaceChildren)
    assert got["evWrapSame"] is True, got
    assert got["evSectionSame3"] is True, got
    assert got["evWrapNew3"] is True, got
    assert got["evRows3"] == 2, got
    assert got["h2"] == "Restarts and outages · ai1 · last 1h", got


# --- F2: the hash carries the caller's note and empty text -----------------------------------


def test_f2_a_changed_note_over_the_same_rows_is_not_skipped(tmp_path):
    """The endpoint page's note names the range ("jobs that ran on ai1 in the last 1h"); a range
    switch over an unchanged row set must redraw the note, so the note is part of the hash."""
    got = _page_js(tmp_path, _IDS + """
  var rows = [mkJob("00000000-1111-2222-3333-444444444444", 1)];
  var before = M.renders.tableSkipped;
  var el1 = M.jobsTable(rows, { id: "t312bfold-note", note: "in the last 1h", now: 1000000 });
  document.body.appendChild(el1); // level 1 and 2 only persist a container that is in the document
  var el2 = M.jobsTable(JSON.parse(JSON.stringify(rows)), { id: "t312bfold-note", note: "in the last 6h", now: 1000000 });
  var el3 = M.jobsTable(JSON.parse(JSON.stringify(rows)), { id: "t312bfold-note", note: "in the last 6h", now: 1000000 });
  var emptyA = M.jobsTable([], { id: "t312bfold-empty", empty: "none in 1h" });
  document.body.appendChild(emptyA);
  var emptyB = M.jobsTable([], { id: "t312bfold-empty", empty: "none in 6h" });
  return { same: el1 === el2 && el2 === el3, note: el2.querySelector(".jobs-note").textContent,
    skippedAfterNoteChange: M.renders.tableSkipped - before,
    emptyText: emptyB.querySelector(".empty").textContent, emptySame: emptyA === emptyB };
""")
    assert got["same"] is True, got
    assert got["note"] == "in the last 6h", got
    assert got["skippedAfterNoteChange"] == 1, got  # only the third call, identical in every field
    assert got["emptyText"] == "none in 6h", got
    assert got["emptySame"] is True, got
