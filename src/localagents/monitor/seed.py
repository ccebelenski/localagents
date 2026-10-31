"""``localagents seed`` (monitor spec §7 "Scripted live data"): an offline, deterministic
database built from the fixtures under ``tests/fixtures/monitor/`` with every timestamp shifted
to ``now``. It is the world the browser smoke and the route tests run against, and it shows
every display state at once:

- ``ai1`` (strata, host ``ai1``) — live, an hour of probes at 5 s with the busy ai2 capture's
  counters interpolated across it and a running job on its one slot; the counters restart at
  the midpoint (every counter drops to its pass-1 value and climbs again, ``since`` with it),
  so the world exercises the real reset path instead of asserting one its rows deny;
- ``dgx1-a`` (vllm, host ``dgx1``) — down since 20 min ago (a refused connection: the box
  answers, nothing listens);
- ``llamacpp`` (llama.cpp, host ``local``) — dormant, last up a week ago;
- one finished job with requests and tool calls (the real story-1.9 export, re-timed to have
  ended 10 min ago) and one running job (the same export truncated to its first 40 requests
  and 50 tool calls, started 5 min ago).

``seed`` is a pure function of ``now`` and the fixture bytes: no network, no wall clock. It
refuses a database that already holds a ``jobs`` or ``probes`` row unless ``force=True`` —
which deletes every row of every table and seeds fresh, never appending to a live database.
"""

from __future__ import annotations

import errno
import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import db
from ..registry import classify_connect_failure, parse_metrics, parse_strata_extra, parse_strata_metrics
from . import rates, read, rows
from .collect import _event

# The seed's own mcp_servers row lives under the repo it is run from.
_REPO = Path(__file__).resolve().parents[3]

# probe series shapes (pure in the world, not in now): ai1 a full hour at 5 s (720 intervals,
# 721 probes) ending at now; dgx1-a up for an hour then down for the last 20 min at 5 s;
# llamacpp one up probe a week ago then dormant-cadence down probes every 60 s over the last
# 24 h. AI1_RESTART_I is the probe whose counters restarted: probe i sits at
# now - (AI1_PROBES - 1 - i) * AI1_DT, so i = 360 is exactly now - 1800.
AI1_PROBES = 721
AI1_DT = 5.0
AI1_RESTART_I = 360
AI1_RESTART_S = AI1_RESTART_I * AI1_DT  # 1800.0: the restart, and the new ``since``
DGX_UP_S = 3600.0  # the up window starts 3600 s ago; up from now-3600 to now-1200
DGX_DOWN_SPAN_S = 20 * 60.0  # down from now-1200 to now
LCPP_UP_AGO_S = 7 * 24 * 3600.0 + 60.0  # last up a week (plus a minute) ago: inside the 7-day
# prune cutoff, so retention removes exactly this probe
LLAMACPP_DOWN_SPAN_S = 24 * 3600.0  # dormant down probes over the last 24 h
LLAMACPP_DOWN_DT = 60.0
BUSY_TAIL_S = 5 * 60.0  # ai1's slot is busy for the last five minutes (the running job)
AI1_CONTEXT = 131072
LCPP_CONTEXT = 262144
LATENCY_MS = 12


class SeedRefused(RuntimeError):
    """The target database already holds a ``jobs`` or ``probes`` row and ``force`` was not
    given, or ``force`` was given while a localagents server is still running against it: the
    seed must not append to, or clobber, an operator's live database by accident."""


REFUSED_SENTENCE = (
    "the database already holds jobs or probes rows; pass --force to replace them"
)
LIVE_SERVER_S = 60.0  # a server's heartbeat is fresh this long (status.collect's rule)
COLLECTOR_LIVE_S = 120.0  # a collector's newest probe is fresh this long


def _live_server_sentence(pid: int) -> str:
    return (f"a live server (pid {pid}) is running against this database; stop it, "
            f"or point --db at a throwaway file")


def _collector_sentence(age: int) -> str:
    return (f"a collector wrote this database {age} s ago; stop the monitor first, "
            f"or point --db at a throwaway file")


