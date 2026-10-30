# Handoff

Resume note for the next session. Rewritten at every session close; history is in git.

## 2026-10-29, late evening

**State.** The monitor MVP (Epics 1–3 in `EPICS.md`) is feature complete on local main at
`c4eddee`: 611 tests, 126 commits ahead of origin, nothing pushed, no worktrees, no stray
processes, no stash. Every story and fold was built by one model and reviewed by another;
the live gate is green (`gates/2026-10-29-epic3-live-gate.md`); the three retros are under
`retros/`. Standing rulings (a), (d), (e) and (f)–(m) are inked in `EPICS.md`; the operator's
rule on them: guidelines, not stone tablets, reworked as the work needs.

**The operator's first impression of the page** (over the real fleet, `uv run localagents
monitor`): "needs work - especially on presentation. The bones are looking okay." Looked at
only briefly; the detailed findings come next session.

**Next session starts here.**
1. The operator walks the page and gives findings (overview, `#/endpoint/<id>`,
   `#/job/<id>`); presentation first. Capture them verbatim in `EPICS.md` as a new story
   group under Epic 3 (3.7 "Presentation pass", cut per finding) before any lane is briefed.
2. Presentation work is page work: ruling (j) (DOM-only browser smokes, never a screenshot
   read by a lane), (k) (one file per parallel page story, the `pages` hook in
   `static/app.js`), (l) (contract gaps become the next story's input). The DeepSeek lane
   reads at most one image per prompt; screenshots reach the operator only through the
   coordinator. The DevTools harness is at the scratchpad `smoke/cdp.mjs` and `conn.mjs`
   (chrome-headless-shell from Playwright's cache; node at
   `/home/linuxbrew/.linuxbrew/bin/node`); copy it into the repo under `tools/` if the
   scratchpad is gone.
3. Each finding's fold is reviewed by a model that did not build it; the coordinator judges
   per pass whether a chain needs another (ruling (g)).

**Decided, do not reopen.**
- Push: held. The operator gates it by reviewing the built work, then a refine cycle; feature
  complete is not the gate.
- The collector's lost-job sweep: deferred to Epic 4; the MCP server stays the only sweeper.
- Epic 4 (SSH guided setup, start/stop recipes, node telemetry): "not yet".
- The MCP server never imports from `localagents.monitor`; api keys are never stored.

**Open, small.** The overview's history fetch is N+1 per refresh (one per live endpoint for
the strip's segments); Strata's `_mb` tiles read `MB` unitless until Strata documents the
sample window. Both in the Epic 3 retro's open rulings.

**Endpoints as of tonight.** ai1 and ai2 (Strata), dgx1-a (vLLM, DeepSeek V4.1 Flash, one
image per prompt), llamacpp (local llama.cpp, Qwen3.8-27B, 2 slots); ai9 dead. The operator
brings models up by hand.
