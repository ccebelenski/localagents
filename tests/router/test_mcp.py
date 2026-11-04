"""router 5 (docs/superpowers/specs/2026-11-02-router-design.md §11, phase 1): the router's
command-and-control MCP surface — ``definitions`` (get, set, dry run) and a score-only
``preflight``. Every failure is a dict, never an exception; every success is a dict. The d3
server is a faked ``score`` or a MockTransport; the registry is a monkeypatched ``Registry.load``
holding one ``backend: router`` entry; the database is conftest's ``conn``."""

import asyncio
import copy
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from localagents import db
from localagents.jobs import state_dir
from localagents.registry import Defaults, Endpoint, Registry
from localagents.router import calibrate as calibrate_mod
from localagents.router import decision
from localagents.router import definitions as defsmod
from localagents.router import mcp as router
from localagents.router import scorecard


@pytest.fixture
def dbconn(conn, monkeypatch):
    """The tool's database is the fixture's file: the module's once-per-process connection
    stays shut for the whole test (monkeypatch restores ``_db`` and no conn was opened)."""
    monkeypatch.setattr(router, "_db", lambda: conn)
    return conn


def _router_registry(monkeypatch):
    """A config with exactly one router entry; its decision_* fields are the client's arguments."""
    entry = Endpoint(name="auto", base_url="", backend="router",
                     decision_url="http://d3.test:8765", decision_model="d3-test",
                     decision_timeout_s=5.0, decision_max_tokens=100)
    reg = Registry(path=None, endpoints={"auto": entry}, defaults=Defaults(), raw={}, local={})
    monkeypatch.setattr(Registry, "load", staticmethod(lambda path=None: reg))
    return reg


def _proposed():
    """A valid revision the dry-run tests propose: recognizable by its instruction."""
    doc = copy.deepcopy(defsmod.DEFAULT_DOCUMENT)
    doc["complexity"]["instruction"] = "PROPOSED: how much scope and judgement?"
    return doc


def test_definitions_get_returns_current_version(dbconn):
    out = asyncio.run(router.definitions())
    assert out["version"] == 1
    assert out["document"] == defsmod.DEFAULT_DOCUMENT


def test_definitions_set_increments(dbconn):
    doc = copy.deepcopy(defsmod.DEFAULT_DOCUMENT)
    doc["complexity"]["instruction"] = "How much scope, and how much judgement?"
    out = asyncio.run(router.definitions(action="set", document=doc))
    assert out == {"version": 2, "document": doc}
    assert asyncio.run(router.definitions())["version"] == 2


def test_definitions_set_invalid_is_a_dict_error(dbconn):
    doc = copy.deepcopy(defsmod.DEFAULT_DOCUMENT)
    del doc["complexity"]
    out = asyncio.run(router.definitions(action="set", document=doc))
    assert out["error"] == "invalid_document"
    assert "complexity" in out["message"]
    assert defsmod.load(dbconn).version == 1  # nothing was written


def test_corrupt_stored_document_is_database_error(dbconn):
    """A stored document that is not JSON is a broken database, not a rejected edit: the
    answer is database_error, and a valid set is refused the same way rather than being
    read as an invalid_document that would send the caller editing a fine document."""
    defsmod.load(dbconn)  # seed v1, then corrupt the stored text
    dbconn.execute("UPDATE dimension_definitions SET document = 'not json' WHERE version = 1")
    out = asyncio.run(router.definitions())
    assert out["error"] == "database_error"
    out = asyncio.run(router.definitions(action="set", document=copy.deepcopy(defsmod.DEFAULT_DOCUMENT)))
    assert out["error"] == "database_error"
    # validate's ValueError keeps its own answer even beside a corrupt store
    out = asyncio.run(router.definitions(action="set", document={"complexity": {}}))
    assert out["error"] == "invalid_document"


def test_definitions_unknown_action_is_a_dict_error(dbconn):
    """The surface's contract: no action string raises, and none writes."""
    out = asyncio.run(router.definitions(action="drop"))
    assert out["error"] == "bad_argument"
    assert defsmod.load(dbconn).version == 1


