#!/usr/bin/env bash
# accept.sh — the mechanical half of accepting a lane's branch (hints "Accepting the result").
# Adapted from ADOCK's tools/accept.sh for a Python/uv project. Prints facts, writes nothing to git;
# the judgement (the diff against the brief) stays with the reader, between this and tools/land.sh.
#
#   tools/accept.sh <branch> [--files <list-file>] [--claims <file>]
#
# Rows: worktree; base (BEHIND main → take files by path, never squash); stash entries; the commits;
# diff stat and file list; strays outside --files (one per line); package files (pyproject.toml,
# uv.lock); scratch artefacts in the diff (t/, REPORT.md, *.patch, *.log); raw NUL bytes in changed
# text files; /tmp in added lines; test functions ADDED by the diff per test file (cross-check the
# report's claims against this list); --claims: each non-empty line grepped literally in the diff.
# Exit 1 on raw NUL, scratch artefacts, or a NOT FOUND claim; BEHIND is a row, not a refusal.
set -u
MAIN=$(git rev-parse --show-toplevel) || exit 1
branch=${1:-}; [ -n "$branch" ] || { sed -n '2,14p' "$0"; exit 2; }
shift
files_list=""; claims=""
while [ $# -gt 0 ]; do case "$1" in
  --files) files_list=$2; shift;; --claims) claims=$2; shift;; *) echo "unknown $1"; exit 2;; esac; shift; done
rc=0
row() { printf '%-14s %s\n' "$1" "$2"; }
ind() { sed 's/^/               /'; }
wt=$(git -C "$MAIN" worktree list --porcelain | awk -v b="refs/heads/$branch" '/^worktree /{w=$2} /^branch /{if($2==b)print w}')
row worktree "${wt:-<none: reading the branch from $MAIN>}"
base=$(git -C "$MAIN" merge-base main "$branch") || { echo "no merge-base"; exit 1; }
behind=$(git -C "$MAIN" rev-list --count "$branch"..main); ahead=$(git -C "$MAIN" rev-list --count main.."$branch")
if [ "$behind" -gt 0 ]; then row base "BEHIND main by $behind commits (ahead $ahead; base ${base:0:7}): a squash is three-way from the base; land.sh aborts on a conflict, read the shared files' hunks"
else row base "on main's tip (ahead $ahead; base ${base:0:7})"; fi
row stash "$(git -C "$MAIN" stash list | wc -l) entries"
[ -n "$wt" ] && row wt-dirty "$(git -C "$wt" status --short | wc -l) uncommitted paths in the worktree"
row commits ""; git -C "$MAIN" log --oneline main.."$branch" | ind
row diff-stat "$(git -C "$MAIN" diff --stat "$base".."$branch" | tail -1)"
changed=$(git -C "$MAIN" diff --name-only "$base".."$branch")
echo "$changed" | ind
if [ -n "$files_list" ]; then
  stray=$(comm -23 <(echo "$changed" | sort) <(grep -v '^\s*$' "$files_list" | sort))
  [ -z "$stray" ] && row strays none || { row strays "OUTSIDE the story's list:"; echo "$stray" | ind; }
fi
pk=$(echo "$changed" | grep -E '(^|/)(pyproject\.toml|uv\.lock)$' || true); [ -z "$pk" ] && row package unchanged || row package "CHANGED (a uv add in the worktree? take by path): $pk"
scratch=$(echo "$changed" | grep -E '(^|/)(t/|REPORT\.md|PLAN\.md|REVIEW\.md)|\.(orig|rej|patch|log)$' || true)
[ -z "$scratch" ] && row scratch none || { row scratch "ARTEFACTS in the diff: $scratch"; rc=1; }
nul=""; for f in $changed; do
  case "$f" in *.png|*.jpg|*.gif|*.ico|*.woff*|*.tar|*.gz|*.zip|*.db|*.sqlite) continue;; esac
  n=$(git -C "$MAIN" show "$branch:$f" 2>/dev/null | tr -cd '\000' | wc -c); [ "$n" -gt 0 ] && nul="$nul $f($n)"
done
[ -z "$nul" ] && row raw-NUL none || { row raw-NUL "FOUND:$nul"; rc=1; }
tmp=$(git -C "$MAIN" diff "$base".."$branch" | grep -nE '^\+.*(^|[^A-Za-z0-9_])/tmp/' || true)
[ -z "$tmp" ] && row /tmp-in-diff none || { row /tmp-in-diff "added lines mention /tmp:"; echo "$tmp" | head -10 | ind; }
row test-funcs "added by the diff (cross-check the report against these):"
git -C "$MAIN" diff "$base".."$branch" -- 'tests/*.py' | awk '
  /^\+\+\+ b\//{f=substr($0,7)} /^\+[[:space:]]*(async )?def test_/{ s=$0; sub(/^\+[[:space:]]*(async )?def /,"",s); sub(/\(.*/,"",s); print f ": " s }' | ind
if [ -n "$claims" ]; then
  diff_txt=$(git -C "$MAIN" diff "$base".."$branch")
  while IFS= read -r c; do [ -z "$c" ] && continue
    if grep -qF -- "$c" <<<"$diff_txt"; then row claim "FOUND      $c"; else row claim "NOT FOUND  $c"; rc=1; fi
  done <"$claims"
fi
exit $rc
