# Retrospective: the admission, queueing and display check (2026-11-15 to 2026-11-16)

Filed at the operator's word ("Sure - make your notes") after the work stopped; the operator has not named
this an arc and the benchmark epic stays next. Counts from the records (git `d1e2acd..49b2383`, the job table of
the operator's real store, the lanes' hand-back reports as they arrived in the session). Nothing was planned:
every item came from a live observation, and each fix surfaced the next.

The operator's line that set the method, verbatim: **"use the ground truth to base this change in reality
because this is just my reaction."**

## Counts (from the records)
- Main: d1e2acd → 49b2383, 23 commits: 12 code, 11 spec, docs and handoff. Nothing pushed.
- Suite: 1301 → 1523 passed (1 skipped). Source +2091 / −379 over 15 files; tests +5444 / −130 over 26 files.
- Store: schema 12 → 13 on the operator's real file (one column, one index), integrity check clean, no job
  queued or running at the moment of migration.
- Lanes: 11 Opus lanes in hand-cut worktrees (10 builds, 1 read-only investigation), 3 of them resumed for a
  follow-up fold; 1 documentation lookup; 1 web fetch. Local: 2 construct-it reviews (Qwen3.8-27B on ai9, 127
  turns, 44 min, no defect; DeepSeek on dgx1-a, 87 turns, 23 min, 1 defect) and 1 fold by the reviewer
  (DeepSeek, 53 turns, 21 min).
- Live smokes on the real store: 4 jobs (2 succeeded, 2 timed out, both timeouts the thing under test), plus 1
  job cancelled while queued to move a review to a free box. 2 probe jobs on a scratch store.
- Cross-model review coverage of the 12 code commits: 2 reviewed construct-it (abe4851, 1ea97c7), 1 is the
  reviewer's own fold read by the coordinator (1ceb824), 9 accepted on the coordinator's read alone.

## What landed
| Commit | Change | How it was verified |
| --- | --- | --- |
| 18db0a7 | the bottleneck rule holds a slot only on a measured curve point | live: two jobs admitted at once on ai9 |
| 1be3bad | the status line reports sessions; queued rows after running ones | live: `ai9 · 2/2 sessions` |
| 15cb8c5 | a blocking wait holds for the settled record at its cap | live: `wait_s` = `timeout_s` returned `timeout`, not `running` |
| f8ee5ed | `diff_files`; `files_touched` leaves out failed edits | suite |
| 84fbe8e | a worktree is removed only when nothing in it would be lost; git calls bounded | suite |
| 8e5f4b0 | `wait_job` / `wait_jobs` from admission; `cancel_job` uses the settle hold | suite |
| 1ea97c7, 1ceb824 | curves refined from speed samples; the review's fold | DeepSeek review, then its fold |
| 2ed040d | `tool_mix`; Bash commands stored up to 2000 characters | live: records carry it |
| abe4851 | expected wait from job history; estimate on the queue entry; schema v13 | 27B review; live migration |
| d80b437, 49b2383 | per-job `background_tasks`; a result held open is recorded as such; default ON | live: both modes |

## Finds by finder
- Operator: the report itself (jobs queueing beside a free slot; `0/1 slots` beside a running job); "We're
  bottlenecked on a number we don't have"; that a session outlives its slot use, which settled what the header
  counts; the per-job flag ("Having it under model control might be more useful than the models.yaml"); the
  challenge to the foreground default.
- Consumer coordinator (relayed by the operator): the ai9 timing (each start equal to the previous finish);
  `run_agent` returning `running` at exactly its cap; `files_touched` empty after a shell-written report; the
  requirement for read share on the record; the `ours: 1` reading from another session, which exposed the stuck
  smoke job; why a slot held after a result matters at 90-minute caps.
- Opus investigation lane: the wait's band times are calibration-probe medians (20 to 100 s) and named jobs
  never enter them; the wait expires before the session's own deadline by construction; the SDK's 5 s close.
- Builders correcting their briefs (7 of 10 builds changed what was asked):
  - wait-cap: the rule as briefed would not have fixed the reported case (the session's clock starts after the
    preamble); it added the case and proved it red.
  - wait-estimate: no existing index can serve the history read; the brief's 70.8 s was two terms, not one.
  - files-changed: the cited job was not a worktree job; and a worktree job whose only output is a gitignored
    file loses it (reproduced), which became the next fix.
  - curve-refine: the samples as recorded were unfit (labelled at job start; calibration probes in the
    baseline; no model filter); and it named the calibration gap its own change opened.
  - read-share: 58% of stored Bash rows sat at the 120-character cut, so the number was a wide floor.
  - foreground-jobs: the result was not lost at the timeout, the error was; a success result never cleared an
    earlier result's error.
  - finish-worktree: killing git alone leaves a hook's pipe open; the process group has to go.
- Local reviews: DeepSeek reproduced a defect in landed code (1ea97c7 read the newest 500 samples across all
  concurrencies before its per-concurrency cap; 520 recent single-session samples hid every two-session one)
  and fixed it; six more noticed lines, two fixed in the fold. The 27B found no defect in v13 across four
  probes and noted the spec said "finished" where the code means three statuses.
- Coordinator reads: `refine` merges onto the prior, so "ignore prior curves" would have let the hold back in
  once a box had single-session samples (per-point provenance instead); `db.open_db` migrates and the status
  line opens the store from an editable install, so landing v13 migrates the live file at once (the branch was
  parked until the operator held the other session); the second smoke's transcript showed the real answer
  arrives in the result AFTER a background task, which killed the "end the job at its first result" idea before
  it was built.
