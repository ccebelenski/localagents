import asyncio
import json
import re
import time
from pathlib import Path

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


def _settle(job, status="succeeded"):
    """As run_job leaves a job: final status, finally done (resume needs a settled job)."""
    job.status = status
    job._done.set()
    return job


def test_resume_refuses_a_job_that_has_not_settled(reg):
    """Review of 1.7, "also noticed": resume_job took a running job, and one still in run_job's
    finally (status final, finish_worktree running in its thread) let the new job reuse a tree
    the old one was about to remove. It is refused until the previous job has settled."""
    prev = _prev_job()
    prev.status = "running"
    out = asyncio.run(server.run_agent(task="next", resume_job=prev.id))
    assert out["error"] == "job_running" and prev.id in out["message"]
    prev.status = "succeeded"  # status final, finally still running
    assert asyncio.run(server.run_agent(task="next", resume_job=prev.id))["error"] == "job_running"
    _settle(prev)
    out = asyncio.run(server.run_agent(task="next", resume_job=prev.id))
    assert out["error"] is None and out["model"] == "big-model"


def test_resume_keeps_previous_model(reg):
    prev = _settle(_prev_job())
    out = asyncio.run(server.run_agent(task="next", resume_job=prev.id))
    assert out["model"] == "big-model" and out["endpoint"] == "second"


def test_resume_with_explicit_model_overrides(reg):
    prev = _settle(_prev_job())
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
    return _settle(job)


def test_resume_worktree_job_keeps_tree_even_when_removed(reg, tmp_path):
    prev = _prev_worktree_job(tmp_path)
    out = asyncio.run(server.run_agent(task="next", resume_job=prev.id))
    assert out.get("error") is None
    assert out["isolation"] == "worktree"
    assert out["run_cwd"] == prev.run_cwd and out["cwd"] == prev.cwd
    # the tree's identity is carried over; the previous outcome (kept/note) is not
    assert out["worktree"] == {k: prev.worktree[k] for k in ("repo", "path", "root", "branch", "base")}


def test_resume_plain_job_with_missing_cwd_is_an_error(reg, tmp_path):
    prev = _settle(_prev_job())
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
    # "1 turns": 3.7h — the runner counts a turn per AssistantMessage, so the line is live
    assert re.fullmatch(r"running: \d+s, 1 turns, 1 tool calls, 1 errors; Bash: uv run pytest -q", ctx.reports[-1][2])


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


def test_wait_jobs_lists_a_job_still_in_its_finally_as_running(reg):
    """Review of 1.7, finding 1: a job whose status is final but whose run_job finally is still
    finishing its worktree (`_done` unset) is not handed back as done: its record is incomplete."""
    a, b = _prev_job(), _prev_job()
    a.status = "failed"
    a._done.set()
    b.status = "succeeded"  # status set, finally still running
    out = asyncio.run(server.wait_jobs([a.id, b.id], timeout_s=5, mode="any"))
    assert out["outcome"] == "some_done"
    assert [j["id"] for j in out["done"]] == [a.id] and [j["id"] for j in out["running"]] == [b.id]


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


def test_startup_writes_survive_a_locked_database(monkeypatch, tmp_path, capsys):
    """Story 1.7 item 4: a locked or broken database at startup costs the two status-line
    writes, not the server (the spec accepts a dropped update, not a dropped server)."""
    import sqlite3

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = jobs.JobStore()
    store.describe_server(config="/cfg/models.yaml", transport="stdio")  # first write lands...

    def boom(now=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "mark_lost_jobs", boom)
    server._startup_writes(store, "/cfg/models.yaml", "stdio")  # must return normally
    assert "database is locked" in capsys.readouterr().err
    row = store.conn.execute(
        "SELECT config, transport FROM mcp_servers WHERE id = ?", (store.server_id,)
    ).fetchone()
    assert row["config"] == "/cfg/models.yaml" and row["transport"] == "stdio"


def test_startup_writes_survive_a_newer_schema_database(monkeypatch, tmp_path, capsys):
    """Review of 1.9a, row 1: the store opens lazily on first use, and _startup_writes is
    usually main's first use — so a file at a newer schema raises db.SchemaError (a
    RuntimeError, not a sqlite3.Error) out of both guards and startup crashed instead of
    dropping the two writes. The server still starts; each dropped write names itself."""
    import sqlite3

    path = tmp_path / "localagents" / "localagents.db"  # conftest's LOCALAGENTS_DB
    path.parent.mkdir(parents=True)
    other = sqlite3.connect(path)
    other.execute("PRAGMA user_version = 99")
    other.commit()
    other.close()

    store = jobs.JobStore()
    server._startup_writes(store, "/cfg/models.yaml", "stdio")  # must return normally
    err = capsys.readouterr().err
    assert err.count("newer than this localagents knows") == 2  # describe line AND sweep line


def test_startup_sweep_runs_when_the_describe_write_fails(monkeypatch, tmp_path, capsys):
    """Review of 1.7, finding 11: the two startup writes are independent; a failed describe
    must not skip the sweep of crashed servers' jobs."""
    import sqlite3

    store = jobs.JobStore()
    swept = []

    def locked(**cols):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "describe_server", locked)
    monkeypatch.setattr(store, "mark_lost_jobs", lambda now=None: swept.append(1) or 0)
    server._startup_writes(store, "/cfg/models.yaml", "stdio")
    assert swept == [1]
    assert capsys.readouterr().err.count("database is locked") == 1


def test_startup_removes_the_old_status_directory(monkeypatch, tmp_path):
    """Story 1.7 item 6: ~/.local/state/localagents/status/ held the pre-1.5a per-pid JSON
    files; nothing writes or reads it any more, and the startup helper deletes it when present."""
    old = tmp_path / "localagents" / "status"
    old.mkdir(parents=True)
    (old / "12345.json").write_text("{}")
    store = jobs.JobStore()
    server._startup_writes(store, "/cfg/models.yaml", "stdio")
    assert not old.exists()
    server._startup_writes(store, "/cfg/models.yaml", "stdio")  # absent: no-op, no error


def test_shim_port_write_failure_cannot_orphan_a_queued_job(reg, monkeypatch):
    """Review of 1.7, finding 4: describe_server(shim_port=...) ran after new() and before the
    task was created, so a locked database left a `queued` job with no task, for good, and set the
    once-guard anyway. The job runs; the port is recorded on a later call."""
    import sqlite3

    started = []

    async def record_run(job, *a, **kw):
        started.append(job.id)

    monkeypatch.setattr(server, "run_job", record_run)
    monkeypatch.setattr(server.shim, "port", 4242)
    real = server.store.describe_server

    def locked(**cols):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(server.store, "describe_server", locked)

    async def go():
        out = await server.run_agent(task="t")
        await asyncio.sleep(0)  # let the task start
        return out

    out = asyncio.run(go())
    assert out["error"] is None and out["status"] == "queued"  # a job record, not a refusal
    job = server.store.get(out["id"])
    assert job._task is not None and started == [job.id]
    assert server._shim_port_recorded is False  # not recorded: the next call tries again
    monkeypatch.setattr(server.store, "describe_server", real)
    asyncio.run(go())
    assert server._shim_port_recorded is True
    row = server.store.conn.execute("SELECT shim_port FROM mcp_servers WHERE id = ?", (server.store.server_id,)).fetchone()
    assert row["shim_port"] == 4242


