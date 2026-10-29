"""Story 3.1 item 2: the monitor's read model (monitor/read.py) over the seeded world (story 2.4):
ai1 live with an hour of 5 s probes, dgx1-a down since NOW-20 min (a refusal), llamacpp dormant
since a week ago; a finished job (112 requests, 142 tool calls) and a running one (40, 50), both
on ai1; four endpoint events. The JSON shapes are the contract of spec §5a."""
import json
import math

import pytest

from localagents import db
from localagents.jobs import state_dir
from localagents.monitor import read, rows, seed
from localagents.registry import Registry

from monitor_seeded import FX, NOW, endpoint_id, registry, seed_file, write_config

WINDOW_S = 24 * 3600.0
HOUR = 3600.0


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "seeded.db"
    seed_file(path)
    conn = db.open_db(path)
    yield conn, registry(tmp_path)
    conn.close()


def _host(ov, name):
    return next(h for h in ov["hosts"] if h["name"] == name)


def _ep(ov, name):
    return next(e for h in ov["hosts"] for e in h["endpoints"] if e["name"] == name)


# --- overview ------------------------------------------------------------------------------


def test_overview_top_level_keys(world):
    conn, reg = world
    ov = read.overview(conn, reg, NOW, WINDOW_S)
    assert set(ov) == {"now", "window_s", "hosts", "jobs", "events"}
    assert ov["now"] == NOW and ov["window_s"] == WINDOW_S
    # json-serialisable as is: it is the SSE snapshot's body
    json.dumps(ov)


def test_overview_hosts_in_config_order_with_their_states(world):
    conn, reg = world
    ov = read.overview(conn, reg, NOW, WINDOW_S)
    assert [h["name"] for h in ov["hosts"]] == ["ai1", "dgx1", "local"]
    assert [[e["name"] for e in h["endpoints"]] for h in ov["hosts"]] == [["ai1"], ["dgx1-a"], ["llamacpp"]]
    assert [e["state"] for h in ov["hosts"] for e in h["endpoints"]] == ["live", "down", "dormant"]
    # a refusal proves the box is up (the seed's node test): every node is up
    assert [h["state"] for h in ov["hosts"]] == ["up", "up", "up"]
    assert [h["dormant"] for h in ov["hosts"]] == [False, False, True]
    assert set(ov["hosts"][0]) == {"name", "state", "dormant", "endpoints"}


ENDPOINT_KEYS = {
    "name", "backend", "base_url", "state", "last_seen", "model", "context", "slots", "requests",
    "kv_cache_usage", "latency_ms", "since", "error", "dropped_job", "rates", "gen_tps_live",
    "hardware", "jobs_running",
}


def test_the_live_endpoint_carries_its_gauges_rates_and_hardware(world):
    conn, reg = world
    ai1 = _ep(read.overview(conn, reg, NOW, WINDOW_S), "ai1")
    assert set(ai1) == ENDPOINT_KEYS
    assert ai1["backend"] == "strata" and ai1["base_url"] == "http://10.0.0.50:8080"
    assert ai1["last_seen"] == NOW
    assert ai1["model"] == "qwen3.8-flash-next-iq3_s"
    assert ai1["context"] == seed.AI1_CONTEXT
    assert ai1["slots"] == {"total": 1, "busy": 1}
    assert ai1["requests"] == {"running": 1, "waiting": 0}
    assert ai1["latency_ms"] == seed.LATENCY_MS
    assert ai1["since"] == NOW - 3 * 24 * 3600.0
    assert ai1["error"] is None and ai1["dropped_job"] is None
    # the seed test's pinned constant rate (test_ai1_rates_are_constant_and_non_zero)
    r = ai1["rates"]
    assert set(r) == {"prompt_tps", "gen_tps", "cached_tps", "spec_acceptance", "reason"}
    assert (r["prompt_tps"], r["gen_tps"], r["cached_tps"], r["spec_acceptance"]) == (1.2, 0.6, 134.8, 0.822)
    assert r["reason"] is None
    assert ai1["gen_tps_live"] is not None
    assert set(ai1["hardware"]) == set(rows.HARDWARE_COLUMNS)
    assert ai1["hardware"]["gpu_util"] == pytest.approx(59.5 - 39.5 * math.cos(2 * math.pi * 719 / 720))
    assert ai1["jobs_running"] == 1


