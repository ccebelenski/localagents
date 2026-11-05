# The router: design

Date: 2026-11-02. Status: approved in conversation ("It stands"); reviewed the same day by Opus (design),
Qwen (plan) and DeepSeek (schema), findings folded below; awaiting the operator's read of the fold. Builds on its own
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
  nominal timings. The package ships shapes and references only: backend-family prior curves and
  the probe set's nominal timings, no measured row, and nothing named from that fleet (the
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
entry's name and served id (the operator names it; it is matched EXACTLY, never by glob or
substring, so a real served id containing the same letters cannot capture it). New arguments,
both optional on every call: `priority` ∈ urgent | normal | background; `admit` ∈ queue | now |
force (§7). `now` is the coordinator's "run it or tell me": if the chosen row cannot admit at
once, the call returns `would_queue` with the position, the expected wait and the acceptable
alternatives instead of enqueueing, so the coordinator can wait, divert, split the brief or name
a model. The operator's rule for this surface: the coordinating model must be able to make the
right submission decision, so everything the router knows at enqueue is available before it.

1. **Resolve the brief.** A pointer brief ("cat `<path>` and follow it") is read and the file's
   text is what gets scored; the record keeps the pointer as `task`. The spike: pointer text as
   stored drops d3-flash from 85% to 65%. A missing file is `brief_unresolved`, no job created.
2. **Score.** One `system_one` call with the current definitions (§5), within `decision_timeout_s`
   (§12). A brief longer than `decision_max_tokens` is scored on its first that many tokens and
   the ticket says `truncated`. Scores, confidences and per-option probabilities go in the ticket.
3. **Read facts.** Every real endpoint's live probe, as `list_models` does today, plus the
   capacity model's view (§7), outside any transaction.
4. **Select** (§6). Result: a chosen row, or a refusal with the reason.
5. **Enqueue** on the chosen endpoint (§7). The job record is created with the real `endpoint_id`
   and `model_id`, `status = queued` meaning queued in our queue, a `queue_entries` row and a
   `router_tickets` row.
6. **Admit.** At admission the chosen endpoint is probed again: if it is down, or no longer serves
   that id, or its window changed, the job fails with `row_down` (named jobs: `model_unavailable`
   as today), the record carries the ticket, and Claude resubmits: the brief is rescored against
   what is up now. There is no automatic re-route: it would rewrite the
   record's endpoint, model, base URL and resolution under a job that has a ticket for another
   row, and a model restart reads as "down" for a minute. From admission on, the job is exactly
   what a directly named job is. **Only the initial brief is routed.** The headless Claude Code
   session's requests go through the shim straight to the chosen endpoint for the life of the
   job; no request is re-scored or re-routed, because the session's prefix cache lives on that
   server and a switch would discard it. `resume_job` of a routed job resumes on the same row and
   ignores a router id passed as `model` (the ticket is copied, marked `resumed`).

The router entry is excluded from default resolution (no `model` and no `defaults.model` never
lands on it), from `local_complete`, and from the real-endpoint logic of `probe_all` and the
monitor's collector, which show it as a router entry. `list_models` shows it with the fleet's
window range in a `context_range` field (the per-id `context` dict keeps its shape for existing
readers), the count of calibrated rows, and the notices (§8). The router entry's probe is the d3
server's `/health`; d3 down, or slower than `decision_timeout_s` ⇒ `model_unavailable` for `auto`
with the guidance "name a model, or check the d3 server".

The ticket (`explain` returns it; `job_status` includes a summary): the dimension scores with
confidences and the undetermined axes, every row with its shortfall or the filter that removed it,
the chosen row, the priority, whether it was a divert (a free acceptable row taken over a busy
better one) and when (at enqueue or at a later wake), queue position and expected wait at enqueue,
the pass id whose cells were used, the time admitted, and `truncated`/`resumed` marks.

**Consumer requirement (operator, 2026-11-04; binds Task 12).** A routed job's record names the
served model and the endpoint it ran on in the same fields a named job uses: `model` is the served
id, `endpoint` the real endpoint's name, `base_url` the real one; `auto` or the router entry's name
never stands in those fields. Two acceptance rules read them from the record: a different model
reviews than the one that built, and the model profiles decide how much to trust a report; a router
that hid the choice would blind both. The record also carries `routed_by`: the router entry's name
and the one-line reason the row was chosen (the chosen row's shortfall, or the divert and what it
diverted from), visible in `list_jobs` and `job_status` without opening the ticket, so a
retrospective can re-check the profiles against what actually ran. A named job's `routed_by` is
null.

## 5. Dimensions

Three groups. The brief's demands and the row's cells are matched axis by axis through the
table below; the endpoint's facts filter.

**What the brief demands** (d3, each a `score` 0–4 unless noted; the definitions are the
versioned text, drafts in the calibration spec):

