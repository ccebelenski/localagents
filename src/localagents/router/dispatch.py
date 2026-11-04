"""The resolution step in ``run_agent`` and the queue path (docs/superpowers/specs/2026-11-02-router-design.md
§4 the dispatch path, the ticket and the "Consumer requirement"; §6 selection; §7 capacity, the
queue, "Accepted" and "Decided" 2026-11-04; §14 failure handling).

``route`` is §4 steps 1-4 for a job aimed at the router entry: the pointer brief is read, d3
scores the text, every real endpoint's live facts are read (outside any transaction), one
``Candidate`` per scorecard row is built and ``select`` decides. The result names a REAL row:
the job record is created on that endpoint with that served id (never ``auto``), carrying the
ticket and ``routed_by``. ``new_job`` writes the record, its queue entry and the routed job's
ticket in ONE transaction, so no job exists without its entry. ``admit_or_wait`` is the queued
job's loop: every wake re-reads its row (a hand drop or the stale sweep may have ended it),
scans this process's running sessions for a stall, re-probes the endpoint (down, re-served or
re-windowed at the job's turn is ``dropped``: ``row_down``, named jobs ``model_unavailable``),
re-applies the priority policy for a routed job (a divert moves the entry and the record), and
tries the admission; any exception is logged and the loop sleeps and retries.

Every job passes admission, routed or named: a named job has an entry and no ticket (a ticket
is the routing decision's record; ``feedback.apply_verdict`` reads its absence as "named")."""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from localagents import db
from localagents.jobs import SERVER_LIVE_S, Job, JobStore
from localagents.registry import Endpoint, Registry, Resolution, backend_kind
from localagents.router import definitions, queue, scorecard
from localagents.router.capacity import (Capacity, context_load_tokens, from_probe, load_capacity,
                                         prior_curve)
from localagents.router.decision import BriefScores, resolve_brief, score
from localagents.router.feedback import brief_files
from localagents.router.select import (FALLBACK_BAND, PRIORITIES, Candidate, Considered, Decision,
                                       Session, band_time, expected_wait, select)

# §7 "Stalled sessions": a running admitted session with no progress for this long gets a
# notice (its endpoint probed); nothing is cancelled, the job's own timeout still governs it
STALL_S = 900.0
# §7: "Each process wakes on its own job completions and on a 30-second poll"
POLL_S = 30.0
# the axes the calibration set's baseline gate lists unproven (calibration-set spec §6), always
# undetermined for selection. Nothing stores that gate's result yet (Task 5's dry run reports
# per-axis accuracy but writes no list), so the set is empty until something does.
UNPROVEN: frozenset[str] = frozenset()

# the causes a job that leaves the queue without a session carries in ``error`` (§7 "Accepted")
CANCELLED_WHILE_QUEUED = "cancelled while queued"
ROW_DOWN = "row_down"
MODEL_UNAVAILABLE = "model_unavailable"
ENTRY_REMOVED = "its queue entry was removed while it was queued"
# what a row_down record tells the coordinator to do (§4 step 6: no automatic re-route)
ROW_DOWN_GUIDANCE = "resubmit; the brief is rescored"
CALIBRATE_GUIDANCE = "run `localagents calibrate <endpoint>`, or name a model"
NO_ACCEPTABLE_GUIDANCE = ("every calibrated row was removed by a filter (see considered); "
                          "name a model, or split the brief")

_EXCLUDE = re.compile(r"^\s*([a-z_]+)\s*>=\s*(\d+)\s*$")

# this process's queued jobs' wake events: ``notify`` sets them all (a session ended here)
_WAKERS: set[asyncio.Event] = set()


class NoRoute(Exception):
    """``select`` refused (§6): ``no_calibrated_row`` or ``no_acceptable_row``, with every row
    and the filter or shortfall that decided it."""

    def __init__(self, reason: str, considered: list[Considered], undetermined: list[str]):
        self.reason = reason
        self.considered = considered
        self.undetermined = undetermined
        self.guidance = CALIBRATE_GUIDANCE if reason == "no_calibrated_row" else NO_ACCEPTABLE_GUIDANCE
        what = ("no scorecard row has a complete core calibration" if reason == "no_calibrated_row"
                else "no calibrated row survived the filters")
        super().__init__(f"auto cannot route this brief: {what}")

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.reason, "message": str(self), "guidance": self.guidance,
                "considered": [asdict(c) for c in self.considered],
                "undetermined": list(self.undetermined)}


@dataclass
class Facts:
    """One endpoint's admission facts now: its live probe, the capacity in sessions, our
    sessions there, and with a declared pool the EFFECTIVE pool cap (the pool's
    ``max_concurrent`` net of the foreign sessions on its members, floored at 0: ``try_admit``
    counts our entries only) with the members' endpoint ids."""

    endpoint: Endpoint
    served_id: str
    probe: dict
    capacity: Capacity
    endpoint_id: int | None
    ours: int
    pool: str | None = None
    pool_cap: int | None = None
    pool_ids: list[int] | None = None
    pool_admits: bool = True


@dataclass
class View:
    """What dispatch would decide now: the Decision, the candidates it read (in ``considered``
    order) and their facts by row id."""

    decision: Decision
    candidates: list[Candidate]
    facts: dict[int, Facts]
    load_tokens: int
    brief_files_n: int | None
    scores: BriefScores
    rows: list[dict] = field(default_factory=list)


@dataclass
class Routed:
    """A routed job's decision: the real row as a Resolution, the ticket dict, the scores (kept
    for the policy's re-application at every wake), the context load the window filter used."""

    resolution: Resolution
    ticket: dict
    scores: BriefScores
    load_tokens: int
    decision: Decision
    router_ep: Endpoint
    text: str
    priority: str
    tolerance: float | None = None
    avoid_served_id: str | None = None
    pool: str | None = None
    facts: Facts | None = None
    view: View | None = None

    @property
    def routed_by(self) -> dict[str, Any]:
        """§4 "Consumer requirement": the router entry, the one-line reason, the pool when a
        pool cap caused the wait or the divert."""
        return {"router": self.router_ep.name, "reason": self.decision.chosen_reason,
                "pool": self.pool}


# --- facts -----------------------------------------------------------------------------------

def _finite(x: float | None) -> float | None:
    return round(float(x), 1) if x is not None and math.isfinite(x) else None


def _endpoint_id(conn: sqlite3.Connection, ep: Endpoint) -> int | None:
    """The endpoints row the config's entry maps to (store.new's lookup key), read-only: an
    entry no job ever ran on has no row, and then no queue entry either."""
    r = conn.execute("SELECT id FROM endpoints WHERE name = ? AND base_url = ? AND backend = ?"
                     " AND host = ?", (ep.name, ep.url, ep.backend, ep.host)).fetchone()
    return r["id"] if r is not None else None


