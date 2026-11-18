"""In-process HTTP shim between headless Claude Code sessions and local backends.

Claude Code targets Anthropic's API and uses features local servers do not all support.
Every job points ``ANTHROPIC_BASE_URL`` at ``http://127.0.0.1:<port>/job/<job_id>``; the
shim normalises the request, forwards it to the job's endpoint, streams the response back,
and records every request in the database's ``requests`` table.

Normalisations (all no-ops when not needed):
* ``role: system`` entries inside ``messages`` (Claude Code's mid-conversation system turns:
  skills listing, token budget, reminders) are folded in place into the adjacent user message —
  chat templates such as Qwen's raise "System message must be at the beginning" otherwise, and
  folding in place (rather than hoisting to ``system``) keeps the KV-cache prefix stable.
* A user message that mixes ``tool_result`` blocks with ``text`` blocks (Claude Code appends a
  system-reminder to the tool-result turn, and the fold above adds one more) has the text moved
  inside the last ``tool_result``'s content. Gemma 4's chat template only renders tool responses
  that are adjacent to the assistant's tool call; the mixed message breaks that adjacency behind
  llama.cpp's ``/v1/messages`` converter and the model never sees any of its tool output.
* ``/v1/messages/count_tokens`` falls back to a char/4 estimate if the backend lacks it.
* Backend "context exceeded" errors (llama.cpp ``exceed_context_size_error``, vLLM "maximum
  context length") are rewritten into Anthropic's ``prompt is too long: N tokens > M maximum``
  envelope, which Claude Code recognises and answers by compacting the conversation. This is the
  backstop; the primary guard is ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` set from the probed window
  (see ``harnesses.claude_code.build_env``) so auto-compact runs *before* the backend overflows.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import time
from typing import TYPE_CHECKING, Any, Callable

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from .jobs import Job
    from .registry import Endpoint

HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding"}
# SSE usage capture bounds (3.10h fold): message_start arrives first (input and cache
# counts) and is read from the head of the stream; message_delta arrives near the end
# (the final output count) and is read from the stream's tail — the last 8 KiB of its
# bytes, not its last read, so a message_stop delivered as its own read no longer hides
# the delta, and a delta split across two reads is still seen whole.
HEAD_BYTES = 65536
TAIL_BYTES = 8192


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


def normalise_messages_body(body: dict[str, Any]) -> dict[str, Any]:
    """Fold ``role: system`` entries in ``messages`` into an adjacent user message, then fold
    text blocks that share a user message with tool results into the last tool result.

    System text keeps its position (appended to the nearest preceding user message, or
    prepended to the next one) rather than being hoisted into the top-level ``system`` field, so
    the prompt stays append-only across turns and the backend's KV-cache prefix is not
    invalidated on every request.
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return body
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


def _apply_levers(body: dict[str, Any], levers: dict[str, Any] | None) -> dict[str, Any]:
    """Write a job's thinking/effort levers into a ``/v1/messages`` body.

    Thinking goes in ``chat_template_kwargs.enable_thinking`` (the argument Qwen-family
    templates read; the backend honours only the per-request template argument) and effort in
    the top-level ``reasoning_effort`` vLLM accepts. Only levers the caller set are written;
    with no levers the body is untouched. Returns what was applied, for the requests row's
    ``levers_applied`` note; whether the backend honours either is the facts probe's question.
    """
    if not levers:
        return {}
    applied: dict[str, Any] = {}
    thinking = levers.get("thinking")
    if thinking is not None:
        kwargs = body.get("chat_template_kwargs")
        kwargs = dict(kwargs) if isinstance(kwargs, dict) else {}
        kwargs["enable_thinking"] = thinking
        body["chat_template_kwargs"] = kwargs
        applied["thinking"] = thinking
    effort = levers.get("effort")
    if effort is not None:
        body["reasoning_effort"] = effort
        applied["effort"] = effort
    return applied


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
)
_CTX_HINTS = (
    "exceeds the available context", "maximum context length", "exceed context limit", "context length exceeded",
    "exceeds the context window",
)


def translate_context_error(status: int, data: bytes) -> tuple[bytes, str] | None:
    """Map a backend context-overflow error onto Anthropic's ``prompt is too long`` error.

    Returns ``(new_body, original_message)`` or None when the error is something else. Claude
    Code keys its recovery (compact, then retry) on the literal ``prompt is too long`` and reads
    ``N tokens > M maximum`` to size the trim, so both are reproduced exactly. vLLM reports the
    condition as HTTP 500 ``internal_error``, so any 4xx/5xx with a matching message qualifies.
    """
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
    text = "prompt is too long"
    if actual is not None and limit is not None:
        text += f": {actual} tokens > {limit} maximum"
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": text}}
    return json.dumps(body).encode(), msg


