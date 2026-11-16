"""Story 2.4: ``localagents seed`` — an offline, deterministic database from the fixtures with
timestamps shifted to ``now``. It is what the browser smoke and the route tests run against and
it shows every display state at once: ai1 live (an hour of counters, a running job on its slot),
dgx1-a down 20 min, llamacpp dormant a week, a finished job with requests and tool calls, and a
running job.

Item 1 pins the seed itself: the per-table row counts, the refusal gate, force-replace, and
determinism in ``now``. Item 2 (below) runs the story-2.3 state functions, the collector's
readers, the rates, the read-time derivations, the status line and retention over the seeded
file. No network, no wall clock: everything is a function of ``now`` and the fixture bytes.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from localagents import db, derive, status
from localagents.monitor import collect, rates, retention, seed, state
from localagents.monitor import jobs as monitor_jobs
from localagents.monitor import rows as rows_mod

FX = Path(__file__).resolve().parent / "fixtures" / "monitor"
_SRC = Path(__file__).resolve().parent.parent / "src"
NOW = 1793288065.0  # a fixed clock: the seed is a pure function of it
EXPORT = json.loads((FX / "db" / "job_20261029-110844-8fd14e0e.json").read_text())
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _seed(tmp_path, now=NOW, name="seed.db", force=False):
    conn = db.open_db(tmp_path / name)
    counts = seed.seed(conn, now, FX, force=force)
    return conn, counts


# The hand-computed per-table row counts of the seeded world (item 1). ai1: 721 probes (a live
# hour at 5 s: 720 intervals, the counters restarting at the midpoint) each with a counters and
# a hardware row. dgx1-a: 481 up + 240 down probes, the up ones carrying counters. llamacpp: one
# up probe (a week ago) + 1441 dormant down probes. Jobs: the finished export (112 requests, 142
# tool calls) and the running truncation (40, 50). Three events: dgx1-a down, ai1 restarted,
# llamacpp down — ai1's up is dropped because the collector writes no event for a first probe.
EXPECTED_COUNTS = {
    "probes": 2884,
    "probe_counters": 1203,
    "probe_hardware": 721,
    "endpoint_events": 3,
    "endpoint_load": 1,
    "endpoints": 3,
    "models": 3,  # one per endpoint: the served id its UP probes carry (story 3.2a row 3)
    "mcp_servers": 1,
    "jobs": 2,
    "requests": 152,
    "tool_calls": 192,
    "paths": 3,
}


def test_seed_counts_match_the_world(tmp_path):
    _, counts = _seed(tmp_path)
    assert counts == EXPECTED_COUNTS


def test_up_probes_carry_their_served_model_like_the_collector_does(tmp_path):
    """Story 3.2a row 3: ``collect.tick`` records ``models[0]`` (``Registry.probe``'s first
    served id) on every UP probe, but the seed wrote a model only for ai1 — the world could
    not show a down or dormant endpoint's last-known model. Now every endpoint's UP probes carry
    their fixture's served id, and DOWN probes carry none, as a collector's never do."""
    conn, _ = _seed(tmp_path)
    for ep, served in (("ai1", "qwen3.8-flash-next-iq3_s"),
                       ("dgx1-a", "DeepSeek-V4.1-Flash-1017"),
                       ("llamacpp", "esatapedico/Qwen3.8-27B-NVFP4-MTP-GGUF")):
        up = conn.execute(
            "SELECT DISTINCT m.served_id FROM probes p JOIN models m ON m.id = p.model_id"
            " JOIN endpoints e ON e.id = p.endpoint_id WHERE e.name = ? AND p.up = 1", (ep,)).fetchall()
        assert [r[0] for r in up] == [served], ep
        assert conn.execute(
            "SELECT COUNT(*) FROM probes p JOIN models m ON m.id = p.model_id"
            " JOIN endpoints e ON e.id = p.endpoint_id WHERE e.name = ? AND p.up = 0", (ep,)).fetchone()[0] == 0


def test_second_seed_refuses_without_force(tmp_path):
    conn, _ = _seed(tmp_path)
    with pytest.raises(seed.SeedRefused, match="already holds jobs or probes"):
        seed.seed(conn, NOW, FX)
    # a refusal writes nothing: the counts are unchanged
    assert conn.execute("SELECT COUNT(*) FROM probes").fetchone()[0] == EXPECTED_COUNTS["probes"]


def test_force_replaces_rather_than_appends(tmp_path):
    conn, first = _seed(tmp_path)
    counts = seed.seed(conn, NOW, FX, force=True)
    # a fresh seed, not a second copy on top: the counts are identical to the first run
    assert counts == first == EXPECTED_COUNTS
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0] == 1


WALL = 1793381565.0  # a fixed wall clock: the collector refusal reads time.time(), not --now


def _a_probe_ago(conn, ago: float) -> None:
    with db.immediate(conn):
        eid = db.lookup_id(conn, "endpoints", name="m", base_url="http://10.0.0.1:8080",
                           backend="strata", host="h")
        conn.execute("INSERT INTO probes (t, endpoint_id, up) VALUES (?, ?, 1)", (WALL - ago, eid))


@pytest.mark.parametrize("force", [False, True])
def test_seed_refuses_a_database_a_collector_wrote_to(tmp_path, monkeypatch, force):
    """Review 3.9C C2: a running monitor never writes ``mcp_servers``, so the live-server check
    cannot see it; a fresh ``probes`` row is its trace. A probe 10 s old on the WALL clock is
    refused with or without ``force`` — reseeding would renumber the endpoints under the
    collector's cached ids."""
    monkeypatch.setattr(seed.time, "time", lambda: WALL)
    conn = db.open_db(tmp_path / "live.db")
    try:
        _a_probe_ago(conn, 10.0)
        with pytest.raises(seed.SeedRefused) as ei:
            seed.seed(conn, NOW, FX, force=force)
        assert str(ei.value) == ("a collector wrote this database 10 s ago; stop the monitor "
                                 "first, or point --db at a throwaway file")
    finally:
        conn.close()


