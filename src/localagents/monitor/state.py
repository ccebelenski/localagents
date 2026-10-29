"""Endpoint state derived from probe rows (monitor spec §4). Story 2.2 holds the probe cadence;
story 2.3 adds the display-state and node-state functions beside it."""

from __future__ import annotations

ATTENTION_WINDOW_S = 24 * 3600  # spec §4: the attention window's default
FAST_S = 5  # spec §3: probe every 5 s ...
DORMANT_S = 60  # ... and a dormant endpoint every 60 s


def interval_s(last_up_t: float | None, now: float, window_s: float = ATTENTION_WINDOW_S) -> int:
    """Seconds until the next probe of an endpoint last seen up at ``last_up_t``.

    Up within the window (the boundary included: live, or down but recent) is 5 s; otherwise
    dormant, 60 s. Never up (a cold start with no history) is dormant (spec §4)."""
    if last_up_t is not None and now - last_up_t <= window_s:
        return FAST_S
    return DORMANT_S
