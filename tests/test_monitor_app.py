"""Story 3.1 item 3: the monitor's routes (monitor/app.py) over the seeded world, through
Starlette's test client (spec §7: "the SSE snapshot JSON, endpoint and job history routes,
time-range parsing with a bad range rejected").

The SSE stream is driven as a raw ASGI call on the test's own event loop: Starlette's
TestClient buffers a whole response body before returning (testclient.py ``handle_request``:
``portal.call(self.app, scope, receive, send)`` returns only when the app does), so it can never
hand back the first event of an endless stream."""
import asyncio
import errno
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

import pytest
from starlette.testclient import TestClient

from localagents import db
from localagents.monitor import app as app_mod
from localagents.monitor import serve as serve_mod
from localagents.monitor.config import MonitorSettings

from monitor_seeded import NOW, endpoint_id, registry, seed_file, write_config

_SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def dbpath(tmp_path):
    p = tmp_path / "seeded.db"
    seed_file(p)
    return p


def _app(dbpath, tmp_path, tick=None, refresh_s=0.1, **kw):
    # story 3.7m: the fan-out coalesces ticks into one push per refresh period; the stream tests
    # use a short period so a test's tick need not wait the production 5 s
    return app_mod.make_app(lambda: db.open_db(dbpath), registry(tmp_path),
                            MonitorSettings(refresh_s=refresh_s),
                            tick or asyncio.Event(), now=lambda: NOW, db_path=dbpath, **kw)


@pytest.fixture
def client(dbpath, tmp_path):
    with TestClient(_app(dbpath, tmp_path)) as c:
        yield c


def _job_id(dbpath, status):
    c = sqlite3.connect(dbpath)
    try:
        return c.execute("SELECT id FROM jobs WHERE status = ?", (status,)).fetchone()[0]
    finally:
        c.close()


# --- the shell and static files -------------------------------------------------------------


