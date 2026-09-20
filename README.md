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
   recognise. The shim fixes both directions (details below) and writes a
   `requests.jsonl` per job so you can see exactly what went over the wire.

3. **The same isolation model as Claude's own subagents.** By default a job works
   in your tree, like the Agent tool does. `isolation: worktree` gives it a fresh
   git worktree on a `local-agent/<job>` branch, kept only if it changed something,
   with a diffstat in the job record so Claude can review it as a diff.

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

## Using it

Claude picks it up like any tool. Ask for it by name and it'll do the right thing:

> Use the local agent to add a `--json` flag to the CLI and cover it in the tests.

What Claude does behind that: `list_models` to see what's up, `run_agent(task=…)`
which returns a job id, then `wait_job` / `job_status` / `job_log` until it's done,
then reads `files_touched` (or the worktree diff) and checks the work. Jobs that
outlive Claude Code's 2-minute tool timeout get backgrounded and picked up later;
you don't have to do anything.

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
| `list_models` | endpoints with live health, served ids, context window, slot occupancy |
| `run_agent` | start a job: `task`, `model`, `cwd`, `isolation` (`none`/`worktree`), `wait_s`, `max_turns`, `permission_mode`, `resume_job`, … |
| `wait_job` / `job_status` / `job_log` / `list_jobs` / `cancel_job` | follow and control jobs |
| `register_endpoint` | add a server from inside a session (written to `models.local.yaml`) |
| `local_complete` | one-shot generation with no tools — summaries, drafts, classification |

Job records live in `~/.local/state/localagents/jobs/<job>/`: `transcript.txt`
(what the agent said and did), `events.jsonl` (every SDK message), `requests.jsonl`
(every backend request with timing, size and usage), and `requests_full.jsonl` if
you turn on request dumping.

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
  `host` and `notes` are informational and shown in `list_models`; `env` adds
  environment variables to sessions that run against that endpoint.
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
