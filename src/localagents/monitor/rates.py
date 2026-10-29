"""Rates between two consecutive ``probe_counters`` rows of one endpoint, and reset detection.

Pure functions; nothing here is stored (monitor spec §3: rates are computed at read time). The
delta arithmetic is ``registry.metrics_delta``'s, so the vLLM/llama.cpp/strata split lives in one
place. A reset is any counter going backwards, or strata's ``since`` changing (spec §3, §6): the
interval then has no rate, never a cliff.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..registry import metrics_delta
from .rows import COUNTER_COLUMNS


@dataclass
class Rates:
    prompt_tps: float | None  # prompt tokens processed (not served from cache) per wall second
    gen_tps: float | None  # tokens generated per wall second
    cached_tps: float | None  # prompt tokens served from the prefix cache per wall second
    spec_acceptance: float | None  # accepted / drafted over the interval; None with no drafts
    reset: bool
    # why there is no rate: a reset (the counters that went backwards, or since changed) or a
    # gap ("gap: <n> down probe(s) in the interval" — the collector suppresses the rate without
    # calling rates(); it is not a reset, and the collector writes a ``restarted`` event for it
    # only when the counters say the reset spanned the gap, suffixed " (during a gap)")
    reason: str | None = None


def _num(v: float) -> str:
    """A counter reading as a reset detail names it (review of 3.3, third server seam): whole
    numbers plain, fractions rounded to two decimals — the stored event used to carry the raw
    repr (``prompt_tokens 392777.0722222222 → 388136``), which is a float artifact, not a
    reading."""
    v = round(float(v), 2)
    return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)


def _present(counters: dict[str, Any] | None) -> dict[str, float]:
    """The columns that hold a value: a NULL column is an absent series, never a zero."""
    return {k: float(v) for k, v in (counters or {}).items() if isinstance(v, (int, float))}


def reset_reason(
    prev: dict[str, Any] | None,
    cur: dict[str, Any] | None,
    prev_since: float | None,
    cur_since: float | None,
) -> str | None:
    """Why the interval between two counters rows is a reset, or None when it is not.

    A counter present on both sides that went backwards (named, in column order), and/or a
    ``since`` known on both sides that changed. A series present on one side only is a backend
    change, not a reset (the same rule ``metrics_delta`` keeps)."""
    p, c = _present(prev), _present(cur)
    parts = []
    back = [f"{k} {_num(p[k])} → {_num(c[k])}" for k in COUNTER_COLUMNS if k in p and k in c and c[k] < p[k]]
    if back:
        parts.append("counters went backwards: " + ", ".join(back))
    if prev_since is not None and cur_since is not None and prev_since != cur_since:
        parts.append(f"since changed ({prev_since!r} → {cur_since!r})")
    return "; ".join(parts) or None


def rates(
    prev: dict[str, Any] | None,
    cur: dict[str, Any] | None,
    dt_s: float,
    backend: str | None,
    prev_since: float | None,
    cur_since: float | None,
) -> Rates:
    """Per-second rates over ``dt_s`` between two counters rows (``backend`` is a
    ``registry.backend_kind`` value). On a reset every rate is None and ``reset`` is True. With no
    previous row, no interval, or a counter missing from either side, that rate is None."""
    reason = reset_reason(prev, cur, prev_since, cur_since)
    if reason is not None:
        return Rates(None, None, None, None, reset=True, reason=reason)
    delta = metrics_delta(_present(prev), _present(cur), backend)
    if not delta or dt_s <= 0:
        return Rates(None, None, None, None, reset=False)

    def per_s(key: str) -> float | None:
        return delta[key] / dt_s if key in delta else None

    return Rates(
        prompt_tps=per_s("prompt_tokens_processed"),
        gen_tps=per_s("generated_tokens"),
        cached_tps=per_s("prompt_tokens_cached"),
        spec_acceptance=delta.get("spec_decode_acceptance"),
        reset=False,
    )
