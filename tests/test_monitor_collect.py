"""Story 2.2 item 6: one collection tick and the loop.

A fake registry hands out probe dicts; ``collect.fetch_metrics_doc`` is replaced by one that
returns story-2.1 capture documents in order. The up probe dicts are what the real
``Registry.probe`` returns over the captures (built once per test), so nothing is typed by hand
except the down probe, which is ``classify_connect_failure``'s own output.
"""
import asyncio
import copy
import errno

import httpx
import pytest

from localagents import db
from localagents.monitor import collect, rates, state
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


def _next_pair_doc(first: str, second: str) -> dict:
    """The second capture advanced by the first pair's own increment: the same growth over
    any interval, so a tick that uses it computes the pair's rates."""
    a, b = doc(first), copy.deepcopy(doc(second))
    for k, v in b["totals"].items():
        if k != "since" and isinstance(v, (int, float)):
            b["totals"][k] = v + (v - a["totals"][k])
    return b


def test_a_rate_whose_interval_spans_a_down_probe_has_no_rate_and_no_event(monkeypatch, conn):
    """Story 2.2a row 3 (review P3, ruling B): probe 2's sequence up -> down -> down -> up ->
    up at T, +5, +10, +1000, +1139. The first up tick after the gap compares counters rows
    1000 s apart; the rate is suppressed with ``reason = "gap: 2 down probe(s) in the
    interval"`` and it is NOT written as a restart. The next up interval contains no down
    probe, so it is the pair's own rate over 139 s (33.482 prompt/s, 18.590 gen/s)."""
    up = _real_probe(monkeypatch, STRATA_AI2, "strata")
    down = _down_probe()
    reg = FakeRegistry({"ai2": [up, down, down, up, up]})
    _docs(monkeypatch, [_strata("strata/ai2_metrics_1.json"), _strata("strata/ai2_metrics_2.json"),
                        ("strata", _next_pair_doc("strata/ai2_metrics_1.json", "strata/ai2_metrics_2.json"))])
    calls = []
    real = rates.rates

    def spy(*args, **kw):
        out = real(*args, **kw)
        calls.append((args[2], out))
        return out
    monkeypatch.setattr(rates, "rates", spy)
    ep = Endpoint(name="ai2", base_url=URL, backend="strata")
    eid = collect.endpoint_id(conn, ep)
    for t in (T0, T0 + 5, T0 + 10, T0 + 1000, T0 + 1139):
        asyncio.run(collect.tick(reg, conn, ep, eid, t, "strata"))
    sentence = ("10.0.0.60 is reachable but nothing is listening on 10.0.0.60:8080 "
                "(nothing started there, or the port in models.yaml is wrong)")
    # no `restarted` for the gap: only the transition events
    assert _events(conn) == [(T0 + 5, "down", sentence), (T0 + 1000, "up", None)]
    assert _count(conn, "probes") == 5 and _count(conn, "probe_counters") == 3
    # the gap tick computed no rate; the next one is the clean 139 s pair
    assert len(calls) == 1, calls
    dt_s, rt = calls[0]
    assert dt_s == pytest.approx(139.0)
    assert rt.reset is False and rt.reason is None
    assert rt.prompt_tps == pytest.approx(4654 / 139)
    assert rt.gen_tps == pytest.approx(2584 / 139)
    assert rt.cached_tps == pytest.approx(484756 / 139)


def test_the_reset_sequence_still_writes_exactly_one_restarted(monkeypatch, conn):
    """Story 2.2a row 3, probe 2 (b): ai2_1 -> ai2_2 -> ai2_1 (three up ticks) has no down
    probe in any interval, so the third tick's backwards counters are still a restart."""
    up = _real_probe(monkeypatch, STRATA_AI2, "strata")
    reg = FakeRegistry({"ai2": [up]})
    _docs(monkeypatch, [_strata("strata/ai2_metrics_1.json"), _strata("strata/ai2_metrics_2.json"),
                        _strata("strata/ai2_metrics_1.json")])
    ep = Endpoint(name="ai2", base_url=URL, backend="strata")
    eid = collect.endpoint_id(conn, ep)
    for k in range(3):
        asyncio.run(collect.tick(reg, conn, ep, eid, T0 + 139 * k, "strata"))
    (t, kind, detail), = _events(conn)
    assert (t, kind) == (T0 + 278, "restarted")
    assert detail.startswith("counters went backwards: prompt_tokens 392790 → 388136, "
                             "prompt_tokens_cached 15727997 → 15243241")


