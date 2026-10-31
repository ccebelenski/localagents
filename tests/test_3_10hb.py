"""Story 3.10h(b): the request table's prompt columns tell the truth (the operator, EPICS 3.10h,
with a screenshot of a job page's request table: "Weird - prompt = 1? I hope that's due to
transient state?"). Not transient: the old "prompt" cell read `tokens.input` — the API's
`input_tokens`, only the uncached, un-created tail of the prompt, which is 1 on every row when
the prefix cache is hot (Strata and llama.cpp re-evaluate the last token). The whole prompt is
input + cache_read + cache_creation.

Design (A): the columns are path, prompt, new, reused, output, prefill t/s, decode t/s,
duration, error — "prompt" = the whole prompt (blank when none of the three parts is a number;
a missing one counts as 0 when at least one is present), "new" = input + cache_creation (what
the engine processed — the numerator of prefill t/s; blank when neither is a number), "reused"
= cache_read as before. The prompt cell's title names the three parts; the two new-ish headers
carry titles. (B): the requests-section totals ("tokens in", "out", "cache read", "cache
creation") already name the parts — untouched unless one says "prompt".

Pins are regex/substring over app.js, like the neighbours' (ruling (o)); the arithmetic lives in
two small named helpers (`promptTotal`, `newTokens`) so the pins are exact."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "localagents" / "monitor" / "static"


def _app_js():
    return (STATIC / "app.js").read_text()


def test_the_request_columns_are_the_design_list_in_order():
    """"new" sits between "prompt" and "reused"; nothing else moves."""
    js = _app_js()
    order = re.search(r'columns\.concat\(\[(.*?)\]\);', js, re.S)
    assert order is not None
    assert re.findall(r'"([^"]+)"', order.group(1)) == [
        "path", "prompt", "new", "reused", "output", "prefill t/s", "decode t/s", "duration", "error"]


def test_req_numeric_marks_new():
    js = _app_js()
    numeric = re.search(r"var REQ_NUMERIC = \{([^}]*)\};", js)
    assert numeric is not None
    assert re.search(r'"?new"?[:\s]', numeric.group(1)), numeric.group(1)


def test_prompt_total_sums_the_three_parts_in_a_named_helper():
    """The whole prompt: input + cache_read + cache_creation, the reads spelled
    `req.tokens.<field>` for the §5a walk; a missing part counts as 0 when at least one part
    is a number, and none-a-number answers null (the blank cell)."""
    js = _app_js()
    body = re.search(r"function promptTotal\(req\) \{(.*?)\n  \}", js, re.S)
    assert body is not None
    text = body.group(1)
    assert "return (req.tokens.input || 0) + (req.tokens.cache_read || 0) " \
           "+ (req.tokens.cache_creation || 0);" in text
    # the at-least-one-is-a-number rule (all missing -> null, not 0)
    assert 'typeof req.tokens.input !== "number" && typeof req.tokens.cache_read !== "number"' in text
    assert 'typeof req.tokens.cache_creation !== "number"' in text


def test_new_tokens_is_the_prefill_numerator_in_a_named_helper():
    """input + cache_creation — the figure prefillRate already divides by the time to first
    token; blank when neither is a number."""
    js = _app_js()
    body = re.search(r"function newTokens\(req\) \{(.*?)\n  \}", js, re.S)
    assert body is not None
    assert "return (req.tokens.input || 0) + (req.tokens.cache_creation || 0);" in body.group(1)
    assert 'typeof req.tokens.input !== "number" && typeof req.tokens.cache_creation !== "number"' \
        in body.group(1)


def test_the_cells_draw_the_helpers_and_the_prompt_title_names_the_three_parts():
    js = _app_js()
    assert "var prompt = promptTotal(req);" in js
    assert "var newTok = newTokens(req);" in js
    assert 'h("td", { class: "n", title: promptTitle || null, text: fmt.int(prompt) })' in js
    assert 'h("td", { class: "n", text: fmt.int(newTok) })' in js
    # the title sentence: "input 1, cache read 16,384, cache creation 3,012"; a missing part
    # drops its half, exactly as the reused cell's title already does
    assert 'promptParts.push("input " + fmt.int(tokens.input))' in js
    assert 'promptParts.push("cache read " + fmt.int(cacheRead))' in js
    assert 'promptParts.push("cache creation " + fmt.int(cacheCreation))' in js
    assert 'var promptTitle = promptParts.join(", ");' in js


def test_the_two_headers_carry_explaining_titles():
    js = _app_js()
    assert 'prompt: "the whole prompt: input + reused + cache creation"' in js
    assert 'new: "tokens the engine processed: input + cache creation"' in js
    # the header build puts the map's title on the th (null stays no attribute)
    assert "title: REQ_TITLES[c] || null" in js
