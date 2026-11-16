"""Story 1.5b: the summary read path and exposure (spec "Derivations" and "Tool contract
decisions"). usage_totals, tool_counts, error_count and last_tool come from the rows;
summary_from_row builds the full job_status dict for any server's job; summary() and
summary_from_row agree for the same job; the read tools cross servers, and since story 1.10
so do the waits (row-polling); only cancel_job and run_agent(resume_job=) still refuse."""

import asyncio
import json
import re
import time

import pytest

from localagents import db, derive, jobs, server


def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return jobs.JobStore()


def _new(store, **kw):
    kw.setdefault("task", "the brief")
    kw.setdefault("model", "served-model")
    kw.setdefault("endpoint", "llamacpp")
    kw.setdefault("base_url", "http://x:8080")
    kw.setdefault("cwd", "/tmp")
    kw.setdefault("run_cwd", "/tmp")
    kw.setdefault("isolation", "none")
    kw.setdefault("backend", "llama.cpp")
    kw.setdefault("host", "local")
    return store.new(**kw)


def test_usage_totals_sum_the_four_token_columns(monkeypatch, tmp_path):
    """NULL tokens count as 0; every request counts, count_tokens included; the key names are
    the API's (the shape Job.usage carried)."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    t = time.time()
    job.record_request({
        "t": t, "path": "/v1/messages", "status": 200, "ms": 100,
        "usage": {"input_tokens": 10, "output_tokens": 5,
                  "cache_read_input_tokens": 100, "cache_creation_input_tokens": 2},
    })
    job.record_request({"t": t + 1, "path": "/v1/messages/count_tokens", "status": 200, "ms": 1})  # no usage: NULLs
    job.record_request({
        "t": t + 2, "path": "/v1/messages", "status": 200, "ms": 50,
        "usage": {"input_tokens": 7, "cache_read_input_tokens": 40},  # partial usage
    })
    assert derive.usage_totals(store.conn, job.id) == {
        "input_tokens": 17, "output_tokens": 5,
        "cache_read_input_tokens": 140, "cache_creation_input_tokens": 2,
    }
    other = _new(store)  # per job; a seeded job's totals must not leak
    assert derive.usage_totals(store.conn, other.id) == {
        "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }


def test_tool_counts_groups_by_tool_with_error_sums(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    t = time.time()
    job.record_tool_call(t, "Bash", "u1", "ls")
    job.record_tool_result("u1", t + 1, False)
    job.record_tool_call(t + 1, "Bash", "u2", "make")
    job.record_tool_result("u2", t + 2, True)
    job.record_tool_call(t + 2, "Edit", "u3", "/a.py")  # result pending: NULL is_error counts as 0
    assert derive.tool_counts(store.conn, job.id) == {
        "Bash": {"calls": 2, "errors": 1},
        "Edit": {"calls": 1, "errors": 0},
    }
    assert derive.error_count(store.conn, job.id) == 1
    other = _new(store)
    assert derive.tool_counts(store.conn, other.id) == {}
    assert derive.error_count(store.conn, other.id) == 0


def test_last_tool_is_the_latest_row(monkeypatch, tmp_path):
    """``<tool>: <input>`` of the latest tool_calls row; a NULL input leaves just the tool name."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    t = time.time()
    assert derive.last_tool(store.conn, job.id) is None
    job.record_tool_call(t, "Read", "u1", json.dumps({"file_path": "/a.py"}))
    assert derive.last_tool(store.conn, job.id) == 'Read: {"file_path": "/a.py"}'
    job.record_tool_call(t + 1, "Edit", "u2", "/b.py")
    assert derive.last_tool(store.conn, job.id) == "Edit: /b.py"
    job.record_tool_call(t + 2, "Bash", "u3", None)  # edit tool without a path, or a NULL input
    assert derive.last_tool(store.conn, job.id) == "Bash"


def test_summary_from_row_reads_the_row_and_the_derivations(monkeypatch, tmp_path):
    """The full job_status dict for a jobs row: every column by its Job field name, the lookup
    ids resolved (model/endpoint/base_url), worktree parsed, metrics rebuilt, task truncated
    like Job.summary, run_cwd/log_dir/elapsed_s derived, storage ids left out."""
    store = _store(monkeypatch, tmp_path)
    wt = {"repo": "/repo", "path": "/wt/sub", "root": "/wt", "branch": "local-agent/x", "base": "abc123"}
    job = _new(store, task="x" * 400, context=131072, worktree=wt, run_cwd="/wt/sub", isolation="worktree")
    job.status, job.started_at, job.finished_at = "succeeded", 1000.0, 1060.0
    job.result, job.error, job.num_turns, job.session_id = "done", None, 4, "sess-9"
    job.metrics = {
        "generated_tokens": 100, "prompt_tokens_processed": 500, "prompt_tokens_cached": 900,
        "cache_hit_ratio": 0.64, "prompt_tps": 110.5, "generate_tps": 42.0, "spec_decode_acceptance": 0.8,
    }
    job.changed()
    job.record_request({
        "t": 990.0, "path": "/v1/messages", "status": 200, "ms": 2000,
        "usage": {"input_tokens": 100, "output_tokens": 80, "cache_read_input_tokens": 900},
    })
    job.record_tool_call(995.0, "Edit", "u1", "/a.py")
    job.record_tool_result("u1", 996.0, True)
    job.record_tool_call(997.0, "Bash", "u2", "ls")
    job.record_tool_result("u2", 998.0, False)

    row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    s = derive.summary_from_row(store.conn, row)
    assert s["id"] == job.id and "name" not in s  # 3.10f-1: the id is the one handle
    assert s["model"] == "served-model" and s["endpoint"] == "llamacpp" and s["base_url"] == "http://x:8080"
    assert s["task"] == "x" * 300 + "…"
    assert s["status"] == "succeeded" and s["result"] == "done" and s["error"] is None
    assert s["num_turns"] == 4 and s["session_id"] == "sess-9" and s["context"] == 131072
    assert s["created_at"] == pytest.approx(job.created_at)
    assert s["started_at"] == 1000.0 and s["finished_at"] == 1060.0
    assert s["elapsed_s"] == 60.0
    assert s["isolation"] == "worktree" and s["worktree"] == wt
    assert s["run_cwd"] == "/wt/sub"  # worktree.path when the tree is present
    assert s["log_dir"] == str(jobs.state_dir() / "jobs" / job.id)
    assert s["dump"] is False and s["log_events"] is False
    assert s["metrics"] == {
        "generated_tokens": 100, "prompt_tokens_processed": 500, "prompt_tokens_cached": 900,
        "cache_hit_ratio": pytest.approx(0.64), "prompt_tps": pytest.approx(110.5),
        "generate_tps": pytest.approx(42.0), "spec_decode_acceptance": pytest.approx(0.8),
    }
    assert s["usage"] == {
        "input_tokens": 100, "output_tokens": 80,
        "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0,
    }
    assert s["tool_calls"] == 2 and s["errors"] == 1
    assert s["tool_counts"] == {"Edit": {"calls": 1, "errors": 1}, "Bash": {"calls": 1, "errors": 0}}
    assert s["last_tool"] == "Bash: ls"
    assert s["files_touched"] == []  # the one Edit's result was an error: the tool wrote nothing
    assert s["diff_files"] is None  # the worktree has no recorded file list: unknown, not []
    assert s["ctx_tokens"] == 1000 and s["gen_tps"] == pytest.approx(40.0)
    for storage in ("mcp_server_id", "endpoint_id", "model_id"):
        assert storage not in s, f"{storage} is a storage detail, not job_status"

    # include_result=False drops the key, as summary() does
    assert "result" not in derive.summary_from_row(store.conn, row, include_result=False)


