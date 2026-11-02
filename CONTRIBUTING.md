# Contributing to localagents

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Something that serves `/v1/messages` locally if you want to run a real job:
  llama.cpp's `llama-server`, Strata, or vLLM (see the README's Backends section for the flags).

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
overflow translation), the registry's model matching, resume and worktree handling, the
blocking waits' progress reporting, the status-line renderer, and the relay agent file. None of them need a model
server. Anything that does — a real job against llama.cpp or vLLM — is done by hand;
if you change `runner.py` or `shim.py`, run a short job and compare its rows in the
`requests` table of `~/.local/state/localagents/localagents.db` before and after.

## Where things live

| file | what |
|---|---|
| `server.py` | the MCP tools; thin, mostly argument handling |
| `registry.py` | `models.yaml` (endpoints only), endpoint probing (health, served ids, context window, slots, metrics), model resolution |
| `runner.py` | one job = one headless Claude Code session via the Agent SDK; env, worktrees, transcript |
| `shim.py` | the in-process HTTP proxy every session talks through; all backend-specific fixups go here |
| `db.py` | the shared SQLite file: path, schema, migrations, `open_db` (WAL, busy timeout) |
| `jobs.py` | job records: the `jobs` and `mcp_servers` rows, the job directory and transcript, retention pruning, the startup lost-job mark |
| `derive.py` | read-time derivations over the `requests`/`tool_calls` rows: usage totals, per-tool counts, the last tool, the whole `job_status` dict for a row |
| `status.py` | the status-line renderer: reads every live server's jobs from the shared database; `localagents statusline` runs it without importing the server |
| `agents/local-agent.md` | Claude Code subagent that relays one job and reports its record; the fallback where auto-backgrounding is off |

A note on `agents/local-agent.md`: the frontmatter must be valid YAML, so quote the
description. The first version had an unquoted description containing `model: <served id>`;
a colon-space inside a plain scalar is a YAML error ("mapping values are not allowed here"),
and Claude Code then falls back to a lenient parse that keeps `name` and `description` and
silently drops everything after (`tools`, `model`, `background`, `omitClaudeMd`, `maxTurns`).
The relay then runs on the main session's model with every tool and CLAUDE.md loaded; the
only visible symptom is "Tools: All tools" in the agent listing. `tests/test_agent_file.py`
parses the file and checks the keys so this cannot come back quietly.

Backend quirks belong in the shim, not in the runner. If a new server rejects
something Claude Code sends, the fix is a normalisation in `shim.py` with a unit test
that uses the real error body you saw.

## Seeding a database for the monitor

The monitor's route tests and browser smokes (`tools/smoke/`) run against a **seeded**
database, built offline and deterministically from the captured fixtures under
`tests/fixtures/monitor/` with every timestamp shifted to `--now` (the current time by
default). To see the page with data and no servers:

```bash
LOCALAGENTS_DB=/tmp/seed.db localagents seed
LOCALAGENTS_DB=/tmp/seed.db localagents monitor --no-collect
```

The seeded world has three endpoints, named after the fixtures' sources: **ai1** (Strata, live — an hour of probes at 5 s with
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

## Pull requests

Keep them focused. A change that touches the request shape or the env passed to
Claude Code should say what you measured (the `requests` rows' timings and the
server's prompt-eval numbers are the usual evidence). CI runs the unit tests on
Python 3.12 and 3.13.