@pytest.mark.parametrize("describe_raises", ["sqlite", "other"])
def test_nothing_between_new_and_the_task_can_orphan_a_job(reg, monkeypatch, capsys, describe_raises):
    """DeepSeek review of 6005f94: the shim_port failure handler wrote its line with
    job.log_text, which raises OSError on a full disk or a read-only state directory — after
    new() and before create_task, so the job stayed `queued` with no task. The whole step is
    guarded and the line falls back to stderr."""
    import sqlite3

    started = []

    async def record_run(job, *a, **kw):
        started.append(job.id)

    def describe(**cols):
        if describe_raises == "sqlite":
            raise sqlite3.OperationalError("database is locked")
        raise OSError(30, "Read-only file system")

    def no_disk(self, text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(server, "run_job", record_run)
    monkeypatch.setattr(server.shim, "port", 4242)
    monkeypatch.setattr(server.store, "describe_server", describe)
    monkeypatch.setattr(jobs.Job, "log_text", no_disk)

    async def go():
        out = await server.run_agent(task="t")
        await asyncio.sleep(0)
        return out

    out = asyncio.run(go())
    assert out["status"] == "queued"
    job = server.store.get(out["id"])
    assert job._task is not None and started == [job.id]
    assert server._shim_port_recorded is False
    assert "shim_port" in capsys.readouterr().err


def test_cancel_job_says_when_the_settle_wait_times_out(reg, monkeypatch):
    """Story 1.9 item 3: cancel_job waited its 15 s and returned the summary as it stood,
    silently, when the settle never came — the caller could not tell a timed-out cancel from a
    completed one. The answer carries a note then; a settled cancel does not."""
    job = _prev_job()  # never settles: _done is not set

    async def short_wait(jid, timeout_s, progress=None):
        assert timeout_s == 15  # the note names this exact wait
        return job

    monkeypatch.setattr(server.store, "wait", short_wait)
    out = asyncio.run(server.cancel_job(job.id))
    assert out["note"] == "cancel requested; the job had not settled after 15 s"
    assert out["status"] == "queued" and out["id"] == job.id  # the summary as it stands

    job._done.set()
    out = asyncio.run(server.cancel_job(job.id))
    assert "note" not in out  # settled: nothing to say


def test_run_agent_answers_a_database_error_like_its_other_refusals(reg, monkeypatch):
    """Review of 1.7, finding 8: new() raising sqlite3.Error reached the caller as a bare "Error
    executing tool run_agent" (the SDK keeps the cause out of the reply). It is a refusal dict
    now, so the caller can tell a locked database from a bug."""
    import sqlite3

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(jobs.db, "lookup_id", locked)
    out = asyncio.run(server.run_agent(task="t"))
    assert out["error"] == "database_error" and "database is locked" in out["message"]
    assert server.store.jobs == {}


# --- story 1.10a row 4 (review of 1.10, D1): a cancelled wait_jobs leaves nothing behind ---
# Standing ruling (d): a cancel mid-wait, a cancel twice, a cancel landing while the finally
# awaits its tasks. The review's probe1b: wait_jobs awaited asyncio.wait(tasks), which never
# cancels its tasks, and cancelled them only after a normal return; a cancelled call orphaned
# wait_many and every _wait_foreign until timeout_s (a SELECT every poll, a sweep every 30 s).


def _foreign_running(other, task):
    job = other.new(task=task, model="m", endpoint="e", base_url="http://e:8080", backend="llama.cpp",
                    host="local", cwd="/tmp", run_cwd="/tmp", isolation="none")
    job.status, job.started_at = "running", time.time() - 10
    job.changed()
    return job


def _others():
    cur = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not cur]


async def _cancelled(task, times=1):
    for _ in range(times):
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("times", [1, 2])
def test_a_cancelled_wait_jobs_leaves_no_task_waiter_or_poll(reg, monkeypatch, times):
    """probe1b as a test: [in-process never finishing, foreign, foreign], mode any, cancelled
    at 0.1 s (once, and twice back to back): CancelledError reaches the caller, no task is left,
    the in-process job's event has no waiter, and no row is polled afterwards."""
    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.02)
    monkeypatch.setattr(server, "FOREIGN_SWEEP_S", 0.05)
    mine = _prev_job()
    mine.status = "running"
    other = jobs.JobStore()
    f1, f2 = _foreign_running(other, "f1"), _foreign_running(other, "f2")
    conn = server.store.conn
    stmts = []

    async def run():
        conn.set_trace_callback(stmts.append)
        try:
            t = asyncio.create_task(server.wait_jobs([mine.id, f1.id, f2.id], timeout_s=30, mode="any"))
            await asyncio.sleep(0.1)
            polls_before = sum(s.startswith("SELECT * FROM jobs") for s in stmts)
            await _cancelled(t, times)
            left = _others()
            waiters = len(mine._done._waiters or ())
            n = len(stmts)
            await asyncio.sleep(0.3)
            return polls_before, left, waiters, stmts[n:]
        finally:
            conn.set_trace_callback(None)

    polls_before, left, waiters, after = asyncio.run(run())
    assert polls_before > 0  # the foreign waits were polling: the check below is not vacuous
    assert left == [] and waiters == 0
    assert after == []  # no poll, no sweep, nothing


@pytest.mark.parametrize("times", [1, 2])
def test_a_cancelled_wait_jobs_stops_its_progress_reporter(reg, monkeypatch, times):
    """Story 1.10a row 5, ruling (d): the one progress reporter wait_jobs runs (row 5, D2) is a
    task of its own, cancelled and awaited in the same finally: a cancel mid-wait (once, twice)
    leaves no task and no notification after it."""
    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.02)
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.02)
    mine = _prev_job()
    mine.status = "running"
    other = jobs.JobStore()
    f1 = _foreign_running(other, "f1")
    ctx = _Ctx()

    async def run():
        t = asyncio.create_task(server.wait_jobs([mine.id, f1.id], timeout_s=30, ctx=ctx))
        await asyncio.sleep(0.1)
        await _cancelled(t, times)
        n, left = len(ctx.reports), _others()
        await asyncio.sleep(0.1)
        return n, left, len(ctx.reports)

    n, left, n_after = asyncio.run(run())
    assert n > 0 and all(total == 2 for _, total, _ in ctx.reports)
    assert left == [] and n_after == n


@pytest.mark.parametrize("times", [1, 2])
def test_a_cancelled_in_process_only_wait_jobs_leaves_nothing(reg, times):
    """probe1b': the same regression with in-process ids only (pre-1.10 awaited wait_many
    directly, so its finally ran on the cancel)."""
    a, b = _prev_job(), _prev_job()

    async def run():
        t = asyncio.create_task(server.wait_jobs([a.id, b.id], timeout_s=30))
        await asyncio.sleep(0.1)
        await _cancelled(t, times)
        return _others(), len(a._done._waiters or ()), len(b._done._waiters or ())

    assert asyncio.run(run()) == ([], 0, 0)