def test_index_is_the_html_shell_loading_app_js(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "localagents monitor" in r.text
    assert '<script src="/static/app.js"></script>' in r.text


def test_the_shell_revalidates_on_every_load(client):
    """Story 3.2c row 3 (the 3.2 combined review's observation): `/` carried no Cache-Control,
    so a browser could reuse a stale shell after an upgrade — the shell is what names the
    page's scripts, so a stale one pins a stale build. Same rule as the static mount: the
    answer is no-cache, so the browser asks on every load."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"


def test_static_serves_the_package_files(client):
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert client.get("/static/nope.js").status_code == 404


def test_static_files_revalidate_on_every_load(client):
    """Story 3.2a row 2 (the 3.3 build saw Chromium reuse an old app.js after an upgrade): the
    mount answers Cache-Control: no-cache, so the browser revalidates with the ETag StaticFiles
    already computes; a fresh file still answers 200, an unchanged one 304."""
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    r2 = client.get("/static/app.js", headers={"If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304
    assert r2.headers["cache-control"] == "no-cache"


# --- /api/snapshot ----------------------------------------------------------------------------


def test_snapshot_is_the_overview(client, dbpath):
    r = client.get("/api/snapshot")
    assert r.status_code == 200
    ov = r.json()
    assert ov["now"] == NOW and ov["window_s"] == 24 * 3600.0
    assert ov["db_path"] == str(dbpath)  # story 3.2a row 3: the file this server reads
    assert [h["name"] for h in ov["hosts"]] == ["ai1", "dgx1", "local"]
    assert [e["state"] for h in ov["hosts"] for e in h["endpoints"]] == ["live", "down", "dormant"]
    # story 3.7g: the log carries the live row first, then the seeded finished one (142 calls)
    assert [j["tool_calls"] for j in ov["jobs"]] == [50, 142]
    assert len(ov["events"]) == 3


def test_the_attention_window_comes_from_the_settings(dbpath, tmp_path):
    """attention_hours 0.1 (6 min): dgx1-a was last up 20 min ago, so it is dormant, not down."""
    app = app_mod.make_app(lambda: db.open_db(dbpath), registry(tmp_path), MonitorSettings(attention_hours=0.1),
                           asyncio.Event(), now=lambda: NOW)
    with TestClient(app) as c:
        ov = c.get("/api/snapshot").json()
    assert ov["window_s"] == 360.0
    assert [e["state"] for h in ov["hosts"] for e in h["endpoints"]] == ["live", "dormant", "dormant"]


# --- /api/endpoints/{name}/history ------------------------------------------------------------


def test_endpoint_history_defaults_to_an_hour(client):
    r = client.get("/api/endpoints/ai1/history")
    assert r.status_code == 200
    h = r.json()
    assert len(h["t"]) == 361 and h["step"] == 2 and h["t"][-1] == NOW
    assert "gpu_util" in h and len(h["gpu_util"]) == 361
    assert [e["kind"] for e in h["events"]] == ["restarted"]


def test_endpoint_history_takes_the_range(client):
    h = client.get("/api/endpoints/ai1/history", params={"range": "15m"}).json()
    assert len(h["t"]) == 181 and h["step"] == 1  # 15 min of 5 s probes, both ends included
    assert h["t"][0] == NOW - 900.0
    h = client.get("/api/endpoints/dgx1-a/history", params={"range": "1h"}).json()
    assert "gpu_util" not in h and "kv_cache_usage" in h


@pytest.mark.parametrize("bad", ["14m", "8d", "1w", "", "1h "])
def test_a_bad_range_is_400_bad_range(client, bad):
    r = client.get("/api/endpoints/ai1/history", params={"range": bad})
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "bad_range" and repr(bad) in body["message"]


def test_an_unknown_endpoint_is_404(client):
    r = client.get("/api/endpoints/nope/history")
    assert r.status_code == 404
    assert r.json()["error"] == "unknown_endpoint"


def test_an_unknown_endpoint_is_404_before_the_range_is_read(client):
    assert client.get("/api/endpoints/nope/history", params={"range": "1w"}).status_code == 404


# --- /api/fleet/history ------------------------------------------------------------------------


def test_fleet_history_default_range_shape(client):
    r = client.get("/api/fleet/history")
    assert r.status_code == 200
    fh = r.json()
    assert set(fh) == {"t", "step", "endpoints"}
    assert set(fh["endpoints"]) == {"ai1", "dgx1-a", "llamacpp"}  # every endpoint with a probe in the hour
    assert 0 < len(fh["t"]) <= 600 and fh["t"][-1] == NOW
    for name, body in fh["endpoints"].items():
        assert set(body) == {"gen_tps", "prompt_tps", "occupancy"}, name
        for key, values in body.items():
            assert len(values) == len(fh["t"]), (name, key)


# --- /api/endpoints/{name}/requests --------------------------------------------------------------


def test_endpoint_requests_route(client):
    r = client.get("/api/endpoints/ai1/requests")
    assert r.status_code == 200
    out = r.json()
    assert set(out) == {"requests", "totals"}
    assert len(out["requests"]) == 152
    ts = [q["t"] for q in out["requests"]]
    assert ts == sorted(ts, reverse=True) and out["requests"][0]["job_name"]
    assert out["totals"]["count"] == 152 and out["totals"]["output_tokens"] > 0
    empty = client.get("/api/endpoints/dgx1-a/requests").json()
    assert empty["requests"] == [] and all(v == 0 for v in empty["totals"].values())
    r = client.get("/api/endpoints/nope/requests")
    assert r.status_code == 404 and r.json()["error"] == "unknown_endpoint"
    r = client.get("/api/endpoints/ai1/requests", params={"range": "30s"})
    assert r.status_code == 400 and r.json()["error"] == "bad_range"


def test_fleet_history_range_and_errors(client):
    assert client.get("/api/fleet/history", params={"range": "15m"}).status_code == 200
    r = client.get("/api/fleet/history", params={"range": "8d"})
    assert r.status_code == 400 and r.json()["error"] == "bad_range"


def test_a_configured_endpoint_never_probed_is_an_empty_history(dbpath, tmp_path):
    from monitor_seeded import write_config
    from localagents.registry import Registry
    cfg = write_config(tmp_path / "x.yaml", "  fresh:\n    base_url: http://10.0.0.99:8080\n    host: ai1\n")
    app = app_mod.make_app(lambda: db.open_db(dbpath), Registry.load(cfg), MonitorSettings(),
                           asyncio.Event(), now=lambda: NOW)
    with TestClient(app) as c:
        r = c.get("/api/endpoints/fresh/history")
    assert r.status_code == 200
    assert r.json() == {"t": [], "step": 1, "events": [], "jobs": []}


# --- /api/jobs and /api/jobs/{id} --------------------------------------------------------------


def test_jobs_in_the_default_24h_range_newest_first(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    js = r.json()
    assert [j["status"] for j in js] == ["running", "succeeded"]
    assert all("result" not in j for j in js)
    assert [j["tool_calls"] for j in js] == [50, 142]


def test_jobs_range_narrows_and_rejects(client):
    # the running job was created 5 min ago, the finished one before the last 15 min
    assert [j["status"] for j in client.get("/api/jobs", params={"range": "15m"}).json()] == ["running"]
    r = client.get("/api/jobs", params={"range": "8d"})
    assert r.status_code == 400 and r.json()["error"] == "bad_range"


def test_job_detail_route(client, dbpath):
    r = client.get(f"/api/jobs/{_job_id(dbpath, 'succeeded')}")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "succeeded"
    assert len(d["requests"]) == 112
    assert d["tool_calls"] == len(d["tool_call_log"]) == 142  # ruling E: count and list, separate keys
    assert d["transcript_tail"] is None and d["transcript_note"]


def test_an_unknown_job_is_404(client):
    r = client.get("/api/jobs/no-such-id")
    assert r.status_code == 404
    assert r.json()["error"] == "unknown_job"


# --- database errors -------------------------------------------------------------------------


def test_a_database_error_is_503(tmp_path):
    def broken():
        raise sqlite3.OperationalError("database is locked")
    app = app_mod.make_app(broken, registry(tmp_path), MonitorSettings(), asyncio.Event(), now=lambda: NOW)
    with TestClient(app) as c:
        for path in ("/api/snapshot", "/api/endpoints/ai1/history", "/api/jobs", "/api/jobs/x"):
            r = c.get(path)
            assert r.status_code == 503, path
            assert r.json() == {"error": "database_error", "message": "OperationalError: database is locked"}


def test_a_too_new_schema_is_503(tmp_path):
    p = tmp_path / "new.db"
    c = sqlite3.connect(p)
    c.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    c.close()
    app = app_mod.make_app(lambda: db.open_db(p), registry(tmp_path), MonitorSettings(), asyncio.Event(),
                           now=lambda: NOW)
    with TestClient(app) as cl:
        r = cl.get("/api/snapshot")
    assert r.status_code == 503 and r.json()["error"] == "database_error"
    assert "newer than this localagents knows" in r.json()["message"]


def test_routes_use_a_connection_per_call_and_close_it(dbpath, tmp_path):
    opened = []

    def factory():
        c = db.open_db(dbpath)
        opened.append(c)
        return c
    app = app_mod.make_app(factory, registry(tmp_path), MonitorSettings(), asyncio.Event(), now=lambda: NOW)
    with TestClient(app) as c:
        c.get("/api/snapshot")
        c.get("/api/jobs")
    assert len(opened) == 2
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):  # closed
            conn.execute("SELECT 1")


# --- /events (SSE) ---------------------------------------------------------------------------


def _scope(path):
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "headers": [(b"host", b"testserver")], "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80)}


class _Stream:
    """One ASGI call of ``GET /events`` on the running loop: collects the response start and
    body chunks; ``close()`` sends the client's disconnect."""

    def __init__(self, app):
        self.start = None
        self.chunks: list[str] = []
        self.arrived = asyncio.Event()
        self.gone = asyncio.Event()
        self.task = asyncio.create_task(app(_scope("/events"), self._receive, self._send))

    async def _receive(self):
        await self.gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, msg):
        if msg["type"] == "http.response.start":
            self.start = msg
        elif msg.get("body"):
            self.chunks.append(msg["body"].decode())
            self.arrived.set()

    async def next_chunk(self, timeout=5.0):
        while not self.chunks:
            self.arrived.clear()
            await asyncio.wait_for(self.arrived.wait(), timeout)
        return self.chunks.pop(0)

    async def close(self):
        self.gone.set()
        await asyncio.wait_for(self.task, 5)


