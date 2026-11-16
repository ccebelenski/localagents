"""A blocking wait at its cap (the wait-cap defect, 2026-11-15).

The report: "when wait_s equalled timeout_s, the blocking call twice returned a record with
status: running at exactly the cap, and the job then ended a few seconds later as timeout or
succeeded." By construction, not a race: the wait's clock starts at admission, the session's
deadline is armed after the runner's preamble (the metrics probe, the worktree), and the SDK
closes its subprocess (a 5 s graceful wait, ~20 s worst case) before run_job writes `timeout`.

The rule: a blocking wait whose cap passes while the job is already over its own session
deadline, or done but not yet settled, waits for the settled record; it never hands back
`running` for a job whose session has ended or is being torn down. A wait whose cap passes
while the session is genuinely still inside its own deadline returns the unsettled record
as before."""

import asyncio
import time

import pytest

from localagents import jobs, runner, server
from localagents.registry import Defaults, Endpoint, Registry

SERVING = {"e": ["served-model"]}
TEARDOWN_S = 0.5  # the fake SDK's close after a cancel: the CLI's graceful exit


@pytest.fixture
def live(monkeypatch, tmp_path):
    """run_agent over the REAL runner.run_job: only the model side is faked (query, the
    metrics probe), so the job's own clocks are the runner's."""
    endpoints = {n: Endpoint(name=n, base_url=f"http://{n}:8080", backend="llama.cpp") for n in SERVING}
    r = Registry(path=None, endpoints=endpoints, defaults=Defaults(job_retention_days=0), raw={}, local={})

    async def probe(self, ep):
        return {"up": True, "models": SERVING[ep.name]}

    async def no_start():
        return 0

    async def no_metrics(*a, **kw):
        return None

    monkeypatch.setattr(Registry, "probe", probe)
    monkeypatch.setattr(server, "_reg", lambda: r)
    monkeypatch.setattr(server, "run_job", runner.run_job)
    monkeypatch.setattr(server.shim, "start", no_start)
    monkeypatch.setattr(runner, "fetch_metrics", no_metrics)
    return r


def _slow_teardown_query(monkeypatch, teardown_s=TEARDOWN_S):
    """A session that never ends on its own; cancelled (its deadline, a cancel_job), it takes
    `teardown_s` to close, as the SDK's close() waits for the CLI before the runner sees the
    cancel land."""

    def fake_query(prompt, options):
        async def session():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(teardown_s)
                raise
            yield

        return session()

    monkeypatch.setattr(runner, "query", fake_query)


def test_run_agent_with_wait_s_equal_to_timeout_returns_the_settled_timeout(live, monkeypatch):
    """The defect itself: wait_s == timeout_s. The session's deadline fires, its teardown
    takes a moment; the blocking call hands back the settled `timeout` record, not `running`."""
    _slow_teardown_query(monkeypatch)
    out = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=1, timeout_s=1))
    assert out["status"] == "timeout", out["status"]
    assert out["error"] == "exceeded timeout of 1s"
    assert out["finished_at"] is not None


class _Ctx:
    """Stand-in for the MCP Context: records when each progress notification was sent."""

    def __init__(self):
        self.at = []

    async def report_progress(self, progress, total=None, message=None):
        self.at.append(time.monotonic())


def test_a_wait_shorter_than_the_timeout_returns_running_at_its_cap(live, monkeypatch):
    """The cap stands for a session still inside its own deadline: wait_s=1 on a 60 s job
    comes back `running` at 1 s, not held for the session."""
    _slow_teardown_query(monkeypatch)

    async def go():
        t0 = time.monotonic()
        out = await server.run_agent(task="t", model="served-model", wait_s=1, timeout_s=60)
        return out, time.monotonic() - t0

    out, took = asyncio.run(go())
    assert out["status"] == "running"
    assert 0.9 <= took < 3, took  # not held: the teardown would only start at 60 s


