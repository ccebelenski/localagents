# Contributing to localagents

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Something that serves `/v1/messages` locally if you want to run a real job:
  llama.cpp's `llama-server` (with `--jinja`) or vLLM.

## Setup

```bash
git clone https://github.com/ccebelenski/localagents.git && cd localagents
uv sync --dev
cp models.example.yaml models.yaml   # point it at your server(s)
```

## Running it

```bash
uv run localagents --config models.yaml           # stdio MCP server, as Claude Code launches it
uv run localagents --transport streamable-http    # if you'd rather poke it with an HTTP client
```

To try it inside Claude Code without installing: add the repo to a project's
`.mcp.json` with `uv run --project <this repo> localagents --config <path>/models.yaml`.

## Tests

```bash
uv run pytest -q
```

The unit tests cover the shim's request rewriting (system-message folding, context
overflow translation) and the registry's model matching. None of them need a model
server. Anything that does — a real job against llama.cpp or vLLM — is done by hand;
if you change `runner.py` or `shim.py`, run a short job and look at
`~/.local/state/localagents/jobs/<job>/requests.jsonl` before and after.

## Where things live

| file | what |
|---|---|
| `server.py` | the MCP tools; thin, mostly argument handling |
| `registry.py` | `models.yaml` (endpoints only), endpoint probing (health, served ids, context window, slots, metrics), model resolution |
| `runner.py` | one job = one headless Claude Code session via the Agent SDK; env, worktrees, transcript |
| `shim.py` | the in-process HTTP proxy every session talks through; all backend-specific fixups go here |
| `jobs.py` | job records and on-disk logs |

Backend quirks belong in the shim, not in the runner. If a new server rejects
something Claude Code sends, the fix is a normalisation in `shim.py` with a unit test
that uses the real error body you saw.

## Pull requests

Keep them focused. A change that touches the request shape or the env passed to
Claude Code should say what you measured (the `requests.jsonl` timings and the
server's prompt-eval numbers are the usual evidence). CI runs the unit tests on
Python 3.12 and 3.13.