def _requests_running(probe: dict) -> int:
    """capacity.from_probe's reading of the live requests (load first, else busy slots)."""
    if not probe.get("up"):
        return 0
    load = probe.get("load") or {}
    slots = probe.get("slots") or {}
    running = load["requests_running"] if "requests_running" in load else slots.get("busy", 0)
    return int(running or 0)


def _facts(reg: Registry, conn: sqlite3.Connection, ep: Endpoint, served_id: str,
           probes: dict[str, dict], now: float, endpoint_id: int | None = None) -> Facts:
    probe = probes[ep.name]
    eid = endpoint_id if endpoint_id is not None else _endpoint_id(conn, ep)
    curve = (load_capacity(conn, eid) if eid is not None else None) or prior_curve(backend_kind(ep.backend))
    cap = from_probe(ep, probe, served_id, curve)
    ours = queue.occupancy(conn, [eid], now) if eid is not None else 0
    f = Facts(ep, served_id, probe, cap, eid, ours)
    if ep.pool and ep.pool in reg.pools:
        ids: list[int] = []
        foreign = 0
        for m in reg.pool_members(ep.pool):
            mid = eid if m.name == ep.name else _endpoint_id(conn, m)
            mine = queue.occupancy(conn, [mid], now) if mid is not None else 0
            if mid is not None:
                ids.append(mid)
            foreign += max(0, _requests_running(probes.get(m.name) or {}) - mine)
        f.pool = ep.pool
        f.pool_cap = max(0, int(reg.pools[ep.pool]["max_concurrent"]) - foreign)
        f.pool_ids = ids
        f.pool_admits = queue.occupancy(conn, ids, now) < f.pool_cap
    return f


async def _probe_missing(reg: Registry, names: list[str], probes: dict[str, dict]) -> dict[str, dict]:
    out = dict(probes)
    missing = [n for n in dict.fromkeys(names) if n not in out and n in reg.endpoints]
    got = await asyncio.gather(*(reg.probe(reg.endpoints[n]) for n in missing))
    out.update(zip(missing, got))
    return out


async def endpoint_facts(reg: Registry, conn: sqlite3.Connection, ep: Endpoint, served_id: str,
                         now: float, *, probes: dict[str, dict] | None = None,
                         endpoint_id: int | None = None) -> Facts:
    """Probe the endpoint and its pool's members (unless ``probes`` has them), then read the
    queue: the probes run first and outside any transaction (§7)."""
    names = [ep.name] + ([m.name for m in reg.pool_members(ep.pool)] if ep.pool in reg.pools else [])
    got = await _probe_missing(reg, names, probes or {})
    return _facts(reg, conn, ep, served_id, got, now, endpoint_id)


# --- the queue as select reads it ------------------------------------------------------------

