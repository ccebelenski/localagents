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
  database path) is ignored by the MCP server, which ignores unknown top-level keys. (True
  now, 3.7m: the section also takes `refresh_s`, the snapshot push period, default 5 s; the
  snapshot carries it — §5a.) The port
  is checked before uvicorn starts (`port_free`), binding with `SO_REUSEADDR` like uvicorn does:
  a TIME-WAIT socket from a just-closed session does not refuse a restart, but an occupied port
  still exits 2 with one sentence (story 3.1 ruling F, the 3.2 review).
- **Collection loop:** every 5 s probe each endpoint with `Registry.probe` and `fetch_metrics`;
  a dormant endpoint every 60 s. Each probe writes one `probes` row: time, endpoint, up, host
  state, served ids, context, slot/queue numbers, raw counters, failure sentence. Strata's
  `hardware` and `requests` sections stored when present. Rates are computed between
  consecutive rows, never stored.
- **Node fallback probe:** only when every endpoint on a host times out, one TCP connect to port
  22 on the host. Answer or refusal = node up (endpoints down); nothing = node down. A shared
  `host` key whose endpoints live on two boxes gets one fallback, aimed at the first endpoint's
  box (config order) — the key names one box, so keep it to one. An endpoint whose ticks never
  write a probe row keeps its host's fallback from being asked.
- **Jobs:** read from the `jobs`/`requests`/`tool_calls` tables every 2 s (running jobs with
  context fill and speed; finished records for history).
- **Storage:** `probes` (monitor-owned) in the shared database; probe rows older than 7 days
  deleted on the same schedule as job directories, per endpoint through the `(endpoint_id, t)`
  index; event rows share the probe horizon; `host_probes` rows share it too, per host through
  the `(host, t)` index. Schema migrated by version number. The database is
  a cache of the fleet, not the record of truth: deleting it costs history only.

  Monitor tables, approved 2026-10-29 (schema version 3; version 2 is `jobs.compactions`,
  story 1.11; version 4, story 3.1a ruling A, appends `probes.state` and the two
  `probe_hardware` disk columns in one step; version 5, story 3.1b, appends `host_probes`
  in one step). Conventions as the MCP schema: epoch REAL timestamps, lookups by id, flat
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
    state         TEXT                               live state string (version 4): Strata's live.state
                                                   verbatim; other backends "busy" when
                                                   requests_running > 0 or slots_busy > 0, else "idle";
                                                   NULL when the probe is down
    INDEX probes_endpoint_t (endpoint_id, t)

  probe_counters    the cumulative counters as parsed, one row per probe that reported counters
                    (none for a down probe or a document without counters); a column is NULL when
                    its counter is absent
    probe_id      INTEGER PRIMARY KEY REFERENCES probes(id)
    prompt_tokens, prompt_tokens_cached, prompt_seconds, generated_tokens, generated_seconds,
    spec_draft_tokens, spec_accepted_tokens, prefix_queries, prefix_hits, preemptions

  probe_hardware    one row per probe when the backend reports hardware (Strata now; SSH nvidia-smi in V2)
    probe_id      INTEGER PRIMARY KEY REFERENCES probes(id)
    gpu_util, gpu_mem_used, gpu_mem_total, gpu_temp, gpu_power, gpu_power_limit,
    gpu_pcie_rx_mb, gpu_pcie_tx_mb, cpu, ram_used, ram_total          REAL
    disk_read_mb, disk_write_mb (version 4)                           REAL

  endpoint_events   id INTEGER PRIMARY KEY, t REAL NOT NULL, endpoint_id INTEGER NOT NULL REFERENCES endpoints(id),
                    kind TEXT NOT NULL CHECK (kind IN ('up', 'down', 'restarted')), detail TEXT
    INDEX endpoint_events_endpoint_t (endpoint_id, t)

  host_probes     (version 5, story 3.1b) the node fallback's answers, monitor-owned, pruned with
                    the probes: one row per port-22 ask, at most one per host per minute — asked
                    only when every endpoint on the host timed out, so a restart reconstructs the
                    node state from these rows
    id            INTEGER PRIMARY KEY
    t             REAL NOT NULL
    host          TEXT NOT NULL                     the endpoints' `host` (the grouping key)
    address       TEXT NOT NULL                     the first endpoint's base_url hostname (config order)
    answer        TEXT NOT NULL                     'answered' | 'refused' | 'nothing'
    INDEX host_probes_host_t (host, t)
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
| dormant | not seen within the window | one muted line inside its host card, "last seen <date>"; never hidden, never alarmed. (True now, 3.7a/3.7b, operator 2026-10-29: a never-seen endpoint is not drawn, a host with nothing to draw and its node not down is not drawn, and one muted line under the cards counts them; a seen-dormant endpoint stays a muted line inside a normal card; nothing collapses to a bar.) (True now (3.7y, 2026-10-30): a never-seen endpoint is hidden on a down node too, a down node with nothing left to draw has no card, and the hidden-count line names it "<endpoint> (node down)".) |