class Shim:
    def __init__(
        self,
        lookup: Callable[[str], "Job | None"],
        endpoint_lookup: Callable[[str], "Endpoint | None"] | None = None,
    ):
        self.lookup = lookup
        # The session is given the placeholder key ``local``; the endpoint's real ``api_key`` (if
        # any) is swapped in here so it never reaches the delegated agent's environment.
        self.endpoint_lookup = endpoint_lookup
        self.port: int | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10))
        self.app = Starlette(routes=[Route("/job/{job_id}/{path:path}", self.relay, methods=["GET", "POST"])])

    # ---- lifecycle
    async def start(self) -> int:
        if self.port:
            return self.port
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False, lifespan="off")
        self._server = uvicorn.Server(cfg)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(100):
            if self._server.started:
                return self.port
            await asyncio.sleep(0.05)
        raise RuntimeError("shim failed to start")

    def base_url_for(self, job_id: str) -> str:
        return f"http://127.0.0.1:{self.port}/job/{job_id}"

    # ---- request handling
    async def relay(self, req: Request) -> Response:
        job = self.lookup(req.path_params["job_id"])
        if job is None:
            return JSONResponse({"error": {"type": "not_found", "message": "unknown job"}}, status_code=404)
        path = "/" + req.path_params["path"]
        if path == "/api/hello":
            # Claude Code opens every job with /api/hello: HEAD, and a GET, as a connection
            # warm-up whose reply it discards (story 3.7t). Forwarded, it recorded the backend's
            # 404/501 as a row the page painted as an error. The requests table records backend
            # requests the shim forwarded, so this one is answered here, not sent, not logged.
            return JSONResponse({})
        raw = await req.body()
        body: dict[str, Any] | None = None
        note: dict[str, Any] = {}
        if req.method == "POST" and raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = None
        if body is not None and path.endswith("/v1/messages"):
            body = normalise_messages_body(body)
            note = body.pop("_localagents", {})
            applied = _apply_levers(body, job.levers)
            if applied:
                note["levers_applied"] = applied
            raw = json.dumps(body).encode()
        headers = {k: v for k, v in req.headers.items() if k.lower() not in HOP_HEADERS}
        ep = self.endpoint_lookup(job.endpoint) if self.endpoint_lookup else None
        if ep is not None and ep.api_key:
            headers.update(ep.auth_headers())
        url = job.base_url + path
        t0 = time.monotonic()
        upstream = self._client.build_request(req.method, url, content=raw, headers=headers, params=req.query_params)
        try:
            resp = await self._client.send(upstream, stream=True)
        except httpx.HTTPError as e:
            self._log(job, path, body, note, status=0, ms=int((time.monotonic() - t0) * 1000), error=str(e))
            return JSONResponse({"error": {"type": "api_error", "message": f"backend unreachable: {e}"}}, status_code=502)

        # "this backend has no count_tokens" is not one status: llama.cpp answers 404, vLLM
        # 405, a gateway may say 501 (review of 3.5, F4: latent robustness; no backend here
        # answered 501 to count_tokens — the 501 row every job used to carry was Claude Code's
        # /api/hello handshake, which the shim answers itself since 3.7t). Any other status — a
        # 500 is a real failure — forwards
        # untouched: hiding a failure behind a plausible token count is worse than the failure.
        if path.endswith("/count_tokens") and resp.status_code in (404, 405, 501):
            await resp.aclose()
            est = max(1, len(raw) // 4)
            # the row records the backend's own status: http_status is always what the backend
            # answered, even though the reply to Claude Code is our 200 with the estimate
            # (story 1.9 item 8), and its own ms, not a 0 that says the round-trip was instant
            # (story 1.9a row 2)
            self._log(job, path, body, note, status=resp.status_code,
                      ms=int((time.monotonic() - t0) * 1000), error="count_tokens unsupported; estimated")
            return JSONResponse({"input_tokens": est})

        if body is not None and (job.dump or os.environ.get("LOCALAGENTS_DUMP_REQUESTS") == "1"):
            # a debugging side file never fails the session's request (3.11a row 6)
            try:
                with open(os.path.join(job.log_dir, "requests_full.jsonl"), "a") as f:
                    f.write(json.dumps({"path": path, "body": body}) + "\n")
            except OSError as e:
                job._log_safe(f"[dump-error] requests_full.jsonl: {type(e).__name__}: {e}")

        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "cache-control")}
        if resp.status_code >= 400 or not resp.headers.get("content-type", "").startswith("text/event-stream"):
            data = await resp.aread()
            await resp.aclose()
            status = resp.status_code
            translated = translate_context_error(status, data) if status >= 400 else None
            # the row records what the backend said — its status and its body — even when the
            # client gets the rewrite. `context_exceeded` carries the backend's original
            # message, so the rewrite is visible in the row (story 1.9a row 2; the contract in
            # the spec: http_status is always the backend's status)
            if translated:
                note = {**note, "context_exceeded": translated[1][:300]}
            self._log(job, path, body, note, status=status, ms=int((time.monotonic() - t0) * 1000),
                      error=data[:800].decode(errors="replace") if status >= 400 else None,
                      usage=_usage_from_json(data))
            if translated:
                data = translated[0]  # the Anthropic envelope Claude Code compacts on
                status = 400
                resp_headers["content-type"] = "application/json"
            return Response(content=data, status_code=status, headers=resp_headers)

        async def gen():
            head = b""  # message_start carries input/cache token counts
            tail = b""  # the stream's last TAIL_BYTES; message_delta carries output_tokens
            t_first: float | None = None  # first chunk carrying generated content, not first byte
            stream_error: str | None = None
            try:
                try:
                    async for chunk in resp.aiter_bytes():
                        # message_start precedes prompt processing on llama.cpp, Strata and vLLM, so
                        # starting the clock at the first byte made ms − gen_ms network and queue
                        # latency, not prefill (a page showing 3,000 tokens "in 19 ms", story
                        # 3.10h(a)). The clock starts at the first content event instead: then
                        # ms − gen_ms is time-to-first-token (queue + prefill), and the prefill rate
                        # derived from it is honest.
                        if t_first is None and (b"content_block_start" in chunk
                                                or b"content_block_delta" in chunk):
                            t_first = time.monotonic()
                        if len(head) < HEAD_BYTES:  # a hard cap: never one whole read past it
                            head += chunk[:HEAD_BYTES - len(head)]
                        # the tail is the stream's own last 8 KiB of bytes, not the final read:
                        # a message_stop delivered as its own read no longer hides the
                        # message_delta that carries the final output_tokens
                        tail = (tail + chunk)[-TAIL_BYTES:]
                        yield chunk
                except Exception as e:
                    # the backend died mid-stream (a closed connection — aiter_bytes raises
                    # httpx's ReadError and friends) or the client's disconnect raised:
                    # record the row as an error — the header's status, which was already
                    # sent, and the page's request rule (status outside 2xx OR a non-empty
                    # error) marks it — then re-raise so the client still sees the failure.
                    stream_error = f"stream ended early: {type(e).__name__}: {e}"[:300]
                    raise
            finally:
                await resp.aclose()
                t_end = time.monotonic()
                usage = _merge_usage(_usage_from_sse_head(head), _usage_from_sse_tail(tail))
                # a stream that sent events but no content decoded nothing: gen_ms 0 makes
                # ms − gen_ms the whole queue+prefill span (the 0-output row in the screenshot
                # is the case that matters). No chunk at all is no evidence the backend ever
                # started the request, so it keeps the old null.
                if t_first is not None:
                    gen_ms: int | None = int((t_end - t_first) * 1000)
                else:
                    gen_ms = 0 if head else None
                self._log(job, path, body, note, status=resp.status_code, ms=int((t_end - t0) * 1000),
                          gen_ms=gen_ms, usage=usage, error=stream_error)

        return StreamingResponse(gen(), status_code=resp.status_code, headers=resp_headers)

    def _log(self, job: "Job", path: str, body: dict[str, Any] | None, note: dict[str, Any], **kw: Any) -> None:
        rec: dict[str, Any] = {"t": round(time.time(), 3), "path": path, **kw}
        if body:
            rec.update(
                model=body.get("model"),
                messages=len(body.get("messages") or []),
                tools=len(body.get("tools") or []),
                stream=body.get("stream"),
                bytes=len(json.dumps(body)),
            )
        rec.update(note)
        rec = {k: v for k, v in rec.items() if v is not None}
        # story 1.5b: no note_usage — the row is the record; the status line and job_status
        # derive ctx_tokens/gen_tps/usage from the requests table.
        job.record_request(rec)
        if kw.get("error") and kw.get("status", 0) >= 400:
            job.log_text(f"[backend {kw['status']}] {str(kw['error'])[:300]}")


def _object(v: Any) -> dict[str, Any] | None:
    """The backend's ``usage`` when it is a JSON object, else None: every reader below hands
    back an object or nothing, so a backend that sends a string or an array there can neither
    fail the reply nor lose the row (3.11a row 4)."""
    return v if isinstance(v, dict) else None


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
