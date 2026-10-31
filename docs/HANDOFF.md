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

**Lanes at the last note (2026-10-31 ~01:10).** The whole 3.10 usability pass is landed: 3.10a, b,
c (+ the id fold 18efad4), d, f-1, f-2, g (the cap), h(a) + its fold, h(b); 1.14, 1.15. Running:
the shim-fold review (Qwen/ai2), the page-batch review (DeepSeek: f-2, b, h(b), g, c), 3.10i the
prefill rule for backends that report no cache (Qwen/ai1). Then: folds from those reviews, then
3.11a (Opus codebase audit) and 3.11b (test-suite audit), then the retro. The MCP server
(reconnected 23:15) predates 3.10h(a) and the shim fold: vLLM rows keep output 0 and every row
the first-byte prefill until the next `/mcp` reconnect at a quiet moment (never while jobs run).
3.10g's floor half and the vLLM flag (`--enable-prompt-tokens-details`, at vLLM's next natural
restart) are the operator's. Suite 798.

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
