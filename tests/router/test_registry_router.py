"""Story "router 2: backend: router in the registry" (router plan 2026-11-02, task 2).

Self-contained by design: the ``tests/router/__init__.py`` and ``conftest.py`` belong
to task 1's lane, so every fixture this module needs lives in this module.
"""

import asyncio
import errno
from pathlib import Path

import httpx
import pytest

from localagents import server
from localagents.registry import (Defaults, Endpoint, ModelUnavailable, Registry, backend_kind,
                                  parse_metrics)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "router" / "vllm_metrics_pooled.txt"

DECISION_URL = "http://127.0.0.1:8765"


def _router(name: str = "auto") -> Endpoint:
    return Endpoint(name=name, base_url="", backend="router", decision_url=DECISION_URL)


def _refused() -> httpx.ConnectError:
    """Mimic httpx: ConnectError -> ConnectError -> OSError (tests/test_probe.py's pattern)."""
    inner = httpx.ConnectError("All connection attempts failed")
    inner.__cause__ = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
    outer = httpx.ConnectError("All connection attempts failed")
    outer.__cause__ = inner
    return outer


def _fake_probe():
    """Up everywhere: the router reports its own name, the reals one model each with a window."""

    async def probe(self, ep):
        if ep.is_router:
            return {"up": True, "models": [ep.name], "router": True,
                    "decision_model": ep.decision_model, "context": {}}
        window = 131072 if ep.name.endswith("1") else 262144
        return {"up": True, "host": "up", "models": [f"m-{ep.name}"],
                "context": {f"m-{ep.name}": window}, "context_source": "test"}

    return probe


def test_backend_kind_router():
    assert backend_kind("router") == "router"


def test_decision_max_tokens_default_is_under_the_server_limit():
    """12,000 tokens (the client's four-chars-each reading: 48,000 chars) sits under the
    server's own 60,000-char limit — measured on the live box — so an over-long brief
    truncates at the client instead of failing inside the model."""
    assert _router().decision_max_tokens == 12_000


def test_load_ignores_unknown_keys_with_a_warning(tmp_path, capsys):
    """§12's upgrade order: a key this package does not know is skipped with a line on
    stderr, never a TypeError inside a running server that re-reads the file."""
    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        "endpoints:\n"
        "  llamacpp:\n"
        "    base_url: http://127.0.0.1:8080\n"
        "    backend: llama.cpp\n"
        "    foo: 1\n"
    )
    reg = Registry.load(cfg)
    assert reg.endpoints["llamacpp"].base_url == "http://127.0.0.1:8080"
    err = capsys.readouterr().err
    assert "localagents: endpoint 'llamacpp': ignoring unknown key 'foo'" in err


def test_invalid_router_entry_is_marked_and_other_entries_still_work(tmp_path, capsys, monkeypatch):
    """I2: the server re-reads models.yaml on every call, so a typo in one entry must not
    raise into every running job. The bad entry is marked and printed once per load; the good
    ones load, resolve and serve as before."""
    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        "endpoints:\n"
        "  auto:\n"
        "    backend: router\n"
        "    decision_url: \n"
        "  real:\n"
        "    base_url: http://127.0.0.1:8080\n"
        "    backend: vllm\n"
    )
    reg = Registry.load(cfg)  # the old code raised ValueError right here
    err = capsys.readouterr().err
    assert "localagents: endpoint 'auto': backend router needs decision_url, ignored until fixed" in err
    auto = reg.endpoints["auto"]
    assert auto.invalid == "backend router needs decision_url"
    # the marked entry's probe is its problem, with no network call to make it up
    out = asyncio.run(reg.probe(auto))
    assert out == {"up": False, "models": [], "error": "backend router needs decision_url"}
    # naming the broken entry with endpoint= names its problem instead of resolving to it
    with pytest.raises(ModelUnavailable, match="backend router needs decision_url"):
        asyncio.run(reg.resolve(endpoint="auto"))
    # the good entry loads, resolves and serves
    assert reg.endpoints["real"].invalid is None
    monkeypatch.setattr(Registry, "probe", _fake_probe())
    res = asyncio.run(reg.resolve(model="m-real"))
    assert res.endpoint.name == "real"


