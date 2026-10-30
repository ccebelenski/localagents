# Handoff

Resume note for the next session. Rewritten at every session close; history is in git.

## 2026-10-30, ~02:10 (the operator went AFK at ~00:20: "work ahead, I'll pick up in the morning")

**State.** The 3.7 presentation pass is built, reviewed and landed on local main (nothing
pushed; the push is the operator's call after their own review). Suite at 649. Every finding
the operator gave on 2026-10-29 is captured verbatim in `EPICS.md` under "3.7 Presentation
pass" with the coordinator's reading of the code behind it, a story per finding, each landed
story carrying its job record (model, box, turns, minutes, tests, what the lane corrected in
the brief). Every lane's work was reviewed by a different model with constructed inputs before
the next fold; every review's defects were folded; the last review (DeepSeek over fold-4,
fold-4s, fold-5 and the coordinator's render fix) was running when this note was written — its
record goes under 3.7 when it lands, and any finding is the first fold of the morning.

**What the page is now** (the README's Monitor section describes it in full; `docs/EPICS.md`
3.7a-s and the folds say why):
- Host cards in config order, stable; never-seen endpoints hidden with one count line; a down
  node a card of the same shape; a live card = state dot, short model id, "active N ago", the
  strip with its window word, twelve fixed tiles with units in the labels and dashes for what
  a backend lacks, the KV meter, the sparkline from the endpoint's own history on one shared
  scale with the peak label, the jobs block as two fixed lines per job; no value ever changes
  a card's height (measured).
- "Recent jobs": a log — live first, then the last 10 finished — with status pills, "tool
  errors" named and linked to the job page, live turn counts.
- One timeline replacing the three charts: a row per live endpoint, its busy band and its job
  bars on a shared axis; the range control drives it.
- The header reads "updated HH:MM:SS" / "connection lost, last update …".
- Server: one refresh clock (`monitor: refresh_s`, default 5), one snapshot read per push
  shared by every tab, histories cached and batched into `/api/overview/history` (jobs
  included), a stored last-active witness (schema v6, backfilled at the monitor's start),
  finished log rows computed once; a `--no-collect` server self-ticks. Measured: a 7 d idle
  endpoint's read 666 ms → 1 ms; ten finished rows 86 → 26 statements per push.
- Running it: `localagents monitor` opens the browser (`--no-open` to keep it shut; a second
  run on a running port opens the page and exits 0); `tools/monitor.sh start|stop|status|log`.
- uPlot is gone from the package; both request tables draw through one renderer.

**For the operator in the morning.**
1. Your monitor on 8765 is the process you started by hand last night (old server code).
   Stop it and run `tools/monitor.sh start` (or `localagents monitor`): the page opens by
   itself. The database migrates to v6 on first open (one column; the witness backfill runs
   at start, once per endpoint).
2. Walk the page again and give findings as before; the capture-verbatim-then-brief loop is
   in `EPICS.md`. The one item the coordinator left for your eye is **3.7-fold-6** (the card's
   job line should lead with the ctx fill, not the long name and the counts).
3. The last review's record (DeepSeek over fold-4/4s/5 and 6249c03) lands under 3.7 when it
   finishes; if this note still says "running", `mcp__local__list_jobs` shows it.

**Tools.** `tools/accept.sh <branch>` (facts, no git writes) → read the diff → `tools/land.sh
<branch> -F msg` (squash onto clean main, commit, suite) → remove the worktree. `tools/smoke/`
is the DOM-only browser probe. The order held for nine landings tonight; three squashes
conflicted only on both-sides-appended CSS/probe/export tails, composed by keeping both.

**Decided tonight, do not reopen** (all in EPICS 3.7's text with the operator's words): cards
in config order, stable; never-seen hidden, dormant-but-seen a muted line; per-JOB context
fill is the number that matters; the jobs table is Strata's LOG concept with OUR columns; one
timeline replaces the rate charts, speeds go to the endpoint page; one "refresh master" clock;
tiles never wrap, units in the labels; the squash/suite step is a script, not a Haiku agent;
the MCP server never imports the monitor (guarded by an AST test, dynamic forms included).

**Open, small** (all recorded in EPICS or the review records): `read.overview` is ~26-32
statements on three endpoints (one windowed probe query per push is the next reduction); the
batched route still builds the fleet grid nobody reads; no all-jobs page (the log's "show all"
waits for it); the 15m timeline draws 3 axis labels; a third concurrent job overlaps lane 1;
`localagents --help` omits seed and monitor; a retrospective for the 3.7 arc is due when the
operator closes it (the ANAS shape: counts from the records, finds by finder, rulings).

Earlier decisions (push held; the sweep deferred to Epic 4; Epic 4 "not yet") stand.
