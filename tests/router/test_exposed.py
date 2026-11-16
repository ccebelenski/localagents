"""Router 14a: the exposed facts (docs/superpowers/specs/2026-11-02-router-design.md §7 "Exposed",
§7 "Accepted" pools, §4 the router entry on ``list_models`` and in the monitor's collector).

``list_models`` gives every real endpoint its sessions (capacity, ours, foreign), its queue
(count, head priority), the curve's prediction for the session a new job would be, the curve's
state with its point count, and its pool; the router entry gives the calibrated rows, the
scorecard's notices per row, and the declared pools. The monitor's collector ticks a router
entry as a router: its decision server's health, no ``/metrics`` fetch of a base URL it does not
have, no slot or throughput figures, and no part in a host's node state or port-22 fallback.

The fleet is a monkeypatched ``Registry.probe`` over a hand-built ``Registry``; our sessions and
queued jobs are written through ``dispatch.new_job`` on the server's own store, so the queue
rows are exactly the ones ``run_agent`` leaves."""

import asyncio
import copy
import time

import pytest

from localagents import db, server
from localagents.monitor import collect, read
from localagents.registry import Defaults, Endpoint, Registry
from localagents.router import calibrate, definitions, dispatch, scorecard
from localagents.router.capacity import Curve, save_capacity

ROUTER = Endpoint(name="auto", base_url="", backend="router", decision_url="http://d3.test:8766")


def _reg(endpoints, pools=None):
    eps = {e.name: e for e in endpoints}
    return Registry(path=None, endpoints=eps, defaults=Defaults(), raw={}, local={},
                    pools=dict(pools or {}))


def _patch(monkeypatch, reg, facts):
    async def probe(self, ep):
        if ep.is_router:
            return {"up": True, "models": [ep.name], "router": True, "context": {}}
        return copy.deepcopy(facts[ep.name])

    monkeypatch.setattr(Registry, "probe", probe)
    monkeypatch.setattr(server, "_reg", lambda: reg)


def _job(ep, served, *, priority="normal", forced=False, status=None):
    """A job on the server's store with its queue entry (admitted at once when forced)."""
    job = dispatch.new_job(server.store, priority=priority, forced=forced, task="t", model=served,
                           endpoint=ep.name, base_url=ep.url, cwd="/w", run_cwd="/w",
                           isolation="none", backend=ep.backend, host=ep.host, context=131_072)
    if status is not None:
        job.status = status
        job.started_at = time.time() if status == "running" else None
        job.changed()
    return job


def _calibrated(conn, ep, served, *, complete=True, constants="{}"):
    """A scorecard row with a core run (complete or not) and one pass of cells."""
    eid = db.lookup_id(conn, "endpoints", name=ep.name, base_url=ep.url, backend=ep.backend,
                       host=ep.host)
    mid = db.lookup_id(conn, "models", served_id=served)
    row = scorecard.get_or_create_row(conn, ep.name, served, eid, mid)
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
                 " VALUES (?, ?, 'core', '2026.11', '{}', 1.0, ?)",
                 (db.new_uuid(), row.id, "complete" if complete else "incomplete"))
    pass_id = db.new_uuid()
    conn.execute("INSERT INTO scorecard_passes (id, row_id, at, constants, observations)"
                 " VALUES (?, ?, 2.0, ?, 0)", (pass_id, row.id, constants))
    version = definitions.load(conn).version
    conn.execute("INSERT INTO scorecard_cells (pass_id, dimension, language, estimate, count,"
                 " probes_n, jobs_n, thin, defs_version) VALUES (?, 'software_dev', '', 3, 6, 6, 0, 0, ?)",
                 (pass_id, version))
    return row.id


