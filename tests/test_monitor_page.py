"""Story 3.2: the overview page (monitor spec §5 "Overview"). The page is one HTML file and
its page scripts plus one stylesheet, served from the package's ``monitor/static/`` directory
with no CDN at runtime. (Story 3.2 shipped them beside a vendored uPlot; 3.7l retired the
charts that used it and 3.7-fold-4 dropped the library — the vendored-file tests went with it.)

There is no JavaScript runtime in the suite: these tests pin what Python can see (the shell's
static references, the class inventory the browser smoke looks for, and that ``app.js`` reads
only §5a field names). The DOM itself is the browser smoke's (spec §7)."""
import asyncio
import contextlib
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from localagents import db
from localagents.monitor import app as app_mod
from localagents.monitor.config import MonitorSettings

from monitor_seeded import NOW, registry, seed_file, write_config

STATIC = Path(app_mod.__file__).resolve().parent / "static"

# the shell loads exactly these, in this order (stylesheet first, then the page scripts; the
# detail pages register themselves after app.js, stories 3.3 and 3.4 in their own files; the
# vendored uPlot sat between the stylesheet and app.js until 3.7-fold-4 dropped it)
SHELL_ASSETS = ["/static/app.css", "/static/app.js", "/static/endpoint.js", "/static/job.js"]


@pytest.fixture
def client(tmp_path):
    p = tmp_path / "seeded.db"
    seed_file(p)
    app = app_mod.make_app(lambda: db.open_db(p), registry(tmp_path), MonitorSettings(), asyncio.Event(),
                           now=lambda: NOW)
    with TestClient(app) as c:
        yield c


# --- item 1: the shell and its static files ------------------------------------------------------


def test_the_shell_references_exactly_the_page_static_files(client):
    html = client.get("/").text
    refs = [r for r in re.findall(r'(?:src|href)="([^"]+)"', html) if not r.startswith("#")]  # in-page links
    assert refs == SHELL_ASSETS
    assert "//" not in "".join(refs)  # nothing off the package: no CDN, no scheme-relative URL


@pytest.mark.parametrize("path, kind", [
    ("/static/app.css", "text/css"),
    ("/static/app.js", "javascript"),
    ("/static/endpoint.js", "javascript"),
    ("/static/job.js", "javascript"),
])
def test_each_static_file_is_served_with_its_content_type(client, path, kind):
    r = client.get(path)
    assert r.status_code == 200
    assert kind in r.headers["content-type"]
    assert r.content == (STATIC / path.removeprefix("/static/")).read_bytes()

# (3.7-fold-4: test_vendor_md_sha256_lines_match_the_vendored_files retired with its files —
# VENDOR.md documented only the uPlot pair, and the uPlot pair went when 3.7l left no page
# using it; there is no other vendored file, so VENDOR.md went with them.)


# --- item 2: the page's class inventory and its reads of the §5a JSON ----------------------------

SPEC = Path(__file__).resolve().parent.parent / "docs" / "superpowers" / "specs" / "2026-10-29-monitor-design.md"

# the classes the browser smoke and the spec name (story 3.2's brief)
SMOKE_CLASSES = ["host", "endpoint", "state-live", "state-down", "state-dormant", "node-down", "node-unknown",
                 "jobs", "fleet", "range",
                 # 3.7l: the fleet timeline (the class "timeline" also names the job page's
                 # tool timeline — one stylesheet, both styled)
                 "timeline", "tl-row", "tl-label", "tl-track", "tl-band", "tl-job", "tl-axis", "tl-now",
                 # the live endpoint card (stories 3.7d-f): the fixed field grid, the jobs block
                 # that is always the card's last child, its status-line rows and their activity
                 # tail, and the state dot (this lane styles all three variants; the live card's
                 # span is the token the list checks, the down/dormant spans are lane A's markup)
                 "figures", "ep-jobs", "ep-job", "ep-job-figs", "ep-act", "sdot",
                 # 3.7-fold-1 (3.7n/3.7q): the job row's glued ctx bar+percent span; the
                 # shared-scale peak label, its wrap, and the strip row's range word
                 "ep-ctx", "spark-peak", "spark-wrap", "strip-range",
                 # 3.7g: the status pill in the Recent jobs log (the pill-<status> colour
                 # variants are styled too, but the smoke reads the pill by its word)
                 "pill",
                 # 3.7-fold-3 (3.7s page half): the last-active phrase on the live head line
                 "ep-active",
                 # 3.7u-fix (U1/U3): the one shared class for every one-line text element on a
                 # card, and the class the floor's pass marks on the lines that scroll in their
                 # own box (worn through classList.toggle, which the scan below reads)
                 "ep-line", "overflows"]
# the classes the job page's smoke names (story 3.4's brief): the page root and its sections,
# the status classes the header's mark takes (a queued job takes one more, styled but not
# smoked)
JOB_SMOKE_CLASSES = ["job-detail", "gauge", "timeline", "by-tool", "job-requests", "transcript", "files",
                     "status-running", "status-succeeded", "status-failed", "status-timeout",
                     "status-cancelled", "status-lost", "log-dir",
                     # 3.7v: the shared requests renderer's muted pill for the /api/hello
                     # startup handshake (drawn by app.js, which the scan below reads)
                     "pill-muted"]

# the names app.js reads the route bodies through (its header says so): snapshot, host, endpoint,
# running job (and, since 3.7l, a timeline job span), endpoint hardware, endpoint rates, endpoint
# history. 3.7l removed the fleet charts' `fleet` and `fleetEp` — no page file reads the fleet
# history any more (the route stays; 3.7-fold-4 dropped the vendored uPlot the charts needed)
# `req` is the shared requests renderer's request row (3.7k), read through the same walk
APP_JS_VARS = ("snap", "host", "ep", "job", "hw", "rates", "hist", "req")
# the names job.js (story 3.4) reads the route bodies through (its header says so): the job
# detail through `detail`, a snapshot through `snap`, a request row through `req`, a tool call
# through `tc`, a snapshot jobs[] entry through `job`
JOB_JS_VARS = ("detail", "snap", "req", "tc", "job")
PAGE_VARS = {"app.js": APP_JS_VARS, "job.js": JOB_JS_VARS}
# Array methods called on those values: not fields (the allow-list of the regex's false positives)
JS_BUILTINS = {"forEach", "map", "filter", "some", "length", "slice", "sort", "push", "join", "concat", "indexOf"}


def _section_5a():
    text = SPEC.read_text()
    start = text.index("## 5a. Routes and JSON shapes")
    return text[start:text.index("## 6. ", start)]


def _app_js():
    return (STATIC / "app.js").read_text()


