# Retrospective: the router's live test and the close of Epic 5 (2026-11-04, evening)

Filed at the operator's close of the epic ("this epic is done after your last steps you outlined"). Counts from the records (the SDD ledger `.superpowers/sdd/2026-11-02-router/progress.md`, the job table of the operator's real store, git); the operator's and the consumer's words verbatim; ruling proposals only for what binds lanes or needs the operator's decision. The phase retrospectives are `2026-11-04-router-phase-3.md` and the phase-1/2 notes in the handoff's history.

The operator's one-line retrospective, verbatim: **"real-ground truth beats canned tests."**

## Counts (from the records)
- Main: 73253b0 (the phase-3 close-out) → the commit this file rides on: 7 small commits in one evening, every one from something the live test showed.
- Suite: 1298 → 1301 passed (1 skipped).
- The operator's real store: migrated to schema 12 on the reconnect; four scorecard rows calibrated (llamacpp × the 27B, dgx1-a × DeepSeek, ai1 and ai2 × Flash-Next); 4 complete core runs, 56 of 56 probes pass, 72 calibration jobs all succeeded; one backup before (from /tmp, rescued to t/live) and one after.
- Jobs of the evening on the real store besides calibration: 7 routed (all succeeded), 17 named (16 succeeded, 1 dropped by the queue test, 1 timeout), the consumer's seven read-only probes among them.
- Local models as lanes tonight: the 27B (blind reads of the coordinator's scripts, slot fillers), Flash-Next on ai1 (one blind read). No Opus lane; no build lane. Every change was the coordinator's own, each under the suite.
- Consumer acceptance: approved for use; 3 findings (1 defect fixed, 1 text fixed, 1 known).

## What the live test proved
| Check | Result |
| --- | --- |
| store migration on the real file | v12 on reconnect, clean; a copy had been migrated the day before |
| calibration, four endpoints | 56/56 probes pass; speed factors 0.94, 1.84, 1.32, 1.42; no measured curve bottlenecks at two |
| d3 managed | `tools/d3.sh` start 25 s to ready, stop clean, the loader-failed branch reports and stops |
| first routed job | llamacpp; the record names endpoint, served id, `routed_by`; the ticket complete |
| the tie-break after the ruling | "tie broken by predicted speed"; the second routed job spread to ai1 |
| a full endpoint, a third named job | queued, rank 1, on the status line |
| `admit: now` on a full endpoint | `would_queue` at once, position 2, no job row |
| cancel while queued | `dropped`, "cancelled while queued", never started, no session |
| the consumer's seven probes | routing, steering by served id + endpoint and by endpoint alone, `avoid_job`, `admit: now`, the clock from admission: all passed |

## Finds by finder
- Operator: `localagents --help` listed two of nine subcommands; "who installs the weights?" (nothing in the docs said the first start downloads 9 GB); the d3 management gap; "ai1 and ai2 are running the same model" and "a lot of backends allow aliases for the models - the name alone isn't sufficient" (the row-identity gap, deferred); the ruling that config order never decides; the monitor's d3 exposure kept to running state.
- Consumer (live acceptance, 7 probes): `model: auto` + `endpoint: ai2` silently dropped the forced endpoint (DEFECT, fixed); `avoid_job` on a steered job excluded its model where the text said refused (the text was wrong); the queue estimate is a static prior (known); a probe brief worded "should be refused" made the 27B decline to run (a rule now).
- Coordinator reads of the live records: the authored probe set gives every row IDENTICAL capability cells (the set measured the fleet's floor, not the models); the §6 tie-break read 1.0 for every idle row so config order chose and the reason line said otherwise; the `SystemExit: 2` health text; the health-wait variable unvalidated was confirmed from the 27B's read; the dropped job's queue entry is deleted by design (the coordinator first wrote the opposite).
- Lanes: the 27B's blind read of `tools/d3.sh` found the unvalidated wait (TRUE, landed); the 27B's claim that `setsid` forks under interactive job control (FALSE, constructed with a pty: the pidfile holds the server, its own session leader); Flash-Next's claim that `accept.sh`'s `/tmp` pattern misses `+/tmp/foo` (FALSE: its own last tool call shows the match; it reported the opposite of its output).
- Suites: 0. Canned tests found nothing tonight; every finding came from a live record, a live probe, or a reader.

## What cost
- Three corrections of the coordinator's own words, all in the ledger: the "second llamacpp run" was dgx1-a's; the arithmetic claiming a busy 27B beats every idle row (the code was right, the sentence wrong); the dropped job's queue entry "persists" (it is deleted, by design).
- The backup command given to the operator named a directory that did not exist; the operator saved to /tmp (tmpfs) instead; rescued to t/live before a reboot could take it.
- The run_agent tool description sat at exactly 2048 characters, so the one-line text fix for the consumer's finding 2 cost five other lines their words.
- EVENT: the Agent-tool/fillers' `sleep 120` hit the lane's Bash timeout once each (one Bash error per filler); harmless, the slot was held.

## The consumer's verdict (verbatim, relayed by the operator)
"Approved for use, with two steering defects you should know about before anyone relies on the refusals. Seven read-only probes ran from a hand-cut worktree, now removed; main is untouched." Findings 1-3 and the lane note are in design §4 as the consumer's live acceptance block, verbatim.

## The operator's verdicts (verbatim, the evening's messages)
- "SO I think we really can't close the arc until we've had a successful test, and then some working truth."
- "Before we start local data recalibration - A clean install would not have that data already. So where are we with the benchmark based calibrations?"
- "I don't think any of those are critical except running state - the other two would show problems, but in general I don't expect a router to hit high volume unless it is shared across many claude session."
- "ai1 and ai2 are running the same model - the only different would be performance"
- "Addiitional a lot of backends allow aliases for the models - the name alone isn't sufficient."
- "We can try it with the tie break - the config order shouldn't ever matter because we never said it did."
- "No push because we are not complete, but this epic is done after your last steps you outlined. The retrospective is that real-ground truth beats canned tests."

## Ruling proposals (only what binds lanes or needs the operator's decision)
1. Binding lanes (in the hints, 2026-11-04): a probe or sentinel brief never states the verdict the coordinator expects; it says only what to do if it runs.
2. For the operator, when the benchmark arc is shaped: the authored set alone cannot separate these four models; the arc's first step is the licence table, its acceptance a gradient at least one row fails.
3. For the operator: the row-identity gap. A model swapped behind an unchanged alias leaves a stale row nothing can see. Proposal: record the backend's artifact facts beside the served id at calibration and raise the existing needs-recalibration notice on a mismatch; a notice, never an inference.
Coordinator habits adopted without a ruling: read a lane's finding against its own last tool call before believing it; construct a claim about a shell script with a real pty before acting; name the deciding tie-break level in every reason line.

## Open rulings read out (to the handoff)
- Deferred from the review and the live test: N5, N7, the two lost-reason strings, the expected wait as a static prior (14 s vs 83 s, 9.9 s vs ~75 s), the unreliable rule blind to harness-host load, UNPROVEN has no store.
- Not built: the benchmark tiers and `tools/probeset/build.py`; a read-only CLI readout of calibrated rows; d3 decision latency and queue depth on the monitor; a systemd unit for d3; the row-identity facts.
- The operator's models.yaml: `auto` declared (by the coordinator at the operator's word); no `pools:` block.
- The push: held. "No push because we are not complete."

## Closing step
The shipped narrative moves out of the live plan: docs/HANDOFF.md carries the live-test note, docs/EPICS.md Epic 5 the live-test table and the True-now line reading done; the plan file keeps its task text with its Decided notes; this retrospective is linked from the handoff. The design's §4 holds the consumer's acceptance verbatim, §6 the tie-break ruling. Epic 5 is done; what follows it is a new epic, not a phase.