def test_the_down_endpoint_names_its_sentence_and_no_dropped_job(world):
    conn, reg = world
    dgx = _ep(read.overview(conn, reg, NOW, WINDOW_S), "dgx1-a")
    assert dgx["state"] == "down"
    assert dgx["error"] == seed._refused_sentence("http://10.0.0.70:8000")
    assert "refused" in dgx["error"] or "nothing" in dgx["error"]
    assert dgx["dropped_job"] is None  # the seed's jobs ran on ai1
    assert dgx["last_seen"] == pytest.approx(NOW - 20 * 60.0)
    # a down probe has no gauges, no counters, no hardware
    assert dgx["model"] is None and dgx["slots"] is None and dgx["requests"] is None
    assert dgx["hardware"] is None
    assert dgx["rates"]["prompt_tps"] is None and dgx["rates"]["reason"] is not None
    assert dgx["jobs_running"] == 0


def test_the_dormant_endpoint_is_last_seen_a_week_ago(world):
    conn, reg = world
    lcpp = _ep(read.overview(conn, reg, NOW, WINDOW_S), "llamacpp")
    assert lcpp["state"] == "dormant"
    assert lcpp["last_seen"] == pytest.approx(NOW - seed.LCPP_UP_AGO_S)
    assert lcpp["dropped_job"] is None  # named only for a down-recent endpoint


def test_dropped_job_names_the_job_running_when_the_endpoint_dropped(world):
    """ai1 goes down at NOW+5 while the seed's running job is still running: the overview at
    NOW+10 names that job."""
    conn, reg = world
    ai1 = endpoint_id(conn, "ai1")
    with db.immediate(conn):  # rows.write went with story 2.2a: a tick's insert in its own transaction
        rows.insert(conn, rows.ProbeRows(
            {**dict.fromkeys(rows.PROBE_COLUMNS), "t": NOW + 5, "endpoint_id": ai1, "up": 0, "host": "up",
             "error": seed._refused_sentence("http://10.0.0.50:8080")}, None, None))
    running = conn.execute("SELECT name FROM jobs WHERE status = 'running'").fetchone()[0]
    ep = _ep(read.overview(conn, reg, NOW + 10, WINDOW_S), "ai1")
    assert ep["state"] == "down"
    assert ep["dropped_job"] == running


def test_overview_running_jobs_carry_the_status_line_entry(world):
    conn, reg = world
    ov = read.overview(conn, reg, NOW, WINDOW_S)
    assert len(ov["jobs"]) == 1
    j = ov["jobs"][0]
    row = conn.execute("SELECT * FROM jobs WHERE status = 'running'").fetchone()
    assert j["id"] == row["id"] and j["name"] == row["name"]
    assert j["status"] == "running"
    assert j["endpoint"] == "ai1" and j["model"] == "qwen3.8-flash-next-iq3_s"
    assert j["tool_calls"] == 50 and j["num_turns"] == 40
    assert j["elapsed_s"] == pytest.approx(300.0)
    assert j["started_at"] == pytest.approx(NOW - 300.0) and j["finished_at"] is None
    assert j["errors"] == sum(tc["is_error"] or 0 for tc in seed._load(FX).job_tool_calls[:50])
    assert isinstance(j["last_tool"], str)
    assert j["compactions"] == 0
    assert isinstance(j["files"], int)
    assert j["ctx_pct"] is None or 0 <= j["ctx_pct"] <= 100
    assert j["server_live"] is True  # the seed's server row: a live pid, a heartbeat at NOW
    assert set(j) == {
        "id", "name", "model", "endpoint", "status", "started_at", "finished_at", "elapsed_s",
        "num_turns", "tool_calls", "errors", "last_tool", "last_activity", "ctx_pct", "ctx_tokens",
        "context", "gen_tps", "compactions", "files", "owner", "server_live",
    }


def test_overview_events_are_the_newest_twenty_with_endpoint_names(world):
    conn, reg = world
    ev = read.overview(conn, reg, NOW, WINDOW_S)["events"]
    assert len(ev) == 4
    assert [e["t"] for e in ev] == sorted((e["t"] for e in ev), reverse=True)
    assert [(e["endpoint"], e["kind"]) for e in ev] == [
        ("dgx1-a", "down"), ("ai1", "restarted"), ("ai1", "up"), ("llamacpp", "down")]
    assert set(ev[0]) == {"t", "endpoint", "kind", "detail"}
    # capped at twenty
    ai1 = endpoint_id(conn, "ai1")
    with db.immediate(conn):
        for k in range(30):
            conn.execute("INSERT INTO endpoint_events (t, endpoint_id, kind) VALUES (?, ?, 'up')", (NOW - k, ai1))
    assert len(read.overview(conn, reg, NOW, WINDOW_S)["events"]) == 20


