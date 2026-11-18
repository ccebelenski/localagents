"""Foreground-only jobs and the truthful record of a session held open by background tasks
(the 2026-11-15 incident).

The incident, live: a job whose model ran a Bash command with ``run_in_background: true`` ended
its turn ("[done] subtype=error_max_turns" at 87 s), was re-invoked when the background task
finished, left a watcher shell that never exits, and stayed ``running`` holding its slot until
its own timeout fired at 300 s. The record then said ``timeout``, "exceeded timeout of 300s":
the result the session had reported, and its error, were gone.

The SDK messages here are built by the SDK's own parser (``parse_message``) from dicts shaped
like the bundled CLI's emitters (2.1.239: ``subtype:"task_started",task_id,tool_use_id,
description,task_type,...``; ``subtype:"task_notification",task_id,tool_use_id,status,
output_file,summary,usage``; ``subtype:"task_updated",task_id,patch``), so a field the parser
requires and the fake forgot fails here, not live."""

import asyncio
import time

import pytest
from claude_agent_sdk._internal.message_parser import parse_message

from localagents import runner, server
from localagents.registry import Defaults, Endpoint, Registry
from localagents.harnesses import claude_code

SERVING = {"e": ["served-model"]}


def _reg(**defaults):
    endpoints = {n: Endpoint(name=n, base_url=f"http://{n}:8080", backend="llama.cpp") for n in SERVING}
    return Registry(path=None, endpoints=endpoints, defaults=Defaults(job_retention_days=0, **defaults),
                    raw={}, local={})


@pytest.fixture
def live(monkeypatch):
    """run_agent over the REAL runner.run_job; only the model side is faked (query, the metrics
    probe, the shim's start). ``live.reg`` is the registry run_agent reads; replace it to change
    the config's defaults or an endpoint."""

    class Live:
        reg = _reg()

    async def probe(self, ep):
        return {"up": True, "models": SERVING.get(ep.name, ["served-model"])}

    async def no_start():
        return 0

    async def no_metrics(*a, **kw):
        return None

    monkeypatch.setattr(Registry, "probe", probe)
    monkeypatch.setattr(server, "_reg", lambda: Live.reg)
    monkeypatch.setattr(server, "run_job", runner.run_job)
    monkeypatch.setattr(server.shim, "start", no_start)
    monkeypatch.setattr(runner, "fetch_metrics", no_metrics)
    return Live


# ---- the CLI's messages, through the SDK's parser ----

def init(session="sess-1"):
    return parse_message({"type": "system", "subtype": "init", "session_id": session, "model": "m",
                          "cwd": "/w"})


def started(task_id, description, task_type="local_bash"):
    return parse_message({"type": "system", "subtype": "task_started", "task_id": task_id,
                          "tool_use_id": f"tu-{task_id}", "description": description,
                          "task_type": task_type, "uuid": f"u-s-{task_id}", "session_id": "sess-1"})


def notified(task_id, status="completed"):
    return parse_message({"type": "system", "subtype": "task_notification", "task_id": task_id,
                          "tool_use_id": f"tu-{task_id}", "status": status, "output_file": "",
                          "summary": f"task {task_id} {status}", "uuid": f"u-n-{task_id}",
                          "session_id": "sess-1"})


def updated(task_id, **patch):
    return parse_message({"type": "system", "subtype": "task_updated", "task_id": task_id,
                          "patch": patch, "uuid": f"u-u-{task_id}", "session_id": "sess-1"})


def result(text, subtype="success", turns=3):
    return parse_message({"type": "result", "subtype": subtype, "duration_ms": 5, "duration_api_ms": 4,
                          "is_error": subtype != "success", "num_turns": turns, "session_id": "sess-1",
                          "result": text})


def text(t):
    return parse_message({"type": "assistant", "message": {"role": "assistant", "model": "m",
                                                          "content": [{"type": "text", "text": t}]}})


