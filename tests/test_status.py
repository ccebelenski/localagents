"""Status-line publishing: the server writes a per-pid file, the renderer merges live ones."""

import json
import os
import subprocess
import sys
import time

from localagents import jobs, status


def _job(**kw):
    return jobs.JobStore().new(
        task="t", model=kw.pop("model", "unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS"),
        endpoint="llamacpp", base_url="http://x:8080", cwd="/tmp", run_cwd="/tmp", isolation="none",
    )


def test_short_model_and_elapsed():
    assert status.short_model("unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS") == "Qwen3.8-Flash-Next"
    assert status.short_model("GLM-5.3-Flash-EXL3") == "GLM-5.3-Flash"
    assert status.short_model("deepseek-ai/DeepSeek-V4-Flash-0731") == "DeepSeek-V4-Flash-0731"
    assert status.fmt_elapsed(45) == "45s"
    assert status.fmt_elapsed(78) == "1m18s"
    assert status.fmt_elapsed(3 * 3600 + 7 * 60) == "3h07m"


def test_publish_and_render_running_job(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    job = _job()
    job.status = "running"
    job.started_at = time.time() - 78
    job.tool_calls = 4
    job.changed()
    data = json.loads((status.status_dir() / f"{os.getpid()}.json").read_text())
    assert data["pid"] == os.getpid() and data["jobs"][0]["id"] == job.id
    me = os.getppid()  # what publish() records as owner for this process
    assert status.render(owner=me) == "local: ⟳ Qwen3.8-Flash-Next 1m18s 4 calls"
    job.tool_calls, job.files_touched = 1, ["/tmp/a.py"]
    job.changed()
    assert status.render(owner=me) == "local: ⟳ Qwen3.8-Flash-Next 1m18s 1 call 1 file"


def test_finished_jobs_linger_then_drop(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    job = _job()
    job.status, job.started_at, job.finished_at = "succeeded", 1000.0, 1180.0
    job._done.set()
    status.publish([job], now=1200.0)
    assert status.render(now=1200.0, owner=os.getppid()) == "local: ✓ Qwen3.8-Flash-Next 3m00s"
    assert status.render(now=1180.0 + status.KEEP_FINISHED_S + 1) == ""
    status.publish([job], now=1180.0 + status.KEEP_FINISHED_S + 1)
    assert json.loads((status.status_dir() / f"{os.getpid()}.json").read_text())["jobs"] == []


def test_dead_server_file_is_ignored_and_removed(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    proc = subprocess.Popen(["true"])
    proc.wait()  # a pid that has exited
    dead = status.status_dir() / f"{proc.pid}.json"
    dead.write_text(json.dumps({"pid": proc.pid, "updated": time.time(), "jobs": [
        {"id": "x", "model": "m", "status": "running", "started_at": time.time()}]}))
    assert status.render() == ""
    assert not dead.exists()


def test_remove_deletes_own_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    status.publish([])
    assert (status.status_dir() / f"{os.getpid()}.json").exists()
    status.remove()
    assert not (status.status_dir() / f"{os.getpid()}.json").exists()


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


def test_other_sessions_jobs_fold_into_a_count(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = time.time()
    mine = _job()
    mine.status, mine.started_at = "running", now - 30
    status.publish([mine], {"llamacpp": "1/1 slots"}, now=now)
    # a second live server (same pid is fine for the liveness check) owned by another session
    other = status.status_dir() / "999999999.json"
    other.write_text(json.dumps({"pid": os.getpid(), "owner": 4242, "updated": now, "jobs": [
        {"id": "o1", "model": "x/GLM-5.3-Flash-EXL3", "endpoint": "dgx1", "status": "running", "started_at": now - 5},
        {"id": "o2", "model": "x/GLM-5.3-Flash-EXL3", "endpoint": "dgx1", "status": "succeeded",
         "started_at": now - 50, "finished_at": now - 10}],
        "endpoints": {"dgx1": "2 run 1 wait kv 34%"}}))
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
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    job = _job()
    job.status, job.started_at, job.context = "running", time.time() - 10, 131072
    job.note_usage({"input_tokens": 1000, "cache_read_input_tokens": 39000, "output_tokens": 300}, ms=3000)
    assert job.ctx_tokens == 40000 and job.gen_tps == 100.0
    job.note_usage({"output_tokens": 300}, ms=9000, gen_ms=3000)  # prompt processing excluded
    assert job.ctx_tokens == 40000 and job.gen_tps == 100.0
    job.note_usage({"input_tokens": 2000, "cache_read_input_tokens": 40000, "output_tokens": 200}, ms=4000)
    assert job.ctx_tokens == 42000 and job.gen_tps == 75.0  # smoothed
    job.note_usage(None, ms=0)  # count_tokens etc.: no change
    assert status.render(owner=os.getppid()) == "local: ⟳ Qwen3.8-Flash-Next 10s ctx 32% 75 t/s"


def test_my_owner_walks_to_a_claude_process_or_none():
    owner = status.my_owner()
    if owner is not None:  # running under Claude Code's Bash tool
        assert open(f"/proc/{owner}/comm").read().strip() == "claude"
