# Retrospective: Epic 2 (monitor core) and the Epic 1 fold chain, 2026-10-29

Scope: everything merged on main between 10:00 and 15:00 on 2026-10-29: the Epic 1 fold chain
(1.9 → 1.9a → 1.9b, 1.10 → 1.10a, 1.11 → 1.11a → 1.11b), Epic 2 (2.1 → 2.1a → 2.1b, 2.2 → 2.2a →
2.2b, 2.3 → 2.3a, 2.4 → 2.4a) and the first Epic 3 stories (3.1 → 3.1a, 3.5 → 3.5a), because
they were built in the same cycle with the same process. Epic 1's retrospective is
`2026-10-29-epic1-mcp-sqlite.md`; this one carries its fold chain as an addendum. Counts are from
`docs/EPICS.md` and the git log; the 2.4a review and stories 3.1b/3.2 were still running when
this was written and are recorded in EPICS when they land.

## What shipped

| | |
|---|---|
| Commits on main (10:00–15:00) | 57 |
| Story and fold commits | 22 |
| Tests | 221 → 554, all passing at every merge |
| Local lane jobs (Qwen ai1/ai2, Qwen3.8-27B llamacpp, DeepSeek dgx1-a) | 42 distinct, 26 with turn counts in EPICS summing to 2147 turns |
| Opus-high lanes | 5 (builds 2.2, 3.1, 3.2; the 1.10 review; the 1.10a fold) |
| Cross-model reviews recorded | 19; five found no defect at all |
| Schema versions | 1 → 4 (compactions; the monitor tables; state and disk columns), 5 in flight |
| Live runs | the smoke three times at build and once at review, every one against the real endpoints; the monitor command's SIGINT/SIGTERM exits measured in a subprocess |

The MCP server imports nothing from the monitor package; a test pins it and every lane's
self-check grepped it.

## Finds by finder

