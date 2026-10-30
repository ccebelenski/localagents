"""Story 3.1 item 2: the monitor's read model (monitor/read.py) over the seeded world (story 2.4):
ai1 live with an hour of 5 s probes, dgx1-a down since NOW-20 min (a refusal), llamacpp dormant
since a week ago; a finished job (112 requests, 142 tool calls) and a running one (40, 50), both
on ai1; three endpoint events (ai1's counters restart at NOW-1800). The JSON shapes are the
contract of spec §5a."""
import json
import math
import os
import time

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


@pytest.fixture(autouse=True)
def _empty_finished_cache():
    """Story 3.7-fold-4s: the finished-entry cache is module-level (per process), and every
    test reads its own scratch world with hand-inserted ids; start each test from an empty
    cache so a scratch database can never answer from another one's figures."""
    read._FINISHED_CACHE.clear()
    yield


def _host(ov, name):
    return next(h for h in ov["hosts"] if h["name"] == name)


def _ep(ov, name):
    return next(e for h in ov["hosts"] for e in h["endpoints"] if e["name"] == name)


# --- overview ------------------------------------------------------------------------------


def test_overview_top_level_keys(world):
    conn, reg = world
    ov = read.overview(conn, reg, NOW, WINDOW_S)
    assert set(ov) == {"now", "window_s", "refresh_s", "db_path", "hosts", "jobs", "events"}
    assert ov["now"] == NOW and ov["window_s"] == WINDOW_S
    assert ov["refresh_s"] == 5.0  # story 3.7m: the fan-out's clock period, carried to the page
    assert read.overview(conn, reg, NOW, WINDOW_S, refresh_s=0.5)["refresh_s"] == 0.5
    # json-serialisable as is: it is the SSE snapshot's body
    json.dumps(ov)


def test_overview_names_the_database_file_it_reads(world):
    """Story 3.2a row 3 (the 3.2 review's §5a gap): the snapshot says which database the server
    opened, so the page can name the file it is showing. app.py passes it in (serve's --db, the
    monitor: section's db, else db.db_path()); a read that was not told one says null."""
    conn, reg = world
    assert read.overview(conn, reg, NOW, WINDOW_S)["db_path"] is None
    ov = read.overview(conn, reg, NOW, WINDOW_S, db_path="/home/operator/.state/localagents.db")
    assert ov["db_path"] == "/home/operator/.state/localagents.db"


def test_overview_hosts_in_config_order_with_their_states(world):
    conn, reg = world
    ov = read.overview(conn, reg, NOW, WINDOW_S)
    assert [h["name"] for h in ov["hosts"]] == ["ai1", "dgx1", "local"]
    assert [[e["name"] for e in h["endpoints"]] for h in ov["hosts"]] == [["ai1"], ["dgx1-a"], ["llamacpp"]]
    assert [e["state"] for h in ov["hosts"] for e in h["endpoints"]] == ["live", "down", "dormant"]
    # a refusal proves the box is up (the seed's node test): every node is up
    assert [h["state"] for h in ov["hosts"]] == ["up", "up", "up"]
    assert [h["dormant"] for h in ov["hosts"]] == [False, False, True]
    assert set(ov["hosts"][0]) == {"name", "state", "dormant", "fallback", "fallback_t", "endpoints"}
    # the seed's world holds no host_probes row: the fallback is null on every host
    assert [h["fallback"] for h in ov["hosts"]] == [None, None, None]
    assert [h["fallback_t"] for h in ov["hosts"]] == [None, None, None]


def _unknown_latest_probe(conn, name):
    """Make the endpoint's LAST probe a timeout (host 'unknown') at NOW: the node is all-timeout."""
    eid = endpoint_id(conn, name)
    with db.immediate(conn):
        rows.insert(conn, rows.ProbeRows(
            {**dict.fromkeys(rows.PROBE_COLUMNS), "t": NOW, "endpoint_id": eid, "up": 0,
             "host": "unknown",
             "error": "127.0.0.1:8080 timed out (host off, or a firewall dropping packets)"},
            None, None))


def test_fallback_resolves_an_all_timeout_host(world):
    """Story 3.1b: a host whose endpoints' last probes are all timeouts (host 'unknown') is
    resolved from the newest host_probes row within 120 s of now (the port-22 ask's answer):
    refused (the box answers port 22) -> up; nothing (silent) -> down; a row older than 120 s
    is stale, so the state stays unknown and the fallback fields are null."""
    conn, reg = world
    _unknown_latest_probe(conn, "llamacpp")
    with db.immediate(conn):
        conn.execute("INSERT INTO host_probes (t, host, address, answer) VALUES (?, 'local', '127.0.0.1', 'refused')",
                     (NOW - 10,))
    host = _host(read.overview(conn, reg, NOW, WINDOW_S), "local")
    assert host["state"] == "up"
    assert host["fallback"] == "refused" and host["fallback_t"] == NOW - 10
    # the other hosts hold no host_probes row: the fields are null
    assert _host(read.overview(conn, reg, NOW, WINDOW_S), "ai1")["fallback"] is None
    with db.immediate(conn):
        conn.execute("UPDATE host_probes SET answer = 'nothing'")
    host = _host(read.overview(conn, reg, NOW, WINDOW_S), "local")
    assert host["state"] == "down" and host["fallback"] == "nothing"
    with db.immediate(conn):
        conn.execute("UPDATE host_probes SET t = ?", (NOW - 200,))
    host = _host(read.overview(conn, reg, NOW, WINDOW_S), "local")
    assert host["state"] == "unknown" and host["fallback"] is None and host["fallback_t"] is None


ENDPOINT_KEYS = {
    "name", "backend", "base_url", "host", "state", "last_seen", "last_active", "model", "context",
    "probe_state", "last_model", "last_context", "slots", "requests", "kv_cache_usage", "latency_ms",
    "since", "error", "dropped_job", "dropped_job_id", "rates", "gen_tps_live", "hardware",
    "jobs_running",
}


def test_last_active_is_the_newest_advancing_counters_interval(world):
    """Story 3.7s over the seeded world, read from the stored witness (3.7-fold-2 D5: the seed
    backfills each endpoint's last_active at its end, the way the monitor does at its own start):
    ai1's counters advance on every 5 s interval of its plan (both halves climb; the restart at
    the midpoint is a reset and is excluded), so the newest probe at NOW is the last active one.
    dgx1-a's counters stop when it went down 20 minutes ago. llamacpp's one counters row (a week
    ago) has no predecessor in the retained history, so it is null: a counter backend with no
    advancing pair."""
    conn, reg = world
    ov = read.overview(conn, reg, NOW, WINDOW_S)
    assert _ep(ov, "ai1")["last_active"] == NOW
    assert _ep(ov, "dgx1-a")["last_active"] == NOW - 20 * 60.0
    assert _ep(ov, "llamacpp")["last_active"] is None


