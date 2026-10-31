"""Fold of the page-batch review (2026-10-30, DeepSeek-V4.1-Flash): F1 and F2, the two 3.10c
helper rules the story's pins could not see.

F1 (app.js ``cellText``): the filter text is the cell's text PLUS its title whenever the title
adds characters — the full UUID behind the short-id cell and the full date-time behind the
clock-time cell both match a pasted needle. The old guard (``title.indexOf(own) < 0``) dropped
the title exactly when it was a superset, so a pasted UUID and today's date matched nothing.

F2 (app.js ``sortRows``): a missing value is absent, not small — it sorts past every present
value in BOTH directions, and a text column's null/blank is missing too, so ``last activity``
blanks sit at the end whether the arrow points up or down. The old sentinel multiplied by
``dir`` and sent text blanks through ``localeCompare``.

Ruling (o): regex/substring pins over the page source, like the neighbours in
tests/test_3_10c.py. The runtime witness is tools/smoke/tables.js's ``f``: a filter on the
started cell's date title and on a full UUID, then ``prefill tok/s`` descending and ``last
activity`` ascending with the blanks counted on page 1 and at the end.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "localagents" / "monitor" / "static"


def _app_js():
    return (STATIC / "app.js").read_text()


def _nested_function(js, name):
    """A function nested inside ``dataTable`` (four-space indented), to its closing brace."""
    m = re.search(r"\n    function " + re.escape(name) + r"\([^\n]*\n.*?\n    \}\n", js, re.S)
    assert m is not None, name
    return m.group(0)


# --- F1: the filter text includes a title that adds characters ----------------------------------


def test_f1_cell_text_appends_the_title_that_extends_the_cell():
    """Both halves of the P1 probe's case. (1) A superset title is exactly the case the
    comment names (the started cell's full date-time behind the clock); the old
    ``t.indexOf(own) < 0`` guard dropped it. A title equal to the cell adds nothing, so ``t ?``
    alone is the rule. (2) The actionable title is often on a CHILD, not the td — jobLink's
    anchor carries the full UUID behind the short id, the requests pill's span the numeric
    code behind "ok" — so the cell's titles are its own and its descendants'."""
    body = _nested_function(_app_js(), "cellText")
    # 3.11a row 38: the same expression, now stored lower-cased in the per-table cache
    # (the cache's behaviour, title matches included: tests/test_3_11a.py)
    assert '(t ? own + " " + t : own)' in body, body
    assert "indexOf(own)" not in body, body
    assert 'td.querySelectorAll("[title]")' in body, body
    assert 'titles.join(" ")' in body, body


# --- F2: a missing value sorts last in both directions -----------------------------------------


def test_f2_missing_sorts_last_in_both_directions():
    """The sentinel branch carries no direction: a missing value is absent, not small, so it
    sorts past every present value whether the arrow points up or down (the old
    ``(xn ? -1 : 1) * dir`` flipped it). A text column's null/blank is missing under the same
    branch — ``last activity``'s blanks are ``""`` — so the present test must admit a
    non-empty text, not only a number."""
    body = _nested_function(_app_js(), "sortRows")
    sentinel = [ln.strip() for ln in body.splitlines() if "xn !== yn" in ln]
    assert len(sentinel) == 1, sentinel
    assert "* dir" not in sentinel[0], sentinel[0]
    assert "return xn ? -1 : 1;" in sentinel[0], sentinel[0]
    # the present test treats a text column's null/blank as missing too
    assert '(x != null && x !== "")' in body, body
    assert '(y != null && y !== "")' in body, body