@pytest.mark.parametrize("path", ["cancelled", "returned"])
def test_a_cancel_during_the_finallys_await_still_awaits_every_task(reg, monkeypatch, path):
    """A cancel that lands while the finally awaits the tasks it cancelled: the awaiting runs to
    the end (a slow-to-unwind foreign wait finishes its cleanup before the caller sees the
    cancel) and the cancel still reaches the caller — on the cancelled path (a first cancel at
    0.05 s, a second while the finally awaits) and on the normal path (mode any returned at once,
    the finally awaits the other task, a cancel lands there: never swallowed into an answer)."""
    other = jobs.JobStore()
    quick, slow = _foreign_running(other, "quick"), _foreign_running(other, "slow")
    unwound = []

    async def fake_wait_foreign(job_id, *a, **kw):
        if job_id == quick.id and path == "returned":
            return {"id": job_id}
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)  # a slow unwind: the finally must wait for it
            unwound.append(job_id)
            raise

    monkeypatch.setattr(server, "_wait_foreign", fake_wait_foreign)

    async def run():
        t = asyncio.create_task(server.wait_jobs([quick.id, slow.id], timeout_s=30, mode="any"))
        if path == "cancelled":
            await asyncio.sleep(0.05)
            t.cancel()
        await asyncio.sleep(0.1)  # inside the finally's await: the slow unwind takes 0.2 s
        assert not t.done()
        await _cancelled(t)
        return list(unwound), _others()

    got_unwound, left = asyncio.run(run())
    expected = [quick.id, slow.id] if path == "cancelled" else [slow.id]
    assert sorted(got_unwound) == sorted(expected) and left == []


def _monitor_import_hits(source):
    """Story 3.7-fold-2b's guard walker, over module source text: every spelling of an import
    of the monitor package. Story 3.7-fold-4s completed it (DeepSeek review of f670097/6e686bf:
    the AST walk caught every static spelling except three) — it now also catches the runtime
    forms ``importlib.import_module("localagents.monitor...")`` / ``__import__(...)`` and the
    package-then-attribute form ``from localagents import monitor``."""
    import ast

    tree = ast.parse(source)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "localagents.monitor" or a.name.startswith("localagents.monitor."):
                    hits.append(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            # absolute: from localagents.monitor import x; relative inside the localagents
            # package (these files are all one level below its root): from .monitor import x
            if node.module is None:  # from . import x: the names are the modules
                mods = [a.name for a in node.names] if node.level >= 1 else []
            else:
                mods = [node.module]
            for m in mods:
                top = m.split(".", 1)[0]
                if top == "monitor" and node.level >= 1:
                    hits.append(f"from {'.' * node.level}{node.module or ''}")
                elif m == "localagents.monitor" or m.startswith("localagents.monitor."):
                    hits.append(f"from {m}")
            # from localagents import monitor (the package, then the attribute)
            if node.module == "localagents" and any(a.name == "monitor" for a in node.names):
                hits.append("from localagents import monitor")
        elif isinstance(node, ast.Call):
            # importlib.import_module("localagents.monitor..."), __import__("localagents.monitor...")
            # and the from-import spelling import_module(...); only literal first arguments are
            # decidable, and those are the forms a static import smuggles into a runtime call
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if fname in ("import_module", "__import__") and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str) and (
                        a0.value == "localagents.monitor" or a0.value.startswith("localagents.monitor.")):
                    hits.append(f"{fname}({a0.value!r})")
    return hits


def test_the_mcp_server_side_never_imports_the_monitor_package():
    """The decided rule (HANDOFF "Decided, do not reopen", and db.py's module docstring): the
    MCP server never imports from ``localagents.monitor``. Story 3.7-fold-2 briefly broke it —
    the version-6 migration lazily imported ``monitor.read`` for the one-time witness backfill —
    and the rule moved that backfill to the monitor's start. This guard is the regression net:
    it parses the AST of every module the MCP server imports at steady state (``__init__.py`` is
    excluded on purpose: its monitor import is lazy, inside the ``monitor`` command branch, and
    that laziness is itself load-bearing) and fails on any absolute or relative import of the
    monitor package, wherever it sits — including inside a function, and including the runtime
    spellings the static import statements do not cover (3.7-fold-4s)."""
    src = Path(__file__).resolve().parent.parent / "src" / "localagents"
    core = ["server.py", "jobs.py", "runner.py", "status.py", "derive.py", "registry.py",
            "shim.py", "db.py"]
    assert all((src / m).is_file() for m in core), "a core module moved; update this guard"
    offenders = {m: hits for m in core if (hits := _monitor_import_hits((src / m).read_text()))}
    assert not offenders, f"the MCP server side imports the monitor package: {offenders}"


def test_the_guard_catches_the_runtime_import_spellings():
    """Story 3.7-fold-4s (DeepSeek review of 6e686bf, the guard's three misses): each of
    importlib.import_module("localagents.monitor..."), __import__("localagents.monitor...")
    and from localagents import monitor is caught; look-alikes that name other packages are
    not."""
    caught = {
        "importlib.import_module": 'import importlib\nm = importlib.import_module("localagents.monitor.read")\n',
        "importlib.import_module (package)": 'import importlib\nm = importlib.import_module("localagents.monitor")\n',
        "from importlib import import_module": "from importlib import import_module\nm = import_module('localagents.monitor.state')\n",
        "__import__": '__import__("localagents.monitor")\n',
        "from localagents import monitor": "from localagents import monitor\n",
        "from localagents import db, monitor": "from localagents import db, monitor\n",
    }
    for form, source in caught.items():
        assert _monitor_import_hits(source), f"the guard misses {form}"
    clean = {
        "other package via importlib": 'import importlib\nm = importlib.import_module("json.decoder")\n',
        "sibling package": "from localagents import db, jobs\n",
        "monitor as a substring only": '__import__("localagents2.monitor")\n',
    }
    for form, source in clean.items():
        assert not _monitor_import_hits(source), f"the guard falsely flags {form}"


def test_wait_jobs_counts_a_repeated_id_once(reg, monkeypatch):
    """Review 3.9A, A1: an id given twice is one job. The classification loop walked job_ids
    verbatim, so the same record came back twice in `done` (or twice in `running`) and the
    progress line's total counted it twice."""
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.02)
    ctx = _Ctx()
    a = _prev_job()

    async def run():
        asyncio.create_task(_finish_later(a, 0.05))
        return await server.wait_jobs([a.id, a.id], timeout_s=5, ctx=ctx)

    out = asyncio.run(run())
    assert out["outcome"] == "all_done"
    assert [j["id"] for j in out["done"]] == [a.id] and out["running"] == []
    assert ctx.reports and ctx.reports[-1][1] == 1  # total = number of distinct jobs
    running = asyncio.run(server.wait_jobs([_prev_job().id] * 2, timeout_s=0))
    assert running["outcome"] == "timeout" and len(running["running"]) == 1


# --- router 12: model=auto routes, every job passes admission, tickets ------------------------
# docs/superpowers/specs/2026-11-02-router-design.md §4 (the dispatch path, the ticket, the
# "Consumer requirement"), §6, §7 (queues, "Accepted", "Decided" 2026-11-04), §14. A queued
# job's task waits inside the asyncio.run that started it, so each scenario runs in ONE loop:
# a loop's close cancels a task still waiting, and a cancel while queued drops the job.

STRONG = {"software_dev": 4, "tool_use": 4, "computer_use": 4, "complexity": 4, "judgement": 4,
          "completeness": 4, "adherence": 4}
