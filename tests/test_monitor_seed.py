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


# The hand-computed per-table row counts of the seeded world (item 1). ai1: 720 probes (a live
# hour at 5 s) each with a counters and a hardware row. dgx1-a: 481 up + 240 down probes, the up
# ones carrying counters. llamacpp: one up probe (a week ago) + 1441 dormant down probes. Jobs:
# the finished export (112 requests, 142 tool calls) and the running truncation (40, 50).
EXPECTED_COUNTS = {
    "probes": 2883,
    "probe_counters": 1202,
    "probe_hardware": 720,
    "endpoint_events": 4,
    "endpoint_load": 1,
    "endpoints": 3,
    "models": 1,
    "mcp_servers": 1,
    "jobs": 2,
    "requests": 152,
    "tool_calls": 192,
    "paths": 3,
}


def test_seed_counts_match_the_world(tmp_path):
    _, counts = _seed(tmp_path)
    assert counts == EXPECTED_COUNTS


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


def _dump(conn, table, cols, order):
    return [tuple(row) for row in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}")]


def test_seed_is_deterministic_in_now(tmp_path):
    """Two files seeded with the same now are byte-identical on probes and jobs, apart from the
    uuid columns (jobs.id, jobs.mcp_server_id, and jobs.name, which embeds the id's hex) and the
    probes rowid (insertion order)."""
    a = db.open_db(tmp_path / "a.db")
    seed.seed(a, NOW, FX)
    b = db.open_db(tmp_path / "b.db")
    seed.seed(b, NOW, FX)
    assert _dump(a, "probes", list(rows_mod.PROBE_COLUMNS), "endpoint_id, t") == \
           _dump(b, "probes", list(rows_mod.PROBE_COLUMNS), "endpoint_id, t")
    job_cols = [c for c in seed._JOB_COLS if c not in ("id", "mcp_server_id", "name")]
    assert _dump(a, "jobs", job_cols, "started_at") == _dump(b, "jobs", job_cols, "started_at")
    a.close()
    b.close()


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


def test_the_running_job_is_the_one_on_ai1(seeded):
    conn, _ = seeded
    ai1 = _endpoint_id(conn, "ai1")
    row = monitor_jobs.running_job_at(conn, ai1, NOW - 60)
    assert row is not None and row["status"] == "running"
    # it started five minutes ago and is still running (no finished_at)
    assert row["started_at"] == pytest.approx(NOW - 300.0)
    assert row["finished_at"] is None


def test_ai1_rates_are_constant_and_non_zero(seeded):
    """The counters were interpolated linearly across the hour, so the rate between any two
    consecutive counters rows is the same non-zero figure (the busy ai2 pair spread over 719
    intervals, per wall second). Pinned from the fixtures' pass-1 and pass-2 deltas."""
    conn, _ = seeded
    ai1 = _endpoint_id(conn, "ai1")
    cols = tuple(rows_mod.COUNTER_COLUMNS)
    cur = conn.execute(
        "SELECT c.*, p.t, p.since FROM probe_counters c JOIN probes p ON p.id = c.probe_id"
        " WHERE p.endpoint_id = ? ORDER BY p.t DESC, p.id DESC LIMIT 1", (ai1,)).fetchone()
    prev = conn.execute(
        "SELECT c.*, p.t, p.since FROM probe_counters c JOIN probes p ON p.id = c.probe_id"
        " WHERE p.endpoint_id = ? AND p.t < ? ORDER BY p.t DESC, p.id DESC LIMIT 1",
        (ai1, cur["t"])).fetchone()
    r = rates.rates({k: prev[k] for k in cols}, {k: cur[k] for k in cols}, cur["t"] - prev["t"],
                    "strata", prev["since"], cur["since"])
    assert r.reset is False
    assert (r.prompt_tps, r.gen_tps, r.cached_tps, r.spec_acceptance) == (1.2, 0.6, 134.8, 0.822)
    # constancy: the same rate over the first interval too (linear interpolation)
    first = conn.execute(
        "SELECT c.*, p.t, p.since FROM probe_counters c JOIN probes p ON p.id = c.probe_id"
        " WHERE p.endpoint_id = ? ORDER BY p.t ASC, p.id ASC LIMIT 2", (ai1,)).fetchall()
    r0 = rates.rates({k: first[0][k] for k in cols}, {k: first[1][k] for k in cols},
                     first[1]["t"] - first[0]["t"], "strata", first[0]["since"], first[1]["since"])
    assert (r0.prompt_tps, r0.gen_tps, r0.cached_tps) == (r.prompt_tps, r.gen_tps, r.cached_tps)


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
    assert lines and lines[0] == "local ai1 · 1/1 slots"
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
    assert "probes: 2883" in lines and "jobs: 2" in lines and "requests: 152" in lines
    assert dbpath.exists()


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
