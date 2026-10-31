"""Story 1.5a: the read-time derivations (spec "Derivations"). Each reproduces the value the
in-memory fields (note_usage) or the runner used to keep, computed from the rows instead."""

import time

import pytest

from localagents import derive, jobs


def _job(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return jobs.JobStore().new(
        task="t", model="m", endpoint="e", base_url="http://x:8080", backend="llama.cpp", host="local",
        cwd="/tmp", run_cwd="/tmp", isolation="none",
    )


def _req(job, t, path="/v1/messages", ms=1000, gen_ms=None, usage=None):
    rec = {"t": t, "path": path, "status": 200, "ms": ms}
    if gen_ms is not None:
        rec["gen_ms"] = gen_ms
    if usage is not None:
        rec["usage"] = usage
    job.record_request(rec)


def test_ctx_tokens_picks_latest_nonzero_prompt(monkeypatch, tmp_path):
    job = _job(tmp_path, monkeypatch)
    t = time.time()
    _req(job, t, usage={"input_tokens": 1000, "cache_read_input_tokens": 39000})
    _req(job, t + 1, usage={"input_tokens": 2000, "cache_creation_input_tokens": 40000})
    # a trailing request with zero/null usage must not erase the context figure (note_usage rule)
    _req(job, t + 2, usage={"output_tokens": 50})
    _req(job, t + 3, usage=None)
    # a count_tokens row with a big prompt is not a /v1/messages row: ignored
    _req(job, t + 4, path="/v1/messages/count_tokens", usage={"input_tokens": 99999})
    assert derive.ctx_tokens(job._conn, job.id) == 42000


def test_ctx_tokens_none_when_nothing_recorded(monkeypatch, tmp_path):
    job = _job(tmp_path, monkeypatch)
    _req(job, time.time(), usage={"output_tokens": 10})  # only zero-prompt rows
    assert derive.ctx_tokens(job._conn, job.id) is None
    other = _job(tmp_path, monkeypatch)  # seeded through a shared db? no: fresh store, same file
    assert derive.ctx_tokens(other._conn, other.id) is None


def test_gen_tps_replays_note_usage_ema(monkeypatch, tmp_path):
    job = _job(tmp_path, monkeypatch)
    t = time.time()
    # 100 out / 1s = 100; gen_ms 900 wins over ms 5000: 90/0.9 = 100; 150/1s = 150
    _req(job, t, ms=1000, usage={"output_tokens": 100})
    _req(job, t + 1, ms=5000, gen_ms=900, usage={"output_tokens": 90})
    _req(job, t + 2, ms=1000, usage={"output_tokens": 150})
    # rows that note_usage skips: no output, or no span (ms 0 / null)
    _req(job, t + 3, ms=500, usage={"input_tokens": 7})
    _req(job, t + 4, ms=0, usage={"output_tokens": 60})
    # a count_tokens row is not replayed
    _req(job, t + 5, path="/v1/messages/count_tokens", ms=1000, usage={"output_tokens": 1000})
    # EMA over t order: 100, then 0.5*100+0.5*100 = 100, then 0.5*100+0.5*150 = 125
    assert derive.gen_tps(job._conn, job.id) == pytest.approx(125.0)


def test_gen_tps_none_when_no_usable_rows(monkeypatch, tmp_path):
    job = _job(tmp_path, monkeypatch)
    _req(job, time.time(), ms=0, usage={"output_tokens": 10})
    assert derive.gen_tps(job._conn, job.id) is None


def test_prefill_tps_ratios_processed_prompt_over_prefill_span(monkeypatch, tmp_path):
    """Story 3.10a: a plain ratio over the job — every processed prompt token
    (input + cache_creation) over every second of prefill (ms − gen_ms) — with the page's own
    row rule (prefillRate): all four fields numbers, ms > gen_ms, processed tokens > 0."""
    job = _job(tmp_path, monkeypatch)
    t = time.time()
    # two qualifying rows: (1000 + 200) processed over 3 s of prefill, (500 + 0) over 1 s
    _req(job, t, ms=5000, gen_ms=2000,
         usage={"input_tokens": 1000, "cache_creation_input_tokens": 200})
    _req(job, t + 1, ms=4000, gen_ms=3000,
         usage={"input_tokens": 500, "cache_creation_input_tokens": 0})
    # rows the rule skips: gen_ms reaches ms; a null token number (no usage at all); a null
    # gen_ms (a non-stream request: no prefill span); zero processed tokens
    _req(job, t + 2, ms=800, gen_ms=800, usage={"input_tokens": 5000})
    _req(job, t + 3, ms=2000, gen_ms=1000)
    _req(job, t + 4, ms=3000, usage={"input_tokens": 4000})
    _req(job, t + 5, ms=3000, gen_ms=1000,
         usage={"input_tokens": 0, "cache_creation_input_tokens": 0})
    # a count_tokens call is not a /v1/messages row (the same fragment gen_tps reads)
    _req(job, t + 6, path="/v1/messages/count_tokens", ms=9000, gen_ms=1000,
         usage={"input_tokens": 9999, "cache_creation_input_tokens": 9999})
    assert derive.prefill_tps(job._conn, job.id) == pytest.approx(1700.0 / 4.0)  # 425.0


def test_prefill_tps_none_when_no_usable_rows(monkeypatch, tmp_path):
    job = _job(tmp_path, monkeypatch)
    # a non-stream request: ms but no gen_ms, so no prefill span and no ratio
    _req(job, time.time(), ms=2000, usage={"input_tokens": 1000, "cache_creation_input_tokens": 0})
    assert derive.prefill_tps(job._conn, job.id) is None


def test_files_touched_first_seen_order_dedup(monkeypatch, tmp_path):
    job = _job(tmp_path, monkeypatch)
    t = time.time()
    job.record_tool_call(t, "Edit", "u1", "/x/a.py")
    job.record_tool_call(t + 1, "Write", "u2", "/x/b.py")
    job.record_tool_call(t + 2, "Edit", "u3", "/x/a.py")       # dedup, keeps first-seen order
    job.record_tool_call(t + 3, "Bash", "u4", "ls -la")         # not an edit tool
    job.record_tool_call(t + 4, "Read", "u5", "/x/a.py")        # not an edit tool
    job.record_tool_call(t + 5, "MultiEdit", "u6", "/x/c.py")
    job.record_tool_call(t + 6, "Edit", "u7", None)             # edit tool without a path
    job.record_tool_call(t + 7, "NotebookEdit", "u8", "/x/d.ipynb")
    assert derive.files_touched(job._conn, job.id) == ["/x/a.py", "/x/b.py", "/x/c.py", "/x/d.ipynb"]


def test_tool_call_count(monkeypatch, tmp_path):
    job = _job(tmp_path, monkeypatch)
    t = time.time()
    assert derive.tool_call_count(job._conn, job.id) == 0
    job.record_tool_call(t, "Bash", "u1", "make")
    job.record_tool_call(t + 1, "Edit", "u2", "/x/a.py")
    job.record_tool_call(t + 2, "Edit", "u3", None)  # every ToolUseBlock counts, path or not
    assert derive.tool_call_count(job._conn, job.id) == 3
    other = _job(tmp_path, monkeypatch)  # counts are per job
    assert derive.tool_call_count(other._conn, other.id) == 0
