# MCP server SQLite schema, version 1 (Epic 1)

Agreed in conversation 2026-10-29; revised after an independent read-only review by
Qwen3.8-Flash-Next on ai1 (job 20261029-031617-adf7; 15 findings, 11 accepted, see the end).

One file: `~/.local/state/localagents/localagents.db` (`XDG_STATE_HOME` honoured as
`state_dir()` does; `LOCALAGENTS_DB=/path` overrides). Written by every localagents MCP server
process directly; the monitor (Epic 2) adds its own tables later. Timestamps are epoch seconds
(REAL). Nothing in this schema is ever deleted by the server: liveness is computed from
heartbeat and pid, rows are history.

Rules that shaped it: a key is only a key (no readable or dual-purpose keys); nothing stored
twice (derivable values are derived at read time, each derivation defined below); repeated
strings that are real dimensions get a lookup, written on first sight; secrets (`api_key`)
are never stored.

```
mcp_servers                         -- one row per localagents process (one per Claude Code session)
  id          TEXT PRIMARY KEY      -- uuid4, minted at start
  pid, owner  INTEGER               -- owner: Claude Code pid that spawned it
  cwd, config, version, transport   TEXT
  shim_port   INTEGER
  started, updated, stopped   REAL  -- updated: heartbeat every 10 s; stopped: clean exit;
                                    --   stopped null + no heartbeat = crashed

endpoints                           -- every distinct description models.yaml has given
  id          INTEGER PRIMARY KEY
  name, base_url, backend, host   TEXT
  first_seen  REAL
  UNIQUE (name, base_url, backend, host)

endpoint_load                       -- the sampler's compact load text, last writer wins;
  endpoint_id INTEGER PRIMARY KEY REFERENCES endpoints(id)   -- superseded by the monitor's probes
  updated     REAL
  load        TEXT                  -- "1/1 slots", "2 run 1 wait kv 34%" (status.load_summary)

models
  id          INTEGER PRIMARY KEY
  served_id   TEXT UNIQUE

paths
  id          INTEGER PRIMARY KEY
  path        TEXT UNIQUE           -- /v1/messages, /v1/messages/count_tokens, ...

jobs
  id            TEXT PRIMARY KEY    -- uuid4; the job_id of every tool, including resume_job
  name          TEXT                -- RETIRED in v9 (3.10f-1, 2026-10-30): was
                                    --   YYYYMMDD-HHMMSS-<first 8 hex of id>, the directory and
                                    --   branch name; a second key whose stamp only proxied
                                    --   created_at. True now: the job's one key is its UUID;
                                    --   the directory, worktree and branch are named by it;
                                    --   name is gone (v9 dropped the column after renaming
                                    --   terminal jobs' directories); prune ages a job
                                    --   directory by its newest mtime.
  mcp_server_id TEXT REFERENCES mcp_servers(id)
  endpoint_id   INTEGER REFERENCES endpoints(id)
  model_id      INTEGER REFERENCES models(id)
  task, cwd     TEXT                -- task: the full brief (task.txt is dropped)
  isolation     TEXT CHECK (isolation IN ('none','worktree'))   -- requested; stored because a
                                    --   worktree job can fail before its tree exists
  status        TEXT CHECK (status IN ('queued','running','succeeded','failed','cancelled','timeout','lost'))
  created_at, started_at, finished_at   REAL
  updated       REAL                -- set on every upsert (Job.changed)
  session_id, result, error, last_activity   TEXT
  num_turns, context   INTEGER      -- context: window handed to the session
  -- metrics delta over the job (endpoint-wide counters; nothing else records these);
  --   all NULL when any counter went backwards during the job (backend restarted)
  generated_tokens, prompt_tokens_processed, prompt_tokens_cached   INTEGER
  cache_hit_ratio, prompt_tps, generate_tps, spec_decode_acceptance   REAL
  worktree      TEXT (JSON)         -- repo, path, root, branch, base, recreated, diffstat, kept, note; read as a unit
  dump, log_events   INTEGER (0/1)  -- which opt-in debug files exist in the job directory
  compactions   INTEGER            -- added in version 2 (story 1.11): how many of the harness's
                                   --   compact_boundary messages the job saw (a trailing
                                   --   indicator of a context-tuning problem)

requests                            -- one row per backend request the shim forwarded
  rowid
  job_id      TEXT REFERENCES jobs(id) ON DELETE CASCADE
  path_id     INTEGER REFERENCES paths(id)
  t           REAL
  http_status INTEGER
  ms, gen_ms  INTEGER               -- whole request; first streamed byte to end
  -- True now (3.10h, 2026-10-30): gen_ms runs from the first chunk carrying generated content
  --   (content_block_start/delta), not the first byte — message_start precedes prompt processing —
  --   so ms − gen_ms is time-to-first-token and the prefill rate derived from it is honest.
  stream      INTEGER (0/1)
  input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens   INTEGER
  messages, tools, bytes   INTEGER  -- request body: message count, tool count, size
  moved_system, folded_text   INTEGER   -- shim normalisation counts
  context_exceeded, error   TEXT    -- backend's original overflow message; first 800 bytes of a 4xx/5xx body
                                    -- True now (3.10h fold, 2026-10-31): the stream's usage is read from its last
                                    --   8 KiB, not its last read, so a message_stop delivered alone no longer hides
                                    --   the output count; a stream that ends before message_stop records `error`
                                    --   'stream ended early: …' with the header's status.
  -- the request's model is not stored: it is the job's model (dropped deliberately)
  -- True now (3.7t, 2026-10-30): Claude Code's startup `/api/hello` handshake (a connection
  --   warm-up) is answered by the shim itself with 200 and writes no row; rows with that path
  --   in older databases are the forwarded handshakes of earlier versions.

tool_calls                          -- one row per ToolUseBlock the runner saw
  rowid
  job_id      TEXT REFERENCES jobs(id) ON DELETE CASCADE
  t, finished REAL                  -- finished: when the matching ToolResultBlock arrived
  tool        TEXT                  -- Read, Edit, Bash, ...
  tool_use_id TEXT
  input       TEXT                  -- an input with a string `command` key (Bash): the first
                                    --   120 chars of the command; else a path-shaped tool — a
                                    --   dict input with a string `file_path`, `notebook_path`
                                    --   or `path` (the first key found, in that order: the
                                    --   edit tools Edit/Write/MultiEdit/NotebookEdit and the
                                    --   readers Read/Glob/Grep/LS that carry one): that path,
                                    --   NULL for an edit tool whose input carries none; any
                                    --   other tool: the first 120 chars of its input JSON
                                    --   (corrected after the 1.5b review: the command rule;
                                    --   amended after 1.11b: the path rule covers any
                                    --   path-shaped tool, not only the edit tools — a Read
                                    --   stores the file it is reading, its JSON head only
                                    --   when it has no path key)
  is_error    INTEGER (0/1)         -- from the matching ToolResultBlock

transcripts                         -- one row per FINISHED job: the transcript file's text as of
                                    --   job completion (version 8, story 1.12; written once in
                                    --   the runner's finally, after the last transcript line and
                                    --   before the jobs row turns terminal. A terminal row has
                                    --   its transcript row whenever a transcript file existed at
                                    --   completion; a job that never logged a line has neither,
                                    --   and readers say so; a `lost` job has none)
  job_id      TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id) ON DELETE CASCADE
                                    --   NOT NULL beyond the design's text: a TEXT PRIMARY KEY
                                    --   admits NULLs and a NULL key skips the foreign key
  text        TEXT NOT NULL
  bytes       INTEGER NOT NULL      -- len(text.encode())
  written     REAL NOT NULL         -- epoch seconds, when the row was written; no index: the
                                    --   primary key is the lookup

indexes
  jobs(name)                        -- RETIRED in v9 (3.10f-1): dropped with the column
  jobs(mcp_server_id), jobs(endpoint_id), jobs(model_id), jobs(status, finished_at),
  jobs(created_at)                  -- added to v1 before the first release (story 1.8); after a
                                    --   release an index is a version-N step
  mcp_servers(updated)
  requests(job_id, t)
  tool_calls(job_id, t)

PRAGMA user_version = 1; journal_mode = WAL; busy_timeout = 5000; foreign_keys = ON (per connection)
```

