# Handoff

Resume note for the next session. Rewritten at every session close; history is in git.

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
their machine. The operator found the 672-line README unreviewable ("TLDR"); it is split, the front page is 299 lines and the rest is docs/using.md, docs/monitor.md, docs/shim.md (EPICS 3.14 True-now). NEXT: the operator reads the README and the diff since f676d0e, then pushes (the push
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