def test_list_models_exposes_sessions_and_queue(monkeypatch):
    """§7 "Exposed", amendment 1: per real endpoint the sessions (capacity, ours, foreign), the
    queue (count, head priority), the predicted speed at ours + foreign + 1 and the curve's
    state with its point count, its measured concurrencies and where the bottleneck rule holds
    from (curve-refine moved this pin: ``measured`` and ``holds_from`` added). A down endpoint admits nothing and predicts nothing; the router
    entry carries none of these."""
    box = Endpoint(name="box", base_url="http://box:8080", backend="llama.cpp")
    pooled = Endpoint(name="pooled", base_url="http://pooled:8000", backend="vllm")
    gone = Endpoint(name="gone", base_url="http://gone:8080", backend="llama.cpp")
    reg = _reg([ROUTER, box, pooled, gone])
    facts = {
        # two slots, both busy: one is our session, the other a foreign one
        "box": {"up": True, "host": "up", "models": ["model-a"], "context": {"model-a": 131_072},
                "slots": {"total": 2, "busy": 2}},
        # floor(300000 / 100000) = 3 sessions, nothing running
        "pooled": {"up": True, "host": "up", "models": ["model-b"], "context": {"model-b": 100_000},
                   "load": {"requests_running": 0, "kv_cache_size_tokens": 300_000}},
        "gone": {"up": False, "models": [], "host": "down", "error": "connection refused"},
    }
    _patch(monkeypatch, reg, facts)
    _job(box, "model-a", forced=True, status="running")          # ours, admitted
    _job(box, "model-a", priority="background")                   # waiting
    _job(box, "model-a", priority="normal")                       # waiting, ahead of it by rank
    _job(gone, "model-c", forced=True, status="running")          # ours on a box now down
    conn = server.store.conn
    pooled_id = db.lookup_id(conn, "endpoints", name="pooled", base_url=pooled.url,
                             backend=pooled.backend, host=pooled.host)
    # curve-refine: points 1-3 measured on model-b, 3 x 0.2 = 0.6 < 0.75 holds from 3
    save_capacity(conn, pooled_id, "pooled",
                  Curve({1: 1.0, 2: 0.9, 3: 0.2}, "refined", 7, frozenset({1, 2, 3}), "model-b"))

    out = asyncio.run(server.list_models())
    eps = out["endpoints"]
    b = eps["box"]
    assert b["sessions"] == {"capacity": 2, "ours": 1, "foreign": 1}
    assert b["queue"] == {"count": 2, "head_priority": "normal"}
    # the llama.cpp prior {1: 1.0, 2: 0.35, 3: 0.25} at ours + foreign + 1 = 3
    assert b["predicted_speed"] == pytest.approx(0.25)
    assert b["curve_state"] == {"state": "prior", "points": 0, "measured": [], "holds_from": None}
    assert b["pool"] is None and "pool_sessions" not in b
    # the probe's own fields are still spread as they were
    assert b["slots"] == {"total": 2, "busy": 2} and b["up"] is True

    p = eps["pooled"]
    assert p["sessions"] == {"capacity": 3, "ours": 0, "foreign": 0}
    assert p["queue"] == {"count": 0, "head_priority": None}
    assert p["predicted_speed"] == pytest.approx(1.0)
    assert p["curve_state"] == {"state": "refined", "points": 7, "measured": [1, 2, 3],
                                "holds_from": 3}

    g = eps["gone"]
    # down: no session can start there, but the session we hold there still counts as ours
    assert g["sessions"] == {"capacity": 0, "ours": 1, "foreign": 0}
    assert g["predicted_speed"] is None
    assert g["curve_state"] == {"state": "prior", "points": 0, "measured": [], "holds_from": None}

    for key in ("sessions", "queue", "predicted_speed", "curve_state", "pool"):
        assert key not in eps["auto"]
    assert "facts_error" not in out


def test_router_entry_lists_notices(monkeypatch):
    """§8 "Notices, not triggers": the scorecard's notices, per row, on the router entry. A row
    with none is not listed; an unreadable probe set is named on the entry (the set-upgrade
    notice cannot be computed without it), and the other notices still are."""
    first = Endpoint(name="first", base_url="http://first:8080", backend="llama.cpp")
    second = Endpoint(name="second", base_url="http://second:8080", backend="llama.cpp")
    reg = _reg([ROUTER, first, second])
    facts = {n: {"up": True, "host": "up", "models": [f"model-{n}"],
                 "context": {f"model-{n}": 131_072}, "slots": {"total": 1, "busy": 0}}
             for n in ("first", "second")}
    _patch(monkeypatch, reg, facts)
    conn = server.store.conn
    # a pass whose traffic disagreed with its probes beyond the margin asks for recalibration
    _calibrated(conn, first, "model-first", constants='{"disagreement": 0.5}')
    _calibrated(conn, second, "model-second")

    auto = asyncio.run(server.list_models())["endpoints"]["auto"]
    assert auto["notices"] == [{"endpoint": "first", "served_id": "model-first",
                                "notices": ["needs recalibration"]}]
    assert "probe_set_error" not in auto

    def broken(path=None):
        raise ValueError("core.yaml: missing field 'version'")

    monkeypatch.setattr(calibrate, "load_set", broken)
    auto = asyncio.run(server.list_models())["endpoints"]["auto"]
    assert auto["probe_set_error"] == "ValueError: core.yaml: missing field 'version'"
    assert auto["notices"] == [{"endpoint": "first", "served_id": "model-first",
                                "notices": ["needs recalibration"]}]


