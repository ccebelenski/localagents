# The router

The router will pick which endpoint in `models.yaml` should take a job, and it will
decide by scoring the job's brief. One endpoint needs none of this. A fleet wants it,
because the right model for a two-line rename and the right model for a cross-module
refactor are not the same model. A small decision model (d3) reads each brief, and the
job will go to the endpoint calibrated for the shape of work the brief describes. The
full design is the [router design spec](superpowers/specs/2026-11-02-router-design.md).

This page covers the config entry, the d3 server, the command-and-control tools and
the calibration runner as they stand. Routing a job is a later story.

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
running through the `localagents` server. The four tools on it are `preflight`,
`definitions`, `calibrate` and `scorecard`.

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

## Calibration

A named endpoint is usable through `run_agent` with or without calibration. Calibration is
what the router needs before it will pick a row: routing will pick only a row whose core
calibration is complete (the `core_complete` flag). The endpoint being calibrated is an
ordinary entry in `models.yaml`:

```yaml
endpoints:
  mybox:
    base_url: http://127.0.0.1:8080
    backend: llama.cpp
```

The model behind the name must be running, on a quiet box; calibration needs neither the d3
server nor the `router` extra.

```bash
localagents calibrate mybox   # the core tier, the default
```

`mybox` is the endpoint name. A completed run exits 0; a stopped or incomplete one exits 1. A
down endpoint, a busy one or a run already in flight exits 1 with the reason. An unknown name,
a router entry, a tier with no probes or an unreadable config exits 2. `--tier` picks the tier
to run, a tier being one group of the set's probes, from core (the default), medium and large.
`--estimate` prints the estimate and stops; it writes the throughput probe's capacity curve
and the endpoint's scorecard row, and no run, result or job. `--recompute` runs a scorecard
pass for each live row of the endpoint, prints each cell's dimension, language, estimate,
count and thin flag, and exits. `--reset` archives the live row; `--keep-going` reads below;
`--set` and `--config` name another manifest and `models.yaml`.

The `calibrate` tool on `localagents router` drives the same runner. It starts the run in the
background and answers at once with the run id and the estimate line. While a run is in
flight, a second call for that endpoint is refused with `run_in_progress`. A second
`localagents calibrate` is refused the same way; a lock file in the state directory (the
package's home for its jobs and runs) tells the command that a process already drives the run.
Refusals go to stderr, and report lines are flushed as they print.

The first line is the estimate, the tier's nominal seconds times the row's speed factor with a
50% margin, as in `estimate: 14 probes, ~9.4-14.0 min (speed factor 1.00, margin 50%)`.
Before any run the factor comes from the throughput probe: its single-session speed against
the set's reference speed, the speed of the row the nominal seconds were measured on (a dense
27B model on llama.cpp for the shipped set); a set without one seeds the factor at 1.0. A complete run
measures the factor as the median of actual over nominal seconds across its passing probes,
and the next estimate uses it. A first command with no capacity curve yet runs the throughput
probe, which refuses while the endpoint serves. Each probe prints one line as it finishes
(`tool-bisect: pass (22.4s)`). After three probes the run recomputes the estimate from
actuals, and twice the original stops the run; `--keep-going` runs on. A complete run prints
its status and the speed factor it measured; a stopped or incomplete run prints its status. A
later `calibrate` resumes an unfinished run and keeps its passes and fails; after a complete
run, `calibrate` starts a new one.

The shipped core set holds fourteen probes, thirteen of them across five task families.
Judgement probes plant a bug no brief mentions, assert a premise the code contradicts, lean on
a module that is not installed and leave a design decision open. Review probes ship a defect
to find and a clean module that must yield none. Tool-use probes fix a failing build, bisect a
broken commit, lift a wrong config value out of a log and count a tree. `tool-mcp-stats`
registers a stub `localagents` MCP server inside the session and records every call; a job
that dispatches work through the stub fails the probe with the checker's reason `dispatched`.
`adherence-tmp-temptation` offers `/tmp` to a job whose rules say `t/`, and
`completeness-tail` puts the report last in a brief that can run out of turns. The fourteenth
probe is the facts probe, which runs its brief five times as five jobs (defaults, thinking on
and off, effort low and high) and writes what it saw to the row. The benchmark probes behind
medium and large are unshipped, so those tiers plan nothing and a run of them is refused.

A probe runs on a freshly committed copy of its packaged fixture, materialised into the state
directory; your checkout stays untouched and each attempt keeps its directory as evidence.
Every probe runs as a background job the server records like any other, and the facts probe is
five such jobs. Probe jobs wear a `cal` mark in the status line and the monitor, and stay out
of their traffic counts. A probe measured under foreign load comes back `unreliable`, and the
run reruns it once before it closes. A rerun still unreliable keeps the run from being
complete, and a later `calibrate` resumes it. A probe whose toolchain is absent, or whose job
failed for an environment reason (the box went away, the job was cancelled or lost), comes
back `skipped` with the reason; a resume retries it. A probe that runs into the session's turn
limit, its cap, fails as `tail_capped`, and a capped capability probe stops the tier; a capped
lever of the facts probe fails the facts probe only. A run is complete when every planned
probe reads pass, fail or skipped and at least two thirds of them read pass or fail, and a
complete run then ends with a scorecard pass.

### The scorecard

A row pairs an endpoint name with the model id it serves. A different served model is a
different pair and opens a fresh row. `--reset` archives the live row by command, and when
that pair serves again the archived row reopens with its history. `scorecard()` on
`localagents router` answers the live rows; it is an MCP tool, not a shell command. A reply is
`{"rows": [{endpoint_name, served_id, archived, set_version, speed_factor, core_complete,
notices, facts, latest_run, cells}]}`, one entry per live row, and a notice is a standing flag
on the row. `latest_run` carries the newest run's id, status and probe counts, so a caller can
follow a run the `calibrate` tool started in the background. `endpoint` narrows to a name,
`history` adds every past pass so a swap reads before and after, and `recompute` runs a fresh
pass first, as does `localagents calibrate --recompute` from a shell.

A cell is an estimate from 0 to 4 for one dimension. It carries the weighted count, how many
probes and how many jobs fed it, and flags for no estimate yet and thin evidence. The
capability dimensions are software_dev, per language and in aggregate, plus tool_use,
computer_use, complexity and judgement. Completeness and adherence are rates over clean job
gates, and the ceiling is the largest file count finished before a cap. Nothing writes job
gates until the routing stories land, so after a core run these three cells show no estimate
and a count of 0. A pass recomputes every cell from the row's whole evidence log with its
constants recorded, so a constant change replays the log. `pass pending` means twenty evidence
items since the last pass; `needs recalibration` means job evidence disagrees with probes
beyond a margin, or a newer probe set.

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
registry that has one. The calibration runner and the `calibrate` and `scorecard`
tools landed with phase 2. The mechanical job gates and the `verdict` tool that fill
a row's job-side evidence, the benchmark probes behind the medium and large tiers,
and the remaining command tools `explain` and `queue` are not built.
