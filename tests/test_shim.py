import json

import pytest

from localagents.shim import normalise_messages_body, translate_context_error

LLAMA_ERR = (
    b'{"error":{"code":400,"message":"request (327691 tokens) exceeds the available context size '
    b'(262144 tokens), try increasing it","type":"exceed_context_size_error",'
    b'"n_prompt_tokens":327691,"n_ctx":262144}}'
)


def test_translate_llamacpp_error():
    out = translate_context_error(400, LLAMA_ERR)
    assert out is not None
    body, original = out
    doc = json.loads(body)
    assert doc["type"] == "error"
    assert doc["error"]["type"] == "invalid_request_error"
    assert doc["error"]["message"] == "prompt is too long: 327691 tokens > 262144 maximum"
    assert original.startswith("request (327691 tokens)")


def test_translate_llamacpp_without_numeric_fields_uses_message():
    err = {"error": {"message": "request (5000 tokens) exceeds the available context size (4096 tokens), try increasing it",
                     "type": "exceed_context_size_error"}}
    body, _ = translate_context_error(400, json.dumps(err).encode())
    assert json.loads(body)["error"]["message"] == "prompt is too long: 5000 tokens > 4096 maximum"


def test_translate_vllm_style_error():
    err = {"object": "error", "type": "BadRequestError", "code": 400,
           "message": "This model's maximum context length is 32768 tokens. However, you requested 40000 tokens "
                      "(39000 in the messages, 1000 in the completion). Please reduce the length of the messages or completion."}
    body, _ = translate_context_error(400, json.dumps(err).encode())
    assert json.loads(body)["error"]["message"] == "prompt is too long: 40000 tokens > 32768 maximum"


def test_translate_strata_error():
    # captured from strata 0.1.44.1 on ai1 (2026-10-16); it rejects prompt + max_tokens > window with a 400
    err = {"error": {"type": "invalid_request_error",
                     "message": "request exceeds the context window: prompt (150053 tokens) + max tokens (16) exceeds the "
                                "context (131072); requests are never truncated. Send a smaller max_tokens (at most 0 here), "
                                "or add \"fit_max_tokens\": true to the model's strata-<model>.json to shorten it to the room left (#545)"}}
    body, original = translate_context_error(400, json.dumps(err).encode())
    assert json.loads(body)["error"]["message"] == "prompt is too long: 150053 tokens > 131072 maximum"
    assert original.startswith("request exceeds the context window")


def test_translate_leaves_other_errors_alone():
    assert translate_context_error(400, b'{"error":{"message":"bad json","type":"invalid_request_error"}}') is None
    assert translate_context_error(500, b'{"error":{"type":"internal_error","message":"CUDA out of memory"}}') is None
    assert translate_context_error(400, b"not json") is None


def test_fold_system_messages_in_place():
    body = {
        "system": "sys",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
            {"role": "system", "content": "reminder"},
            {"role": "user", "content": [{"type": "text", "text": "next"}]},
        ],
    }
    out = normalise_messages_body(body)
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert out["messages"][0]["content"][-1]["text"] == "<system>\nreminder\n</system>"
    assert out["system"] == "sys"
    assert out["_localagents"]["moved_system_messages"] == 1


def test_fold_leading_system_goes_to_next_user():
    body = {"messages": [{"role": "system", "content": "lead"}, {"role": "user", "content": "q"}]}
    out = normalise_messages_body(body)
    assert out["messages"][0]["content"][0]["text"] == "<system>\nlead\n</system>"
    assert out["messages"][0]["content"][1]["text"] == "q"


def test_fold_text_into_last_tool_result():
    body = {"messages": [
        {"role": "user", "content": "read both"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Read", "input": {}},
                                          {"type": "tool_use", "id": "b", "name": "Read", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "file a"},
            {"type": "tool_result", "tool_use_id": "b", "content": [{"type": "text", "text": "file b"}]},
            {"type": "text", "text": "<system-reminder>\nremember\n</system-reminder>"},
        ]},
    ]}
    out = normalise_messages_body(body)
    blocks = out["messages"][2]["content"]
    assert [b["type"] for b in blocks] == ["tool_result", "tool_result"]
    assert blocks[0]["content"] == "file a"
    assert blocks[1]["content"] == [{"type": "text", "text": "file b"},
                                    {"type": "text", "text": "<system-reminder>\nremember\n</system-reminder>"}]
    assert out["_localagents"]["folded_tool_result_text"] == 1


