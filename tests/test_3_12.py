"""Story 3.12a: typed table filters, everything in the filter row (spec
docs/superpowers/specs/2026-10-31-table-filters-design.md §2-§7, §10). The operator (EPICS 3.12):
"status colume is limited values, should be dropdown picker for filter. Same with model, endpoint.
... turns, tool calls, errors are all values so range picker would be appropriate with absolute
and inclusive endpoints." and "For absolute values like numbers, text search first and then allow
x-y range and simple < and > - no need for a pop-out" and "For the pick I don't see a need for any
ability to free-text search".

Each column spec grows a `filter` kind (pick / number / time / text, default text) beside
`numeric`; every control stays in the filter row. `pick` columns get a native <select> ("any" +
the distinct sort-key values with counts over the rows the OTHER filters leave). `number` and
`time` boxes read their text in order: a range `x-y` / `a - b` (inclusive), a strict `<n` / `>n`,
else today's substring search. The state shape is unchanged: TABLE_STATE[id].filters stays
{col: string}; an empty value deletes the key.

The behavioural tests run the page scripts under node over tests/fakedom.js (3.11a's loader
pattern, rebuilt here rather than importing the 3.11a module); the source pins are the neighbours'
family (ruling (o)).
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
    """Run ``body`` (a function body returning a JSON-able value) with the three page scripts
    loaded over the DOM stand-in; returns what it returned."""
    script = tmp_path / "page_probe.js"
    script.write_text(_PROLOGUE % {"fakedom": json.dumps(str(ROOT / "tests" / "fakedom.js")),
                                   "static": json.dumps(str(STATIC)), "body": body})
    p = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


# --- step 1: the kinds, column by column (spec §3) -------------------------------------------------


def test_each_column_declares_its_filter_kind_and_the_table_draws_it(tmp_path):
    """§3: the kind is a `filter` field on the column spec beside `numeric`, read by dataTable;
    the filter row's control cell carries it (data-filter-kind) so the drawn controls can be
    pinned per column. Jobs: job text, status pick, started time, model pick, endpoint pick, the
    seven figures number, last activity text. Requests: time time, status pick, job text, path
    pick, the seven figures number, error text."""
    got = _page_js(tmp_path, """
  var job = { id: "00000000-1111-2222-3333-444444444444", status: "succeeded", server_live: true,
    started_at: 1000, model: "m", endpoint: "ai1", elapsed_s: 1, num_turns: 1, tool_calls: 1,
    errors: 0, compactions: 0, ctx_pct: 10, ctx_tokens: 1, context: 10, prefill_tps: 1.5,
    gen_tps: 2.5, last_activity: "a" };
  var req = { t: 1000, path: "/v1/messages", http_status: 200, error: null, ms: 100, gen_ms: 50,
    job_id: null, tokens: { input: 1, output: 1, cache_read: 0, cache_creation: 0 } };
  var kinds = function (el) {
    return el.querySelectorAll(".table-filters th").map(function (th) {
      var c = th.children[0];
      return { name: c ? c.getAttribute("data-filter") : null, kind: th.getAttribute("data-filter-kind") };
    });
  };
  return { jobs: kinds(M.jobsTable([job], { id: "t312-kinds-jobs", now: 1000 })),
    reqs: kinds(M.requestsTable([req], { now: 1000 })) };
""")
    jobs = [(c["name"], c["kind"]) for c in got["jobs"]]
    assert jobs == [
        ("job", "text"), ("status", "pick"), ("started", "time"), ("model", "pick"),
        ("endpoint", "pick"), ("elapsed", "number"), ("turns", "number"),
        ("tool calls", "number"), ("tool errors", "number"), ("context", "number"),
        ("prefill tok/s", "number"), ("decode tok/s", "number"), ("last activity", "text"),
    ], got
    reqs = [(c["name"], c["kind"]) for c in got["reqs"]]
    assert reqs == [
        ("time", "time"), ("status", "pick"), ("job", "text"), ("path", "pick"),
        ("prompt", "number"), ("new", "number"), ("reused", "number"), ("output", "number"),
        ("prefill t/s", "number"), ("decode t/s", "number"), ("duration", "number"),
        ("error", "text"),
    ], got


# --- step 2: the pick is a native select (spec §4) -------------------------------------------------

_PICK_JOBS = """
  var mk = function (i) { return {
    id: String(i).padStart(8, "0") + "-1111-2222-3333-444444444444",
    status: i % 3 === 0 ? "failed" : i % 3 === 1 ? "running" : "succeeded",
    server_live: true, started_at: 1000000 - 60 * i, model: "m" + (i % 4),
    endpoint: i % 2 ? "ai1" : "dgx1-a", elapsed_s: i * 7, num_turns: i % 130,
    tool_calls: i % 10, errors: i % 5, compactions: 0, ctx_pct: null,
    prefill_tps: 1.5, gen_tps: 2.5, last_activity: "act " + i }; };
  var jobs = []; for (var i = 0; i < 120; i++) jobs.push(mk(i));
