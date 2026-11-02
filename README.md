# localagents

**Hand Claude Code's grunt work to a model running on your own hardware.**

localagents is an MCP server that gives Claude Code a `run_agent` tool. Each call
starts a full headless Claude Code session — same tools, same `CLAUDE.md`, same
working tree — except its API traffic goes to a llama.cpp or vLLM server you run
instead of to Anthropic. Claude writes the brief, the local model does the work,
Claude reviews the result. Your Anthropic token budget goes on the parts that
need it.

A 27B Qwen on one GPU is perfectly capable of "add a CLI for this module and
tests to match"; Opus is better spent on the design conversation than on
watching pytest run. Two local agents in parallel can build the two halves of a
package against a pinned interface.

It comes with a web **monitor** for the servers and the jobs running on them.

> **Status: early.** It works, I use it daily, and the interface will move. It
> targets llama.cpp and vLLM specifically; Ollama isn't a goal.

## How it works

```
Claude Code (your session)
   │  MCP: run_agent(task, model=...)
   ▼
localagents ── spawns ──▶ headless `claude` (Agent SDK)
   │                          │  ANTHROPIC_BASE_URL
   │                          ▼
   └──── in-process shim ◀────┘   normalises requests, logs them,
              │                   translates backend errors
              ▼
   llama-server / vllm   (/v1/messages, on your machine or your LAN)
```

Three things make this more than an environment variable:

1. **Endpoints, not models, in the config.** `models.yaml` is a list of servers.
   What each one is actually serving right now, its real context window, and how
   many slots are busy are discovered on every call via `/v1/models` and friends.
   You bring models up and down by hand — the server never launches anything —
   and Claude uses whatever is up. If nothing suitable is running it asks you to
   start something.