def _event(chunk):
    lines = chunk.rstrip("\n").split("\n")
    assert lines[0].startswith("event: ") and lines[1].startswith("data: "), chunk
    return lines[0][len("event: "):], json.loads(lines[1][len("data: "):])


def test_the_stream_sends_a_snapshot_on_connect_and_one_per_refresh_period(dbpath, tmp_path):
    """Story 3.7m: the collector's tick no longer wakes a stream by itself; the fan-out's clock
    waits its refresh period for the round to settle, then pushes once. Nothing is sent between
    pushes."""
    async def main():
        tick = asyncio.Event()
        app = _app(dbpath, tmp_path, tick)
        s = _Stream(app)
        name, body = _event(await s.next_chunk())
        headers = dict(s.start["headers"])
        assert headers[b"content-type"].startswith(b"text/event-stream")
        assert headers[b"cache-control"] == b"no-cache"
        assert name == "snapshot"
        assert [h["name"] for h in body["hosts"]] == ["ai1", "dgx1", "local"]
        assert body["jobs"][0]["tool_calls"] == 50
        assert body["refresh_s"] == 0.1
        # nothing more until the collector ticks
        with pytest.raises(TimeoutError):
            await s.next_chunk(timeout=0.2)
        tick.set()
        name, body = _event(await s.next_chunk())
        assert name == "snapshot" and len(body["hosts"]) == 3
        assert not tick.is_set()  # the fan-out consumed it
        with pytest.raises(TimeoutError):  # and no second push without a fresh tick
            await s.next_chunk(timeout=0.15)
        await s.close()
    asyncio.run(main())


def test_ticks_inside_one_refresh_period_are_one_push(dbpath, tmp_path):
    """Story 3.7m's timing test: three ticks inside one period (0.2 s) coalesce into exactly one
    snapshot after the first; a tick does not extend the period or earn a push of its own."""
    async def main():
        tick = asyncio.Event()
        app = _app(dbpath, tmp_path, tick, refresh_s=0.2)
        s = _Stream(app)
        assert _event(await s.next_chunk())[0] == "snapshot"  # the immediate connect snapshot
        tick.set()
        await asyncio.sleep(0.05)
        tick.set()
        await asyncio.sleep(0.05)
        tick.set()  # three ticks inside the first period
        assert _event(await s.next_chunk(timeout=1.0))[0] == "snapshot"  # exactly one push
        with pytest.raises(TimeoutError):  # and none of the coalesced ticks earns a second
            await s.next_chunk(timeout=0.15)
        await s.close()
    asyncio.run(main())


def test_one_tick_reaches_every_open_stream(dbpath, tmp_path):
    async def main():
        tick = asyncio.Event()
        app = _app(dbpath, tmp_path, tick)
        a, b = _Stream(app), _Stream(app)
        for s in (a, b):
            assert _event(await s.next_chunk())[0] == "snapshot"
        tick.set()
        for s in (a, b):
            assert _event(await s.next_chunk())[0] == "snapshot"
        await a.close()
        await b.close()
        assert app.state.fanout.subscribers == set()  # a closed stream unsubscribes
    asyncio.run(main())


def test_the_snapshot_is_read_once_per_push_and_shared(dbpath, tmp_path):
    """Story 3.7p: every subscriber of one push receives the same bytes from ONE read. The
    second stream to connect in the same push opens no connection; a push opens one more for
    both streams together, not one each."""
    opened = []

    def factory():
        conn = db.open_db(dbpath)
        opened.append(conn)
        return conn

    async def main():
        tick = asyncio.Event()
        app = app_mod.make_app(factory, registry(tmp_path), MonitorSettings(refresh_s=0.05),
                               tick, now=lambda: NOW, db_path=dbpath)
        a = _Stream(app)
        _, first = _event(await a.next_chunk())
        after_a = len(opened)
        b = _Stream(app)
        _, second = _event(await b.next_chunk())
        assert second == first                        # the same snapshot, not a second read
        assert len(opened) == after_a                 # the connect read was shared
        tick.set()
        assert _event(await a.next_chunk())[0] == "snapshot"
        assert _event(await b.next_chunk())[0] == "snapshot"
        assert len(opened) == after_a + 1             # one read for the push, not one per stream
        await a.close()
        await b.close()
    asyncio.run(main())


def test_snapshot_route_serves_the_current_push_from_its_cache(dbpath, tmp_path):
    """Story 3.7p: the same cache answers /api/snapshot while its push is current; a second
    call in that push computes nothing."""
    opened = []

    def factory():
        conn = db.open_db(dbpath)
        opened.append(conn)
        return conn

    app = app_mod.make_app(factory, registry(tmp_path), MonitorSettings(), asyncio.Event(),
                           now=lambda: NOW, db_path=dbpath)
    with TestClient(app) as c:
        first = c.get("/api/snapshot")
        assert first.status_code == 200
        assert len(opened) == 1
        second = c.get("/api/snapshot")
        assert second.status_code == 200 and second.json() == first.json()
        assert len(opened) == 1


