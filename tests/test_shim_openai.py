"""Epic 6.6: the shim's OpenAI version, ``/v1/chat/completions``, over the relay core.

The backend replies are the 6.2 captures, byte for byte (tests/fixtures/opencode/wire): Strata's
and llama.cpp's chat-completions SSE with tool calls (usage on the finish chunk vs a trailing
empty-choices chunk), and both backends' context-overflow 400s. The rows carry the same
Anthropic-named token columns every reader sums; an overflow goes back unrewritten (OpenCode
compacts on the backends' own 400s) while the row still notes it.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from localagents import derive, jobs, shim_openai
from localagents.shim import Shim, api_for
from localagents.shim_anthropic import translate_context_error

WIRE = Path(__file__).resolve().parent / "fixtures" / "opencode" / "wire"


def _capture(name):
    """(status, headers, body bytes) of a captured reply file: a STATUS line, header lines, a
    blank line, then the body as received."""
    raw = (WIRE / name).read_bytes()
    head, _, body = raw.partition(b"\n\n")
    lines = head.decode().splitlines()
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        if k:
            headers[k.strip().lower()] = v.strip()
    return status, headers, body


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for c in self._chunks:
            yield c


def _send_capture(name, split=4096):
    status, headers, body = _capture(name)
    chunks = [body[i:i + split] for i in range(0, len(body), split)] or [b""]

    async def send(upstream, stream=True):
        return httpx.Response(status, headers={"content-type": headers.get("content-type", "application/json")},
                              stream=_Chunks(chunks), request=upstream)
    return send, body


def _relay(tmp_path, monkeypatch, send, body, path="v1/chat/completions", hook=None):
    from starlette.requests import Request

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAGENTS_DB", str(tmp_path / "s.db"))
    job = jobs.JobStore().new(
        task="t", model="m", endpoint="e", base_url="http://x:8080", backend="llama.cpp", host="local",
        cwd="/tmp", run_cwd="/tmp", isolation="none",
    )
    if hook is not None:
        job._on_request = hook
    shim = Shim(lambda jid: job)
    shim._client.send = send
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {"type": "http", "method": "POST", "headers": [(b"content-type", b"application/json")],
             "path": f"/job/{job.id}/{path}", "query_string": b"", "root_path": "",
             "path_params": {"job_id": job.id, "path": path}}

    async def drive():
        resp = await shim.relay(Request(scope, receive))
        out = b""
        if hasattr(resp, "body_iterator"):
            async for chunk in resp.body_iterator:
                out += chunk
        else:
            out = resp.body
        return resp, out

    resp, out = asyncio.run(drive())
    rows = job._conn.execute("SELECT p.path, r.* FROM requests r JOIN paths p ON p.id = r.path_id").fetchall()
    return resp, out, rows, job


BODY = {"model": "m", "stream": True, "stream_options": {"include_usage": True}, "max_completion_tokens": 16384,
        "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}]}


def test_the_path_picks_the_api_version():
    assert api_for("/v1/chat/completions") is shim_openai
    assert api_for("/v1/messages").NAME == "anthropic"
    assert api_for("/v1/messages/count_tokens").NAME == "anthropic"
    assert api_for("/api/hello").NAME == "anthropic"
    assert api_for("/v1/models") is None


@pytest.mark.parametrize("capture, prompt, cached, output", [
    ("strata-chat-sse-tool-calls.txt", 9116, 0, 78),               # usage on the finish_reason chunk
    ("llamacpp-chat-sse-reasoning-tool-calls.txt", 9130, 0, 93),  # usage on a trailing empty-choices chunk
])
def test_a_captured_stream_passes_through_whole_and_its_usage_is_the_row(tmp_path, monkeypatch, capture,
                                                                        prompt, cached, output):
    send, body = _send_capture(capture)
    resp, out, rows, job = _relay(tmp_path, monkeypatch, send, BODY)
    assert resp.status_code == 200 and out == body  # every byte, unchanged
    (row,) = rows
    assert row["path"] == "/v1/chat/completions" and row["http_status"] == 200
    assert (row["input_tokens"], row["cache_read_tokens"], row["output_tokens"]) == (prompt - cached, cached, output)
    assert row["stream"] == 1 and row["messages"] == 2 and row["tools"] == 1
    assert row["gen_ms"] is not None and row["error"] is None
    # the readers sum it as they sum Anthropic's rows (a model request, by kind)
    assert derive.ctx_tokens(job._conn, job.id) == prompt
    assert derive.usage_totals(job._conn, job.id)["output_tokens"] == output


def test_cached_prompt_tokens_are_the_cache_read_column():
    usage = {"prompt_tokens": 10185, "completion_tokens": 142, "total_tokens": 10327,
             "prompt_tokens_details": {"cached_tokens": 10040}}  # r1-tools wire 005, Strata
    assert shim_openai.row_usage(usage) == {"output_tokens": 142, "input_tokens": 145, "cache_read_input_tokens": 10040}
    assert shim_openai.row_usage({"prompt_tokens": 5, "completion_tokens": 1}) == {
        "output_tokens": 1, "input_tokens": 5, "cache_read_input_tokens": 0}
    assert shim_openai.row_usage({"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 9}})[
        "cache_read_input_tokens"] == 0  # a nonsense count is not trusted
    assert shim_openai.row_usage(None) is None


@pytest.mark.parametrize("capture, actual, limit", [
    ("strata-overflow-400.txt", 390295, 131072),
    ("llamacpp-overflow-400.txt", 585373, 212992),
])
def test_an_overflow_goes_back_as_the_backend_sent_it_and_the_row_notes_it(tmp_path, monkeypatch, capture,
                                                                            actual, limit):
    send, body = _send_capture(capture)
    resp, out, rows, _ = _relay(tmp_path, monkeypatch, send, BODY)
    assert resp.status_code == 400 and out == body  # OpenCode compacts on the backend's own 400
    (row,) = rows
    assert row["http_status"] == 400 and row["context_exceeded"] and row["error"]
    # the same captured error through the Anthropic version is rewritten, with the numbers
    # (Strata's current wording gained its pattern with 6.6)
    rewritten, _ = translate_context_error(400, body)
    assert json.loads(rewritten)["error"]["message"] == f"prompt is too long: {actual} tokens > {limit} maximum"


def test_the_harness_hook_sees_each_request_and_cannot_fail_it(tmp_path, monkeypatch):
    seen = []
    send, _ = _send_capture("strata-chat-sse-tool-calls.txt")
    _relay(tmp_path, monkeypatch, send, BODY, hook=lambda path, body: seen.append((path, len(body["messages"]))))
    assert seen == [("/v1/chat/completions", 2)]

    def broken(path, body):
        raise RuntimeError("boom")

    send, _ = _send_capture("strata-chat-sse-tool-calls.txt")
    resp, out, rows, job = _relay(tmp_path / "b", monkeypatch, send, BODY, hook=broken)
    assert resp.status_code == 200 and len(rows) == 1
    assert "[hook-error] RuntimeError: boom" in (Path(job.log_dir) / "transcript.txt").read_text()


def test_the_generation_clock_starts_at_generated_content_not_the_role_chunk():
    role = b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n\n'
    null = b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":null}}]}\n\n'
    text = b'data: {"choices":[{"index":0,"delta":{"content":"I"}}]}\n\n'
    reasoning = b'data: {"choices":[{"index":0,"delta":{"reasoning_content":"I"}}]}\n\n'
    tool = b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0}]}}]}\n\n'
    assert not shim_openai.content_started(role) and not shim_openai.content_started(null)
    assert all(shim_openai.content_started(c) for c in (text, reasoning, tool))
    # the captured streams' first chunks are role announcements
    for capture in ("strata-chat-sse-tool-calls.txt", "llamacpp-chat-sse-reasoning-tool-calls.txt"):
        _, _, body = _capture(capture)
        first = body.split(b"\n\n")[0]
        assert not shim_openai.content_started(first), capture


def test_the_core_holds_no_api_version():
    """The 6.6+6.7 review (DeepSeek, job fcf6dbe3), NOTICED made a rule: the Anthropic version was
    copied into shim_anthropic.py and its old copy left in the core, already drifted (the core's
    translate_context_error lacked Strata's current wording). One owner each: the core defines no
    normaliser, no overflow rewrite or pattern and no usage reader."""
    import ast

    import localagents.shim as core

    tree = ast.parse(Path(core.__file__).read_text())
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    defined |= {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    for name in ("normalise_messages_body", "translate_context_error", "_fold_text_into_tool_results",
                 "_text_tool_references", "_text_of", "_CTX_PATTERNS", "_CTX_HINTS", "_object",
                 "_usage_from_json", "_usage_from_sse_head", "_usage_from_sse_tail", "_merge_usage"):
        assert name not in defined, name
    assert defined >= {"Shim", "api_for", "_apply_levers", "APIS"}


def test_a_client_that_goes_away_mid_stream_leaves_an_error_row(tmp_path, monkeypatch):
    """The 6.6+6.7 review, NOTICED (older than 6.6) made a rule: when the client disconnects,
    Starlette cancels the task draining the stream; the CancelledError passed the generator's
    `except Exception` and the row read as a clean 200 with no error. The row now says the stream
    ended early because the client went away (either API)."""
    from starlette.requests import Request

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAGENTS_DB", str(tmp_path / "s.db"))
    job = jobs.JobStore().new(task="t", model="m", endpoint="e", base_url="http://x:8080", backend="llama.cpp",
                              host="local", cwd="/tmp", run_cwd="/tmp", isolation="none")

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"I"}}]}\n\n'
            await asyncio.sleep(30)  # the backend still working when the client leaves
            yield b"data: [DONE]\n\n"

    async def send(upstream, stream=True):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Slow(), request=upstream)

    shim = Shim(lambda jid: job)
    shim._client.send = send
    raw = json.dumps(BODY).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {"type": "http", "method": "POST", "headers": [(b"content-type", b"application/json")],
             "path": f"/job/{job.id}/v1/chat/completions", "query_string": b"", "root_path": "",
             "path_params": {"job_id": job.id, "path": "v1/chat/completions"}}

    async def drive():
        resp = await shim.relay(Request(scope, receive))
        got = []

        async def drain():
            async for chunk in resp.body_iterator:
                got.append(chunk)

        task = asyncio.create_task(drain())
        while not got:
            await asyncio.sleep(0.01)
        task.cancel()  # what Starlette does on a disconnect
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    (row,) = job._conn.execute("SELECT http_status, error FROM requests").fetchall()
    assert row["http_status"] == 200
    assert row["error"] and "client went away" in row["error"], row["error"]