2. **A shim between Claude Code and the backend.** Claude Code sends things local
   servers choke on (system messages mid-conversation, text blocks next to tool
   results) and assumes a 200k context window. The shim fixes the requests
   without breaking the server's prefix cache, tells the session the real window,
   and translates overflow errors into the form Claude Code knows how to recover
   from. See [What the shim does](#what-the-shim-does).

3. **One database.** Each job's requests, tool calls, usage and transcript go into
   one SQLite file, so Claude can read a job after the server that ran it has exited,
   the status line can show what is running, and the monitor has something to draw.

## Quick start

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), Claude Code (the Agent
SDK bundles its own `claude` binary, so nothing else to install), and a server that
speaks Anthropic's `/v1/messages`: llama.cpp's `llama-server`, Strata, or vLLM. The
model has to be able to drive Claude Code — solid native tool calling and a context
window of 128k or more per request. Qwen3.8-27B works well. Smaller windows work but
compact constantly; see [Context windows](#context-windows).

1. **Install it.** Editable, so edits in the checkout take effect:

   ```bash
   git clone https://github.com/ccebelenski/localagents.git && cd localagents
   uv tool install -e .          # puts `localagents` on PATH
   ```

2. **Tell it where your servers are.** Copy the example and edit it (the file is
   gitignored). It says where each server is and what kind it is, never which model is
   loaded — see [Backends](#backends) for starting each kind and
   [Config](#config-modelsyaml) for the file:

   ```bash
   cp models.example.yaml models.yaml
   ```

3. **Register the MCP server with Claude Code.** User scope means every project
   gets it; the server inherits the cwd of the Claude Code session that launched
   it, so `run_agent` defaults to that project's tree:

   ```bash
   claude mcp add --scope user local -- localagents --config "$PWD/models.yaml"
   ```

   To keep it to one project instead, put this in that project's `.mcp.json`:

   ```json
   {"mcpServers": {"local": {"command": "localagents", "args": ["--config", "/path/to/models.yaml"]}}}
   ```

   MCP servers load at startup: restart Claude Code, or `/mcp` and reconnect. The
   same applies after every `git pull` of this repo — a running server keeps the
   code it started with until you reconnect.

4. **Shorten the auto-background delay.** `run_agent` blocks until the job ends, and
   Claude Code only moves a blocking MCP call to a background task after
   `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` milliseconds — two minutes by default, and since
   MCP calls run one at a time that is two minutes per agent you launch. Ten seconds is
   about right; `list_models` and the other quick calls still return inline:

   ```json
   {"env": {"CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "10000"}}
   ```

5. **Install the `local-agent` subagent.** Jobs then show up as agents, and it covers
   the cases where auto-backgrounding is off (see [below](#the-local-agent-subagent)):

   ```bash
   mkdir -p ~/.claude/agents && ln -s "$PWD/agents/local-agent.md" ~/.claude/agents/
   ```

   It names the server's tools as `mcp__local__*`; if you registered the server under
   a name other than `local`, copy the file instead and fix its `tools:` line.

6. **Optionally, the status line.** `localagents statusline` prints the running jobs
   for your status line script; see [Status line](#status-line).

7. **Run a job.** Start a model on one of your servers, then ask Claude:

   > Use the local agent to add a `--json` flag to the CLI and cover it in the tests.

   Claude calls `list_models`, picks a served model, and runs `run_agent` with the
   brief. If nothing suitable is up it asks you to start something.

`localagents monitor` opens the [Monitor](#monitor) at <http://127.0.0.1:8765/> when you
want to watch.

## Backends

An endpoint is anything that serves Anthropic's `/v1/messages` and lists its models
at `/v1/models`. Beyond that, what localagents can see depends on the backend and
on how you start it. The `backend:` key in `models.yaml` picks the probe; it is
declared, never guessed.

| localagents reads | llama.cpp | Strata | vLLM |
|---|---|---|---|
| served model ids | `/v1/models` | `/v1/models` | `/v1/models` |
| context window per request | `/props` → `n_ctx`, always on | `/props` → `n_ctx`, always on | `max_model_len` in `/v1/models`, always on |
| slot or request occupancy (`list_models`, the status line, the monitor) | `/slots`, on by default (`--no-slots` turns it off) | `/slots`, always on | `/metrics`, always on |
| server counters (per-job cache hit and throughput, the monitor's speed tiles) | `/metrics`, **needs `--metrics`** | `/metrics`, always on (JSON) | `/metrics`, always on |
| cache fields in each response's `usage` (the request tables' reused and prefill columns) | always | always | **needs `--enable-prompt-tokens-details`** |
| GPU, VRAM, temperature, power | — | `/metrics` | — |

Nothing here is required for a job to run; a figure the server doesn't expose is left
out of `list_models` and shows as a dash in the status line and the monitor. The two
flags in bold are the ones that get forgotten.

### llama.cpp

```bash
llama-server -hf <repo>:<quant> -c 262144 --parallel 2 --metrics
```

The flag defaults below are quoted from `llama-server --help` (build b11913); check
yours, the defaults have moved before.

- `--jinja` — "whether to use jinja template engine for chat (default: enabled)".
  Tool calling needs it; leave it on.
- `--slots` / `--no-slots` — "expose slots monitoring endpoint (default: enabled)".
  With `/slots` on, `list_models` shows `{total, busy, free}` so Claude knows whether
  a second agent will run now or queue.
- `--metrics` — "enable prometheus compatible metrics endpoint (default: disabled)".
  With it on, each job records prompt tokens processed vs. cached, the cache hit
  ratio, prompt and generation tok/s and speculative-decode acceptance, and the
  monitor's speed and cache tiles have data. The counters are server-wide, so
  overlapping jobs share the delta.
- `-c N` / `--ctx-size N` and `-np N` / `--parallel N` — the window localagents
  reports is the **per-slot** window `n_ctx` from `/props`. Without unified KV
  (`--kv-unified`, "default: enabled if number of slots is auto") that is `-c`
  divided by the slot count. Give each slot 128k or more.
- `--reasoning on` for thinking models; the default, `auto`, detects it from the
  template.
- `--api-key KEY` — put the same key in the endpoint's `api_key:`; see
  [Config](#config-modelsyaml).

llama.cpp reports a too-long prompt as `exceed_context_size_error`; the shim
translates it so the session compacts and retries.

### Strata

A llama.cpp-compatible server; mark it `backend: strata`. Its `/props`, `/slots` and
`/v1/messages` have llama-server's shapes, but `/slots` and `/metrics` are always on
and never advertised in `/props`, and `/metrics` is JSON (cumulative totals plus a
live queue depth and the GPU figures) rather than Prometheus text. The strata probe
reads them unconditionally: `list_models` shows `{total, busy, free}` plus
`requests_waiting`, jobs get the same cache and throughput record as on llama.cpp,
and the monitor draws its hardware tiles. Marked `llama.cpp` it still works (window
from `/props`) but without occupancy or per-job metrics, and `list_models` says so in
a `hint`. Strata counts `max_tokens` against the window instead of clamping it, so it
refuses a request earlier than llama-server would; the shim translates that refusal,
or set `"fit_max_tokens": true` in its `strata-<model>.json` to clamp.

### vLLM

```bash
vllm serve <model> --served-model-name <alias> \
    --enable-auto-tool-choice --tool-call-parser <parser> \
    --enable-prompt-tokens-details
```

From the vLLM CLI reference:

- `--enable-auto-tool-choice` — "Enable auto tool choice for supported models. Use
  `--tool-call-parser` to specify which parser to use." Default `False`. Both are
  required for tool calling; the parser depends on the model.
- `--enable-prompt-tokens-details` — "If set to True, enable prompt_tokens_details in
  usage." Default `False`. Without it vLLM's Anthropic-shaped responses carry no cache
  fields: the request tables' reused column is blank and the prefill rate is an upper
  bound (the cell's title says so).
- `--served-model-name` — the id `list_models` shows and `run_agent(model=...)`
  matches; `--max-model-len` is the window localagents reports.

The Anthropic `/v1/messages` endpoint and the Prometheus `/metrics` endpoint need no
flag. vLLM reports a too-long prompt on the Anthropic endpoint as HTTP 500
`internal_error`; the shim translates that too. Sessions are started with
`CLAUDE_CODE_ATTRIBUTION_HEADER=0`, because the per-request attribution hash defeats
prefix caching. Verified with DeepSeek-V4-Flash: thinking blocks replay with their
signatures, prefix cache hits on every turn after the first.

### Other

`backend: other` probes `/v1/models` and, if it answers, `/props` for a window.
Without a window the session is told nothing and Claude Code assumes 200k, so only
use it for a server whose window really is that large.

The first turn of any job costs about 20k tokens of prompt on a cold slot (system
prompt plus tool schemas), ~10 s on a 27B. Everything after that is a cache hit plus
the delta.

## Using it

Claude picks it up like any tool. Ask for it by name and it'll do the right thing:

> Use the local agent to add a `--json` flag to the CLI and cover it in the tests.

What Claude does behind that: `list_models` to see what's up and pick a model, then
`run_agent` with the brief and `wait_s` set to the job's timeout. The call blocks
until the job ends; after the auto-background delay Claude Code moves it to a
background task (shown in `/tasks`), Claude keeps working, and the final job record
arrives as a task notification. Claude reads `files_touched` (or the worktree diff)
and checks the work. The server sends a progress notification every minute while it
waits — e.g. `running: 312s, 9 turns, 23 tool calls, 2 errors; Bash: uv run pytest -q`
— so the call never trips Claude Code's idle timeout, and the tool timeout itself
defaults to about 28 hours. A swarm is one blocking `run_agent` per job, and every
job delivers its own notification when it finishes, with nothing for Claude to
re-arm.

`wait_jobs` is the fallback for calls that cannot background: start each job with
`wait_s: 0` and wait on all the ids in one call. Use it knowingly: a `mode: "any"` wait
returns on the first finish and must be re-armed at once, or the remaining jobs finish
with nobody watching (one coordinator lost six hours to that). Its `running` list holds
every job whose record is not yet settled, including one whose `status` is already final
while its worktree is still being finished.

If nothing suitable is running you'll be asked to start one:

> No live endpoint is serving a model matching `qwen`. Ask the user to start one.
> Currently serving: {'dgx1': ['GLM-5.3-Flash-EXL3']}

Start whatever you like, however you normally do, say "it's up", and Claude retries.
Which model that is, is your call; the config never names one. The flip side: anything
you leave running is fair game. Claude is told to name the model it's about to use and
to check with you if the id looks unfamiliar, and `list_models` flags windows under
128k, but the real gate is what you choose to start.

### Tools

| tool | what it does |
|---|---|
| `list_models` | endpoints with live health (`host` up/down separately from the endpoint), served ids, context window, slot occupancy |
| `run_agent` | start a job: `task`, `model`, `cwd`, `isolation` (`none`/`worktree`), `wait_s`, `max_turns`, `permission_mode`, `resume_job`, … |
| `wait_job` / `wait_jobs` / `cancel_job` | the waits block on any server's job — another server's is polled from its row in the shared database — reporting progress while they block; `wait_jobs` takes several ids (`mode` all/any); `cancel_job` stops a job this server started |
| `job_status` / `job_log` / `list_jobs` | read any server's jobs, from the database; `job_log(tools=N)` adds the last N tool calls and carries a `note` when neither the finished job's `transcripts` row nor its directory exists — a finished job's transcript is read from the database, so it outlives the file's retention — `job_log(tail_lines=0)` returns the whole transcript, `list_jobs(all=True)` spans every server's, and the listing answers with the 50 newest by default (`limit=0` lists everything) |
| `foreign_job` | the answer only `cancel_job` and `run_agent(resume_job=…)` give for a job another server started (they need its process); the waits and the three reads above work on any job |
| `register_endpoint` | add a server from inside a session (written to `models.local.yaml`) |
| `local_complete` | one-shot generation with no tools — summaries, drafts, classification |

### The `local-agent` subagent

Auto-backgrounding needs Claude Code 2.1.212 or later and is off inside subagents and
in non-interactive runs unless `CLAUDE_AUTO_BACKGROUND_TASKS=1`. Without it a
`run_agent` job is an MCP tool call plus a job id Claude has to remember to poll:
Claude Code doesn't know it's an agent, nothing tells Claude when it finishes, and a
job can be forgotten if Claude ends its turn before it's done.

[`agents/local-agent.md`](agents/local-agent.md) covers those cases by making each job
a real background subagent. It's only a relay. It runs on Haiku, can use only the job
tools (`run_agent`, `wait_job`, `job_status`, `job_log`, `cancel_job`), passes the
brief through verbatim, waits, and returns the final job record unchanged. Picking the
model stays with the main session, which still has all of the server's tools,
`list_models` included. Claude passes its choice to the relay in a short header:

```
model: unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL
isolation: worktree
---
Add a --json flag to the CLI and cover it in the tests. ...
```

`model:` is required and should be the exact served id from `list_models`. Which
model runs matters, and the main session is the one with the live context windows
and slot occupancy, the conversation, and your instructions. The relay doesn't have
any of that, so it won't fill in a model, fall back to a default, or swap one. With
no `model:` it refuses to start, and if the model isn't up it returns the
`model_unavailable` error so Claude asks you as usual. Other header keys: `endpoint`,
`cwd`, `max_turns`, `timeout_s`, `permission_mode`, `resume_job`. A resume header is
the one case where `model:` can be left out: a resumed job continues on the model that
ran the original session.

While it runs, Claude can still check on it directly with `list_jobs` / `job_status` / `job_log`.
Messages sent to the relay let Claude cancel the job or, after it finishes, continue
the same local session (`resume_job`). A follow-up runs on the same model, in the same
directory and isolation: a worktree job continues in its tree, and if the tree was
removed because the job changed nothing, it is recreated at the same path from the
same base commit. Jobs live in the server's memory, so a follow-up is only possible
while that server process is up. Stopping the relay from `/tasks` does **not**
stop the local job; use `cancel_job`. The overhead is a few small Haiku turns per job,
one per 25 minutes of job time (each `wait_job` blocks for 1500 s with progress
notifications in between).

### Status line

Jobs started this way are MCP tasks, not subagents, so they don't appear in the agent
panel. `/tasks` shows the latest progress line the server reported (status, elapsed,
turns, tool calls, last activity). For an always-visible view, every server keeps its
state in the shared database — a heartbeat every ten seconds, plus its jobs, requests
and tool calls — and `localagents statusline` reads it and prints one header per
endpoint plus one row per job (the old one-line form is `localagents statusline
--one-line`):

```
local ai1 · 1/1 slots
  ⟳ Qwen3.8-27B 4m00s · 12 calls · 3 files · ctx 41% · 33 t/s · 2 compactions · Bash: uv run pytest -q
  ✓ Qwen3.8-27B 2m00s · 2 files · 3 compactions
local dgx1-a · 1 run kv 31%
  ⟳ DeepSeek-V4-Flash 2m00s · 3 calls · ctx 12% · 40 t/s
+2 jobs in other sessions
```

Per running job: elapsed time, tool calls, files touched, how full the local model's
context window is (`ctx`, from the token usage recorded on every backend request, so it
tells you how close the agent is to compaction), its recent generation speed, the
compaction count (a trailing indicator of a context-tuning problem — the harness says so
with a `compact_boundary` message) and its last tool call — shown as one line (a multi-line
command is folded to one row) with paths relative to the job's worktree, so the row names
the file instead of the path that leads to it. The load of each endpoint your
jobs run on rides its header line, probed every ten seconds while jobs run: slots busy
for llama.cpp, requests running and queued and KV cache fill for vLLM. Finished jobs
stay on the line for two minutes with a check mark (or a cross, clock, null sign or
dagger — `✗`, `⏱`, `∅`, `†` — for failed, timed out, cancelled and lost), keeping their
compaction count for those two minutes.

"Live" means the heartbeat: a server is shown while its last beat is under a minute
old (and its pid still exists). A server that exited or was killed drops off the line
as its jobs age out; the next server to start marks that one's unfinished jobs `lost`.
The line is scoped to the Claude Code session asking: each server records as its owner
the nearest Claude Code process it runs under (native `claude`, or an npm install running
as `node` with a `claude` entry in its command line; its parent pid when there is none),
so a session's own jobs match their session whichever way the server was started
(`uv run localagents` included). Jobs of other sessions on the same machine fold into a
trailing `+N jobs in other sessions` line, because the boxes are shared and knowing one
is busy matters even when the job isn't yours.

The renderer imports nothing heavy and runs in well under 100 ms, and it never prints a
traceback: the database is another process's file, so a too-new schema or a lock
renders as the empty line, and with no database yet it prints one empty line and
creates nothing. Append it to your status line script and set `refreshInterval` in
the `statusLine` settings block so the elapsed times tick while the session is idle.
Your script is `~/.claude/statusline-command.sh`; it prints your own first line and
the local lines after it (shape only — the script is yours, not in the repo):

```bash
local_lines=$(timeout 2 localagents statusline 2>/dev/null)
if [ -n "$local_lines" ]; then
  printf '%s\n%s\n' "$line" "$local_lines"
else
  printf '%s\n' "$line"
fi
```

### Job records

A job's record is split between the database and its directory. The brief and the
numbers are rows in SQLite (see [The database](#the-database) below): the brief is the
`jobs.task` column and every backend request is a row in the `requests` table with
timing, size and usage, written directly by the shim. `job_status` answers from those
rows: `usage` as the sums over the job's requests, `tool_calls` with `tool_counts` per
tool, `errors`, `last_tool`, `files_touched`, `ctx_tokens` and `gen_tps`. `compactions`
is a column of its own: the runner counts the harness's `compact_boundary` messages
into it. The transcript is written to the `transcripts` table once when the job
finishes, so it outlives the directory below.

The directory — `~/.local/state/localagents/jobs/<id>/`, where `<id>` is the job's uuid
(the same `job_id` the tools take; it also names the worktree branch `local-agent/<id>`)
— holds `transcript.txt` (what the agent said and did) and nothing else unless
debugging is on: `events.jsonl` (every SDK message, tool results included; it was 98%
of a 6 GB state directory before it became opt-in) with `defaults.log_events: true` or
`LOCALAGENTS_LOG_EVENTS=1`, and `requests_full.jsonl` (every request body) with
`defaults.dump_requests: true` or `LOCALAGENTS_DUMP_REQUESTS=1`.

**Retention.** Job directories with nothing written for more than
`defaults.job_retention_days` (default 7, `0` disables) are deleted when a server starts
and after every job finishes. The age is mtime — the newest of the directory itself and
its files — and nothing that is not named like a job directory is touched; database
rows are never deleted. Keep retention above the longest `timeout_s` you configure:
prune deletes by age, so a job still running past its directory's expiry would have
its transcript deleted out from under it. The same pass is available by hand:

```bash
localagents prune --dry-run   # list what would go
localagents prune             # delete it; --days N overrides the default
```

### The database

Every server writes to one SQLite file: `~/.local/state/localagents/localagents.db`
(`XDG_STATE_HOME` honoured; `LOCALAGENTS_DB=/path` overrides). It is in WAL mode and
holds every server process's jobs, the requests their shims saw, their tool calls,
their transcripts, the monitor's probes, and the endpoints and models seen so far. The
full schema is
[`docs/superpowers/specs/2026-10-29-mcp-sqlite-schema.md`](docs/superpowers/specs/2026-10-29-mcp-sqlite-schema.md).
Because the record is the database rather than a server's memory, it outlives the server
that ran the job: any server's `job_status`, `job_log` and `list_jobs(all=True)` can
read it after the original process is gone, and the waits can wait on it too; only
`cancel_job` and `run_agent(resume_job=…)` cannot — see `foreign_job` above. Rows are
history: retention removes job directories and the monitor's old probes, never job
rows.

It lives on one machine — SQLite's WAL files do not work on a network filesystem, so
never point `LOCALAGENTS_DB` at one. Back it up with SQLite, not `cp`:

```bash
sqlite3 ~/.local/state/localagents/localagents.db ".backup /path/to/backup.db"
```

## Monitor

`localagents monitor` is a web dashboard for the endpoints in `models.yaml` and the jobs
localagents runs on them: one process that probes every endpoint (every 5 s; a dormant
one every 60 s) into the shared database and serves a page that redraws from a
server-sent events stream, one snapshot per probe round.

### Running it

```bash
localagents monitor                 # http://127.0.0.1:8765/, opens in your browser
localagents monitor --port 9000 --config ~/.config/localagents/models.yaml
```

Flags:

- `--port N` — default: the `monitor:` section's `port`, else **8765**.
- `--host ADDR` — default `127.0.0.1`. The page has no authentication, so bind another
  address only on a network you trust.
- `--db PATH` — default: `monitor: db`, else `LOCALAGENTS_DB`, else the state
  directory's `localagents.db`.
- `--config PATH` — the `models.yaml` to read; found as the MCP server finds it (see
  [Config](#config-modelsyaml)).
- `--no-collect` — serve the database as it is, without probing or pruning. The page
  still follows the jobs the MCP server writes, a snapshot every `refresh_s`.
- `--no-open` — do not open the browser. By default the page opens in a new tab once
  the server is up (`$BROWSER` is honoured). If a monitor is already running on that
  port, the command opens the page and exits 0 instead of failing; with `--no-open`
  the "port in use" failure stands.

It prints one line at start — the URL, the database path, the endpoint count, and
"not collecting" when `--no-collect` was given — and stops on Ctrl-C or SIGTERM.

To keep it running in the background from a checkout, `tools/monitor.sh` wraps it:

```bash
tools/monitor.sh start            # detached with setsid; the page opens
tools/monitor.sh status           # running (pid, last start line) or stopped
tools/monitor.sh log              # tail -f the log
tools/monitor.sh stop             # SIGTERM, waits up to 10 s
```

It writes its pid to `${XDG_STATE_HOME:-~/.local/state}/localagents/monitor.pid` and its
output to `monitor.log` beside it, needs `localagents` on PATH, and passes any other
arguments after `start` to `localagents monitor`.

### Settings

An optional top-level `monitor:` section in `models.yaml` holds the monitor's settings.
The MCP server ignores the section; the monitor rejects an unknown key or a bad value
with an error naming it:

```yaml
monitor:
  port: 8765
  attention_hours: 24     # an endpoint seen up within this window and down now is an alert; older is dormant
  retention_days: 7       # probe rows older than this are deleted (job rows never are)
  refresh_s: 5            # at most one snapshot push per this many seconds (max 3600)
  # db: /path/to/localagents.db
```

### The page

The overview is one card per host, in `models.yaml` order, and a card never moves when
its state changes. A live endpoint gets a fixed grid of tiles — load, KV fill, decode and
prompt speed, cache hit, context window, probe latency, and on Strata the GPU, VRAM,
temperature and power — with a dash where the backend reports nothing. The head line
has the served model and when the endpoint was last active; the foot lists the jobs
running on it. An endpoint that was up within the attention window and is down now is
drawn in the alert colour with the probe's sentence; an older one is a muted "last
seen" line; endpoints never seen aren't drawn, just counted in one line under the cards.

Under the cards, **Recent jobs** lists every running and queued job and the last ten
finished ones, with a status pill (`queued`, `running`, `succeeded`, `failed`,
`timeout`, `cancelled`, or `lost` for a live job whose MCP server stopped beating), the
model and endpoint, elapsed, turns, tool calls, tool errors, context fill, prefill and
decode speed and the last activity. Below that is a timeline: one row per live endpoint,
its busy intervals as a band and its jobs as bars you can click.

Every table pages at 50 rows, sorts by clicking a header, and filters per column —
a select for columns with a fixed set of values, a number box in the column's own unit,
a date-and-hour picker for start times, plain text elsewhere. The range buttons (`15m`
to `7d`, default `1h`) apply to everything and are remembered by the browser. The page
redraws when the server pushes a snapshot and never polls; the header shows when the
last one arrived and goes red while the stream reconnects. It loads nothing from the
internet.

Clicking an endpoint's name opens its page: the state and last-seen time, what it is
serving, a tile with a sparkline for every figure the backend reports over the range,
the jobs that ran on it, restarts and outages when the range holds any, and its request
table. Clicking a job opens the job page: status and times, a context gauge, the usage
counters and `/metrics` deltas, the files touched, a tool-call timeline with errors in
red, the breakdown by tool, the request table and the transcript tail. The job page
also shows the build stamp of the MCP server that ran it, with a note when the
monitor's own code is newer — handy when a server has been left running on old code.

The request tables show each request's status, path, prompt size (split into new and
reused tokens), output, prefill and decode speed, duration and error. Claude Code's
connection warm-up (`/api/hello`) is answered by the shim itself and labelled
`handshake`, outside the error counts. A backend that reports no cache fields (vLLM
without `--enable-prompt-tokens-details`) is marked as such, and its prefill speed is
an upper bound.

### Seeing it without a fleet

The monitor's route tests and browser smokes run against a **seeded** database, built
offline and deterministically from the captured fixtures under `tests/fixtures/monitor/`
with every timestamp shifted to `--now` (the current time by default). To see the page
with data and no servers:

```bash
LOCALAGENTS_DB=/tmp/seed.db localagents seed
LOCALAGENTS_DB=/tmp/seed.db localagents monitor --no-collect
```

The seeded world has three hosts: **ai1** (Strata, live — an hour of probes at 5 s with
a running job on its one slot), **dgx1-a** (vLLM, down since 20 min ago — a refused
connection: the box answers, nothing listens) and **llamacpp** (dormant — last up a week
ago), plus one finished job with its requests and tool calls (ended 10 min ago) and one
running job (started 5 min ago). The file is a snapshot at `--now`: its states decay
after the attention window, so re-seed to see them again. A naive ISO `--now`
(`2026-10-29T12:00:00`) is read in local time.

`localagents seed` is for the tests and this demo, not for your live fleet: point it
at a throwaway file. It refuses a database that already holds `jobs` or `probes` rows
unless `--force` is given, and `--force` *replaces* (every row of every table) rather
than appending. Even with `--force` it refuses a database a running localagents server
owns (a heartbeat in the last 60 s and a live pid), naming the pid.

### Smoke test

`localagents smoke --endpoint NAME --model SERVED_ID` is the online acceptance run: a
few short `/v1/messages` requests to the named endpoint, one real agent job through the
shim and runner with a fixed brief (`--skip-job` leaves it out; `--timeout-s` caps it),
a probe of a port nothing listens on, and a printed checklist of the rows and states
that appeared (exit 1 on any missing row). It needs a model actually running on that
endpoint (it runs a real job there, Bash included), and it writes real rows — into
`LOCALAGENTS_DB` or `--db` when set, otherwise the shared database; point it at a
throwaway file to rehearse.

### Routes

The page's JSON — `/api/snapshot`, `/api/endpoints/<name>/history?range=1h`,
`/api/fleet/history?range=1h`, `/api/overview/history?range=1h`,
`/api/endpoints/<name>/requests?range=24h`, `/api/jobs?range=24h`, `/api/jobs/<id>`
and the `/events` stream — is specified in
[`docs/superpowers/specs/2026-10-29-monitor-design.md`](docs/superpowers/specs/2026-10-29-monitor-design.md)
§5a. Ranges run from `15m` to `7d`.

## Config: `models.yaml`

Start from `models.example.yaml`. It's re-read on every call, so edits take effect
immediately, and the server never rewrites it — `register_endpoint` writes to a
sidecar `models.local.yaml` that is merged on top. Both the MCP server and the monitor
find it the same way: `--config`, else `LOCALAGENTS_CONFIG`, else `./models.yaml` in the
current directory (so a project can carry its own), else
`~/.config/localagents/models.yaml`.

```yaml
endpoints:
  llamacpp:
    base_url: http://127.0.0.1:8080
    backend: llama.cpp
  gpu-server:
    base_url: http://gpu-server.lan:8000
    backend: vllm
    host: gpu-server
```

- **endpoints** are places that serve `/v1/messages`. That's the whole config:
  what each one is serving is probed, never written down. Order is priority —
  with no model named, the first endpoint that is up and serving something wins.
  `backend` (`llama.cpp`, `vllm`, `strata`, `other`) selects how the window and occupancy
  are probed (see [Backends](#backends)); `host` groups endpoints under one card in the
  monitor and, with `notes`, is shown in `list_models`; `env` adds environment
  variables to sessions that run against that endpoint. `api_key` is sent on every
  call to that endpoint (as both `x-api-key` and `Authorization: Bearer`, so
  llama.cpp's and vLLM's `--api-key` and Anthropic-shaped servers all accept it). It
  stays in the MCP server: the delegated session is given the placeholder `local` and
  the shim swaps the real key in, so an agent's `env` or Bash never sees it. Without a
  key the placeholder is what goes out, which open servers ignore.
- **Picking a model** is done per call. `run_agent(model=...)` takes a served id,
  a glob, or a fuzzy name (`qwen3.8-27b` finds `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL`);
  `endpoint=` pins a server. Both are only needed when more than one thing is up.
  `defaults.model` is an optional standing preference in the same syntax.
- **defaults** also cover `permission_mode` (`acceptEdits`), `allowed_tools` and
  `disallowed_tools` (subagents can't spawn subagents), `setting_sources` (which
  Claude settings the session loads), `max_turns` (60), `timeout_s` (1800),
  `probe_timeout_s` (2.5), `job_retention_days` (7), the debugging switches
  `log_events` and `dump_requests`, and `system_prompt_append`, a suffix telling the
  agent it's a delegate and how to report back.
- **monitor** is the monitor's own section; see [Settings](#settings).

A `models:` section from an older config is ignored.

## What the shim does

Both llama.cpp and vLLM speak `/v1/messages` natively, so pointing
`ANTHROPIC_BASE_URL` at them almost works. The shim closes the gaps:

**System messages mid-conversation.** Claude Code puts `role: system` entries
inside `messages` — the skills listing, a token-budget marker, and one more per
turn. Qwen's chat template refuses: *"System message must be at the beginning"*.
The shim folds each one into the adjacent user message as a `<system>…</system>`
text block, **in place**. Hoisting them into the top-level `system` field instead
changes the start of the prompt every turn, which invalidates the server's
KV-cache prefix and re-evaluates the whole ~35k-token prompt each time (21–47 s
per turn on a 27B). Folding in place keeps the prompt append-only: `f_sim_best`
0.88–0.99 in llama-server's log, 2.5–14 s per turn.

**Text next to tool results.** Claude Code appends a `<system-reminder>` text
block to the user turn that carries tool results (and the fold above adds a
`<system>` block there too). Gemma 4's chat template renders a tool response
only when it sits directly after the assistant's tool call; behind llama.cpp's
`/v1/messages` converter that mixed turn breaks the adjacency and the model
sees *none* of its tool output — it then invents file contents, every Edit
misses, and it reports success anyway. The shim moves such text blocks inside
the last `tool_result`'s content, which every template renders. Measured on
gemma-4-31B-it-qat (llama.cpp b11515): prompt growth after three file reads went
from 256 tokens (blind) to ~3.1k (sees them); Qwen3.6 and Muse-Glimmer were
unaffected either way.

**Context overflow.** Claude Code assumes a 200k window for any model it doesn't
recognise; with a smaller slot it runs into llama.cpp's
`exceed_context_size_error`, which it doesn't understand, and the job dies. See
the next section.

**The handshake.** Claude Code opens every session with a request to `/api/hello`,
which no local backend serves. The shim answers it itself, so the backend never sees
it and it is not counted as an error.

Everything the shim does is a no-op when it isn't needed, and every request is
recorded with its timing, message count, byte size and reported usage.

## Context windows

Two layers keep a session inside the real window:

1. The probe reads it — llama.cpp `/props` `n_ctx` (per slot: `-c` divided by
   `--parallel` when unified KV is off), vLLM `max_model_len` — and the session
   gets `CLAUDE_CODE_MAX_CONTEXT_TOKENS`. Claude Code's own auto-compact then fires
   at the right point. Below 128k the output budget is also shrunk to `n_ctx/8`
   (floor 8k), because the compact threshold is `window − max_output` and would
   otherwise sit at zero.
2. If a request still overflows, the shim rewrites the backend's error into
   Anthropic's `prompt is too long: N tokens > M maximum`, which Claude Code
   answers by compacting and retrying.

At 64k this works but thrashes: Claude Code's ~20k of fixed prompt and tool
schemas, plus a ~7k-token compaction summary (~55 s on a 27B) and the files it
re-attaches, refill the window within a few turns and its thrash guard ends the
job. Give each slot 128k or more.

## Development

```bash
uv sync --dev
uv run pytest -q
```

A checkout that is a git worktree refuses the default state directory, so a test
server or a probe run from one cannot touch your real database and job directories:
set `XDG_STATE_HOME` (and `LOCALAGENTS_DB`) to a directory of its own. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the layout and how to test changes against a
real server.

## License

MIT. See [`LICENSE`](LICENSE).

Copyright © 2026 Chris Cebelenski
