"""Story 2.2 item 2: the raw metrics document and the Strata sections the counters parser skips.

Every number pinned here was read from the story-2.1 capture named beside it.
"""
import asyncio
import copy
import json

import httpx
import pytest

from localagents import registry
from localagents.registry import Endpoint, fetch_metrics, fetch_metrics_doc, parse_metrics, parse_strata_extra

from monitor_fixtures import MONITOR, doc, raw, serve

URL = "http://10.0.0.60:8080"
EP = Endpoint(name="ai2", base_url=URL, backend="strata")


def test_fetch_metrics_doc_returns_strata_json_as_a_dict(monkeypatch):
    serve(monkeypatch, URL, {"/metrics": "strata/ai2_metrics_2.json"})
    kind, body = asyncio.run(fetch_metrics_doc(EP))
    assert kind == "strata" and body == doc("strata/ai2_metrics_2.json")


def test_fetch_metrics_doc_returns_prometheus_text_verbatim(monkeypatch):
    serve(monkeypatch, URL, {"/metrics": "vllm/dgx1-a_metrics_1.txt"})
    kind, body = asyncio.run(fetch_metrics_doc(Endpoint(name="dgx1-a", base_url=URL, backend="vllm")))
    assert kind == "prometheus" and body == raw("vllm/dgx1-a_metrics_1.txt").decode()


