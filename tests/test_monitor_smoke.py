"""Story 3.5: the smoke script's checklist, requests, real job and collector checks.

The unit half of the story stays offline: the network parts run against fakes (httpx
MockTransport, the story-2.1 refused route) and the live run — the story's gate — is a
separate step, not a test.
"""

import asyncio
import errno
import json
import os
import subprocess
import sys
import time

import httpx
import pytest

from localagents import db, jobs
from localagents.monitor import smoke
from localagents.monitor.smoke import Check, Checklist, exit_code
from localagents.registry import Defaults, Endpoint, Registry

from monitor_fixtures import STRATA_AI2, serve


def test_render_one_line_per_check_in_order():
    cl = Checklist()
    cl.append(Check("request 1 of 3", True, "HTTP 200, output_tokens=4, 812 ms"))
    cl.append(Check("Bash tool_calls row", False, "0 rows for this job"))
    cl.append(Check("job row terminal", True))
    assert cl.render() == (
        "[ok] request 1 of 3: HTTP 200, output_tokens=4, 812 ms\n"
        "[MISSING] Bash tool_calls row: 0 rows for this job\n"
        "[ok] job row terminal"
    )
    assert cl.failed == 1
    assert exit_code(cl) == 1


def test_empty_and_all_ok_checklists_exit_zero():
    assert Checklist().failed == 0
    assert exit_code(Checklist()) == 0
    cl = Checklist()
    cl.append(Check("a", True))
    cl.extend([Check("b", True), Check("c", True)])
    assert cl.failed == 0 and exit_code(cl) == 0


# ---------------------------------------------------------------- item 2: direct_requests

def _fake_transport():
    """Two 200s with a usage block, then a refused connection chained exactly as httpx chains
    the kernel's ECONNREFUSED (monitor_fixtures' shape), so classify_connect_failure reads the
    errno. Records each request's body."""
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 3:
            try:
                raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
            except OSError as e:
                raise httpx.ConnectError("All connection attempts failed", request=request) from e
        return httpx.Response(200, json={"usage": {"input_tokens": 12, "output_tokens": 7}})

    return httpx.MockTransport(handler), seen


def test_direct_requests_records_status_usage_latency_and_the_refusal_sentence():
    ep = Endpoint(name="x", base_url="http://x:8080", backend="strata")
    transport, seen = _fake_transport()
    checks = asyncio.run(smoke.direct_requests(ep, "some-model", n=3, transport=transport))
    assert [c.ok for c in checks] == [True, True, False]
    assert "HTTP 200" in checks[0].detail and "output_tokens=7" in checks[0].detail
    assert "ms" in checks[0].detail
    assert "nothing is listening" in checks[2].detail  # classify_connect_failure's sentence


def test_direct_requests_bodies_are_the_short_ping_and_the_endpoint_key():
    ep = Endpoint(name="x", base_url="http://x:8080", backend="strata", api_key="secret")
    transport, seen = _fake_transport()
    asyncio.run(smoke.direct_requests(ep, "some-model", n=2, transport=transport))
    assert len(seen) == 2
    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 16 and body["stream"] is False
    assert body["model"] == "some-model"
    assert body["messages"] == [{"role": "user", "content": "Reply with the single word pong."}]
    assert seen[0].headers["x-api-key"] == "secret"  # auth_headers, not the session placeholder


# ---------------------------------------------------------------- item 3: real_job

class _FakeShim:
    """stand-in for the real Shim: starts instantly, hands out the base URL run_job wants"""

    def __init__(self):
        self.started = 0

    async def start(self):
        self.started += 1
        return 4321

    def base_url_for(self, job_id):
        return f"http://127.0.0.1:4321/job/{job_id}"


def _registry(monkeypatch):
    """a Registry whose probe is canned, so resolve() needs no network (the story-2.2 FakeRegistry
    shape from tests/test_monitor_collect.py)"""

    async def probe(self, ep):
        return {"up": True, "models": ["served-model"], "context": {"served-model": 131072}}

    monkeypatch.setattr(Registry, "probe", probe)
    return Registry(
        path=None,
        endpoints={"ep": Endpoint(name="ep", base_url="http://ep:8080", backend="llama.cpp")},
        defaults=Defaults(), raw={}, local={},
    )


