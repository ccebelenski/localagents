# Epics and stories

Preliminary breakdown of `docs/superpowers/specs/2026-10-29-monitor-design.md`, cut for
incremental builds with a test gate per story. Adjust as we go. `☐` open, `☑` merged.
Each story: scope, gate (what must be green), lane hint (per `~/.claude/hints/local-delegation.md`).

Standing rule (2026-10-29): every piece of work is reviewed, read-only, by a model that did
not do it, before it merges; designs and specs included. Findings carry file:line and a
scenario; the disposition (accepted / declined with reason) is recorded.

Standing rulings (lettered; rationale in `docs/retros/`). These are guidelines, not stone
tablets: the process is reworked to fit the work as we go (operator 2026-10-29); a ruling that
fights the work in hand is raised and reworded, not obeyed literally.
- **(a)** The reviewer fixes its own verified findings, test-first, and a third model reviews
  the fix. (Epic 1 retro; operator: "that tightens the loop somewhat and all of our reviewers
  are capable builders".)
- **(d)** A change to cancellation or thread hand-off gets a cancel-mid-operation and a
  cancel-twice probe before merge. (Epic 1 retro: three of eleven defects were there.)
- **(e)** Defects found by a review are cleaned up during the build phase, as the next fold,
  not deferred to a later story; a big perturbation (a contract or schema change) is discussed
  with the operator first. (Operator 2026-10-29: "keep it clean as we go".)
- **(f)** Acceptance takes files by path; a file main moved since the lane's base is three-way
  patched; the suite runs on main before the commit, every time. (Epic 2 retro: twelve merges
  over a moved main, two cross-lane breaks caught only by the suite on main.)
- **(g)** A fold chain closes when a review finds no defect; after each pass the coordinator
  judges whether another is needed from what the pass found, never from a count. (Epic 2
  retro: every chain closed inside three, each round smaller than the last; the operator:
  "three is what you get" is arbitrary, the count is the observation, not the rule.)
- **(h)** A contract between stories is written into the spec as the shapes the code emits,
  with a test that fails when the code emits a key the spec does not name. (Epic 2 retro: §5a
  and its spec-vs-code test; the 3.1 review used it as the oracle.)
- **(i)** Two lanes writing the same module in parallel are three-way patched at acceptance,
  not serialised. (Epic 2 retro: fourteen parallel pairs, zero lost hunks.)
- **(j)** A page story's acceptance is a browser smoke driven over the DevTools protocol, DOM
  dumps and JSON only, never a screenshot read by a lane. (Epic 3 retro: seven page lanes and
  six page reviews; the one lane that read a second image died.)
- **(k)** Two page stories in parallel each own one file and register through a hook; the
  merge composes the shared files by hand. (Epic 3 retro: 3.3 and 3.4, four conflicts composed,
  both smokes held.)
- **(l)** A brief's "not provided by the contract" list is the next story's input, not a
  blocker. (Epic 3 retro: every page lane built without the missing fields; nothing faked.)
- **(m)** A fold whose whole diff the coordinator read may fold into the next review of the
  same door instead of its own review; the coordinator decides per fold, and a fold touching
  cancellation, a contract or a schema always gets its own. (Epic 3 retro: seven folds of
  under thirty lines; the combined review found two defects at a quarter of the lane time.)
Rulings (f)–(m) accepted by the operator 2026-10-29 in the closing interview; (g) and (m) reworded
at the operator's correction: a judgement per pass, not a fixed count or line limit.
Declined as rulings (practice already in the hints file): a probe cap per review brief (b);
briefs passed by path (c).

Order: Epic 1 before anything else (the monitor assumes its tables); 2.1 before any parser;
2.4 before any page story; 3.5 last.

## Epic 1: the MCP server writes SQLite

Replaces `requests.jsonl`, `task.txt` and `status/<pid>.json` with tables in
`~/.local/state/localagents/localagents.db`; adds tool-call records and exposes them to the
coordinating model. `transcript.txt` and the opt-in debug dumps stay files. The MCP server
never depends on the monitor. Spec: `docs/superpowers/specs/2026-10-29-mcp-sqlite-schema.md`
(reviewed by Qwen 2026-10-29). Cut as write path first (1.2–1.4: rows are written, in-memory
fields and the status files stay), then read path (1.5a/b: readers switch to the tables and
the duplicates go), then docs.

- ☑ **1.1 Schema module.** (8d01b41, Opus-high; reviewed by Qwen/ai2 job 20261029-033804-d6e5: clean, nits folded in f5c090e) New `db.py`: `db_path()`, `open_db()` (WAL, busy timeout,
  foreign_keys per connection, `LOCALAGENTS_DB` override), versioned `migrate()`,
  `immediate()` context manager (BEGIN IMMEDIATE), `lookup_id()` (INSERT OR IGNORE then
  SELECT), `new_uuid()`, `job_name()`. Stdlib only (the status line imports it). Gate: fresh
  file and a version-1 file both come up current; version 99 raises; four real processes
  writing concurrently lose nothing; CHECK and FK enforced. Lane: Opus-high (dispatched
  2026-10-29 night).
- ☑ **1.2 Jobs write path.** (b331747 + c603007, Qwen/ai1 job 20261029-033850-e873; reviewed by DeepSeek/dgx1-a job 20261029-034720-3071: one low defect, folded in 1535399) `Job.id` becomes a uuid4 (`job_id` of every tool, `resume_job`
  included), `Job.name` the readable stamp (directory and worktree branch `local-agent/<name>`).
  `JobStore` opens the database and inserts its `mcp_servers` row (id, pid, owner, started, cwd,
  version) on first use (not at construction: server.py builds the store at import), and `new()` inserts the job row inside `immediate()` with `endpoint_id` /
  `model_id` via `lookup_id`; `Job.changed()` upserts the mutable columns. `task.txt` no
  longer written. Prune pattern widened to `[0-9a-f]{4,}`. In-memory fields and the status
  file untouched. Gate: a job's row tracks each status transition over a real temp database;
  the `job_id` run_agent returns is the uuid; directory and branch use `name`; existing
  suite green. Lane: Qwen (ai1).
- ☑ **1.3 Requests write path.** (eb458b6, Qwen/ai2 job 20261029-034621-217d; review in the batch below) Shim `_log` inserts a `requests` row (path via `lookup_id`,
  `http_status`, ms, gen_ms, stream, four token columns, messages, tools, bytes,
  moved_system, folded_text, context_exceeded, error) and stops appending `requests.jsonl`.
  `note_usage` still updates the in-memory ctx_tokens/gen_tps for now. Gate: a captured
  `requests.jsonl` from a real job replays into rows that match it field for field (fixture
  captured by the coordinator). Lane: Qwen (ai2).
- ☑ **1.4 Tool-calls write path.** (a295ff8, Qwen/ai1 job 20261029-034639-1970; review in the batch below) Runner inserts a `tool_calls` row per `ToolUseBlock`
  (input rule: edit tools → file_path/notebook_path or NULL; others → first 120 chars),
  keeps `tool_use_id -> rowid` for pending calls in memory, and sets `finished`/`is_error`
  by rowid when the `ToolResultBlock` arrives. Gate: a replayed SDK message sequence (fixture
  built from the SDK dataclasses) yields the expected rows. Lane: DeepSeek (dgx1-a) or Qwen.
- ☑ **1.5a Status line read path.** (3025d00, Qwen/ai1 job 20261029-035222-2edd; batch review 1.3+1.4+1.5a+1535399 by DeepSeek/dgx1-a job 20261029-040715-24e0: all three constructed probes clean, three nits and seven possibles folded into 1.7) `mcp_servers` heartbeat every 10 s (`updated`), `stopped`
  at clean exit, config/transport/shim_port filled; sampler writes `endpoint_load` rows;
  `status.collect()` reads live servers + their queued/running/recent jobs + latest request
  rows + endpoint_load; `status/<pid>.json` and `publish()` removed; the `lost` rule at
  startup. Gate: `test_status.py` renders the same lines from the database; statusline still
  imports no server modules. Lane: Qwen (ai1).
- ☑ **1.5b Summary read path and exposure.** (13551ac + e753ec4, Qwen/ai1 job 20261029-040631-a263; reviewed by Opus-high: no blocking defect, two low defects and the unbounded list_jobs(all=True); the reviewer is fixing its own findings, 2026-10-29 04:30) `summary()` derives usage totals, tool call
  count, `tool_counts`, `last_tool`, files touched, ctx_tokens, gen_tps, run_cwd, log_dir from
  the tables (derivations as in the spec); the in-memory duplicates are removed from `Job`;
  `job_log(tools=N)`; `list_jobs(all=False)`; the progress line names the current tool and
  the error count; `job_status`/`job_log`/`list_jobs` read any server's job, the in-process
  tools refuse another server's with a clear error. Gate: summaries over a seeded database
  match hand-computed values; progress line pinned. Lane: DeepSeek (dgx1-a).
- ☑ **1.7 Review fold: liveness, loop hygiene, cleanup.** (9951438 + 7836d63, Qwen/ai1 job 20261029-043615-edf1; reviewed by Opus-high: two defects NEW from the to_thread change (a wait can return before finish_worktree; a cancel during make_worktree orphans a tree) and two pre-existing cancel/startup holes; the reviewer folded all thirteen items in 6005f94, 184 tests; DeepSeek review of that fold, job 20261029-051337-e3b6: one DEFECT the builder had called unreachable (a cancel in the finally strands the row running), reproduced via two concurrent cancel_job calls; folded with the 1.8 findings by the Opus lane)
- ☑ **1.8 Review fold: listing cost, metrics_delta, hints.** (4476d06, Qwen/ai1 job 20261029-051257-77b2, 99 turns; declined the NULL-ordering rewrite with an EXPLAIN; reviewed by Opus-high: two defects (backend-keyed branch broke vLLM-as-other; a partner counter missing from one snapshot raised in the finally); the reviewer folded its 8 items plus DeepSeek's 8 from the 1.7-fold review in 422deae, 221 tests; final DeepSeek review of 422deae, job 20261029-054854-a7cc, 62 turns: NO defects; 64/64 metrics_delta cases by hand; every cancel probe settled; three possibles and four nits → 1.9) From the DeepSeek review of
  47602e0 (job 20261029-043652-7d80: no blocking defect): a `jobs(created_at)` index in the
  v1 DDL (nothing deployed yet); `_row_summary` catches only expected errors and logs; NULL
  `created_at` sorts oldest; the unknown-job hint falls back to the database; docstring and
  `last_activity` follow the input rule; `metrics_delta` branches on the backend and ignores
  counters missing from one snapshot (two pre-existing bugs the review noticed); README's
  `list_jobs` limit. Gate: one test per item. Lane: Qwen (ai1), DeepSeek review. From the DeepSeek batch review and
  the Opus 1.5b review: a `†` mark for lost; `mark_lost_jobs` checks the pid like `collect`
  (a stalled server is not a dead one); `collect` LEFT JOINs the lookups; startup writes never
  kill the server on a lock; `new()` removes its directory on a failed insert; the old
  `status/` directory is removed at startup; worktree git calls run in `asyncio.to_thread`;
  subprocess tests point at tmp. Gate: one test per item. Lane: Qwen (ai1), DeepSeek review.
- ☑ **1.6 Docs and version.** (6472c6d, Qwen/ai2 job 20261029-042000-0a8e; every sentence cited; two sentences re-corrected in 1.7) README job records, status line, retention ("retention must
  exceed the longest timeout_s"), `models.example.yaml`; version bump. Gate: full suite;
  README cites nothing that no longer exists. Lane: Qwen or Haiku.

- ☑ **1.9 Consistency fold and contract tightening.** (04ced07, Qwen/ai1 job 20261029-110844-8fd14e0e, 143 turns, 235 tests; reviewed by DeepSeek/dgx1-a job 20261029-112752-b3499250, 97 turns, 31 probe tests: four summary() reads unguarded (`wait_job` 416 contradicting its own comment, `run_agent` 401, `wait_jobs` 460, `cancel_job` 605); the context-overflow rewrite records the shim's 400 not the backend's 500; cancels absorbed during worktree creation are counted nowhere; a half-failed `worktree add -b` leaves its branch; possibles: an npm-installed Claude runs as `node` so the owner walk finds nothing; the count_tokens fallback row's `ms` is 0. Folded in 1.9a.) From the final DeepSeek review:
  `database_error` on EVERY read path, not only foreign rows (in-process `job_status`,
  `list_jobs(all=False)`, `job_log`, `_foreign_job_error`, and the progress reporter's derive
  reads inside `store.wait`); `wait_jobs`'s running branch with a None `created_at`;
  `make_worktree`'s post-add cleanup checks `branch -D`; `cancel_job` says when its 15 s wait
  timed out; the `[cancel]` line counts deliveries per step (two concurrent cancels print
  nothing); `Job.summary()` returns a copy of `worktree`. The four recorded nits become
  contract rules instead of work-arounds (operator 2026-10-29: "tighten up the contracts"):
  `job_log(tail_lines=0)` is the whole transcript, like `list_jobs(limit=0)`;
  `requests.http_status` is always the backend's status (the `count_tokens` fallback row
  records 404, not 200); `mcp_servers.owner` is the pid of the nearest `claude` ancestor (the
  session), parent pid only when there is none, so a wrapper launch (`uv run`) no longer folds
  a session's own jobs into "+N other". Lane: Qwen (ai1), DeepSeek review.
- ☑ **1.9a Fold of the 1.9 review** (DeepSeek/dgx1-a job 20261029-120539-6bd5c17b, 78 turns, 263 tests; three files three-way patched over 1.11a; reviewed by Qwen/ai1 job 20261029-122217-cc1db55a, 77 turns: rules 1-4 clean through the real doors at three database moments, 413 shape, cancel-while-failing order; two defects: `run_agent`'s `store.new` guard catches `sqlite3.Error` only, so a newer-schema file raises `SchemaError` as a traceback; modern node's comm is `node-MainThread` (verified on node 26), so the npm branch of the owner walk never fires; possibles: argv-position false positive, multi-line log entries split `tail_lines`. Folded in 1.9b.) (reviewer fixes its own findings, ruling (a)): every tool's
  final `summary()` answers `database_error`; `requests.http_status` is the backend's status on
  the overflow rewrite too (500 stays 500; the spec names 0 as the no-answer sentinel); the
  count_tokens fallback records its real `ms`; cancels absorbed during worktree creation print a
  `[cancel]` line naming the step; a failed `worktree add -b` deletes the branch it created; the
  owner walk also accepts a `node` ancestor whose cmdline runs `claude`. Lane: DeepSeek, review
  by Qwen.
- ☑ **1.9b Fold of the 1.9a review** (Qwen/ai1 job 20261029-123309-2f18d755, 39 turns, 326 tests; the startup writes were guarded too; log flattening declined: assistant text is multi-line on purpose; coordinator read the 13-line source diff) (reviewer fixes its own findings): `store.new` guarded for
  `db.SchemaError` too; the owner walk's node rule matches `comm` starting with `node` and only
  argv[0]/argv[1] basenames; `Job.log_text` keeps one line per entry (embedded newlines
  flattened). Lane: Qwen (ai1).
- ☑ **1.10 `wait_job`/`wait_jobs` on any job.** (Qwen/ai1 job 20261029-123812-e82215e7, 47 turns, 371 tests; two docs three-way patched over 1.11b; open design point: `_progress_reporter_many` covers in-process jobs only, each foreign wait reports its own line; reviewed by Opus-high, four probes over a real file: D1 a cancelled `wait_jobs` orphans its `wait_many` and `_wait_foreign` tasks until `timeout_s` (a regression: pre-1.10 awaited `wait_many` directly, proven by running the same cancel on the old server.py); D2 mixed waits emit N+1 progress lines per interval on one token with a regressing progress value; D3 `_foreign_job_error`'s message is false since 1.10; possibles: the foreign "terminal = settled" rests on `runner.py:397` being the only terminal write (unstated, untested), a lost worktree job's record says nothing about its tree, a row deleted mid-wait aborts the whole call, NULL-start elapsed 0.0 vs 0, a stray `)` in README. Folded in 1.10a.)
- ☑ **1.10a Fold of the 1.10 review** (Opus-high, 90 tool uses, 400 tests; every fix mutation-checked; `wait_many` left unchanged after a mutation showed the extra await unobservable; reviewed by Qwen/ai2 job 20261029-131546-25a0d3f6, 48 turns: twenty-id cancel fan-out clean, three cancels in a row absorbed then one CancelledError, the invariant test proven non-vacuous by a planted early row write in a module copy, 80/80 in the ten-run flake loop; NO defects; noted: the finally's await is unbounded by timeout_s, the same trade _to_the_end makes. Epic 1 is closed: every story and fold reviewed by another model.) (reviewer fixes its own findings): `wait_jobs` cancels and
  awaits its tasks in a `finally`; one progress reporter over every id with `progress = done
  count, total = len(job_ids)`; the `foreign_job` message corrected; the terminal-is-settled
  invariant stated and pinned; `mark_lost_jobs` writes a worktree `note`; the deleted-row case
  documented; elapsed types aligned; README paren. Lane: Opus-high, review by DeepSeek. The waits work for any job on the machine: an
  in-process job through its settle event as today; another server's job by polling its row
  (2 s) until a terminal status or the timeout, with `mark_lost_jobs` applied every 30 s of
  the wait so a job whose server died resolves as `lost` rather than running out the clock;
  the progress line comes from the row. `foreign_job` remains only for `cancel_job` and
  `run_agent(resume_job=)`, which need the process. README table rows follow. Lane: Qwen
  (ai2) after 1.9 merges (same functions), DeepSeek review.
- ☑ **1.11 Status line: one line per job, grouped by endpoint; compaction count.** (a3ad9da, Qwen3.8-27B/llamacpp job 20261029-113848-f97e5888, 105 turns, 247 tests; `compact_boundary` confirmed in the claude binary and the SDK's bundled CLI; reviewed by Qwen/ai1 job 20261029-120222-edf9e6b2, 46 turns: one-line form byte-identical old vs new over four scenarios, migration with foreign keys clean, CLI 24 ms, NO defects; one ruling: finished rows should keep their compaction count, a trailing indicator; folded in 1.11a.) Claude
  Code renders each printed line as a row (docs: "Multiple lines: each echo or print statement
  displays as a separate row"). Shape approved by the operator 2026-10-29 ("Status line looks
  good - exposing current tool call a good idea - can we track compactions done count? That's
  a good trailing indicator of a tuning problem"): one header per endpoint with its load, one
  row per job (mark, model, elapsed, calls, files, ctx, t/s, compactions when > 0, the last
  tool call for a running job), one trailing line for other sessions' jobs; the one-line
  form stays behind `--one-line`. Schema v2: `jobs.compactions` (migration appended, spec
  updated), counted from the harness's `compact_boundary` system message, written to the
  transcript and carried by `job_status`. Lane: Qwen3.8-27B (llamacpp, local), review by a
  different model.

- ☑ **1.11a Compactions on finished rows; docstring.** (Qwen/ai1 job 20261029-120856-ce6c4f0a,
  23 turns; coordinator read the 20-line diff; third-model probe folded into the next status
  review.)

- ☑ **1.11b Status line: readable last activity.** (Qwen/ai2 job 20261029-123329-8c0fdadb, 64 turns, 364 tests; also fixed a crash on a non-dict edit-tool input; review folded into the next status-line review.) From the operator's bar (2026-10-29): a Read
  shows its JSON head, a multi-line Bash command breaks the row, absolute worktree paths eat
  the 60 characters. The input rule stores the path for any path-shaped tool; the row's
  activity is one line with paths relative to the job's root. Lane: Qwen (ai2).

## Epic 2: monitor core (collector and storage, no page)

- ☑ **2.1 Fixtures.** (a1c3693, Qwen/ai2 job 20261029-112904-212f6c31, 66 turns: 26 verbatim
  captures + 1 error row (vLLM `/health` answers 200 with no body) from Strata ai1/ai2, vLLM
  dgx1-a and llama.cpp local, two passes 2 min 19 s apart (ai2's pair carries the lane's own
  traffic; the llama.cpp pair holds a real restart, every counter backwards), the story-1.9
  job's rows exported read-only; manifest test; 236 tests. Reviewed by Qwen/ai1 job
  20261029-114358-b254dd96, 37 turns: every file byte-exact against manifest and sidecar, live
  shapes equal, db export equal field by field; NO defects; two possibles folded in 2.1a.)
  Gap list for 2.2 in the job's report: Strata `/metrics` has 9 sections, the parser reads 2
  (`engine`, `live`, `requests[]`, `conversation_cache`, `hardware*`, `history`, `totals.since`,
  `time`); no `requests_running` from Strata; vLLM 117 series, 9 mapped
  (`num_requests_waiting_by_reason`, `num_preemptions_total`, `external_prefix_cache_*`,
  `estimated_*_per_gpu`); llama.cpp `/slots` shape differs busy vs idle.
- ☑ **2.1a Manifest oracle fold.** (e1c061d, Qwen/ai1 job 20261029-114842-0a0f5114, 25 turns;
  reviewed by Qwen3.8-27B/llamacpp job 20261029-115233-c9225557, 40 turns: the entry-count pin
  is a presence oracle, a value edit of equal byte length passes every check; `_content_length`
  reads the first block, so a redirect sidecar misreports; an error row without `path` raises
  KeyError. Folded in 2.1b.)
- ☑ **2.1b Manifest values oracle** (reviewer fixes its own findings; Qwen3.8-27B/llamacpp job
  20261029-120606-0d3097a3, 48 turns, 250 tests: rows pin a sha256 digest of the parsed values,
  a same-length value edit now fails the digest while bytes and Content-Length pass;
  Content-Length from the last header block; a pathless error row is a problem string.
  Third-model probe folded into the 2.2 review.)
- ☑ **2.2 Probes table and collector.** (Opus-high, 123 tool uses, 322 tests; schema v3 per the
  approved spec block; `fetch_metrics_doc` + `parse_strata_extra`; `monitor/{rows,rates,state,
  collect,retention}.py`; rates pinned by hand on the fixture pairs (ai2 33.482 prompt/s,
  18.590 gen/s over 139 s; dgx1-a 71.813/44.381; llama.cpp pair = reset; ai1 idle = 0.0);
  events up/down/restarted; probe retention 7 d; the MCP server imports no monitor module
  (boundary test). Corrected against the brief: Strata `requests[]` are NOT stored (the spec
  rules, this line used to say otherwise); reset detection reads the raw counters, since
  `metrics_delta` has no NULL rule. Noticed: llama.cpp `requests_processing`/`requests_deferred`
  are not mapped, so llama.cpp rows carry NULL running/waiting; `parse_strata_metrics` reads a
  missing counter as 0 not NULL. Reviewed by DeepSeek/dgx1-a job 20261029-122423-55ff9294, 106 turns, 38 min: the spec block
  parsed mechanically against PRAGMA (44 columns, identical); every row value against the
  captures; the four pairs' rates equal by hand; cancel-twice mid-tick clean; retention clean.
  Defects: `kv_cache_usage` stores the prober's 3-decimal copy (0.17 for 0.1696...) while the
  document's exact value is discarded; a Strata counter missing from `totals` is stored 0.0 not
  NULL. Rulings taken by the coordinator: gauges come from the document first; an interval that
  contains a down probe has no rate (a 1000 s gap deflated 33.5 prompt/s to 4.65); a probe
  without counters has no `probe_counters` row (the spec sentence is corrected); llama.cpp
  `requests_processing`/`requests_deferred` are mapped; `rows.write` (dead in production) goes
  and tick's own transaction is pinned; retention deletes per endpoint through the existing
  index. Folded in 2.2a.)
- ☑ **2.2a Fold of the 2.2 review** (DeepSeek/dgx1-a job 20261029-130402-63783c35, 115 turns,
  every row mutation-checked; merged over 2.3a by three-way patch; the merge broke the seed
  (the llama.cpp parser's new gauge keys reached a counters insert): coordinator fix in seed.py,
  14 seed tests red then green; 420 tests. Reviewed by Qwen3.8-27B/llamacpp job 20261029-132418-acff0b90, 148 turns, 32 min: the
  three-way merge checked line by line (every 2.3a line present, none duplicated); rulings A-F
  constructed over the real documents (dgx kv exact, llama.cpp 0/0 from the document, NULL
  drafts); the gap and reset sequences over the real pair without synthesis; the
  all-or-nothing test proven by a two-transaction mutation; retention plans read. NO defects.
  Rulings taken by the coordinator: a reset under a gap IS recorded as `restarted` (detail
  "during a gap"), still no rate; the prober's `load` copy omits a series the document lacks
  so the row stays NULL; `endpoint_events` pruned per endpoint through its index; the "no
  event" docstring says "no restarted event". Folded in 2.2b.)
- ☑ **2.2b Fold of the 2.2a review** (Qwen3.8-27B/llamacpp job 20261029-135720-76dbcf09, 90 turns, 528 tests; recovered the review's probe scripts from the dangling WIP commits rather than rebuilding from prose; third-model probe folded into the 3.1a review).
- ☑ **2.3 Display and node state.** (Qwen3.8-27B/llamacpp job 20261029-122542-1e3d7290, 82
  turns, 361 tests: `endpoint_state`/`node_state`/`node_dormant` with every truth-table row its
  own test, boundary shared with `interval_s`; `probe_port` against a real listener, a closed
  port and 10.255.255.1; `monitor/jobs.py` `running_job_at`; `monitor/config.py`
  `MonitorSettings`/`load_settings`, `Registry.load` verified to ignore `monitor:`;
  `run(window_s=)`. Disclosed: settings values are not type-validated. Reviewed by Qwen/ai2 job
  20261029-124353-5712a31c, 47 turns: every reachable §4 row right against real collector rows;
  one defect: a quoted `attention_hours: "24"` passes `load_settings` and kills `collect.run`
  with a TypeError outside the tick's try; rulings taken by the coordinator: a stale up row
  older than the window is dormant, not live (the display principle is "not seen within the
  window"); `attention_hours <= 0` is a config error; a `ReadTimeout` proves the box up (the
  connect succeeded) and is classified `host: up` with a stalled sentence; the interval line
  joins the tick's guard. Folded in 2.3a.)
- ☑ **2.3a Fold of the 2.3 review** (Qwen/ai2 job 20261029-125456-d4f2fb57, 73 turns, 386 tests; bool excluded from every int setting; a failed tick now costs one dormant sleep, overturning 2.2's fast-cadence-after-failure test; third-model probe folded into the 2.2a review) (reviewer fixes its own findings): `load_settings`
  validates values (`port` int 1-65535, `attention_hours` number > 0, `retention_days` int >= 0,
  `db` str or null); `endpoint_state` live only when the last probe is within the window, spec
  §4 gains the tie-break sentence; `classify_connect_failure` answers `host: up` for a
  `ReadTimeout`; `collect.run`'s interval computation inside the per-endpoint guard;
  `probe_port` reports a reset as `refused`. Lane: Qwen (ai2).
- ☑ **2.4 `localagents seed`.** (Qwen3.8-27B/llamacpp job 20261029-124114-c6fd9b31, 109 turns,
  414 tests: 2883 probes, 1202 counters, 720 hardware, 4 events, 2 jobs, 152 requests, 192 tool
  calls in one transaction, 22 ms; deterministic in `now`; refuses a populated file without
  `--force`, which replaces. Corrected against the brief: `node_dormant(local)` is True (its
  only endpoint is dormant) while `node_state` is up, the two are orthogonal; the running
  job's rows are scaled onto its five minutes. Known limit: the seed's `mcp_servers.pid` is
  the seeding process, so `localagents statusline` over a seeded file renders nothing once
  that process exits; the monitor pages read status from rows and are the consumer. Reviewed by
  DeepSeek/dgx1-a job 20261029-132435-5516bed7, 63 turns, 22 min, twelve findings: F4
  `endpoints.first_seen` is the wall clock (via `db.lookup_id`), so the seed is not pure in
  `now`; F9 `derive.summary_from_row` computes a running job's elapsed from `time.time()`, so a
  fixed `--now` shows a negative duration; F10 the running job's `context` is 262144 against
  ai1's 131072; F1/F2/F3 the seeded events sit at instants the probes contradict and the
  `restarted` detail is not `reset_reason`'s text; F11 `metrics_delta` truncates fractional
  deltas, lossless on real integer counters but 7-17 % low on the seed's interpolated ones; F5
  `--force` deletes a live server's row and its running job's rows; F6 `--now` is parsed after
  the file is created; F7/F8 naive ISO is local time and the states decay after the window,
  both undocumented. Folded in 2.4a.)
- ☑ **2.4a Fold of the 2.4 review** (DeepSeek/dgx1-a job 20261029-134818-e7f5e90e, 138 turns,
  29 min, 554 tests; ai1 became a true hour of 721 probes so the restart splits it evenly; the
  rate pins moved to 2.586/1.436/269.3 per second from the captures; `status.collect` had no
  summary call to thread, the brief was wrong there; the merge crossed three stories, two
  conflicts composed by hand and one 3.1a fleet pin re-pinned by the coordinator. Reviewed by
  Qwen/ai1 job 20261029-142045-bab8a1a3, 92 turns: the world replayed over all 720 pairs with
  one reset byte-equal to the event, the clocks through every route and the status line, the
  float/force/argument-order rules; NO defects. Epic 2 is closed: every story and fold reviewed
  by another model.)


## Epic 3: monitor web

- ☑ **3.1 App and routes.** (Opus-high agent worktree-agent-a3b046dcb938bfd95, 79 tool uses, 514 tests; §5a is the JSON contract with
  a spec-vs-code key test; SSE verified over real HTTP in a subprocess; uvicorn's signal
  capture overridden so SIGTERM exits 0. Corrected the brief: Starlette's TestClient buffers a
  stream, so the SSE tests drive the ASGI app directly. Decisions: overview rates from the
  last two probes so a down endpoint shows no stale rate; running jobs carry `server_live`;
  history rates averaged between kept probes, null across a gap or reset; absent sources
  are absent keys; job detail's `tool_calls` is a list where the summary's is an int (flagged
  for the fold). Noticed: `collect.probe_port` is never called by `run`, the node fallback is
  dead code → 3.1b; `metrics_delta` truncates fractional deltas (seed-only effect); the seed's
  server pid again. Reviewed by Qwen/ai1 job 20261029-134412-26c2432a, 67 turns: every §5 datum tabled against
  §5a; routes recomputed by hand over the seeded world; two streams, cancel, stop and a dropped
  table mid-stream; SIGINT/SIGTERM exit 0 in under 200 ms; 7-day history 1.1 s off the loop (a
  third of it leaks through the GIL). Gaps for the pages: Strata's state strip is never stored;
  no fleet route (one history call per endpoint, each downsampled differently); no latency
  series; PCIe/CPU/RAM stored but not served as series; disk nowhere in the schema; no
  requests-by-endpoint route though the data exists. Defect: a port in use exits 3 with a raw
  traceback. The `tool_calls` int is overwritten by a list in job detail. Folded in 3.1a.)
- ☑ **3.1a Fold of the 3.1 review** (Qwen/ai1 job 20261029-135525-9a542843, 134 turns, 543 tests; ten hardware series not eight, the brief miscounted; llamacpp appears in the fleet with null series because its dormant probes are probes; totals sum the whole range, not the capped page; the port pre-check is not atomic with uvicorn's bind, documented. Reviewed by Qwen/ai2 job 20261029-141340-9d09cd28, 114 turns, 32 min, 94 constructed checks: the v3→v4 migration byte-identical on existing rows, every state rule over the captures, fleet values recomputed by hand at three grid times and equal to the endpoint route's, the 1000-row cap with whole-range totals, the port race driven to uvicorn's loud exit 3; NO defects; two brief arrows corrected (a numeric Strata state stores idle/busy, not NULL, since NULL means down; the requests range filters on each request's own t, so a finished job's tail rows appear in 15m).) (reviewer fixes its own findings): schema v4 adds
  `probes.state` (Strata's `live.state`, else idle/busy) and `probe_hardware.disk_read_mb`/
  `disk_write_mb`; history serves latency, PCIe, CPU, RAM, disk and state series; a fleet route
  on one time grid; a requests-by-endpoint route; job detail's list is `tool_call_log`; a port in
  use exits 2 with one sentence; §5a follows. Lane: Qwen (ai1), review by DeepSeek. 3.2 waits
  for it.
- ☑ **3.1b Node fallback wired.** (Qwen3.8-27B/llamacpp job 20261029-141437-0b7aa654, 94 turns, 562 tests; schema v5 `host_probes`; a re-check inside the insert's transaction; an unprobed sibling blocks the ask, conservatively; the fallback address is the first endpoint's hostname. Reviewed by Qwen/ai1 job
  20261029-143644-36a3b90b, 54 turns, 17 probe tests: the rule over every shape, cancel-twice
  mid-connect clean, the same-round race measured (one row, endpoints-per-host minus one
  connects), the freshness boundary, retention through the index, the merge intact; NO code
  defects. Ruling taken: a name that does not resolve is not "nothing" (the box may be fine,
  the name is wrong), so the ask fails loudly and the node stays unknown. Folded in 3.1c.)
- ☑ **3.1c Fold of the 3.1b review** (Qwen/ai1 job 20261029-144654-b1f6f099, 45 turns, 577 tests; the brief had assumed an existing socket import, the lane added it; coordinator read the 32-line collect.py diff; third-model probe folded into the 3.3 review) (reviewer fixes its own findings): `probe_port` lets a
  `socket.gaierror` propagate so the ask fails with one stderr line and no row; the re-check
  docstring states the duplicate-connect bound as endpoints-per-host minus one; spec §3 says
  out loud that a shared `host` key with two boxes gets one fallback aimed at the first
  endpoint's box, and that an endpoint whose ticks never write a row blocks its host's
  fallback. Lane: Qwen (ai1). `collect.run` asks `probe_port` when every endpoint of a
  host timed out in the same round and stores the answer (a `host_probes` row or a column on
  the probe), so `node_state`'s `fallback` argument has a source; §3's rule becomes code.
  Lane: Qwen, after the 3.1 review.

- ☑ **3.2 Overview page.** (Opus-high agent worktree-agent-aaf4a8a6c26f1fd77, 133 tool uses, 574 tests; browser smoke in a headless
  Chromium driven over the DevTools protocol with node, which the brief wrongly said was absent:
  every state class, one job row, three canvases, phone width clean, error banner, reconnect
  backoff measured. Corrected the brief: the snapshot's `state` is live/down/dormant, the probe
  state string lives only in the history series, so the strip costs one history call per live
  endpoint under the 5 s throttle. Noticed: `port_free` binds without SO_REUSEADDR so a restart
  within TIME-WAIT exits 2 where uvicorn would bind; the seeded running job always reads
  "server gone"; §5a gaps: the database path, a `probe_state` on the snapshot endpoint, an id
  for `dropped_job`. Reviewed by DeepSeek/dgx1-a jobs 20261029-143800-51052727 (died on a
  second screenshot: one image per prompt on that lane) and 20261029-145359-ca56f9f1
  (continuation, 78 turns): every §5 element tabled against the API, eleven constructed states
  with no null/undefined/NaN text, 120 s idle with zero polls, each range one fleet call plus
  one per live endpoint, stop and restart on the same port with the dot back without a reload,
  the wheel and the field gate; one §5 deviation: the fleet charts legend every configured
  endpoint, §5 says per live endpoint; confirmed `port_free`'s TIME-WAIT refusal for ~60 s;
  notes: a node-down card repeats each endpoint's sentence under the card note; the field gate
  cannot see bracket reads. Folded in 3.2a.)
- ☑ **3.2a Fold of the 3.2 build notes and the page reviews.** Split: 3.2a-server ☑ (Qwen/ai1
  job 20261029-153853-bcc9fa21, 123 turns, 594 tests: port_free with SO_REUSEADDR, static
  no-cache, the snapshot fields, relativised tool text, the seed's up-probe models corrected;
  review folded into the 3.3a review) and 3.2a-page ☑ (Opus-high agent worktree-agent-ac5542fc396e8ec29, 110 tool uses, 600 tests: rulings A-G built with browser evidence per row; a cold load of a malformed hash used to kill the whole page, fixed; one field walker with a named allow-list of variable-key reads). Both halves, 3.3a and 3.2b reviewed together by DeepSeek/dgx1-a job 20261029-160736-e72f7f88,
  122 turns, 32 min: every server rule by construction over a mutated copy of the seed, the
  fan-out under SIGTERM with two open streams, every page rule in the browser with hand-edited
  snapshots through render(), the gates and the record (146 emitted keys named, no dead spec
  key). Defects: `_Fanout.close()` has no caller, so a stop on a loop that outlives shutdown
  leaves the drain task pending (masked in the shipped command by asyncio.run); job.js's totals
  still count `error = ''` (the handoff 3.2b did not pick up). Record: the Opus build lanes'
  agent ids were missing from these bullets, added. Observations: `/` carries no Cache-Control;
  `port_free` answers on the first address of a multi-address host; the overview's history
  fetch is N+1 per refresh. Folded in 3.2c.
- ☑ **3.2c Fold of the combined review** (Qwen/ai2 job 20261029-164053-f1b3a874, 55 turns, 611 tests; the brief had placed the stop await in the drain loop, it is in the stream loop, the lane closed from both there and the command's stopper; subscribe after close ends at once rather than raising; Epic 3 is closed: every story and fold reviewed by another model, the live gate green): `_Fanout.close()` runs when `stop` is set; job.js
  counts `error != ''` in its totals; `/` answers `Cache-Control: no-cache`; `port_free` is
  true only when every resolved address binds. Lane: Qwen (ai2). Page items from the reviews: fleet charts for live
  endpoints only; a node-down card does not repeat each endpoint's sentence; the field gate
  sees bracket reads with a literal key. `port_free` with SO_REUSEADDR; snapshot gains
  `db_path`, `endpoint.probe_state` and `dropped_job_id`; the page uses them; the header's
  window reads `24h` not `1d00h`; the job row's current tool and last activity are relativised
  to the job's root as the status bar does (1.11b's rule, server-side: `jobs[].last_tool` and
  `last_activity` in the snapshot carry the display form); from 3.3: the overview stops
  fetching fleet and history while a detail page is open; static files get
  `Cache-Control: no-cache` (ETag revalidation); the snapshot endpoint carries `host` and the
  last-known `model`/`context` of a down endpoint; the endpoint.js field-name test walks `state.`
  chains as the shared gate now does. Lane: with the 3.2 review's findings.

- ☑ **3.3 Endpoint detail.** (Opus-high agent worktree-agent-a330c647b7d550fb9, 88 tool uses, 584 tests; one file `endpoint.js` through
  the pages hook, plus a fix to the hook itself: teardown now runs on every route change;
  browser smoke over all three states with zero console errors. Corrected the brief: no
  snapshot callback exists in app.js, so the page observes the nodes the overview rewrites;
  `state.snap` not `state.snapshot`; the sparkline helpers are not exported. §5a gaps: a down
  endpoint's last-known model and context; `dropped_job` id (3.2a); the endpoint's `host`.
  Noticed: app.js keeps fetching fleet and history on detail pages; static files carry no
  Cache-Control, so a browser can run stale page code after an upgrade; Strata's `_mb`
  fields' unit is unverified. Reviewed by Qwen/ai2 job 20261029-145809-d00ccd83, 146 turns,
  45 min: every tile against the tier table and the live JSON across five runs, fetch
  discipline measured per bucket (zero calls in 65 s idle, four per range click, none after
  teardown), the double-hash race under 300 ms emulated latency, a collection-on server with a
  never-up endpoint for the 30 s refetch gate, the 1000-row cap over a 1,352-row construct with
  totals from SQL; NO page defects. Rulings taken: the `_mb` fields are per-sample bytes whose
  window Strata does not document, so the tiles read `MB` unitless until it does; a malformed
  hash must not blank the pane. Server seams found: the stream fan-out starts draining only at
  the first subscriber, so a tick before it makes the first page load fetch twice; totals count
  `error = ''` rows the page draws as clean; the restart detail stores raw float repr. → 3.3a.)
- ☑ **3.2b Page uses the new snapshot fields** (Qwen3.8-27B/llamacpp job 20261029-160509-3acbf83e, 106 turns, 603 tests; the lane repaired a mangled edit of its own in row 4 and the coordinator read that diff; noticed: the endpoint page's strip still labels its now from the history's newest point, and two aria-labels name the range before the fetch catches up → 3.6's page notes; reviewed with 3.2a and 3.3a) (after 3.2a-server): the header shows `db_path`;
  the endpoint card's strip "now" reads `probe_state` (no history call for it); the down card
  links `dropped_job_id`; the endpoint page's header shows `host` from the endpoint object and
  `last_model`/`last_context` for a down or dormant endpoint. Lane: Qwen3.8-27B.
- ☑ **3.3a Server seams from the 3.3 review** (Qwen/ai2 job 20261029-155444-1499e5ca, 60 turns, 603 tests; the fan-out starts in make_app under a running loop with the lazy start kept as the fallback for the test client's portal loop; one existing test had pinned the old error rule and followed; job.js's client-side totals still count '' rows → 3.2b's lane told; reviewed with 3.2a and 3.2b) (after 3.2a-server): the fan-out task starts with
  the app, not the first subscriber; `endpoint_requests` totals count `error IS NOT NULL AND
  error != ''` and job detail's client-side totals agree; `rates.reset_reason` rounds the
  figures it names. Lane: Qwen (ai2).

- ☑ **3.4 Job detail.** (Qwen3.8-27B/llamacpp job 20261029-144115-e9874e7c, 137 turns, 585
  tests; one file `job.js`; browser smoke over both seeded jobs with zero console errors; a
  real race found and fixed in the browser: the on-connect snapshot could beat the detail
  fetch. Corrected the brief: files relativise by `worktree.path`, as the status bar does, not
  `root`; `metrics` keys are not named in §5a, rendered in `derive._METRICS_COLUMNS` order.
  Decisions: a second EventSource per job-page view, closed by teardown; lost when the status
  is lost or the server stopped beating. Merged beside 3.3 by four hand-composed conflicts.
  Reviewed by Qwen/ai1 job 20261029-151042-fe1a7792, 80 turns: every element against the
  API, both transcript branches, stream counts by EventSource instrumentation cross-checked
  with ss, five cold loads, the a→b race, the merge's bare `h3` measured not to restyle 3.3's
  tier headers; three cosmetic defects: the metrics order comment claims derive's order and
  is not; the context fill uses the browser clock while the marks use the server's; the
  `file://` log-directory link is dead over http. Noticed: the field-name gate skips chains
  rooted at `state.`. Folded in 3.4a.)
- ☑ **3.4a Fold of the 3.4 review** (Qwen/ai1 job 20261029-152401-00ad53ff, 79 turns, 587 tests; the clock fix measured in the browser; noted: endpoint.js's own field-name regex shares the `state.` blind spot → 3.2a; third-model probe folded into the 3.2a review) (reviewer fixes its own findings): the metrics order
  stated honestly; `contextFill` on the same clock as the marks; the log directory shown as
  selectable text with a copy affordance, no dead link; the field-name gate walks `state.`
  chains. Lane: Qwen (ai1).

- ☑ **3.5 `localagents smoke`.** (Qwen/ai1 job 20261029-130838-4304e120, 104 turns, 431 tests;
  three live runs pasted verbatim in the job's report: ai2 full green, llamacpp requests
  green, the real database once with one red line, an ai2 stall at the 60 s cap, classified
  (c) node fact; one (b) script bug found by the live run and fixed, the date match hit the
  runner's own timestamp lines. Reviewed by Qwen/ai2 job 20261029-132945-5cb371e8, 83 turns: every
  `store.new`/`run_job` kwarg tabled against `run_agent` (three deliberate pins); three
  mutations caught by the tests; one live run, 17/17 ok. Corrected the record: the 501 row every
  job carries is Claude Code's `/api/hello` startup handshake, not `count_tokens` (Strata answers
  `count_tokens` 200, llama.cpp 404; three captures agree); the shim's estimate fallback should
  still cover 405/501. Findings: the smoke's server row never heartbeats, so a job past 60 s
  disappears from the status line mid-run; `--requests 0` skips silently where `--skip-job`
  announces; job directories go to the real state dir regardless of `LOCALAGENTS_DB`. Folded in
  3.5a. The merge gates and the README monitor section come with the pages.)
- ☑ **3.5a Fold of the 3.5 review** (Qwen/ai2 job 20261029-135005-491f2d2b, 42 turns, 521 tests; third-model probe folded into the 3.6 live run) (reviewer fixes its own findings): the smoke heartbeats
  its server row while the job runs and closes it at exit; `--requests 0` prints a skip line,
  negative is an error; the shim estimates `count_tokens` on 404, 405 and 501. Lane: Qwen (ai2).
- ☑ **3.6 Merge gates.** (Qwen/ai1 job 20261029-162525-e7fcb80e, 78 turns; report in
  `docs/gates/2026-10-29-epic3-live-gate.md`: the monitor against the real `models.yaml` for
  two minutes with the real database: four live endpoints each serving the model the snapshot
  names, seven dormant with the kernel's sentence, ai9's node down with the port-22 answer
  `nothing`; 5 s / 60 s cadence exact; a rate interval recomputed by hand from ai1's own
  metrics; the smoke once, 17/17, the job seen appearing with `Bash: date` and finishing;
  every page over the real fleet with zero console errors; SIGTERM exit 0. GREEN. One (a) stale
  expectation in the brief (the status line's 120 s linger is for a still-live server's
  finished jobs, not a stopped smoke); one (c) node fact (Strata's counters advance during
  streaming, so a 5 s bin straddling a request can spike above the engine's ceiling). Live
  data the seed never showed: the monitor watching its own operator's session.)

### 3.7 Presentation pass (operator walk, 2026-10-29)

The operator's findings on the overview over the real fleet, verbatim, cards first. Each becomes
a story below; the cut is the coordinator's and the operator corrects it before a lane is briefed.

> First is the page looks very disorganized and monochromatic. There's good use of contrast for
> readability. The cards should organize to available space and adjust as you have done, but the
> flow isn't very logical. Ai9 - the node is down, but the display for that isn't good, taking an
> entirely different size, shape, and form. I would not display cards that are dormant and never
> seen, as they are just clutter right now. You have context on the card, but not context used.
> The different cards have different information available, based on the capabilities of the
> back-end - however you're moving around the positions on the card where information is
> displayed making it hard to scan from card to card for a particular field.

Coordinator's reading of the screenshot against the code (`static/app.js` `hostCard`,
`liveEndpoint`; `static/app.css` `.host`, `.figures`):
- The full-width dormant rows (`.host.dormant` spans `1 / -1`) break the card grid into bands,
  so ai1 and ai2 each sit alone on a row: that is most of "disorganized".
- ai9's shape: every endpoint dormant collapses the host to one line (`host.dormant`), and the
  node-down class only adds a red border to that line. A down node never gets a card.
- Never-seen endpoints appear inside live hosts too (local/vllm, dgx1/dgx1), not only as rows.
- Field positions move because `.figures` is a wrapping flex row and absent figures are
  filtered out, so each card packs differently.
- Context used: the probe stores the window (`context`) and KV usage; no per-endpoint
  used-tokens figure exists in the snapshot. Per-job context fill exists (`ctx_pct`, in the
  jobs table). Which the operator means is the open question for 3.7e.

- ☑ **3.7a Hide what has never been seen.** (abc0613 with 3.7b/c: lane A, Qwen/ai1 job 20261029-222739-5b2fe50a, 61 turns, 7 min, 611 tests; smoke over a config with two never-seen endpoints and a hand-edited ai9 node-down snapshot; landed by hand before tools/land.sh existed. Reviewed with 3.7d-f by DeepSeek/dgx1-a job
  20261029-230137-0ca63782, 59 turns, 19 min, four construct-it probes over hand-rendered
  snapshots: P1 eight host-state cases all matching the story text (down node same-shaped, hidden
  line in config order, hides again); P2 both backends' twelve tiles identical, the >= 80 boundary
  exact, the 60-char activity cut exact, every field read in §5a; P3 six sparkline cases as
  specified; P4 two mutations uncaught (coverage fact: the page has no JS tests, the smoke is
  the gate). DEFECTS: D1 the card row omits the `files` bit the status line emits; D2 the
  errors badge is a bare number with no title (the operator's 3.7i on the card); D3 the
  sparkline's peak and scale are computed before the range filter, so an old peak scales the
  visible line and the aria-label names a peak outside the window. POSSIBLE, judged intended:
  a never-up endpoint that is failing (`state: "down"`, `last_seen` null) on an up node is drawn
  red, not hidden; queued jobs get a row. NOTICED: a stale "queued badge" comment; the decode
  value and its sparkline come from different routes (3.7o fixes that). Folded in 3.7-fold-1.) A never-seen endpoint is not drawn; a host with
  nothing left to draw and its node not down is not drawn; one muted line under the cards says
  how many are hidden. Dormant-but-seen stays a muted line (spec §4). Spec §4's "never hidden"
  gets its True-now sentence.
- ☑ **3.7b Card flow.** (abc0613, lane A.) No full-width rows inside the card grid; config order, stable (a card
  never moves when its state changes; a down node keeps its place as a card); hidden hosts
  drop out and the grid packs. (Operator 2026-10-29: "live first" dropped for stability.)
- ☑ **3.7c A down node is a card.** (abc0613, lane A.) Same shape and size as a live host: the host name, the one
  alert sentence, its endpoints greyed (a never-seen endpoint on a down node is one muted line).
- ☑ **3.7d Fixed field positions.** (4c23d8b with 3.7e/f: lane B, Qwen/ai2 job 20261029-222749-53e44a10, 94 turns, 30 min, 1 compaction, 611 tests; the squash conflicted only on the two lanes' appended CSS blocks, composed by hand; the coordinator's DOM smoke of the merged page: 12 tiles in order, the hidden line, three dot variants, zero exceptions.) The figures and hardware rows are a grid with one slot per
  field in the same order on every card; a field the backend does not provide shows a dash in
  its slot.
- ☑ **3.7e Jobs block on the card: per-job context fill.** (4c23d8b, lane B; the lane corrected the brief: the snapshot job has a `files` count; the error badge on the row is a bare number, 3.7i applies to the card too.) The important number is each
  job's own context fill against the window the harness enforces (the shim's probed limit),
  not an endpoint-level derivation: under unified KV the slot window and the pool differ, and
  vLLM has no slots, so KV percent times ctx is nobody's context used; KV percent stays as the
  engine's pool figure in its own slot. Every live endpoint has a jobs block in the same place
  at the card's foot, zero or more rows, one per running job in the status line's row form
  (`status._job_row`: mark, name, elapsed · calls · files · ctx · t/s · compactions ·
  activity), the job name as the link in place of the model, ctx as the jobs table's bar with
  its percent; a muted "no jobs" line when empty and "busy, no local job" when the engine is
  occupied without one of ours, so the block never moves. All from the snapshot; no server
  change. (Operator: "we kind of already have the mini job display, it's in the MCP status
  line.")
- ☑ **3.7f Colour.** (4c23d8b, lane B; the lane measured the fleet series: idle arrives as 0.0, the dots were the page's drawing.) The page is monochrome; the contrast is right. Colour carries state
  (live/busy/down/dormant) and backend, consistently with the strip and the endpoint page.

The operator's second walk, the running-jobs table (2026-10-29, lanes A and B running), verbatim:

> Running jobs is very transient - and current tool and last activity always seem to be the
> same. Also I see an error count, but don't know what it is or means.

Coordinator's reading against the code (`monitor/read.py:101-135`, `runner.py:205-241`):
- The table lists only `status IN ('queued','running')` (read.py:106): a finished job vanishes
  on the next snapshot, where the status line keeps one for 120 s (`status.KEEP_FINISHED_S`).
- `last_tool` is the newest `tool_calls` row; `last_activity` is whatever the runner wrote last,
  the assistant's text (runner.py:208) or the tool call in the same "Tool: input" shape
  (runner.py:240). A local model mostly emits tool calls, so the two columns are the same
  text nearly always.
- `errors` is `SUM(is_error)` over the job's tool calls (derive.py:80-82): tool results that
  came back as errors (a failing command, a Read of a missing file). The header says only
  "errors".
- `num_turns` is written once, from the final result (runner.py:268): the "turns" column reads
  0 for every running job.

- ☐ **3.7g Recent jobs: a log, our columns.** Operator (2026-10-29, with a screenshot of Strata's
  "Recent requests"): "For the jobs list - Strata does it differently - we should compare the two
  approaches." Then: "I didn't mean 'Copy Strata Verbatim' - I like some of the data we display
  now that strata doesn't. But the log style concept I think is better." Strata's concept: a
  persistent recent-N log (10 rows, "Show all (500)"), a status pill with a word ("● Done"),
  numbers right-aligned. Ours today: live-only (a finished job vanishes on the next tick,
  read.py:106), ✓/✗ marks. Decided: the table becomes "Recent jobs": running and queued first
  (by start), then the last N finished newest first (N = 10; "show all" to the endpoint page's
  jobs section until an all-jobs page exists); a status pill with the word (running / queued /
  done / failed) in place of marks; elapsed frozen on a finished row. OUR columns stay: job,
  model, endpoint, elapsed, turns, tool calls, tool errors, context bar, speed, activity (as 3.7h-j
  settle them). Server side: the snapshot's jobs query takes finished rows too, capped; the
  page draws the pill.
- ☐ **3.7k Requests table: the log look, our columns, Strata's rates added.** The endpoint
  page's requests table (endpoint.js:296 `REQ_COLUMNS`) keeps its columns (time, job, path,
  status, ms, tokens in, out, cache, stream, error) and gains the two rates Strata derives from
  the same facts (read.py:458-466): prefill t/s ((input − cache_read) / (ms − gen_ms)) and
  decode t/s (output / gen_ms); the HTTP status as a pill with the word. VRAM hit rate is
  Strata's own and has no counterpart (not invented). Unverified until built: whether `ms` and
  `gen_ms` are both recorded for every request shape (a non-stream request may have no gen_ms).
  Operator (2026-10-29): "under our model we have more than one log list potentially - one for
  each job?" Yes, three nested logs: the overview's recent jobs (3.7g); per endpoint, its
  requests across jobs (endpoint.js:296-322, `table()` at 291; Strata's one list is this
  level, a Strata server being an endpoint); per job, its own requests (job.js:381-430, a
  second table renderer of its own, `job-requests-table`). The log look lands on both request
  tables through ONE renderer (the endpoint page's `table()` lifted to app.js's exported
  helpers, or a copy by contract if the lanes run in parallel), the same columns and pills on
  both, the job page's table without the job column.

The operator's fourth finding, the fleet charts (2026-10-29, screenshot over the real fleet with
two lanes running), verbatim:

> Fleet graphs are basically useless right now in this form - At least I can't think how I
> would use them.

Coordinator's reading: three uPlot line charts (decode tok/s, prompt tok/s, occupancy) with four
series on 5 s counter bins (`read.fleet_history`, read.py:374). Over an hour with two jobs the
decode chart is flat zero with two spikes at 600 and 1000 tok/s on engines that decode at
40–110: the live gate's known engine fact (gates/2026-10-29-epic3-live-gate.md:137-139, 317-322:
a 5 s bin straddling a completion plus an in-flight request spikes above the ceiling). Prompt
speed likewise (a 1,400 spike). Occupancy is 0/1 steps on one-slot boxes. The legends read
"llamacpp: –" with no value until hover. Three small charts side by side, mostly zero, answer no
question the operator has.

- ☐ **3.7l Fleet section: a timeline, not three rate charts.** Proposed (operator decides):
  (a) ONE fleet timeline on the shared time axis: a row per live endpoint, its busy intervals
  as bars (from the occupancy/state series it has today), each JOB drawn as a labelled span on
  its endpoint's row (`started_at`..`finished_at`|now, the name as the label, a link to the job
  page), so "who ran what, where, when, for how long" is read at a glance and a box idle all
  day is an empty row; (b) speeds from per-REQUEST rates, not counter bins: decode t/s per
  request as a mark per endpoint (the requests rows already hold output and gen_ms, 3.7k's
  derivation), which shows ai1 at ~110 and dgx1-a at ~60 and any drift, with no straddle
  spikes; prompt speed the same on the endpoint page only; (c) the occupancy chart folds into
  (a). Open: whether (b) lives on the overview at all or only on the endpoint page, leaving the
  overview one timeline. Operator 2026-10-29: "I think you're right - the single timeline is
  better." Settled: the overview's fleet section is the one timeline; speeds (per-request marks)
  go to the endpoint page.

The operator's fifth finding (2026-10-29), verbatim: "What's our refresh trigger on this page?
it seems jerky."

Coordinator's reading: the collector runs one task per endpoint, each on its own 5 s cadence
(60 s dormant), and calls `on_tick` after EACH endpoint's tick (collect.py:207-215); the app's
fan-out pushes a snapshot on every tick (app.py:9-10). With four live endpoints that is ~four
unsynchronised snapshots per 5 s plus the dormant ticks; each snapshot `render()`s the whole
overview (app.js:417-460: `replaceChildren` of every host card and the jobs table), then
`scheduleHistory` fetches at a 5 s floor and renders the same snapshot AGAIN after the charts
update (app.js:526). About five full DOM rebuilds per 5 s: text reflows, sparklines and strips
recreated, hover state lost — the jerk.

- ☐ **3.7m One redraw per tick, in place.** Server: the fan-out coalesces ticks, one snapshot
  push per ≤1 s of quiet and at most one per 5 s (a tick during the quiet period extends it
  within that cap), so the page sees one snapshot per probe round. Page: `render` skips a
  snapshot whose content is unchanged (serialise minus `now`, compare), and updates a card's
  figures in place by `data-endpoint` key (text nodes and bar widths, not `replaceChildren`),
  rebuilding a card only when its endpoint list or state changes; the post-history render only
  refreshes strips and sparklines. Evidence: the smoke counts renders per minute before/after
  (a counter on `localagentsMonitor`), and a DOM node identity check across two snapshots.
  Operator 2026-10-29: "I think the pattern of having one 'refresh master' is correct - that
  way we can adjust the refresh too if we want." Settled: the fan-out is the one refresh clock,
  separate from the probe cadence (probes keep their per-endpoint 5 s / 60 s; the clock decides
  when the page hears about them): a snapshot push every `refresh_s` (the `monitor:` section,
  default 5; the snapshot carries it so the page can show it and a later story can offer a
  control), and nothing between pushes. The first-connect snapshot stays immediate.

The operator's sixth finding (2026-10-29), verbatim: "different text in a card can change the
cards' size, causing the screen to jump a little bit. On the vllm instance for instance when the
prompt t/s isn't zero it overflows the size causing it to wrap down a line. I'll check it in the
new layout."

Coordinator's reading: in the new grid a tile is a quarter of a 360 px card (~75 px); the value
is `white-space: normal; overflow-wrap: anywhere` (app.css `.figures .fig .num`), so a value
such as "1,414.8 tok/s" or "47.7 GiB / 48.0 GiB" wraps to a second line (the merged screenshot
shows VRAM and power wrapping already) and the card grows; the grid row reflows and the page
jumps. The rule the brief lacked: a card's height never depends on its values.

- ☐ **3.7n Tiles never wrap; units in the label.** Every tile is exactly one label line and one
  value line: the value is `white-space: nowrap; overflow: hidden; text-overflow: ellipsis` with
  the full text in its `title`. Units move into the label so the value is short: "decode tok/s"
  → "1,414.8"; "VRAM GiB" → "47.7 / 48.0"; "power W" → "248 / 260"; "temp °C" → "61"; "probe ms"
  → "12"; "ctx" → "262k" (a compact form: 262,144 → "262k", 1,500 → "1.5k", under 1,000 as is;
  the full number in the title). The jobs block rows likewise never change the card's height
  by content: one line per job, the activity tail ellipsised (already), the ctx bar and its
  percent kept together (`white-space: nowrap` on that span; today they split across lines).
  The state strip and the head already hold one line. Evidence: the smoke renders the same
  card with the longest and the shortest values the snapshot can carry and reads
  `getBoundingClientRect().height` equal both times.
Seventh (coordinator, from the operator's screenshot of the landed layout over the real fleet,
2026-10-29 23:07): the decode sparklines are dotted lines on every live card. Measured on the
running monitor: `/api/fleet/history?range=15m` has a 408-point grid (the union of every
endpoint's own probe times, read.py:374-386) and dgx1-a fills 111 of them, 297 null; ai1 81 of
408; its OWN history (`/api/endpoints/dgx1-a/history?range=15m`) has 174 points and 0 nulls.
The holes are the fleet grid's construction, not missing data, and the page already fetches
each live endpoint's own history for the strip (`state.hists[name]`).

- ☐ **3.7o The sparkline reads the endpoint's own history.** `sparkline(name, now)` takes its
  `t` and `gen_tps` from `state.hists[name]` (the same object `stateStrip` reads), not from
  `state.fleet`; the fleet grid stays the fleet charts' (until 3.7l retires them). Evidence: the
  smoke over the real-shaped history counts one polyline run per contiguous stretch, no
  circles on a healthy endpoint. Fold with 3.7n.
Operator 2026-10-29, two more, verbatim: "first is we shouldn't be hammering the database hard,
so be mindful of queries and consider caching if you haven't. Second is a sparkline with no
scale isn't too useful."

Coordinator's reading of the first: nothing is cached. Every subscriber (each open tab) runs its
own `read.overview` on every tick (app.py:234-236 `snapshot_event` per stream, 249-254 per
wake), and a tick fires per endpoint probe (3.7m); `read.overview` runs its per-endpoint reads
for all 13 configured endpoints and ~6 `derive.*` queries per running job (read.py:112-137:
ctx_tokens, gen_tps, tool_call_count, error_count, last_tool, files_touched). The page then
fetches the fleet history plus one history per live endpoint every 5 s (the Epic 3 retro's open
N+1), each a downsampling read over the range. Two tabs double all of it.

- ☐ **3.7p One read per tick, shared; histories cached and batched.** Server: the fan-out
  computes the snapshot ONCE per push (3.7m's clock) and every subscriber receives the same
  bytes; the overview's per-job derivations run once per job per push. History responses are
  cached per (endpoint, range) and keyed on the endpoint's newest probe `t`, so a request between
  ticks is a dictionary hit; the fleet route the same. The page's N+1 becomes one call:
  `/api/overview/history?range=` returns the fleet grid plus each live endpoint's own history in
  one body (the page keeps `state.fleet` and `state.hists` as they are, filled from that one
  reply). Evidence: a test counts SQL statements per tick with `sqlite3.Connection.
  set_trace_callback` over the seeded world, before and after, with two subscribers; the
  numbers go in the story record. Folds into 3.7m's lane (same door: the fan-out).
- ☐ **3.7q Sparklines with a scale.** Every live card's sparkline shares ONE y scale: the
  maximum decode rate over all live endpoints' windows (floored at 1), so a tall line on one
  card and a low line on another compare by height across the row. A small muted peak label at
  the sparkline's right edge ("↑ 112" in tok/s, the shared max, the same on every card) is the
  scale; the tile's value is the current rate; the aria-label keeps the endpoint's own peak.
  Operator: "but how do I know the other axis scale?" The time axis is the page's range
  control (every sparkline and strip spans now − range .. now, app.js `rangeSeconds(state.range)`),
  which nothing on the card says. So: the sparkline's label names the window, "decode tok/s ·
  last 1h", following the control; the strip row carries the same window once per card at its
  left ("1h"), since both span it; no tick marks on a 22 px line. Fold with 3.7n/3.7o (one
  lane, the live card).
Operator 2026-10-29: "Does that green dot and label 'live' actually mean anything?" It is the
page's stream to the monitor server (app.js `setConnected`: green "live" while the EventSource
is open, red "reconnecting" when it drops; the "snapshot HH:MM:SS" beside it is the last
snapshot's time). Useful when the server dies; misread as a fleet status beside the cards.

- ☐ **3.7r The header says what it means.** The connection indicator and the snapshot time
  become one phrase: green dot + "updated 11:07:29 PM" while the stream is open (the time
  advances with each push, so staleness is visible); red dot + "connection lost, last update
  11:07:29 PM" while reconnecting; the dot's title says "stream from the monitor server". No
  word "live" in the header. Fold with the next header/page story.
Operator 2026-10-29: "How about extending that concept to the cards? In this case I mean
perhaps a last-update timestamp top-right of the card - that would give me information about
when that endpoint last ran something."

Coordinator's reading: a different fact from the header's. The snapshot has `last_seen` (the
last UP probe, read.py) and the history has a `state` series; neither says when the engine
last did work. The engine's own counters do: the newest counters row whose `generated_tokens`
advanced over the previous one (a rate row with gen_tps > 0, rates.py) is the last interval
the box generated anything, ours or not (a curl from the operator counts; a probe does not).
Our requests table is the fallback witness when counters are absent (an `other` backend).

- ☐ **3.7s Per endpoint, "last active".** Server: §5a gains `last_active` per endpoint: the
  `t` of the newest probe whose counters advanced `generated_tokens` (or `prompt_tokens_processed`)
  over the previous row; null when no such row is in the retained history; the requests
  table's newest `t` on that endpoint when the backend has no counters. One indexed query per
  endpoint per snapshot, inside 3.7p's once-per-tick read. Page: at the right of each
  endpoint's head line, muted: "active 3m ago" (`fmt.ago`, the absolute time in the title), or
  "active 10/28, 9:14 PM" past 24 h, or "no activity in 7d" when null; nothing on the host
  head (a host runs nothing). A down endpoint keeps "last seen" and gains nothing. Evidence: the
  seeded world pins the value for ai1 (its interpolated counters) and null for llamacpp.
- ☐ **3.7-fold-1 The live card fold.** One lane, page-only, the live card's functions and CSS:
  the review's D1 (the `files` bit, "N files", after calls), D2 (the errors badge reads
  "N tool errors" with the title "tool results that came back as errors; the job page lists
  them by tool", the same wording the jobs table will take in 3.7i), D3 (peak and scale over
  the in-range points only), the stale queued-badge comment; 3.7n (no tile wraps, units in the
  label, compact ctx, the ctx bar glued to its percent); 3.7o (the sparkline reads the
  endpoint's own history); 3.7q (one shared y scale across live cards, the peak label at the
  right, the window named in the label and once on the strip row). Review: DeepSeek, three
  probes (the card height invariant, the sparkline over the real-shaped history, the row form).
### 3.8 Running it (operator 2026-10-29, while the 3.7 lanes ran)

- ☑ **3.8a Browser open by default, `--no-open`, already-running opens and exits 0;
  `tools/monitor.sh start|stop|status|log`.** (88a7dbe: Haiku Agent worktree-agent-a3d878d89ce563991,
  48 tool uses, 6 min, 614 tests; operator: "a startup script and default browser open would be
  useful - the uv command isn't friendly, and having to navigate ourselves is friction"; "I think
  open is the default? --noopen is the option." Every live test and the smoke recipe pass
  `--no-open`. The coordinator ran the launcher live with a BROWSER fake and a scratch state
  dir: start → status → start again ("already running … opening", the fake got the URL twice) →
  stop, port free. Review: rides the next batch's review (coordinator read the whole diff).)

## Epic 4: operations (V2, not scheduled)

- ☐ **4.1 Guided SSH setup script.** Walks the operator through key placement per host; no
  secrets stored by localagents; verifies with one `ssh host true`.
- ☐ **4.2 Start/stop/restart recipes** per host in the `monitor:` section; actions from the
  endpoint detail page with confirmation.
- ☐ **4.3 Node telemetry** via periodic `nvidia-smi` over SSH into the tier-3 tiles for
  non-Strata backends.
