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

1. **A registry that is probed live.** `models.yaml` lists where servers are and a
   menu of model names. What each server is actually serving right now, its real
   context window, and how many slots are busy are discovered on every call. You
   bring models up and down by hand — the server never launches anything — and
   when Claude needs a model that isn't running it asks you for it by name.

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

> `qwen3.8-27b` is not running anywhere. Ask the user to bring it up.
> Notes: default mid-size coder on llama.cpp; run with --reasoning on

Start it however you normally do, say "it's up", and Claude retries.

### Tools

| tool | what it does |
|---|---|
| `list_models` | endpoints with live health, served ids, context window, slot occupancy; the pool with `available` |
| `run_agent` | start a job: `task`, `model`, `cwd`, `isolation` (`none`/`worktree`), `wait_s`, `max_turns`, `permission_mode`, `resume_job`, … |
| `wait_job` / `job_status` / `job_log` / `list_jobs` / `cancel_job` | follow and control jobs |
| `request_model` | what to tell the user to bring a pool model up |
| `register_model` / `register_endpoint` | add to the pool from inside a session (written to `models.local.yaml`) |
| `local_complete` | one-shot generation with no tools — summaries, drafts, classification |

Job records live in `~/.local/state/localagents/jobs/<job>/`: `transcript.txt`
(what the agent said and did), `events.jsonl` (every SDK message), `requests.jsonl`
(every backend request with timing, size and usage), and `requests_full.jsonl` if
you turn on request dumping.

## Config: `models.yaml`

Start from `models.example.yaml`. It's re-read on every call, so edits take effect
immediately, and the server never rewrites it — `register_*` write to a sidecar
`models.local.yaml` that is merged on top.

```yaml
endpoints:
  llamacpp:
    base_url: http://127.0.0.1:8080
    backend: llama.cpp
  gpu-server:
    base_url: http://gpu-server.lan:8000
    backend: vllm
    host: gpu-server

models:
  qwen3.8-27b:
    notes: default mid-size coder on llama.cpp; run with --reasoning on
  deepseek-v4-flash:
    host: gpu-server
    notes: vllm needs --enable-auto-tool-choice --tool-call-parser deepseek_v3
```

- **endpoints** are places that serve `/v1/messages`. What they serve is probed.
- **models** are just names. A name is fuzzy-matched against served ids
  (`qwen3.8-27b` finds `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL`), so an entry needs
  nothing but `notes` and maybe a `host` to relay when asking you to start it.
  `served_name` (exact id or glob), `endpoint`, `context` (fallback window) and
  `bring_up` (a start command) exist as overrides if you want them. Start
  commands go stale quickly; a name and a note usually age better.
- **defaults** cover the default model, `permission_mode` (`acceptEdits`), allowed
  and disallowed tools (subagents can't spawn subagents), which Claude settings to
  load, `max_turns`, `timeout_s`, and a system-prompt suffix telling the agent it's
  a delegate and how to report back.

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
  --tool-call-parser <parser>`. The session is started with
  `CLAUDE_CODE_ATTRIBUTION_HEADER=0` because the per-request attribution hash
  defeats prefix caching.
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
