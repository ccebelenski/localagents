"""The router's command-and-control MCP surface (docs/superpowers/specs/2026-11-02-router-design.md
§11), served by ``localagents router`` as a second, small MCP server. Phase 1 builds two of the
spec's tools: ``definitions`` (get, set, dry run) and a score-only ``preflight``; the rest arrive
with the routing stories. Nothing on this surface dispatches work.

The router entry is the config's first ``backend: router`` endpoint; its ``decision_*`` fields are
the d3 client's arguments. The database connection is opened once per process, lazily, the way
``server.py`` holds its job store. Every failure is a dict, never a raised exception: the caller
is a model reading the answer, not a stack trace."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from localagents import db
from localagents.registry import Endpoint, Registry
from localagents.router import definitions as defsmod
from localagents.router.decision import (
    BriefUnresolved,
    DecisionUnavailable,
    resolve_brief,
    score,
)

INSTRUCTIONS = (
    "Command and control for the localagents router: read and edit the dimension definitions "
    "(every edit through a dry run over briefs), and preflight a brief to see the scores d3 "
    "gives it right now. Nothing on this surface dispatches work; jobs run through the "
    "localagents server."
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


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(
        prog="localagents router",
        description="the router's command-and-control MCP server (definitions, preflight); "
                    "nothing here dispatches work",
    )
    ap.add_argument("--config", help="path to models.yaml (or set LOCALAGENTS_CONFIG)")
    ap.add_argument("--transport", default="stdio", choices=["stdio"],
                    help="stdio (the only transport this build serves)")
    a = ap.parse_args(argv)
    if a.config:
        os.environ["LOCALAGENTS_CONFIG"] = str(Path(a.config).expanduser().resolve())
    mcp.run(transport=a.transport)