def test_last_active_skips_the_reset_interval(world):
    """Rates pair consecutive counters rows and a reset interval has no rate; the witness is
    written by that same pairing, so the probe whose counters dropped (ai1's restart at
    NOW-1800) is not active. Trim the world at that probe and re-run the backfill — what the
    migration does once, a hand-edited world must ask for: the newest remaining advancing
    interval ends at NOW-1805 (probe 359), not at the restart. (The start's call restricts to NULL
    witnesses; this hand-edited world asks for the full recompute, the default.)"""
    conn, reg = world
    eid = endpoint_id(conn, "ai1")
    with db.immediate(conn):
        for table in ("probe_counters", "probe_hardware"):
            conn.execute(f"DELETE FROM {table} WHERE probe_id IN"
                         " (SELECT id FROM probes WHERE endpoint_id = ? AND t >= ?)",
                         (eid, NOW - 1800.0))
        conn.execute("DELETE FROM probes WHERE endpoint_id = ? AND t >= ?", (eid, NOW - 1800.0))
        read.backfill_last_active(conn)
    assert _ep(read.overview(conn, reg, NOW, WINDOW_S), "ai1")["last_active"] == NOW - 1805.0


def test_idle_seven_day_history_reads_like_a_two_probe_one(tmp_path):
    """The 3.7 server-batch review's P3 world (DEFECT D5), re-created from its t/p3.py: one
    llama.cpp endpoint with 7 d of idle 5 s probes whose counters never advanced (120,961 rows,
    executemany in one transaction) and one endpoint whose counters advance. Before the stored
    witness one read.overview cost 742 ms, 745 ms of it the idle endpoint's walk to None; after
    it the same history reads in the seeded world's time (well under the 50 ms bound here) and
    costs the same statements whether the history is 7 d deep or two probes deep — the per-push
    statement count never walked the history, and must still not."""
    path = tmp_path / "p3.db"
    conn = db.open_db(path)
    week = 7 * 86400.0
    with db.immediate(conn):
        idle = conn.execute(
            "INSERT INTO endpoints (name, base_url, backend, host)"
            " VALUES ('idle', 'http://127.0.0.1:8080', 'llama.cpp', 'local')").lastrowid
        busy = conn.execute(
            "INSERT INTO endpoints (name, base_url, backend, host)"
            " VALUES ('busy', 'http://127.0.0.1:8081', 'llama.cpp', 'local')").lastrowid
        n = int(week / 5) + 1  # 120,961 five-second probes
        first = conn.execute("SELECT COALESCE(MAX(id), 0) FROM probes").fetchone()[0] + 1
        conn.executemany("INSERT INTO probes (t, endpoint_id, up) VALUES (?, ?, 1)",
                         [(NOW - week + 5 * i, idle) for i in range(n)])
        conn.executemany("INSERT INTO probe_counters (probe_id, prompt_tokens, generated_tokens)"
                         " VALUES (?, 1000.0, 500.0)", [(first + i,) for i in range(n)])
        for t, gen in ((NOW - 10, 500.0), (NOW - 5, 640.0)):  # the active endpoint's advance
            cur = conn.execute("INSERT INTO probes (t, endpoint_id, up) VALUES (?, ?, 1)", (t, busy))
            conn.execute("INSERT INTO probe_counters (probe_id, prompt_tokens, generated_tokens)"
                         " VALUES (?, 1000.0, ?)", (cur.lastrowid, gen))
        read.backfill_last_active(conn)
    cfg = tmp_path / "p3-models.yaml"
    cfg.write_text(
        "endpoints:\n"
        "  idle:\n    base_url: http://127.0.0.1:8080\n    backend: llama.cpp\n    host: local\n"
        "  busy:\n    base_url: http://127.0.0.1:8081\n    backend: llama.cpp\n    host: local\n")
    reg = Registry.load(cfg)

    def measured():
        stmts: list[str] = []
        conn.set_trace_callback(stmts.append)
        t0 = time.monotonic()
        ov = read.overview(conn, reg, NOW, WINDOW_S)
        ms = (time.monotonic() - t0) * 1000
        conn.set_trace_callback(None)
        return ms, len(stmts), ov

    ms, n_stmts, ov = measured()
    assert ms < 50  # the review's before: 742 ms
    assert _ep(ov, "idle")["last_active"] is None  # nothing in 7 d ever advanced
    assert _ep(ov, "busy")["last_active"] == NOW - 5
    with db.immediate(conn):  # same world, shallow idle history: the count must not move
        conn.execute("DELETE FROM probe_counters WHERE probe_id IN"
                     " (SELECT id FROM probes WHERE endpoint_id = ? AND t < ?)", (idle, NOW - 60))
        conn.execute("DELETE FROM probes WHERE endpoint_id = ? AND t < ?", (idle, NOW - 60))
    ms2, n2, ov2 = measured()
    assert ms2 < 50
    assert n2 == n_stmts  # one witness read per endpoint per push, not a walk: depth costs nothing
    assert _ep(ov2, "idle")["last_active"] is None and _ep(ov2, "busy")["last_active"] == NOW - 5
    conn.close()


def test_the_live_endpoint_carries_its_gauges_rates_and_hardware(world):
    conn, reg = world
    ai1 = _ep(read.overview(conn, reg, NOW, WINDOW_S), "ai1")
    assert set(ai1) == ENDPOINT_KEYS
    assert ai1["backend"] == "strata" and ai1["base_url"] == "http://10.0.0.50:8080"
    # story 3.2a row 3: the config host, and the last-known pair is a down/dormant story only
    assert ai1["host"] == "ai1"
    assert ai1["last_model"] is None and ai1["last_context"] is None
    assert ai1["probe_state"] == "busy"  # the newest probe's own state string
    assert ai1["last_seen"] == NOW
    assert ai1["model"] == "qwen3.8-flash-next-iq3_s"
    assert ai1["context"] == seed.AI1_CONTEXT
    assert ai1["slots"] == {"total": 1, "busy": 1}
    assert ai1["requests"] == {"running": 1, "waiting": 0}
    assert ai1["latency_ms"] == seed.LATENCY_MS
    # the restart at NOW-1800 changed since (the last probe is in the post-restart segment)
    assert ai1["since"] == NOW - 1800.0
    assert ai1["error"] is None and ai1["dropped_job"] is None
    # the seed test's pinned constant rate (test_ai1_rates_are_constant_and_non_zero): the ai2
    # pair's deltas (4654 prompt, 2584 generated, 484756 cached) over one 360-interval half
    # hour -> 4654/1800, 2584/1800, 484756/1800 per second; spec acceptance 0.822 = 1731/2106
    r = ai1["rates"]
    assert set(r) == {"prompt_tps", "gen_tps", "cached_tps", "spec_acceptance", "reason"}
    assert (r["prompt_tps"], r["gen_tps"], r["cached_tps"]) == pytest.approx(
        (4654 / 1800, 2584 / 1800, 484756 / 1800))
    assert r["spec_acceptance"] == pytest.approx(0.822)
    assert r["reason"] is None
    assert ai1["gen_tps_live"] is not None
    assert set(ai1["hardware"]) == set(rows.HARDWARE_COLUMNS)
    assert ai1["hardware"]["gpu_util"] == pytest.approx(59.5 - 39.5 * math.cos(2 * math.pi * 720 / 721))
    assert ai1["jobs_running"] == 1