def test_real_entry_without_base_url_is_marked_invalid(tmp_path, capsys):
    cfg = tmp_path / "models.yaml"
    cfg.write_text("endpoints:\n  nope:\n    backend: vllm\n")  # base_url is what it forgot
    reg = Registry.load(cfg)  # used to raise ValueError here
    assert "localagents: endpoint 'nope': needs base_url, ignored until fixed" in capsys.readouterr().err
    assert reg.endpoints["nope"].invalid == "needs base_url"
    assert reg.real_endpoints() == []  # an entry serving nothing is not real
    out = asyncio.run(reg.probe(reg.endpoints["nope"]))
    assert out == {"up": False, "models": [], "error": "needs base_url"}


@pytest.mark.parametrize("value, usable", [
    ('"2"', False),  # quoted: str < int raises in capacity.from_probe's min()
    ("0", False),    # an endpoint that admits nothing
    ("true", False),  # a bool is not a whole number here, not a cap of 1 either
    ("2", True),     # a real cap
    (None, True),    # absent: the capacity model alone decides
])
def test_max_concurrent_must_be_a_positive_int(tmp_path, capsys, value, usable):
    """M7: ``max_concurrent`` is never coerced — it feeds ``min(sessions, cap)`` in the
    throughput probe as written. An entry whose cap is present and not an int >= 1 is marked
    with its problem (I2's pattern: printed once per load, probes down), never a TypeError
    into the calibration run and never a silent zero-cap endpoint."""
    cfg = tmp_path / "models.yaml"
    line = f"    max_concurrent: {value}\n" if value is not None else ""
    cfg.write_text(
        "endpoints:\n  real:\n    base_url: http://127.0.0.1:8080\n    backend: vllm\n" + line
    )
    reg = Registry.load(cfg)  # marked, never raised
    ep = reg.endpoints["real"]
    if usable:
        assert ep.invalid is None
        assert ep.max_concurrent == (2 if value == "2" else None)
    else:
        assert ep.invalid == "max_concurrent must be a whole number of 1 or more"
        assert ("localagents: endpoint 'real': max_concurrent must be a whole number of 1 "
                "or more, ignored until fixed") in capsys.readouterr().err
        out = asyncio.run(reg.probe(ep))  # the probe answers with the problem, no network
        assert out == {"up": False, "models": [], "error": ep.invalid}


def test_shim_lookup_survives_an_invalid_router_entry(tmp_path, monkeypatch):
    """The shim looks up an endpoint per request against a freshly loaded registry: one
    broken router entry must not turn every lookup into a raised ValueError (a 500)."""
    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        "endpoints:\n"
        "  auto:\n"
        "    backend: router\n"  # no decision_url: marked, not fatal
        "  real:\n"
        "    base_url: http://127.0.0.1:8080\n"
        "    backend: vllm\n"
    )
    reg = Registry.load(cfg)  # the per-request load answers, it does not raise
    monkeypatch.setattr(Registry, "probe", _fake_probe())
    res = asyncio.run(reg.resolve())  # what the shim does with no model named
    assert res.endpoint.name == "real" and res.served_model == "m-real"


