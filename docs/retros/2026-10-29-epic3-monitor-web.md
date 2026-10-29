# Retrospective: Epic 3 (monitor web), 2026-10-29

Scope: stories 3.1 through 3.6 with their folds, built between 11:40 and 17:00 on 2026-10-29
in the same lane process as Epics 1 and 2 (retros `2026-10-29-epic1-mcp-sqlite.md` and
`2026-10-29-epic2-monitor-core.md`). Counts are from `docs/EPICS.md` and the git log; story 3.2c
(the fold of the combined review) landed after this was written: 611 tests, Epic 3 closed.

## What shipped

| | |
|---|---|
| Stories and folds in Epic 3 | 15, all ticked |
| Story commits on main | 15 |
| Tests | 431 → 611, all passing at every merge |
| Local lane jobs named in EPICS | 17 named jobs, 1,492 turns where counted, plus the two DeepSeek jobs of the 3.2 review and the 3.6 gate |
| Opus-high lanes | 4 builds (3.1, 3.2, 3.3, 3.2a-page) |
| Cross-model reviews | 8 plus the combined review of four folds; two found no defect at all |
| Browser smokes | every page story and every page review ran a real headless Chromium over the DevTools protocol; no screenshot was ever read by a lane after the one-image incident |
| The live gate | green against the real fleet: 12 endpoints classified, two minutes of collection, the smoke 17/17, every page, SIGTERM exit 0 |

The monitor MVP is complete: `localagents monitor` serves the overview, the endpoint detail and
the job detail over the shared database; `localagents seed` builds the demo world; `localagents
smoke` is the online acceptance run; the MCP server imports nothing from the monitor package.

## Finds by finder

- **Construct-it reviews: 14 defects, 0 false.** The ones that changed the shape: Qwen/ai1's
  review of 3.1 tabled every §5 datum against §5a and found six gaps before any page was built
  (the state strip never stored, no fleet route, no latency series, no requests-by-endpoint
  route, disk nowhere in the schema); DeepSeek's review of 3.2 confirmed the port check's
  TIME-WAIT refusal by timing a restart; Qwen/ai2's review of 3.3 measured fetch discipline per
  bucket and found the fan-out starting only at the first subscriber; Qwen/ai1's review of 3.4
  counted event streams by instrumenting the browser's EventSource constructor and found the
  context fill on a different clock from the marks; DeepSeek's combined review found the
  fan-out's close method with no caller.
- **Builders correcting the brief with evidence: 11 times.** Starlette's test client buffers a
  stream (Opus, 3.1); uvicorn re-raises the signal after shutdown (Opus, 3.1); node was on the
  machine and the brief said it was not (Opus, 3.2); the snapshot's `state` is the display
  state, not the probe string (Opus, 3.2); no snapshot callback exists in app.js (Opus, 3.3);
  files relativise by the worktree's path, not its parent (27B, 3.4); `status.collect` makes no
  summary call (DeepSeek, 2.4a); a numeric Strata state stores idle, not NULL (Qwen/ai2, 3.1a
  review); ten hardware series, not eight (Qwen/ai1, 3.1a); a socket import the brief assumed
  (Qwen/ai1, 3.1c); the status line's 120 s linger is for a live server's finished jobs, not a
  stopped smoke (Qwen/ai1, 3.6). None of the eleven was itself wrong.
- **Builders finding defects in the browser that no offline test could see: 4.** The on-connect
  snapshot beating the detail fetch (27B, 3.4); the hook's teardown not running on a return to
  the overview (Opus, 3.3); a malformed hash on a cold load killing the whole page (Opus,
  3.2a-page); the smoke's date check matching the runner's own timestamp lines (Qwen/ai1, 3.5).
- **The operator: 1 live observation** (the status bar's three readability defects, which
  became 1.11b) and the schema approval for the probes tables.
- **The coordinator's reads: 3** (the 3.2 screenshot's window format and absolute tool path; a
  shim comment repeating a refuted claim; the hand-composed conflicts of 3.4 beside 3.3).
- **The suite on main: 1 cross-lane break** (3.1's tests used the row-writing helper 2.2a
  deleted).

## The operator's verdicts, verbatim

- "Status line looks good - exposing current tool call a good idea - can we track compactions
  done count? That's a good trailing indicator of a tuning problem." → 1.11 and 1.11a.
- "Schema looks okay - and I can see the second slot active - and the famous slowdown with
  multiple slots active for llama.cpp's scheduler - not a concern." → the probes schema, and two
  lanes at once on the 27B.
- "Maybe the status line needs a bit more TLC and editing? [the pasted bar]" → 1.11b.
- "Qwen38-27B up and running locally now on llama.cpp, 2 lanes, 256K context, 100+ t/s gen
  speed." → the 27B as a build and review lane for every page story.

## Ruling proposals (done repeatedly, with results)

- **(j) A page story's acceptance is a browser smoke driven over the DevTools protocol, DOM
  dumps and JSON only, never a screenshot read.** Seven page lanes and six page reviews ran
  this way; the one lane that read a second screenshot died (DeepSeek, one image per prompt);
  every later brief forbade image reads and none died. The operator sees screenshots only
  through the coordinator.
- **(k) Two page stories in parallel each own one file and register through a hook; the merge
  composes the shared files by hand.** 3.3 and 3.4 built against the same ten-line hook, met at
  four conflicts (both script tags, both CSS sections, both README paragraphs, the asset list
  over the shared helper), and both smokes held after the merge. The hook itself carried one
  bug (teardown on return) that the first page story found.
- **(l) A brief's "not provided by the contract" list is the next story's input, not a
  blocker.** Every page lane listed what §5a lacked and built the page without it; 3.1a,
  3.2a-server and 3.2b added the fields in the order the lists arrived; nothing was faked.
- **(m) A fold's review folds into the next review of the same door when the fold is under
  thirty lines and the coordinator read the diff.** Done for 1.11a, 1.11b, 2.1b, 2.3a, 3.1c,
  3.4a and 3.5a; the combined review of four folds then found two defects the single-fold
  reviews would have found one at a time, at a quarter of the lane time.

## What cost

- The DeepSeek lane's one-image limit: one review died at probe 1 and was continued in the same
  worktree; the limit is now in memory and in every brief for that lane.
- Reviews of page stories ran 20 to 45 minutes each; the 3.3 review was the longest of the day
  and found the fan-out seam that no server test had.
- Four Opus lanes: roughly 0.9M subagent tokens (quota); every local lane: zero quota.
- Two leftover processes (a headless browser and a seed-serving monitor on a loopback port)
  from a removed review worktree, found by the 3.6 lane and stopped by the coordinator.

## Open rulings

- Whether the collector runs the lost-job sweep on its own schedule. Asked three times, not yet
  answered; Epic 4 territory if the monitor becomes the long-lived process on the box.
- Strata's `_mb` fields: the tiles read `MB` unitless until Strata documents the sample window.
- The overview's history fetch is N+1 per refresh (one per live endpoint, for the strip's
  segments); fine on this fleet, a fleet route carrying the state series would remove it.
- Epic 4 (operations: SSH setup, start/stop recipes, node telemetry) is not scheduled.

## Closing step

`docs/EPICS.md` keeps one line per story with its commit, job id and review; this file holds the
story; `docs/gates/2026-10-29-epic3-live-gate.md` holds the live run's evidence. The MVP is
feature complete; the push is the operator's call.
