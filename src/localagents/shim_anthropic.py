"""The shim's Anthropic version (Epic 6.6): ``/v1/messages`` as Claude Code sends it.

Claude Code targets Anthropic's API and uses features local servers do not all support. The
relay core (shim.py) forwards the request and records it; this module is what is Anthropic's:

* ``role: system`` entries inside ``messages`` (Claude Code's mid-conversation system turns:
  skills listing, token budget, reminders) are folded in place into the adjacent user message —
  chat templates such as Qwen's raise "System message must be at the beginning" otherwise, and
  folding in place (rather than hoisting to ``system``) keeps the KV-cache prefix stable.
* A user message that mixes ``tool_result`` blocks with ``text`` blocks (Claude Code appends a
  system-reminder to the tool-result turn, and the fold above adds one more) has the text moved
  inside the last ``tool_result``'s content. Gemma 4's chat template only renders tool responses
  that are adjacent to the assistant's tool call; the mixed message breaks that adjacency behind
  llama.cpp's ``/v1/messages`` converter and the model never sees any of its tool output.
* A tool result's ``tool_reference`` items (ToolSearch's answer) become text.
* ``/v1/messages/count_tokens`` falls back to a char/4 estimate if the backend lacks it, and
  Claude Code's ``/api/hello`` warm-up is answered without reaching the backend.
* Backend "context exceeded" errors (shim_wire.context_overflow) are rewritten into Anthropic's
  ``prompt is too long: N tokens > M maximum`` envelope, which Claude Code recognises and answers
  by compacting the conversation. This is the backstop; the primary guard is
  ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` set from the probed window (see
  ``harnesses.claude_code.build_env``) so auto-compact runs *before* the backend overflows.
* Usage is read from the SSE stream's ``message_start`` (prompt side) and ``message_delta``
  (output side) events, in Anthropic's own key names, which the requests row uses.
"""

from __future__ import annotations

import json
from typing import Any

from .shim_wire import _object, context_overflow

NAME = "anthropic"


def matches(path: str) -> bool:
    return path.endswith("/v1/messages") or path.endswith("/v1/messages/count_tokens") or path == "/api/hello"


def answer_locally(path: str) -> dict[str, Any] | None:
    """Claude Code opens every job with /api/hello: HEAD, and a GET, as a connection warm-up
    whose reply it discards (story 3.7t). Forwarded, it recorded the backend's 404/501 as a row
    the page painted as an error. The requests table records backend requests the shim
    forwarded, so this one is answered here, not sent, not logged."""
    return {} if path == "/api/hello" else None


def prepare(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """The request as the backend gets it, with the shim's note under ``_localagents``."""
    return normalise_messages_body(body) if path.endswith("/v1/messages") else body


def count_tokens_estimate(path: str, status: int, raw: bytes) -> int | None:
    """"this backend has no count_tokens" is not one status: llama.cpp answers 404, vLLM 405, a
    gateway may say 501 (review of 3.5, F4). Any other status — a 500 is a real failure —
    forwards untouched: hiding a failure behind a plausible token count is worse than the
    failure. None: not this case."""
    if path.endswith("/count_tokens") and status in (404, 405, 501):
        return max(1, len(raw) // 4)
    return None


def content_started(chunk: bytes) -> bool:
    """The first chunk carrying generated content (not the first byte: message_start precedes
    prompt processing on llama.cpp, Strata and vLLM)."""
    return b"content_block_start" in chunk or b"content_block_delta" in chunk


def usage_from_stream(head: bytes, tail: bytes) -> dict[str, Any] | None:
    return _merge_usage(_usage_from_sse_head(head), _usage_from_sse_tail(tail))


def usage_from_json(data: bytes) -> dict[str, Any] | None:
    return _usage_from_json(data)


def error_reply(status: int, data: bytes) -> bytes | None:
    """The client's reply for a backend error, or None to pass the backend's through."""
    t = translate_context_error(status, data)
    return t[0] if t else None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        b.get("text", "") for b in (content or []) if isinstance(b, dict) and b.get("type") == "text"
    )


def _fold_text_into_tool_results(msgs: list[dict[str, Any]]) -> int:
    """Move ``text`` blocks that share a user message with ``tool_result`` blocks into the last result.

    Returns the number of text blocks moved. Other block types (images) stay where they are.
    """
    folded = 0
    for m in msgs:
        content = m.get("content")
        if m.get("role") != "user" or not isinstance(content, list):
            continue
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        texts = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
        if not results or not texts:
            continue
        last = results[-1]
        inner = last.get("content")
        if isinstance(inner, str):
            inner = [{"type": "text", "text": inner}] if inner else []
        elif not isinstance(inner, list):
            inner = []
        last["content"] = list(inner) + [{"type": "text", "text": t.get("text", "")} for t in texts]
        m["content"] = [b for b in content if not (isinstance(b, dict) and b.get("type") == "text")]
        folded += len(texts)
    return folded


def _text_tool_references(msgs: list[dict[str, Any]]) -> int:
    """Rewrite each ``tool_reference`` item inside a ``tool_result``'s content as a text item
    naming the tool. Claude Code's ToolSearch answers with them (``[{"type": "tool_reference",
    "tool_name": "TaskStop"}]``: the deferred tool it loaded); a local backend's chat template
    accepts only text and images there, and llama.cpp's Qwen template raised "Unexpected item type
    in content" (HTTP 500) and ended the 6.1 review job on ai9 (job 725b4456, 2026-11-17). The
    tool itself reaches the session as a tool definition on the next request either way. Returns
    the number rewritten."""
    n = 0
    for m in msgs:
        content = m.get("content")
        if m.get("role") != "user" or not isinstance(content, list):
            continue
        for b in content:
            if not (isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), list)):
                continue
            items = b["content"]
            for i, item in enumerate(items):
                if isinstance(item, dict) and item.get("type") == "tool_reference":
                    items[i] = {"type": "text", "text": f"tool_reference: {item.get('tool_name', '?')}"}
                    n += 1
    return n


