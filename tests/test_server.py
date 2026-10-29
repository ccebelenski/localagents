import asyncio
import json
import re
import time

import pytest

from localagents import jobs, server
from localagents.registry import Defaults, Endpoint, Registry

SERVING = {"first": ["default-model"], "second": ["big-model"]}


@pytest.fixture
def reg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    endpoints = {n: Endpoint(name=n, base_url=f"http://{n}:8080", backend="llama.cpp") for n in SERVING}
    r = Registry(path=None, endpoints=endpoints, defaults=Defaults(), raw={}, local={})

    async def probe(self, ep):
        return {"up": True, "models": SERVING[ep.name]}

    async def no_run(*a, **kw):
        return None

    async def no_start():
        return 0

    monkeypatch.setattr(Registry, "probe", probe)
    monkeypatch.setattr(server, "_reg", lambda: r)
    monkeypatch.setattr(server, "run_job", no_run)
    monkeypatch.setattr(server.shim, "start", no_start)
    return r


def _prev_job(**kw):
    job = server.store.new(
        task="t", model="big-model", endpoint="second", base_url="http://second:8080",
        backend="llama.cpp", host="local", cwd="/tmp", run_cwd="/tmp", isolation="none", **kw,
    )
    job.session_id = "sess-1"
    return job


def test_resume_keeps_previous_model(reg):
    prev = _prev_job()
    out = asyncio.run(server.run_agent(task="next", resume_job=prev.id))
    assert out["model"] == "big-model" and out["endpoint"] == "second"


def test_resume_with_explicit_model_overrides(reg):
    prev = _prev_job()
    out = asyncio.run(server.run_agent(task="next", resume_job=prev.id, model="default-model"))
    assert out["model"] == "default-model" and out["endpoint"] == "first"


def test_fresh_job_without_model_uses_first_live(reg):
    out = asyncio.run(server.run_agent(task="new"))
    assert out["model"] == "default-model"


def _prev_worktree_job(tmp_path):
    root = tmp_path / "worktrees" / "proj" / "old-job"  # does not exist: removed for having no changes
    job = server.store.new(
        task="t", model="big-model", endpoint="second", base_url="http://second:8080",
        backend="llama.cpp", host="local", cwd="/tmp", run_cwd=str(root), isolation="worktree",
        worktree={"repo": "/tmp", "path": str(root), "root": str(root), "branch": "local-agent/old-job",
                  "base": "abc123", "kept": False, "note": "no changes; worktree removed"},
    )
    job.session_id = "sess-wt"
    return job


def test_resume_worktree_job_keeps_tree_even_when_removed(reg, tmp_path):
    prev = _prev_worktree_job(tmp_path)
    out = asyncio.run(server.run_agent(task="next", resume_job=prev.id))
    assert out.get("error") is None
    assert out["isolation"] == "worktree"
    assert out["run_cwd"] == prev.run_cwd and out["cwd"] == prev.cwd
    # the tree's identity is carried over; the previous outcome (kept/note) is not
    assert out["worktree"] == {k: prev.worktree[k] for k in ("repo", "path", "root", "branch", "base")}


def test_resume_plain_job_with_missing_cwd_is_an_error(reg, tmp_path):
    prev = _prev_job()
    prev.run_cwd = str(tmp_path / "gone")
    out = asyncio.run(server.run_agent(task="next", resume_job=prev.id))
    assert out["error"] == "bad_argument" and "no longer exists" in out["message"]


