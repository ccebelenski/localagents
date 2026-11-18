"""Epic 6.7: the OpenCode harness (harnesses/opencode.py).

Every stream here is a 6.2 capture (tests/fixtures/opencode/events, opencode v2.3.1), never typed:
the reader turns each into the job's record — turns, tool rows, the result, the session id, the
step cap, a tool error, reasoning, retries. ``run()`` itself is driven through the real
run_agent -> run_job by a fake ``opencode`` on PATH that replays a capture and writes down the
arguments and environment it was given; a hanging fake proves a cancel leaves nothing running.
"""

import asyncio
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from localagents import derive, harnesses, jobs, runner, server
from localagents.harnesses import SessionSpec
from localagents.harnesses import opencode as oc
from localagents.registry import Defaults, Endpoint, Registry, Resolution

FIX = Path(__file__).resolve().parent / "fixtures" / "opencode"


def _events(name):
    return (FIX / "events" / f"{name}.jsonl").read_text().splitlines()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LOCALAGENTS_DB", str(tmp_path / "s.db"))
    return jobs.JobStore()


def _job(store, cwd="/tmp"):
    return store.new(task="t", model="m", endpoint="e", base_url="http://x:8080", backend="llama.cpp",
                     host="local", cwd=cwd, run_cwd=cwd, isolation="none", harness="opencode")


def _feed(job, name, steps=40):
    r = oc.Reader(job, steps)
    for line in _events(name):
        r.feed(line)
    return r


def _rows(job):
    return [dict(x) for x in job._conn.execute(
        "SELECT tool, tool_use_id, input, is_error, finished FROM tool_calls WHERE job_id = ? ORDER BY rowid", (job.id,))]


def test_a_captured_job_becomes_the_record(store):
    job = _job(store)
    r = _feed(job, "strata-tools")
    r.finish(0, "")
    events = [json.loads(x) for x in _events("strata-tools")]
    assert job.session_id == events[0]["sessionID"]
    assert job.num_turns == sum(e["type"] == "step_start" for e in events)
    rows = _rows(job)
    assert [x["tool"] for x in rows] == [e["part"]["tool"] for e in events if e["type"] == "tool_use"]
    assert all(x["finished"] is not None and x["is_error"] == 0 for x in rows)
    final = [e["part"]["text"] for e in events if e["type"] == "text"][-1]
    assert job.result == final and job.error is None
    assert derive.files_touched(job._conn, job.id) == [
        e["part"]["state"]["input"]["path"] for e in events
        if e["type"] == "tool_use" and e["part"]["tool"] in ("edit", "write")]
    text = (Path(job.log_dir) / "transcript.txt").read_text()
    assert "[init] session=" in text and "[tool] shell" in text and "[assistant]" in text


def test_a_tool_error_is_an_error_row(store):
    job = _job(store)
    _feed(job, "tool-error").finish(0, "")
    rows = _rows(job)
    assert [x["is_error"] for x in rows if x["tool"] == "read"] == [1]
    assert derive.error_count(job._conn, job.id) == 1


def test_the_silent_step_cap_is_recorded_as_capped(store):
    """step-cap.jsonl ran with steps: 3; its last step had no tools and ended `stop`, exit 0."""
    job = _job(store)
    _feed(job, "step-cap", steps=3).finish(0, "")
    assert job.num_turns == 3
    assert job.error and "max_turns" in job.error and "3 steps" in job.error
    assert job.result and "Maximum steps" in job.result  # its own summary, kept


def test_reasoning_and_retries_and_a_cut_stream(store):
    job = _job(store)
    _feed(job, "llamacpp-thinking").finish(0, "")
    assert "[thinking] (" in (Path(job.log_dir) / "transcript.txt").read_text()
    job2 = _job(store)
    r = _feed(job2, "overflow-strata-retry")
    assert r.retries == 3  # the capture ends on three transport retries (its proxy was gone)
    r.finish(None, "")
    assert job2.result is None  # no final step: no result


def test_cancel_capture_has_no_result_and_a_nonzero_exit_names_itself(store):
    job = _job(store)
    _feed(job, "cancel").finish(143, "terminated")
    assert job.result is None and job.error.startswith("opencode exited 143")


