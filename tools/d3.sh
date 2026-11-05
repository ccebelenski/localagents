#!/usr/bin/env bash
# tools/d3.sh: the router's d3 decision server in the background (start, stop, status, log).
#
#   tools/d3.sh start [d3 serve args...]  start it detached (setsid when present, so a closed
#                                         terminal does not kill it), then wait for /health to
#                                         report ok (the weights take a while to load). If one
#                                         is already running, say so and exit 0.
#   tools/d3.sh stop                      SIGTERM the pidfile's process, wait up to 10 s
#   tools/d3.sh status                    running (pid, start line, /health) or stopped; exit 0/1
#   tools/d3.sh log                       tail -f the log (Ctrl-C stops the tail, not the server)
#
# State: ${XDG_STATE_HOME:-$HOME/.local/state}/localagents/d3.pid and d3.log.
# Needs `localagents` on PATH WITH the router extra (uv tool install -e '.[router]'): without it
# the server starts, /health answers with the install sentence, and start reports that and stops it.
# Pass --port to start to serve elsewhere; status and stop read the port from the start line.
set -u

STATE="${XDG_STATE_HOME:-$HOME/.local/state}/localagents"
PIDFILE="$STATE/d3.pid"
LOG="$STATE/d3.log"
HEALTH_WAIT_S="${D3_HEALTH_WAIT_S:-180}"   # the model load; 22 s on the reference GPU, cold disks slower

usage() {
    echo "usage: tools/d3.sh start [d3 serve args...] | stop | status | log" >&2
    exit 2
}

# alive PID: the process exists AND its command line is a localagents d3. A pid that another
# program took after a reboot is not ours to signal, so it counts as not running.
alive() {
    local pid=$1
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    ps -o args= -p "$pid" 2>/dev/null | grep -q 'localagents d3'
}

pid_in_file() {
    [ -f "$PIDFILE" ] && cat "$PIDFILE"
}

# the server's own start line names its URL: "localagents d3: http://127.0.0.1:8766/ · model on device"
url_from_log() {
    grep '^localagents d3: http' "$LOG" 2>/dev/null | tail -n 1 | sed -E 's#^localagents d3: (http://[^/ ]+)/.*#\1#'
}

health() {
    local url
    url=$(url_from_log)
    [ -n "$url" ] || return 1
    curl -s -m 3 "$url/health"
}

start() {
    if ! command -v localagents >/dev/null 2>&1; then
        echo "localagents d3: localagents is not on PATH (uv tool install -e '.[router]')" >&2
        return 127
    fi
    mkdir -p "$STATE"
    if alive "$(pid_in_file)"; then
        echo "localagents d3: already running (pid $(pid_in_file)) · $(health)"
        return 0
    fi
    local spawn=()
    # setsid: a new session, so the server outlives the terminal. In a non-interactive shell
    # the background job is not a process-group leader, so setsid execs and $! is the server.
    if command -v setsid >/dev/null 2>&1; then
        spawn=(setsid)
    fi
    nohup "${spawn[@]}" localagents d3 serve "$@" >> "$LOG" 2>&1 < /dev/null &
    local pid=$!
    echo "$pid" > "$PIDFILE"
    sleep 1
    if ! alive "$pid"; then
        # it ended at once: a port taken by something else, or a usage error
        local rc=0
        wait "$pid" || rc=$?
        rm -f "$PIDFILE"
        echo "localagents d3: exited at once (status $rc): $(tail -n 1 "$LOG" 2>/dev/null)" >&2
        return "$rc"
    fi
    echo "localagents d3: started (pid $pid) · log $LOG · waiting for /health (up to ${HEALTH_WAIT_S}s)"
    local i body
    for i in $(seq 1 "$HEALTH_WAIT_S"); do
        sleep 1
        if ! alive "$pid"; then
            rm -f "$PIDFILE"
            echo "localagents d3: died while loading: $(tail -n 1 "$LOG" 2>/dev/null)" >&2
            return 1
        fi
        body=$(health) || continue
        case "$body" in
            *'"ok": true'*|*'"ok":true'*)
                echo "localagents d3: ready · $body"
                return 0 ;;
            *'"loading": true'*|*'"loading":true'*|"")
                continue ;;
            *)
                # the loader failed (no extra, a bad revision): the server is up but useless
                echo "localagents d3: the model did not load: $body" >&2
                stop >/dev/null
                return 1 ;;
        esac
    done
    echo "localagents d3: still loading after ${HEALTH_WAIT_S}s (pid $pid kept; tools/d3.sh status to watch)" >&2
    return 1
}

stop() {
    local pid
    pid=$(pid_in_file)
    if [ -z "$pid" ]; then
        echo "localagents d3: stopped (no pidfile)"
        return 0
    fi
    if ! alive "$pid"; then
        rm -f "$PIDFILE"
        echo "localagents d3: stopped (pid $pid was not running; pidfile removed)"
        return 0
    fi
    kill -TERM "$pid"
    local i
    for i in $(seq 1 100); do
        if ! alive "$pid"; then
            rm -f "$PIDFILE"
            echo "localagents d3: stopped (pid $pid)"
            return 0
        fi
        sleep 0.1
    done
    echo "localagents d3: pid $pid still running 10 s after SIGTERM; pidfile kept" >&2
    return 1
}

status() {
    local pid
    pid=$(pid_in_file)
    if alive "$pid"; then
        echo "running (pid $pid)"
        local last
        last=$(grep '^localagents d3: http' "$LOG" 2>/dev/null | tail -n 1)
        [ -n "$last" ] && echo "$last"
        echo "health: $(health || echo 'no answer')"
        return 0
    fi
    echo "stopped"
    return 1
}

log() {
    if [ ! -f "$LOG" ]; then
        echo "localagents d3: no log at $LOG" >&2
        return 1
    fi
    tail -n 50 -f "$LOG"
}

case "${1:-}" in
    start) shift; start "$@"; exit $? ;;
    stop|status|log) [ $# -eq 1 ] || usage; "$1"; exit $? ;;
    *) usage ;;
esac