def _band(scores_text: str | None) -> int | None:
    """The complexity band a ticket's scores determined, or None (a named job, undetermined)."""
    if not scores_text:
        return None
    try:
        scores = json.loads(scores_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(scores, dict):
        return None
    value = (scores.get("values") or {}).get("complexity")
    if "complexity" in set(scores.get("undetermined") or ()) or isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _sessions(conn: sqlite3.Connection, endpoint_id: int, now: float) -> list[Session]:
    """Our sessions on the endpoint (queue.occupancy's set) with their start and ticket band."""
    rows = conn.execute(
        "SELECT j.started_at, q.admitted, t.scores FROM queue_entries q JOIN jobs j ON j.id = q.job_id"
        " JOIN mcp_servers s ON s.id = q.owner_server LEFT JOIN router_tickets t ON t.job_id = q.job_id"
        f" WHERE q.endpoint_id = ? AND q.admitted IS NOT NULL AND j.status IN ('queued', 'running')"
        f" AND {queue._LIVE} ORDER BY q.admitted",
        (endpoint_id, now - SERVER_LIVE_S)).fetchall()
    return [Session(r["started_at"] if r["started_at"] is not None else r["admitted"], _band(r["scores"]))
            for r in rows]


def _waiting(conn: sqlite3.Connection, endpoint_id: int, now: float) -> list[sqlite3.Row]:
    """The endpoint's waiting entries in queue order (queue._WAITING_SQL's set and order) with
    their tickets' scores."""
    return conn.execute(
        "SELECT q.job_id, q.priority_rank, q.enqueued, q.blocked_by, q.rowid AS entry_rowid,"
        " t.scores, t.est_wait_s FROM queue_entries q JOIN jobs j ON j.id = q.job_id"
        " JOIN mcp_servers s ON s.id = q.owner_server LEFT JOIN router_tickets t ON t.job_id = q.job_id"
        f" WHERE q.endpoint_id = ? AND q.admitted IS NULL AND j.status = 'queued' AND {queue._LIVE}"
        " ORDER BY q.priority_rank, q.enqueued, q.rowid",
        (endpoint_id, now - SERVER_LIVE_S)).fetchall()


def _entry_key(r: sqlite3.Row) -> tuple:
    """queue order: priority rank, then arrival, then insertion (queue._key's order)."""
    return (r["priority_rank"], r["enqueued"], r["entry_rowid"])


def _ahead(conn: sqlite3.Connection, endpoint_id: int | None, rank: int, now: float,
           own_job: str | None) -> list[sqlite3.Row]:
    """The waiting entries this job stands behind: behind its own entry when it is queued here
    (amendment 7a: a wake never counts the job's own entry), else as a new arrival of ``rank``
    (every waiting entry of the same or a higher priority)."""
    if endpoint_id is None:
        return []
    waiting = _waiting(conn, endpoint_id, now)
    own = next((w for w in waiting if w["job_id"] == own_job), None)
    if own is not None:
        return [w for w in waiting if _entry_key(w) < _entry_key(own)]
    return [w for w in waiting if w["priority_rank"] <= rank and w["job_id"] != own_job]


def _exclusion(ep: Endpoint, scores: BriefScores, unproven: frozenset[str]) -> str | None:
    """The operator's ``exclude:`` condition (``dimension >= n``, §5) that holds on the brief's
    determined scores, or None. An undetermined axis holds no condition (it is neutral, §6); a
    condition this code cannot read removes the row (fail-closed: the operator meant to keep
    something off this endpoint and the router cannot tell what), named as written."""
    determined = {k: v for k, v in scores.values.items()
                  if k not in scores.undetermined and k not in unproven}
    for cond in ep.exclude or ():
        m = _EXCLUDE.match(cond) if isinstance(cond, str) else None
        if m is None:
            return f"{cond!r} (unreadable)"
        value = determined.get(m.group(1))
        if isinstance(value, int) and not isinstance(value, bool) and value >= int(m.group(2)):
            return cond
    return None


def _brief_band(scores: BriefScores, unproven: frozenset[str]) -> int:
    value = scores.values.get("complexity")
    if ("complexity" in scores.undetermined or "complexity" in unproven
            or not isinstance(value, int) or isinstance(value, bool)):
        return FALLBACK_BAND
    return value


def _wait_default(cand: Candidate, scores: BriefScores, unproven: frozenset[str]) -> bool:
    """§6/§7c: the expected wait is "default" when any band time it summed came from the
    table, not the row (``band_time``'s flag; ``expected_wait`` drops it)."""
    brief = _brief_band(scores, unproven)
    bands = {brief}
    bands |= {s.band if s.band is not None else brief for s in cand.running[:cand.ours_running]}
    bands |= {b if b is not None else brief for b in cand.ahead_bands[:cand.queued_ahead]}
    return any(band_time(cand.row, b)[1] for b in bands)


def _bare_row(ep: Endpoint, served_id: str) -> scorecard.Row:
    """A row-shaped stand-in for an endpoint x served id with no scorecard row (a named job's
    expected wait reads the band defaults)."""
    return scorecard.Row(id=0, endpoint_name=ep.name, served_id=served_id, endpoint_id=0,
                         model_id=0, set_version=None, band_times={}, speed_factor=None,
                         archived=None, facts=None)


def _live_row(conn: sqlite3.Connection, endpoint_name: str, served_id: str) -> scorecard.Row | None:
    for row in scorecard.rows(conn):
        if row.endpoint_name == endpoint_name and row.served_id == served_id:
            return row
    return None


def _candidate(conn: sqlite3.Connection, row: scorecard.Row, cells: list, f: Facts | None,
               scores: BriefScores, rank: int, now: float, own_job: str | None,
               unproven: frozenset[str]) -> Candidate:
    if f is None:
        return Candidate(row=row, cells=cells, capacity=None, ours_running=0, queued_ahead=0,
                         window=None, expected_wait_s=None, excluded_by=None)
    ahead = _ahead(conn, f.endpoint_id, rank, now, own_job)
    cand = Candidate(
        row=row, cells=cells, capacity=f.capacity, ours_running=f.ours, queued_ahead=len(ahead),
        window=(f.probe.get("context") or {}).get(row.served_id), expected_wait_s=None,
        excluded_by=_exclusion(f.endpoint, scores, unproven), pool=f.pool,
        pool_admits=f.pool_admits,
        running=_sessions(conn, f.endpoint_id, now) if f.endpoint_id is not None else [],
        ahead_bands=[_band(a["scores"]) for a in ahead],
        queued_higher=sum(1 for a in ahead if a["priority_rank"] < rank))
    cand.expected_wait_s = expected_wait(cand, scores, now, unproven)
    return cand


async def _candidates(reg: Registry, conn: sqlite3.Connection, scores: BriefScores,
                      priority: str, now: float, *, own_job: str | None = None,
                      probes: dict[str, dict] | None = None,
                      unproven: frozenset[str] = UNPROVEN) -> tuple[list[Candidate], dict[int, Facts]]:
    """One Candidate per live scorecard row, in row order (§4 step 3). A row whose endpoint is
    gone from the config, is a router, is down, or no longer serves the row's id has no
    capacity: select files it ``facts_unavailable``."""
    real = [n for n, e in reg.endpoints.items() if not e.is_router]
    got = await _probe_missing(reg, real, probes or {})
    rank = queue.PRIORITY_RANK[priority]
    cands: list[Candidate] = []
    facts: dict[int, Facts] = {}
    for row in scorecard.rows(conn):
        cells = scorecard.cells(conn, row.id)
        ep = reg.endpoints.get(row.endpoint_name)
        f = None
        if ep is not None and not ep.is_router and row.served_id in (got[ep.name].get("models") or ()):
            f = _facts(reg, conn, ep, row.served_id, got, now)
            facts[row.id] = f
        cands.append(_candidate(conn, row, cells, f, scores, rank, now, own_job, unproven))
    return cands, facts


def _row_dicts(view_cands: list[Candidate], considered: list[Considered], scores: BriefScores,
               unproven: frozenset[str]) -> list[dict]:
    """The ticket's table: every considered row with its shortfall or filter, and the expected
    wait there with its "default" mark."""
    out = []
    for c, cand in zip(considered, view_cands):
        d = asdict(c)
        d["expected_wait_s"] = _finite(cand.expected_wait_s)
        d["wait_default"] = _wait_default(cand, scores, unproven) if cand.capacity is not None else None
        d["pool"] = cand.pool
        out.append(d)
    return out


def _scores_dict(scores: BriefScores, unproven: frozenset[str]) -> dict:
    """router_tickets.scores (scorecard.py's pinned wire shape, plus d3's raw answers: the
    per-option probabilities the ticket carries, §4 step 2)."""
    return {"values": dict(scores.values), "confidences": dict(scores.confidences),
            "undetermined": sorted(set(scores.undetermined) | (set(unproven) & set(scores.values))),
            "truncated": scores.truncated, "defs_version": scores.defs_version, "raw": scores.raw}


async def decide(reg: Registry, conn: sqlite3.Connection, router_ep: Endpoint, text: str,
                 scores: BriefScores, priority: str, tolerance: float | None = None, *,
                 avoid_served_id: str | None = None, own_job: str | None = None,
                 now: float | None = None, probes: dict[str, dict] | None = None,
                 unproven: frozenset[str] = UNPROVEN) -> View:
    """§4 steps 3-4 over a scored brief: the facts, the candidates, ``select``. ``preflight``
    reads this as it stands; ``route`` raises on a refusal; a wake re-applies it with
    ``own_job`` so the job's own entry is never counted ahead of itself."""
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}, not {priority!r}")
    now = time.time() if now is None else now
    cands, facts = await _candidates(reg, conn, scores, priority, now, own_job=own_job,
                                     probes=probes, unproven=unproven)
    calibrated = {c.row.id for c in cands if scorecard.core_complete(conn, c.row.id)}
    load_score = scores.values.get("context_load")
    if ("context_load" in scores.undetermined or "context_load" in unproven
            or not isinstance(load_score, int) or isinstance(load_score, bool)):
        load_score = None   # amendment 7a: None is neutral to context_load_tokens
    load = context_load_tokens(load_score, text)
    files = brief_files(text)
    files_n = len(files) if files else None
    tol = router_ep.tolerance if tolerance is None else tolerance
    d = select(scores, cands, priority, tol, router_ep.divert_after_min * 60.0, load, calibrated,
               files_n, avoid_served_id=avoid_served_id, unproven=unproven)
    view = View(d, cands, facts, load, files_n, scores)
    view.rows = _row_dicts(cands, d.considered, scores, unproven)
    return view


