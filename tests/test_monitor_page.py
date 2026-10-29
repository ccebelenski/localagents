"""Story 3.2: the overview page (monitor spec §5 "Overview"). The page is one HTML file, one
JavaScript file and one stylesheet beside a vendored uPlot, served from the package's
``monitor/static/`` directory with no CDN at runtime.

There is no JavaScript runtime in the suite: these tests pin what Python can see (the shell's
static references, the vendored files' checksums, the class inventory the browser smoke looks
for, and that ``app.js`` reads only §5a field names). The DOM itself is the browser smoke's
(spec §7)."""
import asyncio
import contextlib
import hashlib
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

# the shell loads exactly these, in this order (stylesheets first, uPlot before the page
# scripts; the detail pages register themselves after app.js, stories 3.3 and 3.4 in their own
# files)
SHELL_ASSETS = ["/static/uPlot.min.css", "/static/app.css", "/static/uPlot.iife.min.js", "/static/app.js",
                "/static/endpoint.js", "/static/job.js"]


@pytest.fixture
def client(tmp_path):
    p = tmp_path / "seeded.db"
    seed_file(p)
    app = app_mod.make_app(lambda: db.open_db(p), registry(tmp_path), MonitorSettings(), asyncio.Event(),
                           now=lambda: NOW)
    with TestClient(app) as c:
        yield c


# --- item 1: the shell and the vendored uPlot ---------------------------------------------------


def test_the_shell_references_exactly_the_page_static_files(client):
    html = client.get("/").text
    refs = [r for r in re.findall(r'(?:src|href)="([^"]+)"', html) if not r.startswith("#")]  # in-page links
    assert refs == SHELL_ASSETS
    assert "//" not in "".join(refs)  # nothing off the package: no CDN, no scheme-relative URL


@pytest.mark.parametrize("path, kind", [
    ("/static/uPlot.min.css", "text/css"),
    ("/static/app.css", "text/css"),
    ("/static/uPlot.iife.min.js", "javascript"),
    ("/static/app.js", "javascript"),
    ("/static/endpoint.js", "javascript"),
    ("/static/job.js", "javascript"),
])
def test_each_static_file_is_served_with_its_content_type(client, path, kind):
    r = client.get(path)
    assert r.status_code == 200
    assert kind in r.headers["content-type"]
    assert r.content == (STATIC / path.removeprefix("/static/")).read_bytes()


def test_vendor_md_sha256_lines_match_the_vendored_files():
    text = (STATIC / "VENDOR.md").read_text()
    lines = re.findall(r"^sha256 ([0-9a-f]{64})  (\S+)$", text, re.M)
    assert sorted(name for _, name in lines) == ["uPlot.iife.min.js", "uPlot.min.css"]
    for digest, name in lines:
        assert hashlib.sha256((STATIC / name).read_bytes()).hexdigest() == digest, name
    assert "uPlot 1.6.31" in text
    assert "(v1.6.31)" in (STATIC / "uPlot.iife.min.js").read_text()[:200]


# --- item 2: the page's class inventory and its reads of the §5a JSON ----------------------------

SPEC = Path(__file__).resolve().parent.parent / "docs" / "superpowers" / "specs" / "2026-10-29-monitor-design.md"

# the classes the browser smoke and the spec name (story 3.2's brief)
SMOKE_CLASSES = ["host", "endpoint", "state-live", "state-down", "state-dormant", "node-down", "node-unknown",
                 "jobs", "fleet", "range"]
# the classes the job page's smoke names (story 3.4's brief): the page root and its sections,
# the status classes the header's mark takes (a queued job takes one more, styled but not
# smoked)
JOB_SMOKE_CLASSES = ["job-detail", "gauge", "timeline", "by-tool", "job-requests", "transcript", "files",
                     "status-running", "status-succeeded", "status-failed", "status-timeout",
                     "status-cancelled", "status-lost"]

# the names app.js reads the route bodies through (its header says so): snapshot, host, endpoint,
# running job, endpoint hardware, endpoint rates, fleet history, one endpoint's fleet series,
# endpoint history
APP_JS_VARS = ("snap", "host", "ep", "job", "hw", "rates", "fleet", "fleetEp", "hist")
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
    strings = re.findall(r'class="([^"]*)"|class: "([^"]*)"|cls \+?= "([^"]*)"|"(node-[a-z]+)"|"(status-[a-z]+)"', text)
    classes = {c for groups in strings for s in groups for c in s.split()}
    css = (STATIC / "app.css").read_text()
    for cls in SMOKE_CLASSES + JOB_SMOKE_CLASSES:
        assert cls in classes, cls
        assert re.search(r"\." + re.escape(cls) + r"(?![\w-])", css), cls


