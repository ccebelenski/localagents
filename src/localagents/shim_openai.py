"""The shim's OpenAI version (Epic 6.6): ``/v1/chat/completions`` as an OpenAI-compatible client
(OpenCode, Epic 6.7) sends it, relayed to the backend's own OpenAI endpoint.

No normalisation: the request goes as the client wrote it. A context-overflow error goes back as
the backend sent it — OpenCode compacts and retries on llama.cpp's and Strata's own 400s (the 6.2
captures, events/overflow-*.jsonl), so a rewrite would only hide the backend's words; the
requests row still notes the overflow (the core's ``context_exceeded``).

Usage, from the stream's last chunk that carries a ``usage`` object (Strata puts it on the
``finish_reason`` chunk, llama.cpp on a trailing chunk with empty ``choices``: the 6.2 wire
captures) or a non-streamed body's ``usage``, is mapped onto the requests row's Anthropic-named
keys so every reader sums it the same way: both backends count the cached prompt inside
``prompt_tokens`` and state it as ``prompt_tokens_details.cached_tokens`` (equal to llama.cpp's
``timings.cache_n`` in the captures), so ``input_tokens`` is the uncached part and
``cache_read_input_tokens`` the cached one; their sum is the prompt.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .shim_wire import _object

NAME = "openai"

# a delta that carries generation: non-empty text, reasoning, or a tool call (the first chunk's
# `"content": ""` / `null` role announcement is not generation)
_GENERATED = re.compile(rb'"(?:content|reasoning_content|reasoning)"\s*:\s*"[^"]|"tool_calls"\s*:\s*\[')


def matches(path: str) -> bool:
    return path.endswith("/v1/chat/completions")


def answer_locally(path: str) -> dict[str, Any] | None:
    return None


def prepare(path: str, body: dict[str, Any]) -> dict[str, Any]:
    return body


def count_tokens_estimate(path: str, status: int, raw: bytes) -> int | None:
    return None


def content_started(chunk: bytes) -> bool:
    return _GENERATED.search(chunk) is not None


def row_usage(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """An OpenAI ``usage`` object as the requests row's keys (None when there is none)."""
    if not usage:
        return None
    prompt = usage.get("prompt_tokens")
    details = _object(usage.get("prompt_tokens_details")) or {}
    cached = details.get("cached_tokens")
    out: dict[str, Any] = {"output_tokens": usage.get("completion_tokens")}
    if isinstance(prompt, int):
        cached_n = cached if isinstance(cached, int) and 0 <= cached <= prompt else 0
        out["input_tokens"] = prompt - cached_n
        out["cache_read_input_tokens"] = cached_n
    return out


def usage_from_stream(head: bytes, tail: bytes) -> dict[str, Any] | None:
    """The final usage: the LAST ``data:`` chunk in the stream's tail whose ``usage`` is an
    object (the head is not read: OpenAI streams state usage once, at the end)."""
    usage: dict[str, Any] | None = None
    for line in tail.decode(errors="replace").splitlines():
        if line.startswith("data:") and '"usage"' in line:
            try:
                parsed = _object(json.loads(line[5:]).get("usage"))
            except Exception:  # noqa: BLE001
                continue
            if parsed is not None:
                usage = parsed
    return row_usage(usage)


def usage_from_json(data: bytes) -> dict[str, Any] | None:
    try:
        return row_usage(_object(json.loads(data).get("usage")))
    except Exception:  # noqa: BLE001
        return None


def error_reply(status: int, data: bytes) -> bytes | None:
    return None
