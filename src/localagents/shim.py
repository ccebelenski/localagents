"""The in-process HTTP shim between a job's agent session and its local backend: the relay core.

Every job's session is pointed at ``http://127.0.0.1:<port>/job/<job_id>``; the shim forwards
each request to the job's endpoint (with the endpoint's real ``api_key``, which never reaches the
session), streams the response back, and records every forwarded request in the database's
``requests`` table. What is one API's lives in its own version (Epic 6.6): shim_anthropic.py
(``/v1/messages``, Claude Code's) and shim_openai.py (``/v1/chat/completions``, OpenCode's); the
wire they share in shim_wire.py. A request matching neither is relayed as it is, with no usage.
The backend-facing levers (thinking, effort) are written into either API's body here; a job's
``on_request`` hook (a harness's, Epic 6.7) sees each parsed request body before it is sent.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import shim_anthropic, shim_openai
from .shim_wire import HEAD_BYTES, TAIL_BYTES, context_overflow

if TYPE_CHECKING:
    from .jobs import Job
    from .registry import Endpoint

HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding"}
# the API versions, matched by path in this order
APIS = (shim_anthropic, shim_openai)


def api_for(path: str) -> ModuleType | None:
    """The API version a request path belongs to, or None (relayed as it is, no usage read)."""
    return next((api for api in APIS if api.matches(path)), None)


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
        api = api_for(path)
        local = api.answer_locally(path) if api is not None else None
        if local is not None:
            return JSONResponse(local)
        raw = await req.body()
        body: dict[str, Any] | None = None
        note: dict[str, Any] = {}
        if req.method == "POST" and raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = None
            if not isinstance(body, dict):
                body = None
        if body is not None and api is not None and not path.endswith("/count_tokens"):
            body = api.prepare(path, body)
            note = body.pop("_localagents", {})
            applied = _apply_levers(body, job.levers)
            if applied:
                note["levers_applied"] = applied
            hook = job._on_request
            if hook is not None:
                # a harness's view of the request (Epic 6.7: OpenCode's compactions); never fails it
                try:
                    hook(path, body)
                except Exception as e:  # noqa: BLE001
                    job._log_safe(f"[hook-error] {type(e).__name__}: {e}")
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

        est = api.count_tokens_estimate(path, resp.status_code, raw) if api is not None else None
        if est is not None:
            await resp.aclose()
            # the row records the backend's own status: http_status is always what the backend
            # answered, even though the reply to the client is our 200 with the estimate
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
            overflow = context_overflow(status, data) if status >= 400 else None
            reply = api.error_reply(status, data) if overflow is not None and api is not None else None
            # the row records what the backend said — its status and its body — even when the
            # client gets a rewrite. `context_exceeded` carries the backend's original message,
            # whichever API's reply the client gets (story 1.9a row 2; the contract in the spec:
            # http_status is always the backend's status)
            if overflow is not None:
                note = {**note, "context_exceeded": overflow[0][:300]}
            self._log(job, path, body, note, status=status, ms=int((time.monotonic() - t0) * 1000),
                      error=data[:800].decode(errors="replace") if status >= 400 else None,
                      usage=api.usage_from_json(data) if api is not None else None)
            if reply is not None:
                data = reply  # the API's own envelope (Anthropic's: the one Claude Code compacts on)
                status = 400
                resp_headers["content-type"] = "application/json"
            return Response(content=data, status_code=status, headers=resp_headers)

        async def gen():
            head = b""  # Anthropic's message_start carries input/cache token counts
            tail = b""  # the stream's last TAIL_BYTES: the final usage of either API
            t_first: float | None = None  # first chunk carrying generated content, not first byte
            stream_error: str | None = None
            try:
                try:
                    async for chunk in resp.aiter_bytes():
                        # the first event precedes prompt processing on llama.cpp, Strata and
                        # vLLM, so starting the clock at the first byte made ms − gen_ms network
                        # and queue latency, not prefill (a page showing 3,000 tokens "in 19 ms",
                        # story 3.10h(a)). The clock starts at the first content the API's version
                        # recognises instead: then ms − gen_ms is time-to-first-token (queue +
                        # prefill), and the prefill rate derived from it is honest.
                        if t_first is None and api is not None and api.content_started(chunk):
                            t_first = time.monotonic()
                        if len(head) < HEAD_BYTES:  # a hard cap: never one whole read past it
                            head += chunk[:HEAD_BYTES - len(head)]
                        # the tail is the stream's own last 8 KiB of bytes, not the final read:
                        # a final event delivered as its own read no longer hides the usage
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
                except (asyncio.CancelledError, GeneratorExit):
                    # the client went away: Starlette cancels the task draining this stream
                    # (CancelledError is not an Exception), or the generator is closed. The row
                    # says so rather than reading as a clean 200 (the 6.6+6.7 review)
                    stream_error = "stream ended early: the client went away"
                    raise
            finally:
                await resp.aclose()
                t_end = time.monotonic()
                usage = api.usage_from_stream(head, tail) if api is not None else None
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
