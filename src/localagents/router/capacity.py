"""Capacity in sessions and the throughput curve (docs/superpowers/specs/2026-11-02-router-design.md §7;
the window filter's context load is §6 step 1, the bottleneck rule §6 step 5).

Capacity counts *sessions*, not requests: a running Claude Code session holds no slot and no
KV blocks while it runs tests for ten minutes, so live slot and KV readings say "free" for
most of a job's life (§7). ``from_probe`` turns a ``Registry.probe`` dict into the three
capacity models (partitioned / pooled / unknown); ``Curve`` predicts one session's speed at
the current occupancy — it never refuses, its prediction feeds expected wait, the tie-break
and §6's bottleneck rule (a measured point only: a prior point predicts and holds nothing);
``refine`` is the rule that turns speed samples into measured points, and ``fold_samples`` runs
it at every sampled job's finish over the endpoint's recent ``speed_samples`` rows."""

import json
import math
import statistics
import time
from dataclasses import dataclass, field, replace

from localagents import db
from localagents.registry import Endpoint

# calibration-set spec §7: "context-load tokens per band | 8k / 16k / 32k / 64k / 128k".
# The window filter (§6 step 1) only — never an admission input (§7: "The brief's context
# load is NOT an admission input").
CONTEXT_LOAD_TOKENS = {0: 8_000, 1: 16_000, 2: 32_000, 3: 64_000, 4: 128_000}

# calibration-set spec §7: "prior curves | llama.cpp {1: 1.0, 2: 0.35, 3: 0.25}; vLLM
# {1: 1.0, 2: 0.85, 3: 0.75}; other {1: 1.0, 2: 0.5} | shapes only; the throughput probe
# replaces them". §7: llama.cpp degrades on several slots, vLLM batches.
PRIOR_CURVES = {
    "llamacpp": {1: 1.0, 2: 0.35, 3: 0.25},
    "vllm": {1: 1.0, 2: 0.85, 3: 0.75},
}
_PRIOR_FALLBACK = {1: 1.0, 2: 0.5}  # the "other" family of the same spec row

# §6's bottleneck rule: "aggregate at n below 75% of aggregate at 1, so for the prior shape
# llama.cpp bottlenecks at 2 and vLLM does not" — and since the operator's ruling of
# 2026-11-15 the rule holds a slot only on a MEASURED point: the prior shape predicts and holds
# nothing (``Curve.bottleneck_at``).
BOTTLENECK_RATIO = 0.75

# When speed samples measure a point (§7 "refined by every job's observed speed"; the rule in
# ``Curve.refine``). A job's own work moves its speed (context length, tool mix, speculative
# acceptance: one box's concurrency-1 samples ran 91 to 173 t/s), so one sample measures
# nothing: a concurrency is measured from this many samples there AND this many at 1.
MIN_SAMPLES_PER_POINT = 8
# the newest samples per concurrency the median reads, and how far back a fold looks: a box's
# flags and drivers change without a record, and the window bounds the fold's read
SAMPLES_PER_POINT = 20
SAMPLE_WINDOW_S = 14 * 86_400.0


