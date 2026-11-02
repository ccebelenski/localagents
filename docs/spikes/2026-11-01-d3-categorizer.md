# Spike: can a decision model categorize our briefs? (2026-11-01)

Question: for the proposed routing layer (a router that picks the agent for a job, with
categorization first), is a Decision 3.0 model (vllm-sr/d3-*) a better categorizer than a
prompted one-shot on a served chat model, and is it practical on a CPU?

Everything here is throwaway: scripts and raw results live in the session scratchpad
(`spike/run_d3.py`, `run_prompted.py`, `score.py`, `questions.py`, `*.jsonl`). The oracle is the
coordinator's hand labels of the 135 briefs in the job database (57 of them "cat <file> and
follow it" pointers, resolved to their files, which all still existed).

## The shapes used

Three questions per brief: `kind` (choice: code / fix / review / docs / test / ops, each with a
one-line definition), `difficulty` (score 0–4 with a legend), `read_only` (yes/no). The same
definitions were given to the prompted models as a system prompt asking for one JSON object.

Label spread: review 56, code 38, fix 35, ops 4, docs 1, test 1. Difficulty labels are coarse
(mostly 2 and 3). The set is nearly all coding and review work from this project; it says nothing
about creative writing or summarizing.

## Results

| Categorizer | Where | kind strict | kind w/ alternates | read_only | difficulty within 1 band | p50 latency | memory |
|---|---|---|---|---|---|---|---|
| d3-flash 9B | RTX 6000 Ada | 85% | 87% | 129/135 | 52% (MAE 0.85) | 1.13 s | 19 GiB GPU peak, 17 GiB host RSS |
| d3-mini 4B | RTX 6000 Ada | 84% | 85% | 129/135 | 81% (MAE 0.68) | 0.79 s | 12 GiB GPU peak |
| d3-lite 0.8B | RTX 6000 Ada | 76% | 78% | 131/135 | 86% (MAE 0.64) | 0.18 s | 3 GiB GPU peak |
| Qwen3.8-27B (llama.cpp, no thinking) | local GPU | 84% | 87% | 131/134 (1 parse fail) | 57% (MAE 1.16) | 1.0 s | the served model |
| Qwen3.8-Flash-Next (Strata, no thinking) | LAN box | 85% | 89% | 133/135 | 61% (MAE 1.13) | 4.2 s | the served model |

Briefs average ~6k characters (~1.5–7k tokens); d3's 20 ms figures on its card are for 200-token
inputs, hence the ~1 s here.

**The misses are the shape's, not the model's.** d3-flash missed 20, Qwen-27B missed 21, and 16
of those are the same briefs: almost all "fix" (a fold of review findings) read as "code". A
fold is a small build, so the boundary as written is fuzzy. d3's top probability was ≥ 0.6 on 12
of its 20 misses, so confidence alone would not have flagged them; the definitions need work.

**The router must see the real brief.** On the 57 pointer briefs fed as the job record stores
them (the "cat <path> and follow it" line, not the file), d3-flash fell to 65% strict.

**CPU is not practical as shipped, on this machine** (Core Ultra 9 285K, 24 threads, AVX2 only,
no AVX-512/AMX). The checkpoint pins a bf16 backbone (`dtype` other than None/auto is refused),
and bf16 on an AVX2 CPU is emulated:

| Model | CPU seconds per brief (bf16) |
|---|---|
| d3-flash 9B | 310, 187 (two briefs, then stopped) |
| d3-mini 4B | 176, 166, 151 |
| d3-lite 0.8B | 63–79 |

The time scales weakly with parameter count (11× the parameters, 3–4× the time), which suggests
a kernel fallback rather than the weights is the cost; not investigated. On a CPU with AMX or
on a GPU the picture would differ. A one-second GPU call is fine for a job-level decision.

## Recommendation

- Categorize with a d3 model on a GPU. d3-mini is the sweet spot here: within a point of
  d3-flash on category, better on difficulty, 12 GiB. d3-lite is a fallback where memory is
  tight; it loses ~8 points on category.
- A prompted Qwen one-shot is an equal categorizer when a chat model is already up, at the
  cost of a prompt, parsing, and 1–4 s. d3's advantages are typed answers with probabilities per
  option, no text to parse, and a definitions-only interface (the category set is a JSON object
  the coordinator edits).
- Spend design effort on the category definitions and on a calibration set that covers the
  categories we care about (the 135 briefs do not), not on the categorizer.
- The router takes the brief text itself; a pointer brief is resolved before the decision.
- Serving: the package's own `d3_server.py` (stdlib HTTP, `/v1/systemone`, bearer key) is
  enough; no Docker, no semantic-router.

True now (2026-11-02): the recommendation above was written for the operator's hardware. The
router design (docs/superpowers/specs/2026-11-02-router-design.md) targets a fleet with a d3 model
always available, so d3 is the categorizer, full stop; the prompted one-shot is not a fallback
in the design. The CPU result stands: no CPU path.

True now (2026-11-02, design review): the difficulty figure above is not evidence of anything. The
labels are 90 twos and 29 threes of 135, so a constant answer of 3 is within one band 96% of the
time and a constant 2 is 90%; d3-mini's exact-band accuracy is 42%. The within-one-band column
should be read against those baselines, and the complexity axis counts as unmeasured until the
calibration set measures it against constants. The category result stands.