def test_seed_replaces_a_database_a_collector_left_long_ago(tmp_path, monkeypatch):
    """The refusal is about a LIVE collector: a probe 600 s old is an ordinary stale database
    and ``force`` replaces it as before."""
    monkeypatch.setattr(seed.time, "time", lambda: WALL)
    conn = db.open_db(tmp_path / "stale.db")
    try:
        _a_probe_ago(conn, 600.0)
        assert seed.seed(conn, NOW, FX, force=True)["probes"] == EXPECTED_COUNTS["probes"]
    finally:
        conn.close()


def _dump(conn, table, cols, order):
    return [tuple(row) for row in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}")]


def test_seed_is_deterministic_in_now(tmp_path):
    """Two files seeded with the same now are byte-identical on probes and jobs, apart from the
    uuid columns (jobs.id, jobs.mcp_server_id: 3.10f-1 dropped jobs.name) and the probes rowid
    (insertion order)."""
    a = db.open_db(tmp_path / "a.db")
    seed.seed(a, NOW, FX)
    b = db.open_db(tmp_path / "b.db")
    seed.seed(b, NOW, FX)
    assert _dump(a, "probes", list(rows_mod.PROBE_COLUMNS), "endpoint_id, t") == \
           _dump(b, "probes", list(rows_mod.PROBE_COLUMNS), "endpoint_id, t")
    job_cols = [c for c in seed._JOB_COLS if c not in ("id", "mcp_server_id")]
    assert _dump(a, "jobs", job_cols, "started_at") == _dump(b, "jobs", job_cols, "started_at")
    # the lookup rows too: first_seen is the world's clock, not the wall clock (F4)
    ep_cols = ("id", "name", "base_url", "backend", "host", "first_seen")
    assert _dump(a, "endpoints", ep_cols, "id") == _dump(b, "endpoints", ep_cols, "id")
    a.close()
    b.close()


def test_summary_from_row_elapsed_is_read_on_the_readers_clock(seeded):
    """F9: the running job is 300 s old *at NOW* — the clock the reader passes — not at the
    wall clock, which is what made the old summary negative for a seeded file."""
    conn, _ = seeded
    row = conn.execute("SELECT * FROM jobs WHERE status = 'running'").fetchone()
    s = derive.summary_from_row(conn, row, now=NOW)
    assert s["elapsed_s"] == 300.0
    # a finished job's elapsed is its own span, whatever clock the reader has
    done = conn.execute("SELECT * FROM jobs WHERE status = 'succeeded'").fetchone()
    d = derive.summary_from_row(conn, done, now=NOW)
    assert d["elapsed_s"] == round(done["finished_at"] - done["started_at"], 1)


# --- item 2: the seeded database shows every state ----------------------------------------


def _endpoint_id(conn, name):
    return conn.execute("SELECT id FROM endpoints WHERE name = ?", (name,)).fetchone()[0]


