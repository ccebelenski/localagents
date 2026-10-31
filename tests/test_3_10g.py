"""Story 3.10g (the decided half): a lone column never stretches a card across the window.

The operator (2026-10-30, a 1712-px window): a window just too narrow for two columns spreads
every card to the full ~1680 px — the twelve fixed tiles flung apart, the state strip a
full-width bar. The fix: a card never grows past one and a half floors
(``max-width: calc(var(--card-floor) * 1.5)`` on ``.host``), so the lone column packs left and
leaves the space instead of spreading the fixed tiles; the floor itself (3.7u) is unchanged.
The other half — whether the job lines leave the floor — the operator has not decided, and
``app.js`` is not this story.

No JavaScript runtime in the suite: these are the static pins the page tests' convention
allows (test_monitor_page.py's docstring); the widths themselves are the browser smoke's
(t/REPORT.md, the floor sequence 2200,1712,1400,1000 plus 760)."""
import re
from pathlib import Path

from localagents.monitor import app as app_mod

STATIC = Path(app_mod.__file__).resolve().parent / "static"


def _css_rules(css):
    """(selector, declarations) for every rule, comments removed; a rule inside an @media
    block is returned on its own (the block's prelude is not a selector). The copy from
    test_monitor_page.py, kept local so this story's file stands alone (ruling (o))."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def test_a_card_never_grows_past_one_and_a_half_floors():
    """3.10g: the cap is the card's own rule, the exact text — not the track's. (The track form,
    ``minmax(min(var(--card-floor), 100%), calc(var(--card-floor) * 1.5))`` + justify-content,
    would replace the ``1fr`` the 3.7u pin in test_monitor_page.py holds; and a max on the card
    is the rule as the operator said it: a card never stretches past ~1.5 floors, whatever the
    column arithmetic hands it.)"""
    rules = _css_rules((STATIC / "app.css").read_text())
    host = next(body for sel, body in rules if sel == ".host")
    assert "max-width: calc(var(--card-floor) * 1.5)" in host


def test_the_narrow_query_still_draws_one_full_width_column():
    """3.10g at phone width: the narrow query keeps its single ``1fr`` column, and the card's
    cap is released there — a phone's card is the window, there is nothing to spread (a short
    floor under the 1.5x cap would otherwise shrink the only card off the window's edge)."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "app.css").read_text(), flags=re.S)
    block = re.search(r"@media \(max-width: 760px\) \{(.*?)\n\}", css, re.S)
    assert block is not None, "the narrow query block"
    rules = _css_rules(block.group(1))
    hosts = next(body for sel, body in rules if sel == ".hosts")
    assert "grid-template-columns: 1fr" in hosts
    host = next(body for sel, body in rules if sel == ".host")
    assert "max-width: none" in host