def test_definitions_dry_run_reports_moves_without_saving(dbconn, monkeypatch, tmp_path):
    _router_registry(monkeypatch)
    alpha = tmp_path / "alpha.md"
    alpha.write_text("alpha: fix the parser\n")
    beta = tmp_path / "beta.md"
    beta.write_text("beta: rename a variable\n")
    calls = []

    async def fake(client, url, model, text, defs, **kw):
        # the proposed document (by its marker) moves alpha's complexity a band, and drops
        # beta's language under the confidence bar
        proposed = defs.document["complexity"]["instruction"].startswith("PROPOSED")
        values = {"language": "python", "complexity": 3 if proposed and "alpha" in text else 2}
        if proposed and "beta" in text:
            del values["language"]
        calls.append((text, proposed))
        undetermined = {d for d in defsmod.DIMENSIONS if d not in values}
        return decision.BriefScores(values, {d: 0.9 for d in values}, undetermined, False, {},
                                    defs.version)

    monkeypatch.setattr(router, "score", fake)
    out = asyncio.run(router.definitions(action="set", document=_proposed(), dry_run=True,
                                         briefs=[str(alpha), str(beta)], cwd=str(tmp_path)))
    assert out["current_version"] == 1
    assert out["moved"] == [
        {"brief": str(alpha), "dimension": "complexity", "from": 2, "to": 3},
        {"brief": str(beta), "dimension": "language", "from": "python", "to": None},
    ]
    cells = 2 * len(defsmod.DIMENSIONS)
    assert out["undetermined_rate"] == {"current": round(10 / cells, 3),
                                        "proposed": round(11 / cells, 3)}
    assert len(calls) == 4  # every brief scored under both documents
    assert defsmod.load(dbconn).version == 1  # the proposed document is never saved


def test_definitions_dry_run_without_briefs_is_no_briefs(dbconn, monkeypatch):
    _router_registry(monkeypatch)
    for briefs in (None, []):
        out = asyncio.run(router.definitions(action="set", document=_proposed(), dry_run=True,
                                             briefs=briefs))
        assert out["error"] == "no_briefs"
    assert defsmod.load(dbconn).version == 1


def test_preflight_scores_echoes_priority_and_decides(dbconn, monkeypatch):
    _router_registry(monkeypatch)
    seen = {}

    async def fake(client, url, model, text, defs, **kw):
        seen.update(url=url, model=model, text=text, **kw)
        values = {"complexity": 2, "language": "python"}
        undetermined = {d for d in defsmod.DIMENSIONS if d not in values}
        return decision.BriefScores(values, {d: 0.9 for d in values}, undetermined, False, {},
                                    defs.version)

    monkeypatch.setattr(router, "score", fake)
    out = asyncio.run(router.preflight(task="fix the parser", priority="background"))
    # router 12 (pin moved): the phase-1 score keys stay as they were, and the decision dispatch
    # would take now rides beside them; with no real endpoint there is no row to choose
    assert {k: out[k] for k in ("scores", "confidences", "undetermined", "truncated",
                                "defs_version", "priority")} == {
        "scores": {"complexity": 2, "language": "python"},
        "confidences": {"complexity": 0.9, "language": 0.9},
        "undetermined": sorted(set(defsmod.DIMENSIONS) - {"complexity", "language"}),
        "truncated": False,
        "defs_version": 1,
        "priority": "background",
    }
    assert out["chosen"] is None and out["decision"]["reason"] == "no_calibrated_row"
    # the entry's decision_* fields are the client's arguments; priority echoes, default normal
    assert (seen["url"], seen["model"]) == ("http://d3.test:8765", "d3-test")
    assert seen["timeout_s"] == 5.0 and seen["max_tokens"] == 100
    assert seen["text"] == "fix the parser"
    assert asyncio.run(router.preflight(task="fix the parser"))["priority"] == "normal"


def test_preflight_bad_priority_is_a_dict_error(dbconn, monkeypatch):
    _router_registry(monkeypatch)
    out = asyncio.run(router.preflight(task="fix the parser", priority="whenever"))
    assert out["error"] == "bad_argument"
    assert "priority" in out["message"]


