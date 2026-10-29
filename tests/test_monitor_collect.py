"""Story 2.2 item 6: one collection tick and the loop.

A fake registry hands out probe dicts; ``collect.fetch_metrics_doc`` is replaced by one that
returns story-2.1 capture documents in order. The up probe dicts are what the real
``Registry.probe`` returns over the captures (built once per test), so nothing is typed by hand
except the down probe, which is ``classify_connect_failure``'s own output.
"""
import asyncio
import errno

import httpx
import pytest

from localagents import db
from localagents.monitor import collect, state
from localagents.registry import Defaults, Endpoint, Registry, classify_connect_failure

from monitor_fixtures import LLAMACPP, STRATA_AI2, doc, raw, serve

URL = "http://10.0.0.60:8080"
T0 = 1793288065.0


class FakeRegistry:
    """``probe`` returns the next canned dict for the endpoint (the last one repeats)."""

    def __init__(self, probes: dict[str, list]):
        self.defaults = Defaults()
        self.probes = {k: list(v) for k, v in probes.items()}
        self.calls: dict[str, int] = {}

    async def probe(self, ep):
        self.calls[ep.name] = self.calls.get(ep.name, 0) + 1
        queue = self.probes[ep.name]
        out = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(out, BaseException):
            raise out
        return out


def _docs(monkeypatch, docs):
    """collect.fetch_metrics_doc returns these documents in order; records each call."""
    calls = []

    async def fake(ep, timeout_s=2.5):
        calls.append(ep.name)
        return docs.pop(0) if docs else None
    monkeypatch.setattr(collect, "fetch_metrics_doc", fake)
    return calls


def _real_probe(monkeypatch, routes, backend):
    serve(monkeypatch, URL, routes)
    reg = Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={})
    return asyncio.run(reg.probe(Endpoint(name="x", base_url=URL, backend=backend)))


def _down_probe():
    try:
        try:
            raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
        except OSError as e:
            raise httpx.ConnectError("All connection attempts failed") from e
    except httpx.ConnectError as e:
        return {"up": False, "models": [], **classify_connect_failure(e, URL)}


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "m.db")
    yield c
    c.close()


def _strata(path):
    return ("strata", doc(path))


def _prom(path):
    return ("prometheus", raw(path).decode())


def _events(conn):
    return [tuple(r) for r in conn.execute("SELECT t, kind, detail FROM endpoint_events ORDER BY id")]


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_the_endpoint_row_is_the_one_jobstore_new_uses(conn):
    """server.py passes JobStore.new base_url=res.endpoint.url (no trailing slash) and the raw
    backend string: the monitor keys the endpoints row the same way, so both share one row."""
    ep = Endpoint(name="ai2", base_url=URL + "/", backend="Strata", host="ai2")
    eid = collect.endpoint_id(conn, ep)
    assert eid == db.lookup_id(conn, "endpoints", name="ai2", base_url=URL, backend="Strata", host="ai2")
    assert _count(conn, "endpoints") == 1


def test_two_ticks_on_the_ai2_pair_write_two_probes_and_no_event(monkeypatch, conn):
    probe = _real_probe(monkeypatch, STRATA_AI2, "strata")
    reg = FakeRegistry({"ai2": [probe]})
    _docs(monkeypatch, [_strata("strata/ai2_metrics_1.json"), _strata("strata/ai2_metrics_2.json")])
    ep = Endpoint(name="ai2", base_url=URL, backend="strata")
    eid = collect.endpoint_id(conn, ep)
    p1 = asyncio.run(collect.tick(reg, conn, ep, eid, T0, "strata"))
    p2 = asyncio.run(collect.tick(reg, conn, ep, eid, T0 + 139, "strata"))
    assert p2 > p1
    assert _count(conn, "probes") == 2 and _count(conn, "probe_counters") == 2 and _count(conn, "probe_hardware") == 2
    assert _events(conn) == []
    model = conn.execute("SELECT m.served_id FROM probes p JOIN models m ON m.id = p.model_id WHERE p.id = ?",
                         (p2,)).fetchone()[0]
    assert model == "qwen3.8-flash-next-unsloth-ud-q4_k_xl"
    # pass 2's totals landed in the second counters row: output_tokens 222979
    assert conn.execute("SELECT generated_tokens FROM probe_counters WHERE probe_id = ?", (p2,)).fetchone()[0] == 222979.0