def test_compact_boundary_counts_the_compaction(monkeypatch, tmp_path):
    """Story 1.11 item 2: the harness's compact_boundary system message (type=system, subtype
    compact_boundary, compact_metadata = {trigger: manual|auto, pre_tokens, ...} — verified
    against the claude_agent_sdk's bundled CLI) increments job.compactions and writes a
    transcript line; the count is a jobs column the row carries, and summary() and
    summary_from_row both read it back."""
    from claude_agent_sdk import SystemMessage

    from localagents import runner

    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    msg = SystemMessage(subtype="compact_boundary", data={
        "compact_metadata": {"trigger": "auto", "pre_tokens": 150000},
    })
    runner._apply_message(job, msg)
    runner._apply_message(job, msg)
    assert job.compactions == 2
    job.changed()
    assert _row(store, job)["compactions"] == 2
    tail = job.transcript_tail().splitlines()
    assert "[compact] #1 trigger=auto pre_tokens=150000" in tail
    assert "[compact] #2 trigger=auto pre_tokens=150000" in tail
    assert job.summary()["compactions"] == 2
    assert derive.summary_from_row(store.conn, _row(store, job))["compactions"] == 2


def test_summary_from_row_without_metrics_or_worktree(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    job.changed()
    row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    s = derive.summary_from_row(store.conn, row)
    assert s["metrics"] is None
    assert s["worktree"] is None
    assert s["run_cwd"] == "/tmp"  # cwd when there is no worktree
    assert s["usage"] == {
        "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    assert s["tool_calls"] == 0 and s["errors"] == 0 and s["last_tool"] is None
    assert s["files_touched"] == [] and s["ctx_tokens"] is None and s["gen_tps"] is None


def test_summary_and_summary_from_row_agree_for_the_same_job(monkeypatch, tmp_path):
    """The in-process summary() and the row-derived summary_from_row produce the same dict."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store, context=131072)
    job.status, job.started_at, job.finished_at = "succeeded", 1000.0, 1060.0
    job.result, job.num_turns, job.session_id, job.last_activity = "all done", 3, "sess-1", "Edit /a.py"
    job.changed()
    job.record_request({
        "t": 1050.0, "path": "/v1/messages", "status": 200, "ms": 2000,
        "usage": {"input_tokens": 100, "output_tokens": 80, "cache_read_input_tokens": 900},
    })
    job.record_tool_call(1052.0, "Edit", "u1", "/a.py")
    job.record_tool_result("u1", 1053.0, False)
    row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert job.summary() == derive.summary_from_row(store.conn, row)
    assert job.summary(include_result=False) == derive.summary_from_row(store.conn, row, include_result=False)


def test_summary_hands_out_a_copy_of_the_worktree(monkeypatch, tmp_path):
    """summary() put the live worktree dict into its result: a caller mutating the summary it
    was given moved the job itself (story 1.9 item 1). The summary's dict is a copy."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store, isolation="worktree", worktree={
        "repo": "/r", "path": "/p", "root": "/rt", "branch": "local-agent/x", "base": "abc",
    })
    s = job.summary()
    assert s["worktree"] == job.worktree
    s["worktree"]["kept"] = True
    s["worktree"]["branch"] = "tampered"
    assert "kept" not in job.worktree and job.worktree["branch"] == "local-agent/x"
    assert job.summary(include_result=False)["worktree"] == {
        "repo": "/r", "path": "/p", "root": "/rt", "branch": "local-agent/x", "base": "abc",
    }  # the copy rule both ways


def test_job_status_reads_a_foreign_jobs_row(monkeypatch, tmp_path):
    """A second JobStore means a second server (its own mcp_servers row); job_status answers
    from the database even though the job is nowhere in this process."""
    other = jobs.JobStore()  # conftest points XDG_STATE_HOME at this test's dir: same file
    assert server.store is not other
    foreign = _new(other, task="the other server's brief")
    foreign.status, foreign.started_at, foreign.finished_at = "succeeded", 1000.0, 1060.0
    foreign.result = "their result"
    foreign.changed()
    foreign.record_tool_call(1050.0, "Bash", "u1", "ls")
    foreign.record_tool_result("u1", 1051.0, True)

    out = asyncio.run(server.job_status(foreign.id))
    assert out["id"] == foreign.id and "name" not in out
    assert out["task"] == "the other server's brief" and out["model"] == "served-model"
    assert out["status"] == "succeeded" and out["result"] == "their result" and out["elapsed_s"] == 60.0
    assert out["tool_calls"] == 1 and out["errors"] == 1 and out["last_tool"] == "Bash: ls"
    assert out["log_dir"] == str(jobs.state_dir() / "jobs" / foreign.id)
    assert "mcp_server_id" not in out and "endpoint_id" not in out and "model_id" not in out
    assert asyncio.run(server.job_status("no-such-job"))["error"] == "unknown_job"


def test_cancel_and_resume_refuse_a_foreign_job(monkeypatch, tmp_path):
    """cancel_job and run_agent(resume_job=) act on the process, not the record: they keep the
    clear foreign_job error, not unknown_job (operator rule 2026-10-29; story 1.10 moved the
    waits to row-polling, tested below)."""
    other = jobs.JobStore()
    foreign = _new(other)
    foreign.status, foreign.started_at = "running", time.time() - 10
    foreign.changed()

    cancel = asyncio.run(server.cancel_job(foreign.id))
    assert cancel["error"] == "foreign_job" and other.server_id in cancel["message"]
    assert cancel["message"] == (
        f"job {foreign.id} belongs to another localagents server ({other.server_id}); "
        "cancel_job and run_agent(resume_job=) need the server that started it, while "
        "job_status, job_log, list_jobs, wait_job and wait_jobs work on any job"
    )
    resumed = asyncio.run(server.run_agent(task="continue", resume_job=foreign.id))
    assert resumed["error"] == "foreign_job" and resumed["message"] == cancel["message"]


class _Ctx:
    """Stand-in for the MCP Context: records progress notifications."""

    def __init__(self):
        self.reports = []

    async def report_progress(self, progress, total=None, message=None):
        self.reports.append((progress, total, message))


def test_wait_job_polls_a_foreign_row_until_the_timeout(monkeypatch, tmp_path):
    """Story 1.10 item 1: a running foreign job is waited on by polling its row; the timeout
    answer is the row as it stands (status still running), like the in-process timeout."""
    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.05)
    monkeypatch.setattr(server, "FOREIGN_SWEEP_S", 0.1)
    other = jobs.JobStore()
    foreign = _new(other)
    foreign.status, foreign.started_at = "running", time.time() - 10
    foreign.changed()

    t0 = time.monotonic()
    out = asyncio.run(server.wait_job(foreign.id, timeout_s=1))
    waited = time.monotonic() - t0
    assert out["status"] == "running" and out["id"] == foreign.id
    assert "name" not in out and out["id"] == foreign.id  # a row summary, not an error
    assert 0.9 <= waited < 1.5
    # and an id neither this process nor the database knows is still unknown_job
    assert asyncio.run(server.wait_job("no-such-id", timeout_s=0.05))["error"] == "unknown_job"