def test_an_endpoint_in_the_database_but_not_in_the_config_is_ignored(world, tmp_path):
    conn, _ = world
    # drop llamacpp from the config: its rows stay in the database, the overview ignores them
    cfg = write_config(tmp_path / "two.yaml")
    cfg.write_text(cfg.read_text().split("  llamacpp:")[0])
    reg = Registry.load(cfg)
    ov = read.overview(conn, reg, NOW, WINDOW_S)
    assert [h["name"] for h in ov["hosts"]] == ["ai1", "dgx1"]
    assert "llamacpp" not in [e["endpoint"] for e in ov["events"]]


def test_a_configured_endpoint_with_no_rows_is_dormant_cold_start(world, tmp_path):
    conn, _ = world
    cfg = write_config(tmp_path / "new.yaml",
                       "  fresh:\n    base_url: http://10.0.0.99:8080\n    backend: llama.cpp\n    host: ai1\n")
    ov = read.overview(conn, Registry.load(cfg), NOW, WINDOW_S)
    host = _host(ov, "ai1")
    assert [e["name"] for e in host["endpoints"]] == ["ai1", "fresh"]
    fresh = host["endpoints"][1]
    assert fresh["state"] == "dormant" and fresh["last_seen"] is None
    assert fresh["rates"] == {"prompt_tps": None, "gen_tps": None, "cached_tps": None,
                              "spec_acceptance": None, "reason": "no probes"}
    # read-only: the overview created no endpoints row for it
    assert conn.execute("SELECT COUNT(*) FROM endpoints WHERE name = 'fresh'").fetchone()[0] == 0


# --- endpoint history ----------------------------------------------------------------------


def _true_rate(key):
    """ai1's real per-second rate: the busy ai2 pair's delta over the seeded hour (719 intervals
    of 5 s). The counters are interpolated, so they are fractional; ``metrics_delta`` truncates
    each delta to an int, so a rate over k intervals is within 1/(5k) t/s below this."""
    w = seed._load(FX)
    return (w.ai1_counters_2[key] - w.ai1_counters_1[key]) / (719 * 5.0)


def _within_truncation(values, key, k):
    true = _true_rate(key)
    return all(true - 1.0 / (5 * k) - 1e-9 <= v <= true + 1e-9 for v in values)


SERIES = ("prompt_tps", "gen_tps", "cache_hit", "slots_busy", "requests_running", "requests_waiting",
          "kv_cache_usage", "gpu_util", "gpu_mem_used", "gpu_temp", "gpu_power")


def test_ai1_history_over_an_hour_is_downsampled_to_at_most_600(world):
    conn, _ = world
    ai1 = endpoint_id(conn, "ai1")
    n = conn.execute("SELECT COUNT(*) FROM probes WHERE endpoint_id = ? AND t >= ?", (ai1, NOW - HOUR)).fetchone()[0]
    assert n == 720
    h = read.endpoint_history(conn, ai1, NOW - HOUR, NOW)
    assert 0 < len(h["t"]) <= 600
    assert len(h["t"]) == 360 and h["step"] == 2
    assert h["t"][-1] == NOW  # the newest probe is always kept
    assert h["t"] == sorted(h["t"])
    for k in SERIES:
        if k == "kv_cache_usage":
            assert k not in h  # strata reports no KV fill: absent, not a column of nulls
            continue
        assert k in h, k
        assert len(h[k]) == len(h["t"]), k
    assert h["gpu_util"][-1] == pytest.approx(59.5 - 39.5 * math.cos(2 * math.pi * 719 / 720))
    # the counters are linear: every point's rate (over two intervals) is the real rate,
    # less metrics_delta's int truncation of the delta
    assert _within_truncation(h["prompt_tps"][1:], "prompt_tokens", 2)
    assert _within_truncation(h["gen_tps"][1:], "generated_tokens", 2)
    assert set(h["prompt_tps"][1:]) == {1.2} and set(h["gen_tps"][1:]) == {0.7}
    assert h["cache_hit"][-1] == pytest.approx(_true_rate("prompt_tokens_cached")
                                               / (_true_rate("prompt_tokens_cached") + _true_rate("prompt_tokens")),
                                               abs=0.001)
    assert h["slots_busy"][-1] == 1 and h["slots_busy"][0] == 0


