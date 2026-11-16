# Using localagents

The tools, the relay subagent, the status line, and where a job's record lives. The
[README](../README.md) covers setup; this page is the rest of the day-to-day.

Claude picks it up like any tool. Ask for it by name and it'll do the right thing:

> Use the local agent to add a `--json` flag to the CLI and cover it in the tests.

What Claude does behind that: `list_models` to see what's up and pick a model, then
`run_agent` with the brief and `wait_s` set to the job's timeout. The call blocks
until the job ends; after the auto-background delay Claude Code moves it to a
background task (shown in `/tasks`), Claude keeps working, and the final job record
arrives as a task notification. Claude reads `files_touched` (or the worktree diff)
and checks the work. The server sends a progress notification every minute while it
waits — e.g. `running: 312s, 9 turns, 23 tool calls, 2 errors; Bash: uv run pytest -q`
— so Claude Code's idle timeout does not fire, and the tool timeout itself defaults
to about 28 hours. To run several jobs, Claude makes one blocking call per job; each
one notifies on its own when it finishes.

`wait_jobs` is the fallback for calls that cannot background: start each job with
`wait_s: 0` and wait on all the ids in one call. Be careful with it: a `mode: "any"` wait
returns on the first finish and must be re-armed at once, or the remaining jobs finish
with nobody watching (one coordinator lost six hours to that). Its `running` list holds
every job whose record is not yet settled, including one whose `status` is already final
while its worktree is still being finished.

If nothing suitable is running you'll be asked to start one:

> No live endpoint is serving a model matching `qwen`. Ask the user to start one.
> Currently serving: {'gpu-server': ['GLM-5.3-Flash-EXL3']}

Start whatever you like, however you normally do, say "it's up", and Claude retries.
Which model that is, is your call; the config does not name one. It also means anything
you leave running may get used. Claude is told to name the model it's about to use and
to check with you if the id looks unfamiliar, and `list_models` flags windows under
128k.

## Tools

| tool | what it does |
|---|---|
| `list_models` | endpoints with live health (`host` up/down separately from the endpoint), served ids, context window, slot occupancy |
| `run_agent` | start a job: `task`, `model`, `cwd`, `isolation` (`none`/`worktree`), `wait_s`, `max_turns`, `permission_mode`, `resume_job`, … |
| `wait_job` / `wait_jobs` / `cancel_job` | the waits block on any server's job — another server's is polled from its row in the shared database — reporting progress while they block; `wait_jobs` takes several ids (`mode` all/any); `cancel_job` stops a job this server started |
| `job_status` / `job_log` / `list_jobs` | read any server's jobs, from the database; `job_log(tools=N)` adds the last N tool calls and carries a `note` when neither the finished job's `transcripts` row nor its directory exists — a finished job's transcript is read from the database, so it outlives the file's retention — `job_log(tail_lines=0)` returns the whole transcript, `list_jobs(all=True)` spans every server's, and the listing answers with the 50 newest by default (`limit=0` lists everything) |
| `foreign_job` | the answer only `cancel_job` and `run_agent(resume_job=…)` give for a job another server started (they need its process); the waits and the three reads above work on any job |
| `register_endpoint` | add a server from inside a session (written to `models.local.yaml`) |
| `local_complete` | one-shot generation with no tools — summaries, drafts, classification |

## The `local-agent` subagent

Auto-backgrounding needs Claude Code 2.1.212 or later and is off inside subagents and
in non-interactive runs unless `CLAUDE_AUTO_BACKGROUND_TASKS=1`. Without it a
`run_agent` job is an MCP tool call plus a job id Claude has to remember to poll.
Claude Code doesn't treat it as an agent, so nothing announces when it finishes, and
a job can be forgotten if Claude ends its turn first.

