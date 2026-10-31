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