def test_preflight_without_router_entry_is_no_router_entry(dbconn, monkeypatch):
    real = Endpoint(name="one", base_url="http://one:8080", backend="llama.cpp")
    reg = Registry(path=None, endpoints={"one": real}, defaults=Defaults(), raw={}, local={})
    monkeypatch.setattr(Registry, "load", staticmethod(lambda path=None: reg))
    out = asyncio.run(router.preflight(task="fix the parser"))
    assert out == {"error": "no_router_entry",
                   "message": "add a backend: router entry to models.yaml"}


def _overwrite_config(text: str) -> None:
    """Replace the file the autouse fixture points LOCALAGENTS_CONFIG at; the next
    ``Registry.load()`` by either tool reads it."""
    Path(os.environ["LOCALAGENTS_CONFIG"]).write_text(text)


def test_config_parse_error_is_config_error(dbconn):
    """A models.yaml the parser chokes on is operator input: the tools answer, not raise."""
    _overwrite_config("endpoints: {auto: unclosed\n")
    out = asyncio.run(router.preflight(task="fix the parser"))
    assert out["error"] == "config_error"
    assert out["message"].startswith("ParserError")
    out = asyncio.run(router.definitions(action="set", document=_proposed(), dry_run=True,
                                         briefs=["alpha.md"]))
    assert out["error"] == "config_error"


def test_config_shape_error_is_config_error(dbconn):
    """Not a mapping where a mapping is expected: the same answer, whatever Python raises."""
    _overwrite_config("endpoints:\n  - one\n  - two\n")
    out = asyncio.run(router.preflight(task="fix the parser"))
    assert out["error"] == "config_error"
    assert out["message"].startswith("TypeError")
    out = asyncio.run(router.definitions(action="set", document=_proposed(), dry_run=True,
                                         briefs=["alpha.md"]))
    assert out["error"] == "config_error"


def test_preflight_d3_down_is_model_unavailable(dbconn, monkeypatch):
    _router_registry(monkeypatch)

    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(router, "_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(down)))
    out = asyncio.run(router.preflight(task="fix the parser"))
    assert out["error"] == "model_unavailable"
    assert out["reason"] == "down"
    assert "d3 server" in out["message"]


def test_preflight_pointer_brief_missing_is_brief_unresolved(dbconn, monkeypatch, tmp_path):
    _router_registry(monkeypatch)
    gone = tmp_path / "gone.md"
    out = asyncio.run(router.preflight(task=f"cat {gone} and follow it", cwd=str(tmp_path)))
    assert out["error"] == "brief_unresolved"
    assert out["path"] == str(gone)


def test_tool_schemas_list_exactly_the_routers_tools():
    """Phase 1 shipped definitions and preflight; router 9a adds calibrate and scorecard,
    router 13 verdict, router 12 explain and queue. Descriptions stay under Claude Code's cap."""
    tools = router.mcp._tool_manager.list_tools()
    assert [t.name for t in tools] == ["definitions", "preflight", "calibrate", "scorecard", "verdict",
                                       "explain", "queue"]
    for t in tools:
        assert len(t.description or "") <= 2048, t.name


# --- the calibrate tool: the run is a background task of this server ----------------------------

ESTIMATE = "estimate: 1 probes, ~0.5-1.0 min (speed factor 1.00, margin 50%)"


def _calibrate_setup(monkeypatch):
    """A registry with one calibratable endpoint and the server's start-ups pinned to no-ops:
    the shim and the sampler belong to the real server, not to the tool's answer."""
    from localagents import server

    ep = Endpoint(name="ep", base_url="http://ep:1/v1", backend="llama.cpp", host="local")
    reg = Registry(path=None, endpoints={"ep": ep}, defaults=Defaults(), raw={}, local={})
    monkeypatch.setattr(Registry, "load", staticmethod(lambda path=None: reg))

    async def no_start():
        return 0

    monkeypatch.setattr(server.shim, "start", no_start)
    monkeypatch.setattr(server, "_ensure_sampler", lambda: None)
    monkeypatch.setattr(server.store, "describe_server", lambda **kw: None)


def _served_row(conn):
    eid = db.lookup_id(conn, "endpoints", name="ep", base_url="http://ep:1/v1",
                       backend="llama.cpp", host="local")
    mid = db.lookup_id(conn, "models", served_id="served")
    return scorecard.get_or_create_row(conn, "ep", "served", eid, mid)


def _fake_open_run(monkeypatch, row):
    """A stand-in for ``calibrate.run``: opens the running row and the holder's lock, reports
    the estimate, announces the opening through ``on_open``, then waits on the returned event
    -- a run the tool must answer while it is still working."""
    hold = asyncio.Event()

    async def fake_run(reg, store, conn, endpoint, **kw):
        run_id = "run-bg"
        conn.execute(
            "INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
            " VALUES (?, ?, 'core', '2026.11', '{\"probes\": [\"alpha\"], \"skipped\": []}',"
            " ?, 'running')", (run_id, row.id, time.time()))
        lock_dir = state_dir() / "calibration" / run_id
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "lock").write_text(f"{os.getpid()} {time.time()}\n")
        kw["report"](ESTIMATE)
        kw["on_open"](run_id)
        await hold.wait()
        return run_id

    monkeypatch.setattr(calibrate_mod, "run", fake_run)
    return hold