def _last_probe(conn, name):
    eid = _endpoint_id(conn, name)
    return conn.execute("SELECT * FROM probes WHERE endpoint_id = ? ORDER BY t DESC, id DESC LIMIT 1",
                        (eid,)).fetchone()


@pytest.fixture
def seeded(tmp_path):
    conn, counts = _seed(tmp_path)
    yield conn, counts
    conn.close()


def test_endpoint_states_are_live_down_dormant(seeded):
    conn, _ = seeded
    for name, expected in (("ai1", "live"), ("dgx1-a", "down"), ("llamacpp", "dormant")):
        last = _last_probe(conn, name)
        assert state.endpoint_state(last, collect.last_up_t(conn, _endpoint_id(conn, name)), NOW) == expected


def test_node_states_are_all_up(seeded):
    """A refusal already proves the box is up, so even the down (dgx1-a) and dormant (llamacpp)
    hosts read up: the unreachable endpoint is 'endpoint down', shown on its line only."""
    conn, _ = seeded

    def last_of(name):
        r = _last_probe(conn, name)
        return {"up": bool(r["up"]), "host": r["host"]}
    assert state.node_state([last_of("ai1")]) == "up"
    assert state.node_state([last_of("dgx1-a")]) == "up"
    assert state.node_state([last_of("llamacpp")]) == "up"


def test_node_dormant_flags_only_the_dormant_host(seeded):
    """node_dormant is True only when every endpoint of the host is dormant: ai1 is live and
    dgx1-a is down-recent (neither dormant), while llamacpp — the host's only endpoint — is
    dormant, so its host collapses to one muted line. The host *answering* (node_state up) is a
    separate signal: a box can be alive while its endpoint is dormant."""
    conn, _ = seeded

    def ep_state(name):
        return state.endpoint_state(_last_probe(conn, name),
                                    collect.last_up_t(conn, _endpoint_id(conn, name)), NOW)
    assert state.node_dormant([ep_state("ai1")]) is False
    assert state.node_dormant([ep_state("dgx1-a")]) is False
    assert state.node_dormant([ep_state("llamacpp")]) is True


def test_probes_carry_state_and_the_disk_columns(seeded):
    """Story 3.1a ruling A in the seed: state follows the world's occupancy ("idle"/"busy" by
    its slots; the last five busy minutes are the running job on ai1's slot), a down probe's is
    NULL, and ai1's hardware rows carry the ai2 pass-2 capture's disk counters."""
    conn, _ = seeded
    ai1 = _endpoint_id(conn, "ai1")
    assert {r[0] for r in conn.execute(
        "SELECT DISTINCT state FROM probes WHERE endpoint_id = ? AND up = 1", (ai1,))} == {"idle", "busy"}
    last = conn.execute("SELECT state FROM probes WHERE endpoint_id = ? ORDER BY t DESC LIMIT 1", (ai1,)).fetchone()[0]
    assert last == "busy"  # the busy tail
    for name in ("ai1", "dgx1-a", "llamacpp"):
        assert conn.execute(
            "SELECT COUNT(*) FROM probes WHERE endpoint_id = ? AND up = 0 AND state IS NOT NULL",
            (_endpoint_id(conn, name),)).fetchone()[0] == 0, name
    assert conn.execute(
        "SELECT DISTINCT state FROM probes WHERE endpoint_id = ? AND up = 1", (_endpoint_id(conn, "dgx1-a"),)
    ).fetchone()[0] == "busy"  # vLLM with requests_running 1 throughout its up hour
    assert conn.execute(
        "SELECT state FROM probes WHERE endpoint_id = ? AND up = 1", (_endpoint_id(conn, "llamacpp"),)
    ).fetchone()[0] == "idle"  # llama.cpp 2 slots, neither busy
    hw = conn.execute("SELECT disk_read_mb, disk_write_mb FROM probe_hardware ORDER BY probe_id DESC LIMIT 1").fetchone()
    assert hw[0] == 0.0 and hw[1] == 0.00743732530467772  # the ai2 pass-2 capture's numbers


def test_the_running_job_is_the_one_on_ai1(seeded):
    conn, _ = seeded
    ai1 = _endpoint_id(conn, "ai1")
    row = monitor_jobs.running_job_at(conn, ai1, NOW - 60)
    assert row is not None and row["status"] == "running"
    # it started five minutes ago and is still running (no finished_at)
    assert row["started_at"] == pytest.approx(NOW - 300.0)
    assert row["finished_at"] is None


