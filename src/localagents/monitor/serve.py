"""``localagents monitor`` (monitor spec §3 "Process"): the collection loop and the web app on one
asyncio loop, until SIGINT or SIGTERM.

``localagents/__init__.py`` imports this module only inside the ``monitor`` branch, so neither
the MCP server nor ``localagents statusline`` ever loads it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import http.client
import signal
import socket
import sqlite3
import sys
import urllib.request
import webbrowser
from pathlib import Path
from typing import Iterator

from .. import db
from ..registry import Registry, config_path
from .config import MonitorSettings, load_settings

GRACEFUL_SHUTDOWN_S = 3  # uvicorn's wait for open connections after the stop


def _port(text: str) -> int:
    n = int(text)
    if not 1 <= n <= 65535:
        raise argparse.ArgumentTypeError(f"port must be in 1..65535, got {n}")
    return n


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="localagents monitor",
        description="web dashboard for the endpoints in models.yaml and the jobs localagents runs on them")
    ap.add_argument("--port", type=_port, default=None,
                    help="port to serve on (default: the monitor: section's port, else 8765)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to bind (default 127.0.0.1; the page has no authentication)")
    ap.add_argument("--db", default=None,
                    help="the database file (default: the monitor: section's db, else LOCALAGENTS_DB, "
                         "else the state dir's localagents.db)")
    ap.add_argument("--config", default=None,
                    help="models.yaml (default: LOCALAGENTS_CONFIG, ./models.yaml, ~/.config/localagents/models.yaml)")
    ap.add_argument("--no-collect", action="store_true",
                    help="serve the database as it is, without probing (a seeded file: localagents seed)")
    ap.add_argument("--no-open", action="store_true", dest="no_open",
                    help="do not open the page in a browser (by default it opens in a new tab once it is "
                         "serving, $BROWSER is honoured; if it is already running on the port, the page "
                         "is opened and the command exits 0)")
    return ap.parse_args(argv)


def browse_url(host: str, port: int) -> str:
    """The URL a browser visits for a bind address: a bind-all address (0.0.0.0, ::) is not a URL
    to visit, so the page is opened on loopback."""
    visit = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{visit}:{port}/"


def open_browser(url: str) -> None:
    """webbrowser.open in a new tab. A failure is one stderr line, never an exit: the server
    is up either way, and the URL is on stdout for the operator to visit by hand."""
    try:
        opened = webbrowser.open(url, new=2)
    except Exception as e:
        print(f"localagents monitor: could not open a browser for {url}: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return
    if not opened:
        print(f"localagents monitor: no browser opened for {url} (set BROWSER, or visit it by hand)",
              file=sys.stderr, flush=True)


def port_free(host: str, port: int) -> bool:
    """Review of 3.1 (G1), ruling F: the port is checked before uvicorn starts, so an occupied
    one is one clean sentence on stderr and exit 2, never a traceback out of uvicorn. The bind
    test is not atomic with the server's own bind (another process can take the port in
    between); that race still ends in uvicorn's own startup failure, which stays loud.

    Story 3.2c row 4 (the 3.2 combined review): the port is free only when EVERY address
    getaddrinfo resolves for the host binds — the old answer came from the first address that
    bound, and ``localhost`` resolves ``::1`` before ``127.0.0.1``, so a listener on
    ``127.0.0.1:<p>`` alone let ``--host localhost`` start serving on the other family while
    the port was taken. EADDRNOTAVAIL is a box without that address family (an IPv6-less one
    still resolves ``localhost`` to ``::1``): skipped, not a refusal — the verdict comes from
    the addresses the box can use (all skipped answers not free, as before)."""
    free = False
    for family, stype, proto, _, sa in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        s = socket.socket(family, stype, proto)
        try:
            # story 3.2a row 1: SO_REUSEADDR, as uvicorn's own bind uses — a TIME-WAIT socket
            # left by a just-closed browser session must not refuse a restart for ~60 s. A live
            # LISTENing socket still binds to nothing: EADDRINUSE stands (the 3.1 ruling F test).
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(sa)
        except OSError as e:
            s.close()
            if e.errno == errno.EADDRNOTAVAIL:
                continue  # this box has no such address: says nothing about the port
            if e.errno != errno.EADDRINUSE:
                raise
            return False  # taken on this address of the host: refuse, whatever the rest do
        s.close()
        free = True
    return free


def already_serving(host: str, port: int) -> bool:
    """Step 2: the port is taken, but is it OUR monitor? A 200 from its own JSON route within
    2 s says so; anything else on the port (no answer, another server, an error page) is not."""
    try:
        with urllib.request.urlopen(browse_url(host, port) + "api/snapshot", timeout=2) as r:
            return r.status == 200
    except (OSError, ValueError, http.client.HTTPException):
        return False


def db_file(arg: str | None, settings: MonitorSettings) -> Path:
    """``--db``, else the ``monitor:`` section's ``db`` (config.py: it overrides ``db_path()``),
    else ``db.db_path()`` (``LOCALAGENTS_DB``, else the state dir)."""
    if arg:
        return Path(arg).expanduser()
    if settings.db:
        return Path(settings.db).expanduser()
    return db.db_path()


def backfill_at_start(conn: sqlite3.Connection) -> None:
    """Story 3.7-fold-2 D5's follow-up: the version-6 witness backfill moved out of the db
    migration (the MCP server never imports the monitor package — the static guard in
    tests/test_server.py) and runs HERE, at the monitor's own start, once, before any route can
    answer. ``only_null`` restricts the walk to endpoints with a NULL witness: an endpoint that
    already holds a witness, or the walked-nothing memo, costs one listed row — but an endpoint
    idle since before its own walk pays it at every start until it advances once (read.py's
    docstring). One transaction of its own; open and close are the caller's."""
    from . import read

    with db.immediate(conn):
        read.backfill_last_active(conn, only_null=True)