def test_the_progress_callback_keeps_firing_during_the_extra_wait(live, monkeypatch):
    """Claude Code's idle timeout: the hold past the cap reports progress like the wait does."""
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.05)
    _slow_teardown_query(monkeypatch)
    ctx = _Ctx()

    async def go():
        t0 = time.monotonic()
        out = await server.run_agent(task="t", model="served-model", wait_s=1, timeout_s=1, ctx=ctx)
        return out, t0, time.monotonic()

    out, t0, t1 = asyncio.run(go())
    assert out["status"] == "timeout"
    after_cap = [t for t in ctx.at if t > t0 + 1.1]
    assert len(after_cap) >= 3, (t1 - t0, [round(t - t0, 2) for t in ctx.at])


def test_a_cancelled_job_at_the_cap_comes_back_cancelled(live, monkeypatch):
    """cancel_job lands just before the wait's cap and the session's teardown spans it: the
    blocking call hands back the settled `cancelled` record."""
    _slow_teardown_query(monkeypatch)

    async def go():
        waiter = asyncio.create_task(
            server.run_agent(task="t", model="served-model", wait_s=1, timeout_s=60))
        await asyncio.sleep(0.8)
        jid = next(iter(server.store.jobs))
        cancelled = asyncio.create_task(server.cancel_job(jid))
        return await waiter, await cancelled

    out, cancelled = asyncio.run(go())
    assert out["status"] == "cancelled" and out["error"] == "cancelled"
    assert cancelled["status"] == "cancelled" and "note" not in cancelled


def test_a_session_whose_result_arrived_is_held_while_the_cli_exits(live, monkeypatch):
    """Step 3's case: the session ends on its own (its ResultMessage arrives) inside its
    deadline, and the CLI's exit spans the wait's cap: the record is `succeeded`, not
    `running`."""
    from claude_agent_sdk import ResultMessage

    def fake_query(prompt, options):
        async def session():
            await asyncio.sleep(0.8)
            yield ResultMessage(subtype="success", duration_ms=5, duration_api_ms=4, is_error=False,
                                num_turns=1, session_id="sess-1", usage=None, result="done")
            await asyncio.sleep(0.5)  # the CLI flushes its session file and exits

        return session()

    monkeypatch.setattr(runner, "query", fake_query)
    out = asyncio.run(server.run_agent(task="t", model="served-model", wait_s=1, timeout_s=60))
    assert out["status"] == "succeeded" and out["result"] == "done"


# ---- the store's rule, without a runner ----


def _job(**session):
    job = server.store.new(task="t", model="m", endpoint="e", base_url="http://e:8080", backend="llama.cpp",
                           host="local", cwd="/tmp", run_cwd="/tmp", isolation="none")
    job.status = "running"
    for k, v in session.items():
        setattr(job, k, v)
    return job


def _armed(started_ago, timeout):
    """A session armed `started_ago` seconds before now with `timeout`."""
    return {"_session_deadline": time.monotonic() - started_ago + timeout, "_session_timeout_s": float(timeout)}


def test_held_at_cap_table():
    """Every row of the rule, at a cap 'now' for a wait that began at `start` asking `span`."""
    now = time.monotonic()
    rows = [
        # (job state, wait_start, wait_span, held)
        ({}, now - 5, 5, False),  # no session yet: queued, or the preamble
        (_armed(10, 60), now - 5, 5, False),  # inside its deadline, wait shorter than the job
        (_armed(70, 60), now - 5, 5, True),  # past its own deadline: torn down
        (_armed(4, 5), now - 5, 5, True),  # began after the wait, asked for no more: covered
        (_armed(4, 6), now - 5, 5, False),  # began after the wait, but longer than it asked
        ({"_cancel_requested": True}, now - 5, 5, True),
        ({"_session_over": True, **_armed(1, 60)}, now - 5, 5, True),
        ({"status": "timeout"}, now - 5, 5, True),  # done, not settled
    ]
    for state, start, span, want in rows:
        job = _job(**state)
        assert jobs.held_at_cap(job, start, span, now) is want, state
    settled = _job(**_armed(70, 60))
    settled._done.set()
    assert jobs.held_at_cap(settled, now - 5, 5, now) is False  # nothing left to wait for


