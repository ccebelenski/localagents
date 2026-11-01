# Retrospective: the 3.7 presentation pass, the 3.9 codebase review pass, the 3.10 usability pass, the 3.11 audits and the 3.12 typed filters, 2026-10-29 night to 2026-10-31 evening

Closed by the operator 2026-10-31 evening ("Yes, let's close the arc"). Counts are from
`docs/EPICS.md` (sections 3.7 to 3.12), the jobs table (read-only) and the git log. The first
cut was taken at 2026-10-31 ~04:00 while 3.11a was running; the 3.11a and 3.12 numbers were
folded in at the close. The operator's sentences are verbatim from EPICS.

## What shipped

| | |
|---|---|
| Stories, folds and reviews ticked in 3.7–3.12 | 45 ticked by 04:00 (3.7–3.11b), then 3.11a and 14 entries under 3.12 (10 stories or folds, 4 review entries); 5 open items named by the operator; 1 declined (1.13) |
| Tests | 611 → 911 (813 at 04:00), green at every landing |
| Local lane jobs | 56 named in EPICS by 04:00; 16 more in the jobs table since (all succeeded, none capped) plus the Opus 3.11a worktree; one lane cancelled at 16 turns when the operator withdrew the first 3.12 draft |
| Cross-model reviews | 36 records by 04:00; 9 since (3.11a, 3.12a, the 3.12a fold, 3.12d+e, 3.12b, 3.12c, the 3.12c fold, 3.12f, the 3.12f fold): 5 found defects (10 in all), 4 closed a chain clean |
| Folds landed | 12 by 04:00; since: 3.11a's 17 by Opus from its own audit, the 3.12a fold (4 fixes), the 3.12c fold, the 3.12f fold, and four coordinator folds riding the next review (ruling (m)) |
| Schema | v6 → v9: the finished-jobs index, transcripts at completion, the job name dropped |
| The page | no ellipsis anywhere; one jobs table and one requests table with paging, sorting and typed filters (a select for the bounded columns, a unit-aware number box, a two-level date/hour picker); tables patched in place, never rebuilt, on a push; the endpoint page without tiers; short failure sentences on a down card; every job by its short id; the handshake out of the error counts; the shim's clock honest |
| Tools | tools/accept.sh and land.sh (land drops lane artefacts); tools/smoke/: cdp.mjs, drive.mjs, mem.mjs, floor.mjs, ffmem.py, churn.js and the hand-render scripts (tables.js carries the filter, picker, patch and dayless witnesses); `node --check` and the ES5 gate in the suite; the state-dir guard (1.15); the build stamp (1.14) |

## Finds by finder (counted from the records; the operator checks the count)

- **The operator, looking at the live page: 18 findings by 04:00, 7 more since, every one a
  story or a design correction.** By 04:00: the 3.7 walk (cards in config order, hide the
  never-seen, no ellipsis, the timeline, the refresh clock…); the handshake rows (3.7t); "tool
  errors 2 vs 1" (3.7v); the Firefox tab (3.7w, an extension's orphan nodes); the ai9 exposition
  (3.7x) and its card (3.7y); the whole 3.10 list in one message; the job name as a second key
  (3.10f); the one-column stretch (3.10g); "prompt = 1" (3.10h, which exposed the shim's
  first-byte clock); the vLLM lane's missing speeds (3.10i). Since: the 3.12 list (picks for
  the bounded columns, ranges for the numbers, a time filter that is not a text box); two
  design corrections on the coordinator's first draft ("you've overdesigned for the tables";
  "you're taking me too literally"); the elapsed box reading seconds instead of its own grammar
  (3.12d); the down card's paragraph (3.12e); the picker's hour selects dead without a date
  (3.12f — a sentence in the coordinator's own spec, built faithfully and cleared by every
  model review, found by hands on the page); the picker's end-time shown twice (deferred on
  the operator's word). The design faults — the second key, the tiers taxonomy, the proxy
  timestamp, the overdesigned filters, the disabled hour selects — were the coordinator's,
  not a lane's.
- **Cross-model construct-it reviews: ~40 verified defects by 04:00, 10 since, 0 false after
  reproduction.** The ones that changed the product: the endpoint page had not refreshed itself
  since 3.7r (Qwen, 3.9D); a vanishing card threw inside render (DeepSeek); the 7-day history
  read every probe row and the cache had no bound (DeepSeek, 3.9C); the shim's generation clock
  started on the first byte; the usage parser read only the last chunk (the dense Qwen); the
  table filter ignored the titles it needed (DeepSeek); an unknown node still printed its
  sentence (DeepSeek); an unshielded transcript write could strand a job running (DeepSeek).
  Since 04:00: a blank key unpickable and commas unread in the number box (DeepSeek, 3.12a);
  the ms cells' space and decimal durations unread (DeepSeek, 3.12d+e); the events section a
  new element every draw, resetting the tables' scroll, and note/empty text outside the hash
  (DeepSeek, 3.12b); the persistent picker button faceting by a dead call's column object, the
  clock's local day outside the hash, the events cache never reset (DeepSeek, 3.12c); no
  escape from the picker's hour edges — a clause of the story's own rule no operator could
  reach (DeepSeek, 3.12f). Reviews of folds found something in 6 of 12 cases by 04:00 and in
  1 of 4 since (a false comment, no code); no chain needed a third pass.
