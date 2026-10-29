"""Story 1.3: the requests write path. The shim writes one ``requests`` row per backend
request instead of appending ``requests.jsonl``; everything in-memory (note_usage, ctx_tokens,
gen_tps) is unchanged. The fixture is ten real records in exactly the shape shim._log builds."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from localagents import jobs
from localagents.shim import Shim, translate_context_error


FIXTURE = Path(__file__).parent / "fixtures" / "requests_20261029-031617-adf7.jsonl"


def _job(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return jobs.JobStore().new(
        task="t", model="m", endpoint="e", base_url="http://x:8080", backend="llama.cpp", host="local",
        cwd="/tmp", run_cwd="/tmp", isolation="none",
    )


def test_fixture_replays_into_rows(tmp_path, monkeypatch):
    """Every fixture line maps to a row exactly as the spec's ``requests`` table says."""
    job = _job(tmp_path, monkeypatch)
    lines = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    for line in lines:
        job.record_request(line)
    rows = job._conn.execute(
        "SELECT r.*, p.path AS path_name FROM requests r JOIN paths p ON p.id = r.path_id ORDER BY r.t"
    ).fetchall()
    assert len(rows) == len(lines) == 10
    paths = [r["path"] for r in job._conn.execute("SELECT path FROM paths ORDER BY path").fetchall()]
    assert paths == ["/api/hello", "/v1/messages"]
    for line, row in zip(lines, rows):
        usage = line.get("usage") or {}
        assert row["job_id"] == job.id
        assert row["path_name"] == line["path"]
        assert row["t"] == line["t"]
        assert row["http_status"] == line.get("status")
        assert row["ms"] == line.get("ms")
        assert row["gen_ms"] == line.get("gen_ms")
        want_stream = (1 if line["stream"] else 0) if "stream" in line else None
        assert row["stream"] == want_stream
        assert row["input_tokens"] == usage.get("input_tokens")
        assert row["output_tokens"] == usage.get("output_tokens")
        assert row["cache_read_tokens"] == usage.get("cache_read_input_tokens")
        assert row["cache_creation_tokens"] == usage.get("cache_creation_input_tokens")
        assert row["messages"] == line.get("messages")
        assert row["tools"] == line.get("tools")
        assert row["bytes"] == line.get("bytes")
        assert row["moved_system"] == line.get("moved_system_messages")
        assert row["folded_text"] == line.get("folded_tool_result_text")
        assert row["context_exceeded"] == line.get("context_exceeded")
        assert row["error"] == line.get("error")  # line 1's empty string stays an empty string


def test_shim_log_writes_a_row_and_no_jsonl(tmp_path, monkeypatch):
    """_log's requests.jsonl append becomes a requests row; nothing else in _log changes."""
    job = _job(tmp_path, monkeypatch)
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "tools": [], "stream": True}
    note = {"moved_system_messages": 1}
    # _log uses nothing from self; a bare Shim is enough to reach the method.
    Shim(lambda jid: None)._log(
        job, "/v1/messages", body, note, status=200, ms=120, gen_ms=100,
        usage={"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 100,
               "cache_creation_input_tokens": 0},
    )
    rows = job._conn.execute(
        "SELECT r.*, p.path AS path_name FROM requests r JOIN paths p ON p.id = r.path_id"
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["path_name"] == "/v1/messages" and row["job_id"] == job.id
    assert row["bytes"] == len(json.dumps(body))
    assert row["messages"] == 1 and row["tools"] == 0 and row["stream"] == 1
    assert row["input_tokens"] == 5 and row["output_tokens"] == 7
    assert row["cache_read_tokens"] == 100 and row["cache_creation_tokens"] == 0
    assert row["moved_system"] == 1 and row["folded_text"] is None
    assert row["http_status"] == 200 and row["ms"] == 120 and row["gen_ms"] == 100
    assert row["error"] is None and row["context_exceeded"] is None
    assert not (Path(job.log_dir) / "requests.jsonl").exists()


# the same body tests/test_shim.py::test_translate_llamacpp_error feeds translate_context_error
LLAMA_ERR = (
    b'{"error":{"code":400,"message":"request (327691 tokens) exceeds the available context size '
    b'(262144 tokens), try increasing it","type":"exceed_context_size_error",'
    b'"n_prompt_tokens":327691,"n_ctx":262144}}'
)


def test_context_exceeded_is_stored(tmp_path, monkeypatch):
    """relay stores the backend's original overflow message exactly as shim.py line 283 builds it."""
    job = _job(tmp_path, monkeypatch)
    data, original = translate_context_error(400, LLAMA_ERR)
    job.record_request({
        "t": 1793258200.5, "path": "/v1/messages", "status": 400, "ms": 40,
        "context_exceeded": original[:300],
        "error": data[:800].decode(errors="replace"),  # relay line 285: first 800 bytes of the body
    })
    row = job._conn.execute("SELECT * FROM requests").fetchone()
    assert row["context_exceeded"] == original[:300]
    assert row["context_exceeded"].startswith("request (327691 tokens)")
    assert row["http_status"] == 400


def test_db_error_does_not_break_record_request(tmp_path, monkeypatch):
    """A locked or broken database must never fail the request the shim is serving."""
    job = _job(tmp_path, monkeypatch)

    @contextmanager
    def boom(conn):
        raise sqlite3.OperationalError("database is locked")
        yield  # unreachable; makes this a context manager

    monkeypatch.setattr(jobs.db, "immediate", boom)
    job.record_request({"t": 1.0, "path": "/v1/messages", "status": 200, "ms": 1})  # returns normally
    assert "[db-error] record_request" in job.transcript_tail()
