"""Selection (docs/superpowers/specs/2026-11-02-router-design.md §6; the matched axes §5; "can
admit", the curve and the bottleneck rule §7; the avoid_job filter and the pool rule §7
"Accepted"/"Decided", 2026-11-04).

A pure module: no I/O, no database, no event loop. The caller (router plan Task 12) reads the
live facts and the queue, builds one ``Candidate`` per scorecard row, and hands them here with
the brief's ``BriefScores``; ``select`` filters, ranks by shortfall, applies the priority
policy and returns a ``Decision`` whose ``considered`` list is the ticket's row table. A wake
re-applies the policy by calling ``select`` again with the current waits."""

import heapq
import math
import statistics
from dataclasses import dataclass, field, replace

from localagents.router import queue
from localagents.router.capacity import Capacity
from localagents.router.decision import BriefScores
from localagents.router.scorecard import Cell, Row, software_dev_for

# §5's match table: the brief's dimension -> the row's cell. language picks the software_dev
# cell (software_dev_for); context_load is the window filter only, never a cell.
MATCH = {"software_dev": "software_dev", "tool_use": "tool_use", "computer_use": "computer_use",
         "complexity": "complexity", "specification": "judgement"}

# §6 "Expected wait": with too little job history the band's default is used and the estimate
# says "default". Band 0-2 come from the calibration-set spec §7 "band times (0-2) | measured",
# 3-4 "from the operator's job history". A row's ``band_times`` (calibration probe medians, tens
# of seconds) are never read as wall-clock time (True-now 2026-11-15: "Probe seconds are not a
# wall-clock prediction").
BAND_TIMES_S = {0: 120, 1: 600, 2: 1800, 3: 3600, 4: 5400}

# A session or a queued job whose complexity band is unknown (a named job carries no ticket,
# an undetermined axis carries no band) reads at the brief's own band, else at this middle one.
FALLBACK_BAND = 2

# §6 "Expected wait": with the default table, a running session's remaining time is floored at
# this fraction of its band time, "so an overrunning job never reads as nearly done". With job
# history the conditional residual takes the floor's place (True-now 2026-11-15); this fraction
# of the history's time is then only the residual's fallback (no past job ran that long) and a
# foreign session's hold.
OVERRUN_FLOOR = 0.25

# §6 "Expected wait" (True-now 2026-11-15): a time is read from JOB HISTORY, the finished
# non-calibration jobs on the same endpoint and served id. The newest HISTORY_N of them are read
# (one index walk, never the endpoint's whole history: recent work is the better predictor and
# the cost is bounded per decision and per wake); fewer than MIN_HISTORY is "too little
# history" and the band's default table is used; MIN_AT_BAND of them carrying the job's band is
# "enough" to read that band alone, else all of them. HISTORY_STATUSES are the ends of a session
# that held its slot for the whole of its run: ``dropped`` and ``lost`` never ran (or their end
# is a sweep's moment, not the session's), and ``cancelled`` is excluded because a cancel cuts a
# run short by a decision outside it (the live cancels were a minute or two), which would pull
# the median down; a ``timeout`` did hold the slot until its limit, so it counts.
HISTORY_N = 20
MIN_HISTORY = 5
MIN_AT_BAND = 5
HISTORY_STATUSES = ("succeeded", "failed", "timeout")

PRIORITIES = ("urgent", "normal", "background")

# shortfalls are sums of quarters and eighths divided by a small count: compare them rounded so
# two rows equal on paper are a tie, not a float accident
_PLACES = 9


@dataclass
class History:
    """The finished non-calibration jobs on a candidate's endpoint x served id, newest first,
    at most ``HISTORY_N``: each one's run seconds and the complexity band its ticket scored
    (None: a named job, or the axis undetermined). The caller reads it (``dispatch.history``);
    the arithmetic here stays pure."""

    runs: list[tuple[float, int | None]] = field(default_factory=list)