def _fake_run_job(**writes):
    """A run_job that writes what a real Bash+date run would: one 200 request row, one Bash
    tool-call row with a result, status succeeded (and optionally a date line in the transcript).
    Records the kwargs real_job passed so the call shape is pinned."""
    seen = {}

    async def fake(job, reg, res, **kw):
        seen.update(kw)
        seen["args"] = (job.id, res.served_model, res.endpoint.name)
        if writes.get("request", True):
            job.record_request({
                "path": "/v1/messages", "t": time.time(), "status": 200, "ms": 900, "gen_ms": 800,
                "stream": False, "usage": {"input_tokens": 100, "output_tokens": 7},
            })
        if writes.get("tool", True):
            job.record_tool_call(time.time(), "Bash", "tu-1", "date")
            job.record_tool_result("tu-1", time.time() + 0.1, False)
        if writes.get("transcript", True):
            job.log_text("[assistant] Wed Oct 29 13:00:00 UTC 2026")
        job.status = writes.get("status", "succeeded")
        job._done.set()
        job.changed()

    return fake, seen


def test_real_job_builds_the_job_like_run_agent_and_reports_every_row(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    reg = _registry(monkeypatch)
    store = jobs.JobStore()
    fake, seen = _fake_run_job()
    monkeypatch.setattr(smoke, "run_job", fake)
    shim = _FakeShim()

    checks = asyncio.run(smoke.real_job(reg, store, shim, "ep", "served-model", 180))
    assert shim.started == 1  # the shim starts once, before the job
    # the run_job call carries the fixed brief's job shape: max_turns 4, Bash only, run_agent's
    # default permission mode (None -> run_job reads it from defaults)
    assert seen["max_turns"] == 4 and seen["allowed_tools"] == ["Bash"]
    assert seen["permission_mode"] is None and seen["timeout_s"] == 180
    assert seen["shim_base_url"].endswith(f"/job/{seen['args'][0]}")

    assert all(c.ok for c in checks), [c.name + ": " + c.detail for c in checks if not c.ok]
    names = [c.name for c in checks]
    assert names == [
        "job row terminal and succeeded",
        "requests row for the job (200)",
        "tool_calls row for the job (Bash)",
        "transcript has a date-shaped line",
        "summary usage totals non-zero",
    ]


def test_real_job_job_row_missing_when_the_run_fails(monkeypatch, tmp_path):
    """A job that ends `failed` fails the terminal check and leaves the rest readable; the
    checklist says which rows it did and did not find, it does not raise."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    reg = _registry(monkeypatch)
    store = jobs.JobStore()
    fake, _ = _fake_run_job(status="failed", request=False, tool=False, transcript=False)
    monkeypatch.setattr(smoke, "run_job", fake)

    checks = asyncio.run(smoke.real_job(reg, store, _FakeShim(), "ep", "served-model", 180))
    by_name = {c.name: c for c in checks}
    assert not by_name["job row terminal and succeeded"].ok
    assert "failed" in by_name["job row terminal and succeeded"].detail
    assert not by_name["requests row for the job (200)"].ok
    assert not by_name["tool_calls row for the job (Bash)"].ok
    assert not by_name["transcript has a date-shaped line"].ok
    assert not by_name["summary usage totals non-zero"].ok


def test_transcript_date_check_ignores_the_runner_own_lines(monkeypatch, tmp_path):
    """Live-run finding (classified b, script bug): the date-shaped line must come from what
    ``date`` printed, not from the runner's own lines — a cwd like a worktree named
    20261029-... made the whole-transcript search pass without any date output."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    reg = _registry(monkeypatch)
    store = jobs.JobStore()
    fake, _ = _fake_run_job(transcript=False)

    async def with_start_line_only(job, reg, res, **kw):
        job.log_text("[start] endpoint=ep via shim http://127.0.0.1:1/job/x cwd=/state/worktrees/20261029-130838-4304e120")
        await fake(job, reg, res, **kw)

    monkeypatch.setattr(smoke, "run_job", with_start_line_only)
    checks = asyncio.run(smoke.real_job(reg, store, _FakeShim(), "ep", "served-model", 180))
    by_name = {c.name: c for c in checks}
    assert not by_name["transcript has a date-shaped line"].ok


# ---------------------------------------------------------------- item 4: dead port and collector

@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "m.db")
    yield c
    c.close()


