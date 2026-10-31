# Browser smoke harness (ruling (j): DOM-only, over the DevTools protocol)

Drives a headless Chromium with Node's built-in WebSocket; no npm packages. Used by the page
stories' acceptance: a lane reads the JSON probe and the console log, never the screenshot.

Binary (Playwright's cache; `uv run playwright install chromium-headless-shell` if missing):
`~/.cache/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-linux64/chrome-headless-shell`.
Node: `/home/linuxbrew/.linuxbrew/bin/node` (v26).

```bash
# 1. a seeded world and a monitor serving it without collecting (ports are yours to pick)
# 1.15: BOTH variables — a package running from a worktree refuses the default state directory
# (the 2026-10-30 22:44 incident), and the job directories, transcripts and prune all derive from it
export XDG_STATE_HOME="$PWD/t/state" LOCALAGENTS_DB="$PWD/t/seed.db"
uv run localagents seed
uv run localagents monitor --no-collect --no-open --port 8771 &
# 2. a browser with a DevTools port
"$CH" --headless --no-sandbox --remote-debugging-port=9231 --user-data-dir="$PWD/t/profile" about:blank &
# 3. the probe: JSON of what the DOM holds, then the console/exception log; the PNG is for the operator
node tools/smoke/cdp.mjs 9231 http://127.0.0.1:8771/ 1400 900 t/overview.png
node tools/smoke/cdp.mjs 9231 http://127.0.0.1:8771/ 1400 900 t/a.png '#/endpoint/ai1' t/b.png
# hash-after may be `js:<expression>` to run any script after the first probe, e.g. a render()
# of a hand-edited snapshot (see nodes.js: states the seed does not produce)
# 3.7u: the card width (no ellipsis; the column floor only rises): wide.js renders seeded ->
# wide (a 200-char job name, a 120-char model id, a 300-char activity) -> seeded and measures
# after each render into `u` (floor, columns, overflowing lines, scrolling lines, heights)
node tools/smoke/cdp.mjs 9231 http://127.0.0.1:8771/ 1400 900 t/a.png "js:$(cat tools/smoke/wide.js)"
# 3.9D D1: nodes.js renders the states the seed does not produce (node down, unknown host, empty
# table) and then the transition it gatekeeps — one render hiding a card's only drawn endpoint
# (dormant, never seen). The probe's `d1` field carries target/before/after and `threw`, which
# held NotFoundError before the fix and must read null: remaining cards in #hosts, the hidden
# line counting the new endpoint, and renders.full advanced by the pass.
node tools/smoke/cdp.mjs 9231 http://127.0.0.1:8771/ 1400 900 t/nodes.png "js:$(cat tools/smoke/nodes.js)"
# 3.7y: neverseen.js renders a fourth host ai9 — node down, its one endpoint never seen (the
# state the operator looked at: "ai9 is 'down' yes, but it's also 'never seen'. Perhaps it
# shouldn't have a card in that state?"). Four renders, the DOM read after each into
# __d1.cases[]: (a) no ai9 card, the hidden line names it "ai9 (node down)"; (b) last_seen 3
# days before snap.now — the card returns, .node-down border, head note "down" (3.7x), one
# seen-dormant "last seen" line; (c) render (a) again — the card disappears (the 3.9D D1 path:
# renders.full advances, threw stays null) and the hidden line returns; (d) F5 (fold of the
# page-batch review 2026-10-30) — the down node's one endpoint is state "down", last_seen null
# (never probed up) but seen by its error: the card stays, one `.endpoint.state-down` and no
# `.ep-error` child (`d1.downEndpoint.ok`), which a neverSeenOn hiding every never-seen endpoint
# regardless of state would drop. Read d1 in AFTER_JSON.
node tools/smoke/cdp.mjs 9231 http://127.0.0.1:8771/ 1400 900 t/neverseen.png "js:$(cat tools/smoke/neverseen.js)"
# F1 (fold of the page-batch review 2026-10-30; spec §4 True-now): unknown.js renders a fourth
# host ai9, node state "unknown", whose one endpoint is state "down" with last_seen 2 h before
# snap.now and a 300-char error. Two reads into __d1.cases[]: cardLayout.need/floor before and
# after the render, and after it the head note "unknown" with the sentence in its title, no
# `.ep-error` child and the sentence absent from the card's text. The need must not move
# between the two reads — the regression F1 found (946 -> 2634 when the sentence was drawn).
node tools/smoke/cdp.mjs 9231 http://127.0.0.1:8771/ 1400 900 t/unknown.png "js:$(cat tools/smoke/unknown.js)"
# 3.7v/F3+F4 (commit 39fdb3a; fold of the page-batch review 2026-10-30): handshake.js hand-renders
# the shared requests table with the eight row shapes — three /api/hello handshakes (501, no
# status, 200), a 500, a status-0 backend failure, a clean 200, a context-exceeded 200, and a
# /api/hello with a recorded error. Each row's class, its pill's class/text/title and its error
# cell go into `d1`; d1.ok is true only when all eight match the oracle: the handshake rows are
# muted, read "handshake", keep the numeric status in the title and take NO `req-err` even with
# an error body; the two failures take `req-err`; the context row takes `req-ctx`. That row rule
# is exactly the job header's "request errors" count (F4's computed figure).
node tools/smoke/cdp.mjs 9231 http://127.0.0.1:8771/ 1400 900 t/handshake.png "js:$(cat tools/smoke/handshake.js)"
# 3.9D D3: range.js clicks a real range button (not the internal setter — the handler, the
# aria-pressed rebuild and the scheduled fetch are what is being tested), waits for each click's
# fetch to land, and records the timeline axis labels and pressed button before, after 15m and
# after 7d into the probe's `d3` field. The axis must follow the pressed range; read d3 in the
# AFTER_JSON line. (cdp.mjs awaits the js: expression, so a step may await its own witnesses.)
node tools/smoke/cdp.mjs 9231 http://127.0.0.1:8771/ 1400 900 t/range.png "js:$(cat tools/smoke/range.js)"
# story 1.14: stamp.js opens the running job's page and reads its header: the "server" figure
# (the seed stamps its server row with the tree's build stamp, so a monitor started from the same
# tree draws no note), then dispatches a hand-edited snapshot with another `build` on the page's
# own stream (EventSource is wrapped before the page opens it) — the muted "server code differs
# from the monitor's" note must appear — and the real snapshot again, which must clear it. Read
# d1 in AFTER_JSON: d1.ok is true when all three reads match.
node tools/smoke/cdp.mjs 9231 http://127.0.0.1:8771/ 1400 900 t/stamp.png "js:$(cat tools/smoke/stamp.js)"
```

The seeded world needs a `models.yaml` naming the seed's three endpoints (tests/monitor_seeded.py
`MODELS_YAML`); pass it with `--config` (without one the monitor serves 0 endpoints).