def _ai1_counter_pairs(conn):
    """(prev, cur) rows of ai1's counters in t order, each carrying t and since."""
    return conn.execute(
        "SELECT c.*, p.t, p.since FROM probe_counters c JOIN probes p ON p.id = c.probe_id"
        " WHERE p.endpoint_id = ? ORDER BY p.t, c.probe_id", (_endpoint_id(conn, "ai1"),)).fetchall()


def _rate(prev, cur):
    cols = tuple(rows_mod.COUNTER_COLUMNS)
    return rates.rates({k: prev[k] for k in cols}, {k: cur[k] for k in cols},
                       cur["t"] - prev["t"], "strata", prev["since"], cur["since"])


def test_ai1_rates_are_constant_and_non_zero(seeded):
    """The counters interpolate pass-1 -> pass-2 over each half-hour segment (360 intervals of
    5 s), so the rate between any two consecutive counters rows is the same non-zero figure
    before the restart and after it; across the restart (at ``now - 1800``) the interval is a
    reset, never a cliff.

    The ai2 capture pair's whole deltas are prompt 4654, generated 2584, cached 484756 (and
    accepted/drafted 1731/2106); one 5 s interval carries 1/360 of each, so the per-second rate
    is 4654/360/5 = 2.58556, 2584/360/5 = 1.43556, 484756/360/5 = 269.30889, with the spec
    acceptance ratio 0.822 as ``metrics_delta`` rounds it."""
    conn, _ = seeded
    pairs = _ai1_counter_pairs(conn)
    last = _rate(pairs[-2], pairs[-1])
    assert last.reset is False
    assert (last.prompt_tps, last.gen_tps, last.cached_tps) == pytest.approx(
        (4654 / 1800, 2584 / 1800, 484756 / 1800))
    assert last.spec_acceptance == pytest.approx(0.822)
    # the same constant over the first interval of the first segment
    first = _rate(pairs[0], pairs[1])
    assert (first.prompt_tps, first.gen_tps, first.cached_tps) == pytest.approx(
        (last.prompt_tps, last.gen_tps, last.cached_tps))
    # the interval across the restart is a reset whose reason is the seeded event's detail
    restart = next(i for i, cur in enumerate(pairs) if cur["t"] == NOW - 1800.0)
    across = _rate(pairs[restart - 1], pairs[restart])
    assert across.reset is True
    detail = conn.execute("SELECT detail FROM endpoint_events WHERE kind = 'restarted'").fetchone()[0]
    assert across.reason == detail
    assert "went backwards" in detail and "since changed" in detail
    # review of 3.3 (third server seam): the figures the detail names are rounded, not the
    # raw float repr the interpolated pass-2 reading carries (392777.0722222222 …)
    assert "prompt_tokens 392777.07 → 388136" in detail


def test_ai1_has_exactly_one_reset_and_it_is_the_restarted_event(seeded):
    """Replaying ``rates.rates`` over every consecutive ai1 pair finds exactly one reset, at the
    probe the seeded event names — the uncorroborated event of F2 now matches its own rows."""
    conn, _ = seeded
    pairs = _ai1_counter_pairs(conn)
    resets = []
    for prev, cur in zip(pairs, pairs[1:]):
        r = _rate(prev, cur)
        if r.reason is not None:
            resets.append((cur["t"], r.reason))
    assert len(resets) == 1
    ev = conn.execute("SELECT t, detail FROM endpoint_events WHERE kind = 'restarted'").fetchone()
    assert resets[0] == (ev["t"], ev["detail"])
    assert ev["t"] == NOW - 1800.0


def test_every_event_sits_at_the_probe_that_would_have_written_it(seeded):
    """Ruling D: the collector writes an event at the probe that produced it (a first probe
    writes none), so each seeded event sits on a probe row of the same instant."""
    conn, _ = seeded
    events = {(r["ep"], r["kind"]): r for r in conn.execute(
        "SELECT e.name AS ep, ev.kind, ev.t, ev.detail FROM endpoint_events ev"
        " JOIN endpoints e ON e.id = ev.endpoint_id")}
    assert set(events) == {("dgx1-a", "down"), ("ai1", "restarted"), ("llamacpp", "down")}
    for name in ("dgx1-a", "llamacpp"):
        ev = events[(name, "down")]
        probe = conn.execute("SELECT up FROM probes WHERE endpoint_id = ? AND t = ?",
                             (_endpoint_id(conn, name), ev["t"])).fetchone()
        assert probe is not None and probe["up"] == 0, name
    # nothing was running on dgx1-a at the instant its probe row says down
    dgx = _endpoint_id(conn, "dgx1-a")
    assert monitor_jobs.running_job_at(conn, dgx, events[("dgx1-a", "down")]["t"]) is None