@dataclass
class Session:
    """One of our sessions running on the candidate's endpoint: when it started (the job's
    ``started_at``) and the complexity band its ticket scored, None for a named job."""

    started_at: float
    band: int | None = None


@dataclass
class Candidate:
    """One scorecard row with its live facts, as Task 12 builds it. ``capacity`` None, or a
    capacity of zero sessions (``capacity.from_probe`` on a down probe), is facts_unavailable.
    ``window`` is the served window for the row's id (None: not reported, never filtered).
    ``expected_wait_s`` is ``expected_wait`` computed by the caller (None: unknown).
    ``excluded_by`` is the operator ``exclude:`` condition that held, if one did.
    ``pool``/``pool_admits`` (amendment 2): the row's pool name and whether the queue's
    occupancy across that pool leaves room. ``running``/``ahead_bands`` feed ``expected_wait``:
    our sessions there, and the complexity bands of the queued jobs ahead in queue order
    (padded with unknowns up to ``queued_ahead``). ``queued_higher``: how many of the queued
    jobs ahead are of a higher priority than this job; None reads every queued job ahead
    (background's condition, fail-closed). ``history`` is the endpoint x served id's job
    history (None: not read, the band defaults)."""

    row: Row
    cells: list[Cell]
    capacity: Capacity | None
    ours_running: int
    queued_ahead: int
    window: int | None
    expected_wait_s: float | None
    excluded_by: str | None
    pool: str | None = None
    pool_admits: bool = True
    running: list[Session] = field(default_factory=list)
    ahead_bands: list[int | None] = field(default_factory=list)
    queued_higher: int | None = None
    history: History | None = None


@dataclass
class Considered:
    """One row of the ticket's table: its shortfall or the filter that removed it.
    ``admits``/``acceptable`` (not in the plan's interface, added for Task 12's ``would_queue``
    alternatives): whether the row could admit this job now, and whether it was within
    tolerance; both None/False for a filtered row."""

    row_id: int
    endpoint: str
    served_id: str
    shortfall: float | None
    filtered: str | None
    predicted_speed: float | None
    queued: int
    admits: bool | None = None
    acceptable: bool = False


@dataclass
class Decision:
    """``chosen`` is the row the job goes to; ``queue_for`` is that same candidate when it
    cannot admit now (the job queues there), None when it admits at once. ``reason`` names a
    refusal (``no_calibrated_row`` | ``no_acceptable_row``), None when a row is chosen;
    ``chosen_reason`` is then the one-line sentence for ``routed_by.reason`` (amendment 4).
    ``wait_cause`` (amendment 2) is ``pool`` | ``endpoint`` when the best row could not admit
    (the job queues or diverts), naming what blocked it."""

    chosen: Candidate | None
    divert: bool
    queue_for: Candidate | None
    reason: str | None
    considered: list[Considered]
    undetermined: list[str]
    tie_break: bool
    wait_cause: str | None = None
    chosen_reason: str | None = None


# --- the arithmetic (§6 step 2) -------------------------------------------------------------------

def _known(cell: Cell | None) -> bool:
    return cell is not None and cell.count > 0 and cell.estimate is not None


def weight(demand) -> float:
    return demand / 4


def gap(demand, cell: Cell | None) -> float:
    """``max(0, demand - estimate)`` for a cell with evidence, else the full demand: a row
    with nothing shown on an axis counts as zero there (§6)."""
    if _known(cell):
        return max(0, demand - cell.estimate)
    return demand


def penalty(cell: Cell | None) -> float:
    """``(4 - estimate) / 8`` for completeness or adherence; an unknown cell counts as 0."""
    return (4 - (cell.estimate if _known(cell) else 0)) / 8


def _determined(scores: BriefScores, unproven: frozenset[str]) -> dict:
    """The brief's values the selection may read: determined by d3, and not on an axis the
    definitions' baseline gate lists unproven (amendment 3: always undetermined)."""
    gone = set(scores.undetermined) | set(unproven)
    return {k: v for k, v in scores.values.items() if k not in gone}


