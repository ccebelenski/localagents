"""Story 1.5a: the status line reads the shared database. Servers keep their mcp_servers row
alive (heartbeat / updated=started) and write jobs, requests and tool calls; the renderer
merges the live servers' jobs into one line. The rendered strings are pinned: the table swap
must not change what the status bar shows."""

import os
import subprocess
import sys
import time

from localagents import jobs, status

MODEL = "unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS"


def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return jobs.JobStore()


def _new(store, **kw):
    kw.setdefault("task", "t")
    kw.setdefault("model", MODEL)
    kw.setdefault("endpoint", "llamacpp")
    kw.setdefault("base_url", "http://x:8080")
    kw.setdefault("cwd", "/tmp")
    kw.setdefault("run_cwd", "/tmp")
    kw.setdefault("isolation", "none")
    return store.new(**kw)


def test_short_model_and_elapsed():
    assert status.short_model("unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS") == "Qwen3.8-Flash-Next"
    assert status.short_model("GLM-5.3-Flash-EXL3") == "GLM-5.3-Flash"
    assert status.short_model("deepseek-ai/DeepSeek-V4-Flash-0731") == "DeepSeek-V4-Flash-0731"
    assert status.fmt_elapsed(45) == "45s"
    assert status.fmt_elapsed(78) == "1m18s"
    assert status.fmt_elapsed(3 * 3600 + 7 * 60) == "3h07m"


def test_publish_and_render_running_job(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    now = time.time()
    job.status, job.started_at = "running", now - 78
    for i in range(4):
        job.record_tool_call(now - 40 + i, "Bash", f"b{i}", "ls")
    job.changed()
    srv = store.conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (store.server_id,)).fetchone()
    assert srv["pid"] == os.getpid() and srv["owner"] == os.getppid()
    me = os.getppid()  # what _open records as owner for this process
    assert status.render(now=now, owner=me) == "local: ⟳ Qwen3.8-Flash-Next 1m18s 4 calls"
    # this job retires off the line; a second one shows the single forms
    job.status, job.finished_at = "succeeded", now - 3600
    job.changed()
    job2 = _new(store)
    job2.status, job2.started_at = "running", now - 78
    job2.record_tool_call(now - 20, "Edit", "e1", "/tmp/a.py")
    job2.changed()
    assert status.render(now=now, owner=me) == "local: ⟳ Qwen3.8-Flash-Next 1m18s 1 call 1 file"


