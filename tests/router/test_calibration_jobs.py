"""router 8: calibration-flagged jobs (router design §10 "Transparent": every probe is a job
row with ``calibration = 1``, admitted as ``background`` through the same gate as any job,
shown in the monitor like any job, and excluded from the traffic figures).

``start_job`` is run_agent's dispatch factored out of the tool: the router guards, resolve,
the shim, the job record, the sampler and the task. It raises ``JobRefused`` carrying the dict
run_agent's tool returns; run_agent stays a tool with the same answers."""

import asyncio
from pathlib import Path

import httpx
import pytest

from localagents import jobs, server
from localagents.registry import Defaults, Endpoint, Registry
from localagents.harnesses import claude_code

SERVING = {"first": ["default-model"], "second": ["big-model"]}


@pytest.fixture
def reg(monkeypatch, tmp_path):
    endpoints = {n: Endpoint(name=n, base_url=f"http://{n}:8080", backend="llama.cpp") for n in SERVING}
    r = Registry(path=None, endpoints=endpoints, defaults=Defaults(), raw={}, local={})

    async def probe(self, ep):
        return {"up": True, "models": SERVING[ep.name]}

    async def no_start():
        return 0

    monkeypatch.setattr(Registry, "probe", probe)
    monkeypatch.setattr(server, "_reg", lambda: r)
    monkeypatch.setattr(server.shim, "start", no_start)
    return r


