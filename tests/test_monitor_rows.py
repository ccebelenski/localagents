"""Story 2.2 item 3: one probe result becomes a probes row plus its counters and hardware rows.

The probe dicts come from the real ``Registry.probe`` run over the story-2.1 captures (served by
``monitor_fixtures.serve``), never typed: the rows are built from what the prober really returns.
"""
import asyncio

import httpx
import pytest

from localagents import db
from localagents.monitor import collect, rows
from localagents.registry import Defaults, Endpoint, Registry, classify_connect_failure, fetch_metrics_doc

from monitor_fixtures import LLAMACPP, STRATA_AI2, VLLM_DGX1A, raw, serve

URL = "http://10.0.0.60:8080"
T = 1793288065.0


def _reg():
    return Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={})


def _probe_and_doc(monkeypatch, routes, backend):
    serve(monkeypatch, URL, routes)
    ep = Endpoint(name="ep", base_url=URL, backend=backend)
    return asyncio.run(_reg().probe(ep)), asyncio.run(fetch_metrics_doc(ep))


def _latency(r):
    """latency_ms is the prober's wall clock: an int, whatever its value; pinned out of the rest."""
    assert isinstance(r.probe["latency_ms"], int) and r.probe["latency_ms"] >= 0
    return {k: v for k, v in r.probe.items() if k != "latency_ms"}


def test_strata_probe_and_ai2_doc_give_the_pinned_columns(monkeypatch):
    probe, doc = _probe_and_doc(monkeypatch, STRATA_AI2, "strata")
    r = rows.probe_to_rows(3, 4, probe, doc, T)
    assert _latency(r) == {
        "t": T, "endpoint_id": 3, "model_id": 4, "up": 1, "host": "up", "error": None,
        "context": 131072,  # ai2_props_1 default_generation_settings.n_ctx
        "slots_total": 1, "slots_busy": 0,  # ai2_slots_1: one slot, is_processing false
        "requests_running": 0,  # live.state "idle"
        "requests_waiting": 0,  # live.queued 0
        "kv_cache_usage": None,  # strata reports no KV fill
        "gen_tps": 111.46877721920072,  # hardware.tok_s
        "since": 1793229893.2645304,  # totals.since
        "state": "idle",  # ruling A: live.state verbatim
    }
    # ai2_metrics_1 totals: prompt_tokens 15631377, reused 15243241, output_tokens 220395,
    # prompt_ms 751563.9000000007, decode_ms 3207289.7000000016, drafts 184778 / 132173
    assert r.counters == {
        "prompt_tokens": 15631377.0 - 15243241.0,  # processed = 388136
        "prompt_tokens_cached": 15243241.0,
        "prompt_seconds": 751563.9000000007 / 1000.0,
        "generated_tokens": 220395.0,
        "generated_seconds": 3207289.7000000016 / 1000.0,
        "spec_draft_tokens": 184778.0,
        "spec_accepted_tokens": 132173.0,
        "prefix_queries": None, "prefix_hits": None, "preemptions": None,
    }
    assert r.hardware == {
        "gpu_util": 100.0, "gpu_mem_used": 51178242048.0, "gpu_mem_total": 51539607552.0,
        "gpu_temp": 76.0, "gpu_power": 252.132, "gpu_power_limit": 260.0,
        "gpu_pcie_rx_mb": 90.2646484375, "gpu_pcie_tx_mb": 90.408203125,
        "cpu": 95.9, "ram_used": 98149974016.0, "ram_total": 338017230848.0,
        "disk_read_mb": 0.0, "disk_write_mb": 0.0,  # the pass-1 capture's disk counters
    }


def test_vllm_probe_and_dgx1a_doc(monkeypatch):
    probe, doc = _probe_and_doc(monkeypatch, VLLM_DGX1A, "vllm")
    r = rows.probe_to_rows(3, 4, probe, doc, T)
    assert _latency(r) == {
        "t": T, "endpoint_id": 3, "model_id": 4, "up": 1, "host": "up", "error": None,
        "context": 307712,  # dgx1-a_models_1 max_model_len
        "slots_total": None, "slots_busy": None,
        "requests_running": 1, "requests_waiting": 0,  # the /metrics document
        # the document's exact vllm:kv_cache_usage_perc, not the probe load's rounded 0.17
        # (story 2.2a ruling A: the document wins, the probe dict's copy is the fallback)
        "kv_cache_usage": 0.16963064295485641,
        "gen_tps": None, "since": None,
        "state": "busy",  # ruling A: no live.state in a vLLM document, so requests_running 1 > 0
    }
    assert r.counters == {
        "prompt_tokens": 17994028.0, "prompt_tokens_cached": None, "prompt_seconds": None,
        "generated_tokens": 293517.0, "generated_seconds": None,
        "spec_draft_tokens": 428005.0, "spec_accepted_tokens": 212613.0,
        "prefix_queries": 17994028.0, "prefix_hits": 17176832.0, "preemptions": 0.0,
    }
    assert r.hardware is None


