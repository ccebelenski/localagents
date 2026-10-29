# localagents monitor: design

Date: 2026-10-29. Status: approved in conversation, awaiting written review.

A web dashboard for the fleet of local inference endpoints in `models.yaml` and the jobs
localagents runs on them. MVP is the monitor. V2 adds operations (start/stop over SSH).

## 1. Purpose and scope

- **Audience:** the operator, on a second screen, while local jobs run.
- **Question it answers at a glance:** which boxes are up, what each is serving, who is busy,
  how fast, and is anything wrong. Detail on click, never on the overview.
- **Local-centric, by design.** Jobs are what this machine's MCP servers dispatched. Endpoint
  figures (counters, slots, Strata's request list) are server-wide, so another client's traffic
  appears as throughput and occupancy with no job attached. The overview says "busy, no local
  job" on such an endpoint.
- **Operational data, not a warehouse.** Seven days of probes; job rows kept. A star schema
  later is views over these tables.

## 2. Prerequisite: the MCP server writes SQLite (separate story, built first)

Its own spec and approval. Summary of what was agreed so the monitor can assume it:

- One database at `~/.local/state/localagents/localagents.db` (`XDG_STATE_HOME` honoured as
  `state_dir()` does; `LOCALAGENTS_DB=/path` overrides). WAL mode, busy timeout. Schema in one
  versioned module of the package; the MCP server creates and migrates it. Single machine only.
- Written directly by every MCP server process (one per Claude Code session):
  - `jobs`: every field `job_status` returns, updated in place as the job runs, plus
    `server_pid`. Replaces the idea of a `summary.json`.
  - `requests`: one row per backend request from the shim (what `requests.jsonl` holds today:
    time, path, status, ms, gen_ms, usage, model, messages, tools, bytes, notes). Replaces the file.
  - `tool_calls`: one row per tool call from the runner: time, tool name, short input (file path
    or first 120 chars of a command), error flag.
  - `servers`: pid, owner (Claude Code pid), last heartbeat. Replaces `status/<pid>.json`; the
    status line becomes one indexed query, stdlib only, same pid alive check.
- Exposed to the coordinating model: `job_status` and the final record gain `tool_counts`
  (per tool: calls, errors) and `last_tool`; `job_log` gets `tools: N` (last N tool calls,
  structured); the blocking-wait progress line names the current tool and the error count.
- Stays as files: `transcript.txt` (humans read it; `job_log` tails it; the brief is the `jobs.task` column),
  and the opt-in debug dumps `events.jsonl` / `requests_full.jsonl`. The 7-day prune keeps
  deleting directories by date; job, request and tool-call rows are kept.
- The MCP server never depends on the monitor. The monitor adds its own tables to the same
  file and reads the job tables.

## 3. Architecture

- **Process:** `localagents monitor [--port 8765]`, its own Starlette app under uvicorn (already
  dependencies). Endpoints and `host` grouping come from `models.yaml` through the existing
  registry code. An optional top-level `monitor:` section (port, attention window hours,
  database path) is ignored by the MCP server, which ignores unknown top-level keys.
- **Collection loop:** every 5 s probe each endpoint with `Registry.probe` and `fetch_metrics`;
  a dormant endpoint every 60 s. Each probe writes one `probes` row: time, endpoint, up, host
  state, served ids, context, slot/queue numbers, raw counters, failure sentence. Strata's
  `hardware` and `requests` sections stored when present. Rates are computed between
  consecutive rows, never stored.
- **Node fallback probe:** only when every endpoint on a host times out, one TCP connect to port
  22 on the host. Answer or refusal = node up (endpoints down); nothing = node down.
- **Jobs:** read from the `jobs`/`requests`/`tool_calls` tables every 2 s (running jobs with
  context fill and speed; finished records for history).
- **Storage:** `probes` (monitor-owned) in the shared database; probe rows older than 7 days
  deleted on the same schedule as job directories. Schema migrated by version number. The
  database is a cache of the fleet, not the record of truth: deleting it costs history only.

  Monitor tables, approved 2026-10-29 (schema version 3; version 2 is `jobs.compactions`,
  story 1.11). Conventions as the MCP schema: epoch REAL timestamps, lookups by id, flat
  nullable columns where the field set is fixed, nothing stored that is derived.

  ```
  probes            one row per probe attempt (5 s when live, 60 s when dormant); monitor-owned
    id            INTEGER PRIMARY KEY
    t             REAL NOT NULL
    endpoint_id   INTEGER NOT NULL REFERENCES endpoints(id)
    model_id      INTEGER REFERENCES models(id)      NULL when unknown
    up            INTEGER NOT NULL                   0/1
    host          TEXT                               'up' | 'down' | 'unknown' (errno class from classify_connect_failure)
    error         TEXT                               the failure sentence; NULL when up
    latency_ms    INTEGER
    context       INTEGER                            per-slot window
    slots_total   INTEGER, slots_busy INTEGER        llama.cpp, Strata
    requests_running INTEGER, requests_waiting INTEGER
    kv_cache_usage REAL                              vLLM, 0..1
    gen_tps       REAL                               live speed when the backend reports one (Strata)
    since         REAL                               server start epoch when known (Strata totals.since)
    INDEX probes_endpoint_t (endpoint_id, t)

  probe_counters    the cumulative counters as parsed, one row per probe; all REAL, NULL when absent
    probe_id      INTEGER PRIMARY KEY REFERENCES probes(id)
    prompt_tokens, prompt_tokens_cached, prompt_seconds, generated_tokens, generated_seconds,
    spec_draft_tokens, spec_accepted_tokens, prefix_queries, prefix_hits, preemptions

  probe_hardware    one row per probe when the backend reports hardware (Strata now; SSH nvidia-smi in V2)
    probe_id      INTEGER PRIMARY KEY REFERENCES probes(id)
    gpu_util, gpu_mem_used, gpu_mem_total, gpu_temp, gpu_power, gpu_power_limit,
    gpu_pcie_rx_mb, gpu_pcie_tx_mb, cpu, ram_used, ram_total          REAL

  endpoint_events   id INTEGER PRIMARY KEY, t REAL NOT NULL, endpoint_id INTEGER NOT NULL REFERENCES endpoints(id),
                    kind TEXT NOT NULL CHECK (kind IN ('up', 'down', 'restarted')), detail TEXT
    INDEX endpoint_events_endpoint_t (endpoint_id, t)
  ```

  Rates are computed between consecutive `probe_counters` rows at read time, never stored. A
  reset is any main counter going backwards or `since` changing: it writes a `restarted` event
  and the interval has no rate. `endpoint_load` stays the MCP server's own sampler table: the
  monitor reads it, never writes it. Retention deletes only the monitor's tables. Not stored:
  Strata's per-request table and `history` arrays (the MCP records its own requests; the
  monitor's history is its probe rows) and the `engine` block (static config, read live for
  the detail page).