A probe older than the window, up or not, counts as not seen: a monitor that was stopped for longer than the window shows every endpoint dormant until its first tick (ruling 2026-10-29).

Cold start with no history: a down endpoint is dormant, never alert (no fourteen alarms).

Per node, derived from its endpoints' probes (`classify_connect_failure` already reads the
kernel's errno): **up** if any endpoint answers, including a refusal (then an unreachable
endpoint is "endpoint down", shown on that line only); **down** if every endpoint reports host
unreachable (one alert at the host card; endpoints greyed, not each alarming); **unknown** if
every endpoint times out, resolved by the port-22 fallback. The attention window applies to
nodes too: a node dormant past the window collapses to one line. (True now, 3.7b/3.7c, operator
2026-10-29: nothing collapses — every host, down or all-dormant, is a card of the same shape as
a live host's; a down node keeps the `node-down` border and its one alert sentence in the card
head.) (True now, 3.7x, operator 2026-10-30: the card head's alert is the one word "down" (or
"unknown"); the probe's sentence rides in that note's title and on the endpoint page, so it no
longer sets the card column's floor.) (True now, fold of the batch review F1, 2026-10-30: an
unknown node is quiet the same way — the head note "unknown", the sentence in its title, no
`.ep-error` line.) (True now (3.7y, 2026-10-30): a never-seen endpoint is
hidden on a down node too, a down node with nothing left to draw has no card, and the
hidden-count line names it "<endpoint> (node down)".)

## 5. Page and navigation

Three levels. One HTML page, vanilla JavaScript, uPlot vendored into the package (works with
no internet). One server-sent events stream; the server pushes a state snapshot after every
collection tick; the page redraws in place and never polls. Detail views fetch history over
plain JSON routes. Dark theme by default. A time-range control (15 min to 7 days) applies to
every chart on the page.

**Overview.** One card per host in config order, listing its endpoints. Live: served model,
state strip (Strata's six states where known; up/busy otherwise), slots or queue, decode speed
with a sparkline, jobs running on it. Down-and-recent and dormant as in §4. Below the cards a
Recent jobs log (story 3.7g): job, status pill, model, endpoint, elapsed, turns, tool calls,
tool errors, context fill as a bar, speed, last activity — running and queued first, then the
last 10 finished with elapsed frozen. True now (3.10a, 2026-10-30): a "started" column
(day-stamped) follows the job column (its cell is the job id, first eight characters since
3.10f-2); the "server gone" badge is gone — a live job whose server
stopped beating wears the pill "lost"; "speed" is two columns, prefill tok/s and decode tok/s. Below that the fleet charts: decode and prefill
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

True now (3.10d, 2026-10-30): the tiers are the spec's grouping of data sources, not page
headings — the page draws the tiles in that order under plain labels (speeds, occupancy,
hardware) with no numbering; the endpoint/node state tiles are gone (the header says it, with
the port-22 note); tile values are never cut, the tile grows (the 3.7u rule).

The per-request table for non-Strata endpoints is built from the `requests` table (our jobs'
traffic only).

**Job detail.** Context gauge; request table; the metrics delta the runner recorded; files
touched; a tool-call timeline strip across the job's duration (one mark per call, coloured by
tool, errors red, context fill drawn over it) and a breakdown by tool; transcript tail with a
link to the directory. True now (story 1.14): the header's "server" figure is the build stamp
of the MCP server that ran the job (`server_version`), and when the snapshot's `build` (the
monitor's own stamp) differs from it a muted "server code differs from the monitor's" sits
beside it — a server process left running on older code is visible on its jobs.

## 5a. Routes and JSON shapes (story 3.1)

The contract between the server (`localagents/monitor/app.py`, `read.py`, `ranges.py`) and the
page (stories 3.2-3.4). Field names are exactly what the code emits; a change to either side
changes this section in the same commit. Times are epoch seconds (REAL); every number not
stated otherwise may be `null` when its source did not report it. Values in the examples are
illustrative.

**Routes.**