def test_two_ticks_on_the_llamacpp_pair_write_a_restarted_event(monkeypatch, conn):
    probe = _real_probe(monkeypatch, LLAMACPP, "llama.cpp")
    reg = FakeRegistry({"local": [probe]})
    _docs(monkeypatch, [_prom("llamacpp/llamacpp_metrics_1.txt"), _prom("llamacpp/llamacpp_metrics_2.txt")])
    ep = Endpoint(name="local", base_url=URL, backend="llama.cpp")
    eid = collect.endpoint_id(conn, ep)
    asyncio.run(collect.tick(reg, conn, ep, eid, T0, "llamacpp"))
    asyncio.run(collect.tick(reg, conn, ep, eid, T0 + 139, "llamacpp"))
    (t, kind, detail), = _events(conn)
    assert (t, kind) == (T0 + 139, "restarted")
    assert detail.startswith("counters went backwards: prompt_tokens 4705 → 0, prompt_tokens_cached 18341 → 0")


def test_since_changing_writes_a_restarted_event(monkeypatch, conn):
    probe = _real_probe(monkeypatch, STRATA_AI2, "strata")
    reg = FakeRegistry({"ai2": [probe]})
    moved = doc("strata/ai2_metrics_2.json")
    moved["totals"]["since"] = 1793288100.0
    _docs(monkeypatch, [_strata("strata/ai2_metrics_1.json"), ("strata", moved)])
    ep = Endpoint(name="ai2", base_url=URL, backend="strata")
    eid = collect.endpoint_id(conn, ep)
    asyncio.run(collect.tick(reg, conn, ep, eid, T0, "strata"))
    asyncio.run(collect.tick(reg, conn, ep, eid, T0 + 139, "strata"))
    assert _events(conn) == [(T0 + 139, "restarted", "since changed (1793229893.2645304 → 1793288100.0)")]


def test_up_then_down_then_up_writes_down_with_the_sentence_then_up(monkeypatch, conn):
    up = _real_probe(monkeypatch, STRATA_AI2, "strata")
    down = _down_probe()
    reg = FakeRegistry({"ai2": [up, down, up]})
    fetched = _docs(monkeypatch, [_strata("strata/ai2_metrics_1.json"), _strata("strata/ai2_metrics_2.json")])
    ep = Endpoint(name="ai2", base_url=URL, backend="strata")
    eid = collect.endpoint_id(conn, ep)
    for k in range(3):
        asyncio.run(collect.tick(reg, conn, ep, eid, T0 + 5 * k, "strata"))
    sentence = ("10.0.0.60 is reachable but nothing is listening on 10.0.0.60:8080 "
                "(nothing started there, or the port in models.yaml is wrong)")
    assert _events(conn) == [(T0 + 5, "down", sentence), (T0 + 10, "up", None)]
    assert fetched == ["ai2", "ai2"]  # a down probe does not ask for /metrics
    row = conn.execute("SELECT up, host, error, model_id FROM probes WHERE t = ?", (T0 + 5,)).fetchone()
    assert tuple(row) == (0, "up", sentence, None)
    # the counters comparison skips the down row: pass 1 -> pass 2 grew, no restart
    assert _count(conn, "probe_counters") == 2


def test_a_cold_start_down_writes_no_event(monkeypatch, conn):
    reg = FakeRegistry({"ai2": [_down_probe()]})
    _docs(monkeypatch, [])
    ep = Endpoint(name="ai2", base_url=URL, backend="strata")
    eid = collect.endpoint_id(conn, ep)
    asyncio.run(collect.tick(reg, conn, ep, eid, T0, "strata"))
    asyncio.run(collect.tick(reg, conn, ep, eid, T0 + 60, "strata"))
    assert _events(conn) == [] and _count(conn, "probes") == 2