def test_router_probe_reports_health_as_served_id(monkeypatch):
    seen = []

    async def fake_get(self, url, **kw):
        seen.append(url)
        return httpx.Response(200, json={"ok": True, "model": "vllm-sr/d3-mini"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    reg = Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={})
    out = asyncio.run(reg.probe(_router()))
    assert seen == [f"{DECISION_URL}/health"]
    assert out["up"] is True
    assert out["router"] is True
    assert out["models"] == ["auto"]
    assert out["decision_model"] == "vllm-sr/d3-mini"
    assert out["context"] == {}
    assert "decision_model_mismatch" not in out  # the entry says the same model


def test_router_probe_flags_a_decision_model_mismatch(monkeypatch):
    """The probe's ``decision_model`` is what the server reports; when that disagrees with
    the entry, the probe says so (still up) rather than every job scoring another model."""
    async def fake_get(self, url, **kw):
        return httpx.Response(200, json={"ok": True, "model": "someone-elses/d3-large"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    reg = Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={})
    ep = _router()
    ep.decision_model = "vllm-sr/d3-mini"
    out = asyncio.run(reg.probe(ep))
    assert out["up"] is True  # the server answers; only the identity disagrees
    assert out["decision_model"] == "someone-elses/d3-large"  # the reported one wins the field
    assert out["decision_model_mismatch"] is True
    assert out["hint"] == "the d3 server reports someone-elses/d3-large; the entry says vllm-sr/d3-mini"


def test_router_probe_down_when_health_fails(monkeypatch):
    async def boom(self, url, **kw):
        raise _refused()

    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    reg = Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={})
    out = asyncio.run(reg.probe(_router()))
    assert out["up"] is False
    assert out["models"] == []
    assert out["router"] is True
    # the classifier's shape rides along, same as a real probe's
    assert out["host"] == "up" and "8765" in out["error"]


def test_match_router_id_is_exact_only():
    exact = {"auto"}
    # a router entry's name never answers to anything but itself
    assert Registry.match("auto", ["my-autoround-q4"], exact_only=exact) is None
    assert Registry.match("auto", ["auto"], exact_only=exact) == "auto"
    assert Registry.match("a*", ["auto"], exact_only=exact) is None  # no glob onto the router id
    # real served ids keep the old paths
    assert Registry.match("auto*", ["my-autoround-q4"], exact_only=exact) == "my-autoround-q4"
    assert Registry.match("qwen", ["unsloth/Qwen3.8-27B"], exact_only=exact) == "unsloth/Qwen3.8-27B"


def test_resolve_without_model_skips_router_entries(monkeypatch):
    auto, real = _router(), Endpoint(name="real", base_url="http://127.0.0.1:8080", backend="vllm")
    reg = Registry(path=None, endpoints={"auto": auto, "real": real},
                   defaults=Defaults(), raw={}, local={})
    monkeypatch.setattr(Registry, "probe", _fake_probe())
    res = asyncio.run(reg.resolve())
    assert res.endpoint.name == "real" and res.served_model == "m-real"
    # the router entry is a candidate only when model names it exactly
    res2 = asyncio.run(reg.resolve(model="auto"))
    assert res2.endpoint.name == "auto" and res2.served_model == "auto"
    # and a real model never lands on the router by substring
    res3 = asyncio.run(reg.resolve(model="m-real"))
    assert res3.endpoint.name == "real"


def test_parse_metrics_reads_cache_config_labels():
    mt = parse_metrics(FIXTURE.read_text())
    assert mt["kv_cache_size_tokens"] == 307852.0
    assert mt["kv_cache_max_concurrency"] == pytest.approx(1.00046, abs=1e-4)
    # the series the parser already read are untouched
    assert mt["requests_running"] == 0.0 and mt["requests_waiting"] == 0.0


def test_list_models_router_entry_has_context_range_and_local_complete_skips_it(monkeypatch):
    real1 = Endpoint(name="real1", base_url="http://real1:8080", backend="vllm")
    real2 = Endpoint(name="real2", base_url="http://real2:8080", backend="llama.cpp")
    reg = Registry(path=None, endpoints={"auto": _router(), "real1": real1, "real2": real2},
                   defaults=Defaults(), raw={}, local={})
    monkeypatch.setattr(Registry, "probe", _fake_probe())
    monkeypatch.setattr(server, "_reg", lambda: reg)

    out = asyncio.run(server.list_models())
    auto_d = out["endpoints"]["auto"]
    assert auto_d["router"] is True
    assert auto_d["context_range"] == [131072, 262144]
    # real entries unchanged: no router key, no context_range
    assert "router" not in out["endpoints"]["real1"] and "context_range" not in out["endpoints"]["real1"]

    posts = []

    async def fake_post(self, url, **kw):
        posts.append(url)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}],
                                         "stop_reason": "end_turn", "usage": {}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    res = asyncio.run(server.local_complete(prompt="hi"))
    assert res["endpoint"] == "real1"
    assert posts == ["http://real1:8080/v1/messages"]


