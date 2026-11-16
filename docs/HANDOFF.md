# Handoff

Resume note for the next session. Rewritten at every session close; history is in git.

**2026-11-15: a live check of admission, queueing and display; four fixes landed, one PARKED.** The operator
saw jobs queue beside a free slot and a status header reading `0/1 slots` beside a running job. Landed on main
(suite 1363 passed, 1 skipped; nothing pushed), each built by an Opus lane in a hand-cut worktree and accepted on
the coordinator's read of the diff (the local review lanes were busy with ANAS, so no cross-model review ran):
the bottleneck rule holds a slot only on a MEASURED curve point (18db0a7; design §6 True-now; the curve carries
per-point provenance in its JSON); the status line reports sessions over the capacity admission gates on, queued
rows after running ones (1be3bad); a blocking wait holds for the settled record when the job's session is ending
(15cb8c5, `jobs.held_at_cap`, 120 s backstop); `diff_files` names a worktree job's changed files and
`files_touched` leaves out failed edits (f8ee5ed). **PARKED: branch `wait-estimate` in
`/home/ccebelenski/projects/claudelocalmcp-ew`** (main merged in at 81a6183, suite 1384 passed, 1 skipped): the
expected wait read from job history, the estimate on the queue entry, SCHEMA v13 (a column and an index). It must
not land while another session's server runs: the tool is an editable install of this checkout and `db.open_db`
migrates on open, so the status line's next refresh would migrate the live store under a v12 server. Land it
(`tools/land.sh wait-estimate -F msg`) only at the operator's word that the other sessions' jobs have drained,
then place its stale doc sentences (router.md:435 "version 12", :278, :349; design spec §6/§7 queue_entries
listing; this file's "schema v12") and have every session reconnect. Until a session reconnects its server runs
the old admission and wait code. **Second round, same day, at the operator's "you can build":** a worktree is removed only when nothing in it
would be lost, every finish-path git call is bounded, one copy-index reader (84fbe8e); `wait_job`/`wait_jobs`
count their cap from admission and `cancel_job` uses the settle hold (8e5f4b0); curves are refined from speed
samples under a fail-closed rule and belong to their served model (1ea97c7; design §7 True-now). Suite 1407
passed, 1 skipped. That closes the first three and the fifth of the items listed next, and the gitignored-report
loss. Still open after it: `feedback.note_start` / `sessions_at_start` and `on_finish(sessions=)` feed nothing
now (remove); run_agent's tool text still says a worktree is "kept only if changed" (its description is at 2044
of 2048 characters); `make_worktree`'s `worktree add` is unbounded; calibrate.py:481 holds a second copy of the
queue-then-wait act (`_wait_admitted`'s last caller); an empty ignored directory keeps a tree. The operator will
say when ai9 frees up (a second-model review of the v13 branch waits for a box) and restarts the MCP servers
only at a quiet time. The list as it stood before the second round: `wait_job` counts its cap from the call, not from
admission; the runner's git helper has no timeout (a hung git in `finish_worktree` never settles the job);
`cancel_job`'s 15 s wait is shorter than the SDK's worst-case close; `Curve.refine` is called by nothing in
production (speed samples are written and never folded into a curve); a worktree job whose only output is a
gitignored file (a review's `t/REVIEW.md`) is judged unchanged and its worktree, report included, is removed;
routed jobs now divert sooner once v13 lands (waits are minutes, were seconds); the monitor seed's status pin
reads `+1 foreign` (the seed's job has no queue entry).

**2026-11-04 evening: the router LIVE-TESTED on the operator's real store and ACCEPTED by the consumer; Epic 5
DONE.** Main at the commit this note rides on; suite 1301 passed, 1 skipped; nothing pushed ("No push because we
are not complete, but this epic is done"). The operator ruled the arc could not close on tests alone ("we really
can't close the arc until we've had a successful test, and then some working truth"), reconnected the server (the
real store migrated to v12), calibrated the four live endpoints with the shipped core set (56/56 probes pass), and
the coordinator added the `auto` entry to models.yaml at the operator's word, built `tools/d3.sh` (start, stop,
status, log) and ran the routed, queued, refused and dropped cases end to end; the consumer coordinator session
ran seven read-only probes and approved for use with two steering findings, both folded the same evening (a
router `model` with a forced real `endpoint` is now bad_argument; `avoid_job`'s text says routed or named). Two
live findings changed the design: the authored probe set gives every row IDENTICAL capability cells (the fleet's
floor, not the models; the benchmark arc's concrete case), and the §6 tie-break read 1.0 for every idle row so
config order chose; the predicted speed now divides the curve by the row's measured speed factor and the reason
names the level that decided. Also: `localagents --help` lists every subcommand; the weights' download is
documented; the health-wait guard. Open (the retrospective's list): N5, N7, the lost-reason strings, the expected
wait as a static prior, the unreliable rule blind to harness-host load, UNPROVEN without a store, the row-identity
gap behind model aliases (proposal recorded), the benchmark tiers and build tool, a read-only calibration readout,
d3 latency/depth on the monitor, a systemd unit; no `pools:` block. Next: a NEW epic, shaped by the operator, and the operator named why the push is held: "nobody can use the
product without real calibration - otherwise it's just a dumb router" (a clean install's calibration must
separate models: the benchmark set). Retrospective: docs/retros/2026-11-04-router-live-test.md (the operator's line: "real-ground
truth beats canned tests").

**2026-11-04: router phase 3 BUILT, reviewed and landed; the router is feature complete.** Main at the
commit this note rides on; suite 1298 passed, 1 skipped; nothing pushed (the operator gates the push: "nothing
gets pushed until we're feature complete on the whole", now met pending the operator's own read).
The day: the operator relayed four consumer requirements from the other coordinator session and
accepted two queue proposals (all in design §4/§7 as dated blocks); said "go" for wave 1 and went
AFK ("till the work and reviews are done"); Tasks 10-14 were built by Opus lanes in worktrees cut
BY HAND from main (the Agent tool's isolation=worktree cuts from the pushed tip, 51 commits stale:
three lanes killed within minutes; rule in the hints and memory), each reviewed construct-it by a
local model (DeepSeek, the 27B, Qwen Flash), fix rounds by the reviewer's model, two folds on
local lanes, the docs by Qwen with a blind read by the 27B, then the Opus whole-branch review
(dbb45b3..30c2dc8: 5 defects, 1 possible, 8 noticed; three of the defects lived BETWEEN modules
where no per-story review had an oracle) and fix wave 3 on Opus with a DeepSeek re-review. What
exists now: select.py (the §6 decision), queue.py (the database queue, atomic admission, pools,
the dropped status, schema v12), dispatch.py (route, the one-transaction job, the admission loop,
tickets, queue_view, the stall scan), feedback.py (gates, speed samples, verdicts), the exposed
facts on list_models and the status line, docs/router.md complete. Open (the ledger): N5 (the
wait_s clock one poll early after a failed admitted-write), N7 (admit=now can return a queued job
if mark_dropped raises), UNPROVEN has no store, the run_agent description sits at the 2048 cap,
the operator's real store has no calibration and no pools: block. Next: the operator's read of
phase 3 (the review's and the ledger's words are the map), the retrospective
(docs/retros/2026-11-04-router-phase-3.md, drafted), then the push of the whole router arc. Retrospective: docs/retros/2026-11-04-router-phase-3.md.

**2026-11-03, early: router phase 2 BUILT and landed; the live deliverable met.** Main at the commit this
note rides on; suite 1178 passed 1 skipped; nothing pushed (the feature-complete rule: phases 1-3 together).
Tasks 6-9 of docs/superpowers/plans/2026-11-02-router.md were built by local lanes in worktrees (Task 9 split
into 9a runner, 9b/9c/9d probes), each reviewed construct-it by another model, with an Opus whole-branch review
and a two-lane fix wave; every ruling, deviation and deferred minor is in the SDD ledger
(`.superpowers/sdd/2026-11-02-router/progress.md`, git-ignored; keep it). What exists: the scorecard
(src/localagents/router/scorecard.py: rows by endpoint name x served id, the evidence readers over calibration
results, verdicts and gates, the pass by interval censoring, notices), capacity in sessions and the throughput
curve (capacity.py), calibration-flagged jobs and `server.start_job` (the cal mark on the status line and the
monitor), the shim's thinking/effort levers, the calibration runner (calibrate.py: manifest loader, plan,
estimate, throughput probe, probe jobs in fresh materialised repositories, resume, locks, the facts probe five
ways, the pass at the end), `localagents calibrate`, the `calibrate` (background) and `scorecard` tools, the
stub MCP server (stubmcp.py) and fourteen authored probes under src/localagents/router/sets/ with their
checkers proven on stub and reference by the suite. LIVE: the reference row (the dense 27B on llama.cpp) was
calibrated end to end under a scratch store (t/live, not the real state dir): 14/14 pass after the fix wave,
571 s for the first thirteen, cells complexity 3, tool_use 3, computer_use 1, judgement 3, software_dev 2
(python 2); facts thinking on by default, the switch honoured, effort inert; the manifest now carries that
row's measured nominals and reference_tps 113.2. The operator's REAL store has no calibration yet: run
`localagents calibrate <endpoint>` on a quiet box when wanted. Not built: the gates and verdicts of spec §9
(so completeness, adherence and ceiling cells stay empty), routing itself (`run_agent(model="auto")` is still
`not_routable`), the benchmark tiers and the build tool. Deferred minors (ledger): `Job.summary()` hides the
calibration flag; endpoint_requests totals include calibration jobs; the levers note is dropped at the requests
INSERT; the calibrate tool's run_in_progress names no run id for an incomplete row's lock; a skipped lever's own
reason is lost; two simultaneous FRESH launches can each open a run; P3 (one high-band accept lifts a cell) and
whether a reject on an axis labelled 0 should count are the operator's calls. Next: phase 3 (Tasks 10-14:
selection, the queue, dispatch, feedback, surfaces) on the operator's go, then the retrospective and the push.

**2026-11-02, late: router phase 1 BUILT and landed.** Main at the commit this note rides on; suite
977 passed, 1 skipped (the live d3 test, run with `LOCALAGENTS_D3_URL`); nothing pushed (the
operator: "Nothing gets pushed until we're feature complete on the whole"). Phase 1 of
docs/superpowers/plans/2026-11-02-router.md (Tasks 1-5) was built by local lanes in worktrees
(Qwen3.8-Flash-Next on ai1/ai2, the 27B on llama.cpp), each reviewed construct-it by a different
model (DeepSeek on dgx1-a, the 27B, Qwen) before `tools/land.sh`; then a final whole-branch
review by Opus (4 Important, several minors), ONE fix wave (12 items, Qwen/ai1), one scoped
re-review (DeepSeek, all addressed), two coordinator folds. Landed: schema v10 + v11 (the
router's tables; the operator's real file is at v11 now), the definitions document (d3 takes a
score's criteria as an ORDERED LIST), `backend: router` entries (invalid entries marked, never
raised from load), the d3 client, `localagents d3 serve` (the `router` extra: torch, transformers,
accelerate; default port 8766, the monitor keeps 8765), `localagents router` (definitions get/set/
dry-run, score-only preflight), docs/router.md, README `### router`. Live: the d3 server runs on
the Ada GPU in ~22 s; all 135 stored briefs scored through the real client; undetermined rates at
the shipped bars (specification 100%, context_load 97%, computer_use 73%, complexity 67%,
software_dev 40%, tool_use 18%, language 16%) are input to the calibration set spec's thresholds.
The SDD ledger with every ruling and deferred minor: `.superpowers/sdd/2026-11-02-router/progress.md`
(git-ignored; keep it). Next: phase 2 (Tasks 6-9: scorecard + pass, capacity, calibration-flagged
jobs, the runner) on the operator's go; the calibration set's content (its spec is approved) feeds
Task 9. Open from the reviews (deferred, in the ledger): `_SUMMARY_HIDDEN` hides priority/
calibration/gates until Task 12; the monitor's collector treats a router entry as real until
Task 14; d3 without the extra reports "SystemExit: 2" on /health; `name:` key in an entry is a
TypeError; a real served id equal to the router's name listed first captures it.

**2026-11-02, the router design day.** Main at the commit this note rides on; nothing pushed since
cbb026c (the operator gates the push); no worktree, no lane running. In order: the d3 categorizer
spike (docs/spikes/2026-11-01-d3-categorizer.md, with its True-now on the complexity baseline); the
router design brainstormed and written (docs/superpowers/specs/2026-11-02-router-design.md), reviewed
the same day by Opus (design), Qwen/ai1 (plan) and DeepSeek/dgx1-a (schema) and FOLDED, with the
operator's calls (sessions not requests; fail-and-resubmit, no re-route; unknown cell = zero; admit
queue|now|force; stalled session = probe + notice); the plan in three phases
(docs/superpowers/plans/2026-11-02-router.md: infrastructure 1-5, calibration 6-9, routing 10-14);
the probe-as-job spike (docs/spikes/2026-11-02-probe-as-job.md); the calibration set spec
(docs/superpowers/specs/2026-11-02-calibration-set-design.md), approved as written "until we have
ground-truth". Execution method: subagent-driven, the lane process. **No build "go" has been given.**
Resume: read the two specs and the plan; phase 1 starts on the operator's go; Epic 5 in EPICS is
written by plan Task 14.

## 2026-10-30, afternoon (the 3.9 codebase review pass; the operator watching the page)

**State.** Main at the commit this note rides on; suite at 690; nothing pushed (the push is the
operator's call). Schema is at version 8 (v7: the `jobs_finished_at` index; v8: the
`transcripts` table). Every finding of the day is in `docs/EPICS.md` under "3.9 Codebase review
pass" (doors A–E) and "3.7 Presentation pass" (3.7t–3.7w), each verbatim, each with its
disposition and the fold's job record. Memory: `~/.claude/.../memory/monitor-mvp-state.md`.

**What happened today, in order.**
- The operator: "push a full codebase review pass to ai1" → doors A (MCP request path), B (jobs,
  storage), E (every statement's query plan) by Qwen/ai1; C (monitor server) by DeepSeek/dgx1-a;
  D (page) by Qwen/ai2 — read-only, probes under `t/`, every finding verified by the coordinator
  at its line, the reviewer then fixing its own findings (ruling (a)), landed with
  `tools/land.sh`, each fold reviewed by a model that did not build it.
- Landed folds: A1 (`wait_jobs` dedup); B1–B3 (runner's finish over a missing tree, the kept note,
  `run_cwd` on an absorbed cancel); E1–E3 (schema v7, the live-jobs predicate, plans pinned in
  `tests/test_query_plans.py`); C1/R1/R2/C2/T1/T2 (typed history-cache keys bounded to the five
  page ranges, seed refusing a live collector's database, the 7 d history read as three bounded
  statements, byte-identical body, reviewed clean by Qwen); D1–D5 (the endpoint page's liveness
  through an explicit `onSnapshot` hook — it had watched a dead span since 3.7r and never
  refreshed; the vanishing-card throw; `tools/smoke/drive.mjs`, `range.js`).
- Stories from the operator's words: 1.12 transcripts in the database (schema v8, written once at
  completion, readers row-first, no purge rule: purge and an analytics path are open items, "TBD");
  3.7u no ellipsis on the cards (Opus: a measured column floor that only rises; capped lines
  scroll barless; `ep-line` on every one-line card text) and its fold 3.7u-fix; 3.7w the tab's
  memory (`tools/smoke/mem.mjs`: the page owns no leak).
- Incident: a 3.9B probe forgot the state-dir redirect and `prune(1)` deleted 66 real job
  directories (19 database jobs' transcripts; the operator: development data, nothing valuable).
  Fix: `t/conftest.py` committed (the `.gitignore` re-includes it), so every probe lane carries
  the redirect. The transcripts table (1.12) is the durable copy from now on.

**2026-11-01, the day (the operator present): README for the push; the width bug.** Main at the
commit this note rides on, suite 912, clean, nothing pushed, no worktree, no lane. In order: 3.13
(the seed states) DEFERRED, not built — the operator: "I think the page work is done - The saying
that comes to mind is 'perfect is the enemy of good enough'"; ruling (r) stands, the next page story
that needs a seeded state grows the seed by that state (EPICS 3.13 True-now). Then 3.14, the README
made solid before the push (EPICS 3.14, the operator's words verbatim there): rewritten by the
coordinator from a docs-versus-code audit (quick start 1-7, a Backends section with the flag
defaults quoted from `llama-server --help` — `--jinja` and `--slots` are on by default, `--metrics`
and vLLM's `--enable-prompt-tokens-details` are the ones to add — the monitor described as it is
today), a Qwen/ai1 audit of it (WRONG 2 / MISSING 2 / CLEAN 125, folded), and three voice passes on
the operator's rulings: "if it sounds like an LLM wrote it, people don't read it"; "I notice your
giving examples based on my specific set-up - that's not allowed" (every host, IP, build and slot
value of the fleet replaced by neutral examples; the seeded-database section moved to CONTRIBUTING);
"'declared, never guessed' is exactly the language of the LLM" and "existing text ... was LLM
written also" (the whole file swept: no slogan antitheses, bold-labelled paragraphs or
colon-plus-inventory sentences; two blind reads by other models as the check). Then the bug the
operator found on the live page ("very wide cards for no obvious reason"): 3.10g(1), the job's
activity line out of the card floor, landed 825d80c with the floor oracle as the evidence
(820 → 645 at 1538 px, two columns), the seeded floor sequence and wide.js green, reviewed clean by
DeepSeek (EPICS 3.10g); the operator: "Confirmed fixed on the width". Open items, none blocking:
`ollama` is an accepted `backend:` value the README does not name; `localagents --help` lists only
serve/statusline; the `.ep-line` set (a long `.ep-error`) can still widen the floor for the page's
life; Epic 4 purge and analytics; the operator's own readings (the Firefox scrollbar after-reading,
the picker's "00 to 23" notice). The operator's `vllm` shim on this box is broken (ModuleNotFoundError),
their machine. The operator found the 672-line README unreviewable ("TLDR"); it is split, the front page is 299 lines and the rest is docs/using.md, docs/monitor.md, docs/shim.md (EPICS 3.14 True-now). PUSHED 2026-11-01 evening at cbb026c (389 commits, 0100fb6..cbb026c), on the operator's "put it up - let the world have it". NEXT (superseded by the push): the operator reads the README and the diff since f676d0e, then pushes (the push
is theirs). The night note follows.

**Paused for the night 2026-11-01 (the operator: "I'll refresh you in the morning").** Main
246dbd1, suite 911, clean, nothing pushed, no worktree, no lane. The arc is closed and the retro
filed (docs/retros/2026-10-31-presentation-review-usability.md); its proposals are decided.
Rulings now (a)(d)(e)(f)(g)(h)(j)(m)(n)(o)(p)(q)(r): (i)(k) merged into (f), (l) retired, (q)
the coordinator owns its habits (the operator flags the ones to think harder about), (r) the seed
holds the states seen live. The coordinator's rule set was reviewed 2026-11-01 (the delegation
hints cut from 351 to 196 lines, a dated backup beside the file; memory merged to 14 files).
NEXT (superseded the next morning: 3.13 deferred, see above), on the operator's "go": story 3.13 the seed states (design under EPICS 3.13; Qwen/ai1 for
the seed and its pins, then a DeepSeek review with the smokes run live). Waiting for real work on
the page: the 3.12b scrollbar after-reading, 3.10g(1), the picker's doubled end-time (deferred).
The evening note follows.

