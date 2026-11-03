"""router 9d: the shim's thinking and effort levers.

The backend never sees SDK options; it sees the ``/v1/messages`` body the shim relays. A job
whose caller set ``thinking`` or ``effort`` (``job.levers``) has them written into that body,
and the requests row's note records which were applied. A job without levers sends exactly the
body the shim sent before this translation existed.
"""

import asyncio
import json

import httpx

from localagents import jobs
from localagents import shim as shim_mod

BODY = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}


def _capture():
    """A fake upstream: records each request's raw body, answers a small JSON message."""
    seen = []

    async def send(upstream, stream=True):
        seen.append(upstream.content)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=b'{"type":"message","content":[]}', request=upstream)

    return send, seen


def _relay(send, body, levers=None, path="v1/messages"):
    """Drive ``shim.relay`` once; returns the job and the records ``_log`` handed to it."""
    from starlette.requests import Request

    extra = {} if levers is None else {"levers": levers}
    job = jobs.JobStore().new(
        task="t", model="m", endpoint="e", base_url="http://x:8080", backend="llama.cpp",
        host="local", cwd="/srv/9d", run_cwd="/srv/9d", isolation="none", **extra,
    )
    records = []
    job.record_request = records.append  # the note is observed where the row is written
    shim = shim_mod.Shim(lambda jid: job)
    shim._client.send = send
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {
        "type": "http", "method": "POST", "headers": [(b"content-type", b"application/json")],
        "path": f"/job/{job.id}/{path}", "query_string": b"", "root_path": "",
        "path_params": {"job_id": job.id, "path": path},
    }
    asyncio.run(shim.relay(Request(scope, receive)))
    return job, records


def test_thinking_false_reaches_the_body_and_the_note():
    """The Qwen-family template argument: ``chat_template_kwargs.enable_thinking``."""
    send, seen = _capture()
    _, records = _relay(send, BODY, levers={"thinking": False})
    body = json.loads(seen[0])
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    assert records[0]["levers_applied"] == {"thinking": False}


def test_effort_reaches_the_body_and_the_note():
    """The OpenAI-style top-level field vLLM accepts."""
    send, seen = _capture()
    _, records = _relay(send, BODY, levers={"effort": "low"})
    body = json.loads(seen[0])
    assert body["reasoning_effort"] == "low"
    assert records[0]["levers_applied"] == {"effort": "low"}


def test_a_job_without_levers_sends_the_body_unchanged():
    """Byte-identical to what the shim sent before the translation existed."""
    send, seen = _capture()
    _, records = _relay(send, BODY)
    assert seen[0] == json.dumps(BODY).encode()
    assert "levers_applied" not in records[0]


def test_a_non_messages_path_is_untouched():
    """The levers are a ``/v1/messages`` translation; other paths go through as they came."""
    send, seen = _capture()
    _, records = _relay(send, BODY, levers={"thinking": False, "effort": "high"},
                        path="v1/completions")
    assert seen[0] == json.dumps(BODY).encode()
    assert "levers_applied" not in records[0]
