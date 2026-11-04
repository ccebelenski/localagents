"""Story "router 7: capacity in sessions, throughput curve, speed samples"
(router plan 2026-11-02, task 7; spec §7 and §6's window filter / bottleneck rule).

The from_probe tests build the probe dict the way ``Registry.probe`` does — the vLLM
``load`` keys from the real ``parse_metrics`` over the captured /metrics text, the llama.cpp
``slots`` from the two captured JSON files (count of dict entries, busy = is_processing) —
so the capacity model is tested against what the probe actually hands it, not against a
typed-out string that could drift from registry.py.
"""

import json
from pathlib import Path

import pytest

from localagents.registry import Endpoint, parse_metrics
from localagents.router.capacity import (CONTEXT_LOAD_TOKENS, Capacity, Curve,
                                         context_load_tokens, from_probe, load_capacity,
                                         prior_curve, save_capacity)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "router"

# the served id and window the captured vLLM box reports (the cache_config_info line's
# model_name label; 307,712 against the pool's 307,852 tokens: spec §7's one-session box)
SERVED = "DeepSeek-V4.1-Flash-1017"
WINDOW = 307_712


def _vllm_probe(mt: dict, served_id: str) -> dict:
    """The probe dict ``Registry.probe`` returns for vLLM: ``/v1/models``' max_model_len as
    ``context``, the ``/metrics`` text parsed into ``load`` (_probe_context's vLLM block)."""
    load = {}
    for key in ("requests_running", "requests_waiting"):
        if key in mt:
            load[key] = int(mt[key])
    if "kv_cache_usage" in mt:
        load["kv_cache_usage"] = round(mt["kv_cache_usage"], 3)
    for key in ("kv_cache_size_tokens", "kv_cache_max_concurrency"):
        if key in mt:
            load[key] = mt[key]
    return {"up": True, "models": [served_id], "context": {served_id: WINDOW}, "load": load}


def _llama_probe() -> tuple[dict, str]:
    """The probe dict ``Registry.probe`` returns for llama.cpp from the two captures: /props'
    per-slot n_ctx as context, /slots' occupancy as ``slots``."""
    props = json.loads((FIXTURES / "llamacpp_props.json").read_text())
    body = json.loads((FIXTURES / "llamacpp_slots.json").read_text())
    slots = [x for x in body if isinstance(x, dict)]
    busy = sum(1 for x in slots if x.get("is_processing"))
    served = props["model_alias"]
    probe = {
        "up": True,
        "models": [served],
        "context": {served: props["default_generation_settings"]["n_ctx"]},
        "slots": {"total": len(slots), "busy": busy, "free": len(slots) - busy},
    }
    return probe, served


def _vllm_endpoint(**kw) -> Endpoint:
    return Endpoint(name="deepseek", base_url="http://d:8000", backend="vllm", **kw)


def _llama_endpoint(**kw) -> Endpoint:
    return Endpoint(name="box", base_url="http://b:8080", backend="llama.cpp", **kw)


def test_pooled_from_captured_vllm_is_one_session():
    mt = parse_metrics((FIXTURES / "vllm_metrics_pooled.txt").read_text())
    probe = _vllm_probe(mt, SERVED)
    cap = from_probe(_vllm_endpoint(), probe, SERVED, prior_curve("vllm"))
    assert cap.kind == "pooled"
    # spec §7: "the operator's pool, 307,852 tokens for a 307,712 window, holds exactly one"
    assert cap.sessions_capacity == 1
    assert cap.window == WINDOW
    assert cap.pool_tokens == 307_852
    assert cap.requests_running == 0
    assert cap.curve.state == "prior"


def test_partitioned_from_captured_llamacpp_is_two_sessions():
    probe, served = _llama_probe()
    cap = from_probe(_llama_endpoint(), probe, served, prior_curve("llamacpp"))
    assert cap.kind == "partitioned"
    assert cap.sessions_capacity == 2  # two slots, one session each (spec §7)
    assert cap.window == 262_144
    assert cap.pool_tokens is None
    assert cap.requests_running == 0  # both slots idle in the capture


