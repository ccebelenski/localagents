# Epics and stories

Preliminary breakdown of `docs/superpowers/specs/2026-10-29-monitor-design.md`, cut for
incremental builds with a test gate per story. Adjust as we go. `☐` open, `☑` merged.
Each story: scope, gate (what must be green), lane hint (per `~/.claude/hints/local-delegation.md`).

Order: Epic 1 before anything else (the monitor assumes its tables); 2.1 before any parser;
2.4 before any page story; 3.5 last.

## Epic 1: the MCP server writes SQLite

Replaces `requests.jsonl` and `status/<pid>.json` with tables in
`~/.local/state/localagents/localagents.db`; adds tool-call records and exposes them to the
coordinating model. `task.txt`, `transcript.txt` and the opt-in debug dumps stay files.
The MCP server never depends on the monitor. Needs its own short design before 1.1.

- ☐ **1.1 Schema module.** New `db.py`: `open_db()` (WAL, busy timeout, `LOCALAGENTS_DB`
  override, `XDG_STATE_HOME` as `state_dir()`), versioned schema, `migrate()`. Tables: `servers`,
  `jobs`, `requests`, `tool_calls`; `probes` reserved for the monitor (created by it).
  Gate: empty file and a version-1 file both come up current; two processes writing
  concurrently (subprocess test) lose nothing. Lane: Opus or Qwen (single file).
- ☐ **1.2 Jobs table.** `JobStore.new`/`Job.changed` upsert the job row (every `job_status`
  field plus `server_pid`); `summary()` unchanged for callers. Gate: a job's row tracks each
  status transition over a real temp database; existing server tests green. Lane: Qwen.
- ☐ **1.3 Requests table.** Shim `_log` writes a `requests` row instead of `requests.jsonl`
  (same fields: t, path, status, ms, gen_ms, usage, model, messages, tools, bytes, notes).
  README "Job records" updated. Gate: rows reproduce a captured `requests.jsonl` line for line
  (fixture from a real job, captured in 2.1). Lane: Qwen.
- ☐ **1.4 Tool calls and exposure.** Runner writes one `tool_calls` row per `ToolUseBlock`
  (time, name, short input: file path or first 120 chars of a command) and marks the error
  flag from the matching `ToolResultBlock`. `job_status`/final record gain `tool_counts`
  (per tool: calls, errors) and `last_tool`; `job_log(tools=N)` returns the last N rows;
  the blocking-wait progress line names the current tool and the error count. Gate: a
  replayed SDK message sequence (fixture) yields the expected rows and counts; progress line
  pinned. Lane: DeepSeek (runner + server + jobs) with the fixture named.
- ☐ **1.5 Servers table and status line.** Heartbeat row per server pid (owner, updated);
  `status.collect()` reads the tables; `status/<pid>.json` and its publish path removed; the
  pid alive check kept. Gate: `test_status.py` renders the same lines as today from the
  database; `localagents statusline` still imports no server modules (existing subprocess
  test). Lane: Qwen.
- ☐ **1.6 Retention and docs.** Prune leaves job/request/tool-call rows; README retention and
  job-records sections rewritten for the database; `models.example.yaml`; version bump.
  Gate: `test_retention.py` extended for rows kept; full suite. Lane: Qwen (docs story).

## Epic 2: monitor core (collector and storage, no page)

- ☐ **2.1 Fixtures.** Capture: fresh Strata `/metrics` from ai1 (with `hardware`/`history`);
  vLLM `/metrics` + `/v1/models` from a DGX box; llama.cpp `/props`, `/slots`, `/metrics`; two
  captures of each counter document a minute apart; one real job's `requests` and
  `tool_calls` rows. A manifest test that every fixture is listed and parses. Lane:
  coordinator by hand, through the manifest test before commit.
- ☐ **2.2 Probes table and collector.** `monitor/collect.py`: fast/slow cadence per state,
  one `probes` row per probe (failure sentence included), Strata `hardware`/`requests` stored
  when present, rates between consecutive rows with reset detection (negative delta →
  endpoint event, no rate for the interval), 7-day probe retention. Gate: rates from the
  paired captures match hand-computed values; reset case; cadence selection. Lane: DeepSeek
  or Opus (cross-module with registry).
- ☐ **2.3 Display and node state.** Pure functions from rows: live / down-recent / dormant
  with the attention window (24 h default, `monitor:` section), cold start → dormant; node
  up/down/unknown from a host's endpoints; port-22 fallback behind an injected connector.
  Gate: both sides of the boundary; every truth-table row. Lane: Qwen.
- ☐ **2.4 `localagents seed`.** Offline database from fixtures, timestamps shifted to now:
  three hosts; live endpoint with an hour of history; one down 20 min; one dormant a week;
  finished job with requests and tool calls; one running. Gate: seeded database passes the
  2.3 state functions with the expected states. Lane: Qwen.

## Epic 3: monitor web

- ☐ **3.1 App and routes.** `localagents monitor [--port 8765]`: Starlette app, SSE stream
  pushing a snapshot per tick, JSON history routes for endpoint and job, time-range parsing
  (15 min to 7 d; bad range rejected). Gate: Starlette test client over the seeded database.
  Lane: DeepSeek or Opus.
- ☐ **3.2 Overview page.** Host cards with the three endpoint states and node collapse,
  running-jobs table (current tool, error count, context bar), fleet charts with vendored
  uPlot, range control, dark theme. Gate: browser smoke over `seed` (every state visible).
  Lane: Opus (the user-facing sentences and layout are judgement work); a Glimmer slice only
  with the exact reply shape.
- ☐ **3.3 Endpoint detail.** Tiered stat tiles with sparklines (absent when no source), context
  window, request table with totals line, jobs in range. Gate: browser smoke; tier table
  from the spec checked against each fixture backend. Lane: Opus or Qwen with the tier table
  quoted.
- ☐ **3.4 Job detail.** Context gauge, request table, metrics delta, files touched, tool-call
  timeline strip with context fill overlaid, breakdown by tool, transcript tail. Gate:
  browser smoke over the seeded finished job. Lane: Qwen with the row shapes quoted.
- ☐ **3.5 `localagents smoke` and merge gates.** Online script: direct `/v1/messages`
  requests to a named endpoint; one real job through `run_job` with the fixed brief ("run
  `date` with Bash and report it"); a temp config entry on a dead port; printed checklist,
  non-zero exit on a missing row. README monitor section. Gate: smoke green against the real
  `models.yaml`; two-minute live run at merge. Lane: Qwen on Strata (node-pass shape), the
  endpoint and model named in the brief.

## Epic 4: operations (V2, not scheduled)

- ☐ **4.1 Guided SSH setup script.** Walks the operator through key placement per host; no
  secrets stored by localagents; verifies with one `ssh host true`.
- ☐ **4.2 Start/stop/restart recipes** per host in the `monitor:` section; actions from the
  endpoint detail page with confirmation.
- ☐ **4.3 Node telemetry** via periodic `nvidia-smi` over SSH into the tier-3 tiles for
  non-Strata backends.
