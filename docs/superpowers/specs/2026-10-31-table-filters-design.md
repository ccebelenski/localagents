# Table filters by column type (3.12) — design, for the operator's approval before any build

Operator (2026-10-31, EPICS 3.12): "some of the table filters don't make sense, some are not
useful. Examples: Jobs table, status colume is limited values, should be dropdown picker for
filter. Same with model, endpoint. Started is a time so a text search isn't the perfect way to do
that filter either. elapsed could be a range. turns, tool calls, errors are all values so range
picker would be appropriate with absolute and inclusive endpoints. Filters that don't fit nicely
into the colum size can be pop-out elements, activated on a click on the filter row/column. Do a
design on that before any build since there are multiple aspects to this request and a bad choice
can ruin the page."

Status: APPROVED with amendments (operator 2026-10-31 morning, §14 holds the words): the pick is a
native dropdown, no presets, the live redraw is HELD while a pop-out is open, the typed `>n` grammar
stays as a shortcut inside the range pop-out, no "blank only" option. The build story is 3.12a.

## 1. What exists (3.10c, folded by 3.11a)

`dataTable(id, columns, rows, opts)` in `src/localagents/monitor/static/app.js` draws every table
on the page: the overview's jobs table (id `jobs`), the endpoint page's (`jobs-endpoint`), and the
two requests tables (`requests-job`, `requests-endpoint`). The view lives in the module-level
`TABLE_STATE[id] = {sort, filters, page}` so a push redraws into the same view. Under the header
row sits one filter row: one `<input type="text">` per column (`draw()`, the `table-filters` row).
A filter is a case-insensitive substring over the cell's text plus every `title` the cell carries
(`cellText`, app.js:754, cached per row and column since 3.11a); a numeric column also takes `>n`
/ `<n` (`filterRows`, app.js:770). A column spec is `{name, numeric, title, cell(row), sortKey(row)}`;
`sortKey` already returns the typed value the cell shows (the timestamp for started/time, the
pill's word for status, a number for every figure).

Measured: a keystroke redraws in 7–9 ms at 1,000 rows (3.11a). The rows are at most 1,000
(the requests route's cap) and in practice tens to hundreds.

## 2. The decision in one paragraph

Each column declares a filter KIND. Four kinds: `text` (what exists), `pick` (one value chosen
from a dropdown of the values present, with counts), `range` (two inclusive numeric bounds),
`time` (two inclusive instants). The filter row keeps one cell per column: a `text` column keeps
its inline box; a `pick` column holds a native `<select>` inline; `range` and `time` show a small
button that reads the current filter in a few characters (or the column name, muted, when none)
and opens ONE pop-out under the cell. The pop-out applies live and closes on Escape, on a click
outside, or on its own close control. While a pop-out is open the table's live redraw is HELD:
a push keeps the newest rows aside and the table redraws from them when the pop-out closes.
Filters AND across columns. The pager line gains "clear filters".

## 3. The kinds, column by column

Jobs table (both pages; `JOB_COLUMNS`, app.js:617):

| column | kind | unit / values | why |
|---|---|---|---|
| job | text | the short id, the full uuid in the title | a pasted uuid or a prefix is a text match (3.10c's decision stands) |
| status | pick | the pill's words: queued, running, done, failed, cancelled, timeout, lost | closed vocabulary; the operator's example |
| started | time | local time | the operator's example |
| model | pick | the served ids present in the rows | the operator's example |
| endpoint | pick | the endpoint names present (omitted on the endpoint page already) | the operator's example |
| elapsed | range | seconds (the cell prints `fmt.dur`; the box takes seconds) | the operator's example |
| turns, tool calls, tool errors | range | counts | the operator's example |
| context | range | percent | a figure |
| prefill tok/s, decode tok/s | range | tokens per second | figures |
| last activity | text | | free text |

Requests table (both pages):

| column | kind | unit / values |
|---|---|---|
| time | time | local time |
| status | pick | ok, error, handshake, – |
| job | text | the short id / uuid |
| path | pick | the paths present (a handful: /v1/messages, /v1/messages/count_tokens, /api/hello…) |
| prompt, new, reused, output | range | tokens |
| prefill t/s, decode t/s | range | tokens per second |
| duration | range | milliseconds (the cell prints ms under a second and seconds above; the box takes ms, the pop-out says so) |
| error | text | |

The kind is a field on the column spec (`filter: "pick"`), set beside `numeric` in the two
column maps; `dataTable` reads it and defaults to `text`. No column changes its cell or its sort.

## 4. The pick (operator: "I think the drop-down is sufficient")

- A native `<select>` in the filter cell, styled as the text box is (muted, 13 px, the column's
  width, `min-width: 3.5em`). Its first option is "any" (no filter); then every distinct value of
  the column across ALL fetched rows (the sort key's value: the word the pill prints, the cell's
  text), sorted as the column sorts, each option labelled "value (count)". The count is the number
  of rows that match the OTHER columns' filters (faceted: "done (40)" under a turns range), so a
  value whose count is 0 stays listed rather than vanishing when another filter drops its rows.
- One value at a time. (A pick of two — "failed or timeout" — is two looks; the operator weighed
  this against the pop-out's checkbox list and chose the dropdown.)
- A value that disappears from a later push (a model no longer in the rows) stays selected and
  listed with "(0)" until the operator picks "any", so the choice never silently evaporates.
- A `change` on the select updates the state and redraws, as a keystroke in a text box does; the
  select keeps focus through the redraw by the same `data-filter` re-focus.

## 5. The range

- Two boxes, "from" and "to", `type="number"`, both optional. Blank = unbounded. Both endpoints
  INCLUSIVE (the operator's words: "absolute and inclusive endpoints"): `from ≤ v ≤ to`.
- The pop-out names the unit beside the heading ("elapsed, seconds"; "duration, ms";
  "context, %"). The boxes take the sort key's unit, which is the figure the column sorts by.
- A row with no finite value (blank cell) never matches a bounded range; with both boxes blank
  the filter is off and blanks show. (The sort already treats a blank as absent, not small.)
- `from > to` matches nothing, and the pop-out says "from is past to" in muted text rather than
  swapping the operator's numbers.
- The button reads "≥ 100", "≤ 5", "10–50" (`fmt.int` on both ends), the column name muted
  when none.
- The 3.10c `>n` / `<n` typed grammar moves INSIDE the pop-out as a shortcut (operator: "We can try
  it and see"): a third box under the two, placeholder "or type >n, <n, n-m", whose `input` fills
  the from/to boxes. `n-m` sets both (inclusive); `>=n` / `<=n` set one side inclusive; `>n` / `<n`
  set one side OPEN (exclusive) — the only way an endpoint is exclusive, since the boxes are always
  inclusive. The state records the open side (`fromOpen`, `toOpen`, §8) and the button reads
  "> 100" against "≥ 100". Editing a from/to box clears that side's open flag.

## 6. The time

- Two `type="datetime-local"` boxes, "from" and "to", both optional, local time (the page's
  clock everywhere else is local: `fmt.time`, `fmt.date`). Firefox and Chromium both ship a native
  picker for this input; no library.
- Inclusive at the operator's resolution: a datetime-local value is to the minute, so "from
  12:30" means `t ≥ 12:30:00` and "to 12:30" means `t ≤ 12:30:59`. The pop-out says "to the end
  of the minute" under the "to" box.
- A value is parsed by hand from `YYYY-MM-DDTHH:MM` into a local `Date` (ES5: `new Date(y,
  m-1, d, h, mi)`), never through `Date.parse`.
- The button reads the way the time cells read (the `timeTd` rule, app.js:639: clock time inside
  24 h, date past it): "from 12:30", "to Oct 30", "12:30–14:00".
- No presets ("last hour", "today") in this story (operator: "Agree").
- On the endpoint page the fetched range already bounds the rows (`#range`); the filter narrows
  inside it. The overview's jobs table holds live jobs plus the last ten finished, so the time
  filter is weakest there; one rule for every table is simpler than an exception.

## 7. The pop-out

- One per table, at most one open across the page. Opening another closes the first.
- A `<div class="filter-pop" role="dialog" aria-label="<column> filter">` with: a heading (the
  column name and, for a range, the unit), the kind's controls, a "clear" link (empties this
  column's filter), and a "close" control. Applies live on `input`/`change`; no apply button —
  the redraw is 7–9 ms.
- Placement: a child of the `.table-block` (which becomes `position: relative`), absolutely
  positioned under the filter cell by the cell's offset. It is NOT inside `.table-wrap`, whose
  `overflow-x: auto` would clip it. It is clamped to the block's right edge so it never overflows
  the page (the 3.10g lesson: a window "not quite wide enough" must not degrade). A horizontal
  scroll of the wrap closes it (the anchor moved).
- Width: a fixed `min-width: 14em`, grows with its content; never an ellipsis (3.7u's rule). A
  pick list longer than ~12 entries scrolls inside the pop-out (`max-height`, `overflow-y: auto`);
  a model list of 30 served ids is the realistic worst case.
- Keyboard: the button is a real `<button>` (Enter/Space opens); focus moves to the first control
  on open and back to the button on close; Escape closes; Tab stays inside the pop-out's controls
  in DOM order (no focus trap code: the pop-out is the last child of the block, so Tab past its end
  leaves the table, which is acceptable).
- A push (the overview rebuilds `#jobs-body` every snapshot; the detail pages on their fetch)
  calls `jobsTable`/`requestsTable` again, which call `dataTable` again. The HOLD (operator: "if
  too often the picker is impossible to use. I would hold to be safe"): `TABLE_STATE[id].open`
  names the open pop-out's column; while it is set, a `dataTable` call for that id does NOT build
  a new table — it keeps the newest `rows` in `st.held`, returns the container it already owns
  (a module-level `TABLE_DOM[id]`, the one element the callers re-append), and the DOM, the
  pop-out and any native picker stay exactly as they are. Closing the pop-out clears `open`,
  takes `st.held` as the rows and draws. The pager line reads "updates held while the filter is
  open" in muted text while the hold is on, so a stale table is never a surprise (a running job's
  elapsed stops moving for as long as the pop-out is open). The hold is the helper's, so the
  detail pages get it without a change; the cards above the overview's table keep updating.
- Narrow screens (the 760 px query): the pop-out keeps its placement; the filter row already
  scrolls with the table. Nothing special in this story.

## 8. The state

```
TABLE_STATE[id] = {
  sort: null | {col, dir},
  filters: { <col>: <value> },     // by kind:
                                   //   text:  "substring"
                                   //   pick:  ["done", "failed"]        (non-empty array)
                                   //   range: {from: 10, to: 50}        (either side null)
                                   //   time:  {from: 1761900000, to: null}   (epoch seconds, inclusive)
  page: n,
  open: null | "<col>",            // the pop-out, if one is open (the hold is on)
  held: null | rows                // the newest rows a push brought while the hold was on
}
```

A range value may carry `fromOpen: true` / `toOpen: true` when the typed shortcut set an
exclusive end (§5); absent means inclusive. `TABLE_DOM[id]` holds the container a held table
returns; it is not state and the smoke does not read it.

A filter whose value is "empty" (blank text, empty array, both sides null) is deleted from
`filters`, so "any filter active?" stays `Object.keys(st.filters).length > 0` and the smoke's
state reads stay simple. The `filters` object is still exported through `TABLE_STATE` for the
smoke.

## 9. The pager line and clearing

- "no rows match the filters" gains " · clear filters" (a `pager-btn`) when any filter is set,
  and the normal "1–50 of 889" line gains the same link at its end under the same condition.
- Each pop-out has its own "clear" for its column.

## 10. Risks named, each with its answer

| risk ("a bad choice can ruin the page") | answer |
|---|---|
| the filter row grows taller or wider | the button is styled to the input's exact height and font; the row's cells keep `min-width: 3.5em`; a button's text is short by construction (§4, §5, §6) |
| the pop-out is clipped or pushes the layout | positioned, outside `.table-wrap`, clamped to the block's right edge; it overlays, never reflows |
| the pop-out dies on a push mid-edit | the hold (§7): no redraw while a pop-out is open; the held rows draw on close; the pager says so |
| the hold leaves a stale table unnoticed | the pager line names it; closing the pop-out (Escape, click outside) ends it; nothing else on the page is held |
| a picked value vanishes from the list on a push | the state keeps it; listed with count 0 until unchecked |
| two kinds disagree about "blank" | one rule: a blank never matches a bounded range or time; a pick lists only values that exist; text matches "" trivially (filter off) |
| ES5 | no `Set`/`Map`/arrow; the pick's distinct values through an object keyed by the string; datetime-local parsed by hand; the ES5 gate and `node --check` run in the suite |
| the "no ellipsis" rule | the button wraps or the column grows, never `text-overflow`; the pop-out grows |
| keyboard and screen readers | a real button, `role="dialog"`, `aria-label`, Escape, focus return |
| the smoke reads the old grammar | `tools/smoke/tables.js` step (c) moves to the pop-out (§12) |

## 11. What does not change

The cells, the sort, the paging, the ids, the caller contracts (`jobsTable(rows, opts)`,
`requestsTable(rows, opts)`), the rows' order with no sort, the endpoint route's cap, the
`cellText` cache (text columns only use it; the typed kinds read `sortKey`, which is cheaper).

## 12. Tests and the smoke (what moves)

- `tests/test_3_10c.py::test_the_filter_grammar_substring_and_the_numeric_comparators` and
  `::test_the_filter_row_is_inputs_one_per_column_built_from_the_state` pin the old grammar and
  the one-input-per-column row; both are rewritten to the new contract (ruling (o): the new
  behaviour tests go in `tests/test_3_12.py`; these two are the existing tests that must change).
- `tests/test_3_11a.py::test_a_filter_keystroke_builds_each_rows_filter_text_once_per_table`
  stays (text columns).
- New behaviour tests drive `dataTable` under node with `tests/fakedom.js` (3.11a): the pick's
  distinct values and faceted counts; inclusive range both ends; blanks under a bounded range;
  the time bounds at :00 and :59; the state shape; a push rebuilding the open pop-out; the
  "clear" paths. fakedom has no layout, so placement is the smoke's.
- `tools/smoke/tables.js` step (c) becomes: the status select set to "done" → 40 rows (the option
  label "done (40)"); open turns, from 101 → the same 7 rows `>100` gave, then the shortcut box
  ">100" → the same 7 with the button reading "> 100"; a started range inside the hand-made day;
  a push (a new `render(copy2)`) with the turns pop-out open leaves the tbody's first cell text
  and the pop-out's box values unchanged and the pager reading "updates held…"; closing it draws
  copy2's rows. A new read: the pop-out's bounding box lies inside the table block's at 1200 px
  and at 700 px widths.

## 13. Build plan

One story, one lane, after 3.11a's review lands: Qwen/ai1 (3.10c was 81 turns on it; this is
about twice the surface) at 250 turns, worktree, files: app.js (the `dataTable` region and the
two column maps), app.css (the button, the pop-out, the pager link), `tools/smoke/tables.js`,
`tests/test_3_12.py`, the two 3.10c tests. The brief quotes §3–§9 as the contract, cites the lines
above, and carries the two judgement sentences. Then a construct-it review by DeepSeek/dgx1-a
(≤4 probes: the inclusive ends, the faceted count under two filters, the push with an open
pop-out, the clamp at 700 px), the fold by the reviewer, a third-model review of the fold. The
operator reads the live page before the story is ticked (the renders-not-truth caution).

## 14. The questions and the operator's answers (2026-10-31 morning, verbatim)

- Q1 (multi-valued pick in a pop-out vs a native single `<select>`): "I think the drop-down is
  sufficient." → §4 is the native select.
- Q2 (time presets): "Agree." → out.
- Q3 (a push while a pop-out is open): "Depends on frequency of the push - if too often the picker
  is impossible to use. I would hold to be safe." → the hold, §7. (The overview pushes every
  snapshot and a running job changes the table on each, so the picker would have closed every few
  seconds.)
- Q4 (the `>n` grammar): "We can try it and see." → the shortcut box inside the range pop-out, §5.
- Q5 ("blank only"): "Not sure what you're considering here." → explained (a request with no
  prefill figure or a job with no last activity cannot be found by any filter: a bounded range
  hides blanks, text cannot match an empty cell); recommended out of this story and left out.