def _d3_unreachable(monkeypatch):
    """router 12: run_agent on the router entry scores the brief with d3; the tests never reach
    a real d3 server (the operator's box may run one on the default port): the client's
    transport refuses every connection."""
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(server, "_d3_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse)))


def test_run_agent_on_a_router_entry_is_refused_before_a_job_exists(monkeypatch):
    """Router 12 (pin moved: the dispatch story is built, so `not_routable` is gone): a router
    entry routes the job, and with d3 unreachable the answer is model_unavailable with the
    d3 guidance (design §4, §14), before store.new(): no job is recorded and nothing is left
    `queued` for good."""
    real = Endpoint(name="real", base_url="http://127.0.0.1:8080", backend="vllm")
    reg = Registry(path=None, endpoints={"auto": _router(), "real": real},
                   defaults=Defaults(), raw={}, local={})
    monkeypatch.setattr(Registry, "probe", _fake_probe())
    monkeypatch.setattr(server, "_reg", lambda: reg)
    _d3_unreachable(monkeypatch)
    out = asyncio.run(server.run_agent(task="x", model="auto"))
    assert out["error"] == "model_unavailable" and out["reason"] == "down"
    assert out["guidance"] == "name a model, or check the d3 server"
    assert server.store.jobs == {}


def test_run_agent_on_a_router_entry_is_refused_even_when_d3_is_down(monkeypatch):
    """M1: the refusal was posed after resolve, so with the d3 server down the probe said
    the router entry was dead and the operator got "no endpoint serving 'auto'" instead of
    the true answer. The guard reads the config, not the network."""
    real = Endpoint(name="real", base_url="http://127.0.0.1:8080", backend="vllm")
    reg = Registry(path=None, endpoints={"auto": _router(), "real": real},
                   defaults=Defaults(), raw={}, local={})

    async def down(self, ep):  # nothing is up: neither d3 nor the real box
        return {"up": False, "models": [], "error": "test: down"}

    monkeypatch.setattr(Registry, "probe", down)
    monkeypatch.setattr(server, "_reg", lambda: reg)
    _d3_unreachable(monkeypatch)
    for kwargs in ({"model": "auto"}, {"endpoint": "auto"}):  # named either way
        out = asyncio.run(server.run_agent(task="x", **kwargs))
        # router 12 (pin moved): the true answer is now the d3 server's, never "no endpoint
        # serving 'auto'"; the guard still reads the config, not the router entry's probe
        assert out["error"] == "model_unavailable" and out["reason"] == "down"
        assert "d3 server" in out["message"]
    assert server.store.jobs == {}


def test_run_agent_with_defaults_model_naming_a_router_is_refused_even_when_d3_is_down(monkeypatch):
    """The fix wave's re-review: `defaults: {model: auto}` and no `model` argument reached
    resolve and, with d3 down, answered model_unavailable instead of not_routable."""
    real = Endpoint(name="real", base_url="http://127.0.0.1:8080", backend="vllm")
    reg = Registry(path=None, endpoints={"auto": _router(), "real": real},
                   defaults=Defaults(model="auto"), raw={}, local={})

    async def down(self, ep):
        return {"up": False, "models": [], "error": "test: down"}

    monkeypatch.setattr(Registry, "probe", down)
    monkeypatch.setattr(server, "_reg", lambda: reg)
    _d3_unreachable(monkeypatch)
    out = asyncio.run(server.run_agent(task="x"))
    # router 12 (pin moved): `defaults: {model: auto}` routes like a named router entry
    assert out["error"] == "model_unavailable" and out["reason"] == "down"
    assert "d3 server" in out["message"]
    assert server.store.jobs == {}


def test_real_only_with_only_router_entries_names_the_cause():
    """local_complete needs an endpoint that serves /v1/messages; on a config of nothing
    but routers, "No endpoint is up. Ask the user to start a model server" would send the
    operator to servers this config never named. Name the actual cause."""
    reg = Registry(path=None, endpoints={"auto": _router()}, defaults=Defaults(), raw={}, local={})
    with pytest.raises(ModelUnavailable, match="only router entries are configured"):
        asyncio.run(reg.resolve(real_only=True))


# --- router 11: pools, operator-written and inert until declared (design §7 "Accepted" and
# "Decided" 2026-11-04: "as long as it's ignored until needed, otherwise we do self-harm") ---

_POOLED = (
    "endpoints:\n"
    "  dgx1:\n"
    "    base_url: http://127.0.0.1:8000\n"
    "    backend: vllm\n"
    "    pool: gpu\n"
    "  dgx1-lcpp:\n"
    "    base_url: http://127.0.0.1:8081\n"
    "    backend: llama.cpp\n"
    "    pool: gpu\n"
    "  solo:\n"
    "    base_url: http://127.0.0.1:8080\n"
    "    backend: llama.cpp\n"
)


def test_pool_named_but_undeclared_is_noted_and_ignored(tmp_path, capsys):
    """A ``pool:`` naming no ``pools:`` entry is one line on stderr at load and has no effect:
    never a load or validation error, the entry stays usable, its pool reads None."""
    cfg = tmp_path / "models.yaml"
    cfg.write_text(_POOLED)
    reg = Registry.load(cfg)
    err = capsys.readouterr().err
    assert ("localagents: endpoint 'dgx1': pool 'gpu' is not declared under pools:, ignoring it"
            in err)
    assert ("localagents: endpoint 'dgx1-lcpp': pool 'gpu' is not declared under pools:, ignoring it"
            in err)
    assert reg.endpoints["dgx1"].pool is None and reg.endpoints["dgx1"].invalid is None
    assert [e.name for e in reg.real_endpoints()] == ["dgx1", "dgx1-lcpp", "solo"]
    assert reg.pools == {}
    assert reg.pool_members("gpu") == []


def test_pools_block_is_read_and_members_listed(tmp_path, capsys):
    """The ``pools:`` block (``{<name>: {max_concurrent: int}}``) is read as written and the
    members are the entries that name it, in config order; nothing is inferred from the
    shared host or the ports."""
    cfg = tmp_path / "models.yaml"
    cfg.write_text(_POOLED + "pools:\n  gpu:\n    max_concurrent: 1\n  ups:\n    max_concurrent: 2\n")
    reg = Registry.load(cfg)
    assert "pool" not in capsys.readouterr().err
    assert reg.pools == {"gpu": {"max_concurrent": 1}, "ups": {"max_concurrent": 2}}
    assert [e.name for e in reg.pool_members("gpu")] == ["dgx1", "dgx1-lcpp"]
    assert reg.pool_members("ups") == []  # declared, nobody in it
    assert reg.endpoints["dgx1"].pool == "gpu" and reg.endpoints["solo"].pool is None


def test_no_pools_block_means_no_pool(tmp_path, capsys):
    """A config with neither key is today's config: no pool anywhere, nothing printed, and a
    Registry built directly (the tests' pattern) has an empty pools map too."""
    cfg = tmp_path / "models.yaml"
    cfg.write_text("endpoints:\n  solo:\n    base_url: http://127.0.0.1:8080\n    backend: llama.cpp\n")
    reg = Registry.load(cfg)
    assert capsys.readouterr().err == ""
    assert reg.pools == {} and reg.endpoints["solo"].pool is None
    assert Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={}).pools == {}


@pytest.mark.parametrize("block", [
    "pools:\n  gpu:\n    max_concurrent: \"1\"\n",  # quoted
    "pools:\n  gpu:\n    max_concurrent: 0\n",
    "pools:\n  gpu:\n    max_concurrent: true\n",
    "pools:\n  gpu: {}\n",  # no cap at all
    "pools:\n  gpu: 1\n",  # not a mapping
])
def test_a_pool_with_a_bad_cap_is_noted_and_ignored(tmp_path, capsys, block):
    """A ``pools:`` entry whose cap is not a whole number of 1 or more is named once on stderr
    and ignored, never coerced and never an error that takes its members down; its members
    then belong to no pool."""
    cfg = tmp_path / "models.yaml"
    cfg.write_text(_POOLED + block)
    reg = Registry.load(cfg)
    err = capsys.readouterr().err
    assert ("localagents: pool 'gpu': max_concurrent must be a whole number of 1 or more, "
            "ignoring the pool") in err
    assert reg.pools == {}
    assert reg.endpoints["dgx1"].pool is None and reg.endpoints["dgx1"].invalid is None