def scripted(monkeypatch, steps, then_hang=True, seen=None):
    """A fake ``query`` that yields each step (a message, or a float: sleep that long), then
    hangs until cancelled (its deadline, a cancel_job) when ``then_hang``. ``seen`` collects
    the options each session was started with."""

    def fake_query(prompt, options):
        if seen is not None:
            seen.append(options)

        async def session():
            for s in steps:
                if isinstance(s, (int, float)):
                    await asyncio.sleep(s)
                else:
                    yield s
            if then_hang:
                await asyncio.sleep(3600)

        return session()

    monkeypatch.setattr(claude_code, "query", fake_query)


# ---- step 1: the incident, reproduced ----

def test_a_result_held_open_by_a_background_task_keeps_its_result_at_the_timeout(live, monkeypatch):
    """The incident's shape: the session reports its result while a background task it started
    is still alive, and the task never notifies; the session's timeout ends it. The record keeps
    the result and its error says the session had reported one and what held it open, not
    only "exceeded timeout"."""
    scripted(monkeypatch, [init(), started("b1", "pytest -q in the background"), text("Started the run."),
                           result("Waiting for the command to finish.")])
    out = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=5, timeout_s=1))
    assert out["status"] == "timeout"
    assert out["result"] == "Waiting for the command to finish."
    assert out["error"] != "exceeded timeout of 1s", out["error"]
    assert "reported a result" in out["error"] and "pytest -q in the background" in out["error"], out["error"]


# ---- step 2: foreground only by default; the per-job value; the config keys ----

FOREGROUND_ENV = ("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS")


def _res(env=None):
    from localagents.registry import Resolution

    ep = Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp", env=env or {})
    return Resolution.make(ep, "served-model", {"up": True, "models": ["served-model"]})


def test_build_env_foreground_by_default():
    env = claude_code.build_env(_res(), "http://127.0.0.1:1/job/x", Defaults(), False)
    assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert env["BASH_DEFAULT_TIMEOUT_MS"] == "600000" and env["BASH_MAX_TIMEOUT_MS"] == "1800000"
    d = Defaults(bash_timeout_s=30, bash_max_timeout_s=90)
    env = claude_code.build_env(_res(), "http://127.0.0.1:1/job/x", d, False)
    assert env["BASH_DEFAULT_TIMEOUT_MS"] == "30000" and env["BASH_MAX_TIMEOUT_MS"] == "90000"


def test_build_env_with_background_tasks_sets_none_of_it():
    env = claude_code.build_env(_res(), "http://127.0.0.1:1/job/x", Defaults(), True)
    assert not set(FOREGROUND_ENV) & set(env), env


def test_build_env_the_endpoint_env_wins_over_the_foreground_keys():
    over = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0", "BASH_MAX_TIMEOUT_MS": "5000"}
    env = claude_code.build_env(_res(over), "http://127.0.0.1:1/job/x", Defaults(), False)
    assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "0" and env["BASH_MAX_TIMEOUT_MS"] == "5000"
    assert env["BASH_DEFAULT_TIMEOUT_MS"] == "600000"  # what the endpoint did not name stays


def _one_session(live, monkeypatch, **run_agent_kw):
    """Run one job to its result; returns (the record, the options its session got, its
    transcript)."""
    seen = []
    scripted(monkeypatch, [init(), result("done")], then_hang=False, seen=seen)
    out = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=5, timeout_s=5, **run_agent_kw))
    assert out["status"] == "succeeded", out
    job = server.store.get(out["id"])
    return out, seen[0], job.transcript_tail(0)


def _foreground(opts, transcript):
    """What the session ran with, read from every place that says it: (env switch, Monitor
    disallowed, prompt sentence, [start] line)."""
    append = opts.system_prompt.get("append") or ""
    return (opts.env.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS") == "1",
            "Monitor" in opts.disallowed_tools,
            claude_code.FOREGROUND_NOTE in append,
            "background_tasks=off" in transcript)


def test_a_job_runs_with_background_tasks_by_default(live, monkeypatch):
    # the default is ON (the operator's call, 2026-11-16, on the store's record: 24 of 209
    # jobs backgrounded a test suite or a server and none was stuck after a result)
    out, opts, transcript = _one_session(live, monkeypatch)
    assert _foreground(opts, transcript) == (False, False, False, False)
    assert "background_tasks=on" in transcript
    assert not set(FOREGROUND_ENV) & set(opts.env)


