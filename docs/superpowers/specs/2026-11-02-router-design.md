# The router: design

Date: 2026-11-02. Status: approved in conversation, awaiting written review. Builds on its own
"go" (ruling (p)). The calibration SET (which probes, how many, their nominal timings) is a
sub-project with its own spec; this document fixes the interface it must satisfy (§10).

Operator, verbatim, on what this is: "The routing model needs to make an informed decision about
which 'agent' is best suited to the task - that takes it out of Claude's 'hands' to decide that
on a job basis - The routing model needs calibration up front in addition to basic stats, like
context size." On who it is for: "This project makes no sense for the user with one local gaming
GPU - they don't need it at all. [...] The real user actually we be someone like me or a small
business with multiple endpoints. [...] I'd rather plan to have the d3 model always available
because that gives us a consistent target." On where it sits: "I would include it as an endpoint
type in the existing models.yaml file for simplicity - Claude can then use it without even having
the second MCP available - Then the second MCP becomes purely C&C and monitoring specific to the
router." On the learning: "the classifier doesn't ever get 'better' - it just gets a correction
signal." On calibration: "pick the right subsets, be honest about time investment, and be
transparent when running - plus the usual details, be restartable/resumeable etc because it might
take some time. Ultimately I like the core to be itself calibrated time based on whatever timings
we find here - That way we can estimate time to complete on whatever hardware we find on the
install."

## 1. Purpose and scope

- **For:** an operator with several local endpoints (a fleet) who wants a job sent to the model
  best suited to it without naming the model each time, and wants a record of why.
- **Not for:** one endpoint. Claude uses localagents directly and nothing here is needed.
- **Target:** a fleet plus a Decision 3.0 model (vllm-sr/d3-mini by default, d3-lite where memory
  is tight) served on a GPU, always available. No prompted-chat fallback, no CPU path: the spike
  (docs/spikes/2026-11-01-d3-categorizer.md) measured both and the one consistent reader is the
  point.
- **Ground truth vs data:** the operator's fleet is the reference for tests, fixtures and the
  nominal timings. The package ships no scores, no curves and nothing named from that fleet (the
  no-operator-setup rule). Everything a row holds is learned on the installing hardware.

## 2. The decision in one paragraph

The router is a `backend: router` entry in `models.yaml`. `run_agent` targeting it runs one extra
step before the job runner does what it does today: the brief is resolved (a pointer brief is
read), d3 scores it on a fixed set of dimensions, the live facts of every real endpoint are read,
the scorecard rows are filtered by facts and ranked by weighted shortfall, the priority policy
decides between the best row and a free acceptable one, and the job is enqueued on the chosen
real endpoint. From there it is an ordinary job (shim, record, status line, monitor) carrying a
ticket that says who was considered and why each lost. The router is never on the wire: no proxy,
a decision per job, never per request. Admission queues move up into the MCP layer for every
endpoint, router or not, with the database as the queue. The scorecard is seeded by a static
calibration run and corrected by two feeds, the mechanical gates that already exist in accept.sh
and Claude's explicit verdict. The categorizer itself does not learn; its definitions are text
Claude edits, with the probe set as the regression test. A second MCP server is command, control
and inspection only.

## 3. Components

Same repository, same package, installed as an extra (`localagents[router]`: torch, transformers
pinned to what d3 needs). Shares registry, probes, shim and database as code. Own modules, own
tables, own MCP surface.

| Component | Does | Lives |
| --- | --- | --- |
| Router endpoint | the resolution step in `run_agent` dispatch | `backend: router` in the registry |
| d3 server | serves `/v1/systemone` for the pinned model; `localagents d3 serve` | its own process; URL in the router entry |
| Dimension definitions | the questions d3 is asked, versioned | a table, edited through the C&C tool |
| Capacity model + queue | admission per real endpoint, the curve, the wait | registry + jobs store |
| Scorecard | rows (endpoint × served id), cells per dimension | tables |
| Feedback | mechanical gates after every job; the verdict tool | job completion hook + C&C tool |
| Calibration runner | runs a tier of the probe set as calibration-marked jobs | `localagents calibrate` (CLI, like `monitor`) and the `calibrate` tool; the set's spec is separate |
| Router MCP | calibrate, scorecard, explain, verdict, definitions, queue, preflight | second server, `localagents router` |