**Lanes at the last note (2026-10-31 evening, the operator present).** The operator's hands on
the live picker found 3.12f: "when the date is not selected ("any") none of the other parts
function" — the spec's own §5 sentence ("disabled until a date is chosen"), built faithfully by
3.12c; the operator: "there's no point in limiting it. Go for it." Landed 9866bb1 (Qwen/ai1, 72
turns): a third state shape "HH-HH" (every day, hours inclusive), the hour selects never disabled,
the all-days hour list under "any", the summary "09 to 11" alone, two 3.12c pins moved, smoke (c4),
suite 907; the spec's §3/§5 carry True-now lines. The DeepSeek review of 3.12f (EPICS 3.12f)
found one defect, inherited from 3.12c: no sequence of the hour selects could empty the state
once both hours were set (the read-back put the day's edge into a cleared select and the next
"any" re-composed "00-23"). The reviewer's own fold landed ebc9706 (the read-back only: a held
from of "00" and a held to of "23" show as "any"; grammar, writes and summary unchanged; suite
911). The Qwen/ai1 review of that fold found no defect (every escape order, the focus rule,
the live page; EPICS 3.12f): the 3.12f chain is closed, no lane is running, suite 911 at
ebc9706, nothing pushed, `git worktree list` is one line. The operator closed the arc ("Yes, let's close the
arc"): the retro is filed at docs/retros/2026-10-31-presentation-review-usability.md (the DRAFT
is gone) its two proposals decided 2026-11-01: the seed inked as ruling (r) and cut as story 3.13 (design in EPICS, awaiting go), the lifted probe declined as a habit (three habit-shaped ones withdrawn to the hints under the new ruling (q): the coordinator owns its working habits), the open items
read out, and the EPICS-history closing step proposed. 3.12f is ticked. Open on the operator's side: their
hands on the picker again (the fold's notice: a "00 to 23" state reads back as two "any"
selects; only the summary tells it from an empty filter), the AFTER reading of the Firefox
scrollbars, 3.10g(1) on its own go. Then the retro on their word. The late-afternoon note follows.

