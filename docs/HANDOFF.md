# Handoff

Resume note for the next session. Rewritten at every session close; history is in git.

## 2026-10-30, overnight (in progress; the operator went AFK at ~00:20 saying "work ahead")

**State.** The 3.7 presentation pass is most of the way through on local main (nothing
pushed). The operator's findings are captured verbatim in `EPICS.md` under "3.7 Presentation
pass", each with the coordinator's reading of the code behind it; a story per finding; every
landed story has its job record. Landed tonight, in order: 3.7a-c host cards (abc0613), 3.7d-f
live card (4c23d8b), the launcher and default browser open (88a7dbe; `tools/monitor.sh
start|stop|status|log`, `localagents monitor --no-open`), 3.7-fold-1 live card fold (fb47aac),
3.7m/p/s server (4627647), 3.7-fold-2 server (0091694, schema v6: the stored last-active
witness), 3.7g-j the recent-jobs log (f670097), 3.7-fold-2b (6e686bf). Suite at 643.

**Tools.** `tools/accept.sh <branch>` prints the mechanical acceptance facts; `tools/land.sh
<branch> -F msg` squashes onto a clean main and runs the suite (the only script that writes to
git); `tools/smoke/` is the DOM-only browser probe (ruling (j)). The order: accept → read the
diff → land → remove the worktree. See `tools/README.md`.

**Running at the time of this note** (each lands, is reviewed by the other model, and folds):
- 3.7k requests tables (DeepSeek/dgx1-a): one renderer for the endpoint and job pages.
- 3.7-fold-3 + 3.7r (Qwen/ai1): last-active on the card, in-place card updates, the refresh
  interval from the snapshot, the header's "updated HH:MM:SS" phrase.
Then 3.7l, the fleet timeline (design in its EPICS bullet), replaces the three uPlot charts.

**For the operator in the morning.** Your monitor on 8765 is the process you started by hand
last night: it serves the new page files on reload, but its SERVER is the old code. Stop it and
run `tools/monitor.sh start` (or `localagents monitor`): the page opens by itself; the one
refresh clock, the cached reads, last-active and the jobs log need the new server. The
database migrates to schema v6 on first open (a column; the witness backfill runs at the
monitor's start, once per endpoint).

**Decided tonight, do not reopen** (all in EPICS 3.7's text with the operator's words): cards
in config order, stable; never-seen endpoints hidden with a count line, dormant-but-seen a
muted line; per-JOB context fill is the number that matters; the jobs table is Strata's LOG
concept with OUR columns; one fleet timeline replaces the three rate charts, speeds go to the
endpoint page; one "refresh master" clock, `monitor: refresh_s`; tiles never wrap, units in the
labels; the squash/suite step is a script, not a Haiku agent.

**Open, small.** `read.overview` is still ~32 statements on three endpoints (one windowed
probe query per push would cut it); uPlot is vendored but used only by the overview's charts
(drop it after 3.7l); the header's `/api/jobs` has no cache; no all-jobs page yet (the log's
"show all" waits for it).

Earlier decisions (push held; the sweep deferred to Epic 4; Epic 4 "not yet"; the MCP server
never imports the monitor, now guarded by a test) stand.