## 4. The dispatch path

`run_agent(task, model="auto", priority="normal", admit="queue", ...)` where `auto` is the router
entry's served id (the operator names it). New arguments, both optional on every call:
`priority` ∈ urgent | normal | background; `admit` ∈ queue | force (§7).

1. **Resolve the brief.** A pointer brief ("cat `<path>` and follow it") is read and the file's
   text is what gets scored; the record keeps the pointer as `task`. The spike: pointer text as
   stored drops d3 from 85% to 65%.
2. **Score.** One `system_one` call with the current definitions (§5). ~1 s on a GPU for a 6k
   character brief. Scores and per-option probabilities go in the ticket.
3. **Read facts.** Every real endpoint's live probe, as `list_models` does today, plus the
   capacity model's view (§7).
4. **Select** (§6). Result: a chosen row, or a refusal with the reason.
5. **Enqueue** on the chosen endpoint (§7). The job record is created at this point with the
   real `endpoint_id` and `model_id`, `status = queued` meaning queued in our queue, and a
   `router_tickets` row.
6. From admission on, the job is exactly what a directly named job is. **Only the initial brief
   is routed.** The headless Claude Code session's requests go through the shim straight to the
   chosen endpoint for the life of the job; no request is re-scored or re-routed, because the
   session's prefix cache lives on that server and a switch would discard it. The row is fixed at
   admission, and `resume_job` of a routed job resumes on the same row without consulting the
   router (if that row is gone, the resume refuses as it does today).

`list_models` shows the router entry with the fleet's window range in the context column (the
real window is the chosen row's and is checked at dispatch). The router entry's probe is the d3
server's `/health`; d3 down ⇒ `model_unavailable` for `auto`, with the guidance "name a model".

The ticket (`explain` returns it; `job_status` includes a summary): the dimension scores, every
row with its shortfall or the filter that removed it, the chosen row, the priority, whether it was
a divert (a free acceptable row taken over a busy better one), queue position and expected wait
at enqueue, and the time admitted.

## 5. Dimensions

Three groups. The first two share axes and are matched; the third filters.

**What the brief demands** (d3, each a `score` 0–4 unless noted; definitions are the versioned
text, drafts in the calibration spec):

