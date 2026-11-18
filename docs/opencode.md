# OpenCode

OpenCode fits localagents in two places, and they are independent. It can be the
**coordinator**, the session that calls `run_agent`, as Claude Code is. And it can be the
**harness** a job runs in: the headless agent on the local model, which is a Claude Code
session unless the call says otherwise. An OpenCode session can start Claude Code jobs, and a
Claude Code session can start OpenCode jobs.

This page assumes OpenCode 2.x (built against 2.3.1).

## OpenCode as the coordinator

Register the server and install OpenCode's relay agent:

```bash
opencode mcp add local --global -- localagents --config "$PWD/models.yaml"
mkdir -p ~/.config/opencode/agents && ln -s "$PWD/agents/opencode/local-agent.md" ~/.config/opencode/agents/
```

OpenCode reloads its config on save, so the `local` tools appear without a restart. The
server reads the client's name when OpenCode connects and sends OpenCode's version of its
instructions; `localagents --client opencode --config ...` says so explicitly. Claude Code gets
its own version, and the two differ only in how a long call is waited on.

OpenCode waits on a tool call for as long as it runs (its MCP execution timeout is 12 hours by
default), so a blocking `run_agent` simply returns when the job ends and there is no
auto-background delay to tune. To run several jobs at once, give each to the `local-agent`
subagent. Subagents run in the background and wake the main session when their job ends. The
relay takes the same header as Claude Code's (`model:`, optional `endpoint:`, `cwd:`,
`isolation:`, `max_turns:`, `timeout_s:`, `permission_mode:`, `resume_job:`,
`background_tasks:`, then `---` and the brief), makes one blocking `run_agent` call, and hands
the record back. It runs on the session's model unless you add a `model:` line to its
frontmatter, and its permissions allow only the server's tools. Registered under a name other
than `local`, copy the file and change `local_*` in its permissions and the tool names in its
text.

Each job records the OpenCode session whose call started it (`owner_session`; for a relay's
job that is the relay's own session) and each server row the client it answered (`client`). There is no OpenCode
status line yet; `localagents statusline` is written for Claude Code's status line script.

OpenCode can replace the `local` process a session talks to (after `opencode reload`, and
sometimes on its own), and a subagent may get a process of its own. A job lives in the
process that started it: reading it (`job_status`, `job_log`, `list_jobs(all=true)`) and
waiting on it work from any process, but `cancel_job` and `resume_job` need the one that
started it and answer `foreign_job` otherwise. Cancel a relay's job through the relay.

## OpenCode as the harness

```
run_agent(task=..., model=..., harness="opencode")
```

or `defaults.harness: opencode` in `models.yaml` for every job. A resume runs in the harness of
the job it resumes. The `opencode` binary has to be on the server's `PATH`. The job page and the
job records show the harness; the status line and the Recent jobs table mark a job that did not
run in Claude Code with the harness's name.

Each job is one `opencode run --standalone --format json` in the job's directory. It talks to
the backend's OpenAI-compatible `/v1/chat/completions` through the shim (llama.cpp, Strata and
vLLM all serve it), so every request is recorded like a Claude Code job's and the same figures
come out: context fill, generation and prefill speed, cache reuse, tool calls, files touched.

The job is kept apart from your own OpenCode and Claude Code setup. Its configuration is a file
the server writes into the job's directory (`opencode-config/`), which replaces yours for that
process: one provider pointing at the shim, the model's real context window, the job's
permissions and instructions. Your `~/.claude` (its CLAUDE.md and skills) is kept out as well.
OpenCode's own session data for jobs is under `~/.local/state/localagents/opencode/`, shared by
every job so a resume finds its session, and apart from your own sessions.

### What the settings mean

The job settings are written in the vocabulary `run_agent` and `models.yaml` have always used,
Claude Code's. For an OpenCode job each one is translated, the transcript's `[opencode] policy:`
lines state the result, and a setting with no equivalent is refused with `bad_argument` before
the job exists (a refusal caused by your config's defaults says so).

| setting | in an OpenCode job |
|---|---|
| tool names | Claude Code's map to OpenCode's: `Bash` → `shell`; `Edit`, `MultiEdit`, `NotebookEdit` → `edit` and `patch`; `Write` → `write`; `Read`, `LS`, `NotebookRead` → `read`; `Glob` → `glob`; `Grep` → `grep`; `WebFetch` → `webfetch`; `WebSearch` → `websearch`; `Agent`, `Task` → `subagent`; `Skill` → `skill`. OpenCode's own names are taken as they are. `mcp__<server>` is all of that server's tools, `mcp__<server>__<tool>` one of them. Any other name is refused. |
| `permission_mode` | A headless run cannot stop to ask. `default`: exactly the allowed tools. `acceptEdits`: the allowed tools plus the edit tools. `bypassPermissions`: every tool. `plan`: the allowed tools minus `edit`, `patch`, `write` and `shell`, so the job can only read. The disallowed tools are taken out after the mode. Any other mode is refused. |
| tools not permitted | switched off, so they are not offered at all. A job never gets OpenCode's interactive `question`, image generation, code mode, or OpenCode's own worktree, session and model tools. With the stock defaults a job gets `edit`, `write`, `read`, `glob`, `grep`, `shell` and `webfetch` (and `patch` for a model OpenCode offers it to). |
| `max_turns` | OpenCode's step limit. Its last step runs without tools and is told to summarise; the record reads `failed` with `max_turns: the session reached its step limit (N steps)`. |
| `system_prompt_append` | added as an instruction file, after the config's own. |
| `setting_sources` | which instruction files the job gets: `project`, the CLAUDE.md files from the job's directory up to the repository root; `local`, their CLAUDE.local.md files; `user`, your `~/.claude/CLAUDE.md`. Any other source is refused. OpenCode also reads a project's `AGENTS.md` itself. |
| `background_tasks: false` | an instruction not to start background commands. OpenCode's shell tool keeps its `background` parameter, so unlike Claude Code's this is a request, not a switch. |
| `thinking`, `effort` | written into every request by the shim, as for Claude Code. |
| MCP servers (router and calibration callers) | written into the job's config; a stdio server becomes a local one, an http or sse server a remote one. `strict_mcp_config: false`, which would give the job your own MCP servers, is refused. |
| an endpoint's `env:` | Claude Code sessions only; it does not reach an OpenCode job. |

### Things that differ from a Claude Code job

- **Compactions** are counted at the shim: OpenCode emits no event for one, but every request
  after a compaction opens with a new conversation checkpoint, and each new one counts.
- **Context overflow** reaches OpenCode as the backend's own error, unrewritten. OpenCode
  compacts and retries on llama.cpp's and Strata's overflow errors.
- **A tool call in flight** when a job is cancelled or times out leaves no row: OpenCode reports
  a tool call only once it has finished.
- **Transport errors** are retried by OpenCode without limit; the harness ends a session after
  six retries in a row.
- **Cancel and timeout** end OpenCode's whole process group (SIGTERM, then SIGKILL after 20 s).
- Calibration jobs always run in Claude Code, whatever `defaults.harness` says: its probes are
  built for Claude Code. A routed job runs in the harness the call or the config names.
