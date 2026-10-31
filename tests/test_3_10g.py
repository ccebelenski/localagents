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


NARROW = re.compile(r"@media\s*\(max-width:\s*760px\)")


def _css_blocks(css):
    """(top-level rules, [(media prelude, rules)]), comments removed. A brace-balanced walk,
    so a rule inside an ``@media`` block is never returned as a top-level rule — the flattened
    regex the old copy used could not tell them apart, which is exactly the escape hatch F4
    found (a later top-level ``.host { max-width: none; }`` read as just another rule)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    top, media = [], []
    i, n = 0, len(css)
    while i < n:
        j = css.find("{", i)
        if j < 0:
            break
        sel = css[i:j].strip()
        depth, k = 1, j + 1
        while k < n and depth:
            if css[k] == "{":
                depth += 1
            elif css[k] == "}":
                depth -= 1
            k += 1
        body = css[j + 1:k - 1]
        if sel.startswith("@media"):
            media.append((sel, [(m.group(1).strip(), m.group(2))
                                for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", body)]))
        else:
            top.append((sel, body))
        i = k
    return top, media


def test_a_card_never_grows_past_one_and_a_half_floors():
    """3.10g: the cap is the card's own rule, the exact text — not the track's. (The track form,
    ``minmax(min(var(--card-floor), 100%), calc(var(--card-floor) * 1.5))`` + justify-content,
    would replace the ``1fr`` the 3.7u pin in test_monitor_page.py holds; and a max on the card
    is the rule as the operator said it: a card never stretches past ~1.5 floors, whatever the
    column arithmetic hands it.) EVERY top-level ``.host`` rule carries the cap and none
    releases it: the old pin took only the first rule found, so a later
    ``.host { max-width: none; }`` outside the narrow query passed while the cards stretched."""
    top, _ = _css_blocks((STATIC / "app.css").read_text())
    hosts = [body for sel, body in top if sel == ".host"]
    assert hosts, "a top-level .host rule"
    for body in hosts:
        assert "max-width: calc(var(--card-floor) * 1.5)" in body, body
        assert "max-width: none" not in body, body


def test_the_narrow_query_still_draws_one_full_width_column():
    """3.10g at phone width: the narrow query keeps its single ``1fr`` column, and the card's
    cap is released there — a phone's card is the window, there is nothing to spread (a short
    floor under the 1.5x cap would otherwise shrink the only card off the window's edge). The
    release sits in that block ALONE: no ``.host`` rule at the top level or in any other media
    block may carry ``max-width: none`` (F4)."""
    top, media = _css_blocks((STATIC / "app.css").read_text())
    narrow = next(rules for prelude, rules in media if NARROW.match(prelude))
    hosts = next(body for sel, body in narrow if sel == ".hosts")
    assert "grid-template-columns: 1fr" in hosts
    host = next(body for sel, body in narrow if sel == ".host")
    assert "max-width: none" in host
    outside = [body for sel, body in top if sel == ".host"]
    for prelude, rules in media:
        if not NARROW.match(prelude):
            outside += [body for sel, body in rules if sel == ".host"]
    assert all("max-width: none" not in body for body in outside), outside
