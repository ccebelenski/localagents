# The router

The router will pick which endpoint in `models.yaml` should take a job, and it will
decide by scoring the job's brief. One endpoint needs none of this. A fleet wants it,
because the right model for a two-line rename and the right model for a cross-module
refactor are not the same model. A small decision model (d3) reads each brief, and the
job will go to the endpoint calibrated for the shape of work the brief describes. The
full design is the [router design spec](superpowers/specs/2026-11-02-router-design.md).

This page covers the config entry, the d3 server, the routing of a job, the queue and
pools, the command-and-control tools and the calibration runner as they stand.

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

In a checkout, `tools/d3.sh start` runs the same command detached, waits until `/health`
reports the model resident, and `stop`, `status` and `log` manage it afterwards; the
`tools/README.md` row says what it needs.

## Registering the command server

```bash
claude mcp add localagents-router -- localagents router
```

This adds a second, small MCP server next to the `localagents` one. It carries the
router's command, control and inspection, and nothing on it dispatches work; jobs keep
running through the `localagents` server, where the routing arguments (`priority`,
`admit`, `tolerance`, `avoid_job`) ride on `run_agent`. The seven tools on the router
server are `definitions`, `preflight`, `calibrate`, `scorecard`, `verdict`, `explain`
and `queue`.

## Scoring a brief

`preflight(task="fix the parser", priority="normal")` scores the brief with d3 and
creates no job. `scores` holds one entry per dimension, the top band for a score
question and the chosen option for a choice question, and `confidences` holds the
model's confidence in each answer. A dimension whose answer sits below its type's
confidence bar is listed in `undetermined` and left out of `scores`, as is a language
of none. An undetermined axis is neutral in selection: an axis that says nothing
pushes no endpoint in or out. `truncated` marks a brief longer than
`decision_max_tokens`, scored on its front part. `priority` echoes back (urgent,
normal or background), which is what the queue sorts by. A pointer is a brief of at
most three non-empty lines whose first line starts with `cat <path>`, as in "cat
notes/brief.md and follow it"; it is read first, and the file is looked for against the
`cwd` argument, or the directory the command server was started in without one.

The reply also carries the routing decision for the brief. `decision` lists the rows
that would be considered, each with its shortfall or the filter that removed it, and
names the row that would be chosen with a one-line reason. The `divert` flag marks a
choice that moved off the best row because that row could not admit. When the answer
is a wait, `decision` names the row the job would wait on, with its position and
expected wait. When no row can take the job, it carries the refusal and its reason
instead. `chosen` names the chosen row's endpoint and served id, the model id that
endpoint answers for, and is null when the answer is a refusal. `avoid_job` works here
as it does on `run_agent`, and naming a job with no recorded model is refused with
`bad_argument`. Nothing is created.

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
what the router needs before it picks a row: routing picks only a row whose core
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
five such jobs. A probe's wait counts from its admission, so queue time does not spend the
probe's session budget. Probe jobs wear a `cal` mark in the status line and the monitor, and stay
out of their traffic counts. A probe measured under foreign load comes back `unreliable`, and the
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
gates, and the ceiling is the largest file count finished before a cap. Every finished job
that reached a session and was not cancelled writes its gates as it lands, so these three
cells fill as routed and named jobs run alongside the probes; the Verdicts section says
what each gate reads. A pass recomputes every cell from the row's whole evidence log
with its constants recorded, so a constant change replays the log. `pass pending` means
twenty evidence items since the last pass; `needs recalibration` means job evidence
disagrees with probes beyond a margin, or a newer probe set.

## Routing a job

```
run_agent(task="refactor the parser", model="auto")
```

The example entry's name is a model name, and `model="auto"` routes the job. Naming the
entry in `endpoint` or setting it as `defaults.model` does the same thing. Naming it in
`endpoint` together with a real `model` is refused with `bad_argument`, because the two
arguments ask for different rows. The brief is resolved first, pointer files read in, d3
scores it, and a real row is chosen from the scorecard cells, the live facts (what a
fresh probe of each endpoint answers right now) and the queue. The job record then names
the chosen row's endpoint, model and base_url the way a directly-named job does, so a
routed job reads back like any other. `routed_by` carries the router entry's name, the
one-line reason the choice came to, and the pool's name when a pool is what made the job
wait or divert. `list_models` marks the entry `router: true` and gives the fleet's
`context_range`, the smallest and largest context window probed on any real endpoint
just now.

Nothing is created until a row is chosen, and a refusal writes no record.
`brief_unresolved` names the pointer's file that could not be read. `model_unavailable`
says the d3 server is down, slower than `decision_timeout_s`, or answering badly, and
names which; a router entry missing its `decision_url` gets the same answer.
`no_calibrated_row` says no row of the config has finished its core calibration and
points at `calibrate`. `no_acceptable_row` says every calibrated row was filtered out,
and the refusal carries the table of considered rows with the filter each met and the
axes d3 left undetermined.

### Choosing