def test_list_models_shows_pool_and_pool_sessions(monkeypatch):
    """Amendment 1, §7 "Accepted": a real endpoint in a declared pool names it and shows the
    pool's cap and the sessions used across every member (ours plus the foreign ones each
    member's probe reports); an endpoint in no pool says null and has no pool_sessions."""
    a = Endpoint(name="a", base_url="http://a:8000", backend="vllm", pool="gpu")
    b = Endpoint(name="b", base_url="http://b:8000", backend="vllm", pool="gpu")
    c = Endpoint(name="c", base_url="http://c:8080", backend="llama.cpp")
    reg = _reg([ROUTER, a, b, c], pools={"gpu": {"max_concurrent": 2}})
    facts = {
        # a: our one session, which is the one live request it reports
        "a": {"up": True, "host": "up", "models": ["m-a"], "context": {"m-a": 100_000},
              "load": {"requests_running": 1, "kv_cache_size_tokens": 300_000}},
        # b: one live request that is not ours (a foreign session on the shared GPU)
        "b": {"up": True, "host": "up", "models": ["m-b"], "context": {"m-b": 100_000},
              "load": {"requests_running": 1, "kv_cache_size_tokens": 300_000}},
        "c": {"up": True, "host": "up", "models": ["m-c"], "context": {"m-c": 131_072},
              "slots": {"total": 1, "busy": 0}},
    }
    _patch(monkeypatch, reg, facts)
    _job(a, "m-a", forced=True, status="running")

    eps = asyncio.run(server.list_models())["endpoints"]
    assert eps["a"]["pool"] == "gpu" and eps["a"]["pool_sessions"] == {"cap": 2, "used": 2}
    assert eps["b"]["pool"] == "gpu" and eps["b"]["pool_sessions"] == {"cap": 2, "used": 2}
    assert eps["a"]["sessions"] == {"capacity": 3, "ours": 1, "foreign": 0}
    assert eps["b"]["sessions"] == {"capacity": 3, "ours": 0, "foreign": 1}
    assert eps["c"]["pool"] is None and "pool_sessions" not in eps["c"]


def test_router_entry_lists_calibrated_rows_and_pools(monkeypatch):
    """Amendment 2: the router entry lists the rows with a complete core calibration (endpoint
    name x served id; an incomplete run and an archived row are not calibrated rows) and every
    declared pool with its cap, its members in config order and the sessions used."""
    first = Endpoint(name="first", base_url="http://first:8080", backend="llama.cpp", pool="ups")
    second = Endpoint(name="second", base_url="http://second:8080", backend="llama.cpp", pool="ups")
    third = Endpoint(name="third", base_url="http://third:8080", backend="llama.cpp")
    reg = _reg([ROUTER, first, second, third], pools={"ups": {"max_concurrent": 1}})
    facts = {n: {"up": True, "host": "up", "models": [f"model-{n}"],
                 "context": {f"model-{n}": 131_072}, "slots": {"total": 1, "busy": 0}}
             for n in ("first", "second", "third")}
    _patch(monkeypatch, reg, facts)
    conn = server.store.conn
    _calibrated(conn, first, "model-first")
    _calibrated(conn, second, "model-second", complete=False)
    archived = _calibrated(conn, third, "model-old")
    scorecard.archive_row(conn, archived, "test")
    _calibrated(conn, third, "model-third")
    _job(second, "model-second", forced=True, status="running")

    auto = asyncio.run(server.list_models())["endpoints"]["auto"]
    assert auto["calibrated_rows"] == [{"endpoint": "first", "served_id": "model-first"},
                                       {"endpoint": "third", "served_id": "model-third"}]
    assert auto["pools"] == {"ups": {"cap": 1, "members": ["first", "second"], "used": 1}}
    # the phase-1 fields stay
    assert auto["router"] is True and auto["context_range"] == [131_072, 131_072]