@dataclass
class Curve:
    """One session's tokens/s at each concurrency, normalised so concurrency 1 is 1.0.
    ``state`` is the endpoint_capacity.state CHECK list (prior | probed | refined); ``n`` is
    the speed samples behind it (the endpoint_capacity.points column), 0 for an unrevised prior.

    ``measured`` is each point's provenance (§6, the operator's ruling 2026-11-15): the keys of
    ``points`` the throughput probe wrote or speed samples at that concurrency refined; every
    other key is still the backend-family prior. It rides in the endpoint_capacity.curve JSON
    beside the points (no column). Left out (None), it is read from ``state`` — the rule for a
    stored row written before the field existed: ``probed`` means every point is measured (the
    probe writes only what it measured, never a prior point), ``prior`` and ``refined`` mean none
    is (a refined row's points cannot be told apart from the prior it merged onto, and a point
    nobody can show was measured holds nothing). A ``prior`` curve has no measured point
    whatever it is given, and a key not in ``points`` is dropped.

    ``served_id`` is the served model the measured points were taken on (None: not recorded,
    a row from before the field; the probe does not record it yet). ``probe`` is the throughput
    probe's own points when a refinement has merged samples over them, kept so every fold
    re-derives from the probe rather than from its own last answer (None: the points are the
    probe's when ``state`` is ``probed``, and there is no probe otherwise). Both ride in the
    JSON beside the points, and only when set."""

    points: dict[int, float]
    state: str = "prior"
    n: int = 0
    measured: frozenset[int] | None = None
    served_id: str | None = None
    probe: dict[int, float] | None = None

    def __post_init__(self) -> None:
        if self.state == "prior":
            self.measured = frozenset()
        elif self.measured is None:
            self.measured = frozenset(self.points) if self.state == "probed" else frozenset()
        else:
            self.measured = frozenset(int(k) for k in self.measured if int(k) in self.points)

    def _at(self, sessions: int) -> float:
        """The curve's value at a concurrency, clamped to its own range (§7: the curve never
        refuses, it predicts at the current occupancy, whatever the occupancy is): below the
        smallest key the smallest key's value, beyond the largest that largest key's value,
        and an interior gap reads the nearest LOWER key — a curve learned at {1, 3} answers
        concurrency 2 with the concurrency-1 value, never a KeyError."""
        if not self.points:
            return 0.0
        lo, hi = min(self.points), max(self.points)
        if sessions <= lo:
            return self.points[lo]
        if sessions >= hi:
            return self.points[hi]
        return self.points[max(k for k in self.points if k <= sessions)]

    def predict(self, sessions_now: int) -> float:
        """A session's expected tokens/s fraction at this occupancy (§7: predicts, never refuses)."""
        return self._at(max(1, sessions_now))  # zero occupancy reads as one

    def bottleneck_at(self, n: int) -> bool:
        """§6: "bottlenecks at n" means from n on — True when the aggregate rule
        (k * value(k) below 75% of the aggregate at 1) holds for ANY k in 2..n, so a box
        that bottlenecks at 2 and flattens after keeps background-only admission at 3
        rather than swinging back open. n of 1 or below is False: a single session is
        never a bottleneck.

        Only a MEASURED point holds (§6, the operator's ruling 2026-11-15): k counts only when
        k is itself a key of ``measured``, read at its own concurrency. A prior point holds
        nothing, on a prior curve or beside measured points. A concurrency ``_at`` answers
        from ANOTHER key (an interior gap reads the nearest lower key, beyond the curve the
        largest) holds nothing on its own: the reading is borrowed, and since k * v grows with
        k for a fixed v, a borrowed reading could never cross the line where its source key
        did not — so this loses no hold a measured key gives (it carries on through "from n
        on") and lets no unmeasured key's value hold at a neighbour. The baseline (the
        value at 1) is the normalisation, not a holding point: a measured ratio was taken
        against the concurrency-1 rate it was measured with."""
        if n <= 1:
            return False
        limit = BOTTLENECK_RATIO * self._at(1)
        return any(k * self.points[k] < limit for k in range(2, n + 1) if k in self.measured)

    def probe_points(self, served_id: str | None = None) -> dict[int, float]:
        """The points the throughput probe measured on this curve: ``probe`` when a refinement
        kept them, else every point of a ``probed`` curve, else none. For ``served_id``, none
        when the curve records another served model: a probe taken on one model is no probe
        for another (a curve that records no model is read as it stands)."""
        if served_id is not None and self.served_id not in (None, served_id):
            return {}
        if self.probe is not None:
            return dict(self.probe)
        return dict(self.points) if self.state == "probed" else {}

    def refine(self, samples: list[tuple[int, float]]) -> "Curve":
        """§7: the curve is "seeded by a backend-family prior ... refined by every job's
        observed speed logged with the occupancy beside it" — MERGE onto this curve, which
        ``fold_samples`` makes the base (the probe's points, else the prior). ``samples`` are
        (concurrency, generate_tps) pairs. The rule, fail-closed (a point not well measured
        predicts and holds nothing, §6):

        - a concurrency counts only with at least ``MIN_SAMPLES_PER_POINT`` samples, and the
          baseline (concurrency 1) needs as many: without it there is nothing to divide by and
          the curve comes back unchanged;
        - a counted concurrency k >= 2 takes the median ratio, median(k) / median(1), and
          becomes measured; a key with too few samples keeps its value and its provenance (a
          prior point stays a prior point);
        - a point the throughput probe measured takes the higher of the probe's ratio and the
          samples': the probe is two requests decoding at once, the worst case a session sees,
          and a session decodes alongside another only part of its run, so a samples' median
          below the probe's is the job mix (long contexts decode slower), not the box. Samples
          can lift a probed point off the line, never push it below the probe;
        - nothing counted at 2 or above: the curve comes back unchanged (point 1 is the
          normalisation, 1.0 either way).

        A refined curve keeps the probe's points in ``probe`` and counts in ``n`` the samples
        behind its counted concurrencies."""
        by_sessions: dict[int, list[float]] = {}
        for sessions, tps in samples:
            by_sessions.setdefault(int(sessions), []).append(float(tps))
        counted = {k: v for k, v in by_sessions.items() if len(v) >= MIN_SAMPLES_PER_POINT}
        if 1 not in counted:
            return self
        base = statistics.median(counted[1])
        if base <= 0:
            return self  # a zero baseline normalises everything away; change nothing
        ratios = {k: statistics.median(v) / base for k, v in counted.items() if k >= 2}
        if not ratios:
            return self
        probe = self.probe_points()
        merged = dict(self.points)
        merged.update({k: max(r, probe[k]) if k in probe else r for k, r in ratios.items()})
        return Curve({k: merged[k] for k in sorted(merged)}, "refined",
                     sum(len(counted[k]) for k in (1, *ratios)),
                     frozenset(self.measured) | {1} | frozenset(ratios), self.served_id,
                     probe or None)

    def first_hold(self) -> int | None:
        """The concurrency the bottleneck rule first holds at (from there on, §6), or None
        when no measured point holds."""
        return next((k for k in range(2, max(self.points, default=1) + 1)
                     if self.bottleneck_at(k)), None)


