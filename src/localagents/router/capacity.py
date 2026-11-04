"""Capacity in sessions and the throughput curve (docs/superpowers/specs/2026-11-02-router-design.md §7;
the window filter's context load is §6 step 1, the bottleneck rule §6 step 5).

Capacity counts *sessions*, not requests: a running Claude Code session holds no slot and no
KV blocks while it runs tests for ten minutes, so live slot and KV readings say "free" for
most of a job's life (§7). ``from_probe`` turns a ``Registry.probe`` dict into the three
capacity models (partitioned / pooled / unknown); ``Curve`` predicts one session's speed at
the current occupancy — it never refuses, its prediction feeds expected wait, the tie-break
and §6's bottleneck rule; ``refine`` folds the ``speed_samples`` rows every job leaves behind
back into the curve."""

import json
import math
import statistics
import time
from dataclasses import dataclass, field

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
# llama.cpp bottlenecks at 2 and vLLM does not".
BOTTLENECK_RATIO = 0.75


@dataclass
class Curve:
    """One session's tokens/s at each concurrency, normalised so concurrency 1 is 1.0.
    ``state`` is the endpoint_capacity.state CHECK list (prior | probed | refined); ``n`` is
    the speed samples behind it (the endpoint_capacity.points column), 0 for an unrevised prior."""

    points: dict[int, float]
    state: str = "prior"
    n: int = 0

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
        never a bottleneck."""
        if n <= 1:
            return False
        limit = BOTTLENECK_RATIO * self._at(1)
        return any(k * self._at(k) < limit for k in range(2, n + 1))

    def refine(self, samples: list[tuple[int, float]]) -> "Curve":
        """§7: the curve is "seeded by a backend-family prior ... refined by every job's
        observed speed logged with the occupancy beside it" — MERGE, not replacement: the
        median generate_tps per concurrency (speed_samples rows) normalised by the
        concurrency-1 median updates the prior's points, keys without samples keep the
        prior's value, keys with samples take the median ratio, keys beyond the prior appear.
        Without a concurrency-1 sample there is no baseline to divide by, and the curve
        comes back unchanged."""
        by_sessions: dict[int, list[float]] = {}
        for sessions, tps in samples:
            by_sessions.setdefault(sessions, []).append(tps)
        if 1 not in by_sessions:
            return self
        medians = {k: statistics.median(v) for k, v in by_sessions.items()}
        if medians[1] <= 0:
            return self  # a zero baseline normalises everything away; change nothing
        base = medians[1]
        merged = dict(self.points)
        merged.update({k: m / base for k, m in medians.items()})
        return Curve({k: merged[k] for k in sorted(merged)}, "refined", len(samples))


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


def save_capacity(conn, endpoint_id: int, kind: str, curve: Curve) -> None:
    """The endpoint's curve row in ``endpoint_capacity``, one immediate transaction
    (the schema spec's rule: rows only, and the shared connection cannot nest)."""
    with db.immediate(conn):
        conn.execute(
            """INSERT INTO endpoint_capacity (endpoint_id, kind, curve, state, points, updated)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(endpoint_id) DO UPDATE SET
                   kind = excluded.kind, curve = excluded.curve, state = excluded.state,
                   points = excluded.points, updated = excluded.updated""",
            (endpoint_id, kind,
             json.dumps({"points": {str(k): v for k, v in curve.points.items()}}),
             curve.state, curve.n, time.time()),
        )


def load_capacity(conn, endpoint_id: int) -> Curve | None:
    """The stored curve, or None for an endpoint with no curve row yet."""
    row = conn.execute(
        "SELECT curve, state, points FROM endpoint_capacity WHERE endpoint_id = ?",
        (endpoint_id,),
    ).fetchone()
    if row is None:
        return None
    points = {int(k): float(v) for k, v in json.loads(row["curve"])["points"].items()}
    return Curve(points, row["state"], row["points"])


def context_load_tokens(score: int | None, text: str) -> int:
    """The brief's context load for the window filter (§6 step 1) only, never for admission
    (§7). The band's calibration figure floored by the text's own size read at four chars per
    token — decision.py's token reading, the same estimate the client truncates on. A score
    outside the bands (an undetermined axis, or none) is the text term alone: neutral."""
    text_tokens = math.ceil(len(text) / 4)
    band = CONTEXT_LOAD_TOKENS.get(score) if score is not None else None
    return text_tokens if band is None else max(band, text_tokens)
