# Story 3.6 — Merge gates: live acceptance run (real fleet, real database)

Run by this job (20261029-162525-e7fcb80e) on 2026-10-29, in this worktree, branch
`local-agent/20261029-162525-e7fcb80e`. No image files were opened; the page was read through
DOM dumps and JSON only. No memory writes, no stash, no /tmp artifacts, no push, nothing installed.

Other sessions were live during the run: my own agent session runs on ai1 (visible in the
monitor's `jobs[]` — the monitor watching me), and one DeepSeek job on dgx1-a
(20261029-160736-e72f7f88, running). A `--no-collect` seeded-world monitor from an earlier
session sits on 127.0.0.1:18765; untouched, and not on the port or database used here.

## Step 1 — Start the monitor for real

Command (collection ON, real database by default):

```
LOCALAGENTS_CONFIG=/home/ccebelenski/projects/claudelocalmcp/models.yaml \
  nohup uv run localagents monitor --port 18781 > t/monitor.log 2>&1 &
```

(The brief's `tee` became a redirect under `nohup`: the log gets the same stdout+stderr, but
the exit status of a nohup'd process cannot be captured — see step 5's twin for that proof.)

Banner line (t/monitor.log, written 16:26:14):

```
localagents monitor: http://127.0.0.1:18781/ · database /home/ccebelenski/.local/state/localagents/localagents.db · 12 endpoints
```

(40 lines of venv build above it: first `uv run` in this worktree; not the monitor's output.)

`GET /api/snapshot` at 16:26:21 — 7 s after the banner, inside the 30 s window. Full dump in
`t/snapshot-step1.json`. `now=1793305581.73`, `window_s=86400.0`, `db_path` is the real file.
All 12 config endpoints appear, under 7 hosts (`local`, `dgx1`, `dgx2`, `dgx3`, `ai1`, `ai9`,
`ai2`). Host names come from the config `host:` keys; endpoints without one group under
`local`. `events` was still `[]` at this first pull — see step 2.

Per-endpoint table (from the snapshot) cross-checked by my own read-only `GET /v1/models`
against every endpoint (the grant allows exactly this; that is what the Registry probe does):

| endpoint | state | probe_state | error sentence | model | context | direct GET reality |
|---|---|---|---|---|---|---|
| llamacpp | live | idle | — | esatapedico/Qwen3.8-27B-NVFP4-MTP-GGUF | 262144 | 200, same model, 1.0 ms |
| vllm | dormant | — | 127.0.0.1 is reachable but nothing is listening on 127.0.0.1:8000 (nothing started there, or the port in models.yaml is wrong) | — | — | refused (curl exit 7) |
| dgx1 | dormant | — | 10.0.0.70 … nothing is listening on 10.0.0.70:8888 … | — | — | refused |
| dgx1-a | live | busy | — | DeepSeek-V4.1-Flash-1017 | 307712 | 200, same model |
| dgx2 | dormant | — | 10.0.0.71 … nothing is listening on 10.0.0.71:8000 … | — | — | refused |
| dgx3 | dormant | — | 10.0.0.72 … nothing is listening on 10.0.0.72:8000 … | — | — | refused |
| dgx3-lcpp | dormant | — | 10.0.0.72 … nothing is listening on 10.0.0.72:8080 … | — | — | refused |
| dgx2-lcpp | dormant | — | 10.0.0.71 … nothing is listening on 10.0.0.71:8080 … | — | — | refused |
| dgx1-lcpp | dormant | — | 10.0.0.70 … nothing is listening on 10.0.0.70:8080 … | — | — | refused |
| ai1 | live | generating | — | qwen3.8-flash-next-iq3_s | 262144 | 200, same model |
| ai9 | dormant | — | 10.0.0.62:8080 timed out (host off, or a firewall dropping packets) | — | — | timed out (curl exit 7 after 3.1 s) |
| ai2 | live | idle | — | qwen3.8-flash-next-unsloth-ud-q4_k_xl | 131072 | 200, same model |

Host states: `local` up, `dgx1` up, `dgx2` up, `dgx3` up, `ai1` up, `ai2` up, `ai9` **down**
(the timeout proves nothing about the box except that it does not answer: the collector marks
the node down, `fallback=nothing`). This matches the brief's expectation: ai1, ai2, llamacpp
and dgx1-a live; ai9, dgx2, dgx3 (and the unstarted vllm/dgx1 :8888) refuse with the kernel's
sentence.

Contradictions between snapshot state and `list_models`-style reality: **none** — every live
endpoint served `/v1/models` 200 with exactly the model the snapshot names; every refused one
was refused to my own curl as well. No (c) or (d) at this step.

Notes, not defects:
- The seven never-seen endpoints are `dormant`, not `down`: fresh state for the display rule
  (never seen up ⇒ dormant), and their probe rows do exist in the database (the sentences came
  from this run's first tick). This is the designed cold-start behaviour, same as the seeded
  world's llamacpp.
- Only the Strata endpoints (ai1, ai2) carry `since` (up-since epoch, ~20 h) and
  `gen_tps_live`; llama.cpp/vLLM report no uptime in their counters. ai1's
  `probe_state=generating` and `slots={total:1, busy:1}` — that is my own session on ai1.
  Live-data note for the verdict; the seeded world never showed the monitor observing the
  agent running the monitor.

## Step 2 — Two minutes of collection

The monitor kept collecting from 16:26:14; the SQL below ran 16:28:25–16:29:1x (≥120 s of
collection in the window). All queries: `sqlite3 -readonly ~/.local/state/localagents/localagents.db`.

Probes in the last 120 s, per endpoint — cadence exactly as designed (live every 5 s,
dormant every 60 s):

| endpoint | probe rows / 120 s | first | last |
|---|---|---|---|
| ai1, ai2, dgx1-a, llamacpp (live) | 24 each | 16:26:25 | 16:28:22–23 |
| ai9, dgx1, dgx2, dgx3, dgx1-lcpp, dgx2-lcpp, dgx3-lcpp, vllm (dormant) | 2 each | 16:27:14–17 | 16:28:15–21 |

`probe_counters` rows joined to those probes: ai1 25, ai2 25, dgx1-a 25, llamacpp 26
(the 25-vs-26 and 24-vs-25 mismatches are window-boundary artifacts: the first probe row
sat just outside the 120 s cutoff when I queried). `probe_hardware` rows: ai1 25, ai2 25 —
Strata only, as designed (vLLM/llama.cpp carry no hardware document; Epic 4.3 is V2).

`host_probes` (the port-22 fallback, asked only when *every* endpoint of a host timed out):
3 rows, all **ai9**, answer `nothing` — ai9 is the host whose every endpoint timed out, and
ssh on :22 does not answer either, consistent with the node being off/firewalled:

```
ai9|10.0.0.62|nothing|2026-10-29 16:26:17
ai9|10.0.0.62|nothing|2026-10-29 16:27:20
ai9|10.0.0.62|nothing|2026-10-29 16:28:23
```

`endpoint_events`: **0 rows**, and that is correct, not a failure: in this window nothing
transitioned — every endpoint's display state was stable — and `collect.tick` writes no
event for an endpoint's first probe ("a cold start is not a transition", collect.py:74).
The seven never-up endpoints therefore got their sentences but no `down` event. I did not
manufacture a transition; the events path is covered by unit tests, and the live run simply
showed its cold-start rule. Classification: **expected behaviour, no defect** (recording it
because the brief's parenthetical expected to see some — nothing did change to write one about).

`GET /api/endpoints/ai1/history?range=15m` (t/ai1-history.json): 30 points, arrays
`t, prompt_tps, gen_tps, cache_hit, slots_busy, requests_running, requests_waiting,
latency_ms, state, gpu_util, gpu_mem_used, gpu_temp, gpu_power, gpu_pcie_rx_mb, cpu,
ram_used, disk_read_mb, disk_write_mb, events, jobs`. Rates non-null on the intervals that
have work: tail gen_tps `[0, 0, 0, 138.6, 63.5, 0.0]`, prompt_tps `[0, 0, 0, 168.4, 87.3, 0]`,
cache_hit `[None, None, None, 0.985, 0.992, None]`, `state` follows Strata's `live.state`
verbatim (`idle`/`reading`/`generating`). `jobs[]` carries my own session (running).

Hand-computed interval, checked against ai1's raw `/metrics` (fetched directly, per the grant):

- Collector rows t=1793305744.71→1793305749.83 (dt=5.116 s): Δprocessed=9463, Δcached=62361,
  Δgenerated=1310, Δprompt_s=3.872, Δdecode_s=11.075 → gen_tps=256, cache_hit=0.868
  (read.py's hit = cached/(cached+processed), not cached/processed — my first hand division
  of 6.59 was me misreading the stored columns: `prompt_tokens` stores prompt *minus* reused,
  `prompt_tokens_cached` stores reused, per `parse_strata_metrics`).
- The raw `/metrics` `requests[]` array for that same interval holds exactly one completed
  request: 73309 prompt / 73173 reused / 632 output (processed 136, 0.383 s prefill + 4.132 s
  decode). So the extra Δ (≈678 generated, most of the processed prompt) is **live in-flight
  increment**: Strata's `totals` counters advance *during* streaming, not at completion, and
  the bin boundary cuts a request in half. Δdecode_s > dt for the same reason (a request's
  phase time spans more wall time than the bin shows).
- Direct double-fetch proof the collector is faithful to the document: two `/metrics` fetches
  7.03 s apart while the engine was idle (during a tool call) — every `totals` field
  byte-identical, and the collector's probes in that window likewise differ by 0.
- Verdict on plausibility: with a single-slot engine that streams, 5 s gen_tps bins can spike
  above the engine's steady ceiling (~110–140 tok/s, per Strata's own `hardware.tok_s`) when
  a bin straddles a completion plus an in-flight request — a **node/engine fact (c)**, not a
  monitor defect; the monitor deltas exactly what `/metrics` reports. The gauge
  `gen_tps_live` (ai1: 127.3) matches the engine's own instantaneous `tok_s` (107.9–109.4).

Other live-data notes for the verdict:
- Strata's `history.gpu_util` shows 35 leading zeros — the engine was idle ~3 min before my
  run started; the monitor's sparklines show that faithfully.
- ai1's `/metrics` shows the conversation cache parking/restoring my session's 60–90k-token
  prompts (`parks: 73`, `restores: 3`, `reused: 153.9M/156.6M` since boot) — the seeded world
  never showed prefix-cache reuse at this scale; every monitor rate for ai1 during my run is
  dominated by one giant streaming session, i.e. the live fleet is a single-user engine where
  a probe bin IS one request.

## Step 3 — The smoke, once, into the real database, while the monitor runs

```
LOCALAGENTS_CONFIG=/home/ccebelenski/projects/claudelocalmcp/models.yaml \
  uv run localagents smoke --endpoint ai2 --model qwen3.8-flash-next-unsloth-ud-q4_k_xl | tee t/smoke.txt
```

Started 16:31:43, finished 16:32:13, exit 0. Checklist verbatim:

```
[ok] model 'qwen3.8-flash-next-unsloth-ud-q4_k_xl' resolves on endpoint 'ai2': serving qwen3.8-flash-next-unsloth-ud-q4_k_xl at http://10.0.0.60:8080
[ok] direct request 1: HTTP 200, output_tokens=16, 2586 ms
[ok] direct request 2: HTTP 200, output_tokens=16, 352 ms
[ok] direct request 3: HTTP 200, output_tokens=16, 347 ms
[ok] dead-port probe row (up = 0): probes row for http://127.0.0.1:1: up=0
[ok] dead-port refusal names the box up (host = up): host=up
[ok] failure sentence (nothing is listening): 127.0.0.1 is reachable but nothing is listening on 127.0.0.1:1 (nothing started there, or the port in models.yaml is wrong)
[ok] endpoint_state after a never-up probe (dormant): state=dormant
[ok] node_state from the refusal alone (up): node=up
[ok] live probe row (up = 1): probes row for http://10.0.0.60:8080: up=1
[ok] counters row for the live probe: probe_counters written
[ok] endpoint_state after a live probe (live): state=live
[ok] job row terminal and succeeded: job 20261029-163147-d2c25f7e: status=succeeded
[ok] requests row for the job (200): 3 request row(s), 2 with http_status 200
[ok] tool_calls row for the job (Bash): 1 tool-call row(s), 1 for Bash
[ok] transcript has a date-shaped line: [result] Thu Oct 29 04:32:12 PM EDT 2026
[ok] summary usage totals non-zero: usage={'input_tokens': 2, 'output_tokens': 99, 'cache_read_input_tokens': 19630, 'cache_creation_input_tokens': 19628}
```

Every check ok; nothing MISSING, exit 0. The smoke job ran in this worktree (`isolation=none`,
cwd = here — visible in its transcript's `[start]` line and `cwd`).

Snapshot polling every 2 s over the whole smoke window (t/poll.py → t/snapshot-polls.log; the
line changes only):

```
16:31:42 ai2[live/idle] jobs_ai2: - | smoke-dead in snapshot: False
16:31:48 ai2[live/idle] jobs_ai2: 20261029-163147-d2c25f7e status=running server_live=True last_tool='None' ctx=None | smoke-dead in snapshot: False
16:32:12 ai2[live/reading] jobs_ai2: 20261029-163147-d2c25f7e status=running server_live=True last_tool='Bash: date' ctx=19568 | smoke-dead in snapshot: False
16:32:14 ai2[live/reading] jobs_ai2: - | smoke-dead in snapshot: False
```

- The job APPEARED in `jobs[]` 1 s after its row was created (started_at 16:31:47.94), with
  `server_live=True`; its `last_tool` went from null to the live `Bash: date` text;
  `ctx_tokens` filled (19568 of 131072).
- ai2's `probe_state` followed the job: `idle` before, `reading` while the job's request
  prefilled — the monitor watching the smoke, live.
- It left `jobs[]` at 16:32:14, the tick after it turned `succeeded` (finished_at 16:32:13.48).
  That is the snapshot's rule (`_running_jobs` selects `status IN ('queued','running')`,
  read.py:110) — completion removes it; the 120 s retention mentioned in the brief is the
  **status line's** rule for stopped servers (step 5), not the snapshot's. Not a defect.
- `smoke-dead` appears in NO snapshot anywhere in the window (its probe rows do land in the
  real `endpoints`/`probes` tables — that is the smoke's point — but hosts in the snapshot are
  built from the config, and it is not in the config).
- After it ended: `GET /api/jobs?range=15m` lists `20261029-163147-d2c25f7e succeeded ai2`
  (t/jobs-15m.json), and `GET /api/jobs/d2c25f7e-8872-47dd-a21f-3655a2ce2c7c`
  (t/job-d2c25f7e.json) carries the requests — `/api/hello` 501 (the SDK capability probe),
  two `/v1/messages` 200 (22422 ms, 1093 ms) — and the Bash tool call verbatim:
  `{"t": 1793305931.018, "finished": 1793305932.313, "tool": "Bash", "input": "date",
  "is_error": false}`, `tool_counts={'Bash': {'calls': 1, 'errors': 0}}`, num_turns 2,
  transcript_tail with the `[start]` line.
- The checklist's "3 request rows, 2 with http_status 200": the third is the `/api/hello` 501
  capability probe the SDK fires at the shim — normal, the check only needs a 200.

Failures: none. No (a)/(b)/(c)/(d) items at this step.

## Step 4 — The page over the real fleet

Harness: `chrome-headless-shell` (Playwright cache 1217) with `--remote-debugging-port=19333`,
`--user-data-dir=t/profile` (browser cache off otherwise; nothing installed), driven by the
copied read-only `cdp.mjs`/`conn.mjs` (in `t/`), node from linuxbrew. Node pass rules honoured:
**no image file was opened** — the screenshots cdp.mjs writes (`t/overview.png`, `t/ep-ai1.png`)
were never read; every fact below is from DOM dumps (`t/page-*.txt/json`, `t/conn.txt`) and the
JSON APIs.

Overview (`http://127.0.0.1:18781/`, t/page-overview.txt):
- Host cards and state classes, real hosts: `local`, `dgx1`, `ai1`, `ai2` = `host` (up);
  `dgx2`, `dgx3` = `host dormant`; `ai9` = `host node-down dormant` — three distinct classes,
  all real, none seeded.
- Endpoint cards: `state-live` for llamacpp / dgx1-a / ai1 / ai2 with full gauges;
  `state-dormant` for the other eight, each reading "vllm, never seen" / "llama.cpp, never seen"
  (the fresh database's cold start). ai1's card while I work: `generating slots 1/1
  decode 218.2 tok/s cache hit 100% GPU 70% VRAM 55.5/56.0 GiB temp 78 °C` — the monitor
  showing its own operator's session.
- Running-jobs table: 11 headers, 2 rows — the DeepSeek job on dgx1-a and my own job on ai1
  (97.4 tok/s, 40% ctx). The smoke's job itself was observed in the snapshot `jobs[]` feeding
  this table at 16:31:48–16:32:12 (step 3); the overview render at 16:33:08 is after it
  succeeded, so the table correctly no longer lists it. (I did not re-run a job to see it in
  the table DOM: the smoke runs once.)
- Fleet legends (all three charts): series = `time, llamacpp, dgx1-a, ai1, ai2` — **live
  endpoints only**, dormant ones absent.
- Connection dot: `conn conn-up / live` (t/conn.txt, stable over 6 s), `updated: snapshot
  4:34:04 PM`, `banner` empty, `meta`: `attention window 24h · database
  /home/ccebelenski/.local/state/localagents/localagents.db`.
- Zero console errors: `LOGS []` on every page load.

`#/endpoint/ai1` (t/page-ep-ai1.txt): header `ai1 · strata · live · host ai1 (up) …
last seen 0s ago; 1 job running`; tiers 0–3 all populated — tier 0 (endpoint up, node up,
model, 262,144 ctx, probe 59 ms), tier 1 (prompt 126.5, decode 254.4 tok/s, cache hit 99%),
tier 2 (slots 0/1, running 0, waiting 0), tier 3 hardware (VRAM 55.5/56.0 GiB, temp 74 °C,
206/600 W, PCIe, CPU, RAM, disk); 3 canvases + 4 sparkline paths; strip-now row
`idle/idle/generating/reading`; Jobs-on-it (1h): 3 jobs (mine running + two succeeded from
earlier sessions); Events (1h): none — truthful, 0 rows in the table; Requests (1h): 175
requests, with the earlier sessions' rows too.

`#/endpoint/ai9` (t/page-ep-ai9.json): dormant header — `ai9 · llama.cpp · dormant ·
host ai9 (down) … never seen up`; tier 0: `endpoint dormant, never seen up · node down ·
port 22: nothing` (the host_probes answer rendered); no tiers 1–3 (nothing to show); no jobs,
no events, no requests — all three empty-state sentences present. conn dot still live,
no console errors.

`#/job/d2c25f7e-8872-47dd-a21f-3655a2ce2c7c` (t/page-job.json): `✓ succeeded`, context gauge
`19,692 / 131,072 tokens, 15%`, gauge element present (`class="gauge"`), Usage-and-metrics
(usage totals, prompt 884.4 t/s, generate 82.7 t/s, spec acceptance 86%, cache hit 50%),
Tool-calls timeline (`4:32:11 PM Bash: date`), By-tool table, Requests table (hello 501,
two 200s, 3-totals row), and the **transcript tail rendered** — the real directory
`~/.local/state/localagents/jobs/20261029-163147-d2c25f7e` exists, tail runs from `[start]`
through `[thinking]/[tool]/[result] Thu Oct 29 04:32:12 PM EDT 2026 … [done] subtype=success`
with the `[metrics]` line. No console errors.

Page notes inherited from 3.2b (recorded here for the operator, not fixed): the endpoint-page
strip still labels its `now` from the history's newest point, and two aria-labels name the
range before the fetch catches up — visible on the ai1 page's strip as expected, cosmetic.

Failures at this step: none.

## Step 5 — Stop and verify

- **SIGTERM.** The live monitor (pid 4043751, listening on 18781) was SIGTERMed at 16:34:29;
  it exited within 3 s, `t/monitor.log` ends after the banner with **no traceback** (0 matches
  for "Traceback" in the whole log), and port 18781 stopped listening.
  Decision: the monitor had been started under `nohup`, so its exit status was never captured.
  To prove the exit-0 path I ran an identical twin — same code path, `--no-collect` so it
  touched neither the fleet nor the real db (`--db t/stopcheck.db`) — and captured
  `exit=0` on SIGTERM. The stop path in `serve.py` is identical (the stop event drives
  uvicorn's graceful shutdown, `GRACEFUL_SHUTDOWN_S=3`); the live run's log corroborates it.
- **Status line over the real database** (`uv run localagents statusline`, 16:34:53):
  ```
  +2 jobs in other sessions
  ```
  exit 0. The two are the live jobs of other sessions — the DeepSeek job on dgx1-a and my own
  ai1 session. The **smoke job was NOT on the line and never would have been after the smoke
  process exited** — this is the one item the brief got wrong, classified **(a) stale
  expectation** (recording, not fixing):
  - `status.collect` lists jobs of **live servers only** (`stopped IS NULL` + heartbeat within
    60 s, status.py:125). The smoke deliberately sets `stopped` at its exit — that is 3.5
    review F2's fix, so a finished smoke is never called live. I replayed `collect` at
    finished+2 s / +60 s / +121 s: the smoke job appears at none of them.
  - `KEEP_FINISHED_S = 120` is the retention for **finished jobs of a still-live server**
    (e.g. a run_agent job that ends while its MCP server keeps beating — that row stays on
    the line 120 s, then drops). For the smoke, whose server closes its row at exit, the job
    drops at exit. The code is right; the brief's parenthetical conflated the two rules.
- **Tests:** `uv run pytest -q` → `603 passed in 35.89s`.
- **git status:** clean — every file under `t/` is gitignored (hence `git add -f`); no tracked
  file outside `t/` was touched at any point. All evidence lives in the gate commits.

## Step 6 — Report, classifications, verdict

Failures encountered and classified (before touching anything):

1. **(a) stale expectation** — the brief's "the smoke's job … shows for 120 s after
   finishing, then drops" (step 5 above). The live run shows the smoke job leaving the
   status line at process exit; the 120 s rule belongs to finished jobs of still-live servers.
   Evidence: `collect` replay at finished+2/60/121 s + mcp_servers row with
   `stopped=1793305933.51`. No change made.
2. **(c) node fact / engine accounting** — 5 s `gen_tps` bins on Strata can spike above the
   engine's steady ceiling (256 vs ~110–140 tok/s) because Strata's `totals` counters advance
   live during streaming and a bin straddles one completion plus an in-flight request
   (step 2's hand computation). The monitor deltas exactly what `/metrics` reports; faithful
   mirror of a real artifact of live engine counters. Nothing fixed; operators reading the
   endpoint charts should know a single spike bin can be one half-finished request.
3. **Not a failure** — `endpoint_events` empty in the live window (cold start, nothing
   transitioned; collect.py:74 rule), and "3 requests, 2 with 200" in the smoke checklist
   (the third is the SDK's `/api/hello` 501 capability probe — the checklist is built to
   tolerate it and says so in the detail).
4. **(b) script bugs: none.** No script or test line needed fixing; the harness ran as copied.
   No (d) product defects found at any step.

Decisions I made where the brief left room:
- Kept the other session's seeded-world monitor on 18765 running; used 18781 and the real db.
- Ran the smoke from this worktree (`cwd` = here, isolation none — smoke's own design).
- Proved SIGTERM exit-0 with a `--no-collect` twin instead of restarting the live gate run
  (the nohup'd exit status was uncapturable); zero extra load on the fleet.
- Did not re-run a job to catch the smoke's row in the overview's jobs-table DOM (the smoke
  runs once; the snapshot-API poll at 16:31:48–16:32:12 covers the "during the smoke's job"
  requirement, and the table renders that same `jobs[]` two seconds later minus the finished
  row).
- Ran my own `GET /v1/models` reality-checks on all 12 endpoints — read-only GETs on config
  endpoints, which is exactly the grant's allowance.

### Verdict

**The gate is green.** The monitor came up against the real `models.yaml` with the real
database and classified the real fleet correctly — four live endpoints each serving the model
the snapshot names, seven dormant with the kernel's sentence, ai9's node down with the port-22
fallback answering `nothing` — held two minutes of correct 5 s/60 s cadence collection with
counters, Strata hardware rows and hand-verified rates; the single smoke pass filled the job,
request and tool-call tables end to end (17/17 checks, exit 0), the monitor watched the job
appear with live `Bash: date` activity and watched it finish, and every page — overview with
three distinct host classes and live-only fleet legends, ai1's full tiers 0–3, ai9's dormant
header, the smoke job's page with gauge, timeline, requests and real transcript tail — rendered
with the connection dot live and zero console errors. SIGTERM exited 0 with no traceback,
603 tests pass, and the tree is clean. The only brief-vs-reality gap was (a): the status
line's stopped-server rule, which is the product being right and the parenthetical being
stale. Nothing blocks merge.

Things the operator should look at with their own eyes (re-run `localagents monitor` over the
real db and open the page):
- The **ai9** host card (`node-down dormant`) and its endpoint page: "never seen up",
  "node down · port 22: nothing" — the fallback probe's answer rendered end to end.
- **ai1's tier-3 tiles during a live session** (78 °C, 206/600 W, VRAM 55.5/56.0 GiB) — the
  Strata hardware rows have never been shown this hot.
- The **smoke job page** `#/job/d2c25f7e-8872-47dd-a21f-3655a2ce2c7c`: gauge, tool timeline,
  requests table with the 501 hello row, and the transcript from the real job directory.
- The **fleet legends list live endpoints only** while the dormant cards say "never seen" —
  together the cold-start story of the fresh real database.
- The two cosmetic notes carried from 3.2b: the endpoint strip's `now` labels from the
  history's newest point, and two aria-labels naming the range before the fetch catches up.

### What the live data showed that the seeded world never did

- **The monitor watching its operator.** The biggest one: the agent running this gate IS the
  fleet's dominant load. The overview showed ai1 `generating, slots 1/1, decode 218 tok/s,
  cache hit 100%`, the running-jobs table's "current tool" cell quoting the very tool call
  that was fetching the snapshot, and the jobs[] ctx climbing toward 110k/262k in real time.
  The seeded world shows a running job as fixture rows; it never showed the feedback loop
  closed like this.
- A **single 60–110k-token streaming session dominating a single-slot engine**, with Strata's
  conversation cache parking/restoring it (153.9M/156.6M tokens prefix-reused since boot):
  every rate bin is one request, and cancelled requests land in the requests table as
  `output=0` rows (the 3:41 PM and 4:29 PM entries on the ai1 page) — half-streamed prompts
  the seeded fixtures never produced.
- **Strata's live-increment counters**: a probe bin straddling a request boundary reports a
  partial request's tokens (the 256 tok/s spike) — a data property no seeded capture exhibits,
  since seeded timestamps never bisect a request.
- **Dormant as the majority state of a fresh database**: eight "never seen" cards with kernel
  sentences, one node down with the port-22 answer, and an events table exercising only its
  cold-start rule — the seeded world shows one dormant decoy with a week-old history, not a
  whole fleet that has never been seen.