def test_the_down_endpoint_names_its_sentence_and_no_dropped_job(world):
    conn, reg = world
    dgx = _ep(read.overview(conn, reg, NOW, WINDOW_S), "dgx1-a")
    assert dgx["state"] == "down"
    assert dgx["error"] == seed._refused_sentence("http://10.0.0.70:8000")
    assert "refused" in dgx["error"] or "nothing" in dgx["error"]
    assert dgx["dropped_job"] is None  # the seed's jobs ran on ai1
    assert dgx["dropped_job_id"] is None
    assert dgx["last_seen"] == pytest.approx(NOW - 20 * 60.0)
    # a down probe has no gauges, no counters, no hardware
    assert dgx["model"] is None and dgx["slots"] is None and dgx["requests"] is None
    assert dgx["hardware"] is None
    # story 3.2a row 3 (3.3's gap: a down endpoint's last-known model and context): the newest
    # UP probe's served id and window, while the live model/context above stay null
    assert dgx["host"] == "dgx1"
    assert dgx["probe_state"] is None  # the newest probe is a down one: it has no state string
    assert dgx["last_model"] == "DeepSeek-V4.1-Flash-1017"
    assert dgx["last_context"] == 307712
    assert dgx["rates"]["prompt_tps"] is None and dgx["rates"]["reason"] is not None
    assert dgx["jobs_running"] == 0


def test_the_dormant_endpoint_is_last_seen_a_week_ago(world):
    conn, reg = world
    lcpp = _ep(read.overview(conn, reg, NOW, WINDOW_S), "llamacpp")
    assert lcpp["state"] == "dormant"
    assert lcpp["last_seen"] == pytest.approx(NOW - seed.LCPP_UP_AGO_S)
    assert lcpp["dropped_job"] is None  # named only for a down-recent endpoint
    assert lcpp["dropped_job_id"] is None
    # dormant too gets the last-known pair: its one UP probe a week ago
    assert lcpp["last_model"] == "esatapedico/Qwen3.8-27B-NVFP4-MTP-GGUF"
    assert lcpp["last_context"] == seed.LCPP_CONTEXT


def test_dropped_job_names_the_job_running_when_the_endpoint_dropped(world):
    """ai1 goes down at NOW+5 while the seed's running job is still running: the overview at
    NOW+10 names that job."""
    conn, reg = world
    ai1 = endpoint_id(conn, "ai1")
    with db.immediate(conn):  # rows.write went with story 2.2a: a tick's insert in its own transaction
        rows.insert(conn, rows.ProbeRows(
            {**dict.fromkeys(rows.PROBE_COLUMNS), "t": NOW + 5, "endpoint_id": ai1, "up": 0, "host": "up",
             "error": seed._refused_sentence("http://10.0.0.50:8080")}, None, None))
    running = conn.execute("SELECT id, name FROM jobs WHERE status = 'running'").fetchone()
    ep = _ep(read.overview(conn, reg, NOW + 10, WINDOW_S), "ai1")
    assert ep["state"] == "down"
    assert ep["dropped_job"] == running["name"]
    # story 3.2a row 3 (3.3's gap: dropped_job had no id to click through to)
    assert ep["dropped_job_id"] == running["id"]


def test_snapshot_running_job_tool_text_is_the_status_bar_display_form(world):
    """Story 3.2a row 5 (the 3.2 review: the job row showed the raw worktree-prefixed text the
    status bar never shows): ``last_tool`` and ``last_activity`` in the snapshot carry the
    DISPLAY form — one line, whitespace collapsed, paths relative to the job's root (its
    worktree path, the way status._job_root derives it), cut after relativising, exactly as
    ``status.display_activity`` renders a status row (1.11b's rule). The raw text stays on the
    job detail (and the /api/jobs summaries)."""
    conn, reg = world
    row = conn.execute("SELECT id FROM jobs WHERE status = 'running'").fetchone()
    jid = row["id"]
    root = "/home/op/.local/state/localagents/worktrees/demo/20261029-120000-deadbeef"
    with db.immediate(conn):
        conn.execute("UPDATE jobs SET worktree = ?, last_activity = ? WHERE id = ?",
                     (json.dumps({"path": root}), "Bash: uv run pytest -q\n   && echo done\ttwice", jid))
        conn.execute(
            "UPDATE tool_calls SET tool = 'Edit', input = ? WHERE rowid ="
            " (SELECT rowid FROM tool_calls WHERE job_id = ? ORDER BY t DESC, rowid DESC LIMIT 1)",
            (f"{root}/tests/x.py", jid))
    j = read.overview(conn, reg, NOW, WINDOW_S)["jobs"][0]
    assert j["last_tool"] == "Edit: tests/x.py"  # root prefix gone, one line
    assert j["last_activity"] == "Bash: uv run pytest -q && echo done twice"
    d = read.job_detail(conn, jid)
    assert d["last_tool"] == f"Edit: {root}/tests/x.py"  # raw on the detail route
    assert d["last_activity"] == "Bash: uv run pytest -q\n   && echo done\ttwice"


