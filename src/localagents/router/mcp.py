"""The router's command-and-control MCP surface (docs/superpowers/specs/2026-11-02-router-design.md
§11), served by ``localagents router`` as a second, small MCP server. Phase 1 builds two of the
spec's tools: ``definitions`` (get, set, dry run) and a score-only ``preflight``; the rest arrive
with the routing stories. Nothing on this surface dispatches work.

The router entry is the config's first ``backend: router`` endpoint; its ``decision_*`` fields are
the d3 client's arguments. The database connection is opened once per process, lazily, the way
``server.py`` holds its job store. Every failure is a dict, never a raised exception: the caller
is a model reading the answer, not a stack trace."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from localagents import db
from localagents.registry import Endpoint, Registry
from localagents.router import definitions as defsmod
from localagents.router import feedback
from localagents.router import scorecard as scorecard_mod
from localagents.router.decision import (
    BriefUnresolved,
    DecisionUnavailable,
    resolve_brief,
    score,
)

INSTRUCTIONS = (
    "Command and control for the localagents router: read and edit the dimension definitions "
    "(every edit through a dry run over briefs), preflight a brief to see the scores d3 gives "
    "it right now, start and resume a calibration run (calibrate), read the scorecard "
    "(scorecard), and record your verdict on a finished job (verdict). Nothing on this surface "
    "dispatches work; jobs run through the localagents server."
)

# §4's priorities; routing itself (which of these admits where) is a later story
PRIORITIES = ("urgent", "normal", "background")

mcp = MCPServer("localagents-router", instructions=INSTRUCTIONS)

_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    """The shared database, opened once per process like the job store in server.py."""
    global _conn
    if _conn is None:
        _conn = db.open_db()
    return _conn


def _client() -> httpx.AsyncClient:
    """The d3 client's transport; a seam so tests can hand back a MockTransport."""
    return httpx.AsyncClient()


def _router_entry() -> Endpoint | None:
    """The config's first usable ``backend: router`` entry, or None. Any failure of
    ``Registry.load`` rides out here (a config file is operator input); callers answer
    config_error. An entry validate marked invalid is skipped: returning it would hand the
    caller a None decision_url (I2: marked entries stay out of the way until fixed)."""
    for ep in Registry.load().endpoints.values():
        if ep.is_router and not ep.invalid:
            return ep
    return None


def _no_router_entry() -> dict[str, Any]:
    return {"error": "no_router_entry", "message": "add a backend: router entry to models.yaml"}


def _database_error(e: Exception) -> dict[str, Any]:
    return {"error": "database_error", "message": f"the database could not be read: {e}"}