def test_llamacpp_probe_takes_slots_from_the_probe_dict(monkeypatch):
    probe, doc = _probe_and_doc(monkeypatch, LLAMACPP, "llama.cpp")
    r = rows.probe_to_rows(3, 4, probe, doc, T)
    assert _latency(r) == {
        "t": T, "endpoint_id": 3, "model_id": 4, "up": 1, "host": "up", "error": None,
        "context": 262144,  # llamacpp_props_1 n_ctx (per slot)
        "slots_total": 2, "slots_busy": 0,  # llamacpp_slots_1: two slots, neither processing
        # llamacpp:requests_processing / :requests_deferred 0 in the capture (story 2.2a ruling D)
        "requests_running": 0.0, "requests_waiting": 0.0,
        "kv_cache_usage": None, "gen_tps": None, "since": None,
        "state": "idle",  # ruling A: two slots, neither busy, nothing running
    }
    assert r.counters == {
        "prompt_tokens": 4705.0, "prompt_tokens_cached": 18341.0, "prompt_seconds": 2.90835,
        "generated_tokens": 3822.0, "generated_seconds": 33.3306,
        "spec_draft_tokens": 3422.0, "spec_accepted_tokens": 2105.0,
        "prefix_queries": None, "prefix_hits": None, "preemptions": None,
    }
    assert r.hardware is None


def test_a_document_gauge_beats_the_probe_dicts_rounded_copy():
    """Ruling A, constructed: the probe dict carries a rounded occupancy and the parsed
    document an exact one; the row holds the document's number. Only when the document
    carries no such gauge does the probe dict's copy stand in."""
    probe = {"up": True, "host": "up", "models": [], "latency_ms": 1,
             "load": {"requests_running": 3, "requests_waiting": 2, "kv_cache_usage": 0.17}}
    doc = ("prometheus", "vllm:num_requests_running 1\nvllm:num_requests_waiting 0\n"
                          "vllm:kv_cache_usage_perc 0.16963064295485641\n")
    row = rows.probe_to_rows(1, None, probe, doc, T).probe
    assert (row["requests_running"], row["requests_waiting"], row["kv_cache_usage"]) == (
        1, 0, 0.16963064295485641)
    # no gauge in the document: the probe dict's copy is the only source left
    row = rows.probe_to_rows(1, None, probe, ("prometheus", "vllm:prompt_tokens_total 5\n"), T).probe
    assert (row["requests_running"], row["requests_waiting"], row["kv_cache_usage"]) == (3, 2, 0.17)


def test_an_up_probe_with_no_metrics_document_has_no_counters_row(monkeypatch):
    probe, _ = _probe_and_doc(monkeypatch, LLAMACPP, "llama.cpp")
    r = rows.probe_to_rows(3, None, probe, None, T)
    assert r.probe["up"] == 1 and r.probe["slots_total"] == 2 and r.probe["model_id"] is None
    assert r.counters is None and r.hardware is None


def test_a_down_probe_is_a_row_with_the_failure_sentence_and_no_counters(monkeypatch):
    probe, _ = _probe_and_doc(monkeypatch, {"/v1/models": None}, "strata")
    assert probe["up"] is False
    # a refusal: the box answers, nothing listens (classify_connect_failure reads ECONNREFUSED)
    sentence = ("10.0.0.60 is reachable but nothing is listening on 10.0.0.60:8080 "
                "(nothing started there, or the port in models.yaml is wrong)")
    assert probe["error"] == sentence
    for doc in (None, ("strata", {"totals": {"prompt_tokens": 5}})):  # a stale document is ignored
        r = rows.probe_to_rows(3, None, probe, doc, T)
        assert r.probe == {
            "t": T, "endpoint_id": 3, "model_id": None, "up": 0, "host": "up", "error": sentence,
            "latency_ms": None, "context": None, "slots_total": None, "slots_busy": None,
            "requests_running": None, "requests_waiting": None, "kv_cache_usage": None,
            "gen_tps": None, "since": None, "state": None,  # ruling A: NULL when down
        }
        assert r.counters is None and r.hardware is None


def test_a_down_probe_keeps_the_host_class_classify_connect_failure_gives():
    import errno

    import httpx

    try:
        try:
            raise OSError(errno.EHOSTUNREACH, "No route to host")
        except OSError as e:
            raise httpx.ConnectError("unreachable") from e
    except httpx.ConnectError as e:
        failure = classify_connect_failure(e, URL)
    r = rows.probe_to_rows(3, None, {"up": False, "models": [], **failure}, None, T)
    assert r.probe["host"] == "down" and r.probe["error"] == "10.0.0.60 is unreachable (EHOSTUNREACH)"