def test_unknown_is_one_session():
    """Nothing pooled, no slots reported: "else one session until probed" (spec §7)."""
    probe = {"up": True, "models": ["m"], "context": {"m": 4_096}}
    cap = from_probe(Endpoint(name="odd", base_url="http://o:1"), probe, "m", prior_curve("other"))
    assert cap.kind == "unknown"
    assert cap.sessions_capacity == 1


def test_down_endpoint_admits_nothing():
    probe = {"up": False, "models": [], "error": "box is unreachable (EHOSTUNREACH)"}
    cap = from_probe(_llama_endpoint(), probe, "m", prior_curve("llamacpp"))
    assert cap.kind == "unknown"
    assert cap.sessions_capacity == 0
    assert not cap.admits(0)


def test_admits_counts_our_sessions_not_live_requests():
    """§7's premise: a session running tests for ten minutes holds no slot and reads as free;
    admission is ours-entries + foreign, never the live request count."""
    probe, served = _llama_probe()
    cap = from_probe(_llama_endpoint(), probe, served, prior_curve("llamacpp"))
    assert cap.requests_running == 0  # idle slots while our sessions are mid-tool-run
    assert cap.admits(1)   # one of ours running: one slot still free of two
    assert not cap.admits(2)


def test_foreign_requests_reduce_capacity():
    """§7's occupancy: foreign = max(0, requests_running_live − our sessions)."""
    held = Capacity("pooled", 1, window=WINDOW, pool_tokens=307_852, requests_running=1)
    assert not held.admits(0)  # the one session's pool is taken by someone else's request
    ours = Capacity("partitioned", 3, requests_running=2)
    assert ours.admits(2)      # the two live requests are our two sessions: foreign 0, one free
    assert not ours.admits(3)  # ours alone fill the lane


def test_max_concurrent_caps():
    """§7: "max_concurrent in the entry caps below any of these" — and never raises above."""
    probe, served = _llama_probe()
    cap = from_probe(_llama_endpoint(max_concurrent=1), probe, served, prior_curve("llamacpp"))
    assert cap.sessions_capacity == 1
    assert cap.max_concurrent == 1
    loose = from_probe(_llama_endpoint(max_concurrent=4), probe, served, prior_curve("llamacpp"))
    assert loose.sessions_capacity == 2


def test_prior_llamacpp_bottlenecks_at_two_vllm_does_not():
    """§6: aggregate at k below 75% of the aggregate at 1 for ANY k in 2..n — llama.cpp
    bottlenecks at 2 (2*0.35 = 0.7 < 0.75) and stays background-only above it, so the third
    session on a three-slot box is not admitted for normal work while the second was
    background only. vLLM's aggregate rises (1.7, 2.25) and never crosses the line."""
    ll = prior_curve("llamacpp")
    assert ll.points == {1: 1.0, 2: 0.35, 3: 0.25}
    assert ll.state == "prior"
    assert not ll.bottleneck_at(1)  # a single session is never a bottleneck
    assert ll.bottleneck_at(2)      # 2*0.35 = 0.7 < 0.75
    assert ll.bottleneck_at(3)      # from 2 on: the rule already held at 2
    assert ll.bottleneck_at(4)      # the far end reads 0.25: 4*0.25 = 1.0, still 2 holds
    vl = prior_curve("vllm")
    assert vl.points == {1: 1.0, 2: 0.85, 3: 0.75}
    assert not vl.bottleneck_at(1) and not vl.bottleneck_at(2) and not vl.bottleneck_at(3)
    assert not vl.bottleneck_at(4)  # 4*0.75 = 3.0 on the far end; no k in 2..4 crosses
    assert prior_curve("other").points == {1: 1.0, 2: 0.5}
    # the bottleneck rule read as admission: the n-th concurrent session, §6
    ll_cap = Capacity("partitioned", 2, curve=ll)
    assert ll_cap.bottleneck_slot(1)   # the next session would be the 2nd: background only
    assert not ll_cap.bottleneck_slot(0)
    assert not Capacity("pooled", 3, curve=vl).bottleneck_slot(1)


