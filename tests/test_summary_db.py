"""Story 1.5b: the summary read path and exposure (spec "Derivations" and "Tool contract
decisions"). usage_totals, tool_counts, error_count and last_tool come from the rows;
summary_from_row builds the full job_status dict for any server's job; summary() and
summary_from_row agree for the same job; the read tools cross servers and the in-process
tools refuse another server's job."""

import asyncio
import json
import time

import pytest

from localagents import derive, jobs, server


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
    assert s["id"] == job.id and s["name"] == job.name
    assert s["model"] == "served-model" and s["endpoint"] == "llamacpp" and s["base_url"] == "http://x:8080"
    assert s["task"] == "x" * 300 + "…"
    assert s["status"] == "succeeded" and s["result"] == "done" and s["error"] is None
    assert s["num_turns"] == 4 and s["session_id"] == "sess-9" and s["context"] == 131072
    assert s["created_at"] == pytest.approx(job.created_at)
    assert s["started_at"] == 1000.0 and s["finished_at"] == 1060.0
    assert s["elapsed_s"] == 60.0
    assert s["isolation"] == "worktree" and s["worktree"] == wt
    assert s["run_cwd"] == "/wt/sub"  # worktree.path when the tree is present
    assert s["log_dir"] == str(jobs.state_dir() / "jobs" / job.name)
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
    assert s["files_touched"] == ["/a.py"]
    assert s["ctx_tokens"] == 1000 and s["gen_tps"] == pytest.approx(40.0)
    for storage in ("mcp_server_id", "endpoint_id", "model_id"):
        assert storage not in s, f"{storage} is a storage detail, not job_status"

    # include_result=False drops the key, as summary() does
    assert "result" not in derive.summary_from_row(store.conn, row, include_result=False)


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
    assert out["id"] == foreign.id and out["name"] == foreign.name
    assert out["task"] == "the other server's brief" and out["model"] == "served-model"
    assert out["status"] == "succeeded" and out["result"] == "their result" and out["elapsed_s"] == 60.0
    assert out["tool_calls"] == 1 and out["errors"] == 1 and out["last_tool"] == "Bash: ls"
    assert out["log_dir"] == str(jobs.state_dir() / "jobs" / foreign.name)
    assert "mcp_server_id" not in out and "endpoint_id" not in out and "model_id" not in out
    assert asyncio.run(server.job_status("no-such-job"))["error"] == "unknown_job"


def test_in_process_tools_refuse_a_foreign_job(monkeypatch, tmp_path):
    """wait_job, wait_jobs, cancel_job and resume need the in-process task: a clear foreign_job
    error, not unknown_job (spec Tool contract decisions)."""
    other = jobs.JobStore()
    foreign = _new(other)
    foreign.status, foreign.started_at = "running", time.time() - 10
    foreign.changed()

    wait = asyncio.run(server.wait_job(foreign.id, timeout_s=0.05))
    assert wait["error"] == "foreign_job" and other.server_id in wait["message"]
    assert "only job_status, job_log and list_jobs can read it" in wait["message"]
    assert asyncio.run(server.cancel_job(foreign.id))["error"] == "foreign_job"
    assert asyncio.run(server.wait_jobs([foreign.id], timeout_s=0.05))["error"] == "foreign_job"
    resumed = asyncio.run(server.run_agent(task="continue", resume_job=foreign.id))
    assert resumed["error"] == "foreign_job"


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
    """job_log answers for another server's job (log_dir derived from its name); tools=N adds
    the last N recorded calls, oldest first."""
    other = jobs.JobStore()
    foreign = _new(other)
    for i, tool in enumerate(("Read", "Bash", "Edit", "Grep")):
        foreign.record_tool_call(1000.0 + i, tool, f"u{i}", f"/x/{i}.py" if tool == "Edit" else f"cmd {i}")
        foreign.record_tool_result(f"u{i}", 1000.5 + i, tool == "Bash")

    out = asyncio.run(server.job_log(foreign.id, tools=2))
    assert out["job_id"] == foreign.id and out["status"] == "queued"
    assert out["log_dir"] == str(jobs.state_dir() / "jobs" / foreign.name)
    assert out["transcript"] == ""  # the job never ran: no transcript file
    calls = out["tool_calls"]
    assert [c["tool"] for c in calls] == ["Edit", "Grep"]  # last two, oldest first
    assert calls[0]["input"] == "/x/2.py" and calls[0]["finished"] == 1002.5 and calls[0]["is_error"] == 0
    assert calls[1]["t"] == 1003.0 and calls[1]["is_error"] == 0
    assert "tool_calls" not in asyncio.run(server.job_log(foreign.id))  # tools=0: the key is absent
    assert asyncio.run(server.job_log("no-such-job"))["error"] == "unknown_job"
