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
    assert status.render() == "local: ⟳ Qwen3.8-Flash-Next 1m18s 4 calls"
    job.tool_calls, job.files_touched = 1, ["/tmp/a.py"]
    job.changed()
    assert status.render() == "local: ⟳ Qwen3.8-Flash-Next 1m18s 1 call 1 file"


def test_finished_jobs_linger_then_drop(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    job = _job()
    job.status, job.started_at, job.finished_at = "succeeded", 1000.0, 1180.0
    job._done.set()
    status.publish([job], now=1200.0)
    assert status.render(now=1200.0) == "local: ✓ Qwen3.8-Flash-Next 3m00s"
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