`conn.mjs <port> <url> <seconds>` prints the connection label every second (stream reconnects).
The probe's field list is in cdp.mjs; add fields for new classes rather than reading pixels.
A `--no-collect` server pushes a fresh snapshot every `refresh_s` anyway (the fan-out self-ticks,
story 3.7-fold-2), so capture right after a hand-render — the next push overwrites the
hand-edited DOM.

## drive.mjs: hold a page for N seconds and report what changed

`node tools/smoke/drive.mjs <devtools-port> <url> <hash> <seconds> [t/drive.json]` opens the shell,
arms a `window.fetch` counter (every `/api/` call with its offset in seconds from arming), switches
to `<hash>` (`'#/endpoint/ai1'`, `'#/job/<id>'`), then samples the detail pane every 2 s for N
seconds. The report (JSON file plus a stdout summary) compares first and last sample: the snapshot
clock and render counters, the clock-relative phrases (the endpoint page's "last seen … ago", the
job page's elapsed), a hash of the latency tile — its whole `outerHTML`, so a sliding sparkline
counts as changed — and the count of `/api/endpoints/<name>/(history|requests)` fetches that landed
after the 30 s refetch window. This is the time axis cdp.mjs's fixed 2500 ms after-sleep cannot
see: a page that never redraws on a snapshot freezes every field while the stream keeps ticking.

## mem.mjs: does the page grow with its pushes?

`node tools/smoke/mem.mjs <devtools-port> <url> <seconds> [every_s=5]` opens a target, navigates, and
prints a tab-separated header then one row per sample: ISO time, DOM `Nodes`, `JSEventListeners`,
`Documents`, `JSHeapUsedSize` in MB, and the page's render-pass count (`window.localagentsMonitor.renders`
full + inPlace + skipped; `-1` if the page does not export it). `HeapProfiler.collectGarbage` runs before
the first and the last sample so the heap figure is what survives collection. Use `--no-collect` and a
small `refresh_s` in the monitor's models.yaml to get many pushes quickly. Read the counters, not pixels.

## floor.mjs: the column floor is the text's, never the window's (P-1)