def test_the_snapshot_cache_expires_by_age_not_only_by_push(dbpath, tmp_path):
    """The 3.7 server-batch review's P2(d), re-created as a test (DEFECT D4): a ``--no-collect``
    server — an app whose tick event nobody ever sets — had a push counter that never moved, so
    ``/api/snapshot`` served its startup snapshot byte-identically forever (0 statements, the
    jobs the MCP server wrote missing). The cache entry now also carries the monotonic time of
    its compute and expires at ``refresh_s``: within the period the entry still answers without
    a read; after it, the hand-inserted job is there."""
    app = app_mod.make_app(lambda: db.open_db(dbpath), registry(tmp_path),
                           MonitorSettings(refresh_s=0.3), asyncio.Event(),
                           now=lambda: NOW, db_path=dbpath)
    with TestClient(app) as c:
        first = c.get("/api/snapshot").json()
        assert [j["id"] for j in first["jobs"]] == [_job_id(dbpath, "running")]
        conn = db.open_db(dbpath)
        try:
            with db.immediate(conn):
                conn.execute("INSERT INTO jobs (id, name, status, created_at)"
                             " VALUES ('hand', 'hand-insert', 'queued', ?)", (NOW,))
        finally:
            conn.close()
        # inside the period the cached entry still answers: the job is not visible yet
        assert len(c.get("/api/snapshot").json()["jobs"]) == 1
        time.sleep(0.5)  # past refresh_s (0.3): the entry is stale, the call recomputes
        third = c.get("/api/snapshot").json()
        assert [j["id"] for j in third["jobs"]] == [_job_id(dbpath, "running"), "hand"]


def test_a_no_collect_app_self_ticks_the_streams_every_refresh_period(dbpath, tmp_path):
    """Story 3.7-fold-2 (D4): --no-collect has no collector ticks at all, so serve starts the
    fan-out self-ticking — the one refresh clock drives itself. The review's P2(d) world on the
    stream side: a job written by hand after the page connected arrives with the next push,
    within two periods."""
    async def main():
        app = _app(dbpath, tmp_path, refresh_s=0.1, self_tick=True)
        s = _Stream(app)
        name, body = _event(await s.next_chunk())
        assert name == "snapshot" and [j["id"] for j in body["jobs"]] == [_job_id(dbpath, "running")]
        conn = db.open_db(dbpath)
        try:
            with db.immediate(conn):
                conn.execute("INSERT INTO jobs (id, name, status, created_at)"
                             " VALUES ('hand', 'hand-insert', 'queued', ?)", (NOW,))
        finally:
            conn.close()
        name, body = _event(await s.next_chunk(timeout=0.7))  # the period is 0.1 s; 2x + slack
        assert name == "snapshot"
        assert [j["id"] for j in body["jobs"]] == [_job_id(dbpath, "running"), "hand"]
        await s.close()
    asyncio.run(main())


def test_the_self_tick_ends_on_the_commands_stop_sequence(dbpath, tmp_path):
    """The self-tick rides the same stop/close path as the collector-driven drain (story
    3.7-fold-2 D4): ``serve.stopper``'s stop + tick + one loop turn + close leaves the stream
    ended and nothing of the fan-out pending on this loop."""

    async def main():
        tick, stop = asyncio.Event(), asyncio.Event()
        app = _app(dbpath, tmp_path, tick, stop=stop, self_tick=True)
        s = _Stream(app)
        assert _event(await s.next_chunk())[0] == "snapshot"
        stop.set()
        tick.set()  # what the command does on SIGTERM: wake the clock and the streams
        await asyncio.sleep(0)
        await app.state.fanout.close()
        await asyncio.wait_for(s.task, 1)
        assert _drain_tasks() == []
        assert app.state.fanout.task is None
    asyncio.run(main())


def test_overview_history_batches_the_fleet_and_each_live_endpoint(client):
    """Story 3.7p: one call carries the fleet grid and every live endpoint's own history; the
    live set is the snapshot's classification (ai1 only in the seeded world)."""
    body = client.get("/api/overview/history?range=1h").json()
    assert set(body) == {"fleet", "endpoints"}
    assert set(body["endpoints"]) == {"ai1"}
    assert body["fleet"] == client.get("/api/fleet/history?range=1h").json()
    assert body["endpoints"]["ai1"] == client.get("/api/endpoints/ai1/history?range=1h").json()
    assert client.get("/api/overview/history?range=nope").status_code == 400


def test_history_is_cached_until_a_new_probe(dbpath, tmp_path):
    """Story 3.7p: a second request in the same probe generation is a cache hit (only the
    id/newest-t statement pair runs); a new probe changes the key and recomputes."""
    stmts: list[str] = []

    def factory():
        conn = db.open_db(dbpath)
        conn.set_trace_callback(stmts.append)
        return conn

    app = app_mod.make_app(factory, registry(tmp_path), MonitorSettings(), asyncio.Event(),
                           now=lambda: NOW, db_path=dbpath)
    with TestClient(app) as c:
        first = c.get("/api/endpoints/ai1/history?range=1h").json()
        stmts.clear()
        second = c.get("/api/endpoints/ai1/history?range=1h").json()
        assert second == first
        assert len(stmts) == 2  # the endpoints read and the grouped MAX(t): the cache hit
        conn = db.open_db(dbpath)
        try:
            with db.immediate(conn):
                conn.execute("INSERT INTO probes (t, endpoint_id, up) VALUES (?, ?, 1)",
                             (NOW + 5, endpoint_id(conn, "ai1")))
        finally:
            conn.close()
        stmts.clear()
        third = c.get("/api/endpoints/ai1/history?range=1h").json()
        assert third == first  # the future probe is outside the range's answer
        assert len(stmts) > 2  # but it is a new generation: the body was recomputed


def test_two_spellings_of_one_window_share_the_history_entry(dbpath, tmp_path):
    """Story 3.7-fold-2 (the review's POSSIBLE row): ``history_cache`` keys on the parsed window
    seconds, not the range text — ``parse_range`` reads 1h and 60m as one 3600 s window, so the
    second spelling is the same entry (the two key statements only), and the cache cannot grow
    with every spelling a caller invents."""
    stmts: list[str] = []

    def factory():
        conn = db.open_db(dbpath)
        conn.set_trace_callback(stmts.append)
        return conn

    app = app_mod.make_app(factory, registry(tmp_path), MonitorSettings(), asyncio.Event(),
                           now=lambda: NOW, db_path=dbpath)
    with TestClient(app) as c:
        first = c.get("/api/endpoints/ai1/history?range=1h").json()
        stmts.clear()
        minute = c.get("/api/endpoints/ai1/history?range=60m").json()
        assert minute == first
        assert len(stmts) == 2  # the key pair only: one entry, two spellings
        fleet_first = c.get("/api/fleet/history?range=24h").json()
        stmts.clear()
        fleet_day = c.get("/api/fleet/history?range=1440m").json()
        assert fleet_day == fleet_first
        assert len(stmts) == 2


