# What the shim does

Every job's requests go through an in-process shim on their way to the backend. It
records each one (timing, message count, byte size, the backend's usage) in the
`requests` table, puts the endpoint's real `api_key` on it so the session never sees the
key, and writes the `thinking`/`effort` levers into the body when a caller set them.
It speaks two APIs, one per harness: Anthropic's `/v1/messages` for Claude Code jobs and
OpenAI's `/v1/chat/completions` for OpenCode jobs. llama.cpp, Strata and vLLM serve both.

## Claude Code jobs: `/v1/messages`

The backends speak `/v1/messages` natively, so pointing `ANTHROPIC_BASE_URL` at them
almost works. The shim handles what doesn't.

Claude Code puts `role: system` entries
inside `messages` — the skills listing, a token-budget marker, and one more per
turn. Qwen's chat template refuses: *"System message must be at the beginning"*.
The shim folds each one into the adjacent user message as a `<system>…</system>`
text block, **in place**. Hoisting them into the top-level `system` field instead
changes the start of the prompt every turn, which invalidates the server's
KV-cache prefix and re-evaluates the whole ~35k-token prompt each time (21–47 s
per turn on a 27B). Folding in place keeps the prompt append-only: `f_sim_best`
0.88–0.99 in llama-server's log, 2.5–14 s per turn.

Claude Code also appends a `<system-reminder>` text
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

Claude Code assumes a 200k window for any model it doesn't recognise; with a smaller slot it runs into llama.cpp's
`exceed_context_size_error`, which it doesn't understand, and the job dies. See
the next section.

Claude Code opens every session with a request to `/api/hello`,
which no local backend serves. The shim answers it itself, so the backend never sees
it and it is not counted as an error.

Claude Code's ToolSearch tool answers with a `tool_reference` item inside a tool result.
llama.cpp's Qwen template accepts only text there and fails the request with HTTP 500
("Unexpected item type in content"); the shim rewrites each such item as a line of text
naming the tool.

Everything the shim does is a no-op when it isn't needed.

## OpenCode jobs: `/v1/chat/completions`

An OpenCode job's requests are relayed as OpenCode wrote them. The backends' usage is
mapped onto the same columns as a Claude Code job's, so every figure reads the same way:
both count the cached part of the prompt inside `prompt_tokens` and state it as
`prompt_tokens_details.cached_tokens`, so `input_tokens` is the uncached part and
`cache_read_tokens` the cached one. The generation clock starts at the first chunk that
carries generated text, reasoning or a tool call, not at the role announcement before it.
On vLLM, `--enable-prompt-tokens-details` is needed for the cached part here too.

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
   answers by compacting and retrying. llama.cpp reports the overflow as
   `exceed_context_size_error`; vLLM's Anthropic endpoint reports it as an HTTP 500
   `internal_error`; Strata refuses earlier, because it counts `max_tokens` against
   the window. All three are translated.

An OpenCode job gets the window as its model's `limit.context` in the configuration the
server writes for it, with an output budget of an eighth of the window (8k to 32k), and
OpenCode compacts on its own before it fills. An overflow that still happens goes back to
OpenCode as the backend sent it; OpenCode compacts and retries on llama.cpp's and Strata's
errors. Either way the request's row notes the overflow.

At 64k this works but thrashes: Claude Code's ~20k of fixed prompt and tool
schemas, plus a ~7k-token compaction summary (~55 s on a 27B) and the files it
re-attaches, refill the window within a few turns and its thrash guard ends the
job. Give each slot 128k or more.

