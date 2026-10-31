"""Story 3.10c: every table pages, sorts and filters, client-side, and keeps the operator's
view across pushes (the operator, EPICS 3.10: "it shows me we need pagination on our tables -
this one I'm looking at as 889 lines, which is not useful. Pagination with local column filters
and sorting?" — "3.10c - yes, client side filter.").

Shape (EPICS 3.10c): client-side over the rows already fetched (the endpoint requests route's
1000-row cap and whole-range totals stay — read.py ``endpoint_requests``); page size 50 with a
pager line; click-to-sort on every header (numeric columns numeric, a second click reverses);
one text filter per column (substring, case-insensitive; ``>n``/``<n`` on numeric columns); and
sort, filters and page survive every push — the state lives in a module-level map keyed by a
table id, never in the DOM (the overview replaces its jobs table on every snapshot).

Ruling (o): regex/substring pins over app.js and app.css, like the neighbours'. The cell logic
itself (columns, pills, titles, promptTotal/newTokens/timeTd) is pinned unchanged by
tests/test_monitor_page.py, test_3_10b.py and test_3_10hb.py — the committed DOM smoke
tools/smoke/tables.js reads the pager, the sort, the filter grammar and the survival across a
push."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "localagents" / "monitor" / "static"


def _app_js():
    return (STATIC / "app.js").read_text()


def _css():
    return (STATIC / "app.css").read_text()


def _js_function(js, name):
    """Same slice as tests/test_monitor_page.py's helper: a top-level function's source."""
    m = re.search(r"\n  function " + re.escape(name) + r"\(.*?\n  \}\n", js, re.S)
    assert m is not None, name
    return m.group(0)


# --- A: the helper ------------------------------------------------------------------------------


def test_the_data_table_helper_exists_and_is_exported():
    js = _app_js()
    assert "function dataTable(id, columns, rows, opts) {" in js
    assert "dataTable: dataTable," in js  # exported on window.localagentsMonitor
    assert "TABLE_STATE: TABLE_STATE," in js  # and the state map, for the smoke


def test_the_state_is_a_module_map_keyed_by_the_table_id():
    """A missing entry is the default (no sort, no filter, page 1); the overview's re-render on
    every push finds the same entry, so the operator's view survives the replaceChildren."""
    js = _app_js()
    assert "var TABLE_STATE = {};" in js
    assert re.search(r"TABLE_STATE\[id\]", js)
    # the default entry: no sort, no filters, page 1
    assert re.search(r"TABLE_STATE\[id\] = \{ sort: null, filters: \{\}, page: 1 \}", js)


def test_the_page_size_is_named_once_as_a_constant():
    js = _app_js()
    assert js.count("var TABLE_PAGE_SIZE = 50;") == 1, "one named 50, not littered literals"
    assert "opts.page || TABLE_PAGE_SIZE" in js


def test_the_renderers_draw_through_the_helper_with_stable_ids():
    """jobsTable hands id "jobs"; requestsTable its two pages apart (the job page's
    opts.job === false; the endpoint page's opts.job is undefined — the brief's
    `opts.job ? "job" : "endpoint"` reads inverted against the code and is corrected)."""
    js = _app_js()
    # the overview's table is "jobs"; a caller may pass its own id (the endpoint page: "jobs-endpoint"),
    # so a filter typed on one page never filters the other (coordinator fold of the 3.10c landing)
    assert 'dataTable(opts.id || "jobs"' in _js_function(js, "jobsTable")
    assert 'id: "jobs-endpoint"' in (STATIC / "endpoint.js").read_text()
    rbody = _js_function(js, "requestsTable")
    assert 'dataTable("requests-"' in rbody
    assert 'opts.job === false ? "job" : "endpoint"' in rbody


def test_the_sort_is_a_click_on_the_header_with_aria_sort_and_a_reversible_direction():
    js = _app_js()
    body = _js_function(js, "dataTable")
    assert '"aria-sort"' in body
    assert '"ascending"' in body and '"descending"' in body
    # a second click on the sorted column reverses: the direction is a sign that flips
    assert re.search(r"st\.sort\.dir = -st\.sort\.dir|st\.sort\.dir \*= -1", body)


def test_the_filter_grammar_substring_and_the_numeric_comparators():
    """3.10c's grammar as extended by 3.12a §5: substring stays the base, the numeric
    comparators stay, and the INCLUSIVE range `x-y` joins them on number columns (the
    operator: "absolute and inclusive endpoints")."""
    js = _app_js()
    body = _js_function(js, "dataTable")
    # substring, case-insensitive (indexOf on the lower-cased text)
    assert re.search(r"\.indexOf\(", body) and ".toLowerCase()" in body
    # a leading > or < on a numeric column compares the sort key as a number
    assert 'f.charAt(0) === ">" || f.charAt(0) === "<"' in body
    assert "parseFloat(f.slice(1))" in body
    # 3.12a: the number box reads x-y first — two decimals around a hyphen, spaces allowed,
    # inclusive on both ends (>= and <= in the filter)
    assert r"(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)" in body
    assert re.search(r"v >= \w+ && v <= \w+", body)


def test_the_filter_row_is_inputs_one_per_column_built_from_the_state():
    """3.10c: one control per column, built from TABLE_STATE's filters. 3.12a §4 widened the
    control: a pick column draws a native <select data-filter> (no free-text search), the rest
    keep the text input."""
    js = _app_js()
    body = _js_function(js, "dataTable")
    assert 'h("input", { type: "text"' in body
    assert 'h("select", { "data-filter"' in body  # 3.12a: the pick
    assert 'addEventListener("change"' in body  # the select redraws on change
    assert "placeholder:" in body
    # typing updates the state and redraws THIS table in place through the container it owns
    assert 'addEventListener("input"' in body
    assert re.search(r"st\.filters\[[^\]]+\] = ", body)


# --- C: the stylesheet --------------------------------------------------------------------------


def test_app_css_styles_the_filter_row_the_arrow_and_the_pager():
    css = _css()
    for sel in ("table-filters", "table-pager", "sort-asc", "sort-desc"):
        assert re.search(r"\." + re.escape(sel) + r"(?![\w-])", css), sel
    # the arrow is drawn (a content rule on the sorted header), not text the probe would eat
    assert re.search(r'content\s*:\s*"[▲▼]', css)
