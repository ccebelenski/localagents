# Installing localagents

**These instructions are for the coding agent.** If you are a person: open Claude Code or
OpenCode and say *"Install localagents by following
https://github.com/ccebelenski/localagents/blob/main/INSTALL.md"*. The agent asks you what it
needs to know and shows you each change before it makes it. The same sentence with *"update"*
or *"uninstall"* in place of *"install"* does those. The README's Quick start lists the same
steps for doing it by hand.

---

You are a coding agent, Claude Code or OpenCode, and your user has asked you to install
localagents. localagents is an MCP server that lets a coordinating session (you, from now on)
hand coding tasks to models on the user's own llama.cpp, vLLM or Strata servers: `run_agent`
starts a headless Claude Code or OpenCode session on a local model and returns its record. Read
this whole file before you start.

## Rules

1. **Check before you act, and after.** Once the package is installed (step 2),
   `localagents check` reports every install item as `ok`, `warn`, `fail`, `absent` or `info`,
   with a fix for each one that is not ok; `--json` gives the same as data. Run it before a
   step and do only what it shows is missing; run it after the step to see that it took. The
   install is re-runnable this way, and an update or an uninstall starts from the same report.
   Do not decide from memory or from a file's look that something is done.
2. **The user decides; you do the work.** Ask everything you need in one message at the
   start (step 0), not one question per step. Before you change anything outside the checkout,
   say which file and what changes; for an existing file, show the change and keep a copy
   (`<file>.bak-localagents`). Never delete a file you did not create in this install.
3. **Use the clients' own commands** where they exist (`claude mcp add`, `opencode mcp add`).
   Edit a settings file only where no command does it, and edit it as data, never by
   rewriting the file from memory: read it, change the one key, write it back.
4. **No `sudo`, no system packages, no shell profile edits** unless the user says yes to that
   specific change.
5. **Stop and say so** when something fails in a way this file does not cover. Quote the
   command and its output; do not improvise around it.
6. **Secrets.** An `api_key` goes in `models.yaml` only. Do not repeat it in your messages.

## 0. Ask

Look first, then ask in one message:

- `command -v claude opencode git uv` and `uv --version`; `localagents check --json` if
  `localagents` is already on PATH (a re-run: skip whatever it reports `ok`).
- Whether your working directory is already a checkout of this repository (`pyproject.toml`
  with `name = "localagents"`). If it is, use it and skip the clone.

Then ask the user:

- **Which clients to set up.** Offer the ones installed: Claude Code, OpenCode, or both. You
  are one of them; say which.
- **Where the checkout goes** (default `~/localagents`).
- **Their servers.** For each: its address (`http://host:port`), and an `api_key` if it was
  started with one. Ask whether any share a machine; those get the same `host:` name. If they
  do not know, offer to try `http://127.0.0.1:8080` and `http://127.0.0.1:8000`. A server
  doesn't have to be running now, but one that is can be identified for them.
- Whether they want the running jobs on screen: in Claude Code, the status line under the
  prompt (if they already have one, say you will add two lines to their script, not replace
  it); in OpenCode, the same rows in a block above the prompt (a plugin).

Then state the plan in a few lines (what you will install where, what files you will change)
and go ahead once they agree.

## 1. Prerequisites

