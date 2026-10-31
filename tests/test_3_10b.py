"""Story 3.10b: one jobs table and one requests table, drawn by one renderer each, filtered per
page (the operator, EPICS 3.10: "that table should be the same table from the primary page, just
filtered to this endpoint - no need to have two different tables - and the code should be
re-used. What is 'events'? I have none. Requests table should re-use the one from the job page
with an appropriate filter for this page. Re-use code. Summary line for that table is fine. …
Don't be chatty.").

Server side (A): ``endpoint_history``'s ``jobs`` stop being the five-key listing and become full
§5a job rows — the snapshot's ``_JOB_SELECT`` + ``_job_entry``, restricted to the endpoint and to
jobs overlapping the range (the overlap predicate factored out of ``jobs_overlapping``), ordered
as the overview's log (live first, then newest finished). The queries ride ``jobs_endpoint_id``
(the 3.9E audit's shape: an EXPLAIN pin in the style of tests/test_query_plans.py).

Page side (B, C): app.js's ``jobsTable(rows, opts)`` is the one renderer both pages call (opts:
``empty``, ``note``, ``omit``); endpoint.js drops its own ``JOB_COLUMNS`` and table and draws
through ``M.jobsTable`` with ``omit: ["endpoint"]``; every heading on the endpoint page is a noun
phrase carrying the filter ("Jobs · ai1 · last 24h"), and the events section exists only when the
range holds an event ("Restarts and outages · …").
"""
import re
from pathlib import Path

import pytest

from localagents import db
from localagents.monitor import read

from monitor_seeded import FX, NOW, endpoint_id, registry, seed_file  # noqa: F401 (FX, registry)

STATIC = Path(__file__).resolve().parent.parent / "src" / "localagents" / "monitor" / "static"
WINDOW_S = 24 * 3600.0
HOUR = 3600.0


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "seeded.db"
    seed_file(path)
    conn = db.open_db(path)
    yield conn, registry(tmp_path)
    conn.close()


@pytest.fixture(autouse=True)
def _empty_finished_cache():
    """Same guard as test_monitor_read.py: the finished-entry cache is module-level and every
    test reads its own scratch world."""
    read._FINISHED_CACHE.clear()
    yield


# --- A: the history body's jobs are §5a job rows -----------------------------------------------


def test_history_jobs_rows_have_the_snapshot_job_row_key_set(world):
    """The history body's ``jobs`` entries carry exactly the §5a ``running_job`` keys — compared
    live against a snapshot row's keys from the same seeded world, so the two can never drift."""
    conn, reg = world
    h = read.endpoint_history(conn, endpoint_id(conn, "ai1"), NOW - HOUR, NOW)
    snap = read.overview(conn, reg, NOW, WINDOW_S)
    assert h["jobs"], "the seeded world has two jobs on ai1"
    snap_keys = set(snap["jobs"][0])
    assert all(set(j) == snap_keys for j in h["jobs"])


def test_history_job_rows_answer_field_for_field_like_the_snapshot(world):
    """Not just the key set: the same §5a row for the same job. Both sides ran ``_job_entry`` at
    the same clock, so the dicts must be equal — the figures (tool_calls, ctx, the two speeds)
    included, not just the five keys the old listing carried."""
    conn, reg = world
    h = read.endpoint_history(conn, endpoint_id(conn, "ai1"), NOW - HOUR, NOW)
    snap = {j["id"]: j for j in read.overview(conn, reg, NOW, WINDOW_S)["jobs"]}
    assert len(h["jobs"]) == 2
    for j in h["jobs"]:
        assert j == snap[j["id"]]


def test_history_jobs_are_ordered_live_first_then_newest_finished(world):
    """The overview's log order, not the old oldest-started-first: the live job first, then the
    finished tail newest-finished first (the endpoint page draws through the same renderer and
    must not re-sort)."""
    conn, _ = world
    h = read.endpoint_history(conn, endpoint_id(conn, "ai1"), NOW - HOUR, NOW)
    assert [j["status"] for j in h["jobs"]] == ["running", "succeeded"]


def test_history_jobs_are_the_endpoint_own_overlapping_the_range(world):
    """Only this endpoint's jobs (the other endpoint's overlapping job must not ride in), only
    overlapping ones (a job finished before the range opens is out), and — the predicate
    ``jobs_overlapping`` already states — a job that never started has no span to overlap, so a
    queued job with no ``started_at`` is out of the history body."""
    conn, _ = world
    ai1 = endpoint_id(conn, "ai1")
    dgx = endpoint_id(conn, "dgx1-a")
    with db.immediate(conn):
        conn.execute(
            "INSERT INTO jobs (id, endpoint_id, status, created_at, started_at, finished_at, updated)"
            " VALUES ('other-ep', ?, 'succeeded', ?, ?, ?, ?)", (dgx, NOW - 500, NOW - 400.0, NOW - 300.0, NOW - 300.0))
        conn.execute(
            "INSERT INTO jobs (id, endpoint_id, status, created_at, started_at, finished_at, updated)"
            " VALUES ('too-old', ?, 'succeeded', ?, ?, ?, ?)", (ai1, NOW - 7200, NOW - 7100.0, NOW - 7000.0, NOW - 7000.0))
        conn.execute(
            "INSERT INTO jobs (id, endpoint_id, status, created_at, started_at, finished_at, updated)"
            " VALUES ('never-started', ?, 'queued', ?, NULL, NULL, ?)", (ai1, NOW - 50.0, NOW - 50.0))
    h = read.endpoint_history(conn, ai1, NOW - HOUR, NOW)
    assert [j["id"] for j in h["jobs"]] != []
    ids = {j["id"] for j in h["jobs"]}
    assert "other-ep" not in ids and "too-old" not in ids and "never-started" not in ids
    assert all(j["endpoint"] == "ai1" for j in h["jobs"])


