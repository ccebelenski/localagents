"""Every tool's final summary() answers database_error when the database underneath it fails.

Story 1.9 review (job 20261029-112752-b3499250), probe 2: `wait_job`, `run_agent`, `wait_jobs`
and `cancel_job` returned a traceback to the caller when the database broke between the wait
and the summary — `job.summary()` derives its usage/tool figures from the rows, so it is a
read like any other (story 1.9a row 1).

Construction: `DROP TABLE tool_calls` on the live connection. Every derive read over that
table raises sqlite3.OperationalError("no such table: tool_calls") — the same class of failure
as a locked or broken database — and the jobs/requests rows the tools also read stay intact,
so each tool still gets as far as its final summary(). Each call is made in-process, exactly
as MCP calls the tool function. A dict with error="database_error" is the contract; an
exception is a traceback-shaped answer to the caller.
"""

import asyncio
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest


class Ctx:
    """Just enough of mcp Context for the progress reporters."""

    def __init__(self):
        self.msgs = []

    async def report_progress(self, progress, total, message):
        self.msgs.append((progress, total, message))


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LOCALAGENTS_DB", str(tmp_path / "state" / "localagents" / "localagents.db"))
    cfg = tmp_path / "models.yaml"
    cfg.write_text("")
    monkeypatch.setenv("LOCALAGENTS_CONFIG", str(cfg))
    from localagents import jobs, server

    store = jobs.JobStore()
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "_shim_port_recorded", True)  # no shim in these tests
    monkeypatch.setattr(server, "_ensure_sampler", lambda: None)  # no heartbeat task to cancel
    return SimpleNamespace(server=server, jobs=jobs, store=store, monkeypatch=monkeypatch)


def make_job(srv):
    return srv.store.new(task="t", model="m", endpoint="e", base_url="http://e:8080",
                         backend="llama.cpp", host="e", cwd="/tmp", run_cwd="/tmp", isolation="none")


def break_tool_calls(store):
    """Every derive read over the tool_calls table now raises OperationalError."""
    store.conn.execute("DROP TABLE tool_calls")


def test_wait_job_answers_database_error_when_its_summary_cannot_be_read(srv):
    """wait_job's `return job.summary()` used to sit after the try whose comment said the guard
    covered it: the wait survived, the answer was an OperationalError out of the tool."""
    server, store, jobs = srv.server, srv.store, srv.jobs
    mine = make_job(srv)
    mine.status = "running"
    mine.changed()
    mine.log_text("[start] test")
    srv.monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.02)
    break_tool_calls(store)
    ctx = Ctx()
    out = asyncio.run(server.wait_job(mine.id, timeout_s=0.1, ctx=ctx))
    assert out["error"] == "database_error"
    assert "no such table: tool_calls" in out["message"]
    assert ctx.msgs, "the wait itself must still report progress while the derive reads fail"


def test_run_agent_answers_database_error_when_its_final_summary_cannot_be_read(srv):
    """run_agent(wait_s>0) waits, the job settles, and the final job.summary() raised."""
    server = srv.server
    from localagents.registry import Endpoint, Resolution

    ep = Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")

    class FakeReg:
        defaults = SimpleNamespace(dump_requests=False, log_events=False, probe_timeout_s=1.0, harness="claude-code")
        endpoints = {"e": ep}
        pools: dict = {}  # router 12 (pin moved): admission reads the facts and the pool

        async def resolve(self, model=None, endpoint=None):
            return Resolution(ep, "served-model")

        async def probe(self, endpoint):
            return {"up": True, "models": ["served-model"]}

        def pool_members(self, name):
            return []

    async def fake_run_job(job, *a, **kw):
        job.status = "succeeded"
        job.result = "ok"
        job.finished_at = time.time()
        job._done.set()
        job.changed()

    srv.monkeypatch.setattr(server, "_reg", lambda: FakeReg())
    srv.monkeypatch.setattr(server, "run_job", fake_run_job)
    break_tool_calls(srv.store)
    out = asyncio.run(server.run_agent(task="t", wait_s=5))
    assert out["error"] == "database_error"
    assert "no such table: tool_calls" in out["message"]


def test_wait_jobs_answers_database_error_when_a_done_summary_cannot_be_read(srv):
    """wait_jobs built its `done` records after the try: one done job's summary raised. The
    whole call answers database_error — a partial record is worse than none."""
    server = srv.server
    done = make_job(srv)
    done.status = "succeeded"
    done._done.set()
    done.changed()
    running = make_job(srv)
    break_tool_calls(srv.store)
    out = asyncio.run(server.wait_jobs([done.id, running.id], timeout_s=0.1))
    assert out["error"] == "database_error"
    assert "no such table: tool_calls" in out["message"]


def test_cancel_job_answers_database_error_when_its_summary_cannot_be_read(srv):
    """cancel_job returns the settled record it waited for; when the database cannot produce
    it, the answer is database_error, not the exception."""
    server = srv.server
    job = make_job(srv)
    job.status = "succeeded"
    job._done.set()
    job.changed()
    break_tool_calls(srv.store)
    out = asyncio.run(server.cancel_job(job.id))
    assert out["error"] == "database_error"
    assert "no such table: tool_calls" in out["message"]


def test_run_agent_answers_database_error_on_a_newer_schema_file(srv):
    """Review of 1.9a (job 20261029-122217-cc1db55a, probe 1 scenario B): the store opens
    lazily, and run_agent's `store.new` guard caught `sqlite3.Error` only. A second
    connection raised `PRAGMA user_version` to 99 — newer than this localagents knows — so
    a fresh store's first use raises `db.SchemaError` (a RuntimeError) out of migrate as a
    traceback to the caller. The answer is database_error, like every other guard."""
    make_job(srv)  # the file opens at the current schema first, then another process upgrades it
    other = sqlite3.connect(os.environ["LOCALAGENTS_DB"])
    other.execute("PRAGMA user_version = 99")
    other.commit()
    other.close()
    fresh = srv.jobs.JobStore()  # a fresh store: the in-process jobs dict is empty, open fails
    srv.monkeypatch.setattr(srv.server, "store", fresh)

    from localagents.registry import Endpoint, Resolution

    ep = Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")

    class FakeReg:
        defaults = SimpleNamespace(dump_requests=False, log_events=False, probe_timeout_s=1.0, harness="claude-code")
        endpoints = {}

        async def resolve(self, model=None, endpoint=None):
            return Resolution(ep, "served-model")

    srv.monkeypatch.setattr(srv.server, "_reg", lambda: FakeReg())
    out = asyncio.run(srv.server.run_agent(task="t", wait_s=5))
    assert out["error"] == "database_error"
    assert "newer than this localagents knows" in out["message"]


def test_a_healthy_database_still_answers_unknown_job(srv):
    """The new guards must not turn a real unknown_job into database_error."""
    server = srv.server
    for name, coro in (
        ("job_status", server.job_status("nope")),
        ("job_log", server.job_log("nope")),
        ("wait_job", server.wait_job("nope", timeout_s=0.1)),
        ("cancel_job", server.cancel_job("nope")),
        ("wait_jobs", server.wait_jobs(["nope"], timeout_s=0.1)),
    ):
        out = asyncio.run(coro)
        assert out["error"] == "unknown_job", name
