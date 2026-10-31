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

**Lanes at the last note (2026-10-30 ~23:00).** Landed tonight: 3.10a, 3.10d, 1.14 (reviews clean),
3.10f-1 the job name dropped (c768dfd + 8ad16e6; schema v9 with directory renames — ITS REVIEW IS
NEXT, Qwen/ai2; until it is clean, no restart of the operator's monitor or MCP server, because the
first process on this code migrates the real database and renames the real job directories).
INCIDENT 22:44: the real database was migrated to v9 from the lane's worktree code before the
landing (EPICS 3.10f-1 incident); the operator reconnected the MCP server (~23:15) and must restart
the monitor the same way. 1.15 (a4252ce) now makes a worktree refuse the default state directory.
Queue: the 3.10f-1 review (Qwen/ai2, with 1.15 in its brief), 3.10f-2 (monitor side: the JSON
keys and pages show the short id), 3.10g (the one-column stretch: cap the card width; the job
lines out of the floor awaits the operator), 3.10b, 3.10c. Suite 752. Every lane brief from now
exports XDG_STATE_HOME and LOCALAGENTS_DB together (the smoke README recipe does).

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