def test_bottleneck_slot_counts_foreign_sessions():
    """The session this job would become is the same occupancy ``admits`` counts — ours +
    foreign + 1, foreign = max(0, requests_running - ours) — so a box holding one foreign
    session admits our normal job as the session AFTER it, not the first. The old ours-only
    arithmetic read ours + 1 and checked no bottleneck at all."""
    ll = prior_curve("llamacpp")
    cap = Capacity("partitioned", 3, requests_running=1, curve=ll)
    assert cap.admits(0)                 # 0 ours + 1 foreign = 1 of 3: room for the job
    assert cap.bottleneck_slot(0)        # it enters as the 2nd session: background only
    # a curve that bottlenecks at 3 only (2*0.85 = 1.7 clears the line, 3*0.2 does not):
    # one of ours plus one foreign, the job enters as the 3rd session — the old
    # arithmetic read the 2nd and said no bottleneck
    mid = Curve({1: 1.0, 2: 0.85, 3: 0.2}, "refined", 3)
    mixed = Capacity("pooled", 4, requests_running=2, curve=mid)
    assert mixed.admits(1)               # 1 ours + 1 foreign = 2 of 4
    assert mixed.bottleneck_slot(1)      # the 3rd session, not the 2nd
    # every live request ours: the foreign term is 0 and the reading is what it always was
    ours = Capacity("partitioned", 3, requests_running=2, curve=ll)
    assert ours.bottleneck_slot(2)       # the 3rd session: the rule held from 2 on
    assert not Capacity("partitioned", 3, requests_running=0, curve=ll).bottleneck_slot(0)


def test_refine_from_samples():
    """§7: median generate_tps per concurrency, normalised to the concurrency-1 median,
    MERGED onto the prior (a concurrency with no sample yet keeps the prior's point);
    without a concurrency-1 point there is no baseline and the curve stands."""
    prior = prior_curve("vllm")
    refined = prior.refine([(1, 40.0), (2, 30.0), (1, 60.0), (2, 34.0)])
    assert refined.state == "refined"
    assert refined.n == 4  # len(samples): the endpoint_capacity.points column
    # median 50 and 32; 32/50 at 2, and 3 has no samples so the prior's 0.75 stays
    assert refined.points == pytest.approx({1: 1.0, 2: 0.64, 3: 0.75})
    assert prior.points == {1: 1.0, 2: 0.85, 3: 0.75} and prior.state == "prior"
    same = prior.refine([(2, 30.0)])
    assert same.points == prior.points and same.state == "prior" and same.n == 0


def test_refine_keeps_the_prior_where_no_samples_exist():
    """The two-slot llama.cpp box whose first jobs all ran alone: samples at 1 refine 1,
    but the prior's 2 and 3 stay — dropping them would turn bottleneck_at(2) False and
    admit the second concurrent session at normal priority where the prior said background
    only. §7: the curve is "seeded by a backend-family prior ... refined by every job's
    observed speed", refined not replaced."""
    refined = prior_curve("llamacpp").refine([(1, 40.0), (1, 42.0)])
    assert refined.points == pytest.approx({1: 1.0, 2: 0.35, 3: 0.25})
    assert refined.n == 2 and refined.state == "refined"
    assert refined.bottleneck_at(2)  # 2 * 0.35 = 0.7 < 0.75, exactly as the prior said


