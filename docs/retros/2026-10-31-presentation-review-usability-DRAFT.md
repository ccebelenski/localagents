# Retrospective (DRAFT): the 3.7 presentation pass, the 3.9 codebase review pass and the 3.10 usability pass, 2026-10-29 night to 2026-10-31 morning

DRAFT: the arc is closed by the operator, not by this file. Counts are from `docs/EPICS.md`
(sections 3.7 to 3.11), the job records and the git log, taken at 2026-10-31 ~04:00 while 3.11a
(the Opus codebase pass) was still running; its numbers go in when it lands. The operator's
sentences are verbatim from EPICS.

## What shipped

| | |
|---|---|
| Stories and folds ticked in 3.7–3.11 | 45 ticked, 6 open (named, the operator's), 1 declined (1.13) |
| Tests | 611 → 813, green at every landing (one landing left a red test the coordinator fixed in the next commit, twice) |
| Local lane jobs named in EPICS | 56 (Qwen/ai1 the most, DeepSeek second, Qwen/ai2, the dense Qwen3.8-27B on llamacpp, Opus ×4 Agent worktrees, Haiku ×2) |
| Cross-model reviews | 36 review records; six closed a chain with no finding; every other one found something real |
| Folds landed | 12, each by the reviewer that found the defect (ruling (a)), each then reviewed by a third model |
| Schema | v6 → v9: the finished-jobs index, transcripts at completion, the job name dropped |
| The page | no ellipsis anywhere; one jobs table and one requests table with paging, sorting and column filters; the endpoint page without tiers; every job shown by its short id; the handshake out of the error counts; the shim's clock honest |
| Tools | tools/accept.sh and land.sh (land drops lane artefacts); tools/smoke/: cdp.mjs, drive.mjs, mem.mjs, floor.mjs, ffmem.py, churn.js and fourteen hand-render scripts; `node --check` and the ES5 gate in the suite |

## Finds by finder (counted from the records; the operator checks the count)

- **The operator, looking at the live page: 18 findings, every one a story.** The 3.7 walk
  (cards in config order, hide the never-seen, no ellipsis, the timeline, the refresh clock…);
  the handshake rows (3.7t); "tool errors 2 vs 1" (3.7v); the Firefox tab (3.7w, which turned out
  to be an extension's orphan nodes); the ai9 exposition (3.7x) and its card (3.7y); the whole
  3.10 list in one message (a timestamp, "server gone", prefill, the tiers, "every endpoint",
  the ellipsis on tiles, "Jobs on it", events, two tables, 889 rows); the job name as a second
  key (3.10f: "What you've created is essentially two PK's"); the one-column stretch (3.10g);
  "prompt = 1" (3.10h, which exposed the shim's first-byte clock); the vLLM lane's missing
  speeds (3.10i). The design faults — the second key, the tiers taxonomy on the page, the
  proxy timestamp — were the operator's, not a model's.
- **Cross-model construct-it reviews: ~40 verified defects, 0 false after reproduction.** The
  ones that changed the product: the endpoint page had not refreshed itself since 3.7r (Qwen,
  3.9D); a vanishing card threw inside render (DeepSeek, fold-3 D1); the 7-day history read
  every probe row and the cache had no bound (DeepSeek, 3.9C); the shim's generation clock
  started on the first byte (found by the operator's "prompt = 1", confirmed by the review);
  the usage parser read only the last chunk, so vLLM's output was 0 (the dense Qwen); the table
  filter ignored exactly the titles it needed and blanks flipped with the sort direction
  (DeepSeek); an unknown node still printed its sentence (DeepSeek); an unshielded transcript
  write could strand a job running (DeepSeek). Reviews of folds found something in 6 of 12
  cases; the second round was smaller every time; no chain needed a third.
- **Builders correcting the brief with evidence: 12.** "A test pins ES5" (false, twice — the
  gate was then built); the requests-table id test inverted; the brief's httpx type list wrong
  (`ReadError` is a TransportError); `parents[2]`, not "parent's parent"; cdp.mjs's `tiles`
  field reads the overview's hidden cards (twice); `prune --dry-run 0` not a valid argv;
  "51,519.7" misdivided; "0.5 s" would print "0.0 s"; a file the brief's list missed
  (test_summary_db pins two summaries equal); `job_detail` needs no own line; the state-dir
  import cycle. None of the twelve was itself wrong.
- **The coordinator's own reads and folds: 9**, three of them its own defects (the
  `scrollbar-width: thin` tweak, the fold-3 render path, a duplicated story number); the rest
  folds at landing (the per-page table id; the UUID pattern made structural; lifting two
  reviewers' unpinned probes into the suite; the subprocess guard test made deterministic on
  the main checkout).
- **The suites on main: 2 cross-lane breaks** (1.14's hand-built `Job(name=…)` after 3.10f-1;
  3.11b's subprocess test assuming a worktree path) — both caught by land.sh's suite step,
  both one-line fixes.
- **Measurement, not review: 4.** The Firefox memory hunt (ffmem.py: the page holds 0.0 MB of
  orphan nodes under 960 rebuilds; the tab's 278 MB belonged to a content script); the history
  read 861 → 390 ms; the card floor against an independent oracle; the filter keystroke 131 →
  28-45 ms after the fold.

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

## Ruling proposals (done repeatedly, with results; the operator inks or declines)

- **(p) The state-dir guard is code, not convention.** Two incidents in one day reached the
  real state directory from a lane (3.9B's prune of 66 directories; 22:44's migration from a
  worktree's code). Since 1.15 a worktree checkout refuses the default state directory by
  construction, the smoke recipe exports both variables, and 3.11b's test drives the guard
  through a real child interpreter. Proposed as a ruling because it replaced a brief-level rule
  that failed twice.
- **(q) A reviewer's unpinned probe is lifted into the suite at landing.** Three times tonight
  (the dense Qwen's P2/P3 hand-renders as jobsrows.js/tiles.js; Qwen/ai2's p1c2; DeepSeek's
  table witnesses in tables.js) a review found a behaviour no test pinned and its own probe was
  the test; the coordinator or the folding reviewer committed it. Results: each later fold ran
  those witnesses first.
- **(r) A brief's claim "a test pins X" is grepped before dispatch.** Two false claims in one
  evening cost two lanes a correction each; the gate that did not exist was then built (ES5,
  then `node --check` after a stray brace passed the regex). Cite-check is in the hints already;
  this names the one claim shape that slipped.
- **(s) The seed grows the states the operator has seen live** before a smoke is trusted on
  it. Three lanes noted the seeded running job reads "lost" (the seed's own pid as its server),
  the floor-bearer is a down endpoint the live page lacks, and no seeded request is cache-silent
  or a handshake. The operator's own caution ("renders, not truth") is the rationale.

## What cost

- The 3.9B prune incident (66 job directories, 19 jobs' transcripts): a probe lane without the
  redirect. One hour of the operator's data, nothing valuable; fixed by t/conftest.py, then made
  structural by 1.15 after the second incident.
- The 22:44 incident: the real database migrated to v9 from a lane's worktree code before the
  landing. The migration behaved as designed; both running processes then needed a restart;
  the exact subprocess was never identified from the record. An event, not a ruling by itself;
  the ruling is (p).
- The Firefox memory hunt: about an hour of operator attention on something that was not ours.
  The tool that proves it now takes four minutes.
- The MCP server on yesterday's code for a whole day, unnoticed until the transcripts table
  stayed empty: sixteen jobs ran without the day's fixes. Now visible (1.14's build stamp and
  the job page's "server code differs" note).
- Two briefs' false "a test pins it"; the brief's wrong httpx types; the brief's inverted id
  test; a stale README floor number three lanes re-measured. Each cost a lane ten minutes.
- Both-sides-appended test conflicts twice before ruling (o).

## Open rulings

- 3.7u's trade: the two job lines in or out of the card floor (the operator's).
- The pytest-driven DOM smoke: proposed, then held by the operator's "renders, not truth"; (s)
  is the precondition if it is ever built.
- Database purge and the analytics path: facts and three shapes under Epic 4, not scheduled.
- vLLM's `--enable-prompt-tokens-details`: at its next natural restart.
- The MCP server reconnect to pick up the shim's clock and tail fixes: at a quiet moment.

## Closing step (proposed)

`docs/EPICS.md` is past 1,900 lines and every dispatch pays for it. The ANAS closing step
applies: move the shipped narrative of 3.7–3.11 out of the live plan into
`docs/EPICS-history.md` (line-range moves with a multiset check, a Haiku or Qwen job), leaving
in EPICS the standing rulings, the open items and the stories not yet built. The operator decides
when.