| route | answer |
|---|---|
| `GET /` | the page: `monitor/static/index.html` from the package |
| `GET /static/{path}` | a file of the package's `monitor/static/` directory (`app.js`, vendored uPlot); 404 otherwise; every answer carries `Cache-Control: no-cache` (story 3.2a: the browser revalidates against the ETag/Last-Modified StaticFiles computes, so an upgrade never runs stale page code) |
| `GET /api/snapshot` | the snapshot object (below): what `/events` pushes, on demand (True now, 3.7p: served from the current push's shared cache — the bytes every stream of that push received — and only computed, once, when no snapshot of the current push exists yet; True now, 3.7-fold-2 D4: the cache entry also expires by age — older than `refresh_s` recomputes — so a `--no-collect` server, whose push counter never moves, is at most `refresh_s` stale rather than frozen at its startup snapshot) |
| `GET /api/endpoints/{name}/history?range=<r>` | the history object of the endpoint named `{name}` in models.yaml; `range` defaults to `1h` (True now, 3.7p: bodies are cached per endpoint and keyed on the endpoint's newest probe t; True now, 3.7-fold-2: the range half of the key is the parsed window in SECONDS, so `1h` and `60m` are one entry and the cache is bounded by the windows the page can ask for) |
| `GET /api/fleet/history?range=<r>` | the fleet history object (below): speed and occupancy for the whole fleet on one time grid; `range` defaults to `1h` |
| `GET /api/overview/history?range=<r>` | story 3.7p: one body combining the fleet history and the histories of the live endpoints, and (story 3.7l) the jobs overlapping the window for the fleet timeline (all below); `range` defaults to `1h` |
| `GET /api/endpoints/{name}/requests?range=<r>` | the endpoint's request table object (below): our jobs' requests that ran on it in the range, with totals; `range` defaults to `24h` |
| `GET /api/jobs?range=<r>` | an array of job summaries, jobs **created** within the range, newest first, at most 500; `range` defaults to `24h` |
| `GET /api/jobs/{id}` | the job detail object of the job with that `id` |
| `GET /events` | the server-sent events stream (below) |

**Errors** are `{"error": "<code>", "message": "<sentence>"}`:

| code | status | when |
|---|---|---|
| `bad_range` | 400 | `range` is malformed or outside 15m..7d; the message names the text |
| `unknown_endpoint` | 404 | `{name}` is not an endpoint in models.yaml (checked before the range) |
| `unknown_job` | 404 | no `jobs` row has that id |
| `database_error` | 503 | any `sqlite3.Error` or `db.SchemaError` in the route; message `"<ExceptionName>: <text>"` |

Every route's database work runs on a worker thread (`asyncio.to_thread`) with its own
connection from the factory, closed when the call ends; a connection is never shared across
threads.

**Range grammar.** A whole number and one unit letter, nothing else: `<n>m`, `<n>h`, `<n>d`
(`15m`, `90m`, `6h`, `2d`); no spaces, sign, fraction, upper case or other unit (`1w`, `30s`,
`"1h "` are malformed). From 15 minutes to 7 days, both included (7 days is the probe
retention). The page offers `15m`, `1h`, `6h`, `24h`, `7d` (`ranges.RANGES`). A range `r`
covers `[now - r, now]`, both ends included.

**Snapshot** (`GET /api/snapshot`, and the data of every `snapshot` event):

```
{
  "now": 1793288065.0,              the server's clock when it was built
  "window_s": 86400.0,              the attention window: monitor: attention_hours × 3600
  "refresh_s": 5.0,                 story 3.7m: the fan-out's one refresh clock — the minimum
                                    spacing of snapshot pushes; monitor: refresh_s, default 5,
                                    in (0, 3600] (the page may say how often it is told)
  "db_path": "/home/op/.local/state/localagents/localagents.db",
                                    the database file this server opened (serve's --db, else the
                                    `monitor:` section's `db`, else `db.db_path()`); named by the
                                    snapshot so the page can say what it is showing (story 3.2a);
                                    null only when the app was built without one
  "build": "0.2.0+8ce7d91",         story 1.14: the monitor's own build stamp (`db.build_stamp`:
                                    `<version>+<short sha>`, the version alone when no checkout
                                    or git answers), computed once at the app's start
                                    (`app.state.build`); the job page compares it with the job's
                                    `server_version` and says "server code differs from the
                                    monitor's" (muted) when they differ; null only when the read
                                    was not given one
  "hosts": [ host, ... ],           models.yaml order: endpoints grouped by `host`, first-seen order
  "jobs": [ running_job, ... ],     the page's Recent jobs log (story 3.7g): every queued or
                                    running job oldest first, then the last 10 finished jobs
                                    (any terminal status) newest-finished first; a finished
                                    row's live figures are frozen at its finish, not re-read
                                    against now
  "events": [ event, ... ]          the newest 20 endpoint_events, newest first
}

host = {
  "name": "ai1",                    the endpoints' `host` (config default "local")
  "state": "up" | "down" | "unknown",   state.node_state over each endpoint's LAST probe
                                    (endpoints with no probe are left out), the all-timeout case
                                    resolved by the port-22 fallback below: "answered"/"refused"
                                    -> up (the box answers), "nothing" -> down, absent or stale ->
                                    "unknown"
  "dormant": false,                 state.node_dormant: every endpoint dormant (collapse the card)
  "fallback": "refused",            the newest host_probes.answer within 120 s of now (story 3.1b:
                                    the port-22 ask, at most one per host per minute), else null
  "fallback_t": 1793288055.0,      its t, null with fallback
  "endpoints": [ endpoint, ... ]    config order
}

endpoint = {
  "name": "ai1",
  "backend": "strata",              as configured
  "base_url": "http://10.0.0.50:8080",   no trailing slash
  "host": "ai1",                    the endpoint's `host` from models.yaml: the card it lives
                                    under (story 3.2a: a page that re-sorts needs it beside it)
  "state": "live" | "down" | "dormant",  state.endpoint_state (§4)
  "last_seen": 1793288065.0,        t of the newest up probe, or null (never seen up)
  "last_active": 1793287000.0,      story 3.7s: the t of the newest probe whose counters row
                                    advanced generated_tokens or prompt_tokens_processed over the
                                    previous counters row of that endpoint (the pairing
                                    rates.rates uses; an interval that is a reset never counts).
                                    True now, 3.7-fold-2 (D5): stored — the collector writes that
                                    tick's `t` into the endpoint's row (`endpoints.last_active`,
                                    schema version 6) inside the tick's own transaction, and the
                                    read is that one value; null when no witness has ever been
                                    written (a pre-v6 file gets it backfilled once from its
                                    retained rows at migration). A backend
                                    that writes no counters (llamacpp/vllm/strata do; ollama and
                                    other do not) is witnessed by the newest requests row's t
                                    on its jobs instead, else null. Not "last seen": a probe
                                    that finds the engine idle advances nothing
  "model": "qwen3.8-flash-next-iq3_s",   the last probe's served id, or null
  "context": 131072,                the last probe's per-slot window
  "probe_state": "busy",            the newest probe's own `state` string (idle/busy; the
                                    history series' same value), null on a down probe or with
                                    no probe (story 3.2a: the strip used to pay a history call
                                    per live endpoint for this one word)
  "last_model": "DeepSeek-V4.1-Flash-1017", "last_context": 307712,
                                    state "down" or "dormant" only: the newest UP probe's
                                    served id and per-slot window — what the engine last served
                                    here, shown while the live pair above is null (3.3's gap);
                                    null while live, and when it was never seen up
  "slots": {"total": 1, "busy": 1} | null,          null when the last probe has no slot count
  "requests": {"running": 1, "waiting": 0} | null,  null when it has neither figure
  "kv_cache_usage": 0.17,           vLLM, 0..1
  "latency_ms": 12,
  "since": 1793028865.0,            the server's start epoch when known (Strata)
  "error": "...",                   the last probe's failure sentence when it is down, else null
  "dropped_job_id": "0f9c…",        state "down" only: the id of the job that was running on it
                                    at its first down probe after last_seen
                                    (jobs.running_job_at), so the sentence is clickable
                                    (3.3's gap, story 3.2a); else null
  "rates": {                        rates.rates between the LAST TWO probes
    "prompt_tps": 1.2, "gen_tps": 0.6, "cached_tps": 134.8, "spec_acceptance": 0.822,
    "reason": null                  null when computed; else why the four are null: "no probes",
                                    "one probe: no interval yet", "the last probe has no counters",
                                    "the probe before the last has no counters" (a gap), or the
                                    reset sentence from rates.reset_reason. A single value
                                    may still be null with reason null when the counters do not
                                    carry its series (spec_acceptance with no drafts)
  },
  "gen_tps_live": 9.8,              the last probe's own speed figure (Strata)
  "hardware": {gpu_util, gpu_mem_used, gpu_mem_total, gpu_temp, gpu_power, gpu_power_limit,
               gpu_pcie_rx_mb, gpu_pcie_tx_mb, cpu, ram_used, ram_total,
               disk_read_mb, disk_write_mb} | null,
                                    the LAST probe's probe_hardware row; null when that probe has
                                    none (a down probe never has one, so a stale reading is never
                                    shown as current)
  "jobs_running": 1                 queued/running jobs whose endpoint row is this endpoint's
}

running_job = {
  "id", "model", "endpoint",
  "status",                         live: "queued" | "running"; on a finished log row
                                    (story 3.7g) a terminal value: "succeeded" | "failed" |
                                    "cancelled" | "timeout" | "lost" (jobs.py's done check)
  "started_at",                     started_at, or created_at when it has not started
  "finished_at",                    null while running; the finish time on a terminal row
  "elapsed_s",                      now - started_at on a live row, finished_at - started_at
                                    (the run's length, frozen) on a finished row, 1 decimal
  "num_turns", "tool_calls" (count), "errors" (tool calls with is_error),
  "last_tool",                      the DISPLAY form of derive.last_tool — "<tool>: <input>" or
                                    the tool name, ONE line with whitespace collapsed, paths
                                    under the job's root relative to it (its worktree path, else
                                    its cwd), cut to 120 chars after relativising: exactly what
                                    the status bar shows (status.display_activity, story 1.11b's
                                    rule, story 3.2a); null in, null out
  "last_activity",                  the same display form of jobs.last_activity; the RAW text
                                    stays on the job summaries (`GET /api/jobs`) and on
                                    `GET /api/jobs/{id}` (the detail route's summary)
  "ctx_tokens", "context", "ctx_pct" (round(100 × ctx_tokens / context), or null),
  "gen_tps",                        derive.gen_tps rounded to 1 decimal, or null
  "prefill_tps",                    derive.prefill_tps rounded to 1 decimal, or null — the job's
                                    processed prompt tokens (input + cache_creation) per second
                                    of prefill (ms − gen_ms), story 3.10a
  "compactions",
  "files",                          count of distinct files touched (derive.files_touched)
  "owner",                          the job's MCP server's owner pid (the Claude Code session)
  "server_version",                 story 1.14: that server's build stamp (its
                                    `mcp_servers.version`, written once at its start); null
                                    when the server row is gone
  "server_live"                     true when that server is alive: not stopped, a heartbeat
                                    within 60 s, its pid alive (the status line's rule); false
                                    means the job's record says running but nothing is running
                                    it (spec §6 "lost")
}

event = {"t", "endpoint" (its name), "kind" ("up" | "down" | "restarted"), "detail"}
```

True now (3.10f-2, 2026-10-30): a job has one key, its UUID; the pages show its first eight
characters with the full id in the title; `name`, `dropped_job` and `job_name` are gone from
every shape (`dropped_job_id` and `job_id` stay).

An endpoint in the database but not in models.yaml is ignored: it has no host card and its
events are left out of `events`. A configured endpoint with no `endpoints` row yet (never
probed, never ran a job) is listed, dormant, every figure null; the read creates no row (the
collector's first tick does). Jobs are listed whatever endpoint they ran on. A finished log
row's derived figures are computed once per monitor process and served from that cache after
its first finished push (only `server_live` re-reads the server row, and a finished row's
`elapsed_s` is the run's frozen length); the store's write rules make the cached figures
static — after a job's terminal row UPDATE nothing writes its requests or tool_calls rows
(story 3.7-fold-4s). A terminal row whose `finished_at` is NULL (no write path produces one;
the schema allows it) reports `elapsed_s` null — a finished row never shows a moving number
(story 3.7-fold-4s).

**Endpoint history** (`GET /api/endpoints/{name}/history`):

```
{
  "t": [ ... ],                     the probes' times in the range, oldest first
  "step": 2,                        k: every k-th probe was kept (1 = every probe)
  "prompt_tps": [ ... ],            prompt tokens processed per second       ← tier 1
  "gen_tps": [ ... ],               tokens generated per second               ← tier 1
  "cache_hit": [ ... ],             cached / (processed + cached) over the interval, 0..1;
                                    null when the interval had no prompt tokens
  "slots_busy": [ ... ],                                                      ← tier 2
  "requests_running": [ ... ], "requests_waiting": [ ... ],
  "kv_cache_usage": [ ... ],
  "latency_ms": [ ... ],              each kept probe's own probe latency     ← tier 0 (ruling B)
  "state": [ ... ],                   each kept probe's probes.state: "idle"/"busy" or Strata's
                                      own string, null while down or before version 4  ← tier 0
  "gpu_util": [ ... ], "gpu_mem_used": [ ... ], "gpu_temp": [ ... ], "gpu_power": [ ... ],
  "gpu_pcie_rx_mb": [ ... ], "gpu_pcie_tx_mb": [ ... ], "cpu": [ ... ], "ram_used": [ ... ],
  "disk_read_mb": [ ... ], "disk_write_mb": [ ... ]                                          ← tier 3
  "events": [ {"t", "kind", "detail"}, ... ],     the endpoint's events in the range, oldest first
  "jobs": [ {"id", "status", "started_at", "finished_at", "model"}, ... ]
                                    jobs on the endpoint that overlap the range (started — or
                                    created, if never started — by `now`, not finished before
                                    the range starts), oldest first
}
```

- Every series present has exactly `len(t)` values, aligned with `t`; a probe that did not
  report a figure (every down probe) is a `null` at its place.
- **A series is absent, not empty, when its source holds no value in the range** (§5: "a tile
  with no source is absent"): a gauge or hardware series when every probe in the range has it
  null; `latency_ms` and `state` the same way (a range of only down probes has neither);
  `prompt_tps` when no probe in the range has `prompt_tokens`, `gen_tps` when none has
  `generated_tokens`, `cache_hit` when none has `prompt_tokens_cached` or `prefix_hits`. So a
  vLLM endpoint has no `gpu_*`, `cpu`, `ram_used` or `slots_busy` keys, and Strata no
  `kv_cache_usage`.
- **Downsampling.** With `n` probes in the range and `n > max_points` (600), `k = ceil(n /
  600)` and every k-th probe is kept, counted back from the newest, which is always kept;
  otherwise `k = 1`. Gauges and hardware are the kept probe's own values.
- **Rates.** A point's rates are `rates.rates` from the previous kept probe to this one. For
  the first kept point the reference is the probe `k` before it; when that lies before the
  start of the range, the newest probe before the range stands in instead, if there is one;
  the first point has no rate when neither exists — the endpoint's first probe ever, with
  nothing before it. So a downsampled rate is the average over its interval, not a 5 s
  sample. The
  rates are null when any raw interval between consecutive probes inside it is a **gap**
  (either probe has no counters row: a down probe, or a backend without `/metrics`) or a
  **reset** (`rates.reset_reason`: a counter went backwards or `since` changed; §6: no
  cliff). The first probe an endpoint ever wrote has no rate.
- An endpoint configured but never seen answers `{"t": [], "step": 1, "events": [], "jobs": []}`.

**Endpoint requests** (`GET /api/endpoints/{name}/requests`; review of 3.1, ruling D — §5's
"per-request table for non-Strata endpoints … built from the `requests` table"):

```
{
  "requests": [ a job detail request row — {"path", "t", "http_status", "ms", "gen_ms",
                "tokens", "stream", "context_exceeded", "error"} — plus "job_id", ... ]
                                    the requests of jobs on this endpoint (joined through
                                    jobs.endpoint_id) whose t is in the range, newest first,
                                    at most 1000
  "totals": {                       the whole range, capped list or not:
    "count": 152,                   requests in the range
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
    "ms",                           the ms sum (NULLs skipped)
    "errors"                        requests whose error column is set: not null and not
                                    the empty string a clean export request carries (review
                                    of 3.3); the job detail page's client-side totals count
                                    errors by this same rule
  }
}
```

The endpoint page's requests table and the job page's requests table (below) are drawn through
one renderer, `app.js`'s exported `requestsTable`: the endpoint's with a job column (its rows
span jobs), the job page's without (every row is that job). The columns are time · status · job
· path · prompt · reused · output · prefill t/s · decode t/s · duration · error. Status is a
pill with the word (`ok` for a 2xx, `error` otherwise; the HTTP code in its title; a null status
reads "–"); prompt is `tokens.input`, reused is `tokens.cache_read`, output is
`tokens.output`; error is the row's error, cut to 60 characters with the full sentence in its
title, or "context exceeded" when `context_exceeded` is true and the error is null (True-now,
3.7-fold-4: the stored type is TEXT — the backend's own sentence, shim.py — never a boolean; the
cell reads the row's error first, then falls back to "context exceeded" when the column holds a
non-empty string or true). The derived
columns are: **prefill t/s** = (input + cache_creation) / ((ms − gen_ms) / 1000), when `ms`
and `gen_ms` are numbers, `ms > gen_ms` and `input + cache_creation > 0`; **decode t/s** =
output / (gen_ms / 1000), when `gen_ms > 0` and `output > 0`; **duration** = `ms / 1000`. Each
is blank when its inputs are missing (a non-stream request may have no `gen_ms`; the hello
handshake rows have no tokens). True now (3.10h, 2026-10-30): gen_ms runs from the first chunk
carrying generated content (content_block_start/delta), not the first byte — message_start
precedes prompt processing — so ms − gen_ms is time-to-first-token and the prefill rate derived
from it is honest. Strata's prefill is "input − reused" because its input is the
whole prompt; our `tokens.input` excludes cache reads (§5a's prompt is input + cache_read +
cache_creation, `derive._PROMPT_SUM`), so the tokens a request actually prefilled are input +
cache_creation — the analogue of Strata's processed count (`prompt_tokens − cached`,
`registry.py:589`). Strata's VRAM hit rate is its own figure and has no counterpart here; it is
not invented. True now (3.7v, 2026-10-30): the job page's header says "N tool errors" (tool
results flagged is_error) and "N request errors" (requests outside 2xx or with an error text)
side by side; a `/api/hello` handshake row is labelled "handshake" and counted as neither.

**Fleet history** (`GET /api/fleet/history`; review of 3.1, ruling C):

```
{
  "t": [ ... ],                     ONE grid for every endpoint: the union of the kept probe
                                    times across the endpoints, each endpoint downsampled by
                                    the endpoint-history rules first; the union itself kept
                                    to at most 600 points by the same every-kth-from-the-
                                    newest rule, oldest first
  "step": 1,                        k for that union grid (1 when it already fits 600)
  "endpoints": {
    "<name>": {                     every CONFIGURED endpoint with at least one probe in the
                                    range — a dormant one appears with all-null series; an
                                    endpoint with no probe in the range is absent
      "gen_tps": [ ... ], "prompt_tps": [ ... ],     the endpoint's own rates (as its history
                                                     computes them), placed on the grid
      "occupancy": [ ... ]          slots_busy for a backend that reports slots, else
                                    requests_running (vLLM)
    }, ...
  }
}
```

A value at a grid time is the endpoint's own value at its kept probe nearest that time, and
**null where the endpoint has no kept probe within half a grid step of it**: the grid can be
finer than one endpoint's cadence (or coarser, after the union is downsampled), and an
endpoint's absent series (null throughout) is served as a column of nulls rather than dropped.

**Overview history** (`GET /api/overview/history`, story 3.7p): the page's one call — it used
to fetch the fleet grid plus one history per live endpoint every refresh period.

```
{
  "fleet": { ... },                 the fleet history object above
  "endpoints": {                    every endpoint whose snapshot state is "live" at that
                                    moment (the overview's own classification, §4), each the
                                    endpoint history object above; not a second rule
    "<name>": { ... }, ...
  },
  "jobs": [                         story 3.7l (`read.jobs_overlapping`): the job spans the
                                    fleet timeline draws — every job OVERLAPPING the window
                                    (`started_at` set and at or before its end, unfinished or
                                    `finished_at` at or after its start), oldest start first,
                                    no cap and no summary: a timeline row reads the spans, it
                                    does not enumerate the log
    { "id": "...",                   the five fields (3.10f-2: no name — the id is the key);
      "endpoint": "...",             `endpoint` is the endpoints row's name (null when the job
      "status": "...",               has no endpoint row); a running job's `finished_at` is
      "started_at": ...,             null and draws to now on the page
      "finished_at": ... }, ...
  ]
}
```

Each of the three history routes is cached server-side per (endpoint name or `"fleet"`,
range text) and keyed on the newest probe `t` (for `"fleet"`, the newest over the configured
endpoints): a request between probes is a cache hit, and the first request after a new probe
recomputes. The cache holds the newest entry per key. `/api/overview/history` shares the same
cache entries, so the per-endpoint route is a hit after it. Story 3.7l: the `jobs` list is
cached under a `"jobs"` key keyed on the FLEET's newest probe `t` (the same value the fleet
entry uses): a job that starts between two probes appears at the next probe, at most
`refresh_s` late — the timeline draws minute-wide spans, so a job's bar appearing one push
late is inside the drawing's own resolution.

**Job summaries** (`GET /api/jobs`): each is `derive.summary_from_row(include_result=False)`,
the `job_status` dict: `id`, `task` (first 300 chars), `model`, `endpoint`,
`base_url`, `cwd`, `run_cwd`, `isolation`, `status`, `created_at`, `started_at`,
`finished_at`, `session_id`, `error`, `num_turns`, `compactions`, `worktree`,
`last_activity`, `log_dir`, `dump`, `log_events`, `context`, `metrics` (the seven-column
`/metrics` delta, or null), `usage` (`input_tokens`, `output_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens`), `tool_calls` (count),
`tool_counts` (`{tool: {"calls", "errors"}}`), `errors`, `last_tool`, `files_touched` (list),
`ctx_tokens`, `gen_tps`, `prefill_tps`, `elapsed_s` (to the wall clock while unfinished),
`server_version` (story 1.14: the build stamp — `<version>+<short sha>`, `db.build_stamp` — of
the MCP server that ran the job, its `mcp_servers.version`; null when that row is gone). No `result`.

**Job detail** (`GET /api/jobs/{id}`): the same dict **with** `result`, and:

```
  "requests": [ {"path", "t", "http_status", "ms", "gen_ms",
                 "tokens": {"input", "output", "cache_read", "cache_creation"},
                 "stream" (bool | null), "context_exceeded", "error"}, ... ]     time order
  "tool_call_log": [ {"t", "finished", "tool", "tool_use_id", "input",
                      "is_error" (bool; null while the result has not arrived)}, ... ]   time order
  "transcript_tail": "...",         the last 80 lines of <state_dir>/jobs/<id>/transcript.txt,
                                    joined by "\n"; null when there is none
  "transcript_note": null           null when there is a tail; else the sentence saying why not
                                    (the job directory is gone — pruned after the retention days,
                                    or never created on this machine — or it holds no transcript.txt)
```

The job page's requests table is the endpoint requests block's one renderer (`requestsTable`),
with the job column omitted: these rows carry no `job_id` and are in time order.

In the job detail the LIST of calls lives under `tool_call_log` and `tool_calls` stays the
count (its length is `len(tool_call_log)`); in a job summary `tool_calls` is the count. Named
apart on purpose (review of 3.1, ruling E): one key, one type, on every route.

**Server-sent events** (`GET /events`; `Content-Type: text/event-stream`, `Cache-Control:
no-cache`):

- on connect: one `event: snapshot` whose `data:` is the snapshot object as one JSON line;
- then one `event: snapshot` each time the collector ticks (`collect.run(on_tick=...)` sets
  the app's tick after each endpoint's tick that wrote its rows). Ticks that arrive while a
  stream is building or sending a snapshot wake it once more: one next snapshot carries them all;
  (True now, 3.7m: ticks are coalesced by the fan-out's one refresh clock — a tick starts a
  `refresh_s` settle period, further ticks inside it join the same round without extending it,
  and every subscriber is woken once at its end; no snapshot is sent between pushes, and a
  stop still wakes and ends the streams at once). True now, 3.7p: the snapshot is read and
  serialised ONCE per push (a per-push counter keys the cache); every subscriber of that push
  yields the same bytes, and a database error is the same `error` event for all of them.
  True now, 3.7-fold-2 (D4): when the server runs without a collector (`--no-collect`) no tick
  will ever arrive, so the fan-out self-ticks — the same one refresh clock waits a period, calls
  a period with no tick a round of nothing, and pushes: an open page keeps following the jobs
  the MCP server writes, one snapshot per `refresh_s`; a real tick still pre-empts and rides
  the same push;
- `event: error` with data `{"error": "database_error", "message": ...}` in place of a
  snapshot the database could not answer; the stream goes on;
- a `: keepalive` comment line after 15 s with nothing sent;
- the stream ends when the server stops (SIGINT/SIGTERM).

**The command.** `localagents monitor [--port N] [--host 127.0.0.1] [--db PATH] [--config PATH]
[--no-collect]`: the port is `--port`, else `monitor: port`, else 8765; the database is
`--db`, else `monitor: db`, else `db.db_path()` (`LOCALAGENTS_DB`, else the state dir). The
collector (`collect.run`, with `monitor: retention_days` pruning and `attention_hours` as the
window) and uvicorn run on one loop; SIGINT or SIGTERM sets one stop event that ends both.
`--no-collect` serves the file as it is (no probes, no pruning): the seeded demo. True now,
3.7-fold-2 (D4): a `--no-collect` server still refreshes the page — the fan-out self-ticks every
`refresh_s` and `/api/snapshot` answers a cache entry at most `refresh_s` old.

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
  timestamps shifted to now: three hosts — **ai1** (Strata, live, an hour of probes at 5 s
  with the busy ai2 capture's counters interpolated across it, a running job on its one slot),
  **dgx1-a** (vLLM, down since 20 min ago — a refused connection: the box answers, nothing
  listens), and **llamacpp** (dormant, last up a week ago) — plus a finished job with requests
  and tool calls (the real story-1.9 export, re-timed to end 10 min ago) and a running job
  (the same export truncated to its first 40 requests and 50 tool calls, started 5 min ago).
  What the browser smoke and route tests run against; shows every display state at once. A
  naive ISO `--now` is read in local time; the file is a snapshot at `--now` whose states decay
  after the attention window (re-seed to see them). True now: with or without `--force`, the
  seed refuses a database a running collector is writing — a `probes` row within the last 120 s
  of the wall clock — naming how long ago it wrote, since reseeding renumbers the endpoints under
  the collector's cached ids; stop the monitor first, or point `--db` at a throwaway file.
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
