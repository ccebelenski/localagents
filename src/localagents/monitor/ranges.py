"""The time-range grammar of the monitor's history routes (monitor spec §5: "A time-range control
(15 min to 7 days) applies to every chart on the page"; §7: a bad range is rejected).

A range is a whole number followed by one unit letter: ``m`` minutes, ``h`` hours, ``d`` days —
``15m``, ``90m``, ``6h``, ``2d``. Nothing else: no spaces, no fractions, no sign, no other
units (``1w``, ``30s``), no upper case. The value must be from 15 minutes to 7 days, both
included; the upper bound is the probe retention (spec §3: seven days of probes), so a wider
range could only show what retention already deleted.
"""

from __future__ import annotations

import re

# the ranges the page offers, narrowest first (story 3.2's range control)
RANGES = ["15m", "1h", "6h", "24h", "7d"]
DEFAULT_RANGE = "1h"

MIN_S = 15 * 60.0
MAX_S = 7 * 86400.0

_UNIT_S = {"m": 60.0, "h": 3600.0, "d": 86400.0}
_GRAMMAR = re.compile(r"([0-9]{1,7})([mhd])")


def parse_range(text: str) -> float:
    """``text`` as seconds; a ``ValueError`` naming ``text`` when it is malformed or outside
    15 min .. 7 d."""
    m = _GRAMMAR.fullmatch(text) if isinstance(text, str) else None
    if m is None:
        raise ValueError(f"bad range {text!r}: expected <n>m, <n>h or <n>d, e.g. {', '.join(RANGES)}")
    seconds = int(m.group(1)) * _UNIT_S[m.group(2)]
    if not MIN_S <= seconds <= MAX_S:
        raise ValueError(f"bad range {text!r}: must be from 15m to 7d")
    return seconds
