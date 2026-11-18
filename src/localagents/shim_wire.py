"""What the shim's two API versions share about the wire (Epic 6.6): the SSE capture bounds, the
object guard for a backend's ``usage``, and the detection of a backend's context-overflow error.
No API's envelope lives here: shim_anthropic.py rewrites an overflow into Anthropic's, and
shim_openai.py passes it through as the backend sent it."""

from __future__ import annotations

import json
import re
from typing import Any

# SSE usage capture bounds (3.10h fold): message_start arrives first (input and cache
# counts) and is read from the head of the stream; message_delta arrives near the end
# (the final output count) and is read from the stream's tail — the last 8 KiB of its
# bytes, not its last read, so a message_stop delivered as its own read no longer hides
# the delta, and a delta split across two reads is still seen whole. The OpenAI version reads
# its final usage chunk from the same tail.
HEAD_BYTES = 65536
TAIL_BYTES = 8192


def _object(v: Any) -> dict[str, Any] | None:
    """The backend's ``usage`` when it is a JSON object, else None: every reader below hands
    back an object or nothing, so a backend that sends a string or an array there can neither
    fail the reply nor lose the row (3.11a row 4)."""
    return v if isinstance(v, dict) else None


_CTX_PATTERNS = (
    # llama.cpp: "request (327691 tokens) exceeds the available context size (262144 tokens), ..."
    re.compile(r"request \((?P<actual>\d+) tokens\) exceeds the available context size \((?P<limit>\d+) tokens\)", re.I),
    # vLLM (Anthropic endpoint, seen as HTTP 500): "This model's maximum context length is 937472 tokens. However,
    # you requested 16 output tokens and your prompt contains at least 937457 input tokens, for a total of at least 937473 tokens."
    re.compile(r"maximum context length is (?P<limit>\d+) tokens.*?total of at least (?P<actual>\d+) tokens", re.I | re.S),
    # vLLM / OpenAI-style chat endpoint: "... However, you requested 40000 tokens (39000 in the messages, ...)"
    re.compile(r"maximum context length is (?P<limit>\d+) tokens.*?requested (?P<actual>\d+) tokens", re.I | re.S),
    # strata (llama.cpp-compatible server; counts max_tokens against the window instead of clamping it):
    # "request exceeds the context window: prompt (150053 tokens) + max tokens (16) exceeds the context (131072); ..."
    re.compile(r"prompt \((?P<actual>\d+) tokens\) \+ max tokens \(\d+\) exceeds the context \((?P<limit>\d+)\)", re.I),
    # strata, the current wording (Epic 6.6, captured 2026-11-17, tests/fixtures/opencode/wire/
    # strata-overflow-400.txt): "request exceeds the context window: prompt (390295 tokens) leaves
    # no room to answer in the context (131072); requests are never truncated"
    re.compile(r"prompt \((?P<actual>\d+) tokens\) leaves no room to answer in the context \((?P<limit>\d+)\)", re.I),
)
_CTX_HINTS = (
    "exceeds the available context", "maximum context length", "exceed context limit", "context length exceeded",
    "exceeds the context window",
)


def context_overflow(status: int, data: bytes) -> tuple[str, int | None, int | None] | None:
    """A backend's context-overflow error: ``(its message, prompt tokens, the window)``, the two
    numbers when the backend states them, or None when the error is something else. vLLM reports
    the condition as HTTP 500 ``internal_error``, so any 4xx/5xx with a matching message
    qualifies."""
    if status < 400:
        return None
    try:
        doc = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    err = doc.get("error") if isinstance(doc, dict) else None
    err = err if isinstance(err, dict) else doc if isinstance(doc, dict) else {}
    msg = str(err.get("message") or "")
    etype = str(err.get("type") or "")
    if etype != "exceed_context_size_error" and not any(h in msg.lower() for h in _CTX_HINTS):
        return None
    actual, limit = err.get("n_prompt_tokens"), err.get("n_ctx")  # llama.cpp includes these
    if not (isinstance(actual, int) and isinstance(limit, int)):
        actual = limit = None
        for pat in _CTX_PATTERNS:
            m = pat.search(msg)
            if m:
                actual, limit = int(m.group("actual")), int(m.group("limit"))
                break
    return msg, actual, limit