`git` and [uv](https://docs.astral.sh/uv/). uv brings its own Python 3.12+ if the system has
none. If uv is missing, ask, then install it as uv documents
(`curl -LsSf https://astral.sh/uv/install.sh | sh`), and use its full path
(`~/.local/bin/uv`) until the user opens a new shell.

The models are the user's business: localagents starts no servers. A model that drives a
coding agent well needs solid native tool calling and a context window of 128k or more.

## 2. The code

```bash
git clone https://github.com/ccebelenski/localagents.git <checkout>
cd <checkout>
uv tool install -e .
```

Editable, so a `git pull` updates it. `uv tool install` puts `localagents` in uv's tool bin
directory (`uv tool dir --bin`); if that is not on PATH, `uv tool update-shell` adds it, which
edits the shell profile (rule 4: ask). The clients start the server by the name
`localagents`, so it must resolve on PATH for them; until the shell is new, run it here by its
full path. Then `localagents check`: `package` must be `ok`.

## 3. The servers: `models.yaml`

The config lists servers, not models. Which model each server runs, and its window, are read
from it on every call, so nothing changes here when the user swaps a model.

The config goes at `~/.config/localagents/models.yaml` (outside the checkout, found without
`--config`). On a re-run, keep the config the user already has (`check` names it in its
`config` item).

For each server the user gave you:

```bash
localagents check --url http://host:port [--api-key KEY]
```

This says what is there (llama.cpp, vLLM or Strata, the model, the window, launch flags that
are missing) and prints the entry for `models.yaml`. Pass on its notes to the user. An entry's
name is what the user and you will call it (`endpoint:` in `run_agent`); the suggested one is
generic when the address is an IP, so use the user's. Every server not on this machine needs a
`host:` line naming its machine (entries on one machine share it; the entry's own name is fine
for a machine with one server). Without one, the monitor and the status line show it as this
machine. A server that is down: write its
entry by hand, with `backend:` as the user tells you (`llama.cpp`, `vllm` or `strata`).

Start the file from the example's `defaults:` block, then the entries:

```bash
mkdir -p ~/.config/localagents
cp <checkout>/models.example.yaml ~/.config/localagents/models.yaml
```

and replace its `endpoints:` section with the user's entries, in the order the user prefers
them (with no model named, the first one up is used). Put an `api_key:` line under an entry
that needs it. Then `localagents check --config ~/.config/localagents/models.yaml`: `config`
must be `ok`, and each `endpoint:` item `ok`, or `warn` for a reason the user accepts (a server
that is off now).

## 4a. Claude Code

The server's registered name is `local` (its tools are then `mcp__local__*`). If `check` shows
a different server already registered as `local`, ask the user for another name, use it
everywhere below, and pass `--name <it>` to `check`.

1. **Register it**, user scope (every project):

   ```bash
   claude mcp add --scope user local -- localagents --config ~/.config/localagents/models.yaml
   ```

   Use the absolute path of the config in place of `~`. Check: `claude-code` is `ok`.

2. **The auto-background delay.** A blocking `run_agent` call is moved to the background
   after `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`, two minutes by default; ten seconds is right.
   Set it in `~/.claude/settings.json` (`$CLAUDE_CONFIG_DIR/settings.json` when that is set),
   rule 2 and 3: back it up, then for example

   ```bash
   python3 - <<'EOF'
   import json, os, pathlib, shutil
   p = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "settings.json"
   if p.exists():
       shutil.copy2(p, p.with_name(p.name + ".bak-localagents"))
   d = json.loads(p.read_text()) if p.exists() else {}
   d.setdefault("env", {})["CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS"] = "10000"
   p.parent.mkdir(parents=True, exist_ok=True)
   p.write_text(json.dumps(d, indent=2) + "\n")
   EOF
   ```

   Check: `claude-code.auto-background` is `ok`.

3. **The relay subagent**, which runs one job and reports it; the coordinator uses it to run
   several jobs at once:

   ```bash
   mkdir -p ~/.claude/agents
   ln -s <checkout>/agents/claude-code/local-agent.md ~/.claude/agents/local-agent.md
   ```

   If the server is not named `local`, copy the file instead and change `mcp__local__` to
   `mcp__<name>__` in it. If a different `local-agent.md` is already there, ask first. Check:
   `claude-code.relay` is `ok`.

4. **The status line, if the user wants it.** If `settings.json` has no `statusLine`, set it
   (as data, as in 2) to

   ```json
   {"type": "command", "command": "<checkout>/tools/statusline.sh", "refreshInterval": 5}
   ```

   which prints the session's model, directory and context use (when `jq` is installed) and the
   running local jobs. If the user has a status line script, show them these two lines and add
   them at its end with their agreement:

   ```bash
   local_lines=$(command -v localagents >/dev/null 2>&1 && timeout 2 localagents statusline 2>/dev/null)
   [ -n "$local_lines" ] && printf '%s\n' "$local_lines"
   ```

   and add `"refreshInterval": 5` to their `statusLine` so the times tick while idle. If their
   status line is not a script, show them the lines and let them decide. Check:
   `claude-code.statusline` is `ok`.

A running Claude Code session loads its MCP servers at startup: the new tools appear after
the user restarts it, or runs `/mcp` and reconnects.

## 4b. OpenCode

The same name rule as 4a (`local`, tools `local_*`).

1. **Register it**, globally:

   ```bash
   opencode mcp add local --global -- localagents --config ~/.config/localagents/models.yaml
   ```

   with the config's absolute path. OpenCode reloads its config on save, so a running
   OpenCode session gets the tools without a restart. Check: `opencode` is `ok`.

2. **The relay subagent**:

   ```bash
   mkdir -p ~/.config/opencode/agents
   ln -s <checkout>/agents/opencode/local-agent.md ~/.config/opencode/agents/local-agent.md
   ```

   (`$XDG_CONFIG_HOME/opencode/agents` when that is set.) For another server name, copy it and
   change `local_*` in its permissions and the tool names in its text. Check:
   `opencode.relay` is `ok`.

3. **The status block, if the user wants it**: a TUI plugin that shows, above the prompt, the
   rows Claude Code's status line shows, for the open session and the relays it started. It
   is listed by its directory in `~/.config/opencode/cli.json` (`$XDG_CONFIG_HOME/opencode/`
   when that is set; OpenCode's terminal settings, not `opencode.json`). Edit it as data, after
   a backup: add `"<checkout>/plugins/opencode"` to its `plugins` list, creating the list (or
   the file, as `{"plugins": [...]}`) if needed, keeping every other entry. OpenCode reloads
   `cli.json` on save. Check: `opencode.statusline` is `ok`.

## 5. Finish

Run `localagents check` once more and give the user its summary: what is `ok`, anything still
`warn` and why, nothing `fail`. Then tell them:

- What to restart (Claude Code: restart or `/mcp` reconnect; OpenCode: nothing).
- How to use it: *"Use the local agent to <a task>."* The coordinator calls `list_models`,
  picks a running model, and starts the job; a job can run in OpenCode instead of Claude Code
  with `harness="opencode"` (needs `opencode` on PATH).
- That `localagents monitor` opens a web view of the servers and jobs at
  http://127.0.0.1:8765/.
- Where to read more: the README, `docs/using.md`, and `docs/opencode.md`.

If you are OpenCode and the tools have appeared, call `local_list_models` and show the user
what is serving: that is the end-to-end check. In Claude Code the same check happens after
the restart; tell the user to ask *"list the local models"*.

## Update

If the checkout has moved, `localagents` itself fails to start (the editable install points
at the old place): run `uv tool install -e . --force` from the new place first, then
`check` shows the relay links and status line that still point at the old one; make them
again. Otherwise, from the checkout: `git pull`, then `uv tool install -e . --force` (picks up new
dependencies; harmless when there are none). Then `localagents check`, and follow any fix it
reports (a new release can add an install item). Running servers keep the old code until
their client reconnects: Claude Code `/mcp` reconnect or restart; OpenCode `opencode reload`.
Reconnecting ends the jobs that server is running (they are marked `lost`), so tell the user
to do it when none are running (`localagents statusline`, or the monitor, shows them).

## Uninstall

Start from `localagents check` and undo only what it shows is installed, asking before each
file outside the checkout:

- Claude Code: `claude mcp remove local --scope <scope>` for every scope `check` names
  (it lists each one the server is registered in); remove
  `~/.claude/agents/local-agent.md` if it is the link (or the copy) this install made; take
  `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` out of `settings.json`'s `env` (ask: it may predate
  this install); take out the status line lines, or the `statusLine` key if it runs
  `tools/statusline.sh`.
- OpenCode: there is no `opencode mcp remove`. Remove the `local` entry from the file
  `check` names, keeping the rest of it exactly (it may be JSONC with comments; edit the
  entry, do not re-serialise the file). Remove `~/.config/opencode/agents/local-agent.md` if
  it is this install's. Take `<checkout>/plugins/opencode` out of `cli.json`'s `plugins`.
- The package: `uv tool uninstall localagents`.
- Ask before each of these: the config (`~/.config/localagents/`), the job records and
  database (`~/.local/state/localagents/`), the checkout.

Afterwards `check` cannot run (the package is gone): confirm with `claude mcp list` and
`opencode mcp list` that `local` is not listed.

## When `check` says

| item | status | meaning, and what to do |
|---|---|---|
| `package` | fail | `localagents` is not on PATH: step 2's PATH note. |
| `config` | fail | No file, unreadable YAML, or no server: step 3. A YAML error names its line. |
| `config` | warn | An entry is unusable (no `base_url`, a router without its `decision_url`): the message names it; the rest work. |
| `endpoint:<name>` | warn, down | Nothing answers there now. Fine if the server is off; else the address or port is wrong. The hint says which (refused, unreachable, timed out). |
| `endpoint:<name>` | warn, window | The model's window is under 128k: jobs compact constantly. Usable, not good. |
| `endpoint:<name>` | warn, hint | A labelling fix, e.g. a Strata server marked `llama.cpp`. Do what it says. |
| `claude-code` / `opencode` | absent | Not registered. Expected if the user did not pick that client. |
| `claude-code` / `opencode` | warn | Registered with another config, without `--config`, or a different server under that name: show the user and ask. |
| `claude-code` / `opencode` | fail | Registered, but its command does not resolve: step 2's PATH note, then re-register. |
| `opencode.statusline` | warn | `cli.json` lists a plugin directory that is gone (a moved checkout) or another checkout's: list this one's. |
| `*.relay` | warn | An old copy that differs from the checkout's, or a link whose target is gone: replace it with the link. |
| `harness` | fail | `defaults.harness: opencode` without `opencode` on PATH: every job would fail. |
| any | info | A fact, nothing to do: the default harness, a router entry, a relay with no checkout to compare it with. |