def test_local_complete_reports_transport_errors(reg, monkeypatch):
    import httpx

    async def boom(self, *a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    out = asyncio.run(server.local_complete(prompt="hi", model="big-model"))
    assert out["error"] == "backend_error"
    assert out["endpoint"] == "second" and "ConnectError" in out["message"]


class _Ctx:
    """Stand-in for the MCP Context: records progress notifications."""

    def __init__(self):
        self.reports = []

    async def report_progress(self, progress, total=None, message=None):
        self.reports.append((progress, total, message))


def _finish_later(job, delay):
    async def go():
        await asyncio.sleep(delay)
        job.status = "succeeded"
        job._done.set()
    return go()


def test_wait_s_reports_progress_until_done(reg, monkeypatch):
    """Story 1.5b pins the line: "<status>: <elapsed>s, <turns> turns, <calls> tool calls,
    <errors> errors; <last_tool>" — errors and last_tool read from the rows the job recorded."""
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.02)
    ctx = _Ctx()
    job = _prev_job()
    t = time.time()
    job.record_tool_call(t, "Read", "u1", json.dumps({"file_path": "/a.py"}))
    job.record_tool_result("u1", t + 0.01, False)
    job.record_tool_call(t + 1, "Bash", "u2", "make test")
    job.record_tool_result("u2", t + 2, True)  # the one error
    job.record_tool_call(t + 2, "Edit", "u3", "/a.py")
    job.record_tool_result("u3", t + 3, False)  # the latest row: last_tool

    async def run():
        task = asyncio.create_task(server.wait_job(job.id, timeout_s=5, ctx=ctx))
        await asyncio.sleep(0.01)
        asyncio.create_task(_finish_later(job, 0.1))
        return await task

    out = asyncio.run(run())
    assert out["status"] == "succeeded"
    assert len(ctx.reports) >= 2
    line = re.compile(r"queued: \d+s, 0 turns, 3 tool calls, 1 errors; Edit: /a\.py")
    assert all(line.fullmatch(m) for _, _, m in ctx.reports), ctx.reports


def test_progress_line_omits_zero_errors_and_the_tail_until_the_first_tool(reg, monkeypatch):
    """No rows yet: no errors part and no "; ..." tail; a call without a result is not an error.
    Two jobs reported side by side: the line's tool counts identify which is which, no timing
    races."""
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.02)
    ctx = _Ctx()
    quiet, busy = _prev_job(), _prev_job()
    busy.record_tool_call(time.time(), "Glob", "u1", '{"pattern": "*.py"}')  # result pending: no errors part

    async def run():
        a = asyncio.create_task(server.wait_job(quiet.id, timeout_s=0.05, ctx=ctx))
        b = asyncio.create_task(server.wait_job(busy.id, timeout_s=0.05, ctx=ctx))
        await a
        await b

    asyncio.run(run())
    bare = re.compile(r"queued: \d+s, 0 turns, 0 tool calls")
    tail = re.compile(r'queued: \d+s, 0 turns, 1 tool calls; Glob: \{"pattern": "\*\.py"\}')
    assert all(bare.fullmatch(m) or tail.fullmatch(m) for _, _, m in ctx.reports), ctx.reports
    assert any(bare.fullmatch(m) for _, _, m in ctx.reports)
    assert any(tail.fullmatch(m) for _, _, m in ctx.reports)


def test_progress_tail_shows_the_bash_command_the_runner_recorded(reg):
    """Through the real runner._apply_message: a Bash call's row holds its command (not the
    input JSON with the description), so the line reads "...; Bash: uv run pytest -q"
    (review of 1.5b, finding 4)."""
    from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

    from localagents import runner

    job = _prev_job()
    job.status = "running"
    runner._apply_message(job, AssistantMessage(content=[
        ToolUseBlock(id="tu-1", name="Bash", input={"command": "uv run pytest -q", "description": "Run tests"}),
    ], model="m"))
    runner._apply_message(job, UserMessage(content=[ToolResultBlock(tool_use_id="tu-1", content="1 failed", is_error=True)]))
    row = server.store.conn.execute("SELECT input FROM tool_calls WHERE job_id = ?", (job.id,)).fetchone()
    assert row["input"] == "uv run pytest -q"
    ctx = _Ctx()
    asyncio.run(server._progress_reporter(ctx)(job))
    assert re.fullmatch(r"running: \d+s, 0 turns, 1 tool calls, 1 errors; Bash: uv run pytest -q", ctx.reports[-1][2])