def test_the_running_job_has_ai1_context(seeded):
    """F10: the running job was handed ai1's window (131072), not the export's 262144, so the
    page's context bar and the endpoint's own reading agree."""
    conn, _ = seeded
    row = conn.execute("SELECT * FROM jobs WHERE status = 'running'").fetchone()
    assert row["context"] == seed.AI1_CONTEXT
    s = derive.summary_from_row(conn, row, now=NOW)
    assert s["ctx_tokens"] is not None and s["ctx_tokens"] <= s["context"]


def test_the_finished_job_carrries_its_usage_and_files(seeded):
    conn, _ = seeded
    row = conn.execute("SELECT * FROM jobs WHERE status = 'succeeded'").fetchone()
    summary = derive.summary_from_row(conn, row)
    reqs = EXPORT["requests"]
    assert summary["usage"] == {
        "input_tokens": sum(r["input_tokens"] or 0 for r in reqs),
        "output_tokens": sum(r["output_tokens"] or 0 for r in reqs),
        "cache_read_input_tokens": sum(r["cache_read_tokens"] or 0 for r in reqs),
        "cache_creation_input_tokens": sum(r["cache_creation_tokens"] or 0 for r in reqs),
    }
    files = []
    for tc in EXPORT["tool_calls"]:
        if tc["tool"] in EDIT_TOOLS and tc["input"] and tc["input"] not in files:
            files.append(tc["input"])
    assert summary["files_touched"] == files


def test_statusline_lists_the_running_job_under_ai1(tmp_path):
    """The seed's own live server row (stopped NULL, a fresh heartbeat, a live pid) makes its
    running job show on the status line, under ai1's header with its endpoint load. The renderer
    reads db.db_path() (the conftest's LOCALAGENTS_DB), so the seed writes there."""
    c = db.open_db()  # db.db_path()
    seed.seed(c, NOW, FX)
    owner = c.execute("SELECT owner FROM mcp_servers").fetchone()[0]
    c.close()
    lines = status.render_lines(now=NOW, owner=owner)
    # status-sessions: the header counts our admitted sessions (queue entries) and reads the
    # sample's live requests; the seed's job has no queue entry (the seed predates the queue)
    # and its sample is the old text ("1/1 slots"), so its one busy slot reads as foreign
    assert lines and lines[0] == "local ai1 · +1 foreign"
    assert lines[1].startswith("  ⟳")


def test_retention_deletes_only_the_week_old_llamacpp_probe(seeded):
    """The one probe older than seven days is llamacpp's last-up probe (placed a minute past the
    cutoff so the strict t < now-7d boundary removes it); everything else — including the
    down event a week ago — survives. Job rows are never touched by probe retention."""
    conn, _ = seeded
    assert retention.prune_probes(conn, days=7, now=NOW) == 1
    assert conn.execute("SELECT COUNT(*) FROM probes").fetchone()[0] == EXPECTED_COUNTS["probes"] - 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == EXPECTED_COUNTS["jobs"]
    lcpp = _endpoint_id(conn, "llamacpp")
    oldest = conn.execute("SELECT MIN(t) FROM probes WHERE endpoint_id = ?", (lcpp,)).fetchone()[0]
    assert oldest == pytest.approx(NOW - 24 * 3600.0)  # the week-old up probe is gone


# --- item 3: the CLI -----------------------------------------------------------------------


def _run_cli(args, env):
    """``localagents <args>`` as a subprocess (cwd=src, so ``import localagents`` resolves there),
    with ``LOCALAGENTS_DB`` pointed at the test's tmp_path — never a real database."""
    argv = "['localagents', " + ", ".join(repr(a) for a in args) + "]"
    code = f"import sys; sys.argv={argv}; import localagents; localagents.main()"
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=_SRC, env=env)