async def serve(reg: Registry, settings: MonitorSettings, path: Path, host: str, port: int, collect_: bool,
                open_url: str | None = None) -> None:
    import uvicorn

    from . import collect
    from .app import make_app

    # the witness backfill, with or without a collector: every route that reads last_active must
    # see the post-migration file already carried over (an old file opens at v6 with NULLs; main
    # migrates before serve, and a bare serve in a test does the same inside open_db below)
    bf = db.open_db(path)
    try:
        backfill_at_start(bf)
    finally:
        bf.close()

    stop = asyncio.Event()
    tick = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    class Server(uvicorn.Server):
        # uvicorn's own handlers would replace ours and re-raise the signal after shutdown (the
        # process would then die by SIGTERM instead of exiting 0): the stop event drives it.
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

    # story 3.7-fold-2 (D4): with --no-collect there are no collector ticks, so the fan-out
    # self-ticks every refresh_s — an open page still follows the jobs the MCP server writes.
    app = make_app(lambda: db.open_db(path), reg, settings, tick, stop=stop, db_path=path,
                   self_tick=not collect_)
    server = Server(uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False,
                                   timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S))

    async def stopper() -> None:
        await stop.wait()
        server.should_exit = True
        tick.set()  # wake the event streams so they see stop and end
        # story 3.2c row 1 (the 3.2 combined review): after stop, no `_Fanout._run` task is
        # pending even on a loop that outlives the app. A connected stream closes the fan-out
        # itself; this covers the stop with no browser ever open. The sleep(0) lets the drain
        # run once first — it sets every subscriber's event in one step, so a stream the stop
        # is meant to wake is already awake when the drain is cancelled. close() is idempotent:
        # a stream's own close may get here first, and `finally: stop.set()` below is the
        # second stop ruling (d) names.
        await asyncio.sleep(0)
        await app.state.fanout.close()

    async def opener() -> None:
        # --open: one call, after uvicorn has bound (its `started` flag). The browser launch runs
        # in a thread: a slow launcher never holds up the loop that serves the page.
        while not server.started:
            if stop.is_set():
                return
            await asyncio.sleep(0.05)
        await asyncio.to_thread(open_browser, open_url)

    tasks = [stopper(), server.serve()]
    if open_url is not None:
        tasks.append(opener())
    conn = None
    if collect_:
        conn = db.open_db(path)  # the collector's own connection, used on this loop only
        tasks.append(collect.run(reg, conn, list(reg.endpoints.values()), stop,
                                 retention_days=settings.retention_days,
                                 window_s=settings.attention_hours * 3600.0, on_tick=tick.set))
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        if conn is not None:
            conn.close()


def main(argv: list[str]) -> None:
    a = parse_args(argv)
    cfg = Path(a.config).expanduser() if a.config else config_path()
    try:
        settings = load_settings(cfg)
    except ValueError as e:
        print(f"localagents monitor: {cfg}: {e}", file=sys.stderr)
        sys.exit(2)
    reg = Registry.load(cfg)
    path = db_file(a.db, settings)
    port = a.port if a.port is not None else settings.port
    try:
        db.open_db(path).close()  # create or migrate now: a bad file fails here, not in the first route
    except (db.SchemaError, sqlite3.Error, OSError) as e:
        print(f"localagents monitor: cannot open {path}: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
    try:
        free = port_free(a.host, port)
    except OSError as e:  # an unresolvable host, a privileged port: the same one-sentence rule
        print(f"localagents monitor: cannot bind {a.host}:{port}: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
    if not free:
        if not a.no_open and already_serving(a.host, port):
            url = browse_url(a.host, port)
            print(f"localagents monitor: already running at {url} · opening", flush=True)
            open_browser(url)
            return  # the running monitor serves the page; this command has nothing to start
        print(f"localagents monitor: port {port} is already in use", file=sys.stderr)
        sys.exit(2)
    n = len(reg.endpoints)
    mode = "" if not a.no_collect else " · not collecting"
    print(f"localagents monitor: http://{a.host}:{port}/ · database {path} · "
          f"{n} endpoint{'' if n == 1 else 's'}{mode}", flush=True)
    asyncio.run(serve(reg, settings, path, a.host, port, not a.no_collect,
                      open_url=None if a.no_open else browse_url(a.host, port)))