def test_down_state_reads_the_refused_endpoint_end_to_end(monkeypatch, conn):
    """The real Registry probe against the fixture's refused route (a chained ECONNREFUSED,
    as the kernel delivers it), the real collect.tick, and the rows/states it must produce:
    up = 0, host = up (the refusal proves the box), the nothing-is-listening sentence,
    endpoint_state dormant (never seen up) and node_state up."""
    serve(monkeypatch, "http://127.0.0.1:1", {"/v1/models": None})
    reg = Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={})
    checks = asyncio.run(smoke.down_state(conn, reg=reg))
    assert all(c.ok for c in checks), [c.name + ": " + c.detail for c in checks if not c.ok]
    row = conn.execute("SELECT * FROM probes ORDER BY id DESC LIMIT 1").fetchone()
    assert row["up"] == 0 and row["host"] == "up"
    assert "nothing is listening" in row["error"]
    # never written to yaml: the only endpoints row is the temporary one the tick made
    eps = conn.execute("SELECT name FROM endpoints").fetchall()
    assert [r["name"] for r in eps] == ["smoke-dead"]


def _smoke_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """the real CLI in a subprocess: argv[1:] reaches localagents.main's branch dispatch"""
    return subprocess.run(
        [sys.executable, "-c", "import sys; from localagents import main; main()", *args],
        capture_output=True, text=True, timeout=120, env=env,
    )


def test_smoke_subcommand_help_exits_zero():
    p = _smoke_cli("smoke", "--help")
    assert p.returncode == 0
    assert "--endpoint" in p.stdout and "--skip-job" in p.stdout


def test_smoke_unknown_endpoint_is_one_missing_line_and_exit_one(tmp_path):
    """No network needed: an endpoint name not in models.yaml fails at the resolve, before any
    request, and the answer is exactly one MISSING line and exit 1."""
    cfg = tmp_path / "models.yaml"
    cfg.write_text("")  # no endpoints at all
    env = {**os.environ, "LOCALAGENTS_CONFIG": str(cfg), "LOCALAGENTS_DB": str(tmp_path / "s.db")}
    p = _smoke_cli("smoke", "--endpoint", "nosuchname", "--model", "whatever", "--skip-job", env=env)
    assert p.returncode == 1
    missing = [ln for ln in p.stdout.splitlines() if ln.startswith("[MISSING]")]
    assert len(missing) == 1 and "Unknown endpoint" in missing[0], p.stdout


def test_live_tick_offline_sees_counters_and_live_state(monkeypatch, conn):
    """The live half's shape, offline over the captured strata documents (the story-2.1 pass-1
    routes): a tick, a counters row beside the probe, endpoint_state live. The live half against
    the running node is the live run; this pins the read path against real captures."""
    serve(monkeypatch, "http://10.0.0.60:8080", STRATA_AI2)
    reg = Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={})
    ep = Endpoint(name="ai2", base_url="http://10.0.0.60:8080", backend="strata", host="ai2")
    checks = asyncio.run(smoke.live_tick(conn, reg, ep))
    assert all(c.ok for c in checks), [c.name + ": " + c.detail for c in checks if not c.ok]
    row = conn.execute("SELECT * FROM probes ORDER BY id DESC LIMIT 1").fetchone()
    assert row["up"] == 1
    assert conn.execute("SELECT 1 FROM probe_counters WHERE probe_id = ?", (row["id"],)).fetchone()