def test_wait_job_returns_when_a_foreign_row_turns_terminal(monkeypatch, tmp_path):
    """Another server's UPDATE lands on the row; the next poll returns that server's record."""
    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.05)
    monkeypatch.setattr(server, "FOREIGN_SWEEP_S", 0.1)
    other = jobs.JobStore()
    foreign = _new(other)
    foreign.status, foreign.started_at = "running", time.time() - 10
    foreign.changed()

    async def run():
        task = asyncio.create_task(server.wait_job(foreign.id, timeout_s=10))
        await asyncio.sleep(0.2)
        with db.immediate(other.conn):
            other.conn.execute("UPDATE jobs SET status = 'succeeded', result = 'their result' WHERE id = ?",
                               (foreign.id,))
        return await task

    t0 = time.monotonic()
    out = asyncio.run(run())
    assert out["status"] == "succeeded" and out["result"] == "their result"
    assert time.monotonic() - t0 < 0.5


def test_wait_job_resolves_a_dead_servers_job_as_lost(monkeypatch, tmp_path):
    """The lost sweep runs on the foreign wait's clock: a server whose row has `stopped` set
    (its own mark at exit) is dead — its running job becomes `lost` within one sweep, not at
    the timeout (spec Liveness, mark_lost_jobs)."""
    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.05)
    monkeypatch.setattr(server, "FOREIGN_SWEEP_S", 0.1)
    other = jobs.JobStore()
    foreign = _new(other)
    foreign.status, foreign.started_at = "running", time.time() - 100
    foreign.changed()
    with db.immediate(other.conn):
        other.conn.execute("UPDATE mcp_servers SET stopped = ? WHERE id = ?", (time.time(), other.server_id))

    t0 = time.monotonic()
    out = asyncio.run(server.wait_job(foreign.id, timeout_s=10))
    assert out["status"] == "lost" and out["error"] == "server exited"
    assert time.monotonic() - t0 < 1.0


def test_foreign_wait_reports_the_same_progress_line(monkeypatch, tmp_path):
    """The foreign wait's progress line has the shape _progress_reporter pins: "status: elapsed
    s, turns turns, calls tool calls..." — elapsed from started_at, the counts derived from the
    shared rows the other server recorded."""
    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.05)
    monkeypatch.setattr(server, "FOREIGN_SWEEP_S", 0.1)
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.02)
    other = jobs.JobStore()
    foreign = _new(other)
    foreign.status, foreign.started_at = "running", time.time() - 10
    foreign.changed()
    foreign.record_tool_call(time.time(), "Bash", "u1", "ls")
    foreign.record_tool_result("u1", time.time() + 1, False)
    ctx = _Ctx()

    out = asyncio.run(server.wait_job(foreign.id, timeout_s=0.15, ctx=ctx))
    assert out["status"] == "running"
    assert ctx.reports, "the foreign wait reported nothing"
    line = re.compile(r"running: 1[01]s, 0 turns, 1 tool calls; Bash: ls")
    assert any(line.fullmatch(m) for _, _, m in ctx.reports), ctx.reports


def test_wait_jobs_mixes_in_process_and_foreign_ids(monkeypatch, tmp_path):
    """Story 1.10 item 2: in-process ids wait as always, foreign ids join the same call. mode
    all with a short timeout: the settled in-process job is done, the foreign one is running
    with the same entry keys as an in-process one."""
    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.05)
    monkeypatch.setattr(server, "FOREIGN_SWEEP_S", 0.1)
    mine = _new(server.store, task="mine")
    mine.status, mine.started_at = "succeeded", time.time() - 5
    mine._done.set()
    mine.changed()
    other = jobs.JobStore()
    foreign = _new(other, task="theirs")
    foreign.status, foreign.started_at, foreign.num_turns = "running", time.time() - 5, 3
    foreign.changed()

    out = asyncio.run(server.wait_jobs([mine.id, foreign.id], timeout_s=0.3))
    assert out["outcome"] == "some_done"
    assert [j["id"] for j in out["done"]] == [mine.id]
    assert [j["id"] for j in out["running"]] == [foreign.id]
    entry = out["running"][0]
    assert set(entry) == {"id", "status", "elapsed_s", "num_turns"}  # same keys as in-process
    assert entry["status"] == "running" and entry["num_turns"] == 3 and entry["elapsed_s"] >= 5
    # an unknown id anywhere still answers unknown_job before any waiting
    t0 = time.monotonic()
    assert asyncio.run(server.wait_jobs([foreign.id, "nope"], timeout_s=10))["error"] == "unknown_job"
    assert time.monotonic() - t0 < 0.5


def test_wait_jobs_any_returns_when_the_foreign_job_finishes_first(monkeypatch, tmp_path):
    """mode any: an UPDATE on the foreign row ends the call at once; the still-running
    in-process job is reported running (and its wait_many waiter is cancelled cleanly)."""
    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.05)
    monkeypatch.setattr(server, "FOREIGN_SWEEP_S", 0.1)
    mine = _new(server.store, task="mine")
    mine.status, mine.started_at = "running", time.time() - 5
    mine.changed()
    other = jobs.JobStore()
    foreign = _new(other, task="theirs")
    foreign.status, foreign.started_at = "running", time.time() - 5
    foreign.changed()

    async def run():
        task = asyncio.create_task(server.wait_jobs([mine.id, foreign.id], timeout_s=10, mode="any"))
        await asyncio.sleep(0.2)
        with db.immediate(other.conn):
            other.conn.execute("UPDATE jobs SET status = 'succeeded', result = 'theirs' WHERE id = ?",
                               (foreign.id,))
        return await task

    t0 = time.monotonic()
    out = asyncio.run(run())
    assert out["outcome"] == "some_done"
    assert [j["id"] for j in out["done"]] == [foreign.id]
    assert out["done"][0]["result"] == "theirs"
    assert [j["id"] for j in out["running"]] == [mine.id]
    assert time.monotonic() - t0 < 1.0  # first completion, not the timeout


def test_wait_jobs_reports_one_line_per_interval_over_every_id(monkeypatch, tmp_path):
    """Story 1.10a row 5 (review of 1.10, D2; probe4 4b as a test): wait_jobs over [mine,
    foreign_old, foreign_young] emitted three notifications per interval on one token — the
    in-process line counting 0/1, and one unnamed line per foreign id whose progress value (its
    elapsed seconds) ran 0, 500, 42, 0, 500, 42. One reporter covers every id now: one line per
    interval, progress = the done count (non-decreasing), total = len(job_ids), every unfinished
    job named in job_ids order; a foreign job that turns terminal counts as done and leaves the
    line."""
    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.02)
    monkeypatch.setattr(server, "FOREIGN_SWEEP_S", 0.1)
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.05)
    mine = _new(server.store, task="mine")
    mine.status, mine.started_at, mine.num_turns = "running", time.time() - 42, 4
    mine.changed()
    other = jobs.JobStore()
    old, young = _new(other, task="old"), _new(other, task="young")
    old.status, old.started_at = "running", time.time() - 500
    young.status, young.started_at, young.num_turns = "running", time.time() - 42, 4
    for j in (old, young):
        j.changed()
    ctx = _Ctx()

    async def run():
        task = asyncio.create_task(server.wait_jobs([mine.id, old.id, young.id], timeout_s=0.4, ctx=ctx))
        await asyncio.sleep(0.2)
        with db.immediate(other.conn):
            other.conn.execute("UPDATE jobs SET status = 'succeeded' WHERE id = ?", (young.id,))
        return await task

    out = asyncio.run(run())
    assert out["outcome"] == "some_done" and [d["id"] for d in out["done"]] == [young.id]
    reports = ctx.reports
    assert 4 <= len(reports) <= 9, reports  # one per 0.05 s interval of a 0.4 s wait, not three
    assert all(total == 3 for _, total, _ in reports), reports
    values = [p for p, _, _ in reports]
    assert values == sorted(values) and values[0] == 0 and values[-1] == 1, values
    before = re.compile(
        rf"0/3 done; {mine.id}: running 4[23]s, 4 turns; {old.id}: running 50[01]s, 0 turns; "
        rf"{young.id}: running 4[23]s, 4 turns"
    )
    after = re.compile(rf"1/3 done; {mine.id}: running 4[23]s, 4 turns; {old.id}: running 50[01]s, 0 turns")
    for p, _, m in reports:
        assert (before if p == 0 else after).fullmatch(m), m
    assert any(p == 0 for p in values) and any(p == 1 for p in values)


