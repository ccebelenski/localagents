"""Story 2.2 item 5: the probe cadence. 5 s for an endpoint up within the attention window
(live, or down but recent); 60 s for a dormant one, including a cold start with no history."""
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