[`agents/local-agent.md`](../agents/local-agent.md) covers those cases by making each job
a real background subagent. It's only a relay. It runs on Haiku, can use only the job
tools (`run_agent`, `wait_job`, `job_status`, `job_log`, `cancel_job`). It passes the
brief through unchanged, waits, and hands back the job record. Picking the model stays
with the main session, which has `list_models` and the rest of the server's tools.
Claude passes its choice to the relay in a short header:

```
model: unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL
isolation: worktree
---
Add a --json flag to the CLI and cover it in the tests. ...
```

`model:` is required and should be the exact served id from `list_models`. Which
model runs matters, and the main session is the one with the live context windows
and slot occupancy, the conversation, and your instructions. The relay has none of
that, so it never picks a model for you. With
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

## Status line

Jobs started this way are MCP tasks, not subagents, so they don't appear in the agent
panel. `/tasks` shows the latest progress line the server reported (status, elapsed,
turns, tool calls, last activity). For an always-visible view, every server keeps its
state in the shared database — a heartbeat every ten seconds, plus its jobs, requests
and tool calls — and `localagents statusline` reads it and prints one header per
endpoint plus one row per job (the old one-line form is `localagents statusline
--one-line`):

```
local llamacpp · 1/1 sessions q1
  ⟳ Qwen3.8-27B 4m00s · 12 calls · 3 files · ctx 41% · 33 t/s · 2 compactions · Bash: uv run pytest -q
  ✓ Qwen3.8-27B 2m00s · 2 files · 3 compactions
local gpu-server · 1/2 sessions +1 foreign
  ⟳ DeepSeek-V4-Flash 2m00s · 3 calls · ctx 12% · 40 t/s
+2 jobs in other sessions
```

Per running job: elapsed time, tool calls, files touched, how full the local model's
context window is (`ctx`, from the token usage recorded on every backend request, so it
tells you how close the agent is to compaction), its recent generation speed, the
compaction count (compactions mean the window is too small for the job; the harness
reports each one with a `compact_boundary` message) and its last tool call, folded to
one line with paths relative to the job's worktree. Each header counts sessions, the
same number the queue admits on. `1/2 sessions` means one job of yours holds one of the
two sessions the endpoint can carry, whether or not it is generating at that moment (a
job running its tests holds its session while the server's slot sits idle). `+1 foreign`
appears when the server reports a request that is not one of yours, `2 wait` when the
server has requests queued of its own, and `q1` when one job waits in the localagents
queue for that endpoint. The capacity is probed every ten seconds while jobs run, and
an endpoint that could not be read shows the session count alone. Queued jobs are
listed under the running ones in the order they will be admitted. Finished jobs
stay on the line for two minutes with a check mark (or a cross, clock, null sign or
dagger — `✗`, `⏱`, `∅`, `†` — for failed, timed out, cancelled and lost), compaction
count included.

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

## Job records

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

Job directories with nothing written for more than
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

## The database

Every server writes to one SQLite file: `~/.local/state/localagents/localagents.db`
(`XDG_STATE_HOME` honoured; `LOCALAGENTS_DB=/path` overrides). It is in WAL mode and
holds every server process's jobs, the requests their shims saw, their tool calls,
their transcripts, the monitor's probes, and the endpoints and models seen so far. The
full schema is
[`docs/superpowers/specs/2026-10-29-mcp-sqlite-schema.md`](superpowers/specs/2026-10-29-mcp-sqlite-schema.md).
The record lives in the database, so it outlives the server process that ran the job: any server's `job_status`, `job_log` and `list_jobs(all=True)` can
read it after the original process is gone, and the waits can wait on it too; only
`cancel_job` and `run_agent(resume_job=…)` cannot — see `foreign_job` above. Retention
removes job directories and the monitor's old probes; job rows stay for good.

It lives on one machine — SQLite's WAL files do not work on a network filesystem, so
don't point `LOCALAGENTS_DB` at one. Back it up with SQLite's own command; a plain `cp`
of a WAL database can miss the last writes:

```bash
sqlite3 ~/.local/state/localagents/localagents.db ".backup /path/to/backup.db"
```