def _collector_age(conn: sqlite3.Connection, now_wall: float) -> int | None:
    """The whole seconds since the newest ``probes`` row, when a collector wrote it within
    ``COLLECTOR_LIVE_S`` of the wall clock; else None (review 3.9C C2).

    The monitor never writes ``mcp_servers`` (``serve`` starts no status registration), so
    ``_live_server_pid`` cannot see a running collector: a fresh probe row is the only trace it
    leaves. Reseeding under it rewrites the ``endpoints`` table, and the collector's per-task
    cached ``eid`` then names a different endpoint's row. Runs on the WALL clock: a collector
    writes now, whatever world clock ``--now`` names (ruling E)."""
    row = conn.execute("SELECT MAX(t) FROM probes").fetchone()
    if row is None or row[0] is None:
        return None
    age = max(0.0, now_wall - row[0])
    return int(age) if age <= COLLECTOR_LIVE_S else None


def _live_server_pid(conn: sqlite3.Connection, now_wall: float) -> int | None:
    """The pid of a running localagents server on this database, or None: ``stopped IS NULL``,
    a heartbeat within the last 60 s, and the pid alive (the status line's liveness rule,
    heartbeat first because pids are reused). Runs on the WALL clock: a server is live now or
    not, whatever world clock ``--now`` names (ruling E)."""
    for row in conn.execute(
        "SELECT pid FROM mcp_servers WHERE stopped IS NULL AND updated >= ? AND pid IS NOT NULL",
        (now_wall - LIVE_SERVER_S,),
    ).fetchall():
        if db.pid_alive(row["pid"]):
            return row["pid"]
    return None

# the jobs columns, in the schema's order (db._V1 plus jobs.compactions from _V2)
_JOB_COLS = (
    "id", "mcp_server_id", "endpoint_id", "model_id", "task", "cwd", "isolation",
    "status", "created_at", "started_at", "finished_at", "updated", "session_id", "result",
    "error", "last_activity", "num_turns", "context", "generated_tokens",
    "prompt_tokens_processed", "prompt_tokens_cached", "cache_hit_ratio", "prompt_tps",
    "generate_tps", "spec_decode_acceptance", "worktree", "dump", "log_events", "compactions",
)
_REQ_COLS = (
    "job_id", "path_id", "t", "http_status", "ms", "gen_ms", "stream", "input_tokens",
    "output_tokens", "cache_read_tokens", "cache_creation_tokens", "messages", "tools",
    "bytes", "moved_system", "folded_text", "context_exceeded", "error",
)
_TC_COLS = ("job_id", "t", "finished", "tool", "tool_use_id", "input", "is_error")


def _refused_sentence(url: str) -> str:
    """The exact ECONNREFUSED sentence ``classify_connect_failure`` writes for ``url``: the box
    is up and answers, but nothing listens on its port. Built by feeding the classifier a real
    ``ConnectionRefusedError`` (offline: no connection is made, only the exception's errno is
    read)."""
    return classify_connect_failure(ConnectionRefusedError(errno.ECONNREFUSED, "refused"), url)["error"]



def default_fixtures_dir() -> Path:
    """The story-2.1 captures under the repo's ``tests/fixtures/monitor``: the seed is a
    dev/testing tool that runs from the repo, where the fixtures live (an installed package has
    no tests directory, so the seed is not useful there)."""
    return _REPO / "tests" / "fixtures" / "monitor"


def parse_now(value: str | None) -> float:
    """The CLI's ``--now``: epoch seconds (a number) or an ISO-8601 timestamp, shifted to the
    world's clock; ``None`` (no flag) is the current time. A naive ISO timestamp is read in
    local time."""
    if value is None:
        return time.time()
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        return datetime.fromisoformat(value).timestamp()


@dataclass
class _World:
    """The loaded fixture bytes and the values the seed derives from them."""

    ai1_counters_1: dict[str, float]
    ai1_counters_2: dict[str, float]
    ai1_hardware: dict[str, float | None]
    ai1_gen_tps: float | None
    dgx_counters_1: dict[str, float]
    dgx_counters_2: dict[str, float]
    dgx_context: int
    dgx_served: str
    lcpp_counters_1: dict[str, float]
    lcpp_served: str
    job: dict
    job_requests: list[dict]
    job_tool_calls: list[dict]


