#!/usr/bin/env bash
# tools/statusline.sh: a Claude Code status line for someone who has none (INSTALL.md step 4a).
#
#   settings.json: {"statusLine": {"type": "command", "command": "<checkout>/tools/statusline.sh",
#                                  "refreshInterval": 5}}
#
# First line: the session's model, directory and context use (when jq is installed); then one
# row per local endpoint and job from `localagents statusline` (nothing when no job runs). A
# status line of your own instead: add the `localagents statusline` lines below to it.
input=$(cat)
line=""
if command -v jq >/dev/null 2>&1; then
  model=$(jq -r '.model.display_name // empty' <<<"$input" 2>/dev/null)
  dir=$(jq -r '.workspace.current_dir // .cwd // empty' <<<"$input" 2>/dev/null)
  pct=$(jq -r '.context_window.used_percentage // empty' <<<"$input" 2>/dev/null | cut -d. -f1)
  line="${model}${dir:+ ${dir##*/}}${pct:+ ${pct}% ctx}"
fi
local_lines=$(command -v localagents >/dev/null 2>&1 && timeout 2 localagents statusline 2>/dev/null)
[ -n "$line" ] && printf '%s\n' "$line"
[ -n "$local_lines" ] && printf '%s\n' "$local_lines"
exit 0