def test_fold_system_reminder_lands_inside_tool_result():
    body = {"messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]},
        {"role": "system", "content": "budget: 90% left"},
    ]}
    out = normalise_messages_body(body)
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "user"]
    blocks = out["messages"][2]["content"]
    assert len(blocks) == 1 and blocks[0]["type"] == "tool_result"
    assert blocks[0]["content"] == [{"type": "text", "text": "ok"},
                                    {"type": "text", "text": "<system>\nbudget: 90% left\n</system>"}]
    assert out["_localagents"] == {"moved_system_messages": 1, "folded_tool_result_text": 1}


def test_fold_leaves_plain_and_result_only_messages_alone():
    body = {"messages": [
        {"role": "user", "content": [{"type": "text", "text": "q"}, {"type": "image", "source": {}}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]},
    ]}
    before = json.dumps(body)
    out = normalise_messages_body(body)
    assert json.dumps(out) == before
    assert "_localagents" not in out


def test_metrics_delta():
    from localagents.registry import metrics_delta
    before = {"prompt_tokens": 100, "prompt_tokens_cached": 50, "prompt_seconds": 1.0,
              "generated_tokens": 10, "generated_seconds": 1.0, "spec_draft_tokens": 0, "spec_accepted_tokens": 0}
    after = {"prompt_tokens": 1100, "prompt_tokens_cached": 4050, "prompt_seconds": 3.0,
             "generated_tokens": 210, "generated_seconds": 3.0, "spec_draft_tokens": 100, "spec_accepted_tokens": 70}
    d = metrics_delta(before, after)
    assert d == {"prompt_tokens_processed": 1000, "prompt_tokens_cached": 4000, "generated_tokens": 200,
                 "cache_hit_ratio": 0.8, "prompt_tps": 500.0, "generate_tps": 100.0, "spec_decode_acceptance": 0.7}
    assert metrics_delta(None, after) is None


def test_metrics_delta_branches_on_the_backend_not_on_a_key():
    """A vLLM /metrics without the prefix-cache series must NOT be computed as llama.cpp's
    cached/(processed+cached): vLLM's ``prompt_tokens`` already includes cached tokens, so the
    processed count is the prompt delta itself and the cache keys are omitted rather than
    guessed. With ``backend=None`` the old key sniff stays for callers that have no backend
    (review of 1.8)."""
    from localagents.registry import metrics_delta

    before = {"prompt_tokens": 100.0, "generated_tokens": 10.0}
    after = {"prompt_tokens": 600.0, "generated_tokens": 90.0}
    d = metrics_delta(before, after, backend="vllm")
    assert d["prompt_tokens_processed"] == 500 and d["generated_tokens"] == 80
    assert "cache_hit_ratio" not in d and "prompt_tokens_cached" not in d
    # the sniff is unchanged for backend-less callers: no prefix keys -> llama.cpp arithmetic,
    # and with no cached counter in the snapshots there is no cached key or ratio to report
    # (item 2 of the 1.8 review fold: an unknown is omitted, never a 0)
    assert metrics_delta(before, after) == {"generated_tokens": 80, "prompt_tokens_processed": 500}
    # backend=vllm with the prefix series present is still the vLLM arithmetic
    b2 = {"prompt_tokens": 0.0, "prefix_hits": 0.0, "prefix_queries": 0.0}
    a2 = {"prompt_tokens": 1000.0, "prefix_hits": 450.0, "prefix_queries": 900.0}
    assert metrics_delta(b2, a2, backend="vllm") == {
        "prompt_tokens_processed": 550, "prompt_tokens_cached": 450,
        "cache_hit_ratio": 0.5,
    }