- **Builders correcting the brief with evidence: 12 by 04:00, 8 since; none of the twenty was
  itself wrong.** Since: four builds (3.12b, 3.12d, 3.12e, 3.12f) each moved a test pin the
  brief had not named because the gate broke it, each disclosed with the reason and each
  right; the 3.12c brief's panel geometry ("top at or below the wrap's bottom") could not hold
  and the lane witnessed parentage and no-scroll instead; the 3.12c-fold brief's reproduction
  ("new row objects, same values") was hash-identical and the lane changed one id; the
  3.12f-fold brief's "from any, to any → key gone" was unreachable one select at a time and
  the lane pinned the reachable sequence; the 3.12b spec's job.js item was wrong (the job page
  draws once per load) and was not built.
- **The coordinator's own reads and folds: 9 by 04:00, 8 since.** Since: the 3.10c F3 fold
  ("null" printed under every table); the 3.12a fold (`<a`/`>a` strict over the literal's
  whole span); the 3.12d+e fold; the 3.12b fold; a lane's false "0 ellipses added" caught at
  acceptance (one in the README line); and three of the coordinator's own defects found by
  others — the overdesigned first 3.12 draft (the operator), the picker spec's "disabled until
  a date is chosen" (the operator), the false comment that the midnight redraw switches an
  unchanged row's cell (Qwen).
- **The Opus audit (3.11a): 47 rows, 17 folds, Qwen review clean; row 37 awaits the operator.**
  The dense Qwen's test-suite audit (3.11b): 721 tests read, 703 sound, 14 sycophant page pins,
  3 too narrow, 1 dead.
- **The suites on main: 2 cross-lane breaks** by 04:00, 0 since (land.sh's suite step and the
  per-story test files of ruling (o)).
- **Lane claims found false at acceptance: 2 since 04:00** ("0 ellipses added"; the 3.12c fold's
  midnight-redraw comment) — both caught, neither a code defect.
- **Measurement, not review: 4.** The Firefox memory hunt; the history read 861 → 390 ms; the
  card floor against an independent oracle; the filter keystroke 131 → 28-45 ms after the fold.

## The operator's verdicts, verbatim