async def _drain():
    await asyncio.gather(*list(router._RUN_TASKS))


def test_calibrate_tool_starts_a_run_and_returns_at_once(dbconn, monkeypatch):
    row = _served_row(dbconn)
    _calibrate_setup(monkeypatch)
    hold = _fake_open_run(monkeypatch, row)

    async def go():
        out = await router.calibrate("ep")
        assert out == {"run_id": "run-bg", "status": "started", "estimate": ESTIMATE}
        # the open run reads back through the scorecard while it is still working
        card = await router.scorecard()
        run = card["rows"][0]["latest_run"]
        assert run["id"] == "run-bg" and run["status"] == "running" and run["planned"] == 1
        hold.set()
        await _drain()

    asyncio.run(go())


def test_calibrate_tool_refuses_a_second_run_in_progress(dbconn, monkeypatch):
    row = _served_row(dbconn)
    _calibrate_setup(monkeypatch)
    hold = _fake_open_run(monkeypatch, row)

    async def go():
        first = await router.calibrate("ep")
        assert first["status"] == "started"
        # the pre-check sees the open row whose lock the first task holds
        second = await router.calibrate("ep")
        assert second == {"error": "run_in_progress", "run_id": "run-bg"}
        hold.set()
        await _drain()

    asyncio.run(go())


def test_scorecard_reports_the_latest_run(dbconn):
    conn = dbconn
    row = _served_row(conn)
    eid2 = db.lookup_id(conn, "endpoints", name="ep2", base_url="http://ep2:1/v1",
                        backend="llama.cpp", host="local")
    mid = db.lookup_id(conn, "models", served_id="served")
    scorecard.get_or_create_row(conn, "ep2", "served", eid2, mid)
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan,"
                 " started, finished, status) VALUES ('old', ?, 'core', '2026.10', '{}',"
                 " 5.0, 6.0, 'complete')", (row.id,))
    plan = json.dumps({"probes": ["alpha", "beta"], "skipped": [["gamma", "no tool"]]})
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan,"
                 " started, finished, status) VALUES ('new', ?, 'core', '2026.11', ?,"
                 " 20.0, 22.0, 'complete')", (row.id, plan))
    conn.execute("INSERT INTO calibration_results (run_id, probe_id, status, seconds)"
                 " VALUES ('new', 'alpha', 'pass', 5.0)")
    conn.execute("INSERT INTO calibration_results (run_id, probe_id, status, seconds)"
                 " VALUES ('new', 'beta', 'unreliable', 5.0)")
    out = asyncio.run(router.scorecard())
    by_ep = {r["endpoint_name"]: r for r in out["rows"]}
    assert by_ep["ep"]["latest_run"] == {
        "id": "new", "tier": "core", "status": "complete", "planned": 3,
        "pass": 1, "fail": 0, "skipped": 1, "unreliable": 1, "started": 20.0, "finished": 22.0}
    assert by_ep["ep2"]["latest_run"] is None


# --- router 12: the full preflight, explain, queue (design §11) --------------------------------