## 4. Display states

Per endpoint, derived from rows (so a restart reconstructs them from the database):

| state | rule | rendering |
|---|---|---|
| live | last probe up | full content |
| down, recent | up within the attention window (24 h default), unreachable now | full size, alert colour, the probe's sentence; names a job that was running on it when it dropped |
| dormant | not seen within the window | one muted line inside its host card, "last seen <date>"; never hidden, never alarmed |

Cold start with no history: a down endpoint is dormant, never alert (no fourteen alarms).

Per node, derived from its endpoints' probes (`classify_connect_failure` already reads the
kernel's errno): **up** if any endpoint answers, including a refusal (then an unreachable
endpoint is "endpoint down", shown on that line only); **down** if every endpoint reports host
unreachable (one alert at the host card; endpoints greyed, not each alarming); **unknown** if
every endpoint times out, resolved by the port-22 fallback. The attention window applies to
nodes too: a node dormant past the window collapses to one line.

## 5. Page and navigation

Three levels. One HTML page, vanilla JavaScript, uPlot vendored into the package (works with
no internet). One server-sent events stream; the server pushes a state snapshot after every
collection tick; the page redraws in place and never polls. Detail views fetch history over
plain JSON routes. Dark theme by default. A time-range control (15 min to 7 days) applies to
every chart on the page.