def test_snapshot_running_job_tool_text_falls_back_to_the_cwd(world):
    """A job with no worktree relativises against its cwd (status._job_root's rule, story
    1.11b): the cwd prefix goes from the snapshot row's text too."""
    conn, reg = world
    row = conn.execute("SELECT id, cwd FROM jobs WHERE status = 'running'").fetchone()
    with db.immediate(conn):
        conn.execute("UPDATE jobs SET worktree = NULL WHERE id = ?", (row["id"],))
        conn.execute(
            "UPDATE tool_calls SET tool = 'Edit', input = ? WHERE rowid ="
            " (SELECT rowid FROM tool_calls WHERE job_id = ? ORDER BY t DESC, rowid DESC LIMIT 1)",
            (f"{row['cwd']}/src/app.py", row["id"]))
    j = read.overview(conn, reg, NOW, WINDOW_S)["jobs"][0]
    assert j["last_tool"] == "Edit: src/app.py"


def test_overview_running_jobs_carry_the_status_line_entry(world):
    conn, reg = world
    ov = read.overview(conn, reg, NOW, WINDOW_S)
    # story 3.7g: the log is the live rows first, then the seeded finished one
    assert len(ov["jobs"]) == 2 and ov["jobs"][1]["status"] == "succeeded"
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
    # F10: the job's window is ai1's, so the bar reads the same 94 % the endpoint would
    assert j["context"] == seed.AI1_CONTEXT and j["ctx_pct"] == 94
    assert j["server_live"] is True  # the seed's server row: a live pid, a heartbeat at NOW
    assert set(j) == {
        "id", "name", "model", "endpoint", "status", "started_at", "finished_at", "elapsed_s",
        "num_turns", "tool_calls", "errors", "last_tool", "last_activity", "ctx_pct", "ctx_tokens",
        "context", "gen_tps", "compactions", "files", "owner", "server_live",
    }


def test_finished_jobs_follow_the_running_ones_in_the_log(world):
    """Story 3.7g: the snapshot's jobs list is the page's Recent jobs log — the seeded
    finished job follows the running one, with the same columns, and its ``elapsed_s`` is
    the run's length (finished_at - started_at), not now - started_at."""
    conn, reg = world
    jobs_ = read.overview(conn, reg, NOW, WINDOW_S)["jobs"]
    assert [j["status"] for j in jobs_] == ["running", "succeeded"]
    row = conn.execute("SELECT * FROM jobs WHERE status = 'succeeded'").fetchone()
    f = jobs_[1]
    assert f["id"] == row["id"] and set(f) == set(jobs_[0])
    assert f["finished_at"] == pytest.approx(NOW - 600.0)
    assert f["elapsed_s"] == pytest.approx(row["finished_at"] - row["started_at"], abs=0.05)
    # the live form would be exactly the 600 s since the finish larger
    assert f["elapsed_s"] == pytest.approx(NOW - row["started_at"] - 600.0, abs=0.1)


def test_only_the_ten_newest_finished_jobs_join_the_log(world):
    """Story 3.7g: the finished tail is capped at 10 and newest-finished first; every
    terminal status joins it (jobs.py's done check, not just 'succeeded')."""
    conn, reg = world
    running_id = conn.execute("SELECT id FROM jobs WHERE status = 'running'").fetchone()["id"]
    with db.immediate(conn):
        for k in range(12):  # 'failed': a terminal status of its own, finished k s apart
            conn.execute(
                "INSERT INTO jobs (id, name, status, created_at, started_at, finished_at, updated,"
                " num_turns) VALUES (?, ?, 'failed', ?, ?, ?, ?, ?)",
                (f"seedfin{k}", f"fin-{k}", NOW - 200.0 + k, NOW - 200.0 + k,
                 NOW - 100.0 + k, NOW - 100.0 + k, k))
    ids = [j["id"] for j in read.overview(conn, reg, NOW, WINDOW_S)["jobs"]]
    assert ids[0] == running_id  # the live row stays first
    assert ids[1:] == [f"seedfin{k}" for k in range(11, 1, -1)]  # 12 in, 10 out, newest first


def test_finished_elapsed_is_frozen_across_clock_reads(world):
    """Story 3.7g: two reads a clock-tick apart — the finished row's elapsed says the same
    thing (the run is over), the running row's has grown by exactly the tick."""
    conn, reg = world
    a = read.overview(conn, reg, NOW, WINDOW_S)["jobs"]
    b = read.overview(conn, reg, NOW + 120.0, WINDOW_S)["jobs"]
    by = lambda jobs_, s: next(j for j in jobs_ if j["status"] == s)
    assert by(b, "succeeded")["elapsed_s"] == by(a, "succeeded")["elapsed_s"]
    assert by(b, "running")["elapsed_s"] == pytest.approx(by(a, "running")["elapsed_s"] + 120.0)


# --- story 3.7-fold-4s: the finished-row cache -------------------------------------------------


def _add_finished(conn, n):
    """``n`` hand-inserted finished rows, the shape test_only_the_ten_newest uses: 'failed',
    started NOW-200+k s and finished NOW-100+k s (newer k is a newer finish)."""
    with db.immediate(conn):
        for k in range(n):
            conn.execute(
                "INSERT INTO jobs (id, name, status, created_at, started_at, finished_at, updated,"
                " num_turns) VALUES (?, ?, 'failed', ?, ?, ?, ?, ?)",
                (f"seedfin{k}", f"fin-{k}", NOW - 200.0 + k, NOW - 200.0 + k,
                 NOW - 100.0 + k, NOW - 100.0 + k, k))


def test_finished_rows_derive_once_across_pushes(world):
    """Story 3.7-fold-4s (the review's P1 world): 12 hand-inserted finished rows plus the live
    one; the SECOND overview's statement count equals the first's minus 6 per finished row
    shown — the six derive statements each row costs come from the cache, only the SELECTs
    re-run. (Pre-fix the two calls are equal: 86 and 86 measured; the review's world: 92.)"""
    conn, reg = world
    _add_finished(conn, 12)
    stmts = []
    conn.set_trace_callback(stmts.append)
    read.overview(conn, reg, NOW, WINDOW_S)
    first = len(stmts)
    stmts.clear()
    ov = read.overview(conn, reg, NOW, WINDOW_S)
    second = len(stmts)
    conn.set_trace_callback(None)
    shown = [j for j in ov["jobs"] if j["status"] not in read.LIVE_STATUSES]
    assert len(shown) == 10  # the tail cap: 12 in, plus the seeded finished one, 10 shown
    assert first - second == 6 * len(shown)