def test_a_failing_row_read_degrades_the_wait_jobs_line_not_its_count(monkeypatch, tmp_path, capsys):
    """Row 5's reporter reads each foreign row per interval. A read that fails degrades the line
    (the unreadable id shows `?`, one stderr line), never the wait; a row already seen terminal
    stays counted, so `progress` never goes down."""
    import sqlite3

    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.02)
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.03)
    other = jobs.JobStore()
    first, second = _new(other, task="first"), _new(other, task="second")
    for j in (first, second):
        j.status, j.started_at = "running", time.time() - 5
        j.changed()
    broken = {"on": False}
    real = server._progress_rows

    def rows(ids):
        if broken["on"]:
            raise sqlite3.OperationalError("database is locked")
        return real(ids)

    monkeypatch.setattr(server, "_progress_rows", rows)
    ctx = _Ctx()

    async def run():
        task = asyncio.create_task(server.wait_jobs([first.id, second.id], timeout_s=0.5, ctx=ctx))
        await asyncio.sleep(0.1)
        with db.immediate(other.conn):
            other.conn.execute("UPDATE jobs SET status = 'succeeded' WHERE id = ?", (first.id,))
        await asyncio.sleep(0.1)  # at least one line counts it
        broken["on"] = True
        return await task

    out = asyncio.run(run())
    assert out["outcome"] == "some_done" and [d["id"] for d in out["done"]] == [first.id]
    values = [p for p, _, _ in ctx.reports]
    assert values == sorted(values) and values[-1] == 1, ctx.reports
    assert ctx.reports[-1][2] == f"1/2 done; {second.id}: ?"
    assert "progress line degraded: OperationalError: database is locked" in capsys.readouterr().err


def test_wait_jobs_all_done_when_both_foreign_jobs_finish(monkeypatch, tmp_path):
    """Both ids foreign: the call waits for both rows and returns both records."""
    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.05)
    monkeypatch.setattr(server, "FOREIGN_SWEEP_S", 0.1)
    a_store, b_store = jobs.JobStore(), jobs.JobStore()
    job_a, job_b = _new(a_store, task="a"), _new(b_store, task="b")
    for j in (job_a, job_b):
        j.status, j.started_at = "running", time.time() - 5
        j.changed()

    async def run():
        task = asyncio.create_task(server.wait_jobs([job_a.id, job_b.id], timeout_s=10))
        await asyncio.sleep(0.2)
        with db.immediate(a_store.conn):
            a_store.conn.execute("UPDATE jobs SET status = 'succeeded' WHERE id = ?", (job_a.id,))
        await asyncio.sleep(0.1)
        with db.immediate(b_store.conn):
            b_store.conn.execute("UPDATE jobs SET status = 'failed', error = 'nope' WHERE id = ?", (job_b.id,))
        return await task

    out = asyncio.run(run())
    assert out["outcome"] == "all_done"
    assert [j["id"] for j in out["done"]] == [job_a.id, job_b.id]
    assert out["done"][0]["status"] == "succeeded" and out["done"][1]["error"] == "nope"
    assert out["running"] == []


def test_list_jobs_all_reads_every_servers_jobs(monkeypatch, tmp_path):
    mine = _new(server.store)
    other = jobs.JobStore()
    theirs = _new(other)
    theirs.status, theirs.finished_at = "succeeded", time.time()
    theirs.changed()

    mine_only = asyncio.run(server.list_jobs())
    assert [j["id"] for j in mine_only] == [mine.id]

    out = asyncio.run(server.list_jobs(all=True))
    ids = [j["id"] for j in out]
    assert mine.id in ids and theirs.id in ids
    assert ids.index(mine.id) < ids.index(theirs.id)  # newest last
    assert all("result" not in j for j in out)
    assert all("mcp_server_id" not in j for j in out)

    live = asyncio.run(server.list_jobs(include_finished=False, all=True))
    assert [j["id"] for j in live] == [mine.id]  # finished rows of any server filter out


def test_job_log_works_for_any_job_and_tails_the_tool_calls(monkeypatch, tmp_path):
    """job_log answers for another server's job (log_dir derived from its id); tools=N adds
    the last N recorded calls, oldest first."""
    other = jobs.JobStore()
    foreign = _new(other)
    for i, tool in enumerate(("Read", "Bash", "Edit", "Grep")):
        foreign.record_tool_call(1000.0 + i, tool, f"u{i}", f"/x/{i}.py" if tool == "Edit" else f"cmd {i}")
        foreign.record_tool_result(f"u{i}", 1000.5 + i, tool == "Bash")

    out = asyncio.run(server.job_log(foreign.id, tools=2))
    assert out["job_id"] == foreign.id and out["status"] == "queued"
    assert out["log_dir"] == str(jobs.state_dir() / "jobs" / foreign.id)
    assert out["transcript"] == ""  # the job never ran: no transcript file
    calls = out["tool_calls"]
    assert [c["tool"] for c in calls] == ["Edit", "Grep"]  # last two, oldest first
    assert calls[0]["input"] == "/x/2.py" and calls[0]["finished"] == 1002.5 and calls[0]["is_error"] == 0
    assert calls[1]["t"] == 1003.0 and calls[1]["is_error"] == 0
    assert "tool_calls" not in asyncio.run(server.job_log(foreign.id))  # tools=0: the key is absent
    assert asyncio.run(server.job_log("no-such-job"))["error"] == "unknown_job"


def test_job_log_tail_lines_zero_is_all_lines_negative_is_bad_argument(monkeypatch, tmp_path):
    """Story 1.9 item 7: tail_lines=0 returned every line only by Python accident
    (`lines[-0:]`). It is the contract now, as list_jobs(limit=0) is; a negative value is
    bad_argument (it used to drop that many lines from the FRONT)."""
    from pathlib import Path

    job = _new(server.store)
    (Path(job.log_dir) / "transcript.txt").write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
    all_lines = [f"line {i}" for i in range(50)]

    out = asyncio.run(server.job_log(job.id, tail_lines=0))
    assert out["transcript"].splitlines() == all_lines
    assert asyncio.run(server.job_log(job.id, tail_lines=10))["transcript"].splitlines() == all_lines[-10:]

    bad = asyncio.run(server.job_log(job.id, tail_lines=-1))
    assert bad["error"] == "bad_argument" and "tail_lines" in bad["message"]

    # a foreign job takes the same rule (its branch did the same accidental slice)
    other = jobs.JobStore()
    foreign = _new(other)
    (Path(foreign.log_dir) / "transcript.txt").write_text("\n".join(all_lines) + "\n")
    assert asyncio.run(server.job_log(foreign.id, tail_lines=0))["transcript"].splitlines() == all_lines
    assert asyncio.run(server.job_log(foreign.id, tail_lines=-1))["error"] == "bad_argument"


