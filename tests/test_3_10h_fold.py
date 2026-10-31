"""Fold of the 3.10h(a) review findings (t/REVIEW.md): F1 — the usage tail scan.

F1: the relay kept only the FINAL read of the stream and scanned it for the
``message_delta`` usage, so a backend that delivers ``message_stop`` as its own read
hides the ``message_delta`` that carries the final ``output_tokens`` and the row keeps
the ``message_start`` placeholder (1). The relay now keeps a bounded tail (the last
8 KiB) of the stream's bytes and reads the LAST ``message_delta`` usage from it: a
``message_stop`` delivered alone no longer hides the output count, a ``message_delta``
split across two reads is still seen whole, and a backend that sends usage twice
reports the later figure. Harness copied from tests/test_3_10h.py; the probes that
reproduced the finding are t/probe_2.py (d6, d2).
"""

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
MESSAGE_DELTA_10 = (
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{},"usage":{"output_tokens":10}}\n\n'
)
MESSAGE_DELTA_57 = (
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":57}}\n\n'
)
MESSAGE_STOP = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


class _SseStream(httpx.AsyncByteStream):
    """An SSE body the fake backend yields at the given delays: (seconds, bytes) steps;
    optionally raises after the last yield (the backend dying mid-stream)."""

    def __init__(self, steps, error=None):
        self._steps = steps
        self._error = error

    async def __aiter__(self):
        for delay, chunk in self._steps:
            if delay:
                await asyncio.sleep(delay)
            yield chunk
        if self._error is not None:
            raise self._error


def _sse_send(steps, error=None):
    async def send(upstream, stream=True):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_SseStream(steps, error), request=upstream)
    return send


def _relay_and_row(tmp_path, monkeypatch, send, body):
    """Drive shim.relay for /v1/messages against a fake upstream, consume the reply stream
    (the relay's generator records the row in its finally); return (reply, reply bytes,
    the drain's exception or None, row)."""
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
        err = None
        if hasattr(resp, "body_iterator"):
            try:
                async for chunk in resp.body_iterator:
                    out += chunk
            except Exception as e:
                err = e
        return resp, out, err

    resp, out, err = asyncio.run(_drive())
    row = job._conn.execute(
        "SELECT r.* FROM requests r JOIN paths p ON p.id = r.path_id"
    ).fetchone()
    return resp, out, err, row


STREAM_BODY = {"model": "m", "messages": [{"role": "user", "content": "hello"}], "stream": True}


def test_message_delta_not_last_read_keeps_the_final_count(tmp_path, monkeypatch):
    """F1 (probe d6): message_delta and message_stop land as separate reads, the stop last.
    The old last-chunk-only tail saw only message_stop and kept the message_start
    placeholder; the row's output_tokens is the delta's count (57), not 1."""
    send = _sse_send([
        (0, MESSAGE_START),
        (0.5, CONTENT_BLOCK_START),
        (0, CONTENT_BLOCK_DELTA),
        (0.3, MESSAGE_DELTA_57),
        (0, MESSAGE_STOP),
    ])
    resp, out, err, row = _relay_and_row(tmp_path, monkeypatch, send, STREAM_BODY)
    assert resp.status_code == 200 and err is None
    assert row["gen_ms"] is not None and 200 <= row["gen_ms"] <= 500  # content was decoded
    assert row["output_tokens"] == 57, row["output_tokens"]
    assert row["input_tokens"] == 67000  # the head's message_start usage survives the merge


def test_message_delta_split_across_two_reads(tmp_path, monkeypatch):
    """F1: the message_delta event itself is split across two reads (the data line's JSON
    straddles the boundary) and message_stop follows as its own read. The bounded tail is
    the stream's bytes joined, so the reassembled event is seen whole: 57, not 1."""
    half = len(MESSAGE_DELTA_57) // 2
    send = _sse_send([
        (0, MESSAGE_START),
        (0.5, CONTENT_BLOCK_START),
        (0, CONTENT_BLOCK_DELTA),
        (0.3, MESSAGE_DELTA_57[:half]),
        (0, MESSAGE_DELTA_57[half:]),
        (0, MESSAGE_STOP),
    ])
    resp, out, err, row = _relay_and_row(tmp_path, monkeypatch, send, STREAM_BODY)
    assert resp.status_code == 200 and err is None
    assert row["output_tokens"] == 57, row["output_tokens"]