def test_history_without_downsampling_keeps_every_probe(world):
    conn, _ = world
    ai1 = endpoint_id(conn, "ai1")
    h = read.endpoint_history(conn, ai1, NOW - HOUR, NOW, max_points=1000)
    assert len(h["t"]) == 720 and h["step"] == 1
    # the first in-range probe is ai1's first ever: no previous counters, no rate
    assert h["prompt_tps"][0] is None
    # one interval each: the seed test's pinned figures, every interval
    assert set(h["prompt_tps"][1:]) == {1.2} and set(h["gen_tps"][1:]) == {0.6}


def test_history_downsampling_takes_every_kth_probe_ending_at_the_newest(world):
    conn, _ = world
    ai1 = endpoint_id(conn, "ai1")
    h = read.endpoint_history(conn, ai1, NOW - HOUR, NOW, max_points=100)
    assert h["step"] == 8 and len(h["t"]) == 90
    assert h["t"][-1] == NOW and h["t"][-2] == NOW - 8 * 5.0
    # the rate over a sampled interval (eight raw intervals) is the real rate within truncation
    assert _within_truncation(h["prompt_tps"][1:], "prompt_tokens", 8)
    assert _within_truncation(h["gen_tps"][1:], "generated_tokens", 8)


def test_ai1_history_events_are_its_two(world):
    conn, _ = world
    h = read.endpoint_history(conn, endpoint_id(conn, "ai1"), NOW - HOUR, NOW)
    assert [(e["kind"], e["t"]) for e in h["events"]] == [("up", NOW - HOUR), ("restarted", NOW - 1800.0)]
    assert set(h["events"][0]) == {"t", "kind", "detail"}


def test_ai1_history_jobs_are_the_two_seeded(world):
    conn, _ = world
    h = read.endpoint_history(conn, endpoint_id(conn, "ai1"), NOW - HOUR, NOW)
    assert sorted(j["status"] for j in h["jobs"]) == ["running", "succeeded"]
    assert set(h["jobs"][0]) == {"name", "id", "status", "started_at", "finished_at", "model"}
    assert all(j["model"] == "qwen3.8-flash-next-iq3_s" for j in h["jobs"])


def test_dgx_history_has_no_hardware_keys_and_nulls_while_down(world):
    conn, _ = world
    h = read.endpoint_history(conn, endpoint_id(conn, "dgx1-a"), NOW - HOUR, NOW)
    for k in ("gpu_util", "gpu_mem_used", "gpu_temp", "gpu_power", "slots_busy"):
        assert k not in h, k  # spec §5: a tile with no source is absent, not empty
    for k in ("prompt_tps", "gen_tps", "kv_cache_usage", "requests_running"):
        assert k in h and len(h[k]) == len(h["t"]), k
    # the last 20 min are down probes: no gauges and no rates (a gap)
    assert h["kv_cache_usage"][-1] is None and h["prompt_tps"][-1] is None
    assert [e["kind"] for e in h["events"]] == ["down"]
    assert h["jobs"] == []


def test_dormant_history_over_fifteen_minutes_is_times_only(world):
    conn, _ = world
    h = read.endpoint_history(conn, endpoint_id(conn, "llamacpp"), NOW - 900.0, NOW)
    assert len(h["t"]) == 16  # down probes every 60 s, both ends included
    assert not any(k in h for k in SERIES)


def test_a_reset_interval_has_no_rate(world):
    """A probe at NOW+5 whose counters went backwards (ai1 restarted): that interval's rates are
    null, never a cliff (spec §6)."""
    conn, _ = world
    ai1 = endpoint_id(conn, "ai1")
    last = conn.execute(
        "SELECT c.* FROM probe_counters c JOIN probes p ON p.id = c.probe_id WHERE p.endpoint_id = ?"
        " ORDER BY p.t DESC LIMIT 1", (ai1,)).fetchone()
    counters = {k: (last[k] / 2 if last[k] is not None else None) for k in rows.COUNTER_COLUMNS}
    probe = conn.execute("SELECT * FROM probes WHERE endpoint_id = ? ORDER BY t DESC LIMIT 1", (ai1,)).fetchone()
    new = {k: probe[k] for k in rows.PROBE_COLUMNS}
    new["t"] = NOW + 5
    with db.immediate(conn):
        rows.insert(conn, rows.ProbeRows(new, counters, None))
    h = read.endpoint_history(conn, ai1, NOW + 5 - HOUR, NOW + 5, max_points=1000)
    assert h["t"][-1] == NOW + 5
    assert h["prompt_tps"][-1] is None and h["gen_tps"][-1] is None and h["cache_hit"][-1] is None
    assert h["prompt_tps"][-2] == 1.2
    # the overview's rates name the reset
    ep = _ep(read.overview(conn, registry_from(conn), NOW + 5, WINDOW_S), "ai1")
    assert ep["rates"]["prompt_tps"] is None
    assert "went backwards" in ep["rates"]["reason"]