def test_a_tick_is_all_or_nothing(monkeypatch, conn):
    """Story 2.2a row 4 (review P5, ruling E): the collect path that runs is ``tick``'s own
    ``immediate()`` block, not ``rows.write`` (deleted). An event insert failing inside a tick
    rolls back that tick's probes row too: no probes row at the failed t, no counters row, no
    event, no open transaction — and the next tick writes normally."""
    up = _real_probe(monkeypatch, STRATA_AI2, "strata")
    reg = FakeRegistry({"ai2": [up, _down_probe(), up]})
    _docs(monkeypatch, [_strata("strata/ai2_metrics_1.json"), _strata("strata/ai2_metrics_2.json")])
    ep = Endpoint(name="ai2", base_url=URL, backend="strata")
    eid = collect.endpoint_id(conn, ep)
    real_event = collect._event
    calls = {"n": 0}

    def failing_event(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("event write failed")
        return real_event(*args, **kw)
    monkeypatch.setattr(collect, "_event", failing_event)
    p1 = asyncio.run(collect.tick(reg, conn, ep, eid, T0, "strata"))  # no transition event
    with pytest.raises(RuntimeError, match="event write failed"):
        asyncio.run(collect.tick(reg, conn, ep, eid, T0 + 5, "strata"))  # down: event raises
    assert _count(conn, "probes") == 1 and _count(conn, "probe_counters") == 1
    assert conn.execute("SELECT COUNT(*) FROM probes WHERE t = ?", (T0 + 5,)).fetchone()[0] == 0
    assert _events(conn) == [] and not conn.in_transaction
    p3 = asyncio.run(collect.tick(reg, conn, ep, eid, T0 + 10, "strata"))
    assert p3 > p1
    assert _count(conn, "probes") == 2 and _count(conn, "probe_counters") == 2
    assert not conn.in_transaction


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


def _parking_sleeps(monkeypatch):
    """``_sleep_or_stop`` replaced by a recorder that never really waits: it returns at once
    for the endpoint's own sleeps and parks on ``stop`` for the prune task's 3600, exactly
    like the real sleep, so the prune task cannot run ahead of the endpoint task."""
    sleeps = []

    async def record(stop, seconds):
        sleeps.append(seconds)
        if seconds == 3600:
            await stop.wait()
    monkeypatch.setattr(collect, "_sleep_or_stop", record)
    return sleeps


def test_a_failing_tick_is_logged_and_the_loop_goes_on(monkeypatch, conn, capsys):
    """Row 4 (review 2.3, ruling 2026-10-29: the interval line joins the tick's guard): a
    failed tick is one stderr line and a DORMANT_S back-off, then the next attempt ticks."""
    up = _real_probe(monkeypatch, STRATA_AI2, "strata")
    reg = FakeRegistry({"a": [RuntimeError("database is locked"), up]})
    _docs(monkeypatch, [_strata("strata/ai2_metrics_1.json")])
    monkeypatch.setattr(state, "interval_s", lambda *a, **k: 0)
    sleeps = _parking_sleeps(monkeypatch)
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
    err = capsys.readouterr().err
    assert "a: tick failed: RuntimeError: database is locked" in err
    assert err.count("tick failed") == 1
    assert state.DORMANT_S in sleeps  # the back-off after the failed attempt
    assert _count(conn, "probes") == 1  # and the loop did go on: the second attempt ticked


def test_an_error_between_ticks_does_not_kill_the_loop(monkeypatch, conn, capsys):
    """Row 4, the brief's reproduction (review 2.3 probe 3): the cadence read/interval_s
    used to sit outside the guard, so one TypeError (a quoted ``window_s``) escaped the
    task and ``gather`` killed the process. Now: one stderr line, the loop ticks again,
    and ``run`` returns when ``stop`` is set."""
    up = _real_probe(monkeypatch, STRATA_AI2, "strata")
    reg = FakeRegistry({"a": [up]})
    _docs(monkeypatch, [_strata("strata/ai2_metrics_1.json"), _strata("strata/ai2_metrics_2.json")])
    boom = [True]

    def flaky(last_up_t, now, window_s=state.ATTENTION_WINDOW_S):
        if boom[0]:
            boom[0] = False
            raise TypeError("'<=' not supported between instances of 'float' and 'str'")
        return 0
    monkeypatch.setattr(state, "interval_s", flaky)
    sleeps = _parking_sleeps(monkeypatch)
    ep = Endpoint(name="a", base_url=URL, backend="strata")

    async def main():
        stop = asyncio.Event()
        orig = reg.probe

        async def probe(e):
            out = await orig(e)
            if reg.calls["a"] >= 2:
                stop.set()
            return out
        reg.probe = probe
        await asyncio.wait_for(collect.run(reg, conn, [ep], stop, now=lambda: T0), 10)
    asyncio.run(main())
    err = capsys.readouterr().err
    assert "a: tick failed: TypeError" in err
    assert err.count("tick failed") == 1  # one line, not a crash and not a repeated log
    assert state.DORMANT_S in sleeps  # the back-off after the between-ticks error
    assert reg.calls["a"] == 2  # the loop ticked again after it
    assert _count(conn, "probes") == 2  # the healthy second tick wrote its row


def test_a_failed_tick_backs_off_once_then_recovers_the_fast_cadence(monkeypatch, conn):
    """Row 4 changed the story-2.2 contract (the read and interval_s now sit INSIDE the
    tick's guard): a failed tick no longer re-reads the newest up probe — it costs exactly
    one DORMANT_S back-off — and the next successful tick is back on the fast cadence."""
    up = _real_probe(monkeypatch, STRATA_AI2, "strata")
    reg = FakeRegistry({"a": [up, RuntimeError("database is locked"), up]})
    _docs(monkeypatch, [_strata("strata/ai2_metrics_1.json"), _strata("strata/ai2_metrics_2.json")])
    waits = []

    def record(last_up_t, now, window_s=state.ATTENTION_WINDOW_S):
        waits.append(last_up_t)
        return 0
    monkeypatch.setattr(state, "interval_s", record)
    sleeps = _parking_sleeps(monkeypatch)
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
    assert waits == [T0, T0]  # the failed middle attempt never consulted the cadence
    assert sleeps.count(state.DORMANT_S) == 1  # one back-off, then fast cadence again


def test_run_passes_the_attention_window_to_the_cadence(monkeypatch, conn):
    """Story 2.3 item 5: run(window_s=1) on an endpoint whose newest up probe is 2 s old:
    the window flows from run into interval_s, so the endpoint sleeps 60 (dormant), not 5.
    Stop after one tick; the sleep arguments are asserted through a monkeypatched
    _sleep_or_stop (the prune task's retention sleep is in there too)."""
    ep = Endpoint(name="a", base_url=URL, backend="strata")
    eid = collect.endpoint_id(conn, ep)
    with db.immediate(conn):
        conn.execute("INSERT INTO probes (t, endpoint_id, up) VALUES (?, ?, 1)", (998.0, eid))
    reg = FakeRegistry({"a": [{"up": False, "models": [], "host": "down",
                                "error": "a is unreachable (EHOSTUNREACH)"}]})
    sleeps = []

    async def record(stop, seconds):
        # Waits on stop like the real _sleep_or_stop, so the prune task (whose retention
        # sleep is 3600) can never stop the run before the endpoint task has ticked once.
        sleeps.append(seconds)
        if seconds != 3600:
            stop.set()
        await stop.wait()
    monkeypatch.setattr(collect, "_sleep_or_stop", record)
    stop = asyncio.Event()
    asyncio.run(collect.run(reg, conn, [ep], stop, now=lambda: 1000.0, window_s=1))
    assert 60 in sleeps and 5 not in sleeps
