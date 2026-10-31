# Table filters by column type (3.12) — design, for the operator's approval before any build

Operator (2026-10-31, EPICS 3.12): "some of the table filters don't make sense, some are not
useful. Examples: Jobs table, status colume is limited values, should be dropdown picker for
filter. Same with model, endpoint. Started is a time so a text search isn't the perfect way to do
that filter either. elapsed could be a range. turns, tool calls, errors are all values so range
picker would be appropriate with absolute and inclusive endpoints. Filters that don't fit nicely
into the colum size can be pop-out elements, activated on a click on the filter row/column. Do a
design on that before any build since there are multiple aspects to this request and a bad choice
can ruin the page."

Status: DESIGN. Nothing here is built. The operator approves, amends or declines each decision;
the build stories are cut from the approved text.

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

Each column declares a filter KIND. Four kinds: `text` (what exists), `pick` (a set of values
chosen from a list with counts), `range` (two inclusive numeric bounds), `time` (two inclusive
instants). The filter row keeps one cell per column: a `text` column keeps its inline box; the
other three show a small button that reads the current filter in a few characters (or the column
name, muted, when none) and opens ONE pop-out under the cell. The pop-out is the only place a
pick, a range or a time is edited; it applies live, closes on Escape, on a click outside, or on
its own close control, and survives a push because it is rebuilt from `TABLE_STATE`, not kept in
the DOM. Filters AND across columns and OR within a pick. The pager line gains "clear filters".

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

## 4. The pick

- The list holds every distinct value of the column across ALL fetched rows (the sort key's
  value, which is the word the pill prints or the cell's text), sorted as the column sorts, each
  with a count. The count is the number of rows that match the OTHER columns' filters (faceted:
  "done 40, failed 12, running 3" under a turns range), so a value whose count is 0 stays listed
  and selectable rather than vanishing when another filter drops its rows.
- Each value is a checkbox. None checked = no filter. Several checked = any of them.
- A value that disappears from a later push (a model no longer in the rows) stays in the state and
  is listed with count 0 until unchecked, so the operator's choice never silently evaporates.
- The button reads the one value when one is checked ("done"), "N of M" when several
  ("2 of 7"), the column name muted when none. The full list rides in the button's title.

Alternative considered and not recommended: a native `<select>` inline. It is a true dropdown
with zero custom code, but single-valued ("failed or timeout" needs two), has no counts, and would
be a second mechanism beside the pop-out the range and time kinds need anyway. One mechanism.

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
- The 3.10c `>n` / `<n` typed grammar goes. It was an exclusive comparator in a text box, half of
  what the operator asked for and invisible to anyone who did not read the title. (Decline if the
  typing shortcut is wanted; it can stay as a parser behind the "from" box: "≥100" typed there.)

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
- No presets ("last hour", "today") in this story. They are three links that fill the boxes and
  are cheap; the operator decides whether they earn the space (question Q2).
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
  replaces the whole table block. `TABLE_STATE[id].open = <column name>` records the open pop-out;
  the new `dataTable` call rebuilds it from the state and re-focuses the control that had the
  caret (the 3.10c caret trick, by a `data-filter-field` attribute). Known cost: a NATIVE date
  picker popup is closed by the rebuild. The overview's render skips when nothing but `now`
  changed (3.7m), so this bites only when a job actually moves while the picker is open; typing the
  value is unaffected. (Q3 asks whether this is acceptable or whether the overview should hold
  its redraw while a pop-out is open.)
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
  open: null | "<col>"             // the pop-out, if one is open
}
```

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
| the pop-out dies on a push mid-edit | rebuilt from `TABLE_STATE.open` with focus restored; the native date picker is the one loss (Q3) |
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
- `tools/smoke/tables.js` step (c) becomes: open the status pop-out, check "done" → 40 rows;
  open turns, from 101 → the same 7 rows `>100` gave; a started range inside the hand-made day;
  a push with a pop-out open keeps it open on the same column with the same values. A new read:
  the pop-out's bounding box lies inside the table block's at 1200 px and at 700 px widths.

## 13. Build plan

One story, one lane, after 3.11a's review lands: Qwen/ai1 (3.10c was 81 turns on it; this is
about twice the surface) at 250 turns, worktree, files: app.js (the `dataTable` region and the
two column maps), app.css (the button, the pop-out, the pager link), `tools/smoke/tables.js`,
`tests/test_3_12.py`, the two 3.10c tests. The brief quotes §3–§9 as the contract, cites the lines
above, and carries the two judgement sentences. Then a construct-it review by DeepSeek/dgx1-a
(≤4 probes: the inclusive ends, the faceted count under two filters, the push with an open
pop-out, the clamp at 700 px), the fold by the reviewer, a third-model review of the fold. The
operator reads the live page before the story is ticked (the renders-not-truth caution).

## 14. Questions the operator decides

- Q1. Pick is multi-valued with counts in a pop-out (§4), not a native single `<select>`. Agree?
- Q2. Time presets ("last hour", "today", "last 24 h") as three links in the time pop-out: in or
  out? (Recommendation: out for now; the boxes come first.)
- Q3. A push while a pop-out is open: rebuild it from the state and accept that the native date
  picker closes (recommended, simplest), or hold the overview's table redraw while a pop-out is
  open and redraw on close (the live table goes stale while the operator edits a filter)?
- Q4. The `>n` / `<n` typed grammar goes (§5). Keep it as a typing shortcut inside the range
  pop-out instead?
- Q5. A "blank only" / "has a value" choice for range and time columns (a request with no
  prefill figure; a job with no last activity): not in this story unless wanted.