def test_the_page_carries_every_class_the_smoke_and_spec_name():
    """Each class appears as a whole word inside a class string of the shell or of a page
    script, and the stylesheet styles it."""
    text = (STATIC / "index.html").read_text() + (STATIC / "app.js").read_text() + (STATIC / "job.js").read_text()
    # the overview's `cls += "node-down"` style and the job page's status class map values
    # (3.7u-fix: classList.toggle too — the overflows class is never in a class string, it is
    # toggled on measured lines by raiseCardFloor)
    strings = re.findall(r'class="([^"]*)"|class: "([^"]*)"|cls \+?= "([^"]*)"|"(node-[a-z]+)"|"(status-[a-z]+)"'
                         r'|classList\.toggle\("([^"]+)"', text)
    classes = {c for groups in strings for s in groups for c in s.split()}
    css = (STATIC / "app.css").read_text()
    for cls in SMOKE_CLASSES + JOB_SMOKE_CLASSES:
        assert cls in classes, cls
        assert re.search(r"\." + re.escape(cls) + r"(?![\w-])", css), cls


# one step of a property chain: `.name`, `["name"]` / `['name']` (a field read with a literal
# key: a field like any other), `[0]` (an index: no field), or `[<expression>]` (a read with a
# variable key, one level of nested brackets: `[m[0]]`)
_CHAIN_STEP = re.compile(r"""\.([A-Za-z_$][\w$]*)|\[\s*"([^"\\\n]*)"\s*\]|\[\s*'([^'\\\n]*)'\s*\]|\[\s*(\d+)\s*\]"""
                         r"""|\[([^\[\]\n]*(?:\[[^\[\]\n]*\][^\[\]\n]*)*)\]""")


def _walk_chains(js_text, vars, holders=("state",)):
    """(fields, variable-key reads) over every property chain rooted at one of ``vars``.

    A chain may start at ``<holder>.<var>`` — the page's own object holding a route body,
    ``state.snap.now`` — and is then walked from the variable on, so ``state.snap.<field>``
    is checked against §5a just like ``snap.<field>`` (story 3.4a). A step is ``.field`` or a
    bracket: ``fleetEp["gen_tps"]`` reads the field ``gen_tps`` (story 3.2a, ruling G: the old
    pattern stopped at the bracket); ``[0]`` is an index; any other key is a read with a
    variable key, returned as ``<chain>[<key>]`` (whitespace dropped) for the caller to
    allow-list by name. The chain goes on past every bracket (``fleet.endpoints[name].gen_tps``),
    and a chain nested inside a bracket is walked on its own."""
    fields, var_keys = set(), set()
    root = re.compile(r"(?<![\w.$])(?:(?:" + "|".join(holders) + r")\.)?(" + "|".join(vars) + r")(?![\w$])")
    for m in root.finditer(js_text):
        pos, path = m.end(), [m.group(1)]
        while (step := _CHAIN_STEP.match(js_text, pos)) is not None:
            ident, dq, sq, index, expr = step.groups()
            field = next((f for f in (ident, dq, sq) if f is not None), None)
            if field is not None:
                fields.add(field)
                path.append(field)
            elif index is not None:
                path.append("[" + index + "]")
            else:
                key = re.sub(r"\s+", "", expr)
                var_keys.add(".".join(path) + "[" + key + "]")
                path.append("[" + key + "]")
            pos = step.end()
    return fields - JS_BUILTINS, var_keys


def _read_fields(js_text, vars, holders=("state",)):
    """The field names read through the given variable names (dotted or with a literal bracket
    key), the allow-list of the regex's array-method false positives removed."""
    return _walk_chains(js_text, vars, holders)[0]


# every read with a variable key off a route body, by name (story 3.2a, ruling G): such a key is
# invisible to the §5a check, so each is listed here with what it reads, and a new one fails the
# gate until it is listed. Index reads into a §5a array (`hist.t[i]`) are listed the same way.
VARIABLE_KEY_READS = {
    "app.js": {
        # 3.7l: the charts' "fleet.endpoints[n]" and "fleetEp[key]" are gone — no app.js chain
        # is rooted at the fleet body any more
        "hist.t[i]", "hist.t[j+1]",  # the history time axis, by index (the state strip, the
                                     # sparkline since 3.7o, and the timeline's busy band)
                                     # the sparkline since 3.7o, which reads the endpoint's own history)
        "hist.state[i]", "hist.state[j+1]",  # the state series, by index (the strip's segments;
                                              # its "now" label is the snapshot's probe_state)
    },
    "job.js": {
        "detail.metrics[k]",  # the metrics delta by column: derive._METRICS_COLUMNS (a test pins METRICS)
        "detail.metrics[m[0]]",  # the same, from a METRICS row's key
    },
    "endpoint.js": {
        "hist[d.series]",  # a history series by TILES series name (a test pins them to read.py)
        "snap.hosts[i]", "host.endpoints[k]",  # the snapshot's arrays, by index (find())
        "hist.t[i]", "hist.t[j+1]",  # the history time axis, by index (the state strip)
        "hist.state[i]", "hist.state[j+1]", "hist.state[hist.state.length-1]",  # the state series, by index
        "hist.t[page.hist.t.length-1]",  # the newest time (nowOf, through page.hist)
    },
}


def test_app_js_reads_only_section_5a_field_names():
    """Every property chain read off a route body (``ep.rates.gen_tps``, ``fleet.endpoints``,
    ``detail.requests``, ``job.server_live``) names keys §5a names, so a field the page
    invents (or a renamed one) fails here. Walked per page file, each through the names its
    header declares (app.js: JSON_VARS, job.js: detail/snap/req/tc/job)."""
    spec = _section_5a()
    for name, vars in PAGE_VARS.items():
        fields = _read_fields((STATIC / name).read_text(), vars)
        # the regex does see the reads (a guard against a pattern that matches nothing)
        assert {"hosts", "endpoints", "jobs", "rates", "gen_tps", "state", "t", "server_live",
                "dropped_job_id", "db_path", "probe_state",
                # 3.7k: the shared requests renderer's row reads; 3.10f-2: the job half of the
                # row is job_id alone (the name keys are gone from every shape)
                "http_status", "tokens", "cache_read", "job_id", "gen_ms"} <= fields \
            if name == "app.js" else \
            {"requests", "tool_call_log", "ctx_tokens", "usage", "metrics", "tool_counts",
             "files_touched", "transcript_tail", "elapsed_s", "server_live"} <= fields, name
        missing = sorted(f for f in fields if not re.search(r"(?<![A-Za-z_])" + re.escape(f) + r"(?![A-Za-z_])", spec))
        assert missing == [], (name, missing)


@pytest.mark.parametrize("name, vars, holders", [
    ("app.js", APP_JS_VARS, ("state",)),
    ("job.js", JOB_JS_VARS, ("state",)),
    ("endpoint.js", None, ("state", "page")),  # ENDPOINT_JSON_VARS, defined below
])
def test_every_variable_key_read_is_listed_by_name(name, vars, holders):
    """Story 3.2a, ruling G: a read off a route body with a variable key (``fleetEp[key]``)
    cannot be checked against §5a, so each one is listed in VARIABLE_KEY_READS with what it
    reads; a new one, or a listed one that is gone, fails here."""
    _, var_keys = _walk_chains((STATIC / name).read_text(), vars or ENDPOINT_JSON_VARS, holders)
    assert var_keys == VARIABLE_KEY_READS[name]


