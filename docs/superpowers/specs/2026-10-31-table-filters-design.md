# Table filters by column type (3.12) — design

Operator (2026-10-31, EPICS 3.12): "some of the table filters don't make sense, some are not
useful. Examples: Jobs table, status colume is limited values, should be dropdown picker for
filter. Same with model, endpoint. Started is a time so a text search isn't the perfect way to do
that filter either. elapsed could be a range. turns, tool calls, errors are all values so range
picker would be appropriate with absolute and inclusive endpoints. Filters that don't fit nicely
into the colum size can be pop-out elements, activated on a click on the filter row/column. Do a
design on that before any build since there are multiple aspects to this request and a bad choice
can ruin the page."

Then, on the first draft (pop-outs for range and time, a multi-pick, a redraw hold): "I think the
drop-down is sufficient" (the pick), "Agree" (no time presets), and "Actually I think you've
overdesigned for the tables - I don't think we need a multi-pick for example. For absolute values
like numbers, text search first and then allow x-y range and simple < and > - no need for a
pop-out". This is the second draft: everything stays in the filter row.

Status: APPROVED to build as 3.12a (operator: "no pop out needed for the simple values - dates
maybe still need one? Your call. Maybe go with your new understanding and we can see how that
works. For the pick I don't see a need for any ability to free-text search - the bounded choices
aren't a big list"). The coordinator's call on dates: the typed form first (§6), the pop-out held
as the fallback if typing dates proves awkward on the live page. The pick is the plain select,
no search. The first draft's pop-out, hold and multi-pick are gone (history: 660e3aa, ff2d819).

## 1. What exists (3.10c, folded by 3.11a)

`dataTable(id, columns, rows, opts)` (app.js:735) draws every table: one `<input type="text">`
per column in a filter row under the header; a filter is a case-insensitive substring over the
cell's text plus its titles (`cellText`, cached per row and column); a numeric column also takes
`>n` / `<n` (`filterRows`, app.js:770). A column spec is `{name, numeric, title, cell, sortKey}`;
`sortKey(row)` already returns the typed value the cell shows (the pill's word, the epoch
timestamp, the figure). The view is `TABLE_STATE[id] = {sort, filters: {col: text}, page}`.

## 2. The decision in one paragraph

Each column declares a filter KIND; every control stays in the filter row at the input's size.
`pick` columns get a native `<select>` ("any" + the values present, with counts). `number`
columns keep their text box and read it in order: a range `x-y`, a comparator `<n` / `>n`,
else the substring search that exists today. `time` columns keep their text box with the same
three readings over time literals. `text` columns are unchanged. No pop-out, no multi-select, no
hold; a push redraws as it does today. The state stays `{col: string}` — the box's text IS the
filter — so nothing in the smoke's state reads changes shape.

## 3. The kinds, column by column

Jobs table (both pages): job `text` · status `pick` · started `time` · model `pick` · endpoint
`pick` · elapsed, turns, tool calls, tool errors, context, prefill tok/s, decode tok/s `number` ·
last activity `text`.

Requests table (both pages): time `time` · status `pick` · job `text` · path `pick` · prompt,
new, reused, output, prefill t/s, decode t/s, duration `number` · error `text`.

The kind is a `filter` field on the column spec beside `numeric` (a `FILTER_KIND` map next to
each column map), read by `dataTable`, default `text`. No cell, sort or id changes.

## 4. The pick: a native select

- `<select data-filter="<col>">` in the filter cell, styled as the text box is (the app.css:349
  rule: muted, 13 px, `width: 100%`, `min-width: 3.5em`). Option 0 "any" (value ""), then every
  distinct sort-key value across ALL fetched rows, sorted as the column sorts, labelled
  "value (count)". The count is the number of rows matching the OTHER columns' filters, so
  "done (40)" updates as another filter narrows and a value at 0 stays listed.
- One value. The rule: `String(sortKey(row)) === value`. "any" deletes the key.
- A selected value a later push drops stays selected, listed "(0)", until "any".
- A `change` redraws; the select keeps focus through the redraw by the existing re-focus trick.

## 5. The number box: text first, then range, then comparator

The box's text is read in this order; the first reading that applies wins:

1. `x-y` (two decimals around a hyphen, spaces allowed): an INCLUSIVE range `x ≤ v ≤ y`
   (the operator's "absolute and inclusive endpoints"). `x > y` matches nothing.
2. `<n` or `>n`: strict, as 3.10c's grammar was ("simple < and >").
3. Anything else: the substring search over the cell's text and titles, exactly as today
   (typing "12" still matches an elapsed of "12m 3s").

Units are the sort key's: elapsed in seconds, duration in ms, context in percent, rates in
tokens per second, counts and tokens as themselves. The box's `title` names the unit and the
grammar ("elapsed, seconds — text, x-y, <n, >n"); the placeholder stays the column name. A row
with no finite value never matches a range or a comparator. (`>=`/`<=` are not read; "x-y"
covers inclusive; add them if wanted, two lines.)

## 6. The time box: the same three readings over time literals

1. `a-b`: inclusive from `a`'s start to `b`'s end (each literal's whole span: the end of its
   minute, or of its day).
2. `<a` / `>a`: strict over the literal's whole span — `>a` is after `a`'s end, `<a` before
   `a`'s start; the literal's own minute (or day) is in neither, and the two never share a row.
   (True now, the fold at 3.12a's landing: the first text below said `<a` reached the end of
   its minute, which made `<12:30` and `>12:30` overlap by a minute and `<2026-10-30` and
   `>2026-10-30` by a day; the lane built that sentence faithfully.)
3. Else: substring over the cell's text and its title — the title holds the full local
   date-time (`fmt.date`: the browser's `toLocaleString()`, e.g. "10/30/2026, 2:15:03 PM"), so
   "10/30" and "2:15" already work today and keep working.

A time literal is one of: `HH:MM` (24-hour, the clock on the day the table's `now` falls in),
`YYYY-MM-DD` (a date: the start of that day as a bound; as the `b` of a range or under `<`, the
end of that day), `YYYY-MM-DD HH:MM` (both). Parsed by hand into a local `Date` (never
`Date.parse`). A range's `b` end is inclusive to the end of its minute (`+59 s`) or day. A literal that does
not parse is read as substring (reading 3), so a mistyped bound silently becomes a text search:
the box's title names the three forms, and the pager's count makes a wrong reading visible.
(The hyphen in `YYYY-MM-DD` and the range hyphen are told apart by shape: a range is two
literals around ` - ` with spaces, or two `HH:MM` around a bare hyphen; a date literal alone
is never split.)

## 7. The pager line

"clear filters" (a `pager-btn`) at the end of the pager line whenever any filter is set, also
after "no rows match the filters". A kind's empty value deletes its key: blank text, "any".

## 8. Risks named, each with its answer

| risk | answer |
|---|---|
| the filter row grows taller or wider | the select copies the input's box; nothing else is added to the row |
| a mistyped bound silently becomes a text search | the title names the grammar; the pager's "N of M" shows what matched; no modal, no error state — the operator sees the count |
| a range hyphen inside a date | the shapes in §6; a date literal alone is never split |
| ES5 | no new syntax; `Date` built by components; the gate and `node --check` run in the suite |
| the "no ellipsis" rule | the select's options are the browser's own list; the row keeps its width |

## 9. What does not change

The cells, the sort, the paging, the ids, the renderers' contracts, the row order, the endpoint
route's cap, the `cellText` cache, the state shape (`filters` stays `{col: string}`).

## 10. Tests and the smoke

- `tests/test_3_10c.py::test_the_filter_grammar_substring_and_the_numeric_comparators` is
  extended, not replaced (the `>`/`<` grammar stays; `x-y` joins it);
  `::test_the_filter_row_is_inputs_one_per_column_built_from_the_state` becomes "one control
  per column": an input or a select.
- New tests in `tests/test_3_12.py` under node with `tests/fakedom.js` (3.11a's loader pattern):
  the kinds per column; the select's options, counts and the "(0)" survivor; the three readings in
  order for number and time; inclusive ends; strict comparators; blanks; the time literals and the
  end-of-day/minute rule; "clear filters".
- `tools/smoke/tables.js` step (c): the status select set to "done" → 40 rows, option label
  "done (40)"; turns ">100" unchanged (7 rows); turns "101-119" → the same 7; started "HH:MM-HH:MM"
  inside the hand-made day → the rows in that window; the uuid-in-job text witness unchanged.

## 11. Build plan

One story (3.12a), one lane: Qwen/ai1 at 200 turns, worktree; files: app.js (the `dataTable`
region, the two column maps), app.css (the select rule), `tools/smoke/tables.js`,
`tests/test_3_12.py`, the two 3.10c tests. Then a construct-it review by a different model (the
reading order, the inclusive ends, the date-hyphen shapes, the faceted counts), the fold by the
reviewer, a third-model review of the fold. The operator reads the live page before the story is
ticked.
