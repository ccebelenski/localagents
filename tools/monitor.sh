#!/usr/bin/env bash
# tools/monitor.sh: the localagents monitor in the background (start, stop, status, log).
#
#   tools/monitor.sh start [monitor args...]  start it detached (setsid when present, so a closed
#                                             terminal does not kill it); the page opens in a
#                                             browser. If one is already running, it just opens
#                                             the page and exits 0 (the monitor's own check).
#   tools/monitor.sh stop                     SIGTERM the pidfile's process, wait up to 10 s
#   tools/monitor.sh status                   running (pid, last start line) or stopped; exit 0/1
#   tools/monitor.sh log                      tail -f the log (Ctrl-C stops the tail, not the monitor)
#
# State: ${XDG_STATE_HOME:-$HOME/.local/state}/localagents/monitor.pid and monitor.log.
# Needs `localagents` on PATH (uv tool install -e .). Pass --no-open to start to keep the browser shut.
set -u

STATE="${XDG_STATE_HOME:-$HOME/.local/state}/localagents"
PIDFILE="$STATE/monitor.pid"
LOG="$STATE/monitor.log"

usage() {
    echo "usage: tools/monitor.sh start [monitor args...] | stop | status | log" >&2
    exit 2
}

# alive PID: the process exists AND its command line is a localagents monitor. A pid that another
# program took after a reboot is not ours to signal, so it counts as not running.
alive() {
    local pid=$1
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    ps -o args= -p "$pid" 2>/dev/null | grep -q 'localagents monitor'
}

pid_in_file() {
    [ -f "$PIDFILE" ] && cat "$PIDFILE"
}

start() {
    if ! command -v localagents >/dev/null 2>&1; then
        echo "localagents monitor: localagents is not on PATH (uv tool install -e .)" >&2
        return 127
    fi
    mkdir -p "$STATE"
    if alive "$(pid_in_file)"; then
        # already running: the monitor's own short-circuit opens the page and exits 0
        exec localagents monitor "$@"
    fi
    local spawn=()
    # setsid: a new session, so the monitor outlives the terminal. In a non-interactive shell
    # the background job is not a process-group leader, so setsid execs and $! is the monitor.
    if command -v setsid >/dev/null 2>&1; then
        spawn=(setsid)
    fi
    nohup "${spawn[@]}" localagents monitor "$@" >> "$LOG" 2>&1 < /dev/null &
    local pid=$!
    echo "$pid" > "$PIDFILE"
    sleep 1
    if alive "$pid"; then
        echo "localagents monitor: started (pid $pid) · log $LOG"
        return 0
    fi
    # it ended at once: a port taken by something else (exit 2), or already running (exit 0)
    local rc=0
    wait "$pid" || rc=$?
    rm -f "$PIDFILE"
    echo "localagents monitor: exited at once (status $rc): $(tail -n 1 "$LOG" 2>/dev/null)" >&2
    return "$rc"
}

stop() {
    local pid
    pid=$(pid_in_file)
    if [ -z "$pid" ]; then
        echo "localagents monitor: stopped (no pidfile)"
        return 0
    fi
    if ! alive "$pid"; then
        rm -f "$PIDFILE"
        echo "localagents monitor: stopped (pid $pid was not running; pidfile removed)"
        return 0
    fi
    kill -TERM "$pid"
    local i
    for i in $(seq 1 100); do
        if ! alive "$pid"; then
            rm -f "$PIDFILE"
            echo "localagents monitor: stopped (pid $pid)"
            return 0
        fi
        sleep 0.1
    done
    echo "localagents monitor: pid $pid still running 10 s after SIGTERM; pidfile kept" >&2
    return 1
}

status() {
    local pid
    pid=$(pid_in_file)
    if alive "$pid"; then
        echo "running (pid $pid)"
        local last
        last=$(grep '^localagents monitor: http' "$LOG" 2>/dev/null | tail -n 1)
        [ -n "$last" ] && echo "$last"
        return 0
    fi
    echo "stopped"
    return 1
}

log() {
    if [ ! -f "$LOG" ]; then
        echo "localagents monitor: no log at $LOG" >&2
        return 1
    fi
    tail -n 50 -f "$LOG"
}

case "${1:-}" in
    start) shift; start "$@"; exit $? ;;
    stop|status|log) [ $# -eq 1 ] || usage; "$1"; exit $? ;;
    *) usage ;;
esac
