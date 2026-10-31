"""Story 3.10f-2 (ruling (o): the job's one key is its UUID — the monitor side). 3.10f-1 dropped
``jobs.name`` and left the JSON keys ``name``/``dropped_job``/``job_name`` carrying the id so the
pages kept running; this story removes those keys from every read shape and shows the short id
(``id.slice(0, 8)``, full id in the title) on the pages. The shapes are seeded-world reads (the
same world as tests/test_monitor_read.py); the static pins walk the page sources."""
import re
from pathlib import Path

import pytest

from localagents import db
from localagents.monitor import read, rows, seed

from monitor_seeded import NOW, endpoint_id, registry, seed_file

WINDOW_S = 24 * 3600.0
HOUR = 3600.0
STATIC = Path(__file__).resolve().parent.parent / "src" / "localagents" / "monitor" / "static"


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "seeded.db"
    seed_file(path)
    conn = db.open_db(path)
    yield conn, registry(tmp_path)
    conn.close()


@pytest.fixture(autouse=True)
def _empty_finished_cache():
    """Same rule as tests/test_monitor_read.py: the finished-entry cache is module-level; each
    test reads its own scratch world, so start from an empty cache."""
    read._FINISHED_CACHE.clear()
    yield


# --- the read shapes ---------------------------------------------------------------------------


def test_the_snapshot_job_rows_carry_no_name_key(world):
    """Every snapshot job row (the Recent jobs log's rows, live and finished alike) is keyed by
    ``id`` alone: no ``name`` key anywhere in the snapshot."""
    conn, reg = world
    ov = read.overview(conn, reg, NOW, WINDOW_S)
    assert len(ov["jobs"]) == 2  # the seeded running job and the seeded finished one
    for j in ov["jobs"]:
        assert "name" not in j
        assert j["id"]  # the one key, the UUID


def test_history_jobs_rows_carry_no_name_key(world):
    conn, _ = world
    h = read.endpoint_history(conn, endpoint_id(conn, "ai1"), NOW - HOUR, NOW)
    assert sorted(j["status"] for j in h["jobs"]) == ["running", "succeeded"]
    # story 3.10b made these full §5a job rows (their key set is pinned against the snapshot's
    # rows in tests/test_3_10b.py); the name key stays out of every one.
    assert all("name" not in j and j["id"] for j in h["jobs"])


def test_timeline_job_rows_carry_no_name_key(world):
    conn, _ = world
    jobs = read.jobs_overlapping(conn, NOW - HOUR, NOW)
    assert len(jobs) == 2
    assert all(set(j) == {"id", "endpoint", "status", "started_at", "finished_at"} for j in jobs)


def test_endpoint_entry_keeps_dropped_job_id_and_drops_the_name(world):
    """ai1 goes down at NOW+5 while the seed's running job is still running: the overview at
    NOW+10 carries only the job's id — no ``dropped_job`` key."""
    conn, reg = world
    ai1 = endpoint_id(conn, "ai1")
    with db.immediate(conn):  # rows.insert as in tests/test_monitor_read.py's dropped-job test
        rows.insert(conn, rows.ProbeRows(
            {**dict.fromkeys(rows.PROBE_COLUMNS), "t": NOW + 5, "endpoint_id": ai1, "up": 0,
             "host": "up", "error": seed._refused_sentence("http://10.0.0.50:8080")}, None, None))
    running = conn.execute("SELECT id FROM jobs WHERE status = 'running'").fetchone()
    ep = next(e for h in read.overview(conn, reg, NOW + 10, WINDOW_S)["hosts"]
              for e in h["endpoints"] if e["name"] == "ai1")
    assert ep["state"] == "down"
    assert "dropped_job" not in ep
    assert ep["dropped_job_id"] == running["id"]


def test_the_endpoint_entry_keys_are_named_without_dropped_job(world):
    """The full key set of an endpoint entry, so a re-added key fails here."""
    conn, reg = world
    ep = next(e for h in read.overview(conn, reg, NOW, WINDOW_S)["hosts"]
              for e in h["endpoints"] if e["name"] == "ai1")
    assert set(ep) == {
        "name", "backend", "base_url", "host", "state", "last_seen", "last_active", "model",
        "context", "probe_state", "last_model", "last_context", "slots", "requests",
        "kv_cache_usage", "latency_ms", "since", "error", "dropped_job_id", "rates",
        "gen_tps_live", "hardware", "jobs_running",
    }


def test_requests_rows_carry_job_id_not_job_name(world):
    conn, _ = world
    ids = {r["id"] for r in conn.execute("SELECT id FROM jobs")}
    out = read.endpoint_requests(conn, endpoint_id(conn, "ai1"), NOW - 24 * HOUR, NOW)
    reqs = out["requests"]
    assert reqs
    assert all(set(r) == {"path", "t", "http_status", "ms", "gen_ms", "tokens", "stream",
                          "context_exceeded", "error", "job_id"} for r in reqs)
    assert {r["job_id"] for r in reqs} == ids


def test_job_detail_has_no_name_key(world):
    conn, _ = world
    jid = conn.execute("SELECT id FROM jobs WHERE status = 'succeeded'").fetchone()["id"]
    d = read.job_detail(conn, jid)
    assert "name" not in d
    assert d["id"] == jid


# --- the page sources ---------------------------------------------------------------------------

# 3.10f-2: the pages show the short id; no read shape carries a job name any more, so no page may
# read one. `dropped_job\b` leaves `dropped_job_id` (the underscore is a word character, so the
# \b does not fire inside it) — the id key stays, the name key goes.
_STATIC_BANNED = [
    re.compile(r"job\.name"),
    re.compile(r"detail\.name"),
    re.compile(r"job_name"),
    re.compile(r"dropped_job\b"),
]


def _each_static():
    return {p.name: p.read_text() for p in sorted(STATIC.glob("*.js"))}


def test_no_static_script_reads_a_dropped_name_key():
    found = []
    for name, text in _each_static().items():
        for rx in _STATIC_BANNED:
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                found.append(f"{name}:{line} {m.group(0)}")
    assert found == [], found


def test_shortid_is_defined_and_exported():
    """The one short-id rule lives in app.js and rides on the export, so job.js and endpoint.js
    draw the same eight characters."""
    app = _each_static()["app.js"]
    assert re.search(r"function shortId\(\s*id\s*\)", app), "shortId not defined"
    assert re.search(r"\bshortId: shortId\b", app), "shortId not exported on window.localagentsMonitor"


def test_joblink_draws_the_short_id_with_the_full_id_as_title():
    """jobLink takes only the id, draws shortId(id) and titles the full id — a caller cannot
    pass a name any more (no two-argument call survives in any page file)."""
    src = _each_static()
    app = src["app.js"]
    body = re.search(r"function jobLink\(\s*id\s*\)\s*\{.*?\}", app, re.S)
    assert body, "jobLink(id) not defined in app.js"
    assert "title: id" in body.group(0), body.group(0)
    assert "shortId(id)" in body.group(0), body.group(0)
    for name, text in src.items():
        assert not re.search(r"jobLink\([^)]*,", text), f"{name}: jobLink still takes a second argument"
    # endpoint.js draws through the export, not a local copy of the old (id, name) builder
    assert "M.jobLink(" in src["endpoint.js"]
    assert not re.search(r"function jobLink\(", src["endpoint.js"])