- **Construct-it reviews (ten models' worth): 31 defects, 0 false.** The ones that mattered:
  DeepSeek's review of 1.9 ran two concurrent cancels during worktree creation and found an empty
  transcript; Opus's review of 1.10 proved a regression by running the same cancel against the
  pre-story server code; Qwen/ai2's review of 2.3 made a quoted `attention_hours` kill the monitor;
  DeepSeek's review of 2.2 parsed the schema block out of the spec and compared it to PRAGMA;
  DeepSeek's review of 2.4 found the seed's negative elapsed and wall-clock first-seen that the
  tests could not see because they seed in-process; Qwen/ai1's review of 3.1 tabled every §5 datum
  against §5a and found six gaps before any page was built; Qwen/ai2's review of 3.5 refuted a
  recorded claim with three captures (the 501 row is `/api/hello`, not `count_tokens`).
- **Lanes correcting the brief with evidence: 14 times.** Sampled: the delta function's NULL rule
  does not exist (Opus, 2.2); `status.collect` makes no summary call (DeepSeek, 2.4a); Starlette's
  test client buffers a stream (Opus, 3.1); `node_dormant` and `node_state` are orthogonal (27B,
  2.4); the hardware series count was ten not eight (Qwen, 3.1a); construction C's prediction was
  wrong (27B, 2.1a review). None of the fourteen was itself wrong.
- **The suite on main: 2 cross-lane breaks**, both invisible in either lane's own worktree: 2.2a's
  parser change reached 2.4's seed through a counters insert (14 tests red); 2.2a's deletion of
  `rows.write` reached 3.1's tests. Both fixed by the coordinator with the red tests as the gate.
- **The operator: 5 verdicts** that changed the plan (below) and one live observation (the status
  bar's three readability defects, which became 1.11b).
- **Live runs: 2 finds.** The smoke's date check matched the runner's own timestamp lines (a script
  bug no offline test saw); the 1.9 lane's worktree run caught CPython folding a second cancel.
- **The coordinator's reads: 1** (a shim comment that repeated the refuted 501 claim).

## The operator's verdicts, verbatim

- "agreed, let's keep it clean as we go. Generally as you review the reviews we should just clean
  up defects during the build phase and not wait until later - big purtubations however should be
  discussed." → ruling (e).
- "a - okay, that tightens the loop somewhat and all of our reviewers are capable builders. b. Do
  we need a ruling on that actually? Why the cap? c. If necessary - what is this addressing? d.
  Addressing what issue? Why is this cross project?" → (a) and (d) lettered, (b) and (c) declined.
- "These sound like work-arounds? If so, can we tighten up the contracts for the APIs instead or
  provide new endpoints if there's a feature gap?" → the four nits rebuilt as contract rules
  (1.9, 1.10).
- "Status line looks good - exposing current tool call a good idea - can we track compactions done
  count? That's a good trailing indicator of a tuning problem." → 1.11, and the ruling that
  finished rows keep the count.
- "Schema looks okay" (the probes tables) and "Maybe the status line needs a bit more TLC and
  editing?" with the pasted bar → 2.2 as designed, 1.11b.

## Ruling proposals (done repeatedly, with results)

- **(f) Acceptance takes files by path; a file main moved since the lane's base is three-way
  patched; the suite runs on main before the commit, every time.** Twelve merges over a moved main
  today, two conflicts composed by hand, two cross-lane breaks caught only by the suite on main.
  The rule that held: never commit a merge on a lane's green run; run the suite on main.
- **(g) A fold chain closes when a review finds no defect, and that has taken at most three
  rounds.** Every chain today (1.9×3, 1.10×2, 1.11×3, 2.1×3, 2.2×3, 2.3×2, 3.5×2) closed inside
  three; the second round found fewer and smaller defects than the first in every chain.
- **(h) A contract between stories is written into the spec as the shapes the code emits, with a
  test that fails when the code emits a key the spec does not name.** 3.1 wrote §5a with that test;
  3.1a extended both and the test held; the 3.1 review used §5a as the oracle and found the six
  gaps before 3.2 was briefed.
- **(i) Two lanes writing the same module in parallel get three-way patched at acceptance, not
  serialised.** Fourteen parallel pairs today, zero lost hunks (the 2.2a review diffed one merge
  line by line and found every 2.3a line present and none duplicated). The cost is the two
  cross-lane breaks, paid once each at the suite on main.

Operator's verdict, 2026-10-29 closing interview: (f) through (i) all accepted; inked in `docs/EPICS.md` standing rulings.

## What cost

- The two cross-lane breaks: each under ten minutes, but each a coordinator fix that then needed a
  mention in the next review brief.
- The seed's clock: three reviews (2.4, 3.1, 2.4a's own) all found elapsed or first-seen on the
  wall clock before the fold made the readers pass their `now`.
- Reviews of DeepSeek work ran 20–38 minutes each; every one paid (the 2.2 review alone produced
  six rulings).
- One lane headed its report "Reviewer: Opus" while it was Qwen on ai1; the job record is the
  identity, never the report's header.
- Opus lanes: roughly 1.1M subagent tokens over five dispatches (quota); local lanes: zero quota.

## Open rulings

- Whether the collector runs the lost-job sweep on its own schedule (a crashed server's jobs stay
  `running` until another MCP server starts). Asked twice, not yet answered.
- The 2.4a review landed clean after this was written (Qwen/ai1, no defects: Epic 2 closed);
  3.1b (the node fallback) and 3.2 (the overview page) were in flight; their findings land in
  EPICS.
- Events outside the rulings: the seed's server pid is dead the moment the command exits, so the
  status line never shows a seeded job (documented, not fixed); a `wait_jobs` cancel awaits its
  inner unwinds without a bound (the same trade the runner makes).

## Closing step

`docs/EPICS.md` keeps one line per story with its commit, job id and review; this file holds the
story. Epic 3 continues from 3.2 (the overview page, Opus) with 3.3 and 3.4 briefed against §5a
after the page skeleton lands, and 3.6's live run closes the epic.