def _load(fixtures_dir: Path) -> _World:
    """The fixture captures, parsed through the real registry parsers (never typed values).

    ai1's own capture pair is idle (zero deltas), so ai1's counters are the busy ai2 pair's
    (spec §7): interpolated across the hour for a constant non-zero rate. ai1's hardware and
    ``gen_tps`` come from the ai2 captures (pass 2 and pass 1 respectively)."""
    strata = fixtures_dir / "strata"
    vllm = fixtures_dir / "vllm"
    lcpp = fixtures_dir / "llamacpp"
    ai2_1 = json.loads((strata / "ai2_metrics_1.json").read_text())
    ai2_2 = json.loads((strata / "ai2_metrics_2.json").read_text())
    export = json.loads((fixtures_dir / "db" / "job_20261029-110844-8fd14e0e.json").read_text())
    dgx_model = json.loads((vllm / "dgx1-a_models_1.json").read_text())
    lcpp_model = json.loads((lcpp / "llamacpp_models_1.json").read_text())
    return _World(
        ai1_counters_1=parse_strata_metrics(ai2_1),
        ai1_counters_2=parse_strata_metrics(ai2_2),
        ai1_hardware=parse_strata_extra(ai2_2)["hardware"],
        ai1_gen_tps=parse_strata_extra(ai2_1)["gen_tps"],
        dgx_counters_1=parse_metrics((vllm / "dgx1-a_metrics_1.txt").read_text()),
        dgx_counters_2=parse_metrics((vllm / "dgx1-a_metrics_2.txt").read_text()),
        dgx_context=next(m["max_model_len"] for m in dgx_model["data"] if isinstance(m.get("max_model_len"), int)),
        # story 3.2a row 3: the first served id is what Registry.probe's models[0] and
        # collect.tick record on an UP probe; the seed had one only for ai1, so the world could
        # not show a down or dormant endpoint's last-known model
        dgx_served=next(m["id"] for m in dgx_model["data"] if isinstance(m.get("id"), str)),
        lcpp_counters_1=parse_metrics((lcpp / "llamacpp_metrics_1.txt").read_text()),
        lcpp_served=next(m["id"] for m in lcpp_model["data"] if isinstance(m.get("id"), str)),
        job=export["job"],
        job_requests=export["requests"],
        job_tool_calls=export["tool_calls"],
    )


def _interpolate(before: dict[str, float], after: dict[str, float], fraction: float) -> dict[str, float | None]:
    """Every counter column at ``fraction`` of the way from ``before`` to ``after`` (linear, so
    the rate between any two consecutive probes is constant); a column missing from either
    capture is an absent series (NULL)."""
    out: dict[str, float | None] = {}
    for c in rows.COUNTER_COLUMNS:
        a, b = before.get(c), after.get(c)
        out[c] = (a + (b - a) * fraction) if (a is not None and b is not None) else None
    return out


def _probe_row(t: float, endpoint_id: int, model_id: int | None, *, up: bool, host: str,
              error: str | None = None, **gauges) -> dict:
    """A ``probes``-columns dict: a down probe carries its host class and sentence and nothing
    else (a failed probe has no gauges), as ``rows.probe_to_rows`` keeps."""
    row: dict = {c: None for c in rows.PROBE_COLUMNS}
    row.update(t=t, endpoint_id=endpoint_id, model_id=model_id, up=1 if up else 0,
               host=host, error=None if up else error)
    row.update(gauges)
    return row


def _insert_probe(conn: sqlite3.Connection, row: dict, counters: dict | None,
                  hardware: dict | None) -> None:
    # a parsed metrics dict can carry gauges beside the counters (llama.cpp's
    # requests_processing/requests_deferred map to running/waiting since story 2.2a); the
    # counters row takes the counter columns only, NULL when absent (spec §3, ruling C)
    if counters is not None:
        counters = {k: counters.get(k) for k in rows.COUNTER_COLUMNS}
    rows.insert(conn, rows.ProbeRows(row, counters, hardware))


def _ai1_hardware(base: dict[str, float | None], i: int) -> dict[str, float | None]:
    """The ai2 pass-2 hardware row with ``gpu_util`` sweeping a slow sine between 20 and 99
    over the hour, so the sparkline has a shape."""
    hw = dict(base)
    hw["gpu_util"] = 59.5 - 39.5 * math.cos(2 * math.pi * i / AI1_PROBES)
    return hw