def _best_blocked(view: View) -> Candidate | None:
    """The best row when the decision diverted (select's order: shortfall, then the faster
    prediction, then fewer queued, then the given order), for ``routed_by.pool``."""
    pairs = [(c, cand, i) for i, (c, cand) in enumerate(zip(view.decision.considered, view.candidates))
             if c.acceptable]
    if not pairs:
        return None
    pairs.sort(key=lambda p: (round(p[0].shortfall, 9), -(p[0].predicted_speed or 0.0),
                              p[1].queued_ahead, p[2]))
    return pairs[0][1]


def _pool_of(view: View) -> str | None:
    """routed_by.pool: the pool's name when the pool, not the endpoint, held the best row
    (the job queued for it, or diverted from it); else None (amendment 5)."""
    d = view.decision
    if d.wait_cause != "pool":
        return None
    held = d.queue_for if d.queue_for is not None else _best_blocked(view)
    return held.pool if held is not None else None


def decision_dict(view: View) -> dict[str, Any]:
    """The Decision as a dict (``preflight``, amendment 6)."""
    d = view.decision
    q = d.queue_for
    return {"chosen_row": d.chosen.row.id if d.chosen is not None else None,
            "divert": d.divert, "queue_for": q.row.id if q is not None else None,
            "reason": d.reason, "chosen_reason": d.chosen_reason, "wait_cause": d.wait_cause,
            "pool": _pool_of(view), "tie_break": d.tie_break, "undetermined": d.undetermined,
            "position": (q.queued_ahead + 1) if q is not None else None,
            "expected_wait_s": _finite(q.expected_wait_s) if q is not None else 0.0,
            "load_tokens": view.load_tokens, "brief_files": view.brief_files_n,
            "considered": view.rows}


def _ticket(view: View, router_ep: Endpoint, priority: str, now: float,
            avoid_served_id: str | None, unproven: frozenset[str]) -> dict:
    d = view.decision
    chosen = d.chosen
    q = d.queue_for
    pass_ids = {c.pass_id for c in chosen.cells}
    flags = {"router": router_ep.name, "priority": priority, "reason": d.chosen_reason,
             "tie_break": d.tie_break, "wait_cause": d.wait_cause, "pool": _pool_of(view),
             "truncated": view.scores.truncated, "load_tokens": view.load_tokens,
             "est_wait_default": (_wait_default(q, view.scores, unproven) if q is not None else False)}
    if avoid_served_id is not None:
        flags["avoid_served_id"] = avoid_served_id
    return {"defs_version": view.scores.defs_version,
            "pass_id": next(iter(pass_ids)) if len(pass_ids) == 1 else None,
            "scores": _scores_dict(view.scores, unproven), "rows": view.rows,
            "chosen_row": chosen.row.id, "divert": bool(d.divert),
            "divert_at": now if d.divert else None,
            "est_wait_s": _finite(q.expected_wait_s) if q is not None else 0.0,
            # §4's ticket contents: the queue position it faced at enqueue, beside the wait.
            # None when the row admits it at once (no queue: 0 would read as "head of queue")
            "position": (q.queued_ahead + 1) if q is not None else None,
            "admitted": None, "flags": flags}


async def route(reg: Registry, conn: sqlite3.Connection, router_ep: Endpoint, task: str, cwd: str,
                priority: str, tolerance: float | None = None, *,
                avoid_served_id: str | None = None, client: httpx.AsyncClient | None = None,
                now: float | None = None) -> Routed:
    """§4 steps 1-4. Raises BriefUnresolved (a pointer to a missing file: no job),
    DecisionUnavailable (d3 down, slow or answering badly: ``model_unavailable``), NoRoute
    (select refused), ValueError (a bad priority)."""
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}, not {priority!r}")
    text = resolve_brief(task, cwd)
    defs = definitions.load(conn)
    own = client is None
    http = client if client is not None else httpx.AsyncClient()
    try:
        scores = await score(http, router_ep.decision_url, router_ep.decision_model, text, defs,
                             timeout_s=router_ep.decision_timeout_s,
                             max_tokens=router_ep.decision_max_tokens)
    finally:
        if own:
            await http.aclose()
    now = time.time() if now is None else now
    view = await decide(reg, conn, router_ep, text, scores, priority, tolerance,
                        avoid_served_id=avoid_served_id, now=now)
    d = view.decision
    if d.chosen is None:
        raise NoRoute(d.reason, d.considered, d.undetermined)
    f = view.facts[d.chosen.row.id]
    res = Resolution.make(f.endpoint, d.chosen.row.served_id, f.probe)
    return Routed(res, _ticket(view, router_ep, priority, now, avoid_served_id, UNPROVEN), scores,
                  view.load_tokens, d, router_ep, text, priority, tolerance, avoid_served_id,
                  _pool_of(view), f, view)


# --- tickets ---------------------------------------------------------------------------------

_TICKET_JSON = ("scores", "rows", "flags")


def insert_ticket(conn: sqlite3.Connection, job_id: str, ticket: dict) -> None:
    """The ticket row, inside the CALLER's transaction (``new_job`` writes it with the job).
    ``position`` has no column of its own (schema v12): it rides the flags JSON beside the
    other queue marks, and ``read_ticket`` lifts it back to the ticket's top level."""
    flags = dict(ticket.get("flags") or {})
    if "position" in ticket:
        flags["position"] = ticket["position"]
    conn.execute(
        "INSERT INTO router_tickets (job_id, defs_version, pass_id, scores, rows, chosen_row,"
        " divert, divert_at, est_wait_s, admitted, flags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, ticket["defs_version"], ticket.get("pass_id"), json.dumps(ticket["scores"]),
         json.dumps(ticket["rows"]), ticket.get("chosen_row"), 1 if ticket.get("divert") else 0,
         ticket.get("divert_at"), ticket.get("est_wait_s"), ticket.get("admitted"),
         json.dumps(flags)))


def write_ticket(conn: sqlite3.Connection, job_id: str, routed: Routed) -> None:
    """The plan's interface: the routed decision's ticket in a transaction of its own."""
    with db.immediate(conn):
        insert_ticket(conn, job_id, routed.ticket)