def _count(dbpath, table):
    c = sqlite3.connect(dbpath)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def test_seed_cli_seeds_and_prints_counts_and_path(tmp_path):
    """``localagents seed`` (LOCALAGENTS_DB under tmp_path) seeds the world, prints the per-table
    counts one per line and the path it wrote."""
    dbpath = tmp_path / "localagents" / "localagents.db"
    env = {**os.environ, "LOCALAGENTS_DB": str(dbpath)}
    out = _run_cli(["seed", "--now", str(NOW)], env)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert lines[-1] == str(dbpath)
    assert "probes: 2884" in lines and "jobs: 2" in lines and "requests: 152" in lines
    assert dbpath.exists()


def test_force_refuses_while_a_live_server_owns_the_database():
    """F5, ruling E: --force must not delete a running server's row and jobs out from under it.
    A JobStore in this process leaves a row with this pid and a fresh heartbeat, so the seed
    refuses with a sentence naming the pid — against the WALL clock, not the world's --now
    (a future ``now`` must not make the server look stale) — and proceeds once the store has
    closed its row."""
    from localagents.jobs import JobStore

    store = JobStore()
    conn = store.conn
    try:
        assert conn.execute("SELECT COUNT(*) FROM mcp_servers WHERE stopped IS NULL").fetchone()[0] == 1
        with pytest.raises(seed.SeedRefused, match=rf"pid {os.getpid()}"):
            seed.seed(conn, time.time() + 3600.0, FX, force=True)
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0  # nothing was deleted
        store.close()
        assert seed.seed(conn, NOW, FX, force=True) == EXPECTED_COUNTS
    finally:
        store.close()
        conn.close()


def test_seed_cli_force_refuses_a_live_servers_database(tmp_path):
    """The CLI path of ruling E: the SeedRefused sentence (naming the pid) on stdout, exit 2."""
    dbpath = tmp_path / "live" / "localagents.db"
    env = {**os.environ, "LOCALAGENTS_DB": str(dbpath)}
    assert _run_cli(["seed", "--now", str(NOW)], env).returncode == 0
    c = sqlite3.connect(dbpath)
    try:
        c.execute("INSERT INTO mcp_servers (id, pid, owner, started, updated, cwd, version)"
                  " VALUES ('s', ?, 0, ?, ?, '', 'dev')", (os.getpid(), time.time(), time.time()))
        c.commit()
    finally:
        c.close()
    out = _run_cli(["seed", "--force"], env)
    assert out.returncode == 2, (out.stdout, out.stderr)
    assert str(os.getpid()) in out.stdout


def test_a_bad_now_exits_2_with_one_line_and_creates_no_file(tmp_path):
    """F6 (review of 2.4): --now is parsed before the file is opened, so a typo'd clock aimed
    at a real path leaves nothing behind — no schema-only database, no traceback."""
    dbpath = tmp_path / "never" / "world.db"
    out = _run_cli(["seed", "--db", str(dbpath), "--now", "not-a-time"], {**os.environ})
    assert out.returncode == 2, (out.stdout, out.stderr)
    assert len((out.stderr or out.stdout).strip().splitlines()) == 1
    assert not dbpath.exists() and not dbpath.parent.exists()


def test_parse_now_passes_a_number_through():
    """Review 3.9C T2: the ISO branch and the wall-clock fallback had no test at all."""
    assert seed.parse_now("1793288065.5") == 1793288065.5


def test_parse_now_reads_a_naive_iso_string_in_local_time():
    text = "2026-10-30T12:00:00"
    assert seed.parse_now(text) == datetime.fromisoformat(text).timestamp()


def test_parse_now_none_is_the_wall_clock(monkeypatch):
    monkeypatch.setattr(seed.time, "time", lambda: 1234.5)
    assert seed.parse_now(None) == 1234.5


def test_seed_cli_refuses_without_force_and_force_replaces(tmp_path):
    """A second run without --force exits 2 with the refusal sentence and changes nothing; a
    --force run replaces the database (still one seed's worth of rows) and exits 0."""
    dbpath = tmp_path / "localagents" / "localagents.db"
    env = {**os.environ, "LOCALAGENTS_DB": str(dbpath)}
    assert _run_cli(["seed", "--now", str(NOW)], env).returncode == 0
    assert _count(dbpath, "jobs") == 2
    out = _run_cli(["seed"], env)  # no --force
    assert out.returncode == 2
    assert "already holds jobs or probes" in out.stdout
    assert _count(dbpath, "jobs") == 2  # the refusal wrote nothing
    assert _run_cli(["seed", "--force"], env).returncode == 0
    assert _count(dbpath, "jobs") == 2 and _count(dbpath, "mcp_servers") == 1
