"""Story 3.10i: a backend that reports no cache fields still gets a prefill rate (the operator,
EPICS 3.10i: "I notice the vllm lane never reports token speeds, always shows the prompt as new
not cached."). vLLM's Anthropic-shaped usage carries NO cache fields — a live probe of dgx1-a's
stream showed ``usage: {input_tokens: 38, output_tokens: 8}`` and nothing else — so every vLLM
row has cache_read/cache_creation NULL, and the old rule (all four of input, cache_creation, ms,
gen_ms numbers) computed nothing for them.

Rule now: a missing cache_creation counts as 0 when ``input`` is a number. The processed tokens
are then ``input`` alone — an UPPER bound, since the engine's own prefix cache is invisible —
and the cell's title says so. Same rule in derive.prefill_tps and app.js prefillRate; the title
lives on the prefill cell in requestsTable (the numerator when the backend does report cache,
the upper-bound sentence when it reports neither cache field).

Tests: derive over hand rows (test_derive.py's shapes) and static pins over app.js (ruling
(o)); the page's arithmetic itself is checked by the smoke (t/REPORT.md) with the vLLM-shaped
and Strata-shaped rows."""

import re
import time
from pathlib import Path

import pytest

from localagents import derive, jobs

STATIC = Path(__file__).resolve().parent.parent / "src" / "localagents" / "monitor" / "static"


def _app_js():
    return (STATIC / "app.js").read_text()


def _job(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return jobs.JobStore().new(
        task="t", model="m", endpoint="e", base_url="http://x:8000", backend="vllm", host="dgx1",
        cwd="/tmp", run_cwd="/tmp", isolation="none",
    )


def _req(job, t, path="/v1/messages", ms=1000, gen_ms=None, usage=None):
    rec = {"t": t, "path": path, "status": 200, "ms": ms}
    if gen_ms is not None:
        rec["gen_ms"] = gen_ms
    if usage is not None:
        rec["usage"] = usage
    job.record_request(rec)


def test_prefill_tps_null_cache_counts_as_zero(monkeypatch, tmp_path):
    """The vLLM row (the live probe's shape: usage carries only input and output): input alone
    over the prefill span. 62750 / 1.218 = 51,518.9 (the brief's 51,519.7 misdivides) — an
    upper bound, the figure the cell prints with the "cache not reported" title."""
    job = _job(tmp_path, monkeypatch)
    _req(job, time.time(), ms=5264, gen_ms=4046, usage={"input_tokens": 62750, "output_tokens": 8})
    assert derive.prefill_tps(job._conn, job.id) == pytest.approx(62750 / 1.218)


def test_prefill_tps_still_skips_a_null_input(monkeypatch, tmp_path):
    """The relaxation is cache_creation alone: `input` must still be a number (no usage at all,
    or only cache figures, keeps the row out — there is no figure to name)."""
    job = _job(tmp_path, monkeypatch)
    t = time.time()
    _req(job, t, ms=5264, gen_ms=4046)  # no usage at all
    _req(job, t + 1, ms=5264, gen_ms=4046, usage={"cache_creation_input_tokens": 62750})
    assert derive.prefill_tps(job._conn, job.id) is None


def test_prefill_tps_cache_reported_rows_unchanged(monkeypatch, tmp_path):
    """The Strata-shaped rows of story 3.10a's test keep their exact figure: input +
    cache_creation over ms − gen_ms, summed as a plain ratio over the job."""
    job = _job(tmp_path, monkeypatch)
    t = time.time()
    _req(job, t, ms=5000, gen_ms=2000,
         usage={"input_tokens": 1000, "cache_creation_input_tokens": 200})
    _req(job, t + 1, ms=4000, gen_ms=3000,
         usage={"input_tokens": 500, "cache_creation_input_tokens": 0})
    assert derive.prefill_tps(job._conn, job.id) == pytest.approx(1700.0 / 4.0)  # 425.0


def test_prefill_tps_mixed_job_sums_both_shapes(monkeypatch, tmp_path):
    """One Strata row and one vLLM row in the same job: both join, tokens and seconds sum —
    (1200 + 62750) over (3 + 1.218) seconds."""
    job = _job(tmp_path, monkeypatch)
    t = time.time()
    _req(job, t, ms=5000, gen_ms=2000,
         usage={"input_tokens": 1000, "cache_creation_input_tokens": 200})
    _req(job, t + 1, ms=5264, gen_ms=4046, usage={"input_tokens": 62750})
    assert derive.prefill_tps(job._conn, job.id) == pytest.approx(63950 / 4.218)


def test_prefill_rate_reads_cache_creation_as_zero_when_absent():
    """The same rule on the page: prefillRate no longer returns null for a missing
    cache_creation — the row qualifies on input, ms, gen_ms (and ms > gen_ms), with the cache
    figure 0 when it is not a number."""
    body = re.search(r"function prefillRate\(req\) \{(.*?)\n  \}", _app_js(), re.S).group(1)
    assert 'var cc = typeof req.tokens.cache_creation === "number" ? req.tokens.cache_creation : 0;' in body
    # the old null-out of a missing cache_creation is gone from this helper
    assert 'typeof req.tokens.cache_creation !== "number"' not in body
    assert 'if (typeof req.tokens.input !== "number") return null;' in body


def test_the_prefill_cell_title_names_the_numerator_or_the_missing_cache():
    """The title rides on the prefill cell in requestsTable: the numerator sentence when the
    backend reports cache ("3,001 new tokens over 0.5 s"); the upper-bound sentence when it
    reports NEITHER cache field (vLLM's Anthropic shape until --enable-prompt-tokens-details)."""
    js = _app_js()
    assert '"cache not reported by this backend: the rate counts the whole input"' in js
    assert '" new tokens over "' in js
    # the test that picks the sentence: both cache figures not numbers
    assert 'typeof cacheCreation !== "number" && typeof cacheRead !== "number"' in js
    # the cell carries the title
    assert 'h("td", { class: "n", title: prefillTitle, text: prefill == null ? "" : fmt.speed(prefill) })' in js