`node tools/smoke/floor.mjs <devtools-port> <url> [widths=2200,2000,2200,1400,2200] [settle_s=3]` opens
the overview at the first width, resizes through the rest (waiting `settle_s` of pushes: the floor is
measured after every render, there is no resize listener) and evaluates `floor.js` at each step. floor.js
measures every card line's need on its own — a block line's text extent through a Range, a flex row's
items summed with their gaps, plus the chrome between the line's box and the card's edge, a flex item
counted through its row — and reports it as `oracle` beside the page's `cardLayout.floor`/`need`, the
grid's `columns`, `needIsText` and `floorIsText`. The process exits 1 when any step's floor or need is
not the text's. Run it on the seeded world with no hand render (a hand render that shrinks the texts
leaves the floor, a high-water mark, above the current text by design; `needIsText` still holds there).
Seeded world, 3.7u-fix P-1 fold: floor 946 (the down endpoint's error line) at every step, columns
2/2/2/1/2; before the fold the floor read 1078 → 1953 → 2153 → 2153 → 2153, columns 2/1/1/1/1.

## keep.js: the overflow mark never costs a node its identity (P-2)

`node tools/smoke/cdp.mjs 9231 http://127.0.0.1:8771/ 900 900 t/keep.png "js:$(cat tools/smoke/keep.js)"`
(run at 900 and at 1400). Read `keep` in the AFTER_JSON line: `pushes` stamps every `.endpoint` and
`.host-head`, holds while 7 pushes render, and lists the nodes `replaced` (at 900 px the seeded down
endpoint's error line scrolls: `overflowing` names it); `inPlace` hand-renders a world with a 300-char
speeds note, down error and host note and a 200-char job name, then the same world with only the running
job's elapsed changed (`path.inPlace` 1) and lists the nodes `replaced`. Both `replaced` lists must be
empty. 3.7u-fix P-2 fold: before, `pushes.replaced` held "endpoint dgx1-a" at 900 px and
`inPlace.replaced` held "endpoint ai1", "endpoint dgx1-a" and "head ai9" at both widths; after, [] and
[] with `kept` 6 and 8.

## ffmem.py + churn.js: the Firefox side of 3.7w (orphan nodes)

`python3 tools/smoke/ffmem.py <url> <seconds> [every_s=60] [--js tools/smoke/churn.js]` launches the
workstation's Firefox headless on a fresh, extension-free profile (raw Marionette on port 2829, no
geckodriver), opens the page and every `every_s` takes about:memory's "Minimize memory usage" +
"Measure" reading of the tab's content process through `nsIMemoryInfoDumper`: `dom/orphan-nodes` MB
(nodes out of the document but still reachable from JS — the thing a GC cannot free), DOM total,
JS, derived heap-unclassified, resident-unique, and the page's render count. The dumps stay under
`t/ff/memory-<n>.json.gz` for about:memory's "Load". `--js churn.js` renders two alternating
hand-edited snapshots every 250 ms (a host card torn down and rebuilt, every endpoint's "active …"
sub-block swapped in place, the jobs table rebuilt), the three paths that detach nodes.
2026-10-30, Firefox 158, seeded world at refresh_s 0.5, four minutes: 960 churn renders + 1,692
pushes → orphan 0.0 MB, dom 0.2 MB, js 2.4 MB throughout; resident-unique 21 → 55 MB (Firefox's
own warm-up, flattening). The operator's tab held 278 MB of orphan nodes with LastPass, uBlock
Origin and Download Manager (S3) content scripts in the same process.

## jobsrows.js and tiles.js: the 3.10a / 3.10d witnesses (from the dense-Qwen review, 2026-10-30)

The review of 39936f1 and 9c41b21 found every regex pin for those stories satisfiable by a wrong
implementation and no committed smoke reading the Recent-jobs cells or the endpoint page's tiles;
its two hand-renders are kept here as the witnesses. `jobsrows.js` (overview, empty hash) renders a
snapshot copy whose jobs cover running/queued/done with a dead server (lost, lost, done), a started
2 days and 2 h back (date vs clock form, compared cell-for-cell with the request table's time cell
at the same instant), a null and a 1234.5 prefill; `tiles.js` (`#/endpoint/ai1`, run at 1400×900
and at 760×900) reads every tile label, heading and value overflow before and after a 200-character
served model with `host.fallback: "refused"`. Both store into `window.__d1`; read it in AFTER_JSON.
A 200-character model widens its tile past the viewport (the grow-never-cut rule; realistic ids
do not).
```bash
node tools/smoke/cdp.mjs 9231 http://127.0.0.1:8771/ 1400 900 t/j.png '' "js:$(cat tools/smoke/jobsrows.js)"
node tools/smoke/cdp.mjs 9231 http://127.0.0.1:8771/ 1400 900 t/t.png '#/endpoint/ai1' "js:$(cat tools/smoke/tiles.js)"
```