def _job_insert(conn: sqlite3.Connection, job_id: str, server_id: str,
                endpoint_id: int, model_id: int, src: dict, *, status: str, created_at: float,
                started_at: float, finished_at: float | None, updated: float, num_turns: int,
                result: str | None, requests: list[dict], shift,
                tool_calls: list[dict], shift_finished, context: int | None = None) -> None:
    """One ``jobs`` row plus its re-timed ``requests`` and ``tool_calls`` rows (``shift`` maps
    a request ``t`` and ``shift_finished`` a tool-call ``t``/``finished``). ``context`` overrides
    the export's window: the running job was handed ai1's own (F10)."""
    conn.execute(
        f"INSERT INTO jobs ({', '.join(_JOB_COLS)}) VALUES ({', '.join('?' * len(_JOB_COLS))})",
        (job_id, server_id, endpoint_id, model_id, src["task"], src["cwd"],
         src["isolation"], status, created_at, started_at, finished_at, updated,
         src["session_id"], result, src["error"], src["last_activity"], num_turns,
         src["context"] if context is None else context, src["generated_tokens"],
         src["prompt_tokens_processed"],
         src["prompt_tokens_cached"], src["cache_hit_ratio"], src["prompt_tps"],
         src["generate_tps"], src["spec_decode_acceptance"], src["worktree"], src["dump"],
         src["log_events"], 0),
    )
    for r in requests:
        conn.execute(
            f"INSERT INTO requests ({', '.join(_REQ_COLS)}) VALUES ({', '.join('?' * len(_REQ_COLS))})",
            (job_id, r["path_id"], shift(r["t"]), r["http_status"], r["ms"], r["gen_ms"],
             r["stream"], r["input_tokens"], r["output_tokens"], r["cache_read_tokens"],
             r["cache_creation_tokens"], r["messages"], r["tools"], r["bytes"],
             r["moved_system"], r["folded_text"], r["context_exceeded"], r["error"]),
        )
    for tc in tool_calls:
        conn.execute(
            f"INSERT INTO tool_calls ({', '.join(_TC_COLS)}) VALUES ({', '.join('?' * len(_TC_COLS))})",
            (job_id, shift(tc["t"]), shift_finished(tc["finished"]), tc["tool"],
             tc["tool_use_id"], tc["input"], tc["is_error"]),
        )


def seed(conn: sqlite3.Connection, now: float, fixtures_dir: Path | str, force: bool = False) -> dict[str, int]:
    """Seed ``conn`` with the seeded world, a pure function of ``now`` and ``fixtures_dir``.

    Everything is written in ONE ``db.immediate()`` transaction. A database that already holds
    a ``jobs`` or ``probes`` row is refused (``SeedRefused``) unless ``force=True``, which
    deletes every row of every table and seeds fresh — the seed never appends to a live
    database. ``force`` itself refuses a database a running localagents server owns (a fresh
    heartbeat and a live pid, read on the wall clock), naming the pid. With or without
    ``force``, a database a collector is writing is refused too (a ``probes`` row within
    ``COLLECTOR_LIVE_S`` of the wall clock): the monitor writes no ``mcp_servers`` row, so a
    fresh probe is the only trace of it (review 3.9C C2). Returns the number of rows written
    per table.
    """
    fx = Path(fixtures_dir)
    # review 3.9C C2: force or not, never reseed under a running collector (see _collector_age)
    age = _collector_age(conn, time.time())
    if age is not None:
        raise SeedRefused(_collector_sentence(age))
    has_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    has_probes = conn.execute("SELECT COUNT(*) FROM probes").fetchone()[0]
    if (has_jobs or has_probes) and not force:
        raise SeedRefused(REFUSED_SENTENCE)
    world = _load(fx)

    with db.immediate(conn):
        if force:
            # a running server's rows (and jobs) must not be deleted from under it: the check
            # is inside the delete's own transaction, so no server can appear between them
            pid = _live_server_pid(conn, time.time())
            if pid is not None:
                raise SeedRefused(_live_server_sentence(pid))
            # the same collector check, again inside the write lock: a collector that committed
            # between the outer read and this transaction must not be overwritten either
            age = _collector_age(conn, time.time())
            if age is not None:
                raise SeedRefused(_collector_sentence(age))
            # children first (the foreign keys are enforced), then the lookup and job tables
            for table in ("probe_hardware", "probe_counters", "probes", "endpoint_events",
                          "endpoint_load", "requests", "tool_calls", "jobs", "mcp_servers",
                          "paths", "models", "endpoints"):
                conn.execute(f"DELETE FROM {table}")
        _write(conn, now, world)
        # story 3.7-fold-2 (D5): the read path takes last_active from the stored witness, so the
        # seeded world must carry it too — backfilled from its own rows by the pairing (the same
        # call the monitor makes at its own start; the version-6 migration adds the column only)
        read.backfill_last_active(conn)
    return _counts(conn)