def test_new_checkpoints_are_compactions():
    sig = json.loads((FIX / "wire" / "compaction-signature.json").read_text())
    first = sig["after_compaction_first_user"]
    assert first.startswith(oc.CHECKPOINT)
    job = jobs.Job(id="j", task="t", model="m", endpoint="e", base_url="b", cwd="/tmp", run_cwd="/tmp",
                   isolation="none", log_dir=str(Path(os.environ.get("TMPDIR", "/tmp"))))
    job.log_text = lambda text: None
    count = oc.CompactionCounter(job)
    body = lambda first_user: {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": first_user}]}  # noqa: E731
    count("/v1/chat/completions", body("plain task"))
    count("/v1/chat/completions", body(first))
    count("/v1/chat/completions", body(first))  # the same checkpoint again: not a new one
    count("/v1/chat/completions", body(first + " and an update"))
    assert job.compactions == 2


def test_the_generated_config_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("OPENCODE", "1")
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_parent")
    ep = Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")
    res = Resolution(endpoint=ep, served_model="org/model:Q4", context=212992)
    spec = SessionSpec(defaults=Defaults(max_turns=60), shim_base_url="http://127.0.0.1:9/job/j", background=True)
    job = jobs.Job(id="j", task="t", model="m", endpoint="e", base_url="b", cwd=str(tmp_path), run_cwd=str(tmp_path),
                   isolation="none", log_dir=str(tmp_path))
    from localagents.harnesses import opencode_policy, policy

    pol = policy.resolve(spec)
    cfg = oc.config(job, res, spec, pol, opencode_policy.translate(pol), ["/x/CLAUDE.md"])
    prov = cfg["providers"]["localagents"]
    assert prov["settings"]["baseURL"] == "http://127.0.0.1:9/job/j/v1" and prov["settings"]["apiKey"] == "local"
    model = prov["models"]["job"]
    assert model["modelID"] == "org/model:Q4" and model["limit"] == {"context": 212992, "output": 26624}
    assert cfg["agents"]["job"]["steps"] == 60 and cfg["instructions"] == ["/x/CLAUDE.md"]
    denied = {p["action"] for p in cfg["permissions"] if p["effect"] == "deny"}
    # the config's defaults (acceptEdits, Read Edit Write Glob Grep Bash WebFetch) in OpenCode's terms
    assert denied == {"subagent", "websearch", "skill", "question", "experimental_image_generate", "execute",
                      "opencode_*"}, denied
    assert cfg["permissions"][0] == {"action": "*", "resource": "*", "effect": "allow"}
    # the 6.6+6.7 review's fold: OpenCode's own management tools (worktree remove, session move...)
    # are switched off by plugin, a deny does not reach them (checked live)
    assert {"-opencode.tools", "-opencode.tools.mcp-resources"} <= set(cfg["plugins"])
    assert all(p.startswith("-opencode.") for p in cfg["plugins"])
    env = oc.environment(job, tmp_path / "cfg")
    assert env["PWD"] == str(tmp_path) and env["XDG_CONFIG_HOME"] == str(tmp_path / "cfg")
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert "OPENCODE" not in env and "OPENCODE_SESSION_ID" not in env
    assert env["XDG_DATA_HOME"].endswith("localagents/opencode/data")


def test_project_instructions_are_the_repositorys_claude_md_files(tmp_path):
    from localagents.harnesses.opencode_policy import instruction_files

    repo = tmp_path / "repo"
    sub = repo / "pkg"
    sub.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (tmp_path / "CLAUDE.md").write_text("outside the repository")
    (repo / "CLAUDE.md").write_text("root")
    (sub / "CLAUDE.md").write_text("package")
    (sub / "CLAUDE.local.md").write_text("mine")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("the operator's")
    assert instruction_files(str(sub), ("project",), home) == [str(repo / "CLAUDE.md"), str(sub / "CLAUDE.md")]
    assert instruction_files(str(sub), ("project", "local"), home) == [
        str(repo / "CLAUDE.md"), str(sub / "CLAUDE.md"), str(sub / "CLAUDE.local.md")]
    assert instruction_files(str(sub), ("user", "project"), home)[0] == str(home / ".claude" / "CLAUDE.md")
    assert instruction_files(str(sub), (), home) == []  # a calibration job's empty sources


# --- run() through the real run_agent -> run_job, a fake opencode on PATH ----------------------------

FAKE = r'''#!{python}
import json, os, sys, time
mode = os.environ.get("FAKE_OPENCODE_MODE", "replay")
with open(os.environ["FAKE_OPENCODE_LOG"], "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "pwd": os.environ.get("PWD"), "cwd": os.getcwd(),
                        "xdg_config": os.environ.get("XDG_CONFIG_HOME"),
                        "disable": os.environ.get("OPENCODE_DISABLE_CLAUDE_CODE"),
                        "pid": os.getpid()}}) + "\n")
if mode == "hang":
    print(json.dumps({{"type": "step_start", "sessionID": "ses_hang", "part": {{}}}}), flush=True)
    time.sleep(3600)
for line in open(os.environ["FAKE_OPENCODE_EVENTS"]):
    sys.stdout.write(line)
sys.stdout.flush()
'''


@pytest.fixture
def live(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "opencode"
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    log = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_OPENCODE_LOG", str(log))
    monkeypatch.setenv("FAKE_OPENCODE_EVENTS", str(FIX / "events" / "strata-tools.jsonl"))
    reg = Registry(path=None, endpoints={"e": Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")},
                   defaults=Defaults(job_retention_days=0), raw={}, local={})

    async def probe(self, ep):
        return {"up": True, "models": ["served-model"], "context": {"served-model": 131072}}

    async def nothing(*a, **kw):
        return None

    async def no_start():
        return 0

    monkeypatch.setattr(Registry, "probe", probe)
    monkeypatch.setattr(server, "_reg", lambda: reg)
    monkeypatch.setattr(server, "run_job", runner.run_job)
    monkeypatch.setattr(server.shim, "start", no_start)
    monkeypatch.setattr(runner, "fetch_metrics", nothing)
    return log


def _calls(log):
    return [json.loads(x) for x in log.read_text().splitlines()]


def test_a_job_runs_in_opencode_end_to_end(live, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    out = asyncio.run(server.run_agent(task="fix it", model="served-model", cwd=str(work), harness="opencode",
                                       wait_s=30))
    assert out["status"] == "succeeded", out
    assert out["harness"] == "opencode" and out["session_id"].startswith("ses_")
    assert out["tool_calls"] == 9 and out["result"]
    (call,) = _calls(live)
    assert call["pwd"] == str(work) and call["cwd"] == str(work) and call["disable"] == "1"
    assert call["argv"][:4] == ["run", "--standalone", "--format", "json"]
    assert "--session" not in call["argv"] and call["argv"][-1] == "fix it"
    cfg = json.loads((Path(call["xdg_config"]) / "opencode" / "opencode.json").read_text())
    assert cfg["providers"]["localagents"]["models"]["job"]["modelID"] == "served-model"

    again = asyncio.run(server.run_agent(task="and more", resume_job=out["id"], wait_s=30))
    assert again["status"] == "succeeded" and again["harness"] == "opencode"
    second = _calls(live)[1]
    i = second["argv"].index("--session")
    assert second["argv"][i + 1] == out["session_id"]


def test_only_what_has_no_equivalent_is_refused(live, tmp_path):
    """6.8: a setting with no OpenCode equivalent is refused before the job exists; one that has
    one runs (default mode with the allowed tools)."""
    for kw, word in (({"permission_mode": "dontAsk"}, "permission_mode"),
                     ({"allowed_tools": ["Read", "TodoWrite"]}, "TodoWrite"),
                     ({"allowed_tools": ["Read"], "permission_mode": "default"}, None)):
        out = asyncio.run(server.run_agent(task="t", model="served-model", harness="opencode", cwd=str(tmp_path),
                                           wait_s=30, **kw))
        if word is None:
            assert out["status"] == "succeeded", out
        else:
            assert out["error"] == "bad_argument" and word in out["message"], out


def test_a_cancel_ends_the_process_group(live, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_MODE", "hang")

    async def go():
        out = await server.run_agent(task="t", model="served-model", harness="opencode", wait_s=0)
        for _ in range(100):
            if live.exists() and server.store.get(out["id"]).session_id:
                break
            await asyncio.sleep(0.05)
        return await server.cancel_job(out["id"])

    rec = asyncio.run(go())
    assert rec["status"] == "cancelled"
    pid = _calls(live)[0]["pid"]
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_both_harnesses_are_registered():
    assert harnesses.names() == ["claude-code", "opencode"]
    assert harnesses.get("opencode") is oc
