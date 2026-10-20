"""strata (llama.cpp-compatible server) probing and metrics. Fixtures captured from strata
0.1.44.1 on 2026-10-16: /props has no endpoint_slots/endpoint_metrics, /slots and /metrics are
always on, /metrics is JSON with cumulative ``totals`` and a ``live`` queue depth."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from localagents.registry import (
    Defaults, Endpoint, Registry, backend_kind, fetch_metrics, metrics_delta, parse_strata_metrics,
)
from localagents.status import load_summary

FIX = Path(__file__).parent / "fixtures"
METRICS = json.loads((FIX / "strata_metrics.json").read_text())
PROPS = json.loads((FIX / "strata_props.json").read_text())
MODELS = {"object": "list", "data": [{"id": "qwen3.8-flash-next-iq3_s", "object": "model",
                                      "status": {"value": "loaded"}, "meta": {"n_ctx": 131072}}]}
SLOTS = [{"id": 0, "n_ctx": 131072, "is_processing": True, "n_prompt_tokens": 516}]
HEALTH = {"status": "ok", "max_context": 131072, "model": "qwen3.8-flash-next-iq3_s", "loaded": True, "service": "strata"}
URL = "http://10.0.0.50:8080"


def _serve(monkeypatch, routes: dict[str, object], seen: list[str] | None = None):
    async def get(self, url, **kw):
        path = url[len(URL):]
        if seen is not None:
            seen.append(path)
        if path not in routes:
            return httpx.Response(404, json={"error": {"message": "not found"}}, request=httpx.Request("GET", url))
        body = routes[path]
        if isinstance(body, str):
            return httpx.Response(200, text=body, request=httpx.Request("GET", url))
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", get)


STRATA_ROUTES = {"/v1/models": MODELS, "/props": PROPS, "/slots": SLOTS, "/metrics": METRICS, "/health": HEALTH}


def _reg():
    return Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={})


def test_backend_kind_normalises():
    assert backend_kind("llama.cpp") == "llamacpp"
    assert backend_kind("llama-cpp") == "llamacpp"
    assert backend_kind("Strata") == "strata"
    assert backend_kind("vllm") == "vllm"
    assert backend_kind("") == "other" and backend_kind("sglang") == "other"


def test_strata_probe_reads_slots_and_queue_without_advertising(monkeypatch):
    seen: list[str] = []
    _serve(monkeypatch, STRATA_ROUTES, seen)
    out = asyncio.run(_reg().probe(Endpoint(name="ai1", base_url=URL, backend="strata")))
    assert out["up"] and out["models"] == ["qwen3.8-flash-next-iq3_s"]
    assert out["context"] == {"qwen3.8-flash-next-iq3_s": 131072}
    assert out["context_source"] == "strata:/props n_ctx"
    assert out["slots"] == {"total": 1, "busy": 1, "free": 0}  # from /slots, not /props total_slots
    assert out["metrics"] is True
    assert out["load"] == {"requests_waiting": 0}
    assert "hint" not in out
    assert "/slots" in seen and "/metrics" in seen and "/health" not in seen


def test_llamacpp_marked_strata_gets_a_hint_and_still_works(monkeypatch):
    _serve(monkeypatch, STRATA_ROUTES)
    out = asyncio.run(_reg().probe(Endpoint(name="ai1", base_url=URL, backend="llama.cpp")))
    assert out["context"] == {"qwen3.8-flash-next-iq3_s": 131072}
    assert out["slots"] == {"total": 1}  # no endpoint_slots advertised, so only the count
    assert out["metrics"] is False
    assert "backend: strata" in out["hint"] and "'ai1'" in out["hint"]


def test_real_llamacpp_gets_no_hint(monkeypatch):
    routes = {"/v1/models": {"data": [{"id": "m", "owned_by": "llamacpp"}]},
              "/props": {"default_generation_settings": {"n_ctx": 262144}, "total_slots": 2, "endpoint_slots": True, "endpoint_metrics": True},
              "/slots": [{"id": 0, "is_processing": False}, {"id": 1, "is_processing": False}],
              "/health": {"status": "ok"}}
    _serve(monkeypatch, routes)
    out = asyncio.run(_reg().probe(Endpoint(name="lc", base_url=URL, backend="llama.cpp")))
    assert out["context_source"] == "llama.cpp:/props n_ctx"
    assert out["slots"] == {"total": 2, "busy": 0, "free": 2} and out["metrics"] is True
    assert "hint" not in out


def test_parse_strata_metrics_maps_totals_onto_llamacpp_counters():
    m = parse_strata_metrics(METRICS)
    t = METRICS["totals"]
    assert m["prompt_tokens"] == t["prompt_tokens"] - t["reused"]
    assert m["prompt_tokens_cached"] == t["reused"]
    assert m["generated_tokens"] == t["output_tokens"]
    assert m["prompt_seconds"] == pytest.approx(t["prompt_ms"] / 1000)
    assert m["generated_seconds"] == pytest.approx(t["decode_ms"] / 1000)
    assert m["spec_draft_tokens"] == t["drafts_offered"] and m["spec_accepted_tokens"] == t["drafts_accepted"]
    assert m["requests_waiting"] == 0
    assert parse_strata_metrics({"live": {}}) == {} and parse_strata_metrics([]) == {}  # type: ignore[arg-type]


def test_fetch_metrics_accepts_json_and_delta_matches_one_job(monkeypatch):
    before = json.loads(json.dumps(METRICS))
    before["totals"] = {"since": 0, "requests": 12, "prompt_tokens": 22677, "reused": 1339, "output_tokens": 1159,
                        "prompt_ms": 5302.0, "decode_ms": 11000.0, "drafts_offered": 800, "drafts_accepted": 600}
    _serve(monkeypatch, {"/metrics": before})
    b = asyncio.run(fetch_metrics(Endpoint(name="ai1", base_url=URL, backend="strata")))
    _serve(monkeypatch, {"/metrics": METRICS})
    a = asyncio.run(fetch_metrics(Endpoint(name="ai1", base_url=URL, backend="strata")))
    d = metrics_delta(b, a)
    # the span is the run_agent job of 2026-10-16: one 20k cold prompt, then a 20288-token cache hit
    assert d["prompt_tokens_processed"] == (43027 - 21627) - (22677 - 1339) == 62
    assert d["prompt_tokens_cached"] == 21627 - 1339 == 20288
    assert d["cache_hit_ratio"] == pytest.approx(20288 / (20288 + 62), abs=1e-3)
    assert d["generated_tokens"] == 121
    assert d["prompt_tps"] > 0 and d["generate_tps"] > 0
    assert d["spec_decode_acceptance"] == pytest.approx((723 - 600) / (975 - 800), abs=1e-3)


def test_fetch_metrics_still_parses_prometheus(monkeypatch):
    _serve(monkeypatch, {"/metrics": "# HELP x\nllamacpp:prompt_tokens_total 100\nllamacpp:tokens_predicted_total 7\n"})
    assert asyncio.run(fetch_metrics(Endpoint(name="ai1", base_url=URL, backend="strata"))) == {"prompt_tokens": 100.0, "generated_tokens": 7.0}


def test_load_summary_shows_strata_queue_beside_slots():
    assert load_summary({"slots": {"total": 1, "busy": 1}, "load": {"requests_waiting": 2}}) == "1/1 slots 2 wait"
    assert load_summary({"slots": {"total": 1, "busy": 0}, "load": {"requests_waiting": 0}}) == "0/1 slots"