def _read_fields(js_text, vars):
    """The field names read through the given variable names, the allow-list of the regex's
    array-method false positives removed."""
    fields = set()
    for m in re.finditer(r"(?<![\w.$])(" + "|".join(vars) + r")((?:\.[A-Za-z_$][\w$]*)+)", js_text):
        fields |= set(m.group(2).split(".")[1:])
    return fields - JS_BUILTINS


def test_app_js_reads_only_section_5a_field_names():
    """Every property chain read off a route body (``ep.rates.gen_tps``, ``fleet.endpoints``,
    ``detail.requests``, ``job.server_live``) names keys §5a names, so a field the page
    invents (or a renamed one) fails here. Walked per page file, each through the names its
    header declares (app.js: JSON_VARS, job.js: detail/snap/req/tc/job)."""
    spec = _section_5a()
    for name, vars in PAGE_VARS.items():
        fields = _read_fields((STATIC / name).read_text(), vars)
        # the regex does see the reads (a guard against a pattern that matches nothing)
        assert {"hosts", "endpoints", "jobs", "rates", "gen_tps", "state", "t", "server_live", "dropped_job"} <= fields \
            if name == "app.js" else \
            {"requests", "tool_call_log", "ctx_tokens", "usage", "metrics", "tool_counts",
             "files_touched", "transcript_tail", "elapsed_s", "server_live"} <= fields, name
        missing = sorted(f for f in fields if not re.search(r"(?<![A-Za-z_])" + re.escape(f) + r"(?![A-Za-z_])", spec))
        assert missing == [], (name, missing)


# --- story 3.3: the endpoint detail page (endpoint.js) -------------------------------------------

# the classes the endpoint page's browser smoke names (story 3.3's brief)
ENDPOINT_CLASSES = ["endpoint-detail", "tiles", "tile", "tier-0", "tier-1", "tier-2", "tier-3", "requests", "totals",
                    "events"]

# the names endpoint.js reads the route bodies through (its header says so): snapshot, host,
# snapshot endpoint, endpoint hardware, endpoint rates, endpoint history, one history job, one
# history event, the requests body, one request row, the requests totals
ENDPOINT_JSON_VARS = ("snap", "host", "ep", "hw", "rates", "hist", "job", "ev", "reqs", "req", "totals")


def _endpoint_js():
    return (STATIC / "endpoint.js").read_text()


def test_endpoint_js_carries_every_class_the_smoke_names():
    """Each class is a whole word inside a string literal of endpoint.js (the tier classes are
    picked from a list by tier), and the stylesheet styles it."""
    js = _endpoint_js()
    tokens = {c for lit in re.findall(r'"([^"\n]*)"', js) for c in lit.split()}
    css = (STATIC / "app.css").read_text()
    for cls in ENDPOINT_CLASSES:
        assert cls in tokens, cls
        assert re.search(r"\." + re.escape(cls) + r"(?![\w-])", css), cls


def test_endpoint_js_reads_only_section_5a_field_names():
    """The §5a field-name check of app.js, over endpoint.js with its own variable names."""
    spec = _section_5a()
    fields = set()
    for m in re.finditer(r"(?<![\w.$])(" + "|".join(ENDPOINT_JSON_VARS) + r")((?:\.[A-Za-z_$][\w$]*)+)",
                         _endpoint_js()):
        fields |= set(m.group(2).split(".")[1:])
    fields -= JS_BUILTINS
    # the regex does see the reads, through every one of the names
    assert {"hosts", "endpoints", "now", "state", "fallback", "hardware", "rates", "gpu_mem_total", "ram_total",
            "prompt_tps", "cached_tps", "t", "jobs", "events", "requests", "totals", "tokens", "cache_read",
            "job_id", "job_name", "http_status", "count", "cache_creation_tokens", "errors", "started_at",
            "kind", "detail", "dropped_job", "last_seen", "base_url"} <= fields
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


def test_endpoint_js_registers_through_the_pages_hook_and_never_polls():
    js = _endpoint_js()
    assert "M.pages.endpoint = endpointPage;" in js
    assert "M.endpointPage = { tilesFor: tilesFor" in js
    assert "setInterval" not in js and "EventSource" not in js  # app.js owns the stream
    assert "innerHTML" not in js


def test_route_tears_the_detail_page_down_on_every_route_change():
    """app.js's route() calls the open detail page's teardown before it branches, so going back
    to the overview tears the page down too (story 3.3 found it ran only between detail
    pages, leaving the endpoint page's observers and fetches alive on the overview)."""
    js = _app_js()
    body = js[js.index("function route() {"):js.index("// --- start")]
    assert body.count("pages.teardown()") == 1
    assert body.index("pages.teardown()") < body.index("if (!m) {")


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
    code = ("import sys; sys.argv=['localagents', 'monitor', '--no-collect', '--port', " + repr(str(port))
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
    and its five static files answer 200 over loopback with their content types, and the
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
