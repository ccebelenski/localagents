"""Story 3.1 item 3: the monitor's routes (monitor/app.py) over the seeded world, through
Starlette's test client (spec §7: "the SSE snapshot JSON, endpoint and job history routes,
time-range parsing with a bad range rejected").

The SSE stream is driven as a raw ASGI call on the test's own event loop: Starlette's
TestClient buffers a whole response body before returning (testclient.py ``handle_request``:
``portal.call(self.app, scope, receive, send)`` returns only when the app does), so it can never
hand back the first event of an endless stream."""
import asyncio
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
from localagents.monitor.config import MonitorSettings

from monitor_seeded import NOW, registry, seed_file, write_config

_SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def dbpath(tmp_path):
    p = tmp_path / "seeded.db"
    seed_file(p)
    return p


def _app(dbpath, tmp_path, tick=None, **kw):
    return app_mod.make_app(lambda: db.open_db(dbpath), registry(tmp_path), MonitorSettings(),
                            tick or asyncio.Event(), now=lambda: NOW, **kw)


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


def test_static_serves_the_package_files(client):
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert client.get("/static/nope.js").status_code == 404


# --- /api/snapshot ----------------------------------------------------------------------------


def test_snapshot_is_the_overview(client):
    r = client.get("/api/snapshot")
    assert r.status_code == 200
    ov = r.json()
    assert ov["now"] == NOW and ov["window_s"] == 24 * 3600.0
    assert [h["name"] for h in ov["hosts"]] == ["ai1", "dgx1", "local"]
    assert [e["state"] for h in ov["hosts"] for e in h["endpoints"]] == ["live", "down", "dormant"]
    assert [j["tool_calls"] for j in ov["jobs"]] == [50]
    assert len(ov["events"]) == 4


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
    assert len(h["t"]) == 360 and h["step"] == 2 and h["t"][-1] == NOW
    assert "gpu_util" in h and len(h["gpu_util"]) == 360
    assert [e["kind"] for e in h["events"]] == ["up", "restarted"]


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
    assert len(d["requests"]) == 112 and len(d["tool_calls"]) == 142
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


def test_the_stream_sends_a_snapshot_on_connect_and_one_per_tick(dbpath, tmp_path):
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
        # nothing more until the collector ticks
        with pytest.raises(TimeoutError):
            await s.next_chunk(timeout=0.2)
        tick.set()
        name, body = _event(await s.next_chunk())
        assert name == "snapshot" and len(body["hosts"]) == 3
        assert not tick.is_set()  # the fan-out consumed it
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


def test_a_live_run_serves_the_seeded_file_and_stops_on_sigterm(tmp_path):
    """The command end to end over loopback: a seeded file (now = the wall clock, so the states
    are the seed's), --no-collect, a free port; /api/snapshot answers within 10 s with the
    three hosts, /events streams its first snapshot over real HTTP, and SIGTERM — with that
    stream still open — ends the process with exit 0 within 5 s."""
    dbpath = tmp_path / "seeded.db"
    seed_file(dbpath, now=time.time())
    cfg = write_config(tmp_path / "models.yaml")
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", _cli(["--no-collect", "--port", str(port)])], cwd=_SRC,
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