def read_ticket(conn: sqlite3.Connection, job_id: str) -> dict | None:
    """The job's ticket with its JSON decoded, or None (a named job). A JSON column another
    writer garbled reads None and is named in ``unreadable``: one bad ticket never breaks a
    status read."""
    r = conn.execute("SELECT * FROM router_tickets WHERE job_id = ?", (job_id,)).fetchone()
    if r is None:
        return None
    t = {k: r[k] for k in r.keys() if k != "job_id"}
    bad = []
    for k in _TICKET_JSON:
        try:
            t[k] = json.loads(t[k]) if t[k] is not None else None
        except (TypeError, ValueError):
            t[k] = None
            bad.append(k)
    t["divert"] = bool(t["divert"])
    if t["flags"] is None:
        t["flags"] = {}
    if bad:
        t["unreadable"] = bad
    # insert_ticket stores the queue position among the flags (no column); it is a top-level
    # queue fact like est_wait_s, so lift it out of the marks it was only stored with
    flags = t["flags"] if isinstance(t["flags"], dict) else {}
    t["position"] = flags.pop("position", None)
    return t


def resumed_ticket(prev: dict, prev_job: str) -> dict:
    """§4: a resumed routed job keeps its row; the ticket is copied, marked ``resumed``, and
    admitted again at the new job's own admission."""
    t = json.loads(json.dumps(prev))
    t["flags"] = {**(t.get("flags") or {}), "resumed": True, "resumed_from": prev_job}
    t["admitted"] = None
    # the previous job's queue position says nothing about where this one will stand
    t["position"] = None
    t.pop("unreadable", None)
    return t


