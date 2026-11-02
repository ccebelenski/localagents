# localagents

**Hand Claude Code's grunt work to a model running on your own hardware.**

localagents is an MCP server that gives Claude Code a `run_agent` tool. Each call
starts a full headless Claude Code session — same tools, same `CLAUDE.md`, same
working tree — except its API traffic goes to a llama.cpp or vLLM server you run
instead of to Anthropic. Claude writes the brief and checks the result. The local
model does the work, and the Anthropic tokens go where Claude is actually needed.

A 27B Qwen on one GPU can handle "add a CLI for this module and tests to match" on
its own, while Opus does the design instead of watching pytest. Two local agents can
build the two halves of a package in parallel against an agreed interface.

It comes with a web **monitor** for the servers and the jobs running on them.

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

Pointing `ANTHROPIC_BASE_URL` at a local server almost works by itself. localagents
adds three things on top. The config is a list of servers. The model each one is
serving, the real context window and how many slots are busy are read from the server
on every call. You start and stop models yourself. A shim between Claude Code and the
backend mediates the requests, reports the real context window and translates overflow
errors ([what the shim does](docs/shim.md)). Every result is recorded in one SQLite
database, so Claude can read a job after the work is completed.

## Quick start

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), Claude Code (the Agent
SDK bundles its own `claude` binary, so nothing else to install), and a server that
speaks Anthropic's `/v1/messages`: llama.cpp's `llama-server`, Strata, or vLLM. The
model has to be able to drive Claude Code — solid native tool calling and a context
window of 128k or more per request. Qwen3.8-27B works well. Smaller windows work but
compact constantly; see [Context windows](docs/shim.md#context-windows).

1. **Install it.** Editable, so edits in the checkout take effect:

   ```bash
   git clone https://github.com/ccebelenski/localagents.git && cd localagents
   uv tool install -e .          # puts `localagents` on PATH
   ```

2. **Tell it where your servers are.** Copy the example and edit it (the file is
   gitignored). It lists servers and their kind; which model is loaded on each is
   discovered at call time. See [Backends](#backends) for starting each kind and
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
   `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` milliseconds — two minutes by default, per
   agent you launch. Ten seconds is about right; `list_models` and the other quick
   calls still return inline:

   ```json
   {"env": {"CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "10000"}}
   ```

5. **Install the `local-agent` subagent.** Jobs then show up as agents, and it covers
   the cases where auto-backgrounding is off (see [Using it](docs/using.md#the-local-agent-subagent)):

   ```bash
   mkdir -p ~/.claude/agents && ln -s "$PWD/agents/local-agent.md" ~/.claude/agents/
   ```

   It names the server's tools as `mcp__local__*`; if you registered the server under
   a name other than `local`, copy the file instead and fix its `tools:` line.

6. **Optionally, the status line.** `localagents statusline` prints the running jobs
   for your status line script; see [Status line](docs/using.md#status-line).

7. **Run a job.** Start a model on one of your servers, then ask Claude:

   > Use the local agent to add a `--json` flag to the CLI and cover it in the tests.

   Claude calls `list_models`, picks a served model, and runs `run_agent` with the
   brief. If nothing suitable is up it asks you to start something. Which model that
   is, is your call; the config does not name one, and anything you leave running
   may get used.

`localagents monitor` opens the [Monitor](#monitor) at <http://127.0.0.1:8765/> when you
want to watch. The tools, the relay subagent, the status line and where a job's record
lives are in [docs/using.md](docs/using.md).

## Backends

An endpoint is anything that serves Anthropic's `/v1/messages` and lists its models
at `/v1/models`. Beyond that, what localagents can see depends on the backend and
on how you start it. The `backend:` key in `models.yaml` picks the probe.

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
llama-server -hf <repo>:<quant> -c 131072 --metrics
```

The flag defaults below are quoted from `llama-server --help` as of build b11913
(November 2026); check yours, the defaults have moved before.

- `--jinja` — "whether to use jinja template engine for chat (default: enabled)".
  Tool calling needs it; leave it on.
- `--slots` / `--no-slots` — "expose slots monitoring endpoint (default: enabled)".
  With `/slots` on, `list_models` shows `{total, busy, free}` so Claude knows whether
  a second agent will run now or queue.
- `--metrics` — "enable prometheus compatible metrics endpoint (default: disabled)".
  With it on, each job records prompt tokens processed vs. cached, the cache hit
  ratio and prompt and generation tok/s, and the monitor's speed and cache tiles
  have data. The counters are server-wide, so overlapping jobs share the delta.
- `-c N` / `--ctx-size N` and `-np N` / `--parallel N` — the window localagents
  reports is the **per-slot** window `n_ctx` from `/props`. Without unified KV
  (`--kv-unified`, "default: enabled if number of slots is auto") that is `-c`
  divided by the slot count. Give each slot 128k or more.
- `--reasoning on` for thinking models; the default, `auto`, detects it from the
  template.
- `--api-key KEY` — put the same key in the endpoint's `api_key:`; see
  [Config](#config-modelsyaml).

### Strata

A llama.cpp-compatible server; mark it `backend: strata`. Its `/slots` and `/metrics`
are always on and `/metrics` is JSON (cumulative totals, queue depth and the GPU
figures), so no flags are needed: `list_models` shows `{total, busy, free}` plus
`requests_waiting`, jobs get the same cache and throughput record as on llama.cpp, and
the monitor draws its hardware tiles. Marked `llama.cpp` it still works, but without
occupancy or per-job metrics, and `list_models` says so in a `hint`. Strata counts
`max_tokens` against the window instead of clamping it, so it refuses a request
earlier than llama-server would; the shim translates that refusal, or set
`"fit_max_tokens": true` in its `strata-<model>.json` to clamp.

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
flag. Sessions are started with `CLAUDE_CODE_ATTRIBUTION_HEADER=0` on every backend,
because on vLLM the per-request attribution hash defeats prefix caching.

### Other

`backend: other` probes `/v1/models` and, if it answers, `/props` for a window.
Without a window the session is told nothing and Claude Code assumes 200k, so only
use it for a server whose window really is that large.

Whatever the backend, the first turn of any job costs about 20k tokens of prompt on a
cold slot (system prompt plus tool schemas), ~10 s on a 27B. Everything after that is
a cache hit plus the delta.

## Monitor

```bash
localagents monitor                 # http://127.0.0.1:8765/, opens in your browser
localagents monitor --port 9000 --config ~/.config/localagents/models.yaml
```

One process probes every endpoint in `models.yaml` into the shared database and serves
a page of them and the jobs running on them: a card per host, a table of recent jobs,
a timeline, and a page per endpoint and per job. Flags: `--port`, `--host` (default
`127.0.0.1`; the page has no authentication), `--db`, `--config`, `--no-collect` (serve
the database without probing) and `--no-open`. It stops on Ctrl-C. `tools/monitor.sh
start|status|log|stop` keeps it running in the background from a checkout.

Its settings are an optional `monitor:` section in `models.yaml`:

```yaml
monitor:
  port: 8765
  attention_hours: 24     # an endpoint seen up within this window and down now is an alert; older is dormant
  retention_days: 7       # probe rows older than this are deleted (job rows never are)
  refresh_s: 5            # at most one snapshot push per this many seconds (max 3600)
  # db: /path/to/localagents.db
```

The flags in detail, what the page shows, the smoke test and the JSON routes are in
[docs/monitor.md](docs/monitor.md).

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

`endpoints` are places that serve `/v1/messages`, and that is all the file holds; which
model a server is serving is found by probing it. Order is priority: with no model
named, the first endpoint that is up and serving something wins. `backend`
(`llama.cpp`, `vllm`, `strata`, `other`) selects the probe; `host` groups endpoints
under one card in the monitor; `env` adds environment variables to sessions that run
against that endpoint. `api_key` is sent on every call to that endpoint (as both
`x-api-key` and `Authorization: Bearer`). It stays in the MCP server: the delegated
session is given the placeholder `local` and the shim swaps the real key in, so an
agent's `env` or Bash never sees it.

`run_agent(model=...)` takes a served id, a glob, or a fuzzy name (`qwen3.8-27b` finds
`unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL`); `endpoint=` pins a server. Both are only needed
when more than one thing is up. An optional `defaults:` section holds `model` (a
standing preference in the same syntax), `permission_mode` (`acceptEdits`),
`allowed_tools` and `disallowed_tools`, `setting_sources`, `max_turns` (60),
`timeout_s` (1800), `probe_timeout_s` (2.5), `job_retention_days` (7), the debugging
switches `log_events` and `dump_requests`, and `system_prompt_append`, a suffix telling
the agent it's a delegate and how to report back. `monitor:` is the monitor's own
section, above.

## Further reading

- [docs/using.md](docs/using.md) — the tools, the `local-agent` relay, the status
  line, job records and the database.
- [docs/monitor.md](docs/monitor.md) — the monitor's flags, the page, the smoke test
  and the routes.
- [docs/shim.md](docs/shim.md) — what the shim rewrites and why, and how a session is
  kept inside the real context window.
- [CONTRIBUTING.md](CONTRIBUTING.md) — running from a checkout, the tests, and a
  seeded database for looking at the monitor without servers.

## License

MIT. See [`LICENSE`](LICENSE).

Copyright © 2026 Chris Cebelenski
