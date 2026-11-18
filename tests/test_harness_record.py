"""Epic 6.4: the harness and the coordinator's session on the job record, the client on the
server row (schema version 14; the migration itself is tests/test_db.py's).

The operator (2026-11-17): "The additions seem logical and useful, so yes the columns are good
to add." ``jobs.harness`` is the harness the session ran in (``run_agent(harness=)``, else
``defaults.harness``; a resume keeps its job's); ``jobs.owner_session`` is OpenCode's
``_meta["ai.opencode/sessionID"]`` from the tool call; ``mcp_servers.client`` is 6.1's client.
"""

import asyncio
from types import SimpleNamespace

import pytest
from claude_agent_sdk._internal.message_parser import parse_message

from localagents import clients, runner, server, status
from localagents.harnesses import claude_code
from localagents.registry import Defaults, Endpoint, Registry


def _reg(**defaults):
    endpoints = {"e": Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")}
    return Registry(path=None, endpoints=endpoints, defaults=Defaults(job_retention_days=0, **defaults),
                    raw={}, local={})


@pytest.fixture
def live(monkeypatch):
    class Live:
        reg = _reg()

    async def probe(self, ep):
        return {"up": True, "models": ["served-model"]}

    async def nothing(*a, **kw):
        return None

    async def no_start():
        return 0

    def fake_query(prompt, options):
        async def session():
            yield parse_message({"type": "system", "subtype": "init", "session_id": "sess-1", "model": "m",
                                 "cwd": "/w"})
            yield parse_message({"type": "result", "subtype": "success", "duration_ms": 5, "duration_api_ms": 4,
                                 "is_error": False, "num_turns": 1, "session_id": "sess-1", "result": "ok"})
        return session()

    monkeypatch.setattr(Registry, "probe", probe)
    monkeypatch.setattr(server, "_reg", lambda: Live.reg)
    monkeypatch.setattr(server, "run_job", runner.run_job)
    monkeypatch.setattr(server.shim, "start", no_start)
    monkeypatch.setattr(runner, "fetch_metrics", nothing)
    monkeypatch.setattr(claude_code, "query", fake_query)
    return Live


def _ctx(meta):
    return SimpleNamespace(request_context=SimpleNamespace(meta=meta))


def _row(job_id):
    return server.store.conn.execute(
        "SELECT harness, owner_session FROM jobs WHERE id = ?", (job_id,)).fetchone()


def test_a_job_records_the_default_harness_and_no_owner_session(live):
    out = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=10))
    assert out["status"] == "succeeded", out
    assert out["harness"] == "claude-code" and out["owner_session"] is None
    assert tuple(_row(out["id"])) == ("claude-code", None)


def test_opencodes_session_id_on_the_call_is_the_jobs_owner_session(live):
    ctx = _ctx({clients.OPENCODE_SESSION_META: "ses_abc123", "progressToken": None})
    out = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=0, ctx=ctx))
    asyncio.run(server.store.wait(out["id"], 10))
    assert tuple(_row(out["id"])) == ("claude-code", "ses_abc123")


def test_owner_session_reads_only_a_non_empty_string_and_never_raises():
    key = clients.OPENCODE_SESSION_META
    assert clients.owner_session(None) is None
    assert clients.owner_session(_ctx(None)) is None
    assert clients.owner_session(_ctx({})) is None
    assert clients.owner_session(_ctx({key: ""})) is None
    assert clients.owner_session(_ctx({key: 7})) is None
    assert clients.owner_session(_ctx({key: "ses_x"})) == "ses_x"

    class NoRequest:
        @property
        def request_context(self):
            raise ValueError("Context is not available outside of a request")

    assert clients.owner_session(NoRequest()) is None


def test_an_unknown_harness_is_a_bad_argument(live):
    out = asyncio.run(server.run_agent(task="t", model="served-model", harness="nope"))
    assert out["error"] == "bad_argument" and "claude-code" in out["message"]


def test_a_resume_keeps_its_jobs_harness_and_refuses_another(live):
    first = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=10))
    prev = server.store.get(first["id"])
    prev.harness = "x-other"  # a harness the registry does not name yet: only the record's word
    out = asyncio.run(server.run_agent(task="again", resume_job=first["id"], harness="claude-code"))
    assert out["error"] == "bad_argument" and "x-other" in out["message"], out


def test_the_configs_harness_is_checked(capsys):
    d = Defaults(harness="nope")
    d.check()
    assert d.harness == "claude-code"
    assert "harness must be one of claude-code" in capsys.readouterr().err
    ok = Defaults(harness="claude-code")
    ok.check()
    assert ok.harness == "claude-code"


def test_the_server_row_records_its_client(live):
    server.store.set_client("opencode")
    row = server.store.conn.execute("SELECT client FROM mcp_servers WHERE id = ?",
                                    (server.store.server_id,)).fetchone()
    assert row[0] == "opencode"


def test_the_status_line_marks_only_another_harness():
    assert status._cal_mark({"harness": "claude-code"}) == ""
    assert status._cal_mark({}) == ""
    assert status._cal_mark({"harness": "opencode"}) == "opencode "
    assert status._cal_mark({"harness": "opencode", "calibration": True}) == "cal opencode "


def test_a_job_naming_an_unknown_harness_fails_and_settles(monkeypatch, tmp_path):
    """The 6.3+6.4 review (DeepSeek, job f352098f), POSSIBLE made a rule: run_job resolved the
    harness above its try, so a job whose row names a harness this install lacks (two installs
    sharing one store, the older resuming the newer's job) raised before the finally and stayed
    `queued`, never settled. A job always settles: an unknown harness is `failed`, its error
    naming the harness, and the finish runs."""
    import asyncio as aio
    from pathlib import Path

    from localagents.jobs import Job
    from localagents.registry import Resolution

    async def nothing(*a, **kw):
        return None

    monkeypatch.setattr(runner, "fetch_metrics", nothing)
    monkeypatch.setattr(runner.feedback, "note_start", lambda job: None)
    monkeypatch.setattr(runner, "_finish", nothing)
    ep = Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")
    job = Job(id="j", task="t", model="m", endpoint="e", base_url=ep.url, cwd=str(tmp_path),
              run_cwd=str(tmp_path), isolation="none", log_dir=str(tmp_path / "log"), harness="not-installed")
    Path(job.log_dir).mkdir()
    monkeypatch.setattr(job, "record_transcript", lambda: None)
    monkeypatch.setattr(job, "changed", lambda: None)
    aio.run(runner.run_job(job, _reg(), Resolution(endpoint=ep, served_model="m"), permission_mode=None,
                           max_turns=None, allowed_tools=None, system_prompt_append=None,
                           resume_session=None, timeout_s=5, shim_base_url="http://shim"))
    assert job.status == "failed", job.status
    assert "not-installed" in (job.error or "")
    assert job.done and job.finished_at is not None
