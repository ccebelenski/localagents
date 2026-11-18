/**
 * localagents' status block for OpenCode's TUI (Epic 6.12): the rows Claude Code's status line
 * shows — one header per endpoint, one row per job, `+N jobs in other sessions` — directly above
 * the prompt of an open session.
 *
 * The rows are `localagents statusline`'s, the same renderer Claude Code's status line runs, so
 * the two never differ. Scoped by OpenCode session rather than by process: the open session and
 * the relay subagents it started (`--session`, matched against each job's `owner_session`).
 * Nothing is drawn while no job is running or recently finished. Polled every few seconds
 * (the render reads the shared database in well under 100 ms); a failure draws nothing.
 *
 * Options (cli.json's object form): `command` (default ["localagents"]), `refresh_ms` (3000).
 */
import { Plugin } from "@opencode/plugin/tui"
import { execFile } from "node:child_process"
import { createSignal, For, onCleanup, Show } from "solid-js"

const REFRESH_MS = 3000
const TIMEOUT_MS = 5000

export default Plugin.define({
  id: "localagents.statusline",
  setup(context) {
    const opt = context.options ?? {}
    const command: string[] =
      Array.isArray(opt.command) && opt.command.length ? opt.command.map(String) : ["localagents"]
    const refresh = Number(opt.refresh_ms) > 0 ? Number(opt.refresh_ms) : REFRESH_MS

    function sessions(sessionID: string): string {
      let family: string[] = []
      try {
        family = context.data.session.family(sessionID) ?? []
      } catch {
        family = []
      }
      return [...new Set([sessionID, ...family])].join(",")
    }

    function read(sessionID: string): Promise<string[]> {
      return new Promise((resolve) => {
        execFile(
          command[0],
          [...command.slice(1), "statusline", "--session", sessions(sessionID)],
          { timeout: TIMEOUT_MS, encoding: "utf8" },
          (error, stdout) => resolve(error ? [] : stdout.split("\n").filter((line) => line.trim() !== "")),
        )
      })
    }

    function Block(props: { sessionID: string }) {
      const [lines, setLines] = createSignal<string[]>([])
      let busy = false
      const tick = async () => {
        if (busy) return
        busy = true
        try {
          setLines(await read(props.sessionID))
        } finally {
          busy = false
        }
      }
      void tick()
      const timer = setInterval(() => void tick(), refresh)
      onCleanup(() => clearInterval(timer))
      const color = () => context.theme.text.feedback.info.base
      return (
        <Show when={lines().length > 0}>
          <box flexDirection="column">
            <For each={lines()}>{(line) => <text fg={color()}>{line}</text>}</For>
          </box>
        </Show>
      )
    }

    return context.ui.slot({
      append: "session.composer.top",
      render: (input) => <Block sessionID={input.sessionID} />,
    })
  },
})