def test_the_backstop_returns_a_teardown_that_never_settles(monkeypatch):
    """A hung teardown cannot hold the call forever: past SETTLE_BACKSTOP_S the wait returns
    what there is, as at a plain cap."""
    monkeypatch.setattr(jobs, "SETTLE_BACKSTOP_S", 0.3)
    job = _job(**_armed(70, 60))  # past its deadline; nothing will ever settle it

    async def go():
        t0 = time.monotonic()
        got = await server.store.wait(job.id, 0.1, settle=True)
        return got, time.monotonic() - t0

    got, took = asyncio.run(go())
    assert got.status == "running" and not got.settled
    assert 0.4 <= took < 3, took  # the cap plus the backstop, not 120 s


def test_without_settle_the_cap_stands(monkeypatch):
    """cancel_job's short wait does not pass `settle`: its 15 s and its note stay as they were."""
    job = _job(**_armed(70, 60))

    async def go():
        t0 = time.monotonic()
        await server.store.wait(job.id, 0.1)
        return time.monotonic() - t0

    assert asyncio.run(go()) < 0.3


def test_wait_many_all_holds_only_the_jobs_held_at_the_cap(monkeypatch):
    """mode="all": the torn-down job is waited for until it settles; the job still inside its
    own deadline is not, and comes back unsettled once the held one has settled."""
    torn = _job(**_armed(70, 60))
    live_job = _job(**_armed(1, 600))

    async def settle_later():
        await asyncio.sleep(0.4)
        torn.status = "timeout"
        torn._done.set()

    async def go():
        t0 = time.monotonic()
        asyncio.create_task(settle_later())
        got = await server.store.wait_many([torn.id, live_job.id], 0.1, mode="all", settle=True)
        return got, time.monotonic() - t0

    (a, b), took = asyncio.run(go())
    assert a.settled and a.status == "timeout" and not b.settled
    assert 0.35 <= took < 3, took


def test_wait_many_any_holds_for_a_torn_down_job(monkeypatch):
    """mode="any": nothing settled at the cap, one job is being torn down: the wait goes on
    until it settles."""
    torn = _job(_cancel_requested=True, **_armed(1, 600))
    live_job = _job(**_armed(1, 600))

    async def settle_later():
        await asyncio.sleep(0.4)
        torn.status = "cancelled"
        torn._done.set()

    async def go():
        asyncio.create_task(settle_later())
        return await server.store.wait_many([torn.id, live_job.id], 0.1, mode="any", settle=True)

    a, b = asyncio.run(go())
    assert a.settled and not b.settled


def test_wait_many_with_nothing_held_returns_at_the_cap(monkeypatch):
    """No job held: wait_many returns at its cap, settle or not (mode semantics unchanged)."""
    jobs_ = [_job(**_armed(1, 600)), _job()]

    async def go():
        t0 = time.monotonic()
        await server.store.wait_many([j.id for j in jobs_], 0.1, mode="all", settle=True)
        return time.monotonic() - t0

    assert asyncio.run(go()) < 0.3


def test_wait_jobs_returns_the_torn_down_job_as_done(monkeypatch):
    """The tool: wait_jobs at its cap lists a job settled during the hold under done; its belt
    leaves room for the hold."""
    torn = _job(**_armed(70, 60))

    async def settle_later():
        await asyncio.sleep(0.3)
        torn.status = "timeout"
        torn._done.set()

    async def go():
        asyncio.create_task(settle_later())
        return await server.wait_jobs([torn.id], timeout_s=0.1)

    out = asyncio.run(go())
    assert out["outcome"] == "all_done" and out["done"][0]["status"] == "timeout"


def test_wait_job_holds_a_job_past_its_deadline(monkeypatch):
    """The tool: wait_job at its cap waits for the settle of a job past its own deadline."""
    torn = _job(**_armed(70, 60))

    async def settle_later():
        await asyncio.sleep(0.3)
        torn.status = "timeout"
        torn._done.set()

    async def go():
        asyncio.create_task(settle_later())
        return await server.wait_job(torn.id, timeout_s=0.1)

    assert asyncio.run(go())["status"] == "timeout"