WEAK = {"software_dev": 1, "tool_use": 1, "computer_use": 1, "complexity": 1, "judgement": 1,
        "completeness": 4, "adherence": 4}


def _row_with_cells(conn, endpoint, served, cells):
    """A scorecard row with a complete core run and one pass of these cells (rebuilt here from
    tests/router/test_dispatch.py, never imported: ruling (o))."""
    from localagents import db
    from localagents.router import definitions, scorecard

    eid = db.lookup_id(conn, "endpoints", name=endpoint, base_url=f"http://{endpoint}:8080",
                       backend="llama.cpp", host="local")
    mid = db.lookup_id(conn, "models", served_id=served)
    row = scorecard.get_or_create_row(conn, endpoint, served, eid, mid)
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
                 " VALUES (?, ?, 'core', '2026.11', '{}', 1.0, 'complete')", (db.new_uuid(), row.id))
    pass_id = db.new_uuid()
    conn.execute("INSERT INTO scorecard_passes (id, row_id, at, constants, observations)"
                 " VALUES (?, ?, 2.0, '{}', 0)", (pass_id, row.id))
    version = definitions.load(conn).version
    for dimension, estimate in cells.items():
        conn.execute("INSERT INTO scorecard_cells (pass_id, dimension, language, estimate, count,"
                     " probes_n, jobs_n, thin, defs_version) VALUES (?, ?, '', ?, 6, 6, 0, 0, ?)",
                     (pass_id, dimension, estimate, version))
    return row.id


@pytest.fixture
def routed(monkeypatch, tmp_path):
    """The fleet of the `reg` fixture plus the router entry `auto`; one slot per endpoint, a
    131k window per served id; `first` is a weak row and `second` a strong one, so a brief
    demanding software_dev 3 routes to second x big-model. d3 is a faked dispatch.score that
    counts its calls. run_job never runs, so an admitted job holds its slot."""
    import copy
    from types import SimpleNamespace

    from localagents.router import definitions, dispatch
    from localagents.router.decision import BriefScores

    endpoints = {n: Endpoint(name=n, base_url=f"http://{n}:8080", backend="llama.cpp") for n in SERVING}
    endpoints["auto"] = Endpoint(name="auto", base_url="", backend="router",
                                 decision_url="http://d3.test:8766")
    r = Registry(path=None, endpoints=endpoints, defaults=Defaults(), raw={}, local={})
    facts = {n: {"up": True, "models": list(m), "context": {m[0]: 131_072},
                 "slots": {"total": 1, "busy": 0}} for n, m in SERVING.items()}

    async def probe(self, ep):
        if ep.is_router:
            return {"up": True, "models": [ep.name], "router": True}
        return copy.deepcopy(facts[ep.name])

    scored = []

    async def fake_score(client, url, model, text, defs, **kw):
        scored.append(text)
        values = {"software_dev": 3, "complexity": 2, "tool_use": 2}
        undetermined = {d for d in definitions.DIMENSIONS if d not in values}
        return BriefScores(values, {d: 0.9 for d in values}, undetermined, False, {}, defs.version)

    async def no_run(*a, **kw):
        return None

    async def no_start():
        return 0

    monkeypatch.setattr(Registry, "probe", probe)
    monkeypatch.setattr(server, "_reg", lambda: r)
    monkeypatch.setattr(server, "run_job", no_run)
    monkeypatch.setattr(server.shim, "start", no_start)
    monkeypatch.setattr(dispatch, "score", fake_score)
    monkeypatch.setattr(dispatch, "POLL_S", 0.01)
    conn = server.store.conn
    rows = {"first": _row_with_cells(conn, "first", "default-model", WEAK),
            "second": _row_with_cells(conn, "second", "big-model", STRONG)}
    return SimpleNamespace(reg=r, facts=facts, scored=scored, conn=conn, rows=rows)


def _ticket_row(job_id):
    return server.store.conn.execute("SELECT * FROM router_tickets WHERE job_id = ?", (job_id,)).fetchone()


def test_run_agent_auto_records_a_ticket_and_the_real_endpoint(routed, tmp_path):
    """§4 step 5: the routed job's record is created on the real endpoint with a
    router_tickets row naming the chosen row; job_status carries the ticket's summary."""
    out = asyncio.run(server.run_agent(task="build the parser", model="auto", cwd=str(tmp_path)))
    assert out.get("error") is None, out
    assert out["endpoint"] == "second" and out["model"] == "big-model"
    t = _ticket_row(out["id"])
    assert t is not None and t["chosen_row"] == routed.rows["second"] and t["divert"] == 0
    rows = {r["row_id"]: r for r in json.loads(t["rows"])}
    assert set(rows) == set(routed.rows.values())
    assert json.loads(t["flags"])["priority"] == "normal"
    status = asyncio.run(server.job_status(out["id"]))
    assert status["ticket"]["chosen"] == "second x big-model"
    assert status["ticket"]["divert"] is False and status["ticket"]["admitted"] is not None
    assert routed.scored == ["build the parser"]


def test_run_agent_auto_record_names_served_model_endpoint_and_routed_by(routed, tmp_path):
    """Consumer requirement (§4): model/endpoint/base_url are the real row's, never 'auto' or
    the router entry's name; routed_by names the router and the one-line reason; a named job's
    routed_by is None; both are visible in list_jobs rows, in-process and from the database."""
    out = asyncio.run(server.run_agent(task="build the parser", model="auto", cwd=str(tmp_path)))
    assert (out["model"], out["endpoint"], out["base_url"]) == ("big-model", "second", "http://second:8080")
    assert "auto" not in (out["model"], out["endpoint"], out["base_url"])
    assert out["routed_by"]["router"] == "auto" and out["routed_by"]["pool"] is None
    assert out["routed_by"]["reason"].startswith("shortfall 0.00 on second x big-model")
    named = asyncio.run(server.run_agent(task="t", model="default-model", cwd=str(tmp_path)))
    assert named["routed_by"] is None and named["endpoint"] == "first"
    for rows in (asyncio.run(server.list_jobs()), asyncio.run(server.list_jobs(all=True))):
        by_id = {r["id"]: r for r in rows}
        assert by_id[out["id"]]["routed_by"] == out["routed_by"]
        assert by_id[out["id"]]["model"] == "big-model" and by_id[out["id"]]["endpoint"] == "second"
        assert by_id[named["id"]]["routed_by"] is None


def test_run_agent_auto_brief_unresolved_creates_no_job(routed, tmp_path):
    """§4 step 1, §14: a pointer brief whose file is missing is brief_unresolved and no job
    record exists, in memory or in the database; d3 was never asked."""
    out = asyncio.run(server.run_agent(task="cat missing.md and follow it", model="auto",
                                       cwd=str(tmp_path)))
    assert out["error"] == "brief_unresolved"
    assert server.store.jobs == {}
    assert server.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert routed.scored == []