A row is dropped before ranking when it serves the model `avoid_job` names, when its core
calibration is incomplete, when its window is smaller than the brief's load, when its
ceiling is under the number of files the brief names, when its facts are unavailable, or
when an `exclude:` condition of the entry matches it. What is left is ranked by
shortfall. It comes from each determined axis's gap against the row's cell, weighted by
the brief's demand on the axis. Thin completeness and adherence add a penalty. An axis
the row has shown nothing on counts at the brief's full demand. The best row is always
acceptable. A row whose shortfall sits at most `tolerance` above the best one is
acceptable too. `tolerance` is an absolute distance in shortfall units, and its default
is 0.5. A tie goes to the faster predicted speed, then to fewer jobs already queued
there, and the order the scorecard's rows come back in decides the rest. Only a
background job may take the session slot where the endpoint's capacity curve says its
sessions begin to slow. That rule holds at admission for named jobs too. On a two-slot box
whose curve slows at the second session, a second named job of normal priority waits for the
first to finish, a background job takes the slot, and `admit: force` takes it regardless.

### The ticket

Every routed job stores its decision's reasoning in a ticket, written with the record in
one transaction. It holds d3's scores with confidences, undetermined axes and the
truncated mark, the definitions version, the pass whose cells were read, the considered
rows each with its shortfall or filter, the chosen row, the queue position and expected
wait as they stood at enqueue, and flags carrying the priority, the reason, what caused
the wait, the pool, and later the forced, resumed, stalled and row-down marks. The
position is absent when the job admitted at once. `job_status` and `list_jobs` carry a
short summary of the ticket, and `explain` answers with the whole ticket beside the
record's endpoint, model and base_url. A job aimed at a named model has no ticket, and
the answer says `no_ticket`.

## Waiting and the queue

The queue is the database. Every job gets its entry in the queue table in the same
transaction as its record, routed or named alike, and a session starts only when that
entry is admitted. An entry admits when it is the head of its endpoint's queue, the
endpoint's sessions have room, and any pool it shares has room too. The order is priority
rank first and arrival second. An entry whose owning server has gone quiet is skipped by
every admission, and the admission that notices closes its still-queued job as `lost`, so
a closed session at the head of a queue blocks nothing.

`priority` is `urgent`, `normal` or `background`, and the queue orders by it while it
decides what a busy best row means. An urgent job finds the best row busy and moves to
the best free acceptable row at once. A normal job waits for the best row and diverts
only once its expected wait passes `divert_after_min`. A background job takes a free
acceptable row only when no higher-priority job is queued on it ahead, and a background
job with nowhere safe to go waits on the best row rather than crowd one. The decision is
re-applied at every wake, and a divert moves the entry, the record and the ticket
together.

`admit` is `queue` (the default), `now` or `force`. `now` refuses with `would_queue` and
creates no job when the answer is a wait, and the refusal carries the position, the
expected wait, every considered row that could admit now with a flag saying whether it
was acceptable, and the ticket; when the room disappears between that answer and the
record, the job is dropped with `would_queue` as its error and the reply names the
dropped id. `force` admits out of order and runs past
capacity; its record is marked forced, and its session counts against the endpoint and
any pool like any other. `tolerance` overrides the entry's tolerance for one job and must
be a number that is zero or greater. `avoid_job` names a job whose served model routing
must not choose. That model leaves the considered table. Naming that model directly as
`model` is refused with `bad_argument`, and so is an `avoid_job` whose job has no
recorded model.

The blocking wait counts from admission. `run_agent` reports `queued #n, ~m min` while
its entry waits, and the queue phase runs until admission or the caller cancels;
`admit="now"` is how to not wait. The job's `timeout_s` and the call's `wait_s` both
start at admission, so time spent in the queue does not spend the session's budget.

`queued` means recorded, waiting, not started; the record already names the row it waits
on. `dropped` is the terminal sibling, for a job that left the queue before its session
and never ran: `started_at` stays null and `error` names the cause. The causes are
cancellation while queued, the chosen row going down (the guidance is to resubmit, which
rescores the brief), the served model disappearing, the entry being removed by hand,
`would_queue` in the race above, and a hand drop by the `queue` tool. A dropped job
never had a session and has no result to read. That is what sets it apart from a failed
job.

A queued job's wake comes when a session in the same process ends, or from a 30-second
poll. Each wake reads the row against the live registry first. An endpoint that left the
config, went down, stopped serving the chosen model or moved its window moves the job off
it: a job at the head of that endpoint's queue whose row went down is dropped,
`row_down` with resubmit guidance for a routed job and `model_unavailable` for a named
one, while a job further back waits, since an endpoint that is restarting reads as down
for about a minute. A
running session with no message, tool call or start for 15 minutes gets one notice in its
log saying how long it has been quiet and whether the endpoint answers a probe, and a
routed job takes a `stalled` mark on its ticket. Nothing is cancelled; the job's own
timeout still applies. The notice comes from the periodic load sampler, so a stall shows
up even when nothing is queued and no wake would come. That sampler wakes every ten
seconds, and while this process has unfinished jobs it probes the endpoints those jobs
are on. It counts only the sessions that actually run, so a queued job does not make its
endpoint look busy.