def test_wait_job_times_out_and_still_reports(reg, monkeypatch):
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.02)
    ctx = _Ctx()
    job = _prev_job()
    out = asyncio.run(server.wait_job(job.id, timeout_s=0.1, ctx=ctx))
    assert out["status"] == "queued" and out["id"] == job.id
    assert 2 <= len(ctx.reports) <= 6


def test_wait_without_context_is_silent(reg):
    job = _prev_job()
    out = asyncio.run(server.wait_job(job.id, timeout_s=0.05))
    assert out["id"] == job.id


def test_tool_schemas_hide_the_context_parameter():
    for name in ("run_agent", "wait_job", "wait_jobs"):
        tool = server.mcp._tool_manager.get_tool(name)
        assert "ctx" not in tool.parameters["properties"]
        assert tool.context_kwarg == "ctx"


def test_wait_jobs_all_waits_for_every_job(reg, monkeypatch):
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.02)
    ctx = _Ctx()
    a, b = _prev_job(), _prev_job()

    async def run():
        asyncio.create_task(_finish_later(a, 0.05))
        asyncio.create_task(_finish_later(b, 0.15))
        return await server.wait_jobs([a.id, b.id], timeout_s=5, ctx=ctx)

    out = asyncio.run(run())
    assert out["outcome"] == "all_done"
    assert [j["id"] for j in out["done"]] == [a.id, b.id] and out["running"] == []
    assert ctx.reports and ctx.reports[-1][1] == 2  # total = number of jobs
    assert any(m.startswith("1/2 done") for _, _, m in ctx.reports)


def test_wait_jobs_any_returns_on_first_finish(reg):
    a, b = _prev_job(), _prev_job()

    async def run():
        asyncio.create_task(_finish_later(b, 0.05))
        return await server.wait_jobs([a.id, b.id], timeout_s=5, mode="any")

    out = asyncio.run(run())
    assert out["outcome"] == "some_done"
    assert [j["id"] for j in out["done"]] == [b.id]
    assert [j["id"] for j in out["running"]] == [a.id]
    assert out["running"][0]["status"] == "queued"


def test_wait_jobs_timeout_and_bad_arguments(reg):
    a = _prev_job()
    out = asyncio.run(server.wait_jobs([a.id], timeout_s=0.05))
    assert out["outcome"] == "timeout" and out["done"] == [] and len(out["running"]) == 1
    assert asyncio.run(server.wait_jobs([a.id, "nope"], timeout_s=1))["error"] == "unknown_job"
    assert asyncio.run(server.wait_jobs([], timeout_s=1))["error"] == "bad_argument"
    assert asyncio.run(server.wait_jobs([a.id], timeout_s=1, mode="first"))["error"] == "bad_argument"


def test_wait_jobs_already_finished_returns_immediately(reg):
    a = _prev_job()
    a.status = "failed"
    a._done.set()
    out = asyncio.run(server.wait_jobs([a.id], timeout_s=60))
    assert out["outcome"] == "all_done" and out["done"][0]["status"] == "failed"


def test_instructions_and_descriptions_fit_claude_codes_cap():
    """Claude Code truncates server instructions and each tool description at 2048 characters."""
    assert len(server.INSTRUCTIONS) <= 2048, len(server.INSTRUCTIONS)
    for tool in server.mcp._tool_manager.list_tools():
        assert len(tool.description or "") <= 2048, tool.name


def test_swarm_guidance_names_the_rearm_trap():
    """Every session reads the pattern from here: one blocking run_agent per job is the rule and
    `wait_jobs(mode="any")` carries its re-arm warning, so a later trim cannot drop either."""
    assert "one blocking run_agent per job" in server.INSTRUCTIONS
    assert "re-arm" in server.INSTRUCTIONS
    desc = {t.name: t.description or "" for t in server.mcp._tool_manager.list_tools()}
    assert "Fallback only" in desc["wait_jobs"] and "re-arm" in desc["wait_jobs"]
    assert "swarms included" in desc["run_agent"]
