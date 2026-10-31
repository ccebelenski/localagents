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
- **(n)** Operator, 2026-10-30: "You're free to argue with me on any point, at any time by the
  way - project wide. I'm not always right, and sometimes I'm even pretty badly wrong. … The two
  arguments I will never accept is 'The code is written', and 'It's difficult'. Those are factors
  to consider, not blockers." A proposal is judged on correctness, data integrity and clarity;
  effort is a cost to schedule around, never the verdict; the coordinator argues openly with
  evidence and concedes plainly.
- **(o)** 2026-10-30 (two both-sides-appended conflicts in tests/test_monitor_page.py in one
  evening): a story's new tests go in a file of their own (`tests/test_<story>.py`, e.g.
  `tests/test_3_10b.py`) unless they amend an existing test; a lane's artefacts under `t/` never
  land (tools/land.sh drops added `t/` paths from the squash; t/conftest.py stays). Both are in
  the brief template and the land script, not left to memory.

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

- ☑ **1.12 Transcripts in the database.** Built 2026-10-30, one commit per step: schema v8
  (`transcripts`, key-only PK, no index); `Job.record_transcript` (INSERT OR REPLACE, one
  `[db-error]` line on failure) and row-first `transcript_tail`; called in `run_job`'s finally
  tail — after `_finish`'s last transcript line, before `_done.set()`/the terminal `changed()`,
  so a terminal jobs row always has its transcript row; `job_log` and the monitor's
  `read._transcript` row-first, their note only when neither row nor file exists; a `lost` job
  has no row; no purge rule written. Original proposal kept below. Operator (2026-10-30, after
  the 3.9B incident deleted 19 jobs' transcript files): "should we have a BLOB table to hold the
  transcripts? That would keep it independent of the files, and under database purge rules
  instead of file purge rules. It doesn't need to be updated, just populated at job completion,
  and otherwise we have the file itself while the job is in progress for real-time reading."
  Coordinator's reading: sizes today are small (59 transcripts: median 75 KB, largest 152 KB,
  4.2 MB in all), so a `transcripts (job_id TEXT PRIMARY KEY REFERENCES jobs, text TEXT, bytes
  INTEGER, written REAL)` row per job costs nothing SQLite minds. The write is one statement in
  the runner's finish step after the last `[result]` line (the `[metrics]`/`[worktree]` tail
  included, so the row equals the file at completion); readers (`job_log`, `Job.transcript_tail`,
  the monitor's `read._transcript`) take the file while the row is absent (the job running, or
  pre-change jobs) and the row once present. Purge: the schema spec says rows are history and
  nothing deletes them; a transcripts row would be the first bulky one, so the proposal needs a
  rule ("transcripts older than N days are deleted, the job row stays", or none while sizes stay
  this small). The opt-in debug dumps (`--dump`, `log_events`) stay files. Schema v7, a spec
  edit (schema document "What stays a file"), an Epic 1 story: discussed here first per ruling
  (e), built only on the operator's word.
  Operator (2026-10-30, on the purge question): "I think database purge is still TBD. It does
  need to be done, but what shape that takes is not yet determined - also TBD is if we need some
  kind of analytics path. If all your queries are properly scoped and keyed, the size of the
  database shouldn't affect performance until it grows unreasonably large." So: the story
  writes the row with no purge rule; purge (every table, not transcripts alone) and an
  analytics path are open items of their own, not part of 1.12. Standing expectation from the
  same sentence: every query is scoped by job/endpoint/time and served by an index; an
  `EXPLAIN QUERY PLAN` sweep over every statement in `read.py`, `derive.py`, `status.py` and
  `collect.py` (a table: statement, plan, index used or SCAN) is the cheap audit that pins it,
  for a lane after the 3.9 pass.
- ☒ **1.13 Proposal, declined: linkable tool errors and requests.** Operator's verdict
  (2026-10-30): "Doesn't seem worth it - if the error 'stuck' and was terminal, the whole job
  would have failed." A tool error the model recovered from is routine; a terminal one ends the
  job with its error on the job row. The examination below stays as the record of what is and
  is not stored, for the next time the question comes up.
- ☑ **1.14 A build stamp on every server and job: stale code becomes visible.** (Landed 94fc11b: Opus Agent worktree, 126 tool uses, 18 min, 743 tests; tests/test_1_14.py (ruling (o)); `tools/smoke/stamp.js`. Deviations disclosed: `Job.summary()` touched for the equal-summaries pin; the checkout is this file's own tree first (`parents[2]` for the src layout — the brief's "parent's parent" was one level short), `direct_url.json` second; the seed stamps itself with the real stamp so the seeded page draws no note. Smoke: the seeded job page's last figure "server 0.2.0+79fd9b0"; with the seed's server row set to `0.1.0+stale00` the real snapshot drew "server code differs from the monitor's". Opus's notes: the job page's WHOLE header is muted (app.css:166 `.detail { color: var(--muted) }`, only the endpoint page gets the ink override at :169) — likely unintended, for the next page story; the note compares the server with the MONITOR's stamp, so both on the same stale code show nothing; a dirty tree carries the same sha; `make_app`, `JobStore._open` and `seed()` each run one 2 s-capped git call once; ruff is not installed, so there is no lint gate.) Operator
  (2026-10-30, on the coordinator's assessment: "The stale server process ran sixteen jobs on
  yesterday's code and nothing said so. A package version or git sha stamped into each job
  record at server start would make that visible"): "Your four calls sound useful - schedule
  them per your judgement. Opus is free if you need an agent that isn't 'in the fold' right
  now." Facts: `mcp_servers.version` exists (db.py:69) and is written at server start from
  `importlib.metadata.version("localagents")` (jobs.py:381) — which reads the installed
  dist-info, `localagents-0.1.0` for this editable install while pyproject says 0.2.0: the
  stamp is stale metadata, not the code that runs. Design: the stamp is the package version
  plus the checkout's short git sha when the install is editable (`direct_url.json` says so;
  `git -C <checkout> rev-parse --short HEAD`, best effort, never a failure), written once at
  server start; the job record (`job_status`, `list_jobs`) carries `server_version`; the
  snapshot's job row and the job page header show it ("server 0.2.0+8ce7d91"); the monitor
  stamps itself the same way and a job whose server's stamp differs from the monitor's reads
  "server code differs" (muted) on the job page — the fact that was invisible all day. No
  schema change. Lane: Opus (outside the fold), in parallel with 3.10f-1 (no migration).
  The other three calls from the same assessment: land.sh drops lane artefacts (done, with
  ruling (o)); per-story test files (ruling (o)); the record size — a correction: the brief is
  already truncated in the record; what bills is `files_touched` and the note, left alone. Operator (2026-10-30
  evening, job page of 20261030-132735-94015337): "shows 8 errors, all 'Bash' on the details.
  But then I can't go deeper into that - do we only record the error count? There's no link to
  the actual request. And actually there's no link from the request to the response or request
  prompt. I know that's in the transcript, but it would be useful to have it more linkable -
  don't do it yet, let's examine the ramifications of that, since I don't want to add much
  overhead." Examination (coordinator, from the code):
  - What is recorded today. `tool_calls` (db.py:149): t, finished, tool, tool_use_id, `input`
    (a Bash command's first 120 chars; a path for path-shaped tools; else 120 chars of JSON),
    is_error — no result text, no request. `requests` (db.py:129): one row per HTTP request
    through the shim — status, ms, tokens, message and tool counts, bytes, the backend's error
    text — no content, no tool ids. The transcript (runner.py:240-282): `[assistant] text`,
    `[tool] Bash <400 chars>`, `[result ERROR] <300 chars>`, `[backend NNN] …` — no tool_use_id,
    no request marker, so nothing in it is addressable. `requests_full.jsonl` (shim.py:280) dumps
    request bodies only when `LOCALAGENTS_DUMP_REQUESTS=1` or `job.dump`. The job page draws the
    tool-call log as timeline marks whose hover title is "time tool (error): input" (job.js:335);
    there is no tool-call table, so "8 errors, all Bash" is all the page can say.
  - Sizes. 70 transcripts on disk average 71 KB, the largest 154 KB (5 MB in all); v8 already
    stores them. A request BODY is the whole context every turn (a 70 %-full 1M-token DeepSeek
    job ≈ 2.8 MB × 124 turns ≈ 350 MB for one job): storing prompts is a non-starter, and the
    per-turn delta (one assistant message plus the tool results) IS the transcript.
  - Options, cheapest first, each a few hundred bytes per row at most:
    (a) the error text on the row: `tool_calls.result TEXT`, written by `record_tool_result`
        only when is_error, capped (2 KB); schema v9, one UPDATE. Answers "what failed" with no
        join. (b) request ↔ tool call: the shim already parses the response stream for usage
        (shim.py:352-374); `content_block_start` events name the tool_use ids a response
        produced, and the next request's body carries the matching tool_result blocks in its
        last message (the body is parsed already, shim.py:330). A `request_rowid` on
        `tool_calls`, set by the runner from a per-job in-memory id→rowid map the shim fills,
        links each call to the request that asked for it; the request that returned its result
        is the next row by (job_id, t). Same process, no new I/O. (c) an anchor in the
        transcript: write the tool_use_id into the `[tool …]` and `[result …]` lines so a page
        or `job_log` can jump to the line; zero storage, a line-format change that `job_log`
        readers and the tests pinning the format see. (d) a tool-calls table on the job page
        (error rows first: time, tool, input, duration, result text) with a jump to the
        transcript line — the page story that makes (a)–(c) visible.
  - Ramifications. Overhead is in the write path of every tool call (one UPDATE already
    happens; (a) adds a column to it, (b) a dict lookup) and in the shim's stream pass (b: one
    more event type to read in a loop that already reads two); storage is dominated by v8's
    transcripts either way. (c) changes text other sessions read (the coordinator reads
    `job_log`), benign but a contract change. Full prompt/response storage is the one thing not
    to do; the opt-in dump covers debugging. Recommendation when scheduled: (a)+(b) as one
    schema story, (c) with it, (d) as the page story after.

## Epic 2: monitor core (collector and storage, no page)

  Review: Qwen/ai1 job 20261030-223744-42be9bb2, 75 turns, 8 min, three probes with a REAL fake
  `git` on PATH: NO defect — `9.9.9+abc1234` with a `.git` dir or file, a 5 s git answers the
  version alone in 2.00 s, whitespace trimmed, an inner space rejected, the default call names
  the worktree's own HEAD; each record names ITS server's stamp, a deleted server row → None; a
  pre-1.14 row (`version='0.1.0'`) draws the figure and the note from a real snapshot; a null
  stamp draws no figure and no note (ruled right: unknown never claims staleness). Possible: P-1
  `build_stamp(foreign_root)` takes that root's sha with the package's metadata version
  (unreachable from production; a vendored layout would stamp the host repo's HEAD — cheap guard
  if wanted); P-2 the note is the same muted colour as the figure (the whole job header inherits
  `--muted`, app.css:166 — for the next page story). Chain closed.
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