def normalise_messages_body(body: dict[str, Any]) -> dict[str, Any]:
    """Fold ``role: system`` entries in ``messages`` into an adjacent user message, then fold
    text blocks that share a user message with tool results into the last tool result. A tool
    result's ``tool_reference`` items become text first (_text_tool_references).

    System text keeps its position (appended to the nearest preceding user message, or
    prepended to the next one) rather than being hoisted into the top-level ``system`` field, so
    the prompt stays append-only across turns and the backend's KV-cache prefix is not
    invalidated on every request.
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return body
    refs = _text_tool_references(msgs)
    if refs:
        body.setdefault("_localagents", {})["tool_references_as_text"] = refs
    if not any(m.get("role") == "system" for m in msgs):
        folded = _fold_text_into_tool_results(msgs)
        if folded:
            body.setdefault("_localagents", {})["folded_tool_result_text"] = folded
        return body
    out: list[dict[str, Any]] = []
    pending: list[str] = []  # system text with no preceding user message yet
    moved = 0
    for m in msgs:
        role = m.get("role")
        if role == "system":
            text = _text_of(m.get("content")).strip()
            if not text:
                continue
            moved += 1
            block = {"type": "text", "text": f"<system>\n{text}\n</system>"}
            # nearest preceding user message
            for prev in reversed(out):
                if prev.get("role") == "user":
                    c = prev.get("content")
                    prev["content"] = ([{"type": "text", "text": c}] if isinstance(c, str) else list(c or [])) + [block]
                    break
            else:
                pending.append(block["text"])
            continue
        m = dict(m)
        if pending and role == "user":
            c = m.get("content")
            m["content"] = [{"type": "text", "text": t} for t in pending] + (
                [{"type": "text", "text": c}] if isinstance(c, str) else list(c or [])
            )
            pending = []
        out.append(m)
    if pending:  # no user message at all: fall back to the system field
        system = body.get("system") or []
        if isinstance(system, str):
            system = [{"type": "text", "text": system}]
        body["system"] = system + [{"type": "text", "text": t} for t in pending]
    body["messages"] = out
    note = body.setdefault("_localagents", {})
    note["moved_system_messages"] = moved
    folded = _fold_text_into_tool_results(out)
    if folded:
        note["folded_tool_result_text"] = folded
    return body


def translate_context_error(status: int, data: bytes) -> tuple[bytes, str] | None:
    """Map a backend context-overflow error onto Anthropic's ``prompt is too long`` error.

    Returns ``(new_body, original_message)`` or None when the error is something else. Claude
    Code keys its recovery (compact, then retry) on the literal ``prompt is too long`` and reads
    ``N tokens > M maximum`` to size the trim, so both are reproduced exactly.
    """
    found = context_overflow(status, data)
    if found is None:
        return None
    msg, actual, limit = found
    text = "prompt is too long"
    if actual is not None and limit is not None:
        text += f": {actual} tokens > {limit} maximum"
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": text}}
    return json.dumps(body).encode(), msg


def _usage_from_json(data: bytes) -> dict[str, Any] | None:
    try:
        return _object(json.loads(data).get("usage"))
    except Exception:  # noqa: BLE001
        return None


def _usage_from_sse_head(chunk: bytes) -> dict[str, Any] | None:
    """Usage from the stream's ``message_start`` event: input and cache token counts."""
    for line in chunk.decode(errors="replace").splitlines():
        if line.startswith("data:") and '"message_start"' in line:
            try:
                return _object((json.loads(line[5:]).get("message") or {}).get("usage"))
            except Exception:  # noqa: BLE001
                pass
    return None


def _merge_usage(head: dict[str, Any] | None, tail: dict[str, Any] | None) -> dict[str, Any] | None:
    """Combine message_start usage (prompt side) with message_delta usage (output side); the
    delta wins where both carry a key, as it is the later, final figure."""
    if not head and not tail:
        return None
    return {**(head or {}), **{k: v for k, v in (tail or {}).items() if v is not None}}


def _usage_from_sse_tail(bytes_: bytes) -> dict[str, Any] | None:
    """The final usage of a stream: the LAST ``message_delta`` event's usage in the bytes
    (the stream's tail, not its last read). A backend that sends usage twice — a mid-stream
    count and the final one — reports the later figure; a ``message_delta`` carrying no
    ``usage`` (a stop_reason-only event) is skipped, not the final one's."""
    usage: dict[str, Any] | None = None
    for line in bytes_.decode(errors="replace").splitlines():
        if line.startswith("data:") and '"message_delta"' in line:
            try:
                parsed = _object(json.loads(line[5:]).get("usage"))
            except Exception:  # noqa: BLE001
                continue
            if parsed is not None:
                usage = parsed
    return usage