def test_metrics_delta_diffs_only_counters_present_in_both_snapshots():
    """A counter that vanished from ``after`` (a backend release dropping a series) is not a
    reset: the reset rule must fire only when a counter truly went backwards, so the diff runs
    over the intersection, and a counter that only appears in ``after`` is not a whole
    since-start delta either (review of 1.8)."""
    from localagents.registry import metrics_delta

    before = {"generated_tokens": 100.0, "prompt_tokens": 1000.0, "prompt_tokens_cached": 500.0,
              "spec_draft_tokens": 90.0, "spec_accepted_tokens": 60.0}
    after = {"generated_tokens": 300.0, "prompt_tokens": 4000.0, "prompt_tokens_cached": 900.0}
    d = metrics_delta(before, after, backend="llamacpp")
    assert "spec_decode_acceptance" not in d, d  # the vanished spec counters are simply absent
    assert not any(v < 0 for v in d.values()), d  # and nothing reads as a reset
    assert d["generated_tokens"] == 200
    assert d["prompt_tokens_processed"] == 3000 and d["prompt_tokens_cached"] == 400

    # only in `after`: the whole since-start seconds must not count as the span's (old union)
    d2 = metrics_delta({"generated_tokens": 0.0}, {"generated_tokens": 50.0, "prompt_seconds": 100.0},
                       backend="llamacpp")
    assert "prompt_tps" not in d2, d2


@pytest.mark.parametrize("backend, before, after, absent", [
    # generated_seconds in both, generated_tokens in `before` only: was KeyError at the tps
    ("llamacpp", {"generated_tokens": 10.0, "generated_seconds": 1.0, "prompt_tokens": 5.0},
     {"generated_seconds": 3.0, "prompt_tokens": 9.0}, ("generated_tokens", "generate_tps")),
    # spec_draft_tokens in both, spec_accepted_tokens in one: was KeyError on every branch
    ("llamacpp", {"spec_draft_tokens": 10.0, "spec_accepted_tokens": 5.0, "generated_tokens": 1.0},
     {"spec_draft_tokens": 30.0, "generated_tokens": 2.0}, ("spec_decode_acceptance",)),
    ("vllm", {"spec_draft_tokens": 10.0, "spec_accepted_tokens": 5.0, "generated_tokens": 1.0},
     {"spec_draft_tokens": 30.0, "generated_tokens": 2.0}, ("spec_decode_acceptance",)),
    ("vllm", {"spec_draft_tokens": 10.0, "spec_accepted_tokens": 5.0, "prompt_tokens": 1.0,
              "prefix_queries": 1.0, "prefix_hits": 0.0},
     {"spec_draft_tokens": 30.0, "prompt_tokens": 9.0, "prefix_queries": 9.0, "prefix_hits": 4.0},
     ("spec_decode_acceptance", "generated_tokens")),
    # prompt_seconds in both, prompt_tokens in `after` only: was a 0 processed and a 0.0 tps
    ("llamacpp", {"prompt_seconds": 1.0, "prompt_tokens_cached": 0.0},
     {"prompt_seconds": 2.0, "prompt_tokens": 100.0, "prompt_tokens_cached": 50.0},
     ("prompt_tokens_processed", "prompt_tps", "cache_hit_ratio", "generated_tokens")),
    # the cached counter in one snapshot only: no cached key, no ratio built from a guessed 0
    ("llamacpp", {"prompt_tokens": 1.0, "prompt_tokens_cached": 1.0}, {"prompt_tokens": 5.0},
     ("prompt_tokens_cached", "cache_hit_ratio")),
])
def test_metrics_delta_never_reads_a_partner_it_did_not_guard(backend, before, after, absent):
    """Every counter metrics_delta reads must be in both snapshots: one that is not yields no
    output key, never a KeyError (which reached run_job's finally and stranded the job) and
    never a 0 standing in for an unknown (review of 1.8, construct-it)."""
    from localagents.registry import metrics_delta

    d = metrics_delta(before, after, backend=backend)
    assert d is not None
    for k in absent:
        assert k not in d, (k, d)


def _vllm_text(pt, gen, pq=None, ph=None):
    s = f'vllm:prompt_tokens_total{{model_name="m"}} {pt}\nvllm:generation_tokens_total{{model_name="m"}} {gen}\n'
    if pq is not None:
        s += f'vllm:prefix_cache_queries_total{{model_name="m"}} {pq}\nvllm:prefix_cache_hits_total{{model_name="m"}} {ph}\n'
    return s