def test_fetch_metrics_doc_is_none_on_404_connect_error_and_bad_json(monkeypatch):
    serve(monkeypatch, URL, {})
    assert asyncio.run(fetch_metrics_doc(EP)) is None
    serve(monkeypatch, URL, {"/metrics": None})
    assert asyncio.run(fetch_metrics_doc(EP)) is None
    # a body that starts like JSON and is not: the capture's first 100 bytes, served from memory

    async def get(self, url, **kw):
        return httpx.Response(200, content=raw("strata/ai2_metrics_2.json")[:100], request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    assert asyncio.run(fetch_metrics_doc(EP)) is None


@pytest.mark.parametrize("path, parse", [
    ("strata/ai2_metrics_2.json", lambda b: registry.parse_strata_metrics(json.loads(b))),
    ("vllm/dgx1-a_metrics_2.txt", lambda b: parse_metrics(b.decode())),
    ("llamacpp/llamacpp_metrics_1.txt", lambda b: parse_metrics(b.decode())),
])
def test_fetch_metrics_is_the_parsed_doc(monkeypatch, path, parse):
    """fetch_metrics is a thin wrapper over fetch_metrics_doc: same short-name dict as before."""
    serve(monkeypatch, URL, {"/metrics": path})
    assert asyncio.run(fetch_metrics(EP)) == parse(raw(path))


def test_fetch_metrics_keeps_none_for_a_document_with_no_counters(monkeypatch):
    """The llama.cpp health capture is JSON with no totals: parsed empty, so None as before."""
    serve(monkeypatch, URL, {"/metrics": "llamacpp/llamacpp_health_1.json"})
    assert asyncio.run(fetch_metrics(EP)) is None
    assert asyncio.run(fetch_metrics_doc(EP)) == ("strata", {"status": "ok"})


def test_vllm_preemptions_counter_is_parsed():
    """vllm:num_preemptions_total is 0.0 in both dgx1-a captures (one labelled series each)."""
    for n in (1, 2):
        m = parse_metrics(raw(f"vllm/dgx1-a_metrics_{n}.txt").decode())
        assert m["preemptions"] == 0.0


def test_metrics_delta_over_the_vllm_pair_is_unchanged_by_preemptions():
    """metrics_delta diffs the intersection of keys and reads named ones: the new key changes
    nothing. dgx1-a pair: prompt +367870 (18361898 - 17994028), hits +357888 (17534720 -
    17176832), queries +367870, generation +6169 (299686 - 293517), drafts +9214
    (437219 - 428005), accepted +4433 (217046 - 212613)."""
    b = parse_metrics(raw("vllm/dgx1-a_metrics_1.txt").decode())
    a = parse_metrics(raw("vllm/dgx1-a_metrics_2.txt").decode())
    assert registry.metrics_delta(b, a, "vllm") == {
        "generated_tokens": 6169,
        "prompt_tokens_processed": 367870 - 357888,
        "prompt_tokens_cached": 357888,
        "cache_hit_ratio": round(357888 / 367870, 3),
        "spec_decode_acceptance": round(4433 / 9214, 3),
    }


def test_parse_strata_extra_reads_since_speed_hardware_and_state_from_ai2_pass_2():
    x = parse_strata_extra(doc("strata/ai2_metrics_2.json"))
    assert x["since"] == 1793229893.2645304  # totals.since
    assert x["gen_tps"] == 0.0  # hardware.tok_s is 0.0 (a number); live.tok_s is null
    assert x["requests_running"] == 0  # live.state "idle"
    assert x["hardware"] == {
        "gpu_util": 99.0, "gpu_mem_used": 51178242048.0, "gpu_mem_total": 51539607552.0,
        "gpu_temp": 61.0, "gpu_power": 247.846, "gpu_power_limit": 260.0,
        "gpu_pcie_rx_mb": 193.357421875, "gpu_pcie_tx_mb": 63.8955078125,
        "cpu": 95.8, "ram_used": 98043719680.0, "ram_total": 338017230848.0,
    }
    assert all(type(v) is float for v in x["hardware"].values())


def test_parse_strata_extra_speed_from_ai2_pass_1():
    """Pass 1 caught ai2 0.3 s after a request finished: live.state idle, live.tok_s null,
    hardware.tok_s 111.46877721920072."""
    x = parse_strata_extra(doc("strata/ai2_metrics_1.json"))
    assert x["gen_tps"] == 111.46877721920072
    assert x["requests_running"] == 0


def test_parse_strata_extra_on_a_capture_without_hardware():
    """tests/fixtures/strata_metrics.json was captured from strata 0.1.44.1 before the hardware
    section existed: hardware None, no speed (live.tok_s null), since read."""
    old = json.loads((MONITOR.parent / "strata_metrics.json").read_text())
    assert "hardware" not in old
    x = parse_strata_extra(old)
    assert x["hardware"] is None and x["gen_tps"] is None
    assert x["since"] == 1792123400.7976325
    assert x["requests_running"] == 0


def test_parse_strata_extra_running_when_live_state_is_not_idle():
    """Every captured live.state is "idle" (all four story-2.1 Strata captures and the 0.1.44.1
    one), so the busy case mutates one field of a capture: any other state string is a request
    in flight; a missing state is unknown. live.tok_s is the speed when hardware.tok_s is absent."""
    busy = copy.deepcopy(doc("strata/ai2_metrics_2.json"))
    busy["live"]["state"] = "decode"
    assert parse_strata_extra(busy)["requests_running"] == 1
    del busy["live"]["state"]
    assert parse_strata_extra(busy)["requests_running"] is None
    busy["live"]["tok_s"] = 93.0
    del busy["hardware"]["tok_s"]
    assert parse_strata_extra(busy)["gen_tps"] == 93.0


def test_parse_strata_extra_hardware_missing_keys_are_none_and_bools_are_not_numbers():
    d = copy.deepcopy(doc("strata/ai1_metrics_1.json"))
    del d["hardware"]["gpu_power_limit"]
    d["hardware"]["cpu"] = True
    hw = parse_strata_extra(d)["hardware"]
    assert hw["gpu_power_limit"] is None and hw["cpu"] is None
    assert hw["gpu_mem_total"] == 60121153536.0  # ai1: two GPUs, summed by strata itself


def test_parse_strata_extra_on_garbage_is_all_none():
    for bad in ({}, [], None, {"totals": "x", "live": 3, "hardware": []}):
        assert parse_strata_extra(bad) == {"since": None, "gen_tps": None, "hardware": None, "requests_running": None}
