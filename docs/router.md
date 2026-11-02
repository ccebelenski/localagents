# The router

The router will pick which endpoint in `models.yaml` should take a job, and it will
decide by scoring the job's brief. One endpoint needs none of this. A fleet wants it,
because the right model for a two-line rename and the right model for a cross-module
refactor are not the same model. A small decision model (d3) reads each brief, and the
job will go to the endpoint calibrated for the shape of work the brief describes. The
full design is the [router design spec](superpowers/specs/2026-11-02-router-design.md).

This page covers the config entry, the d3 server and the two command-and-control tools
as phase 1 built them. Routing a job is a later story.

## The entry (models.yaml)

```yaml
endpoints:
  auto:
    backend: router
    decision_url: http://127.0.0.1:8766
```

`decision_url`, the server running the d3 decision model, is the only required key, and
the entry needs no `base_url` because it serves no model itself. The entry's name is
the model name it answers for. The optional keys are `decision_model` (the probe reports
what the d3 server's `/health` says, and flags a mismatch with the entry),
`decision_timeout_s`, `decision_max_tokens` (default 12,000 tokens — kept under the
server's own limit on this class of GPU, measured on the live box — so an over-long brief
is truncated by the client instead of failing inside the model; a longer brief is scored
on its front part and marked truncated), and the policy knobs `tolerance` and
`divert_after_min`, which have shipped defaults. The README's config section says where
`models.yaml` lives, and `localagents router --config <path>` names another file.

## Starting d3

d3 needs the router extra (`pip install 'localagents[router]'`, or `uv sync --extra
router` in a checkout): torch, transformers and accelerate.

```bash
localagents d3 serve   # 127.0.0.1:8766, the pinned vllm-sr/d3-mini
```

Loading the weights takes a while, and `/health` reports it: `{"ok": false,
"loading": true}` until the model is resident, then the model id and device. `localagents
d3 serve --help` lists the flags for model, revision, device, host and port. A machine
that only runs Claude Code does not need the extra; only the d3 server's host does.

## Registering the command server

```bash
claude mcp add localagents-router -- localagents router
```

This adds a second, small MCP server next to the `localagents` one. It carries the
router's command, control and inspection, and nothing on it dispatches work; jobs keep
running through the `localagents` server. Phase 1 builds two tools on it, `preflight`
and `definitions`.

## Scoring a brief

`preflight(task="fix the parser", priority="normal")` scores the brief with d3 and
creates no job. `scores` holds one entry per dimension, the top band for a score
question and the chosen option for a choice question, and `confidences` holds the
model's confidence in each answer. A dimension whose answer sits below its type's
confidence bar is listed in `undetermined` and left out of `scores`, as is a language
of none. An undetermined axis is neutral: when routing decides, an axis that says
nothing pushes no endpoint in or out. `truncated` marks a brief longer than
`decision_max_tokens`, scored on its front part. `priority` echoes back (urgent,
normal or background), which is what routing will sort by. A pointer is a brief of at
most three non-empty lines whose first line starts with `cat <path>`, as in "cat
notes/brief.md and follow it"; it is read first, and the file is looked for against the
`cwd` argument, or the directory the command server was started in without one.

## Editing the definitions

The definitions document holds the questions d3 asks of every brief. Each dimension
carries an instruction and its options or bands. `definitions()` returns the
current version and the document. `definitions(action="set", document=d)` validates
the document and appends it as the next version, refusing an incomplete one.

Before saving a revision, score real briefs against it with `dry_run=True` and
`briefs=[paths]`. Each brief is read from disk and scored twice through the d3
server, once under the saved document and once under the proposed one, and nothing is
saved (a fresh store seeds version 1 of the definitions on first use). The answer lists
every dimension whose value or determined-ness moved, with the value under each
document, and gives the undetermined rate of both, so a revision
that leaves more axes neutral is visible before it is saved.

## Upgrade order

Add the router entry last. A process running the previous package builds its endpoints
from the config's keys and raises when it meets `decision_url`, a key it does not
know, and the shim re-reads the config on every request. So install the new package,
let every Claude Code session reconnect to it, and only then add the entry to
`models.yaml`. The new `Endpoint` ignores unknown keys with a warning, so the next key
added this way does not repeat the problem.

## Not built yet

Routing itself is a later story. Until the dispatch story lands, a job aimed at a
router entry is refused with `not_routable`, and so is `run_agent(model="auto")` on a
registry that has one. The calibration set, the `calibrate` command and the rest of the
router's command surface arrive with the routing stories.
