# tools/ — the coordinator's scripts (no model call in any of them)

Copied-and-adapted from ADOCK's `tools/` (2026-10-29). Each prints facts; the judgement stays
with the reader. Run from anywhere inside the checkout (they find the root).

| When | Command | What it removes from the by-hand list |
|---|---|---|
| accepting a branch | `tools/accept.sh <branch> [--files list] [--claims file]` | base (BEHIND → take by path), stash, worktree dirt, commits, stat, strays, package files, scratch artefacts, raw NUL, `/tmp` in added lines, the test functions the diff ADDS (cross-check the report), literal claims. Writes nothing. |
| landing a branch, AFTER reading its diff against the brief | `tools/land.sh <branch> -F msg.txt` | the squash onto a clean main, the commit, the suite as its own step with its closing line. The one script that writes to git; refuses on dirt, stash, nothing to apply, or a conflict (behind is a note: the squash is three-way). |
| a page story's acceptance | `tools/smoke/` (see its README) | the DOM-only browser probe over the DevTools protocol (ruling (j)). |
| running the router's d3 decision server in the background | `tools/d3.sh start \| stop \| status \| log` | the foreground `localagents d3 serve` dying with the terminal (setsid, a pidfile, a log), the wait on `/health` until the weights are resident (start returns only when the server answers ok, and stops it when the loader failed, with the reason), and the port read back from the start line. Needs the tool install WITH the router extra: `uv tool install -e '.[router]'`. |
| running the monitor dashboard in the background | `tools/monitor.sh start \| stop \| status \| log` | the uv command, the browser opened by hand, the "port in use" dead end when it is already up (start opens the running page), and the monitor dying with the terminal (setsid, a pidfile, a log). |

The order is: `accept.sh` → read the diff → `land.sh` → remove the worktree by hand (never
one a running review has as its cwd).