**Overview.** One card per host in config order, listing its endpoints. Live: served model,
state strip (Strata's six states where known; up/busy otherwise), slots or queue, decode speed
with a sparkline, jobs running on it. Down-and-recent and dormant as in §4. Below the cards a
running-jobs table: model, endpoint, elapsed, turns, tool calls, current tool, error count,
context fill as a bar, speed, last activity. Below that the fleet charts: decode and prefill
speed per live endpoint on a shared time axis, and occupancy.

**Endpoint detail.** Stat tiles with sparklines for the tiers the endpoint provides (table
below), the context window, the request table with a totals line, and the jobs that ran on it
in the range. A tile with no source is absent, not empty. Our own page for every backend; no
link-out dependency on any endpoint's UI.

| tier | provided by | content |
|---|---|---|
| 0 | every endpoint | up/down, node state, served model, context window, probe latency |
| 1 | llama.cpp `--metrics`, vLLM, Strata | prompt and decode speed, cache hit ratio (counter deltas) |
| 2 | llama.cpp `--slots`, vLLM, Strata | slots busy / requests running and waiting / KV fill |
| 3 | Strata today; V2 node access for others | GPU util, VRAM, temperature, power, PCIe, CPU, RAM, disk; per-request list; conversation cache |

The per-request table for non-Strata endpoints is built from the `requests` table (our jobs'
traffic only).

**Job detail.** Context gauge; request table; the metrics delta the runner recorded; files
touched; a tool-call timeline strip across the job's duration (one mark per call, coloured by
tool, errors red, context fill drawn over it) and a breakdown by tool; transcript tail with a
link to the directory.

## 6. Failure handling

- A failed probe is a row with its failure sentence, never a gap.
- Counter reset (negative delta): record an endpoint event "restarted <time>", skip the rate
  for that interval rather than plotting a cliff.
- A job whose server died mid-run (server row's pid dead, job not finished): shown "lost" with
  its last known state, not running forever.
- Timestamps stored as epoch seconds, rendered in the browser's local time. A range wider than
  the data shows what exists.

## 7. Testing

Every parser is tested against a captured document, never a typed one.

**Fixtures to capture before building:** a fresh Strata `/metrics` from ai1 (the repo's fixture
predates the `hardware`/`history` sections); vLLM `/metrics` and `/v1/models` from a DGX box;
llama.cpp `/props`, `/slots`, `/metrics`; two captures of each counter document a minute apart
for real deltas; one real job's rows from the new tables.

**Unit, no network, no browser:** display state from rows (both sides of the 24 h boundary; cold
start yields dormant); node state from a host's rows (fake connector for the port-22 fallback);
rates from counters including the reset case; probe-interval selection; retention (probe rows
go, job rows stay); schema migration from empty and from version 1.

**Routes, Starlette test client** over a seeded database: the SSE snapshot JSON, endpoint and
job history routes, time-range parsing with a bad range rejected.

**Scripted live data, two subcommands in the package (acceptance depends on them):**
- `localagents seed`: offline, deterministic. Builds a database from the fixtures with
  timestamps shifted to now: three hosts; one live endpoint with an hour of counter history;
  one down since 20 min ago; one dormant since last week; a finished job with requests and tool
  calls; one running job. What the browser smoke and route tests run against; shows every
  display state at once.
- `localagents smoke`: online, no Claude session. (1) a few short `/v1/messages` requests over
  httpx to a named endpoint; (2) one real job through `run_job` with a fixed brief ("run `date`
  with Bash and report it") on the model given, so the job, request and tool-call tables fill
  through the real shim and runner; (3) a temporary config entry pointing at a port nothing
  listens on, to exercise the down state against the kernel's real refusal; (4) a printed
  checklist of tables that gained rows and states that appeared; non-zero exit on any missing row.

**Browser smoke (by hand or a browser lane at acceptance, not in the unit suite):** open the
overview against the seeded database, confirm host cards and state classes render, open an
endpoint and a job. Static gates cannot see a runtime error in the browser.

**Live smoke at merge:** the monitor against the real `models.yaml` for two minutes with
`localagents smoke` run once: a live endpoint, a dormant one, the job appearing and finishing.

## 8. V2: operations (not in this build; recorded so the MVP leaves room)

- Start/stop/restart of endpoints and periodic `nvidia-smi` on nodes, over SSH with the
  operator's own keys. No secrets stored by localagents. A guided setup script of ours walks
  the operator through key placement and a per-host launch recipe.
- Node telemetry gathered this way fills the same tier-3 tiles Strata fills today, so the data
  model and the page need no change.
- The monitor's host concept and the `monitor:` config section are the hooks.

## 9. Out of scope

Prometheus/Grafana (the backends' `/metrics` stay scrapeable by anyone who wants that), network
or multi-machine databases, GPU telemetry for non-Strata backends before V2, alerting beyond
the page.