def _merge_flags(conn: sqlite3.Connection, job_id: str, **flags: Any) -> None:
    """Add keys to a ticket's flags, one transaction; no ticket (a named job) is a no-op."""
    with db.immediate(conn):
        r = conn.execute("SELECT flags FROM router_tickets WHERE job_id = ?", (job_id,)).fetchone()
        if r is None:
            return
        try:
            current = json.loads(r["flags"]) if r["flags"] else {}
        except (TypeError, ValueError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current.update(flags)
        conn.execute("UPDATE router_tickets SET flags = ? WHERE job_id = ?", (json.dumps(current), job_id))


def ticket_summary(ticket: dict | None) -> dict | None:
    """The ticket in a few keys for ``job_status`` and ``list_jobs`` (``explain`` returns it
    whole): the row taken, the reason line, the priority, the divert and when, the queue
    position and wait expected at enqueue, the admission, and the marks."""
    if ticket is None:
        return None
    flags = ticket.get("flags") or {}
    rows = ticket.get("rows") or []
    chosen = next((r for r in rows if isinstance(r, dict) and r.get("row_id") == ticket.get("chosen_row")), None)
    out = {
        "chosen": f"{chosen['endpoint']} x {chosen['served_id']}" if chosen else None,
        "reason": flags.get("reason"), "priority": flags.get("priority"),
        "divert": bool(ticket.get("divert")), "divert_at": ticket.get("divert_at"),
        "position": ticket.get("position"),
        "est_wait_s": ticket.get("est_wait_s"), "est_wait_default": flags.get("est_wait_default"),
        "admitted": ticket.get("admitted"), "pass_id": ticket.get("pass_id"),
        "defs_version": ticket.get("defs_version"), "considered": len(rows),
        "undetermined": (ticket.get("scores") or {}).get("undetermined"),
        "truncated": bool(flags.get("truncated")), "resumed": bool(flags.get("resumed")),
    }
    if flags.get("stalled"):
        out["stalled"] = flags["stalled"]
    if flags.get("row_down"):
        out["row_down"] = flags["row_down"].get("why")
        out["guidance"] = flags["row_down"].get("guidance")
    if ticket.get("unreadable"):
        out["unreadable"] = ticket["unreadable"]
    return out


# --- the record ------------------------------------------------------------------------------

def served_id_of(conn: sqlite3.Connection, job_id: str) -> str | None:
    """The served id a job ran on (``jobs.model``), any server's job; None when unknown."""
    r = conn.execute("SELECT m.served_id FROM jobs j JOIN models m ON m.id = j.model_id"
                     " WHERE j.id = ?", (job_id,)).fetchone()
    return r["served_id"] if r is not None else None


def new_job(store: JobStore, *, priority: str, forced: bool = False, routed: Routed | None = None,
            copy_ticket: dict | None = None, now: float | None = None, **kw: Any) -> Job:
    """``store.new`` with the queue entry (admitted at once when ``forced``, §7) and a routed
    job's ticket (or a resumed job's copy) written in the same transaction as the jobs row, and
    ``routed_by`` from the decision. Raises what ``store.new`` raises; then nothing exists."""
    if routed is not None:
        kw["routed_by"] = routed.routed_by
    ticket = routed.ticket if routed is not None else copy_ticket
    if ticket is not None and forced:
        # §7: "the record says it forced" (the entry's forced column says it too); it skipped
        # the wait, so it held no queue position
        ticket = {**ticket, "admitted": now if now is not None else time.time(), "position": None,
                  "flags": {**(ticket.get("flags") or {}), "forced": True}}

    def write(conn: sqlite3.Connection, job: Job) -> None:
        queue.insert_entry(conn, job.id, job.endpoint_id, store.server_id, priority, forced, now=now)
        if ticket is not None:
            insert_ticket(conn, job.id, ticket)

    job = store.new(priority=priority, on_insert=write, **kw)
    if forced:
        job._admitted.set()
    return job


def _mark_admitted(conn: sqlite3.Connection, job: Job, now: float) -> None:
    job._admitted.set()
    job._queue_estimate = None
    with db.immediate(conn):
        conn.execute("UPDATE router_tickets SET admitted = ? WHERE job_id = ?", (now, job.id))


def try_admit_now(conn: sqlite3.Connection, job: Job, facts: Facts, now: float) -> bool:
    """One admission attempt (``queue.try_admit`` with the effective pool cap, amendment 7b);
    on success the ticket's ``admitted`` is written and the job's admitted event set."""
    kw: dict[str, Any] = {}
    if facts.pool_cap is not None:
        kw = {"pool_cap": facts.pool_cap, "pool_endpoint_ids": list(facts.pool_ids or [])}
    if not queue.try_admit(conn, job.id, facts.capacity, now, **kw):
        return False
    _mark_admitted(conn, job, now)
    return True


def estimate(conn: sqlite3.Connection, job: Job, facts: Facts, scores: BriefScores | None,
             now: float) -> float | None:
    """The job's expected wait where it stands (§6 "Expected wait", its own entry not counted
    ahead of it); a named job reads the band defaults at the middle band."""
    row = _live_row(conn, facts.endpoint.name, facts.served_id) or _bare_row(facts.endpoint, facts.served_id)
    s = scores or BriefScores({}, {}, set(), False, {}, 0)
    cand = _candidate(conn, row, [], facts, s, queue.PRIORITY_RANK[job.priority], now, job.id, UNPROVEN)
    return _finite(cand.expected_wait_s)


def named_wait(conn: sqlite3.Connection, facts: Facts, priority: str, now: float) -> tuple[bool, int, float | None]:
    """For a named job not yet recorded (``admit: now``): (admits at once, the position it would
    take, the expected wait there). Admits when the endpoint has room, the pool has room, and
    nobody of the same or a higher priority waits ahead."""
    row = _live_row(conn, facts.endpoint.name, facts.served_id) or _bare_row(facts.endpoint, facts.served_id)
    cand = _candidate(conn, row, [], facts, BriefScores({}, {}, set(), False, {}, 0),
                      queue.PRIORITY_RANK[priority], now, None, UNPROVEN)
    admits = facts.capacity.admits(facts.ours) and facts.pool_admits and cand.queued_ahead == 0
    return admits, cand.queued_ahead + 1, _finite(cand.expected_wait_s)


def would_queue(position: int, expected_wait_s: float | None, alternatives: list[dict],
                ticket: dict | None) -> dict[str, Any]:
    """§4 ``admit: now``: everything the router knows at enqueue, and no job."""
    expected_wait_s = _finite(expected_wait_s)
    mins = f"~{math.ceil(expected_wait_s / 60)} min" if expected_wait_s is not None else "an unknown wait"
    return {"error": "would_queue",
            "message": (f"the chosen row cannot admit now: it would queue at #{position}, {mins}; "
                        "queue (admit='queue'), take an alternative by naming its model, or split "
                        "the brief"),
            "position": position, "expected_wait_s": expected_wait_s,
            "alternatives": alternatives, "ticket": ticket_summary(ticket)}


def alternatives(decision: Decision) -> list[dict]:
    """The considered rows that could admit this job now, each with whether it was acceptable."""
    return [{"row_id": c.row_id, "endpoint": c.endpoint, "served_id": c.served_id,
             "shortfall": c.shortfall, "acceptable": c.acceptable}
            for c in decision.considered if c.filtered is None and c.admits]


def queued_line(conn: sqlite3.Connection, job: Job) -> str | None:
    """The blocking wait's progress line while queued (§7): ``queued #n, ~m min``; None when the
    job has no waiting entry (admitted, over, or never entered)."""
    try:
        pos = queue.position(conn, job.id, time.time())
    except KeyError:
        return None
    if not pos:
        return None
    est = job._queue_estimate
    if est is None:
        r = conn.execute("SELECT est_wait_s FROM router_tickets WHERE job_id = ?", (job.id,)).fetchone()
        est = r["est_wait_s"] if r is not None else None
    mins = str(math.ceil(est / 60)) if est is not None else "?"
    return f"queued #{pos}, ~{mins} min"


def note_queued(conn: sqlite3.Connection, job: Job, facts: Facts, now: float) -> None:
    """The first admission attempt left the job waiting: its estimate, the transcript line, and
    ``routed_by.pool`` when the entry's ``blocked_by`` says the pool held it (amendment 5)."""
    route_ = job._route
    blocked = conn.execute("SELECT blocked_by FROM queue_entries WHERE job_id = ?", (job.id,)).fetchone()
    blocked_by = blocked["blocked_by"] if blocked is not None else None
    if route_ is not None and route_.decision.queue_for is not None:
        job._queue_estimate = _finite(route_.decision.queue_for.expected_wait_s)
    else:
        job._queue_estimate = estimate(conn, job, facts, route_.scores if route_ else None, now)
    if (job.routed_by is not None and blocked_by == "pool" and facts.pool
            and job.routed_by.get("pool") is None):
        job.routed_by = {**job.routed_by, "pool": facts.pool}
        job.changed()
    pos = queue.position(conn, job.id, now)
    est = job._queue_estimate
    mins = f"~{math.ceil(est / 60)} min" if est is not None else "an unknown wait"
    job._log_safe(f"[queue] queued #{pos} on {job.endpoint}, {mins}"
                  f" (blocked by {blocked_by or 'the queue'})")


# --- the loop --------------------------------------------------------------------------------

def notify() -> None:
    """A session of this process ended (or a test moved the world): every queued job's loop
    wakes now instead of at its next poll."""
    for ev in list(_WAKERS):
        ev.set()


def _settle_from_row(job: Job, row: sqlite3.Row | None) -> None:
    """The row moved under the job (the queue tool's drop, the stale sweep, a second
    process): the row is the truth, copied onto the Job so its next changed() cannot write
    `queued` back over it (amendment 7b), and the waits settle."""
    if row is None:
        job.status, job.error = "lost", "the job's row is gone"
    else:
        job.status, job.error, job.finished_at = row["status"], row["error"], row["finished_at"]
    job._log_safe(f"[queue] left the queue: {job.status} ({job.error})")
    job.record_transcript()
    job._done.set()


def end_queued(conn: sqlite3.Connection, job: Job, error: str, now: float | None = None) -> None:
    """The job leaves the queue without a session: ``dropped`` with ``error``, the entry gone,
    ``started_at`` null (§7 "Accepted"). Never raises: a failing write falls back to the plain
    row update, and the job settles either way. A job already settled is left as it is (a
    cancel_job that dropped it before its task's loop saw the cancel)."""
    if job.settled:
        return
    now = time.time() if now is None else now
    try:
        wrote = queue.mark_dropped(conn, job.id, error, now)
    except Exception as e:  # noqa: BLE001 -- the job must settle whatever the database says
        job._log_safe(f"[queue-error] drop: {type(e).__name__}: {e}")
        wrote = None
    if wrote is False:
        # the row was not queued any more: another process ended it first; its word stands
        try:
            row = conn.execute("SELECT status, error, finished_at FROM jobs WHERE id = ?",
                               (job.id,)).fetchone()
        except Exception:  # noqa: BLE001
            row = None
        if row is not None and row["status"] != "queued":
            _settle_from_row(job, row)
            return
    job.status, job.error, job.finished_at = "dropped", error, now
    job._log_safe(f"[queue] dropped: {error}")
    job.changed()  # the in-memory record now agrees with the row; a failure is a logged line
    job.record_transcript()
    job._done.set()


def _row_changed(job: Job, probe: dict) -> str | None:
    """§4 step 6: down, no longer serving the job's id, or a different window. A window the
    probe did not report is not a change (a read that could not see never says moved)."""
    if not probe.get("up"):
        return f"endpoint {job.endpoint} is down" + (f" ({probe['error']})" if probe.get("error") else "")
    if job.model not in (probe.get("models") or ()):
        return f"endpoint {job.endpoint} no longer serves {job.model}"
    window = (probe.get("context") or {}).get(job.model)
    if job.context and window and window != job.context:
        return f"endpoint {job.endpoint} changed its window for {job.model}: {job.context} -> {window}"
    return None


def _drop_row_down(conn: sqlite3.Connection, job: Job, why: str, now: float) -> None:
    routed = job.routed_by is not None
    error = ROW_DOWN if routed else MODEL_UNAVAILABLE
    if routed:
        try:
            _merge_flags(conn, job.id, row_down={"at": now, "why": why, "guidance": ROW_DOWN_GUIDANCE})
        except Exception as e:  # noqa: BLE001 -- the drop itself must still happen
            job._log_safe(f"[queue-error] ticket: {type(e).__name__}: {e}")
    job.last_activity = f"dropped at admission: {why}"
    job._log_safe(f"[queue] {why} at the job's admission; {ROW_DOWN_GUIDANCE}")
    end_queued(conn, job, error, now)


def _last_progress(conn: sqlite3.Connection, job: Job) -> float | None:
    """The job's last forward progress: its start, its last request, its last tool call."""
    req = conn.execute("SELECT MAX(t) FROM requests WHERE job_id = ?", (job.id,)).fetchone()[0]
    tool = conn.execute("SELECT MAX(COALESCE(finished, t)) FROM tool_calls WHERE job_id = ?",
                        (job.id,)).fetchone()[0]
    moments = [x for x in (job.started_at, req, tool) if x is not None]
    return max(moments) if moments else None


async def stall_scan(store: JobStore, conn: sqlite3.Connection, reg: Registry, now: float) -> None:
    """§7 "Stalled sessions": this process's running sessions with no progress for STALL_S get
    their endpoint probed, a ``[stalled]`` line, the notice as ``last_activity`` (until the next
    message replaces it) and, routed, ``flags.stalled`` on the ticket. One notice per stall;
    nothing is cancelled. Every admission wake calls it, and so does every sampler tick (fold
    2): a stalled session goes unnoticed while nothing is queued behind it on the wake alone."""
    for j in list(store.jobs.values()):
        if j.status != "running" or j._conn is None:
            continue
        last = _last_progress(conn, j)
        if last is None or now - last < STALL_S or j._stall_seen == last:
            continue
        ep = reg.endpoints.get(j.endpoint)
        up = False
        if ep is not None:
            up = bool((await reg.probe(ep)).get("up"))
        text = f"stalled: no progress for {int((now - last) // 60)} min, endpoint {'up' if up else 'down'}"
        j._stall_seen = last
        j._log_safe(f"[stalled] {text}")
        j.last_activity = text
        j.changed()
        if j.routed_by is not None:
            _merge_flags(conn, j.id, stalled={"at": now, "last_progress": last, "endpoint_up": up,
                                              "notice": text})


async def _reapply(conn: sqlite3.Connection, job: Job, reg: Registry, routed: Routed,
                   facts: Facts, now: float) -> Facts | None:
    """§6 step 5 at a wake: the policy re-applied with the current waits. A divert to another
    row moves the entry and the record (endpoint, served id, window, routed_by) and the ticket
    records it and when; anything else leaves the job where it is (no re-route, §4 step 6)."""
    view = await decide(reg, conn, routed.router_ep, routed.text, routed.scores, job.priority,
                        routed.tolerance, avoid_served_id=routed.avoid_served_id, own_job=job.id,
                        now=now, probes={job.endpoint: facts.probe})
    d = view.decision
    current = routed.ticket.get("chosen_row")
    if d.chosen is None or not d.divert or d.chosen.row.id == current:
        return None
    f = view.facts[d.chosen.row.id]
    ep, served = f.endpoint, d.chosen.row.served_id
    res = Resolution.make(ep, served, f.probe)
    pool = _pool_of(view)
    routed_by = {"router": routed.router_ep.name, "reason": d.chosen_reason, "pool": pool}
    with db.immediate(conn):
        eid = db.lookup_id(conn, "endpoints", name=ep.name, base_url=ep.url, backend=ep.backend,
                           host=ep.host)
        mid = db.lookup_id(conn, "models", served_id=served)
        moved = conn.execute(
            "UPDATE jobs SET endpoint_id = ?, model_id = ?, context = ?, routed_by = ?, updated = ?"
            " WHERE id = ? AND status = 'queued'",
            (eid, mid, res.context, json.dumps(routed_by), now, job.id)).rowcount
        if not moved:
            return None   # the row left `queued` meanwhile: the next wake reads why
        conn.execute("UPDATE queue_entries SET endpoint_id = ?, blocked_by = NULL"
                     " WHERE job_id = ? AND admitted IS NULL", (eid, job.id))
        # the position it now holds on the NEW row (the ticket's enqueue position was the old
        # row's); stored among the flags like insert_ticket stores it, lifted by read_ticket
        position = queue.position(conn, job.id, now)
        r = conn.execute("SELECT flags FROM router_tickets WHERE job_id = ?", (job.id,)).fetchone()
        flags = json.loads(r["flags"]) if r is not None and r["flags"] else {}
        flags.update(reason=d.chosen_reason, diverted_from=current, wait_cause=d.wait_cause,
                     pool=pool, divert_at_wake=True, position=position)
        pass_ids = {c.pass_id for c in d.chosen.cells}
        conn.execute("UPDATE router_tickets SET chosen_row = ?, divert = 1, divert_at = ?, rows = ?,"
                     " pass_id = ?, flags = ? WHERE job_id = ?",
                     (d.chosen.row.id, now, json.dumps(view.rows),
                      next(iter(pass_ids)) if len(pass_ids) == 1 else None, json.dumps(flags), job.id))
    job.endpoint, job.model, job.base_url, job.context = ep.name, served, ep.url, res.context
    job.endpoint_id, job.model_id, job.routed_by = eid, mid, routed_by
    routed.ticket.update(chosen_row=d.chosen.row.id, divert=True, divert_at=now,
                         position=position)
    routed.decision, routed.resolution, routed.pool, routed.view = d, res, pool, view
    job._log_safe(f"[queue] {d.chosen_reason} (re-applied at a wake)")
    f.endpoint_id = eid
    f.ours = queue.occupancy(conn, [eid], now)
    routed.facts = f
    return f


async def _wake(store: JobStore, conn: sqlite3.Connection, job: Job, reg: Registry,
                routed: Routed | None) -> Resolution | bool:
    """One wake: True when the job left the queue without a session, a Resolution when it
    was admitted, False to keep waiting."""
    row = conn.execute("SELECT status, error, finished_at FROM jobs WHERE id = ?", (job.id,)).fetchone()
    if row is None or row["status"] != "queued":
        _settle_from_row(job, row)
        return True
    entry = conn.execute("SELECT admitted FROM queue_entries WHERE job_id = ?", (job.id,)).fetchone()
    if entry is None:
        end_queued(conn, job, ENTRY_REMOVED)
        return True
    now = time.time()
    await stall_scan(store, conn, reg, now)
    ep = reg.endpoints.get(job.endpoint)
    if ep is None:
        why: str | None = f"endpoint {job.endpoint} is no longer in the config"
        facts = None
    else:
        facts = await endpoint_facts(reg, conn, ep, job.model, now, endpoint_id=job.endpoint_id)
        why = _row_changed(job, facts.probe)
    if entry["admitted"] is not None and facts is not None and why is None:
        _mark_admitted(conn, job, now)   # admitted by an earlier wake whose return was lost
        return Resolution.make(facts.endpoint, job.model, facts.probe)
    if why is not None:
        if queue.position(conn, job.id, time.time()) <= 1:
            _drop_row_down(conn, job, why, time.time())
            return True
        return False   # not its turn yet: an endpoint restarting reads as down for a minute
    if routed is not None:
        moved = await _reapply(conn, job, reg, routed, facts, now)
        if moved is not None:
            facts = moved
    now = time.time()
    if try_admit_now(conn, job, facts, now):
        job._log_safe(f"[queue] admitted on {job.endpoint} after {now - (job.created_at or now):.0f} s")
        return Resolution.make(facts.endpoint, job.model, facts.probe)
    job._queue_estimate = estimate(conn, job, facts, routed.scores if routed else None, now)
    return False


async def admit_or_wait(store: JobStore, conn: sqlite3.Connection, job: Job, reg: Registry,
                        poll_s: float | None = None, *, routed: Routed | None = None) -> Resolution | None:
    """The queued job's loop (§7): sleep until a wake (a session of this process ended, or the
    poll, ``POLL_S`` unless ``poll_s``), then ``_wake``. Returns the Resolution to run on once
    admitted (a divert at a wake changes it), or None when the job left the queue without a
    session (dropped, lost, cancelled while queued); the job is then settled. A wake that
    raises is logged and retried: an exception never ends the loop or leaves the job queued
    without its task. A cancel (cancel_job) drops the job: ``cancelled while queued``."""
    routed = routed if routed is not None else job._route
    ev = asyncio.Event()
    _WAKERS.add(ev)
    try:
        while True:
            try:
                await asyncio.wait_for(ev.wait(), POLL_S if poll_s is None else poll_s)
            except asyncio.TimeoutError:
                pass
            ev.clear()
            try:
                got = await _wake(store, conn, job, reg, routed)
            except Exception as e:  # noqa: BLE001 -- §14: logged, the job stays queued, retried
                job._log_safe(f"[queue-error] {type(e).__name__}: {e}")
                continue
            if got is True:
                return None
            if got is not False:
                return got
    except asyncio.CancelledError:
        end_queued(conn, job, CANCELLED_WHILE_QUEUED)
        return None
    finally:
        _WAKERS.discard(ev)


# --- the queue tool's view -------------------------------------------------------------------

def queue_priority(rank: int) -> str:
    """The priority name of a queue_entries.priority_rank."""
    return next((p for p, r in queue.PRIORITY_RANK.items() if r == rank), str(rank))


async def queue_view(reg: Registry, conn: sqlite3.Connection, ep: Endpoint,
                     now: float | None = None) -> dict[str, Any]:
    """§11 ``queue``: one endpoint's sessions and live facts, our running sessions, and the
    queued entries in order with their positions and expected waits."""
    now = time.time() if now is None else now
    probe = await reg.probe(ep)
    served = (probe.get("models") or [""])[0]
    f = await endpoint_facts(reg, conn, ep, served, now, probes={ep.name: probe})
    cap = f.capacity
    running, queued = [], []
    if f.endpoint_id is not None:
        for r in conn.execute(
                "SELECT q.job_id, q.admitted, q.forced, j.status, j.started_at, j.priority,"
                " m.served_id FROM queue_entries q JOIN jobs j ON j.id = q.job_id"
                " JOIN mcp_servers s ON s.id = q.owner_server LEFT JOIN models m ON m.id = j.model_id"
                f" WHERE q.endpoint_id = ? AND q.admitted IS NOT NULL"
                f" AND j.status IN ('queued', 'running') AND {queue._LIVE} ORDER BY q.admitted",
                (f.endpoint_id, now - SERVER_LIVE_S)).fetchall():
            running.append({"job_id": r["job_id"], "status": r["status"], "model": r["served_id"],
                            "priority": r["priority"], "admitted": r["admitted"],
                            "started_at": r["started_at"], "forced": bool(r["forced"])})
        row = _live_row(conn, ep.name, served) or _bare_row(ep, served)
        sessions = _sessions(conn, f.endpoint_id, now)
        waiting = _waiting(conn, f.endpoint_id, now)
        for i, w in enumerate(waiting):
            band = _band(w["scores"])
            s = BriefScores({"complexity": band} if band is not None else {}, {}, set(), False, {}, 0)
            cand = Candidate(row=row, cells=[], capacity=cap, ours_running=f.ours, queued_ahead=i,
                             window=None, expected_wait_s=None, excluded_by=None, pool=f.pool,
                             pool_admits=f.pool_admits, running=sessions,
                             ahead_bands=[_band(a["scores"]) for a in waiting[:i]])
            queued.append({"job_id": w["job_id"], "position": i + 1,
                           "priority": queue_priority(w["priority_rank"]), "enqueued": w["enqueued"],
                           "blocked_by": w["blocked_by"],
                           "expected_wait_s": _finite(expected_wait(cand, s, now))})
    pool = None
    if f.pool is not None:
        pool = {"name": f.pool, "max_concurrent": reg.pools[f.pool]["max_concurrent"],
                "effective_cap": f.pool_cap, "ours": queue.occupancy(conn, f.pool_ids or [], now)}
    return {"endpoint": ep.name,
            "sessions": {"kind": cap.kind, "capacity": cap.sessions_capacity, "ours_running": f.ours,
                         "foreign": max(0, cap.requests_running - f.ours),
                         "max_concurrent": ep.max_concurrent},
            "pool": pool, "running": running, "queued": queued, "facts": probe}