def test_two_message_deltas_in_the_tail_the_last_usage_wins(tmp_path, monkeypatch):
    """F1: a backend that sends usage twice — a mid-stream message_delta (10) and the
    final one (57), both inside the stream's last 8 KiB. The row carries the LATER
    figure: the final count, not the first."""
    send = _sse_send([
        (0, MESSAGE_START),
        (0.5, CONTENT_BLOCK_START),
        (0, CONTENT_BLOCK_DELTA),
        (0.1, MESSAGE_DELTA_10),
        (0.3, MESSAGE_DELTA_57),
        (0, MESSAGE_STOP),
    ])
    resp, out, err, row = _relay_and_row(tmp_path, monkeypatch, send, STREAM_BODY)
    assert resp.status_code == 200 and err is None
    assert row["output_tokens"] == 57, row["output_tokens"]


def test_delta_and_stop_in_one_read_unchanged(tmp_path, monkeypatch):
    """F1 regression guard: the one-chunk case the old code handled — message_delta and
    message_stop delivered together in the final read — still records 57."""
    send = _sse_send([
        (0, MESSAGE_START),
        (0.5, CONTENT_BLOCK_START),
        (0, CONTENT_BLOCK_DELTA),
        (0.3, MESSAGE_DELTA_57 + MESSAGE_STOP),
    ])
    resp, out, err, row = _relay_and_row(tmp_path, monkeypatch, send, STREAM_BODY)
    assert resp.status_code == 200 and err is None
    assert row["output_tokens"] == 57, row["output_tokens"]


def test_backend_dies_after_message_start_records_the_error(tmp_path, monkeypatch):
    """P2 (probe d2): the backend closes the connection mid-stream, after message_start
    (no content): the aiter_bytes loop raises. The relay records the row with the
    transport error — error 'stream ended early: ReadError: …', the header's status (the
    200 was already sent before the body) — and re-raises, so the client still sees the
    failure. The page's request rule (status outside 2xx OR a non-empty error) marks the
    row; a clean 200 row with an error is what distinguishes a truncated stream from a
    successful one."""
    send = _sse_send(
        [(0, MESSAGE_START), (0.2, b"")],
        error=httpx.ReadError("backend closed the connection"),
    )
    resp, out, err, row = _relay_and_row(tmp_path, monkeypatch, send, STREAM_BODY)
    assert isinstance(err, httpx.ReadError)  # the client saw the failure
    assert row is not None  # the row reached record_request (the finally ran)
    assert row["http_status"] == 200  # the header's status, as received
    assert row["gen_ms"] == 0  # events but no content: the whole span is prefill
    assert row["error"] == "stream ended early: ReadError: backend closed the connection"


def test_a_clean_stream_records_no_error(tmp_path, monkeypatch):
    """P2 regression guard: a stream that ends normally records error NULL, as before —
    only a stream that ends early carries the 'stream ended early' text (and the status
    pill the page draws for it)."""
    send = _sse_send([
        (0, MESSAGE_START),
        (0.5, CONTENT_BLOCK_START),
        (0, CONTENT_BLOCK_DELTA),
        (0.3, MESSAGE_DELTA_57),
        (0, MESSAGE_STOP),
    ])
    resp, out, err, row = _relay_and_row(tmp_path, monkeypatch, send, STREAM_BODY)
    assert resp.status_code == 200 and err is None
    assert row["http_status"] == 200
    assert row["error"] is None
    assert row["output_tokens"] == 57
