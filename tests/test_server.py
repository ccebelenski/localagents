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
    assert out["error"] == "job_running" and prev.name in out["message"]
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