def _statements_during_one_push(dbpath, tmp_path, streams_n):
    """The SQL statements executed across every connection during ONE push with ``streams_n``
    subscribed streams (the connect snapshots' work is cleared away first)."""
    stmts: list[str] = []

    def factory():
        conn = db.open_db(dbpath)
        conn.set_trace_callback(stmts.append)
        return conn

    async def main():
        tick = asyncio.Event()
        app = app_mod.make_app(factory, registry(tmp_path), MonitorSettings(refresh_s=0.05),
                               tick, now=lambda: NOW, db_path=dbpath)
        streams = [_Stream(app) for _ in range(streams_n)]
        for s in streams:
            assert _event(await s.next_chunk())[0] == "snapshot"
        stmts.clear()
        tick.set()
        for s in streams:
            assert _event(await s.next_chunk())[0] == "snapshot"
        await asyncio.sleep(0.05)  # let the losing subscriber take the cached answer
        for s in streams:
            await s.close()
        return len(stmts)
    return asyncio.run(main())


def test_one_push_reads_the_snapshot_once_however_many_subscribers(dbpath, tmp_path):
    """Story 3.7p's evidence: a push's database work is shared, so two subscribers cost the
    same statements as one (the numbers are recorded in the story report)."""
    one = _statements_during_one_push(dbpath, tmp_path, 1)
    two = _statements_during_one_push(dbpath, tmp_path, 2)
    assert one > 0
    assert two == one


def test_overview_history_warm_call_is_the_two_key_statements(dbpath, tmp_path):
    """Story 3.7p's evidence: the first /api/overview/history call computes the snapshot (for
    the live set) and the histories; the second, with no new probe, is two statements — the
    configured-endpoint read and the grouped MAX(t) that key the caches."""
    stmts: list[str] = []

    def factory():
        conn = db.open_db(dbpath)
        conn.set_trace_callback(stmts.append)
        return conn

    app = app_mod.make_app(factory, registry(tmp_path), MonitorSettings(), asyncio.Event(),
                           now=lambda: NOW, db_path=dbpath)
    with TestClient(app) as c:
        cold = c.get("/api/overview/history?range=1h").json()
        assert stmts
        stmts.clear()
        warm = c.get("/api/overview/history?range=1h").json()
        assert warm == cold
        assert len(stmts) == 2, stmts


def test_the_fanout_drains_from_app_start_not_the_first_subscriber(dbpath, tmp_path):
    """Review of 3.3 (its first server seam): the drain task used to start inside the first
    ``subscribe()``, so a collector tick before any browser connected was still set when the
    task began and the first page load received TWO snapshots. It starts with the app instead:
    the pre-connect tick is consumed, the first stream receives exactly one snapshot, and the
    next tick brings exactly one more."""

    async def main():
        tick = asyncio.Event()
        app = _app(dbpath, tmp_path, tick)  # make_app on this loop: the drain task starts here
        assert app.state.fanout.task is not None
        tick.set()                          # a collector tick before any browser connects
        await asyncio.sleep(0.2)            # let the drain consume it and finish its push
        s = _Stream(app)
        assert _event(await s.next_chunk())[0] == "snapshot"
        with pytest.raises(TimeoutError):   # and it earns no second snapshot
            await s.next_chunk(timeout=0.5)
        tick.set()                          # the first tick after the page loads: exactly one more
        assert _event(await s.next_chunk())[0] == "snapshot"
        await s.close()
    asyncio.run(main())


def test_the_stream_sends_keepalive_comments_when_idle(dbpath, tmp_path):
    async def main():
        app = _app(dbpath, tmp_path, keepalive_s=0.05)
        s = _Stream(app)
        assert _event(await s.next_chunk())[0] == "snapshot"
        assert await s.next_chunk() == ": keepalive\n\n"
        await s.close()
    asyncio.run(main())


def test_stop_ends_the_stream(dbpath, tmp_path):
    async def main():
        tick, stop = asyncio.Event(), asyncio.Event()
        app = _app(dbpath, tmp_path, tick, stop=stop)
        s = _Stream(app)
        assert _event(await s.next_chunk())[0] == "snapshot"
        stop.set()
        tick.set()  # what the command does on SIGTERM: wake the streams so they see stop
        await asyncio.wait_for(s.task, 5)  # the app returned without a client disconnect
        assert s.chunks == []
    asyncio.run(main())


def _drain_tasks():
    """The ``_Fanout._run`` tasks pending on the running loop (story 3.2c row 1)."""
    return [t for t in asyncio.all_tasks() if getattr(t.get_coro(), "__name__", "") == "_run"]


def test_stop_ends_the_streams_and_closes_the_fanout(dbpath, tmp_path):
    """Story 3.2c row 1 (the 3.2 combined review): ``_Fanout.close()`` had no caller, so a stop
    on a loop that outlives the app left the drain task pending forever — masked in the shipped
    command only by ``asyncio.run`` cancelling every leftover task at exit. The stop path now
    also cancels the drain and awaits it: with two streams open, stop + tick ends both streams
    AND leaves no ``_Fanout._run`` task pending on this loop."""

    async def main():
        tick, stop = asyncio.Event(), asyncio.Event()
        app = _app(dbpath, tmp_path, tick, stop=stop)
        a, b = _Stream(app), _Stream(app)
        for s in (a, b):
            assert _event(await s.next_chunk())[0] == "snapshot"
        assert _drain_tasks()  # the drain is pending while the app runs
        stop.set()
        tick.set()  # what the command does on SIGTERM: wake the streams so they see stop
        await asyncio.wait_for(a.task, 5)
        await asyncio.wait_for(b.task, 5)
        assert _drain_tasks() == []  # nothing of the fan-out survives the stop on this loop
        assert app.state.fanout.task is None
    asyncio.run(main())