def _undetermined(scores: BriefScores, unproven: frozenset[str]) -> list[str]:
    return sorted(set(scores.undetermined) | (set(unproven) & set(scores.values)))


def _cell(cells: list[Cell], dimension: str) -> Cell | None:
    for c in cells:
        if c.dimension == dimension and c.language == "":
            return c
    return None


def shortfall(scores: BriefScores, cand: Candidate,
              unproven: frozenset[str] = frozenset()) -> float:
    """§6: the weighted gaps over the determined matched axes, divided by their number, plus
    the completeness and adherence penalties. A brief with nothing determined is the
    penalties alone. software_dev reads the language's cell when the language is determined
    and that cell has evidence, else the aggregate."""
    values = _determined(scores, unproven)
    language = values.get("language")
    total, axes = 0.0, 0
    for axis, dimension in MATCH.items():
        if axis not in values:
            continue
        demand = int(values[axis])
        if dimension == "software_dev":
            cell = software_dev_for(cand.cells, language if isinstance(language, str) else "")
        else:
            cell = _cell(cand.cells, dimension)
        total += gap(demand, cell) * weight(demand)
        axes += 1
    matched = total / axes if axes else 0.0
    return (matched + penalty(_cell(cand.cells, "completeness"))
            + penalty(_cell(cand.cells, "adherence")))


# --- expected wait (§6 "Expected wait") -----------------------------------------------------------

def brief_band(scores: BriefScores, unproven: frozenset[str] = frozenset()) -> int:
    """The brief's complexity band as the wait reads it: determined (not undetermined, not
    unproven) and an integer band, else ``FALLBACK_BAND``. The one reading: dispatch has no copy
    of its own (two copies once disagreed on a bool or a non-integer value)."""
    value = _determined(scores, unproven).get("complexity")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return FALLBACK_BAND


def _sample(history: History | None, band: int | None) -> list[float] | None:
    """The run seconds a time at ``band`` is read from: the history's runs at that band when at
    least ``MIN_AT_BAND`` carry it, else all of them; None when the history is too little
    (fewer than ``MIN_HISTORY`` runs) and the default table answers. A band of None (a named
    session, an unknown band) never has enough at its band: it reads all of them."""
    if history is None or len(history.runs) < MIN_HISTORY:
        return None
    if band is not None:
        at = [secs for secs, b in history.runs if b == band]
        if len(at) >= MIN_AT_BAND:
            return at
    return [secs for secs, _ in history.runs]


def _default_time(band: int | None, fallback: int) -> float:
    b = band if band is not None else fallback
    return float(BAND_TIMES_S.get(b, BAND_TIMES_S[FALLBACK_BAND]))


def job_time(history: History | None, band: int | None,
             fallback: int = FALLBACK_BAND) -> tuple[float, bool]:
    """A session's or a queued job's expected run time, and whether it is the default table's
    (§6 True-now 2026-11-15): the median of the history's sample at the band (``_sample``), else
    the band's default (a band of None reads at ``fallback``, the brief's band)."""
    sample = _sample(history, band)
    if sample is None:
        return _default_time(band, fallback), True
    return float(statistics.median(sample)), False


def remaining(history: History | None, band: int | None, elapsed: float,
              fallback: int = FALLBACK_BAND) -> float:
    """A running session's time left. With history: its expected time minus elapsed, with the
    conditional residual in the place the quarter-band floor held, the median of how much longer
    the past jobs that ran at least ``elapsed`` went on to run (True-now 2026-11-15); so a
    session past its expected time reads that residual, never "nearly done". When no past job
    ran that long the residual's fallback is a quarter of the expected time (the old floor's
    reading). With the default table: the band time minus elapsed, floored at a quarter."""
    sample = _sample(history, band)
    if sample is None:
        total = _default_time(band, fallback)
        return max(total - elapsed, total * OVERRUN_FLOOR)
    total = float(statistics.median(sample))
    longer = [secs - elapsed for secs in sample if secs >= elapsed]
    tail = float(statistics.median(longer)) if longer else total * OVERRUN_FLOOR
    return max(total - elapsed, tail)