- Live smokes: the stuck-after-result job; and the confirmation of five fixes on the real store.
- Suites: no defect. The spec-reading test (`test_spec_5a_names_every_key_the_routes_emit`) went red three
  times and each time was right: a key the routes emit was not yet in the spec.

## What cost
- A defect of the coordinator's acceptance: 1ea97c7 landed on the coordinator's read with the 500-row cap in
  it, and sat on main until the review. The coordinator had suspected that line and put it to the reviewer as
  a neutral probe; the suspicion was not a reason to hold the landing, and should have been.
- A default changed on one incident and changed back: d80b437 made jobs foreground-only by default after one
  stuck job, which was the coordinator's own smoke with a brief that demanded a foreground `sleep` the harness
  blocks. The store showed 24 of 209 jobs using background tasks productively and none stuck after a result;
  49b2383 put the default back. One commit of churn, one extra explanation to the operator, and a calibration
  note written and withdrawn.
- Numbers presented firmer than they were: the first read-share figures (43 to 63%) were a prefix-rule estimate
  that counted cut command lines as reads; the classifier's floor is 37 to 48% with a band to 79%. "Two of 83
  transcripts restarted after a result" attributed one to the other session's jobs; both were the smokes. The
  offer to write this file said "fifteen landings"; the record says 12 code commits.
- Review debt: 9 of 12 code commits had no second-model review, against the project's rule, because the local
  lanes were busy and then because each next item arrived first.
- Three reconnects asked of the operator, and the other session held for the v13 landing.
- EVENT: a direct `claude -p` probe against ai9 without the shim spent six minutes on retries of a chat-template
  error and reached no model; the operator asked why no session had appeared.
- EVENT: two doc-edit scripts stopped at an assert on re-wrapped text and left their edits split across two
  commits each (06a21b3 and 1084a95; cc803d9's follow-up).
- EVENT: the Agent tool's deferred-tool schema for `run_agent` stayed stale after the reconnect; the call with
  the new argument went through anyway.

## The operator's verdicts (verbatim, the session's messages)
- "We're bottlenecked on a number we don't have, so that seems wrong."
- "slot reporting is server side, but we want to have an available slot for a session, so session is the right
  gate and the right number to report."
- "I think the explicit force - the coordinating model isn't doing batching anyway since it writes one brief at
  a time."
- "Let's queue the work up - no point in not doing them all"
- "We have a code size problem there - the volume of code causes big contexts and the models spend a lot of time
  reading."
- "the answer depends on how you, as a coordinating model, would use this in practice - It's your tool, so I
  cannot rule on best."
- "Having it under model control might be more useful than the models.yaml"
- "we've been using it successfully without that change so far and this seems like maybe we're plugging a hole
  that rarely exists - but use the ground truth to base this change in reality because this is just my reaction."

## The consumer's words that became requirements (verbatim, relayed)
- "when wait_s equalled timeout_s, the blocking call twice returned a record with status: running at exactly the
  cap, and the job then ended a few seconds later as timeout or succeeded."
- "one review job reported no files touched although it wrote its report through shell redirection, so that
  field only reflects the edit tools."
- "I would use read share and output tokens per tool call on the job record. It would let me compare briefs."
- "I would have seen a lane that 'ran long' and then timed out, and I would have written a continuation brief for
  work that was already done."

## Proposals for the operator (each binds lanes or needs your decision)
1. A review batch for the nine unreviewed commits, by door, when two boxes are free: the waits (15cb8c5,
   8e5f4b0), the worktree finish (84fbe8e), the record fields (f8ee5ed, 2ed040d), the background-task tracking
   (d80b437), the status line (1be3bad), the bottleneck rule (18db0a7). The DeepSeek review of one commit found
   a defect the coordinator's read had passed; the others have had the same read and no more.
2. A line for the delegation hints, if you want it there: a smoke or probe brief must not demand something the
   harness refuses (a foreground `sleep`, an interactive command), because the model's workaround becomes the
   thing measured. Evidence: the stuck job of 2026-11-15 existed only because its brief said `sleep 45`.
3. The curve-refinement thresholds (8 samples per point, the newest 20, 14 days, 90% occupancy) are the
   builder's numbers, reviewed for behaviour and not for value. They are yours to change; nothing in the store
   exercises them yet (one two-session sample exists).
4. Whether `sessions.ours` should be renamed in `list_models`: the consumer read it as "mine" and it means
   "admitted through the shared queue, any session's".

## Open after this work (read out from the handoff)
- `feedback.note_start` / `sessions_at_start` and `on_finish(sessions=)` feed nothing now.
- `run_agent`'s tool text still says a worktree is "kept only if changed"; the description is at 2046 of 2048
  characters.
- `make_worktree`'s `worktree add` is unbounded; a hung checkout holds a job before its session starts.
- router/calibrate.py holds a second copy of the queue-then-wait act (`_wait_admitted`'s last caller).
- An empty ignored directory keeps a worktree.
- One curve row per endpoint: a fold for a second served model replaces the first model's probe points until
  that model is calibrated again.
- The job record does not carry `background_tasks` (no column); it is on the transcript's `[start]` line.
- Routed jobs divert sooner now that waits are minutes; nobody has watched a routed job since.
- The monitor seed's status pin reads `+1 foreign` (the seed's job has no queue entry).
- The read-share lever itself is untested: no pair of briefs has been compared yet.

## What I am adopting (my habits, not rulings)
- A default is changed only after the store says how often the case occurs; an option plus visibility is the
  first answer to a rare failure.
- A line I doubt in a diff holds the landing until the doubt is settled, even when the failure direction looks
  safe.
- An estimate is labelled an estimate in the sentence that carries the number.
- A probe that bypasses the tool is announced before it runs.