def test_the_commands_stop_sequence_wakes_the_streams_before_it_closes_the_fanout(dbpath, tmp_path):
    """``serve.stopper``'s exact sequence on SIGTERM: ``stop.set()``, ``tick.set()``, one loop
    turn (``sleep(0)``), then ``fanout.close()``. The drain must have set every subscriber's
    event in that one step (story 3.7m's settle has to return without suspending when the stop
    is already set), or the streams would wait for their keepalive and the shutdown would
    hang."""
    async def main():
        tick, stop = asyncio.Event(), asyncio.Event()
        app = _app(dbpath, tmp_path, tick, stop=stop)
        a, b = _Stream(app), _Stream(app)
        for s in (a, b):
            assert _event(await s.next_chunk())[0] == "snapshot"
        stop.set()
        tick.set()
        await asyncio.sleep(0)
        await app.state.fanout.close()
        await asyncio.wait_for(a.task, 1)
        await asyncio.wait_for(b.task, 1)
        assert a.chunks == [] and b.chunks == []  # woken by the stop, no snapshot computed
        assert app.state.fanout.task is None
    asyncio.run(main())


def test_stop_set_twice_ends_the_streams_without_error(dbpath, tmp_path):
    """Standing ruling (d) for the fan-out change: stop twice — the second set landing while the
    first stop's close is still awaiting the drain — must not raise, and must still end every
    stream with nothing pending. ``serve``'s own ``finally: stop.set()`` is exactly this when a
    signal already drove the stop."""

    async def main():
        tick, stop = asyncio.Event(), asyncio.Event()
        app = _app(dbpath, tmp_path, tick, stop=stop)
        a, b = _Stream(app), _Stream(app)
        for s in (a, b):
            assert _event(await s.next_chunk())[0] == "snapshot"
        stop.set()
        tick.set()
        await asyncio.sleep(0)  # one loop pass: the streams are now mid their stop awaits
        stop.set()              # the second, while the first stop's await is in flight
        await asyncio.wait_for(a.task, 5)
        await asyncio.wait_for(b.task, 5)
        assert _drain_tasks() == []
        assert app.state.fanout.task is None
    asyncio.run(main())


def test_a_stream_after_close_does_not_resurrect_the_drain(dbpath, tmp_path):
    """Story 3.2c row 1: the lazy fallback in ``subscribe()`` must not restart the drain after
    close. A browser reconnecting after the stop gets the connect snapshot and the stream ends
    at once — decided over raising a clear error: ``/events`` answers with the snapshot first,
    and an exception there surfaces as a raw ASGI failure of an already-200 response, while the
    page's EventSource would retry into the same answer. Ending at once says "no server here"
    in the only channel the route has."""

    async def main():
        tick, stop = asyncio.Event(), asyncio.Event()
        app = _app(dbpath, tmp_path, tick, stop=stop)
        s = _Stream(app)
        assert _event(await s.next_chunk())[0] == "snapshot"
        stop.set()
        tick.set()
        await asyncio.wait_for(s.task, 5)
        assert _drain_tasks() == []
        s2 = _Stream(app)  # a reconnect after close
        assert _event(await s2.next_chunk())[0] == "snapshot"  # the connect snapshot is the contract
        await asyncio.wait_for(s2.task, 5)  # and the stream ends at once: stop is set, no ticks follow
        assert s2.chunks == []
        assert _drain_tasks() == []  # the subscribe fallback did not restart the drain
    asyncio.run(main())


def test_a_database_error_in_the_stream_is_an_error_event(tmp_path):
    def broken():
        raise sqlite3.OperationalError("disk I/O error")

    async def main():
        app = app_mod.make_app(broken, registry(tmp_path), MonitorSettings(), asyncio.Event(), now=lambda: NOW)
        s = _Stream(app)
        name, body = _event(await s.next_chunk())
        assert name == "error"
        assert body == {"error": "database_error", "message": "OperationalError: disk I/O error"}
        await s.close()
    asyncio.run(main())


# --- item 4: the command ----------------------------------------------------------------------


def _cli(args):
    return ("import sys; sys.argv=['localagents', 'monitor', " + ", ".join(repr(a) for a in args)
            + "]; import localagents; localagents.main()")


def _env(tmp_path, dbpath, cfg):
    return {**os.environ, "LOCALAGENTS_DB": str(dbpath), "LOCALAGENTS_CONFIG": str(cfg),
            "XDG_STATE_HOME": str(tmp_path / "state")}


def test_monitor_help_exits_0(tmp_path):
    out = subprocess.run([sys.executable, "-c", _cli(["--help"])], capture_output=True, text=True, cwd=_SRC,
                         env=_env(tmp_path, tmp_path / "x.db", tmp_path / "models.yaml"), timeout=60)
    assert out.returncode == 0, out.stderr
    assert "localagents monitor" in out.stdout
    for flag in ("--port", "--host", "--db", "--config", "--no-collect"):
        assert flag in out.stdout
    assert not (tmp_path / "x.db").exists()  # --help opens nothing


def test_a_bad_monitor_section_exits_2_naming_the_key(tmp_path):
    cfg = write_config(tmp_path / "models.yaml", 'monitor:\n  port: "8765"\n')
    out = subprocess.run([sys.executable, "-c", _cli(["--no-collect"])], capture_output=True, text=True, cwd=_SRC,
                         env=_env(tmp_path, tmp_path / "x.db", cfg), timeout=60)
    assert out.returncode == 2
    assert "monitor: port must be an int" in out.stderr


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- port_free (story 3.2a row 1) --------------------------------------------------------------