def test_the_field_walk_sees_literal_bracket_keys_and_holder_chains():
    """The walk itself, over constructed lines: a literal bracket key is a field, also behind a
    variable-key bracket and a holder; an index is not; a variable key is returned by name."""
    js = '''var a = fleetEp["gen_tpz"]; var b = state.fleet.endpoints[name]['prompt_tpz'].length;
    var c = hist.t[0].zzz; var d = page.hist.state[i]; var e = xfleet.nope; var f = obj.fleet.nope2;'''
    # 3.7l: the constructed lines name the charts' old chain roots (fleet, fleetEp) — kept as
    # the walker's fixture even though app.js no longer reads those names, so this walk names
    # them itself
    fields, var_keys = _walk_chains(js, APP_JS_VARS + ("fleet", "fleetEp"), ("state", "page"))
    assert fields == {"gen_tpz", "endpoints", "prompt_tpz", "t", "zzz", "state"}
    assert var_keys == {"fleet.endpoints[name]", "hist.state[i]"}


# --- story 3.10a: the Recent jobs table answers "when", says "lost", splits the speed ----------

# the operator (EPICS 3.10): "Recent jobs doesn't have a timestamp - so answering 'when' is not
# possible from the screen. The server gone tag on the job id field - what is the purpose and use
# of that? ... Also it shows 'speed' but really that's decode - where's prefill speed?"

def test_the_jobs_table_columns_answer_when_and_show_both_speeds():
    """JOB_COLUMNS names a day-stamped "started" column, drops the "speed" column for a
    "prefill tok/s" and a "decode tok/s" pair, and both new columns are right-aligned."""
    js = _app_js()
    cols_src = re.search(r"var JOB_COLUMNS = \[([^\]]*)\]", js)
    cols = re.findall(r'"([^"]+)"', cols_src.group(1))
    assert cols == ["job", "status", "started", "model", "endpoint", "elapsed", "turns",
                    "tool calls", "tool errors", "context", "prefill tok/s", "decode tok/s",
                    "last activity"]
    assert "speed" not in cols
    numeric_src = re.search(r"var NUMERIC = \{([^}]*)\}", js)
    assert '"prefill tok/s": 1' in numeric_src.group(1)
    assert '"decode tok/s": 1' in numeric_src.group(1)


def test_the_server_gone_badge_is_gone_and_a_lost_server_inks_the_pill():
    """The badge carried no information on a finished row (every finished job's server is gone)
    and duplicated the lost state on a live one; the status pill now says "lost" when a live
    row's server stopped beating (the job page's word, spec §6)."""
    js = _app_js()
    assert "server gone" not in js
    row_src = js[js.index("function jobRow("):js.index("function oneLine(")]
    assert re.search(r"var lost = job\.server_live === false", row_src)
    assert re.search(r'statusPill\(lost \? "lost" : job\.status\)', row_src)


# --- story 3.7u: no ellipsis on the cards ---------------------------------------------------------

# every `text-overflow: ellipsis` rule the stylesheet may hold, by its exact selector, each naming
# something that is never inside a host card (3.7u, the operator: "I don't like the '...'
# convention because it hides information"). A card rule with an ellipsis, or a new ellipsis rule
# anywhere, fails until it is listed here with why it is not card text.
ELLIPSIS_OUTSIDE_CARDS = {
    "#meta .db",      # the header's database path (elided from the left; the title holds it)
    "td.cut",         # the jobs and requests tables' long cells (the title holds the text)
    # .tile-value (the endpoint page's tiles) held a permit until 3.10d cut the ellipsis there too
    ".tl-label a",    # the timeline's endpoint labels (section.fleet, after the cards)
    ".tl-job",        # the timeline's job bars
}