def test_run_agent_named_model_queues_when_sessions_are_full_and_force_skips(reg, tmp_path):
    """§7 "Directly named jobs pass the same gate": the reg fixture's endpoint has one session
    (unknown capacity), the first job holds it (run_job never runs), the second waits in our
    queue at position 1, and admit='force' skips the wait with an admitted, forced entry."""
    async def go():
        first = await server.run_agent(task="a", model="default-model", cwd=str(tmp_path))
        second = await server.run_agent(task="b", model="default-model", cwd=str(tmp_path))
        forced = await server.run_agent(task="c", model="default-model", cwd=str(tmp_path),
                                        admit="force")
        entries = {r["job_id"]: r for r in server.store.conn.execute("SELECT * FROM queue_entries")}
        return first, second, forced, entries, server.store.get(second["id"])._task.done()

    first, second, forced, entries, second_done = asyncio.run(go())
    assert entries[first["id"]]["admitted"] is not None and entries[first["id"]]["forced"] == 0
    assert entries[second["id"]]["admitted"] is None and second_done is False
    assert second["status"] == "queued" and second["queue"]["position"] == 1
    assert entries[forced["id"]]["admitted"] is not None and entries[forced["id"]]["forced"] == 1
    assert forced["queue"] is None


def test_forced_job_record_says_forced(reg, tmp_path):
    """RULE (design §7: "``admit: force`` ... still writes an admitted entry, so it is counted;
    the record says it forced"): a forced job's summary (``list_jobs``/``job_status``, in
    process and read back from the row) shows ``forced: true`` beside the queue facts, and a
    named forced job — which has no ticket — says it too; the ticket's ``flags.forced`` stays."""
    async def go():
        forced = await server.run_agent(task="pin", model="default-model", cwd=str(tmp_path),
                                        admit="force")
        plain = await server.run_agent(task="wait", model="default-model", cwd=str(tmp_path))
        job = server.store.get(forced["id"])
        job.status, job.finished_at = "succeeded", time.time()   # over: no queue block either
        job.changed()
        return (forced, plain, await server.job_status(forced["id"]),
                await server.job_status(plain["id"]), await server.list_jobs(),
                await server.list_jobs(all=True))

    forced, plain, status, plain_status, listed, listed_all = asyncio.run(go())
    assert status["status"] == "succeeded" and status["queue"] is None
    assert status["forced"] is True
    assert plain_status["status"] == "queued" and plain_status["forced"] is False
    assert {r["id"]: r for r in listed}[forced["id"]]["forced"] is True
    assert {r["id"]: r for r in listed_all}[forced["id"]]["forced"] is True
    assert {r["id"]: r for r in listed_all}[plain["id"]]["forced"] is False


def test_run_agent_admit_now_returns_would_queue_and_creates_no_job(routed, tmp_path):
    """§4: admit='now' is "run it or tell me": the chosen row cannot admit at once, so the call
    returns would_queue with the position, the expected wait, the alternatives that could admit
    now and (routed) the ticket, and creates no job."""
    async def go():
        held = await server.run_agent(task="a", model="big-model", cwd=str(tmp_path))
        before = server.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        named = await server.run_agent(task="b", model="big-model", cwd=str(tmp_path), admit="now")
        auto = await server.run_agent(task="c", model="auto", cwd=str(tmp_path), admit="now")
        after = server.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        return held, named, auto, before, after

    held, named, auto, before, after = asyncio.run(go())
    assert held.get("error") is None
    for out in (named, auto):
        assert out["error"] == "would_queue", out
        assert out["position"] == 1 and out["expected_wait_s"] > 0
        assert isinstance(out["alternatives"], list)
    assert named["ticket"] is None
    assert auto["ticket"]["chosen"] == "second x big-model"
    # the weak row on `first` is free: it could admit now, listed as not acceptable
    assert [(a["endpoint"], a["acceptable"]) for a in auto["alternatives"]] == [("first", False)]
    assert before == after == 1


def test_stalled_session_raises_a_notice_and_is_not_cancelled(reg, tmp_path, monkeypatch):
    """§7 "Stalled sessions": a wake scans this process's running admitted jobs; one with no
    progress for STALL_S gets its endpoint probed, a [stalled] line and a notice, and is
    never cancelled."""
    from localagents.router import dispatch

    monkeypatch.setattr(dispatch, "POLL_S", 0.01)

    async def go():
        first = await server.run_agent(task="a", model="default-model", cwd=str(tmp_path))
        job = server.store.get(first["id"])
        job.status, job.started_at = "running", time.time() - dispatch.STALL_S - 60
        job.changed()
        await server.run_agent(task="b", model="default-model", cwd=str(tmp_path))  # queues: a wake runs
        await asyncio.sleep(0.1)
        return job

    job = asyncio.run(go())
    transcript = Path(job.log_dir, "transcript.txt").read_text()
    assert "[stalled] stalled: no progress for 16 min, endpoint up" in transcript
    assert transcript.count("[stalled]") == 1   # one notice per stall, not one per wake
    assert job.last_activity == "stalled: no progress for 16 min, endpoint up"
    assert job.status == "running" and not job._task.cancelled()


def test_stall_notice_fires_with_an_empty_queue(reg, tmp_path):
    """Fold 2 (§7 "Stalled sessions"): the scan ran only inside the admission wake, so a
    stalled session went unnoticed while nothing was queued behind it. It rides the sampler's
    periodic tick now: the notice arrives with an empty queue, once, and nothing is cancelled."""

    from localagents.router import dispatch

    async def go():
        first = await server.run_agent(task="a", model="default-model", cwd=str(tmp_path))
        job = server.store.get(first["id"])
        job.status, job.started_at = "running", time.time() - dispatch.STALL_S - 60
        job.changed()
        assert not server.store.conn.execute(
            "SELECT 1 FROM queue_entries WHERE admitted IS NULL").fetchone()  # nothing queued
        await server._sample_tick()
        return job

    job = asyncio.run(go())
    transcript = Path(job.log_dir, "transcript.txt").read_text()
    assert "[stalled] stalled: no progress for 16 min, endpoint up" in transcript
    assert transcript.count("[stalled]") == 1
    assert job.status == "running" and not job._task.cancelled()


def test_sampler_skips_queued_jobs(reg, tmp_path, monkeypatch):
    """Fold 2: the sampler treated every not-done job as running and probed a waiting job's
    endpoint every LOAD_SAMPLE_S while it waited. It samples running sessions only; a queued
    job's endpoint is probed by its own admission wake, not by the sampler."""
    probed: list[str] = []
    canned = Registry.probe   # the reg fixture's canned probe

    async def probe(self, ep):
        probed.append(ep.name)
        return await canned(self, ep)

    monkeypatch.setattr(Registry, "probe", probe)

    async def go():
        a = await server.run_agent(task="a", model="default-model", cwd=str(tmp_path))
        b = await server.run_agent(task="b", model="big-model", cwd=str(tmp_path))
        job_a = server.store.get(a["id"])
        job_a.status = "running"   # a session on `first`; `second` is free, so b admitted
        job_a.changed()
        probed.clear()             # what is left: the tick's own probes
        await server._sample_tick()
        return b

    b = asyncio.run(go())
    assert b["status"] == "queued"   # admitted, but no session has run: the sampler must not probe it
    assert probed == ["first"]