@pytest.mark.parametrize("backend", ["other", "ollama", None])
def test_metrics_delta_sniffs_vllm_for_a_backend_that_names_no_format(backend):
    """`backend: other` is the Endpoint and register_endpoint default, and the prober finds a
    vLLM by max_model_len whatever the backend says: such an endpoint with the prefix-cache
    series gets vLLM's arithmetic, as it did before 1.8 (review of 1.8, finding 1: it had become
    processed 8000, cached 0, ratio 0.0)."""
    from localagents.registry import metrics_delta, parse_metrics

    b = parse_metrics(_vllm_text(1000, 100, 1000, 200))
    a = parse_metrics(_vllm_text(9000, 700, 9000, 5000))
    assert metrics_delta(b, a, backend=backend) == {
        "generated_tokens": 600, "prompt_tokens_processed": 3200, "prompt_tokens_cached": 4800,
        "cache_hit_ratio": 0.6,
    }


@pytest.mark.parametrize("backend", ["strata", "llamacpp"])
def test_metrics_delta_never_sniffs_for_a_named_counter_format(backend):
    """strata and llama.cpp name their format: a prefix_queries key (injected here, in both
    snapshots) never moves them onto the vLLM arithmetic."""
    import copy
    import pathlib

    from localagents.registry import metrics_delta, parse_strata_metrics

    doc = json.loads((pathlib.Path(__file__).parent / "fixtures" / "strata_metrics.json").read_text())
    doc2 = copy.deepcopy(doc)
    t = doc2["totals"]
    t.update(prompt_tokens=t["prompt_tokens"] + 30000, reused=t["reused"] + 25000,
             output_tokens=t["output_tokens"] + 800, prompt_ms=t["prompt_ms"] + 2500.0,
             decode_ms=t["decode_ms"] + 8000.0, drafts_offered=t["drafts_offered"] + 400,
             drafts_accepted=t["drafts_accepted"] + 300)
    b, a = parse_strata_metrics(doc), parse_strata_metrics(doc2)
    b.update(prefix_queries=0.0, prefix_hits=0.0)
    a.update(prefix_queries=30000.0, prefix_hits=1.0)
    assert metrics_delta(b, a, backend=backend) == {
        "generated_tokens": 800, "prompt_tokens_processed": 5000, "prompt_tokens_cached": 25000,
        "cache_hit_ratio": 0.833, "prompt_tps": 2000.0, "generate_tps": 100.0, "spec_decode_acceptance": 0.75,
    }


@pytest.mark.parametrize("backend", ["vllm", "other", None])
def test_metrics_delta_prefix_series_in_one_snapshot_only_is_no_prefix(backend):
    """The prefix-cache branch needs the series in BOTH snapshots: present in `after` only it
    used to take that branch and report prompt_tokens_cached 0 (a guess); now it is the
    no-prefix arithmetic with cached and ratio omitted (review of 1.8, finding 3). The same
    the other way round (present in `before` only)."""
    from localagents.registry import metrics_delta, parse_metrics

    b = parse_metrics(_vllm_text(1000, 100))
    a = parse_metrics(_vllm_text(9000, 700, 9000, 5000))
    assert metrics_delta(b, a, backend=backend) == {"generated_tokens": 600, "prompt_tokens_processed": 8000}
    assert metrics_delta(a, parse_metrics(_vllm_text(9500, 800)), backend=backend) == {
        "generated_tokens": 100, "prompt_tokens_processed": 500,
    }


def test_fuzzy_model_match():
    from localagents.registry import Registry
    ids = ["unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL", "DeepSeek-V4-Flash"]
    assert Registry.match("qwen3.8-27b", ids) == ids[0]  # fuzzy
    assert Registry.match("deepseek-v4-flash", ids) == ids[1]
    assert Registry.match("qwen3-coder", ids) is None
    assert Registry.match("*Qwen3.8*", ids) == ids[0]  # glob
    assert Registry.match("DeepSeek-V4-Flash", ids) == ids[1]  # exact
    assert Registry.match("*V4*", ids) == ids[1]


def test_registry_ignores_legacy_models_section(tmp_path):
    from localagents.registry import Registry
    cfg = tmp_path / "models.yaml"
    cfg.write_text("endpoints:\n  a:\n    base_url: http://127.0.0.1:1\nmodels:\n  old:\n    notes: gone\n")
    reg = Registry.load(cfg)
    assert list(reg.endpoints) == ["a"]
    assert reg.defaults.model is None