- ☑ **3.7g Recent jobs: a log, our columns.** (f670097 with 3.7h/i/j: Qwen/ai2 job 20261030-000549-db188e2a, 132 turns, 31 min, 2 compactions; two queries, the jobs read 7 → 14 statements per push, the overview 25 → 32 on the seeded world; the cards' jobs block filters to live statuses; the merge broke two fold-2 tests that pinned the jobs list exactly, fixed by the coordinator to compare live ids: 640 tests. Review: with 3.7-fold-2b and 3.7k, the next DeepSeek batch.) Operator (2026-10-29, with a screenshot of Strata's
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
- ☑ **3.7h One activity column.** (f670097: "current tool" dropped; the activity cell keeps the full text in its title.)
- ☑ **3.7i The error count named.** (f670097: header "tool errors" with the title "tool results that came back as errors; the job page lists them by tool"; the badge links to the job page.)
- ☑ **3.7j Turns while running.** (f670097: the runner increments `num_turns` per AssistantMessage, the result's count still wins; a running row with 0 shows a muted dash.)
- ☑ **3.7k Requests table: the log look, our columns, Strata's rates added.** (c37322c: DeepSeek/dgx1-a job 20261030-003202-5fe7d484, 101 turns, 22 min, 637 tests on its base; corrected the brief on the data model: `tokens.input` excludes cache reads, so prefill is (input + cache_creation) / prefill time, Strata's own "prompt − cached"; hand-checked 1,214.3 and 106.3 tok/s on the seed; the job page's totals redrawn as figs; gen_ms and stream no longer shown. The squash conflicted on the two lanes' appended CSS and probe blocks, composed by the coordinator. Review: the next DeepSeek-reviews-Qwen batch cannot review DeepSeek's own work; Qwen/ai2 reviews it with fold-3.) The endpoint
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

- ☑ **3.7l Fleet section: a timeline, not three rate charts.** (075630e: Qwen/ai1 job 20261030-010210-e0334ea4, 108 turns, 15 min, 645 tests; bar positions recomputed by hand equal the styles; axis labels for all five ranges; two-lane stacking; no canvases left; `jobs` in the batched body from a one-statement `read.jobs_overlapping`. Noticed: uPlot now used by no page (the vendor drop is fold-4's); the batch still builds the fleet grid nobody reads. Review: DeepSeek, running.) Proposed (operator decides):
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
  DESIGN (coordinator, 2026-10-30, the brief's spec). The Fleet section becomes one block,
  heading "Timeline · last 1h" (the range word from the control). Rows: one per live endpoint in
  card order, each a 36 px row: a left label column (the endpoint name as `endpointLink`,
  140 px, ellipsis) and a track spanning now − range .. now. In the track: (1) the endpoint's
  busy intervals from its own history's `state` series as the state strip draws them
  (`stateStrip`'s segment walk, the same colours: busy accent, Strata's own states by their
  hashed colours, idle NOT drawn so an idle track is empty, a null a gap) as a 6 px band at the
  row's bottom; (2) each JOB that ran on that endpoint inside the window as a 20 px bar above
  the band, from `started_at` to `finished_at` (or now while running, with a 2 px accent edge at
  the right), filled in the endpoint's categorical colour at 0.25 opacity with a 1 px border in
  the full colour, the job NAME inside the bar in ink (ellipsis; hidden under ~40 px), the bar
  an `<a href="#/job/<id>">` with a title "name · started HH:MM:SS · elapsed · status"; two
  jobs overlapping on one endpoint (a two-slot box) stack as two 10 px bars. Jobs come from
  `read.jobs_in_range` (`derive.summary_from_row` rows: `id`, `name`, `endpoint`, `status`,
  `started_at`, `finished_at` verified; a job with no `started_at` is not drawn) delivered in
  the overview's one history call: the batched `/api/overview/history` body gains `jobs`
  (server row, cached like the rest, keyed on the newest job row's `created_at` and
  `finished_at`), §5a appended. The axis: one row at the bottom with 4–7 tick labels at nice
  intervals (15m: every 5 min; 1h: every 15 min; 6h: hourly; 24h: every 4 h; 7d: daily) as
  `fmt.time` (`fmt.date` past 24 h) with hairline gridlines up through the rows; a "now" marker
  at the right edge. Hover: the bars' titles; no tooltip layer. Empty: a live endpoint with
  nothing in the window keeps its row; no live endpoints → the muted sentence "no live
  endpoints". Removed: the three uPlot charts, the `charts` object, `CHARTS`,
  `#fleet-charts`/`#fleet-note` and their CSS; `state.fleet`/`fetchFleet` are no longer read by
  the overview (the fleet route stays; uPlot stays vendored until no page uses it — endpoint.js
  and job.js do not, verified by grep: a later cleanup). Tests: SMOKE_CLASSES (`timeline`,
  `tl-row`, `tl-track`, `tl-job`, `tl-axis`), VARIABLE_KEY_READS loses the `fleetEp[key]`
  entries, the §5a gate admits `jobs` on the batched body. Smoke, DOM-only: the row count equals
  the live count; each `.tl-job` bar's `left`/`width` styles recomputed by hand from the seeded
  job's `started_at` and `now` (pasted); the axis labels for each of the five ranges; zero
  exceptions; `scrollWidth == clientWidth`. Built after 3.7-fold-2 lands (the batched route
  is in that lane's file).

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

- ☑ **3.7m One redraw per tick, in place.** (4627647, server half: DeepSeek/dgx1-a job 20261029-232403-f7e2c103, 152 turns, 37 min, 627 tests; the fan-out coalesces ticks into one push per `refresh_s`, default 5, `monitor: refresh_s` in (0, 3600]; three ticks in a 0.2 s period measured as one push. The page half, in-place card updates, is still open: see 3.7-fold-2.) Server: the fan-out coalesces ticks, one snapshot
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

- ☑ **3.7n Tiles never wrap; units in the label.** Every tile is exactly one label line and one
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

- ☑ **3.7o The sparkline reads the endpoint's own history.** `sparkline(name, now)` takes its
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

- ☑ **3.7p One read per tick, shared; histories cached and batched.** (4627647; measured over the seeded world with set_trace_callback: one push 25 statements with one subscriber and 25 with two; /api/overview/history 46 cold, 2 warm; /api/snapshot inside a push 0; one read.overview 25, of which 3 are last_active. Lane's note: read.overview is still 25 statements on three endpoints, well over a hundred on the real fleet with jobs; folding the per-endpoint probe reads into one windowed query is the next reduction.) Server: the fan-out
  computes the snapshot ONCE per push (3.7m's clock) and every subscriber receives the same
  bytes; the overview's per-job derivations run once per job per push. History responses are
  cached per (endpoint, range) and keyed on the endpoint's newest probe `t`, so a request between
  ticks is a dictionary hit; the fleet route the same. The page's N+1 becomes one call:
  `/api/overview/history?range=` returns the fleet grid plus each live endpoint's own history in
  one body (the page keeps `state.fleet` and `state.hists` as they are, filled from that one
  reply). Evidence: a test counts SQL statements per tick with `sqlite3.Connection.
  set_trace_callback` over the seeded world, before and after, with two subscribers; the
  numbers go in the story record. Folds into 3.7m's lane (same door: the fan-out).
- ☑ **3.7q Sparklines with a scale.** Every live card's sparkline shares ONE y scale: the
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

- ☑ **3.7r The header says what it means.** (34e4fcd: green dot + "updated HH:MM:SS", red + "connection lost, last update HH:MM:SS", "connecting" before the first snapshot; "live" gone.) The connection indicator and the snapshot time
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

- ☑ **3.7s Per endpoint, "last active".** (server half in 4627647: `last_active` in §5a, paired with rates.rates so a reset never counts, seed pins ai1 = NOW, dgx1-a = NOW−1200, llamacpp null. Coordinator's concern: the walk is newest-first until an advancing interval, two rows for a busy box, the WHOLE retained history for an idle one, per push; 3.7-fold-2 stores the witness. The page half is open: 3.7-fold-2.) Server: §5a gains `last_active` per endpoint: the
  `t` of the newest probe whose counters advanced `generated_tokens` (or `prompt_tokens_processed`)
  over the previous row; null when no such row is in the retained history; the requests
  table's newest `t` on that endpoint when the backend has no counters. One indexed query per
  endpoint per snapshot, inside 3.7p's once-per-tick read. Page: at the right of each
  endpoint's head line, muted: "active 3m ago" (`fmt.ago`, the absolute time in the title), or
  "active 10/28, 9:14 PM" past 24 h, or "no activity in 7d" when null; nothing on the host
  head (a host runs nothing). A down endpoint keeps "last seen" and gains nothing. Evidence: the
  seeded world pins the value for ai1 (its interpolated counters) and null for llamacpp.
- ☑ **3.7-fold-1 The live card fold.** (fb47aac: Qwen/ai1 job 20261029-232230-a545b6ff, 112
  turns, 14 min, 614 tests; the height invariant measured, 243.906 px both ways after a 1 px
  line-box fix; the shared scale pinned over two hand cards; the brief corrected: the seed's
  own history has one null in 357 points. Noticed: a cdp.mjs template-literal trap (`\s` in a
  probe field needs a double backslash) and a duplicate probe key that shadows silently; the
  shared scale rides on the page's history fetch (a server-side window max would be robust:
  3.7p's lane told). Review: the coordinator read the whole diff; rides the next live-card
  review (ruling (g)). Also ticks 3.7n, 3.7o, 3.7q.) One lane, page-only, the live card's functions and CSS:
  the review's D1 (the `files` bit, "N files", after calls), D2 (the errors badge reads
  "N tool errors" with the title "tool results that came back as errors; the job page lists
  them by tool", the same wording the jobs table will take in 3.7i), D3 (peak and scale over
  the in-range points only), the stale queued-badge comment; 3.7n (no tile wraps, units in the
  label, compact ctx, the ctx bar glued to its percent); 3.7o (the sparkline reads the
  endpoint's own history); 3.7q (one shared y scale across live cards, the peak label at the
  right, the window named in the label and once on the strip row). Review: DeepSeek, three
  probes (the card height invariant, the sparkline over the real-shaped history, the row form).
Review of 4627647 by Qwen/ai1 job 20261030-000539-a0413791, 55 turns, 7 min, three
construct-it probes: P1 the clock clean under six timed races (one push at 0.312 s for three
ticks inside 0.3 s; stop mid-settle ends the stream in 1 ms; a tick at 0.29 s is in the 0.3 s
push because the read runs after the wake); P2 the caches: two spellings of one window
duplicate an entry but never serve stale; a hand-inserted probe recomputes and the next call
is 2 statements; the one-field config edit answers empty exactly as before 4627647; DEFECT D4:
with no collector the push counter never moves, so a `--no-collect` server on a database the
MCP server writes live serves its startup snapshot forever (byte-identical, 0 statements, the
new job missing); P3 DEFECT D5 measured: 7 d of idle probes (120,961 rows) make one
`read.overview` 742 ms, 745 ms of it the idle endpoint's walk to None; the active endpoint
walks 2 rows in 0.1 ms; the seeded world is 1.4 ms. POSSIBLE: `history_cache` grows with
distinct range texts. 627 passed.

- ☑ **3.7-fold-2 Server: the stored witness, the cache that expires, bounded caches.** (0091694: Qwen/ai1 job 20261030-001427-3c94d3d7, 125 turns, 15 min, 636 tests; 7 d idle world 665.9 ms → 1.0 ms at 11 statements; schema v6; self-tick as a fan-out flag; the expiring-cache test fails on the pre-fix code. Noticed: the witness is not pruned with the probes, so a non-null value can be older than retention — fold-3's wording must allow it. Follow-up 3.7-fold-2b: the v6 migration imported the monitor package for the backfill, against the decided rule; moved to the monitor's start.)
- ☑ **3.7-fold-2b db.py never imports the monitor.** (6e686bf: Qwen/ai1 job 20261030-003152-66e8f175, 52 turns, 7 min, 643 tests; a 0.0 memo for walked-nothing so the idle walk is paid once ever; an AST guard over the eight MCP-side modules.) `_v6` adds the column only; `serve()` backfills null witnesses at start; a static guard test greps the MCP-side modules for monitor imports. Lane:
  the reviewer (Qwen/ai1) fixes its own verified findings. (a) D5: the collector already pairs
  each tick's counters with the previous row (collect.py `tick`, rates.rates for the reset
  check); when the pair advanced generated or prompt tokens it writes the probe's `t` into a
  per-endpoint witness (a `last_active` column on `endpoints`, schema bump in db.py's
  migration style, backfilled once at migration from the existing rows by the current walk);
  `_last_active` reads that one value; the counterless-backend fallback stays. Evidence: the
  review's 7 d idle world (its `t/p3.py` construction, re-created) reads in the seeded world's
  time, and the statement count per push is unchanged. (b) D4: the shared snapshot cache
  expires by age as well as by push: an entry older than `refresh_s` is recomputed on demand
  (a `/api/snapshot` call or a connect), so a `--no-collect` server is at most `refresh_s`
  stale; AND, since `--no-collect` has no collector ticks at all, the fan-out self-ticks every
  `refresh_s` when serve starts it without a collector, so an open page still follows the jobs
  the MCP server writes (the smoke's hand-rendered snapshots are already overwritten by the
  next push; the lanes capture right after render). Evidence: the review's `t/p2d.py`
  construction re-created as a test, plus a stream test under `--no-collect` seeing a job
  written between pushes. (c) `history_cache` keys on the parsed window seconds, not the text.
  Spec §5a sentences appended as True-now.
- ☑ **3.7-fold-3 Page: last active on the card, in-place updates, the refresh interval.** (34e4fcd with 3.7r: Qwen/ai1 job 20261030-004205-9f906d21, 116 turns, 17 min, 643 tests; measured: a changed tile → inPlace with node identity kept, the same snapshot → skipped, a changed state → full; the card height unchanged; the squash conflicted on the export literal and the CSS tail, composed. Decisions: a history generation in the snapshot signature; "no activity recorded". Noticed: a 70-char model id wraps the head; `.ep-model` max-width+ellipsis is the next fold's. Review: Qwen built; DeepSeek reviews with the next batch.)
  After the 3.7g-j lane lands (both touch `render`). `last_active` at the right of each
  endpoint's head line as 3.7s says ("active 3m ago", the date form past 24 h, "no activity in
  7d" when null, nothing on the host head); `HISTORY_MIN_INTERVAL_MS` follows the snapshot's
  `refresh_s`; `render` skips a snapshot whose content is unchanged (serialise minus `now`), and
  updates a live card's tile values, meters, strip and sparkline in place by `data-endpoint`
  key, rebuilding a card only when its endpoint list or state changes (3.7m's page half).
  Evidence: the smoke counts renders per minute and checks a card's DOM node keeps its
  identity across two snapshots.

Review of c37322c (3.7k) by Qwen/ai2 job 20261030-005739-2306fb2e, 58 turns, 12 min: CLEAN.
Twelve hand-rendered formula cases matched by hand (prefill 914.0 = 85/0.093 s, decode 106.3 =
543/5.107 s; blanks where inputs are missing; the pills and row classes); the two pages agree
minus the job column, no client sort, the job page's seven totals equal hand sums and the 3.2c
rule holds on the seed's own `error: ""` row. POSSIBLE: `context_exceeded` is stored as TEXT (the
backend's sentence, shim.py:294) so the renderer's `=== true` never fires and §5a's new sentence
describes a value the model never emits (and `error` is always set alongside, so the cell is
right anyway); the endpoint page's time column lost `when()`'s day stamp, so at 7d the same clock
time repeats per day. NOTICED (cleanup): dead CSS `.requests tr.req-err td` (overridden) and
`.job-requests-table` (no emitter); `req-err` in no inventory and the job inventory not listing
the shared classes; a trailing blank in the "cache creation " title; sub-10 ms durations read
"0.0 s"; the two pill families' border conventions differ. 644 passed.

Review of f670097 (3.7g-j) and 6e686bf (3.7-fold-2b) by DeepSeek/dgx1-a job
20261030-005729-5199eb59, 64 turns, 20 min, four construct-it probes: P1 the log's query over
12 finished + edge rows: order right, exactly 10 finished, a cancelled-never-started row frozen
from `created_at`, `jobs_running` live-only; measured +6 statements per finished row per push
(10 finished: 92 vs 32 statements; ≈720 extra per minute at 5 s); P2 the live turn count
reads 1, 2, 3 before the result and 5 after, on the same per-message UPDATE as `last_activity`;
P3 all seven pills as words, the badge a link only when > 0, the dash on running-0, DOM order =
fed order; P4 the v5 → v6 path, the 0.0 memo written and overwritten by an advancing tick, the
second start touches no `probes`; the AST guard catches every static spelling but misses
`importlib.import_module`, `__import__` and `from localagents import monitor`. DEFECTS: none.
POSSIBLE: the finished rows' per-push derivation cost; a terminal row with NULL `finished_at`
(no write path produces one) keeps a moving elapsed; the guard's three misses; "no activity
recorded" for a box active 8 days ago with pruned probes. 644 passed.

- ☑ **3.7-fold-4s Server: frozen rows computed once; the guard complete.** (3aa1219: Qwen/ai2 job 20261030-012128-55e6ea63, 81 turns, 18 min, 649 tests; measured 86 → 26 statements on the second push with 10 finished rows; the guard catches import_module, __import__ and `from localagents import monitor`; a NULL-finished terminal row reports elapsed null. Review: rides the next DeepSeek review of Qwen work.) Lane: Qwen (the
  reviewer of DeepSeek's work reviews this). (a) `_recent_jobs`: a finished row's entry is
  immutable, so its derived figures are computed once and kept in a module-level cache keyed by
  job id, evicted when the id leaves the tail; a finished row costs 0 derive statements on the
  second push. Evidence: the review's P1 world (10 finished): the second push's statement count
  equals the live-only count plus 1 per finished row at most (the tail SELECT itself); state the
  numbers. (b) The guard in tests/test_server.py also catches `importlib.import_module(...)`
  with a string starting "localagents.monitor", `__import__(...)` likewise, and
  `from localagents import monitor` (an ImportFrom with module `localagents` and a name
  `monitor`). (c) A terminal row with NULL `finished_at` is frozen at `created_at`'s best
  witness, never moving (`elapsed_s` null when neither start nor finish is known).
- ☑ **3.7-fold-4 Page cleanups.** (a2f9a1c: Qwen/ai1 job 20261030-012138-3eebd3e3, 111 turns, 12 min, 643 tests; uPlot dropped from the package with its tests; the long model id held on one head line by a flex basis rule the brief lacked, measured; the request time dated past 24 h, ms durations, one pill convention, dead CSS gone, inventories complete. Noticed: README's monitor section describes the retired charts and the old header → 3.7-docs.) One lane after the DeepSeek review of f670097/6e686bf lands
  its findings too: `context_exceeded` read as truthy (a non-empty string), §5a's sentence
  corrected to the stored type; the endpoint page's request time with the day stamp past 24 h
  (`fmt.date`) as the jobs table does; the two dead CSS rules removed and the inventories made
  complete (`req-err`, the shared classes on the job page's list); the title's trailing blank;
  durations under 1 s in ms ("8 ms"); one pill border convention; `.ep-model` max-width +
  ellipsis (fold-3's notice: a 70-char model id wraps the head); plus whatever the DeepSeek
  review files.

Coordinator's read of the merged page's screenshot (2026-10-30 01:20, the seeded world at 1400 px),
for the operator's morning and the next polish fold:
- The decode tile's label "decode tok/s · last 1h" clips to "decode tok/s · l…" at a quarter-card
  width; the strip row's "1h" already names the window once per card, so the tile label can go
  back to "decode tok/s" (3.7q's two places become one).
- The card's job row is one nowrap line, so at card width it reads "⟳ name 5m05s · 50 calls ·
  2 files · …" and the ctx bar, speed, errors and activity are all cut, i.e. the figures the row
  exists for. Two FIXED lines per job keep the no-jump rule: line 1 the figures (name, elapsed,
  calls, files, ctx bar + %, t/s, errors), line 2 the activity, muted, ellipsised.
- The timeline's axis labels carry seconds ("12:30:00 AM"): ticks want "12:30 AM" (a tick
  format without seconds; the bars' titles keep seconds).
- The timeline's rows and bars read well; the "↑ 1.4" peak label sits on the line's right end.

- ☑ **3.7-docs README's Monitor section to the 3.7 page.** (Qwen/ai1 job 20261030-013510-ee7c67cb, 41 turns, 6 min; True-now clauses on kept sentences, replaced paragraphs for the retired table, charts and collapse; a "Refresh and load" paragraph; one word pending fold-5: the decode label no longer names the window.)
Review of 34e4fcd (fold-3 + 3.7r) and 075630e (3.7l) by DeepSeek/dgx1-a job
20261030-011950-1510b72c, 108 turns, 33 min, four construct-it probes. DEFECT D1 (fold-3): a
host card whose signature changed was rebuilt but the old element stayed in `#hosts` — 2 cards
after one state flip, 2 → 6 → 12 in ten seconds across real pushes (the lane's node-identity
smoke never flipped a state). P1: the signature counted never-seen endpoints, so one arriving
on a live host forced a rebuild (and fired D1). P2: the `.ep-active` span added a head row at
the 370 px seeded card (269.6 vs 243.9 px) — measured on the pre-fold-4 base; fold-4's flex
rule holds the head to one line and the merged smoke reads 243.9 px with the span. NOTICED:
15m draws 3 axis labels; a third concurrent job overlaps lane 1; a job finishing exactly at
`t0` draws a 0.4 % sliver; uPlot (dropped by fold-4 since). Clean: the header's four states,
last-active's four forms, in-place identity/skip/escaping/reorder, every timeline geometry case
hand-recomputed, `jobs_overlapping`'s predicate, the §5a fields. Mutation `s !== "idle"` →
`===` left all page tests green (no JS tests). 645 passed. FOLDED by the coordinator in the
commit after 0d0fbae: the old card leaves the DOM before the map forgets it; the signature
covers the drawn endpoints only; verified with the review's reproduction (1 card after a
down/up flip, no full render for a hidden arrival). Rides the next DeepSeek review.
- ☑ **3.7-fold-5 Page polish.** (0beaf24: Qwen/ai2 job 20261030-014102-3214a939, 85 turns, 18 min, 649 tests; the decode label plain, the window named once on the strip row; the card's job row two fixed lines, measured 265.66 px with and without the activity and 93.00 px for two more jobs; ticks without seconds. Review: DeepSeek, with fold-4, fold-4s and the D1 fix.) The
  three items above plus that review's findings.
Coordinator's read of the final merged page (2026-10-30 02:01, seeded world, 1400 px): the
header, cards, log and timeline all read as designed. One item left for the operator's eye:
- The card's job figures line still cuts after "⟳ 20261030-015626-4b27af51 5m05s · 50 calls ·
  2 files · …" at three cards across, so the ctx bar, speed and errors — the figures the row
  exists for (3.7e: "the important number is each job's own context fill") — hide behind the
  ellipsis while the 25-char name and the call/file counts take the width.

Review of a2f9a1c (fold-4), 3aa1219 (fold-4s), 6249c03 (the D1 fix) and 0beaf24 (fold-5) by
DeepSeek/dgx1-a job 20261030-020134-adf4f440, 89 turns, 21 min, four construct-it probes: the
D1 reproduction stays at one ai1 card across flips and ten seconds of real pushes; a never-seen
arrival advances no full render; heights 265.656 px with/without the span, with a 70-char id,
with/without activity, three jobs +93.00 exactly; the finished-row cache 86 → 26 statements,
eviction to 10 keys, `server_live` flips, NULL-finished elapsed null; all six import spellings
hit, literals in prose do not; `context_exceeded` as text, ms/s durations, the dated time cell,
matching pill borders. 649 passed. DEFECT (one, in a test): the fold-4s eviction test was
blind — its cache started empty, so a mutant without the eviction loop still passed; the code
is correct. FOLDED by the coordinator (5ab0b1d): the test primes the cache first; the mutant
now goes red. NOTICED: a superseded cache key for an id still in the tail is never popped
(unreachable through the store); `requestsTable` without `opts.now` dates even just-now rows
(both call sites pass a clock); at a 200-char job name every figure clips behind it (fold-6).
The 3.7 chain's reviews are closed: no open defect.
- ☒ **3.7-fold-6 The card's job line leads with what matters.** SUPERSEDED by 3.7u (operator 2026-10-30: no ellipsis at all). Was proposed: the
  name shortened to its id tail ("…4b27af51", the full name in the link's title; the table and
  the timeline keep the full name), then elapsed, the ctx bar + %, t/s, the errors badge; calls
  and files last, so the ellipsis cuts counts, never the fill. Same two-line, fixed-height row.
- ☑ **3.7t The handshake row is not an error.** (Landed 6c65906: DeepSeek/dgx1-a job 20261030-193134-10ea9bc6, 23 turns, 5 min, 698 tests. The shim answers `/api/hello` (GET and HEAD) with 200 `{}` before the body read, forwards nothing, writes no row; three tests drive the real Starlette route over httpx's ASGI transport against an unreachable stub backend; schema spec True-now under `requests`. Lane's notes for later: `Shim.__init__`'s httpx client is never closed; the route's HEAD depends on GET being in the methods list (now guarded by a test); only content-type and cache-control are forwarded upstream→client (a 429's retry-after is dropped). Not active in the running MCP server until it restarts, 3.8b. Review: Qwen/ai1 job 20261030-194032-f7c013c4, 37 turns, 5 min, three construct-it probes, NO finding: the pre-commit shim loaded side by side gives byte-identical 502-and-row behaviour on `/v1/messages` and `/api/anything`; `/api/hello/` and `/API/hello` still forward, `/api/hello?x=1` is answered (the query is not path bytes); a booby-trapped `receive` proves the branch runs before any body read; over a real Job the handshake leaves `requests`, `paths` and the transcript untouched while a control 501 writes its line. Notes: the HEAD guarantee leans on Starlette auto-adding HEAD to GET routes and stripping the body itself; a status-0 connection failure writes no `[backend]` transcript line (the page counts it from the table). Chain closed.) Operator (2026-10-30, watching the first live job on
  the landed page): "I see a few calls to /api/hello that return an error - what's those?" They
  are Claude Code's own startup handshake: every headless session opens with `GET /api/hello`
  to its base URL (the shim), the shim forwards it and records the backend's own status (501 on
  Strata and vLLM, 404 on llama.cpp; `shim.py:264` comment; the database holds 46 rows of 501
  and 25 of 404, one per job, none a failure). The request tables (`static/app.js`
  `requestsTable`) paint every status of 400 or more as `pill-error`, so each job's log opens
  with a red pill that means nothing. Proposed: a row whose path is `/api/hello` gets a muted
  "handshake" label instead of the error pill (the status still shown), the error count on the
  job row excludes it, and the test pins both. Operator decides.
  Operator: "Should the shim do something with that handshake other than trying to forward
  it?" Read from the Claude Code 2.1.312 binary (three call sites): at startup a `HEAD
  <base url>/api/hello` fired only for the first-party provider with no proxy set, 10 s timeout,
  the result discarded (`.catch(()=>{})`): a connection warm-up to the server it is about to
  talk to; a telemetry probe that accepts any status (`validateStatus: () => true`, reported
  as `http_501`); and a preflight command that requires 200 and exits 1 otherwise (not the
  normal start, since every job runs on). So the handshake's purpose is a warm connection to
  the shim itself; forwarding it sends the warm-up to the wrong server and records a false
  error. Coordinator's recommendation: the shim answers `/api/hello` itself with 200 and an
  empty JSON body and writes no row (the spec's row is "one row per backend request the shim
  forwarded", schema spec line 69); the page labels the hello rows already in history as the
  handshake. One story; operator decides.

- **Operator, 2026-10-30 late evening, during the usability pass (for the retro):** "Making good
  progress, reviews are holding up - including the human one." And at 23:40, with ai1, ai2, dgx1-a
  and the local llamacpp each running a lane: "Achievement unlocks: All endpoints active!"
- **Operator, 2026-10-30, after the first live jobs on the landed page (for the retro):** "Much
  improved UI experience, I like the timeline now." "The cards sizing is better, but could still
  be improved."
- ☑ **3.7u No ellipsis on the cards: the card grows to its widest text and keeps that width.**
  (2b6412f: Opus worktree agent, 87 tool uses, 15 min, 656 tests; measured in the Chromium smoke:
  30/30 lines fit with a 200-char name, a 120-char model id and a 300-char activity; the floor
  812 → 2068 → 2068 over seeded/wide/seeded renders; the live card 265.656 px at every step; past
  the cap a line scrolls in its fixed-height box behind a 3 px bar. CONSEQUENCE FOR THE OPERATOR'S
  EYE: the seeded world at 1400 px goes from three columns to ONE, because the activity line is
  ~120 chars (the server's `SNAPSHOT_ACTIVITY_LIMIT`), and two columns from ~1660 px. If that is
  too few, the one-line follow-up is to leave the activity line out of the floor measurement so
  it scrolls while the head and figures lines set the width. Coordinator added
  `scrollbar-width: thin` for Firefox, where `::-webkit-scrollbar` is ignored; whether Firefox's
  bar adds height is unmeasured (the smoke is Chromium). Review: rides the cross-model review of
  the 3.9 folds.) Opus's notes beyond the story: the server itself cuts the activity at 120
  chars without a mark (`read.py:29`, `runner.py:224` keeps 200) — the server-side form of the
  convention, the operator's call; the Recent jobs table shows a 200-char name whole and gets a
  horizontal scrollbar; `.ep-seen`'s margin leaks onto down cards' heads; `oneLine()` in app.js is
  dead in practice (the server already collapses whitespace).
  Review (Qwen/ai1 job 20261030-134418-66ab71e5, 51 turns, 9 min, construct-it over the smoke
  world at 900/1400/2400 px; src/tests/tools untouched; branch
  `local-agent/20261030-134418-66ab71e5`): the floor's arithmetic, the high-water mark across
  seeded→wide→seeded and server pushes, the skip and route paths, config order, the measuring
  class never escaping: all clean. Findings, all ACCEPTED:
  - U3 `app.css:413-422`: the coordinator's `scrollbar-width: thin` (9880fc6) makes Chromium
    ignore the 3 px `::-webkit-scrollbar` rule, so an overflowing line's pinned 21.75 px box
    loses 10 px to a classic bar (clientHeight 12), clipping the descenders. The coordinator's
    own defect. Fix: `scrollbar-width: none`, the affordance an `overflows` class set by the
    measuring pass.
  - U1/U2: lines the floor does not measure (`.ep-error`, `.ep-note`, the dormant line, the
    dropped-job line, `.host-note`) spill past the card and scroll the whole page (scrollWidth
    1692 > 885 at 900 px) or wrap and change the card's height (30.7 → 74.1 px). Fix: every
    one-line card text is measured and scrolls alike.
  - The three static gates are red on the regression each names and blind to neighbours (a
    `::after` ellipsis, `Array.prototype.slice.call`, an `else` lowering the floor): copy-drift
    tripwires, not proofs; the browser smoke is the gate. Noted, kept.
  Fold 3.7u-fix LANDED (Qwen/ai1 job 20261030-135433-9fbe205b, 68 turns, 12 min, 676 tests):
  barless scrolling boxes, the `overflows` class as the affordance, `ep-line` on every one-line
  card text, measured: live card 265.656 at 900/1400/2200 before and after; dormant 30.6875
  unchanged where it wrapped to 74 before; page scrollWidth equals clientWidth at both widths.
  Noticed: the down card's head is 26.69 px (4 px from `.ep-seen`'s margin, pre-existing since
  3.7c). Review of the fold: Opus worktree agent (77 tool uses, 19 min) over 4e14f35 and 688354a
  with the parent commit's static files served side by side as the baseline. 3.9D-fix CLEAN: no
  listener leak across four page visits, listeners see the fresh snapshot on both paths, 18 host
  transitions render in order without a throw, refetch at most every 30 s. 3.7u-fix: TWO
  REGRESSIONS, both ACCEPTED, fold to Opus (ruling (a)):
  - P-1 the floor tracks the window, not the text: `chrome = cardW − lineWidth` counts a flex
    row's free space for `.ep-seen` (the down head) and `.host-note` (the host head), so each
    reports need = the card's current width; the seeded world at 2200 px: floor 1078 where the
    widest line is 946, and a resize 2200 → 2000 → 2200 leaves the floor at 2153 and ONE column
    for the page's life (the parent stayed at 812 and two columns). The operator's fleet has a
    down host with a note, so their page is in this state until the fold lands.
  - P-2 `overflows` toggled on live nodes defeats the in-place comparison (outerHTML, class
    names): an endpoint with an overflowing line is REPLACED on every push (dgx1-a at 900 px, 7 of
    7), hover state lost; the parent kept it.
  - Possible: render calls listeners without a try/catch (one throwing listener stops the rest
    and skips `scheduleHistory`); `.endpoint:first-of-type` never matches (the host head is the
    first div), so every first endpoint shows a 1 px top border (pre-existing). Static gates:
    eight of nine mutants pass them; the smoke (wide.js, lines.js, drive.mjs) or Opus's own
    drivers caught six — the gates are tripwires, the smoke is the gate (same verdict as the
    Qwen review). Noticed: drive.mjs appends "/" to its URL, so a trailing-slash URL 404s.
  Fold LANDED 146ae8a (Opus, 55 tool uses, 10 min, 694 tests): a flex-item line is measured through
  its row at max-content (`.host-head` joins the measuring rule); the seeded world's floor 946 at
  every step of 2200/2000/2200/1400/2200 against an independent oracle in `tools/smoke/floor.js`
  (was 1078 → 2153 and one column); the in-place comparisons read the live node with the
  `overflows` mark masked (`builtHTML`/`builtClass`), so a scrolling endpoint is kept across
  pushes (`tools/smoke/keep.js`: replaced [] where dgx1-a was replaced 7 of 7). Two static gates
  (every flex row holding an `ep-line` is in the measuring rule; the in-place helpers never read
  raw outerHTML/className), each red on its mutant. Review of the fold: Qwen/ai1 job
  20261030-150909-d41e5277, 69 turns, 21 min, its own oracle (a hidden max-content clone per
  card): delta 0 in 18 rows over five worlds at 900/1400/2200, a long `.ep-active` phrase leaking
  nothing from the head's auto margin; 2400/1200/2400/800/2400 kept floor 946 = oracle with
  columns 2/1/2/1/2, a long-error world held 3439 = its text at every window, a 15-host page's
  vertical scrollbar moved nothing; identity kept for every `.endpoint` and `.host-head` over 7
  renders in four worlds at two widths with live marks, a changed error sentence swapped and
  re-marked. NO DEFECT: the 3.7u chain closes (3.7u → Qwen review U1-3 → fold → Opus review
  P-1/2 → fold → this review). Possible, noted: a `builtHTML` that stopped removing NESTED marks
  would pass every gate and smoke tool (keep.js witnesses endpoint/head identity, not sub-block);
  cdp.mjs's `cardFit`/`cardOverflow` fields still name pre-U1 classes.
  Operator: "I don't like the '...' convention because it hides information - either set a
  minimum card width that can accomodate the widest text, or dynamically size the card to not
  truncate (and use that as min size so it can't shrink below that high water mark). We get
  fewer cards horizontally, but more complete information." Coordinator's reading: the grid is
  `repeat(auto-fill, minmax(360px, 1fr))` (`app.css:72`); the model id line and the job rows
  truncate with `text-overflow: ellipsis` (`app.css:49`, `:104`). The story: the card's minimum
  column width is computed from its content (the widest head, tile and job line), applied as
  the grid's minmax floor and raised but never lowered during the page's life (the high-water
  mark, so the no-jump rule holds); no `text-overflow: ellipsis` on any card text. Fewer
  columns is accepted.
  Lane: Opus (operator: "It can go to Opus."), given the oracle and the acceptance numbers, not
  the method; also the activity line's 60-char cut in `epJobRow` (`app.js:413-414`) and the
  active phrase's 12em ellipsis (`app.css:332`) go, since they are the same convention.
- ☑ **3.7v "tool errors" 2 when the job page shows 1 error.** (Landed 39fdb3a: Qwen/ai1 job 20261030-193144-28191e60, 54 turns, 6 min, 700 tests on main. `requestsTable` draws a `/api/hello` row as a muted "handshake" pill, status in the title, no `req-err`; the job header says "tool errors" and "request errors" (status outside 2xx or an error text, handshake rows skipped); the requests-section total skips them too. DOM smoke over the seeded world, which carries one 501 handshake row per job: `pill-muted:handshake:501`, `pill-error:error:500`, `pill-ok:ok:200`, header tool errors=1 / request errors=0, zero exceptions. Lane's corrections and notes: the brief's "a test pins ES5" was FALSE — no test scans the static JS for let/const/arrow/template strings (gate added by the coordinator in the next commit: tests/test_monitor_es5.py strips comments and fails on let/const, `=>` or a backtick in any static/*.js; proven to catch each planted construct); the ENDPOINT page's requests totals are server-computed (`read.py`, error text non-empty) and do not skip handshake rows — moot once 3.7t's shim runs, and the seeded hello rows carry no error text; the spec's derived-columns prose already said "the hello handshake rows have no tokens".) Operator: "tool errors sometimes
  shows a count of 2 when the job only shows 1 error returned (often the /api/hello we will
  adress) - this could also be related to your earlier fix on dedupe." Coordinator's reading
  over the database (ten newest jobs): the table's count is tool RESULTS flagged `is_error`
  (`derive.error_count`, the `tool_calls` table; the jobs showing 2 have exactly two such rows,
  both Bash), while the one red pill on the job page is the `/api/hello` REQUEST (one row of
  status 501 per job, `requests` table): two different counts of two different things, both
  right, badly told. Not the dedup fix (that touched `wait_jobs`'s answer, nothing stored). The
  job page's own per-tool table sums the same column as the "tool errors" cell, so those two
  agree. Resolution: 3.7t removes the handshake pill; this story makes the job page say "N tool
  errors" and "N request errors" in words beside each other, and the overview column keeps its
  "tool errors" title. If the operator ever sees the per-tool table disagree with the column, the
  job name is what reproduces it.

- ☑ **3.7x A down node's card says "down".** Operator (2026-10-30 evening, the live page with
  ai9 off: its card head read "10.0.0.62:8080 timed out (host off, or a firewall dropping
  packets)" in red beside the name): "We dont' need the exposition on ai9 card - 'down' is a good
  summary state." Decision: the host head's note is the one word of the state, "down" (and
  "unknown" for the port-22 fallback's other answer); the probe's sentence is not lost, it rides
  in the note's `title` (hover) and stays in the snapshot and the endpoint page. Consequence
  that answers 3.7u's open question: that ~120-character sentence was the line setting the
  column floor on the live page (one column at 1400 px); with it gone the floor is the widest
  model id or activity phrase. Coordinator fold, test-first (a source pin in
  tests/test_monitor_page.py), spec §4 True-now sentence appended; rides the next page review
  (ruling (m): a fold whose whole diff the coordinator read). Smoke (cdp.mjs, seeded world,
  a hand-rendered down node with the probe's sentence on its endpoint): the head note's text
  "down", its title the sentence, class `host-note ep-line`, no exception; the card's need 812
  against the seeded floor of 946 (that floor is the seeded world's down ENDPOINT on an up
  node, whose `.ep-error` line is still drawn in full — a different case, unchanged). Suite 695.
- ☑ **3.7y A down node nobody has ever seen gets no card.** (Landed 63df4de: Qwen/ai2 job 20261030-193154-912b1d90, 65 turns, 13 min; 702 tests on main. `neverSeenOn` without the down-node clause, `hostCard` null for any host with nothing drawn, the hidden line lists "<name> (node down)" / "(node unknown)"; `hostSignature`'s rationale comment rewritten by the lane (its stated mechanism rested on the exception). Smoke `tools/smoke/neverseen.js`: (a) no ai9 card, "1 endpoint never seen, not shown: ai9 (node down)"; (b) last_seen set → card back, `node-down`, note "down", one "last seen" line; (c) back to (a) through the in-place path, threw null. Landed by hand: both page lanes appended to tests/test_monitor_page.py, the branch applied as a patch and its test appended. Lane's notes: `hostCard`'s `said` reads hidden endpoints too (the node-level sentence, kept); README.md:411-416 already states the new rule.) Operator (2026-10-30 evening, going
  AFK: "work on those open items"): "One thing that is striking me again - ai9 is 'down' yes,
  but it's also 'never seen'. Perhaps it shouldn't have a card in that state?" Today
  (app.js:564 `neverSeenOn`, 3.7a) a never-seen endpoint is hidden EXCEPT on a down node, and
  app.js:590 keeps a card for every down node even with nothing drawn, so ai9 — down, its one
  endpoint never seen — is a red card with one grey "never seen" line. Decision: the exception
  goes. A never-seen endpoint is hidden on a down node too; a down node with nothing left to
  draw has no card; the hidden line names the node's state so the alert is not lost ("… ai9
  (node down)"). A down node with a SEEN endpoint keeps its card and its "down" note (3.7x).
  Lane: Qwen/ai2, page story; DOM smoke; cross-model review with the batch.
- **Review of the evening page batch** (3.7x 56364bb, 3.7v 39fdb3a, 3.7y 63df4de, the ES5 gate
  ab4ddb6) by DeepSeek/dgx1-a job 20261030-194853-5f581983, 83 turns, 32 min, four construct-it
  probes over the seeded world (eight card worlds, eight request rows, the seeded and a synthetic
  job page, the endpoint page, a 300-char sentence against the floor, eleven gate strings, the
  three pins against their smallest wrong implementations). Findings verbatim:
  F1 (correctness / contract drift) — an `unknown` node still prints the probe sentence as card
  text and it sets the column floor (`app.js:587`, `app.js:591` vs spec §4 lines 161-163):
  VERIFIED at the lines — `downEndpoint(snap, ep, nodeDown)` is quiet only for a down node and
  `said` is computed only for one, so on an unknown node the `.ep-error` line carries the
  sentence; measured need 946 → 2634. F2 (tests) — the ES5 gate is defeated by a `//` inside a
  string before real code on the same line (`tests/test_monitor_es5.py:12`); latent today.
  F3 (tests) — the 3.7v requests-table pin does not pin the `req-err` exclusion; no committed
  smoke does. F4 (tests) — the 3.7v header pin passes with `requestErrors` returning a constant
  0, and the seed's value is 0 too. F5 (tests) — the 3.7y pin passes with every never-seen
  endpoint hidden, down or not (the `ep.state === "dormant"` clause unpinned); `neverseen.js`
  only exercises dormant. Possible: P-1 the job page's two error counts can disagree (header
  row-rule vs section error-text rule — by design, 3.7v); P-2 endpoint server totals count a
  non-empty `/api/hello` error (historical rows only); P-3 a handshake row's recorded body still
  prints in the error column; P-4 latent gate false positives (`x = "a"// c`). Clean: every 3.7y
  card transition (no duplicate, no throw, repeat render skipped), every 3.7v row and the seeded
  job page, the floor unchanged by a 300-char title on a DOWN node. Disposition: F1-F5 folded by
  the reviewer (ruling (a)), fresh job in the review worktree; P-1 and P-2 stay as written; P-3
  and P-4 noted. FOLDED and landed 1bfe4f3 (DeepSeek job 20261030-202159-a6e2707a, 91 turns, 20 min,
  714 tests): `alert` = down or unknown gates the title and the quiet flag (unknown-node smoke
  need 946 → 946, title the 300 chars, no `.ep-error`); the gate blanks string literals first
  (eleven cases); the three pins now fail the described wrong implementations (checked by the
  lane against them); `tools/smoke/handshake.js`, `unknown.js`, `neverseen.js` case (d) committed
  with README paragraphs. Review of the fold by Qwen/ai1 job 20261030-204439-d37cfc03, 57 turns,
  9 min, three probes: NO product defect (unknown.js need 946 with the 300-char title; neverseen.js
  all four cases; an up node's down endpoint keeps its `.ep-error` and may raise the need; a
  dormant-only unknown node has a card, note "unknown", no title — ruled correct, no sentence
  exists; every review-described wrong implementation rejected by its pin). Four test findings,
  verbatim: R1 (tests) `_STR` pairs quotes with no notion of regex literals, so `var r = /"/;
  const a = 1; var s = "x";` hides the const (app.js:723 holds one such literal; the shifts
  cancel there). R2 (tests) a line-continuation string hides a following const, and `x = "a"//
  const y = 1` fires on comment text (the pre-F2 lookbehind, redundant once strings are blanked).
  R3 (tests) the F3/F4 pins pin the row rule's SHAPE as substrings: `bad` rewritten to
  `>= 400`, or the header's `else if` made `if`, passes both pins and handshake.js. R4 (tests)
  the F1 pin's regexes are unanchored, so `… || host.state === "dormant"` passes every pin and
  smoke. Possible: `_LINE` on a `//` inside an unescaped regex literal (none in the files);
  `said` first-vs-last endpoint on a multi-endpoint node. Noticed: `requestsTable` and
  `requestErrors` hand-copy the outside-2xx rule in two files. Disposition: R2 folded by the
  coordinator (0a04720: the escape class admits a backslash-newline, `_LINE` strips every `//`,
  two cases added, 716 tests); R1 documented in the gate's docstring as a known limit (a parser
  is the next step if the scripts grow regex literals with quotes); R3 and R4 are the ceiling of
  regex pins over source — the honest gate for page behaviour is the DOM smoke, which today lives
  in `tools/smoke/` and is run at acceptance, not by pytest. **Chain closed**: this round found no
  defect in the product and each round was smaller; the open question for the operator is below.
- **Proposal for the operator (from R3/R4): a pytest-driven DOM smoke.** One test module that,
  when the Playwright chromium shell and node are present (skipped otherwise), seeds a world,
  starts a `--no-collect` monitor on a free port, drives headless Chromium through
  `tools/smoke/cdp.mjs` and asserts the `d1` cases of `neverseen.js`, `unknown.js`, `handshake.js`
  and `nodes.js` — the hand-renders become the suite's gate instead of regex pins over source.
  Cost: ~10-15 s per run, two external binaries the suite tolerates being absent. Not built.
  Operator (2026-10-30, after `/mcp` reconnect): "The risk of the smoke test is that it runs
  against renders, not truth." Read: a hand-edited snapshot proves the renderer agrees with a
  snapshot we wrote — the fake shaped by the builder's assumption (hints: "where does the fake's
  output come from?"). If this is ever built, its inputs are the seeded world's REAL
  `/api/snapshot` and CAPTURED snapshots from the live database (ai9 down-and-never-seen as the
  collector actually recorded it), with hand edits only for states the seed cannot produce and
  labelled as such; and the seed itself should grow the states the operator has seen live. Open.
- ☑ **3.7w The monitor tab's memory.** (Probe landed: `tools/smoke/mem.mjs`, Haiku worktree
  agent, 31 tool uses, 11 min. Measured at ~3 render passes a second over the seeded world:
  overview 908 passes, DOM nodes 200 → a plateau of 1443–1568 → 200 after a forced GC, heap
  0.5–1.0 MB, 10 listeners throughout; endpoint and job pages 245 passes each, nodes a sawtooth of
  ~115 per pass that the browser collects every 80–100 s back to the baseline (262 / 1449), heap
  ≤ 1.5 MB, listeners constant. Reading: the page owns no leak; the detached nodes are the
  per-push table and detail re-renders awaiting collection. The 562 MB Firefox figure is not
  JS heap or DOM; it is the content process's own footprint (the process holds more than the
  tab) or Firefox's handling of the long-lived EventSource response. The operator's second
  about:processes reading of the same tab decides whether anything grows; if it does, the next
  probe is Firefox-side (about:memory on the tab), not the page's.) Operator (2026-10-30, Firefox about:processes while hunting
  a workstation memory hog that turned out to be a Proxmox console tab at 19 GB): the
  "localagents monitor" tab's process at 562 MB after roughly an hour on the live stream (the
  Strata UI tabs sat at 2 GB each, not ours). Coordinator's reading: no measurement exists; the
  page replaces the jobs table and rebuilds the timeline on every push (`render`,
  `timeline.update`), swaps card sub-blocks in place, keeps bounded history per endpoint, and
  reconnects its EventSource with backoff; any of these can leak nodes or listeners if a
  reference survives the replace. The probe (DOM-only, ruling (j)): a `tools/smoke/mem.mjs`
  that samples the DevTools `Performance.getMetrics` counters (`Nodes`, `JSEventListeners`,
  `JSHeapUsedSize`, `Documents`) every few seconds over a seeded `--no-collect` monitor with a
  short `refresh_s`, so hundreds of pushes happen in minutes; a count that grows with the push
  count is the leak, one that plateaus is not. Then a second reading of the tab by the operator
  an hour later says whether Firefox agrees.
  Second reading (operator, 2026-10-30 22:29 UTC, about:processes): the tab's process at 667 MB,
  5 h 12 min after the 562 MB reading, so +105 MB (~20 MB/h) with no forced collection; the
  claude.ai tab in the same window sat at 619 MB and the GitHub tab at 488 MB. Reading: a
  full-snapshot JSON parse and a re-render every tick leaves garbage Firefox collects on its own
  schedule, and the page's heap measured flat in Chromium, so 20 MB/h of uncollected process
  growth is not evidence of a page leak. What settles it: about:memory → "Minimize memory usage"
  on that tab, then about:processes again; a drop back toward 560 MB is garbage, a figure that
  stays and keeps climbing by the morning is retention and the next probe is Firefox-side.
  Operator (same evening, after "Minimize memory usage"): 661 MB. Six MB came back, so the
  footprint is not collectable garbage; it is resident. The stream answers with
  `Cache-Control: no-cache` (app.py `events`), which permits storing while requiring
  revalidation; whether Firefox keeps the long-lived response body is unverified. The breakdown
  that names the holder is about:memory → "Measure", the tab's `explicit` subtree (window-objects
  js / dom, heap-unclassified, images, network cache). Firefox 158 is on the workstation but no
  geckodriver or Marionette client; a Firefox-side probe is a raw-Marionette memory dump over a
  seeded `--no-collect` monitor, an hour of tooling, the operator's call.
  Operator's about:memory Measure (main process tree, 1,849 MB explicit): heap-unclassified 816,
  gfx 426 (heap-textures 312), network/cache2 189 with no 127.0.0.1 partition among the entries
  of 22 MB and up (the stream body is NOT accumulating in Firefox's HTTP cache), js-non-window
  126, profiler/lul 73. The monitor tab's own tree is the "Web Content (pid 2951858)" section.
  The tab's tree (webIsolated=http://127.0.0.1, 576 MB explicit, resident-unique 661): 309 MB
  window-objects of which **278 MB `dom/orphan-nodes`** (nodes out of the document but still
  reachable from JS, so a GC cannot free them), the window's js-zone 11 MB, js-non-window 30 MB
  (LastPass 10 MB, uBlock Origin 7.5 MB, Download Manager (S3) content-script realms in the same
  process), heap-unclassified 233 MB, 78 listeners, 26 event targets. Nothing in the page's code
  keeps a node: the per-push card, the old-cards map, the floor measurement's lines are locals
  (`render`, `updateHostInPlace`, `raiseCardFloor`). Coordinator's probe, Firefox-side and
  extension-free: `tools/smoke/ffmem.py` (raw Marionette, `nsIMemoryInfoDumper` with minimize, the
  tab's process by its `top(` path) with `tools/smoke/churn.js` driving the three detaching paths
  every 250 ms. Firefox 158, seeded world at refresh_s 0.5, four minutes, 960 churn renders +
  1,692 pushes: orphan-nodes 0.0 MB, dom 0.2 MB, js 2.4 MB at every sample; resident-unique
  21 → 55 MB flattening. Reading: the page retains no node in Firefox either; the 278 MB in the
  operator's tab is held by something else in that process, and the content scripts are the
  candidates (an extension that notes nodes it has scanned keeps every rebuilt subtree alive).
  Not proven. The A/B that proves it: the same page held an hour in a Private Window (extensions
  without private-browsing permission do not run there), then about:memory's orphan-nodes for
  that window's process beside the normal tab's.
  Operator's Private-Window reading (webIsolated=http://127.0.0.1^privateBrowsingId=1): "3GB" is
  the process's vsize (2,841 MB of address space); explicit 49.7 MB, resident-unique 56.7 MB.
  No content-script realm in that process (realms: the page, a junk compartment, the system
  one), so the extensions did not run there and the A/B is clean. `dom/orphan-nodes` 13.4 MB,
  15 listeners, Array elements 5.9 MB (the history arrays). Open question: whether that reading
  followed a minimize (pending cycle-collector garbage looks the same until it does) and how long
  the window had been open; the post-minimize orphan figure decides.
  After minimize (operator): explicit 15.2 MB, resident-unique 46.5 MB, `dom/orphan-nodes`
  **0.02 MB** (from 13.43: pending cycle-collector garbage, freed), the window's dom 0.34 MB,
  15 listeners. The normal tab's 278 MB survived the same minimize. **Closed:** the page owns
  no leak in Chromium (mem.mjs) or Firefox (ffmem.py, the private tab); the normal tab's
  orphan nodes are held by a content script of the operator's profile (LastPass, uBlock
  Origin or Download Manager (S3) run in that process and not in the private one). Not a
  product defect; the operator's remedy is per extension (disable one on this site at a
  time, minimize, re-measure). The "3GB" was vsize, both times.

### 3.8 Running it (operator 2026-10-29, while the 3.7 lanes ran)

- ☑ **3.8a Browser open by default, `--no-open`, already-running opens and exits 0;
  `tools/monitor.sh start|stop|status|log`.** (88a7dbe: Haiku Agent worktree-agent-a3d878d89ce563991,
  48 tool uses, 6 min, 614 tests; operator: "a startup script and default browser open would be
  useful - the uv command isn't friendly, and having to navigate ourselves is friction"; "I think
  open is the default? --noopen is the option." Every live test and the smoke recipe pass
  `--no-open`. The coordinator ran the launcher live with a BROWSER fake and a scratch state
  dir: start → status → start again ("already running … opening", the fake got the URL twice) →
  stop, port free. Review: rides the next batch's review (coordinator read the whole diff).)

- **3.8b Operational fact (coordinator, 2026-10-30 19:40), not a story.** The `localagents` on
  the PATH is a `uv tool install` of this checkout in EDITABLE mode
  (`~/.local/share/uv/tools/localagents/…/direct_url.json`: `"editable": true`), so a landing on
  main is what the next PROCESS start runs — and a running process keeps the code it loaded.
  Tonight: the monitor was restarted at 19:06 and runs today's main (it migrated the real
  database to v8); the MCP server process (pid 1813371) has run since 2026-10-29 21:57 and
  still runs yesterday's package, so none of today's package-side landings are active in it —
  `wait_jobs` dedup (A1), the runner's finish fixes (B1-B3), the live-jobs predicate (E2), and
  1.12's `record_transcript`: the real `transcripts` table holds 0 rows after 16 finished jobs
  since v8 landed. 3.7t (the shim's handshake answer) will be in the same position. The MCP
  server restarts with the Claude Code session (or `/mcp` reconnect); the operator decides
  when. Page files (static/) are served from disk, so a page landing needs only a browser
  reload.

### 3.9 Codebase review pass (operator 2026-10-30)

> Looking it over - much smoother, but I can't see it in action. Please push a full codebase
> review pass to ai1, which will serve both review purposes and get some activity on the monitor
> for me.

The pass is cut by door for the Strata lane (one slot; a review brief holds at most four
probes, hints "Review lane"), the doors run one after another on ai1
(qwen3.8-flash-next-iq3_s), each read-only with its report in `t/REVIEW.md` on its own
branch; the oracle is the spec, the schema document and the README, never the suite ("the same
models wrote the tests"). Findings are dispositioned here; accepted ones become folds under
ruling (e). The lane is also the operator's first live job on the landed page.

- ☑ **3.9A The MCP server's request path**: `server.py`, `registry.py`, `shim.py`, `status.py`,
  `__init__.py` (~2.3k lines). Qwen/ai1 job 20261030-123425-86d1444b, 55 turns, 11 min, 0.98 cache
  hit; four probes (shim 16, registry 16, server 14, status 8: 54 assertions, re-run green by the
  coordinator), src/ and tests/ untouched, report `t/REVIEW.md` on branch
  `local-agent/20261030-123425-86d1444b`. The lane disclosed one process error of its own: it
  wrote the P2 section before running probe 2 and one expected value was wrong (fixed, re-run,
  the report states the measured behaviour); the 3.9B brief now says "run each probe before
  writing its section".
  Findings and disposition:
  - A1 (correctness, minor) `server.py:646`: `wait_jobs([j, j])` answered the same record twice
    and the progress total counted two jobs. ACCEPTED, folded by the coordinator: the ids are
    deduped once at entry (`dict.fromkeys`), red test
    `test_wait_jobs_counts_a_repeated_id_once` (650 tests). Review of the fix: rides the
    cross-model review of the 3.9 folds.
  - P1-a `shim.py:106-109` (an image-only system message is dropped by the fold): DECLINED, no
    Claude Code request carries one; noted.
  - P1-b `shim.py:175-177` (an error body with `error` as a bare string never matches the
    overflow translation): DECLINED for now, no captured backend sends that shape; noted as the
    first place to look if a proxy ever sits in front of a backend.
  - P2-a `registry.py:601-604` (a negative `cache_hit_ratio` could reach the row): DECLINED with
    evidence, `jobs.py:110` lists `prompt_tokens_cached` among the guarded counters, so a
    backwards cached counter nulls the whole set.
  - P2-b `registry.py:454` (processed clamped to 0 when reused exceeds prompt): the lane itself
    judged it acceptable; agreed.
  Noted beyond the door: the relay client has no read timeout (`shim.py:209`, right for streams,
  the job's own `timeout_s` is the backstop); the synchronous sqlite writes on the event loop are
  the spec's known choice; a task cancelled before its first step skips an in-body
  `except CancelledError` (a probe-harness fact, unreachable through `run_agent`).
- ☑ **3.9B Jobs and storage**: `runner.py`, `jobs.py`, `db.py`, `derive.py` (~1.7k). Qwen/ai1 job
  20261030-124622-eaa27899, 44 turns, 9 min; four probes (23 assertions, re-run green by the
  coordinator under a scratch state directory), src/ and tests/ untouched, report `t/REVIEW.md`
  on branch `local-agent/20261030-124622-eaa27899`. Clean: v1→v6 migration parity, the two-process
  `lookup_id` race converging on one row, the liveness sweep, gen_ms-0 span fallback,
  `summary_from_row` key-for-key with `Job.summary()`.
  Findings and disposition (each verified by the coordinator at the cited lines):
  - B1 (correctness, minor) `runner.py:167-178`: a kept tree whose only change is an untracked
    file has a `diffstat` naming it while the note's "Review with: git diff <base>" shows nothing.
    ACCEPTED: the note's instruction adds `status --short`.
  - B2 (correctness, minor) `runner.py:145`: a worktree directory deleted while the job runs
    makes `finish_worktree` raise FileNotFoundError, so the JSON never gains `kept`/`note`.
    ACCEPTED: a missing directory is recorded (`kept: False`, a note, `worktree prune`), the
    branch left for the user.
  - B3 (contract drift) `runner.py:312-313` vs `derive.py:151` and the schema spec line 135: a
    cancel absorbed during the worktree step leaves the in-process `run_cwd` at the original cwd
    while the row derives the tree's path. ACCEPTED: `run_cwd` is set where the tree is recorded.
  - Possible (declined, noted): `record_request` KeyError on a record without `path`/`t` (the
    shim always sets both); a duplicate `tool_use_id` strands a row (the SDK names calls
    uniquely); `transcript_tail(-1)` (guarded at the tool layer); a foreign worktree JSON without
    `repo`/`root` (the code only ever writes them).
  Fold 3.9B-fix LANDED 8335861: Qwen/ai1 job 20261030-125954-3dadfa29, 24 turns, 2 min, one red
  test per rule (`test_kept_note_shows_untracked_files_to_the_reviewer`,
  `test_finish_over_a_missing_directory_records_it`,
  `test_cancel_during_make_worktree_sets_run_cwd_as_the_row_does`), 653 tests; ruling (a): the
  reviewer fixed its own verified findings. Review of the fix: rides the cross-model review of
  the 3.9 folds.
  **Incident (process, not code).** The lane's words: "my first run of `test_prune_days_zero_disables`
  forgot the monkeypatch fixture, so `state_dir()` resolved to the real
  `~/.local/state/localagents/jobs` and the in-test control `prune(1)` deleted its directories
  older than one day — 66 job directories, ~20261023 through 20261029-12:54 … their database rows
  are untouched … a probe-hygiene failure on my side, not a code finding." Inventory by the
  coordinator: 19 database jobs (20261029-110844 to 20261029-124353) lost their directories
  (transcripts; the job page now says "transcript pruned or never written" for them); the 47
  older directories were pre-database, file-only records of jobs from other projects; no backup
  exists. Operator: "On the door D incident, okay what was done is done - the data is still
  development data, and only operational so nothing valuable was lost." (The incident was door
  B's; door D runs on ai2.) Cause: probe files live under `t/`, outside `tests/`, so the suite's
  `tests/conftest.py` redirect of the state directory never applied to them; the brief told the
  lane to copy that shape and one test forgot. Fix (structural, not a reminder): `t/conftest.py`
  is now a committed file with the same autouse redirect (`.gitignore` keeps `t/*` ignored and
  re-includes that one file), so every worktree carries the net from its first probe; the
  coordinator wrote it into the running 3.9C and 3.9D worktrees before either ran a probe. Retro
  candidate: "a probe outside tests/ runs without the suite's safety net".
- ☑ **3.9E Every statement's query plan** (operator: "If all your queries are properly scoped and
  keyed, the size of the database shouldn't affect performance until it grows unreasonably
  large."). Qwen/ai1 job 20261030-130909-508658d5, 30 turns, 5 min, read-only, `t/plan_sql.py`
  runs `EXPLAIN QUERY PLAN` over 88 statements against a seeded file (branch
  `local-agent/20261030-130909-508658d5`). 85 are scoped and keyed. Findings:
  - E1 `read.py:282` (`_JOB_SELECT + _FINISHED_TAIL`): the snapshot's finished tail scans every
    jobs row and sorts them in a temp b-tree on EVERY refresh (`NOT IN` keeps SQLite off
    `jobs_status_finished_at`). CONFIRMED by the coordinator on the real file; with
    `jobs_finished_at` the plan is an index walk stopping at the LIMIT. ACCEPTED: schema v7.
  - E2 `read.py:628` (`jobs_overlapping`): scan + sort per timeline request; the same index
    gives the OR branch something to use. ACCEPTED, pinned to what the plan actually says.
  - E3 `server.py:755` (`list_jobs(include_finished=False)`): walks `jobs_created_at` until
    enough live rows surface. ACCEPTED to investigate: a partial index only if the plan uses it.
  - Observations, not findings: `_NEWEST_REQUEST_SQL` (read.py:72) MAXes over an endpoint's
    every request row (requests are never pruned); endpoint request reads cost with the
    endpoint's all-time job count; `mcp_servers` grows one row per server start. All three
    belong to the open purge item.
  Fold 3.9E-fix to Qwen/ai1 (ruling (a)); 1.12 (transcripts) is schema v8.
  **3.9E-fix LANDED 5499c35** (Qwen/ai1 job 20261030-131541-468b0341, 50 turns, 8 min, 663 tests; the lane's own record:
  `EXPLAIN QUERY PLAN` over the jobs-table statements; E1 verified on a copy of the real
  database before the fold). Findings and disposition (all pinned with
  `EXPLAIN QUERY PLAN` tests in `tests/test_query_plans.py`):
  - E1 (cost) the snapshot's finished-jobs tail sorts the whole job history every snapshot:
    `SCAN j` + `USE TEMP B-TREE FOR ORDER BY`. ACCEPTED: schema version 7 appends
    `jobs_finished_at`; the plan becomes `SCAN j USING INDEX jobs_finished_at`, no sort.
  - E2 (cost) `jobs_overlapping` (fleet timeline) scans the whole table: `SCAN j` + temp sort.
    ACCEPTED on the same index: the OR terms gain `MULTI-INDEX OR` with two
    `SEARCH j USING INDEX jobs_finished_at` seeks — the audit's OR-optimisation expectation
    holds; the `started_at` sort stays (nothing indexes `started_at`), now over the window's
    rows only.
  - E3 (cost) `list_jobs(all=True, include_finished=False)` walks every job ever run through
    `jobs_created_at` to keep two statuses: `SCAN jobs USING INDEX jobs_created_at`. ACCEPTED
    as a predicate rewrite, NO new index: `status NOT IN (<five terminal>)` →
    `status IN ('queued','running')` (the same set under the status CHECK — a NULL status
    matches neither form), and the plan becomes
    `SEARCH jobs USING INDEX jobs_status_finished_at (status=?)`. The candidate partial index
    `jobs_live_created` is REJECTED: the plan never names it, even with a provably-implying
    predicate — SQLite prefers the existing composite index.
  Fold 3.9E-fix LANDED this branch (46b2a6b + 542d5b9 + ae8040f + docs): the v7 migration
  (`test_a_version_6_file_migrates_to_version_7_with_its_job_rows_intact`, the fresh-file test
  updated), six plan pins in `tests/test_query_plans.py` (E1/E2 red proven with the `_V7` step
  commented out; E3 pins `server._LIST_JOBS_SQL` directly, plus a row-set-equality test for the
  rewrite), 660 tests.

- ☑ **3.9C The monitor's server**: `monitor/app.py`, `read.py`, `collect.py`, `serve.py`,
  `state.py`, `config.py`, `rows.py`, `rates.py`, `ranges.py`, `retention.py`, `jobs.py`,
  `seed.py` (~2.9k). DeepSeek/dgx1-a job 20261030-125246-bbdacb91, 89 turns, 32 min, 0.98 cache
  hit; four probes (23 assertions, re-run green by the coordinator under `t/conftest.py`),
  src/ and tests/ untouched, report on branch `local-agent/20261030-125246-bbdacb91`. Clean:
  every §4 state, the reset sentence, the stored witness, the ten-row tail, the collector's
  transactions and the host-probe interval, every range edge and error shape, the shared
  snapshot (one connection for two calls), the batched route equal to its parts, one SSE push
  per settle. Measured: a cold `overview` over six endpoints and fourteen jobs runs 107
  statements, 47 warm. Findings (each verified by the coordinator), all ACCEPTED:
  - C1 `app.py:293-323`: the history cache keys endpoint names and the literals `fleet`/`jobs`
    in one namespace; an endpoint named `fleet` makes `/api/fleet/history` answer the endpoint
    shape. Fix: a typed key.
  - R1 `read.py:484`: a 7 d history loads every probe row in the range (120,961 rows for 599
    points, 861 ms) and `fleet_history` does it once per endpoint. Fix: rows read bounded by the
    points answered, body identical.
  - R2 `app.py:293`: the history cache never evicts and the range grammar admits 10,241
    windows per key. Fix: only the five page ranges are cached.
  - C2 `seed.py:81-92`: `seed --force` is accepted while a collector writes the database (a
    monitor writes no `mcp_servers` row); the collector's cached ids then store probes on the
    wrong endpoint (measured). Fix: refuse when a probe row is younger than 120 s.
  - T1 `serve.db_file` and the `--db` / `monitor: db` overrides have no test; T2
    `seed.parse_now`'s ISO branch has no test. Fix: tests.
  - `read.py:124` docstring says the witness is backfilled at the v6 migration; serve does it.
  - Possible, noted: a zero or negative probe interval writes no rate and no witness; a host
    whose last probe is a stale up row reads `up` while its endpoints read dormant; two
    monitors on one database are not refused (each probe written twice); `_Fanout.start`
    re-homing; the history and finished caches are unlocked dicts across worker threads (wasted
    work, not corruption).
  Fold 3.9C-fix LANDED (DeepSeek/dgx1-a job 20261030-132735-94015337, 124 turns, 48 min, 12 new
  tests): typed cache keys through one `cached()` helper, the cache gated to the five page ranges
  (54 → 5 entries over 50 windows; `app.state.history_cache` exposed for the test), the witness
  docstring, `db_file` and `parse_now` tests, seed refusing a probes row younger than 120 s on the
  wall clock (twice: before and inside the write lock), and `endpoint_history` as three bounded
  statements (counts; ROW_NUMBER/LAG spans, one row per kept point; the kept rows by id) with a
  verbatim copy of the old function as the oracle over the 120,961-row world: byte-identical,
  1201 → 490 ms at the function, 861 → 390 ms cold on the route. DeepSeek's notes: fleet_history
  is still serial per endpoint (~2 s cold for five dense 7 d endpoints); the cache is bounded but
  up to 5 × (endpoints + 2) bodies; a seed with `--now` near the wall clock now trips both
  liveness checks. Review of the fold: Qwen/ai1 job 20261030-141817-f201ca86, 49 turns, 9 min,
  construct-it over the pre-change function as the oracle: every edge world (n = 0/1, k = 1 with
  a reset and a since change, the k boundaries, a down probe newest/oldest/pre-range, mixed
  presence, same-t ties, max_points = 1) and 120 random worlds byte-identical, the kept fetch
  exactly the points answered; fleet 7 d route 23 → 32 statements (narrower: the largest 1441 →
  481 rows); the cache and seed guards as claimed, the 120 s boundary exact. NO DEFECT: the
  3.9C chain closes. Possible, noted: the spans SQL's bare `id` sits beside two aggregates (SQLite
  guarantees the lone min/max case; correct on 3.47.1); the non-force seed path has no inner
  re-check; a `--now` in the future refuses later seeds until the wall clock catches up; the
  history cache has no age term, so on a `--no-collect` server (the smoke's) a jobs-bearing body
  freezes at its first answer (a real monitor's probes move newest-t every tick).
- ☑ **3.9D The monitor's page**: `static/app.js`, `endpoint.js`, `job.js`, `app.css`,
  `index.html`, with `tests/test_monitor_page.py` and `tools/smoke/` (~2.9k). Qwen/ai2 job
  20261030-125256-9ee02e1b, 175 turns, 71 min, SIX compactions (the 131k window; it finished),
  four DOM probes over the smoke world plus a driver of its own (`t/probe_4_drive.mjs`: cdp.mjs's
  fixed 2.5 s read cannot witness anything time-dependent); src/tests/tools untouched; report on
  branch `local-agent/20261030-125256-9ee02e1b`. Clean: both request tables recompute exactly
  (prefill/decode, totals, order, pills), tiles, tiers, gauge, marks, by-tool table, routing back
  with zero console entries. Findings (each verified by the coordinator), all ACCEPTED:
  - D1 (correctness) `app.js` render's rebuild branch: a host whose card vanishes (a down node's
    never-seen endpoints come back up: hidden now) is removed but not forgotten in `oldCards`, so
    the gone-host loop removes a detached node, render throws after `snapSig` is set, and the
    skip path never repairs. The coordinator's fold-3 fix left this path. Fix: forget the host
    unconditionally.
  - D4 (contract drift) `endpoint.js:548` watches `#updated`, which nothing has written since
    3.7r moved the phrase to `#conn-label`: an open endpoint page NEVER refreshes (36 s, 7
    overview renders, 0 mutations, 0 fetches; the job page's own stream moved). Fix: an explicit
    `onSnapshot` hook from app.js, the dead span removed, a gate that every watched id exists.
  - D2, D3, D5 (tests): no gate for a vanishing card, the timeline's range redraw, or the endpoint
    page's liveness past 30 s. Fix: smoke scenarios and `tools/smoke/drive.mjs`.
  - Noticed: a tooltip prints "cache read " on a null; a failed range fetch leaves `histRange`
    stale behind the pressed button; unguarded `tool_call_log.forEach` on the job page.
  Fold 3.9D-fix LANDED 688354a (Qwen/ai2 job 20261030-140556-75160044, 125 turns, 27 min, two
  compactions; cdp.mjs's probe fields composed by hand with 3.7u-fix's, both kept; 690 tests):
  `onSnapshot` hook fired at the end of both render paths, endpoint.js registered through it with
  a remover in teardown, the dead `#updated` span gone; D4 measured over a 40 s hold with the new
  `tools/smoke/drive.mjs`: the last-seen sentence and the latency tile moved, two refetches after
  the 30 s window, 8 renders, 0 exceptions (frozen before). D1: the host forgotten whether or not
  a card was built; nodes.js carries the vanishing transition (NotFoundError before, clean
  after). D3: `tools/smoke/range.js` clicks the real buttons (15m axis 3 labels, 7d 7 labels);
  `js:` steps may await a promise. Tooltip halves drop with their figure; `tool_call_log` loops
  guarded; `histRange` stays with the drawn data on a failed fetch (by design, commented). Qwen's
  notes: 15m yields 3 axis labels against the "4-7" comment (`TL_TICKS`); the range choice
  persists in localStorage, so smoke runs are order-sensitive on it. Review of the fold: the next
  page review.
- ☑ **3.9 folds review, batch 1 (the package side)**: DeepSeek/dgx1-a job 20261030-141807-2b3de9d1,
  57 turns, 25 min, four construct-it probes (17 assertions) over d2fbc24 (A1), 8335861 (B1-B3),
  5499c35 (v7) and 52cb57b (v8/1.12), none of which it built; src/tests untouched; branch
  `local-agent/20261030-141807-2b3de9d1`. Clean: v6→v8 with rows intact and the cascade; row ==
  file at the terminal `changed()`; a second finish replaces; the prune-step race leaves the row
  an exact prefix and every shipped reader takes the row; invalid UTF-8 and 2 MB whole; the
  live predicates identical over every status and NULL; the worktree records; duplicate ids
  (in-process and foreign) counted once in both modes. Findings, all ACCEPTED, fold to DeepSeek
  (ruling (a)):
  - F3 (correctness, low probability, high impact) `runner.py` finally: `record_transcript()` is
    the one call neither shielded by `_to_the_end` nor catching beyond OSError/sqlite3.Error; a
    MemoryError there escapes before `_done.set()` and strands the row `running`, every waiter
    timing out. Fix: catch Exception inside, one `[db-error] transcript` line.
  - F2 (correctness) `runner.py:155-157`: a kept tree whose `.git` cannot be read is recorded
    `kept=True`, empty diffstat, "Changes left … Review with …" (return codes of `status` and
    `rev-parse` ignored). Fix: record "git could not read the worktree", nothing removed.
  - F1 (false invariant) the comment "a terminal jobs row always has its transcript row": a job
    cancelled before its first line has no file and no row (readers say so). Fix: the record
    states the exception; the tests' "1.11" docstrings become 1.12.
  - Possible, noted: a `lost` job can carry a row (death between the two adjacent statements);
    the completion copy is synchronous on the event loop with no size cap (fine at today's
    75 KB median); `IF NOT EXISTS` tolerates a same-named wrong index. Noticed: `make_worktree`
    can hand `run_job` a nonexistent `run_cwd` when `job.cwd` is an untracked subdirectory;
    `job_log` reads the row twice; `_FINISHED_TAIL` drops a NULL-status row like `list_jobs`.
  Fold LANDED (DeepSeek/dgx1-a job 20261030-144409-f1c87ef2, 62 turns, 11 min, 692 tests):
  `record_transcript` catches Exception on read and write with one `[db-error]` line through a
  non-raising `Job._log_safe` (red: a MemoryError left the row `running` and the wait timed
  out); `finish_worktree` reads the return codes of `status` and `rev-parse` and records "git
  could not read the worktree … nothing was removed"; the invariant corrected in the runner
  comment, the `_V8` comment, the schema spec and the test docstrings (1.11 → 1.12). DeepSeek's
  notes: the other `record_*` handlers still call `log_text` inside their excepts (a failing
  transcript write would break "the database never fails the job" there; `_log_safe` now exists
  for them); `finish_worktree`'s changed branch ignores the return codes of `add -A`, `diff
  --cached` and `reset`; `_restore_worktree` trusts an existing root without checking git can
  read it. Review of the fold: the chain closes here unless the page review reopens the runner
  (one defect found by the first review of these folds, each fixed test-first; the three notes
  are the next small story, not a defect).

### 3.10 Usability pass (operator 2026-10-30 evening, reviewing the live page)

> Thinking usability now while reviewing. Recent jobs doesn't have a timestamp - so answering
> "when" is not possible from the screen. The server gone tag on the job id field - what is the
> purpose and use of that? If it has no real use, then drop it to make room for a timestamp
> field. Also it shows "speed" but really that's decode - where's prefill speed? Endpoint
> overview - "endpoint": Up, "node": Up. Yeah, we can see that from the header on the page.
> Redundant. It also says "every endpoint" on tier 0 - why? We're looking at THIS endpoint. Did
> you mean "lane" or "slot"? Confusing. I think also drop the "tier x" - nobody cares - go with
> better groupings and labels. Don't be chatty. Also same rule about ellipses on the cards here -
> "served model" for qwen3.8.flash-next is truncated when the card could just be wider. The
> "Jobs on it" - Should just be Jobs + filter reminder. Also that table should be the same table
> from the primary page, just filtered to this endpoint - no need to have two different tables -
> and the code should be re-used. What is "events"? I have none. Requests table should re-use
> the one from the job page with an appropriate filter for this page. Re-use code. Summary line
> for that table is fine. But it shows me we need pagination on our tables - this one I'm
> looking at as 889 lines, which is not useful. Pagination with local column filters and
> sorting? Jobs page: Same rule to re-use tables with approriate filters for the screen.

Coordinator's answers from the code (2026-10-30):
- **"server gone"** (app.js:639): `server_live` is false when the job's MCP server has stopped,
  has not beaten for 60 s, or its pid is dead (read.py:175). On a FINISHED job that is the normal
  state once the Claude Code session that ran it ends — no information. On a running/queued job
  it means the job is lost (the job page says "lost: its server stopped beating", §6); in the
  Recent-jobs table the only sign of that is this badge, because `statusPill` (app.js:619)
  prints the stored status, "running". So: the badge goes; a live job whose server is gone
  gets the pill "lost" instead. Confirmed droppable.
- **"speed"** (app.js column, read.py:258 `gen_tps`) is decode: `derive.gen_tps` replays the EMA
  of output tokens / generation span over the job's `/v1/messages` rows. Prefill exists per
  request (`prefillRate`, app.js:1419: (input + cache_creation) / (ms − gen_ms)) but there is no
  job-level figure; one is derived the same way (sum of processed prompt tokens over the summed
  prefill span, rows without both skipped) and the two columns read "prefill tok/s" and
  "decode tok/s".
- **On "when" and the job id** (operator, after 3.10a was cut): "you intended the job-id to
  have the date/time embded, and I argued against that. I wanted a pure GUID, but you wanted
  something sortable." Both exist: `jobs.id` is the UUID (every link and row keys on it) and
  `jobs.name` is `YYYYMMDD-HHMMSS-<hex8>`, the sortable one the table and the directory show.
  The name carries the start time in compressed form; 3.10a's "started" column makes it
  readable without the convention. No change to the scheme.
  Operator: "the 'name' isn't very useful otherwise - we could just display the GUID + timestamp,
  and drop the name entirely? Thoughts?" Coordinator: the name is the FILESYSTEM handle — the job
  directory (jobs.py:404), the worktree directory and branch `local-agent/<name>` (runner.py:79-80),
  and `prune` decides age from the stamp in the directory name with no database (jobs.py:30-37,
  by design: a prune that works when the database is gone). The id is the API handle (every MCP
  tool, every page link). Dropping the name means UUID directories and branches in the operator's
  shell, and prune reading the database — a cross-cutting change for a cosmetic gain. On screen
  the smaller move is available: show the 8-hex tail (the UUID's first 8, the same as the name's
  tail) with the started column, full name and id in the title. Operator decides; not scheduled.
  Operator: "Not entirely - the files have actual timestamps for created, so using the name as a
  proxy for a timestamp is a anti-pattern. No human is going to rummage into those directories I
  think - good luck to them under any naming pattern. What you've created is essentially two
  PK's. Prune doesn't need the database because it has the file create times. I argue it's not
  a cosmetic change, it's a 'load bearing' change as you would put it. Thoughts?" Coordinator:
  conceded — two unique keys on one row, the stamp a proxy for data the row and the filesystem
  carry; prune by the directory's newest mtime is "nothing written for N days", the age prune
  wants (trap: an outside touch rejuvenates a directory — docstring). If scheduled: 3.10f, the
  UUID as directory and branch name, `name` kept on old rows and shown when present else
  `id[:8]` with the started column, prune ageing both shapes by mtime, `dropped_job`/`job_name`
  → ids, seed and specs; DeepSeek, after the table stories; its own review (schema-shaped).
  **3.10f-1 LANDED c768dfd (+8ad16e6):** DeepSeek/dgx1-a job 20261030-221127-469b4b83, 220 turns,
  32 min, 25 files; 746 tests on main after one fix (a hand-built `Job(name=…)` in
  tests/test_1_14.py, landed in between). `_v9` is the first filesystem migration step (the
  runner already took callables — `_v6` was one): renames terminal rows' `jobs/<name>` →
  `jobs/<id>` (a live job's directory is left to its server and logged; OSError logged, never
  raised; SQLite ≥ 3.35 guarded), then drops the index and the column. `Job` loses `name`;
  worktrees and branches are `local-agent/<id>`; prune ages UUID-named and old-stamp
  directories alike by the newest mtime of the directory and its children; `state_dir` lives in
  db (derive and read import it there; jobs re-exports). read.py keeps the JSON keys `name`,
  `dropped_job`, `job_name` carrying the id and `job_detail`'s `name` — 3.10f-2 removes them
  (read.py:245, 334, 632-634, 734, 755, 820) — DONE, **3.10f-2 LANDED e86190b** (Qwen/ai1 job
  283af1bd-f874-4a7d-9e17-9a6c3120c396, 152 turns, 13 min, 762 tests; the first lane whose own
  record carries `server_version 0.2.0+85344a5`, `prefill_tps` and a UUID branch — the restarted
  server's code is live). One `shortId`/`jobLink` exported from app.js; the dropped-job line
  links straight off `dropped_job_id`; the job page header shows the full id. Lane's notes for
  folds: the §5a field gate is ONE-directional (emitted keys ⊆ spec) so a key removed from a
  route never fails it — a reverse check (spec-named keys ⊆ emitted) would have caught 3.10f-1's
  spec stragglers; endpoint.js `header()` carries a dead `hist` parameter; `wide.js`/`keep.js`
  hand-render a 200-char job NAME the page no longer draws, so their README floor numbers (946)
  are stale — re-measure with 3.10g. Lane's notes: `_v9` renames while holding BEGIN
  IMMEDIATE (a slow state dir lengthens the write lock); kept worktrees under the old names are
  not renamed (the row's `worktree` JSON holds the path). Review of c768dfd + 8ad16e6 + a4252ce
  (1.15) by Qwen/ai2 job f3e11207-6cee-4208-9272-f9fa18dac5d0, 85 turns, 20 min, four probes,
  eleven probe tests: NO finding. Reproduced: renames byte-identical with nested files; the
  running row left with its stderr line; a gone directory skipped; a COLLISION logs and leaves
  both directories (the old one an orphan prune ages by shape), the column still drops; a second
  open changes nothing; a read-only `jobs/` logs EACCES and the column drops; 1.15 by the
  migration route — a fresh file opens at 9 without naming the state dir, a v8 row needing a
  rename refuses and ROLLS THE FILE BACK WHOLE to 8 (repeatable; a later redirected open
  completes); the guard raises on state_dir/db_path/open_db/prune(1)/JobStore.new, merely returns
  for prune(0) and for db_path with LOCALAGENTS_DB set; prune ages both shapes, keeps a
  today-child, leaves `notes` and a UUID-named FILE, the CLI prints the same two; branch and
  tree are the uuid; no `.name` read on a Job survives in src/. Observations folded by the
  coordinator (999881d): O1 `_v9` called `state_dir()` per row (now once, lazily); O4 `_UUID_DIR`
  admitted any 36 hex-or-dash characters (now a structural uuid). O5 cosmetic: server.py:813's
  `if log_dir else None` unreachable. Chain closed. **Until its review is clean, do not
  restart the operator's monitor or MCP server: the first process on this code migrates the real
  database and renames the real job directories.** — OVERTAKEN, see the incident below.
  **Incident, 2026-10-30 22:44 (the second of the day, same family as 3.9B's):** the REAL database
  was migrated to v9 and 78 real job directories renamed at 22:44:16 — eleven minutes before
  3.10f-1 landed on main (c768dfd ~22:55) and 29 s after the DeepSeek lane's job ended (22:43:47).
  The only code with `_v9` then was that lane's worktree; both running processes (the MCP server
  21:29, the monitor 21:31) are older code and could not have done it. The lane's tool_calls show
  nothing aimed at the real path (last call 22:42:49), so the route is most likely a test or probe
  SUBPROCESS that lost the redirect in its environment — unidentified from the record. Operator:
  "Known hazard - multiple worktrees, but one database." Effects: the migration did what it was
  designed to do (the running job's directory and three orphans untouched; nothing lost); the two
  running processes now fail against v9 — `run_agent` answers "table jobs has no column named
  name" (two dispatches refused at 23:05) and the old-code monitor cannot read jobs — both need a
  restart onto main, which reads v9. Lesson for a ruling: the state-dir redirect in conftests is a
  convention a subprocess can drop; the guard belongs in the code — see 1.15.
- ☑ **1.15 A worktree never opens the real state directory.** (Coordinator, a4252ce, 752 tests, built while no lane could run — the MCP server was down on the old code until the operator reconnected it at ~23:15. `db.state_dir()` with no `XDG_STATE_HOME` raises when `db.PACKAGE_FILE` has a `worktrees` path component (a whole component, not a substring); `_v9` computes the jobs root only when a row needs renaming, so a fresh file from a lane never reaches it; tools/smoke/README.md's recipe exports both variables. Security-shaped coordinator code: rides the next cross-model review (the 3.10f-1 review brief names it).) From the 22:44 incident and 3.9B:
  `db.open_db()`/`state_dir()` with the DEFAULT path (no `LOCALAGENTS_DB`, no `XDG_STATE_HOME`)
  refuse with a clear error when the running package's own checkout is under a `worktrees`
  directory (`Path(__file__)` contains `/worktrees/`, the lanes' and `.claude/worktrees/`'s
  shape) — a lane can only ever touch a redirected state dir, whatever its environment; the
  operator's install (the main checkout) is unaffected. Plus: a filesystem migration step
  (`_v9` and any later one) logs the state dir it is about to touch and refuses the default path
  from a worktree by the same rule. Tests: the guard under a fake `__file__`; the real suite's
  conftests still redirect. Small; Qwen; before any other lane runs. Review with the batch.
  Operator: "I don't think I'm wrong on this one - and it saves one DB column too and some logic.
  … my experience says this is exactly the kind of thing that matters later - data integrity and
  clarity." (And ruling (n) above.) SCHEDULED, and FIRST — before 3.10b/c, so the first column
  changes once. Split for the lanes: **3.10f-1 the package side** (DeepSeek): schema v9 drops
  `jobs_name` and the `name` column (SQLite DROP COLUMN; the migration first renames each
  existing `jobs/<name>` directory to `jobs/<id>` for rows whose status is terminal, best effort,
  leaving live jobs' directories alone and logging what it skipped); `JobStore.new` mints no
  name — `log_dir = jobs/<id>` (jobs.py:399-410, `db.job_name` retired); the worktree directory
  and branch are the id (runner.py:79-80); `prune` ages ANY job directory (UUID-named or the
  old stamp shape) by its newest mtime, docstring naming the outside-touch trap (jobs.py:30-50);
  `derive.log_dir` from the id (derive.py:184); the status line, wait/resume messages and
  `list_jobs` rows show the id (server.py:174-220, 466, 717; status.py); smoke.py:198; the seed
  mints ids; tests. **3.10f-2 the monitor side** (after f-1 and 3.10d): read.py drops `name`,
  `dropped_job` (the id stays in `dropped_job_id`) and `job_name`; the pages show `id.slice(0,
  8)` with the full id in the title wherever a name was shown (app.js:373, 548, 655, 1241-1243,
  1529; endpoint.js:334, 385-389); spec §5a; the field-name gate; DOM smoke.
- **"tier 0: every endpoint"** (endpoint.js:240 `TIER_NAME`): the spec's tiers (§5 line 185)
  group the tiles by which BACKENDS provide them — tier 0 "every backend provides these", tier 1
  counters, tier 2 occupancy, tier 3 hardware. A taxonomy of data sources leaked onto the page as
  headings. Nobody looking at one endpoint needs it; the groupings become plain labels
  ("speeds", "occupancy", "hardware") or nothing, and the "endpoint: up" / "node: up" tiles go
  (the header already says it; the port-22 fallback note moves to the header's node phrase).
- **"events"** (endpoint.js `eventsSection`; collect.py:88): the collector writes an
  `endpoint_events` row on an up/down transition ("up", "down" with the probe's error) — a
  restart log. The real database holds 2 rows in 32 h. Useful once something flaps; empty tables
  are noise, so the section is drawn only when the range holds an event, under a plainer title.
- **Tables**: the overview's `jobsTable` (app.js:608) and the endpoint page's own 5-column jobs
  list (endpoint.js:329 `JOB_COLUMNS`, from the history body's `jobs`) are two renderers of the
  same thing; the requests tables already share one renderer (`M.requestsTable`, 3.7k) and the
  endpoint route caps at 1000 rows (read.py:740) while its totals sum the whole range. The
  endpoint page's jobs come from `/api/endpoints/<name>/history` (name, id, status, started,
  finished, model only — not the overview's figures), so reuse needs the overview row shape for
  those jobs: a `jobs` list filtered by endpoint from the same reader the snapshot uses
  (read.py:~199, `_JOB_SELECT`), range-bounded, keyed by `jobs(endpoint_id)`.

Stories (lanes; every one a DOM smoke over the seeded world, cross-model review per batch):
- ☑ **3.10a Recent jobs: when, lost, prefill.** (Landed 39936f1: Qwen/ai1 job 20261030-215324-2dc084a5, 103 turns, 10 min; 720 tests. `derive.prefill_tps` a plain ratio (the docstring says why not an EMA); `timeTd` factored out of `requestsTable` for both tables; `Job.summary()` mirrors the key (test_summary_db pins the two summaries equal — a file the brief's list missed, the lane caught); the old " tok/s" suffix dropped since the headers carry the unit. Smoke: headers job, status, started, model, endpoint, elapsed, turns, tool calls, tool errors, context, prefill tok/s, decode tok/s, last activity; the seeded running job reads "lost" because the seed stores the seed process's own pid as its server (seed.py:366 — worth knowing). Lane's notes: `test_finished_rows_derive_once_across_pushes` pins the derive count per row (6 → 7), every new per-row figure must update it; prefill figures are dominated by the small `ms − gen_ms` residual — the seeded running job reads 53,414 tok/s — the same exposure the request table has; a NULL `cache_creation` (older writes) blanks the cell.) A "started" column (the row's `started_at`,
  day-stamped as the request tables do it) first after the name; the "server gone" badge
  dropped, a live job with `server_live === false` drawn with the pill "lost"; "speed" becomes
  two columns "prefill tok/s" and "decode tok/s", the first from a new `derive.prefill_tps`
  (read.py adds `prefill_tps` to the snapshot row and the job detail; spec §5a). The compaction
  badge stays (it carries what the pill cannot).
- **Review of 3.10a + 3.10d** (dense Qwen3.8-27B on llamacpp, job 20261030-221551-e4442830, 96
  turns, 22 min, four probes): NO correctness or contract defect — `prefill_tps` 509.09 = the hand
  ratio with all five skip rules; lost/lost/done/running pills right; the started cell equals the
  request table's time cell at the same instant; 19 tile labels, headings speeds/occupancy/
  hardware, port-22 clause in the header, no overflow at 1400 and 760. Possible: a value that
  rounds to 0.0 is treated as present (unreachable, same as gen_tps); a 200-character served
  model widens its tile to 3,175 px and the page scrolls sideways — the grow-never-cut rule the
  operator chose; realistic ids do not reach it (operator may cap). Tests: all five new pins pass
  under a plausible wrong implementation and no committed smoke read those cells/tiles — the
  review's P2/P3 hand-renders are committed as `tools/smoke/jobsrows.js` and `tiles.js` (the
  fold-F3 pattern). Noticed: cdp.mjs's `tiles`/`tileLabels` read the hidden overview cards (a
  footgun, twice now); `fmt.speed` vs `fmt.compact` easy to conflate. Chain closed.
- ☐ **3.10g The one-column stretch.** Operator (2026-10-30 22:40, a 1712-px window, screenshot):
  "Hmm. A window that is just not quite wide enough degrades the render in an interesting and bad
  way." What the screenshot shows: one column (the floor × 2 plus the gap exceeds the window), every
  card stretched to ~1680 px, the twelve fixed tiles spread across it with the sparklines stretched
  between, the state strip full width. Two causes, two fixes: (1) the floor counts the job lines
  (`.ep-act` activity, `.ep-job-figs`), the longest and least structural texts on the card — if
  those scroll barless when wider than the card (the 3.7u-fix mechanism already in place for
  capped lines) and only the head and the tile grid set the floor, the floor on the live page
  drops from ~900 to ~600 and two columns fit at 1400; (2) a card never stretches past ~1.5× the
  floor (`max-width` on `.host`, the grid packing left), so a lone column leaves space instead of
  spreading fixed tiles. Operator decides the first (it changes the 3.7u trade "fewer cards, more
  complete information": the two job lines would show complete on hover/scroll, not at rest).
  **(2) LANDED bc7b46e** (Qwen/ai2 job 0e0de45b-899f-4c86-b9d2-92469a796bdd, 46 turns, 11 min;
  784 tests): `.host { max-width: calc(var(--card-floor) * 1.5) }`, released under the narrow
  query; the 1fr track and the 3.7u pin stand (a clamped grid item packs left on its own).
  Smoke: 2200 → 2 columns at 1078; 1712 → 1 at 1419 (the cap); 1400 → 1353; 1000 → 953; 760 →
  the window; 1940/1990 → 2 (the cap never steals the second); `floorIsText` at every step.
  Lane's note for (1): on the SEEDED world the floor-bearer is the down endpoint's `.ep-error`
  line (898 → 946), the job lines second (786) — the "~900 → ~600 if the job lines leave the
  floor" estimate is live-page-specific; headless Chromium's 15-px scrollbar shifts exact-width
  maths. (1) still awaits the operator.
- ☐ **3.10h "prompt = 1": the request table's prompt column, and the shim's first-byte clock.**
  Operator (2026-10-30 23:00, a job page's request table, screenshot): "Weird - prompt = 1? I hope
  that's due to transient state?" Not transient. Two things: (1) the column shows the API's
  `input_tokens` — the uncached, un-created tail of the prompt. On a hot prefix cache
  (Strata/llama.cpp re-evaluate the last token) that is 1 on every row; the whole prompt is
  `input + cache_read + cache_creation` (the screenshot: 1 + 16,384 + ~3,000). Truthful, useless
  as "prompt". (2) the prefill t/s column (and 3.10a's job-level prefill) is
  `(input + cache_creation) / (ms − gen_ms)`, where the shim stamps `gen_ms` from the FIRST BYTE
  of the stream (shim.py:318-328 `t_first` on the first chunk) — and the first byte is the
  `message_start` event, which the backends emit BEFORE prompt processing. So `ms − gen_ms` is
  network latency, not prefill: 3,000 new tokens "in 19 ms" = 159,775 t/s (physically impossible;
  the one cache-miss row in the screenshot, 67,420 tokens in 6.8 s, shows the real rate near
  10k t/s). Design: (a) the shim stamps `t_first` at the first chunk carrying generated content
  (`content_block_start`/`content_block_delta`), so `gen_ms` is decode time and `ms − gen_ms` is
  time-to-first-token (queue + prefill), the honest prefill denominator; a test over a fake stream
  (message_start at 0, the first delta at 0.5 s: `gen_ms` excludes the 0.5 s); the spec's
  `gen_ms` sentence gets its True-now. (b) the request table: "prompt" becomes the whole prompt
  (input + cache_read + cache_creation), "reused" stays the cache_read share, a "new" column is
  input + cache_creation (what was processed), prefill t/s = new / ttft; the job page and endpoint
  page share the renderer so both follow; the schema spec's derived-columns prose (monitor
  spec ~:477, which already explains "input − reused") gets its True-now. Lanes: (a) DeepSeek now
  (shim only); (b) after 3.10f-2 lands (it edits `requestsTable`'s job column).
  **(a) LANDED a70e08b** (DeepSeek/dgx1-a job 18518895-ba79-422f-8d0f-c20a53532c7a, 36 turns,
  11 min; 766 tests on main): `t_first` on the first chunk containing `content_block_start` or
  `content_block_delta`; a stream with events but no content records `gen_ms = 0` (its whole
  span is queue + prefill — the 0-output cache-miss row's case; decode t/s stays blank because
  its rule needs gen_ms > 0); a stream with no chunks keeps None. Consequence the lane named:
  `derive.gen_tps` reads gen_ms as the decode span, so the status line's decode rate rises to
  the honest figure too (it used to include prefill). Lane's notes: the byte search is
  per-chunk — a marker split across two reads sets the clock one chunk late (the usage parsing
  carries the same assumption; SSE events arrive whole from these backends); a backend that
  emitted `content_block_start` eagerly before generating would move the clock back toward the
  first byte (none does today); `head` is capped at 64 KiB; tests/test_shim.py's `_relay_and_row`
  never consumed the stream, so streaming rows were recorded only when something drained it.
  Review by the dense Qwen3.8-27B on llamacpp, job 4675bbc6-b869-4aee-b215-b82aa61459f6, 65 turns,
  23 min, 19 probe tests: the oracle holds everywhere — a marker split across reads starts the
  clock one read late (the gap charged to TTFT, conservative), a marker in a `data:` line only is
  caught, a batched message_start+content_block_start chunk starts the clock on arrival (right:
  the batch is the observable TTFT); 0-vs-None reads the accumulated head; the real `prefillRate`
  and `decodeRate` run over the rows behave as specified; `gen_tps` rises to the honest decode
  rate; `prefill_tps` counts a gen_ms-0 row's whole span. F1 (pre-existing, real):
  `_usage_from_sse_tail` scans only the LAST chunk (shim.py:328 `last = chunk`; :392-399), so a
  backend whose `message_stop` lands as its own read makes the row record `output_tokens = 1`
  (the message_start placeholder) — reproduced: decoded 57, recorded 1 — feeding usage totals,
  decode t/s and `derive.gen_tps` (a no-content row's placeholder drags the EMA). P2
  (pre-existing): a backend that dies mid-stream is recorded as a clean 200 with no error (the
  exception reaches the client; the row says nothing). P1 the split marker (disclosed). P3 a
  sub-millisecond decode reads 0. Disposition: F1 and P2 folded by the reviewer (ruling (a));
  P1/P3 accepted boundaries. **FOLD LANDED 17b3278** (the same dense Qwen, job
  e4f49aee-6fb3-4c58-8d29-4d8e869e1892, 57 turns, 10 min; 790 tests): `tail = (tail +
  chunk)[-TAIL_BYTES:]` (8 KiB) and the LAST `message_delta` usage in it wins (a stop_reason-only
  delta skipped; `HEAD_BYTES`/`TAIL_BYTES` constants); the generator catches `Exception` (in this
  httpx `ReadError` is a TransportError, not a StreamError — the named-types list in the brief was
  wrong, the lane caught it), records `error = "stream ended early: <type>: <msg>"[:300]` with
  the header's status and the usage it had, re-raises; `GeneratorExit`/`CancelledError` pass
  through (a client walk-away is not a backend failure). Lane's notes: a truncated-200 row writes
  no `[backend]` transcript line (`_log` logs at ≥ 400 only — the row is the record); two error
  shapes now coexist in `error` (800 bytes of a 4xx/5xx body vs the 300-char sentence); a death
  AFTER content now keeps the real decoded count. Review of the fold by Qwen/ai2 job
  197b338e-57ea-4c79-9346-a414537804eb, 71 turns, 20 min, three probes: window-front split skip,
  last-delta-wins, head+tail merge over an 82 KiB stream, all four death/walk-away shapes (a
  death before any byte: error text, status 200, gen_ms None), the 300-char cut, no transcript
  line; the clock shapes re-run on an independent harness; a MUTATED `TAIL_BYTES = 64` fails the
  five window tests and none of the clock tests — the pins bind behaviour. One filed finding, F1:
  more than 8 KiB after the final delta loses the count to the placeholder — the documented bound,
  accepted (no Anthropic-shaped backend sends anything after its final delta but message_stop);
  the fix-shape if ever: parse deltas in the loop and keep the last usage, no window. Possible: a
  GRACEFUL truncation (a clean EOF with no message_stop and no exception) records a clean row.
  Noticed: `head`'s cap is soft (overshoots by one chunk — harmless); the fold's docstring
  promised the usage-less-delta skip and no test pinned it — the reviewer's probe p1c2 is lifted
  into tests/test_3_10h_fold.py by the coordinator. Chain closed. **(b) LANDED 66be3d3** (Qwen/ai1 job
  5ce88d5d-b1c9-4bd0-b37d-d9380b12e1d6, 78 turns, 9 min; 782 tests): columns path, prompt (the
  whole prompt, three parts in the title), new (input + cache creation), reused, output, prefill
  t/s, decode t/s, duration, error; `promptTotal`/`newTokens` named helpers; a missing part is 0
  when another is present, none → blank; the handshake branch untouched (handshake.js d1.ok).
  Smoke {1, 16384, 3000} → 19,385 / 3,001 / 16,384, prefill 6,002.0, decode 133.3. Spec §5a's
  column list corrected, not only appended. Lane's notes: the job and endpoint totals lines sum
  the bare `input` as "tokens in" (the same small number, now under an honest label — 3.11a may
  want the whole prompt there); the job header's METRICS column "prompt" is the `prompt_tps` RATE,
  the last bare "prompt" word on the pages; cdp.mjs computes `reqHeaders`/`reqRows`/`reqPills`
  only onto the AFTER console line, not AFTER_JSON (a lane reading the JSON misses them).
- ☑ **3.10b One jobs table, one requests table.** (Landed f0d423d: Qwen/ai1 job 7956ea64-5fc3-44c8-bbdb-42117d1a44cb, 115 turns, 16 min; 776 tests. `endpoint_history`'s jobs are the §5a rows of this endpoint's jobs overlapping the range — the overlap predicate factored out of `jobs_overlapping` (`_OVERLAP_WHERE`), so a queued never-started job no longer appears (consistent with "jobs that ran"; pinned); not joined to `_FINISHED_CACHE` (its eviction is keyed to the overview's tail); plans SEARCH `jobs_endpoint_id`. `jobsTable(rows, opts)` with empty/note/omit/now, cells built by column name; endpoint.js's own list gone; headings "Jobs · ai1 · last 1h" / "Requests · …" / "Restarts and outages · …" only when the range holds an event (the seed has three events; at 15m the section is absent). Lane's notes: `fleet_history` calls `endpoint_history` per endpoint and now pays ~7 derive statements per overlapping job for a `jobs` list it never reads — a `jobs=False` option is the fix — coordinator's check: the batched overview route and the endpoint page share one cached body per endpoint and span (app.py:319-323, 423), so the rows are derived once per endpoint per refresh, not twice; measured on the seeded world: 2 jobs → 23 statements, ~10 ms at every range; ~7 statements per overlapping job, all by `job_id` index. Left as is; the flag if a profile ever shows it; the seeded running job wears "lost" in every `--no-collect` smoke (the seed's heartbeat is stale against the live clock); the R1 oracle's jobs half compares two empty lists.) `jobsTable(rows, opts)` becomes the renderer
  for every jobs list: the overview (as now), the endpoint page (`opts.filter` = the endpoint;
  the title "Jobs" with the filter and range stated once: "Jobs · ai1 · last 24h"), and later
  any page; the endpoint history body's `jobs` gains the overview row's figures (the shared
  reader, filtered by `endpoint_id`), so the rows are the same shape. `requestsTable` stays the
  one renderer; the endpoint page's title loses "our jobs only" chatter for "Requests · ai1 ·
  last 24h". Events: drawn only when non-empty, titled "Restarts and outages".
- ☐ **3.10c Table paging, sorting, column filters.** One ES5 table helper used by both
  renderers: client-side over the rows already fetched (the endpoint route's 1000-row cap stays;
  the summary line keeps counting the whole range), page size 50 with a pager line ("1–50 of
  889"), click-to-sort on every column header (numeric columns numeric, the current sort shown
  with an arrow), and a filter row under the header with one text box per column (substring,
  case-insensitive; numeric columns accept `>n` / `<n`); the page keeps a table's sort, filter
  and page across pushes (keyed by table id), so a refresh never drops the operator's view.
  Operator asked "Pagination with local column filters and sorting?" — recommended as above;
  operator (2026-10-30): "3.10c - yes, client side filter." Decided: client-side over the
  fetched rows, as written. Sequence: after 3.10a (columns) and 3.10b (one renderer each), so
  the helper is built once under both tables. **LANDED f817d6e (+18efad4)** (Qwen/ai1 job
  f5930fda-bb5c-4d99-8c7f-571c21267928, 81 turns, 18 min; 798 tests): `dataTable(id, columns,
  rows, opts)`, `TABLE_STATE` keyed by table id, `TABLE_PAGE_SIZE` 50; both renderers are callers
  with their cells unchanged (no earlier pin moved). Lane's decisions: the brief's requests id
  test was inverted (`opts.job === false` is the job page — caught); filter text is the cell's
  text plus its title (a pasted UUID matches the short-id cell; "ok" matches its numeric code);
  the sort is type-driven (two finite numbers compare numerically, so the context percent sorts
  numerically; missing figures sort last); the caret survives the redraw; a filter resets the
  page, a sort does not, a push clamps. Smoke `tools/smoke/tables.js`: "1–50 of 120", turns
  asc/desc with aria-sort, "done" → 40, ">100" → 7, next → "51–100", a push keeps
  `{sort: turns desc, filters, page 2}`; both detail pages' request tables sort and filter.
  Coordinator fold at landing (18efad4): the lane keyed both pages' jobs tables to one id "jobs"
  (a filter on the endpoint page would have filtered the overview); `opts.id`, the endpoint
  page passes "jobs-endpoint". Lane's notes: `cellText` builds a td per row per column per
  filter keystroke (O(rows × cols) DOM work — fine at 1000 rows, a 3.11a candidate); cdp.mjs's
  header reads are safe only because the arrow is CSS `::after`; `renders.full` counts card
  rebuilds only.
- ☑ **3.10d Endpoint page labels.** (Landed 9c41b21: Qwen/ai2 job 20261030-215334-3293e876, 93 turns, 20 min, one compaction; 723 tests. Tiers kept as order and border colours, headings plain nouns with none over the first group; the state/node tiles gone, the port-22 answer in the header's host clause; tiles a flex-wrap row with `flex: 1 0 auto; min-width: 170px`, `.tile-value` nowrap without ellipsis — a 120-char served model widened its tile to 1118 px, overflow 0, 17 sparklines drawn. Lane's corrections: cdp.mjs's `tiles`/`tileLabels` read the OVERVIEW's hidden cards, not the endpoint page (the brief was wrong; its own `t/fit.js` read the tiles); the page-test literal scan reads quoted prose in comments too, so a quoted phrase in a comment is de facto pinned.) The tier headings and classes go; "endpoint"/"node" tiles
  go (the header carries both and the fallback note); tiles grouped under "speeds",
  "occupancy", "hardware" with no numbering; "served model" and every tile value follow the
  3.7u rule (no ellipsis: the tile grows, the grid floors on it) — app.css:193 `.tile-value`
  loses `text-overflow: ellipsis`; "Jobs on it" → 3.10b's title; "Don't be chatty": every
  heading on the page is a noun phrase with the filter, no parentheses of explanation.
- ☐ **3.10e Job page tables.** Its requests table is already the shared renderer; its per-tool
  breakdown stays (it is a different thing); the helper of 3.10c applies to its requests table
  with the job as the fixed filter; the "errors" header word pair from 3.7v stays.

- ☐ **3.10i The vLLM lane shows no speeds and no cache.** Operator (2026-10-31 00:45): "I notice
  the vllm lane never reports token speeds, always shows the prompt as new not cached." The rows
  (read-only, dgx1-a's last six `/v1/messages`): `input_tokens` 56-63k (the whole prompt),
  `output_tokens` 0, `cache_read`/`cache_creation` NULL, `gen_ms` ≈ ms − 1 s (the running server
  predates the clock fix). So decode t/s is blank because output is 0 and prefill t/s is blank
  because `prefillRate` demands a numeric `cache_creation` (app.js); "new" = input because the
  cache fields are absent — honest about the API's silence, wrong about the engine. Coordinator's
  live probe of the backend (an 8-token streamed request to dgx1-a, 00:50): nine chunks —
  `message_start` (usage: input_tokens 38, output_tokens 0; NO cache fields), content events,
  then `message_delta` with `usage: {input_tokens: 38, output_tokens: 8}` as chunk 8 and
  `message_stop` ALONE as chunk 9. Two causes, two answers: (1) output 0 is the F1 case the shim
  fold (17b3278) fixed — the running server (85344a5) reads only the last chunk, finds no usage
  in `message_stop`, keeps the placeholder; after the next `/mcp` reconnect vLLM rows carry their
  output and decode t/s appears. (2) vLLM's Anthropic shape reports no prefix-cache fields at
  all (its OpenAI shape has `prompt_tokens_details.cached_tokens`; Claude Code speaks Anthropic),
  so cache_read/cache_creation are NULL for every vLLM row: "reused" blank is honest; "new" =
  input is the API's whole truth; but `prefillRate` (app.js) and `derive.prefill_tps` demand a
  numeric `cache_creation` and so compute nothing. Fix (a story after 3.10c lands — it wraps
  `requestsTable`): a missing cache_creation counts as 0 when input is a number, in both the page
  rule and `derive.prefill_tps`, so vLLM rows and jobs get prefill t/s = input / TTFT (an upper
  bound on the processed tokens, since the engine's own prefix cache is invisible — the cell's
  title says "cache not reported by this backend"); the endpoint-level cache-hit tile already
  comes from vLLM's /metrics (registry.py:585), which is the only place vLLM tells us.
  Operator: "I know for llama.cpp I add --metrics and --slots to enrich the data it provides -
  something similar I can add to vllm later?" Yes: `vllm serve … --enable-prompt-tokens-details`.
  vLLM's Anthropic serving (`_build_anthropic_usage`, docs.vllm.ai api/vllm/entrypoints/anthropic/
  serving) derives `cache_read_input_tokens` from `prompt_tokens_details.cached_tokens` and
  OMITS the cache fields when that detail is off — the default; with it on, `input_tokens`
  becomes prompt minus the cached part, the Anthropic way. Open upstream issues #33923 and #45079
  (vLLM 0.22.1rc1) say the fields are still missing in some builds even so — verify after the
  flag with the same 8-token probe. /metrics (prefix-cache hit counters, the tile) is on by
  default. Our side stays: a missing cache field counts as 0 for the prefill rate, for any
  backend that stays silent. Operator: "something I won't change immediately on the server side -
  getting vllm running is a bit of production since it's almost fully utilizing all three DGX
  spark nodes." The flag waits for vLLM's next natural restart; our fix carries the lane
  meanwhile.

### 3.11 Pre-close passes (operator 2026-10-31 00:15)

> Two passes to schedule pre-close for this sequence - one pass by opus over the codebase, code
> cleanliness review, duplicated code, opportunities for risk reduction. Second pass by any
> model over the automated tests - looking for code sycophants, tests that scope too narrow and
> would miss actual useage patterns, dead tests, etc.

Sequence: after 3.10c and the review of the page batch land, before the retro.
- ☐ **3.11a Codebase pass (Opus, read-only audit → folds).** Every module under src/: cleanliness
  (dead code, stale comments that narrate retired stories, names that lie), duplicated code (the
  two hand-copies of the outside-2xx rule the fold review named; `timeTd`-style factorings not
  yet made; the three `_log_safe`s), and risk reduction (unguarded writes, paths that trust input,
  the `record_*` handlers DeepSeek noted, `finish_worktree`'s ignored return codes, shared
  mutable state across the fan-out). Deliverable: a table — finding, file:line, kind, the fix's
  size — then folds by Opus itself under the usual rule (test-first, a cross-model review of the
  folds). Not a rewrite: the operator's words are review and reduction.
- ☐ **3.11b Test-suite pass (any model; DeepSeek's window fits all of tests/).** Over every test
  file: "code sycophants" — tests that restate the implementation (the regex pins over page
  source are the known family; tests that assert a mock was called the way the code calls it;
  tests whose expected values were copied from the output), tests scoped too narrowly to catch
  real usage (a single-row fixture where the page sees fifty; a seeded world that never flips a
  state; a test that pins a fake's shape and not a captured one), dead tests (vacuous asserts,
  `new == old` over empty worlds like the R1 oracle's jobs half, skipped-forever markers,
  duplicates), and gaps the live incidents exposed (the 22:44 incident: nothing tested that a
  subprocess kept the redirect). Deliverable: a table per file — test, verdict (sycophant /
  too narrow / dead / sound), the usage pattern it misses, the replacement's shape — then the
  replacements built test-first by the reviewer, cross-model reviewed.

## Epic 4: operations (V2, not scheduled)

Open items the operator has named but not shaped (2026-10-30): database purge ("It does need to
be done, but what shape that takes is not yet determined"); an analytics path over the database
("TBD"). Neither is a story yet.

Facts for shaping them (coordinator, read-only over the real database, 2026-10-30 19:40, 32 h of
use, 89 jobs): file 17 MB + 5 MB WAL. Of it the monitor's probe tables are ~12.7 MB (probes 5.3,
probe_counters 3.1, probe_hardware 2.6, the (endpoint_id, t) index 1.6; 78,513 probe rows) and
are already pruned at `retention_days` = 7 (`monitor/retention.py`, per endpoint through the
index), so at this fleet size they level off near ~65 MB. The job side is ~4 MB for 89 jobs
(jobs 1.2, tool_calls 1.4 + 0.4 index, requests 0.5 + 0.4 index; 5,869 requests, 6,782 tool
calls): ~45 KB per job, plus ~70 KB per job of transcript once the server writes them (3.8b),
so ~115 KB per job — at fifty jobs a day about 6 MB a day, 2 GB a year, all behind indexes that
do not scan (3.9E). Nothing is urgent. Shapes on the table, for the operator to pick from or
reject: (P1) one rule, `monitor: job_retention_days` (0 = keep, the default), applied by the
collector beside `prune_probes` as `DELETE FROM jobs WHERE finished_at < ?` through the v7
`jobs_finished_at` index — `requests`, `tool_calls` and `transcripts` all declare
`ON DELETE CASCADE`, so the job row's deletion carries its children (one statement, keyed);
(P2) tiered: transcripts (the bulk) at a shorter horizon than the rows, which stay for
analytics — `DELETE FROM transcripts WHERE job_id IN (SELECT id FROM jobs WHERE finished_at < ?)`;
(P3) a `localagents prune --rows N` for the hand-run shape, beside the directory prune. For the
analytics path: the SQLite file IS a path — `sqlite3 "file:…?mode=ro"` or DuckDB's sqlite
attach read it live under WAL without touching the writers (that is how these numbers were
read); a `localagents export` to CSV/Parquet is the next shape if a tool wants files. The
per-job aggregates (`jobs` + `tool_calls` + `requests`) are the analytics rows; P2 keeps them.

- **Idea, not a story (operator 2026-10-30 23:50): jobs that survive an MCP server restart.**
  "Would be an interesting feature if it could be architected to survive that - that would
  require multi-process work I think - not for now, maybe never as it's a geeky feature." Shape
  if ever: the data already survives (rows, heartbeats, transcripts, the v8 copy at completion);
  what dies with the server process is the shim port the job's Claude Code session talks to and
  the SDK session itself. Surviving means a detached runner — a small daemon owning the jobs and
  the shim, the MCP server a thin client over the database and a local socket; a reconnect then
  re-attaches instead of orphaning. Not scheduled.
- ☐ **4.1 Guided SSH setup script.** Walks the operator through key placement per host; no
  secrets stored by localagents; verifies with one `ssh host true`.
- ☐ **4.2 Start/stop/restart recipes** per host in the `monitor:` section; actions from the
  endpoint detail page with confirmation.
- ☐ **4.3 Node telemetry** via periodic `nvidia-smi` over SSH into the tier-3 tiles for
  non-Strata backends.