def test_context_of_a_multi_model_endpoint_is_the_first_served_ids():
    probe = {"up": True, "host": "up", "models": ["b", "a"], "latency_ms": 3, "context": {"a": 1000, "b": 2000}}
    assert rows.probe_to_rows(1, None, probe, None, T).probe["context"] == 2000
    probe["models"] = []  # no served id named: the first window the prober found
    assert rows.probe_to_rows(1, None, probe, None, T).probe["context"] == 1000


@pytest.mark.parametrize("routes, backend", [(STRATA_AI2, "strata"), (VLLM_DGX1A, "vllm"), (LLAMACPP, "llama.cpp")])
def test_insert_round_trips_through_sqlite_row(monkeypatch, tmp_path, routes, backend):
    """``rows.insert`` under the caller's transaction (the collector's ``tick``) round-trips:
    the probes row is exactly ``ProbeRows.probe`` and the children match the counters and
    hardware dicts, NULL columns included. The tick's all-or-nothing guarantee is
    tests/test_monitor_collect.py::test_a_tick_is_all_or_nothing (story 2.2a ruling E)."""
    probe, doc = _probe_and_doc(monkeypatch, routes, backend)
    conn = db.open_db(tmp_path / "m.db")
    eid = db.lookup_id(conn, "endpoints", name="ep", base_url=URL, backend=backend, host="local")
    mid = db.lookup_id(conn, "models", served_id=probe["models"][0])
    r = rows.probe_to_rows(eid, mid, probe, doc, T)
    with db.immediate(conn):
        pid = rows.insert(conn, r)
    got = dict(conn.execute("SELECT * FROM probes WHERE id = ?", (pid,)).fetchone())
    assert got == {"id": pid, **r.probe}
    c = conn.execute("SELECT * FROM probe_counters WHERE probe_id = ?", (pid,)).fetchone()
    assert dict(c) == {"probe_id": pid, **r.counters}
    h = conn.execute("SELECT * FROM probe_hardware WHERE probe_id = ?", (pid,)).fetchone()
    assert (dict(h) if h else None) == ({"probe_id": pid, **r.hardware} if r.hardware else None)
    assert not conn.in_transaction
    conn.close()


def _serve_dgx_without_the_kv_line(monkeypatch, base_url: str) -> None:
    """The dgx1-a captures with the ``vllm:kv_cache_usage_perc`` line deleted from /metrics
    (story 2.2b ruling B's construction, from the 2.2a review's probe 2): the document lacks
    the kv series, the other series stay. Served from the capture bytes via ``raw``."""
    text = raw("vllm/dgx1-a_metrics_1.txt").decode()
    no_kv = "\n".join(l for l in text.splitlines() if not l.startswith("vllm:kv_cache_usage_perc{"))
    routes = {"/v1/models": raw("vllm/dgx1-a_models_1.json"), "/metrics": no_kv.encode()}

    async def get(self, u, **kw):
        req = httpx.Request("GET", u)
        body = routes.get(u[len(base_url):])
        if body is None:
            return httpx.Response(404, content=b"not found", request=req)
        return httpx.Response(200, content=body, request=req)
    monkeypatch.setattr(httpx.AsyncClient, "get", get)


def test_a_vllm_doc_without_the_kv_series_stores_null_not_the_probers_zero(monkeypatch, tmp_path):
    """Story 2.2b ruling B, over the real prober and a real ``collect.tick``: the dgx1-a
    capture without its kv line. The prober's ``load`` copy carries only the series the
    document has (no 0.0 default for the absent kv), and the row's ``kv_cache_usage`` stays
    NULL — ``rows.probe_to_rows``'s load fallback now means the document AND the copy lack
    the series, which is the schema's absent-is-NULL rule, not a stored zero.
    running/waiting still come from the document."""
    url = "http://10.0.0.70:8000"
    _serve_dgx_without_the_kv_line(monkeypatch, url)
    reg = Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={})
    ep = Endpoint(name="dgx1-a", base_url=url, backend="vllm")
    probe = asyncio.run(reg.probe(ep))
    assert probe["load"] == {"requests_running": 1, "requests_waiting": 0}  # the document's, kv absent
    conn = db.open_db(tmp_path / "m.db")
    eid = collect.endpoint_id(conn, ep)
    pid = asyncio.run(collect.tick(reg, conn, ep, eid, T, "vllm"))
    got = tuple(conn.execute(
        "SELECT kv_cache_usage, requests_running, requests_waiting FROM probes WHERE id = ?",
        (pid,)).fetchone())
    assert got == (None, 1.0, 0.0)
    conn.close()