def test_a_config_default_of_false_runs_foreground_only(live, monkeypatch):
    live.reg = _reg(background_tasks=False)
    out, opts, transcript = _one_session(live, monkeypatch)
    assert _foreground(opts, transcript) == (True, True, True, True)
    assert opts.env["BASH_DEFAULT_TIMEOUT_MS"] == "600000" and opts.env["BASH_MAX_TIMEOUT_MS"] == "1800000"
    assert {"Agent", "Task"} <= set(opts.disallowed_tools)  # the config's own list is kept
    assert opts.system_prompt["append"].count(claude_code.FOREGROUND_NOTE) == 1


def test_a_config_default_of_true_runs_as_today(live, monkeypatch):
    live.reg = _reg(background_tasks=True)
    out, opts, transcript = _one_session(live, monkeypatch)
    assert _foreground(opts, transcript) == (False, False, False, False)
    assert "background_tasks=on" in transcript
    assert not set(FOREGROUND_ENV) & set(opts.env)
    assert opts.disallowed_tools == ["Agent", "Task"]


def test_an_explicit_true_over_a_config_default_of_false(live, monkeypatch):
    live.reg = _reg(background_tasks=False)
    out, opts, transcript = _one_session(live, monkeypatch, background_tasks=True)
    assert _foreground(opts, transcript) == (False, False, False, False)


def test_an_explicit_false_over_a_config_default_of_true(live, monkeypatch):
    live.reg = _reg(background_tasks=True)
    out, opts, transcript = _one_session(live, monkeypatch, background_tasks=False)
    assert _foreground(opts, transcript) == (True, True, True, True)


def test_the_endpoint_env_wins_over_the_resolved_value_in_the_session(live, monkeypatch):
    live.reg = _reg()
    live.reg.endpoints["e"].env = {"BASH_MAX_TIMEOUT_MS": "5000"}
    out, opts, transcript = _one_session(live, monkeypatch, background_tasks=False)
    assert opts.env["BASH_MAX_TIMEOUT_MS"] == "5000"
    assert opts.env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_monitor_already_disallowed_by_the_config_is_not_listed_twice(live, monkeypatch):
    live.reg = _reg(disallowed_tools=["Agent", "Monitor"])
    out, opts, transcript = _one_session(live, monkeypatch)
    assert opts.disallowed_tools == ["Agent", "Monitor"]


