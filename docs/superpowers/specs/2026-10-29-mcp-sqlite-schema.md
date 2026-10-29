# MCP server SQLite schema, version 1 (Epic 1)

Agreed in conversation 2026-10-29. One file: `~/.local/state/localagents/localagents.db`
(`XDG_STATE_HOME` honoured as `state_dir()` does; `LOCALAGENTS_DB=/path` overrides).
Written by every localagents MCP server process directly; the monitor (Epic 2) adds its own
tables later. Timestamps are epoch seconds (REAL). Nothing in this schema is ever deleted by
the server: liveness is computed from heartbeat and pid, rows are history.

Rules that shaped it: a key is only a key (no readable or dual-purpose keys); nothing stored
twice (derivable values are derived at read time); repeated strings that are real dimensions
get a lookup, written on first sight; secrets (`api_key`) are never stored.

```
mcp_servers                         -- one row per localagents process (one per Claude Code session)
  id          TEXT PRIMARY KEY      -- uuid4, minted at start
  pid, owner  INTEGER               -- owner: Claude Code pid that spawned it
  cwd, config, version, transport   TEXT
  shim_port   INTEGER
  started, updated, stopped   REAL  -- updated: heartbeat every 10 s; stopped: clean exit;
                                    --   stopped null + dead pid = crashed

endpoints                           -- every distinct description models.yaml has given
  id          INTEGER PRIMARY KEY
  name, base_url, backend, host   TEXT
  first_seen  REAL
  UNIQUE (name, base_url, backend, host)

models
  id          INTEGER PRIMARY KEY
  served_id   TEXT UNIQUE

paths
  id          INTEGER PRIMARY KEY
  path        TEXT UNIQUE           -- /v1/messages, /v1/messages/count_tokens, ...

jobs
  id            TEXT PRIMARY KEY    -- uuid4; the job_id of every tool, including resume_job
  name          TEXT                -- YYYYMMDD-HHMMSS-<first 8 hex of id>; directory and branch name
  mcp_server_id TEXT REFERENCES mcp_servers(id)
  endpoint_id   INTEGER REFERENCES endpoints(id)
  model_id      INTEGER REFERENCES models(id)
  task, cwd     TEXT                -- task: the full brief (task.txt is dropped)
  status        TEXT CHECK (status IN ('queued','running','succeeded','failed','cancelled','timeout'))
  created_at, started_at, finished_at, updated   REAL
  session_id, result, error, last_activity   TEXT
  num_turns, context   INTEGER      -- context: window handed to the session
  -- metrics delta over the job (endpoint-wide counters; nothing else records these)
  generated_tokens, prompt_tokens_processed, prompt_tokens_cached   INTEGER
  cache_hit_ratio, prompt_tps, generate_tps, spec_decode_acceptance   REAL
  worktree      TEXT (JSON)         -- repo, path, root, branch, base, recreated, diffstat; read as a unit
  dump, log_events   INTEGER (0/1)  -- which opt-in debug files exist in the job directory
  -- derived at read time, not stored: usage totals (sum of requests), tool call count and
  --   counts by tool (tool_calls), files touched (tool_calls where tool is an edit tool),
  --   ctx_tokens (last request's prompt size), gen_tps (smoothed over recent requests),
  --   run_cwd (worktree.path or cwd), isolation (worktree not null), log_dir (state dir + name)

requests                            -- one row per backend request the shim forwarded
  rowid
  job_id      TEXT REFERENCES jobs(id) ON DELETE CASCADE
  path_id     INTEGER REFERENCES paths(id)
  t           REAL
  status      INTEGER               -- HTTP status
  ms, gen_ms  INTEGER               -- whole request; first streamed byte to end
  input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens   INTEGER
  messages, tools, bytes   INTEGER  -- request body: message count, tool count, size
  moved_system, folded_text   INTEGER   -- shim normalisation counts
  context_exceeded, error   TEXT    -- backend's original overflow message; first 800 bytes of a 4xx/5xx body

tool_calls                          -- one row per ToolUseBlock the runner saw
  rowid
  job_id      TEXT REFERENCES jobs(id) ON DELETE CASCADE
  t, finished REAL                  -- finished: when the matching ToolResultBlock arrived
  tool        TEXT                  -- Read, Edit, Bash, ...
  tool_use_id TEXT
  input       TEXT                  -- file path, or first 120 chars of the command/input
  is_error    INTEGER (0/1)         -- from the matching ToolResultBlock

indexes
  jobs(name), jobs(mcp_server_id), jobs(endpoint_id), jobs(model_id), jobs(status, finished_at)
  requests(job_id, t)
  tool_calls(job_id, t), tool_calls(tool_use_id)

PRAGMA user_version = 1; journal_mode = WAL; busy_timeout = 5000; foreign_keys = ON
```

## Access patterns the indexes serve

- Status line (`localagents statusline`, every refresh, must stay under 100 ms, stdlib only):
  live mcp_servers (updated within 60 s and pid alive) joined to their jobs that are running
  or finished within 120 s; per job the last request row for ctx_tokens and gen_tps.
- `job_status`: one job by id, plus grouped counts over tool_calls and sums over requests.
- `job_log(tools=N)`: last N tool_calls rows for a job.
- Monitor (Epic 2): jobs by endpoint_id / model_id over a time range; requests by job.

## What stays a file in the job directory

`transcript.txt` (humans read it; `job_log` tails it) and the opt-in debug dumps
`events.jsonl`, `requests_full.jsonl`. `task.txt` and `requests.jsonl` are no longer written.
`status/<pid>.json` is no longer written.
