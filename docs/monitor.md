# The monitor

`localagents monitor` is a web dashboard for the endpoints in `models.yaml` and the jobs
localagents runs on them: one process that probes every endpoint (every 5 s; a dormant
one every 60 s) into the shared database and serves a page that redraws from a
server-sent events stream, one snapshot per probe round.

## Running it

```bash
localagents monitor                 # http://127.0.0.1:8765/, opens in your browser
localagents monitor --port 9000 --config ~/.config/localagents/models.yaml
```

Flags:

- `--port N` — default: the `monitor:` section's `port`, else **8765**.
- `--host ADDR` — default `127.0.0.1`. The page has no authentication, so bind another
  address only on a network you trust.
- `--db PATH` — default: `monitor: db`, else `LOCALAGENTS_DB`, else the state
  directory's `localagents.db`.
- `--config PATH` — the `models.yaml` to read; found as the MCP server finds it (see
  [Config](../README.md#config-modelsyaml)).
- `--no-collect` — serve the database as it is, without probing or pruning. The page
  still follows the jobs the MCP server writes, a snapshot every `refresh_s`.
- `--no-open` — do not open the browser. By default the page opens in a new tab once
  the server is up (`$BROWSER` is honoured). If a monitor is already running on that
  port, the command opens the page and exits 0 instead of failing; with `--no-open`
  the "port in use" failure stands.

It prints the URL and the database path at start and stops on Ctrl-C or SIGTERM. (To
look at the page with no servers at all, `CONTRIBUTING.md` describes a seeded database.)

To keep it running in the background from a checkout, `tools/monitor.sh` wraps it:

```bash
tools/monitor.sh start            # detached with setsid; the page opens
tools/monitor.sh status           # running (pid, last start line) or stopped
tools/monitor.sh log              # tail -f the log
tools/monitor.sh stop             # SIGTERM, waits up to 10 s
```

It writes its pid to `${XDG_STATE_HOME:-~/.local/state}/localagents/monitor.pid` and its
output to `monitor.log` beside it, needs `localagents` on PATH, and passes any other
arguments after `start` to `localagents monitor`.

## Settings

An optional top-level `monitor:` section in `models.yaml` holds the monitor's settings.
The MCP server ignores the section; the monitor rejects an unknown key or a bad value
with an error naming it:

```yaml
monitor:
  port: 8765
  attention_hours: 24     # an endpoint seen up within this window and down now is an alert; older is dormant
  retention_days: 7       # probe rows older than this are deleted (job rows never are)
  refresh_s: 5            # at most one snapshot push per this many seconds (max 3600)
  # db: /path/to/localagents.db
```

## The page

The overview is one card per host, in `models.yaml` order; a card never moves when its
state changes. A live endpoint's card is a grid of tiles for what the backend reports —
throughput, cache hit, KV fill, context window, and the GPU figures on Strata — with the
jobs running on it listed at the foot. An endpoint that went down recently is drawn in
the alert colour with the probe's own sentence ("connection refused" and the like); one
that has been down longer than the attention window fades to a "last seen" line;
endpoints never seen at all aren't drawn, just counted under the cards. Cards are at least
500 px wide and share the window's width, so the number across follows the window; a line
too long for its card ends in "…", and hovering it shows the whole text.

Under the cards is the **Recent jobs** table: running and queued jobs, then the last ten
finished. Each row has a status pill — one of them is `lost`, for a job whose MCP server
stopped beating — and the numbers from the status line: where it ran, how long, turns,
tool calls and errors, context fill, speed, and what it was last doing. A job that ran in
OpenCode has an `opencode` badge beside its status. A non-zero error count is a link. Below that a timeline draws each live endpoint's busy intervals with
its jobs as bars on top.

Tables sort by header and filter per column, with a filter that fits the column (a
select for status, a number box in the column's unit, a date-and-hour picker for start
times). The range buttons (`15m` to `7d`) apply to everything and the browser remembers
them. The page redraws when the server pushes a snapshot and never polls; the header
says when the last one arrived and turns red while the stream reconnects. Nothing is
loaded from the internet.

An endpoint's name opens its own page — every figure it reports as a tile with a
sparkline, the jobs that ran on it, its restarts and outages, and its requests. A job
opens the job page: the usage and `/metrics` numbers, the harness it ran in, the files
it touched, a timeline of its tool calls with errors in red, its requests, and the
transcript tail. The job page also shows the build stamp of the MCP server that ran it, with a note when the
monitor's own code differs from it; that is how you notice a server left running on old code.

Two rows in the request tables are not what they look like. In a database from before
story 3.7t, Claude Code's connection warm-up (`/api/hello`) was forwarded and recorded; such
a row is labelled `handshake`, outside the error counts (the shim now answers it and
records nothing). And a backend that reports no cache fields (vLLM without
`--enable-prompt-tokens-details`) gets a prefill speed that is an upper bound; the cell's
tooltip says so.

## Smoke test

`localagents smoke --endpoint NAME --model SERVED_ID` is the online acceptance run: a
few short `/v1/messages` requests to the named endpoint, one real agent job through the
shim and runner with a fixed brief (`--requests N` sets how many direct requests, 3 by default and 0 for none;
`--skip-job` leaves the job out; `--timeout-s` caps it),
a probe of a port nothing listens on, and a printed checklist of the rows and states
that appeared (exit 1 on any missing row). It needs a model actually running on that
endpoint (it runs a real job there, Bash included), and it writes real rows — into
`LOCALAGENTS_DB` or `--db` when set, otherwise the shared database; point it at a
throwaway file to rehearse.

## Routes

The page's JSON — `/api/snapshot`, `/api/endpoints/<name>/history?range=1h`,
`/api/fleet/history?range=1h`, `/api/overview/history?range=1h`,
`/api/endpoints/<name>/requests?range=24h`, `/api/jobs?range=24h`, `/api/jobs/<id>`
and the `/events` stream — is specified in
[`docs/superpowers/specs/2026-10-29-monitor-design.md`](superpowers/specs/2026-10-29-monitor-design.md)
§5a. Ranges run from `15m` to `7d`.