# --- fold of the cross-model review of 1.5b ---


def _row(store, job):
    return store.conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()


@pytest.mark.parametrize("before, after", [
    # vLLM: prefix_queries present but unchanged, so metrics_delta omits cache_hit_ratio
    ({"generated_tokens": 100, "prompt_tokens": 1000, "prefix_hits": 0, "prefix_queries": 50},
     {"generated_tokens": 300, "prompt_tokens": 1500, "prefix_hits": 0, "prefix_queries": 50}),
    # llama.cpp without the seconds counters: prompt_tps/generate_tps omitted
    ({"generated_tokens": 1, "prompt_tokens": 1}, {"generated_tokens": 5, "prompt_tokens": 9}),
    # a counter went backwards (backend restarted): the spec nulls the whole set
    ({"generated_tokens": 500, "prompt_tokens": 10}, {"generated_tokens": 5, "prompt_tokens": 20}),
])
def test_summary_metrics_follow_the_row_rule(monkeypatch, tmp_path, before, after):
    """Job.summary()["metrics"] is built by _row_update's rule: all seven keys, missing optional
    keys as None, and None outright when any counter is negative — so summary() and
    summary_from_row agree (review of 1.5b, findings 1 and 2)."""
    from localagents.registry import metrics_delta

    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    job.status, job.started_at, job.finished_at = "succeeded", 1000.0, 1060.0
    job.metrics = metrics_delta(before, after)
    job.changed()
    s = job.summary()
    assert s == derive.summary_from_row(store.conn, _row(store, job))
    if job.metrics["generated_tokens"] < 0:
        assert s["metrics"] is None
    else:
        assert set(s["metrics"]) == set(derive._METRICS_COLUMNS)