Version 2 (story 1.11, 2026-10-29): `jobs.compactions` — one appended migration step
(`ALTER TABLE jobs ADD COLUMN compactions INTEGER NOT NULL DEFAULT 0`); a file opens at
user_version 2, and the version-1 history above stands as written.

Versions 3–7 (the monitor's tables, their two extensions, `endpoints.last_active`, and
`jobs_finished_at`) are recorded in `db.py` and in EPICS at their stories. Version 8 (story
1.12, 2026-10-30): the `transcripts` table above — one appended migration step, nothing else;
a file opens at user_version 8. Readers take the row when present, else the file. Database
purge is its own open item (EPICS 1.12): nothing deletes these rows. True now (1.12): a
terminal row has its transcript row whenever a transcript file existed at completion; a job
that never logged a line has neither, and readers say so.

## Derivations (read time; each reproduces today's value)

- **usage totals**: sums of the four token columns over the job's `requests` rows. This
  replaces the SDK's `ResultMessage.usage` in `job_status` (a contract change: the two count the
  same traffic from different sides and can differ by count_tokens calls and failed requests).
- **tool call count**: `COUNT(*)` over `tool_calls`; **counts by tool**: grouped by `tool`
  with `SUM(is_error)`.
- **files touched**: `DISTINCT input` over `tool_calls` where `tool` is an edit tool and
  `input` is not NULL, in first-seen order (today: runner.py EDIT_TOOLS + file_path present +
  deduplicated keeping first seen).