| Dimension | Asks | Matched against |
| --- | --- | --- |
| software_dev | how much code it must write or change | the row's `software_dev` cell for the brief's language, or the aggregate (§6) |
| language | `choice` over the shipped language list plus `none`; the facet of software_dev; neutral when `none` or uncertain | selects which software_dev cell |
| tool_use | how much it must drive shell, tests, git, probes rather than read and write text | `tool_use` |
| computer_use | whether it must operate a live system: a browser over DevTools, a node, a service | `computer_use` |
| complexity | scope and judgement at the scale of the brief | `complexity` (the highest band the row has been accepted at, §8) |
| specification | how much is left open, from a fully specified fold to "use your judgement" | `judgement` |
| context_load | how much it must hold; a band, refined by the file list in the brief | the window filter only (§6); never a cell |

**What a row can do** (the scorecard, derived by the pass, §8):

| Cell | Derived from |
| --- | --- |
| software_dev (per language and aggregate), tool_use, computer_use, complexity | the highest demand band at which the row's acceptance rate clears the threshold: probe results at their labelled band, verdicts at the brief's scored band |
| judgement | the judgement probes (false premise corrected, decision disclosed, stop on a missing dependency) and verdict marks |
| completeness | the gate feed: tail finished, tests ran, no cap; a rate on 0–4 |
| adherence | the gate feed: `/tmp` absent, pins untouched, file set as listed, the lint rules; a rate on 0–4 |
| ceiling | the largest file count the row finished uncapped; a capped job lowers it |
| time per complexity band, speed factor | logged durations and probe timings |

completeness and adherence are row-level: they enter the shortfall as penalties independent of
the brief (§6). ceiling is a filter. The operator's model profiles in prose ("caps in the tail on
19+ files", "corrects false premises with evidence") are these cells.

**Facts about the endpoint** (read live, never scored): window; partitioned or pooled; capacity
in sessions (§7); our sessions running there; foreign load (requests the server reports that are
not ours); tokens per second; the wall clock cap. Per-endpoint exclusions (`exclude:` in the
entry, a list of `dimension >= n` conditions, empty by default) are a filter; the package ships
none, since a backend quirk such as "one image per prompt" is a brief-writing rule for one
deployment, not a property of a backend family.

The definitions are a JSON document (one entry per question: type, instruction, options with
one-line definitions), versioned in `dimension_definitions`. Every ticket and every cell records
the definitions version it was scored under; a version change rescores the logged briefs before
the next pass (§8). Editing goes through the `definitions` tool, whose dry run rescores the
labelled probe set and reports which probes moved. Priority is NOT a dimension: the brief cannot
know it, the caller passes it.

## 6. Selection