def test_a_resume_keeps_the_first_runs_value_unless_the_call_passes_one(live, monkeypatch):
    seen = []
    scripted(monkeypatch, [init(), result("done")], then_hang=False, seen=seen)

    async def go():
        first = await server.run_agent(task="t", model="served-model", wait_s=5, timeout_s=5,
                                       background_tasks=True)
        kept = await server.run_agent(task="more", resume_job=first["id"], wait_s=5, timeout_s=5)
        changed = await server.run_agent(task="more", resume_job=kept["id"], wait_s=5, timeout_s=5,
                                         background_tasks=False)
        again = await server.run_agent(task="more", resume_job=changed["id"], wait_s=5, timeout_s=5)
        return first, kept, changed, again

    outs = asyncio.run(go())
    assert [o["status"] for o in outs] == ["succeeded"] * 4, outs
    assert [o.resume for o in seen] == [None, "sess-1", "sess-1", "sess-1"]
    on = [o.env.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS") != "1" for o in seen]
    assert on == [True, True, False, False]


def test_run_agent_refuses_a_background_tasks_that_is_not_a_bool(live):
    out = asyncio.run(server.run_agent(task="t", model="served-model", background_tasks="false"))
    assert out["error"] == "bad_argument" and "background_tasks" in out["message"]


def test_the_record_does_not_carry_the_value_in_either_summary(live, monkeypatch):
    """No column holds it (no schema change): the in-process and the row summaries agree by
    both leaving it out; the transcript's [start] line is where it is read."""
    from localagents import derive

    out, opts, transcript = _one_session(live, monkeypatch)
    row = server.store.conn.execute("SELECT * FROM jobs WHERE id = ?", (out["id"],)).fetchone()
    assert "background_tasks" not in out
    assert "background_tasks" not in derive.summary_from_row(server.store.conn, row)


def test_the_config_keys_parse(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text("defaults:\n  background_tasks: false\n  bash_timeout_s: 30\n  bash_max_timeout_s: 90\n")
    d = Registry.load(p).defaults
    assert (d.background_tasks, d.bash_timeout_s, d.bash_max_timeout_s) == (False, 30, 90)
    p.write_text("defaults: {}\n")
    d = Registry.load(p).defaults
    assert (d.background_tasks, d.bash_timeout_s, d.bash_max_timeout_s) == (True, 600, 1800)


@pytest.mark.parametrize("key, bad", [
    ("background_tasks", '"false"'), ("background_tasks", "1"),
    ("bash_timeout_s", "0"), ("bash_timeout_s", '"600"'), ("bash_timeout_s", "true"), ("bash_timeout_s", "1.5"),
    ("bash_max_timeout_s", "-1"),
])
def test_a_bad_config_value_is_named_and_the_default_kept(tmp_path, capsys, key, bad):
    p = tmp_path / "models.yaml"
    p.write_text(f"defaults:\n  {key}: {bad}\nendpoints:\n  e:\n    base_url: http://e:8080\n")
    reg = Registry.load(p)  # never raises: the server re-reads the config on every call
    assert getattr(reg.defaults, key) == getattr(Defaults(), key)
    err = capsys.readouterr().err
    assert f"defaults: {key} must be" in err, err
    assert "e" in reg.endpoints and reg.endpoints["e"].invalid is None


# ---- step 3: the session's end and the record, with background tasks (Part 2 a, b, c) ----

def _bare_job():
    job = server.store.new(task="t", model="m", endpoint="e", base_url="http://e:8080", backend="llama.cpp",
                           host="local", cwd="/w", run_cwd="/w", isolation="none")
    job.status, job.started_at = "running", time.time()
    return job


def _feed(job, *msgs):
    for m in msgs:
        claude_code._handle_message(job, m)
    return job


def test_the_typed_messages_are_what_the_parser_returns():
    """The fakes above go through the SDK's own parser: the classes and fields the runner reads."""
    from claude_agent_sdk import TaskNotificationMessage, TaskStartedMessage, TaskUpdatedMessage

    s, n, u = started("b1", "d"), notified("b1", "failed"), updated("b1", status="killed")
    assert isinstance(s, TaskStartedMessage) and s.subtype == "task_started" and s.data["task_id"] == "b1"
    assert isinstance(n, TaskNotificationMessage) and n.status == "failed"
    assert isinstance(u, TaskUpdatedMessage) and u.status == "killed" and u.data["patch"] == {"status": "killed"}


def test_session_over_at_a_result_with_no_task_alive():
    job = _feed(_bare_job(), init(), text("hi"), result("done"))
    assert job._session_over is True and job.held_open is False


def test_session_not_over_at_a_result_a_background_task_holds_open():
    job = _feed(_bare_job(), init(), started("b1", "sleep 40"), result("waiting"))
    assert job._session_over is False and job.held_open is True
    assert job._tasks == {"b1": "sleep 40"}


def test_session_goes_on_when_re_invoked_and_is_over_at_the_last_result():
    job = _feed(_bare_job(), init(), text("hi"), result("first"))
    assert job._session_over is True
    _feed(job, init())  # re-invoked (no task messages from this SDK: the degrade path)
    assert job._session_over is False and job._after_result is False
    _feed(job, result("second"))
    assert job._session_over is True and job.result == "second"
    _feed(job, text("more"))  # an assistant turn after a result is the session going on too
    assert job._session_over is False


@pytest.mark.parametrize("end", [
    lambda: notified("b1", "completed"), lambda: notified("b1", "failed"), lambda: notified("b1", "stopped"),
    lambda: updated("b1", status="completed"), lambda: updated("b1", status="failed"),
    lambda: updated("b1", status="killed"),
])
def test_every_terminal_status_ends_the_task(end):
    job = _feed(_bare_job(), init(), started("b1", "sleep 40"), result("waiting"), end())
    assert job._tasks == {} and job.held_open is False
    # the task's end alone does not make the session over: the harness takes a turn with it next
    assert job._session_over is False
    _feed(job, init(), text("the outcome"), result("final"))
    assert job._session_over is True and job.result == "final"


def test_a_non_terminal_update_or_a_progress_message_keeps_the_task():
    progress = parse_message({"type": "system", "subtype": "task_progress", "task_id": "b1", "description": "d",
                              "usage": {"total_tokens": 1, "tool_uses": 1, "duration_ms": 1},
                              "uuid": "u-p", "session_id": "sess-1"})
    job = _feed(_bare_job(), init(), started("b1", "sleep 40"), result("waiting"),
                updated("b1", status="running", description="sleep 41"), progress)
    assert job._tasks == {"b1": "sleep 41"} and job.held_open is True


def test_a_task_that_ended_before_the_result_holds_nothing_open():
    """A foreground subagent (or a background command that finished in the turn) is a task too:
    started and ended before the result, it leaves the session over at the result."""
    job = _feed(_bare_job(), init(), started("a1", "explore", task_type="local_agent"), notified("a1"),
                updated("a1", status="completed"), result("done"))
    assert job._session_over is True and job._tasks == {}
    assert job.transcript_tail(0).count("[task] completed a1") == 1  # the late update writes no second line


def test_the_record_follows_the_latest_result_error_included():
    job = _feed(_bare_job(), init(), result(None, subtype="error_max_turns"))
    assert job.error == "error_max_turns:" and job.result is None
    _feed(job, init(), result("the real answer"))
    assert job.error is None and job.result == "the real answer" and job.num_turns == 3


def test_the_second_smoke_the_real_answer_is_the_last_result(live, monkeypatch):
    """The 2026-11-15 second job: its first result was "Waiting for the command to finish.",
    its real answer came in the second, after the background command finished."""
    scripted(monkeypatch, [init(), started("b1", "python3 slow.py"), result("Waiting for the command to finish."),
                           0.2, notified("b1"), init(), text("It printed 42."), result("42")], then_hang=False)
    out = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=5, timeout_s=5))
    assert out["status"] == "succeeded" and out["result"] == "42" and out["error"] is None


