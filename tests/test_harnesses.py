"""Epic 6.3: the harness seam, Claude Code behind it.

runner.py keeps what every harness shares (worktree, metrics, the session's clock, status,
cancel, finish); the session itself is the harness's module in harnesses/. Nothing of Claude
Code may live in the core: the operator's rule for the epic, "if we need two versions of things,
then factor out the common stuff and create the versions". The behaviour is unchanged (the move
was checked by replaying one scripted session through the old and the new tree: identical record,
transcript, tool rows and session options).
"""

import ast
import asyncio
from pathlib import Path

import pytest

from localagents import harnesses, runner
from localagents.harnesses import SessionSpec, claude_code
from localagents.registry import Defaults, Endpoint, Registry, Resolution

SRC = Path(runner.__file__).resolve().parent


def test_the_registry_names_each_harness_once():
    assert harnesses.names() == ["claude-code", "opencode"]  # 6.7 registered OpenCode's
    assert harnesses.DEFAULT == "claude-code"
    assert harnesses.get("claude-code") is claude_code
    with pytest.raises(ValueError, match="unknown harness 'nope'"):
        harnesses.get("nope")


def test_the_core_holds_nothing_of_claude_code():
    """The core imports no SDK, sets no Claude Code variable and names none of its message types
    or tools; comments may name the harness, code may not."""
    tree = ast.parse((SRC / "runner.py").read_text())
    imported = {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any("claude_agent_sdk" in m for m in imported), imported
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not [s for s in strings if "CLAUDE_CODE_" in s or "ANTHROPIC_" in s]
    for claude_only in ("ClaudeAgentOptions", "query", "AssistantMessage", "ResultMessage", "SystemMessage",
                        "build_env", "FOREGROUND_NOTE", "FOREGROUND_DISALLOWED", "_apply_message"):
        assert claude_only not in names, claude_only


def test_the_harness_module_owns_its_session():
    for name in ("run", "build_env", "FOREGROUND_NOTE", "FOREGROUND_DISALLOWED", "_apply_message",
                 "_track_task", "_session_goes_on", "query", "ClaudeAgentOptions"):
        assert hasattr(claude_code, name), name
    assert asyncio.iscoroutinefunction(claude_code.run)


def test_run_job_hands_the_session_to_the_harness(monkeypatch, tmp_path):
    """The core calls ``harness.run(job, res, spec)`` once, inside its clock, with the job's
    resolved settings; the status follows what the harness left on the job."""
    calls = []

    async def fake_run(job, res, spec):
        calls.append(spec)
        job.result = "ok"

    async def nothing(*a, **kw):
        return None

    monkeypatch.setattr(claude_code, "run", fake_run)
    monkeypatch.setattr(runner, "fetch_metrics", nothing)
    monkeypatch.setattr(runner.feedback, "note_start", lambda job: None)

    async def no_finish(*a, **kw):
        return None

    monkeypatch.setattr(runner, "_finish", no_finish)
    from localagents.jobs import Job

    ep = Endpoint(name="e", base_url="http://e:8080", backend="llama.cpp")
    reg = Registry(path=None, endpoints={"e": ep}, defaults=Defaults(job_retention_days=0, background_tasks=False),
                   raw={}, local={})
    res = Resolution(endpoint=ep, served_model="m")
    job = Job(id="j", task="t", model="m", endpoint="e", base_url=ep.url, cwd=str(tmp_path), run_cwd=str(tmp_path),
              isolation="none", log_dir=str(tmp_path / "log"))
    Path(job.log_dir).mkdir()
    monkeypatch.setattr(job, "record_transcript", lambda: None)
    monkeypatch.setattr(job, "changed", lambda: None)
    asyncio.run(runner.run_job(job, reg, res, permission_mode="plan", max_turns=7, allowed_tools=None,
                               system_prompt_append="x", resume_session=None, timeout_s=5,
                               shim_base_url="http://shim"))
    assert job.status == "succeeded" and job.result == "ok"
    assert len(calls) == 1
    spec = calls[0]
    assert isinstance(spec, SessionSpec)
    assert (spec.permission_mode, spec.max_turns, spec.system_prompt_append, spec.background,
            spec.shim_base_url) == ("plan", 7, "x", False, "http://shim")
    assert spec.defaults is reg.defaults