# --- the query plan: the endpoint's jobs ride jobs_endpoint_id (3.9E's standing audit) ---------


@pytest.fixture
def plans_db(tmp_path):
    """A fresh current-schema file with a handful of hand-inserted jobs, shaped like the 3.9E
    probe fixture in tests/test_query_plans.py."""
    c = db.open_db(tmp_path / "plans.db")
    with db.immediate(c):
        c.execute("INSERT INTO endpoints (name, base_url, backend, host)"
                  " VALUES ('p1', 'http://10.0.0.9:8080', 'llamacpp', 'p1')")
    rows = [
        ("j1", "succeeded", 100.0, 101.0, 160.0),
        ("j2", "failed", 110.0, 111.0, 115.0),
        ("j3", "running", 160.0, 200.0, None),
        ("j4", "queued", 170.0, None, None),
    ]
    for jid, status, created, started, finished in rows:
        c.execute(
            "INSERT INTO jobs (id, endpoint_id, status, created_at, started_at, finished_at, updated)"
            " VALUES (?, 1, ?, ?, ?, ?, ?)",
            (jid, status, created, started, finished, started or created),
        )
    yield c
    c.close()


def _plan(c, sql, params=()):
    return [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql, params)]


def test_history_job_queries_seek_jobs_endpoint_id(plans_db):
    """Story 3.10b: the history's job SELECTs filter on ``jobs.endpoint_id = ?``; the plan must
    name the index (``jobs_endpoint_id``, db.py's CREATE INDEX), not walk the job history. Both
    the live and the finished side."""
    for tail in (read._ENDPOINT_JOBS_LIVE_TAIL, read._ENDPOINT_JOBS_FINISHED_TAIL):
        plan = _plan(plans_db, read._JOB_SELECT + tail, (1, 250.0, 150.0))
        assert any("SEARCH j USING INDEX jobs_endpoint_id" in d for d in plan), plan


def test_history_and_timeline_share_the_overlap_predicate(plans_db):
    """The predicate is factored, not copied: one fragment names the window terms, the two
    history tails embed it verbatim, and ``read.py``'s source uses it in three statements (the
    timeline's ``jobs_overlapping`` and the two history SELECTs). The fragment itself answers
    the window: j1 (finished inside) and j3 (unfinished, started inside) overlap [150, 250];
    j2 ended before it; j4 never started."""
    frag = read._OVERLAP_WHERE
    for tail in (read._ENDPOINT_JOBS_LIVE_TAIL, read._ENDPOINT_JOBS_FINISHED_TAIL):
        assert frag in tail
    src = Path(read.__file__).read_text()
    # the definition plus one use per builder: timeline, live tail, finished tail
    assert src.count("_OVERLAP_WHERE") >= 4, src.count("_OVERLAP_WHERE")
    ids = [r["id"] for r in plans_db.execute("SELECT j.id FROM jobs j WHERE" + frag, (250.0, 150.0))]
    assert sorted(ids) == ["j1", "j3"]  # the bare fragment has no ORDER BY: unordered


# --- B and C: the static pins -----------------------------------------------------------------


def _app_js():
    return (STATIC / "app.js").read_text()


def _endpoint_js():
    return (STATIC / "endpoint.js").read_text()


def test_app_js_jobs_table_takes_rows_and_opts():
    """One renderer, one entry shape: ``jobsTable(rows, opts)`` — not ``jobsTable(snap)`` — and
    it reads ``opts.omit`` for the per-page column drop, ``opts.empty`` and ``opts.note``."""
    js = _app_js()
    assert "function jobsTable(rows, opts) {" in js
    assert "opts.omit" in js
    assert "opts.empty" in js
    assert "opts.note" in js
    assert "jobsTable: jobsTable," in js  # exported for endpoint.js


def test_endpoint_js_draws_jobs_through_the_shared_renderer():
    """endpoint.js's own ``JOB_COLUMNS`` and table call are gone; the section calls
    ``M.jobsTable`` and omits the endpoint column (every row is this endpoint)."""
    js = _endpoint_js()
    assert "JOB_COLUMNS" not in js
    assert "M.jobsTable(" in js
    assert 'omit: ["endpoint"]' in js


def test_endpoint_page_headings_are_noun_phrases_with_the_filter():
    """No parentheses of explanation (the operator: "Don't be chatty."): every heading is
    ``Noun · endpoint · last range``; the old parenthesised strings are gone."""
    js = _endpoint_js()
    for wanted in ("Jobs · ", "Requests · ", "Restarts and outages · "):
        assert wanted in js, wanted
    for gone in ("Jobs on it", "our jobs only", "Events ("):
        assert gone not in js, gone


def test_events_section_is_absent_when_the_range_holds_none():
    """The events section returns null when the range holds no event (and the page's draw
    tolerates the null); the old "no events in this range" sentence is gone."""
    js = _endpoint_js()
    assert "no events in this range" not in js
    assert "if (!evs) return null;" in js