def test_context_load_bands_and_files():
    """The window filter (§6 step 1); the band table is the calibration spec §7 row."""
    assert CONTEXT_LOAD_TOKENS == {0: 8_000, 1: 16_000, 2: 32_000, 3: 64_000, 4: 128_000}
    assert context_load_tokens(0, "a brief") == 8_000
    assert context_load_tokens(4, "a brief") == 128_000
    long_brief = "x" * 36_000  # ~9,000 tokens at the four-chars-per-token reading
    assert context_load_tokens(0, long_brief) == 9_000    # the file's own size beats a low band
    assert context_load_tokens(1, long_brief) == 16_000   # but not a band above it
    assert context_load_tokens(None, long_brief) == 9_000  # undetermined: the text term alone
    assert context_load_tokens(None, "abcde") == 2         # ceil(len / 4)


def test_save_and_load_capacity_round_trip(conn):
    # endpoint_capacity.endpoint_id references endpoints(id): the FK parent row first
    conn.execute("INSERT INTO endpoints (name, base_url, backend, host)"
                 " VALUES ('ai1', 'http://ai1:8000', 'vllm', 'ai1')")
    eid = conn.execute("SELECT id FROM endpoints").fetchone()[0]
    curve = prior_curve("vllm").refine([(1, 50.0), (2, 30.0)])
    save_capacity(conn, eid, "pooled", curve)
    row = conn.execute(
        "SELECT kind, curve, state, points FROM endpoint_capacity WHERE endpoint_id = ?", (eid,)
    ).fetchone()
    assert row["kind"] == "pooled"
    # string keys in JSON; 3 carries the prior's value through the merge (no sample there)
    assert json.loads(row["curve"]) == {"points": {"1": 1.0, "2": 0.6, "3": 0.75}}
    assert row["state"] == "refined"
    assert row["points"] == 2  # the column is the curve's sample count (schema comment)
    got = load_capacity(conn, eid)
    assert got.points == {1: 1.0, 2: 0.6, 3: 0.75} and got.state == "refined" and got.n == 2
    # upsert: a later save replaces the row rather than adding one
    save_capacity(conn, eid, "pooled", prior_curve("vllm"))
    assert load_capacity(conn, eid).state == "prior"
    assert conn.execute("SELECT COUNT(*) c FROM endpoint_capacity").fetchone()["c"] == 1
    assert load_capacity(conn, eid + 100) is None


def test_predict_reads_a_gap_from_the_nearest_lower_point():
    """§7: the curve never refuses. One learned at 1 and 3 only still answers at 2 — the
    nearest LOWER key's value (the far end keeps the largest key's value as before)."""
    curve = Curve({1: 1.0, 3: 0.4}, "refined", 2)  # a stored curve whose 2-sample is gone
    assert curve.predict(2) == 1.0   # the gap reads from 1, not a KeyError
    assert not curve.bottleneck_at(2)  # 2 * 1.0 is no bottleneck at the read value
    assert curve.predict(3) == 0.4


def test_bottleneck_beyond_the_curve_uses_the_last_point():
    """A curve learned at concurrency 2 still answers at 8: the largest key's value reads on,
    and 'bottlenecks at n' means from n on — once the rule has held (at 2 here, 0.2 < 0.75),
    higher n stay True even though n * value reads back above the line."""
    curve = Curve({1: 1.0, 2: 0.1}, "refined", 5)
    assert curve.predict(2) == 0.1 and curve.predict(8) == 0.1
    assert curve.bottleneck_at(7) and curve.bottleneck_at(8)  # 2 holds in 2..7 and 2..8
    assert curve.predict(0) == 1.0     # zero occupancy reads as one


def test_bottleneck_at_one_or_below_is_false():
    """A single session is never a bottleneck, however bad the curve reads below 1."""
    assert not Curve({1: 0.1, 2: 0.05}, "refined", 3).bottleneck_at(1)
    assert not Curve({1: 0.1, 2: 0.05}, "refined", 3).bottleneck_at(0)
    assert not prior_curve("llamacpp").bottleneck_at(-5)