def registry_from(conn):
    """A registry naming the seeded endpoints straight from their rows."""
    from localagents.registry import Defaults, Endpoint
    eps = {r["name"]: Endpoint(name=r["name"], base_url=r["base_url"], backend=r["backend"], host=r["host"])
           for r in conn.execute("SELECT * FROM endpoints ORDER BY id")}
    return Registry(path=None, endpoints=eps, defaults=Defaults(), raw={}, local={})


def test_downsampled_rates_are_null_when_a_reset_lies_inside_the_interval(world):
    conn, _ = world
    ai1 = endpoint_id(conn, "ai1")
    # put the reset one interval before the newest sampled point: NOW-5 gets lower counters
    pid = conn.execute("SELECT id FROM probes WHERE endpoint_id = ? AND t = ?", (ai1, NOW - 5.0)).fetchone()[0]
    with db.immediate(conn):
        conn.execute("UPDATE probe_counters SET prompt_tokens = 0, generated_tokens = 0 WHERE probe_id = ?", (pid,))
    h = read.endpoint_history(conn, ai1, NOW - HOUR, NOW, max_points=100)
    assert h["t"][-1] == NOW and h["step"] == 8
    assert h["prompt_tps"][-1] is None  # NOW-40 .. NOW holds the reset at NOW-5
    assert _within_truncation(h["prompt_tps"][-2:-1], "prompt_tokens", 8)


# --- job detail ----------------------------------------------------------------------------


def _finished(conn):
    return conn.execute("SELECT * FROM jobs WHERE status = 'succeeded'").fetchone()


def test_job_detail_of_the_finished_job(world):
    conn, _ = world
    row = _finished(conn)
    d = read.job_detail(conn, row["id"])
    assert d["id"] == row["id"] and d["status"] == "succeeded"
    assert len(d["requests"]) == 112
    assert len(d["tool_calls"]) == 142
    assert d["tool_calls"] == sorted(d["tool_calls"], key=lambda tc: tc["t"])
    assert set(d["requests"][0]) == {"path", "t", "http_status", "ms", "gen_ms", "tokens", "stream",
                                     "context_exceeded", "error"}
    assert set(d["requests"][0]["tokens"]) == {"input", "output", "cache_read", "cache_creation"}
    assert {r["path"] for r in d["requests"]} <= {r["path"] for r in seed._load(FX).job_requests}
    assert set(d["tool_calls"][0]) == {"t", "finished", "tool", "tool_use_id", "input", "is_error"}
    # the seed made no job directory: the tail is null and the note says why
    assert d["transcript_tail"] is None
    assert isinstance(d["transcript_note"], str) and row["name"] in d["transcript_note"]
    # the summary is the job_status dict (derive.summary_from_row)
    assert d["usage"]["output_tokens"] == sum(r["output_tokens"] or 0 for r in seed._load(FX).job_requests)
    json.dumps(d)


def test_job_detail_tails_the_transcript_when_the_directory_exists(world):
    conn, _ = world
    row = _finished(conn)
    d = state_dir() / "jobs" / row["name"]
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text("".join(f"line {i}\n" for i in range(200)))
    out = read.job_detail(conn, row["id"])
    assert out["transcript_tail"].splitlines() == [f"line {i}" for i in range(120, 200)]
    assert out["transcript_note"] is None


def test_job_detail_of_an_unknown_job_is_none(world):
    conn, _ = world
    assert read.job_detail(conn, "no-such-job") is None


def test_jobs_in_range_newest_first_without_result(world):
    conn, _ = world
    js = read.jobs_in_range(conn, NOW - 24 * HOUR, NOW)
    assert [j["status"] for j in js] == ["running", "succeeded"]
    assert all("result" not in j for j in js)
    assert read.jobs_in_range(conn, NOW - 60.0, NOW) == []  # both were created earlier