def _fleet_registry(monkeypatch, pools=None):
    """The router entry plus two real endpoints, one slot each; probes answer from `facts`."""
    entry = Endpoint(name="auto", base_url="", backend="router", decision_url="http://d3.test:8765")
    endpoints = {"auto": entry}
    facts = {}
    for name, served in (("first", "model-a"), ("second", "model-b")):
        endpoints[name] = Endpoint(name=name, base_url=f"http://{name}:8080", backend="llama.cpp")
        facts[name] = {"up": True, "models": [served], "context": {served: 131_072},
                       "slots": {"total": 1, "busy": 0}}
    reg = Registry(path=None, endpoints=endpoints, defaults=Defaults(), raw={}, local={},
                   pools=pools or {})
    monkeypatch.setattr(Registry, "load", staticmethod(lambda path=None: reg))

    async def probe(self, ep):
        if ep.is_router:
            return {"up": True, "models": [ep.name], "router": True}
        return copy.deepcopy(facts[ep.name])

    monkeypatch.setattr(Registry, "probe", probe)
    return reg, facts


def _row(conn, endpoint, served, cells):
    eid = db.lookup_id(conn, "endpoints", name=endpoint, base_url=f"http://{endpoint}:8080",
                       backend="llama.cpp", host="local")
    mid = db.lookup_id(conn, "models", served_id=served)
    row = scorecard.get_or_create_row(conn, endpoint, served, eid, mid)
    conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan, started, status)"
                 " VALUES (?, ?, 'core', '2026.11', '{}', 1.0, 'complete')", (db.new_uuid(), row.id))
    pass_id = db.new_uuid()
    conn.execute("INSERT INTO scorecard_passes (id, row_id, at, constants, observations)"
                 " VALUES (?, ?, 2.0, '{}', 0)", (pass_id, row.id))
    version = defsmod.load(conn).version
    for dimension, estimate in cells.items():
        conn.execute("INSERT INTO scorecard_cells (pass_id, dimension, language, estimate, count,"
                     " probes_n, jobs_n, thin, defs_version) VALUES (?, ?, '', ?, 6, 6, 0, 0, ?)",
                     (pass_id, dimension, estimate, version))
    return row.id


_STRONG = {d: 4 for d in ("software_dev", "tool_use", "computer_use", "complexity", "judgement",
                          "completeness", "adherence")}


def _fake_score(monkeypatch, values):
    async def fake(client, url, model, text, defs, **kw):
        undetermined = {d for d in defsmod.DIMENSIONS if d not in values}
        return decision.BriefScores(dict(values), {d: 0.9 for d in values}, undetermined, False,
                                    {}, defs.version)

    monkeypatch.setattr(router, "score", fake)


def test_preflight_returns_chosen_endpoint_and_served_id(dbconn, monkeypatch):
    """§7 "Decided": preflight returns what dispatch would decide now (the Decision as a dict)
    plus chosen {endpoint, served_id}, so a brief can be sized to the model; avoid_job filters
    that job's served id the same way run_agent does; nothing is created."""
    _fleet_registry(monkeypatch)
    _fake_score(monkeypatch, {"software_dev": 3, "complexity": 2})
    a = _row(dbconn, "first", "model-a", {**_STRONG, "software_dev": 2})
    b = _row(dbconn, "second", "model-b", _STRONG)
    out = asyncio.run(router.preflight(task="build the parser"))
    assert out["chosen"] == {"endpoint": "second", "served_id": "model-b"}
    d = out["decision"]
    assert d["reason"] is None and d["divert"] is False and d["queue_for"] is None
    assert d["chosen_reason"].startswith("shortfall 0.00 on second x model-b")
    assert {c["row_id"] for c in d["considered"]} == {a, b}
    jobs_before = dbconn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # avoid_job: a finished job that ran on model-b moves the choice to model-a
    eid = db.lookup_id(dbconn, "endpoints", name="second", base_url="http://second:8080",
                       backend="llama.cpp", host="local")
    mid = db.lookup_id(dbconn, "models", served_id="model-b")
    dbconn.execute("INSERT INTO jobs (id, endpoint_id, model_id, status, created_at)"
                   " VALUES ('builder', ?, ?, 'succeeded', 1.0)", (eid, mid))
    out = asyncio.run(router.preflight(task="build the parser", avoid_job="builder"))
    assert out["chosen"] == {"endpoint": "first", "served_id": "model-a"}
    assert {c["row_id"]: c["filtered"] for c in out["decision"]["considered"]}[b] == "avoid_job"
    assert asyncio.run(router.preflight(task="t", avoid_job="nope"))["error"] == "bad_argument"
    assert dbconn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs_before + 1