def test_port_free_reads_a_time_wait_socket_as_free():
    """Review of 3.2 (both builds): a browser session just closed leaves a TIME-WAIT socket on
    the port, and a plain bind refuses it for ~60 s: a restart exits 2 where uvicorn — which
    binds with SO_REUSEADDR — would have served. port_free binds with SO_REUSEADDR too, so it
    calls such a port free. (A TIME-WAIT quad only exists after a real connection: listen,
    connect, close both. The holder listens with SO_REUSEADDR as uvicorn does, so the accepted
    socket — and the TIME-WAIT it leaves — carries the flag: on a current kernel plain bind
    fails against TIME-WAIT, and reuse-bind only succeeds when both sides ask for it.)"""
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    client_sock = socket.socket()
    client_sock.connect(("127.0.0.1", port))
    accepted, _ = holder.accept()
    accepted.close()  # the server side closes first: the quad goes TIME-WAIT on this port
    holder.close()
    client_sock.close()
    assert serve_mod.port_free("127.0.0.1", port)


def test_port_free_is_false_for_a_listening_socket():
    """SO_REUSEADDR does not make a live listener look free: the 3.1 ruling F sentence stands."""
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    try:
        assert not serve_mod.port_free("127.0.0.1", holder.getsockname()[1])
    finally:
        holder.close()


