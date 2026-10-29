# Retrospective: Epic 1, the MCP server writes SQLite (2026-10-29, overnight)

Shape per the operator's rule (ANAS EPICS §2): counts from the records only; finds by finder;
the operator's verdicts verbatim; lettered ruling proposals only from things done repeatedly with
results; events in "what cost"; open rulings read out; a closing step that moves the shipped
narrative out of the live plan.

## What shipped

Version 0.2.0 on main, unpushed (operator rule: always hold, review together). Range
0100fb6..<final>, 30+ files, ~4100 insertions. Suite 81 → 184+ tests. Nine stories (1.1–1.7
plus two review folds, 1.8 in flight), three coordinator fold commits, one live smoke.

| story | built by | turns | wall | reviewed by | verdict |
|---|---|---|---|---|---|
| schema spec | coordinator + operator (12 design turns) | | | Qwen iq3/ai1 (16 turns, 3.5 min) | 15 findings, 11 accepted, 2 real defects (endpoint load had no home; prune pattern) |
| 1.1 schema module | Opus-high | 61 tool uses, 11 min | | Qwen q4/ai2 (20 turns, 4.5 min) | clean; nits folded |
| 1.2 jobs write path | Qwen iq3/ai1 | 65 | 5.5 min | DeepSeek/dgx1-a (49 turns, 17 min) | 1 low defect + measured loop-stall risk; 4 folded, 3 deferred |
| 1.3 requests write path | Qwen q4/ai2 | 34 | 5 min | DeepSeek batch (48 turns, 25 min) | clean on all three constructed probes |
| 1.4 tool-calls write path | Qwen iq3/ai1 | 37 | 3.3 min | DeepSeek batch | clean |
| 1.5a status line read path | Qwen iq3/ai1 | 83 | 11 min | DeepSeek batch | 3 nits, 7 possibles → 1.7 |
| 1.5b summary read path, exposure | Qwen iq3/ai1 | 119 | 12.5 min | Opus-high (38 tool uses, 6.5 min) | 2 low defects, unbounded list → reviewer folded 8 items |
| 1.6 docs, version | Qwen q4/ai2 | 50 | 14 min | (docs; every sentence cited to file:line) | caught the Bash-input discrepancy independently |
| 1.7 review fold | Qwen iq3/ai1 | 94 | 6.4 min | Opus-high (52 tool uses, 11 min) | 2 NEW defects from the to_thread change, 2 pre-existing → reviewer folded 13 items |
| Opus fold of 1.5b (47602e0) | Opus-high | 72 tool uses, 7.6 min | | DeepSeek (50 turns, 22 min) | no blocking defect, 10 items → 1.8 |
| Opus fold of 1.7 (6005f94) | Opus-high | 180 tool uses, 27.5 min | | DeepSeek (pending) | pending |
| 1.8 review fold | Qwen iq3/ai1 (pending) | | | DeepSeek (pending) | pending |

Every local job succeeded; no caps, no DeepSeek trouble (the deployment the operator flagged as
untested held four reviews of 48–50 turns each).

## Finds by finder

- **Operator (design review, before any code):** pid is not a key; GUIDs; no dual-purpose
  key; normalise for consistency not space; `servers` is not the YAML and the YAML mutates;
  `path` is a lookup; timestamps question; "models reviewing other models". Eight design
  corrections, every one load-bearing.
- **Cross-model reviews (7, all construct-it):** 2 real defects in the schema (Qwen), 1 low
  defect + 1 measured design risk in 1.2 (DeepSeek), 0 in 1.1/1.3/1.4/1.5a, 2 low in 1.5b
  (Opus), 2 new + 2 pre-existing in 1.7 (Opus), 0 blocking in the 1.5b fold (DeepSeek).
  Every review ran its probes rather than reading; 0 false defects.
