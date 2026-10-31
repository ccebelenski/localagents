# Two-level table refresh (3.12b) — design

Operator (2026-10-31, EPICS 3.12, after the 3.12a review): "we do need the hold, the refresh makes
using a filter very difficult." and "Another render quirk is that on a refresh the scoll bars appear
on the screen briefly in firefox, then go back dormant. Visually annoying. We might need to break
the screen into individually refreshed areas or use websockets for live updates. Opinions there?"
Then, on the coordinator's patch-in-place answer: "I would add a bit more to the table refresh - a
table that hasn't changed content needs no refresh - can we cache a quick hash over the content and
not do a refresh if the hash doesn't change? No cached hash value forces the full refresh. And then
we do your patch in place idea on top. This gives us two levels of content control." And: "So, a
running job would essentially force a refresh, but an idle endpoint would show no updates when no
job was running? If so, that seems okay". Closed with "Agreed on all - let it fly."

Status: design APPROVED in conversation (the operator: "Agreed on all"). The build waits for its
own "go" (ruling (p)).

## 1. What exists and what is wrong

The page is already pushed: an `EventSource` on `/events` (app.js:1787) delivers a snapshot per
`refresh_s`, and `render(snap)` (app.js:1421) skips everything when the snapshot without `now` is
unchanged (`snapSig`, app.js:1429). Since 3.7m the host cards are patched in place. The tables are
not: every changed snapshot runs `byId("jobs-body").replaceChildren(jobsTable(…))` (app.js:1516),
the endpoint page rebuilds its three sections wholesale in `drawTables` (endpoint.js:493), and the
job page rebuilds its requests section (job.js:428). `dataTable` (app.js:782) returns a NEW
`.table-block` on every call: a new `.table-wrap` (`overflow-x: auto`, app.css:159), a new header, a
new filter row, a new body, a new pager.

Consequences the operator has felt: Firefox shows an overlay scrollbar on every freshly created
scroll container, so each push flashes the bars; the open `<select>` and the text caret live in the
discarded filter row (the caret restore at app.js:1080-1086 never had a value to restore — `caret`
is never assigned); a table scrolled sideways snaps back to the left; and a table whose rows did
not change is rebuilt because something else on the page did.

## 2. The decision in one paragraph

A table keeps its DOM across draws of the same id. Level 1: a push whose rows hash the same as the
last draw touches nothing. Level 2: when the hash differs, or the operator acts (sort, filter,
page, clear), the existing wrapper, header and filter row are PATCHED and the body is reconciled
row by row; only rows whose content changed get new elements. No scroll container is ever
recreated on a push, so no flash, no lost dropdown, no lost caret, no lost sideways scroll. A
filter control that has focus is never touched by a patch. The view state (`TABLE_STATE`, the
`{sort, filters, page}` the smoke reads) does not change shape. The hold is not built: the patch
makes it unnecessary.

## 3. Level 1: the content hash

- `dataTable(id, columns, rows, opts)` keeps, per id, the LAST draw: `TABLE_DOM[id] = {container,
  hash, rows, caches}`. The hash is `JSON.stringify(rows)` joined with the column names in order
  (a different column set — the endpoint page omits "endpoint" — is a different table); the
  page already pays this string for the whole snapshot, and 120 rows is under a millisecond.
- A call whose id has an entry, whose `container.isConnected` is true (the element is still in
  the document — a page change discards the old DOM, and the next call is a full build) and whose
  hash equals the stored one returns the SAME container and does nothing else. A counter
  (`renders.tableSkipped`, beside the existing `renders` counts) records it.
- No entry, a disconnected container, or a different hash: level 2.
- Two facts to accept: `elapsed_s`, turns and last activity arrive in each row, so a running job's
  row changes every push and the hash sees it (the operator: "a running job would essentially force
  a refresh"); the started cell reads `now` only to switch from a clock to a date once a row is a
  day old (app.js:650), and under level 1 that switch waits for the next real change in the
  table. Accepted rather than hashing the hour in.

## 4. Level 2: the patch

The internal `draw()` becomes the patch; it runs on a hash change and on every operator action.

- **The container, wrapper, table, thead and header row persist.** The header's sort marks
  (`sort-asc`/`sort-desc` classes, `aria-sort`) are set on the existing `th`s. The filter cells
  persist with their controls: an input's value is never written by a draw (the box's text IS the
  state, 3.12a §2); a select's options are rebuilt in place (the counts change) unless the select
  is `document.activeElement`, in which case the rebuild is deferred to its `blur` (a `focusout`
  listener on the filter row re-runs the option rebuild for the control that lost focus). The
  caret save/restore code goes: the input is never replaced.
