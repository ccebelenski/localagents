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
```

The seeded world needs a `models.yaml` naming the seed's three endpoints (tests/monitor_seeded.py
`MODELS_YAML`); pass it with `--config` (without one the monitor serves 0 endpoints).

`conn.mjs <port> <url> <seconds>` prints the connection label every second (stream reconnects).
The probe's field list is in cdp.mjs; add fields for new classes rather than reading pixels.
A `--no-collect` server pushes a fresh snapshot every `refresh_s` anyway (the fan-out self-ticks,
story 3.7-fold-2), so capture right after a hand-render — the next push overwrites the
hand-edited DOM.
