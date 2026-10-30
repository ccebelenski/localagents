#!/usr/bin/env bash
# land.sh — squash a lane's branch onto main, commit, run the suite. The one tool here that writes
# to git (operator 2026-10-29: "that squash onto main and running the suite seem very mechanical").
# Run tools/accept.sh and read the diff against the brief FIRST; this script does no judgement.
#
#   tools/land.sh <branch> -F <message-file>
#
# Refuses (exit 1, main untouched) when: main has uncommitted changes; the stash has entries; the squash has nothing to apply; the squash
# conflicts (the merge is aborted and the conflicting paths printed). After the commit the suite runs
# as its own step (never in a chain with the commit): prints the commit hash and pytest's closing
# line; exit 2 when the suite failed, the commit left for the reader to judge (revert or fold).
set -u
MAIN=$(git rev-parse --show-toplevel) || exit 1
branch=${1:-}; msg=""
[ -n "$branch" ] || { sed -n '2,14p' "$0"; exit 2; }
shift
while [ $# -gt 0 ]; do case "$1" in -F) msg=$2; shift;; *) echo "unknown $1"; exit 2;; esac; shift; done
[ -n "$msg" ] && [ -f "$msg" ] || { echo "need -F <message-file>"; exit 2; }
cd "$MAIN" || exit 1
[ "$(git branch --show-current)" = main ] || { echo "refusing: not on main ($(git branch --show-current))"; exit 1; }
dirty=$(git status --short | wc -l); [ "$dirty" -eq 0 ] || { echo "refusing: main has $dirty uncommitted paths"; git status --short; exit 1; }
[ "$(git stash list | wc -l)" -eq 0 ] || { echo "refusing: the stash has entries (shared by every worktree)"; git stash list; exit 1; }
behind=$(git rev-list --count "$branch"..main 2>/dev/null) || { echo "no such branch: $branch"; exit 1; }
[ "$behind" -eq 0 ] || echo "note: $branch is behind main by $behind commits; squashing three-way from $(git merge-base main "$branch" | cut -c1-7)"
[ "$(git rev-list --count main.."$branch")" -gt 0 ] || { echo "refusing: $branch has no commits beyond main"; exit 1; }
if ! git merge --squash "$branch" >/dev/null 2>&1; then
  echo "squash CONFLICTS; merge aborted, main untouched:"; git diff --name-only --diff-filter=U; git merge --abort 2>/dev/null || git reset -q --hard HEAD; exit 1
fi
[ "$(git diff --cached --name-only | wc -l)" -gt 0 ] || { echo "refusing: the squash applied nothing"; exit 1; }
git commit -q -F "$msg" || { echo "commit FAILED; staged changes left in place"; exit 1; }
echo "landed $(git log --oneline -1)"
echo "suite:"
out=$(uv run pytest -q 2>&1); e=$?
echo "$out" | tail -1
[ $e -eq 0 ] || { echo "SUITE FAILED (exit $e); the commit stands for you to judge"; echo "$out" | grep -E '^(FAILED|ERROR)' | head -20; exit 2; }