def test_sampler_writes_the_session_capacity_admission_gates_on(reg, tmp_path):
    """status-sessions: the sampler's endpoint_load text is ``status.load_sample``: the session
    capacity ``capacity.from_probe`` gives admission (slots, capped by the entry's
    max_concurrent; a pooled box's pool over the served window, the smallest when several
    served ids run there; one 'until probed'), the live requests from_probe reads, the
    server's queue depth, and our admitted sessions at the same tick (queue.occupancy). A
    probe that could not see the endpoint writes no capacity: the line never says free."""
    from localagents import status
    from localagents.router import queue

    job = server.store.new(task="t", model="m", endpoint="first", base_url="http://first:8080",
                           backend="llama.cpp", host="local", cwd="/tmp", run_cwd="/tmp", isolation="none")
    queue.enqueue(server.store.conn, job.id, job.endpoint_id, server.store.server_id, "normal", forced=True)
    eid = job.endpoint_id

    def sample(ep, probe, served=("m",)):
        return status.read_sample(server._load_sample(ep, probe, list(served), eid))

    slots = Endpoint(name="first", base_url="http://first:8080", backend="llama.cpp")
    assert sample(slots, {"up": True, "slots": {"total": 4, "busy": 0}}) == {
        "sessions": 4, "running": 0, "waiting": 0, "ours": 1}
    capped = Endpoint(name="first", base_url="http://first:8080", backend="strata", max_concurrent=2)
    assert sample(capped, {"up": True, "slots": {"total": 4, "busy": 3},
                           "load": {"requests_waiting": 2}}) == {
        "sessions": 2, "running": 3, "waiting": 2, "ours": 1}
    pooled = Endpoint(name="first", base_url="http://first:8080", backend="vllm")
    probe = {"up": True, "context": {"m": 32768, "big": 65536},
             "load": {"kv_cache_size_tokens": 100_000, "requests_running": 1, "requests_waiting": 0}}
    assert sample(pooled, probe) == {"sessions": 3, "running": 1, "waiting": 0, "ours": 1}
    assert sample(pooled, probe, ("m", "big"))["sessions"] == 1   # the smaller count of the two
    assert sample(slots, {"up": True, "models": ["m"]})["sessions"] == 1   # unknown: one until probed
    assert sample(slots, {"up": False, "models": [], "error": "refused"}) == {
        "sessions": None, "running": 0, "waiting": 0, "ours": 1}


def test_sampler_tick_feeds_the_sessions_header(reg, tmp_path, monkeypatch):
    """status-sessions end to end: a running session on a one-slot endpoint whose slot reads
    idle (the session is running a tool) — the operator's ``0/1 slots`` — renders as
    ``1/1 sessions`` after one sampler tick."""
    from localagents import status

    async def probe(self, ep):
        return {"up": True, "models": SERVING[ep.name], "slots": {"total": 1, "busy": 0}}

    monkeypatch.setattr(Registry, "probe", probe)

    async def go():
        a = await server.run_agent(task="a", model="default-model", cwd=str(tmp_path))
        job = server.store.get(a["id"])
        job.status, job.started_at = "running", time.time() - 30
        job.changed()
        await server._sample_tick()
        return job

    asyncio.run(go())
    lines = status.render_lines(owner=server.store.conn.execute(
        "SELECT owner FROM mcp_servers WHERE id = ?", (server.store.server_id,)).fetchone()[0])
    assert lines[0] == "local first · 1/1 sessions", lines


def test_sampler_tick_survives_a_probe_it_cannot_read(reg, tmp_path, monkeypatch):
    """status-sessions: the sample reads the probe through capacity.from_probe, which raises on
    a value it cannot read (a slot total that is not a number). That endpoint gets no sample
    (so the line shows no capacity), and the rest of the tick still runs: the stall scan is
    not skipped on every tick by one odd probe."""
    from localagents.router import dispatch

    total: list = [1]   # admission reads a good probe; the tick reads the odd one

    async def probe(self, ep):
        return {"up": True, "models": SERVING[ep.name], "slots": {"total": total[0], "busy": 0}}

    monkeypatch.setattr(Registry, "probe", probe)
    scans = []
    real_scan = dispatch.stall_scan

    async def scan(*a, **kw):
        scans.append(1)
        return await real_scan(*a, **kw)

    monkeypatch.setattr(dispatch, "stall_scan", scan)

    async def go():
        a = await server.run_agent(task="a", model="default-model", cwd=str(tmp_path))
        job = server.store.get(a["id"])
        job.status, job.started_at = "running", time.time() - 30
        job.changed()
        total[0] = "many"
        scans.clear()   # the admission wake's own scan
        await server._sample_tick()   # it returns: an exception here would fail the test
        return list(scans)

    assert asyncio.run(go())   # the tick reached its stall scan
    assert server.store.conn.execute("SELECT COUNT(*) FROM endpoint_load").fetchone()[0] == 0


def test_sampler_repairs_a_settled_job_whose_drop_write_failed(reg, tmp_path, monkeypatch):
    """Fold 2 (Task 12 review, probe 4B): a `mark_dropped` that failed at the drop (a locked
    database) left the row `queued` and the queue entry alive while the job settled `dropped`
    in memory and the caller was told so. The phantom entry is the head of its endpoint's
    queue and blocked every later entry, capacity freed or not, and nothing repaired it in
    this process. The sampler's tick now retries the drop writes of this process's settled
    jobs: the row and the entry are repaired and the entry behind the phantom admits."""
    import sqlite3

    from localagents import db
    from localagents.router import dispatch

    # c's wake must see the freed head within the test's wait: its loop polls on POLL_S
    # unless a wake reaches it, and the notify below can miss it (the task registers its
    # waker on its first step) — pace the poll like the stall tests do (fold 2 construction).
    monkeypatch.setattr(dispatch, "POLL_S", 0.01)

    broken = {"on": False}
    real_immediate = db.immediate

    def maybe_locked(conn):
        """The locked database at the drop: every immediate write raises while the flag is
        up (the same sqlite3.OperationalError a real lock raises); reads are untouched."""
        if broken["on"]:
            raise sqlite3.OperationalError("database is locked")
        return real_immediate(conn)

    monkeypatch.setattr(db, "immediate", maybe_locked)

    async def go():
        first = await server.run_agent(task="a", model="default-model", cwd=str(tmp_path))
        b = await server.run_agent(task="b", model="default-model", cwd=str(tmp_path))  # queues
        broken["on"] = True                       # the drop's writes both fail (locked database)
        rec = await server.cancel_job(b["id"])    # the caller is told it dropped
        broken["on"] = False
        row = server.store.conn.execute("SELECT status FROM jobs WHERE id = ?",
                                        (b["id"],)).fetchone()
        entry = server.store.conn.execute("SELECT 1 FROM queue_entries WHERE job_id = ?",
                                          (b["id"],)).fetchone()
        job_b = server.store.get(b["id"])
        # the phantom HEAD blocks everything behind it even after the slot frees (proven below)
        c = await server.run_agent(task="c", model="default-model", cwd=str(tmp_path))
        await server._sample_tick()               # this process repairs its own settled jobs
        row_after = server.store.conn.execute("SELECT status FROM jobs WHERE id = ?",
                                              (b["id"],)).fetchone()
        entry_after = server.store.conn.execute("SELECT 1 FROM queue_entries WHERE job_id = ?",
                                                (b["id"],)).fetchone()
        holder = server.store.get(first["id"])
        holder.status, holder.finished_at = "succeeded", time.time()
        holder.changed()                          # the slot is free
        dispatch.notify()
        await asyncio.sleep(0.3)                  # c's wake admits it now the phantom is gone
        admitted = server.store.conn.execute("SELECT admitted FROM queue_entries WHERE job_id = ?",
                                             (c["id"],)).fetchone()["admitted"]
        return rec, row, entry, job_b, row_after, entry_after, admitted

    rec, row, entry, job_b, row_after, entry_after, admitted = asyncio.run(go())
    assert rec["status"] == "dropped" and rec["error"] == "cancelled while queued"
    assert row["status"] == "queued" and entry is not None   # the defect: the phantom stands
    assert job_b.status == "dropped" and job_b.settled
    assert row_after["status"] == "dropped" and entry_after is None   # repaired by the tick
    assert "[queue-repair]" in Path(job_b.log_dir, "transcript.txt").read_text()
    assert admitted is not None                   # and the entry behind it admits now


