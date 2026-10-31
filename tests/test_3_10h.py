"""Story 3.10h(a): the shim's generation clock starts at the first generated token, not the
first byte. `message_start` precedes prompt processing on llama.cpp, Strata and vLLM, so the
old clock made `ms − gen_ms` (the page's prefill span) measure network and queue latency: the
live page showed 3,000 new tokens "in 19 ms" (159,775 t/s). `gen_ms` now runs from the first
chunk carrying a content event (content_block_start/delta) to the end of the stream, so
`ms − gen_ms` is time-to-first-token (queue + prefill) and the page's prefill rate is honest."""

import asyncio
import json

import httpx
import pytest

from localagents import jobs
from localagents.shim import Shim

MESSAGE_START = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"m","usage":'
    b'{"input_tokens":67000,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,'
    b'"output_tokens":1}}}\n\n'
)
CONTENT_BLOCK_START = (
    b'event: content_block_start\n'
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
)
CONTENT_BLOCK_DELTA = (
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
)
MESSAGE_DELTA = (
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
)
MESSAGE_STOP = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


class _SseStream(httpx.AsyncByteStream):
    """An SSE body the fake backend yields at the given delays: (seconds, bytes) steps."""

    def __init__(self, steps):
        self._steps = steps

    async def __aiter__(self):
        for delay, chunk in self._steps:
            if delay:
                await asyncio.sleep(delay)
            yield chunk


def _sse_send(steps):
    async def send(upstream, stream=True):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_SseStream(steps), request=upstream)
    return send


def _relay_and_row(tmp_path, monkeypatch, send, body):
    """Drive shim.relay for /v1/messages and, when the reply streams, consume it (the relay's
    generator records the row in its finally); return (reply, reply bytes, request row)."""
    from starlette.requests import Request

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    job = jobs.JobStore().new(
        task="t", model="m", endpoint="e", base_url="http://x:8080", backend="llama.cpp", host="local",
        cwd="/tmp", run_cwd="/tmp", isolation="none",
    )
    shim = Shim(lambda jid: job)
    shim._client.send = send
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {
        "type": "http", "method": "POST", "headers": [(b"content-type", b"application/json")],
        "path": f"/job/{job.id}/v1/messages", "query_string": b"", "root_path": "",
        "path_params": {"job_id": job.id, "path": "v1/messages"},
    }

    async def _drive():
        resp = await shim.relay(Request(scope, receive))
        out = b""
        if hasattr(resp, "body_iterator"):
            async for chunk in resp.body_iterator:
                out += chunk
        return resp, out

    resp, out = asyncio.run(_drive())
    row = job._conn.execute(
        "SELECT r.* FROM requests r JOIN paths p ON p.id = r.path_id"
    ).fetchone()
    return resp, out, row


STREAM_BODY = {"model": "m", "messages": [{"role": "user", "content": "hello"}], "stream": True}


def test_gen_ms_starts_at_the_first_generated_token(tmp_path, monkeypatch):
    """message_start arrives immediately; the model then spends 0.5 s on prompt processing.
    gen_ms must measure the delta-to-end time (a few ms), leaving ms − gen_ms ≈ the 0.5 s
    time-to-first-token the prefill rate divides by."""
    send = _sse_send([
        (0, MESSAGE_START),
        (0.5, CONTENT_BLOCK_START),
        (0, CONTENT_BLOCK_DELTA),
        (0, MESSAGE_DELTA),
        (0, MESSAGE_STOP),
    ])
    resp, out, row = _relay_and_row(tmp_path, monkeypatch, send, STREAM_BODY)
    assert resp.status_code == 200
    assert b"content_block_delta" in out  # the relay did forward the stream
    assert row["gen_ms"] is not None and row["gen_ms"] <= 150
    assert row["ms"] - row["gen_ms"] >= 450  # the prefill span survives the clock change


def test_no_content_event_records_zero_decode(tmp_path, monkeypatch):
    """A stream with message_start and no content event (a 0-output reply, or an error after
    prefill) decoded nothing, so gen_ms is 0 and ms − gen_ms is the whole queue+prefill span.
    Zero — not None — lets the page's prefill rule use the row; decode t/s stays blank because
    its rule needs gen_ms > 0."""
    send = _sse_send([
        (0, MESSAGE_START),
        (0.3, MESSAGE_STOP),
    ])
    resp, out, row = _relay_and_row(tmp_path, monkeypatch, send, STREAM_BODY)
    assert resp.status_code == 200
    assert row["gen_ms"] == 0
    assert row["ms"] >= 300


def test_non_streaming_reply_keeps_a_null_gen_ms(tmp_path, monkeypatch):
    """A non-streaming reply has no generation span to measure; its row keeps no gen_ms (a null
    column), exactly as before this story."""
    async def send(upstream, stream=True):
        return httpx.Response(200, json={
            "id": "m", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }, request=upstream)

    resp, out, row = _relay_and_row(
        tmp_path, monkeypatch, send,
        {"model": "m", "messages": [{"role": "user", "content": "hello"}], "stream": False},
    )
    assert resp.status_code == 200
    assert row["gen_ms"] is None


def test_a_stream_with_no_chunks_records_no_gen_ms(tmp_path, monkeypatch):
    """No chunk at all is not 'decoded nothing' — it is no evidence the backend ever started
    the request (a client disconnect during prefill, an empty body). The row keeps gen_ms None
    rather than claiming the whole duration was prefill."""
    send = _sse_send([])
    resp, out, row = _relay_and_row(tmp_path, monkeypatch, send, STREAM_BODY)
    assert resp.status_code == 200
    assert row["gen_ms"] is None