def test_a_wait_at_its_cap_on_a_held_open_session_is_not_held(live, monkeypatch):
    """Today's rule held a wait at its cap once a result arrived ("the CLI is exiting"); a
    result that a background task holds open is not the CLI exiting, so the cap stands."""
    scripted(monkeypatch, [init(), started("b1", "sleep 3600"), result("waiting")])

    async def go():
        t0 = time.monotonic()
        out = await server.run_agent(task="t", model="served-model", wait_s=1, timeout_s=60)
        took = time.monotonic() - t0
        await server.cancel_job(out["id"])
        return out, took

    out, took = asyncio.run(go())
    assert out["status"] == "running" and took < 3, (out["status"], took)


def test_the_timeout_after_a_result_names_the_tasks_that_held_it_open(live, monkeypatch):
    scripted(monkeypatch, [init(), started("b1", "pytest -q"), started("b2", "tail -f log"), result("partial")])
    out = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=5, timeout_s=1))
    assert out["status"] == "timeout" and out["result"] == "partial"
    assert out["error"] == ("the session reported a result at 0 s and was still held open by 2 background "
                            "tasks: pytest -q; tail -f log when the timeout of 1 s ended it"), out["error"]


def test_the_timeout_after_an_error_result_keeps_that_error(live, monkeypatch):
    """The incident's own shape: error_max_turns, then the watcher that never exits."""
    scripted(monkeypatch, [init(), started("b1", "watch the run"), result(None, subtype="error_max_turns")])
    out = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=5, timeout_s=1))
    assert out["status"] == "timeout" and out["result"] is None
    assert out["error"] == ("the session reported a result at 0 s (error_max_turns) and was still held open "
                            "by 1 background task: watch the run when the timeout of 1 s ended it"), out["error"]