def prior_curve(kind: str) -> Curve:
    """The backend-family seed (§7); ``kind`` is a ``registry.backend_kind`` value."""
    return Curve(dict(PRIOR_CURVES.get(kind, _PRIOR_FALLBACK)), "prior", 0)


@dataclass
class Capacity:
    """One endpoint's admission facts right now, in sessions (§7). ``requests_running`` is
    every live request the server reports, ours included; ``admits`` separates them."""

    kind: str  # partitioned | pooled | unknown (the endpoint_capacity.kind CHECK)
    sessions_capacity: int
    window: int | None = None  # the served window the model divided by (or the slot's)
    pool_tokens: int | None = None  # the pooled KV pool, tokens, when the model is pooled
    requests_running: int = 0
    curve: Curve = field(default_factory=lambda: prior_curve("other"))
    max_concurrent: int | None = None

    def admits(self, ours_running: int) -> bool:
        """§7's occupancy rule: our admitted entries (queued or running, fresh heartbeat) plus
        foreign sessions — ``max(0, requests_running − ours)``, the live requests that are not
        ours — below capacity. Our own live requests never double-count us: a session between
        requests still holds the admission it was given."""
        foreign = max(0, self.requests_running - ours_running)
        return ours_running + foreign < self.sessions_capacity

    def bottleneck_slot(self, ours_running: int) -> bool:
        """The session this job would become joins our sessions and the foreign ones at the
        same occupancy ``admits`` counts — ours + foreign + 1 — and on a row whose curve
        bottlenecks there, §6 admits it only for background work: the box already holds a
        session that is not ours, and it is as much a neighbor to this job as ours are."""
        foreign = max(0, self.requests_running - ours_running)
        return self.curve.bottleneck_at(ours_running + foreign + 1)


