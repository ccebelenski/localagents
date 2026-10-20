"""Per-endpoint ``api_key``: sent on every backend call, never handed to the delegated session."""

import asyncio
import json

import httpx
import pytest

from localagents import server
from localagents.registry import Defaults, Endpoint, Registry, fetch_metrics
from localagents.runner import build_env
from localagents.shim import Shim

KEY = "sk-strata-secret-123"
URL = "http://ai1:8080"


class _Recorder:
    """Patch httpx.AsyncClient.send: record every outgoing request, answer with canned bodies."""

    def __init__(self, monkeypatch, bodies=None):
        self.requests: list[httpx.Request] = []
        bodies = bodies or {}

        async def send(client, request, **kw):
            self.requests.append(request)
            path = request.url.path
            body = bodies.get(path, {})
            return httpx.Response(200 if path in bodies else 404, json=body, request=request)

        monkeypatch.setattr(httpx.AsyncClient, "send", send)

    def auth(self, path):
        matches = [r for r in self.requests if r.url.path == path]
        assert matches, f"no request to {path}; saw {[r.url.path for r in self.requests]}"
        h = matches[-1].headers
        return h.get("x-api-key"), h.get("authorization")


# ---------------------------------------------------------------- config

def test_endpoint_without_key_sends_the_local_placeholder_pair():
    ep = Endpoint(name="a", base_url=URL)
    assert ep.auth_headers() == {"x-api-key": "local", "authorization": "Bearer local"}


def test_endpoint_with_key_sends_it_in_both_header_forms():
    ep = Endpoint(name="a", base_url=URL, api_key=KEY)
    assert ep.auth_headers() == {"x-api-key": KEY, "authorization": f"Bearer {KEY}"}


def test_registry_loads_api_key_from_yaml(tmp_path):
    (tmp_path / "models.yaml").write_text(
        f"endpoints:\n  ai1:\n    base_url: {URL}\n    backend: strata\n    api_key: {KEY}\n  open:\n    base_url: http://x:1\n"
    )
    reg = Registry.load(tmp_path / "models.yaml")
    assert reg.endpoints["ai1"].api_key == KEY
    assert reg.endpoints["open"].api_key is None


# ---------------------------------------------------------------- probes and metrics

def test_probe_sends_the_key_on_every_discovery_call(monkeypatch):
    rec = _Recorder(monkeypatch, {
        "/v1/models": {"data": [{"id": "m"}]},
        "/props": {"default_generation_settings": {"n_ctx": 131072}},
        "/slots": [],
        "/metrics": {"live": {"queued": 0}, "totals": {}},
    })
    reg = Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={})
    out = asyncio.run(reg.probe(Endpoint(name="ai1", base_url=URL, backend="strata", api_key=KEY)))
    assert out["up"] is True
    for path in ("/v1/models", "/props", "/slots", "/metrics"):
        assert rec.auth(path) == (KEY, f"Bearer {KEY}"), path


def test_fetch_metrics_sends_the_key(monkeypatch):
    rec = _Recorder(monkeypatch, {"/metrics": {"totals": {"output_tokens": 5}}})
    out = asyncio.run(fetch_metrics(Endpoint(name="ai1", base_url=URL, backend="strata", api_key=KEY)))
    assert out and rec.auth("/metrics") == (KEY, f"Bearer {KEY}")


# ---------------------------------------------------------------- local_complete

@pytest.fixture
def keyed_reg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    endpoints = {
        "ai1": Endpoint(name="ai1", base_url=URL, backend="strata", api_key=KEY),
        "open": Endpoint(name="open", base_url="http://open:8080", backend="llama.cpp"),
    }
    r = Registry(path=None, endpoints=endpoints, defaults=Defaults(), raw={}, local={})

    async def probe(self, ep):
        return {"up": True, "models": [f"{ep.name}-model"]}

    monkeypatch.setattr(Registry, "probe", probe)
    monkeypatch.setattr(server, "_reg", lambda: r)
    return r


def test_local_complete_sends_the_key(keyed_reg, monkeypatch):
    rec = _Recorder(monkeypatch, {"/v1/messages": {"content": [{"type": "text", "text": "hi"}], "usage": {}}})
    out = asyncio.run(server.local_complete(prompt="hi", endpoint="ai1"))
    assert out["text"] == "hi"
    assert rec.auth("/v1/messages") == (KEY, f"Bearer {KEY}")


def test_local_complete_without_key_keeps_the_placeholder(keyed_reg, monkeypatch):
    rec = _Recorder(monkeypatch, {"/v1/messages": {"content": [{"type": "text", "text": "hi"}], "usage": {}}})
    asyncio.run(server.local_complete(prompt="hi", endpoint="open"))
    assert rec.auth("/v1/messages") == ("local", "Bearer local")


# ---------------------------------------------------------------- shim relay

class _Upstream:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    async def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"id": "msg", "content": [], "usage": {"input_tokens": 1, "output_tokens": 1}})


def _relay(tmp_path, endpoints, job_endpoint):
    """Drive one /v1/messages request through the shim; return the upstream request it made."""
    from localagents.jobs import Job

    job = Job(id="j1", task="t", model="m", endpoint=job_endpoint, base_url=URL, cwd=str(tmp_path),
              run_cwd=str(tmp_path), isolation="none", log_dir=str(tmp_path))
    up = _Upstream()
    shim = Shim(lambda jid: job, endpoint_lookup=lambda name: endpoints.get(name))
    shim._client = httpx.AsyncClient(transport=httpx.MockTransport(up))

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=shim.app), base_url="http://shim") as c:
            r = await c.post(
                "/job/j1/v1/messages",
                json={"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]},
                headers={"x-api-key": "local", "authorization": "Bearer local", "anthropic-version": "2023-06-01"},
            )
            assert r.status_code == 200, r.text
    asyncio.run(go())
    assert len(up.requests) == 1
    return up.requests[0]


def test_shim_replaces_the_sessions_placeholder_with_the_endpoint_key(tmp_path):
    endpoints = {"ai1": Endpoint(name="ai1", base_url=URL, api_key=KEY)}
    req = _relay(tmp_path, endpoints, "ai1")
    assert req.headers["x-api-key"] == KEY
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["anthropic-version"] == "2023-06-01"


def test_shim_forwards_the_placeholder_when_the_endpoint_has_no_key(tmp_path):
    endpoints = {"open": Endpoint(name="open", base_url=URL)}
    req = _relay(tmp_path, endpoints, "open")
    assert req.headers["x-api-key"] == "local"
    assert req.headers["authorization"] == "Bearer local"


# ---------------------------------------------------------------- no leaks

def test_session_env_never_carries_the_real_key(keyed_reg):
    from localagents.registry import Resolution

    res = Resolution.make(keyed_reg.endpoints["ai1"], "ai1-model", {"up": True, "models": ["ai1-model"]})
    env = build_env(res, "http://127.0.0.1:1/job/x")
    assert KEY not in json.dumps(env)
    assert env["ANTHROPIC_API_KEY"] == "local" and env["ANTHROPIC_AUTH_TOKEN"] == "local"


def test_list_models_never_shows_the_key(keyed_reg):
    out = asyncio.run(server.list_models())
    assert KEY not in json.dumps(out)
    assert out["endpoints"]["ai1"]["up"] is True