| Dimension | Asks |
| --- | --- |
| software_dev | how much code it must write or change |
| language | `choice` over the shipped language list, plus `none`; the facet of software_dev; neutral when `none` or uncertain (§6) |
| tool_use | how much it must drive shell, tests, git, probes rather than read and write text |
| computer_use | whether it must operate a live system: a browser over DevTools, a node, a service |
| complexity | scope and judgement (the spike's difficulty score: d3-mini within a band 81%) |
| specification | how much is left open, from a fully specified fold to "use your judgement" |
| context_load | how much it must hold; a score, refined by the file list in the brief when present |

**What a row can do** (the scorecard; same first four axes, then behaviour):

| Dimension | Measured by |
| --- | --- |
| software_dev (per language) | probe pass rate; accepted jobs weighted by the brief's demand |
| tool_use, computer_use | probes on those axes; verdicts |
| completeness | the tail finished: tests ran, report written, no cap (gate) |
| judgement | corrects a false premise, discloses decisions, stops on a missing dependency (probes; verdict) |
| adherence | the brief's rules kept: no /tmp, no stash, pins untouched, file set as listed (gates) |
| ceiling | file count / brief size at which caps start (from capped jobs) |
| time per complexity band | observed durations, for expected wait |
| speed factor | the row's time on the probe set against the nominal timings |

**Facts about the endpoint** (read live, never scored): window, partitioned or pooled, free
slots or free pool tokens, requests running/waiting, tokens per second, backend quirks (vLLM: one
image per prompt), the wall clock cap.

The definitions are a JSON document (one entry per question: type, instruction, options with
one-line definitions), versioned in `dimension_definitions`. Every ticket and every scorecard
cell records the definitions version it was scored under. Editing goes through the `definitions`
tool, whose dry run rescores the labelled probe set and reports which probes moved. Priority is
NOT a dimension: the brief cannot know it, the caller passes it.

## 6. Selection

1. **Filter by facts.** Window ≥ the brief's context load; admission possible now or by queue
   (§7); no quirk collides with the brief (an image-reading brief never goes to a vLLM row).
2. **Shortfall.** For each surviving row, `sum over dimensions of max(0, demand − capability) ×
   weight(demand)`, weight rising with demand so a dimension the brief barely needs barely counts,
   and a row that over-delivers gains nothing (what keeps the 27B dense model from winning every
   job). **An undetermined axis is neutral.** A dimension whose d3 answer is below a confidence
   threshold (per question type, set in the calibration spec: top-option probability for a
   `choice`, mass concentrated within one band for a `score`) contributes nothing to any row's
   shortfall and is listed as `undetermined` in the ticket. Not every brief carries signal on
   every axis, and without a tailored prompt the classifier's guess on a silent axis is noise.
   Language is the clearest case: when the answer is `none` or uncertain, the row's overall
   software_dev cell is used, the count-weighted mean of its language cells and its language-free
   evidence, and no row gains or loses by language. Only a determined axis reaches a cell, and
   only then is an empty cell the unknown case above. A brief with nothing determined puts every
   surviving row on a tie, so step 4 decides and the ticket says so.
3. **Acceptable set:** rows within `tolerance` (one number in the router entry, default to be
   set by the calibration spec from the probe spread) of the best shortfall.
4. **Ties** break by predicted speed at the row's current load (§7), then by fewer queued.
5. **Priority policy** when the best row cannot admit now:

| Priority | Best busy, an acceptable row free | Nothing acceptable free |
| --- | --- | --- |
| urgent | take the free row now (divert) | head of the best row's queue |
| normal | queue for best if its expected wait < the divert threshold, else divert | queue for best |
| background | queue for best; take a lesser free row only when nothing else waits for it | queue for best |

Expected wait = the running job's remaining time (its row's time at its complexity band, minus
elapsed) plus queued jobs ahead at the same arithmetic. With no history the band's default is
used and the ticket says "default". The divert threshold is a router-entry setting (minutes).

**Never preempt.** A running job is never cancelled for a higher priority arrival.
**No acceptable row, free or busy:** refuse with `no_acceptable_row` listing each row's
shortfall; Claude may loosen `tolerance` per call or name a model.
**Uncalibrated rows are not routed.** A row without a completed core calibration (§10) is
filtered with reason `uncalibrated`; it is still usable by name. If no row survives, `auto`
refuses with `no_calibrated_row` and the guidance "run `localagents calibrate <endpoint>`". A
cell a completed core run did not fill (a language not probed) counts as demand-level shortfall:
unknown is not zero and not full. The curve of a row carries the backend-family prior until its
throughput probe has run, which the core run does first.

## 7. Capacity models and queues (every real endpoint)

Today nothing queues in the stack: `queued` is the moment between `store.new()` and the task
start, and a job aimed at a full box waits inside the server (llama.cpp slot wait, vLLM
`requests_waiting`). This section moves admission up.

**Capacity model**, by backend:

- *Partitioned* (llama.cpp): fixed slots with their own `n_ctx`. Admission = a free slot.
- *Pooled* (vLLM): one KV pool. Read live from `/metrics` (verified on the operator's vLLM
  2026-11-02): `vllm:cache_config_info{kv_cache_size_tokens, kv_cache_max_concurrency,
  block_size, num_gpu_blocks}`, `vllm:kv_cache_usage_perc`, `vllm:num_requests_running`,
  `vllm:num_requests_waiting`. Admission = the job's context load (score band → tokens, refined
  by file list) fits in the free pool tokens. The model-specific KV cost (DeepSeek's MLA vs a
  Qwen-class model) is already in those numbers; the router never estimates bytes per token.