def wait_default(cand: Candidate) -> bool:
    """§6: the estimate says "default" when its times came from the band table, not job
    history. Every time ``expected_wait`` reads comes from the one history, so that is exactly
    when the history is too little (``_sample``'s rule). The ticket's ``wait_default`` and
    ``est_wait_default`` read this, so the flag and the number cannot disagree."""
    return cand.history is None or len(cand.history.runs) < MIN_HISTORY


def usable_slots(capacity: Capacity, priority: str) -> int:
    """The session slots this priority may take on the endpoint: a slot the bottleneck rule
    holds from the asking priority counts as occupied (§6 True-now 2026-11-15). Read through
    ``queue.endpoint_hold``, the rule admission applies, at each occupancy k from empty (the
    caller counts the foreign sessions as occupants, so they are not counted twice here): slot
    k + 1 is usable when nothing holds it at occupancy k. The rule bottlenecks "from n on", so
    the usable slots are the first ones; background is never held."""
    idle = replace(capacity, requests_running=0)
    return sum(1 for k in range(capacity.sessions_capacity)
               if queue.endpoint_hold(idle, k, priority) is None)


def expected_wait(cand: Candidate, scores: BriefScores, now: float,
                  unproven: frozenset[str] = frozenset(), priority: str = "normal") -> float:
    """§6: the smallest, over the row's session slots this ``priority`` may take, of the time
    until the slot frees, with the queued jobs ahead taking the slots first. Times are read from
    ``cand.history`` (``job_time``/``remaining``: job history, else the band defaults); a
    session or queued job with no band of its own reads across the whole history (with the
    default table: at the brief's complexity band, else the middle band). Foreign sessions
    (``max(0, requests_running - ours)``) hold a slot for a quarter of the expected time: their
    age is not ours to know. Our sessions the caller did not describe read as just started. A
    slot the bottleneck rule holds from ``priority`` counts as occupied (``usable_slots``); the
    default ``normal`` is the fail-closed reading (background is never held). No session slots
    at all is an infinite wait."""
    cap = cand.capacity
    if cap is None or cap.sessions_capacity <= 0:
        return math.inf
    fallback = brief_band(scores, unproven)
    history = cand.history

    def left(session: Session | None) -> float:
        band = session.band if session is not None else None
        elapsed = max(0.0, now - session.started_at) if session is not None else 0.0
        return remaining(history, band, elapsed, fallback)

    sessions: list[Session | None] = list(cand.running[:cand.ours_running])
    sessions += [None] * (cand.ours_running - len(sessions))
    occupants = [left(s) for s in sessions]
    foreign = max(0, cap.requests_running - cand.ours_running)
    occupants += [job_time(history, None, fallback)[0] * OVERRUN_FLOOR] * foreign
    occupants.sort()
    # the slots' free-at times: with more occupants than usable slots (a forced admission, or a
    # slot the bottleneck rule holds from this priority) a slot opens only when the excess has
    # finished, so the earliest openings are skipped
    excess = len(occupants) - usable_slots(cap, priority)
    if excess >= 0:
        slots = occupants[excess:]
    else:
        slots = occupants + [0.0] * (-excess)
    heapq.heapify(slots)
    ahead = list(cand.ahead_bands[:cand.queued_ahead])
    ahead += [None] * (cand.queued_ahead - len(ahead))
    for band in ahead:
        opens = heapq.heappop(slots)
        heapq.heappush(slots, opens + job_time(history, band, fallback)[0])
    return float(slots[0])


# --- selection (§6 steps 1, 3-5) ------------------------------------------------------------------

def _facts_unavailable(cand: Candidate) -> bool:
    return cand.capacity is None or cand.capacity.sessions_capacity <= 0


