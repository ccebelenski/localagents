# Retrospective: router phase 3 (2026-11-04)

Filed at the close of the phase-3 build day, before the operator's read. Counts from the records (the SDD ledger `.superpowers/sdd/2026-11-02-router/progress.md`, the job table, git); the operator's words verbatim; ruling proposals only for what binds lanes or needs the operator's decision.

## Counts (from the ledger, the job records and git)
- Main: dbb45b3 (phase-3 cut) → 30c2dc8 (fold 2) plus docs commits: 11 commits in range, 43 files, +6757/-244.
- Suite: 1178 → 1278 passed (1 skipped), +100 tests.
- Stories: Tasks 10, 11, 12, 13, 14a, 14b; folds 1, 2; fix rounds: Task 13 (1), Task 12 (1), docs (1).
- Opus lanes (Agent tool, worktrees cut by hand): 10, 11, 13, 12, 14a = 5 builds + 1 whole-branch review; 3 lanes killed within minutes (the isolation=worktree incident).
- Local jobs: 14 today, 1050 turns, 5.2 h of lane time (one timeout: fold 2's first lane at 200 turns/3 compactions, rescued).
  - Qwen3.8-Flash-Next (ai1/ai2): 9 jobs (2 builds, 2 fix rounds, 1 docs story, 1 docs fix, 1 review, 1 smoke, 1 cap)
  - DeepSeek-V4.1-Flash (dgx1-a): 4 (3 reviews, 1 fix round)
  - Qwen3.8-27B (llamacpp): 2 (1 review, 1 blind read)
- Reviews: 6 construct-it (10, 11, 12, 13, 14a, + the docs blind read) → defects reproduced: 10: 0; 11: 0; 13: 1; 12: 3; 14a: 0; docs: 2 false + 5 overstated.

## Finds by finder
- Operator (relaying the consumer): the record contract (served model/endpoint, routed_by), the clock from admission, the router owns the box rules, the dropped status, pool config is the operator's, routed_by.pool; "an exclude shape"; pools inert until declared.
- Coordinator (own): avoid_job; the Agent-tool worktree base incident caught by `git worktree list`; the scope error of the whole-branch review caught by counting commits; the §6 regex mis-anchor (own error, fixed twice).
- Builders correcting briefs: Task 13 (gate keys: the plan's names were wrong, the scorecard's bind); Task 11 (the liveness constant did not exist; nine tables reference jobs, not ten); Task 10 (tolerance relative vs absolute; the §6 scale sentence false; predict(ours) reads 0 as 1); Task 12 (admission inside the job's own task, not blocking start_job); Task 14a (queue entries are never deleted; a collector IntegrityError fixed in passing).
- Reviews: Task 13: the 120-char cut → tests gate False (Qwen); Task 12: avoid_job named when it did not bind, no position in the ticket, the phantom row after a failed drop (DeepSeek); Task 10: thin language cell (DeepSeek, possible → fold 1); Task 14a: facts_error too narrow (DeepSeek, possible); docs: the inverted tab sentence, the config-order claim (27B).
- Lanes' noticed items folded: bottleneck ignores foreign; _ceiling counts files_n 0; calibrate wait from enqueue; stall scan only on the wake; endpoint=auto+model; sampler counts queued.
- Suites: 0 (every defect was found by a probe or a read, none by the existing suite).
- Live probes by the coordinator: is_error=1 on a failing Bash call (ai2 smoke); the real store's copy migrated to v12.

## What cost
- EVENT: Agent-tool isolation=worktree cut three lanes from the pushed tip (51 commits stale); ~3 min of Opus each; rule amended in the hints.
- EVENT: fold 2's first lane capped at 200 turns / 3 compactions with step 1 uncommitted (a hanging test it kept re-running); rescued by committing the test files; the continuation finished in 47 min. The brief's "commit after each step" was not honoured by the lane before the cap.
- EVENT: the whole-branch review dispatched with the wrong base (0b960c2 instead of dbb45b3); caught before the reviewer read far, corrected by message.
- The run_agent tool description sits at exactly 2048/2048 chars: the next sentence added there breaks the pin.

## Open after phase 3 (to the handoff)
- UNPROVEN axes: nothing stores the baseline gate's result (Task 5's dry run writes no list); select's unproven filter is wired but inert.
- Deferred minors: the admit=now lost race leaves a dropped record; queue_view's waits use the endpoint's first served id; a cancel between admission and the first runner line reads 'cancelled while queued'; facts_error catches only sqlite errors; one try shares queue and scorecard facts in list_models.
- The operator's models.yaml has no pools: block and no calibration in the real store; both are the operator's.

## The whole-branch review (Opus, dbb45b3..30c2dc8, 63 tool uses, 14 min)
- 5 DEFECTS reproduced: D1 the bottleneck rule enforced by the decision but not by admission (the record lies: "queued (bottleneck slot)" for a job admitted at once); D2 a diverted job pinned to the lesser row; D3 a verdict on a dropped job becomes rejected evidence; D4 a resumed ticket inherits stalled/forced/priority; D5 admit=now vs the strict pool order creates and drops a job.
- 1 POSSIBLE: queue reads walk every admitted entry ever (168-188 ms inside the write lock at 100k entries).
- 8 NOTICED (two deferred: N5 the wait clock one poll early after a failed write; N7 admit=now can return a queued job).
- CLEAN: cancel at every await; the one-transaction job creation; both clocks; the pool cap vs foreign sessions; schema v12 under a v11 writer; the §4 contract from rows.
- None of the five defects was visible to 1278 passing tests or to the four per-story construct-it reviews: three of the five (D1, D2, D5) live BETWEEN modules (select vs queue; dispatch vs queue), where a per-story review has no oracle. The whole-branch review is where cross-module rules are checked; it earned its Opus cost.
- Fix wave 3: one Opus lane, 11 items ordered by severity, commit per item, 21 min, 21 tests; DeepSeek construct-it re-review: 11/11 addressed, no new breakage. Landed 2783870; suite 1298 passed, 1 skipped.
- Final counts: main dbb45b3 → 2783870, 16 commits, 48 files, suite 1178 → 1298 (+120). Local jobs: 16 (15 succeeded, 1 timeout), 1256 turns, about 6 h of lane time. Opus: 6 builds (10, 11, 13, 12, 14a, fix wave 3), 1 review, 3 killed lanes.


## The operator's verdicts (verbatim, the day's messages)
- "I have no real review of the first two phases other than what we corrected for already - real results will drive questions and checks as we go"
- "I would go with your best knowledge on the avoid_job answer since you're a coordinating model also"
- "We can add the pools: tag as long as it's ignored until needed, otherwise we do self-hard"
- "I have a bunch of quota left for Opus work, so I suggest we do as much parallel build in Opus and review on the local agents"
- "Go: all three now"
- "I'm AFK then till the work and reviews are done"
- On the consumer channel: "we are getting 'ground truth' from the one consumer that matters, an actual coordinator model"; "you're also a coordinator model using this so your own input matters too - but the 'other Claude Fable' has been working longer and more independently so it has a lot of history"

## Ruling proposals (only what binds lanes or needs the operator's decision)
1. For the operator to confirm: the admission gate, including §6's bottleneck rule, binds NAMED jobs (a second normal-priority named session on a two-slot llama.cpp prior waits; background takes the slot; `admit: force` overrides). Recorded as a True-now in §7 and in docs/router.md; reversible in a line.
2. For the operator: the two deferred items the wave left (N5 the wait clock one poll early after a failed admitted-write; N7 `admit=now` can return a queued job if the drop write raises) and the two lost-reason strings for a server exit: schedule or accept.
3. Binding lanes (already in the hints, 2026-11-04): worktrees are cut by hand from main; the Agent tool's isolation branches from the pushed tip.
Coordinator habits adopted without a ruling: check `git worktree list` after every dispatch; count the commits in a review's range before dispatching the review; one Opus whole-branch review per phase, because three of its five defects lived between modules where no per-story review had an oracle.

## Open rulings read out
- Pools in the operator's models.yaml: none declared (the operator's call; inert until declared).
- The operator's real store: no calibration run (`localagents calibrate <endpoint>` on a quiet box).
- UNPROVEN axes: no store for the baseline gate's result; the filter is wired and inert.
- The run_agent tool description sits at exactly 2048 of 2048 characters.
- The push: the whole router arc (phases 1-3) after the operator's read.

## Closing step
The shipped narrative moves out of the live plan: docs/HANDOFF.md carries the phase-3 note, docs/EPICS.md Epic 5 the phase-3 table and the True-now line; the plan file keeps its task text with its Decided notes; this retrospective is linked from the handoff.