"""


def test_the_pick_is_a_select_of_any_plus_the_present_values_with_faceted_counts(tmp_path):
    """§4: option 0 "any" (value ""), then every distinct sort-key value across ALL rows sorted
    as the column sorts (localeCompare for text), labelled "value (count)" — the count over the
    rows the OTHER columns' filters leave, so "done (40)" narrows to "done (7)" under a turns
    filter while the values stay listed."""
    got = _page_js(tmp_path, _PICK_JOBS + """
  var el = M.jobsTable(jobs, { id: "t312-pick-a", now: 1000000 });
  var readSel = function () {
    var sel = el.querySelector('select[data-filter="status"]');
    if (!sel) return null;
    return { options: sel.children.map(function (o) { return { value: o.getAttribute("value"),
      label: o.textContent }; }) };
  };
  var plain = readSel();
  el.querySelector('input[data-filter="turns"]').value = ">100";
  el.querySelector('input[data-filter="turns"]').dispatchEvent(new Event("input"));
  var faceted = readSel();
  return { plain: plain, faceted: faceted };
""")
    assert got["plain"]["options"] == [
        {"value": "", "label": "any"},
        {"value": "done", "label": "done (40)"},
        {"value": "failed", "label": "failed (40)"},
        {"value": "running", "label": "running (40)"},
    ], got["plain"]
    # turns > 100 leaves 19 rows (i 101..119): done 7, failed 6, running 6 — still all listed
    assert got["faceted"]["options"] == [
        {"value": "", "label": "any"},
        {"value": "done", "label": "done (7)"},
        {"value": "failed", "label": "failed (6)"},
        {"value": "running", "label": "running (6)"},
    ], got["faceted"]


def test_the_pick_matches_the_sort_key_exactly_redraws_and_keeps_focus(tmp_path):
    """The rule is `String(col.sortKey(row)) === value` — picking "a" must not pull in "aa"
    (what a substring would do); the change redraws the rows and the count; focus returns to
    the select through the redraw (the re-focus trick the input already uses)."""
    got = _page_js(tmp_path, """
  var rows = [{ v: "a" }, { v: "aa" }, { v: "aa" }, { v: "b" }];
  var cols = [{ name: "w", filter: "pick", title: null,
    cell: function (r) { return M.h("td", { text: r.v }); }, sortKey: function (r) { return r.v; } }];
  var el = M.dataTable("t312-pick-exact", cols, rows, {});
  var sel = el.querySelector('select[data-filter="w"]');
  sel.focus(); // as in the browser: a change comes from the focused control (activeElement)
  sel.value = "a";
  sel.dispatchEvent(new Event("change"));
  var sel2 = el.querySelector('select[data-filter="w"]');
  return { rows: el.querySelectorAll("tbody tr").length,
    texts: el.querySelectorAll("tbody tr td").map(function (td) { return td.textContent; }),
    pager: el.querySelector(".table-pager").textContent,
    state: JSON.parse(JSON.stringify(M.TABLE_STATE["t312-pick-exact"].filters)),
    refocused: document.activeElement === sel2,
    caretKind: document.activeElement && document.activeElement.getAttribute("data-filter") };