- **Builders correcting briefs (8 of 8 lanes disclosed something):** the circular import
  1.2 would hit (1.1); the import-time real-database open (1.2); two write transactions per
  request (1.3); a test-order conflict in the brief (1.5a); the NULL-heartbeat hole in the
  brief's own SQL (1.5a); partial metrics disagreement (1.5b); the Bash-input vs commit-message
  discrepancy (1.6); conftest pinning only one env var (1.7).
- **Reviewers fixing their own findings (2):** Opus folded 8 and 13 items, each test-first,
  each item red before green, 147→159 and 167→184 tests.
- **Coordinator reads:** the lazy open, the conftest generalisation, the `IS NULL` sweep
  case, `synchronous=NORMAL`, the composed both-sides-added conflict in jobs.py.
- **The suites:** 1.1's own race tests found the WAL-switch "database is locked" on a fresh
  file (4 of 15 runs) before any review did.
- **Live smoke (coordinator, ai1):** one real job through the merged runner and shim; rows,
  summary, status line and the standalone statusline all correct; 0 finds.

## The operator's verdicts, verbatim

- "I wouldn't use the PID as a PK - it can be reused."
- "Use GUID's - they don't collide. new one on startup."
- "I don't like it - you're creating a dual purpose PK that requires active code to manage
  in the workflow - I don't like that pattern."
- "the value isn't the savings, it's the consistency"
- "Is servers just another view of the .yaml data? That's fine, but it loses some information
  if so, and the yaml file isn't 1:1 reportable since it mutates"
- "Do your own review now of the schema, I think we're close."
- "pass it to an agent for an independent review - qwen3.8-flash would be a good choice.
  Models reviewing other models is valuable."
- "all work is reviewed by a model that didn't do the initial work - this is a good pattern"
- "Use lanes to build as much as possible" / "Always hold - we review together" /
  "if you have deepseek problems tonight just assign the work elsewhere"

## Ruling proposals (done repeatedly, with results)

- **(a) The reviewer fixes its own verified findings, test-first, and a third model reviews
  the fix.** Two rounds (8 and 13 items), every item red-then-green, zero skips, and the fix
  reviews found no blocking defect. Cheapest precise builder on the record.
- **(b) A construct-it review brief holds at most four probes, one door each, "write the
  report after each probe".** Seven reviews, 16–50 turns, 3.5–25 min, every probe reached,
  every finding with a reproduced scenario and a file:line. (Confirms the existing ≤3/≤5
  rules with tonight's numbers on Qwen, DeepSeek and Opus.)
- **(c) A brief passed by path ("cat <file>, follow it") with the must-not-be-lost rules
  repeated inline.** Eight builds, eight followed; two lanes read the brief and corrected it.
- **(d) A story that moves work off the event loop gets a cancel-mid-operation construct-it
  probe before merge.** Once only (1.7's to_thread change introduced two defects the Opus
  review found by cancelling mid-git): recorded as an EVENT, not lettered; propose a ruling
  if it recurs.

## What cost

- The real database was polluted twice by lanes' own isolation checks (a "poisoning" run that
  exported the real path) and deleted twice; the conftest now pins all three variables.
- The 1.7 to_thread change was right and still introduced two defects; the review caught them.
- DeepSeek reviews take 17–25 min each (vs 4–14 for Qwen); every one was worth the wait.
- Opus lanes: ~1.0M subagent tokens over five dispatches (quota); local lanes: zero quota.

## Open rulings

- `wait_job` on a finished foreign job could return its row instead of refusing (recorded,
  not built).
- `tail_lines=0` returns the whole transcript (pre-existing nit, untouched).
- The `count_tokens` 404 fallback logs http_status 200 (pre-1.3 record shape).
- `owner` is the raw parent pid; a wrapper launch folds a session's own jobs into "+N other".

## Closing step

Epic 1's shipped narrative moves out of the live plan: `docs/EPICS.md` keeps one line per story
with commit and job ids; this file holds the story. Epic 2 (monitor core) starts from the
monitor spec with story 2.1 (fixtures captured through a manifest test) and assumes these tables.
