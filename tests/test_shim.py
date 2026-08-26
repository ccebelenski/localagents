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
    assert translate_context_error(500, LLAMA_ERR) is None
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
    from localagents.registry import ModelSpec, Registry
    reg = Registry.load()
    ids = ["unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL", "DeepSeek-V4-Flash"]
    assert reg.match(ModelSpec(name="qwen3.8-27b"), ids) == ids[0]
    assert reg.match(ModelSpec(name="deepseek-v4-flash"), ids) == ids[1]
    assert reg.match(ModelSpec(name="qwen3-coder"), ids) is None
    assert reg.match(ModelSpec(name="*Qwen3.8*"), ids) == ids[0]  # glob as name
    assert reg.match(ModelSpec(name="x", served_name="DeepSeek-V4-Flash"), ids) == ids[1]  # explicit override
    assert reg.match(ModelSpec(name="x", served_name="*V4*"), ids) == ids[1]
