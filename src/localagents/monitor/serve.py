"""``localagents monitor`` (monitor spec §3 "Process"): the collection loop and the web app on one
asyncio loop, until SIGINT or SIGTERM.

``localagents/__init__.py`` imports this module only inside the ``monitor`` branch, so neither
the MCP server nor ``localagents statusline`` ever loads it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sqlite3
import sys
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
    return ap.parse_args(argv)


def db_file(arg: str | None, settings: MonitorSettings) -> Path:
    """``--db``, else the ``monitor:`` section's ``db`` (config.py: it overrides ``db_path()``),
    else ``db.db_path()`` (``LOCALAGENTS_DB``, else the state dir)."""
    if arg:
        return Path(arg).expanduser()
    if settings.db:
        return Path(settings.db).expanduser()
    return db.db_path()


async def serve(reg: Registry, settings: MonitorSettings, path: Path, host: str, port: int, collect_: bool) -> None:
    import uvicorn

    from . import collect
    from .app import make_app

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

    app = make_app(lambda: db.open_db(path), reg, settings, tick, stop=stop)
    server = Server(uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False,
                                   timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S))

    async def stopper() -> None:
        await stop.wait()
        server.should_exit = True
        tick.set()  # wake the event streams so they see stop and end

    tasks = [stopper(), server.serve()]
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
    n = len(reg.endpoints)
    mode = "" if not a.no_collect else " · not collecting"
    print(f"localagents monitor: http://{a.host}:{port}/ · database {path} · "
          f"{n} endpoint{'' if n == 1 else 's'}{mode}", flush=True)
    asyncio.run(serve(reg, settings, path, a.host, port, not a.no_collect))
