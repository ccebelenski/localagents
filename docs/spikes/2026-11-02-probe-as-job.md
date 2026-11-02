# Spike: a benchmark exercise as a calibration probe (2026-11-02)

Question (feeds the calibration set spec, router design §10): what does one Aider Polyglot
exercise cost as a headless Claude Code job on a local model, does the checker tell the truth,
and what does the run look like across rows? Operator's nod: "your spike seems reasonable as data
feeding later steps."

Setup, all throwaway: `Aider-AI/polyglot-benchmark` cloned (34 Python exercises; content is
Exercism's, licensed per track, see its README); three exercises copied into their own git repos
with the stub, the test file and `.docs/instructions.md` only (no `.meta/example.py`); one job
per exercise per row through `run_agent` as it is today (`isolation: none` in the throwaway repo,
`max_turns` 40–60, the same brief with the exercise's names filled in); checker = the exercise's
own unittest module, run by me after the job.

## Results

| Exercise | Row | Result | Wall clock | Turns | Output tokens |
|---|---|---|---|---|---|
| pig-latin | Qwen3.8-27B, llama.cpp | pass | 43 s | 7 | 4.4k |
| pig-latin | Qwen3.8-Flash-Next, Strata | pass | 67 s | 7 | 4.1k |
| pig-latin | DeepSeek-V4.1-Flash, vLLM | pass | 101 s | 8 | 3.6k |
| bowling | Qwen3.8-27B, llama.cpp | pass | 136 s | 7 | 13.4k |
| zebra-puzzle | Qwen3.8-27B, llama.cpp | pass (computed search, not pasted) | 66 s | 8 | 6.6k |

Every job committed exactly the one file, ran the exact test command, reported its judgement
calls and noticed the same things (the discovery trap below, `__pycache__`, `master` vs `main`).

## Findings

1. **The checker trap.** Exercism test files are `name_test.py`; unittest's default discovery
   pattern is `test*.py`, so `python3 -m unittest` prints "NO TESTS RAN" and exits 0. My first
   brief said to run exactly that, and a checker written the same way passes an empty stub. I
   caught it only because I ran the checker on the stub first. Rule for the set spec: every probe
   pins its checker command, and a probe enters the set only after its checker has been shown
   FAILING on the stub and passing on the reference solution.
2. **Pass/fail at this level does not separate these rows.** Five of five passed. These exercises
   are Aider's hard subset of Exercism, yet all three models clear them in one or two minutes. A
   core set built only from them would give every row the same software_dev cell. The set needs a
   measured difficulty gradient: probes that at least one current row fails, found by running
   candidates across rows and keeping the ones with a spread. Time and the judgement probes carry
   the signal where pass/fail does not.
3. **Speed factor from one exercise.** pig-latin: Strata/Flash 1.6× and vLLM/DeepSeek 2.4× the
   27B's wall clock, on similar token counts. One probe is noisy (DeepSeek's turn count and prefill
   differ); the throughput probe plus three or more timed probes is the right estimate, as the
   design says.
4. **Nominal cost.** One to two minutes per probe on this class of hardware for small exercises.
   A core set of thirty such probes is thirty to sixty minutes per row before the judgement and
   review probes; the design's "estimate first, recheck after three" holds.
5. **Puzzle exercises invite pasting.** zebra-puzzle's tests check two string answers; a model
   that knows the puzzle can return them without a search. The brief forbade it and the model
   complied, but a checker cannot tell. Either such exercises leave the set or their checker
   includes a structural assertion (a search routine exists and is called).
6. **The real runner needs a worktree and a cleanup.** `isolation: none` in a throwaway repo was
   fine here; the calibration runner uses a worktree per probe (plan Task 9) and should `.gitignore`
   `__pycache__` in the probe repo so the file-set gate does not see it.

## What this changes

Nothing in the router design. For the calibration set spec: the checker rule (1), the gradient
requirement (2), the paste guard (5), and a brief template that names the exact test command
(this spike's brief, with the exercise names filled in, worked unchanged on all three rows).
