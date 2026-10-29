"""``localagents smoke``: the online acceptance script (monitor spec §7).

No Claude session: direct ``/v1/messages`` requests to a named endpoint, one real job through
``run_job`` with a fixed brief so the job/request/tool-call tables fill through the real shim
and runner, and a temporary endpoint on a port nothing listens on so the down state is checked
against the kernel's real refusal. Everything it found is one printed checklist of rows and
states; a missing row exits non-zero. The monitor web app is not involved (its pages come with
stories 3.1-3.4), and this is the monitor side only: the MCP modules import nothing here
(the monitor → MCP direction is allowed, never the reverse).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .. import db, derive
from ..jobs import JobStore
from ..registry import Endpoint, ModelUnavailable, Registry, backend_kind, classify_connect_failure
from ..runner import run_job
from ..shim import Shim
from . import collect, state


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


class Checklist:
    """The checks in the order they ran; ``failed`` counts the MISSING ones."""

    def __init__(self) -> None:
        self.checks: list[Check] = []

    def append(self, check: Check) -> None:
        self.checks.append(check)

    def extend(self, checks: list[Check]) -> None:
        self.checks.extend(checks)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.ok)

    def render(self) -> str:
        """One line per check: ``[ok]``/``[MISSING]``, the name, and the detail (the status,
        counts or sentence that made it so)."""
        return "\n".join(
            f"[{'ok' if c.ok else 'MISSING'}] {c.name}" + (f": {c.detail}" if c.detail else "")
            for c in self.checks
        )


def exit_code(checklist: Checklist) -> int:
    """0 when every check is ok, 1 when any is MISSING (spec §7: non-zero exit on any missing
    row)."""
    return 1 if checklist.failed else 0


# ---------------------------------------------------------------- item 2: direct requests

_PING = "Reply with the single word pong."


async def direct_requests(
    ep: Endpoint,
    model: str,
    n: int = 3,
    timeout_s: float = 60,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[Check]:
    """``n`` short ``POST /v1/messages`` (``max_tokens`` 16, one user message, ``stream`` False)
    straight to the endpoint — no shim, no job: the model answers or the port refuses. One check
    per request: status, ``usage.output_tokens`` and wall latency; a connection failure is a
    MISSING check carrying ``classify_connect_failure``'s sentence (the same one the collector
    stores). ``transport`` is injectable so the unit test runs it over ``httpx.MockTransport``.
    """
    body = {
        "model": model,
        "max_tokens": 16,
        "stream": False,
        "messages": [{"role": "user", "content": _PING}],
    }
    checks: list[Check] = []
    async with httpx.AsyncClient(
        timeout=timeout_s, headers=ep.auth_headers(), transport=transport
    ) as c:
        for i in range(1, n + 1):
            t0 = time.monotonic()
            try:
                r = await c.post(f"{ep.url}/v1/messages", json=body)
            except httpx.HTTPError as e:
                checks.append(Check(f"direct request {i}", False, classify_connect_failure(e, ep.url)["error"]))
                continue
            ms = round((time.monotonic() - t0) * 1000)
            if r.status_code != 200:
                checks.append(Check(f"direct request {i}", False, f"HTTP {r.status_code} in {ms} ms: {r.text[:200]}"))
                continue
            try:
                data = r.json()
            except ValueError:
                checks.append(Check(f"direct request {i}", False, f"HTTP 200 in {ms} ms: not JSON"))
                continue
            out = (data.get("usage") or {}).get("output_tokens")
            checks.append(Check(f"direct request {i}", True, f"HTTP 200, output_tokens={out}, {ms} ms"))
    return checks


# ---------------------------------------------------------------- item 3: the real job

# The brief is fixed (monitor spec §7): run_job's own harness work must not bend to whatever
# the operator is testing that day, so the checklist lines are comparable run to run.
FIXED_BRIEF = "run `date` with Bash and report it"


async def real_job(
    reg: Registry,
    store: JobStore,
    shim: Shim,
    endpoint: str,
    model: str,
    timeout_s: int,
) -> list[Check]:
    """One real job through the real runner and shim on the named endpoint and model, so the
    ``jobs``/``requests``/``tool_calls`` tables fill the way any server's do. The job is built
    and run exactly as ``run_agent`` builds one (same backend/host keys, task, isolation none,
    cwd = this repo, the shim started once, run_agent's default permission mode, max_turns 4,
    Bash only, the fixed brief) — the smoke is the same machine the MCP server runs on.

    The checks then read the rows back: a terminal ``succeeded`` job row, a 200 request row, a
    Bash tool-call row, a date-shaped transcript line (what ``date`` prints), and non-zero
    usage as ``derive.summary_from_row`` rebuilds it — the monitor's read path over the run's
    rows.
    """
    res = await reg.resolve(model=model, endpoint=endpoint)
    work = str(os.getcwd())  # cwd = the repo, run_agent's default when cwd is not given
    await shim.start()
    job = store.new(
        task=FIXED_BRIEF,
        model=res.served_model,
        endpoint=res.endpoint.name,
        base_url=res.endpoint.url,
        cwd=work,
        run_cwd=work,
        isolation="none",
        backend=res.endpoint.backend,
        host=res.endpoint.host,
        worktree=None,
        dump=reg.defaults.dump_requests,
        log_events=reg.defaults.log_events or os.environ.get("LOCALAGENTS_LOG_EVENTS") == "1",
        context=res.context,
    )
    await run_job(
        job, reg, res,
        permission_mode=None,
        max_turns=4,
        allowed_tools=["Bash"],
        system_prompt_append=None,
        resume_session=None,
        timeout_s=timeout_s,
        shim_base_url=shim.base_url_for(job.id),
    )

    conn = store.conn
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    checks: list[Check] = [Check(
        "job row terminal and succeeded",
        row is not None and row["status"] == "succeeded",
        f"job {job.name}: status={row['status'] if row is not None else 'no row'}"
        + (f", error={row['error']}" if row is not None and row["error"] else ""),
    )]

    n200 = conn.execute(
        "SELECT COUNT(*) FROM requests WHERE job_id = ? AND http_status = 200", (job.id,)
    ).fetchone()[0]
    n_req = conn.execute("SELECT COUNT(*) FROM requests WHERE job_id = ?", (job.id,)).fetchone()[0]
    checks.append(Check(
        "requests row for the job (200)", n200 >= 1,
        f"{n_req} request row(s), {n200} with http_status 200",
    ))

    n_bash = conn.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE job_id = ? AND tool = 'Bash'", (job.id,)
    ).fetchone()[0]
    n_tools = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE job_id = ?", (job.id,)).fetchone()[0]
    checks.append(Check(
        "tool_calls row for the job (Bash)", n_bash >= 1,
        f"{n_tools} tool-call row(s), {n_bash} for Bash",
    ))

    # the year must appear where ``date``'s report lands — an assistant line or a tool result.
    # The whole transcript would also match the runner's own [start]/[init] lines when the cwd
    # is date-shaped (a worktree named 20261029-...: the live run showed exactly that).
    report = [ln for ln in job.transcript_tail(0).splitlines() if ln.startswith(("[assistant]", "[result]"))]
    date_line = next((ln for ln in report if re.search(r"20\d\d", ln)), None)
    checks.append(Check(
        "transcript has a date-shaped line", date_line is not None,
        date_line[:120] if date_line else "no 20\\d\\d in the assistant/result lines",
    ))

    if row is not None:
        usage: dict[str, Any] = derive.summary_from_row(conn, row)["usage"]
        nonzero = sum(usage.values()) if usage else 0
        checks.append(Check("summary usage totals non-zero", nonzero > 0, f"usage={usage}"))
    else:
        checks.append(Check("summary usage totals non-zero", False, "no job row to summarize"))
    return checks


# ---------------------------------------------------------------- item 4: dead port and the collector

async def collector_tick(reg: Registry, conn: Any, ep: Endpoint) -> tuple[int, Any, float]:
    """One real ``collect.tick`` — the collector's own code, from endpoint_id through the rows —
    and the probe row it wrote, as (endpoint id, row, t)."""
    eid = collect.endpoint_id(conn, ep)
    now = time.time()
    probe_id = await collect.tick(reg, conn, ep, eid, now, backend_kind(ep.backend))
    row = conn.execute("SELECT * FROM probes WHERE id = ?", (probe_id,)).fetchone()
    return eid, row, now


async def down_state(conn: Any, host: str = "127.0.0.1", port: int = 1, reg: Registry | None = None) -> list[Check]:
    """The down state against the kernel's real refusal: an endpoint on a port nothing listens
    on, never written to any yaml. The probe is the real Registry probe, the rows are the real
    collector's. What must show: a ``probes`` row with ``up = 0`` and ``host = "up"`` — the
    refusal itself proves the box up — the failure sentence naming ``nothing is listening``,
    and both display states: ``endpoint_state`` dormant (never seen up: §4) and ``node_state``
    up (the refusal settles the node)."""
    ep = Endpoint(name="smoke-dead", base_url=f"http://{host}:{port}", backend="llama.cpp", host="local")
    reg = reg or Registry.load()
    eid, row, now = await collector_tick(reg, conn, ep)
    checks = [
        Check("dead-port probe row (up = 0)", row is not None and row["up"] == 0,
              f"probes row for {ep.base_url}: up={row['up'] if row is not None else 'no row'}"),
        Check("dead-port refusal names the box up (host = up)", row is not None and row["host"] == "up",
              f"host={row['host'] if row is not None else 'no row'}"),
        Check("failure sentence (nothing is listening)", row is not None and "nothing is listening" in (row["error"] or ""),
              row["error"] if row is not None else ""),
    ]
    if row is not None:
        st = state.endpoint_state(row, collect.last_up_t(conn, eid), now)
        checks.append(Check("endpoint_state after a never-up probe (dormant)", st == "dormant", f"state={st}"))
        node = state.node_state([{"up": bool(row["up"]), "host": row["host"]}])
        checks.append(Check("node_state from the refusal alone (up)", node == "up", f"node={node}"))
    else:
        checks.append(Check("endpoint_state after a never-up probe (dormant)", False, "no probe row"))
        checks.append(Check("node_state from the refusal alone (up)", False, "no probe row"))
    return checks


async def live_tick(conn: Any, reg: Registry, ep: Endpoint) -> list[Check]:
    """One collector tick against a live endpoint: its ``probes`` row ``up = 1``, a
    ``probe_counters`` row for it (the backend serves its counters document), and
    ``endpoint_state`` live."""
    eid, row, now = await collector_tick(reg, conn, ep)
    checks = [
        Check("live probe row (up = 1)", row is not None and row["up"] == 1,
              f"probes row for {ep.base_url}: up={row['up'] if row is not None else 'no row'}"
              + (f", error={row['error']}" if row is not None and not row["up"] and row["error"] else "")),
    ]
    if row is not None:
        counters = conn.execute("SELECT 1 FROM probe_counters WHERE probe_id = ?", (row["id"],)).fetchone()
        checks.append(Check("counters row for the live probe", counters is not None,
                            "probe_counters written" if counters else "no probe_counters row (the backend served no counters)"))
        st = state.endpoint_state(row, collect.last_up_t(conn, eid), now)
        checks.append(Check("endpoint_state after a live probe (live)", st == "live", f"state={st}"))
    else:
        checks.append(Check("counters row for the live probe", False, "no probe row"))
        checks.append(Check("endpoint_state after a live probe (live)", False, "no probe row"))
    return checks


# ---------------------------------------------------------------- item 5: the command

async def _run(a: argparse.Namespace) -> int:
    cl = Checklist()
    reg = Registry.load()
    try:
        # resolve first: an endpoint name not in models.yaml fails here, with no network, and
        # the whole run is that one MISSING line
        res = await reg.resolve(model=a.model, endpoint=a.endpoint)
    except ModelUnavailable as e:
        cl.append(Check(f"model '{a.model}' resolves on endpoint '{a.endpoint}'", False, str(e)))
        print(cl.render(), flush=True)
        return exit_code(cl)

    print(f"database: {db.db_path()}")
    print(f"endpoint: {res.endpoint.name} base_url={res.endpoint.url} model={res.served_model}")
    print(f"start: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")
    print(flush=True)
    cl.append(Check(
        f"model '{a.model}' resolves on endpoint '{a.endpoint}'", True,
        f"serving {res.served_model} at {res.endpoint.url}",
    ))

    cl.extend(await direct_requests(res.endpoint, res.served_model, n=a.requests))

    store = JobStore()
    conn = store.conn
    cl.extend(await down_state(conn, reg=reg))
    cl.extend(await live_tick(conn, reg, res.endpoint))
    if not a.skip_job:
        shim = Shim(lambda jid: store.jobs.get(jid), endpoint_lookup=lambda name: reg.endpoints.get(name))
        cl.extend(await real_job(reg, store, shim, a.endpoint, a.model, a.timeout_s))
    else:
        cl.append(Check("real job through run_job", True, "skipped (--skip-job): rows checked are the requests and states only"))

    print()
    print(cl.render(), flush=True)
    return exit_code(cl)


def main(argv: list[str] | None = None) -> None:
    """``localagents smoke``: one online pass over a named endpoint and model, printing the
    checklist of rows and states it found; exit 1 on any MISSING. Lazy-imported from the
    package's main(): it never loads unless asked."""
    ap = argparse.ArgumentParser(
        prog="localagents smoke",
        description="online acceptance: direct /v1/messages requests, one real job through "
        "run_job, a dead-port down state, and a printed checklist of the rows and states found",
    )
    ap.add_argument("--endpoint", required=True, help="endpoint name from models.yaml")
    ap.add_argument("--model", required=True, help="served model id to run against")
    ap.add_argument("--db", help="database path (default: LOCALAGENTS_DB or the shared file)")
    ap.add_argument("--requests", type=int, default=3, help="number of direct /v1/messages requests (default 3)")
    ap.add_argument("--timeout-s", type=int, default=180, help="wall-clock cap for the real job (default 180)")
    ap.add_argument("--skip-job", action="store_true", help="skip the real job: requests and states only")
    a = ap.parse_args(argv)
    if a.db:
        os.environ["LOCALAGENTS_DB"] = str(Path(a.db).expanduser().resolve())
    sys.exit(asyncio.run(_run(a)))
