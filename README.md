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
   chat templates reject, and local servers fail in ways Claude Code doesn't
   recognise. The shim fixes both directions (details below) and records every
   backend request — timing, size, usage, the fixups it applied — in the
   `requests` table of the shared database, so you can see what each turn cost.

3. **The same isolation model as Claude's own subagents.** By default a job works
   in your tree, like the Agent tool does. `isolation: worktree` gives it a fresh
   git worktree on a `local-agent/<name>` branch (named after the job — see
   [Job records](#job-records)), kept only if it changed something, with a
   diffstat in the job record so Claude can review it as a diff.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Claude Code. The Agent SDK bundles its own `claude` binary, so nothing else to install.
- A server that speaks Anthropic's `/v1/messages`:
  - **llama.cpp** `llama-server` — start it with `--jinja`; add `--slots --metrics`
    to get occupancy and cache stats in the tool output.
  - **vLLM** with `--enable-auto-tool-choice --tool-call-parser <parser>`.
- A model that can actually drive Claude Code: solid native tool calling and a
  context window of 128k or more per request. Qwen3.8-27B works well.
  Smaller windows work but compact constantly; see [Context windows](#context-windows).

## Install

```bash
git clone https://github.com/ccebelenski/localagents.git && cd localagents
uv tool install -e .                  # `localagents` on PATH; editable, so repo edits apply
cp models.example.yaml models.yaml    # edit for your servers (gitignored)
claude mcp add --scope user local -- localagents --config "$PWD/models.yaml"
```

User scope means every project gets the `local` server. It inherits the cwd of the
Claude Code session that launched it, so `run_agent` defaults to that project's
tree. A project can carry its own `./models.yaml` to override the registry.

If you'd rather keep it to one project, put this in that project's `.mcp.json`:

```json
{"mcpServers": {"local": {"command": "localagents", "args": ["--config", "/path/to/models.yaml"]}}}
```

Restart Claude Code (or `/mcp` → reconnect) after adding it; MCP servers load at startup.

Then install the `local-agent` subagent, so jobs show up as agents (see [below](#the-local-agent-subagent)):

```bash
mkdir -p ~/.claude/agents && ln -s "$PWD/agents/local-agent.md" ~/.claude/agents/
```

It names the server's tools as `mcp__local__*`; if you registered the server under
another name than `local`, copy the file instead and change the `tools:` line.

## Using it

Claude picks it up like any tool. Ask for it by name and it'll do the right thing:

> Use the local agent to add a `--json` flag to the CLI and cover it in the tests.

What Claude does behind that: `list_models` to see what's up and pick a model, then
`run_agent` with the brief and `wait_s` set to the job's timeout. The call blocks
until the job ends; after two minutes Claude Code moves it to a background task (shown
in `/tasks`), Claude keeps working, and the final job record arrives as a task
notification. Claude reads `files_touched` (or the worktree diff) and checks the work.
The server sends a progress notification every minute while it waits — e.g.
`running: 312s, 9 turns, 23 tool calls, 2 errors; Bash: uv run pytest -q`
— so the call never trips Claude Code's idle timeout, and the tool timeout itself
defaults to about 28 hours.

The two minutes are Claude Code's default (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`, in
milliseconds). MCP calls run one after another, so with several agents to launch that
is two minutes each. Set the variable lower in the `env` block of your settings: `10000`
backgrounds `run_agent` after ten seconds and leaves `list_models` and the other quick
calls inline (a `local_complete` that runs long comes back as a notification instead).
Then a swarm is one blocking `run_agent` per job, and every job delivers its own
notification when it finishes, with nothing for Claude to re-arm.

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
| `wait_job` / `wait_jobs` / `cancel_job` | control jobs this server started; the waits report progress while they block, `wait_jobs` takes several ids (`mode` all/any) |
| `job_status` / `job_log` / `list_jobs` | read any server's jobs, from the database; `job_log(tools=N)` adds the last N tool calls and carries a `note` when the job's directory was pruned, `job_log(tail_lines=0)` returns the whole transcript, `list_jobs(all=True)` spans every server's, and the listing answers with the 50 newest by default (`limit=0` lists everything) |
| `foreign_job` | the answer `wait_job`, `wait_jobs`, `cancel_job` and `run_agent(resume_job=…)` give for a job another server started; the three reads above work on any job |
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
notifications in between; v0.1.5 waited 100 s at a time, which with the relay's turn
cap ended jobs longer than about 2.7 hours).

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
  ✓ Qwen3.8-27B 2m00s · 2 files
local dgx1-a · 1 run kv 31%
  ⟳ DeepSeek-V4-Flash 2m00s · 3 calls · ctx 12% · 40 t/s
+2 jobs in other sessions
```

Per running job: elapsed time, tool calls, files touched, how full the local model's
context window is (`ctx`, from the token usage recorded on every backend request, so it
tells you how close the agent is to compaction), its recent generation speed, the
compaction count (a trailing indicator of a context-tuning problem — the harness says so
with a `compact_boundary` message) and its last tool call. The load of each endpoint your
jobs run on rides its header line, probed every ten seconds while jobs run: slots busy
for llama.cpp, requests running and queued and KV cache fill for vLLM. Finished jobs
stay on the line for two minutes with a check mark (or a cross, clock, null sign or
dagger — `✗`, `⏱`, `∅`, `†` — for failed, timed out, cancelled and lost).

"Live" means the heartbeat: a server is shown while its last beat is under a minute
old (and its pid still exists). A server that exited or was killed drops off the line
as its jobs age out; the next server to start marks that one's unfinished jobs `lost`.
The line is scoped to the Claude Code session asking: each server records as its owner
the nearest `claude` ancestor it runs under (its parent pid when there is none — the
same walk the renderer does up from the status line script), so a session's own jobs
match their session whichever way the server was started (`uv run localagents`
included). Jobs of other sessions on the same machine fold into a trailing
`+N jobs in other sessions` line, because the boxes are shared and knowing one is
busy matters even when the job isn't yours.

The renderer imports nothing heavy and runs in well under 100 ms, and it never prints a
traceback: the database is another process's file, so a too-new schema or a lock
renders as the empty line, and with no database yet it prints one empty line and
creates nothing. Append it to your
status line script and set `refreshInterval` in the `statusLine` settings block so
the elapsed times tick while the session is idle. Your script is
`~/.claude/statusline-command.sh`; it prints your own first line and the local lines
after it (shape only — the script is yours, not in the repo):

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
`jobs.task` column — there is no `task.txt` — and every backend request is a row in the
`requests` table with timing, size and usage, written directly by the shim; there is no
`requests.jsonl`. `job_status` answers from those rows: `usage` as the sums over the
job's requests, `tool_calls` with `tool_counts` per tool, `errors`, `last_tool`,
`files_touched`, `ctx_tokens` and `gen_tps`. `compactions` is a column of its own,
not a derivation: the runner counts the harness's `compact_boundary` messages into
it (and the status line shows the count when it is above zero).

The directory — `~/.local/state/localagents/jobs/<name>/`, where `<name>` is the job's
readable name `YYYYMMDD-HHMMSS-<8 hex>` (tools take the job's uuid as `job_id`; the name
— and with it the directory and the worktree branch `local-agent/<name>` — is the
timestamp plus its first 8 hex digits) — holds
`transcript.txt` (what the agent said and did) and nothing else unless debugging is on:
`events.jsonl` (every SDK message, tool results included; it was 98% of a 6 GB state
directory before it became opt-in) with `defaults.log_events: true` or
`LOCALAGENTS_LOG_EVENTS=1`, and `requests_full.jsonl` (every request body) with
`defaults.dump_requests: true` or `LOCALAGENTS_DUMP_REQUESTS=1`.

**Retention.** Job directories older than `defaults.job_retention_days` (default 7, `0`
disables) are deleted when a server starts and after every job finishes. The date in the
directory name decides, and nothing that is not named like a job directory is touched;
database rows are never deleted. Keep retention above the longest `timeout_s` you
configure: prune deletes by directory name, so a job still running past its directory's
expiry would have its transcript deleted out from under it. The same pass is available
by hand, which is also how you clean up a state directory that predates this rule:

```bash
localagents prune --dry-run   # list what would go
localagents prune             # delete it; --days N overrides the default
```

### The database

Every server writes to one SQLite file: `~/.local/state/localagents/localagents.db`
(`XDG_STATE_HOME` honoured; `LOCALAGENTS_DB=/path` overrides). It is in WAL mode and
holds every server process's jobs, the requests their shims saw, their tool calls, and
the endpoints and models seen so far. The full schema is
[`docs/superpowers/specs/2026-10-29-mcp-sqlite-schema.md`](docs/superpowers/specs/2026-10-29-mcp-sqlite-schema.md).
Because the record is the database rather than a server's memory, it outlives the server
that ran the job: any server's `job_status`, `job_log` and `list_jobs(all=True)` can
read it after the original process is gone (the in-process tools cannot — see
`foreign_job` above). `list_jobs` answers with the 50 newest by default; `limit=0`
lists the whole history. Rows are history: retention removes job directories, never rows.

It lives on one machine — SQLite's WAL files do not work on a network filesystem, so
never point `LOCALAGENTS_DB` at one. Back it up with SQLite, not `cp`:

```bash
sqlite3 ~/.local/state/localagents/localagents.db ".backup /path/to/backup.db"
```

## Config: `models.yaml`

Start from `models.example.yaml`. It's re-read on every call, so edits take effect
immediately, and the server never rewrites it — `register_endpoint` writes to a
sidecar `models.local.yaml` that is merged on top.

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
  are probed; `host` and `notes` are informational and shown in `list_models`; `env` adds
  environment variables to sessions that run against that endpoint. `api_key` is sent on
  every call to that endpoint (as both `x-api-key` and `Authorization: Bearer`, so
  llama.cpp's and vLLM's `--api-key` and Anthropic-shaped servers all accept it). It stays
  in the MCP server: the delegated session is given the placeholder `local` and the shim
  swaps the real key in, so an agent's `env` or Bash never sees it. Without a key the
  placeholder is what goes out, which open servers ignore.
- **Picking a model** is done per call. `run_agent(model=...)` takes a served id,
  a glob, or a fuzzy name (`qwen3.8-27b` finds `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL`);
  `endpoint=` pins a server. Both are only needed when more than one thing is up.
  `defaults.model` is an optional standing preference in the same syntax.
- **defaults** also cover `permission_mode` (`acceptEdits`), allowed and disallowed
  tools (subagents can't spawn subagents), which Claude settings to load,
  `max_turns`, `timeout_s`, and a system-prompt suffix telling the agent it's a
  delegate and how to report back.

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

Everything the shim does is a no-op when it isn't needed, and every request is
logged with its timing, message count, byte size and reported usage.

## Context windows

Two layers keep a session inside the real window:

1. The probe reads it — llama.cpp `/props` `n_ctx` (per slot: `-c` divided by
   `--parallel` when unified KV is off), vLLM `max_model_len` — and the session
   gets `CLAUDE_CODE_MAX_CONTEXT_TOKENS`. Claude Code's own auto-compact then fires
   at the right point. Below 128k the output budget is also shrunk to `n_ctx/8`,
   because the compact threshold is `window − max_output` and would otherwise sit at zero.
2. If a request still overflows, the shim rewrites the backend's error into
   Anthropic's `prompt is too long: N tokens > M maximum`, which Claude Code
   answers by compacting and retrying.

At 64k this works but thrashes: Claude Code's ~20k of fixed prompt and tool
schemas, plus a ~7k-token compaction summary (~55 s on a 27B) and the files it
re-attaches, refill the window within a few turns and its thrash guard ends the
job. Give each slot 128k or more.

## Backend notes

- **llama.cpp**: `llama-server -hf <gguf> --jinja -fa on --slots --metrics`, plus
  `--reasoning on` for thinking models and `--parallel N` for concurrent jobs. With
  `/slots` on, `list_models` shows `{total, busy, free}` so Claude knows whether a
  second agent will run now or queue. With `/metrics` on, each job records prompt
  tokens processed vs. cached, cache hit ratio, prompt and generation tok/s, and
  speculative-decode acceptance — the counters are server-wide, so overlapping jobs
  share the delta.
- **strata**: a llama.cpp-compatible server; mark it `backend: strata`. Its `/props`,
  `/slots` and `/v1/messages` have llama-server's shapes but `/slots` and `/metrics` are
  always on and never advertised in `/props`, and `/metrics` is JSON (cumulative `totals`
  plus a live queue depth) rather than Prometheus text, so the strata backend reads them
  unconditionally: `list_models` shows `{total, busy, free}` plus `requests_waiting`, and
  jobs get the same cache/throughput record as on llama.cpp. Marked `llama.cpp` it still
  works (window from `/props`) but without occupancy or per-job metrics, and the probe
  says so in a `hint`. strata counts `max_tokens` against the window instead of clamping
  it, so it refuses a request earlier than llama-server would; the shim translates that
  refusal, or set `"fit_max_tokens": true` in its `strata-<model>.json` to clamp.
- **vLLM**: `vllm serve <model> --served-model-name <alias> --enable-auto-tool-choice
  --tool-call-parser <parser>`. The context window comes from `max_model_len` in
  `/v1/models`; occupancy (`requests_running`/`waiting`, `kv_cache_usage`) and the per-job
  cache stats come from its always-on `/metrics`. vLLM reports context overflow on the
  Anthropic endpoint as HTTP 500 `internal_error`; the shim translates that too. The session
  is started with `CLAUDE_CODE_ATTRIBUTION_HEADER=0` because the per-request attribution
  hash defeats prefix caching. Verified with DeepSeek-V4-Flash: thinking blocks replay with
  their signatures, prefix cache hits on every turn after the first.
- The first turn of a job costs about 20k tokens of prompt on a cold slot (system
  prompt plus tool schemas), ~10 s on a 27B. Everything after that is a cache hit
  plus the delta.

## Development

```bash
uv sync --dev
uv run pytest -q
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the layout and how to test changes
against a real server.

## License

MIT. See [`LICENSE`](LICENSE).

Copyright © 2026 Chris Cebelenski