def test_more_than_three_tasks_are_counted_past_the_third(live, monkeypatch):
    scripted(monkeypatch, [init(), *[started(f"b{i}", f"cmd {i}") for i in range(5)], result("r")])
    out = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=5, timeout_s=1))
    assert "held open by 5 background tasks: cmd 0; cmd 1; cmd 2 and 2 more when" in out["error"], out["error"]


def test_the_timeout_of_a_re_invoked_session_says_it_was_still_running(live, monkeypatch):
    scripted(monkeypatch, [init(), started("b1", "sleep 1"), result("waiting"), notified("b1"), init(),
                           text("Looking at the output.")])
    out = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=5, timeout_s=1))
    assert out["status"] == "timeout" and out["result"] == "waiting"
    assert out["error"] == ("the session reported a result at 0 s and was still running when the timeout "
                            "of 1 s ended it"), out["error"]


def test_a_cancel_after_a_result_keeps_it(live, monkeypatch):
    scripted(monkeypatch, [init(), started("b1", "sleep 3600"), result("waiting")])

    async def go():
        waiter = asyncio.create_task(server.run_agent(task="t", model="served-model", wait_s=30, timeout_s=60))
        await asyncio.sleep(0.3)
        await server.cancel_job(next(iter(server.store.jobs)))
        return await waiter

    out = asyncio.run(go())
    assert out["status"] == "cancelled" and out["result"] == "waiting"
    assert out["error"] == ("the session reported a result at 0 s and was still held open by 1 background "
                            "task: sleep 3600 when a cancel ended it"), out["error"]


def test_a_timeout_with_no_result_reads_as_today(live, monkeypatch):
    scripted(monkeypatch, [init(), started("b1", "sleep 3600"), text("working")])
    out = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=5, timeout_s=1))
    assert out["status"] == "timeout" and out["error"] == "exceeded timeout of 1s" and out["result"] is None


# ---- step 4: the held-open line (Part 2 d) and the degrade rule ----

def test_the_activity_line_says_what_holds_a_reported_result_open():
    job = _feed(_bare_job(), init(), started("b1", "uv run pytest -q"), text("Running the tests."),
                result("Waiting for the command to finish."))
    assert job.last_activity == "result reported; waiting on 1 background task: uv run pytest -q"
    assert job.summary()["last_activity"] == job.last_activity  # job_status's field
    _feed(job, notified("b1"))
    assert job.last_activity == "result reported; its background tasks ended"
    _feed(job, init())
    assert job.last_activity == "re-invoked after its result"
    _feed(job, text("All 12 passed."))
    assert job.last_activity == "All 12 passed."


def test_the_line_counts_and_cuts_like_the_error():
    from localagents import derive

    long = "x" * 200 + "\nsecond line"
    line = derive.held_open_line([long, "b", "c", "d"])
    assert line == "result reported; waiting on 4 background tasks: " + "x" * 80 + "; b; c and 1 more"
    assert derive.held_open_line([""]) == "result reported; waiting on 1 background task: (no description)"


def test_job_status_and_the_row_carry_the_line(live, monkeypatch):
    from localagents import derive

    scripted(monkeypatch, [init(), started("b1", "sleep 3600"), result("waiting")])

    async def go():
        out = await server.run_agent(task="t", model="served-model", wait_s=1, timeout_s=60)
        status = await server.job_status(out["id"])
        row = server.store.conn.execute("SELECT * FROM jobs WHERE id = ?", (out["id"],)).fetchone()
        await server.cancel_job(out["id"])
        return out, status, derive.summary_from_row(server.store.conn, row)

    out, status, from_row = asyncio.run(go())
    want = "result reported; waiting on 1 background task: sleep 3600"
    assert out["last_activity"] == status["last_activity"] == from_row["last_activity"] == want


class _Ctx:
    def __init__(self):
        self.messages = []

    async def report_progress(self, progress, total=None, message=None):
        self.messages.append(message)