def test_the_loop_runs_two_ticks_for_two_endpoints_and_stops_on_the_event(monkeypatch, conn):
    up = _real_probe(monkeypatch, STRATA_AI2, "strata")
    reg = FakeRegistry({"a": [up], "b": [_down_probe()]})
    _docs(monkeypatch, [_strata("strata/ai2_metrics_1.json"), _strata("strata/ai2_metrics_2.json")])
    waits = []

    def no_wait(last_up_t, now, window_s=state.ATTENTION_WINDOW_S):
        waits.append(last_up_t)
        return 0
    monkeypatch.setattr(state, "interval_s", no_wait)
    eps = [Endpoint(name="a", base_url=URL, backend="strata"), Endpoint(name="b", base_url=URL, backend="strata")]
    clock = iter(T0 + k for k in range(1000))

    async def main():
        stop = asyncio.Event()
        orig = reg.probe

        async def probe(ep):
            out = await orig(ep)
            if sum(reg.calls.values()) >= 4:
                stop.set()
            return out
        reg.probe = probe
        await asyncio.wait_for(collect.run(reg, conn, eps, stop, now=lambda: next(clock)), 10)
    asyncio.run(main())
    assert reg.calls == {"a": 2, "b": 2}
    per_ep = dict(conn.execute("SELECT e.name, COUNT(*) FROM probes p JOIN endpoints e ON e.id = p.endpoint_id"
                               " GROUP BY e.name").fetchall())
    assert per_ep == {"a": 2, "b": 2}
    # the cadence reads the newest up probe: "a" was up (a time), "b" never (None)
    assert None in waits and any(w is not None for w in waits)


def test_a_failing_tick_is_logged_and_the_loop_goes_on(monkeypatch, conn, capsys):
    up = _real_probe(monkeypatch, STRATA_AI2, "strata")
    reg = FakeRegistry({"a": [RuntimeError("database is locked"), up]})
    _docs(monkeypatch, [_strata("strata/ai2_metrics_1.json")])
    monkeypatch.setattr(state, "interval_s", lambda *a, **k: 0)
    ep = Endpoint(name="a", base_url=URL, backend="strata")

    async def main():
        stop = asyncio.Event()
        orig = reg.probe

        async def probe(e):
            try:
                return await orig(e)
            finally:
                if reg.calls["a"] >= 2:
                    stop.set()
        reg.probe = probe
        await asyncio.wait_for(collect.run(reg, conn, [ep], stop), 10)
    asyncio.run(main())
    assert "a: tick failed: RuntimeError: database is locked" in capsys.readouterr().err
    assert _count(conn, "probes") == 1


def test_a_failed_tick_keeps_a_live_endpoint_on_the_fast_cadence(monkeypatch, conn):
    """After an up probe, a tick that fails still reads the newest up probe for the cadence:
    a transient database error never drops a live endpoint to the dormant 60 s."""
    up = _real_probe(monkeypatch, STRATA_AI2, "strata")
    reg = FakeRegistry({"a": [up, RuntimeError("database is locked"), up]})
    _docs(monkeypatch, [_strata("strata/ai2_metrics_1.json"), _strata("strata/ai2_metrics_2.json")])
    waits = []

    def record(last_up_t, now, window_s=state.ATTENTION_WINDOW_S):
        waits.append(last_up_t)
        return 0
    monkeypatch.setattr(state, "interval_s", record)
    ep = Endpoint(name="a", base_url=URL, backend="strata")

    async def main():
        stop = asyncio.Event()
        orig = reg.probe

        async def probe(e):
            try:
                return await orig(e)
            finally:
                if reg.calls["a"] >= 3:
                    stop.set()
        reg.probe = probe
        await asyncio.wait_for(collect.run(reg, conn, [ep], stop, now=lambda: T0), 10)
    asyncio.run(main())
    assert waits == [T0, T0, T0]