- *Unknown* (strata, other): partitioned if slots are reported, else capacity one until probed.

**Throughput curve**: tokens/s per job at concurrency 1, 2, 3 under a fixed load, two prompt
sizes. Seeded by a backend-family prior (llama.cpp degrades on several slots; vLLM batches),
confirmed by one probe point (two concurrent, large prompt), refined by every job's observed speed
recorded with the load that ran beside it. The curve never refuses, it predicts the job's speed at
the current load; that prediction feeds expected wait and the tie-break. A row whose aggregate
halves at two is a bottleneck: its second concurrent slot is offered only to background work.

**The queue is the database.** Several Claude sessions each run a localagents server process
against the same SQLite file. A `queue_entries` row per queued job (endpoint, priority, enqueued
at, context load). Admission is one transaction: read live facts, read running and queued rows for
the endpoint, claim the capacity or stay queued. Each process wakes on its own job completions and
a timer; the claim is atomic (`BEGIN IMMEDIATE`), so two processes cannot admit into one slot.
Order: priority, then arrival. Position and expected wait are computed on read.

**`admit: force`** skips the gate for a directly named job (the operator who knows the box); the
record says so. A directly named job without it now waits in our queue with a visible reason where
it used to wait inside the server with none; same wall clock.

**Exposed** per endpoint on `list_models`, the status line and the monitor's existing endpoint
data: queued, running, free slots or free pool tokens, predicted speed at current load, curve
state (prior | probed | refined, with the point count).

## 8. The scorecard

- **Row identity:** endpoint × served id. A served id change under an endpoint is a new row; the
  old one is archived with its history. `calibrate --reset` archives by command.
- **Cell:** dimension (and language where it applies), estimate 0–4, count, sources (probes n,
  jobs n), computed at, definitions version, the pass that computed it. A cell is never a bare
  number.
- **The evidence log is primary; cells are derived.** Nothing writes a cell when a job finishes.
  Jobs write evidence: the ticket, the gate results, a verdict, a probe result. A **pass** reads
  the log for a row and recomputes every cell from it: per cell, a recency-weighted mean of the
  observations (a probe result and a job verdict are the same kind of evidence with different
  weights; weights, the recency curve and the minimum count below which a cell is "thin" are set
  in the calibration spec). The operator said why: per-job increments are a delta function over
  signals too small to carry one; a batch over accumulated evidence is what the scorecard learns
  from. A pass is initiated, never implicit: `scorecard --recompute` (§11), the end of a
  calibration run, and `localagents calibrate --recompute` from the CLI. The ticket records the
  pass id the decision used, so a decision is reproducible against the cells it saw.
- **Notices, not triggers.** The router never spends resources on its own; it tells the
  coordinating model and the operator decides. Two notices per row, shown on the router entry in
  `list_models`, in every ticket and by `scorecard`: **pass pending** when the row has N new
  observations since its last pass (N in the calibration spec, tens; a ticket then reads
  "decided on cells from pass 7, 23 observations since"); **needs recalibration** when a pass
  finds the row's traffic evidence disagreeing with its probe results beyond a margin (the
  calibration spec), or its served id changed. A pass costs seconds; a recalibration costs GPU
  hours; the notice names which it is asking for.
- **Replay.** Because cells are derived, a change to the weights replays the whole log under the
  new constants, and a definitions version change rescores the logged briefs before the pass.
- **Time per complexity band** and **speed factor** are derived by the same pass from the logged
  durations and probe timings (§5).
- **History:** every pass writes its cells as a new generation (`scorecard_events` keeps the
  previous values), so a swap reads as a before and after.

## 9. Feedback