def test_finished_jobs_linger_then_drop(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    job.status, job.started_at, job.finished_at = "succeeded", 1000.0, 1180.0
    job.changed()
    assert status.render(now=1200.0, owner=os.getppid()) == "local: ✓ Qwen3.8-Flash-Next 3m00s"
    # collect filters by `now`, so the linger boundary moves with the clock, not with a re-publish
    assert status.render(now=1180.0 + status.KEEP_FINISHED_S + 1) == ""


def test_dead_server_jobs_are_dropped(monkeypatch, tmp_path):
    """Liveness is heartbeat first, pid second: a live pid whose row stopped beating 61 s ago
    is not live, and neither is one marked stopped. The row itself is history and stays."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    now = time.time()
    job.status, job.started_at = "running", now - 30
    job.changed()
    assert status.render(now=now, owner=os.getppid()) == "local: ⟳ Qwen3.8-Flash-Next 30s"
    store.conn.execute("UPDATE mcp_servers SET updated = ? WHERE id = ?", (now - 61, store.server_id))
    assert status.render(now=now, owner=os.getppid()) == ""
    store.conn.execute("UPDATE mcp_servers SET updated = ?, stopped = ? WHERE id = ?", (now, now, store.server_id))
    assert status.render(now=now, owner=os.getppid()) == ""
    assert store.conn.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0] == 1


def test_remove_deletes_own_file(monkeypatch, tmp_path):
    """"remove" is close(): stopped set, the server's jobs vanish from the line, and close is
    idempotent."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    now = time.time()
    job.status, job.started_at = "running", now - 30
    job.changed()
    assert status.render(now=now, owner=os.getppid()) == "local: ⟳ Qwen3.8-Flash-Next 30s"
    store.close()
    row = store.conn.execute("SELECT stopped FROM mcp_servers WHERE id = ?", (store.server_id,)).fetchone()
    assert row["stopped"] is not None
    assert status.render(now=now, owner=os.getppid()) == ""
    store.close()
    assert status.render(now=now, owner=os.getppid()) == ""


def test_statusline_command_is_light():
    """It must not import the server stack: it runs on every status-bar refresh."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.argv=['localagents','statusline']; import localagents; localagents.main(); "
         "print(sorted(m for m in sys.modules if m in ('mcp','httpx','uvicorn','localagents.server')))"],
        capture_output=True, text=True, cwd="src",
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "[]"


def test_render_without_a_database_is_an_empty_line(monkeypatch, tmp_path):
    """A machine with no database yet: print the empty line, create no file (or directory)."""
    monkeypatch.setenv("LOCALAGENTS_DB", str(tmp_path / "nope" / "localagents.db"))
    assert status.render() == ""
    assert not (tmp_path / "nope").exists()


def test_other_sessions_jobs_fold_into_a_count(monkeypatch, tmp_path):
    now = time.time()
    mine_store = _store(monkeypatch, tmp_path)
    mine = _new(mine_store)
    mine.status, mine.started_at = "running", now - 30
    mine.changed()
    mine_store.record_endpoint_load({"llamacpp": "1/1 slots"})
    # a second live server (same pid is fine for the liveness check) owned by another session
    other = jobs.JobStore()
    other.conn.execute("UPDATE mcp_servers SET owner = 4242 WHERE id = ?", (other.server_id,))
    o1 = _new(other, model="x/GLM-5.3-Flash-EXL3", endpoint="dgx1", base_url="http://y:8000")
    o1.status, o1.started_at = "running", now - 5
    o1.changed()
    o2 = _new(other, model="x/GLM-5.3-Flash-EXL3", endpoint="dgx1", base_url="http://y:8000")
    o2.status, o2.started_at, o2.finished_at = "succeeded", now - 50, now - 10
    o2.changed()
    other.record_endpoint_load({"dgx1": "2 run 1 wait kv 34%"})
    assert status.render(now=now, owner=os.getppid()) == "local: ⟳ Qwen3.8-Flash-Next 30s · llamacpp 1/1 slots · +2 other"
    assert status.render(now=now, owner=4242) == "local: ✓ GLM-5.3-Flash 40s · ⟳ GLM-5.3-Flash 5s · dgx1 2 run 1 wait kv 34% · +1 other"
    # no identifiable Claude Code parent: everything is ours, no endpoint load shown for non-running
    monkeypatch.setattr(status, "my_owner", lambda: None)
    assert status.render(now=now).count("·") == 4 and "+" not in status.render(now=now)


def test_load_summary_shapes():
    assert status.load_summary({"slots": {"total": 2, "busy": 1, "free": 1}}) == "1/2 slots"
    assert status.load_summary({"load": {"requests_running": 2, "requests_waiting": 1, "kv_cache_usage": 0.337}}) == "2 run 1 wait kv 34%"
    assert status.load_summary({"load": {"requests_running": 0, "requests_waiting": 0, "kv_cache_usage": 0.0}}) == "0 run kv 0%"
    assert status.load_summary({"up": True, "models": ["m"]}) is None
    assert status.load_summary({"slots": {"total": 4}}) is None  # llama.cpp without /slots enabled


def test_shim_usage_feeds_context_and_speed(monkeypatch, tmp_path):
    """The shim calls both: note_usage keeps the in-memory fields (job_status still reads them)
    and _log records the request row — the line now derives ctx fill and speed from the rows."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store, context=131072)
    now = time.time()
    job.status, job.started_at = "running", now - 10
    job.note_usage({"input_tokens": 1000, "cache_read_input_tokens": 39000, "output_tokens": 300}, ms=3000)
    assert job.ctx_tokens == 40000 and job.gen_tps == 100.0
    job.note_usage({"output_tokens": 300}, ms=9000, gen_ms=3000)  # prompt processing excluded
    assert job.ctx_tokens == 40000 and job.gen_tps == 100.0
    job.note_usage({"input_tokens": 2000, "cache_read_input_tokens": 40000, "output_tokens": 200}, ms=4000)
    assert job.ctx_tokens == 42000 and job.gen_tps == 75.0  # smoothed
    job.note_usage(None, ms=0)  # count_tokens etc.: no change
    for t, ms, gen_ms, usage in (
        (1, 3000, None, {"input_tokens": 1000, "cache_read_input_tokens": 39000, "output_tokens": 300}),
        (2, 9000, 3000, {"output_tokens": 300}),
        (3, 4000, None, {"input_tokens": 2000, "cache_read_input_tokens": 40000, "output_tokens": 200}),
        (4, 5, None, None),  # the count_tokens call note_usage(None) stands for: a null-usage row
    ):
        rec = {"t": now + t, "path": "/v1/messages", "status": 200, "ms": ms, "usage": usage}
        if gen_ms is not None:
            rec["gen_ms"] = gen_ms
        job.record_request(rec)
    assert status.render(owner=os.getppid()) == "local: ⟳ Qwen3.8-Flash-Next 10s ctx 32% 75 t/s"


def test_my_owner_walks_to_a_claude_process_or_none():
    owner = status.my_owner()
    if owner is not None:  # running under Claude Code's Bash tool
        assert open(f"/proc/{owner}/comm").read().strip() == "claude"