def from_probe(ep: Endpoint, probe: dict, served_id: str, curve: Curve) -> Capacity:
    """The probe dict (``Registry.probe``'s shape) read as one of §7's three models:
    pooled where the KV pool and the served window are both reported, partitioned where
    slots are (strata included — §7 counts them by slots when reported), else unknown at one
    session 'until probed'. A down probe admits nothing."""
    cap = ep.max_concurrent
    if not probe.get("up"):
        # facts_unavailable filters the row (§6 step 1); the capacity reads as zero admission
        return Capacity("unknown", 0, requests_running=0, curve=curve, max_concurrent=cap)
    load = probe.get("load") or {}
    window = (probe.get("context") or {}).get(served_id)
    pool = load.get("kv_cache_size_tokens")
    slots = probe.get("slots") or {}
    if pool is not None and window:
        # §7: floor(kv_cache_size_tokens / window); "the model-specific KV cost is already in
        # those numbers, read from /metrics cache_config_info; the router never estimates"
        kind = "pooled"
        sessions = max(1, math.floor(pool / window))
        pool_tokens: int | None = int(pool)
    elif slots.get("total") is not None:
        kind, sessions, pool_tokens = "partitioned", int(slots["total"]), None
    else:
        kind, sessions, pool_tokens = "unknown", 1, None
    if cap is not None:
        sessions = min(sessions, cap)  # §7: "max_concurrent in the entry caps below any of these"
    running = (load["requests_running"] if "requests_running" in load
               else slots.get("busy", 0))
    return Capacity(kind, sessions, window=window, pool_tokens=pool_tokens,
                    requests_running=int(running), curve=curve, max_concurrent=cap)


def _write_capacity(conn, endpoint_id: int, kind: str, curve: Curve, now: float,
                    by: str | None = None) -> None:
    """The row write, inside the caller's transaction. The curve JSON carries the points and,
    beside them, the measured keys (§6's provenance), the served model and the probe's points
    when the curve has them, and ``by``: the job whose finish refined it, whose transcript has
    the ``[curve]`` line (the trace from a hold back to its samples; a probe's write has none)."""
    blob: dict = {"points": {str(k): v for k, v in curve.points.items()},
                  "measured": sorted(curve.measured)}
    if curve.served_id is not None:
        blob["served_id"] = curve.served_id
    if curve.probe is not None:
        blob["probe"] = {str(k): v for k, v in curve.probe.items()}
    if by is not None:
        blob["by"] = by
    conn.execute(
        """INSERT INTO endpoint_capacity (endpoint_id, kind, curve, state, points, updated)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(endpoint_id) DO UPDATE SET
               kind = excluded.kind, curve = excluded.curve, state = excluded.state,
               points = excluded.points, updated = excluded.updated""",
        (endpoint_id, kind, json.dumps(blob), curve.state, curve.n, now),
    )


def save_capacity(conn, endpoint_id: int, kind: str, curve: Curve) -> None:
    """The endpoint's curve row in ``endpoint_capacity``, one immediate transaction
    (the schema spec's rule: rows only, and the shared connection cannot nest)."""
    with db.immediate(conn):
        _write_capacity(conn, endpoint_id, kind, curve, time.time())


_CAPACITY_ROW_SQL = "SELECT kind, curve, state, points FROM endpoint_capacity WHERE endpoint_id = ?"


def _curve_from(row) -> Curve:
    blob = json.loads(row["curve"])
    measured = blob.get("measured")
    probe = blob.get("probe")
    return Curve({int(k): float(v) for k, v in blob["points"].items()}, row["state"],
                 row["points"], None if measured is None else frozenset(int(k) for k in measured),
                 blob.get("served_id"),
                 None if probe is None else {int(k): float(v) for k, v in probe.items()})


def load_capacity(conn, endpoint_id: int, served_id: str | None = None) -> Curve | None:
    """The stored curve, or None for an endpoint with no curve row yet. A row written before
    the measured field existed reads its provenance from its state (``Curve``'s rule:
    ``probed`` is all measured, ``refined`` none). Read for ``served_id``, a curve that records
    ANOTHER served model keeps its values to predict with and has no measured point: a curve
    measured on one model holds no slot for another (a curve that records none is read as it
    stands)."""
    row = conn.execute(_CAPACITY_ROW_SQL, (endpoint_id,)).fetchone()
    if row is None:
        return None
    curve = _curve_from(row)
    if served_id is not None and curve.served_id not in (None, served_id):
        return replace(curve, measured=frozenset())
    return curve