def test_explain_returns_the_ticket_and_routed_by(monkeypatch):
    """§11 explain: the ticket (§4) of a routed job, with the record's routed_by and real
    endpoint; a named job has no ticket, an unknown id is unknown_job."""
    from localagents import jobs
    from localagents.router import dispatch

    store = jobs.JobStore()
    monkeypatch.setattr(router, "_db", lambda: store.conn)
    reg, _facts = _fleet_registry(monkeypatch)
    monkeypatch.setattr(dispatch, "score", lambda *a, **kw: _score_now({"software_dev": 2}))
    _row(store.conn, "second", "model-b", _STRONG)

    async def go():
        routed = await dispatch.route(reg, store.conn, reg.endpoints["auto"], "t", "/w", "normal")
        res = routed.resolution
        job = dispatch.new_job(store, priority="normal", routed=routed, task="t",
                               model=res.served_model, endpoint=res.endpoint.name,
                               base_url=res.endpoint.url, cwd="/w", run_cwd="/w",
                               isolation="none", backend=res.endpoint.backend,
                               host=res.endpoint.host, context=res.context)
        return job, await router.explain(job.id)

    job, out = asyncio.run(go())
    assert out["job_id"] == job.id and out["endpoint"] == "second" and out["model"] == "model-b"
    assert out["routed_by"]["router"] == "auto"
    assert out["ticket"]["chosen_row"] is not None and out["ticket"]["scores"]["values"] == {"software_dev": 2}
    named = dispatch.new_job(store, priority="normal", task="t", model="model-a", endpoint="first",
                             base_url="http://first:8080", cwd="/w", run_cwd="/w",
                             isolation="none", backend="llama.cpp", host="local")
    assert asyncio.run(router.explain(named.id))["error"] == "no_ticket"
    assert asyncio.run(router.explain("nope"))["error"] == "unknown_job"
    store.close()


async def _score_now(values):
    undetermined = {d for d in defsmod.DIMENSIONS if d not in values}
    return decision.BriefScores(dict(values), {d: 0.9 for d in values}, undetermined, False, {}, 1)


def test_queue_lists_waiting_and_running_and_drops(monkeypatch):
    """§11 queue: per endpoint, the sessions and live facts, our running sessions and the
    queued entries in order with positions and expected waits; drop(job_id) removes an entry
    by hand and a still-queued job becomes dropped ("dropped by the queue tool")."""
    from localagents import jobs
    from localagents.router import dispatch

    store = jobs.JobStore()
    monkeypatch.setattr(router, "_db", lambda: store.conn)
    _fleet_registry(monkeypatch)

    def named(task, forced=False):
        return dispatch.new_job(store, priority="normal", forced=forced, task=task, model="model-a",
                                endpoint="first", base_url="http://first:8080", cwd="/w",
                                run_cwd="/w", isolation="none", backend="llama.cpp", host="local",
                                context=131_072)

    holder, waiting = named("a", forced=True), named("b")
    holder.status = "running"
    holder.changed()
    out = asyncio.run(router.queue(endpoint="first"))
    [first] = out["endpoints"]
    assert first["endpoint"] == "first" and first["sessions"]["capacity"] == 1
    assert first["sessions"]["ours_running"] == 1
    assert [r["job_id"] for r in first["running"]] == [holder.id]
    assert [(q["job_id"], q["position"]) for q in first["queued"]] == [(waiting.id, 1)]
    assert first["queued"][0]["expected_wait_s"] > 0
    assert first["facts"]["up"] is True
    dropped = asyncio.run(router.queue(drop=waiting.id))
    assert dropped == {"dropped": True, "job_id": waiting.id}
    row = store.conn.execute("SELECT status, error FROM jobs WHERE id = ?", (waiting.id,)).fetchone()
    assert tuple(row) == ("dropped", "dropped by the queue tool")
    assert asyncio.run(router.queue(drop=waiting.id)) == {"dropped": False, "job_id": waiting.id}
    store.close()