def test_endpoint_auto_with_a_named_model_is_bad_argument(routed, tmp_path):
    """Fold 2: a router entry named as `endpoint` together with a real `model` was silently
    ignored (the router won). It is a contradiction now: bad_argument, naming both. The
    router named as `endpoint` alone still routes exactly like `model=<router>`."""
    out = asyncio.run(server.run_agent(task="t", endpoint="auto", model="default-model",
                                       cwd=str(tmp_path)))
    assert out["error"] == "bad_argument"
    assert "auto" in out["message"] and "default-model" in out["message"]
    assert server.store.jobs == {}
    out = asyncio.run(server.run_agent(task="build the parser", endpoint="auto",
                                       cwd=str(tmp_path)))
    assert out.get("error") is None, out
    assert out["endpoint"] == "second" and out["model"] == "big-model"


def test_model_auto_with_a_forced_real_endpoint_is_bad_argument(routed, tmp_path):
    """The consumer's live acceptance (2026-11-04, job ff8db6cb): `model=auto` with a real
    `endpoint` forced beside it silently dropped the endpoint and routed. The mirror of fold
    2's case, and the same contradiction: bad_argument naming both, no job. The router named
    as `model` alone still routes."""
    out = asyncio.run(server.run_agent(task="t", model="auto", endpoint="second",
                                       cwd=str(tmp_path)))
    assert out["error"] == "bad_argument"
    assert "auto" in out["message"] and "second" in out["message"]
    assert server.store.jobs == {}
    out = asyncio.run(server.run_agent(task="build the parser", model="auto", cwd=str(tmp_path)))
    assert out.get("error") is None, out
    assert out["routed_by"]["router"] == "auto"


def test_run_agent_bad_priority_is_a_dict_error(reg):
    """§4: a bad priority or admit is bad_argument, a dict, before any job exists."""
    out = asyncio.run(server.run_agent(task="t", priority="whenever"))
    assert out["error"] == "bad_argument" and "priority" in out["message"]
    out = asyncio.run(server.run_agent(task="t", admit="later"))
    assert out["error"] == "bad_argument" and "admit" in out["message"]
    assert server.store.jobs == {}


def test_job_status_shows_queue_position_while_queued(reg, tmp_path):
    """job_status of a queued job carries priority and queue {position, expected_wait_s}; an
    admitted job's queue is None."""
    async def go():
        first = await server.run_agent(task="a", model="default-model", cwd=str(tmp_path))
        second = await server.run_agent(task="b", model="default-model", cwd=str(tmp_path),
                                        priority="urgent")
        return await server.job_status(first["id"]), await server.job_status(second["id"])

    first, second = asyncio.run(go())
    assert first["queue"] is None and first["priority"] == "normal"
    assert second["status"] == "queued" and second["priority"] == "urgent"
    assert second["queue"]["position"] == 1 and second["queue"]["expected_wait_s"] > 0


def test_ticket_records_position_at_enqueue(routed, tmp_path):
    """RULE (design §4, the ticket's contents): the ticket carries ``position`` at enqueue
    beside ``est_wait_s`` — 1 for the head of the queue, None when the row admitted the job at
    once (it never waited, so it has no queue position) — and ``ticket_summary`` surfaces both.
    A forced job was admitted at once, so its position is None, and its ticket still says
    ``flags.forced``. (The divert's position is checked in tests/router/test_dispatch.py.)"""
    from localagents.router import dispatch

    async def go():
        await server.run_agent(task="hold", model="big-model", cwd=str(tmp_path))
        queued = await server.run_agent(task="build the parser", model="auto", cwd=str(tmp_path))
        ticket = dispatch.read_ticket(server.store.conn, queued["id"])
        status = await server.job_status(queued["id"])
        # admitted at once (admit='force'): it never waited, so it has no queue position
        forced = await server.run_agent(task="forced build", model="auto", cwd=str(tmp_path),
                                        admit="force")
        forced_ticket = dispatch.read_ticket(server.store.conn, forced["id"])
        forced_status = await server.job_status(forced["id"])
        return queued, ticket, status, forced_ticket, forced_status

    queued, ticket, status, forced_ticket, forced_status = asyncio.run(go())
    assert queued["status"] == "queued" and queued["endpoint"] == "second"
    assert ticket["position"] == 1 and ticket["est_wait_s"] > 0
    assert status["ticket"]["position"] == 1
    assert status["ticket"]["est_wait_s"] == ticket["est_wait_s"]
    assert status["queue"]["position"] == 1
    assert forced_ticket["admitted"] is not None and forced_ticket["position"] is None
    assert forced_ticket["flags"]["forced"] is True
    assert forced_status["forced"] is True


def test_resume_with_router_id_stays_on_its_row(routed, tmp_path):
    """§4: resume_job of a routed job with the router's name as model resumes on the same row
    (no new scoring), and the ticket is copied with flags.resumed."""
    async def go():
        prev = await server.run_agent(task="build the parser", model="auto", cwd=str(tmp_path))
        job = server.store.get(prev["id"])
        job.session_id, job.status = "sess-r", "succeeded"
        job._done.set()
        job.changed()
        nxt = await server.run_agent(task="go on", model="auto", resume_job=prev["id"])
        return prev, nxt

    prev, nxt = asyncio.run(go())
    assert nxt.get("error") is None, nxt
    assert (nxt["model"], nxt["endpoint"]) == (prev["model"], prev["endpoint"]) == ("big-model", "second")
    assert routed.scored == ["build the parser"]   # resume never rescores
    t = _ticket_row(nxt["id"])
    assert t["chosen_row"] == routed.rows["second"]
    assert json.loads(t["flags"])["resumed"] is True
    assert nxt["routed_by"]["router"] == "auto"


def test_cancel_removes_the_queue_entry(reg, tmp_path):
    """§14: "A queued job cancelled: the entry is removed, nothing was sent."""
    async def go():
        await server.run_agent(task="a", model="default-model", cwd=str(tmp_path))
        second = await server.run_agent(task="b", model="default-model", cwd=str(tmp_path))
        assert server.store.conn.execute("SELECT 1 FROM queue_entries WHERE job_id = ?",
                                         (second["id"],)).fetchone() is not None
        return second, await server.cancel_job(second["id"])

    second, out = asyncio.run(go())
    assert server.store.conn.execute("SELECT 1 FROM queue_entries WHERE job_id = ?",
                                     (second["id"],)).fetchone() is None
    assert out["status"] == "dropped" and "note" not in out


