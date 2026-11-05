# Licences of the calibration set's sources

Licences are apportioned, not inherited (the calibration set design, §9, the operator's ruling of
2026-11-04). The project is MIT (the repository's LICENSE). Each source whose items enter a probe set
keeps its own licence, carried verbatim in a file here, and that file covers the checkers, labels and
any adapted text shipped for that source's items. It does not cover the rest of the project. A source
whose licence would reach past its own items does not enter. Benchmark instances are fetched from the
source at the pinned revision at run time and never vendored.

| Source | File | Licence | Where verified | Pinned revision |
| --- | --- | --- | --- | --- |
| ProgramBench | programbench.txt | MIT, code and data | the GitHub repository under facebookresearch, read by the operator 2026-11-04 | set by the build when its items enter |
| authored probes (fixtures/) | the repository's LICENSE | MIT | ours | |

Still to read before their items are taken: LiveCodeBench (the data licence), Aider Polyglot (each
Exercism track's licence). HumanEval, SWE-bench Verified and Terminal-Bench are MIT or Apache-2.0 by
their repositories and get their files when their items enter.
