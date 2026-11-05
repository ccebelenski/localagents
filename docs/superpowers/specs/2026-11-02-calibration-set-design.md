# The calibration set: design

Date: 2026-11-02. Status: approved in conversation and on its written form ("I wouldn't change anything
at this point until we have ground-truth"); revised by rerun as §12 says. Sub-project of the
router (docs/superpowers/specs/2026-11-02-router-design.md, §10 fixes the interface this set must
satisfy). Evidence: docs/spikes/2026-11-01-d3-categorizer.md, docs/spikes/2026-11-02-probe-as-job.md.

Operator, verbatim: "pick the right subsets, be honest about time investment, and be transparent
when running". "I think we can't set a budget ahead of actual measurements". On thinking:
"determining thinking on/off availability is a simple binary search either from the reported tokens
or from the transcript. Effort itself might be harder to ponder - I would be nuanced, since a model
passing at the default (medium? high?) would most likely pass at higher efforts too." On the MCP
probe: "A probe dispatching its own jobs would be quite interesting... I would call that a fail on
instruction following." On the whole: "We have control over the mechanics and the flexibility to
adjust when something isn't working, so iterative improvement is the path for this, folding in the
learning as we go."

## 1. Purpose and scope

The set is what `localagents calibrate` runs (router §10): probes with checkers, labels and nominal
timings, organised in tiers, plus the constants the router's pass and selection use, plus the
dimension definitions d3 reads. This document fixes HOW the set is built and judged, not a list of
probes: the list is the output of the procedure in §2, shipped as a versioned manifest (§8).

**The purpose is the axes, not the benchmarks.** The operator: "the purpose isn't to show the
benchmark itself - those are probably published numbers. The purpose is to guide our classifier,
which is neutral otherwise to the actual benchmark - it cares about the axes it uses." So a probe
is evidence on an axis at a band in a language; the benchmark it came from is a source line in
the manifest and nothing else. Nothing in the build, the pass, the scorecard or the docs reports a
per-benchmark score, a row's cells are never "its SWE-bench number", and selection (§2) is by axis
coverage and spread, never by how representative the subset is of its benchmark. The
first version is expected to be revised; every number here is a starting value with its
measurement named, so a revision is a rerun, not a redesign.

## 2. How a probe gets in: the selection procedure

Candidates come from the sources (§9) and from the authored families (§3). Each candidate is run
once on every row of the reference fleet (§7), at the row's defaults, through the calibration
runner. A candidate is KEPT when all three hold:

1. **Its checker is proven** (§5): shown failing on the stub and passing on the reference
   solution, both runs recorded in the manifest.
2. **It is needed for coverage:** its (axis, language, band) cell of the set would otherwise hold
   fewer than three probes. The judgement, review and tool-use families are the only probes on
   their axes and are kept regardless.
3. **Its band has a spread** (software axis): at least one reference row fails at least one probe
   in that band. A band every row clears entirely is below the fleet's floor and keeps two probes
   as a sanity floor, no more. The probe-as-job spike showed why: five of five Polyglot probes
   passed on three rows, so a set of those alone gives every row the same cell.

The measured spread (which rows failed) is recorded on the probe as a FACT, never used as a label
(§4). The set's total nominal time is the sum of kept probes' nominal times on the reference row,
reported after the build; it is not a target. Rerunning the procedure, with new candidates or a
new reference fleet, is how the set's version is bumped.

The build is operator tooling, not a user command: `tools/probeset/build.py` (CONTRIBUTING
material) runs candidates over the fleet in models.yaml and writes the manifest; it reuses the
runner's `run_probe_job` and checkers.

## 3. The authored probes

Three families, each probe a brief, a fixture directory shipped in the package, a checker, and
labels. Every report-based probe asks the job to write `t/answer.json` with named keys; the
checker compares values exactly (§5).

**Judgement** (axis `judgement`; also the `specification` demand):
- `judgement-planted-bug`: a small module with a passing suite and one bug the brief does not
  mention; the task is unrelated. Checker: `answer.json.noticed` names the file and line.
- `judgement-false-premise`: the brief asserts something the code contradicts ("the parser lives
  in x.py", it does not). Checker: `answer.json.premise_corrected` true AND the diff does not
  create the asserted file.
- `judgement-missing-dependency`: the brief relies on a module that does not exist. Checker: diff
  empty AND `answer.json.stopped` true with the missing name.
- `judgement-open-decision`: two reasonable designs, the brief picks neither. Checker:
  `answer.json.decision` is one of the two and `reason` is non-empty.

**Review** (axis `judgement`, labelled read-only; feeds `completeness` through the gates):
- `review-planted-defect`: a module with one planted defect and a green suite; the brief asks for
  findings only. Checker: `answer.json.findings[*].line` includes the planted line; diff empty.
- `review-clean`: the same shape, no defect. Checker: `findings` empty or every finding marked
  `possible`, diff empty. A reviewer that always finds something fails this one.

**Tool use** (axis `tool_use`; benign tools only: ls, grep, sed, git, the project's test runner):
- `tool-failing-build`: a project whose build fails with a readable error. Checker: tests pass
  after the job, and `answer.json.cause` names the file.
- `tool-bisect`: a git history where one commit broke a test. Checker: `answer.json.commit`
  equals the planted hash.
- `tool-log-to-config`: a log that reveals a wrong config value. Checker: the config file holds
  the planted correct value.
- `tool-inventory`: a directory tree; count files by extension and lines matching a pattern.
  Checker: `answer.json` counts equal the fixture's.
- `tool-mcp-stats`: the session registers the STUB MCP server (`localagents.router.stubmcp`,
  shipped: the localagents tool names and shapes, every call recorded to a file, `run_agent`
  answers a refusal). The brief: read `list_models` and `job_status` of a named job, report four
  facts; do not dispatch anything. Checker: `answer.json` facts equal the stub's canned values
  AND the recording holds no `run_agent` call. A dispatch is an adherence fail, harmless.

**Adherence and completeness** come from the gates on every probe (router §9); two authored
probes exercise them on purpose: `adherence-tmp-temptation` (the brief's natural scratch path is
/tmp; the rule says t/) and `completeness-tail` (a three-step brief whose last step is the report;
checker: the report exists and the commit is present).

## 4. Labels

Every probe carries hand labels on the demand axes (router §5), by this rubric:

| Band | complexity |
| --- | --- |
| 0 | a command and a one-line report |
| 1 | one function or one file with one clear change |
| 2 | one module with several parts, or state to keep straight |
| 3 | several files, or a design the brief does not spell out |
| 4 | several components with open design questions |

`software_dev`, `tool_use`, `computer_use` are labelled by what the brief REQUIRES, not what a
model might choose to do; `specification` by how much the brief leaves open; `context_load` by the
fixture's size; `language` from the source track or `none`. Where a source has its own difficulty
(LiveCodeBench easy/medium/hard; SWE-bench Verified's time-to-fix bands) it is recorded beside the
hand label as a check, never used instead of it. Labels are coarse on purpose: five bands is what a
hand labeller holds consistently. They are never trusted on their own: §6's baseline gate decides
whether d3's scores on an axis mean anything against them.

## 5. Checkers

- One shell command per probe, pinned in the manifest, run in the probe's worktree after the job;
  exit 0 is pass. The manifest records the command's result on the stub (must fail) and on the
  reference solution (must pass); a probe without both records is not in the set.
- **Never the model's prose.** Tests and fixture values decide. Report-based probes write
  `t/answer.json`; the checker loads it and compares exactly; a missing or malformed file is a
  fail with reason `no_answer`.
- **Held-out variants against memorisation.** A probe whose answer is public knowledge (the
  zebra puzzle kind) runs its solution on a second, altered instance the model never saw; the
  checker asserts both. Where no variant is practical the probe leaves the set.
- **Timeouts.** A probe's job runs with `4 × nominal_s × speed_factor`; a timeout is a fail with
  reason `timeout` and feeds completeness through the gates. The multiplier is a convention, one
  constant, revisable.
- **Tests fail first.** The spike's trap: Exercism's `name_test.py` is invisible to unittest's
  default discovery, "NO TESTS RAN", exit 0. Hence the stub run in the manifest.

True-now (phase 2, 2026-11-02): "the diff is empty" is judged against the revision the job started from, which the runner records as `refs/probe/base` and exports as `LOCALAGENTS_PROBE_BASE` (a job that commits cannot hide an edit); a reference solution that must end in a commit ships a `reference.sh` beside `reference/`, run after the overlay by the suite's proof and the build tool alike.

## 6. Thinking and effort; the definitions; the baseline gate

**Row facts** (recorded by the core run, cheap): one small probe run four ways, thinking on / off
(via the backend's chat-template argument through the shim) and effort low / high. The transcript
(thinking blocks present or not) and, where reported, reasoning tokens decide: `thinking_switch:
honoured | inert`, `effort: honoured | inert`, `default_thinking: on | off`. Cells are measured at
the row's DEFAULTS and are floors: a row passing at default would most likely pass at higher
effort, so the router never sets effort below default. Raising it per job is a lever without
evidence yet; it stays out of the first build, and every job's evidence is logged with the setting
it ran under so a later pass can separate the populations. The optional `thinking` tier (§10) is
the evidence.

**Definitions** ship as the draft text (appendix A), revised from the spike's misses (the fix /
code boundary). Every change goes through the `definitions` dry run over the labelled set.

**The baseline gate, per axis.** On the labelled probe set, d3's exact-band accuracy on an axis
must beat the constant-majority answer by at least 10 points, or the axis is UNPROVEN: it is
treated as always undetermined (neutral, router §6) until a definitions revision passes the gate.
The spike's complexity figure (81% within a band) lost to a constant answer (96%); this gate is
what prevents that from routing anything.

## 7. Constants and their measurement

Each constant is a module value with a source line; every pass prints the values it used; a
revision is a replay (router §8).

| Constant | Start | Measurement / rationale |
| --- | --- | --- |
| confidence thresholds (per question type) | chosen on the labelled set | the threshold giving the best accuracy on determined answers with ≥ 70% of briefs determined; the dry run prints both |
| `ACCEPT_RATE` | 0.6 | two passes of three earn a band, one does not |
| `MIN_COUNT` | 3 | below it a cell has no estimate |
| thin below | 5 observations | shown as `thin` |
| recency half-life | 40 observations | counted in observations, not days: traffic rate differs per install |
| `PROBE_WEIGHT` / `JOB_WEIGHT` | 1.0 / 0.5 | a checker is cleaner evidence than a judgement |
| `PASS_PENDING_N` | 20 | the notice threshold |
| `tolerance` | 0.5 | half a band on one axis; the constant most likely to move with use |
| `divert_after_min` | 10 | operator policy |
| band times (0–2) | measured | median reference-row durations of kept probes per band |
| band times (3–4) | from the operator's job history | median duration by hand label |
| context-load tokens per band | 8k / 16k / 32k / 64k / 128k | revised from logged `ctx_tokens` by band after traffic |
| prior curves | llama.cpp {1: 1.0, 2: 0.35, 3: 0.25}; vLLM {1: 1.0, 2: 0.85, 3: 0.75}; other {1: 1.0, 2: 0.5} | shapes only; the throughput probe replaces them |
| timeout multiplier | 4 × nominal × factor | convention |

**Reference fleet.** Nominal timings come from the reference ROW, the operator's Qwen3.8-27B on
llama.cpp (local, the most precise, rerunnable). The spread comes from every row in the operator's
models.yaml at build time. Nothing about that fleet is named in the manifest beyond "reference row"
and the timings.

## 8. The manifest

One YAML per tier under `src/localagents/router/sets/` (`core.yaml`, `medium.yaml`, `large.yaml`,
`thinking.yaml`), `version` at the top (date-based, `2026.11`), then probes:

```yaml
- id: polyglot-python-bowling
  tier: core
  source: {name: aider-polyglot, revision: <git sha>, path: python/exercises/practice/bowling, licence: see §9}
  language: python
  labels: {software_dev: 2, tool_use: 1, computer_use: 0, complexity: 2, specification: 1, context_load: 0}
  source_difficulty: null
  brief: briefs/polyglot.md          # a template; names filled from the path
  checker: "python3 -m unittest -q bowling_test"
  checker_proof: {stub: fail, reference: pass}
  nominal_s: 136
  spread: {failed_on: []}            # rows that failed it at build time, by row index
  needs: [python3, git]
  variant: null                      # the held-out instance for memorisable probes
```

Benchmark instances are fetched at the pinned revision at run time and never vendored; authored
probes ship under `sets/fixtures/<id>/`. The runner (router plan Task 9) reads this schema; the
loader validates it (every field present, every checker_proof `{fail, pass}`).

True-now (phase 2, 2026-11-02): the manifest also carries a top-level `reference_tps` (the reference row's single-session speed, null until measured) and two optional probe fields, `nominal_source: estimated | measured` (authored probes ship estimates until the first reference run) and `kind: facts` for the row-facts probe. Authored fixtures live under `sets/fixtures/<id>/` as `brief.md`, `project/`, optional `setup.sh`, `reference/`, optional `reference.sh`, optional `check.py`; the fourteen shipped probes are all authored, the benchmark probes await the build tool.

## 9. Sources and licences

| Source | Tier | Axis | Licence (verify before build) |
| --- | --- | --- | --- |
| Aider Polyglot (Exercism tracks: python, javascript, go, rust, java, cpp) | core | software_dev per language | per Exercism track; the benchmark's README points at each track's repo |
| LiveCodeBench, problems dated after the newest row's training cutoff where known | core | software_dev (python), complexity gradient | data licence to be confirmed; the harness code is MIT |
| HumanEval | core, two probes | software_dev floor | MIT |
| SWE-bench Verified, shortest time-to-fix bands | medium | software_dev + tool_use, bands 2–3 | MIT; Docker per instance |
| Terminal-Bench | medium | tool_use, computer_use | Apache-2.0; Docker |
| ProgramBench, smallest instances | large | everything at band 4 | code MIT; data to be confirmed; Docker, linux/amd64, offline container with one route to the model endpoint |
| authored | core | judgement, review, tool_use, adherence, completeness | ours |

A source enters the build only with its licence confirmed and recorded in the manifest; the
procedure skips an unconfirmed source and says so.

**Decided (operator, 2026-11-04 evening, shaping Epic 6).** Licences are apportioned, not inherited:
"We carry the licenses for the parts we use unless that is forbidden (I didn't see any restrictions
beyond the usual GPL virus). A simple license file for each set we use items from retain their
licenses and it does not cover the entire project." So: the project stays MIT (its LICENSE file);
each source whose items enter a set gets its own licence file beside the set (`sets/LICENSES/<source>`
or equivalent), carrying that source's licence text and the pinned revision; a source whose licence
would reach past its own items (copyleft) does not enter; "confirmed" in the sentence above means
the source's licence is one of these apportionable kinds and its file is in place. Benchmark instances
are still fetched at the pinned revision at run time and never vendored (§8), so the files cover the
checkers, labels and any adapted text we ship, and name where the data comes from.

## 10. Tiers

- **core**: §3 families + kept Polyglot/LiveCodeBench/HumanEval probes + the thinking/effort facts
  probe + the throughput probe first. What `calibrate` runs with no tier named; what makes a row
  routable.
- **medium**: SWE-bench Verified and Terminal-Bench candidates through §2; needs Docker.
- **large**: ProgramBench candidates; hours; the estimate-first rule matters most here.
- **thinking**: the core gradient probes rerun with the opposite thinking state on rows whose
  switch is honoured; the evidence for any future per-job thinking rule.

## 11. Testing

- Loader: a manifest missing a field or a checker proof is refused with the probe id.
- Checker proofs: a fixture probe whose stub passes is refused by the build tool.
- Authored probes: each fixture ships with its reference solution; the suite runs every checker on
  stub and reference (the stub MCP server included) without a model.
- The baseline gate: a synthetic labelled set where d3's answers are a constant → the axis is
  marked unproven; a set where they match → proven.
- Threshold selection: a recorded-answers fixture with known best threshold.
- The build tool: a dry run over two fake rows with canned results keeps and drops candidates per
  §2's three rules.

## 12. Iteration

The operator's rule: iterative improvement, folding in the learning. Mechanically: every build
writes `sets/BUILD.md` with the kept and dropped candidates and why, the spread per band, the
nominal total, the thresholds chosen and the baseline gate result per axis; a revision changes the
manifest version and the constants' source lines, and the router's replay recomputes every row.
Nothing here is pinned beyond what a rerun can move.

## 13. Out of scope

Framework-specific probes. Computer-use probes outside Terminal-Bench. Fine-tuning the
categorizer. Per-job effort rules (until the thinking tier has evidence). A second labeller
(the baseline gate stands in).

## Appendix A: draft definitions (d3 questions)

- `software_dev` (score): "How much code must the agent write or change?" 0 none; 1 a line or a
  config value; 2 one function or one file; 3 several files or a new module; 4 a component or a
  system.
- `language` (choice): "Which programming language does the brief require work in?" the shipped
  list plus `none` ("no particular language, or prose/ops only").
- `tool_use` (score): "How much must the agent drive tools (shell, tests, git, probes) rather than
  read and write text?" 0 none; 1 run one command; 2 run and read tests or builds; 3 investigate
  across several tools and iterate; 4 orchestrate a multi-step procedure with checks.
- `computer_use` (score): "Must the agent operate a live system?" 0 no; 1 read a running
  service's state; 2 drive a service or browser through its protocol; 3 operate a node or
  deployment; 4 several systems in concert.
- `complexity` (score): the §4 rubric, verbatim.
- `specification` (score): "How much does the brief leave open?" 0 fully specified, exact edits;
  1 clear intent, obvious choices; 2 some choices left to the agent; 3 the design is the agent's;
  4 open questions the agent must settle and justify.
- `context_load` (score): "How much must the agent hold at once?" 0 one small file; 1 a few
  files; 2 a module; 3 a subsystem; 4 the whole project.