def _filter(cand: Candidate, load_tokens: int | None, calibrated, brief_files: int | None,
            avoid_served_id: str | None) -> str | None:
    """The first filter that removes the row, in the plan's order with avoid_job first
    (amendment 1), or None."""
    if avoid_served_id is not None and cand.row.served_id == avoid_served_id:
        return "avoid_job"
    if cand.row.id not in calibrated:
        return "uncalibrated"
    if cand.window is not None and load_tokens is not None and cand.window < load_tokens:
        return "window"
    ceiling = _cell(cand.cells, "ceiling")
    if brief_files is not None and _known(ceiling) and brief_files > ceiling.estimate:
        return "ceiling"
    if _facts_unavailable(cand):
        return "facts_unavailable"
    if cand.excluded_by:
        return "excluded"
    return None


def _predicted_speed(cand: Candidate) -> float | None:
    """The curve's prediction for the session this job would be (our sessions plus the
    foreign ones, §7's occupancy, plus this one), divided by the row's measured speed factor
    when it has one. The curve alone reads 1.0 for every idle row, so an idle fleet tied by
    shortfall fell to row order (the first live routed job, 2026-11-04; §6 True-now): the
    speed factor is calibration's one cross-row speed reading, median actual over nominal
    seconds, so a bigger factor is a slower row and divides the prediction down. A row
    without a factor (not yet calibrated, a bare row) keeps the curve's reading."""
    cap = cand.capacity
    if cap is None:
        return None
    foreign = max(0, cap.requests_running - cand.ours_running)
    curve = cap.curve.predict(cand.ours_running + foreign + 1)
    factor = cand.row.speed_factor
    if factor is not None and factor > 0:
        return curve / factor
    return curve


def _can_admit(cand: Candidate, priority: str) -> bool:
    """``queue.room``, the predicate ``try_admit`` applies (fix wave 3, D1): §7's sessions
    rule, §6's bottleneck rule (the session that would be the curve's bottleneck on a measured
    point is admitted only for background work), the head of the endpoint's queue (``queued_ahead``), and the
    pool (``pool_admits``, ``queue.pool_room``'s answer)."""
    return queue.room(cand.capacity, cand.ours_running, priority, ahead=cand.queued_ahead,
                      pool_admits=cand.pool_admits)


def _label(cand: Candidate) -> str:
    return f"{cand.row.endpoint_name} x {cand.row.served_id}"


def _blocked_by(cand: Candidate, priority: str) -> tuple[str, str]:
    """(wait_cause, the words for the reason line) for a row that cannot admit. The pool is
    named whenever it refused (amendment 2), even when the endpoint is full as well."""
    if not cand.pool_admits:
        return "pool", f"pool {cand.pool}" if cand.pool else "pool"
    hold = queue.endpoint_hold(cand.capacity, cand.ours_running, priority)
    if hold == "full":
        return "endpoint", "endpoint busy"
    if hold == "bottleneck":
        return "endpoint", "bottleneck slot"
    return "endpoint", "behind the queue"


