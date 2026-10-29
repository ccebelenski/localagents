"""Story 2.2 item 7: probe retention. The monitor's four tables lose rows older than the cutoff
(children first); the job tables are never touched (monitor spec §3: retention deletes only the
monitor's tables)."""
import asyncio

import pytest

from localagents import db
from localagents.monitor import collect, retention, state
from localagents.registry import Defaults, Endpoint

NOW = 1793288065.0
DAY = 86400.0


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "m.db")
    yield c
    c.close()


def _seed(conn, t):
    """One probe with its counters and hardware rows, and one event, at time t; one job row."""
    eid = db.lookup_id(conn, "endpoints", name="ai2", base_url="http://ai2:8080", backend="strata", host="ai2")
    pid = conn.execute("INSERT INTO probes (t, endpoint_id, up) VALUES (?, ?, 1)", (t, eid)).lastrowid
    conn.execute("INSERT INTO probe_counters (probe_id, prompt_tokens) VALUES (?, 1.0)", (pid,))
    conn.execute("INSERT INTO probe_hardware (probe_id, cpu) VALUES (?, 1.0)", (pid,))
    conn.execute("INSERT INTO endpoint_events (t, endpoint_id, kind) VALUES (?, ?, 'up')", (t, eid))
    conn.execute("INSERT INTO jobs (id, endpoint_id, status, created_at, finished_at) VALUES (?, ?, 'succeeded', ?, ?)",
                 (f"job-{t}", eid, t, t))
    return pid


def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("probes", "probe_counters", "probe_hardware", "endpoint_events", "jobs", "endpoints")}


def test_rows_older_than_the_cutoff_go_newer_stay_and_jobs_are_untouched(conn):
    old = _seed(conn, NOW - 8 * DAY)
    new = _seed(conn, NOW - 6 * DAY)
    assert retention.prune_probes(conn, days=7, now=NOW) == 1
    assert _counts(conn) == {"probes": 1, "probe_counters": 1, "probe_hardware": 1, "endpoint_events": 1,
                             "jobs": 2, "endpoints": 1}
    assert [r[0] for r in conn.execute("SELECT id FROM probes")] == [new]
    for child in ("probe_counters", "probe_hardware"):
        assert conn.execute(f"SELECT COUNT(*) FROM {child} WHERE probe_id = ?", (old,)).fetchone()[0] == 0
    assert conn.execute("SELECT t FROM endpoint_events").fetchone()[0] == NOW - 6 * DAY
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not conn.in_transaction


def test_the_probes_delete_uses_the_endpoint_t_index(conn):
    """Story 2.2a row 5 (review: a bare ``t < ?`` scan; ruling F): retention deletes per
    endpoint through the existing ``(endpoint_id, t)`` index. The plan for the exact statement
    the pruner runs must name ``probes_endpoint_t``."""
    plan = " | ".join(str(row[3]) for row in
                      conn.execute("EXPLAIN QUERY PLAN " + retention._DELETE_OLD_PROBES, (1, 0)))
    assert "probes_endpoint_t" in plan, plan
    assert "INDEX" in plan, plan


def test_zero_days_disables_pruning(conn):
    _seed(conn, NOW - 30 * DAY)
    assert retention.prune_probes(conn, days=0, now=NOW) == 0
    assert _counts(conn)["probes"] == 1


def test_now_defaults_to_the_clock(conn):
    _seed(conn, 1.0)  # 1970: long past any cutoff
    assert retention.prune_probes(conn) == 1


class _Reg:
    defaults = Defaults()

    async def probe(self, ep):
        return {"up": False, "models": [], "host": "unknown", "error": "x timed out"}


def test_run_prunes_at_start_and_then_on_its_period(monkeypatch, conn):
    _seed(conn, NOW - 8 * DAY)
    calls = []
    real = retention.prune_probes

    def counting(c, days=7, now=None):
        calls.append((days, now))
        return real(c, days=days, now=now)
    monkeypatch.setattr(retention, "prune_probes", counting)
    monkeypatch.setattr(state, "interval_s", lambda *a, **k: 0)

    async def main():
        stop = asyncio.Event()
        reg = _Reg()
        n = 0

        async def probe(ep):
            nonlocal n
            n += 1
            if n >= 3 and len(calls) >= 2:
                stop.set()
            await asyncio.sleep(0.01)
            return await _Reg.probe(reg, ep)
        reg.probe = probe
        ep = Endpoint(name="x", base_url="http://x:1", backend="strata")
        await asyncio.wait_for(collect.run(reg, conn, [ep], stop, now=lambda: NOW, retention_every_s=0.005), 10)
    asyncio.run(main())
    assert len(calls) >= 2 and calls[0] == (7, NOW)
    # the seeded 8-day-old rows went at the first prune; the job row stayed
    assert conn.execute("SELECT COUNT(*) FROM probes WHERE t < ?", (NOW - 7 * DAY,)).fetchone()[0] == 0
    assert _counts(conn)["jobs"] == 1


def test_run_defaults_to_an_hourly_seven_day_prune():
    import inspect

    params = inspect.signature(collect.run).parameters
    assert params["retention_days"].default == 7 and params["retention_every_s"].default == 3600
