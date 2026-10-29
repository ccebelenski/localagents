"""Endpoint state derived from probe rows (monitor spec §4). Story 2.2 holds the probe cadence;
story 2.3 adds the display-state and node-state functions beside it."""

from __future__ import annotations

import sqlite3

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


def endpoint_state(
    last_probe: sqlite3.Row | dict | None,
    last_up_t: float | None,
    now: float,
    window_s: float = ATTENTION_WINDOW_S,
) -> str:
    """The endpoint's display state (spec §4) from its last probe row and the time of its
    newest up probe — derived from rows, so a restart reconstructs it from the database:

    ``"live"`` — the last probe is up *and* within the window (measured from its ``t``,
    boundary included, as ``interval_s``): the display principle is "not seen within the
    window", so a probe older than the window is not seen (ruling 2026-10-29). An up row
    with a stale window happens whenever collection is silent past the window — the
    monitor stopped, or the machine slept, while the endpoint kept answering — and shows
    dormant until the first tick, not full content off a day-old row.
    ``"down"`` — the last probe is not up, but it was seen up within the attention window
    (the boundary included, as ``interval_s``): it dropped recently, alert colour.
    ``"dormant"`` — not seen up within the window, including no history at all: a cold
    start with no probes is dormant, never alarmed (spec §4)."""
    if last_probe is not None and last_probe["up"] and now - last_probe["t"] <= window_s:
        return "live"
    if last_up_t is not None and now - last_up_t <= window_s:
        return "down"
    return "dormant"


def node_state(endpoints: list[dict], fallback: str | None = None) -> str:
    """The state of a node from the LAST probe of each of its endpoints (spec §4 "Per node"):
    each entry is ``{"up": bool, "host": "up"|"down"|"unknown"|None}``.

    ``"up"`` — any endpoint is up, or any answers ``host == "up"``: a refusal already proves
    the box is up (then the unreachable endpoint is "endpoint down", shown on that line only).
    ``"down"`` — every endpoint reports the box unreachable.
    ``"unknown"`` — every endpoint timed out (``host == "unknown"`), resolved by the port-22
    fallback when ``fallback`` is given: ``"answered"`` or ``"refused"`` -> up (the box
    answers), ``"nothing"`` -> down. Also unknown when the endpoints are a mix of ``"down"``
    and ``"unknown"`` with no ``"up"``: one timed out (box off, or a firewall dropping
    packets) and another is unreachable — nothing proves the box is down, and an unproven
    down is an alert, so the ambiguous mix stays unknown rather than alarming. A host with no
    endpoints is unknown too."""
    if not endpoints:
        return "unknown"
    if any(e.get("up") for e in endpoints) or any(e.get("host") == "up" for e in endpoints):
        return "up"
    if all(e.get("host") == "down" for e in endpoints):
        return "down"
    if all(e.get("host") == "unknown" for e in endpoints):
        if fallback in ("answered", "refused"):
            return "up"
        if fallback == "nothing":
            return "down"
    return "unknown"


def node_dormant(endpoint_states: list[str]) -> bool:
    """True when every endpoint is dormant: the node collapses to one muted line (spec §4:
    the attention window applies to nodes too). An empty list is vacuously dormant."""
    return all(s == "dormant" for s in endpoint_states)