def test_metric_integer_columns_are_rounded_and_real_ones_keep_the_fraction(monkeypatch, tmp_path):
    """Ruling A: ``metrics_delta`` keeps fractional deltas, and the ``jobs`` columns declared
    INTEGER in ``db._V1`` (the three counters) are rounded when the row is written; the REAL
    columns (ratios and t/s) keep their fraction."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    job.metrics = {
        "generated_tokens": 6.47, "prompt_tokens_processed": 12.93, "prompt_tokens_cached": 1346.54,
        "cache_hit_ratio": 0.99, "prompt_tps": 2.5855, "generate_tps": 1.4355,
        "spec_decode_acceptance": 0.822,
    }
    job.changed()
    row = _row(store, job)
    assert row["generated_tokens"] == 6 and isinstance(row["generated_tokens"], int)
    assert row["prompt_tokens_processed"] == 13 and isinstance(row["prompt_tokens_processed"], int)
    assert row["prompt_tokens_cached"] == 1347 and isinstance(row["prompt_tokens_cached"], int)
    assert row["prompt_tps"] == pytest.approx(2.5855)  # a REAL column: no rounding
    s = derive.summary_from_row(store.conn, row)
    assert s["metrics"]["generated_tokens"] == 6 and s["metrics"]["prompt_tps"] == pytest.approx(2.5855)


def test_summary_metrics_none_when_unset(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    job.changed()
    assert job.summary()["metrics"] is None
    assert jobs.Job(id="x", task="t", model="m", endpoint="e", base_url="b", cwd="c",
                    run_cwd="c", isolation="none").summary()["metrics"] is None


def test_unknown_job_hint_lists_the_ids(monkeypatch, tmp_path):
    """The tools take ids; the hint shows the ten newest jobs' ids, newest first, so a caller
    can copy the one it needs (review of 1.5b; 3.10f-1: the id is the only handle)."""
    store = _store(monkeypatch, tmp_path)
    made = [_new(store, created_at=1000.0 + i) for i in range(12)]
    with pytest.raises(KeyError) as e:
        store.get("nosuch")
    msg = e.value.args[0]
    newest = made[-1]
    assert newest.id in msg
    assert msg.index(made[-1].id) < msg.index(made[-2].id)  # newest first
    assert made[1].id not in msg and made[0].id not in msg  # ten newest only


def test_unknown_job_hint_falls_back_to_the_database(monkeypatch, tmp_path):
    """A store that has run nothing says ``none`` and leaves the caller guessing: with the
    in-process list empty the ten newest rows of the shared database (any server's) name the
    job instead; a non-empty in-process list keeps its own message (review of 1.8)."""
    empty = _store(monkeypatch, tmp_path)
    other = jobs.JobStore()
    theirs = [_new(other, created_at=1000.0 + i) for i in range(12)]

    with pytest.raises(KeyError) as e:
        empty.get("nosuch")
    msg = e.value.args[0]
    assert theirs[-1].id in msg
    assert theirs[-11].id not in msg and theirs[0].id not in msg  # ten newest, from the rows

    mine = _new(empty, created_at=2000.0)
    with pytest.raises(KeyError) as e2:
        empty.get("nosuch")
    assert mine.id in e2.value.args[0] and theirs[-1].id not in e2.value.args[0]


def test_an_in_process_job_with_no_created_at_is_the_oldest(monkeypatch, tmp_path):
    """The row listing puts a NULL created_at at the oldest end; the in-process listing and the
    unknown-job hint sort the same way instead of raising TypeError on None < float (review of
    1.8, finding 7). Its summary's elapsed_s is 0, as summary_from_row's is."""
    made = [_new(server.store, created_at=1000.0 + i) for i in range(3)]
    made[1].created_at = None
    out = asyncio.run(server.list_jobs())
    assert [j["id"] for j in out] == [made[1].id, made[0].id, made[2].id]
    assert out[0]["elapsed_s"] == 0
    assert [j["id"] for j in asyncio.run(server.list_jobs(limit=1))] == [made[2].id]
    with pytest.raises(KeyError) as e:
        server.store.get("nosuch")
    msg = e.value.args[0]
    assert msg.index(made[2].id) < msg.index(made[0].id) < msg.index(made[1].id)  # newest first, None last


def test_unknown_job_hint_survives_a_database_it_cannot_open(monkeypatch, tmp_path):
    """Any failure of the hint's database fallback leaves the plain unknown_job message: a file
    whose schema is newer than this code raises db.SchemaError (a RuntimeError, not a
    sqlite3.Error), which escaped get() (review of 1.8, finding 6). job_status, whose next step
    reads the same database, answers database_error rather than raising."""
    import sqlite3

    newer = tmp_path / "newer.sqlite"
    c = sqlite3.connect(newer)
    c.execute("PRAGMA user_version = 99")
    c.close()
    monkeypatch.setenv("LOCALAGENTS_DB", str(newer))
    store = jobs.JobStore()
    with pytest.raises(KeyError) as e:
        store.get("nosuch")
    assert e.value.args[0] == "unknown job 'nosuch'. Known (newest first): none"

    monkeypatch.setattr(server, "store", store)
    s = asyncio.run(server.job_status("nosuch"))
    assert s["error"] == "database_error" and "schema version 99" in s["message"], s


def test_run_job_writes_the_worktree_before_the_first_message(monkeypatch, tmp_path):
    """run_job flushes job.worktree/run_cwd to the row as soon as the tree exists, so another
    server's job_status sees it before the session's first SDK message (review of 1.5b,
    finding 5). The query stub reads the row at the moment the session would start."""
    from localagents import runner
    from localagents.registry import Defaults, Endpoint, Registry, Resolution

    store = _store(monkeypatch, tmp_path)
    job = _new(store, isolation="worktree")
    wt = {"repo": "/repo", "path": "/wt/sub", "root": "/wt", "branch": f"local-agent/{job.id}", "base": "abc"}
    seen = {}

    async def no_metrics(*a, **kw):
        return None

    def fake_query(prompt, options):
        row = _row(store, job)
        seen["worktree"] = row["worktree"]
        seen["run_cwd"] = derive.summary_from_row(store.conn, row)["run_cwd"]

        async def nothing():
            return
            yield  # an async generator that yields no message

        return nothing()

    monkeypatch.setattr(runner, "make_worktree", lambda j: dict(wt))
    monkeypatch.setattr(runner, "finish_worktree", lambda j: None)
    monkeypatch.setattr(runner, "fetch_metrics", no_metrics)
    monkeypatch.setattr(runner, "query", fake_query)
    ep = Endpoint(name="llamacpp", base_url="http://x:8080", backend="llama.cpp")
    reg = Registry(path=None, endpoints={"llamacpp": ep}, defaults=Defaults(job_retention_days=0), raw={}, local={})
    asyncio.run(runner.run_job(
        job, reg, Resolution(ep, "served-model"), permission_mode=None, max_turns=None, allowed_tools=None,
        system_prompt_append=None, resume_session=None, timeout_s=5, shim_base_url="http://127.0.0.1:1/job/x",
    ))
    assert seen["worktree"] is not None and json.loads(seen["worktree"]) == wt
    assert seen["run_cwd"] == "/wt/sub"


@pytest.mark.parametrize("fails", ["metrics_delta", "fetch_metrics"])
def test_a_metrics_failure_in_the_finally_cannot_strand_the_job(monkeypatch, tmp_path, fails):
    """run_job's finally computes the metrics delta before it finishes the worktree, sets _done
    and writes the final row. An exception there (the 1.8 review built one: a KeyError from a
    counter whose partner was in one snapshot only) left the row `running` with no finished_at,
    the worktree unfinished and every waiter blocked. The block is guarded: one [metrics-error]
    transcript line, metrics None, and the rest of the finally runs (review of 1.8)."""
    from localagents import runner
    from localagents.registry import Defaults, Endpoint, Registry, Resolution

    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    calls = {"fetch": 0, "finish_worktree": 0}

    async def fake_metrics(*a, **kw):
        calls["fetch"] += 1
        if calls["fetch"] > 1 and fails == "fetch_metrics":
            raise RuntimeError("after-snapshot failed")
        return {"generated_tokens": 10.0, "generated_seconds": 1.0, "prompt_tokens": 5.0}

    def boom(*a, **kw):
        raise KeyError("generated_tokens")

    def fake_query(prompt, options):
        async def nothing():
            return
            yield

        return nothing()

    monkeypatch.setattr(runner, "fetch_metrics", fake_metrics)
    if fails == "metrics_delta":
        monkeypatch.setattr(runner, "metrics_delta", boom)
    monkeypatch.setattr(runner, "query", fake_query)
    monkeypatch.setattr(runner, "finish_worktree", lambda j: calls.__setitem__("finish_worktree", 1))
    ep = Endpoint(name="llamacpp", base_url="http://x:8080", backend="llama.cpp")
    reg = Registry(path=None, endpoints={"llamacpp": ep}, defaults=Defaults(job_retention_days=0), raw={}, local={})
    asyncio.run(runner.run_job(
        job, reg, Resolution(ep, "served-model"), permission_mode=None, max_turns=None, allowed_tools=None,
        system_prompt_append=None, resume_session=None, timeout_s=5, shim_base_url="http://127.0.0.1:1/job/x",
    ))
    assert job._done.is_set() and calls["finish_worktree"] == 1
    row = _row(store, job)
    assert row["status"] == job.status and row["status"] != "running" and row["finished_at"] is not None
    assert job.metrics is None and row["generated_tokens"] is None
    assert "[metrics-error] " in job.transcript_tail()


def test_list_jobs_limit_keeps_the_newest(monkeypatch, tmp_path):
    """list_jobs returns the ``limit`` newest jobs (default 50; 0 = all), still newest last; the
    history never shrinks, so all=True must not grow without bound (review of 1.5b)."""
    other = jobs.JobStore()
    made = [_new(other, created_at=1000.0 + i) for i in range(60)]
    out = asyncio.run(server.list_jobs(all=True))
    assert [j["id"] for j in out] == [j.id for j in made[-50:]]
    assert [j["id"] for j in asyncio.run(server.list_jobs(all=True, limit=0))] == [j.id for j in made]
    assert [j["id"] for j in asyncio.run(server.list_jobs(all=True, limit=3))] == [j.id for j in made[-3:]]
    assert all("result" not in j for j in out)
    # this server's own list honours the same parameter
    mine = [_new(server.store, created_at=2000.0 + i) for i in range(4)]
    assert [j["id"] for j in asyncio.run(server.list_jobs(limit=2))] == [j.id for j in mine[-2:]]
    assert len(asyncio.run(server.list_jobs(limit=0))) == 4


def test_list_jobs_newest_n_is_served_by_the_created_at_index(monkeypatch, tmp_path):
    """The newest-``limit`` half of list_jobs(all=True) is the query that would otherwise scan
    and sort the whole ``jobs`` history twice (EXPLAIN: SCAN + temp B-trees). The
    jobs(created_at) index serves it: the inner query's plan says USING INDEX (review of 1.8)."""
    other = jobs.JobStore()
    _new(other)
    plan = " ".join(
        r["detail"]
        for r in server.store.conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM (SELECT *, rowid AS _rowid FROM jobs"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?) ORDER BY created_at, _rowid",
            (50,),
        ).fetchall()
    )
    assert "USING INDEX" in plan and "jobs_created_at" in plan, plan


def _insert_row(**cols):
    """A jobs row written straight to the table: the schema has no NOT NULL, so another writer
    (or an older one) can leave any column NULL."""
    from localagents import db

    conn = server.store.conn
    jid = db.new_uuid()
    base = dict(id=jid, mcp_server_id=server.store.server_id,
                task="x", cwd="/c", isolation="none", status="succeeded", created_at=time.time())
    base.update(cols)
    with db.immediate(conn):
        conn.execute(f"INSERT INTO jobs ({', '.join(base)}) VALUES ({', '.join('?' * len(base))})", tuple(base.values()))
    return jid


def test_null_created_at_sorts_at_the_oldest_end_of_the_listing(monkeypatch, tmp_path):
    """The decision (review of 1.8): a row with no ``created_at`` means oldest. It is first in
    the full listing and survives ``limit`` >= the history; a smaller limit cuts it as the oldest
    row, never silently hides a row that would otherwise fit. SQLite already puts NULLs last in
    DESC and first in ASC, so the plain ``created_at`` ordering does this — and keeps the inner
    query served by jobs(created_at) (the ``created_at IS NULL`` prefix the review proposed would
    sort NULLs to the *newest* end of the outer re-sort and force a second temp B-tree)."""
    other = jobs.JobStore()
    made = [_new(other, created_at=1000.0 + i) for i in range(60)]
    nul = _insert_row(created_at=None)

    full = [j["id"] for j in asyncio.run(server.list_jobs(all=True, limit=0))]
    assert len(full) == 61 and full[0] == nul and full[1:] == [j.id for j in made]

    sixtyone = [j["id"] for j in asyncio.run(server.list_jobs(all=True, limit=61))]
    assert sixtyone == full

    fifty = [j["id"] for j in asyncio.run(server.list_jobs(all=True, limit=50))]
    assert fifty == [j.id for j in made[-50:]] and nul not in fifty


def test_summary_from_row_reads_nullable_columns_defensively(monkeypatch, tmp_path):
    """created_at (and started_at) NULL -> elapsed_s 0; a worktree JSON without a path ->
    run_cwd is cwd; log_dir follows from the id whatever the row says (3.10f-1: there is no
    nullable name any more); and one bad row never breaks list_jobs(all=True) (review of 1.5b)."""
    bare = _insert_row()
    no_created = _insert_row(created_at=None, finished_at=None)
    no_path = _insert_row(worktree=json.dumps({"repo": "/r"}), isolation="worktree")
    garbled = _insert_row(worktree="{not json")

    s = asyncio.run(server.job_status(bare))
    assert s["log_dir"] == str(jobs.state_dir() / "jobs" / bare) and "name" not in s
    assert asyncio.run(server.job_status(no_created))["elapsed_s"] == 0
    s = asyncio.run(server.job_status(no_path))
    assert s["run_cwd"] == "/c" and s["worktree"] == {"repo": "/r"}

    out = asyncio.run(server.list_jobs(all=True, limit=0))
    by_id = {j["id"]: j for j in out}
    assert {bare, no_created, no_path, garbled} <= set(by_id)
    assert "row_error" in by_id[garbled] and by_id[garbled]["status"] == "succeeded"
    assert all("row_error" not in by_id[j] for j in (bare, no_created, no_path))


def test_row_summary_catches_only_row_problems_and_says_so(monkeypatch, tmp_path, capsys):
    """A garbled row still answers with row_error, and one line per degraded row goes to stderr
    (an invisible silent degradation is how a broken writer hides). An exception that is not
    something a row can cause — a bug in this code — propagates instead (review of 1.8)."""
    garbled = _insert_row(worktree="{not json")
    out = asyncio.run(server.list_jobs(all=True, limit=0))
    row = {j["id"]: j for j in out}[garbled]
    assert "row_error" in row and row["status"] == "succeeded"
    err = capsys.readouterr().err
    assert f"row_error {garbled}: JSONDecodeError:" in err, err

    def boom(*a, **kw):
        raise RuntimeError("a bug in this code, not in the row")

    monkeypatch.setattr(derive, "summary_from_row", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(server.list_jobs(all=True, limit=0))


def test_a_database_error_is_not_a_row_error(monkeypatch, tmp_path, capsys):
    """A sqlite error inside a derivation is the database's problem (locked, I/O, closed), not
    the row's: it answers database_error, as run_agent does, instead of reporting a healthy row
    as garbled (review of 1.8, finding 5)."""
    import sqlite3

    jid = _insert_row()

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(derive, "tool_call_count", locked)
    s = asyncio.run(server.job_status(jid))
    assert s.get("error") == "database_error" and "database is locked" in s["message"], s
    assert "row_error" not in s
    out = asyncio.run(server.list_jobs(all=True, limit=0))
    assert isinstance(out, dict) and out["error"] == "database_error" and "database is locked" in out["message"], out
    assert "row_error" not in capsys.readouterr().err


def test_job_log_says_when_the_directory_is_gone(monkeypatch, tmp_path):
    """A pruned (or never written) job directory is said, not just an empty transcript
    (review of 1.5b): foreign and in-process jobs alike; a present directory has no note."""
    import shutil

    other = jobs.JobStore()
    foreign = _new(other)
    foreign.record_tool_call(1000.0, "Bash", "u1", "ls")
    present = asyncio.run(server.job_log(foreign.id))
    assert "note" not in present
    shutil.rmtree(foreign.log_dir)
    out = asyncio.run(server.job_log(foreign.id, tools=1))
    assert out["transcript"] == ""
    assert out["note"] == f"transcript pruned or never written ({foreign.log_dir})"
    assert [c["tool"] for c in out["tool_calls"]] == ["Bash"]  # the rows outlive the directory

    mine = _new(server.store)
    shutil.rmtree(mine.log_dir)
    out = asyncio.run(server.job_log(mine.id))
    assert out["transcript"] == "" and out["note"] == f"transcript pruned or never written ({mine.log_dir})"

    never = _insert_row()  # a row whose directory was never created: the id still derives it
    out = asyncio.run(server.job_log(never))
    assert out["log_dir"] == str(jobs.state_dir() / "jobs" / never)
    assert out["transcript"] == "" and "note" in out


def test_job_log_reads_the_transcript_row_first(monkeypatch, tmp_path):
    """Story 1.12: a finished job's transcript comes from the ``transcripts`` row — foreign and
    in-process alike — and the directory no longer matters. The note is said only when neither
    the row nor the directory exists."""
    import shutil
    from pathlib import Path

    other = jobs.JobStore()
    foreign = _new(other)
    foreign.log_text("row line")
    foreign.log_text("second line")
    foreign.record_transcript()
    (Path(foreign.log_dir) / "transcript.txt").write_text("only the file\n")  # row wins
    out = asyncio.run(server.job_log(foreign.id))
    assert out["transcript"] == "row line\nsecond line" and "note" not in out

    shutil.rmtree(foreign.log_dir)  # the row survives the file's retention
    out = asyncio.run(server.job_log(foreign.id, tail_lines=1))
    assert out["transcript"] == "second line" and "note" not in out

    other.conn.execute("DELETE FROM transcripts WHERE job_id = ?", (foreign.id,))  # neither
    out = asyncio.run(server.job_log(foreign.id))
    assert out["transcript"] == ""
    assert out["note"] == f"transcript pruned or never written ({foreign.log_dir})"

    mine = _new(server.store)
    mine.log_text("mine row line")
    mine.record_transcript()
    shutil.rmtree(mine.log_dir)
    out = asyncio.run(server.job_log(mine.id))
    assert out["transcript"] == "mine row line" and "note" not in out


def test_unknown_job_answers_are_uniform_and_unquoted(monkeypatch, tmp_path):
    """Every tool answers an id that exists nowhere with unknown_job, resume_job included, and
    the message is the KeyError's text, not its repr in quotes (review of 1.5b, nits)."""
    run = asyncio.run
    answers = {
        "job_status": run(server.job_status("nosuch")),
        "job_log": run(server.job_log("nosuch")),
        "wait_job": run(server.wait_job("nosuch", timeout_s=0.01)),
        "wait_jobs": run(server.wait_jobs(["nosuch"], timeout_s=0.01)),
        "cancel_job": run(server.cancel_job("nosuch")),
        "run_agent": run(server.run_agent(task="x", resume_job="nosuch")),
    }
    for tool, out in answers.items():
        assert out["error"] == "unknown_job", (tool, out)
        assert out["message"].startswith("unknown job 'nosuch'"), (tool, out["message"])


def test_wait_jobs_running_elapsed_does_not_build_a_summary(monkeypatch, tmp_path):
    """The running list's elapsed_s comes from the timestamps: building a whole summary (eight
    database reads) for one number is not needed (review of 1.5b, nit)."""
    job = _new(server.store)
    job.status, job.started_at = "running", time.time() - 30

    def no_summary(self, include_result=True):
        raise AssertionError("summary() built for a running job")

    monkeypatch.setattr(jobs.Job, "summary", no_summary)
    out = asyncio.run(server.wait_jobs([job.id], timeout_s=0.01))
    assert out["outcome"] == "timeout"
    assert out["running"][0]["id"] == job.id and 29.0 <= out["running"][0]["elapsed_s"] <= 40.0


def test_wait_jobs_running_elapsed_survives_a_job_with_no_timestamps(monkeypatch, tmp_path):
    """A Job whose started_at and created_at are both None (one built outside new(), or a row
    whose created_at is NULL): `time.time() - None` raised TypeError in the running branch
    (story 1.9 item 2). elapsed_s is 0.0 when neither is set, as summary() rules."""
    job = _new(server.store)
    job.status, job.started_at, job.created_at = "running", None, None
    out = asyncio.run(server.wait_jobs([job.id], timeout_s=0.01))
    assert out["outcome"] == "timeout"
    assert out["running"][0]["elapsed_s"] == 0.0


def test_a_running_entrys_elapsed_is_a_float_on_both_paths(monkeypatch, tmp_path):
    """Story 1.10a row 2 (review of 1.10, P6a): a running entry whose start is NULL said 0.0 on
    the in-process path (_elapsed, story 1.9 item 2) and the int 0 on the foreign path; both
    are floats now, as every other elapsed_s in the entry is."""
    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.02)
    mine = _new(server.store, task="mine")
    mine.status, mine.started_at, mine.created_at = "running", None, None
    mine.changed()
    other = jobs.JobStore()
    theirs = _new(other, task="theirs")
    started = _new(other, task="started")
    started.status, started.started_at = "running", time.time() - 5
    started.changed()
    with db.immediate(other.conn):
        other.conn.execute("UPDATE jobs SET status = 'running', started_at = NULL, created_at = NULL WHERE id = ?",
                           (theirs.id,))

    out = asyncio.run(server.wait_jobs([mine.id, theirs.id, started.id], timeout_s=0.05))
    got = [(r["id"], r["elapsed_s"], type(r["elapsed_s"])) for r in out["running"]]
    assert got[:2] == [(mine.id, 0.0, float), (theirs.id, 0.0, float)], got
    assert got[2][0] == started.id and got[2][2] is float and got[2][1] >= 5


def test_a_row_deleted_mid_wait_answers_unknown_job_for_the_whole_call(monkeypatch, tmp_path):
    """Story 1.10a row 2 (review of 1.10, P5), the documented behaviour pinned: nothing deletes
    job rows, but a row that disappears mid-wait (a database rewritten under the server) makes
    the whole wait_jobs call answer unknown_job, not a list entry."""
    monkeypatch.setattr(server, "FOREIGN_POLL_S", 0.02)
    other = jobs.JobStore()
    gone, kept = _new(other, task="gone"), _new(other, task="kept")
    for j in (gone, kept):
        j.status, j.started_at = "running", time.time() - 5
        j.changed()

    async def run():
        task = asyncio.create_task(server.wait_jobs([kept.id, gone.id], timeout_s=5, mode="any"))
        await asyncio.sleep(0.1)
        with db.immediate(other.conn):
            other.conn.execute("DELETE FROM jobs WHERE id = ?", (gone.id,))
        return await task

    out = asyncio.run(run())
    assert out == {"error": "unknown_job", "message": f"unknown job '{gone.id}'"}


# --- story 1.9 item 6: database_error on every read path (spec "Tool contract decisions") ---


def test_read_paths_answer_database_error_not_a_traceback(monkeypatch, tmp_path):
    """Any sqlite3.Error raised while a tool READS the database is answered database_error,
    never a traceback. Dropping a table the read paths touch is the plainest way to fail every
    one of them at once (the brief's derive-patch sketch cannot reach job_log's direct tools
    query, so this fails the database itself instead)."""
    job = _new(server.store)
    server.store.conn.execute("DROP TABLE tool_calls")

    for name, call in (
        ("job_status", lambda: server.job_status(job.id)),  # the in-process summary() derive reads
        ("list_jobs", lambda: server.list_jobs()),  # the all=False branch
        ("job_log", lambda: server.job_log(job.id, tools=2)),  # the tools=N query
    ):
        out = asyncio.run(call())
        assert out["error"] == "database_error" and "no such table" in out["message"], name


class _BrokenConn(jobs.JobStore):
    """Every read through the connection fails: the row lookups behind the unknown-job answers."""

    @property
    def conn(self):
        import sqlite3

        raise sqlite3.OperationalError("database is locked")


def test_a_failing_row_lookup_is_never_an_unknown_job(monkeypatch, tmp_path):
    """"not found" would be a lie when the finding itself failed: when the row read inside
    _foreign_job_error (or behind job_status's fallback) cannot run, every tool answers
    database_error (story 1.9 item 6)."""
    _store(monkeypatch, tmp_path)  # the env; the replacement store never opens a connection
    server.store = _BrokenConn()
    for name, call in (
        ("job_status", lambda: server.job_status("no-such-id")),
        ("job_log", lambda: server.job_log("no-such-id")),
        ("wait_job", lambda: server.wait_job("no-such-id", timeout_s=0.01)),
        ("wait_jobs", lambda: server.wait_jobs(["no-such-id"], timeout_s=0.01)),
        ("cancel_job", lambda: server.cancel_job("no-such-id")),
        ("run_agent", lambda: server.run_agent(task="x", resume_job="no-such-id")),
    ):
        out = asyncio.run(call())
        assert out["error"] == "database_error", name


def test_a_failing_derive_read_degrades_the_progress_line_not_the_wait(monkeypatch, tmp_path, capsys):
    """The reporter's derive reads run inside store.wait: one failing must not abort the wait.
    The line says what it can without the derived parts, one stderr line names the failure, and
    the wait still returns the finished job once the rows are readable again."""
    import re
    import sqlite3

    job = _new(server.store)
    real = derive.tool_call_count

    def locked(conn, jid):
        if not job.settled:  # the rows are unreadable for as long as the wait runs
            raise sqlite3.OperationalError("database is locked")
        return real(conn, jid)

    monkeypatch.setattr(derive, "tool_call_count", locked)
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.02)

    class Ctx:
        def __init__(self):
            self.reports = []

        async def report_progress(self, progress, total=None, message=None):
            self.reports.append(message)

    ctx = Ctx()

    async def go():
        async def finish_later():
            await asyncio.sleep(0.08)
            job.status = "succeeded"
            job._done.set()

        asyncio.create_task(finish_later())
        return await server.wait_job(job.id, timeout_s=5, ctx=ctx)

    out = asyncio.run(go())
    assert out["status"] == "succeeded" and out["id"] == job.id
    assert ctx.reports, "the wait ran but reported nothing"
    assert all(re.fullmatch(r"(queued|running): \d+s, 0 turns", m) for m in ctx.reports), ctx.reports
    err = capsys.readouterr().err
    assert err.count("database is locked") >= 1, err