def test_a_job_finishing_between_pushes_enters_the_cache_with_its_figures(world):
    """Story 3.7-fold-4s: a job live on one push and finished on the next appears in the
    second with its figures (the cache computes them on the first finished push), and a third
    push reads them from the cache unchanged."""
    conn, reg = world
    running_id = conn.execute("SELECT id FROM jobs WHERE status = 'running'").fetchone()["id"]
    before = next(j for j in read.overview(conn, reg, NOW, WINDOW_S)["jobs"] if j["id"] == running_id)
    assert before["status"] == "running" and before["tool_calls"] > 0
    with db.immediate(conn):
        conn.execute("UPDATE jobs SET status = 'succeeded', finished_at = ?, updated = ?, result = 'done'"
                     " WHERE id = ?", (NOW - 1.0, NOW - 1.0, running_id))
    after = next(j for j in read.overview(conn, reg, NOW + 60.0, WINDOW_S)["jobs"] if j["id"] == running_id)
    assert after["status"] == "succeeded"
    for key in ("num_turns", "tool_calls", "errors", "last_tool", "files", "ctx_tokens", "gen_tps"):
        assert after[key] == before[key], key
    assert after["elapsed_s"] == pytest.approx((NOW - 1.0) - before["started_at"], abs=0.05)
    third = next(j for j in read.overview(conn, reg, NOW + 120.0, WINDOW_S)["jobs"] if j["id"] == running_id)
    # the cached entry answers the third push with the same figures (server_live rides the
    # server row's age and is not part of the cached entry)
    assert {k: v for k, v in third.items() if k != "server_live"} == \
           {k: v for k, v in after.items() if k != "server_live"}


def test_the_finished_cache_holds_only_the_current_tail(world):
    """Story 3.7-fold-4s: 11 finished jobs (10 hand-inserted plus the seeded one), the tail
    shows 10 — after a call the cache holds exactly those 10 ids; the one that left the tail
    (the seeded job, oldest finish) is evicted."""
    conn, reg = world
    seeded_finished = conn.execute("SELECT id FROM jobs WHERE status = 'succeeded'").fetchone()["id"]
    # the last review (DeepSeek, 2026-10-30): this test was blind — the cache started empty, so
    # the seeded row never entered it and a mutant without the eviction loop still passed. The
    # cache is PRIMED first: one call with the seeded job as the whole tail puts it in the cache
    read.overview(conn, reg, NOW, WINDOW_S)
    assert seeded_finished in {k[0] for k in read._FINISHED_CACHE}
    _add_finished(conn, 10)
    ov = read.overview(conn, reg, NOW, WINDOW_S)
    tail = [j["id"] for j in ov["jobs"] if j["status"] not in read.LIVE_STATUSES]
    assert len(tail) == 10
    ids = {k[0] for k in read._FINISHED_CACHE}
    assert ids == set(tail) and len(read._FINISHED_CACHE) == 10
    assert seeded_finished not in ids  # finished NOW-600: the oldest, out of the 10-tail — evicted


def test_server_live_recomputes_on_a_cached_finished_row(world):
    """Story 3.7-fold-4s: the cached figures never change but the job's server still beats —
    marking it stopped between pushes flips ``server_live`` on the cached entry while every
    other field (the derive figures included) stays exactly as cached."""
    conn, reg = world
    with db.immediate(conn):
        conn.execute("INSERT INTO mcp_servers (id, pid, owner, started, updated) VALUES (?, ?, ?, ?, ?)",
                     ("srv-fin", os.getpid(), os.getuid(), NOW - 30.0, NOW))
        conn.execute("INSERT INTO jobs (id, name, mcp_server_id, status, created_at, started_at,"
                     " finished_at, updated) VALUES ('fin-srv', 'fin srv', 'srv-fin', 'succeeded', ?, ?, ?, ?)",
                     (NOW - 400.0, NOW - 400.0, NOW - 200.0, NOW - 200.0))
        conn.execute("INSERT INTO tool_calls (job_id, t, tool, tool_use_id, input)"
                     " VALUES ('fin-srv', ?, 'Bash', 'tu1', 'ls')", (NOW - 300.0,))
    a = next(j for j in read.overview(conn, reg, NOW, WINDOW_S)["jobs"] if j["id"] == "fin-srv")
    assert a["server_live"] is True and a["tool_calls"] == 1
    with db.immediate(conn):
        conn.execute("UPDATE mcp_servers SET stopped = ? WHERE id = 'srv-fin'", (NOW + 5.0,))
    b = next(j for j in read.overview(conn, reg, NOW + 10.0, WINDOW_S)["jobs"] if j["id"] == "fin-srv")
    assert b["server_live"] is False and b["tool_calls"] == 1
    assert {k: v for k, v in b.items() if k != "server_live"} == \
           {k: v for k, v in a.items() if k != "server_live"}


def test_a_terminal_row_with_null_finished_at_shows_no_moving_elapsed(world):
    """Story 3.7-fold-4s (the review's POSSIBLE): the schema allows a terminal row with a
    NULL ``finished_at`` (no write path produces one). Its ``elapsed_s`` is null on every
    read — a finished row never shows a moving number. Pre-fix the frozen clock fell back
    to ``now`` and the figure grew 120 s between the two reads below."""
    conn, reg = world
    with db.immediate(conn):
        conn.execute("INSERT INTO jobs (id, name, status, created_at, started_at, updated)"
                     " VALUES ('fin-nul', 'fin null', 'failed', ?, ?, ?)",
                     (NOW - 400.0, NOW - 300.0, NOW - 300.0))
    a = next(j for j in read.overview(conn, reg, NOW, WINDOW_S)["jobs"] if j["id"] == "fin-nul")
    b = next(j for j in read.overview(conn, reg, NOW + 120.0, WINDOW_S)["jobs"] if j["id"] == "fin-nul")
    assert a["status"] == "failed" and a["started_at"] == NOW - 300.0
    assert a["elapsed_s"] is None and b["elapsed_s"] is None


def test_overview_events_are_the_newest_twenty_with_endpoint_names(world):
    conn, reg = world
    ev = read.overview(conn, reg, NOW, WINDOW_S)["events"]
    assert len(ev) == 3
    assert [e["t"] for e in ev] == sorted((e["t"] for e in ev), reverse=True)
    assert [(e["endpoint"], e["kind"]) for e in ev] == [
        ("dgx1-a", "down"), ("ai1", "restarted"), ("llamacpp", "down")]
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
    """ai1's real per-second rate: the busy ai2 pair's delta spread over one half-hour segment
    (360 intervals of 5 s, before the restart and after it). The counters are interpolated and
    fractional; ``metrics_delta`` keeps the fraction (ruling A), so a rate over any number of
    intervals equals this exactly."""
    w = seed._load(FX)
    return (w.ai1_counters_2[key] - w.ai1_counters_1[key]) / (360 * 5.0)


def _all_are(values, key):
    """Every non-null value is ai1's one real rate (the null one is the restart's span)."""
    true = _true_rate(key)
    return all(v == pytest.approx(true) for v in values if v is not None)


