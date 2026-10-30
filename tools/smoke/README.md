# Browser smoke harness (ruling (j): DOM-only, over the DevTools protocol)

Drives a headless Chromium with Node's built-in WebSocket; no npm packages. Used by the page
stories' acceptance: a lane reads the JSON probe and the console log, never the screenshot.

Binary (Playwright's cache; `uv run playwright install chromium-headless-shell` if missing):
`~/.cache/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-linux64/chrome-headless-shell`.
Node: `/home/linuxbrew/.linuxbrew/bin/node` (v26).

```bash
# 1. a seeded world and a monitor serving it without collecting (ports are yours to pick)
LOCALAGENTS_DB="$PWD/t/seed.db" uv run localagents seed
LOCALAGENTS_DB="$PWD/t/seed.db" uv run localagents monitor --no-collect --no-open --port 8771 &
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
# 3.9D D3: range.js clicks a real range button (not the internal setter — the handler, the
# aria-pressed rebuild and the scheduled fetch are what is being tested), waits for each click's
# fetch to land, and records the timeline axis labels and pressed button before, after 15m and
# after 7d into the probe's `d3` field. The axis must follow the pressed range; read d3 in the
# AFTER_JSON line. (cdp.mjs awaits the js: expression, so a step may await its own witnesses.)
node tools/smoke/cdp.mjs 9231 http://127.0.0.1:8771/ 1400 900 t/range.png "js:$(cat tools/smoke/range.js)"
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