def _css_rules(css):
    """(selector, declarations) for every rule of the stylesheet, comments removed; a rule inside
    an @media block is returned on its own (the block's prelude is not a selector)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def test_no_ellipsis_rule_applies_inside_a_host_card():
    """3.7u: no card text ends in an ellipsis. Every selector of every rule that sets
    ``text-overflow: ellipsis`` is one of the listed non-card selectors, and none of them names a
    card class (the card's lines are ep-head, ep-model, ep-active, ep-job-figs, ep-act and the
    field grid's tile label and value)."""
    rules = _css_rules((STATIC / "app.css").read_text())
    selectors = {s.strip() for sel, body in rules if re.search(r"text-overflow\s*:\s*ellipsis", body)
                 for s in sel.split(",")}
    assert selectors  # the scan sees the rules it is about (the header's path keeps one)
    assert selectors <= ELLIPSIS_OUTSIDE_CARDS, sorted(selectors - ELLIPSIS_OUTSIDE_CARDS)
    card = re.compile(r"\.(host|endpoint|ep-[\w-]+|figures|fig|hosts)(?![\w-])")
    assert [s for s in ELLIPSIS_OUTSIDE_CARDS if card.search(s)] == []


def _js_function(js, name):
    """The source of a function declared at the IIFE's top level (two-space indent): from its
    ``function`` line to its closing brace at the same indent."""
    m = re.search(r"\n  function " + re.escape(name) + r"\(.*?\n  \}\n", js, re.S)
    assert m is not None, name
    return m.group(0)


def test_ep_job_row_cuts_no_text():
    """3.7u: the card's job row draws the activity whole (3.7-fold-5 cut it at 60 chars and
    appended an ellipsis; the full text rode only in the title). No slice, substring or
    ellipsis-appending concatenation in epJobRow."""
    body = _js_function(_app_js(), "epJobRow")
    assert "last_activity" in body  # the extraction found the row builder
    assert re.search(r"\.(slice|substring|substr)\(", body) is None
    assert re.search(r"\+\s*\"…\"", body) is None


def test_the_card_floor_starts_where_the_stylesheet_does_and_only_rises():
    """3.7u: the grid's column floor is app.css's ``--card-floor`` (360px, the old fixed
    minimum) until app.js measures a wider card; app.js's copy is the same number, the grid
    caps the floor at its own width, and raiseCardFloor only ever raises it (the high-water
    mark: one assignment of the floor, behind a greater-than)."""
    css = (STATIC / "app.css").read_text()
    hosts = next(body for sel, body in _css_rules(css) if sel == ".hosts")  # the first: not @media's
    css_floor = re.search(r"--card-floor:\s*(\d+)px", hosts)
    assert css_floor is not None
    assert "minmax(min(var(--card-floor), 100%), 1fr)" in hosts
    js = _app_js()
    js_floor = re.search(r"var CARD_FLOOR_MIN = (\d+);", js)
    assert js_floor is not None and js_floor.group(1) == css_floor.group(1)
    body = _js_function(js, "raiseCardFloor")
    assert re.findall(r"cardLayout\.floor = [^;]+;", body) == ["cardLayout.floor = cardLayout.need;"]
    assert "if (cardLayout.need > cardLayout.floor) {" in body


def test_every_card_line_the_css_makes_scrollable_is_measured_by_the_floor():
    """3.7u-fix (U1/U3): the card's one-line texts live by one device — the stylesheet scrolls
    them (an ``overflow-x: auto`` rule under ``.host``) and raiseCardFloor measures them (its
    line selector). The lists are each other's oracle: a class the CSS scrolls but the floor
    does not measure can wrap or spill the page; one the floor measures but the CSS does not
    scroll grows a bar or a second line. Every scrolling class must also suppress its bar
    (U3: a scrollbar steals height from a pinned line box)."""
    rules = _css_rules((STATIC / "app.css").read_text())
    scrollable = set()
    for sel, body in rules:
        if not re.search(r"overflow-x\s*:\s*auto", body):
            continue
        for s in sel.split(","):
            if not s.strip().startswith(".host"):
                continue
            scrollable.update(m.group(1) for m in re.finditer(r"\.([A-Za-z][\w-]*)", s)
                              if m.group(1) != "host")
    assert scrollable  # the scan sees the 3.7u block it is about
    js = _app_js()
    measured_sel = re.search(r'querySelectorAll\("(\.ep-head[^"]*)"\)', _js_function(js, "raiseCardFloor"))
    assert measured_sel is not None
    measured = {m.group(1) for m in re.finditer(r"\.([A-Za-z][\w-]*)", measured_sel.group(1))}
    assert scrollable == measured, (sorted(scrollable - measured), sorted(measured - scrollable))
    suppressed = set()
    for sel, body in rules:
        if re.search(r"scrollbar-width\s*:\s*none", body):
            for s in sel.split(","):
                suppressed.update(m.group(1) for m in re.finditer(r"\.([A-Za-z][\w-]*)", s))
    assert scrollable <= suppressed, sorted(scrollable - suppressed)


def test_no_webkit_scrollbar_rule_remains_in_the_stylesheet():
    """3.7u-fix U3 (app.css:419-422 at 3.7u): once ``scrollbar-width`` is set, Chromium follows
    the standard property and ignores the 3 px ``::-webkit-scrollbar`` rules — the thin bar it
    took stole 10 px from the pinned line box (clientHeight 12 of 21.75). The webkit pair was
    dead weight and its bar a height thief; none may come back."""
    rules = _css_rules((STATIC / "app.css").read_text())
    offenders = [sel for sel, _ in rules if "::-webkit-scrollbar" in sel]
    assert offenders == [], offenders


def _ep_line_parents(src):
    """For every class literal in ``src`` that carries ``ep-line``: (the literal, the first class
    of the ``h(...)`` call whose children array holds that line directly — None when the line is
    not built inside another ``h()``, e.g. a function's return value). A small scanner: comments
    and string literals are skipped, brackets tracked on a stack of (char, position)."""
    out = []
    stack = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            i = src.index("\n", i)
            continue
        if src.startswith("/*", i):
            i = src.index("*/", i) + 2
            continue
        if c in "\"'":
            j = i + 1
            while src[j] != c:
                j += 2 if src[j] == "\\" else 1
            lit = src[i + 1:j]
            if c == '"' and re.search(r"class:\s*$", src[max(0, i - 12):i]) and "ep-line" in lit.split():
                # the stack at a child's class literal: ( parent h-call, [ children, ( child h-call, { attrs
                parent = None
                if [ch for ch, _ in stack[-4:]] == ["(", "[", "(", "{"]:
                    m = re.match(r'\("\w+",\s*\{\s*class:\s*"([^"]+)"', src[stack[-4][1]:])
                    if m:
                        parent = m.group(1).split()[0]
                out.append((lit, parent))
            i = j + 1
            continue
        if c in "([{":
            stack.append((c, i))
        elif c in ")]}":
            stack.pop()
        i += 1
    return out


def test_every_flex_row_holding_a_card_line_is_measured_at_max_content():
    """P-1 (Opus review of the page folds, 3.7u-fix): a line that is a flex item — .ep-seen in a
    down endpoint's head, .host-note beside the host name — is measured through its ROW: its own
    box left the row's free space counted as chrome, so every such line needed its card's whole
    width and the floor followed the window up for the page's life (one column at 2200 px). The
    row reads its text only at max-content, which app.css's .measuring rule gives; this gate
    reads the card builders for every ``ep-line`` built directly inside another element, and
    every such parent the stylesheet makes a flex container must be in the .measuring
    max-content set. What width the row then reads is the smoke's (tools/smoke/floor.mjs)."""
    js = _app_js()
    found = []
    for name in ("liveEndpoint", "downEndpoint", "dormantEndpoint", "hostCard", "epJobRow", "jobsBlock"):
        found += _ep_line_parents(_js_function(js, name))
    parents = {p for _, p in found}
    # the scan sees what it is about: the two flex rows and a block parent
    assert {"ep-head", "host-head", "endpoint"} <= parents, found
    flex, max_content = set(), set()
    for sel, body in _css_rules((STATIC / "app.css").read_text()):
        for part in (p.strip() for p in sel.split(",")):
            if not part:
                continue
            classes = set(re.findall(r"\.([A-Za-z][\w-]*)", part.split()[-1]))
            if re.search(r"display\s*:\s*(inline-)?flex\b", body):
                flex |= classes
            if part.startswith(".hosts.measuring") and re.search(r"width\s*:\s*max-content", body):
                max_content |= classes
    rows = {p for p in parents if p in flex}
    assert rows >= {"ep-head", "host-head"}, (rows, sorted(flex))
    assert rows <= max_content, sorted(rows - max_content)
    # and the floor measures a flex item through its row (the substitution in raiseCardFloor)
    assert re.search(r"/flex/\.test\(getComputedStyle\(line\.parentNode\)\.display\)",
                     _js_function(js, "raiseCardFloor"))


def test_in_place_comparisons_mask_the_overflows_mark():
    """P-2 (Opus review of the page folds, 3.7u-fix): raiseCardFloor marks scrolling lines on the
    LIVE nodes; the in-place update compares live nodes with freshly built ones, which never carry
    the mark, so a raw outerHTML or className comparison replaced the whole endpoint (or host
    head) on every push whenever a line scrolled. Every comparison in the two in-place helpers
    reads the live side through the masking helpers, and the helpers mask the very class
    raiseCardFloor toggles. (Node identity across pushes is the smoke's: tools/smoke/keep.js.)"""
    js = _app_js()
    toggled = re.findall(r'classList\.toggle\("([^"]+)"', _js_function(js, "raiseCardFloor"))
    assert toggled == ["overflows"], toggled
    assert re.search(r'var OVERFLOW_MARK = "' + toggled[0] + '";', js)
    for helper in ("builtHTML", "builtClass"):
        assert "OVERFLOW_MARK" in _js_function(js, helper), helper
    update = _js_function(js, "updateHostInPlace")
    swap = _js_function(js, "swapEndpointSubBlocks")
    # no raw read of a live node's serialisation or class string is left in either helper; the
    # one className read allowed is the freshly built side's
    assert ".outerHTML" not in update and ".outerHTML" not in swap
    assert re.findall(r"[\w\]\[.]+\.className", update + swap) == ["newEp.children[i].className"]
    assert update.count("sameAsBuilt(") == 2 and swap.count("sameAsBuilt(") == 1
    assert swap.count("builtClass(") == 1


# --- story 3.4: the job detail page (job.js) ----------------------------------------------------


def test_job_js_lists_exactly_the_metrics_columns():
    """The page lists the metrics delta's columns in its own display order (job.js's comment
    says so, story 3.4a); what must not drift is the SET: it equals derive._METRICS_COLUMNS,
    so a column added to the delta without a label on the page fails here."""
    from localagents import derive
    js = (STATIC / "job.js").read_text()
    m = re.search(r"var METRICS = \[(.*?)\n  \];", js, re.S)
    assert m is not None
    keys = re.findall(r'\["([a-z_]+)",', m.group(1))
    assert sorted(keys) == sorted(derive._METRICS_COLUMNS)


def test_job_js_links_to_no_file_urls():
    """A file:// link is dead in a page served over http (story 3.4a): the log directory is
    selectable text with a copy button instead, and the file never carries the scheme again."""
    assert (STATIC / "job.js").read_text().count("file://") == 0


def test_job_js_totals_count_only_real_errors():
    """Story 3.2c row 2 (the 3.2 combined review): the totals row counted every request with
    ``error != null``, so a server that writes the empty string for a clean request was tallied
    as an error while its own cell drew clean (the cell reads ``req.error || ""``; 3.2b's
    handoff did not pick the totals up). The totals now use the row's rule — non-null AND
    non-empty. This is a source pin, not a browser check: the rule is one expression, and the
    review reproduced the defect by reading it (a browser pass is available but adds nothing)."""
    js = (STATIC / "job.js").read_text()
    assert 'req.error != null && req.error !== ""' in js
    assert "if (req.error != null) tot.errors++;" not in js


# --- story 3.3: the endpoint detail page (endpoint.js) -------------------------------------------

# the classes the endpoint page's browser smoke names (story 3.3's brief; 3.7k adds the shared
# requests-table renderer's classes — the table, the two status pills and the context-exceeded
# and errored rows — defined in app.js, which the page loads first; fold-4's review added the
# missing req-err)
ENDPOINT_CLASSES = ["endpoint-detail", "tiles", "tile", "tier-0", "tier-1", "tier-2", "tier-3", "requests", "totals",
                    "events", "requests-table", "pill-ok", "pill-error", "req-ctx", "req-err"]

# the names endpoint.js reads the route bodies through (its header says so): snapshot, host,
# snapshot endpoint, endpoint hardware, endpoint rates, endpoint history, one history event,
# the requests body, one request row, the requests totals. 3.10b: the history's jobs arrive as
# §5a rows and are read by app.js's shared jobs-table renderer, so endpoint.js names no job
# variable of its own anymore.
ENDPOINT_JSON_VARS = ("snap", "host", "ep", "hw", "rates", "hist", "ev", "reqs", "req", "totals")


def _endpoint_js():
    return (STATIC / "endpoint.js").read_text()


def test_endpoint_js_carries_every_class_the_smoke_names():
    """Each class is a whole word inside a string literal of endpoint.js (the tier classes are
    picked from a list by tier), and the stylesheet styles it. Story 3.7k moved the requests
    table's markup into app.js's shared renderer, so the page's class literals are read from
    app.js too — the page loads it before endpoint.js."""
    js = _endpoint_js() + _app_js()
    tokens = {c for lit in re.findall(r'"([^"\n]*)"', js) for c in lit.split()}
    css = (STATIC / "app.css").read_text()
    for cls in ENDPOINT_CLASSES:
        assert cls in tokens, cls
        assert re.search(r"\." + re.escape(cls) + r"(?![\w-])", css), cls


# the shared renderer's classes the job page's request log also wears (3.7k): the renderer lives
# in app.js, so the overview/job inventory's class-string scan cannot see them (they are built
# with cls.push and a ternary) — they belong to a scan that reads app.js too, the way the
# endpoint inventory's does (3.7k review NOTICED: the job list named none of them; fold-4)
JOB_SHARED_CLASSES = ["requests-table", "pill-ok", "pill-error", "req-err", "req-ctx"]


def test_the_job_page_carries_the_shared_renderer_classes():
    js = (STATIC / "job.js").read_text() + _app_js()
    tokens = {c for lit in re.findall(r'"([^"\n]*)"', js) for c in lit.split()}
    css = (STATIC / "app.css").read_text()
    for cls in JOB_SHARED_CLASSES:
        assert cls in tokens, cls
        assert re.search(r"\." + re.escape(cls) + r"(?![\w-])", css), cls


def test_requests_tables_share_one_renderer():
    """Story 3.7k: the endpoint page and the job page draw their request logs through app.js's
    one exported renderer. Its column order is the operator's (Strata's log look plus our
    columns, with Strata's two derived rates), the job column is the endpoint page's alone, and
    the formulas are the spec §5a sentences' — a change to either side fails here."""
    app_js = _app_js()
    assert "requestsTable: requestsTable," in app_js  # exported on window.localagentsMonitor
    order = re.search(r'var columns = \["time", "status"\];.*?columns\.concat\(\[(.*?)\]\);', app_js, re.S)
    assert order is not None
    # 3.10h(b): "new" (input + cache_creation) sits between "prompt" (the whole prompt) and
    # "reused"; test_3_10hb.py pins the full list and the helpers' arithmetic
    assert re.findall(r'"([^"]+)"', order.group(1)) == ["path", "prompt", "new", "reused", "output", "prefill t/s",
                                                        "decode t/s", "duration", "error"]
    assert "if (withJob) columns.push(\"job\");" in app_js
    assert "var withJob = opts.job !== false;" in app_js
    assert "(req.ms - req.gen_ms) / 1000" in app_js  # prefill t/s
    # the brief's "input − cache_read" is Strata's total-input model; ours excludes cache reads,
    # so the processed count is input + cache_creation (Strata's processed = prompt − cached)
    assert "req.tokens.input + req.tokens.cache_creation" in app_js
    assert "req.tokens.output / (req.gen_ms / 1000)" in app_js  # decode t/s
    # each page calls it, with the job column dropped on the job page; 3.7-fold-4 adds each
    # page's clock as `now` for the day-stamped time column (the snapshot's now via nowOf on
    # the endpoint page, the detail clock via nowClock on the job page)
    assert "M.requestsTable(reqs.requests, { empty: \"no requests in this range\", now: now })" in _endpoint_js()
    assert "M.requestsTable(reqs, { job: false, now: nowClock() })" in (STATIC / "job.js").read_text()
    # the endpoint page's old column list and local numeric map are gone; the generic table()
    # stays for its events section (3.10b: the jobs table moved to app.js's shared renderer,
    # and the page's own JOB_COLUMNS with it — gone)
    js = _endpoint_js()
    assert "REQ_COLUMNS" not in js and "REQ_NUMERIC" not in js and "JOB_COLUMNS" not in js
    assert "function table(columns, rows, numeric)" in js


def test_endpoint_js_reads_only_section_5a_field_names():
    """The §5a field-name check of app.js, over endpoint.js with its own variable names, through
    the shared walk (story 3.2a: its own regex skipped `state.` chains and bracket reads); the
    page's own `page` object holds two route bodies (`page.hist`, `page.reqs`), so it is a
    holder like `state`."""
    spec = _section_5a()
    fields = _read_fields(_endpoint_js(), ENDPOINT_JSON_VARS, ("state", "page"))
    # the regex does see the reads, through every one of the names (3.7k: the request row's own
    # fields — tokens, cache_read, job_id, http_status — moved with the table into
    # app.js's shared renderer, whose `req` walk checks them there; 3.10b: the job row's fields
    # — started_at among them — moved the same way, into app.js's `job` walk)
    assert {"hosts", "endpoints", "now", "state", "fallback", "hardware", "rates", "gpu_mem_total", "ram_total",
            "prompt_tps", "cached_tps", "t", "jobs", "events", "requests", "totals",
            "count", "cache_creation_tokens", "errors",
            "kind", "detail", "dropped_job_id", "last_seen", "base_url",
            "host", "last_model", "last_context"} <= fields
    missing = sorted(f for f in fields if not re.search(r"(?<![A-Za-z_])" + re.escape(f) + r"(?![A-Za-z_])", spec))
    assert missing == []


def test_endpoint_js_draws_one_tile_per_history_series():
    """The tier table's history-drawn tiles name exactly the numeric series §5a's endpoint
    history serves (besides t, step, state: the strip, events and jobs: the tables)."""
    from localagents.monitor import read
    series = re.findall(r'series: "([a-z_]+)"', _endpoint_js())
    expected = ["latency_ms", "prompt_tps", "gen_tps", "cache_hit", *read._GAUGES, *read._HW_SERIES]
    assert sorted(series) == sorted(expected)
    assert len(series) == len(set(series))


def test_endpoint_js_reads_the_mb_fields_as_megabytes_per_sample():
    """Story 3.2a ruling F: Strata's ``_mb`` hardware fields are megabytes per sample over a
    window Strata does not document, so their tiles read ``MB``, never a rate, and each carries
    the title that says so. The DOM is the browser smoke's; this pins the definitions."""
    js = _endpoint_js()
    assert "MB/s" not in js
    assert 'var PER_SAMPLE = "per Strata sample; the sample window is not documented";' in js
    mb = re.findall(r'series: "([a-z_]+_mb)", hint: PER_SAMPLE,', js)
    assert sorted(mb) == ["disk_read_mb", "disk_write_mb", "gpu_pcie_rx_mb", "gpu_pcie_tx_mb"]


def test_endpoint_js_registers_through_the_pages_hook_and_never_polls():
    js = _endpoint_js()
    assert "M.pages.endpoint = endpointPage;" in js
    assert "M.endpointPage = { tilesFor: tilesFor" in js
    assert "setInterval" not in js and "EventSource" not in js  # app.js owns the stream
    assert "innerHTML" not in js


def test_endpoint_page_stays_live_on_the_snapshot_hook_not_a_dead_element():
    """3.9D D4: the page watched #updated, whose phrase moved to #conn-label in 3.7r, so the
    element never mutated again and the endpoint pane froze mid-sentence. Liveness is now the
    onSnapshot hook app.js calls at the end of render() — on the skip path too, where only the
    clock-relative phrases move. The dead span is gone from the shell."""
    ep = _endpoint_js()
    assert "M.onSnapshot(onSnapshot)" in ep
    assert 'watch("updated"' not in ep
    assert 'id="updated"' not in (STATIC / "index.html").read_text()
    app = _app_js()
    assert "onSnapshot: onSnapshot," in app
    render = app[app.index("function render(snap) {"):app.index("function getJSON(url)")]
    # the notifier on both exits: the skip path's early return and the full path's end
    assert render.count("snapshotListeners.slice().forEach") == 2


def test_every_element_id_a_page_script_names_exists_in_the_shell():
    """3.9D D4's gate: the frozen page watched a name the shell no longer drew, and a missing
    element is exactly what watch()/getElementById() fail silent on. Every literal id a page
    script reads or watches must exist in index.html."""
    ids = set(re.findall(r'id="([^"]+)"', (STATIC / "index.html").read_text()))
    assert "updated" not in ids  # the dead span, removed 3.9D D4 (the phrase lives in #conn-label)
    for name in ("app.js", "endpoint.js", "job.js"):
        js = (STATIC / name).read_text()
        used = (re.findall(r'\bbyId\("([^"]+)"\)', js)
                + re.findall(r'getElementById\("([^"]+)"\)', js)
                + re.findall(r'\bwatch\("([^"]+)"', js))
        missing = sorted(set(used) - ids)
        assert missing == [], (name, missing)
    # job.js draws inside the container route() hands it and names no shell element; the other
    # two must have reads, or the extraction above silently matched nothing
    assert re.findall(r'\bbyId\("([^"]+)"\)', _app_js()), "app.js byId reads vanished"
    assert re.findall(r'\bwatch\("([^"]+)"', _endpoint_js()), "endpoint.js watch reads vanished"


def test_route_tears_the_detail_page_down_on_every_route_change():
    """app.js's route() calls the open detail page's teardown before it branches, so going back
    to the overview tears the page down too (story 3.3 found it ran only between detail
    pages, leaving the endpoint page's observers and fetches alive on the overview)."""
    js = _app_js()
    body = js[js.index("function route() {"):js.index("// --- start")]
    assert body.count("pages.teardown()") == 1
    assert body.index("pages.teardown()") < body.index("if (!m) {")


def test_route_decodes_the_name_in_a_try_before_it_tears_down():
    """Story 3.2a ruling E: a malformed escape (``#/endpoint/ai%zz``) makes decodeURIComponent
    throw URIError; route() decodes inside a try with the raw text as the fallback, before the
    teardown and the visibility flips, so the pane never blanks (on a cold load the throw
    stopped start() before the stream connected). The DOM is the browser smoke's."""
    js = _app_js()
    body = js[js.index("function route() {"):js.index("// --- start")]
    decode = "try { name = decodeURIComponent(m[2]); } catch (e) { name = m[2]; }"
    assert body.count("decodeURIComponent") == 1 and decode in body
    assert body.index(decode) < body.index("pages.teardown()") < body.index("overview.hidden")


def test_app_js_offers_the_server_ranges():
    from localagents.monitor import ranges
    js = _app_js()
    m = re.search(r"var RANGES = \[([^\]]*)\];", js)
    assert m is not None
    assert re.findall(r'"([^"]+)"', m.group(1)) == ranges.RANGES
    assert f'var DEFAULT_RANGE = "{ranges.DEFAULT_RANGE}";' in js


def test_the_page_loads_nothing_from_the_internet():
    """No CDN, no font service at runtime: the only absolute URL in the page's own files is the
    SVG namespace (an identifier, never fetched)."""
    for name in ("index.html", "app.js", "app.css", "endpoint.js"):
        urls = re.findall(r"https?://[^\s\"')]+", (STATIC / name).read_text())
        assert set(urls) <= {"http://www.w3.org/2000/svg"}, (name, urls)


def test_the_page_never_polls():
    """Spec §5: the page redraws from the event stream and never polls. History is fetched in
    response to a snapshot (or a range change), never on an interval timer."""
    js = _app_js()
    assert "setInterval" not in js
    assert 'new EventSource("/events")' in js


def test_the_page_fetches_the_overview_history_in_one_batched_call():
    """Story 3.7p: the overview's history fetch is ONE call (fleet plus every live endpoint's
    own history), not the fleet call plus one per endpoint; the per-endpoint functions stay
    for the endpoint page."""
    js = _app_js()
    assert "/api/overview/history?range=" in js
    assert "function fetchOverviewHistory(range)" in js
    assert "fetchHistory(n, range)" not in js  # the per-endpoint loop is gone from the overview
    assert "function fetchHistory(name, range)" in js  # but the helper stays


# --- item 3: the page over the seeded world, served by the command ------------------------------

_SRC = Path(__file__).resolve().parent.parent / "src"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _seeded_monitor(tmp_path):
    """``localagents monitor --no-collect`` in a subprocess over a seeded file (now = the wall
    clock); yields (base URL, the page response, the process) once ``/`` answers; kills the
    process on the way out if the test did not stop it."""
    dbpath = tmp_path / "seeded.db"
    seed_file(dbpath, now=time.time())
    cfg = write_config(tmp_path / "models.yaml")
    port = _free_port()
    code = ("import sys; sys.argv=['localagents', 'monitor', '--no-collect', '--no-open', '--port', " + repr(str(port))
            + "]; import localagents; localagents.main()")
    env = {**os.environ, "LOCALAGENTS_DB": str(dbpath), "LOCALAGENTS_CONFIG": str(cfg),
           "XDG_STATE_HOME": str(tmp_path / "state")}
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=_SRC, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        page = None
        while time.monotonic() < deadline:
            try:
                page = httpx.get(base + "/", timeout=1)
                break
            except httpx.HTTPError:
                assert proc.poll() is None, proc.stderr.read()
                time.sleep(0.1)
        assert page is not None, "the monitor did not answer within 10 s"
        yield base, page, proc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_the_command_serves_the_page_and_its_files_over_the_seeded_world(tmp_path):
    """``localagents monitor --no-collect`` over a seeded file (now = the wall clock): the page
    and its static files answer 200 over loopback with their content types, and the
    routes the page calls on load (the snapshot, the fleet history, a live endpoint's history)
    answer 200; SIGTERM then exits 0."""
    with _seeded_monitor(tmp_path) as (base, page, proc):
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        for path in SHELL_ASSETS:
            assert f'"{path}"' in page.text
            r = httpx.get(base + path, timeout=5)
            assert r.status_code == 200, path
            assert ("text/css" if path.endswith(".css") else "javascript") in r.headers["content-type"], path
            assert r.content == (STATIC / path.removeprefix("/static/")).read_bytes(), path
        snap = httpx.get(base + "/api/snapshot", timeout=5).json()
        assert [e["state"] for h in snap["hosts"] for e in h["endpoints"]] == ["live", "down", "dormant"]
        fleet = httpx.get(base + "/api/fleet/history?range=1h", timeout=5)
        assert fleet.status_code == 200 and fleet.json()["t"]
        hist = httpx.get(base + "/api/endpoints/ai1/history?range=1h", timeout=5)
        assert hist.status_code == 200 and "state" in hist.json()
        # the job detail route answers the seeded finished job (the story 3.4 page's source)
        jobs = httpx.get(base + "/api/jobs", timeout=5).json()
        finished = [j for j in jobs if j["status"] == "succeeded"]
        assert len(finished) == 1
        r = httpx.get(base + f"/api/jobs/{finished[0]['id']}", timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "succeeded"
        assert len(body["requests"]) == 112 and len(body["tool_call_log"]) == 142
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0


def test_the_command_serves_the_endpoint_page_and_the_three_calls_it_makes_for_ai1(tmp_path):
    """Story 3.3 over the seeded world served by the command: ``/`` loads endpoint.js, which
    answers 200 with its bytes, and the three routes the endpoint page reads for ai1 (the
    snapshot, its history, its requests) answer 200 with what its tiles and tables draw from:
    ai1 in the snapshot, a series of every tier in the history, the 152 seeded requests with
    totals.count 152. The DOM itself is the browser smoke's."""
    with _seeded_monitor(tmp_path) as (base, page, proc):
        assert page.status_code == 200 and '"/static/endpoint.js"' in page.text
        js = httpx.get(base + "/static/endpoint.js", timeout=5)
        assert js.status_code == 200 and "javascript" in js.headers["content-type"]
        assert js.content == (STATIC / "endpoint.js").read_bytes()
        snap = httpx.get(base + "/api/snapshot", timeout=5)
        assert snap.status_code == 200
        assert "ai1" in [e["name"] for h in snap.json()["hosts"] for e in h["endpoints"]]
        hist = httpx.get(base + "/api/endpoints/ai1/history?range=24h", timeout=5)
        assert hist.status_code == 200
        assert {"latency_ms", "gen_tps", "slots_busy", "gpu_util", "state", "events", "jobs"} <= set(hist.json())
        reqs = httpx.get(base + "/api/endpoints/ai1/requests?range=24h", timeout=5)
        assert reqs.status_code == 200
        assert len(reqs.json()["requests"]) == 152 and reqs.json()["totals"]["count"] == 152
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0


def test_a_down_node_card_head_says_down_and_keeps_the_probe_sentence_in_the_title():
    """3.7x (operator 2026-10-30): "We dont' need the exposition on ai9 card - 'down' is a good
    summary state." The host head's note is the state word; the probe's sentence ("… timed out
    (host off, or a firewall dropping packets)") rides in the note's title, never as its text,
    so it no longer sets the card column's floor (3.7u)."""
    js = _app_js()
    head = js[js.index("function hostCard("):js.index("// --- the Recent jobs log")]
    assert re.search(r'var note = nodeDown \? "down" : host\.state === "unknown" \? "unknown" : ""', head), head
    assert "if (said) note = said.error" not in head
    assert re.search(r'class: "host-note ep-line", title: \w+', head), head
    assert 'text: note' in head


# --- story 3.7v: the two error figures, and the handshake row told apart --------------------------


def test_requests_table_labels_a_handshake_row_as_no_error():
    """3.7v (operator 2026-10-30: "tool errors sometimes shows a count of 2 when the job only
    shows 1 error returned (often the /api/hello…)"): the shim's recorded startup handshake
    rows (path /api/hello, the backend's 404/501) drew as the table's one red error pill.
    The shared renderer draws them as a muted pill reading "handshake" — the numeric status
    stays in the title — and pins the new class styled in the stylesheet. The row class and the
    counting exclusions are the committed DOM smoke's (tools/smoke/handshake.js; F3)."""
    body = _js_function(_app_js(), "requestsTable")
    assert '"handshake"' in body
    assert "pill-muted" in body
    assert '"/api/hello"' in body
    assert re.search(r"\.pill-muted(?![\w-])", (STATIC / "app.css").read_text())
    # F3 (fold of the page-batch review, 2026-10-30): the handshake row takes no req-err mark
    # (the wrong implementation kept the muted pill but pushed req-err anyway), and the
    # handshake branch is chosen before the null-status branch (a handshake with no status is
    # a handshake, not the "–" dash)
    assert 'if (bad && !handshake) cls.push("req-err");' in body, body
    assert body.index("if (handshake)") < body.index("else if (req.http_status == null)"), body


def test_job_header_names_both_error_figures():
    """3.7v: the job page's header says which errors it means — the counts of the overview's
    "tool errors" column (tool results flagged is_error) and the request table's failures
    sit side by side under their own words; the bare "errors" figure is gone."""
    js = (STATIC / "job.js").read_text()
    assert 'fig("tool errors"' in js
    assert 'fig("request errors"' in js
    assert 'fig("errors"' not in js
    # F4 (fold of the page-batch review, 2026-10-30): the figure is computed from the rows —
    # both of the shared renderer's reads, with /api/hello skipped — not a constant that merely
    # keeps the label (tools/smoke/handshake.js exercises the row rule the count must follow)
    body = js[js.index("function requestErrors("):]
    body = body[:body.index("\n    function ")]
    assert 'req.path === "/api/hello"' in body, body
    assert "req.http_status" in body, body
    assert "req.error" in body, body
    assert "n++" in body and "return n;" in body, body
    assert "return 0;" not in body, body


def test_a_never_seen_endpoint_is_hidden_on_a_down_node_which_then_gets_no_card():
    """3.7y (operator 2026-10-30: "ai9 is 'down' yes, but it's also 'never seen'. Perhaps it
    shouldn't have a card in that state?"): the 3.7a exception is dropped — a never-seen
    endpoint is hidden on a down node too, a host with nothing drawn (down included) has no
    card, and the hidden-count line names the node's state so the alert is not lost."""
    js = _app_js()
    # neverSeenOn: the `host.state !== "down"` clause is gone; the body speaks only of the
    # endpoint (a never-seen endpoint is never drawn, whatever the node says)
    never = js[js.index("function neverSeenOn("):js.index("function hostCard(")]
    assert "host.state" not in never, never
    # F5 (fold of the page-batch review, 2026-10-30): BOTH reads stay — the dormant state and
    # the null last_seen. `return ep.last_seen == null;` (drop the state test) would hide a
    # `down` endpoint that never probed up but is seen by its error, whose card the oracle says
    # must stay (tools/smoke/neverseen.js case (d) renders it)
    assert 'ep.state === "dormant"' in never, never
    assert "ep.last_seen == null" in never, never
    # hostCard: an empty body returns null for ANY host — no `&& !nodeDown` rescue
    head = js[js.index("function hostCard("):js.index("// --- the Recent jobs log")]
    assert re.search(r"if \(!body\.length\) return null;", head), head
    assert "!body.length && !nodeDown" not in head
    # the hidden list names the node's state: a hidden endpoint on a down node reads
    # "<name> (node down)", on an unknown node "<name> (node unknown)"
    assert re.search(r'hidden\.push\(ep\.name \+ \(host\.state === "down" \? " \(node down\)"', js), js
    assert "(node unknown)" in js


def test_an_unknown_node_is_an_alert_like_a_down_node():
    """F1 (fold of the page-batch review, 2026-10-30): spec §4's True-now sentence says the
    head's alert is "down" (or "unknown") and the probe's sentence rides in that note's title,
    so an unknown node is quiet the same way. The code gated both the title source and the
    endpoint's quiet flag on `nodeDown` alone: an unknown node drew the sentence as the
    endpoint's `.ep-error` text and lifted the column floor."""
    js = _app_js()
    head = js[js.index("function hostCard("):js.index("// --- the Recent jobs log")]
    assert re.search(r'var alert = host\.state === "down" \|\| host\.state === "unknown"', head), head
    assert re.search(r"var said = alert \?", head), head
    assert re.search(r"downEndpoint\(snap, ep, alert\)", head), head
    assert "var said = nodeDown" not in head, head
    assert "downEndpoint(snap, ep, nodeDown)" not in head, head


# --- story 3.10d: the endpoint tiles (no tier headings, no state tiles, no cut values) ------------


def test_endpoint_page_draws_no_tier_numbered_headings():
    """3.10d (operator 2026-10-30: "I think also drop the 'tier x' - nobody cares - go with
    better groupings and labels. Don't be chatty."): no ``tier N: …`` heading string survives
    in endpoint.js, and the groups the grid labels speak plain nouns — none above the first
    group (the snapshot's own figures), then speeds, occupancy, hardware. The ``tier-N``
    CLASSES stay on the tiles (the stylesheet's border colours read them; ENDPOINT_CLASSES
    pins them); the taxonomy of data sources is the spec's, not the page's prose."""
    js = _endpoint_js()
    literals = {lit for lit in re.findall(r'"([^"\n]*)"', js)}
    assert [lit for lit in sorted(literals) if "tier " in lit] == []
    for noun in ("speeds", "occupancy", "hardware"):
        assert noun in literals, noun


def test_tilesFor_draws_no_endpoint_or_node_tile():
    """3.10d A (operator: "Endpoint overview - 'endpoint': Up, 'node': Up. … Redundant."):
    tilesFor pushes no endpoint-state tile and no node tile — the header says both, and now
    carries the port-22 fallback clause the node tile's note used to hold. The served-model
    and context-window tiles stay."""
    body = _js_function(_endpoint_js(), "tilesFor")
    assert 'key: "state"' not in body
    assert 'key: "node"' not in body
    assert 'label: "endpoint"' not in body and 'label: "node"' not in body
    assert 'key: "model"' in body and 'key: "context"' in body  # the survivors


def test_a_tile_value_is_never_cut():
    """3.10d C (the 3.7u rule, same words as the cards: "I don't like the '...' convention
    because it hides information"): ``.tile-value`` clips nothing — no text-overflow, no
    overflow — and keeps the one line; the tile grows to its value (app.css:193's old rule
    ellipsised a long served-model id "when the card could just be wider")."""
    css = (STATIC / "app.css").read_text()
    bodies = [body for sel, body in _css_rules(css) if sel.strip() == ".tile-value"]
    assert bodies, "the .tile-value rule is gone entirely"
    for body in bodies:
        assert not re.search(r"overflow", body), body
        assert "white-space: nowrap" in body, body