def _write(conn: sqlite3.Connection, now: float, world: _World) -> None:
    # --- lookup rows -------------------------------------------------------------
    # first_seen is the world's clock, not the wall clock: two seeds with the same --now stay
    # identical row for row (review of 2.4, F4; ruling B)
    ai1 = db.lookup_id(conn, "endpoints", now=now, name="ai1", base_url="http://10.0.0.50:8080",
                       backend="strata", host="ai1")
    dgx = db.lookup_id(conn, "endpoints", now=now, name="dgx1-a", base_url="http://10.0.0.70:8000",
                       backend="vllm", host="dgx1")
    lcpp = db.lookup_id(conn, "endpoints", now=now, name="llamacpp", base_url="http://127.0.0.1:8080",
                        backend="llama.cpp", host="local")
    model = db.lookup_id(conn, "models", served_id="qwen3.8-flash-next-iq3_s")
    # story 3.2a row 3: the ids collect.tick would record on the UP probes of the other two
    # endpoints (models[0] of their captured /v1/models)
    dgx_model_id = db.lookup_id(conn, "models", served_id=world.dgx_served)
    lcpp_model_id = db.lookup_id(conn, "models", served_id=world.lcpp_served)

    # the seed's own live server row: stopped NULL, a fresh heartbeat, a live pid — the status
    # line reads it as a live server so its jobs show on the line. Its version is the build
    # stamp of the tree the seed runs from (story 1.14): a monitor started from the same tree
    # stamps itself the same, so the seeded job page draws no "server code differs" note
    server_id = db.new_uuid()
    conn.execute(
        "INSERT INTO mcp_servers (id, pid, owner, started, updated, cwd, version)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (server_id, os.getpid(), db.session_pid() or os.getppid(),
         now - 3600.0, now, str(_REPO), db.build_stamp()),
    )

    # --- jobs ---------------------------------------------------------------------
    job_id_f, job_id_r = db.new_uuid(), db.new_uuid()
    j = world.job
    delta = (now - 10 * 60.0) - j["finished_at"]  # the finished job ends 10 minutes ago
    created_f = j["created_at"] + delta
    reqs_r = world.job_requests[:40]
    tcs_r = world.job_tool_calls[:50]
    # the running job's rows span 408 s of source time but the job is only 5 min old: scale the
    # truncated timeline onto [now-5 min, now] so every row is recent and none is in the future
    all_t = ([r["t"] for r in reqs_r] + [tc["t"] for tc in tcs_r]
             + [tc["finished"] for tc in tcs_r if tc["finished"] is not None])
    t_min, t_max = min(all_t), max(all_t)
    rshift = lambda t: (now - 300.0) + (t - t_min) * (300.0 / (t_max - t_min))

    # the paths the requests name, re-resolved by path string (lookup_id dedups; the export's
    # own path_ids named the original database's rows, so they are rebuilt here). reqs_r shares
    # its first-40 objects with world.job_requests, so the loop resolves each path once.
    for r in world.job_requests:
        r["path_id"] = db.lookup_id(conn, "paths", path=r["path"])

    _job_insert(
        conn, job_id_f, server_id, ai1, model, j,
        status="succeeded", created_at=created_f, started_at=j["started_at"] + delta,
        finished_at=now - 10 * 60.0, updated=j["updated"] + delta, num_turns=j["num_turns"],
        result=j["result"], requests=world.job_requests, shift=lambda t: t + delta,
        tool_calls=world.job_tool_calls, shift_finished=lambda t: t + delta if t is not None else None,
    )
    _job_insert(
        conn, job_id_r, server_id, ai1, model, j,
        status="running", created_at=now - 300.0, started_at=now - 300.0, finished_at=None,
        updated=now, num_turns=40, result=None, requests=reqs_r, shift=rshift,
        tool_calls=tcs_r, shift_finished=rshift, context=AI1_CONTEXT,
    )

    # --- ai1: a live hour with a restart at its midpoint ------------------------
    # The counters walk pass-1 -> pass-2 over each half-hour segment (360 intervals, so the
    # per-interval delta and the rate are the same before and after); at AI1_RESTART_I they
    # drop back to the pass-1 values and the backend's ``since`` moves forward — the restart
    # ``rates.reset_reason`` finds in the rows. No ``up`` event: the collector writes none for
    # an endpoint's first probe (ruling D).
    since_before = now - 3 * 24 * 3600.0
    since_after = now - AI1_RESTART_S
    prev_counters = None
    restart_detail = None
    for i in range(AI1_PROBES):
        t = now - (AI1_PROBES - 1 - i) * AI1_DT
        restarted = i >= AI1_RESTART_I
        fraction = (i - AI1_RESTART_I if restarted else i) / AI1_RESTART_I
        counters = _interpolate(world.ai1_counters_1, world.ai1_counters_2, fraction)
        since = since_after if restarted else since_before
        if i == AI1_RESTART_I:
            restart_detail = rates.reset_reason(prev_counters, counters, since_before, since_after)
        prev_counters = counters
        busy = (now - t) <= BUSY_TAIL_S
        _insert_probe(
            conn,
            _probe_row(t, ai1, model, up=True, host="up", latency_ms=LATENCY_MS,
                       context=AI1_CONTEXT, slots_total=1, slots_busy=1 if busy else 0,
                       requests_running=1 if busy else 0, requests_waiting=0,
                       gen_tps=world.ai1_gen_tps, since=since,
                       state="busy" if busy else "idle"),  # ruling A: state by its own slots
            counters,
            _ai1_hardware(world.ai1_hardware, i),
        )
    _event(conn, now - AI1_RESTART_S, ai1, "restarted", restart_detail)

    # --- dgx1-a: up for an hour, down for the last 20 min ------------------------
    refused_dgx = _refused_sentence("http://10.0.0.70:8000")
    up_n = int(round((DGX_UP_S - DGX_DOWN_SPAN_S) / AI1_DT))  # 480 intervals, 481 up probes
    for k in range(up_n + 1):
        t = now - DGX_UP_S + k * AI1_DT
        _insert_probe(
            conn,
            _probe_row(t, dgx, dgx_model_id, up=True, host="up", latency_ms=LATENCY_MS,
                       context=world.dgx_context, requests_running=1, requests_waiting=0,
                       kv_cache_usage=0.17, state="busy"),
            _interpolate(world.dgx_counters_1, world.dgx_counters_2, k / up_n),
            None,
        )
    down_n = int(round(DGX_DOWN_SPAN_S / AI1_DT))  # 240 down probes, last at now
    for k in range(1, down_n + 1):
        _insert_probe(conn, _probe_row(now - DGX_DOWN_SPAN_S + k * AI1_DT, dgx, None,
                                       up=False, host="up", error=refused_dgx), None, None)
    # at the first down probe's own t: the collector writes the down event with that probe's t,
    # so a reader sorting events against probes sees the same instant (F1)
    _event(conn, now - DGX_DOWN_SPAN_S + AI1_DT, dgx, "down", refused_dgx)

    # --- llamacpp: up a week ago, dormant-cadence down for the last 24 h ---------
    # The last up probe sits a minute past the 7-day prune cutoff, so retention removes exactly
    # this one row (prune_probes deletes t < now-7d); the down event, at the first down probe's
    # own t (the collector's placement), survives it.
    lcpp_up_t = now - LCPP_UP_AGO_S
    lcpp_down_event_t = now - LLAMACPP_DOWN_SPAN_S
    _insert_probe(
        conn,
        _probe_row(lcpp_up_t, lcpp, lcpp_model_id, up=True, host="up", latency_ms=LATENCY_MS,
                   context=LCPP_CONTEXT, slots_total=2, slots_busy=0,
                   requests_running=0, requests_waiting=0, state="idle"),
        world.lcpp_counters_1,
        None,
    )
    refused_lcpp = _refused_sentence("http://127.0.0.1:8080")
    lcpp_downs = int(round(LLAMACPP_DOWN_SPAN_S / LLAMACPP_DOWN_DT))  # 1440 intervals, 1441 probes
    for k in range(lcpp_downs + 1):
        _insert_probe(
            conn, _probe_row(now - LLAMACPP_DOWN_SPAN_S + k * LLAMACPP_DOWN_DT, lcpp, None,
                             up=False, host="up", error=refused_lcpp),
            None, None,
        )
    _event(conn, lcpp_down_event_t, lcpp, "down", refused_lcpp)

    # --- endpoint load: ai1 is busy with the running job -------------------------
    conn.execute("INSERT INTO endpoint_load (endpoint_id, updated, load) VALUES (?, ?, ?)",
                 (ai1, now, "1/1 slots"))


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ("probes", "probe_counters", "probe_hardware", "endpoint_events", "endpoint_load",
              "endpoints", "models", "mcp_servers", "jobs", "requests", "tool_calls", "paths")
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