def _one_reset(values):
    """The history has exactly one gap in these rates: the span across the seeded restart."""
    return sum(1 for v in values if v is None) == 1


SERIES = ("prompt_tps", "gen_tps", "cache_hit", "slots_busy", "requests_running", "requests_waiting",
          "kv_cache_usage", "latency_ms", "state",
          "gpu_util", "gpu_mem_used", "gpu_temp", "gpu_power",
          "gpu_pcie_rx_mb", "gpu_pcie_tx_mb", "cpu", "ram_used", "disk_read_mb", "disk_write_mb")


def test_ai1_history_over_an_hour_is_downsampled_to_at_most_600(world):
    conn, _ = world
    ai1 = endpoint_id(conn, "ai1")
    n = conn.execute("SELECT COUNT(*) FROM probes WHERE endpoint_id = ? AND t >= ?", (ai1, NOW - HOUR)).fetchone()[0]
    assert n == 721
    h = read.endpoint_history(conn, ai1, NOW - HOUR, NOW)
    assert 0 < len(h["t"]) <= 600
    assert len(h["t"]) == 361 and h["step"] == 2
    assert h["t"][-1] == NOW  # the newest probe is always kept
    assert h["t"] == sorted(h["t"])
    for k in SERIES:
        if k == "kv_cache_usage":
            assert k not in h  # strata reports no KV fill: absent, not a column of nulls
            continue
        assert k in h, k
        assert len(h[k]) == len(h["t"]), k
    assert h["gpu_util"][-1] == pytest.approx(59.5 - 39.5 * math.cos(2 * math.pi * 720 / 721))
    # the counters are linear within each segment: every point's rate (over two intervals) is
    # the real rate; the span across the restart at NOW-1800 has no rate
    assert _all_are(h["prompt_tps"][1:], "prompt_tokens")
    assert _all_are(h["gen_tps"][1:], "generated_tokens")
    assert _one_reset(h["prompt_tps"][1:])
    assert h["cache_hit"][-1] == pytest.approx(_true_rate("prompt_tokens_cached")
                                               / (_true_rate("prompt_tokens_cached") + _true_rate("prompt_tokens")),
                                               abs=0.001)
    assert h["slots_busy"][-1] == 1 and h["slots_busy"][0] == 0


def test_history_without_downsampling_keeps_every_probe(world):
    conn, _ = world
    ai1 = endpoint_id(conn, "ai1")
    h = read.endpoint_history(conn, ai1, NOW - HOUR, NOW, max_points=1000)
    assert len(h["t"]) == 721 and h["step"] == 1
    # the first in-range probe is ai1's first ever: no previous counters, no rate
    assert h["prompt_tps"][0] is None
    # one interval each: the seed test's pinned figures, every interval except the restart's
    assert _one_reset(h["prompt_tps"][1:])
    assert _all_are(h["prompt_tps"][1:], "prompt_tokens")
    assert _all_are(h["gen_tps"][1:], "generated_tokens")


def test_history_downsampling_takes_every_kth_probe_ending_at_the_newest(world):
    conn, _ = world
    ai1 = endpoint_id(conn, "ai1")
    h = read.endpoint_history(conn, ai1, NOW - HOUR, NOW, max_points=100)
    assert h["step"] == 8 and len(h["t"]) == 91
    assert h["t"][-1] == NOW and h["t"][-2] == NOW - 8 * 5.0
    # the rate over a sampled interval (eight raw intervals) is the real rate too; the one
    # sampled span holding the restart has no rate
    assert _all_are(h["prompt_tps"][1:], "prompt_tokens")
    assert _all_are(h["gen_tps"][1:], "generated_tokens")
    assert _one_reset(h["prompt_tps"][1:])


def test_ai1_history_serves_the_state_latency_and_every_hardware_series(world):
    """Ruling B (review of 3.1): the history serves every tier its probes carry — latency_ms and
    state (the probes' own strings, no gauges) beside all ten hardware series."""
    conn, _ = world
    h = read.endpoint_history(conn, endpoint_id(conn, "ai1"), NOW - HOUR, NOW)
    assert len(h["latency_ms"]) == len(h["t"]) and set(h["latency_ms"]) == {seed.LATENCY_MS}
    assert len(h["state"]) == len(h["t"]) and set(h["state"]) == {"idle", "busy"}
    assert h["state"][-1] == "busy" and h["state"][0] == "idle"  # the busy tail reads through
    w = seed._load(FX)
    for key in ("gpu_pcie_rx_mb", "gpu_pcie_tx_mb", "cpu", "ram_used", "disk_read_mb", "disk_write_mb"):
        assert key in h and len(h[key]) == len(h["t"]), key
        # the seed sweeps only gpu_util: every other hardware figure is the ai2 pass-2 capture's,
        # stored as REAL and read back exactly
        assert set(h[key]) == {w.ai1_hardware[key]}, key
    assert h["disk_write_mb"][0] == 0.00743732530467772  # pinned from the capture, not derived


def test_dgx_history_has_latency_and_state_but_no_hardware_series(world):
    """Ruling B over the seeded world: a vLLM endpoint has latency_ms and state (strings while
    up, null while down) but not one hardware series."""
    conn, _ = world
    h = read.endpoint_history(conn, endpoint_id(conn, "dgx1-a"), NOW - HOUR, NOW)
    assert len(h["latency_ms"]) == len(h["t"]) and set(h["latency_ms"]) == {seed.LATENCY_MS, None}
    assert set(h["state"]) == {"busy", None}  # busy while up, null while down
    for key in ("gpu_pcie_rx_mb", "gpu_pcie_tx_mb", "cpu", "ram_used", "disk_read_mb", "disk_write_mb",
                "gpu_util", "gpu_mem_used", "gpu_temp", "gpu_power"):
        assert key not in h, key  # null throughout: absent, not a column of nulls


def test_ai1_history_event_is_the_restart(world):
    """The collector writes no event for a first probe, so ai1 has only its restart."""
    conn, _ = world
    h = read.endpoint_history(conn, endpoint_id(conn, "ai1"), NOW - HOUR, NOW)
    assert [(e["kind"], e["t"]) for e in h["events"]] == [("restarted", NOW - 1800.0)]
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
    assert h["prompt_tps"][-2] == pytest.approx(_true_rate("prompt_tokens"))
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
    assert _all_are(h["prompt_tps"][-2:-1], "prompt_tokens")


# --- fleet history -------------------------------------------------------------------------


