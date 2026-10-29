"""Story 3.1 item 1: the monitor's time-range grammar (spec §5: "A time-range control (15 min
to 7 days) applies to every chart on the page"; §7: "time-range parsing with a bad range
rejected"). ``<n>m``, ``<n>h`` or ``<n>d``, a whole number, 15 min to 7 d inclusive."""
import pytest

from localagents.monitor import ranges


@pytest.mark.parametrize("text, seconds", [
    ("15m", 900.0), ("1h", 3600.0), ("6h", 21600.0), ("24h", 86400.0), ("7d", 604800.0),
])
def test_every_offered_range_parses(text, seconds):
    assert text in ranges.RANGES
    assert ranges.parse_range(text) == seconds


def test_ranges_is_exactly_the_offered_list_in_order():
    assert ranges.RANGES == ["15m", "1h", "6h", "24h", "7d"]
    assert [ranges.parse_range(r) for r in ranges.RANGES] == sorted(ranges.parse_range(r) for r in ranges.RANGES)


@pytest.mark.parametrize("text, seconds", [
    ("30m", 1800.0), ("90m", 5400.0), ("2d", 172800.0), ("168h", 604800.0), ("10080m", 604800.0),
])
def test_other_values_inside_the_bounds_parse(text, seconds):
    assert ranges.parse_range(text) == seconds


@pytest.mark.parametrize("text", ["14m", "8d", "169h", "0m"])
def test_out_of_bounds_is_rejected_naming_the_text(text):
    with pytest.raises(ValueError) as e:
        ranges.parse_range(text)
    assert repr(text) in str(e.value)


@pytest.mark.parametrize("text", ["1w", "", "1h ", " 1h", "1", "h", "1.5h", "-1h", "1H", "1hh", "1 h", "1s"])
def test_malformed_is_rejected_naming_the_text(text):
    with pytest.raises(ValueError) as e:
        ranges.parse_range(text)
    assert repr(text) in str(e.value)


def test_a_non_string_is_rejected():
    with pytest.raises(ValueError):
        ranges.parse_range(None)  # type: ignore[arg-type]
