# The started picker (3.12c) — design

Operator (2026-10-31, EPICS 3.12): "Started: hard to use or figure out, not intuitive as
implemented." Then: "my 'not intuitive' remark on the started column for example was that I really
couldn't figure out how to make it work at all - so functionality is undetermined by me." On a pick
of relative windows: "No I think a pick would be good - but it's relative right? 'Last 15 minutes'
on a endpoint jobs list is not literally last 15 minutes from NOW. On the 7d filter we literally
have dates too - so it's not a simple pick value." On a pick of hour-or-day buckets: "I think if you
make it too fine grained the picker gets too long - maybe a two-level picker, the first being a date
(today, yesterday, list of dates) + a clock filter (graphical or digital clock bounding hours?
Digital would be two hours choices, analog would be two hands on a clock face representing the
bounds)." Closed with "Agreed on all - let it fly."

Status: design APPROVED in conversation. Builds AFTER 3.12b (the picker lives in the filter row and
needs the row to survive a push). The build waits for its own "go" (ruling (p)).

## 1. What exists and why it failed

3.12a gave the `time` columns (jobs "started", requests "time") a text box reading three grammars
(`HH:MM`, `YYYY-MM-DD`, `a - b`, `<a`, `>a`; spec 2026-10-31-table-filters-design.md §6), named only
in the box's hover title. The operator could not work it at all. The likeliest path: a date typed
alone matches zero rows (the review's possible (1)). A filter whose grammar must be discovered is
not rescued by fixing its grammar; the typed time grammar is retired by this story.

## 2. The decision in one paragraph

The `time` kind becomes a two-level picker in the filter row: a compact control in the filter cell
that reads the column name when unset and a summary when set, opening a small panel with three
native selects — a DATE (any, today, yesterday, then each older date present in the rows, each with
its count) and two HOUR bounds (from, to; the hours present on the chosen date; inclusive) — and a
clear link. Digital, not analog: two native selects give the same bound in a form the browser draws,
focuses and scrolls; a clock face is a custom widget with an AM/PM ambiguity, drag and keyboard
paths and tests of its own, which the DOM smoke could not see. Absolute, not relative: the options
are the dates and hours actually in the table, so nothing drifts as `now` moves, and the endpoint
page's range selector keeps its own job. The state stays `{col: string}`.

## 3. The state string

`filters[col]` is one of: `""` (any — deleted, as every kind); `"YYYY-MM-DD"` (that local day);
`"YYYY-MM-DD HH-HH"` (that day, from the first hour's start to the second hour's end, inclusive:
`09-11` is 09:00:00 through 11:59:59; from > to matches nothing, as a number range does). Hours are
two digits, 24-hour. The date is absolute even when the panel labelled it "today": the label is
display, the value is the date, so a filter set before midnight still means the day it named. A
row matches when its sort key (epoch seconds; null never matches) falls in the span, computed with
local `Date` components (the cells print local time).

## 4. The control in the cell

- `<button type="button" class="filter-pick" data-filter="<col>" data-filter-kind="time">` in the
  filter cell, styled as the text box is (the app.css:349 rule's look: muted, 13 px, the same
  border and padding, `width: 100%`, text left-aligned). Unset it reads the column name as the
  box's placeholder did; set it reads the summary: "today", "yesterday", or the date as the
  started cell prints one (`fmt.date` without the time — the same local short date the cells use),
  then " 09 to 11" when hours are bound. A set control carries the `set` class (the muted colour
  gives way to ink, as a typed filter's text does).
- Click (or Enter/Space, a button's own) opens the panel; a second click closes it.

## 5. The panel

- A `div.filter-panel` appended to the `.table-block` container (NOT inside `.table-wrap`: its
  `overflow-x: auto` clips an absolutely positioned child and would scroll instead), positioned
  absolutely under the cell: `.table-block { position: relative }` and the panel's `top`/`left`
  from the cell's `getBoundingClientRect()` relative to the block's, left clamped so the panel
  stays inside the block. One panel at a time per table.
- Contents, one line: "date" `<select>` (any; today; yesterday; each older date present, newest
  first; every option labelled "<label> (<count>)", the count over the rows the OTHER columns'
  filters leave, as the pick does; today and yesterday carry the real date in their `title`) ·
  "from" `<select>` and "to" `<select>` (any, then the hours present among the rows on the chosen
  date, "09:00" for from and "09:59" for to; disabled until a date is chosen) · "clear" (a
  `pager-btn`, empties the state) · "done" (closes).
- Every change writes the state string (§3) through `setFilter` and redraws (level 2 of 3.12b: the
  panel and its selects persist, being part of the filter row's cell state — the panel is rebuilt
  only when closed; while open, its option counts update in place by the same focus rule as the
  pick's select).
- Closes on "done", Escape, or a `mousedown` outside the panel and the control (one document
  listener while open, removed on close). A push never closes it (3.12b).
- Dates present: the distinct local `YYYY-MM-DD` of every row's sort key. Today and yesterday are
  listed only when rows fall on them (an option at 0 is noise here, unlike the pick's faceted
  zero, because the date list is the point). A selected date a push drops stays as an option "(0)"
  until cleared, as the pick does.

## 6. What goes

`parseTimeLiteral` and the `time` branch of `filterIdx` (app.js:869-908), their tests in
`tests/test_3_12.py` (the time-literal and comparator tests are deleted, the record stays in
EPICS), the time box's title grammar, the smoke's typed `HH:MM-HH:MM` window and the date-substring
witness (replaced by the picker's witnesses, §8). The `time` kind name stays; its control changes.
The requests table's "time" column takes the same picker (a job's requests sit within hours, so the
date level usually holds one option and the hour bounds do the work).

## 7. Risks named, each with its answer

| risk | answer |
|---|---|
| the panel is clipped or scrolls the table | it is a child of the block, not the wrapper; the brief names this and the smoke reads its bounding box against the wrapper's |
| the panel is long | two levels: dates are days present (7d: at most eight), hours are the hours with rows on one day (at most 24) |
| a pop-out on a page that has none | the operator's own door ("Filters that don't fit nicely into the column size can be pop-out elements, activated on a click on the filter row/column"; "dates maybe still need one? Your call") |
| the summary does not fit the cell | the started column is as wide as a date; "yesterday 09 to 11" is the longest, at 13 px it fits a cell that prints "10/30/2026" — the smoke measures the cell width before and after; no ellipsis (3.7u) |
| the operator loses the text search on started | accepted: the one thing it did ("10/30") the date select does better |
| a push while the panel is open | 3.12b keeps the filter row; the panel is a child of the block, untouched by the body patch |

## 8. Tests and the smoke

- `tests/test_3_12c.py` under node + fakedom: the state strings per selection (day; day with
  hours; from > to matches nothing; "any" deletes); the date options over hand rows across three
  days (labels, order, counts faceted by another filter); the hour options follow the chosen date
  and are disabled without one; the summary text per state; the match rule at the bounds
  (09:00:00 in, 08:59:59 out, 11:59:59 in, 12:00:00 out); the panel is a child of `.table-block`
  and not of `.table-wrap`; close on Escape and on outside mousedown; the requests table's time
  column draws the same control.
- `tests/test_3_10c.py`'s "one control per column" pin widens to input, select or `.filter-pick`.
- `tools/smoke/tables.js` step (c): the started picker opened, a date chosen → that day's rows
  (count computed from the hand rows), hours 11 to 11 → the 11:xx rows, clear → 120; the panel's
  bounding box lies within the block and outside the wrapper's scroll area; the filter cell's width
  unchanged by the control.

## 9. Build plan

One story (3.12c), one lane, worktree, after 3.12b lands: Qwen/ai1 at 250 turns; files: app.js
(the `time` kind's control, panel and match rule; the retirements), app.css (`.filter-pick`,
`.filter-panel`, `.table-block { position: relative }`), tests/test_3_12c.py, tests/test_3_12.py
(deletions), tests/test_3_10c.py (the pin), tools/smoke/tables.js. Then a DeepSeek construct-it
review (the bounds, the positioning, the close paths), the fold by the reviewer, the operator's
hands on the live page before the tick.