On the read side, `job_status` and `list_jobs` carry a `queue` view with the position and
expected wait while a job waits, a `forced` mark, and the ticket summary. The `queue`
tool answers per endpoint with the sessions' kind and capacity, ours and the foreign
count beside them (foreign sessions are the ones on the endpoint that this queue did not
start), the pool and its effective cap, the running sessions, the queued entries in
order with each position, priority and what it is blocked on, and the endpoint's live
probe. Passing `drop` with a job id removes one entry by hand: a queued
job closes as `dropped`, and a running session keeps its status and merely stops being
counted for admission.

## Pools

A pool is a limit two endpoints share that capacity alone cannot see: two endpoints on
one host, where one job per host is the safe line, or several hosts on one UPS group,
where one at a time is. The operator writes both halves by hand. An entry names its
pool, and a `pools:` block names the group and its cap:

```yaml
endpoints:
  box-a:
    base_url: http://127.0.0.1:8081
    backend: llama.cpp
    pool: gpu-1
  box-b:
    base_url: http://127.0.0.1:8082
    backend: llama.cpp
    pool: gpu-1
pools:
  gpu-1:
    max_concurrent: 2
```

A `pool:` name that `pools:` does not declare is inert: the config load notes it and the
entry carries no pool. The block is hand-written, and nothing of a pool is inferred from
hosts, ports or probes, because a pool states something about wiring that no probe can
see. Admission counts sessions across every member: an entry admits when its endpoint has
room and the pool's running total sits under the effective cap, which is the group's
`max_concurrent` less the foreign sessions the probe sees on the members, floored at zero
since the queue counts only ours. The queue order holds across a pool, so an entry held
by an earlier pool-mate waits even when its own endpoint has a free session. When the
pool is what held the best row, the decision records `pool` as the wait's cause and
`routed_by.pool` names the group; an entry waiting on a plainly full endpoint reads
`endpoint` instead.

## Verdicts and feedback

A finished job feeds back the row it ran on. When the job reached a session and was not
cancelled, the exit hook writes the job's gates, and where the job's session count and a
measured generation speed are both known it writes a speed sample, and a sample comes
only from a measurement. Speed samples merge by median at each session count and refine
the capacity curve the throughput probe seeds. Neither feed
changes the job's status, and a feed that throws is a log line, not a failed job.

The gates are six. `tests` reads the record: where the brief names a test runner, the
last finished runner call decides, and red then green reads a pass. `not_capped` fails a
session that ran out of wall clock or of turns. `stop_rule` is null unless the report
claims the job stopped instead of working, and true only on an empty diff. `no_tmp` fails
when an added line writes under `/tmp`. `files_within` fails a changed file the brief did
not name, and stays null when the brief named none. `lint` reads the three hint rules
over the diff, a constant used after its own use, a wrapper that rethrows nothing new,
and a literal tab on an added line where the format does not want one (Makefile, `.mk`,
`.go` and `.tsv` keep their tabs and are exempt). The three diff gates read the job's
worktree, and a job without one leaves them null. A null on a gate says the gate could
not be read from that job, and a scorecard pass reads it as no observation.

`verdict(job_id, accepted)` records the operator's reading of a terminal job, and a
second verdict for the same job replaces the first. `dimensions` repeats the call per
axis, 1 to accept, -1 to refuse and 0 to defer to the job-level answer; an axis d3 left
undetermined adds no observation and is named in `no_observation`. `correction` gives the
brief's true scores where d3 mis-scored it, logged against the ticket's definitions
version. A routed job's ticket already holds its scores; a job that ran a named model has
none, so its brief is scored through d3 at verdict time and the reply's
`scored_at_verdict` flag says so. The reply carries the row's notices, where `pass
pending` and `needs recalibration` mean what the scorecard section says. A scorecard pass
is what folds the accumulated gates, verdicts and probe results into cells, and a routing
decision reads the cells as they stand and records the pass it read in the ticket.

## Upgrade order

Add the router entry last. A process running the previous package builds its endpoints
from the config's keys and raises when it meets `decision_url`, a key it does not
know, and the shim re-reads the config on every request. So install the new package,
let every Claude Code session reconnect to it, and only then add the entry to
`models.yaml`. The new `Endpoint` ignores unknown keys with a warning, so the next key
added this way does not repeat the problem.

The state directory's schema is at version 12. The step to it rebuilds the jobs table to
carry routing's columns and gives the queue entries the mark of what they wait on. A
newer package migrates an older file on first open, and a package older than the file
refuses to open it with a line naming both versions. Nothing is moved by hand.

## Not built yet

One piece has not shipped: the benchmark probes behind the medium and large tiers, so
those tiers plan nothing and a run of them is refused. Routing, the queue and its tools,
pools, the job gates and the verdict tool landed with phase 3.
