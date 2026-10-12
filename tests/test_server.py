import asyncio

import pytest

from localagents import server
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
        cwd="/tmp", run_cwd="/tmp", isolation="none", **kw,
    )
    job.session_id = "sess-1"
    return job


def test_resume_keeps_previous_model(reg):
    prev = _prev_job()
    out = asyncio.run(server.run_agent(task="next", resume_job=prev.id))
    assert out["model"] == "big-model" and out["endpoint"] == "second"


def test_resume_with_explicit_model_overrides(reg):
    prev = _prev_job()
    out = asyncio.run(server.run_agent(task="next", resume_job=prev.id, model="default-model"))
    assert out["model"] == "default-model" and out["endpoint"] == "first"


def test_fresh_job_without_model_uses_first_live(reg):
    out = asyncio.run(server.run_agent(task="new"))
    assert out["model"] == "default-model"


def _prev_worktree_job(tmp_path):
    root = tmp_path / "worktrees" / "proj" / "old-job"  # does not exist: removed for having no changes
    job = server.store.new(
        task="t", model="big-model", endpoint="second", base_url="http://second:8080",
        cwd="/tmp", run_cwd=str(root), isolation="worktree",
        worktree={"repo": "/tmp", "path": str(root), "root": str(root), "branch": "local-agent/old-job",
                  "base": "abc123", "kept": False, "note": "no changes; worktree removed"},
    )
    job.session_id = "sess-wt"
    return job


def test_resume_worktree_job_keeps_tree_even_when_removed(reg, tmp_path):
    prev = _prev_worktree_job(tmp_path)
    out = asyncio.run(server.run_agent(task="next", resume_job=prev.id))
    assert out.get("error") is None
    assert out["isolation"] == "worktree"
    assert out["run_cwd"] == prev.run_cwd and out["cwd"] == prev.cwd
    # the tree's identity is carried over; the previous outcome (kept/note) is not
    assert out["worktree"] == {k: prev.worktree[k] for k in ("repo", "path", "root", "branch", "base")}


def test_resume_plain_job_with_missing_cwd_is_an_error(reg, tmp_path):
    prev = _prev_job()
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