# the fold's read of an endpoint's recent samples: driven from jobs_created_at over the window
# (the + keeps SQLite off jobs_endpoint_id and jobs_model_id, whose walks grow with the
# history), each sample by its primary key; tests/test_query_plans.py pins the plan
_RECENT_SAMPLES_SQL = (
    "SELECT s.sessions, s.generate_tps FROM jobs j JOIN speed_samples s ON s.job_id = j.id"
    " WHERE j.created_at > ? AND +j.model_id = ? AND j.calibration = 0 AND s.endpoint_id = ?"
    " ORDER BY j.created_at DESC LIMIT ?")
_RECENT_SAMPLES_MAX = 500


def recent_samples(conn, endpoint_id: int, model_id: int, now: float) -> list[tuple[int, float]]:
    """The samples a fold reads (the rule's "which samples count"): this endpoint's, from jobs
    that ran ``model_id`` (a curve learned on one model is not another's), no calibration probe
    (seconds-long short-context work that decodes faster than the jobs it would be the baseline
    for), created within ``SAMPLE_WINDOW_S``, the newest ``SAMPLES_PER_POINT`` per concurrency."""
    out: list[tuple[int, float]] = []
    per: dict[int, int] = {}
    for sessions, tps in conn.execute(_RECENT_SAMPLES_SQL, (now - SAMPLE_WINDOW_S, model_id,
                                                            endpoint_id, _RECENT_SAMPLES_MAX)):
        if per.get(sessions, 0) < SAMPLES_PER_POINT:
            per[sessions] = per.get(sessions, 0) + 1
            out.append((int(sessions), float(tps)))
    return out


def _fold_base(stored: Curve | None, served_id: str, kind: str) -> Curve:
    """The curve a fold refines: the throughput probe's points when the stored curve has them
    and they were taken on ``served_id`` (or the row records no model: a row from before the
    field, adopted by its first fold), else the backend-family prior. Never the stored curve's
    own sample-measured points: every fold re-derives those from the window, so a point whose
    samples age out goes back to the base, and nothing ratchets."""
    probe = stored.probe_points(served_id) if stored is not None else {}
    if probe:
        return Curve(probe, "probed", len(probe), frozenset(probe), served_id)
    return replace(prior_curve(kind), served_id=served_id)


def fold_samples(conn, endpoint_id: int, served_id: str, model_id: int, kind: str,
                 now: float | None = None, *, by: str | None = None
                 ) -> tuple[Curve | None, Curve | None]:
    """§7's third stage, run at a sampled job's finish (feedback's speed-sample write): the
    endpoint's curve re-derived as ``_fold_base(...).refine(recent_samples(...))`` and stored
    when it differs from the row. ``kind`` is a ``registry.backend_kind`` value (the prior's
    family). One immediate transaction, rows only: the read, the window's samples and the
    write cannot interleave with another server's fold or the throughput probe's write. A new
    row's ``kind`` column is ``unknown`` (no live probe is read here; ``from_probe`` reads the
    model live, and nothing reads the column back); an existing row keeps its own. An endpoint
    with no row whose samples measure nothing gets none. Returns (stored before, after); the
    same object twice when nothing changed. ``by`` (the finishing job) rides in the
    written row's JSON."""
    now = time.time() if now is None else now
    with db.immediate(conn):
        row = conn.execute(_CAPACITY_ROW_SQL, (endpoint_id,)).fetchone()
        stored = _curve_from(row) if row is not None else None
        new = _fold_base(stored, served_id, kind).refine(
            recent_samples(conn, endpoint_id, model_id, now))
        if new == stored or (stored is None and not new.measured):
            return stored, stored
        _write_capacity(conn, endpoint_id, row["kind"] if row is not None else "unknown", new,
                        now, by)
    return stored, new


def context_load_tokens(score: int | None, text: str) -> int:
    """The brief's context load for the window filter (§6 step 1) only, never for admission
    (§7). The band's calibration figure floored by the text's own size read at four chars per
    token — decision.py's token reading, the same estimate the client truncates on. A score
    outside the bands (an undetermined axis, or none) is the text term alone: neutral."""
    text_tokens = math.ceil(len(text) / 4)
    band = CONTEXT_LOAD_TOKENS.get(score) if score is not None else None
    return text_tokens if band is None else max(band, text_tokens)
