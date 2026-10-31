# 3.11b — the pre-close test-suite pass

Two phases in one lane: an audit over `tests/` (read-only, one row per test function), then the
safe replacements, test-first, each its own commit. The oracle for every verdict is the
operator's words (EPICS) and the spec (`docs/superpowers/specs/`), never the code.

Verdicts: **sound** · **sycophant** (restates the implementation) · **too narrow** · **dead**
(vacuous / unreachable / never-flips). For a page-source pin, "sound, smoke-covered" means a
committed `tools/smoke/` script drives the real DOM the pin names; otherwise the row names the
smallest wrong implementation that still passes the pin.

Suite shape (what a test gets for free):
- `tests/conftest.py` `fresh_server_store` (autouse): every test gets a fresh
  `XDG_STATE_HOME`/`LOCALAGENTS_DB`/`LOCALAGENTS_CONFIG` under `tmp_path`, and a fresh
  `server.store` (a new `JobStore`) + `_shim_port_recorded=False`. So a test that imports
  `localagents.server` never reads the developer's database; but it also means a test that
  re-imports `server` sees the SAME module-level `store` object the conftest just reset — a
  fixture that captures `server.store` at import time (before a test's `monkeypatch`) can hold a
  stale connection to the previous test's database. Ordering hazard, not a current failure.
- `t/conftest.py` `_fresh_state` (autouse, probes only): the same three env vars for probes
  under `t/` (outside `tests/`), added after the 3.9B probe forgot its fixture and `prune(1)`
  deleted the real jobs directory.
- Fixtures that can hide a failure: the `monitor_fixtures.serve`/`STRATA_AI2` doubles answer
  canned httpx routes; a test that asserts only what the double returns (not what the code under
  test computed from it) passes no matter what the code does to the response. The `world`
  fixture (seeded db + registry) is a double whose `seed.seed` writes a fixed clock — a read
  model test that compares against `seed._load(FX)` constants is comparing the code's output to
  a re-derivation from the same capture, not to an independently-stated expectation.

---

## tests/test_1_14.py — the build stamp

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_build_stamp_is_the_pyproject_version_plus_the_short_sha` | sound | — | — |
| `test_build_stamp_prefers_the_source_tree_over_stale_metadata` | sound (operator's case) | — | — |
| `test_build_stamp_without_a_source_tree_is_the_metadata_version` | sound | — | — |
| `test_build_stamp_without_metadata_or_pyproject_is_dev` | sound | — | — |
| `test_a_failing_git_leaves_the_version_alone` (6 params) | sound | — | — |
| `test_build_stamp_never_raises` | sound | — | — |
| `test_build_stamp_finds_this_checkout_by_default` | sound | — | — |
| `test_the_editable_checkout_is_read_from_direct_url` | sound | — | — |
| `test_the_server_row_version_is_the_build_stamp` | sound | — | — |
| `test_summary_and_summary_from_row_carry_the_same_server_version` | sound | — | — |
| `test_a_job_whose_server_row_is_gone_has_no_server_version` | sound | — | — |
| `test_job_status_and_list_jobs_carry_each_servers_own_stamp` | sound | — | — |
| `test_the_seeded_snapshot_carries_build_and_each_jobs_server_version` | sound | — | — |
| `test_the_app_stamps_itself_once_and_every_snapshot_carries_it` | sound | — | — |
| `test_job_js_reads_server_version_and_build_and_says_the_note` | **sound, smoke-covered** — `tools/smoke/stamp.js` drives the real "server" figure and the muted note over the seeded world | the note's *comparison* (`snap.build !== detail.server_version`) is a source pin; a wrong comparison that still emits the note text is not caught here, but stamp.js reads the drawn note | — (smoke-driven) |

## tests/test_1_15.py — a worktree never opens the real state directory

The 22:44 incident's guard. All four tests monkeypatch `db.PACKAGE_FILE` and call `db.state_dir()`
**in the pytest process**. The operator's named gap: is the guard tested through a **subprocess**
route — the CLI, `uv run python -c` — where a lane *drops* the conftest's redirect?

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_a_worktree_checkout_refuses_the_default_state_directory` (2 params) | **too narrow** — in-process only; the 22:44 incident was a *subprocess* that dropped the redirect | a subprocess (`localagents` CLI, `uv run python -c`) running from a worktree with no `XDG_STATE_HOME` is never exercised; the guard's reach through a real route is untested | a subprocess test: spawn the CLI / `python -c` from this worktree with `XDG_STATE_HOME`+`LOCALAGENTS_DB` unset, assert the refusal (the guard fires in the child, not the test process) |
| `test_a_redirected_worktree_checkout_works_as_before` (2 params) | sound | — | — |
| `test_the_main_checkout_uses_the_default_state_directory` | sound | — | — |
| `test_the_guard_reads_the_path_components_not_a_substring` | sound | — | — |

## tests/test_isolation.py

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_every_path_the_code_resolves_is_under_tmp` | sound (the suite-shape net) | — | — |

## tests/test_3_10b.py — one jobs table, one requests table

Read-shape half (seeded world) is sound; the B/C half is the static-pin family.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_history_jobs_rows_have_the_snapshot_job_row_key_set` | sound (compares live against the snapshot's own keys) | — | — |
| `test_history_job_rows_answer_field_for_field_like_the_snapshot` | sound | — | — |
| `test_history_jobs_are_ordered_live_first_then_newest_finished` | sound | — | — |
| `test_history_jobs_are_the_endpoint_own_overlapping_the_range` | sound (excludes other-ep / too-old / never-started) | — | — |
| `test_history_job_queries_seek_jobs_endpoint_id` | sound (EXPLAIN pin, 3.9E's standing audit) | — | — |
| `test_history_and_timeline_share_the_overlap_predicate` | sound, but the `src.count("_OVERLAP_WHERE") >= 4` clause is a **sycophant** source pin (a wrong impl that keeps the fragment name in 4 places but never uses it in a query passes that clause; the `SELECT` below is the real witness) | a query that embeds the fragment's text but filters on the wrong window terms | the `SELECT j.id … WHERE <frag>` half already answers the window (j1, j3); keep it, the count clause is redundant weight |
| `test_app_js_jobs_table_takes_rows_and_opts` | **sycophant** — pins `function jobsTable(rows, opts) {` + `opts.omit`/`empty`/`note` + the export. Smallest wrong impl: `jobsTable` exists with those option names but the endpoint page's `M.jobsTable` call is dropped (the page draws nothing) — the pin never sees the endpoint page's call site | the endpoint page drawing *through* the renderer (the operator's "re-use the code") is not driven | `tools/smoke/tables.js` drives the overview table; the endpoint page's draw-through is not smoke-covered → a `cdp.mjs` run over `#/endpoint/ai1` asserting the jobs table drew through `M.jobsTable` |
| `test_endpoint_js_draws_jobs_through_the_shared_renderer` | **sycophant** — pins `M.jobsTable(` + `omit: ["endpoint"]` in endpoint.js; no smoke reads the endpoint page's jobs table | the endpoint page's jobs table is never drawn in a committed smoke | same `cdp.mjs` over `#/endpoint/ai1` |
| `test_endpoint_page_headings_are_noun_phrases_with_the_filter` | **sycophant** — pins heading strings in source ("Jobs · ", "Requests · ", "Restarts and outages · ") and the absence of old strings; a heading the page never *renders* (a section that returns before drawing) still satisfies the pin | the headings as the operator *sees* them on the endpoint page | a `cdp.mjs` over `#/endpoint/ai1` reading the drawn heading text |
| `test_events_section_is_absent_when_the_range_holds_none` | **too narrow** — pins `if (!evs) return null;` in source, but the seeded world *does* hold events, so the null branch is never exercised against real data; a wrong impl that returns a non-null empty section passes the source pin and only a hand-render (which no committed smoke does for the empty case) would catch it | the empty-range events section over real data | a `cdp.mjs` hand-render of an event-less range asserting the section is absent |

## tests/test_3_10c.py — the shared data table (paging, sort, filter)

Ruling (o): the regex/substring pins are this file's convention; the committed DOM smoke
`tools/smoke/tables.js` is the witness for the table's behaviour.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_the_data_table_helper_exists_and_is_exported` | sound, smoke-covered (tables.js drives `dataTable`'s pager/sort/filter over a 120-job hand-render) | — | — |
| `test_the_state_is_a_module_map_keyed_by_the_table_id` | **sound, smoke-covered** — tables.js part (d) drives survival across a push through `TABLE_STATE.jobs`; the exact default-entry text is a source pin but the behaviour is smoke-driven | — | — |
| `test_the_page_size_is_named_once_as_a_constant` | **sound, smoke-covered** — tables.js reads "1–50 of 120"; the `count("var TABLE_PAGE_SIZE = 50;") == 1` clause is a redundant source pin on top of the smoke | — | — |
| `test_the_renderers_draw_through_the_helper_with_stable_ids` | **sycophant** — pins the `dataTable(opts.id || "jobs"` call and the `id: "jobs-endpoint"` in endpoint.js; no committed smoke reads the endpoint page's table id or filters it apart from the overview's | a filter typed on the endpoint page never filtering the overview (the two-table-id rule) is never driven | a `cdp.mjs` over `#/endpoint/ai1` + overview asserting a filter on one page does not filter the other |
| `test_the_sort_is_a_click_on_the_header_with_aria_sort_and_a_reversible_direction` | sound, smoke-covered (tables.js part (b) drives the sort and `aria-sort` on a real table) | — | — |
| `test_the_filter_grammar_substring_and_the_numeric_comparators` | sound, smoke-covered (tables.js part (c) drives the `>n`/`<n` and substring filters on real cells) | — | — |
| `test_the_filter_row_is_inputs_one_per_column_built_from_the_state` | sound, smoke-covered (tables.js reads the filter inputs) | — | — |
| `test_app_css_styles_the_filter_rowthe_arrow_and_the_pager` | **sound** (CSS is a Python-visible artifact, no JS runtime; the arrow's DOM draw is smoke-covered by tables.js reading the sorted header) | — | — |

## tests/test_3_10f2.py — the job's one key is its UUID

Read-shape half (seeded world) is sound; the page-source half is the pin family.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_the_snapshot_job_rows_carry_no_name_key` | sound | — | — |
| `test_history_jobs_rows_carry_no_name_key` | sound | — | — |
| `test_timeline_job_rows_carry_no_name_key` | sound (the timeline row's exact key set) | — | — |
| `test_endpoint_entry_keeps_dropped_job_id_and_drops_the_name` | sound | — | — |
| `test_the_endpoint_entry_keys_are_named_without_dropped_job` | sound (the full key set — a re-added key fails) | — | — |
| `test_requests_rows_carry_job_id_not_job_name` | sound | — | — |
| `test_job_detail_has_no_name_key` | sound | — | — |
| `test_no_static_script_reads_a_dropped_name_key` | sound (a banned-name scan is a real gate: a reintroduced `job.name` read fails it) | — | — |
| `test_shortid_is_defined_and_exported` | **sound, smoke-covered** — the short id as drawn is the `jobsrows.js`/`tables.js` job-link draw; the export pin is a redundant source check | — | — |
| `test_joblink_draws_the_short_id_with_the_full_id_as_title` | **sycophant** — pins `jobLink(id)`'s body source (`title: id`, `shortId(id)`) and the absence of a two-arg call; a `jobLink` that draws the short id but titles *nothing* (or the wrong id) still passes if the two source strings are present in the function body | the drawn link (short id text + full-id title) on a real job row | `tools/smoke/jobsrows.js` reads the job link's cell; the *title* (full id) is not smoke-read → a `cdp.mjs` assert on the link's `title` attribute |

## tests/test_3_10g.py — a lone column never stretches a card

Static CSS pins; the widths are the browser smoke's (floor.mjs over 2200,1712,1400,1000,760).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_a_card_never_grows_past_one_and_a_half_floors` | **sound, smoke-covered** — floor.mjs drives the floor/columns across the width sequence; the `.host` max-width is the rule that rule reads | — | — |
| `test_the_narrow_query_still_draws_one_full_width_column` | **sound** (the 760-px floor.mjs step drives the single-column + released cap) | — | — |

## tests/test_3_10hb.py — the request table's prompt columns tell the truth

Entirely exact-expression pins over `app.js` (ruling (o)). The arithmetic (`promptTotal`,
`newTokens`) is never read through a committed smoke: `tables.js` reads the requests table's
filter/sort, not the prompt/new/reused *cell values*; `jobsrows.js` reads the overview's jobs
cells. So the operator's "prompt = 1?" defect — the *rendered* prompt cell — has no DOM witness.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_the_request_columns_are_the_design_list_in_order` | **sycophant** — pins the `columns.concat([...])` order in source; a wrong column order that the page never draws (a section that errors before the table) still passes | the columns as drawn in the request table | a `cdp.mjs` over `#/job/<id>` reading the requests table's header cells in order |
| `test_req_numeric_marks_new` | **sycophant** — pins `"new"` inside `REQ_NUMERIC`; a numeric-mark on the wrong column passes if the string is present | — (the numeric sort on "new" is not smoke-read) | fold into the column-order smoke |
| `test_prompt_total_sums_the_three_parts_in_a_named_helper` | **sycophant** — pins the exact `return (input||0)+(cache_read||0)+(cache_creation||0)` expression and the all-missing→null guard in source; a helper that computes this but is never wired to the cell, or a cell that shows a wrong value, passes | the operator's "prompt = 1?" — the *rendered* prompt cell value | a `cdp.mjs` over `#/job/<id>` asserting the prompt cell = input+cache_read+cache_creation for a known row |
| `test_new_tokens_is_the_prefill_numerator_in_a_named_helper` | **sycophant** — same family; the "new" cell's rendered value is never smoke-read | the rendered "new" cell | fold into the request-table smoke |
| `test_the_cells_draw_the_helpers_and_the_prompt_title_names_the_three_parts` | **sycophant** — pins `var prompt = promptTotal(req)` and the title-parts push calls in source; the drawn cell value and title are never read | the cell value and its hover title | a `cdp.mjs` reading the prompt cell's text and `title` |
| `test_the_two_headers_carry_explaining_titles` | **sycophant** — pins the two header title strings in source | the drawn header titles | fold into the column-order smoke |

## tests/monitor_seeded.py (support) and tests/monitor_fixtures.py (support)

Not test files, but the audit needs their shape. `monitor_seeded.seed_file` writes a world at a
fixed `NOW` through `seed.seed`. The seeded world is the shared double for the read-model and
page tests. Its **too-narrow** character (the operator's "a seeded world that never flips a
state"): the seed produces exactly three endpoint states (ai1 live, dgx1-a down, llamacpp
dormant) and two jobs (one running, one succeeded) on ai1. States the seed cannot produce — a
down node whose endpoint is never seen, a job with a lost server (the running job's server is
always live in the seed), a `/api/hello` handshake row, a vLLM-shaped row with no cache fields —
must be hand-rendered by a committed smoke (nodes.js, neverseen.js, handshake.js, jobsrows.js)
because the seeded `/api/snapshot` never carries them. `test_monitor_read` and `test_3_10b`
compel the seed's two jobs onto ai1, so the seeded jobs log never shows a lost-server row: that
is the usage pattern the seed misses and the operator's "seed grows the states the operator has
seen live" note names. See the `seed.py` row in `test_monitor_seed.py`.

`monitor_fixtures.serve`/`STRATA_AI2` (the story-2.1 captured strata documents) are the
parser/collect doubles: a test over a *typed* string instead of the captured line is a
sycophant, a test over `STRATA_AI2` is sound.

## tests/test_3_10h.py — the shim's generation clock

Behaviour-driven: drives `shim.relay` over an SSE `_SseStream` fake at timed steps and checks
the **recorded row's** `gen_ms`/`ms`/`gen_ms` is None. The fake is a double whose *shape* is the
Anthropic SSE protocol (the captured event names), not a re-derivation of the code.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_gen_ms_starts_at_the_first_generated_token` | sound (a 0.5 s prefill then decode: `ms − gen_ms ≥ 450` is the operator's honest prefill span) | — | — |
| `test_no_content_event_records_zero_decode` | sound (`gen_ms == 0`, not None, so the page's prefill rule uses the row) | — | — |
| `test_non_streaming_reply_keeps_a_null_gen_ms` | sound | — | — |
| `test_a_stream_with_no_chunks_records_no_gen_ms` | sound (no chunk is "no evidence it started", not "decoded nothing") | — | — |

## tests/test_3_10h_fold.py — the usage tail scan (F1/P2)

Behaviour-driven over the same SSE harness; each test reproduces a named probe (d6, d2, p1c2).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_message_delta_not_last_read_keeps_the_final_count` | sound (57, not the message_start placeholder 1) | — | — |
| `test_message_delta_split_across_two_reads` | sound (the reassembled event is seen whole) | — | — |
| `test_two_message_deltas_in_the_tail_the_last_usage_wins` | sound (the LATER figure) | — | — |
| `test_delta_and_stop_in_one_read_unchanged` | sound (regression guard for the one-chunk case) | — | — |
| `test_backend_dies_after_message_start_records_the_error` | sound (the 'stream ended early' error, the 200 header, `gen_ms == 0`, re-raise) | — | — |
| `test_a_clean_stream_records_no_error` | sound (error NULL on a clean end) | — | — |
| `test_a_final_message_delta_without_usage_does_not_hide_the_count` | sound (a no-usage tail delta must not replace the earlier count — the probe p1c2 lifted) | — | — |

## tests/test_3_10i.py — a backend with no cache fields still gets a prefill rate

The derive half (real job rows) is sound; the app.js half is the pin family.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_prefill_tps_null_cache_counts_as_zero` | sound (the vLLM shape: `input` alone over the span, an upper bound) | — | — |
| `test_prefill_tps_still_skips_a_null_input` | sound (the relaxation is cache_creation alone) | — | — |
| `test_prefill_tps_cache_reported_rows_unchanged` | sound (the Strata rows keep their exact figure) | — | — |
| `test_prefill_tps_mixed_job_sums_both_shapes` | sound (one Strata + one vLLM row join) | — | — |
| `test_prefill_rate_reads_cache_creation_as_zero_when_absent` | **sycophant** — pins the exact `var cc = typeof …cache_creation === "number" ? … : 0;` line and the absence of the old null-out in source; a `prefillRate` that has this line but is never called by the cell, or a cell that shows a wrong value, passes | the prefill cell's rendered figure on a vLLM-shaped row | `tools/smoke/jobsrows.js` renders a "null and a 1234.5 prefill" cell; the vLLM-shape (no cache fields) prefill figure is not smoke-read → a `cdp.mjs` hand-render of a vLLM-shaped request row asserting the prefill cell |
| `test_the_prefill_cell_title_names_the_numerator_or_the_missing_cache` | **sycophant** — pins the two title sentences and the `typeof cacheCreation !== "number" && typeof cacheRead !== "number"` test in source; the drawn title is never read | the cell's hover title over a no-cache backend | fold into the prefill-cell smoke (read the cell's `title`) |

## tests/test_agent_file.py — the relay subagent's frontmatter

Pins the relay file's config. The file is the product (a broken frontmatter silently turns the
relay into a full-tool agent on the main model — the v0.1.4 bug), so the exact-value pins are a
real contract, not a sycophant. The `wait_s` pins are behaviour gates (a job must not be capped
mid-run — the v0.1.5 bug).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_frontmatter_parses_with_every_field` | sound (the file parses; a broken frontmatter fails it — the v0.1.4 bug) | the frontmatter's *effect* (that Claude Code actually keeps only name/description on a broken parse) is not driven; the pin checks the file, not the parser's tolerance | a note: the tolerance is Claude Code's, not this package's — out of scope to drive here |
| `test_relay_gets_only_the_job_tools` | sound (the relay gets exactly the job tools, nothing else) | — | — |
| `test_relay_waits_long_enough_not_to_cap_long_jobs` | sound (the `wait_s >= 900` and `maxTurns × wait_s >= 24 h` gates — the v0.1.5 cap) | — | — |

## tests/test_api_key.py — a per-endpoint key is sent on every backend call

Behaviour-driven: `_Recorder` patches `httpx.AsyncClient.send` and records every outgoing
request's auth headers; the no-leak tests assert the key never reaches the delegated session's
env or `list_models`.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_endpoint_without_key_sends_the_local_placeholder_pair` | sound | — | — |
| `test_endpoint_with_key_sends_it_in_both_header_forms` | sound (x-api-key AND Bearer) | — | — |
| `test_registry_loads_api_key_from_yaml` | sound | — | — |
| `test_probe_sends_the_key_on_every_discovery_call` | sound (models/props/slots/metrics all carry the key) | — | — |
| `test_fetch_metrics_sends_the_key` | sound | — | — |
| `test_local_complete_sends_the_key` | sound | — | — |
| `test_local_complete_without_key_keeps_the_placeholder` | sound | — | — |
| `test_shim_replaces_the_sessions_placeholder_with_the_endpoint_key` | sound (the relay's outgoing request carries the key) | — | — |
| `test_shim_forwards_the_placeholder_when_the_endpoint_has_no_key` | sound | — | — |
| `test_session_env_never_carries_the_real_key` | sound (the no-leak half — the operator's "never handed to the session") | — | — |
| `test_list_models_never_shows_the_key` | sound (the no-leak half — `list_models`) | — | — |

## tests/test_database_error_paths.py — every tool answers database_error

Behaviour-driven: `DROP TABLE tool_calls` (or a newer-schema file) on the live connection; each
tool is called in-process exactly as MCP calls it and its contract dict is checked. The P2 guard
(`test_a_healthy_database_still_answers_unknown_job`) is the anti-over-broadening check.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_wait_job_answers_database_error_when_its_summary_cannot_be_read` | sound (and that the wait still reports progress while the derive fails) | — | — |
| `test_run_agent_answers_database_error_when_its_final_summary_cannot_be_read` | sound | — | — |
| `test_wait_jobs_answers_database_error_when_a_done_summary_cannot_be_read` | sound (the whole call answers database_error, not a partial record) | — | — |
| `test_cancel_job_answers_database_error_when_its_summary_cannot_be_read` | sound | — | — |
| `test_run_agent_answers_database_error_on_a_newer_schema_file` | sound (a fresh store's `SchemaError` is a guard, not a traceback) | — | — |
| `test_a_healthy_database_still_answers_unknown_job` | sound (the new guards must not turn a real unknown_job into database_error) | — | — |

## tests/test_db.py — the SQLite schema module

The strongest file in the suite: real migration steps, real **multi-process** races (four
`subprocess.Popen` writers, 25-file fresh-open race, first-sight lookup race), real CHECK/NOT
NULL/foreign-key failures, and the session_pid walk against faked `/proc`. The migration
`test_a_version_N_file_migrates_to_version_N+1_with_its_rows_intact` family drives a hand-built
older file through `open_db` and checks the rows survive.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_fresh_file_comes_up_at_the_current_version_with_every_table` | sound (every table, index and column in spec order) | — | — |
| `test_monitor_tables_carry_the_spec_types_keys_and_indexes` | sound (types, NOT NULL, the probe_id FK, the two indexes) | — | — |
| `test_endpoint_events_kind_check_accepts_up_down_restarted_only` | sound (the CHECK and the FK failure) | — | — |
| `test_opening_the_same_file_twice_is_idempotent` | sound | — | — |
| `test_a_newer_version_than_the_code_knows_raises` | sound | — | — |
| `test_journal_mode_is_wal` | sound | — | — |
| `test_a_version_1_file_opens_without_rerunning_ddl` | sound (a boom in MIGRATIONS[0] proves the step is skipped) | — | — |
| `test_a_version_1_file_migrates_to_version_2_with_its_rows_intact` | sound (the v2 ALTER, compactions 0) | — | — |
| `test_a_version_2_file_migrates_to_version_3_with_its_job_intact` | sound | — | — |
| `test_a_version_3_file_migrates_to_version_4_with_its_probe_rows_intact` | sound (the v4 appends are NULL on the old rows) | — | — |
| `test_a_version_4_file_migrates_to_version_5_with_its_rows_intact` | sound | — | — |
| `test_a_version_5_file_migrates_to_version_6_with_a_null_witness` | sound (v6 is one ALTER; the monitor backfills, not the step) | — | — |
| `test_a_version_6_file_migrates_to_version_7_with_its_job_rows_intact` | sound (the v7 index on finished_at) | — | — |
| `test_a_version_7_file_migrates_to_version_8_with_the_transcripts_table` | sound (the v8 table + the cascade) | — | — |
| `test_host_probes_answer_check_accepts_the_three_answers_and_nothing_else` | sound | — | — |
| `test_foreign_keys_are_enforced` | sound | — | — |
| `test_status_check_accepts_the_seven_values_and_nothing_else` | sound | — | — |
| `test_isolation_check_accepts_none_and_worktree_only` | sound | — | — |
| `test_db_path_honours_localagents_db_over_xdg_state_home` | sound (LOCALAGENTS_DB wins; the state-dir file is not created) | — | — |
| `test_lookup_id_is_stable_and_keyed_by_every_column` | sound (idempotent, one row, every key column matters) | — | — |
| `test_lookup_id_writes_first_seen_from_its_reader_clock` | sound (the seed's deterministic clock) | — | — |
| `test_lookup_id_refuses_a_partial_key_an_unknown_table_and_a_null` | sound | — | — |
| `test_new_uuid_is_uuid4_text` | sound | — | — |
| `test_four_processes_writing_at_once_lose_nothing` | **sound** — the multi-process write-race (four real `Popen` writers, 400 jobs, no lost row). This is the real process-level witness the suite needs and it exists here | — | — |
| `test_processes_opening_fresh_files_at_once_migrate_each_exactly_once` | sound (25 fresh files, 8 processes, each at the current version) | — | — |
| `test_processes_racing_first_sight_lookups_converge_on_one_row_each` | sound (200 distinct paths, no dup) | — | — |
| `test_a_version_8_file_drops_name_and_renames_terminal_job_directories` | sound (the v9 directory rename, the live-job skip, the idempotent second open) | — | — |
| `test_a_version_8_file_with_no_job_directories_still_comes_up_at_9` | sound (a pruned directory is not an error) | — | — |
| `test_the_version_9_rename_is_idempotent` | sound | — | — |
| `test_session_pid_walks_to_the_claude_ancestor` | sound (python → uv → claude) | — | — |
| `test_session_pid_finds_an_npm_installed_claude_running_as_node` | sound (the npm shim's entry) | — | — |
| `test_session_pid_finds_modern_node_whose_comm_carries_the_thread_name` | sound (node-MainThread) | — | — |
| `test_session_pid_ignores_claude_appearing_later_in_a_node_command_line` | sound (a `--name claude` flag is not the session) | — | — |
| `test_session_pid_ignores_a_node_running_something_else` | sound | — | — |
| `test_my_owner_agrees_with_the_walk_on_a_node_session` | sound | — | — |
| `test_session_pid_is_none_below_init_or_without_proc` | sound | — | — |

## tests/test_derive.py — the read-time derivations

Behaviour-driven: records real request/tool rows and checks the derive functions' **computed**
values against independently-stated figures (the docstrings show the arithmetic). The
`test_gen_tps_replays_note_usage_ema` EMA and the `test_files_touched_first_seen_order_dedup`
dedup are the real behaviour.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_ctx_tokens_picks_latest_nonzero_prompt` | sound (a trailing zero/null usage must not erase the figure) | — | — |
| `test_ctx_tokens_none_when_nothing_recorded` | sound | — | — |
| `test_gen_tps_replays_note_usage_ema` | sound (the EMA over t-order, the skipped rows) | — | — |
| `test_gen_tps_none_when_no_usable_rows` | sound | — | — |
| `test_prefill_tps_ratios_processed_prompt_over_prefill_span` | sound (the row rule: all four fields numbers, ms > gen_ms, processed > 0) | — | — |
| `test_prefill_tps_none_when_no_usable_rows` | sound | — | — |
| `test_files_touched_first_seen_order_dedup` | sound (first-seen order, dedup, only edit tools, a path-less edit is out) | — | — |
| `test_tool_call_count` | sound (every ToolUseBlock counts, path or not; per job) | — | — |

## tests/test_fixture_manifest.py — the capture manifest's integrity

The anti-sycophancy mechanism for the parser fixtures: every capture is listed, its byte count
matches, its parser runs over the file's bytes, and the **digest pins the parser's VALUES** (not
a count) so a truncated fixture or a same-length value swap still fails. The sidecar
Content-Length checks the captured body against the curl `-D` headers.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_every_fixture_is_listed_and_parses` | sound (no orphan file, no orphan row; the parser runs and returns non-empty) | — | — |
| `test_each_row_pins_what_its_parser_returns` | sound (the digest pins the values) | — | — |
| `test_a_value_change_moves_the_digest` | sound (a same-length value swap fails the digest — construction D) | — | — |
| `test_every_http_row_has_its_headers_sidecar` | sound | — | — |
| `test_missing_sidecar_is_caught` | sound (a deleted sidecar fails on a copied tree) | — | — |
| `test_wrong_content_length_is_caught` | sound | — | — |
| `test_error_row_without_path_is_a_problem` | sound (a failed capture with no file reports a gap, not a KeyError) | — | — |
| `test_content_length_comes_from_the_last_header_block` | sound (a redirect's first block is not the captured body) | — | — |

## tests/test_monitor_config.py — the monitor: section of models.yaml

Behaviour-driven over the loader: a full section, the defaults, an unknown key, and the
value-validation gates (a quoted number, an out-of-range port, a float retention_days, a
non-string db). The operator's named bugs are the tests: a quoted `attention_hours: "24"`
(string repetition killing collect.run), a zero window deleting the down-and-recent state.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_a_full_section` | sound | — | — |
| `test_missing_keys_are_the_defaults` | sound | — | — |
| `test_no_section_is_all_defaults` | sound | — | — |
| `test_a_missing_file_is_all_defaults` | sound | — | — |
| `test_an_unknown_key_names_it` | sound | — | — |
| `test_quoted_attention_hours_is_a_config_error` | sound (the string-repetition bug) | — | — |
| `test_attention_hours_must_be_positive` | sound (a zero window deletes the alert state) | — | — |
| `test_a_fractional_attention_hours_loads` | sound | — | — |
| `test_quoted_port_is_a_config_error` | sound | — | — |
| `test_port_must_be_in_range` | sound | — | — |
| `test_retention_days_must_be_an_int` | sound | — | — |
| `test_db_must_be_a_string_or_null` | sound | — | — |
| `test_refresh_s_loads_and_defaults_to_five_seconds` | sound | — | — |
| `test_refresh_s_must_be_a_positive_number_within_an_hour` | sound | — | — |
| `test_a_valid_section_still_loads` | sound (the validation must not touch valid values) | — | — |
| `test_registry_load_ignores_the_monitor_section` | sound (the MCP server reads only endpoints/defaults — spec §3) | — | — |

## tests/test_monitor_es5.py — the page scripts are ES5

The gate: a real `let`/`const`/arrow/template in a page script fails it. The parametrised
`test_the_gate_reads_through_string_literals` tests the gate's own reader against constructed
strings (appropriate: it is the gate's regex, not the page, that is under test). The
`test_the_page_script_parses` runs `node --check` on each static script — the floor under every
page story (the 3.10i stray-`}` incident it names).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_the_page_scripts_are_es5` | sound (a real non-ES5 construct in a page script fails it) | a regex-literal-with-a-quote still pairs with the next string's quote (the R1 limit, documented) — a `const` hidden behind such a literal escapes | a parser would close R1; a `node --check` over a constructed file with a quote-in-regex + a hidden const would pin the limit's current bounds |
| `test_the_gate_reads_through_string_literals` (12 params) | sound (the gate's own reader, over constructed strings — the correct target here) | — | — |
| `test_the_page_script_parses` (per script) | sound (`node --check` is the parse floor; skipped, never silently green, where node is absent) | — | — |

## tests/test_monitor_jobs.py — `running_job_at` over a real database

Behaviour-driven: a real temp database with three hand-seeded jobs (finished-before-T, running
across T, lost-with-finished-after-T); the read-only query is checked against all three.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_the_running_job_is_returned` | sound (started ≤ T, finished NULL; the finished-before is not named) | — | — |
| `test_no_job_running_yields_none` | sound (a time before anything started) | — | — |
| `test_a_lost_job_finished_after_t_still_counts` | sound (a lost job whose finished_at is after T was still running) | — | — |

## tests/test_monitor_boundary.py — the MCP server never depends on the monitor

A real subprocess in a fresh interpreter: import the six server modules and assert no
`localagents.monitor` module loaded. This is the suite's existing subprocess pattern (the same
shape the 1.15 gap needs).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_importing_the_mcp_server_loads_no_monitor_module` | sound (the import-isolation witness, in a fresh interpreter) | — | — |

## tests/test_monitor_ranges.py — the time-range grammar

Behaviour-driven over the parser: the offered list in order, other in-bounds values, the
out-of-bounds and malformed rejections each naming the text, a non-string rejection.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_every_offered_range_parses` (5 params) | sound | — | — |
| `test_ranges_is_exactly_the_offered_list_in_order` | sound | — | — |
| `test_other_values_inside_the_bounds_parse` (5 params) | sound | — | — |
| `test_out_of_bounds_is_rejected_naming_the_text` (4 params) | sound | — | — |
| `test_malformed_is_rejected_naming_the_text` (11 params) | sound | — | — |
| `test_a_non_string_is_rejected` | sound | — | — |

## tests/test_monitor_rates.py — rates between consecutive counters rows

The anti-sycophancy done right: the rates are over the **captured** story-2.1 counter pairs,
and every expected figure is **computed by hand in the docstring** (not copied from the code).
The reset cases (a llama.cpp restart, a `since` change with growing counters, a NULL series)
are the real behaviour.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_the_manifest_gives_139_seconds_between_passes` | sound (the dt over the four capture pairs) | — | — |
| `test_ai2_pair_rates_by_hand` | sound (the hand figures: 4654/139, 2584/139, 484756/139, 0.822) | — | — |
| `test_dgx1a_pair_rates_by_hand` | sound (the vLLM hand figures, 0.481) | — | — |
| `test_llamacpp_pair_is_a_reset_with_no_rates` | sound (a restart: every counter 0, the reason names the rounded figures) | — | — |
| `test_reset_reason_rounds_the_figures_it_names` | sound (the event-list sentence rounds) | — | — |
| `test_ai1_idle_pair_rates_are_zero_not_none` | sound (idle: 0.0 rates, no acceptance value) | — | — |
| `test_since_changing_with_counters_that_only_grew_is_a_reset` | sound | — | — |
| `test_unknown_since_on_either_side_is_not_a_reset` | sound | — | — |
| `test_no_previous_counters_or_no_interval_gives_no_rates` | sound | — | — |
| `test_a_counter_absent_on_one_side_is_not_a_reset` | sound (a NULL series is not a backwards jump) | — | — |

## tests/test_monitor_node.py — the node state and the port-22 fallback

The node-state **truth table** (a pure function over last probes) plus the port-22 connector
over **real 127.0.0.1 sockets** (a listening port → answered, a bound-and-closed port →
refused, a non-routable address → nothing) and a faked connector (a reset → refused, a timeout
→ nothing, an unresolvable name → the gaierror propagates).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_a_live_endpoint_makes_the_node_up` | sound | — | — |
| `test_a_refusal_proves_the_box_is_up` | sound | — | — |
| `test_a_read_timeout_node_is_up_without_the_fallback` | sound (the stalled box pays no port-22 ask) | — | — |
| `test_every_endpoint_unreachable_is_down` | sound | — | — |
| `test_all_timeouts_with_an_answered_fallback_is_up` | sound | — | — |
| `test_all_timeouts_with_a_refused_fallback_is_up` | sound | — | — |
| `test_all_timeouts_with_a_nothing_fallback_is_down` | sound | — | — |
| `test_all_timeouts_without_a_fallback_is_unknown` | sound | — | — |
| `test_a_mixed_down_and_unknown_node_is_unknown` | sound | — | — |
| `test_refused_and_down_is_up` | sound | — | — |
| `test_a_host_with_no_endpoints_is_unknown` | sound | — | — |
| `test_every_endpoint_dormant_collapses_the_node` | sound | — | — |
| `test_one_live_endpoint_keeps_the_node_expanded` | sound | — | — |
| `test_one_down_recent_endpoint_keeps_the_node_expanded` | sound | — | — |
| `test_no_endpoint_states_is_vacuously_dormant` | sound | — | — |
| `test_a_listening_port_answers` | sound (a real `asyncio.start_server`) | — | — |
| `test_a_closed_port_on_localhost_refuses` | sound (the kernel's own refusal) | — | — |
| `test_a_non_routable_address_yields_nothing` | sound | — | — |
| `test_a_reset_from_the_connector_is_refused` | sound (a reset is an answer — the box is up) | — | — |
| `test_a_timeout_from_the_connector_is_nothing` | sound | — | — |
| `test_an_unresolvable_name_raises_instead_of_answering_nothing` | sound (a wrong name is not "nothing") | — | — |

## tests/test_monitor_parsers.py — the metrics parsers over the captures

The parser tests over the **captured** story-2.1 documents (the operator's "a parser test over
a typed string instead of a captured line is sycophant" — these are the captured line). The
hand-read figures are in the docstrings. The constructed mutations (a deleted key, a busy state,
garbage) are the real behaviour.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_fetch_metrics_doc_returns_strata_json_as_a_dict` | sound | — | — |
| `test_fetch_metrics_doc_returns_prometheus_text_verbatim` | sound | — | — |
| `test_fetch_metrics_doc_is_none_on_404_connect_error_and_bad_json` | sound | — | — |
| `test_fetch_metrics_is_the_parsed_doc` (3 params) | sound | — | — |
| `test_fetch_metrics_keeps_none_for_a_document_with_no_counters` | sound | — | — |
| `test_vllm_preemptions_counter_is_parsed` | sound | — | — |
| `test_metrics_delta_over_the_vllm_pair_is_unchanged_by_preemptions` | sound (the hand delta, the new key changes nothing) | — | — |
| `test_parse_strata_metrics_omits_a_counter_the_totals_do_not_carry` | sound (a missing key is absent, never a 0.0) | — | — |
| `test_a_strata_document_without_drafts_has_no_spec_keys_in_the_delta_or_the_job_row` | sound (no KeyError, the job row reads None) | — | — |
| `test_parse_strata_extra_reads_since_speed_hardware_and_state_from_ai2_pass_2` | sound (the hand hardware figures) | — | — |
| `test_parse_strata_extra_speed_from_ai2_pass_1` | sound | — | — |
| `test_parse_strata_extra_on_a_capture_without_hardware` | sound (the pre-hardware-section capture) | — | — |
| `test_parse_strata_extra_running_when_live_state_is_not_idle` | sound (the busy case: a mutated field, a missing state) | — | — |
| `test_parse_strata_extra_hardware_missing_keys_are_none_and_bools_are_not_numbers` | sound | — | — |
| `test_parse_strata_extra_on_garbage_is_all_none` | sound | — | — |

## tests/test_monitor_seed.py — `localagents seed`

Item 1 (the seed itself) is sound: the per-table row counts, the served-model on UP probes, the
refusal gate, force-replace, the collector-trace refusal (a probe 10 s old is refused, 600 s
replaced), determinism in `now`, and the CLI paths (counts + path, the live-server refusal, a
bad `--now` creating no file). Item 2 runs the state functions, the rates, the read-time
derivations, the status line and retention over the seeded file — all behaviour-driven.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_seed_counts_match_the_world` | sound (the hand-computed per-table counts) | — | — |
| `test_up_probes_carry_their_served_model_like_the_collector_does` | sound (the 3.2a row-3 fix: every endpoint's UP probes carry the served id) | — | — |
| `test_second_seed_refuses_without_force` | sound | — | — |
| `test_force_replaces_rather_than_appends` | sound | — | — |
| `test_seed_refuses_a_database_a_collector_wrote_to` (2 params) | sound (a live collector's fresh probe is refused, a sentence naming the age) | — | — |
| `test_seed_replaces_a_database_a_collector_left_long_ago` | sound (600 s is stale, `force` replaces) | — | — |
| `test_seed_is_deterministic_in_now` | sound (two files byte-identical apart from the uuid/rowid columns) | — | — |
| `test_summary_from_row_elapsed_is_read_on_the_readers_clock` | sound (the F9 fix: the clock the reader passes, not the wall) | — | — |
| `test_endpoint_states_are_live_down_dormant` | sound | — | — |
| `test_node_states_are_all_up` | sound (a refusal proves the box is up) | — | — |
| `test_node_dormant_flags_only_the_dormant_host` | sound | — | — |
| `test_probes_carry_state_and_the_disk_columns` | sound (3.1a ruling A in the seed) | — | — |
| `test_the_running_job_is_the_one_on_ai1` | sound | — | — |
| `test_ai1_rates_are_constant_and_non_zero` | sound (the hand rate over each segment, the reset's rounded detail) | — | — |
| `test_ai1_has_exactly_one_reset_and_it_is_the_restarted_event` | sound | — | — |
| `test_every_event_sits_at_the_probe_that_would_have_written_it` | sound (ruling D) | — | — |
| `test_the_running_job_has_ai1_context` | sound (the F10 fix) | — | — |
| `test_the_finished_job_carrries_its_usage_and_files` | sound | — | — |
| `test_statusline_lists_the_running_job_under_ai1` | sound | — | — |
| `test_retention_deletes_only_the_week_old_llamacpp_probe` | sound | — | — |
| `test_seed_cli_seeds_and_prints_counts_and_path` | sound (the CLI path, counts one per line, the path) | — | — |
| `test_force_refuses_while_a_live_server_owns_the_database` | sound (ruling E: `--force` must not delete a live server's rows) | — | — |
| `test_seed_cli_force_refuses_a_live_servers_database` | sound (the CLI path of ruling E: exit 2, the pid named) | — | — |
| `test_a_bad_now_exits_2_with_one_line_and_creates_no_file` | sound (a typo'd `--now` leaves no schema-only file) | — | — |
| `test_parse_now_passes_a_number_through` | sound | — | — |
| `test_parse_now_reads_a_naive_iso_string_in_local_time` | sound | — | — |
| `test_parse_now_none_is_the_wall_clock` | sound | — | — |
| `test_seed_cli_refuses_without_force_and_force_replaces` | sound | — | — |

**The seeded world itself is the operator's named too-narrow finding.** `seed.seed` produces
exactly three endpoint states (ai1 live, dgx1-a down, llamacpp dormant) and two jobs on ai1,
and the seed's own running job's server is always live. The states the operator has seen live —
a down node whose endpoint is **never seen**, a job with a **lost** server, a `/api/hello`
**handshake** row, a vLLM-shaped request row with **no cache fields** — are never in the
seeded `/api/snapshot`; they are hand-rendered by committed smokes (nodes.js, neverseen.js,
handshake.js, jobsrows.js) precisely because the seed cannot produce them. Every seeded-world
read test (`test_monitor_read`, `test_3_10b`) therefore only sees the three states and the
live-server job. Replacement shape (the operator's "make the seed grow the states the operator
has seen live"): grow `seed.seed` with a fourth endpoint (a never-seen endpoint on a down
node), a lost-server job, a handshake request row and a vLLM-shaped no-cache request row — a
product-adjacent change to the fixture, which ripples through `EXPECTED_COUNTS` and every
seeded-world assertion, so it is listed rather than built this lane.

## tests/test_monitor_collect.py — one collection tick and the loop

Behaviour-driven: drives the real `collect.tick`/`collect.run` over the real prober (a real
`Registry.probe` over the captured documents) and a `FakeRegistry` of canned probe dicts; the
up probe dicts are the real prober's output over the captures, the down probe is
`classify_connect_failure`'s own. The gap/restart cases (a down probe in the interval, a reset
during a gap, the all-or-nothing tick) and the node-fallback connector (the one-ask-per-host,
the unresolvable address) are the real behaviour.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_the_endpoint_row_is_the_one_jobstore_new_uses` | sound (the monitor and the server share one endpoints row) | — | — |
| `test_two_ticks_on_the_ai2_pair_write_two_probes_and_no_event` | sound | — | — |
| `test_two_ticks_on_the_llamacpp_pair_write_a_restarted_event` | sound | — | — |
| `test_the_tick_whose_counters_advanced_writes_the_witness` | sound (3.7-fold-2 D5) | — | — |
| `test_a_tick_whose_counters_did_not_advance_leaves_the_witness` | sound | — | — |
| `test_a_reset_interval_leaves_the_witness` | sound (a drop is not activity) | — | — |
| `test_since_changing_writes_a_restarted_event` | sound | — | — |
| `test_up_then_down_then_up_writes_down_with_the_sentence_then_up` | sound (a down probe does not ask /metrics) | — | — |
| `test_a_rate_whose_interval_spans_a_down_probe_has_no_rate_and_no_restarted_event` | sound (the gap: no rate, no restart; the next interval is the clean pair) | — | — |
| `test_a_reset_across_a_gap_writes_restarted_during_a_gap` | sound (a restart during an outage is still recorded) | — | — |
| `test_a_plain_gap_writes_no_restarted` | sound | — | — |
| `test_the_reset_sequence_still_writes_exactly_one_restarted` | sound | — | — |
| `test_a_tick_is_all_or_nothing` | sound (ruling E: a failed event rolls back the tick's probe row too) | — | — |
| `test_a_cold_start_down_writes_no_event` | sound | — | — |
| `test_the_loop_runs_two_ticks_for_two_endpoints_and_stops_on_the_event` | sound | — | — |
| `test_a_failing_tick_is_logged_and_the_loop_goes_on` | sound (ruling 4: one stderr line, the back-off, the loop ticks again) | — | — |
| `test_an_error_between_ticks_does_not_kill_the_loop` | sound (the brief's probe-3 reproduction: the TypeError is caught, the loop ticks) | — | — |
| `test_a_failed_tick_backs_off_once_then_recovers_the_fast_cadence` | sound | — | — |
| `test_run_passes_the_attention_window_to_the_cadence` | sound (item 5: the window flows into interval_s) | — | — |
| `test_on_tick_is_called_after_each_written_tick_and_its_error_is_one_line` | sound (3.1: the listener, a failed tick calls nothing, a raising listener is one line) | — | — |
| `test_two_timeouts_ask_the_fallback_once_and_a_fresh_row_silences_the_next_round` | sound (3.1b: one ask per host per minute, stored as a host_probes row) | — | — |
| `test_a_host_with_a_refusing_endpoint_never_asks_the_fallback` | sound (a refusal proves the box is up) | — | — |
| `test_a_host_with_one_timing_out_endpoint_gets_one_fallback_row` | sound | — | — |
| `test_a_failing_fallback_ask_is_one_line_per_ask_no_row_and_the_loop_goes_on` | sound | — | — |
| `test_an_unresolvable_fallback_address_fails_the_ask_loudly` | sound (ruling 3.1b: an unparseable address fails, the node stays unknown) | — | — |
| `test_three_endpoints_one_round_three_connects_one_row` | sound (the duplicate-connect bound: one host_probes row for the round) | — | — |

## tests/test_monitor_state.py — the cadence and the endpoint display state

The pure-function truth tables: the cadence (5 s live/recent, 60 s dormant, the boundary
included, the window a parameter) and the endpoint display state (live/down/dormant, the
boundary, a stale up probe is dormant not live, a sqlite3.Row is accepted).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_up_just_now_is_fast` | sound | — | — |
| `test_up_exactly_a_window_ago_is_still_fast` | sound (the boundary is fast) | — | — |
| `test_up_just_over_a_window_ago_is_dormant` | sound | — | — |
| `test_never_up_is_dormant` | sound | — | — |
| `test_the_window_is_a_parameter` | sound | — | — |
| `test_the_intervals_are_ints` | sound | — | — |
| `test_live_when_the_last_probe_is_up` | sound | — | — |
| `test_down_at_the_window_boundary` | sound | — | — |
| `test_dormant_just_over_the_window` | sound | — | — |
| `test_no_probe_at_all_is_dormant` | sound (a cold start is never alarmed) | — | — |
| `test_a_stale_up_probe_is_dormant_not_live` | sound (ruling 2.3 probe 1 D) | — | — |
| `test_a_stale_up_probe_and_the_cadence_agree` | sound (the two consumers of the same rows must not disagree) | — | — |
| `test_endpoint_state_window_is_a_parameter` | sound | — | — |
| `test_a_sqlite3_row_is_accepted_as_the_last_probe` | sound | — | — |

## tests/test_monitor_retention.py — probe retention

Behaviour-driven over real probe rows: the cutoff (old go, new stay, jobs untouched), the
EXPLAIN pins (each delete rides the `(endpoint_id, t)` / `(host, t)` index — the 2.2a review's
"bare `t < ?` scan" fix), the bounded discovery (the small `endpoints` table, never a monitor
table), and the performance bound (3 endpoints × 1000 old probes each stays under a second).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_rows_older_than_the_cutoff_go_newer_stay_and_jobs_are_untouched` | sound (children first, the FK check, jobs never touched) | — | — |
| `test_the_probes_delete_uses_the_endpoint_t_index` | sound (the EXPLAIN pin) | — | — |
| `test_the_events_delete_uses_the_endpoint_t_index` | sound | — | — |
| `test_the_host_probes_delete_uses_the_host_t_index` | sound | — | — |
| `test_discovery_is_bounded_to_the_endpoints_table` | sound (the bounded discovery) | — | — |
| `test_pruning_three_endpoints_of_a_thousand_old_probes_each_stays_under_a_second` | sound (the performance bound) | — | — |
| `test_zero_days_disables_pruning` | sound | — | — |
| `test_now_defaults_to_the_clock` | sound | — | — |
| `test_run_prunes_at_start_and_then_on_its_period` | sound | — | — |
| `test_run_defaults_to_an_hourly_seven_day_prune` | sound (the signature defaults) | — | — |

## tests/test_monitor_rows.py — one probe result becomes the rows

Behaviour-driven: `rows.probe_to_rows` over the **real prober's output** over the captured
documents (never typed — the docstring says so), with the hand-read figures. The constructed
cases (a document gauge beats the probe dict's rounded copy, a down probe's failure sentence,
the multi-model context, the absent-kv-series vLLM doc) are the real behaviour.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_strata_probe_and_ai2_doc_give_the_pinned_columns` | sound (the hand figures: context, slots, the counters, the hardware) | — | — |
| `test_vllm_probe_and_dgx1a_doc` | sound (the vLLM doc's exact kv_cache_usage, not the prober's rounded copy) | — | — |
| `test_llamacpp_probe_takes_slots_from_the_probe_dict` | sound | — | — |
| `test_a_document_gauge_beats_the_probe_dicts_rounded_copy` | sound (ruling A: the document wins; the copy is the fallback) | — | — |
| `test_an_up_probe_with_no_metrics_document_has_no_counters_row` | sound | — | — |
| `test_a_down_probe_is_a_row_with_the_failure_sentence_and_no_counters` | sound (the refusal sentence, the host, no counters) | — | — |
| `test_a_down_probe_keeps_the_host_class_classify_connect_failure_gives` | sound | — | — |
| `test_context_of_a_multi_model_endpoint_is_the_first_served_ids` | sound | — | — |
| `test_insert_round_trips_through_sqlite_row` (3 params) | sound (the insert round-trips under the caller's transaction, NULLs included) | — | — |
| `test_a_vllm_doc_without_the_kv_series_stores_null_not_the_probers_zero` | sound (2.2b ruling B: the absent series is NULL, not a stored zero) | — | — |

## tests/test_jobs_db.py — the jobs store over a real database

Behaviour-driven: every test runs `jobs.JobStore` over a real SQLite file (or a `monkeypatch`ed
refusal) and asserts the row. The `mark_lost_jobs` family (a stale heartbeat with a live pid,
a long server history, a corrupt pid, a dead pid, a stopped server, an unfinished worktree)
and the `record_transcript` family are the real behaviour; the negative-metrics NULL and
the `changed`-on-DB-error cases are the refusals the operator relies on.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_new_job_inserts_row` | sound | — | — |
| `test_new_leaves_no_orphan_directory_when_the_database_refuses` | sound (a refusal is cleaned up) | — | — |
| `test_new_leaves_no_directory_when_the_first_open_fails` | sound | — | — |
| `test_new_cleans_up_on_any_error_not_only_sqlite` | sound | — | — |
| `test_store_does_not_open_the_database_until_used` | sound | — | — |
| `test_new_row_carries_the_fields_known_at_creation` | sound | — | — |
| `test_lost_counts_as_done` | sound | — | — |
| `test_mcp_servers_row_at_first_use` | sound | — | — |
| `test_the_job_directory_is_named_by_the_id` | sound | — | — |
| `test_changed_tracks_status_transitions` | sound | — | — |
| `test_negative_metrics_store_null` | sound | — | — |
| `test_db_error_does_not_break_changed` | sound (a DB error degrades the line, not the call) | — | — |
| `test_lookup_rows_reused_across_jobs` | sound | — | — |
| `test_summary_reads_the_duplicated_values_from_rows` | sound | — | — |
| `test_summary_of_a_job_outside_the_store_has_empty_derived_fields` | sound | — | — |
| `test_heartbeat_advances_updated` | sound | — | — |
| `test_describe_server_updates_only_given_keys` | sound | — | — |
| `test_close_sets_stopped_and_is_idempotent` | sound | — | — |
| `test_close_on_an_unused_store_is_a_noop` | sound | — | — |
| `test_close_never_raises` | sound | — | — |
| `test_mark_lost_jobs_marks_other_servers_not_this_one` | sound | — | — |
| `test_mark_lost_jobs_marks_a_server_with_no_heartbeat_at_all` | sound | — | — |
| `test_mark_lost_jobs_spares_a_stale_heartbeat_with_a_live_pid` | sound (a live pid spares the server) | — | — |
| `test_mark_lost_jobs_sweeps_a_stale_pid_owned_by_another_user` | sound (a foreign pid is swept, not trusted) | — | — |
| `test_mark_lost_jobs_survives_a_long_server_history` | sound | — | — |
| `test_mark_lost_jobs_sweeps_a_corrupt_pid` | sound | — | — |
| `test_mark_lost_jobs_sweeps_a_non_positive_pid` | sound | — | — |
| `test_mark_lost_jobs_sweeps_a_dead_pid` | sound | — | — |
| `test_mark_lost_jobs_marks_a_stopped_server_despite_a_fresh_heartbeat` | sound | — | — |
| `test_mark_lost_jobs_notes_an_unfinished_worktree` | sound | — | — |
| `test_record_transcript_row_equals_the_file` | sound | — | — |
| `test_record_transcript_second_call_replaces` | sound | — | — |
| `test_record_transcript_of_a_missing_file_writes_nothing` | sound | — | — |
| `test_record_transcript_failure_is_one_db_error_line` | sound | — | — |
| `test_transcript_tail_reads_the_row_when_it_exists` | sound | — | — |
| `test_record_endpoint_load_writes_by_running_jobs_endpoint` | sound | — | — |

## tests/test_summary_db.py — the job summary and the wait protocol

Behaviour-driven over real rows: the usage totals (the four token columns), the tool counts,
the compaction boundary, the `summary` vs `summary_from_row` agreement, and the long
`wait_job`/`wait_jobs` family (a foreign row's timeout, a terminal row, a dead server's
`lost`, the in-process + foreign mix, a row deleted mid-wait, a failing derive read). The
`test_wait_jobs_reports_one_line_per_interval_over_every_id` and `test_a_row_deleted_mid_wait`
are the real progress-protocol behaviour.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_usage_totals_sum_the_four_token_columns` | sound | — | — |
| `test_tool_counts_groups_by_tool_with_error_sums` | sound | — | — |
| `test_last_tool_is_the_latest_row` | sound | — | — |
| `test_summary_from_row_reads_the_row_and_the_derivations` | sound | — | — |
| `test_compact_boundary_counts_the_compaction` | sound | — | — |
| `test_summary_from_row_without_metrics_or_worktree` | sound | — | — |
| `test_summary_and_summary_from_row_agree_for_the_same_job` | sound (the two paths must agree) | — | — |
| `test_summary_hands_out_a_copy_of_the_worktree` | sound | — | — |
| `test_job_status_reads_a_foreign_jobs_row` | sound | — | — |
| `test_cancel_and_resume_refuse_a_foreign_job` | sound | — | — |
| `test_wait_job_polls_a_foreign_row_until_the_timeout` | sound | — | — |
| `test_wait_job_returns_when_a_foreign_row_turns_terminal` | sound | — | — |
| `test_wait_job_resolves_a_dead_servers_job_as_lost` | sound | — | — |
| `test_foreign_wait_reports_the_same_progress_line` | sound | — | — |
| `test_wait_jobs_mixes_in_process_and_foreign_ids` | sound | — | — |
| `test_wait_jobs_any_returns_when_the_foreign_job_finishes_first` | sound | — | — |
| `test_wait_jobs_reports_one_line_per_interval_over_every_id` | sound | — | — |
| `test_a_failing_row_read_degrades_the_wait_jobs_line_not_its_count` | sound | — | — |
| `test_wait_jobs_all_done_when_both_foreign_jobs_finish` | sound | — | — |
| `test_list_jobs_all_reads_every_servers_jobs` | sound | — | — |
| `test_job_log_works_for_any_job_and_tails_the_tool_calls` | sound | — | — |
| `test_job_log_tail_lines_zero_is_all_lines_negative_is_bad_argument` | sound | — | — |
| `test_summary_metrics_follow_the_row_rule` | sound | — | — |
| `test_metric_integer_columns_are_rounded_and_real_ones_keep_the_fraction` | sound | — | — |
| `test_summary_metrics_none_when_unset` | sound | — | — |
| `test_unknown_job_hint_lists_the_ids` | sound | — | — |
| `test_unknown_job_hint_falls_back_to_the_database` | sound | — | — |
| `test_an_in_process_job_with_no_created_at_is_the_oldest` | sound | — | — |
| `test_unknown_job_hint_survives_a_database_it_cannot_open` | sound | — | — |
| `test_run_job_writes_the_worktree_before_the_first_message` | sound | — | — |
| `test_a_metrics_failure_in_the_finally_cannot_strand_the_job` | sound (param: a failure in finally still settles) | — | — |
| `test_list_jobs_limit_keeps_the_newest` | sound | — | — |
| `test_list_jobs_newest_n_is_served_by_the_created_at_index` | sound (the index serves it) | — | — |
| `test_null_created_at_sorts_at_the_oldest_end_of_the_listing` | sound | — | — |
| `test_summary_from_row_reads_nullable_columns_defensively` | sound | — | — |
| `test_row_summary_catches_only_row_problems_and_says_so` | sound | — | — |
| `test_a_database_error_is_not_a_row_error` | sound | — | — |
| `test_job_log_says_when_the_directory_is_gone` | sound | — | — |
| `test_job_log_reads_the_transcript_row_first` | sound | — | — |
| `test_unknown_job_answers_are_uniform_and_unquoted` | sound | — | — |
| `test_wait_jobs_running_elapsed_does_not_build_a_summary` | sound | — | — |
| `test_wait_jobs_running_elapsed_survives_a_job_with_no_timestamps` | sound | — | — |
| `test_a_running_entrys_elapsed_is_a_float_on_both_paths` | sound | — | — |
| `test_a_row_deleted_mid_wait_answers_unknown_job_for_the_whole_call` | sound | — | — |
| `test_read_paths_answer_database_error_not_a_traceback` | sound | — | — |
| `test_a_failing_row_lookup_is_never_an_unknown_job` | sound | — | — |
| `test_a_failing_derive_read_degrades_the_progress_line_not_the_wait` | sound | — | — |

## tests/test_requests_db.py — one request row

Behaviour-driven: the fixture replay into rows, the shim-log row (and no jsonl),
the context-exceeded storage, the db-error refusal.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_fixture_replays_into_rows` | sound (the fixture's requests replay into the requests table) | — | — |
| `test_shim_log_writes_a_row_and_no_jsonl` | sound (a request row, and no .jsonl on disk) | — | — |
| `test_context_exceeded_is_stored` | sound | — | — |
| `test_db_error_does_not_break_record_request` | sound | — | — |

## tests/test_tool_calls_db.py — the tool-call rows

Behaviour-driven over a real database with a fake clock: the tool-use rows,
the last-activity text matching the row, the path-shaped tools, the result
reconciliation, the orphan-result refusal, and the db-error refusal.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_tool_use_rows` | sound | — | — |
| `test_last_activity_shows_the_same_text_as_the_row` | sound (the display and the row agree) | — | — |
| `test_path_shaped_tools_store_their_path` | sound | — | — |
| `test_results_set_finished_and_is_error` | sound | — | — |
| `test_orphan_result_is_ignored` | sound | — | — |
| `test_turns_count_live_and_the_result_reconciles` | sound | — | — |
| `test_result_message_usage_stays_off_the_job` | sound | — | — |
| `test_counts_and_files_touched_come_from_the_rows` | sound | — | — |
| `test_db_error_does_not_break_tool_recording` | sound | — | — |

## tests/test_retention.py — the job-directory retention

Behaviour-driven: the log-event flag (a no-op by default, writes when enabled),
the defaults, the prune by newest mtime (the directory's and its children's),
the untrusted old stamp, the not-named-like-a-job refusal, the dry-run,
the disabled-when-zero, and the CLI subcommand.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_log_event_is_a_no_op_by_default` | sound | — | — |
| `test_log_event_writes_when_enabled` | sound | — | — |
| `test_defaults_carry_retention_and_events_flags` | sound | — | — |
| `test_prune_removes_job_dirs_by_newest_mtime` | sound | — | — |
| `test_prune_does_not_trust_the_old_stamp_any_more` | sound (the stamp is no longer trusted) | — | — |
| `test_prune_reads_the_newest_mtime_of_the_directory_and_its_children` | sound | — | — |
| `test_prune_leaves_anything_not_named_like_a_job` | sound | — | — |
| `test_prune_dry_run_lists_without_deleting` | sound | — | — |
| `test_prune_disabled_when_days_is_zero` | sound | — | — |
| `test_cli_prune_subcommand` | sound | — | — |

## tests/test_worktree.py — the worktree lifecycle

Behaviour-driven over a real git repo: the no-change removal, the failed-removal
recorded-not-reported, the resume, the worktree-git-on-a-thread, the wait-settle
ordering (the row turns terminal only after the finally work, the transcript row
exists before the row turns terminal), the cancel-during-make-worktree family,
and the partial-failure branch deletion. The `test_a_cancel_during_make_worktree`
and `test_a_second_cancel_inside_the_finally_cannot_strand_the_job` are the
real settlement behaviour.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_no_change_job_removes_tree_and_branch` | sound | — | — |
| `test_a_failed_removal_is_recorded_not_reported_removed` | sound | — | — |
| `test_resume_recreates_removed_tree_at_base` | sound | — | — |
| `test_run_job_offloads_worktree_git_to_a_thread` | sound | — | — |
| `test_resume_reuses_kept_tree_and_reports_its_diff` | sound | — | — |
| `test_wait_returns_only_after_the_finally_work` | sound | — | — |
| `test_wait_many_returns_only_after_the_finally_work` | sound | — | — |
| `test_the_row_turns_terminal_only_after_the_finally_work` | sound | — | — |
| `test_a_finished_job_gets_its_transcript_row_at_the_settle` | sound | — | — |
| `test_a_cancelled_job_gets_its_transcript_row_too` | sound | — | — |
| `test_the_transcript_row_exists_before_the_row_turns_terminal` | sound | — | — |
| `test_an_unexpected_error_recording_the_transcript_still_settles_the_job` | sound | — | — |
| `test_cancel_during_make_worktree_leaves_no_tree` | sound | — | — |
| `test_cancel_during_the_first_metrics_probe_finishes_cancelled` | sound | — | — |
| `test_kept_note_shows_untracked_files_to_the_reviewer` | sound | — | — |
| `test_finish_over_a_missing_directory_records_it` | sound | — | — |
| `test_finish_over_an_unreadable_git_pointer_records_it` | sound | — | — |
| `test_cancel_during_make_worktree_sets_run_cwd_as_the_row_does` | sound | — | — |
| `test_make_worktree_removes_its_tree_when_a_step_after_the_add_fails` | sound | — | — |
| `test_make_worktree_names_a_branch_its_cleanup_could_not_delete` | sound | — | — |
| `test_a_make_worktree_error_during_a_cancel_is_not_dropped` | sound | — | — |
| `test_a_partially_failed_add_deletes_the_branch_it_created` | sound | — | — |
| `test_a_cancel_during_make_worktree_prints_a_cancel_line` | sound | — | — |
| `test_two_cancel_job_calls_during_make_worktree_print_a_cancel_line` | sound | — | — |
| `test_cancel_job_on_an_unsettled_job_returns_the_settled_record` | sound | — | — |
| `test_a_second_cancel_inside_the_finally_cannot_strand_the_job` | sound | — | — |
| `test_the_cancel_line_counts_every_absorbed_cancel` | sound | — | — |

## tests/test_probe.py — the connect-failure classification

Behaviour-driven: the real `classify_connect_failure` over real `httpx` exceptions
(refused means host-up/wrong-port, unreachable means down, timeout is unknown,
a read/write timeout proves the box up, connect+pool stay unknown, other errors
verbatim, a vLLM probe over a doc without the kv series carries no kv key).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_refused_means_host_up_wrong_port` | sound | — | — |
| `test_unreachable_means_host_down` | sound | — | — |
| `test_timeout_is_unknown` | sound | — | — |
| `test_read_timeout_proves_the_box_up` | sound | — | — |
| `test_write_timeout_proves_the_box_up` | sound | — | — |
| `test_connect_and_pool_timeouts_stay_unknown` | sound | — | — |
| `test_other_errors_fall_through_verbatim` | sound | — | — |
| `test_probe_reports_host_state` | sound | — | — |
| `test_a_vllm_probe_over_a_doc_without_the_kv_series_carries_no_kv_key` | sound | — | — |

## tests/test_progress_protocol.py — the wait job's progress notifications

Behaviour-driven: `wait_job` sends the progress notifications the operator's
client reads. The single test drives the real store and captures the
notifications.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_wait_job_sends_progress_notifications` | sound | — | — |

## tests/test_query_plans.py — the 3.9E query-plan pins

Sound, each `EXPLAIN QUERY PLAN` pin paired with a behaviour check (the rows
the plan answers, the rewritten-predicate equivalence): E1 (the finished tail
seeks `jobs_finished_at`, no sort), E2 (the OR terms seek, the sort stays over
the window's rows, the overlapping rows are the window spanners), E3 (the
live-only branch seeks the status index, the rewrite selects the same rows,
the all-branch still walks `created_at`). The plan pins are the standing 1.11
purge expectation, not sycophancy.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_e1_finished_tail_walks_jobs_finished_at_with_no_sort` | sound (the plan pin + no TEMP B-TREE) | — | — |
| `test_e2_overlapping_or_terms_seek_jobs_finished_at` | sound (two seeks + the sort stays) | — | — |
| `test_e2_overlapping_rows_are_the_window_spanners` | sound (the behaviour the plan serves) | — | — |
| `test_e3_live_only_branch_seeks_the_status_index` | sound (the rewrite, the old plan pinned for contrast) | — | — |
| `test_e3_rewritten_predicate_selects_the_same_rows` | sound (the equivalence) | — | — |
| `test_e3_list_all_branch_still_walks_created_at` | sound | — | — |

## tests/test_status.py — the statusline and the owner walk

Behaviour-driven: the render over a real store (a running job, finished-linger-then-drop,
the lost mark, a dead server's dropped jobs, other sessions' jobs folded into a count),
the one-header-per-endpoint render, the `statusline` command (light, one-line flag),
the corrupt-pid / pid-error refusals, the `load_summary` truth table (slots, load,
the gauge-less vLLM empty load, llama.cpp without slots), the shim-usage → ctx/speed
derivation, and the owner walk (a claude process or none, `my_owner` = the shared
`db.session_pid` walk, `open` records the walked session pid).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_short_model_and_elapsed` | sound | — | — |
| `test_publish_and_render_running_job` | sound | — | — |
| `test_finished_jobs_linger_then_drop` | sound | — | — |
| `test_null_lookup_ids_still_render` | sound | — | — |
| `test_lost_job_has_a_mark` | sound | — | — |
| `test_dead_server_jobs_are_dropped` | sound | — | — |
| `test_remove_deletes_own_file` | sound | — | — |
| `test_statusline_command_is_light` | sound (the command does not open a monitor) | — | — |
| `test_render_without_a_database_is_an_empty_line` | sound | — | — |
| `test_a_corrupt_pid_is_not_a_live_server` | sound | — | — |
| `test_render_swallows_a_pid_error_too` | sound | — | — |
| `test_other_sessions_jobs_fold_into_a_count` | sound | — | — |
| `test_render_lines_is_one_header_per_endpoint_one_row_per_job` | sound | — | — |
| `test_render_lines_puts_jobs_without_an_endpoint_under_local_question` | sound | — | — |
| `test_render_lines_is_empty_and_never_raises` | sound | — | — |
| `test_statusline_prints_lines_and_one_line_flag` | sound | — | — |
| `test_collect_carries_the_compactions_count` | sound | — | — |
| `test_display_activity_is_one_line_relative_with_the_cut_after` | sound | — | — |
| `test_row_activity_is_one_line_relative_to_the_root` | sound | — | — |
| `test_load_summary_shapes` | sound (the pure-function truth table) | — | — |
| `test_shim_usage_feeds_context_and_speed` | sound (the derivation off the request rows alone) | — | — |
| `test_my_owner_walks_to_a_claude_process_or_none` | sound (a live-process walk; skips when not under Claude) | — | — |
| `test_my_owner_is_the_dbs_shared_walk` | sound (by construction, the same walk) | — | — |
| `test_open_records_the_walked_session_pid` | sound | — | — |

## tests/test_strata.py — the strata (llama.cpp) backend

Behaviour-driven over the captured documents: the backend-kind normalisation, the
slot/queue read without advertising, a llama.cpp-marked-strata hint, the strata
metrics mapping onto llama.cpp counters, `fetch_metrics` accepting JSON (delta
matching one job) and still parsing Prometheus, and the load summary showing the
strata queue beside slots.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_backend_kind_normalises` | sound | — | — |
| `test_strata_probe_reads_slots_and_queue_without_advertising` | sound | — | — |
| `test_llamacpp_marked_strata_gets_a_hint_and_still_works` | sound | — | — |
| `test_real_llamacpp_gets_no_hint` | sound | — | — |
| `test_parse_strata_metrics_maps_totals_onto_llamacpp_counters` | sound | — | — |
| `test_fetch_metrics_accepts_json_and_delta_matches_one_job` | sound | — | — |
| `test_fetch_metrics_still_parses_prometheus` | sound | — | — |
| `test_load_summary_shows_strata_queue_beside_slots` | sound | — | — |

## tests/test_server.py — the MCP server's tools and startup

Behaviour-driven over a real registry/store: the resume family (refusing an unsettled
job, keeping the previous model, the explicit override, a fresh job on the first live,
a worktree job keeping its tree, a plain job with a missing cwd), the wait/progress
family (the progress line omitting zero errors, the bash tail, the timeout, the silent
without-context), the tool-schemas hide the context parameter, the instructions
fitting the cap, the startup-writes surviving a locked / newer-schema database, the
sweep on a failed describe write, the shim-port write refusal not orphaning a queued
job, the nothing-between-new-and-task orphan refusal, a cancelled wait leaving no
waiter/poll, the guard that the MCP server never imports the monitor package (and the
spellings it catches), and the wait_jobs repeated-id count.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_resume_refuses_a_job_that_has_not_settled` | sound | — | — |
| `test_resume_keeps_previous_model` | sound | — | — |
| `test_resume_with_explicit_model_overrides` | sound | — | — |
| `test_fresh_job_without_model_uses_first_live` | sound | — | — |
| `test_resume_worktree_job_keeps_tree_even_when_removed` | sound | — | — |
| `test_resume_plain_job_with_missing_cwd_is_an_error` | sound | — | — |
| `test_local_complete_reports_transport_errors` | sound | — | — |
| `test_wait_s_reports_progress_until_done` | sound | — | — |
| `test_progress_line_omits_zero_errors_and_the_tail_until_the_first_tool` | sound | — | — |
| `test_progress_tail_shows_the_bash_command_the_runner_recorded` | sound | — | — |
| `test_wait_job_times_out_and_still_reports` | sound | — | — |
| `test_wait_without_context_is_silent` | sound | — | — |
| `test_tool_schemas_hide_the_context_parameter` | sound | — | — |
| `test_wait_jobs_all_waits_for_every_job` | sound | — | — |
| `test_wait_jobs_any_returns_on_first_finish` | sound | — | — |
| `test_wait_jobs_timeout_and_bad_arguments` | sound | — | — |
| `test_wait_jobs_lists_a_job_still_in_its_finally_as_running` | sound | — | — |
| `test_wait_jobs_already_finished_returns_immediately` | sound | — | — |
| `test_instructions_and_descriptions_fit_claude_codes_cap` | sound | — | — |
| `test_swarm_guidance_names_the_rearm_trap` | sound | — | — |
| `test_startup_writes_survive_a_locked_database` | sound | — | — |
| `test_startup_writes_survive_a_newer_schema_database` | sound | — | — |
| `test_startup_sweep_runs_when_the_describe_write_fails` | sound | — | — |
| `test_startup_removes_the_old_status_directory` | sound | — | — |
| `test_shim_port_write_failure_cannot_orphan_a_queued_job` | sound | — | — |
| `test_nothing_between_new_and_the_task_can_orphan_a_job` | sound | — | — |
| `test_cancel_job_says_when_the_settle_wait_times_out` | sound | — | — |
| `test_run_agent_answers_a_database_error_like_its_other_refusals` | sound | — | — |
| `test_a_cancelled_wait_jobs_leaves_no_task_waiter_or_poll` | sound | — | — |
| `test_a_cancelled_wait_jobs_stops_its_progress_reporter` | sound | — | — |
| `test_a_cancelled_in_process_only_wait_jobs_leaves_nothing` | sound | — | — |
| `test_a_cancel_during_the_finallys_await_still_awaits_every_task` | sound | — | — |
| `test_the_mcp_server_side_never_imports_the_monitor_package` | sound (the AST guard) | — | — |
| `test_the_guard_catches_the_runtime_import_spellings` | sound | — | — |
| `test_wait_jobs_counts_a_repeated_id_once` | sound | — | — |

## tests/test_shim.py — the shim's translation and metrics

Behaviour-driven: the backend-error translation (llama.cpp with/without numeric
fields, vLLM-style, strata, a vLLM anthropic 500, other errors left alone), the
system-message fold (in-place, leading-system-to-next-user, text-into-last-result,
a system-reminder landing inside the tool result, plain/result-only left alone),
the metrics delta (a fractional delta, branching on the backend not a key, diffing
only the present-in-both counters, never reading an unguarded partner, the vLLM
sniff for an unnamed backend, never sniffing for a named counter format, a
prefix-in-one-snapshot-only is no prefix), the fuzzy model match, the registry
ignoring the legacy models section, the token-count fallback recording the backend's
status (and its real ms, a 500 not estimated), the overflow rewrite, the stream-usage
merge, the parse-metrics summing a labelled series, and the hello-handshake answered
with no row (and no body, an unknown job still 404).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_translate_llamacpp_error` | sound | — | — |
| `test_translate_llamacpp_without_numeric_fields_uses_message` | sound | — | — |
| `test_translate_vllm_style_error` | sound | — | — |
| `test_translate_strata_error` | sound | — | — |
| `test_translate_leaves_other_errors_alone` | sound | — | — |
| `test_fold_system_messages_in_place` | sound | — | — |
| `test_fold_leading_system_goes_to_next_user` | sound | — | — |
| `test_fold_text_into_last_tool_result` | sound | — | — |
| `test_fold_system_reminder_lands_inside_tool_result` | sound | — | — |
| `test_fold_leaves_plain_and_result_only_messages_alone` | sound | — | — |
| `test_metrics_delta` | sound | — | — |
| `test_metrics_delta_keeps_a_fractional_delta` | sound | — | — |
| `test_metrics_delta_branches_on_the_backend_not_on_a_key` | sound | — | — |
| `test_metrics_delta_diffs_only_counters_present_in_both_snapshots` | sound | — | — |
| `test_metrics_delta_never_reads_a_partner_it_did_not_guard` | sound (param: a backend/before/after/absent matrix) | — | — |
| `test_metrics_delta_sniffs_vllm_for_a_backend_that_names_no_format` | sound | — | — |
| `test_metrics_delta_never_sniffs_for_a_named_counter_format` | sound | — | — |
| `test_metrics_delta_prefix_series_in_one_snapshot_only_is_no_prefix` | sound | — | — |
| `test_fuzzy_model_match` | sound | — | — |
| `test_registry_ignores_legacy_models_section` | sound | — | — |
| `test_translate_vllm_anthropic_endpoint_500` | sound | — | — |
| `test_parse_metrics_sums_labelled_series` | sound | — | — |
| `test_count_tokens_fallback_records_the_backends_status` | sound (param: a status matrix) | — | — |
| `test_overflow_rewrite_records_the_backends_status` | sound | — | — |
| `test_count_tokens_fallback_records_its_real_ms` | sound | — | — |
| `test_count_tokens_500_is_not_estimated` | sound | — | — |
| `test_stream_usage_merges_message_start_and_message_delta` | sound | — | — |
| `test_hello_handshake_is_answered_by_the_shim_with_no_row` | sound | — | — |
| `test_hello_handshake_answers_head_with_no_body` | sound | — | — |
| `test_hello_handshake_unknown_job_is_still_404` | sound | — | — |

## tests/fixture_parsers.py (support)

The capture-fixture's request-shape parsers (the `request`/`usage`/`tools` helpers the
`test_*_db` files replay). A support module, audited by the tests that use it.

## tests/test_monitor_app.py — the monitor's routes and the push stream

Behaviour-driven over a real app instance and a real seeded database (`client` /
`dbpath`): the HTML shell (revalidating on every load, the static files revalidating),
the routes (the snapshot is the overview, the attention window from the settings, the
endpoint history default/range, a bad range is 400, an unknown endpoint is 404 before
the range is read, the fleet-history default shape, the endpoint-requests route, the
fleet range and errors, a configured endpoint never probed is an empty history, an
endpoint named fleet does not collide with the fleet cache, the jobs default-24h
newest-first, the jobs range narrowing and rejecting, the job-detail route, an unknown
job is 404, a database error is 503, a too-new schema is 503, a connection per call,
the stream sending a snapshot on connect and one per refresh period, ticks inside a
refresh period being one push, one tick reaching every open stream, the snapshot read
once per push and shared, the snapshot route serving the current push from its cache,
the cache expiring by age, a no-collect app self-ticking, the self-tick ending on the
commands stop sequence, the overview history batching the fleet and each live endpoint,
the overview history jobs overlapping the window, the history cached until a new probe,
two spellings of one window sharing the history entry, only the page range words
cached, one push reading the snapshot once however many subscribers, the overview
history warm call being the two-key statements, the fanout draining from app start,
the stream sending keepalive comments when idle, the stop family (ending the stream,
ending the streams and closing the fanout, the stop sequence waking the streams,
stop-set-twice, a stream after close not resurrecting the drain), a database error in
the stream being an error event, the monitor help, a bad monitor section, the db-file
flag/section/database precedence, the port-free family (a TIME_WAIT socket free, a
listening socket not free, any resolved address taken refusing, a free port, an
address family the box lacks skipped, a port in use exiting 2, a migrated v5 file
backfilling its witnesses, the start backfill skipping endpoints that carry a
witness, a live run serving the seeded file and stopping on SIGTERM, `--no-open` as a
flag with the browser opening by default, the default run opening the browser once and
stopping on SIGTERM, a second run on a running port opening it and exiting 0).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_index_is_the_html_shell_loading_app_js` | sound | — | — |
| `test_the_shell_revalidates_on_every_load` | sound | — | — |
| `test_static_serves_the_package_files` | sound | — | — |
| `test_static_files_revalidate_on_every_load` | sound | — | — |
| `test_snapshot_is_the_overview` | sound | — | — |
| `test_the_attention_window_comes_from_the_settings` | sound | — | — |
| `test_endpoint_history_defaults_to_an_hour` | sound | — | — |
| `test_endpoint_history_takes_the_range` | sound | — | — |
| `test_a_bad_range_is_400_bad_range` | sound (param: bad ranges) | — | — |
| `test_an_unknown_endpoint_is_404` | sound | — | — |
| `test_an_unknown_endpoint_is_404_before_the_range_is_read` | sound | — | — |
| `test_fleet_history_default_range_shape` | sound | — | — |
| `test_endpoint_requests_route` | sound | — | — |
| `test_fleet_history_range_and_errors` | sound | — | — |
| `test_a_configured_endpoint_never_probed_is_an_empty_history` | sound | — | — |
| `test_an_endpoint_named_fleet_does_not_collide_with_the_fleet_cache` | sound | — | — |
| `test_jobs_in_the_default_24h_range_newest_first` | sound | — | — |
| `test_jobs_range_narrows_and_rejects` | sound | — | — |
| `test_job_detail_route` | sound | — | — |
| `test_an_unknown_job_is_404` | sound | — | — |
| `test_a_database_error_is_503` | sound | — | — |
| `test_a_too_new_schema_is_503` | sound | — | — |
| `test_routes_use_a_connection_per_call_and_close_it` | sound | — | — |
| `test_the_stream_sends_a_snapshot_on_connect_and_one_per_refresh_period` | sound | — | — |
| `test_ticks_inside_one_refresh_period_are_one_push` | sound | — | — |
| `test_one_tick_reaches_every_open_stream` | sound | — | — |
| `test_the_snapshot_is_read_once_per_push_and_shared` | sound | — | — |
| `test_snapshot_route_serves_the_current_push_from_its_cache` | sound | — | — |
| `test_the_snapshot_cache_expires_by_age_not_only_by_push` | sound | — | — |
| `test_a_no_collect_app_self_ticks_the_streams_every_refresh_period` | sound | — | — |
| `test_the_self_tick_ends_on_the_commands_stop_sequence` | sound | — | — |
| `test_overview_history_batches_the_fleet_and_each_live_endpoint` | sound | — | — |
| `test_overview_history_jobs_overlap_the_window` | sound | — | — |
| `test_history_is_cached_until_a_new_probe` | sound | — | — |
| `test_two_spellings_of_one_window_share_the_history_entry` | sound | — | — |
| `test_only_the_page_range_words_are_cached` | sound | — | — |
| `test_one_push_reads_the_snapshot_once_however_many_subscribers` | sound | — | — |
| `test_overview_history_warm_call_is_the_two_key_statements` | sound | — | — |
| `test_the_fanout_drains_from_app_start_not_the_first_subscriber` | sound | — | — |
| `test_the_stream_sends_keepalive_comments_when_idle` | sound | — | — |
| `test_stop_ends_the_stream` | sound | — | — |
| `test_stop_ends_the_streams_and_closes_the_fanout` | sound | — | — |
| `test_the_commands_stop_sequence_wakes_the_streams_before_it_closes_the_fanout` | sound | — | — |
| `test_stop_set_twice_ends_the_streams_without_error` | sound | — | — |
| `test_a_stream_after_close_does_not_resurrect_the_drain` | sound | — | — |
| `test_a_database_error_in_the_stream_is_an_error_event` | sound | — | — |
| `test_monitor_help_exits_0` | sound | — | — |
| `test_a_bad_monitor_section_exits_2_naming_the_key` | sound | — | — |
| `test_db_file_prefers_the_flag_over_the_monitor_section` | sound | — | — |
| `test_db_file_uses_the_monitor_section_over_the_database_default` | sound | — | — |
| `test_db_file_falls_back_to_the_database_path` | sound | — | — |
| `test_port_free_reads_a_time_wait_socket_as_free` | sound | — | — |
| `test_port_free_is_false_for_a_listening_socket` | sound | — | — |
| `test_port_free_refuses_when_any_resolved_address_is_taken` | sound | — | — |
| `test_port_free_is_true_for_a_free_port` | sound | — | — |
| `test_port_free_skips_an_address_family_the_box_lacks` | sound | — | — |
| `test_a_port_already_in_use_exits_2_with_the_sentence` | sound | — | — |
| `test_a_migrated_v5_file_gets_its_witnesses_backfilled_at_the_start` | sound | — | — |
| `test_the_start_backfill_skips_endpoints_that_already_carry_a_witness` | sound | — | — |
| `test_a_live_run_serves_the_seeded_file_and_stops_on_sigterm` | sound | — | — |
| `test_no_open_is_a_flag_and_the_browser_opens_by_default` | sound | — | — |
| `test_the_default_run_opens_the_browser_once_and_still_stops_on_sigterm` | sound | — | — |
| `test_a_second_run_on_a_running_port_opens_it_and_exits_0` | sound | — | — |
| `test_spec_5a_names_every_key_the_routes_emit` | sound (the key-set contract against spec §5a) | — | — |
| `test_spec_5a_quotes_every_rates_reason_error_code_and_event` | sound (a completeness check; the `len(reasons)==4` count is a deliberate pin of the four reason strings read.py emits) | — | — |

## tests/test_monitor_read.py — the read model over the seeded world

Behaviour-driven: every test drives `read` over the `world` fixture (the seeded database —
the three endpoints ai1 live / dgx1-a down / llamacpp dormant) or a hand-built
`tmp_path` database, and asserts the figure. The overview (the top-level keys, the
database file it reads, the hosts in config order with their states, the fallback
resolving an all-timeout host, the last-active skipping the reset interval, the
live endpoint carrying its gauges/rates/hardware, the down endpoint naming its
sentence, the dormant endpoint last seen a week ago, the dropped job id, the
running-job tool text, the running jobs carrying the status-line entry, the finished
jobs following the running ones, only the ten newest finished jobs joining the log,
the finished elapsed frozen across clock reads, the finished rows deriving once,
a job finishing between pushes entering the cache, the finished cache holding only
the current tail, the server live recomputing on a cached finished row, a terminal
row with null finished_at showing no moving elapsed, the overview events being the
newest twenty with endpoint names, an endpoint in the database but not in the config
ignored, a configured endpoint with no rows being dormant), the endpoint history
(over an hour downsampled to at most 600, without downsampling keeping every probe,
the downsampling taking every kth probe ending at the newest, serving the state
latency and every hardware series, the dgx history having no hardware series, the
reset interval having no rate, the downsampled rates null when a reset lies inside
the interval, the dormant history over fifteen minutes being times only, a reset
interval having no rate), the bounded read (the seven-day history reading only the
points it answers), the fleet history (one grid for the configured endpoints with
probes, the occupancy following the backend, the grid capped and endpoints without
probes left out), the endpoint requests (holding both seeded jobs newest first,
the totals counting only real errors, an endpoint without jobs empty with zero
totals, the list capped but not the totals, the range excluding older traffic), and
the job detail (of the finished job, tailing the transcript when the directory
exists, reading the transcript row before the file, an unknown job is none, the
jobs in range and job detail taking the reader's now, the jobs in range newest first
without result).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_overview_top_level_keys` | sound | — | — |
| `test_overview_names_the_database_file_it_reads` | sound | — | — |
| `test_overview_hosts_in_config_order_with_their_states` | sound | — | — |
| `test_fallback_resolves_an_all_timeout_host` | sound | — | — |
| `test_last_active_is_the_newest_advancing_counters_interval` | sound | — | — |
| `test_last_active_skips_the_reset_interval` | sound | — | — |
| `test_idle_seven_day_history_reads_like_a_two_probe_one` | sound (an idle endpoint's seven-day history is a flat line) | — | — |
| `test_the_live_endpoint_carries_its_gauges_rates_and_hardware` | sound | — | — |
| `test_the_down_endpoint_names_its_sentence_and_no_dropped_job` | sound | — | — |
| `test_the_dormant_endpoint_is_last_seen_a_week_ago` | sound | — | — |
| `test_the_dropped_job_id_names_the_job_running_when_the_endpoint_dropped` | sound | — | — |
| `test_snapshot_running_job_tool_text_is_the_status_bar_display_form` | sound | — | — |
| `test_snapshot_running_job_tool_text_falls_back_to_the_cwd` | sound | — | — |
| `test_overview_running_jobs_carry_the_status_line_entry` | sound | — | — |
| `test_finished_jobs_follow_the_running_ones_in_the_log` | sound | — | — |
| `test_only_the_ten_newest_finished_jobs_join_the_log` | sound | — | — |
| `test_finished_elapsed_is_frozen_across_clock_reads` | sound | — | — |
| `test_finished_rows_derive_once_across_pushes` | sound | — | — |
| `test_a_job_finishing_between_pushes_enters_the_cache_with_its_figures` | sound | — | — |
| `test_the_finished_cache_holds_only_the_current_tail` | sound | — | — |
| `test_server_live_recomputes_on_a_cached_finished_row` | sound | — | — |
| `test_a_terminal_row_with_null_finished_at_shows_no_moving_elapsed` | sound | — | — |
| `test_overview_events_are_the_newest_twenty_with_endpoint_names` | sound | — | — |
| `test_an_endpoint_in_the_database_but_not_in_the_config_is_ignored` | sound | — | — |
| `test_a_configured_endpoint_with_no_rows_is_dormant_cold_start` | sound | — | — |
| `test_ai1_history_over_an_hour_is_downsampled_to_at_most_600` | sound | — | — |
| `test_history_without_downsampling_keeps_every_probe` | sound | — | — |
| `test_history_downsampling_takes_every_kth_probe_ending_at_the_newest` | sound | — | — |
| `test_ai1_history_serves_the_state_latency_and_every_hardware_series` | sound | — | — |
| `test_dgx_history_has_latency_and_state_but_no_hardware_series` | sound | — | — |
| `test_ai1_history_event_is_the_restart` | sound | — | — |
| `test_ai1_history_jobs_are_the_two_seeded` | sound | — | — |
| `test_dgx_history_has_no_hardware_keys_and_nulls_while_down` | sound | — | — |
| `test_dormant_history_over_fifteen_minutes_is_times_only` | sound | — | — |
| `test_a_reset_interval_has_no_rate` | sound | — | — |
| `test_downsampled_rates_are_null_when_a_reset_lies_inside_the_interval` | sound | — | — |
| `test_a_seven_day_history_reads_only_the_points_it_answers` | **dead (the jobs half)** — the bounded read's body half is sound (the algorithm half vs the pre-R1 oracle, the 599 points, the probe read < 2×600), but the oracle's jobs block (lines 922-923) pops `name` off `old["jobs"]` which is empty, so `new == old` compares two empty jobs lists: the jobs half never exercises a job. The seven-day world (`_seven_day_world`) holds no job. Replacement shape: give `_seven_day_world` an overlapping job (a job started in the window, one running past it) and assert the bounded read's `jobs` equals the pre-R1 oracle's jobs for that job — the jobs half then has a row to compare. |
| `test_fleet_history_one_grid_for_the_configured_endpoints_with_probes` | sound | — | — |
| `test_fleet_history_occupancy_follows_the_backend` | sound | — | — |
| `test_fleet_history_grid_capped_and_endpoints_without_probes_left_out` | sound | — | — |
| `test_endpoint_requests_holds_both_seeded_jobs_newest_first` | sound | — | — |
| `test_endpoint_requests_totals_count_only_real_errors` | sound | — | — |
| `test_endpoint_requests_endpoint_without_jobs_is_empty_with_zero_totals` | sound | — | — |
| `test_endpoint_requests_caps_the_list_but_not_the_totals` | sound | — | — |
| `test_endpoint_requests_range_excludes_older_traffic` | sound | — | — |
| `test_job_detail_of_the_finished_job` | sound | — | — |
| `test_job_detail_tails_the_transcript_when_the_directory_exists` | sound | — | — |
| `test_job_detail_reads_the_transcript_row_before_the_file` | sound | — | — |
| `test_job_detail_of_an_unknown_job_is_none` | sound | — | — |
| `test_jobs_in_range_and_job_detail_take_the_readers_now` | sound | — | — |
| `test_jobs_in_range_newest_first_without_result` | sound | — | — |

## tests/test_monitor_smoke.py — the smoke script's checklist and requests

Behaviour-driven: the checklist render (one line per check in order, the empty and
all-ok exit zero), the `direct_requests` over a real `httpx.MockTransport` (two 200s
with a usage block then a refused connection chained exactly as httpx chains the
kernel's ECONNREFUSED, recording the status/usage/latency and the refusal sentence,
the bodies being the short ping and the endpoint key with the api key), the
`real_job` building the job like run_agent (reporting every row, a missing job row
when the run fails, the transcript date check ignoring the runner's own lines,
heartbeating the server row while the job runs), the down state reading the refused
endpoint end to end, the smoke subcommand (help exits zero, an unknown endpoint is
one missing line and exit one, requests-zero skips with an ok line, a negative
requests is one line and exit two, the run marking the server row stopped, the live
tick offline seeing counters and live state).

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_render_one_line_per_check_in_order` | sound | — | — |
| `test_empty_and_all_ok_checklists_exit_zero` | sound | — | — |
| `test_direct_requests_records_status_usage_latency_and_the_refusal_sentence` | sound (the real httpx chain of the kernel's ECONNREFUSED) | — | — |
| `test_direct_requests_bodies_are_the_short_ping_and_the_endpoint_key` | sound | — | — |
| `test_real_job_builds_the_job_like_run_agent_and_reports_every_row` | sound | — | — |
| `test_real_job_job_row_missing_when_the_run_fails` | sound | — | — |
| `test_transcript_date_check_ignores_the_runner_own_lines` | sound | — | — |
| `test_real_job_heartbeats_the_server_row_while_the_job_runs` | sound | — | — |
| `test_down_state_reads_the_refused_endpoint_end_to_end` | sound | — | — |
| `test_smoke_subcommand_help_exits_zero` | sound | — | — |
| `test_smoke_unknown_endpoint_is_one_missing_line_and_exit_one` | sound | — | — |
| `test_requests_zero_skips_the_direct_requests_with_an_ok_line` | sound | — | — |
| `test_negative_requests_is_one_line_and_exit_two_before_anything` | sound | — | — |
| `test_run_marks_the_server_row_stopped` | sound | — | — |
| `test_live_tick_offline_sees_counters_and_live_state` | sound | — | — |

## tests/test_monitor_page.py — the static-page pins (ruling (o))

The pin family: substring/regex pins over `app.js` / `app.css` / `endpoint.js` /
`job.js`. The verdict rule: **sound, smoke-covered** when a committed `tools/smoke/`
script drives the real DOM the pin names (the smokes are the witnesses — the DOM
render proves the class/figure exists and is correct); otherwise a **source constraint
pin** (a legitimate negative-space or ordering guard whose witness is the source, not a
DOM smoke — the wrong implementation is named per row). The one **too-narrow** finding
is `test_job_header_names_both_error_figures`: the pill half is smoke-covered
(`handshake.js`), but the job header's request-errors **count** is pinned only in
source — the operator's 3.7v "tested on the pill but not the count" gap.

| test | verdict | usage pattern it misses | replacement shape |
|---|---|---|---|
| `test_the_shell_references_exactly_the_page_static_files` | sound (the shell contract: it loads exactly the static files; no DOM smoke — the wrong impl is a shell that drops or adds a script tag) | — | — |
| `test_each_static_file_is_served_with_its_content_type` | sound (param: a real HTTP content-type check) | — | — |
| `test_the_page_carries_every_class_the_smoke_and_spec_name` | sound, smoke-covered (the committed smokes render the classes they read) | — | — |
| `test_app_js_reads_only_section_5a_field_names` | sound, smoke-covered (negative-space: a read of a field §5a does not name fails; the smoke-covered reads are present) | a read the smoke never drives (a field the page reads but no smoke renders) | a `cdp.mjs`-over-seeded-world that renders the field the pin names |
| `test_every_variable_key_read_is_listed_by_name` | sound, smoke-covered (param: a bracket-key read not in the allow-list fails) | a listed key the page reads and ignores | a smoke that renders the value the listed key feeds |
| `test_the_field_walk_sees_literal_bracket_keys_and_holder_chains` | sound (a meta-test of the field-walk parser itself) | — | — |
| `test_the_jobs_table_columns_answer_when_and_show_both_speeds` | sound, smoke-covered (`jobsrows.js` / `tables.js`) | — | — |
| `test_the_server_gone_badge_is_gone_and_a_lost_server_inks_the_pill` | sound, smoke-covered (`jobsrows.js` — a dead server's lost/lost/done) | — | — |
| `test_no_ellipsis_rule_applies_inside_a_host_card` | sound, smoke-covered (`keep.js` / `wide.js` — the grow-never-cut) | — | — |
| `test_ep_job_row_cuts_no_text` | sound, smoke-covered (`wide.js`) | — | — |
| `test_the_card_floor_starts_where_the_stylesheet_does_and_only_rises` | sound, smoke-covered (`floor.js` / `floor.mjs`) | — | — |
| `test_every_card_line_the_css_makes_scrollable_is_measured_by_the_floor` | sound, smoke-covered (`floor.js`) | — | — |
| `test_no_webkit_scrollbar_rule_remains_in_the_stylesheet` | sound (a source constraint pin; no DOM smoke — the wrong impl is a `::-webkit-scrollbar` rule the page would render) | — | — |
| `test_every_flex_row_holding_a_card_line_is_measured_at_max_content` | sound, smoke-covered (`floor.js`) | — | — |
| `test_in_place_comparisons_mask_the_overflows_mark` | sound, smoke-covered (`keep.js`) | — | — |
| `test_job_js_lists_exactly_the_metrics_columns` | sound, smoke-covered (`tables.js` / `jobsrows.js`) — the "exactly" is a negative-space guard | a column the page lists but no smoke renders | a smoke that renders the column the pin names |
| `test_job_js_links_to_no_file_urls` | sound (a source constraint pin; no DOM smoke — the wrong impl is a `file:` link the page would draw) | — | — |
| `test_job_js_totals_count_only_real_errors` | sound, smoke-covered (`handshake.js` — the row rule is the count's oracle) | — | — |
| `test_endpoint_js_carries_every_class_the_smoke_names` | sound, smoke-covered (`tiles.js`) | — | — |
| `test_the_job_page_carries_the_shared_renderer_classes` | sound, smoke-covered (the shared renderer's classes) | — | — |
| `test_requests_tables_share_one_renderer` | sound, smoke-covered (`tables.js`) | — | — |
| `test_endpoint_js_reads_only_section_5a_field_names` | sound, smoke-covered (negative-space: a read of an undefined field fails) | a read the smoke never drives | a `cdp.mjs` render of the field the pin names |
| `test_endpoint_js_draws_one_tile_per_history_series` | sound, smoke-covered (`tiles.js`) | — | — |
| `test_endpoint_js_reads_the_mb_fields_as_megabytes_per_sample` | sound, smoke-covered (`tiles.js`) | — | — |
| `test_endpoint_js_registers_through_the_pages_hook_and_never_polls` | sound, smoke-covered (the pages hook; the no-poll) | — | — |
| `test_endpoint_page_stays_live_on_the_snapshot_hook_not_a_dead_element` | sound, smoke-covered (the stream push; the monitor_app stream tests) | — | — |
| `test_every_element_id_a_page_script_names_exists_in_the_shell` | sound (a completeness pin: a dangling id the JS names fails) | — | — |
| `test_route_tears_the_detail_page_down_on_every_route_change` | sound (behaviour: the teardown on a route change) | — | — |
| `test_route_decodes_the_name_in_a_try_before_it_tears_down` | sound (a source-ordering pin; no DOM smoke — the wrong impl is a teardown before the decode, which throws on an encoded name) | — | — |
| `test_app_js_offers_the_server_ranges` | sound, smoke-covered (`range.js`) | — | — |
| `test_the_page_loads_nothing_from_the_internet` | sound (a source constraint pin: no external URL the page would fetch) | — | — |
| `test_the_page_never_polls` | sound, smoke-covered (the no-poll; `drive.mjs`) | — | — |
| `test_the_page_fetches_the_overview_history_in_one_batched_call` | sound, smoke-covered (the batched call; the monitor_app batched test) | — | — |
| `test_the_command_serves_the_page_and_its_files_over_the_seeded_world` | sound (a real HTTP test over the seeded world) | — | — |
| `test_the_command_serves_the_endpoint_page_and_the_three_calls_it_makes_for_ai1` | sound (a real HTTP test) | — | — |
| `test_a_down_node_card_head_says_down_and_keeps_the_probe_sentence_in_the_title` | sound, smoke-covered (`nodes.js` / `unknown.js`) | — | — |
| `test_requests_table_labels_a_handshake_row_as_no_error` | sound, smoke-covered (`handshake.js` — the pill half) | — | — |
| `test_job_header_names_both_error_figures` | **too narrow (the count half)** — the pill half is smoke-covered (`handshake.js` labels the handshake row as no error), but the job header's request-errors **count** figure is pinned only in source; no smoke reads the count. The smallest wrong impl that still passes: a header that counts a context-exceeded request (or a handshake with an error body) as a request error — the pill is still correct, the count is not, and no smoke catches it. | a `cdp.mjs`-over-seeded-world that opens the job page and reads the job header's request-errors figure, asserted against the seed's request rows (the count that counts only real errors) |
| `test_a_never_seen_endpoint_is_hidden_on_a_down_node_which_then_gets_no_card` | sound, smoke-covered (`neverseen.js`) | — | — |
| `test_an_unknown_node_is_an_alert_like_a_down_node` | sound, smoke-covered (`unknown.js`) | — | — |
| `test_endpoint_page_draws_no_tier_numbered_headings` | sound (a source constraint pin: no tier-numbered heading the page would draw) | — | — |
| `test_tilesFor_draws_no_endpoint_or_node_tile` | sound, smoke-covered (`tiles.js`) | — | — |
| `test_a_tile_value_is_never_cut` | sound, smoke-covered (`tiles.js` — the 200-character model) | — | — |

## Step 3 — final states (the 3.11b pass)

The 728 sound rows need no change. The flagged rows, their disposition, and the suite's
closing line (810 passed at the 3.11b head; 808 at the pre-3.11b head):

**Replaced (test-first, each its own commit, each gated by `uv run pytest -q`):**

| flagged row | state | commit |
|---|---|---|
| `test_1_15.py::test_a_worktree_checkout_refuses_the_default_state_directory` (too narrow — in-process only) | **replaced** by two subprocess tests that run the real package from this real worktree (the refusal case with XDG_STATE_HOME/LOCALAGENTS_DB unset, and the redirect case); the guard's reach through a real import is now exercised | `e89b47b` |
| `test_monitor_read.py::test_a_seven_day_history_reads_only_the_points_it_answers` (dead — the jobs half) | **replaced**: the pre-3.10b five-key jobs fossil is removed from the oracle and excluded from the body comparison; the bounded read's jobs half is tested on the seeded world by `test_ai1_history_jobs_are_the_two_seeded` | `b29184e` |

**Vehicle committed (the (a) family, deferred):**

`tools/smoke/dom.js` (`0b75870`) — the robust CDP driver (polls for page readiness, not a fixed
settle), documented in the smoke README. The (a) sycophant family is deferred by the seeded
world's clock (below), not by the harness.

**Listed (needs a product / fixture change — not built this lane):**

| finding | why listed |
|---|---|
| the seeded world never shows the live-seen states (a down node whose endpoint is never seen, a lost-server job, a handshake row, a vLLM-shaped no-cache row) | the page's `snap.now` is the wall clock (confirmed: the snapshot's `now` == wall, not the seed's `NOW`), so a seeded world renders its endpoints dormant and its jobs out of the page's range. Growing `seed.seed` to produce the live-seen states ripples through `EXPECTED_COUNTS` and every seeded-world assertion (`test_monitor_read`, `test_3_10b`) — a fixture change this lane does not open |
| the (a) sycophant family: `test_3_10b` (the endpoint page's jobs table drawn through the shared renderer, the headings), `test_3_10c` (the two-table-id rule), `test_3_10f2` (the job link's full-id title), `test_3_10hb` (the request-table cells, the prompt total, the prefill cell), `test_3_10i` (the prefill cell's no-cache figure), `test_monitor_page::test_job_header_names_both_error_figures` (the request-errors count) | a `dom.js`-over-seeded-world test is the replacement shape; blocked by the seeded world's clock (the page's `snap.now` is the wall clock, so the seed's states and jobs are not visible on the page). The `dom.js` vehicle is committed; the tests are the next lane once the seed grows the live-seen states (or a state the seed cannot produce is hand-rendered, labelled so) |
| `test_3_10b::test_history_and_timeline_share_the_overlap_predicate`'s `src.count("_OVERLAP_WHERE") >= 4` clause | a sycophant source pin (redundant — the `SELECT` half is the real witness); the count clause is dead weight, not a blocker. Left as-is this lane (removing it is a one-line cleanup, not a behaviour gap) |

**Dead rows removed:** the R1 oracle's jobs half (`test_monitor_read`) — see the replaced row above.
No test function was deleted outright this lane; the dead *half* of the R1 oracle was removed and
the test kept its sound body half.
