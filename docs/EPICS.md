# Epics and stories

Preliminary breakdown of `docs/superpowers/specs/2026-10-29-monitor-design.md`, cut for
incremental builds with a test gate per story. Adjust as we go. `☐` open, `☑` merged.
Each story: scope, gate (what must be green), lane hint (per `~/.claude/hints/local-delegation.md`).

Standing rule (2026-10-29): every piece of work is reviewed, read-only, by a model that did
not do it, before it merges; designs and specs included. Findings carry file:line and a
scenario; the disposition (accepted / declined with reason) is recorded.

Order: Epic 1 before anything else (the monitor assumes its tables); 2.1 before any parser;
2.4 before any page story; 3.5 last.

## Epic 1: the MCP server writes SQLite

Replaces `requests.jsonl`, `task.txt` and `status/<pid>.json` with tables in
`~/.local/state/localagents/localagents.db`; adds tool-call records and exposes them to the
coordinating model. `transcript.txt` and the opt-in debug dumps stay files. The MCP server
never depends on the monitor. Spec: `docs/superpowers/specs/2026-10-29-mcp-sqlite-schema.md`
(reviewed by Qwen 2026-10-29). Cut as write path first (1.2–1.4: rows are written, in-memory
fields and the status files stay), then read path (1.5a/b: readers switch to the tables and
the duplicates go), then docs.

- ☑ **1.1 Schema module.** (8d01b41, Opus-high; reviewed by Qwen/ai2 job 20261029-033804-d6e5: clean, nits folded in f5c090e) New `db.py`: `db_path()`, `open_db()` (WAL, busy timeout,
  foreign_keys per connection, `LOCALAGENTS_DB` override), versioned `migrate()`,
  `immediate()` context manager (BEGIN IMMEDIATE), `lookup_id()` (INSERT OR IGNORE then
  SELECT), `new_uuid()`, `job_name()`. Stdlib only (the status line imports it). Gate: fresh
  file and a version-1 file both come up current; version 99 raises; four real processes
  writing concurrently lose nothing; CHECK and FK enforced. Lane: Opus-high (dispatched
  2026-10-29 night).
- ☑ **1.2 Jobs write path.** (b331747 + c603007, Qwen/ai1 job 20261029-033850-e873; reviewed by DeepSeek/dgx1-a job 20261029-034720-3071: one low defect, folded in 1535399) `Job.id` becomes a uuid4 (`job_id` of every tool, `resume_job`
  included), `Job.name` the readable stamp (directory and worktree branch `local-agent/<name>`).
  `JobStore` opens the database and inserts its `mcp_servers` row (id, pid, owner, started, cwd,
  version) on first use (not at construction: server.py builds the store at import), and `new()` inserts the job row inside `immediate()` with `endpoint_id` /
  `model_id` via `lookup_id`; `Job.changed()` upserts the mutable columns. `task.txt` no
  longer written. Prune pattern widened to `[0-9a-f]{4,}`. In-memory fields and the status
  file untouched. Gate: a job's row tracks each status transition over a real temp database;
  the `job_id` run_agent returns is the uuid; directory and branch use `name`; existing
  suite green. Lane: Qwen (ai1).
- ☑ **1.3 Requests write path.** (eb458b6, Qwen/ai2 job 20261029-034621-217d; review in the batch below) Shim `_log` inserts a `requests` row (path via `lookup_id`,
  `http_status`, ms, gen_ms, stream, four token columns, messages, tools, bytes,
  moved_system, folded_text, context_exceeded, error) and stops appending `requests.jsonl`.
  `note_usage` still updates the in-memory ctx_tokens/gen_tps for now. Gate: a captured
  `requests.jsonl` from a real job replays into rows that match it field for field (fixture
  captured by the coordinator). Lane: Qwen (ai2).
- ☑ **1.4 Tool-calls write path.** (a295ff8, Qwen/ai1 job 20261029-034639-1970; review in the batch below) Runner inserts a `tool_calls` row per `ToolUseBlock`
  (input rule: edit tools → file_path/notebook_path or NULL; others → first 120 chars),
  keeps `tool_use_id -> rowid` for pending calls in memory, and sets `finished`/`is_error`
  by rowid when the `ToolResultBlock` arrives. Gate: a replayed SDK message sequence (fixture
  built from the SDK dataclasses) yields the expected rows. Lane: DeepSeek (dgx1-a) or Qwen.
- ☑ **1.5a Status line read path.** (3025d00, Qwen/ai1 job 20261029-035222-2edd; batch review 1.3+1.4+1.5a+1535399 by DeepSeek/dgx1-a dispatched 2026-10-29 04:20) `mcp_servers` heartbeat every 10 s (`updated`), `stopped`
  at clean exit, config/transport/shim_port filled; sampler writes `endpoint_load` rows;
  `status.collect()` reads live servers + their queued/running/recent jobs + latest request
  rows + endpoint_load; `status/<pid>.json` and `publish()` removed; the `lost` rule at
  startup. Gate: `test_status.py` renders the same lines from the database; statusline still
  imports no server modules. Lane: Qwen (ai1).
- ☐ **1.5b Summary read path and exposure.** `summary()` derives usage totals, tool call
  count, `tool_counts`, `last_tool`, files touched, ctx_tokens, gen_tps, run_cwd, log_dir from
  the tables (derivations as in the spec); the in-memory duplicates are removed from `Job`;
  `job_log(tools=N)`; `list_jobs(all=False)`; the progress line names the current tool and
  the error count; `job_status`/`job_log`/`list_jobs` read any server's job, the in-process
  tools refuse another server's with a clear error. Gate: summaries over a seeded database
  match hand-computed values; progress line pinned. Lane: DeepSeek (dgx1-a).
- ☐ **1.6 Docs and version.** README job records, status line, retention ("retention must
  exceed the longest timeout_s"), `models.example.yaml`; version bump. Gate: full suite;
  README cites nothing that no longer exists. Lane: Qwen or Haiku.

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