""")
    assert got["rows"] == 1 and got["texts"] == ["a"], got
    # a set filter always shows the matched count out of the fetched total (3.10c's pager rule)
    # and carries the clear link (3.12a §7)
    assert got["pager"] == "1–1 of 1 · previous · next · clear filters", got
    assert got["state"] == {"w": "a"}, got
    assert got["refocused"] is True and got["caretKind"] == "w", got


def test_the_pick_survives_as_a_zero_option_and_any_deletes_the_key(tmp_path):
    """A selected value a later push drops stays selected, listed "(0)", until "any" — and
    picking "any" deletes the state key (the filter string stays one value per column)."""
    got = _page_js(tmp_path, _PICK_JOBS + """
  var el = M.jobsTable(jobs, { id: "t312-pick-c", now: 1000000 });
  var sel = el.querySelector('select[data-filter="status"]');
  sel.value = "running";
  sel.dispatchEvent(new Event("change"));
  var before = M.TABLE_STATE["t312-pick-c"].filters.status;
  // a second dataTable call on the SAME id with new rows: no running row left
  var allDone = [];
  for (var i = 0; i < 5; i++) allDone.push({ id: "d" + i + "0000000-1111-2222-3333-444444444444",
    status: "succeeded", server_live: true, started_at: 1000000 - 60 * i, model: "m0",
    endpoint: "ai1", elapsed_s: i, num_turns: i, tool_calls: 0, errors: 0, compactions: 0,
    ctx_pct: null, prefill_tps: 1.5, gen_tps: 2.5, last_activity: "act" });
  var el2 = M.jobsTable(allDone, { id: "t312-pick-c", now: 1000000 });
  var sel2 = el2.querySelector('select[data-filter="status"]');
  var options = sel2.children.map(function (o) { return { value: o.getAttribute("value"),
    label: o.textContent, selected: sel2.value === o.getAttribute("value") }; });
  var pagerNoMatch = el2.querySelector(".table-pager").textContent;
  sel2.value = "";
  sel2.dispatchEvent(new Event("change"));
  var after = el2.querySelector('select[data-filter="status"]');
  return { before: before, options: options, pagerNoMatch: pagerNoMatch,
    state: JSON.parse(JSON.stringify(M.TABLE_STATE["t312-pick-c"].filters)),
    pagerCleared: el2.querySelector(".table-pager").textContent,
    selectedNow: after.value };
""")
    assert got["before"] == "running"
    assert [o["label"] for o in got["options"]] == ["any", "done (5)", "running (0)"], got["options"]
    assert got["pagerNoMatch"] == "no rows match the filters · clear filters", got
    assert got["state"] == {}, got
    assert got["pagerCleared"] == "5 rows", got
    assert got["selectedNow"] == ""


# --- step 3: the number box reads x-y, then <n / >n, then substring (spec §5) ----------------------

_NUM_BOX = """
  var typeIn = function (el, v) {
    var input = el.querySelector('input[data-filter="elapsed"]');
    input.value = v;
    input.dispatchEvent(new Event("input"));
    var p = el.querySelector(".table-pager").textContent;
    var m = / of (\\d+)/.exec(p);
    if (m) return +m[1];
    m = /^(\\d+) rows?$/.exec(p);
    return m ? +m[1] : (/^no rows match/.test(p) ? 0 : -1);
  };