def test_fleet_history_one_grid_for_the_configured_endpoints_with_probes(world):
    """Ruling C (review of 3.1): one shared grid over the union of the endpoints' kept probe
    times; every configured endpoint with ANY probe in the range appears — llamacpp's hour is
    all down probes, so it appears with all-null series."""
    conn, reg = world
    fh = read.fleet_history(conn, reg, NOW - HOUR, NOW)
    assert len(fh["t"]) <= 600 and fh["t"] == sorted(set(fh["t"]))
    assert fh["t"][-1] == NOW  # the newest kept point is always kept
    assert set(fh["endpoints"]) == {"ai1", "dgx1-a", "llamacpp"}
    for name, body in fh["endpoints"].items():
        assert set(body) == {"gen_tps", "prompt_tps", "occupancy"}, name
        for key, values in body.items():
            assert len(values) == len(fh["t"]), (name, key)
    # llamacpp: down probes only -> present with null series
    assert set(fh["endpoints"]["llamacpp"]["gen_tps"]) == {None}
    assert set(fh["endpoints"]["llamacpp"]["occupancy"]) == {None}
    # ai1's grid values are its own history's at the shared times, and null where it has no
    # kept probe within half a grid step (NOW-3600 is dgx1-a's, not ai1's)
    h_ai1 = read.endpoint_history(conn, endpoint_id(conn, "ai1"), NOW - HOUR, NOW)
    own = dict(zip(h_ai1["t"], h_ai1["gen_tps"]))
    at = {t: v for t, v in zip(fh["t"], fh["endpoints"]["ai1"]["gen_tps"]) if t in own}
    assert at == {t: own[t] for t in own}
    assert fh["endpoints"]["ai1"]["gen_tps"][fh["t"].index(NOW - 3600.0)] is None
    # every ai1 value is either its history's or null: never a borrowed dgx1-a figure
    assert all(v is None or v in {own[t] for t in own if t in fh["t"]}
               for v in fh["endpoints"]["ai1"]["gen_tps"])


def test_fleet_history_occupancy_follows_the_backend(world):
    """Ruling C: occupancy is slots_busy for slot backends and requests_running for vLLM; the
    series is null after the endpoint drops (a down probe reports neither)."""
    conn, reg = world
    fh = read.fleet_history(conn, reg, NOW - HOUR, NOW)
    ai1 = fh["endpoints"]["ai1"]
    # strata: its slots. Since story 2.4a ai1's hour is a true hour (721 probes from NOW-3600),
    # so every grid time in the range has an ai1 probe and no null appears; the null-at-a-foreign-
    # grid-time rule is pinned by the dgx1-a tail below and by the capped-grid test.
    assert set(ai1["occupancy"]) == {0, 1}
    assert ai1["occupancy"][-1] == 1
    assert ai1["occupancy"][fh["t"].index(NOW - 3590.0)] == 0
    assert ai1["occupancy"][fh["t"].index(NOW - 3600.0)] == 0
    dgx = fh["endpoints"]["dgx1-a"]
    assert set(dgx["occupancy"]) == {1, None}
    # null after its drop at NOW-20min: every grid point past the drop has no figure
    i_last_up = max(i for i, t in enumerate(fh["t"]) if t <= NOW - 20 * 60.0)
    assert dgx["occupancy"][i_last_up] == 1  # the last up probe is at exactly NOW-20min
    assert all(v is None for v in dgx["occupancy"][i_last_up + 1:])


def test_fleet_history_grid_capped_and_endpoints_without_probes_left_out(world, tmp_path):
    conn, reg = world
    # a 24 h grid: every endpoint's probes, the union downsampled to at most 600 points
    fh = read.fleet_history(conn, reg, NOW - 24 * HOUR, NOW)
    assert len(fh["t"]) <= 600 and fh["t"][-1] == NOW
    # configured but never probed: no probes in any range -> not in endpoints at all
    from localagents.registry import Registry
    cfg = write_config(tmp_path / "four.yaml",
                       "  fresh:\n    base_url: http://10.0.0.99:8080\n    host: ai1\n")
    fh2 = read.fleet_history(conn, Registry.load(cfg), NOW - HOUR, NOW)
    assert "fresh" not in fh2["endpoints"]


# --- requests by endpoint -----------------------------------------------------------------------


def test_endpoint_requests_holds_both_seeded_jobs_newest_first(world):
    """Ruling D (review of 3.1): the requests of jobs that ran on the endpoint in the range,
    joined through jobs.endpoint_id, newest first, each rendered as the job detail renders a
    request plus job_id/job_name, with a totals line summing the range."""
    conn, _ = world
    names = {r["name"] for r in conn.execute("SELECT name FROM jobs")}
    out = read.endpoint_requests(conn, endpoint_id(conn, "ai1"), NOW - 24 * HOUR, NOW)
    reqs, tot = out["requests"], out["totals"]
    assert len(reqs) == 152  # the finished job's 112 plus the running job's truncated 40
    assert [r["t"] for r in reqs] == sorted((r["t"] for r in reqs), reverse=True)
    assert {r["job_name"] for r in reqs} == names
    assert set(reqs[0]) == {"path", "t", "http_status", "ms", "gen_ms", "tokens", "stream",
                            "context_exceeded", "error", "job_id", "job_name"}
    assert set(reqs[0]["tokens"]) == {"input", "output", "cache_read", "cache_creation"}
    all_reqs = seed._load(FX).job_requests
    both = all_reqs + all_reqs[:40]  # the running job is the same export, truncated to 40
    assert tot == {
        "count": 152,
        "input_tokens": sum(r["input_tokens"] or 0 for r in both),
        "output_tokens": sum(r["output_tokens"] or 0 for r in both),
        "cache_read_tokens": sum(r["cache_read_tokens"] or 0 for r in both),
        "cache_creation_tokens": sum(r["cache_creation_tokens"] or 0 for r in both),
        "ms": sum(r["ms"] or 0 for r in both),
        "errors": sum(1 for r in both if r["error"] is not None and r["error"] != ""),
    }