- "Much improved UI experience, I like the timeline now."
- "I don't like the '...' convention because it hides information - either set a minimum card
  width that can accomodate the widest text, or dynamically size the card to not truncate (and
  use that as min size so it can't shrink below that high water mark). We get fewer cards
  horizontally, but more complete information."
- "okay what was done is done - the data is still development data, and only operational so
  nothing valuable was lost." (the 3.9B prune incident)
- "Making good progress, reviews are holding up - including the human one."
- "You're free to argue with me on any point, at any time by the way - project wide. I'm not
  always right, and sometimes I'm even pretty badly wrong. … The two arguments I will never
  accept is 'The code is written', and 'It's difficult'. Those are factors to consider, not
  blockers." → ruling (n).
- "The risk of the smoke test is that it runs against renders, not truth." (on the proposed
  pytest DOM smoke — open)
- "Known hazard - multiple worktrees, but one database." (the 22:44 incident) → 1.15.
- "Achievement unlocks: All endpoints active!"
- "Certainly not going to reset the MCP while jobs running."
- "New rule: 'go' is explicit, not inferred." → ruling (p).
- "Actually I think you've overdesigned for the tables - I don't think we need a multi-pick for
  example." / "No, you're taking me too literally - no pop out needed for the simple values -
  dates maybe still need one? Your call." (the 3.12 design, twice corrected)
- "I say go on the open build work now that all lanes are back."
- "Ah, the old "linear left to right" picker trick, huh? Seriously, not a big deal, and not
  hard to fix. … there's no point in limiting it. Go for it." (3.12f)
- "Human reviews still important as last line of defense." (the operator's own retro comment,
  after their hands found 3.12f — a spec sentence every model review had cleared.)

## Ruling proposals (done repeatedly, with results; the operator inks or declines)

The operator's read of the first list (five proposals): three were the coordinator's own habits.
Inked at once as **(q)**: the coordinator owns its working habits (adopt or drop on evidence);
a recorded rule that has stopped helping goes to the operator for a final check before removal.
Under (q) the three habit-shaped proposals are withdrawn to the coordinator's hints, where they
already live: the state-dir guard as code (a fact since 1.15, not a rule), the grep of a
brief's "a test pins X" before dispatch, and the unnamed-pin letter ("ONLY the pins named,
unless the gate breaks another: then move it to what the rule says and name it in the report
with the reason" — four builds, 3.12b/d/e/f, each right). Two remained for the operator; decided in the 2026-11-01 interview: the lifted probe DECLINED
("it's a habit"), the seed INKED as ruling (r) with the seed story cut at once (EPICS 3.13):

- **(r) A reviewer's unpinned probe is lifted into the suite at landing.** Three times on the
  night of the 30th (the dense Qwen's P2/P3 hand-renders as jobsrows.js/tiles.js; Qwen/ai2's
  p1c2; DeepSeek's table witnesses in tables.js) a review found a behaviour no test pinned and
  its own probe was the test. Since: the 3.12c and 3.12f folds re-ran the reviewer's probes
  first and the reproducing probe became the RED test; the 3.12d fold lifted six unpinned
  readings into tests/test_3_12d_fold.py. A nod keeps it the default.
- **(s) The seed grows the states the operator has seen live** before a smoke is trusted on
  it. Three lanes noted the seeded running job reads "lost", the floor-bearer is a down endpoint
  the live page lacks, and no seeded request is cache-silent or a handshake; the 3.12f review
  could not drive the picker's explicit 00:00 pick live (no seeded row has one) and its smoke
  could not tell "every day" from "today" (every hand row sits on NOW's day). The operator's
  own caution ("renders, not truth") is the rationale. The one proposal worth the operator's
  ink.

## What cost

- The 3.9B prune incident (66 job directories, 19 jobs' transcripts): a probe lane without the
  redirect. One hour of the operator's data, nothing valuable; fixed by t/conftest.py, then made
  structural by 1.15 after the second incident.
- The 22:44 incident: the real database migrated to v9 from a lane's worktree code before the
  landing. The migration behaved as designed; both running processes then needed a restart;
  the exact subprocess was never identified from the record. An event; the guard is code since 1.15.
- The Firefox memory hunt: about an hour of operator attention on something that was not ours.
  The tool that proves it now takes four minutes.
- The MCP server on yesterday's code for a whole day, unnoticed until the transcripts table
  stayed empty: sixteen jobs ran without the day's fixes. Now visible (1.14's build stamp and
  the job page's "server code differs" note); the operator reconnected at a quiet moment on the
  31st and the server answers with the day's package.
- The first 3.12 design: overdesigned (pop-outs, a hold, multi-pick), corrected twice by the
  operator, one lane dispatched on it and cancelled at 16 turns. The design-first rule the
  operator set ("Do a design on that before any build … a bad choice can ruin the page") is
  what made the correction cheap.
- Two spec sentences of the coordinator's were wrong and built faithfully: the 3.12b spec's
  job.js item (not built — the lane read the code) and the picker spec's "disabled until a date
  is chosen" (built, cleared by two model reviews, found by the operator's hands, fixed as
  3.12f with its own review and fold: three lanes, about fifty minutes).
- Two briefs' false "a test pins it"; the brief's wrong httpx types; the brief's inverted id
  test; a stale README floor number three lanes re-measured; the 3.12c geometry claim; the
  3.12c-fold reproduction. Each cost a lane ten minutes.
- Both-sides-appended test conflicts twice before ruling (o); none since.
- One-off events, not rulings: a lane's "0 ellipses added" that was one; a reviewer's comment
  that narrated a behaviour the code does not have; the 3.12f-fold brief's unreachable clause.

## Open rulings and items

- 3.7u's trade: the two job lines in or out of the card floor; and 3.10g(1), the activity line
  out of the floor (designed, builds on its own go; the operator: "no activity for me to judge
  now, so I'll watch later when we have legit work to do").
- The AFTER reading of the Firefox scrollbars under 3.12b ("it's quiet right now").
- The picker's end-time shown twice (cosmetic, deferred: "I would let it go for now").
- The pytest-driven DOM smoke: proposed, then held by the operator's "renders, not truth"; (s)
  is the precondition if it is ever built.
- The 3.11a audit's row 37: the operator's ruling.
- Database purge and the analytics path: facts and three shapes under Epic 4, not scheduled.
- The smoke drivers never exit non-zero on a thrown script (cdp.mjs ignores
  `exceptionDetails`; dom.js prints it): a witness failure is visible only in the JSON.

## Closing step (proposed)

`docs/EPICS.md` is past 2,600 lines and every dispatch pays for it. The ANAS closing step
applies: move the shipped narrative of 3.7–3.12 out of the live plan into
`docs/EPICS-history.md` (line-range moves with a multiset check, a Haiku or Qwen job), leaving
in EPICS the standing rulings, the open items and the stories not yet built. The operator
decides when; nothing moves on this file alone.