Two feeds, one always on, one explicit; both WRITE EVIDENCE (§8) and neither touches a cell. Both
attribute to the row that did the work (a divert is recorded as such and does not touch the row
that was busy).

**Mechanical gates** run on every finished job, calibration or not, from the record and the diff,
as accept.sh does by hand today: tests ran / passed where the brief named them; stop rule honoured
(diff empty when the report says stopped); `/tmp` in the diff; `git stash list` non-empty; tail
capped (max_turns or timeout with the last step's commit missing); file count vs the brief's list;
the lint rules a model ignores. Results are logged on the job (`jobs.gates`) and are what the pass
reads for completeness, adherence and ceiling; they are shown in the job record.

**`verdict`** (C&C tool): job id; accepted | rejected; dimensions shown or lacking (a subset of the
row axes, each −1 | 0 | +1); optional category correction (the brief's true scores on any
dimension). The pass reads accepted/rejected as an observation on the demanded dimensions weighted
by demand (a clean fix says little about computer_use; an axis the ticket lists as `undetermined`
contributes nothing). A correction goes to `category_corrections` with the brief text, the
scored and the corrected values, and the definitions version: the dataset for editing definitions
(now), a nearest-neighbour correction memory (later, if corrections repeat), a fine-tune (if ever).
None of those is in this build; the log is.

## 10. Calibration: the interface this spec fixes

The set itself (which instances from which sources, how many per tier and language, the nominal
timings, the judgement and review probes, the draft definitions) is the calibration spec. What
that spec must satisfy:

- **A probe** = brief, checker (pass/fail, mechanical), nominal duration (measured on the
  reference fleet), hand-labelled dimension scores, source + pinned revision + licence note, and
  the toolchain or container it needs.
- **Tiers:** core (small; Aider Polyglot, recent LiveCodeBench, a few HumanEval as a floor, our
  judgement and review probes; needs only what Claude Code needs plus git, Python and whichever
  language toolchains are present) is what `calibrate` runs when no tier is named. Medium
  (SWE-bench Verified, Terminal-Bench; Docker) and Large (ProgramBench; Docker, linux/amd64,
  hours; the inference container's network allows the model endpoint and nothing else) are named
  explicitly. The router never starts a calibration on its own, and it never routes to a row
  whose core run is not complete (§6): calibration is a deliberate step, started from the CLI
  (`localagents calibrate <endpoint> [--tier core|medium|large] [--reset] [--estimate]`, the way
  `localagents monitor` is started) or from the `calibrate` tool. Results are used whenever they
  exist; nothing enables or disables a tier after the fact. A core run is complete when every
  probe has a result or a `skipped` reason; `unreliable` probes are rerun before it counts.
- **Distribution:** instances fetched from the source at a pinned revision at run time, not
  vendored; a source enters the set only after its licence is checked and recorded. The set is
  versioned; a row records the set version it was calibrated on.
- **Time, honestly:** before anything runs, `calibrate` reports the tier's nominal total × the
  row's speed factor (from the throughput probe, run first) with a stated margin, and the first
  few probes recheck it. The nominal timings are the reference fleet's, which is how the estimate
  carries to the installing hardware.
- **Transparent:** every probe is a job row marked `calibration` (excluded from traffic stats,
  shown in the monitor like any job); a `calibration_runs` row holds the tier, the plan, progress,
  the estimate and the actual.
- **Restartable:** a run resumes by skipping probes with a result for this row and set version;
  a probe that fails for an environment reason (toolchain absent, container pull failed) is
  `skipped` with the reason and the cell says so rather than scoring zero.
- **Reliable:** a probe measured while other load ran on the box is marked `unreliable` and
  rerun; a cap stops the tier early and marks the ceiling.
- **Regression:** the labelled probe briefs are the test set for the definitions (§5).

## 11. The router MCP surface (`localagents router`, C&C only)

| Tool | In | Out |
| --- | --- | --- |
| `calibrate` | endpoint, tier, reset?, estimate_only? | the estimate; then the run id and progress (same runner as the CLI) |
| `scorecard` | endpoint?, history?, recompute? | rows with every cell (estimate, count, sources, pass id), speed factor, curve state; `recompute` runs a pass first (§8) |
| `explain` | job id | the ticket (§4) |
| `verdict` | job id, accepted, dimensions{}, correction{} | the cells it changed |
| `definitions` | get | set(document), dry_run | the current document and version; what moved on the probe set |
| `queue` | endpoint? | queued and running per endpoint, positions, expected waits, live facts |
| `preflight` | task, priority | what dispatch would decide now, no job created |

Nothing on this surface dispatches work. Endpoint monitoring stays with the existing monitor.

## 12. Config

```yaml
endpoints:
  auto:
    backend: router
    decision_url: http://127.0.0.1:8765     # localagents d3 serve
    decision_model: vllm-sr/d3-mini         # pinned revision in the package
    tolerance: 1.0                          # optional; shortfall distance for "acceptable"
    divert_after_min: 10                    # optional; normal priority waits this long before diverting
```

`decision_url` is the only required key. The two policy knobs have shipped defaults (the
calibration spec sets `tolerance` from the probe spread). Nothing in config says which calibration
results to use: whatever exists for a row is used, and the tier is an argument to `calibrate` when
it runs (core when none is named). Real endpoints gain nothing mandatory. Optional:
`max_concurrent` to cap below what the capacity model would admit.

## 13. Tables (same database, own names)

`dimension_definitions` (version, document, created), `scorecard_rows` (endpoint_id, model_id,
created, archived, set_version), `scorecard_cells` (row, dimension, language, estimate, count,
probes_n, jobs_n, updated, defs_version), `scorecard_events`, `endpoint_capacity` (endpoint_id,
kind, curve json, state, updated), `queue_entries` (job_id, endpoint_id, priority, enqueued,
context_load, admitted), `router_tickets` (job_id, defs_version, scores json, rows json, chosen,
divert, est_wait, admitted), `verdicts`, `category_corrections`, `calibration_runs`,
`calibration_results` (run, probe id, job id, status pass|fail|skipped|unreliable, seconds). The
schema module gains a version; the monitor reads these as it reads the rest.

## 14. Failure handling

- d3 unreachable or slow (> a configured seconds): `auto` is `model_unavailable`; direct jobs
  unaffected.
- Chosen endpoint goes down while the job is queued: re-select from the remaining rows; the
  ticket records the re-route. Once running, the job fails as it does today.
- Database locked on admission: retry within the busy timeout; the job stays queued, never lost.
- Facts unreadable for a row (probe failed): the row is filtered with reason `facts_unavailable`.
- No row with a completed core calibration: `auto` refuses with `no_calibrated_row` (§6); direct
  jobs unaffected.
- Probe set source unreachable: the tier reports which probes could not be fetched and runs the
  rest; nothing is scored from a partial fetch without saying so.
- A queued job cancelled: the entry is removed, nothing was sent.

## 15. Testing

- Selection arithmetic: unit tests over fixture rows and briefs with hand-computed shortfalls,
  every priority-policy cell, tie-breaks, the empty-cell rule.
- Capacity models against captured metrics text (the 2026-11-02 dgx1-a `/metrics` capture, a
  llama.cpp `/slots` capture), per the captured-not-typed rule.
- Admission: two store handles on one file racing for one slot; exactly one admits.
- d3: a recorded-answers fixture so the suite needs no GPU; one live contract test against the
  pinned model, marked, for the operator's machine.
- Definitions dry run: the labelled probe set, a deliberate definition change, the expected moves.
- Live smoke (CONTRIBUTING material): one routed job on the operator's fleet, the ticket read
  back through `explain`, the verdict applied, the cell changed.

## 16. Out of scope (recorded so the build leaves room)

A router page in the monitor (its own story). Proxy mode. Fine-tuning or a correction memory
(§9 keeps the log). Framework probes beyond the operator's own. A one-endpoint install path.
Preemption. Per-dimension tolerance.