def test_endpoint_requests_totals_count_only_real_errors(world):
    """Review of 3.3 (its second server seam): the export writes clean requests with
    ``error = ''`` and the totals read ``SUM(error IS NOT NULL)``, so the table's error figure
    counted requests the page draws as clean — over the seeded world ai1 read 2. An error is a
    column that is not null AND not the empty string (job.js's client-side totals follow the
    same rule; §5a)."""
    conn, _ = world
    ep = endpoint_id(conn, "ai1")
    since, until = NOW - 24 * HOUR, NOW
    scope = " FROM requests r JOIN jobs j ON j.id = r.job_id" \
            " WHERE j.endpoint_id = ? AND r.t >= ? AND r.t <= ?"
    loose = conn.execute("SELECT COUNT(*)" + scope + " AND r.error IS NOT NULL",
                         (ep, since, until)).fetchone()[0]
    assert loose == 2  # the export's two '' rows: clean on the page, once counted as errors
    true_errors = conn.execute("SELECT COUNT(*)" + scope +
                               " AND r.error IS NOT NULL AND r.error != ''",
                               (ep, since, until)).fetchone()[0]
    assert read.endpoint_requests(conn, ep, since, until)["totals"]["errors"] == true_errors
    # a constructed '' row is not an error either; a row with a sentence is
    jid = conn.execute("SELECT id FROM jobs WHERE endpoint_id = ? LIMIT 1", (ep,)).fetchone()[0]
    conn.execute("INSERT INTO requests (job_id, t, error) VALUES (?, ?, '')", (jid, NOW - 60.0))
    conn.execute("INSERT INTO requests (job_id, t, error) VALUES (?, ?, 'upstream refused')"
                 , (jid, NOW - 59.0))
    out = read.endpoint_requests(conn, ep, since, until)
    assert out["totals"]["errors"] == true_errors + 1
    assert out["totals"]["count"] == 154  # the totals span the range, constructed rows included


def test_endpoint_requests_endpoint_without_jobs_is_empty_with_zero_totals(world):
    conn, _ = world
    out = read.endpoint_requests(conn, endpoint_id(conn, "dgx1-a"), NOW - 24 * HOUR, NOW)
    assert out["requests"] == []
    assert set(out["totals"]) == {"count", "input_tokens", "output_tokens", "cache_read_tokens",
                                  "cache_creation_tokens", "ms", "errors"}
    assert all(v == 0 for v in out["totals"].values())


def test_endpoint_requests_caps_the_list_but_not_the_totals(world):
    conn, _ = world
    out = read.endpoint_requests(conn, endpoint_id(conn, "ai1"), NOW - 24 * HOUR, NOW, limit=5)
    assert len(out["requests"]) == 5
    assert out["totals"]["count"] == 152  # the totals are the range's, not the capped page's


def test_endpoint_requests_range_excludes_older_traffic(world):
    conn, _ = world
    out = read.endpoint_requests(conn, endpoint_id(conn, "ai1"), NOW - 2 * 60.0, NOW)
    assert 0 < len(out["requests"]) < 152  # only the running job's last minutes
    assert all(r["t"] >= NOW - 2 * 60.0 for r in out["requests"])
    assert out["totals"]["count"] == len(out["requests"])


# --- job detail ----------------------------------------------------------------------------


def _finished(conn):
    return conn.execute("SELECT * FROM jobs WHERE status = 'succeeded'").fetchone()


def test_job_detail_of_the_finished_job(world):
    conn, _ = world
    row = _finished(conn)
    d = read.job_detail(conn, row["id"])
    assert d["id"] == row["id"] and d["status"] == "succeeded"
    assert len(d["requests"]) == 112
    # ruling E (review of 3.1): the list lives under tool_call_log; tool_calls stays the count
    assert len(d["tool_call_log"]) == 142
    assert d["tool_calls"] == 142 == len(d["tool_call_log"])
    assert d["tool_call_log"] == sorted(d["tool_call_log"], key=lambda tc: tc["t"])
    assert set(d["requests"][0]) == {"path", "t", "http_status", "ms", "gen_ms", "tokens", "stream",
                                     "context_exceeded", "error"}
    assert set(d["requests"][0]["tokens"]) == {"input", "output", "cache_read", "cache_creation"}
    assert {r["path"] for r in d["requests"]} <= {r["path"] for r in seed._load(FX).job_requests}
    assert set(d["tool_call_log"][0]) == {"t", "finished", "tool", "tool_use_id", "input", "is_error"}
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


def test_job_detail_reads_the_transcript_row_before_the_file(world):
    """Story 1.11: a ``transcripts`` row tails ahead of everything — no directory needed, and a
    present directory's transcript.txt does not win either. With neither, the note names both
    things the reader looked for."""
    conn, _ = world
    row = _finished(conn)
    conn.execute(
        "INSERT INTO transcripts (job_id, text, bytes, written) VALUES (?, ?, ?, ?)",
        (row["id"], "".join(f"row {i}\n" for i in range(200)), 0, NOW),
    )
    out = read.job_detail(conn, row["id"])  # the seed world has no job directory: the row carries it
    assert out["transcript_tail"].splitlines() == [f"row {i}" for i in range(120, 200)]
    assert out["transcript_note"] is None

    d = state_dir() / "jobs" / row["name"]
    d.mkdir(parents=True)  # a file exists too: the row still wins
    (d / "transcript.txt").write_text("only the file\n")
    out = read.job_detail(conn, row["id"])
    assert out["transcript_tail"].splitlines()[-1] == "row 199"
    assert out["transcript_note"] is None

    conn.execute("DELETE FROM transcripts WHERE job_id = ?", (row["id"],))
    (d / "transcript.txt").unlink()  # neither: the note says what was looked for
    out = read.job_detail(conn, row["id"])
    assert out["transcript_tail"] is None
    assert "no transcripts row" in out["transcript_note"] and row["name"] in out["transcript_note"]


def test_job_detail_of_an_unknown_job_is_none(world):
    conn, _ = world
    assert read.job_detail(conn, "no-such-job") is None


def test_jobs_in_range_and_job_detail_take_the_readers_now(world):
    """F9, ruling C: every route passes its own ``now`` into ``derive.summary_from_row``, so the
    running job reads 300 s old on the page's clock, not on the wall clock."""
    conn, _ = world
    running = next(j for j in read.jobs_in_range(conn, NOW - 24 * HOUR, NOW) if j["status"] == "running")
    assert running["elapsed_s"] == 300.0
    d = read.job_detail(conn, running["id"], now=NOW)
    assert d["elapsed_s"] == 300.0
    # the overview's running row (its own clock already)
    j = read.overview(conn, registry_from(conn), NOW, WINDOW_S)["jobs"][0]
    assert j["elapsed_s"] == 300.0


def test_jobs_in_range_newest_first_without_result(world):
    conn, _ = world
    js = read.jobs_in_range(conn, NOW - 24 * HOUR, NOW)
    assert [j["status"] for j in js] == ["running", "succeeded"]
    assert all("result" not in j for j in js)
    assert read.jobs_in_range(conn, NOW - 60.0, NOW) == []  # both were created earlier