**Lanes at the last note (2026-10-31 late afternoon, the operator present).** The operator ticked
3.12a, 3.10c and 3.10e on the live page and gave the go on "the open build work now that all lanes
are back" (EPICS 3.12, verbatim). Landed since, in order: 3.12d the number box reads the cell's own
unit (5d5479b, Qwen/ai1), 3.12e short failure sentences with a `hint` for list_models/ModelUnavailable
(746de21, Qwen/ai2), 3.12b the two-level table refresh (86204ad, Qwen/ai1; the spec's job.js item
was wrong and is not built), 3.12c the started picker (2646b28, Qwen/ai1; its smoke caught its own
captured-closure bug), the DeepSeek review of 3.12d+e and its coordinator fold (8051db5: the ms
cells' space, decimal durations, two pins), the DeepSeek review of 3.12b and its coordinator fold
(3aa52c4: the events section persists, the hash carries note/empty), the DeepSeek review of 3.12c
and ITS fold by the reviewer (a5b62dc: faceting by name, the clock's day in the hash, the events
cache). Suite 901 at a5b62dc. The Qwen/ai1 review of the 3.12c fold found no code defect (one false comment,
corrected at dc6dbc0): every chain of the round is closed and no lane is running. vLLM on dgx1-a
runs with `--enable-prompt-tokens-details` and the cache fields populate (EPICS 3.10i). Open on the
operator's side: the AFTER reading of the Firefox scrollbars (their before reading is under 3.12b),
their hands on the live picker, and 3.10g(1) narrowed to the activity line (a design under 3.10g,
builds on its own "go"). Then the retro (the DRAFT at docs/retros/ takes the 3.11a and 3.12 numbers;
one process observation to carry: three builds in a row edited a test pin the brief did not name,
each disclosed and right — "ONLY the pins named" is the wrong letter when the gate breaks a pin).
Morning note follows.

**Lanes at the morning note (2026-10-31 morning, the operator present).** Landed since 06:30: the 3.11a
review (clean; EPICS 3.11a), the 3.10c F3 fold (a3207a5, the "null" under tables), 3.12a the typed
filters (1938d98 + the coordinator fold 318c39c) and its DeepSeek review (EPICS 3.12, one latent
defect, six possibles, four noticed). The operator's dispositions are in EPICS 3.12 verbatim. Running:
the 3.12a-fold on DeepSeek/dgx1-a (four fixes; brief at the session scratchpad `brief-3.12a-fold.md`);
when it reports: `tools/accept.sh`, read the diff, land, a Qwen review of the fold. Designed and
APPROVED, each build waiting for its own explicit "go" (ruling (p)): 3.12b the two-level table
refresh (docs/superpowers/specs/2026-10-31-table-refresh-design.md) then 3.12c the started picker
(docs/superpowers/specs/2026-10-31-started-picker-design.md). 3.12a stays unticked until the operator
reads the live page. Earlier note follows.

**Lanes at the earlier note (2026-10-31 ~06:30, the operator asleep).** 3.11a LANDED (1f1bfe9: 17 folds,
834 tests; the audit at docs/audits/2026-10-31-codebase-audit.md; EPICS 3.11a holds the listed-not-folded
rows and row 37, which wants a ruling). Running: the cross-model review of 3.11a's folds, Qwen/ai1
(five construct-it probes: the shim's usage readers and head cap, finish_worktree's git failures,
_probe_context's odd shapes, the page's rowMemo/cellText cache, _log_safe and seed --force); when
it reports: verify each finding at its line, a fold job by the reviewer for the verified ones
(ruling (a)), land, record, tick. The operator's morning request — typed table filters (3.12) — is a
DESIGN awaiting approval at docs/superpowers/specs/2026-10-31-table-filters-design.md (its §14 lists
the five questions); no build until the operator answers. Earlier note follows.

**Lanes at the earlier note (2026-10-31 ~04:10).** Landed overnight: the page-batch
fold (81b199a; its review clean), 3.11b the test-suite audit (27faf06; the table at
docs/audits/2026-10-31-test-suite-audit.md). Running: 3.11a, the Opus codebase audit + folds
(the brief at the session scratchpad `brief-3.11a.md`); when it lands: accept, read every fold's
diff, land, a cross-model review of its folds (Qwen/ai1 or ai2), record. A retro DRAFT is at
docs/retros/2026-10-31-presentation-review-usability-DRAFT.md (counts at ~04:00; the operator
closes the arc). Suite 813. No push, no MCP reconnect, no touch of port 8765.

**Awaiting the operator.**
- 3.7t: the shim answers `/api/hello` itself (Claude Code's connection warm-up; today it is
  forwarded and recorded as a 501/404 "error"); the page labels the history rows "handshake".
- 3.7v: the job page labels "N tool errors" and "N request errors" side by side (the "2 vs 1").
- 3.7u's consequence, half answered by 3.7x (the down node's head says "down", the probe's
  sentence in its title): on the live page that sentence was the floor-setting line. The seeded
  world is still one column at 1400 px from its down endpoint's `.ep-error` line and the
  ~120-char activity line; leaving those out of the floor (they scroll) is the open follow-up.