def test_start_job_marks_calibration(reg, monkeypatch):
    """The row carries the caller's flags: calibration = 1 and the background priority the
    spec admits every probe at. The task starts like any other job's."""
    started = []

    async def record_run(job, *a, **kw):
        started.append(job.id)

    monkeypatch.setattr(server, "run_job", record_run)

    async def go():
        job = await server.start_job("probe", calibration=True, priority="background")
        await asyncio.sleep(0)  # let the task start
        return job

    job = asyncio.run(go())
    row = server.store.conn.execute(
        "SELECT calibration, priority FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert row["calibration"] == 1 and row["priority"] == "background"
    assert job.calibration is True and job.priority == "background"
    assert started == [job.id]


def test_start_job_rejects_bad_priority_and_admit(reg):
    """The two admission inputs are validated before anything is resolved or recorded: a
    typo is bad_argument, the same refusal shape every other tool argument gets."""
    for kwargs in ({"priority": "later"}, {"admit": "later"}):
        with pytest.raises(server.JobRefused) as exc:
            asyncio.run(server.start_job("t", **kwargs))
        assert exc.value.reply["error"] == "bad_argument"
    assert server.store.jobs == {}


def test_run_agent_still_returns_refusals_as_dicts(monkeypatch):
    """The guards moved into start_job; the tool still answers its callers with the same
    dict, because run_agent turns JobRefused back into e.reply."""
    real = Endpoint(name="real", base_url="http://127.0.0.1:8080", backend="vllm")
    auto = Endpoint(name="auto", base_url="", backend="router", decision_url="http://127.0.0.1:8766")
    r = Registry(path=None, endpoints={"auto": auto, "real": real}, defaults=Defaults(), raw={}, local={})
    monkeypatch.setattr(server, "_reg", lambda: r)

    def refuse(request):  # never a real d3 server (the operator's box may run one on 8766)
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(server, "_d3_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    out = asyncio.run(server.run_agent(task="x", model="auto"))
    # router 12 (pin moved): routing is built; d3 unreachable is model_unavailable, a dict
    assert out["error"] == "model_unavailable" and "d3 server" in out["message"]
    assert server.store.jobs == {}


def test_pass_throughs_reach_the_options(reg, monkeypatch):
    """mcp_servers, thinking and effort are start_job's keyword-only pass-throughs: a fake
    run_job sees them unchanged (run_job maps thinking onto the SDK's config there)."""
    seen = {}

    async def record_run(job, *a, **kw):
        seen.update(kw)

    monkeypatch.setattr(server, "run_job", record_run)
    mcp = {"tools": {"type": "stdio", "command": "tools"}}

    async def go():
        await server.start_job("t", mcp_servers=mcp, thinking=True, effort="high")
        await asyncio.sleep(0)

    asyncio.run(go())
    assert seen["mcp_servers"] == mcp
    assert seen["thinking"] is True and seen["effort"] == "high"


async def _no_metrics(*a, **kw):
    return None


def _empty_query(prompt, options):
    async def nothing():
        return
        yield

    return nothing()


def test_strict_mcp_and_setting_sources_reach_the_options(reg, monkeypatch):
    """The two new keyword-only pass-throughs (C1) travel start_job -> run_job ->
    ClaudeAgentOptions: a calibration job's session runs with the strict MCP config and no
    setting sources; None keeps today's behaviour (the SDK default, the config's sources)."""
    from localagents import runner

    captured = {}

    def fake_options(**kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr(runner, "fetch_metrics", _no_metrics)
    monkeypatch.setattr(runner, "finish_worktree", lambda j: None)
    monkeypatch.setattr(claude_code, "query", _empty_query)
    monkeypatch.setattr(claude_code, "ClaudeAgentOptions", fake_options)

    async def go(**kw):
        job = await server.start_job("t", **kw)
        await job._done.wait()
        return job

    asyncio.run(go(strict_mcp_config=True, setting_sources=[]))
    assert captured["strict_mcp_config"] is True
    assert captured["setting_sources"] == []
    captured.clear()
    asyncio.run(go())
    assert "strict_mcp_config" not in captured
    assert captured["setting_sources"] == Defaults().setting_sources


@pytest.mark.parametrize("thinking, expected", [
    (True, {"type": "adaptive"}),
    (False, {"type": "disabled"}),
    (None, None),
])
def test_run_job_maps_the_levers_onto_the_sdk_options(monkeypatch, tmp_path, thinking, expected):
    """run_job translates the bool for the SDK: adaptive, disabled, or the field left unset.
    mcp_servers reaches the SDK field (None is the empty dict), effort is passed as given."""
    from localagents import runner
    from localagents.registry import Resolution

    store = jobs.JobStore()
    job = store.new(task="t", model="m", endpoint="e", base_url="http://e:8080",
                    backend="llama.cpp", host="local", cwd=str(tmp_path), run_cwd=str(tmp_path),
                    isolation="none")
    captured = {}

    def fake_options(**kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr(runner, "fetch_metrics", _no_metrics)
    monkeypatch.setattr(runner, "finish_worktree", lambda j: None)
    monkeypatch.setattr(claude_code, "query", _empty_query)
    monkeypatch.setattr(claude_code, "ClaudeAgentOptions", fake_options)
    ep = Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")
    reg = Registry(path=None, endpoints={"e": ep}, defaults=Defaults(job_retention_days=0), raw={}, local={})
    asyncio.run(runner.run_job(
        job, reg, Resolution(ep, "m"), permission_mode=None, max_turns=None, allowed_tools=None,
        system_prompt_append=None, resume_session=None, timeout_s=5,
        shim_base_url="http://127.0.0.1:1/job/x", thinking=thinking, effort="high",
    ))
    assert captured["mcp_servers"] == {}
    if expected is None:  # None leaves the field unset rather than passing None through
        assert "thinking" not in captured
    else:
        assert captured["thinking"] == expected
    assert captured["effort"] == "high"


def test_runner_logs_the_thinking_length(tmp_path):
    """The thinking line names the block's real length before its excerpt: the transcript
    records only the first 500 chars, and the facts probe must measure thinking by the
    number, not by the truncated tail (final review M4)."""
    from claude_agent_sdk import AssistantMessage, ThinkingBlock


    store = jobs.JobStore()
    job = store.new(task="t", model="m", endpoint="e", base_url="http://e:8080",
                    backend="llama.cpp", host="local", cwd=str(tmp_path), run_cwd=str(tmp_path),
                    isolation="none")
    thinking = "x" * 700
    claude_code._apply_message(job, AssistantMessage(
        content=[ThinkingBlock(thinking=thinking, signature="")], model="m"))
    line = (Path(job.log_dir) / "transcript.txt").read_text()
    assert line == f"[thinking] ({len(thinking)} chars) {thinking[:500]}\n"