def test_port_free_refuses_when_any_resolved_address_is_taken():
    """Story 3.2c row 4 (the 3.2 combined review): port_free answered on the FIRST address
    getaddrinfo resolved. `localhost` resolves ::1 before 127.0.0.1 on this box, so a listener
    on 127.0.0.1 only still read free and `--host localhost` started serving where the port was
    taken. The port is free only when EVERY address for the host binds."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    try:
        assert not serve_mod.port_free("localhost", holder.getsockname()[1])
    finally:
        holder.close()


def test_port_free_is_true_for_a_free_port():
    """Every address of the host binds: the port is free. (127.0.0.1 names one address; the
    multi-address `localhost` of the row's other test reads free the same way when nothing
    listens.)"""
    assert serve_mod.port_free("127.0.0.1", _free_port())


def test_port_free_skips_an_address_family_the_box_lacks(monkeypatch):
    """Story 3.2c row 4: an IPv6-less box still resolves `localhost` to ::1, and binding it
    answers EADDRNOTAVAIL, not EADDRINUSE — that address is skipped, not a refusal, and the
    free IPv4 bind still answers True. The box is simulated (this kernel has IPv6): a socket
    subclass fails the v6 bind the way a box without the family does."""
    port = _free_port()

    class _NoV6Socket(socket.socket):
        def bind(self, sa):
            if self.family == socket.AF_INET6:
                raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
            return super().bind(sa)

    def fake_gai(host, port_, type=None):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port_, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port_))]

    monkeypatch.setattr(serve_mod.socket, "socket", _NoV6Socket)
    monkeypatch.setattr(serve_mod.socket, "getaddrinfo", fake_gai)
    assert serve_mod.port_free("localhost", port)


def test_a_port_already_in_use_exits_2_with_the_sentence(tmp_path):
    """Review of 3.1 (G1), ruling F: hold the port with a listening socket; the command exits 2
    naming the port, with no traceback — the bind is tested before uvicorn starts."""
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    port = holder.getsockname()[1]
    try:
        cfg = write_config(tmp_path / "models.yaml")
        out = subprocess.run([sys.executable, "-c", _cli(["--no-collect", "--no-open", "--port", str(port)])],
                             capture_output=True, text=True, cwd=_SRC,
                             env=_env(tmp_path, tmp_path / "x.db", cfg), timeout=60)
    finally:
        holder.close()
    assert out.returncode == 2, out.stderr
    assert f"localagents monitor: port {port} is already in use" in out.stderr
    assert "Traceback" not in out.stderr


def test_a_live_run_serves_the_seeded_file_and_stops_on_sigterm(tmp_path):
    """The command end to end over loopback: a seeded file (now = the wall clock, so the states
    are the seed's), --no-collect, a free port; /api/snapshot answers within 10 s with the
    three hosts, /events streams its first snapshot over real HTTP, and SIGTERM — with that
    stream still open — ends the process with exit 0 within 5 s."""
    dbpath = tmp_path / "seeded.db"
    seed_file(dbpath, now=time.time())
    cfg = write_config(tmp_path / "models.yaml")
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", _cli(["--no-collect", "--no-open", "--port", str(port)])], cwd=_SRC,
                            env=_env(tmp_path, dbpath, cfg), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        snap = None
        while time.monotonic() < deadline:
            try:
                r = httpx.get(base + "/api/snapshot", timeout=1)
                if r.status_code == 200:
                    snap = r.json()
                    break
            except httpx.HTTPError:
                pass
            assert proc.poll() is None, proc.stderr.read()
            time.sleep(0.1)
        assert snap is not None, "the monitor did not answer within 10 s"
        assert [h["name"] for h in snap["hosts"]] == ["ai1", "dgx1", "local"]
        assert [e["state"] for h in snap["hosts"] for e in h["endpoints"]] == ["live", "down", "dormant"]
        with httpx.stream("GET", base + "/events", timeout=5) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            lines = r.iter_lines()
            assert next(lines) == "event: snapshot"
            data = next(lines)
            assert data.startswith("data: ") and len(json.loads(data[6:])["hosts"]) == 3
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=5) == 0
        first = proc.stdout.readline()
        assert f"http://127.0.0.1:{port}/" in first and str(dbpath) in first and "3 endpoints" in first
        assert "Traceback" not in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# --- the browser open (--no-open, the already-running short-circuit) ---------------------------


def test_no_open_is_a_flag_and_the_browser_opens_by_default():
    assert serve_mod.parse_args([]).no_open is False
    assert serve_mod.parse_args(["--no-open"]).no_open is True


def _fake_browser(tmp_path):
    """A BROWSER script that appends the URL webbrowser passes it to tmp_path/opened.

    Platform note: POSIX only. webbrowser runs a BROWSER value that has no %s as `<value> <url>`,
    so the script is a /bin/sh file that must be executable (chmod 0o755); a Windows run would need
    a different launcher and webbrowser's %s form."""
    script = tmp_path / "fake-browser"
    opened = tmp_path / "opened"
    script.write_text(f'#!/bin/sh\necho "$1" >> {opened}\n')
    script.chmod(0o755)
    return script, opened


def _wait_for_lines(path, n, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and len(path.read_text().splitlines()) >= n:
            break
        time.sleep(0.1)
    return path.read_text().splitlines() if path.exists() else []


def _wait_snapshot(base, proc, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if httpx.get(base + "/api/snapshot", timeout=1).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        assert proc.poll() is None, proc.stderr.read()
        time.sleep(0.1)
    return False


def test_the_default_run_opens_the_browser_once_and_still_stops_on_sigterm(tmp_path):
    """No flag: once /api/snapshot answers, BROWSER receives the URL exactly once (within 10 s),
    and SIGTERM still ends the process with exit 0. Platform: POSIX only, see _fake_browser."""
    dbpath = tmp_path / "seeded.db"
    seed_file(dbpath, now=time.time())
    cfg = write_config(tmp_path / "models.yaml")
    script, opened = _fake_browser(tmp_path)
    port = _free_port()
    env = {**_env(tmp_path, dbpath, cfg), "BROWSER": str(script)}
    proc = subprocess.Popen([sys.executable, "-c", _cli(["--no-collect", "--port", str(port)])], cwd=_SRC,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        assert _wait_snapshot(base, proc), "the monitor did not answer within 10 s"
        assert _wait_for_lines(opened, 1) == [f"{base}/"]
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
        assert "Traceback" not in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_second_run_on_a_running_port_opens_it_and_exits_0(tmp_path):
    """With the first run still up: a second default run on the same port prints the
    already-running line, opens the page (the file now has two lines) and exits 0 within 10 s.
    The same second run with --no-open is the refusal: exit 2 with the port-in-use sentence, and
    the browser is not touched."""
    dbpath = tmp_path / "seeded.db"
    seed_file(dbpath, now=time.time())
    cfg = write_config(tmp_path / "models.yaml")
    script, opened = _fake_browser(tmp_path)
    port = _free_port()
    env = {**_env(tmp_path, dbpath, cfg), "BROWSER": str(script)}
    first = subprocess.Popen([sys.executable, "-c", _cli(["--no-collect", "--port", str(port)])], cwd=_SRC,
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        assert _wait_snapshot(base, first), "the monitor did not answer within 10 s"
        assert _wait_for_lines(opened, 1) == [f"{base}/"]
        second = subprocess.run([sys.executable, "-c", _cli(["--no-collect", "--port", str(port)])],
                                capture_output=True, text=True, cwd=_SRC, env=env, timeout=10)
        assert second.returncode == 0, second.stderr
        assert f"localagents monitor: already running at {base}/ · opening" in second.stdout
        assert "Traceback" not in second.stderr
        assert _wait_for_lines(opened, 2) == [f"{base}/", f"{base}/"]
        refused = subprocess.run([sys.executable, "-c", _cli(["--no-collect", "--no-open", "--port", str(port)])],
                                 capture_output=True, text=True, cwd=_SRC, env=env, timeout=10)
        assert refused.returncode == 2, refused.stderr
        assert f"localagents monitor: port {port} is already in use" in refused.stderr
        assert len(opened.read_text().splitlines()) == 2  # --no-open opened nothing
        first.send_signal(signal.SIGTERM)
        assert first.wait(timeout=5) == 0
    finally:
        if first.poll() is None:
            first.kill()
            first.wait()


# --- item 5: the spec's §5a names every key the routes emit -------------------------------------

_SPEC = Path(__file__).resolve().parent.parent / "docs" / "superpowers" / "specs" / "2026-10-29-monitor-design.md"


def _section_5a():
    text = _SPEC.read_text()
    start = text.index("## 5a. Routes and JSON shapes")
    return text[start:text.index("## 6. ", start)]


def _keys(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, out)
    return out


def test_spec_5a_names_every_key_the_routes_emit(client, dbpath):
    """The JSON shapes are a contract for stories 3.2-3.4: every key any route emits over the
    seeded world is named in §5a, so a renamed or added field fails here until the spec says so.
    The inner keys of ``tool_counts`` (tool names), ``metrics`` and ``worktree`` are the MCP
    schema's, not this route's shape, and are left out."""
    keys: set = set()
    _keys(client.get("/api/snapshot").json(), keys)
    for name in ("ai1", "dgx1-a", "llamacpp"):
        _keys(client.get(f"/api/endpoints/{name}/history").json(), keys)
    _keys(client.get("/api/endpoints/ai1/requests").json(), keys)
    fh = client.get("/api/fleet/history").json()
    _keys({k: v for k, v in fh.items() if k != "endpoints"}, keys)
    for body in fh["endpoints"].values():  # the fleet dict is keyed by endpoint name: a value, not a shape key
        _keys(body, keys)
    oh = client.get("/api/overview/history").json()  # story 3.7p: fleet + the live endpoints
    _keys({"fleet": {k: v for k, v in oh["fleet"].items() if k != "endpoints"}}, keys)
    _keys({"endpoints": {k: v for k, v in oh["endpoints"]["ai1"].items()}}, keys)
    inner = ("worktree", "metrics", "tool_counts")
    for j in client.get("/api/jobs").json():
        keys |= set(j)
        _keys({k: v for k, v in j.items() if k not in inner}, keys)
    d = client.get(f"/api/jobs/{_job_id(dbpath, 'succeeded')}").json()
    _keys({k: v for k, v in d.items() if k not in inner}, keys)
    _keys(client.get("/api/jobs/nope").json(), keys)
    spec = _section_5a()
    missing = sorted(k for k in keys if not re.search(r"(?<![A-Za-z_])" + re.escape(k) + r"(?![A-Za-z_])", spec))
    assert missing == []


def test_spec_5a_quotes_every_rates_reason_error_code_and_event():
    from localagents.monitor import read
    spec = _section_5a()
    src = Path(read.__file__).read_text()
    reasons = re.findall(r'"reason": "([^"]+)"', src)
    assert len(reasons) == 4
    for reason in reasons:
        assert f'"{reason}"' in spec, reason
    for code in ("bad_range", "unknown_endpoint", "unknown_job", "database_error"):
        assert f"`{code}`" in spec, code
    for event in ("event: snapshot", "event: error", ": keepalive"):
        assert event in spec, event