- 3.7w closed: the normal tab's 661 MB is 278 MB of `dom/orphan-nodes` that survive a
  minimize; the same page in a Private Window (no extension realm in its process) minimizes to
  0.02 MB orphan / 46 MB resident, and the extension-free probe (`tools/smoke/ffmem.py` +
  `churn.js`, 960 rebuilds) holds 0.0 MB. A content script of the operator's profile holds the
  nodes; not a product defect. (Moved here from "awaiting" for the record; nothing to decide.)
- Open items named, not shaped: database purge and an analytics path — EPICS Epic 4 now carries
  the measured facts and three candidate shapes (P1 one job-retention rule through the v7 index
  with cascades; P2 transcripts first; P3 a hand-run `prune --rows`); fleet history serial per
  endpoint (~2 s cold for five dense 7 d endpoints).
- 3.8b: the operator reconnected the MCP server (`/mcp`, 2026-10-30 ~21:00), so today's package
  landings (A1, B1-B3, E2, 1.12 transcripts, 3.7t handshake) are live from the next job: expect a
  `transcripts` row per finished job and no `/api/hello` row.
- On the proposed pytest DOM smoke the operator: "The risk of the smoke test is that it runs
  against renders, not truth" — inputs must be real/captured snapshots, not hand edits (EPICS).
- The retrospective for the 3.7 + 3.9 arc (ANAS shape, `docs/retros/`) when the operator closes
  it: finds by finder, the operator's verdicts verbatim, lettered rulings only from repeated
  results (candidates: the reviewer fixes its own findings held all day; probes outside tests/
  need the committed net; a schema fold gets its own review; cdp.mjs's fixed wait cannot witness
  time, drive.mjs can).

**Process that held today.** Capture the operator's words verbatim first → the coordinator reads
the code behind them → brief with file:line and the two judgement sentences → `tools/accept.sh` →
read the diff → `tools/land.sh` (compose a both-sides-appended conflict, never pick a side) →
cross-model construct-it review → the reviewer folds its own findings → record in EPICS at every
step. Lanes: Qwen/ai1 (Strata) for ≤15-file stories and reviews of DeepSeek work, DeepSeek for
the deep SQL/cross-module work and reviews of Qwen work, Opus for page design with acceptance
numbers, Haiku for one bounded tool.