- **The body is reconciled.** The filtered, sorted page rows are computed as today. The renderer
  may give `opts.key(row)` (jobsTable: `job.id`; requestsTable: none — request rows carry no id).
  With a key: the existing `tr`s are indexed by key (`data-key` on the tr); a page row whose key
  exists and whose row SIGNATURE (`JSON.stringify(row)`) equals the one stored on the old tr keeps
  that element, moved into position if needed; otherwise a new tr is built through `col.cell`. Without
  a key: positional — the tr at position i is kept when its stored signature equals the new row's,
  else replaced. Surplus trs are removed. A new job at the top of a keyed table shifts nothing but
  one insert; the unkeyed requests table rebuilds the rows below an insert, as today, no worse.
- **The pager and the note** are patched: the pager's children are rebuilt (text and buttons are
  cheap, carry no focus the operator loses) inside the same `.table-pager` element; the note's text
  is set.
- **Caches.** `textCache` and `keyCache` are keyed by row index for ONE `rows` array; a new rows
  array (a hash change) resets both. `sortRows` reads through `keyOf` (the review noticed it bypasses
  the cache).
- **The empty list.** `dataTable` returns the `.empty` div today when `rows` is empty, with no
  container. Now the container persists and holds EITHER the empty sentence or the table; a list that
  empties then refills rebuilds the table once (one flash, on a real change). The "clear filters" link
  is drawn on the empty sentence too when a filter is set, so a filter can be cleared while no rows
  are fetched (the review's "empty rows remove the clear link").

## 5. The callers

Three callers stop re-inserting the table:

- app.js:1516: `jobsTable(snap.jobs, …)` returns the persistent container; the page appends it to
  `#jobs-body` only when it is not already there (`if (el.parentNode !== body) body.replaceChildren(el)`).
  Re-inserting the same element recreates its scroll frame, so this check is the contract, written
  once as `mountOnce(parent, el)` in app.js and exported for the other two pages.
- endpoint.js `drawTables` (:482-497): the three sections persist as elements (`jobsSection`,
  `eventsSection`, `requestsSection` keep their `<section>` across calls, patching the heading text,
  the totals line and the loading/empty body); the table inside each is mounted once. The events
  section stays rebuilt (small, no filters, usually absent).
- job.js `requestsSection` (:405-428): the same — the section element persists, the totals line is
  rebuilt, the table is mounted once.

## 6. What does not change

The cells, the sort order rule (3.7g), the paging, the ids (per endpoint since the 3.12a fold),
`TABLE_STATE` and its shape, the filter grammar (3.12c retires the time box separately), the
renderers' column specs, the endpoint route's cap, the smoke's state reads.

## 7. Risks named, each with its answer

| risk | answer |
|---|---|
| a kept tr shows stale cells because its signature missed a change | the signature is the whole row serialized; a cell is a function of its row and `opts.now` (§3's accepted exception) |
| a select's options go stale while focused | deferred to blur; the counts are the only staleness and the operator is holding the list open |
| the same tr placed twice | an element is moved, never cloned; `rowMemo`'s one-placement rule is untouched |
| `isConnected` absent in the node stand-in | `tests/fakedom.js` gets it (a walk to `document`), said in the brief |
| a page change leaves `TABLE_DOM` entries for discarded DOM | the connected check makes them a full build; entries are overwritten, never grow past one per id |
| the Firefox flash is not what the code reading says | the smoke proves element identity and scroll position; the operator's eyes prove the flash — if it persists, the next reading is the document scrollbar (a height change), measured then |

## 8. Tests and the smoke

- `tests/test_3_12b.py` under node + fakedom (3.11a's loader pattern): the same container returned
  on an equal hash and `renders.tableSkipped` incremented; a different hash keeps the wrapper, thead
  and filter-row elements (identity) and replaces only the changed row's tr (keyed and positional
  cases); a kept tr moves on reorder; surplus rows removed; a focused select's options untouched
  until blur, then rebuilt with the new counts; an input's value untouched by a push; a disconnected
  container forces a full build; the empty sentence with "clear filters" when a filter is set; sort
  marks patched in place; `render(snap)` twice keeps `#jobs-body .table-wrap` identity.
- `tests/test_3_12a_fold.py` and `tests/test_3_12.py` keep passing (the filter behaviour is
  unchanged); the 3.10c pins on the pager text unchanged.
- `tools/smoke/tables.js`: a step (h) that pushes the rows again with one job's turns changed:
  the wrapper element identity (a marker property set before, read after), `scrollLeft` set before
  the push and read after, the changed row's new text, the unchanged rows' element identity; and a
  step (i) pushing identical rows: nothing changed (`renders.tableSkipped` up by one).

## 9. Build plan

One story (3.12b), one lane, worktree: Qwen/ai1 (the fast ES5 lane) at 250 turns; files: app.js
(`dataTable`, `TABLE_DOM`, `mountOnce`, the render caller, `sortRows`), endpoint.js (`drawTables` and
the two sections), job.js (`requestsSection`), tests/fakedom.js (`isConnected`), tests/test_3_12b.py,
tools/smoke/tables.js. Then a DeepSeek construct-it review (the reconciliation's identity claims, the
focus deferral, the three callers), the fold by the reviewer, the operator's eyes on Firefox before
the tick.
