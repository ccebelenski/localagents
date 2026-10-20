"""In-process HTTP shim between headless Claude Code sessions and local backends.

Claude Code targets Anthropic's API and uses features local servers do not all support.
Every job points ``ANTHROPIC_BASE_URL`` at ``http://127.0.0.1:<port>/job/<job_id>``; the
shim normalises the request, forwards it to the job's endpoint, streams the response back,
and writes a per-job ``requests.jsonl`` for debugging.

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
  (see ``runner.build_env``) so auto-compact runs *before* the backend overflows.
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

        if path.endswith("/count_tokens") and resp.status_code == 404:
            await resp.aclose()
            est = max(1, len(raw) // 4)
            self._log(job, path, body, note, status=200, ms=0, error="count_tokens unsupported; estimated")
            return JSONResponse({"input_tokens": est})

        if body is not None and (job.dump or os.environ.get("LOCALAGENTS_DUMP_REQUESTS") == "1"):
            with open(os.path.join(job.log_dir, "requests_full.jsonl"), "a") as f:
                f.write(json.dumps({"path": path, "body": body}) + "\n")

        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "cache-control")}
        if resp.status_code >= 400 or not resp.headers.get("content-type", "").startswith("text/event-stream"):
            data = await resp.aread()
            await resp.aclose()
            status = resp.status_code
            translated = translate_context_error(status, data) if status >= 400 else None
            if translated:
                data, original = translated
                status = 400
                resp_headers["content-type"] = "application/json"
                note = {**note, "context_exceeded": original[:300]}
            self._log(job, path, body, note, status=status, ms=int((time.monotonic() - t0) * 1000),
                      error=data[:800].decode(errors="replace") if status >= 400 else None,
                      usage=_usage_from_json(data))
            return Response(content=data, status_code=status, headers=resp_headers)

        async def gen():
            head = b""  # message_start carries input/cache token counts
            last = b""  # message_delta carries output_tokens
            t_first: float | None = None
            try:
                async for chunk in resp.aiter_bytes():
                    if t_first is None:
                        t_first = time.monotonic()
                    if len(head) < 65536:
                        head += chunk
                    last = chunk
                    yield chunk
            finally:
                await resp.aclose()
                t_end = time.monotonic()
                usage = _merge_usage(_usage_from_sse_head(head), _usage_from_sse_tail(last))
                self._log(job, path, body, note, status=resp.status_code, ms=int((t_end - t0) * 1000),
                          gen_ms=int((t_end - t_first) * 1000) if t_first else None, usage=usage)

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
        if path.endswith("/v1/messages"):
            job.note_usage(kw.get("usage"), int(kw.get("ms") or 0), kw.get("gen_ms"))
        with open(os.path.join(job.log_dir, "requests.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        if kw.get("error") and kw.get("status", 0) >= 400:
            job.log_text(f"[backend {kw['status']}] {str(kw['error'])[:300]}")


def _usage_from_json(data: bytes) -> dict[str, Any] | None:
    try:
        return json.loads(data).get("usage")
    except Exception:  # noqa: BLE001
        return None


def _usage_from_sse_head(chunk: bytes) -> dict[str, Any] | None:
    """Usage from the stream's ``message_start`` event: input and cache token counts."""
    for line in chunk.decode(errors="replace").splitlines():
        if line.startswith("data:") and '"message_start"' in line:
            try:
                return (json.loads(line[5:]).get("message") or {}).get("usage")
            except Exception:  # noqa: BLE001
                pass
    return None


def _merge_usage(head: dict[str, Any] | None, tail: dict[str, Any] | None) -> dict[str, Any] | None:
    """Combine message_start usage (prompt side) with message_delta usage (output side); the
    delta wins where both carry a key, as it is the later, final figure."""
    if not head and not tail:
        return None
    return {**(head or {}), **{k: v for k, v in (tail or {}).items() if v is not None}}


def _usage_from_sse_tail(chunk: bytes) -> dict[str, Any] | None:
    for line in chunk.decode(errors="replace").splitlines():
        if line.startswith("data:") and '"message_delta"' in line:
            try:
                return json.loads(line[5:]).get("usage")
            except Exception:  # noqa: BLE001
                pass
    return None