def test_run_agent_auto_avoid_job_excludes_the_builders_model(routed, tmp_path):
    """§7 "Decided": avoid_job filters every row serving that job's served id (any endpoint),
    lists the filter in the ticket and names it in routed_by.reason when it bound the choice."""
    async def go():
        builder = await server.run_agent(task="t", model="big-model", cwd=str(tmp_path), admit="force")
        review = await server.run_agent(task="review the diff", model="auto", cwd=str(tmp_path),
                                        avoid_job=builder["id"], tolerance=2.0)
        return builder, review

    builder, review = asyncio.run(go())
    assert review.get("error") is None, review
    assert review["model"] == "default-model" and review["endpoint"] == "first"
    rows = {r["row_id"]: r for r in json.loads(_ticket_row(review["id"])["rows"])}
    assert rows[routed.rows["second"]]["filtered"] == "avoid_job"
    assert "avoid_job excluded second x big-model" in review["routed_by"]["reason"]


def test_avoid_job_naming_the_named_model_is_bad_argument(reg, tmp_path):
    """§7 "Decided": a named model whose served id is the avoided job's is bad_argument; so is
    an avoid_job that names no job."""
    async def go():
        builder = await server.run_agent(task="t", model="big-model", cwd=str(tmp_path))
        same = await server.run_agent(task="r", model="big-model", cwd=str(tmp_path),
                                      avoid_job=builder["id"])
        unknown = await server.run_agent(task="r", model="big-model", cwd=str(tmp_path),
                                         avoid_job="no-such-job")
        other = await server.run_agent(task="r", model="default-model", cwd=str(tmp_path),
                                       avoid_job=builder["id"])
        return same, unknown, other

    same, unknown, other = asyncio.run(go())
    assert same["error"] == "bad_argument" and "big-model" in same["message"]
    assert unknown["error"] == "bad_argument" and "no-such-job" in unknown["message"]
    assert other.get("error") is None and other["model"] == "default-model"


def test_cancel_while_queued_is_dropped_with_started_at_null(reg, tmp_path):
    """§7 "Accepted": a job that leaves the queue without a session is `dropped`, error
    `cancelled while queued`, started_at null, settled; the row says the same."""
    async def go():
        await server.run_agent(task="a", model="default-model", cwd=str(tmp_path))
        second = await server.run_agent(task="b", model="default-model", cwd=str(tmp_path))
        return await server.cancel_job(second["id"])

    out = asyncio.run(go())
    assert (out["status"], out["error"], out["started_at"]) == ("dropped", "cancelled while queued", None)
    assert out["finished_at"] is not None
    row = server.store.conn.execute("SELECT status, error, started_at FROM jobs WHERE id = ?",
                                    (out["id"],)).fetchone()
    assert tuple(row) == ("dropped", "cancelled while queued", None)
    assert server.store.get(out["id"]).settled


def test_row_down_at_admission_is_dropped_with_ticket_and_guidance(routed, tmp_path):
    """§4 step 6 and §7 "Accepted": the routed head of a queue finds its endpoint down when its
    turn comes: dropped, error row_down, started_at null; the record carries the ticket and the
    guidance "resubmit; the brief is rescored"."""
    async def go():
        await server.run_agent(task="a", model="big-model", cwd=str(tmp_path))   # holds second
        queued = await server.run_agent(task="build the parser", model="auto", cwd=str(tmp_path))
        assert queued["status"] == "queued" and queued["queue"]["position"] == 1
        routed.facts["second"] = {"up": False, "models": [], "error": "connection refused"}
        job = server.store.get(queued["id"])
        await asyncio.wait_for(job._done.wait(), 5)
        return await server.job_status(queued["id"])

    out = asyncio.run(go())
    assert (out["status"], out["error"], out["started_at"]) == ("dropped", "row_down", None)
    assert out["ticket"]["chosen"] == "second x big-model"
    assert out["ticket"]["guidance"] == "resubmit; the brief is rescored"


def test_wait_s_is_armed_at_admission_not_enqueue(reg, tmp_path, monkeypatch):
    """§7 "Accepted" (the clock): the blocking call keeps waiting while the job is queued, its
    progress line reads `queued #n, ~m min`, and wait_s counts from the admission."""
    from localagents.router import dispatch

    monkeypatch.setattr(dispatch, "POLL_S", 0.01)
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.02)
    ctx = _Ctx()

    async def go():
        first = await server.run_agent(task="a", model="default-model", cwd=str(tmp_path))
        call = asyncio.create_task(server.run_agent(task="b", model="default-model",
                                                    cwd=str(tmp_path), wait_s=0.3, ctx=ctx))
        await asyncio.sleep(0.6)   # twice wait_s since the enqueue: still blocking
        still = not call.done()
        holder = server.store.get(first["id"])
        holder.status = "succeeded"
        holder.changed()
        dispatch.notify()
        freed = time.monotonic()
        out = await asyncio.wait_for(call, 5)
        return still, out, time.monotonic() - freed

    still, out, after_free = asyncio.run(go())
    assert still is True
    assert out["status"] == "queued" and out["queue"] is None   # admitted; run_job never ran
    assert 0.25 <= after_free < 2.0   # the wait ran its 0.3 s from the admission
    assert any(re.fullmatch(r"queued #1, ~\d+ min", m or "") for _, _, m in ctx.reports), ctx.reports


def test_routed_by_pool_when_pool_cap_blocked(routed, tmp_path):
    """§7 "Accepted": with both endpoints in pool `gpu` (max_concurrent 1) and the pool's one
    session held on `first`, the routed job queues for second although second itself is free:
    routed_by.pool names the pool, and the entry's blocked_by says pool."""
    routed.reg.pools = {"gpu": {"max_concurrent": 1}}
    for n in ("first", "second"):
        routed.reg.endpoints[n].pool = "gpu"

    async def go():
        await server.run_agent(task="a", model="default-model", cwd=str(tmp_path))   # holds the pool
        out = await server.run_agent(task="build the parser", model="auto", cwd=str(tmp_path))
        # read inside the loop: its close cancels the waiting task, which drops the job
        entry = server.store.conn.execute("SELECT blocked_by FROM queue_entries WHERE job_id = ?",
                                          (out["id"],)).fetchone()
        return out, entry

    out, entry = asyncio.run(go())
    assert out["status"] == "queued" and out["endpoint"] == "second"
    assert out["routed_by"]["pool"] == "gpu"
    assert entry["blocked_by"] == "pool"


def test_summary_shows_priority_calibration_routed_by(reg, tmp_path):
    """The phase-2 deferral closes (§13, plan Task 1's ruling): priority, calibration and
    routed_by are in summary() and summary_from_row alike; the ids and levers stay hidden."""
    from localagents import derive

    out = asyncio.run(server.run_agent(task="t", model="big-model", cwd=str(tmp_path),
                                       priority="background"))
    assert (out["priority"], out["calibration"], out["routed_by"]) == ("background", False, None)
    job = server.store.get(out["id"])
    row = server.store.conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    from_row = derive.summary_from_row(server.store.conn, row)
    for key in ("priority", "calibration", "routed_by"):
        assert job.summary()[key] == from_row[key]
    for hidden in ("mcp_server_id", "endpoint_id", "model_id", "levers", "gates"):
        assert hidden not in out and hidden not in from_row
    listed = {r["id"]: r for r in asyncio.run(server.list_jobs(all=True))}
    assert listed[job.id]["priority"] == "background"