def test_monitor_collector_treats_router_entry_as_router(monkeypatch, tmp_path):
    """§4: the router entry is excluded from the real-endpoint logic of the monitor's collector,
    which shows it as a router entry. Its tick probes the decision server's health only (no
    ``/metrics`` of a base URL it does not have), writes no served model row and no slot,
    request, cache, throughput or idle/busy figure; a timed-out router is never grouped into a
    host's port-22 fallback; and its probe takes no part in the host's node state."""
    conn = db.open_db(tmp_path / "m.db")
    try:
        router = Endpoint(name="auto", base_url="", backend="router",
                          decision_url="http://d3.test:8766", host="box")
        real = Endpoint(name="real", base_url="http://box:8080", backend="llama.cpp", host="box")
        probes = {
            "auto": [{"up": True, "models": ["auto"], "router": True, "decision_model": "d3",
                      "context": {}, "latency_ms": 7},
                     {"up": False, "models": [], "router": True, "host": "unknown",
                      "error": "timed out"}],
            "real": [{"up": False, "models": [], "host": "down", "error": "connection refused"},
                     {"up": False, "models": [], "host": "unknown", "error": "timed out"}],
        }

        class Reg:
            defaults = Defaults()
            endpoints = {"auto": router, "real": real}

            async def probe(self, ep):
                queue = probes[ep.name]
                return queue.pop(0) if len(queue) > 1 else queue[0]

        fetched, asked = [], []

        async def fake_fetch(ep, timeout_s=2.5):
            fetched.append(ep.name)
            return None

        async def fake_port(host, port=22, timeout_s=2.0):
            asked.append(host)
            return "nothing"

        monkeypatch.setattr(collect, "fetch_metrics_doc", fake_fetch)
        monkeypatch.setattr(collect, "probe_port", fake_port)
        reg = Reg()
        rid = collect.endpoint_id(conn, router)
        real_id = collect.endpoint_id(conn, real)

        asyncio.run(collect.tick(reg, conn, router, rid, 1000.0, "router"))
        asyncio.run(collect.tick(reg, conn, real, real_id, 1000.0, "llamacpp"))
        assert fetched == []  # the router has no base URL; the real one was down
        row = conn.execute("SELECT * FROM probes WHERE endpoint_id = ?", (rid,)).fetchone()
        assert row["up"] == 1 and row["latency_ms"] == 7 and row["model_id"] is None
        for col in ("context", "slots_total", "slots_busy", "requests_running", "requests_waiting",
                    "kv_cache_usage", "gen_tps", "since", "state"):
            assert row[col] is None, col
        assert conn.execute("SELECT COUNT(*) FROM models WHERE served_id = 'auto'").fetchone()[0] == 0

        # the overview: the box's only real endpoint is unreachable, so the node is down even
        # though the router entry filed under the same host answers
        snap = read.overview(conn, reg, 1001.0, 3600.0)
        host = next(h for h in snap["hosts"] if h["name"] == "box")
        assert host["state"] == "down"
        entry = next(e for e in host["endpoints"] if e["name"] == "auto")
        assert entry["backend"] == "router" and entry["state"] == "live"
        assert entry["slots"] is None and entry["requests"] is None and entry["model"] is None

        # the loop: both time out on their next tick. The host's fallback is asked once, at the
        # real endpoint's address: the router entry is no member of the host's group (it has no
        # base URL to take an address from, and listed first it would have given its empty one)
        stop = asyncio.Event()

        async def go():
            async def stopper():
                await asyncio.sleep(0.05)
                stop.set()
            await asyncio.gather(collect.run(reg, conn, [router, real], stop, now=lambda: 1002.0,
                                             retention_every_s=3600), stopper())

        asyncio.run(go())
        last = conn.execute("SELECT up, host, error FROM probes WHERE endpoint_id = ?"
                            " ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
        assert (last["up"], last["host"], last["error"]) == (0, "unknown", "timed out")
        assert asked == ["box"]
        assert conn.execute("SELECT COUNT(*) FROM host_probes").fetchone()[0] == 1
    finally:
        conn.close()
