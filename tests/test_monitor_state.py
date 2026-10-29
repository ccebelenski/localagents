"""Story 2.2 item 5: the probe cadence. 5 s for an endpoint up within the attention window
(live, or down but recent); 60 s for a dormant one, including a cold start with no history.
Story 2.3 item 1: the endpoint display state (spec §4), every truth-table row its own test."""
import sqlite3

from localagents.monitor import state

NOW = 1793288065.0
DAY = 24 * 3600


def test_up_just_now_is_fast():
    assert state.interval_s(NOW, NOW) == 5


def test_up_exactly_a_window_ago_is_still_fast():
    assert state.interval_s(NOW - DAY, NOW) == 5


def test_up_just_over_a_window_ago_is_dormant():
    assert state.interval_s(NOW - DAY - 0.001, NOW) == 60


def test_never_up_is_dormant():
    assert state.interval_s(None, NOW) == 60


def test_the_window_is_a_parameter():
    assert state.interval_s(NOW - 3601, NOW, window_s=3600) == 60
    assert state.interval_s(NOW - 3599, NOW, window_s=3600) == 5


def test_the_intervals_are_ints():
    assert type(state.interval_s(NOW, NOW)) is int and type(state.interval_s(None, NOW)) is int


# --- Story 2.3 item 1: the endpoint display state (spec §4) ---


def _up_probe():
    """A successful probe as the probes table stores it: ``up`` is 0/1, ``host`` text."""
    return {"up": 1, "host": "up"}


def _down_probe():
    return {"up": 0, "host": "down"}


def test_live_when_the_last_probe_is_up():
    """live: the last probe is up."""
    assert state.endpoint_state(_up_probe(), NOW - 10, NOW) == "live"


def test_down_at_the_window_boundary():
    """down, now - last_up_t == window_s (boundary: down, not dormant, as interval_s's)."""
    assert state.endpoint_state(_down_probe(), NOW - DAY, NOW) == "down"


def test_dormant_just_over_the_window():
    """dormant, now - last_up_t == window_s + 1."""
    assert state.endpoint_state(_down_probe(), NOW - DAY - 1, NOW) == "dormant"


def test_no_probe_at_all_is_dormant():
    """dormant: no probe at all — a cold start with no history, never alarmed."""
    assert state.endpoint_state(None, None, NOW) == "dormant"


def test_an_up_probe_beats_stale_history():
    """live: the probe is up but last_up_t is older than the window — impossible, but the
    function must trust the probe row."""
    assert state.endpoint_state(_up_probe(), NOW - 10 * DAY, NOW) == "live"


def test_endpoint_state_window_is_a_parameter():
    """window_s as 1 h: the boundary still counts, the second just over it does not."""
    assert state.endpoint_state(_down_probe(), NOW - 3600, NOW, window_s=3600) == "down"
    assert state.endpoint_state(_down_probe(), NOW - 3601, NOW, window_s=3600) == "dormant"


def test_a_sqlite3_row_is_accepted_as_the_last_probe():
    """The last probe comes off the probes table as a sqlite3.Row, not a dict: both work."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT 1 AS up, 'up' AS host").fetchone()
    assert state.endpoint_state(row, NOW - 10, NOW) == "live"
