# OpenCode captures (Epic 6.2)

Captured 2026-11-17 with opencode v2.3.1, one `opencode run --standalone --format json --auto
--archived` per file, through a recording proxy in front of the backend. Each run had its own
`XDG_CONFIG_HOME` holding one generated `opencode.json`: an `openai-compatible` provider (one
run `anthropic-compatible`) at the proxy, the model's `capabilities.tools` and `limit.context`,
an allow-all `permissions` list and an agent `job` with `steps`. `PWD` was set to the job's
directory (see the findings in docs/EPICS.md 6.2: without it OpenCode works in the inherited
`$PWD`). The project was a three-file git repo: `calc.py` with `add` returning `a - b`, its
test, and a 6000-line `notes.txt`.

## events/ — stdout of `--format json`, one JSON object per line

| file | backend | what it shows |
|---|---|---|
| strata-tools.jsonl | ai2, Strata | a normal job: glob, shell, read, edit, shell; `step_finish` reasons `tool-calls` then `stop` |
| strata-apikey.jsonl | ai1, Strata (api key added by the proxy) | the same task |
| llamacpp-thinking.jsonl | ai9, llama.cpp, `--thinking` | `reasoning` events |
| anthropic-llamacpp.jsonl | ai9, llama.cpp `/v1/messages` | the same task over the Anthropic-compatible provider |
| tool-error.jsonl | ai2 | a `tool_use` whose `state.status` is `error` |
| step-cap.jsonl | ai2, `steps: 3` | the cap: the last step has no tools and ends `stop`, exit 0 |
| resume.jsonl | ai2, `--session` of strata-tools | the same session ID, the backend's prompt cache read |
| cancel.jsonl | ai2, SIGTERM after 25 s | the stream ends at a step boundary; exit 143 |
| overflow-strata-retry.jsonl | ai2, Strata's overflow 400 injected past 100 kB | recovery events are absent; `retry` events (ECONNREFUSED once the proxy was gone) |
| overflow-llamacpp.jsonl | ai9, llama.cpp's overflow 400 injected past 100 kB | recovered and finished |
| compaction.jsonl | ai2, `limit.context` 32000 | OpenCode's own compaction (no event marks it) |

Tool output strings longer than 2000 characters in compaction, overflow-strata-retry and
overflow-llamacpp are trimmed, marked `[TRIMMED by the fixture copy: N chars captured]`; they
are the read tool's chunks of `notes.txt`. Recapture with `t/oc/run.py` (scratch, not
committed) for the whole text.

## wire/ — the backend side

- `strata-chat-sse-tool-calls.txt`, `llamacpp-chat-sse-reasoning-tool-calls.txt`: OpenAI chat
  completions SSE as received (status, headers, frames). Strata puts `usage` and `timings` on the
  `finish_reason` chunk; llama.cpp sends them on a trailing chunk with empty `choices`.
- `llamacpp-messages-sse-tool-calls.txt`: the Anthropic Messages SSE for comparison.
- `strata-overflow-400.txt`, `llamacpp-overflow-400.txt`: each backend's answer to a prompt
  past its window (sent directly, not through OpenCode).
- `request-shapes.json`: what OpenCode sends (keys, parameters, tool names, message roles and
  sizes); the bodies are dropped because the system prompt carried the operator's own
  instruction file (OpenCode's Claude Code compatibility reads `~/.claude/CLAUDE.md` unless
  `OPENCODE_DISABLE_CLAUDE_CODE=1`).
- `compaction-signature.json`: the user messages that mark a summary request and the checkpoint
  that opens the request after it, and the overflow recovery's request sequence.