"""


def test_the_number_box_reads_a_range_then_the_comparators_then_the_substring(tmp_path):
    """§5, in order: `x-y` is an INCLUSIVE range on the sort key (both ends kept; x > y matches
    nothing; a null sort key never matches a range or a comparator); `<n` / `>n` stay STRICT
    (3.10c's grammar); anything else is today's substring over the cell's text — "12" still
    matches an elapsed cell reading "12m03s". And the order: "1-2" is a range, never a
    substring hunt through text that happens to hold "1-2"."""
    got = _page_js(tmp_path, _NUM_BOX + """
  var rows = [null, 5, 10, 12.5, 50, 100, 723].map(function (v) { return { v: v }; });
  var cols = [{ name: "elapsed", filter: "number", unit: "seconds", title: null,
    cell: function (r) { return M.h("td", { text: M.fmt.dur(r.v) }); },
    sortKey: function (r) { return r.v; } }];
  var el = M.dataTable("t312-num-a", cols, rows, {});
  var out = {
    rangeInclusive: typeIn(el, "10-50"), // 10, 12.5, 50 — both ends kept
    pointRange: typeIn(el, "100-100"), // one row at the point
    reversed: typeIn(el, "50-10"), // x > y: nothing
    nullNeverMatches: typeIn(el, "0-100000"), // six figures, never the null
    strictGt: typeIn(el, ">100"), // the 100 row dropped
    strictLt: typeIn(el, "<10"), // only 5
    spacesInRange: typeIn(el, "10 - 12.5"), // spaces around the hyphen
    substring12: typeIn(el, "12"), // 12.5 (its number) and 723 ("12m03s"); 5/10/50/100 not
  };
  // the reading order: this column's TEXT holds "1-2" but its sort key is 99 — the range wins
  var el2 = M.dataTable("t312-num-b", [{ name: "elapsed", filter: "number", unit: null, title: null,
    cell: function (r) { return M.h("td", { text: r.t }); }, sortKey: function (r) { return r.v; } }],
    [{ t: "1-2 off", v: 99 }, { t: "x", v: 1.5 }], {});
  out.rangeNotSubstring = typeIn(el2, "1-2");
  out.titleWithUnit = el.querySelector('input[data-filter="elapsed"]').getAttribute("title");
  out.titleNoUnit = el2.querySelector('input[data-filter="elapsed"]').getAttribute("title");
  return out;
""")
    assert got["rangeInclusive"] == 3, got
    assert got["pointRange"] == 1, got
    assert got["reversed"] == 0, got
    assert got["nullNeverMatches"] == 6, got
    assert got["strictGt"] == 1, got  # only 723
    assert got["strictLt"] == 1, got  # only 5
    assert got["spacesInRange"] == 2, got  # 10 and 12.5
    assert got["substring12"] == 2, got  # 12.5 and 723, by their cell text
    assert got["rangeNotSubstring"] == 1, got  # the 1.5 row, not the text-holding 99 row
    assert got["titleWithUnit"] == "elapsed, seconds — text, x-y, <n, >n; n as 4m12s, 2h03m or seconds", got  # 3.12d
    assert got["titleNoUnit"] == "elapsed — text, x-y, <n, >n", got


# --- step 4: the time box reads the same three readings over time literals (spec §6) ----------------


def test_the_time_box_reads_time_literals_inclusively_then_comparators_then_substring(tmp_path):
    """§6: `HH:MM` (24-hour, the clock on the day opts.now falls in), `YYYY-MM-DD` (a day: its
    start as a lower bound, its END as an upper bound) and `YYYY-MM-DD HH:MM`, parsed by hand;
    a range is two literals around " - " (spaces) or two HH:MM around a bare hyphen, a bare
    date is never split; a range's ends are inclusive over each literal's whole span (the
    end of its minute, +59 s, or of its day); `>a` and `<a` are STRICT over the whole span —
    `>a` means after the literal's end, `<a` before its start, so `<11:30` and `>11:30` never
    share a row and the 11:30 minute itself is in neither (the coordinator's fold at landing:
    the first spec text had `<a` reaching the end of its minute, so `<a` and `>a` overlapped
    by a minute, and by a day for a bare date); an unparseable literal is
    reading 3, substring — including the date the cell's title carries ("10/30" style, read
    from a drawn cell so the locale does not matter). Hand rows: started_at noon - 60 i for
    i 0..119 on Oct 30 2026 local, so "11:00-11:30" keeps 11:00:00..11:30:59 — rows i 30..60."""
    got = _page_js(tmp_path, _NUM_BOX.replace('elapsed', 'started') + """
  var noon = new Date(2026, 9, 30, 12, 0, 0).getTime() / 1000;
  var rows = []; for (var i = 0; i < 120; i++) rows.push({ v: noon - 60 * i });
  var cols = [{ name: "started", filter: "time", unit: null, title: null,
    cell: function (r) { return M.h("td", { text: M.fmt.time(r.v), title: M.fmt.date(r.v) }); },
    sortKey: function (r) { return r.v; } }];
  var el = M.dataTable("t312-time-a", cols, rows, { now: noon });
  var out = {
    window: typeIn(el, "11:00-11:30"),
    gtStrict: typeIn(el, ">11:30"),
    ltStrict: typeIn(el, "<11:30"),
    exactMinute: typeIn(el, "11:30-11:30"),
    dateAsUpperEnd: typeIn(el, "11:00 - 2026-10-30"),
    ltDateStrict: typeIn(el, "<2026-10-30"),
    gtPreviousDay: typeIn(el, ">2026-10-29"),
    fullLiteralGt: typeIn(el, ">2026-10-30 11:30"),
    fullLiteralRange: typeIn(el, "2026-10-30 10:30 - 2026-10-30 11:00"),
  };
  // the F1 witness moved here from the smoke's text path: the date the title carries still
  // matches as substring (locale-independent: take it from a drawn cell's own title)
  var dayText = el.querySelector("tbody tr td").getAttribute("title").split(",")[0];
  out.substringDay = typeIn(el, dayText);
  out.dateNeverSplit = typeIn(el, "2026-10-30"); // not a range: substring, holds nothing
  out.badLiteral = typeIn(el, ">25:00"); // outside 24 h: reading 3
  out.title = el.querySelector('input[data-filter="started"]').getAttribute("title");
  return out;