VLLM_ERR = (b'{"type":"error","error":{"type":"internal_error","message":"This model\'s maximum context length is '
            b'937472 tokens. However, you requested 16 output tokens and your prompt contains at least 937457 input '
            b'tokens, for a total of at least 937473 tokens. Please reduce the length of the input prompt or the '
            b'number of requested output tokens. (parameter=input_tokens, value=937457)"}}')


def test_translate_vllm_anthropic_endpoint_500():
    body, original = translate_context_error(500, VLLM_ERR)
    assert json.loads(body)["error"]["message"] == "prompt is too long: 937473 tokens > 937472 maximum"
    assert original.startswith("This model's maximum context length")


def test_parse_metrics_sums_labelled_series():
    from localagents.registry import parse_metrics, metrics_delta
    text = """# HELP x
vllm:prompt_tokens_total{engine="0",model_name="a"} 1000
vllm:prompt_tokens_total{engine="1",model_name="a"} 200
vllm:generation_tokens_total{engine="0",model_name="a"} 50
vllm:prefix_cache_queries_total{engine="0",model_name="a"} 900
vllm:prefix_cache_hits_total{engine="0",model_name="a"} 450
vllm:num_requests_running{engine="0",model_name="a"} 1
llamacpp:prompt_tokens_total 5
"""
    m = parse_metrics(text)
    assert m["prompt_tokens"] == 1205 and m["generated_tokens"] == 50 and m["requests_running"] == 1
    zero = {k: 0.0 for k in m}
    d = metrics_delta(zero, m)
    assert d["prompt_tokens_cached"] == 450 and d["prompt_tokens_processed"] == 755 and d["cache_hit_ratio"] == 0.5


def test_count_tokens_fallback_records_the_backends_404(tmp_path, monkeypatch):
    """Story 1.9 item 8: the count_tokens fallback logged status=200 although the backend
    answered 404 — a request row that claimed a status the backend never sent. The row records
    http_status=404 with the estimate's error note; the shim's reply to Claude Code stays 200
    with the estimate (that reply is the shim's own contract, unchanged)."""
    import asyncio

    import httpx
    from starlette.requests import Request

    from localagents import jobs
    from localagents.shim import Shim

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    job = jobs.JobStore().new(
        task="t", model="m", endpoint="e", base_url="http://x:8080", backend="llama.cpp", host="local",
        cwd="/tmp", run_cwd="/tmp", isolation="none",
    )
    shim = Shim(lambda jid: job)

    async def not_found(upstream, stream=True):
        return httpx.Response(404, request=upstream)  # the backend has no /v1/messages/count_tokens

    monkeypatch.setattr(shim._client, "send", not_found)

    raw = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hello"}]}).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {
        "type": "http", "method": "POST", "headers": [(b"content-type", b"application/json")],
        "path": f"/job/{job.id}/v1/messages/count_tokens", "query_string": b"", "root_path": "",
        "path_params": {"job_id": job.id, "path": "v1/messages/count_tokens"},
    }

    resp = asyncio.run(shim.relay(Request(scope, receive)))
    assert resp.status_code == 200  # the reply to Claude Code: the estimate, as before
    assert json.loads(resp.body) == {"input_tokens": max(1, len(raw) // 4)}

    row = job._conn.execute(
        "SELECT r.*, p.path AS path_name FROM requests r JOIN paths p ON p.id = r.path_id"
    ).fetchone()
    assert row["path_name"] == "/v1/messages/count_tokens"
    assert row["http_status"] == 404  # what the backend actually answered
    assert row["error"] == "count_tokens unsupported; estimated"


def test_stream_usage_merges_message_start_and_message_delta():
    from localagents.shim import _merge_usage, _usage_from_sse_head, _usage_from_sse_tail

    head = (b'event: message_start\ndata: {"type":"message_start","message":{"id":"m","usage":'
            b'{"input_tokens":1200,"cache_read_input_tokens":38000,"cache_creation_input_tokens":0,"output_tokens":1}}}\n\n'
            b'event: content_block_start\ndata: {"type":"content_block_start","index":0}\n\n')
    tail = (b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":123}}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    merged = _merge_usage(_usage_from_sse_head(head), _usage_from_sse_tail(tail))
    assert merged == {"input_tokens": 1200, "cache_read_input_tokens": 38000, "cache_creation_input_tokens": 0, "output_tokens": 123}
    assert _merge_usage(None, {"output_tokens": 5}) == {"output_tokens": 5}
    assert _merge_usage(None, None) is None
