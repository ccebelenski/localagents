import json

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