def test_the_progress_line_shows_the_held_open_line_in_place_of_the_last_tool(live, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    from localagents import jobs

    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.1)
    bash = AssistantMessage(content=[ToolUseBlock(id="tu-1", name="Bash", input={
        "command": "sleep 3600", "run_in_background": True})], model="m")
    scripted(monkeypatch, [init(), bash, 0.3, started("b1", "sleep 3600"), result("waiting")])
    ctx = _Ctx()

    async def go():
        out = await server.run_agent(task="t", model="served-model", wait_s=1, timeout_s=60, ctx=ctx)
        await server.cancel_job(out["id"])

    asyncio.run(go())
    early = [m for m in ctx.messages if m and m.endswith("Bash: sleep 3600")]
    held = [m for m in ctx.messages if m and m.endswith("; result reported; waiting on 1 background task: sleep 3600")]
    assert early and held, ctx.messages


def test_progress_activity_is_the_held_open_line_or_the_last_tool():
    from localagents import derive

    assert derive.progress_activity("result reported; waiting on 1 background task: x", "Bash: x") \
        == "result reported; waiting on 1 background task: x"
    assert derive.progress_activity("Running the tests.", "Bash: x") == "Bash: x"
    assert derive.progress_activity(None, None) is None


def test_the_status_line_shows_the_held_open_line(live, monkeypatch):
    from localagents import status

    job = _feed(_bare_job(), init(), started("b1", "sleep 3600"), result("waiting"))
    job.changed()
    entries, _ = status.collect(conn=server.store.conn)
    assert len(entries) == 1, entries
    lines = status.render_lines(owner=entries[0]["owner"])
    assert any("result reported; waiting on 1 background task: sleep 3600" in ln for ln in lines), lines


def test_a_held_open_session_gets_no_stall_notice(live):
    from localagents.router import dispatch

    job = _feed(_bare_job(), init(), started("b1", "sleep 3600"), result("waiting"))
    job.started_at = time.time() - 3 * dispatch.STALL_S
    job.changed()
    asyncio.run(dispatch.stall_scan(server.store, server.store.conn, live.reg, time.time()))
    assert job.last_activity == "result reported; waiting on 1 background task: sleep 3600"
    assert job._stall_seen is None
    _feed(job, notified("b1"), init())  # re-invoked: an ordinary running session again
    asyncio.run(dispatch.stall_scan(server.store, server.store.conn, live.reg, time.time()))
    assert job.last_activity.startswith("stalled: no progress for")


def test_without_task_messages_the_session_reads_as_today(live, monkeypatch):
    """The degrade rule: an SDK that emits no task messages gives no held-open state and no
    held-open line; a result makes the session over as today, and a re-invocation (its [init],
    an assistant turn) makes it go on, never a wrong "over" for the turn that follows."""
    job = _feed(_bare_job(), init(), text("Started it."), result("Waiting for the command to finish."))
    assert job._session_over is True and job.held_open is False and job.last_activity == "Started it."
    _feed(job, init())
    assert job._session_over is False and job.last_activity == "Started it."
    scripted(monkeypatch, [init(), text("Started it."), result("first")])
    out = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=5, timeout_s=1))
    assert out["status"] == "timeout" and out["result"] == "first"
    assert out["error"] == "the session reported a result at 0 s and was still running when the timeout of 1 s ended it"


# ---- the tool surface: the description line and the stub ----

def test_run_agent_describes_background_tasks_within_the_cap():
    desc = {t.name: t.description or "" for t in server.mcp._tool_manager.list_tools()}["run_agent"]
    assert "background_tasks: false: foreground only, the job ends at its result; true (config" in desc
    assert "a resume keeps its job's" in desc
    assert len(desc) <= 2048, len(desc)


def test_the_stub_records_background_tasks(tmp_path, monkeypatch):
    import json

    from localagents.router import stubmcp

    log = tmp_path / "stub.jsonl"
    monkeypatch.setenv("LOCALAGENTS_STUB_LOG", str(log))
    asyncio.run(stubmcp.run_agent(task="t", background_tasks=True))
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["args"]["background_tasks"] is True, rec