1. **Filter by facts.** `uncalibrated` (no complete core run, §10); `window` (the row's window is
   below the brief's context load, §7); `ceiling` (the brief names more files than the row has
   finished uncapped, when both are known); `facts_unavailable` (the endpoint's probe failed);
   `excluded` (an operator exclusion, §5).
2. **Shortfall.** For each surviving row:
   `shortfall = (Σ over determined matched axes of gap(axis) × weight(demand)) / (number of
   determined axes) + penalty(completeness) + penalty(adherence)`, where `gap = max(0, demand −
   cell)` for a cell with a count and `gap = demand` for a cell with no evidence (the operator:
   a row with nothing shown on an axis "can appear suitable in a competition with an actual
   suitable model", so unknown counts as zero and loses to any row with evidence; calibration,
   not traffic, is how it earns a number), `weight(demand) = demand / 4`, and `penalty(cell) =
   (4 − cell) / 8` (at most 0.5 each; an unknown completeness or adherence cell counts as 0). Dividing by the determined axes gives the tolerance one scale: 1.0 is about one
   band short on one axis whatever the brief determines. *(True-now, 2026-11-04, from Task 10 as built: for this formula 1.0 is one band short at demand 4 on EVERY determined axis; one band short on one axis of n determined is 1/n, since demand-0 axes still count in the divisor.)* A row that over-delivers gains nothing,
   which is what keeps the biggest model from winning every job.
   **An undetermined axis is neutral.** A dimension whose d3 answer is below the confidence
   threshold for its question type (set in the calibration spec from the probe set; the spike's
   `confidence` field and top-option probability behave very differently, so the threshold is
   measured, not guessed) contributes nothing and is listed `undetermined` in the ticket. Not
   every brief carries signal on every axis, and without a tailored prompt the classifier's guess
   on a silent axis is noise. Language is the clearest case: when `none` or uncertain, the
   aggregate software_dev cell is used and no row gains or loses by language. A brief with
   nothing determined puts every surviving row on the penalties alone, usually a tie, so step 4
   decides and the ticket says so.
3. **Acceptable set:** rows within `tolerance` of the best shortfall.
4. **Ties** break by predicted speed at the row's current load (§7), then by fewer queued.
   *(True-now, 2026-11-04, the first live routed job: the predicted speed is the curve's reading at that load DIVIDED BY the row's measured speed factor (§8, median actual over nominal seconds; a bigger factor is a slower row). The curve alone reads 1.0 for every idle row, so four idle calibrated rows tied on shortfall fell to config order, which the design never named as a rule; the operator: "the config order shouldn't ever matter because we never said it did". A row without a factor keeps the curve's reading. The ticket's reason names the level that decided: predicted speed, then queue, then row order.)*
5. **Priority policy**, applied at enqueue AND re-applied at every wake while the job is queued
   (a normal job that queued on a five-minute estimate behind a job now overrunning by an hour
   diverts when its expected wait crosses the threshold; the ticket records when):

| Priority | Best cannot admit, an acceptable row can | Nothing acceptable can admit |
| --- | --- | --- |
| urgent | take the free row now (divert) | head of the best row's queue |
| normal | queue for best while its expected wait < `divert_after_min`, divert when it crosses | queue for best |
| background | queue for best; take a lesser free row only when no higher-priority job is queued for that lesser row | queue for best |

"Can admit" is §7's sessions rule. **Expected wait** on a row = the smallest, over its session
slots, of the remaining time of the session there (its row's band time at the job's complexity
band minus elapsed, floored at a quarter of the band time so an overrunning job never reads as
nearly done) plus the band times of queued jobs ahead that would take the same slot. With no
history the band's default is used and the ticket says "default". **Bottleneck rule:** on a row
whose curve bottlenecks at n sessions, the n-th concurrent session is admitted only for
background work.

**Never preempt.** A running job is never cancelled for a higher priority arrival.
**No acceptable row, free or busy:** refuse with `no_acceptable_row` listing each row's
shortfall; Claude may pass `tolerance` on the call (a per-call override, §12) or name a model. *(True-now, 2026-11-04: tolerance is RELATIVE, rows within `tolerance` of the best shortfall, so raising it never turns a refusal into a choice; `no_acceptable_row` means every calibrated row was removed by a filter. The absolute reading was ruled out: it would contradict the "unknown cell = zero, route anyway" call.)*
**Uncalibrated rows are not routed:** filtered with reason `uncalibrated`; still usable by name.
If no row survives that filter, `auto` refuses with `no_calibrated_row` and the guidance "run
`localagents calibrate <endpoint>`". **No exploration in traffic:** selection is greedy; a row
that is never chosen is corrected only by calibration, which is the exploration. Recorded as a
known limit; exploring with background jobs is out of scope (§16).

## 7. Capacity models and queues (every real endpoint)

Today nothing queues in the stack: `queued` is the moment between `store.new()` and the task
start, and a job aimed at a full box waits inside the server (llama.cpp slot wait, vLLM
`requests_waiting`), with three sessions on two slots all making progress at request granularity
and thrashing the prefix cache, which is why the operator runs one big job per box by hand. This
section moves admission up and makes that rule a measurement.

**Capacity is counted in sessions, not requests.** A running Claude Code session holds no slot
and no KV blocks while it runs tests for ten minutes, so live slot and KV readings say "free" for
most of a job's life; and every session grows toward the full window because the runner hands it
`max_model_len`. So:

- *Partitioned* (llama.cpp): capacity = slots, each session takes one slot and gets the slot's
  window.
- *Pooled* (vLLM): capacity = `floor(kv_cache_size_tokens / window)` sessions (the operator's
  pool, 307,852 tokens for a 307,712 window, holds exactly one; the model-specific KV cost is
  already in those numbers, read from `/metrics` `cache_config_info`; the router never estimates
  bytes per token).
- *Unknown* (strata, other): capacity = slots when reported, else one session until probed.
- `max_concurrent` in the entry caps below any of these.

**Occupancy** = our admitted entries on the endpoint whose jobs are queued or running and whose
owning server's heartbeat is fresh (§7 stale rule) + foreign sessions, estimated as
`max(0, requests_running_live − our sessions)` from the probe. The brief's context load is NOT an
admission input; it serves the window filter (§6) only, so named and calibration jobs need no
score to be admitted.

**Throughput curve**: tokens/s per session at concurrency 1, 2, 3 under a fixed load, two prompt
sizes. Seeded by a backend-family prior (llama.cpp degrades on several slots; vLLM batches),
confirmed by one probe point (two concurrent, large prompt; refused while the endpoint has any
session, "run when quiet"), refined by every job's observed speed logged with the occupancy beside
it. The curve never refuses, it predicts a session's speed at the current occupancy; that
prediction feeds expected wait, the tie-break and the bottleneck rule (§6: aggregate at n below
75% of aggregate at 1, so for the prior shape llama.cpp bottlenecks at 2 and vLLM does not). The
partitioned model cannot tell a unified-KV llama.cpp (`-c` shared, `/props` reports it) from fixed
slots; the probe records which it saw.

**The queue is the database.** Several Claude sessions each run a localagents server process
against the same SQLite file. A `queue_entries` row per job (endpoint, priority rank, enqueued at,
owner server, admitted at). **The transaction holds database rows only** (the schema spec's rule:
never a probe or a network call inside a transaction, and the store's one shared connection
cannot nest): live facts are read first, then one `BEGIN IMMEDIATE` re-reads the entries,
computes occupancy, and claims or stays queued. Order: priority rank, then arrival. Position and
expected wait are computed on read. Each process wakes on its own job completions and on a
30-second poll; a wake that fails (database locked past the busy timeout, a probe error) logs,
sleeps and retries, and never ends the job's task, so a job can never be left `queued` for good
by an exception.

**Stale owners.** An entry whose owning server's heartbeat is older than the store's liveness
window (the one `mark_lost_jobs` uses) is skipped by every other process's admission and its job
is marked `lost` by the process that notices, so a closed session at the head of a queue blocks
nothing. A stalled server with a live pid is treated the same after the window; the `queue` tool
(§11) can drop an entry by hand.

**Stalled sessions.** Lack of forward progress is the signal the operator named for a stalled,
looping or dead endpoint. A running admitted session whose `last_activity` is older than
`STALL_S` (a constant; the job's own timeout still governs the job) makes the next wake probe its
endpoint and raise a notice on the job and the endpoint: `stalled: no progress for 14 min,
endpoint up` or `… endpoint down`. The router never cancels it: the session still holds its slot
on the server, and cancelling is the coordinator's call (`cancel_job`). While it stands, it counts
as occupancy, so the queue behind it reads an honest wait.

**Directly named jobs** pass the same gate. The behaviour change from today: a job whose
endpoint has no free session waits in our queue with a visible reason where it used to interleave
inside the server with none, and a third session on a two-slot box waits for a job to finish
instead of thrashing two others' caches. *(True-now, 2026-11-04, fix wave 3: the same gate includes §6's bottleneck rule, so on a two-slot box whose curve bottlenecks at the second session a named job of normal or urgent priority waits for the first to finish as well; a background job takes that slot; `admit: force` takes it regardless. This matches the operator's standing one-big-job-per-box rule and gives the decision and admission one predicate.)* `admit: now` refuses instead of waiting (`would_queue`,
§4); `admit: force` skips the wait but still writes an admitted entry, so it is counted; the
record says it forced. **Queue time counts inside the
blocking wait**: `run_agent(wait_s)` covers queue plus run, the progress line says `queued #n,
~m min`, and a wait that ends while the job is still queued returns the non-final record with
its position, as it does today for a running job.

**Consumer questions (operator, relaying another session, 2026-11-04; answers pending the operator's
call at the phase-3 go).** *(1) Where the clock starts.* Settled by the design as it stands: a
queued job has the distinct state `queued` (§4 step 5) and `timeout_s` runs from admission, because
`admit_or_wait` sits in `start_job` before `create_task` (plan Task 12) and the runner's cap wraps
only the session (`asyncio.wait_for(consume(), timeout_s)`); a brief sized to finish in 90 minutes
is never killed for waiting 40. OPEN: the blocking `wait_s` covers queue plus run (above), so the
consumer's rule `wait_s = timeout_s` no longer guarantees seeing the finish; a call that expires
while the job is queued returns the non-final record with its position and expected wait.
Proposal: both clocks start at admission — `wait_s` counts from admission too, the call keeps
blocking while queued and reports `queued #n, ~m min`, and a caller that will not wait uses
`admit: now`. The cost is an unbounded block on a deep queue, bounded by the caller's own cancel.
*(2) Who honours the box rules.* The capacity model counts SESSIONS per endpoint, caps them by
`max_concurrent`, and keeps the brief's context load out of admission (§7), so "one big-context
job per box" is encoded only when the operator sets `max_concurrent: 1` on that entry. Not encoded
at all: rules that span endpoints — two endpoints on one host (dgx1, dgx1-a, dgx1-lcpp share a
GPU), and boxes that share a UPS. Proposal: an optional `pool: <name>` on real entries; admission
counts occupancy across every endpoint in the pool and `max_concurrent` may be set on the pool
(a `pools:` block in `models.yaml`); a UPS is a pool with `max_concurrent: 1`. The router should be
the one that honours them: the hand-honoured version reads the depth at preflight and submits
later, and two sessions dispatching at once both see depth zero. The depth stays visible either
way (`preflight`, `queue`, `list_models` `sessions`/`queued`, the status line).

**Accepted (2026-11-04).** Both proposals adopted: the consumer side accepted them and the operator
relayed the acceptance; the operator may reverse either in a line before Task 11 is dispatched.
Three consumer asks come with them, binding for phase 3:
- *A job that leaves the queue without a session is `dropped`*, a new terminal status beside
  `cancelled`, `failed` and `timeout`, with `error` naming the cause: `cancelled while queued`,
  `row_down` (the admission re-probe found the row down, re-served or re-windowed; named jobs
  `model_unavailable`), or `dropped by the queue tool`. `started_at` stays null. Acceptance sorts
  every non-success into never-ran, ran-and-failed or capped from the record alone: `dropped` is
  never-ran without reading the ticket. The sentences above and in §4 that say such a job "fails
  with `row_down`" now mean this status; the spec text is kept.
- *Pool and entry configuration is the operator's.* `pool:` on an entry and the `pools:` block
  encode GPU sharing and the UPS, which neither the coordinator nor the router can learn; nothing
  infers a pool from hosts, ports or probes (the same rule as `backend:`), and a missing `pools:`
  block means no pool.
- *`routed_by` names the pool* (`routed_by.pool`) when a pool cap, not the endpoint's own capacity,
  caused the queue wait or the divert, so a retrospective can tell a capacity wait from a routing
  choice; null otherwise.

**True-now (2026-11-04, from Task 14a as built).** A job's queue entry PERSISTS after its
session ends: nothing deletes it (`queue.remove` has no caller outside the drop paths); every
reader filters on the job's status, so occupancy and order are unaffected, and the entry is the
record of `forced` and of the wait after the fact. One row per job, as `jobs` itself grows. Where
§7 above reads as if entries were removed at session end, this is what the code does.

**Coordinator question (this project's coordinator, 2026-11-04; pending the consumer's answer and the
operator's call).** The pairing rule, a different model reviews than the one that built, has no
voice in a routed dispatch: the only exclusion is the operator's per-endpoint `exclude:` (a
dimension filter, above) and `run_agent` gains priority, admit and tolerance but nothing per call.
So a review dispatch, half the jobs of a lane process, stays hand-named, or the coordinator reads
the builder's record and passes the reviewer back as a named model. Proposal: `run_agent` gains
`avoid_job: str | None`, the id of a finished job; the router filters that job's row (its served id
on any endpoint, since the rule is about the model, not the box) with reason `avoid_job` listed
in the ticket, and `routed_by.reason` names it when it bound the choice. A served-id list
(`avoid: [..]`) would do the same but copies ids by hand; the job id is the shape of the rule and
of the acceptance record. A named `model` with `avoid_job` naming that model's own row is
`bad_argument`. Also asked: `preflight` returns the chosen served id, so a brief can still be
sized to the model when a coordinator wants to (the hints' caps are per model).

**Decided (operator, 2026-11-04: "go with your best knowledge on the avoid_job answer since you're a
coordinating model also"; "we can refine it later").** `avoid_job` as proposed; the coordinator keeps
direct access to the real endpoints and names a model when it needs to. Also decided: the `pool:`
tag and the `pools:` block are accepted now and INERT until declared ("as long as it's ignored
until needed, otherwise we do self-harm"): an entry's `pool:` naming no `pools:` entry is noted on
stderr at load and has no effect, never a load or validation error. Phase 3 build method: Opus
lanes build in parallel in worktrees (the operator's quota), local models review.

**Exposed** per real endpoint on `list_models`, the status line and the monitor's endpoint data:
`sessions` (capacity, ours running, foreign), `queued` (count, head priority), predicted speed at
current occupancy, curve state (prior | probed | refined, with the point count).

## 8. The scorecard

- **Row identity:** endpoint NAME × served id (text), with the endpoint and model ids kept for
  joins. A served id change under an endpoint archives the row and opens a new one; when the
  pair returns, the archived row is revived with its history, so swapping a model back costs
  nothing. A base URL, backend or host change under the same name keeps the row (the operator's
  `backend: strata` fix must not de-route a box). `calibrate --reset` archives by command.
- **Cell:** dimension (and language where it applies), estimate 0–4, count, sources (probes n,
  jobs n), `thin` when the count is below the minimum, the pass that computed it, definitions
  version. A cell is never a bare number.
- **The evidence log is primary; cells are derived.** Nothing writes a cell when a job finishes.
  Jobs write evidence: the ticket, the gate results, a verdict, a probe result. A **pass** reads
  the log for a row and recomputes every cell from it. The operator said why: per-job increments
  are a delta function over signals too small to carry one; a batch over accumulated evidence is
  what the scorecard learns from. A pass is initiated, never implicit: `scorecard --recompute`
  (§11), the end of a calibration run, `localagents calibrate --recompute`. Every pass writes its
  cells as a new generation with its constants recorded; the ticket records the pass id the
  decision used.
- **How a pass computes a capability cell** (software_dev, tool_use, computer_use, complexity,
  judgement). Evidence is censored: "accepted at demand 3" says capability ≥ 3, not = 3, so a
  mean would pull a row toward the work it happens to get. The cell is therefore the highest
  band b such that, over observations at bands ≥ b within the recency window, the weighted
  acceptance rate ≥ `ACCEPT_RATE` and the weighted count ≥ `MIN_COUNT`; probe results are
  observations at their labelled band with `PROBE_WEIGHT`, verdicts at the brief's scored band
  with `JOB_WEIGHT`; an axis `undetermined` in the ticket contributes no observation; recency is a
  weight, not a cutoff. completeness and adherence are the weighted rate of clean gates mapped to
  0–4; ceiling is the largest uncapped file count less any cap seen since. Constants (`ACCEPT_RATE`,
  `MIN_COUNT`, weights, recency curve) belong to the calibration spec.
- **Replay.** Because cells are derived, a change to the constants replays the whole log, and a
  definitions version change rescores the logged briefs before the pass.
- **Notices, not triggers.** The router never spends resources on its own; it tells the
  coordinating model and the operator decides. Per row, shown on the router entry in
  `list_models`, in every ticket and by `scorecard`: **pass pending** when the row has N new
  observations since its last pass (N in the calibration spec, tens; a ticket then reads
  "decided on cells from pass 7, 23 observations since"); **needs recalibration** when a pass
  finds the row's traffic evidence disagreeing with its probe results beyond a margin, when its
  served id changed, or when the installed probe set's version is newer than the row's (a set
  upgrade never de-routes a fleet). A pass costs seconds; a recalibration costs GPU hours; the
  notice names which it is asking for.
- **Time per complexity band** and **speed factor** are derived by the same pass from the logged
  durations and probe timings (§5).
- **History:** generations are kept, so a swap reads as a before and after.

## 9. Feedback

Two feeds, one always on, one explicit; both WRITE EVIDENCE (§8) and neither touches a cell. Both
attribute to the row that did the work (a divert is recorded as such and does not touch the row
that was busy).

**Mechanical gates** run when a job finishes, on worktree jobs only (a non-isolated job's diff
mixes the operator's and other jobs' changes), from the record and the worktree diff, as accept.sh
does by hand today: tests ran / passed where the brief named them; stop rule honoured (diff empty
when the report says stopped); `/tmp` in the diff; tail capped (max_turns or timeout with the last
step's commit missing); file count vs the brief's list; the lint rules a model ignores. No stash
gate: `refs/stash` is repository-wide, so another lane's stash would fail every job. Results are
logged on the job (`jobs.gates`) and shown in the record; the pass reads them for completeness,
adherence and ceiling. A gate that cannot be evaluated is `null`, not false.

**`verdict`** (C&C tool): job id; accepted | rejected; dimensions shown or lacking (a subset of the
row cells, each −1 | 0 | +1); optional category correction (the brief's true scores on any
dimension). One verdict per job; a second replaces the first. A verdict on a directly named job
is accepted too: the brief is scored by d3 at verdict time so the demand bands exist, and the row
is the one that ran it. The pass reads accepted/rejected as an observation at the demanded bands
(an `undetermined` axis contributes nothing). A correction goes to `category_corrections` with the
brief text, the scored and the corrected values and the definitions version: the dataset for
editing definitions (now), a nearest-neighbour correction memory (later, if corrections repeat),
a fine-tune (if ever). None of those is in this build; the log is.

## 10. Calibration: the interface this spec fixes

The set itself (which instances from which sources, how many per tier and language, the nominal
timings, the judgement and review probes, the draft definitions, the thresholds and pass
constants) is the calibration spec. What that spec must satisfy:

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
  (`localagents calibrate <endpoint> [--tier core|medium|large] [--reset] [--estimate]
  [--recompute]`, the way `localagents monitor` is started) or from the `calibrate` tool.
- **Complete** means every planned probe has `pass`, `fail` or `skipped`, AND at least two thirds
  of them have `pass` or `fail`: a run that skipped everything because a source was unreachable
  is not complete and the row is not routable. `unreliable` probes are rerun before the run
  counts; a resumed run retries `skipped` probes as well as missing ones.
- **Distribution:** instances fetched from the source at a pinned revision at run time, not
  vendored; a source enters the set only after its licence is checked and recorded. The set is
  versioned; a row records the set version it was calibrated on; a newer installed set raises the
  "needs recalibration" notice (§8) and nothing else.
- **Time, honestly:** before anything runs, `calibrate` reports the tier's nominal total × the
  row's speed factor (from the throughput probe, run first, which needs the endpoint quiet) with
  a stated margin. After the first three probes the estimate is recomputed from their actuals and
  the run stops to ask when it exceeds twice the original unless `--keep-going` was given. The
  nominal timings are the reference fleet's, which is how the estimate carries to the installing
  hardware.
- **Transparent:** every probe is a job row with `calibration = 1`, admitted as `background`
  through the same gate as any job, shown in the monitor like any job, and excluded from the
  monitor's and the status line's traffic figures; a `calibration_runs` row holds the tier, the
  plan, progress, the estimate and the actual.
- **Restartable:** a run resumes by skipping probes with `pass` or `fail` for this row and set
  version; a probe that fails for an environment reason (toolchain absent, container pull failed,
  source unreachable) is `skipped` with the reason, retried on resume, and the cell it would have
  fed says `thin`.
- **Reliable:** a probe measured while foreign load ran on the box is `unreliable` and rerun; a
  capped probe stops the tier early and lowers the ceiling.
- **Regression:** the labelled probe briefs are the test set for the definitions (§5), and the
  calibration spec measures every axis against constant baselines before trusting it: the spike's
  complexity figure (81% within a band) is beaten by a constant answer on those labels, so that
  axis is unproven until the set measures it.
- **Ends with a pass** (§8) so the row has cells the moment it is complete.

## 11. The router MCP surface (`localagents router`, C&C only)

| Tool | In | Out |
| --- | --- | --- |
| `calibrate` | endpoint, tier, reset?, estimate_only? | the estimate; then the run id and progress (same runner as the CLI) |
| `scorecard` | endpoint?, history?, recompute? | rows with every cell (estimate, count, sources, pass id), speed factor, curve state; `recompute` runs a pass first (§8) |
| `explain` | job id | the ticket (§4) |
| `verdict` | job id, accepted, dimensions{}, correction{} | the cells it changed |
| `definitions` | get | set(document), dry_run | the current document and version; what moved on the probe set |
| `queue` | endpoint?, drop? | queued and running per endpoint, positions, expected waits, sessions and live facts; `drop(job_id)` removes a stale or stuck entry by hand (§7) |
| `preflight` | task, priority | what dispatch would decide now, no job created |

Nothing on this surface dispatches work. Endpoint monitoring stays with the existing monitor.

## 12. Config

```yaml
endpoints:
  auto:
    backend: router
    decision_url: http://127.0.0.1:8766     # localagents d3 serve (the monitor has 8765)
    decision_model: vllm-sr/d3-mini         # optional; checked against the server's /health
    decision_timeout_s: 20                  # optional; slower is model_unavailable
    decision_max_tokens: 12000              # optional; longer briefs are scored truncated (the
                                            # live d3-mini answers 500 above ~60k characters)
    tolerance: 0.5                          # optional; shortfall distance for "acceptable"
    divert_after_min: 10                    # optional; normal priority waits this long before diverting
```

`decision_url` is the only required key. The policy knobs have shipped defaults (the calibration
spec sets `tolerance` from the probe spread); `tolerance` may also be passed per call on
`run_agent`. Nothing in config says which calibration results to use: whatever exists for a row
is used, and the tier is an argument to `calibrate` when it runs (core when none is named). Real
endpoints gain nothing mandatory. Optional: `max_concurrent` to cap below what the capacity model
would admit; `exclude:` (§5).

**Upgrade order.** `Endpoint` is built from the config's keys, so a server process running the
previous package raises on `decision_url` the next time it reads the file, and that read happens
on every request through the shim. The router entry is added only after every Claude session has
reconnected to the new package; `docs/router.md` says so, and the new `Endpoint` ignores unknown
keys with a warning so the next such change does not repeat this. **A bad entry never raises
from `load`** (the phase-1 final review: a router typo would otherwise fail every running job's
requests): it is marked invalid, named once on stderr, probes as down with the problem, and is
skipped by resolution.

## 13. Tables (same database, own names)

Keying follows the schema spec: `jobs.id`-style TEXT uuids from `db.new_uuid` for runs and passes,
INTEGER surrogates with NOT NULL natural keys elsewhere, every key column NOT NULL (SQLite treats
NULLs as distinct under UNIQUE and skips the FK check on a NULL), every reference declared, CHECKs
on every status column, plain CREATE in one new step `_V10`.

`dimension_definitions` (version INTEGER PK, document TEXT NOT NULL, created REAL NOT NULL);
`scorecard_rows` (id INTEGER PK, endpoint_name TEXT NOT NULL, served_id TEXT NOT NULL,
endpoint_id REFERENCES endpoints, model_id REFERENCES models, created, archived, archive_reason,
set_version, band_times TEXT, speed_factor REAL, facts TEXT (thinking/effort facts, calibration
spec §6); a partial UNIQUE index on (endpoint_name,
served_id) WHERE archived IS NULL); `scorecard_passes` (id TEXT PK, row_id NOT NULL REFERENCES
scorecard_rows, at REAL NOT NULL, constants TEXT NOT NULL, observations INTEGER NOT NULL);
`scorecard_cells` (pass_id TEXT NOT NULL REFERENCES scorecard_passes, dimension TEXT NOT NULL,
language TEXT NOT NULL DEFAULT '', estimate REAL, count REAL NOT NULL, probes_n, jobs_n, thin
INTEGER NOT NULL, defs_version INTEGER NOT NULL, PRIMARY KEY (pass_id, dimension, language));
`endpoint_capacity` (endpoint_id INTEGER PK REFERENCES endpoints, kind TEXT NOT NULL CHECK
(partitioned|pooled|unknown), curve TEXT NOT NULL, state TEXT NOT NULL CHECK (prior|probed|refined),
points INTEGER NOT NULL, updated REAL NOT NULL); `queue_entries` (job_id TEXT PK NOT NULL REFERENCES
jobs, endpoint_id INTEGER NOT NULL REFERENCES endpoints, owner_server TEXT NOT NULL REFERENCES
mcp_servers, priority_rank INTEGER NOT NULL, enqueued REAL NOT NULL, admitted REAL, forced INTEGER
NOT NULL DEFAULT 0; index (endpoint_id, admitted, priority_rank, enqueued)); `router_tickets`
(job_id TEXT PK NOT NULL REFERENCES jobs, defs_version INTEGER NOT NULL, pass_id TEXT, scores TEXT
NOT NULL, rows TEXT NOT NULL, chosen_row INTEGER REFERENCES scorecard_rows, divert INTEGER NOT
NULL, divert_at REAL, est_wait_s REAL, admitted REAL, flags TEXT); `verdicts` (job_id TEXT PK NOT
NULL REFERENCES jobs, accepted INTEGER NOT NULL, dimensions TEXT, at REAL NOT NULL);
`category_corrections` (id INTEGER PK, job_id TEXT NOT NULL REFERENCES jobs, brief TEXT NOT NULL,
scored TEXT NOT NULL, corrected TEXT NOT NULL, defs_version INTEGER NOT NULL REFERENCES
dimension_definitions, at REAL NOT NULL); `router_tickets.pass_id` REFERENCES scorecard_passes and
every `defs_version` REFERENCES dimension_definitions (added as a v11 rebuild after the phase-1
final review; the operator's file had already reached v10);
`calibration_runs` (id TEXT PK, row_id INTEGER NOT NULL REFERENCES scorecard_rows, tier TEXT NOT
NULL, set_version TEXT NOT NULL, plan TEXT NOT NULL, estimate_s REAL, actual_s REAL, started REAL
NOT NULL, finished REAL, status TEXT NOT NULL CHECK (running|complete|incomplete|stopped));
`speed_samples` (job_id TEXT PK NOT NULL REFERENCES jobs, endpoint_id INTEGER NOT NULL REFERENCES
endpoints, sessions INTEGER NOT NULL, generate_tps REAL NOT NULL, at REAL NOT NULL: the curve's
refinement evidence, §7); `calibration_results` (run_id TEXT NOT NULL REFERENCES calibration_runs, probe_id TEXT NOT NULL,
job_id TEXT REFERENCES jobs, status TEXT NOT NULL CHECK (pass|fail|skipped|unreliable), seconds
REAL, reason TEXT, PRIMARY KEY (run_id, probe_id)); `scorecard_events` is replaced by the pass
generations. `jobs` gains `priority TEXT NOT NULL DEFAULT 'normal' CHECK (urgent|normal|background)`,
`calibration INTEGER NOT NULL DEFAULT 0`, `gates TEXT`; `JobStore.new`'s INSERT and `_row_update`'s
column lists are extended for them (they are fixed lists today, so an unlisted column is silently
never written). The query plans for the queue head and the cells-by-pass reads are pinned in
`tests/test_query_plans.py` like the existing ones. Retention: no table of per-job rows is purged
today (EPICS 1.12 is open); the router's per-job rows join that open item and are not deleted by
this build.

## 14. Failure handling

- d3 unreachable, or slower than `decision_timeout_s`: `auto` is `model_unavailable` with the
  reason (down | slow); direct jobs unaffected. A brief over `decision_max_tokens` is scored
  truncated and the ticket says so.
- Pointer brief whose file is missing: `brief_unresolved`, no job.
- Chosen endpoint down, serving another id, or a different window at admission: the job fails
  with `row_down` (named jobs `model_unavailable`), Claude re-dispatches; no automatic re-route.
- Owning server exits or stalls with a queued or admitted job: the stale-owner rule (§7) marks it
  lost and frees the endpoint; `queue` can drop an entry by hand.
- Database locked, probe error or any exception in a wake: logged, retried on the next wake, the
  job stays queued and is never abandoned by its task.
- Facts unreadable for a row (probe failed): filtered with reason `facts_unavailable`.
- No row with a complete core calibration: `no_calibrated_row` (§6); direct jobs unaffected.
- No acceptable row: `no_acceptable_row` with every row's shortfall.
- Probe set source unreachable: those probes are `skipped` with the reason, the rest run, and the
  run is complete only if two thirds have results (§10).
- A queued job cancelled: the entry is removed, nothing was sent.
- `admit: now` and the row cannot admit: `would_queue` with position, wait and alternatives; no job.
- A running session with no progress past `STALL_S`: the endpoint is probed, a notice is raised on
  the job and the endpoint, nothing is cancelled (§7).
- A process at the previous package version reading a config with the router entry: see §12.

## 15. Testing

- Selection arithmetic: unit tests over fixture rows and briefs with hand-computed shortfalls,
  every priority-policy cell including background's condition, the unknown-cell half rule, the
  normalisation, tie-breaks, the all-undetermined case, re-application at a wake.
- Capacity models against captured data (`tests/fixtures/router/`: the 2026-11-02 vLLM
  `cache_config_info` and usage series, llama.cpp `/props` and `/slots`), per the
  captured-not-typed rule: sessions capacity, foreign estimate, the pooled pool of one.
- Admission: two store handles on one file racing for one session; exactly one admits. A stale
  owner's entry is skipped and its job marked lost. A wake exception leaves the job queued and
  the task alive.
- The pass: a logged set of observations with known censoring yields the expected band; replay
  under changed constants; a definitions version change rescores before the pass.
- d3: a recorded-answers fixture so the suite needs no GPU; one live contract test against the
  pinned model, marked, for the operator's machine.
- Definitions dry run: the labelled probe set, a deliberate definition change, the expected moves.
- Calibration: complete vs incomplete (two-thirds rule), resume retries skipped, the estimate
  recheck stops at twice.
- Live smoke (CONTRIBUTING material): one routed job on the operator's fleet, the ticket read
  back through `explain`, the verdict applied, a pass run, the cell changed.

## 16. Out of scope (recorded so the build leaves room)

A router page in the monitor (its own story). Proxy mode. Fine-tuning or a correction memory
(§9 keeps the log). Framework probes beyond the operator's own. A one-endpoint install path.
Preemption. Per-dimension tolerance. Automatic re-route of a queued job. Exploration in traffic
(a background job sent to a never-chosen row). Purging the router's per-job rows (EPICS 1.12).
