import sys


def main() -> None:
    # `localagents statusline` runs on every status-bar refresh: answer it without importing the
    # server (mcp, httpx, uvicorn), which takes far longer than the render itself.
    if sys.argv[1:2] == ["statusline"]:
        from .status import render, render_lines

        if "--one-line" in sys.argv[2:]:
            print(render())
        else:
            lines = render_lines()
            # as render() always did in the empty case: exactly one empty line — the operator's
            # script -n-tests the output, so "nothing" is an empty line, not silence
            print("\n".join(lines) if lines else "")
        return
    if sys.argv[1:2] == ["prune"]:
        import argparse

        from .jobs import prune

        ap = argparse.ArgumentParser(prog="localagents prune", description="delete job directories older than N days")
        ap.add_argument("--days", type=int, default=7, help="retention in days (default 7)")
        ap.add_argument("--dry-run", action="store_true", help="list what would be removed")
        a = ap.parse_args(sys.argv[2:])
        removed = prune(a.days, dry_run=a.dry_run)
        for p in removed:
            print(p)
        print(f"{'dry run: ' if a.dry_run else ''}{len(removed)} job director{'y' if len(removed) == 1 else 'ies'} older than {a.days} days")
        return
    if sys.argv[1:2] == ["seed"]:
        import argparse
        from pathlib import Path

        from . import db
        from .monitor import seed

        ap = argparse.ArgumentParser(
            prog="localagents seed",
            description="build an offline, deterministic monitor database from the fixtures "
                        "(three hosts, a finished and a running job), timestamps shifted to --now")
        ap.add_argument("--db", default=None,
                        help="the database file (default: db.db_path(): LOCALAGENTS_DB, else the state dir)")
        ap.add_argument("--now", default=None,
                        help="epoch seconds or ISO-8601 to shift the world to (default: the current time)")
        ap.add_argument("--force", action="store_true",
                        help="replace an existing database: delete every row and seed fresh")
        a = ap.parse_args(sys.argv[2:])
        path = Path(a.db).expanduser() if a.db else db.db_path()
        conn = db.open_db(path)
        try:
            counts = seed.seed(conn, seed.parse_now(a.now), seed.default_fixtures_dir(), force=a.force)
        except seed.SeedRefused as e:
            conn.close()
            print(e)
            sys.exit(2)
        conn.close()
        for table, n in counts.items():
            print(f"{table}: {n}")
        print(path)
        return
    from .server import main as serve

    serve()


__all__ = ["main"]