""")
    assert got["window"] == 31, got  # 11:00:00 through 11:30:59 inclusive, both ends
    assert got["gtStrict"] == 30, got  # after 11:30:59: 11:31 through 12:00
    assert got["ltStrict"] == 89, got  # before 11:30:00: 10:01 through 11:29
    assert got["exactMinute"] == 1, got  # the minute itself, by the range form
    assert got["gtStrict"] + got["ltStrict"] + got["exactMinute"] == 120, got  # a partition, no overlap
    assert got["dateAsUpperEnd"] == 61, got  # 11:00 through the END of Oct 30
    assert got["ltDateStrict"] == 0, got  # before Oct 30 starts: every row is on Oct 30
    assert got["gtPreviousDay"] == 120, got  # after Oct 29 ends: every row
    assert got["fullLiteralGt"] == 30, got
    assert got["fullLiteralRange"] == 31, got  # 10:30:00 .. 11:00:59
    assert got["substringDay"] == 120, got
    assert got["dateNeverSplit"] == 0, got
    assert got["badLiteral"] == 0, got
    assert got["title"] == "started — text, HH:MM, YYYY-MM-DD, a - b, <a, >a", got


# --- step 5: "clear filters" and the empty-deletes rule (spec §7) -----------------------------------


def test_the_clear_filters_link_appears_with_any_filter_and_empty_values_delete_their_key(tmp_path):
    """§7: a "clear filters" pager-btn sits at the end of the pager line whenever any filter is
    set (also after "no rows match the filters") and deletes every key and resets the page; and
    one setFilter backs both controls — a blank box or "any" deletes the key, so the state never
    carries an empty filter."""
    got = _page_js(tmp_path, """
  var rows = [1, 2, 3, 4, 5].map(function (v) { return { v: v }; });
  var cols = [{ name: "n", filter: "number", unit: null, title: null,
    cell: function (r) { return M.h("td", { text: String(r.v) }); },
    sortKey: function (r) { return r.v; } }];
  var el = M.dataTable("t312-clear", cols, rows, { page: 2 }); // two per page
  var btns = function () { return el.querySelectorAll(".pager-btn").map(function (b) { return b.textContent; }); };
  var typeIn = function (v) {
    var input = el.querySelector('input[data-filter="n"]');
    input.value = v;
    input.dispatchEvent(new Event("input"));
  };
  var st = M.TABLE_STATE["t312-clear"];
  typeIn("1-2"); // a filter set: the link appears next to the pager buttons
  var withFilter = { btns: btns(), keys: Object.keys(st.filters) };
  typeIn(""); // blank deletes the key: no filter, no link
  var blanked = { keys: Object.keys(st.filters), btns: btns() };
  typeIn("9"); // matches nothing
  var noMatch = { pager: el.querySelector(".table-pager").textContent, btns: btns() };
  // two filters, page 2: a clear wipes the state, resets the page and redraws
  typeIn("1-4");
  el.querySelectorAll(".pager-btn").filter(function (b) { return b.textContent === "next"; })[0].click();
  var paged = { page: st.page, keys: Object.keys(st.filters) };
  el.querySelectorAll(".pager-btn").filter(function (b) { return b.textContent === "clear filters"; })[0].click();
  var afterClear = { page: st.page, keys: Object.keys(st.filters), pager: el.querySelector(".table-pager").textContent,
    rows: el.querySelectorAll("tbody tr").length };
  return { withFilter: withFilter, blanked: blanked, noMatch: noMatch, paged: paged, afterClear: afterClear };
""")
    assert got["withFilter"]["keys"] == ["n"], got
    assert got["withFilter"]["btns"] == ["previous", "next", "clear filters"], got
    assert got["blanked"] == {"keys": [], "btns": ["previous", "next"]}, got
    assert got["noMatch"]["pager"] == "no rows match the filters · clear filters", got
    assert got["paged"] == {"page": 2, "keys": ["n"]}, got
    assert got["afterClear"] == {"page": 1, "keys": [], "pager": "1–2 of 5 · previous · next", "rows": 2}, got