- **ctx_tokens**: the prompt sum (input + cache_read + cache_creation) of the latest
  `/v1/messages` row whose prompt sum is non-zero (today: jobs.py note_usage keeps the older
  value over a null or zero one).
- **gen_tps**: replay of today's EMA, `0.5*prev + 0.5*(output_tokens / span)`, over the job's
  `/v1/messages` rows in `t` order where output_tokens > 0 and span > 0, with span = gen_ms if
  gen_ms else ms. O(requests per job), exact.
- **prefill_tps** (3.10a, not a replay — the screen had no prefill figure before): processed
  prompt tokens (input + cache_creation) per second of prefill (ms − gen_ms), summed over the
  job's `/v1/messages` rows where all four fields are numbers, ms > gen_ms and the processed
  tokens > 0 (the monitor page's own row rule). O(requests per job), exact.
- **run_cwd**: `worktree.path` when the worktree JSON is present, else `cwd`.
- **log_dir**: `state_dir() / "jobs" / id` (True now: 3.10f-1 — the id is the directory name;
  the retired rule was `state_dir() / "jobs" / name`).

## Write rules (multi-process)

- Lookups (`endpoints`, `models`, `paths`): `INSERT OR IGNORE` then `SELECT`; never
  select-then-insert.
- Every read-then-write sequence runs under `BEGIN IMMEDIATE` (a deferred transaction that
  upgrades can hit SQLITE_BUSY immediately, which busy_timeout does not retry).
- `tool_calls`: the runner keeps `tool_use_id -> rowid` for pending calls in memory and sets
  `finished`/`is_error` by rowid, so no index on `tool_use_id` is needed.
- `foreign_keys = ON` is per connection and must be set on every open.
- A transaction holds one statement, or one job's creation; never a probe cycle or a network
  call. Every writer shares one write lock, and a server's `changed()` runs on its event loop
  with a 5 s busy timeout (measured: a 2 s hold by another process stalls the loop 2 s; a hold
  past the timeout drops that update). The monitor's collector commits each probe row on its
  own.
- `synchronous = NORMAL` with WAL: commits survive a process crash without a per-commit fsync;
  only a power loss can lose the last commits, which is the right trade for a row per SDK message.
- On startup a server marks every `queued`/`running` job whose `mcp_servers` row is not live
  (`stopped` set, or no heartbeat for 60 s AND its pid no longer alive: a stalled server is
  not a dead one, 1.7) as `lost` with `error = "server exited"`. That is
  the only status write to another server's job. Correction after 1.10a: the same sweep
  also writes `note` into the `worktree` JSON of a lost job whose tree was never finished (no
  `kept`): "the server exited before finishing the worktree; check <root>", every other key kept.

## Liveness

An `mcp_servers` row is live when `updated` is within 60 s and `stopped` is NULL; `os.kill(pid, 0)`
is a second check, not the first, because pids are reused. A job's server not being live is
what hides it from the status line and what the `lost` rule above acts on.
Correction after 1.7: the `lost` rule acts on a server that is not live only when `stopped` is
set or its pid is gone too (`os.kill` raising ProcessLookupError, or PermissionError: another
user's process), so a stale heartbeat with a live pid of this user is spared as stalled.

## Access patterns the indexes serve

- Status line (`localagents statusline`, every refresh, under 100 ms, stdlib only): live
  `mcp_servers` (index `updated`) joined to their jobs that are queued, running, or finished
  within 120 s (`jobs(mcp_server_id)`, `jobs(status, finished_at)`); per job the latest
  request rows for ctx_tokens and gen_tps (`requests(job_id, t)`); `endpoint_load` rows
  updated within 60 s for the load segments.
- `job_status`: one job by id, grouped counts over `tool_calls(job_id, t)`, sums over
  `requests(job_id, t)`.
- `job_log(tools=N)`: last N rows by `tool_calls(job_id, t)`.
- Monitor (Epic 2): jobs by `endpoint_id` / `model_id` over a time range; requests by job.
- `jobs(name)` — RETIRED in v9 (3.10f-1): the index went with the column, and nothing looks a
  job up by name; the id is the lookup.
- Monitor snapshot (3.9E audit, schema version 7 appended 2026-10-30): the Recent-jobs log's
  finished tail (terminal statuses, `ORDER BY finished_at DESC, rowid DESC LIMIT 10`) walks
  `jobs(finished_at)` (`jobs_finished_at`) backwards, no sort — at version 6 it was a full
  `SCAN j` plus a temp-tree sort of the whole job history on every snapshot. The fleet
  timeline's `jobs_overlapping` OR terms (`finished_at IS NULL OR finished_at >= ?`) take
  seeks of the same index (`MULTI-INDEX OR`); its `started_at` sort stays, over the window's
  rows only.
- `list_jobs(all=True, include_finished=False)` (3.9E audit): the live-only filter is
  `status IN ('queued','running')` and seeks `jobs(status, finished_at)` on the two live
  statuses. The semantically identical `NOT IN` over the five terminal statuses is *not*
  equivalent to the planner: its plan walks `jobs(created_at)` over every job ever run. Keep
  the IN form; `tests/test_query_plans.py` pins both plans and the row-set equality.

## Tool contract decisions

- `job_status`, `job_log` and `list_jobs` read the database and therefore see every server's
  jobs; `list_jobs` gains `all: bool = False` (default: this server's). `wait_job`,
  `wait_jobs`, `cancel_job` and `resume_job` need the in-process task and keep refusing
  another server's job with a clear error.
  Correction 2026-10-29 (story 1.10, operator's rule): the waits act on any job. `wait_job`
  and `wait_jobs` wait another server's job by polling its row (terminal status or timeout,
  as for an in-process job), and `mark_lost_jobs` runs on the foreign wait's clock so a job
  whose server died resolves as `lost`. Only `cancel_job` and `run_agent(resume_job=…)` keep
  the `foreign_job` refusal — they act on the process, not the record.
- `job_status.usage` is the request sums (above).
- `job_log(tail_lines=0)` is the whole transcript (the contract, as `list_jobs(limit=0)` is); a
  negative `tail_lines` is `bad_argument` (story 1.9 item 7).
- `requests.http_status` is always the status the backend answered: the `count_tokens` fallback
  records the backend's 404 with `error="count_tokens unsupported; estimated"` even though the
  shim replies to Claude Code with its own 200 and the estimate (story 1.9 item 8); the
  context-overflow rewrite records the backend's status (vLLM answers 500) and its body in
  `error`, with `context_exceeded` carrying the backend's original message, while the client
  still gets Anthropic's 400 `prompt is too long` envelope (story 1.9a row 2).
  `http_status` 0 is the sentinel for no answer at all — the backend connection failed — and
  `error` says why.
- `mcp_servers.owner` is the pid of the nearest Claude Code process the server runs under
  (`db.session_pid`: an ancestor with `comm` `claude`, or `node` whose command line has an
  argument named `claude` — an npm install runs as node), the parent pid when there is none —
  the same walk `status.my_owner` does, so a session's own jobs match their session on the
  status line (story 1.9 item 9; the node case added in 1.9a row 5).
- `localagents prune` — RETIRED in v9 (3.10f-1): the old rule matched directories named
  `YYYYMMDD-HHMMSS-<hex>{4,}` and aged them by the stamp in the name. True now: it ages any
  directory under `jobs/` whose name is a UUID (the job's id) or the old stamp shape by the
  newest mtime of the directory and its direct children — "nothing written for N days" — and
  leaves a name that is neither shape alone; retention must exceed the longest configured
  `timeout_s`, which the README says.

## What stays a file in the job directory

`transcript.txt` (humans read it; `job_log` tails it) and the opt-in debug dumps
`events.jsonl`, `requests_full.jsonl`. `task.txt`, `requests.jsonl` and `status/<pid>.json`
are no longer written. True now (1.12): transcript.txt stays the live file while the job runs;
at completion its text is copied into `transcripts`, and readers take the row first.

## Known limitation carried over

The shim's SSE usage capture reads the first 64 KB and the last chunk of a stream; a
`message_start` pushed past the head buffer or a `message_delta` not in the last chunk leaves
null tokens on that request row. It was the same in `requests.jsonl`; the table is now the
only copy. Tracked as a follow-up, not fixed here. True now (3.10h fold, 2026-10-31): the
stream's usage is read from its last 8 KiB, not its last read, so a message_stop delivered
alone no longer hides the output count; a stream that ends before message_stop records
`error` 'stream ended early: …' with the header's status.

## Review disposition (job 20261029-031617-adf7)

Accepted: 1 endpoint load (new table); 2 prune pattern; 3 isolation stored; 4 files-touched
rule and the edit-tool `input` rule; 5, 6 derivations defined exactly; 7 usage contract
change stated; 8 `stream` added, `model` dropped explicitly; 9 write rules; 12 log_dir
wording; 13 worktree keys; 14 queued jobs in the status query and NULL metrics on a counter
reset; 15 `http_status` rename and `updated` defined.
Declined: 10 (pid reuse): liveness is heartbeat first, pid second, so a reused pid without a
heartbeat is not live; the `lost` rule covers the stale jobs. 11 in part: `jobs(name)` kept
for ordering; `tool_calls(tool_use_id)` dropped as suggested; `mcp_servers(updated)` added.
Correction after 1.7 (the decline of 10): the `lost` rule now spares a stale row whose pid is
alive, so a crashed server's pid reused by another process of this user delays the sweep of its
jobs until that process exits; a pid reused by another user's process is read as dead.