def select(scores: BriefScores, candidates: list[Candidate], priority: str, tolerance: float,
           divert_after_s: float, load_tokens: int | None, calibrated: set[int],
           brief_files: int | None, avoid_served_id: str | None = None,
           unproven: frozenset[str] = frozenset()) -> Decision:
    """§6: filter by facts, rank by shortfall, keep the rows within ``tolerance`` of the best,
    break ties by predicted speed then fewer queued, and apply the priority policy. Every
    candidate appears in ``considered`` in the order given."""
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}, not {priority!r}")
    undetermined = _undetermined(scores, unproven)
    considered: list[Considered] = []
    survivors: list[tuple[Candidate, float, float, int]] = []   # cand, shortfall, speed, index
    avoided: list[tuple[Candidate, str]] = []   # the row and its label, filtered by avoid_job
    for index, cand in enumerate(candidates):
        why = _filter(cand, load_tokens, calibrated, brief_files, avoid_served_id)
        speed = _predicted_speed(cand)
        entry = Considered(cand.row.id, cand.row.endpoint_name, cand.row.served_id, None, why,
                           speed, cand.queued_ahead)
        considered.append(entry)
        if why == "avoid_job":
            avoided.append((cand, _label(cand)))
        if why is not None:
            continue
        entry.shortfall = shortfall(scores, cand, unproven)
        entry.admits = _can_admit(cand, priority)
        survivors.append((cand, entry.shortfall, speed or 0.0, index))

    def refuse(reason: str) -> Decision:
        return Decision(None, False, None, reason, considered, undetermined, False)

    if not any(c.row.id in calibrated for c in candidates):
        return refuse("no_calibrated_row")
    if not survivors:
        return refuse("no_acceptable_row")

    # §6 steps 3-4: lowest shortfall first, ties by the faster prediction, then fewer queued;
    # the caller's order settles anything left so the same input always picks the same row
    survivors.sort(key=lambda s: (round(s[1], _PLACES), -s[2], s[0].queued_ahead, s[3]))
    best_short = round(survivors[0][1], _PLACES)
    tied = [s for s in survivors if round(s[1], _PLACES) == best_short]
    tie_break = len(tied) > 1
    # the reason names the level that actually decided (2026-11-04: a reason that always said
    # "predicted speed, then queue" hid that row order had decided an idle fleet's first job)
    tie_level = None
    if tie_break:
        top, nxt = tied[0], tied[1]
        if round(top[2], _PLACES) != round(nxt[2], _PLACES):
            tie_level = "predicted speed"
        elif top[0].queued_ahead != nxt[0].queued_ahead:
            tie_level = "predicted speed, then queue"
        else:
            tie_level = "predicted speed, then queue, then row order"
    # the best row is always acceptable, whatever tolerance a caller passes
    acceptable = [s for s in survivors
                  if round(s[1] - survivors[0][1], _PLACES) <= max(tolerance, 0.0)]
    for s in acceptable:
        considered[s[3]].acceptable = True

    best = acceptable[0][0]
    best_line = f"shortfall {acceptable[0][1]:.2f} on {_label(best)}; best of {len(survivors)}"
    if tie_break:
        best_line += f"; tie broken by {tie_level}"

    def suffix(chosen_shortfall: float) -> str:
        """Amendment 2: name the avoid_job filter only when it bound the choice. An avoided
        row is named when its shortfall, scored as if the filter were not there, would have
        ranked at or above the chosen row's (it would have been chosen or tied); a row another
        filter would still remove could not have been chosen, so it stays silent. Either way
        the ``considered`` table lists it as ``avoid_job``."""
        names = []
        for cand, label in avoided:
            if _filter(cand, load_tokens, calibrated, brief_files, None) is not None:
                continue   # unfiltered by avoid_job, another filter still removes it
            if round(shortfall(scores, cand, unproven), _PLACES) <= round(chosen_shortfall, _PLACES):
                names.append(label)
        return f"; avoid_job excluded {', '.join(names)}" if names else ""

    if _can_admit(best, priority):
        return Decision(best, False, None, None, considered, undetermined, tie_break,
                        None, best_line + suffix(acceptable[0][1]))

    cause, cause_words = _blocked_by(best, priority)
    free = [s for s in acceptable[1:] if _can_admit(s[0], priority)]
    take = None
    if priority == "urgent" and free:
        take = free[0]
    elif priority == "normal" and free:
        if best.expected_wait_s is not None and best.expected_wait_s >= divert_after_s:
            take = free[0]
    elif priority == "background":
        for s in free:
            higher = s[0].queued_higher if s[0].queued_higher is not None else s[0].queued_ahead
            if higher == 0:
                take = s
                break

    if take is not None:
        line = (f"divert: {_label(take[0])} free (shortfall {take[1]:.2f}),"
                f" {_label(best)} busy ({cause_words})")
        return Decision(take[0], True, None, None, considered, undetermined, tie_break,
                        cause, line + suffix(take[1]))
    return Decision(best, False, best, None, considered, undetermined, tie_break, cause,
                    f"{best_line}; queued ({cause_words}){suffix(acceptable[0][1])}")