@mcp.tool()
async def definitions(
    action: str = "get",
    document: dict | None = None,
    dry_run: bool = False,
    briefs: list[str] | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Read or write the router's dimension definitions (the questions d3 asks of every brief).

    `get` (default): the current version and document. `set`: validate `document` and append it
    as the next version. `set` with `dry_run`: score `briefs` (paths, relative to `cwd` or this
    server's cwd) under both the saved and the proposed document without saving anything, and
    report the dimensions whose value or determined-ness moved plus the undetermined rate of
    each document. Every definitions edit should pass a dry run first (the calibration set
    spec's §6: an axis that never clears its bar is treated as always undetermined, and this is
    the live-briefs view of exactly that).
    """
    if action not in ("get", "set"):
        return {"error": "bad_argument", "message": "action must be 'get' or 'set'"}
    try:
        if action == "get":
            defs = defsmod.load(_db())
            return {"version": defs.version, "document": defs.document}
        defsmod.validate(document)
        if not dry_run:
            defsmod.load(_db())  # seed the default first: a set over a fresh store appends v2
            saved = defsmod.save(_db(), document)
            return {"version": saved.version, "document": saved.document}
    except json.JSONDecodeError as e:
        # the stored document is not JSON: the database is broken, not the proposed edit
        # (JSONDecodeError is a ValueError, so this arm must stay ahead of the next one)
        return _database_error(e)
    except ValueError as e:
        # validate's own complaints about the document the caller passed in
        return {"error": "invalid_document", "message": str(e)}
    except (sqlite3.Error, db.SchemaError) as e:
        return _database_error(e)
    if not briefs:
        return {"error": "no_briefs", "message": "a dry run scores briefs; pass briefs=[paths]"}
    return await _dry_run(document, briefs, cwd)


async def _dry_run(document: dict, briefs: list[str], cwd: str | None) -> dict[str, Any]:
    """Score every brief twice through the d3 server, once under the saved document and once
    under the proposed one (which is not saved). A move is a dimension whose value or
    determined-ness differs between the two scorings; undetermined axes count as the neutral
    value None, matching §6. The undetermined rate counts cells over briefs x dimensions, so
    the dry run says how often each document leaves an axis neutral. The two scorings pair
    per brief, briefs in the caller's order, dimensions in DIMENSIONS order."""
    try:
        ep = _router_entry()
    except Exception as e:  # a config file is operator input: any failure to read it is a config error
        return {"error": "config_error", "message": f"{type(e).__name__}: {e}"}
    if ep is None:
        return _no_router_entry()
    base = Path(cwd).expanduser() if cwd else Path(os.getcwd())
    try:
        current = defsmod.load(_db())
    except (sqlite3.Error, db.SchemaError, json.JSONDecodeError) as e:
        return _database_error(e)
    proposed = defsmod.Definitions(current.version, document)

    texts: dict[str, str] = {}
    for b in briefs:
        p = Path(b).expanduser()
        if not p.is_absolute():
            p = base / p
        try:
            raw = p.read_text()
        except OSError as e:
            return {
                "error": "brief_unresolved",
                "message": f"the brief at {p} cannot be read ({e.strerror or type(e).__name__})",
                "path": str(p),
            }
        try:
            # a brief file may itself hold a pointer brief; dispatch follows it (§4 step 1)
            texts[b] = resolve_brief(raw, str(base))
        except BriefUnresolved as e:
            return e.to_dict()

    moved: list[dict[str, Any]] = []
    und = {"current": 0, "proposed": 0}
    client = _client()
    try:
        for b in briefs:
            try:
                under = {}
                for tag, defs in (("current", current), ("proposed", proposed)):
                    under[tag] = await score(
                        client, ep.decision_url, ep.decision_model, texts[b], defs,
                        timeout_s=ep.decision_timeout_s, max_tokens=ep.decision_max_tokens,
                    )
            except DecisionUnavailable as e:
                return e.to_dict()
            for name in defsmod.DIMENSIONS:
                cur, prop = under["current"], under["proposed"]
                frm = None if name in cur.undetermined else cur.values.get(name)
                to = None if name in prop.undetermined else prop.values.get(name)
                if name in cur.undetermined:
                    und["current"] += 1
                if name in prop.undetermined:
                    und["proposed"] += 1
                if frm != to:
                    moved.append({"brief": b, "dimension": name, "from": frm, "to": to})
    finally:
        await client.aclose()
    cells = len(briefs) * len(defsmod.DIMENSIONS)
    return {
        "current_version": current.version,
        "moved": moved,
        "undetermined_rate": {
            "current": round(und["current"] / cells, 3),
            "proposed": round(und["proposed"] / cells, 3),
        },
    }


@mcp.tool()
async def preflight(
    task: str,
    priority: str = "normal",
    cwd: str | None = None,
) -> dict[str, Any]:
    """Score a brief with d3 and report what the router would see, creating no job.

    Phase 1 returns the scores only: the band or choice per determined dimension (`scores`),
    the model's confidence in each answer (`confidences`), the axes left out for sitting under
    their confidence bar (`undetermined` — neutral to routing, §6), the `truncated` mark, the
    definitions version, and the `priority` echoed back (urgent | normal | background).
    Selection, capacity and queue answers arrive with the routing story. A pointer brief
    ("cat <path> and follow it") is read first, against `cwd` or this server's cwd.
    """
    if priority not in PRIORITIES:
        return {"error": "bad_argument",
                "message": f"priority must be one of {', '.join(PRIORITIES)}"}
    try:
        ep = _router_entry()
    except Exception as e:  # every config failure answers, never raises
        return {"error": "config_error", "message": f"{type(e).__name__}: {e}"}
    if ep is None:
        return _no_router_entry()
    base = str(Path(cwd).expanduser() if cwd else Path(os.getcwd()))
    try:
        text = resolve_brief(task, base)
    except BriefUnresolved as e:
        return e.to_dict()
    try:
        defs = defsmod.load(_db())
    except (sqlite3.Error, db.SchemaError, json.JSONDecodeError) as e:
        return _database_error(e)
    client = _client()
    try:
        got = await score(client, ep.decision_url, ep.decision_model, text, defs,
                          timeout_s=ep.decision_timeout_s, max_tokens=ep.decision_max_tokens)
    except DecisionUnavailable as e:
        return e.to_dict()
    finally:
        await client.aclose()
    return {
        "scores": got.values,
        "confidences": got.confidences,
        "undetermined": sorted(got.undetermined),
        "truncated": got.truncated,
        "defs_version": got.defs_version,
        "priority": priority,
    }


# Calibration runs started in the background by this server process. The set keeps the tasks
# referenced (Python may garbage-collect an unreferenced task mid-flight); a task that ends
# discards itself. A run outlives the tool call that started it and drives on to its own end.
_RUN_TASKS: set[asyncio.Task] = set()


def _open_run_of(conn, endpoint: str) -> str | None:
    """The id of this endpoint's unfinished run when a live process holds its lock, else None.
    A stale lock (the holder crashed) is not an open run: a fresh call will resume that run and
    take the lock over, which is what a crashed run's resume is."""
    from localagents.router import calibrate as calibrate_mod

    r = conn.execute(
        "SELECT r.id FROM calibration_runs r JOIN scorecard_rows s ON s.id = r.row_id"
        " WHERE s.endpoint_name = ? AND r.status = 'running'"
        " ORDER BY r.started DESC, r.rowid DESC LIMIT 1", (endpoint,)).fetchone()
    if r is None:
        return None
    return r["id"] if calibrate_mod.lock_held(r["id"]) else None


@mcp.tool()
async def calibrate(
    endpoint: str,
    tier: str = "core",
    reset: bool = False,
    estimate_only: bool = False,
    keep_going: bool = False,
) -> dict[str, Any]:
    """Run a tier of the calibration set against an endpoint, the same runner as the CLI.

    A run is far longer than a tool call should block: the call starts it as a background
    task of this server and answers at once with the run id and the estimate line. Progress
    reads back through the scorecard tool's `latest_run`. A second call while a run of that
    endpoint is live answers ``run_in_progress`` with the open run's id (the runner's lock
    file is the cross-process backstop). `estimate_only` stays synchronous: it stops after
    the estimate and writes no run. A router entry or an unknown endpoint answers its refusal
    dict. The report lines go to the server's stderr: a background run's caller is long gone,
    and stdout carries this server's protocol.
    """
    from localagents import server
    from localagents.router import calibrate as calibrate_mod

    lines: list[str] = []

    def report(line: str) -> None:
        lines.append(line)
        print(line, file=sys.stderr, flush=True)

    try:
        reg = Registry.load()
    except Exception as e:  # a config file is operator input; every failure answers
        return {"error": "config_error", "message": f"{type(e).__name__}: {e}"}
    ep = reg.endpoints.get(endpoint)
    if ep is None:
        return {"error": "unknown_endpoint", "message": f"unknown endpoint {endpoint!r}"}
    if ep.is_router:
        return {"error": "not_calibratable",
                "message": f"endpoint {endpoint!r} is a router; a router entry is not calibratable"}
    try:
        conn = _db()
        if not estimate_only:
            open_run = _open_run_of(conn, endpoint)
            if open_run is not None:  # a live process already drives this endpoint
                return {"error": "run_in_progress", "run_id": open_run}
        await server.shim.start()
        server.store.describe_server(config=str(getattr(reg, "path", None) or "") or None,
                                     transport="calibrate")
        server._ensure_sampler()

        if estimate_only:
            # it stops after the estimate and opens no run: nothing to wait for in the background
            result = await calibrate_mod.run(reg, server.store, conn, endpoint, tier=tier,
                                             reset=reset, estimate_only=True,
                                             keep_going=keep_going, report=report)
        else:
            opened: asyncio.Future = asyncio.get_running_loop().create_future()

            def on_open(run_id: str) -> None:
                if not opened.done():
                    opened.set_result(("opened", run_id))

            async def drive() -> None:
                try:
                    result = await calibrate_mod.run(reg, server.store, conn, endpoint,
                                                     tier=tier, reset=reset,
                                                     estimate_only=estimate_only,
                                                     keep_going=keep_going, report=report,
                                                     on_open=on_open)
                except Exception as e:  # the tool answers a dict, never a stack trace
                    if not opened.done():
                        opened.set_result(("failed", f"{type(e).__name__}: {e}"))
                    return
                if not opened.done():
                    # a refusal or the estimate stop: nothing opened, the answer is the return
                    opened.set_result(("returned", result))

            task = asyncio.create_task(drive())
            _RUN_TASKS.add(task)
            task.add_done_callback(_RUN_TASKS.discard)
            kind, value = await opened
            if kind == "opened":
                return {"run_id": value, "status": "started",
                        "estimate": next((ln for ln in lines if ln.startswith("estimate:")),
                                         None)}
            if kind == "failed":
                return {"error": "calibrate_failed", "message": value}
            result = value
    except Exception as e:  # the tool answers a dict, never a stack trace
        return {"error": "calibrate_failed", "message": f"{type(e).__name__}: {e}"}
    if result == "run_in_progress":  # the runner's cross-process lock answered, not our pre-check
        return {"error": "run_in_progress", "run_id": _open_run_of(conn, endpoint) or None}
    if result in ("unknown_endpoint", "not_calibratable", "model_unavailable", "endpoint_busy",
                  "no_probes"):
        return {"error": result, "lines": lines}
    if result == "estimate":
        return {"run_id": None, "status": "estimate", "lines": lines}
    row = conn.execute("SELECT status FROM calibration_runs WHERE id = ?", (result,)).fetchone()
    return {"run_id": result, "status": row["status"] if row is not None else "unknown",
            "lines": lines}


def _cell_dict(cell) -> dict[str, Any]:
    return {"dimension": cell.dimension, "language": cell.language,
            "estimate": cell.estimate, "count": cell.count,
            "probes_n": cell.probes_n, "jobs_n": cell.jobs_n, "thin": cell.thin,
            "pass_id": cell.pass_id, "defs_version": cell.defs_version}


def _latest_run(conn, row_id: int) -> dict[str, Any] | None:
    """The row's newest run with its tally, so an operator (or a model) can follow a run
    started by the calibrate tool without joining the result table. ``planned`` counts every
    probe the tier carried, the ones planned-skipped before the run opened included; the
    skipped tally counts both those and any probe skipped later."""
    r = conn.execute(
        "SELECT id, tier, status, plan, started, finished FROM calibration_runs"
        " WHERE row_id = ? ORDER BY started DESC, rowid DESC LIMIT 1", (row_id,)).fetchone()
    if r is None:
        return None
    doc = json.loads(r["plan"])
    statuses = [x["status"] for x in conn.execute(
        "SELECT status FROM calibration_results WHERE run_id = ?", (r["id"],))]
    planned_skips = len(doc.get("skipped", []))
    return {"id": r["id"], "tier": r["tier"], "status": r["status"],
            "planned": len(doc.get("probes", [])) + planned_skips,
            "pass": statuses.count("pass"), "fail": statuses.count("fail"),
            "skipped": statuses.count("skipped") + planned_skips,
            "unreliable": statuses.count("unreliable"),
            "started": r["started"], "finished": r["finished"]}


def _history(conn, row_id: int) -> list[dict[str, Any]]:
    out = []
    for p in conn.execute("SELECT id, at FROM scorecard_passes WHERE row_id = ?"
                          " ORDER BY at, rowid", (row_id,)).fetchall():
        out.append({"pass_id": p["id"], "at": p["at"],
                    "cells": [_cell_dict(c) for c in scorecard_mod.cells(conn, row_id, p["id"])]})
    return out


@mcp.tool()
async def scorecard(
    endpoint: str | None = None,
    history: bool = False,
    recompute: bool = False,
) -> dict[str, Any]:
    """The scorecard's live rows: identity, set version, speed factor, core completeness, the
    two notices, the row's facts, its latest calibration run (id, tier, status, planned and
    the pass/fail/skipped/unreliable tally — a background run's progress reads here) and every
    cell of its latest pass.

    `endpoint` narrows the rows to that name; `history` adds every pass's cells, so a swap
    reads as a before and after; `recompute` runs a pass over the installed set's labels
    first, which is what the operator uses to replay the log after a constants change.
    """
    from localagents.router import calibrate as calibrate_mod

    try:
        installed_set = calibrate_mod.load_set()
    except Exception as e:  # a malformed manifest is operator input; answer, never raise
        return {"error": "probe_set_error", "message": f"{type(e).__name__}: {e}"}
    try:
        conn = _db()
        installed = installed_set.version
        labels = installed_set.labels() if recompute else None
        live = scorecard_mod.rows(conn)
        if endpoint is not None:
            live = [row for row in live if row.endpoint_name == endpoint]
        if recompute:
            for row in live:
                scorecard_mod.run_pass(conn, row.id, probes=labels)
        out = []
        for row in live:
            item = {
                "endpoint_name": row.endpoint_name,
                "served_id": row.served_id,
                "archived": bool(row.archived),
                "set_version": row.set_version,
                "speed_factor": row.speed_factor,
                "core_complete": scorecard_mod.core_complete(conn, row.id),
                "notices": scorecard_mod.notices(conn, row.id, installed),
                "facts": row.facts,
                "latest_run": _latest_run(conn, row.id),
                "cells": [_cell_dict(c) for c in scorecard_mod.cells(conn, row.id)],
            }
            if history:
                item["history"] = _history(conn, row.id)
            out.append(item)
        return {"rows": out}
    except (sqlite3.Error, db.SchemaError) as e:
        return _database_error(e)


@mcp.tool()
async def verdict(
    job_id: str,
    accepted: bool,
    dimensions: dict | None = None,
    correction: dict | None = None,
) -> dict[str, Any]:
    """Record your verdict on a finished job: evidence for the scorecard's next pass, never a
    cell (a pass is run by `scorecard` with `recompute`).

    `accepted` is the job-level verdict. `dimensions` optionally asserts it per axis
    (software_dev, tool_use, computer_use, complexity, judgement): 1 accepted, -1 rejected,
    0 as the job-level verdict; an axis the ticket left undetermined adds no observation and is
    listed in `no_observation`. `correction` optionally gives the brief's true scores
    ({dimension: band or option}) when d3 mis-scored it; it is logged with the ticket's
    definitions version. One verdict per job: a second replaces the first. A job run on a
    named model has no ticket: its brief is scored by d3 now so its demand bands exist. Answers
    `recorded` with the row's notices, or a refusal dict that wrote nothing.
    """
    from localagents.router import calibrate as calibrate_mod

    try:
        reg = Registry.load()
    except Exception as e:  # a config file is operator input; every failure answers
        return {"error": "config_error", "message": f"{type(e).__name__}: {e}"}
    try:
        set_version = calibrate_mod.load_set().version
    except Exception:  # noqa: BLE001 -- a malformed manifest only silences the set-upgrade notice
        set_version = None
    client = _client()
    try:
        return await feedback.apply_verdict(_db(), reg, job_id, accepted, dimensions, correction,
                                            client=client, set_version=set_version)
    except (sqlite3.Error, db.SchemaError, json.JSONDecodeError) as e:
        return _database_error(e)
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(
        prog="localagents router",
        description="the router's command-and-control MCP server (definitions, preflight, "
                    "calibrate, scorecard, verdict); nothing here dispatches work",
    )
    ap.add_argument("--config", help="path to models.yaml (or set LOCALAGENTS_CONFIG)")
    ap.add_argument("--transport", default="stdio", choices=["stdio"],
                    help="stdio (the only transport this build serves)")
    a = ap.parse_args(argv)
    if a.config:
        os.environ["LOCALAGENTS_CONFIG"] = str(Path(a.config).expanduser().resolve())
    mcp.run(transport=a.transport)
