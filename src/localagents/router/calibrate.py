"""The calibration runner, the manifest loader and ``localagents calibrate`` (router-design
spec §10, calibration-set spec §5-§11).

A probe set is one YAML manifest per tier under ``sets/``; the loader validates it (every §8
field present, every checker proof ``{stub: fail, reference: pass}``, unique ids and a fixture
directory per probe). A run plans a tier, reports the nominal estimate before it starts, runs
each probe as a ``calibration = 1`` background job through ``run_probe_job`` (the module seam a
test replaces), records one ``calibration_results`` row per probe, stops early when the actuals
outrun the estimate, and ends with a scorecard pass so the row has cells the moment it is
complete. The facts probe runs five ways and writes the row's thinking/effort facts.

Any probe measured while foreign load ran on the box is ``unreliable`` and rerun; a resumed run
keeps its ``pass``/``fail`` results and reruns ``skipped``, ``unreliable`` and missing probes.
The estimate carries to another machine by the row's speed factor: the row's own when a pass
has measured one, else the manifest's reference tokens/s over this box's throughput probe, else
1.0. Everything the runner writes is a row in the shared database; the model work is a job the
server records like any other."""

import asyncio
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from localagents import db, server
from localagents.jobs import state_dir
from localagents.registry import Registry
from localagents.router import capacity, scorecard

# calibration-set spec §8: every field a probe entry must carry. ``nominal_source`` and ``kind``
# are the manifest's optional fields; the rest are the spec's required ones.
MANIFEST_FIELDS = ("id", "tier", "source", "language", "labels", "source_difficulty", "brief",
                   "checker", "checker_proof", "nominal_s", "spread", "needs", "variant")
PROBE_KINDS = ("probe", "facts")

# calibration-set spec §10: the core run's cheap facts probe, five ways (defaults plus the two
# levers). The names label the five run directories and the levers travel to the job as given.
FACTS_LEVERS = (("defaults", {}),
                ("thinking_on", {"thinking": True}),
                ("thinking_off", {"thinking": False}),
                ("effort_low", {"effort": "low"}),
                ("effort_high", {"effort": "high"}))
THINKING_MARK = "[thinking]"
EFFORT_RATIO = 1.5  # §6: high effort's thinking text is at least 1.5x low's, by characters

# §5: the probe's job may run four times its nominal duration at the row's speed factor.
TIMEOUT_MULTIPLIER = 4.0
# §10 "Reliable": a sample above ours during a probe is foreign load; the run redos the probe.
FOREIGN_LOAD_SAMPLE_S = 30.0

# The throughput probe (§10: "from the throughput probe, run first, which needs the endpoint
# quiet"): a short completion to warm the box and one 2k-token completion alone, then two
# concurrently (the calibration-set spec §7's curves are shapes; the probe replaces the prior's
# shape with this box's). The warm-up is discarded, never measured: a cold box pays prompt setup
# and slot warm-up inside its first completion's wall time, and a single measured that way sits
# low enough to push the concurrency-2 curve point past 1.0 on a two-slot box.
THROUGHPUT_WARMUP_TOKENS = 32
THROUGHPUT_MAX_TOKENS = 2000
THROUGHPUT_PROMPT = "Count upward from 1, one number per line."


@dataclass
class Probe:
    """One manifest entry. ``brief_path`` and ``fixture_dir`` are resolved at load time;
    ``labels`` stays the manifest's mapping (``ProbeSet.labels`` merges the language)."""

    id: str
    tier: str
    brief_path: Path
    checker: str
    nominal_s: float
    labels: dict
    needs: list[str]
    source: dict
    licence: str
    language: str
    fixture_dir: Path
    kind: str = "probe"
    nominal_source: str = "measured"
    variant: object = None
    spread: dict = field(default_factory=dict)

    def brief_text(self) -> str:
        return self.brief_path.read_text()


@dataclass
class ProbeSet:
    """One tier's validated manifest. ``labels()`` is what ``scorecard.run_pass`` takes; a facts
    probe is not an observation and stays out of it (its output is the row's facts, not a cell)."""

    version: str
    reference_tps: float | None
    probes: list[Probe]
    path: Path

    def labels(self) -> dict:
        out = {}
        for probe in self.probes:
            if probe.kind == "facts":
                continue
            labels = dict(probe.labels)
            labels["language"] = probe.language
            out[probe.id] = {"labels": labels, "nominal_s": float(probe.nominal_s)}
        return out


@dataclass
class Plan:
    probes: list[Probe]
    skipped: list[tuple[Probe, str]]


@dataclass
class ProbeOutcome:
    status: str
    seconds: float
    reason: str | None
    job_id: str | None


class EndpointBusy(Exception):
    """The throughput probe refuses to measure an endpoint someone else is using (§10)."""


# --- the loader (spec §8; spec §11 "Loader") ------------------------------------------------------


def _default_set_path() -> Path:
    return Path(__file__).resolve().parent / "sets" / "core.yaml"


def load_set(path=None) -> ProbeSet:
    """The manifest at ``path``, ``LOCALAGENTS_PROBE_SET`` or the package's ``sets/core.yaml``.
    ``brief`` and the fixture directory resolve against the manifest's own directory. A bad
    manifest is a ``ValueError`` naming the probe and the field it refuses."""
    p = Path(path) if path is not None else Path(
        os.environ.get("LOCALAGENTS_PROBE_SET") or _default_set_path())
    p = p.expanduser()
    data = yaml.safe_load(p.read_text()) or {}
    for field_name in ("version", "probes"):
        if field_name not in data:
            raise ValueError(f"{p}: missing field {field_name!r}")
    version = str(data["version"])
    reference_tps = data.get("reference_tps")
    if reference_tps is not None:
        try:
            reference_tps = float(reference_tps)
        except (TypeError, ValueError):
            raise ValueError(f"{p}: reference_tps must be a number or null")
    probes: list[Probe] = []
    seen: set[str] = set()
    base = p.parent
    for entry in data.get("probes") or []:
        if not isinstance(entry, dict):
            raise ValueError(f"{p}: every probe entry must be a mapping")
        pid = entry.get("id") or "?"
        for field_name in MANIFEST_FIELDS:
            if field_name not in entry:
                raise ValueError(f"probe {pid!r}: missing field {field_name!r}")
        pid = str(entry["id"])
        if pid in seen:
            raise ValueError(f"probe {pid!r}: duplicate id")
        seen.add(pid)
        proof = entry["checker_proof"]
        if not (isinstance(proof, dict) and set(proof) == {"stub", "reference"}
                and proof.get("stub") == "fail" and proof.get("reference") == "pass"):
            raise ValueError(f"probe {pid!r}: checker_proof must be "
                             "{stub: fail, reference: pass}")
        kind = str(entry.get("kind") or "probe")
        if kind not in PROBE_KINDS:
            raise ValueError(f"probe {pid!r}: kind must be one of {', '.join(PROBE_KINDS)}")
        fixture_dir = base / "fixtures" / pid
        if not fixture_dir.is_dir():
            raise ValueError(f"probe {pid!r}: fixture directory {fixture_dir} does not exist")
        source = dict(entry["source"] or {})
        probes.append(Probe(
            id=pid,
            tier=str(entry["tier"]),
            brief_path=base / str(entry["brief"]),
            checker=str(entry["checker"]),
            nominal_s=float(entry["nominal_s"]),
            labels=dict(entry["labels"] or {}),
            needs=list(entry["needs"] or []),
            source=source,
            licence=str(source.get("licence", "")),
            language=str(entry["language"]),
            fixture_dir=fixture_dir,
            kind=kind,
            nominal_source=str(entry.get("nominal_source") or "measured"),
            variant=entry["variant"],
            spread=dict(entry["spread"] or {}),
        ))
    return ProbeSet(version, reference_tps, probes, p)


def present_tools(probe_set: ProbeSet | None = None) -> set[str]:
    """The tool names the probes need that this box has. ``stubmcp`` is the set's own stub, so
    it is always present (a probe needing it is runnable wherever the package is)."""
    probe_set = probe_set if probe_set is not None else load_set()
    tools = {"stubmcp"}
    for probe in probe_set.probes:
        for name in probe.needs:
            if name != "stubmcp" and shutil.which(name):
                tools.add(name)
    return tools


def plan(probe_set: ProbeSet, tier: str, tools: set[str]) -> Plan:
    """The tier's runnable probes and, beside them, the ones an absent tool skips (the spec's
    "environment reason": retried on resume and the cell it would feed stays thin)."""
    runnable, skipped = [], []
    for probe in probe_set.probes:
        if probe.tier != tier:
            continue
        missing = [name for name in probe.needs if name != "stubmcp" and name not in tools]
        if missing:
            skipped.append((probe, "missing tool: " + ", ".join(missing)))
        else:
            runnable.append(probe)
    return Plan(runnable, skipped)


def estimate_s(plan_obj: Plan, speed_factor: float, margin: float = 0.5) -> tuple[float, float]:
    """The stated estimate before anything runs (§10 "Time, honestly"): the tier's nominal
    total times the row's speed factor, with the margin's upper bound."""
    low = sum(probe.nominal_s for probe in plan_obj.probes) * float(speed_factor)
    return float(low), float(low * (1.0 + margin))


# --- a probe's tree and checker (spec §5; the fixture contract) -----------------------------------


PROBE_BASE_REF = "refs/probe/base"  # the revision a probe job starts from, recorded by materialize


def _git_head(root, ref: str = "HEAD") -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", ref],
                             capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def _probe_base(root) -> str | None:
    """The base the checkers compare against: the ref materialize recorded, so a job that
    commits (completeness-tail asks for one) cannot move its own base; HEAD only for a repo
    nothing recorded (a checker run by hand)."""
    return _git_head(root, PROBE_BASE_REF) or _git_head(root)


def materialize(probe: Probe, dest) -> Path:
    """The probe's project as a freshly committed repository at ``dest``: ``project/`` copied,
    ``git init`` and one ``fixture`` commit, then ``setup.sh`` at the root if the fixture ships
    one. The runner never commits here again, so a checker can read the base revision. ``dest``
    must not hold a tree already: a non-empty destination is a ``ValueError`` naming it, so no
    caller can inherit a previous attempt's work by accident."""
    dest = Path(dest)
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise ValueError(f"materialize refuses the non-empty destination {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(probe.fixture_dir / "project", dest, dirs_exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(dest), check=True)
    subprocess.run(["git", "-c", "user.name=calibrate", "-c", "user.email=calibrate@localagents",
                    "add", "-A"], cwd=str(dest), check=True)
    subprocess.run(["git", "-c", "user.name=calibrate", "-c", "user.email=calibrate@localagents",
                    "commit", "-q", "-m", "fixture"], cwd=str(dest), check=True)
    setup = probe.fixture_dir / "setup.sh"
    if setup.is_file():
        subprocess.run(["bash", str(setup)], cwd=str(dest), check=True)
    # the base is recorded AFTER setup.sh (tool-bisect rebuilds its history there) as a ref
    # outside branches and tags: what a checker's "untouched" compares against
    subprocess.run(["git", "update-ref", PROBE_BASE_REF, "HEAD"], cwd=str(dest), check=True)
    return dest


def _next_attempt_dir(probe_dir) -> Path:
    """The root for a new attempt of a probe: ``<run_dir>/<probe id>/<n>``, n one past the
    number of existing attempt directories. Nothing is removed: a previous attempt's tree is
    evidence, and a fresh run each time is what keeps it from being read as the fixture base
    (a retried probe — resume after a crash, an unreliable rerun — must never materialise
    into the last attempt's leftovers)."""
    probe_dir = Path(probe_dir)
    n = sum(1 for entry in probe_dir.iterdir() if entry.is_dir()) if probe_dir.is_dir() else 0
    return probe_dir / str(n + 1)


def apply_reference(probe: Probe, root) -> None:
    """The reference solution over a materialised root: ``reference/`` overlaid, then the
    fixture's ``reference.sh`` at the root when it ships one (a reference that must end in a
    commit, as completeness-tail's does, cannot be an overlay alone). The suite's proof that a
    checker passes on the reference, and the build tool's, both go through here."""
    root = Path(root)
    reference = probe.fixture_dir / "reference"
    if reference.is_dir():
        shutil.copytree(reference, root, dirs_exist_ok=True)
    script = probe.fixture_dir / "reference.sh"
    if script.is_file():
        subprocess.run(["bash", str(script)], cwd=str(root), check=True)


def check(probe: Probe, root, env_extra: dict | None = None) -> tuple[bool, str | None]:
    """Run the manifest's one shell command at the probe's root; exit 0 is pass. The checker
    sees ``LOCALAGENTS_PROBE_DIR`` (its fixture) and ``LOCALAGENTS_PROBE_BASE`` (the revision
    the job started from). The failure reason is the first non-empty output line, ``no_answer``
    when the checker printed that, cut to 200 characters."""
    env = dict(os.environ)
    env["LOCALAGENTS_PROBE_DIR"] = str(probe.fixture_dir)
    base = _probe_base(root)
    if base is not None:
        env["LOCALAGENTS_PROBE_BASE"] = base
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(["bash", "-c", probe.checker], cwd=str(root), env=env,
                          capture_output=True, text=True)
    if proc.returncode == 0:
        return True, None
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if any(line == "no_answer" for line in lines):
        return False, "no_answer"
    return False, (lines[0][:200] if lines else f"exit {proc.returncode}")


# --- the throughput probe (spec §7; §10 "run first, which needs the endpoint quiet") --------------


async def _completion(ep, served_id: str, max_tokens: int) -> tuple[int, float]:
    """One non-streaming completion on the endpoint's own ``/v1/messages`` route, with the
    generated tokens and the wall time it took. ``localagents.server``'s ``local_complete`` is
    the completion path this mirrors."""
    body = {"model": served_id, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": THROUGHPUT_PROMPT}]}
    headers = {**ep.auth_headers(), "anthropic-version": "2023-06-01"}
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=600.0) as client:
        response = await client.post(f"{ep.url}/v1/messages", json=body, headers=headers)
    elapsed = time.monotonic() - t0
    response.raise_for_status()
    data = response.json()
    tokens = int((data.get("usage") or {}).get("output_tokens") or 0)
    return tokens, elapsed


async def throughput_probe(reg, ep, served_id: str, conn) -> tuple[capacity.Curve, float]:
    """This box's throughput curve and its single-session tokens/s. Refuses an endpoint that is
    serving anything (``EndpointBusy``). One short completion runs first and is discarded; the
    single figure is the completion after it, so the baseline both ratios divide by is a warm
    box's own rate. Point 2 is the median of the two concurrent completions' own tokens/s over
    the single's: one session's share of the batched run, read per session (the batch as a whole
    says nothing about what a routed session gets). The point is recorded as measured and never
    clamped: above 1.0 is the box's own signal, and the estimate line carries it onward. The
    curve is saved as ``probed`` with n = 2."""
    live = await reg.probe(ep)
    load = live.get("load") or {}
    slots = live.get("slots") or {}
    if int(load.get("requests_running") or 0) > 0 or int(slots.get("busy") or 0) > 0:
        raise EndpointBusy(f"endpoint {ep.name!r} is serving requests; run when quiet")
    await _completion(ep, served_id, THROUGHPUT_WARMUP_TOKENS)  # warms the box; never measured
    single_tokens, single_s = await _completion(ep, served_id, THROUGHPUT_MAX_TOKENS)
    if single_tokens <= 0 or single_s <= 0:
        raise EndpointBusy(f"endpoint {ep.name!r} returned no measurable completion")
    tps_single = single_tokens / single_s
    pair = await asyncio.gather(_completion(ep, served_id, THROUGHPUT_MAX_TOKENS),
                                _completion(ep, served_id, THROUGHPUT_MAX_TOKENS))
    shares = [tokens / seconds for tokens, seconds in pair if tokens > 0 and seconds > 0]
    ratio = statistics.median(shares) / tps_single if shares else 1.0
    curve = capacity.Curve({1: 1.0, 2: ratio}, "probed", 2)
    endpoint_id = db.lookup_id(conn, "endpoints", name=ep.name, base_url=ep.url,
                               backend=ep.backend, host=ep.host)
    # the schema's own kind list (partitioned | pooled | unknown), read from the live probe
    # exactly as the capacity model reads it
    kind = capacity.from_probe(ep, live, served_id, curve).kind
    capacity.save_capacity(conn, endpoint_id, kind, curve)
    return curve, tps_single


# --- one probe's job (the module seam the tests replace) ------------------------------------------


async def run_probe_job(reg, store, conn, ep, served_id, probe: Probe, run_dir, timeout_s: float,
                        levers: dict | None = None) -> ProbeOutcome:
    """Materialise the probe, start its calibration job, wait for it while sampling foreign
    load, then read the gate: foreign sessions make the reading ``unreliable``; a timeout or a
    turn cap is a fail with the spec's reason; otherwise the checker decides."""
    levers = levers or {}
    run_dir = Path(run_dir)
    attempt = _next_attempt_dir(run_dir / probe.id)
    root = materialize(probe, attempt)
    mcp_servers = None
    stub_log = None
    if "stubmcp" in probe.needs:
        stub_log = attempt / "stub.jsonl"
        mcp_servers = {"localagents": {
            "command": sys.executable,
            "args": ["-m", "localagents.router.stubmcp"],
            "env": {"LOCALAGENTS_STUB_LOG": str(stub_log)},
        }}
    try:
        job = await server.start_job(
            task=probe.brief_text(),
            model=served_id,
            endpoint=ep.name,
            cwd=str(root),
            isolation="none",
            permission_mode=None,
            max_turns=None,
            allowed_tools=None,
            system_prompt_append=None,
            timeout_s=timeout_s,
            priority="background",
            calibration=True,
            mcp_servers=mcp_servers,
            thinking=levers.get("thinking"),
            effort=levers.get("effort"),
        )
    except server.JobRefused as e:
        return ProbeOutcome("skipped", 0.0, str(e.reply.get("error") or e), None)

    max_running = 0
    wait_task = asyncio.create_task(store.wait(job.id, timeout_s + 60))
    while True:
        try:
            live = await reg.probe(ep)
            load = live.get("load") or {}
            max_running = max(max_running, int(load.get("requests_running") or 0))
        except Exception:  # a probe failure is a missing sample, never a failed job
            pass
        if wait_task.done():
            break
        done, _ = await asyncio.wait({wait_task}, timeout=FOREIGN_LOAD_SAMPLE_S)
        if done:
            break
    job = wait_task.result()
    seconds = 0.0
    if job.started_at is not None and job.finished_at is not None:
        seconds = float(job.finished_at - job.started_at)
    if max_running > 1:
        return ProbeOutcome("unreliable", seconds, "foreign load", job.id)
    if job.status == "timeout":
        return ProbeOutcome("fail", seconds, "timeout", job.id)
    cap = getattr(reg.defaults, "max_turns", None)
    if cap and job.num_turns >= cap:
        return ProbeOutcome("fail", seconds, "tail_capped", job.id)
    if probe.kind == "facts":
        return ProbeOutcome("pass", seconds, None, job.id)
    # the checker reads the same recording the stub wrote: without this the adherence half of a
    # stubmcp probe's checker sees no log and passes vacuously
    env_extra = {"LOCALAGENTS_STUB_LOG": str(stub_log)} if stub_log is not None else None
    ok, reason = check(probe, root, env_extra)
    if ok:
        return ProbeOutcome("pass", seconds, None, job.id)
    return ProbeOutcome("fail", seconds, reason or "checker failed", job.id)


# --- the facts probe (spec §6) --------------------------------------------------------------------


def _transcript_text(store, job_id: str | None) -> str:
    if not job_id:
        return ""
    try:
        return store.get(job_id).transcript_tail(0)
    except Exception:  # a pruned or foreign job has no transcript to judge
        return ""


def _thinking_chars(text: str) -> int:
    return sum(len(line[len(THINKING_MARK):]) for line in text.splitlines()
               if line.startswith(THINKING_MARK))


def _effort_verdict(texts: dict[str, str]) -> str:
    low = _thinking_chars(texts["effort_low"])
    high = _thinking_chars(texts["effort_high"])
    if low == 0 and high == 0:
        return "inert"  # no thinking on either side says nothing about effort
    return "honoured" if high >= EFFORT_RATIO * low else "inert"


async def _run_facts(reg, store, conn, ep, served_id, probe: Probe, run_dir, timeout_s: float,
                     row_id: int) -> ProbeOutcome:
    """Run the probe five ways (defaults, thinking on/off, effort low/high), read the five
    transcripts and write ``scorecard_rows.facts``. The probe passes when all five jobs
    finished without a timeout; a capped lever fails it with reason ``tail_capped`` (a capped
    reading is not a fact), but the facts probe is not a capability probe, so a cap here does
    not stop the tier (§10's cap rule is about capability ceilings)."""
    texts: dict[str, str] = {}
    outcomes: dict[str, ProbeOutcome] = {}
    for name, levers in FACTS_LEVERS:
        outcome = await run_probe_job(reg, store, conn, ep, served_id, probe,
                                      Path(run_dir) / name, timeout_s, levers=levers)
        outcomes[name] = outcome
        texts[name] = _transcript_text(store, outcome.job_id)
    facts = {
        "default_thinking": "on" if THINKING_MARK in texts["defaults"] else "off",
        "thinking_switch": ("honoured" if THINKING_MARK in texts["thinking_on"]
                            and THINKING_MARK not in texts["thinking_off"] else "inert"),
        "effort": _effort_verdict(texts),
    }
    with db.immediate(conn):
        conn.execute("UPDATE scorecard_rows SET facts = ? WHERE id = ?",
                     (json.dumps(facts), row_id))
    timed_out = any(o.status == "fail" and o.reason == "timeout" for o in outcomes.values())
    capped = any(o.status == "fail" and o.reason == "tail_capped" for o in outcomes.values())
    skipped = any(o.status == "skipped" for o in outcomes.values())
    seconds = sum(o.seconds for o in outcomes.values())
    job_id = outcomes["defaults"].job_id
    if timed_out:
        return ProbeOutcome("fail", seconds, "timeout", job_id)
    if capped:
        return ProbeOutcome("fail", seconds, "tail_capped", job_id)
    if skipped:
        return ProbeOutcome("fail", seconds, "skipped", job_id)
    return ProbeOutcome("pass", seconds, None, job_id)


# --- a run (spec §10 "Restartable", "Complete", "Time, honestly") ---------------------------------


def _row_for(conn, name: str, served_id: str, endpoint_id: int, model_id: int, reset: bool):
    """The live row for the pair; ``reset`` archives it by command and opens a fresh one rather
    than reviving it (``get_or_create_row`` revives an archived pair, which is the swap-back
    rule, not a reset)."""
    if not reset:
        return scorecard.get_or_create_row(conn, name, served_id, endpoint_id, model_id)
    live = conn.execute("SELECT id FROM scorecard_rows WHERE endpoint_name = ? AND served_id = ?"
                        " AND archived IS NULL", (name, served_id)).fetchone()
    if live is not None:
        scorecard.archive_row(conn, live["id"], "calibrate --reset")
    with db.immediate(conn):
        conn.execute("INSERT INTO scorecard_rows (endpoint_name, served_id, endpoint_id,"
                     " model_id, created) VALUES (?, ?, ?, ?, ?)",
                     (name, served_id, endpoint_id, model_id, time.time()))
    return scorecard.get_or_create_row(conn, name, served_id, endpoint_id, model_id)


def _open_run(conn, row_id: int, tier: str, probe_set: ProbeSet, plan_obj: Plan,
              estimate: float) -> tuple[str, float]:
    """The newest unfinished run for this row, tier and set version, or a new one. A resumed
    run keeps its ``started`` (the actual is its whole life, not this invocation)."""
    existing = conn.execute(
        "SELECT id, started FROM calibration_runs WHERE row_id = ? AND tier = ?"
        " AND set_version = ? AND status != 'complete'"
        " ORDER BY started DESC, rowid DESC LIMIT 1",
        (row_id, tier, probe_set.version)).fetchone()
    if existing is not None:
        return existing["id"], float(existing["started"])
    run_id = db.new_uuid()
    started = time.time()
    doc = {"probes": [probe.id for probe in plan_obj.probes],
           "skipped": [[probe.id, why] for probe, why in plan_obj.skipped]}
    with db.immediate(conn):
        conn.execute("INSERT INTO calibration_runs (id, row_id, tier, set_version, plan,"
                     " estimate_s, started, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'running')",
                     (run_id, row_id, tier, probe_set.version, json.dumps(doc), estimate, started))
    return run_id, started


def _write_result(conn, run_id: str, probe_id: str, outcome: ProbeOutcome) -> None:
    with db.immediate(conn):
        conn.execute("INSERT OR REPLACE INTO calibration_results"
                     " (run_id, probe_id, job_id, status, seconds, reason)"
                     " VALUES (?, ?, ?, ?, ?, ?)",
                     (run_id, probe_id, outcome.job_id, outcome.status, outcome.seconds,
                      outcome.reason))


def _complete_run(conn, run_id: str, plan_obj: Plan) -> bool:
    """Every planned probe has a status and at least two thirds are pass or fail. A tier that
    planned nothing is not complete: an empty set has measured nothing."""
    statuses = {r["probe_id"]: r["status"] for r in conn.execute(
        "SELECT probe_id, status FROM calibration_results WHERE run_id = ?", (run_id,))}
    for probe, _why in plan_obj.skipped:
        statuses[probe.id] = "skipped"
    ids = [probe.id for probe in plan_obj.probes] + [probe.id for probe, _ in plan_obj.skipped]
    if not ids:
        return False
    if any(pid not in statuses for pid in ids):
        return False
    decided = sum(1 for pid in ids if statuses[pid] in ("pass", "fail"))
    return decided >= math.ceil(2 * len(ids) / 3)


def _revised_estimate(conn, run_id: str, plan_obj: Plan, speed_factor: float) -> float | None:
    """After three results, the estimate again: the actuals so far plus the remaining nominal
    times scaled by the median ``actual / nominal`` seen in this run."""
    nominal = {probe.id: probe.nominal_s for probe in plan_obj.probes}
    elapsed = 0.0
    decided: set[str] = set()
    ratios: list[float] = []
    for r in conn.execute("SELECT probe_id, seconds FROM calibration_results WHERE run_id = ?",
                          (run_id,)).fetchall():
        decided.add(r["probe_id"])
        if r["seconds"] is None:
            continue
        elapsed += float(r["seconds"])
        if nominal.get(r["probe_id"]):
            ratios.append(float(r["seconds"]) / (nominal[r["probe_id"]] * speed_factor))
    if not ratios:
        return None
    remaining = sum(nominal[probe.id] * speed_factor for probe in plan_obj.probes
                    if probe.id not in decided)
    return elapsed + remaining * statistics.median(ratios)


async def run(reg, store, conn, endpoint: str, tier: str = "core", reset: bool = False,
              estimate_only: bool = False, keep_going: bool = False, set_path=None,
              report=print) -> str:
    """Run a tier of the set against one endpoint and return the run id (or the refusal's error
    name when nothing ran). ``estimate`` is the return when ``estimate_only`` stopped after the
    estimate; nothing is written in that case beyond the throughput curve the estimate needed."""
    ep = reg.endpoints.get(endpoint)
    if ep is None:
        report(f"unknown_endpoint: no endpoint named {endpoint!r}")
        return "unknown_endpoint"
    if ep.is_router:
        report(f"not_calibratable: endpoint {endpoint!r} is a router")
        return "not_calibratable"
    live = await reg.probe(ep)
    models = live.get("models") or []
    if not live.get("up") or not models:
        report(f"model_unavailable: endpoint {endpoint!r} is down or serves nothing")
        return "model_unavailable"
    served_id = str(models[0])
    endpoint_id = db.lookup_id(conn, "endpoints", name=ep.name, base_url=ep.url,
                               backend=ep.backend, host=ep.host)
    model_id = db.lookup_id(conn, "models", served_id=served_id)
    row = _row_for(conn, ep.name, served_id, endpoint_id, model_id, reset)

    probe_set = load_set(set_path)
    tools = present_tools(probe_set)
    plan_obj = plan(probe_set, tier, tools)

    curve = capacity.load_capacity(conn, endpoint_id)
    tps_single = None
    if curve is None:
        try:
            curve, tps_single = await throughput_probe(reg, ep, served_id, conn)
        except EndpointBusy as e:
            report(f"endpoint_busy: {e}")
            return "endpoint_busy"
    if row.speed_factor is not None:
        factor = float(row.speed_factor)
    elif probe_set.reference_tps is not None and tps_single:
        factor = float(probe_set.reference_tps) / tps_single
    else:
        factor = 1.0
    low, high = estimate_s(plan_obj, factor)
    line = (f"estimate: {len(plan_obj.probes)} probes, ~{low / 60:.1f}-{high / 60:.1f} min"
            f" (speed factor {factor:.2f}, margin 50%)")
    if curve is not None and float(curve.points.get(2, 0.0)) > 1.0:
        # the probe never clamps (the curve is the box's own record), but a point above 1.0 at
        # two sessions on a small box usually means the box was not quiet; say so once, here
        line += " (curve point above 1.0 at 2 sessions: check the box)"
    report(line)
    for skipped_probe, why in plan_obj.skipped:
        report(f"skip {skipped_probe.id}: {why}")
    if estimate_only:
        return "estimate"

    run_id, run_started = _open_run(conn, row.id, tier, probe_set, plan_obj, low)
    prior = {r["probe_id"]: r["status"] for r in conn.execute(
        "SELECT probe_id, status FROM calibration_results WHERE run_id = ?", (run_id,))}
    run_dir = state_dir() / "calibration" / run_id
    results_this_run = 0
    stopped = False
    for probe in plan_obj.probes:
        if prior.get(probe.id) in ("pass", "fail"):
            continue
        timeout_s = TIMEOUT_MULTIPLIER * probe.nominal_s * factor
        if probe.kind == "facts":
            outcome = await _run_facts(reg, store, conn, ep, served_id, probe, run_dir,
                                       timeout_s, row.id)
        else:
            outcome = await run_probe_job(reg, store, conn, ep, served_id, probe, run_dir,
                                          timeout_s)
        _write_result(conn, run_id, probe.id, outcome)
        results_this_run += 1
        detail = f"; {outcome.reason}" if outcome.reason else ""
        report(f"{probe.id}: {outcome.status} ({outcome.seconds:.1f}s{detail})")
        if (outcome.status == "fail" and outcome.reason == "tail_capped"
                and probe.kind != "facts"):
            stopped = True
            report("stopping: a capped probe stops the tier")
            break
        if not keep_going and results_this_run == 3:
            revised = _revised_estimate(conn, run_id, plan_obj, factor)
            if revised is not None and revised > 2.0 * low:
                stopped = True
                report(f"estimate revised: ~{revised / 60:.1f} min; stopping"
                       " (twice the original)")
                break
    if stopped:
        status = "stopped"
    elif _complete_run(conn, run_id, plan_obj):
        status = "complete"
    else:
        status = "incomplete"
    finished = time.time()
    with db.immediate(conn):
        conn.execute("UPDATE calibration_runs SET status = ?, actual_s = ?, finished = ?"
                     " WHERE id = ?", (status, finished - run_started, finished, run_id))
    if status == "complete":
        with db.immediate(conn):
            conn.execute("UPDATE scorecard_rows SET set_version = ? WHERE id = ?",
                         (probe_set.version, row.id))
        scorecard.run_pass(conn, row.id, probes=probe_set.labels())
        fresh = conn.execute("SELECT speed_factor FROM scorecard_rows WHERE id = ?",
                             (row.id,)).fetchone()
        speed = fresh["speed_factor"] if fresh is not None else None
        report(f"complete: speed factor {speed:.2f}" if speed is not None
               else "complete: speed factor unmeasured")
    report(f"run {run_id}: {status}")
    return run_id


# --- the CLI (spec §10: started from the CLI the way `localagents monitor` is) --------------------


def _recompute(conn, endpoint: str) -> None:
    """``--recompute``: a pass for every live row of the endpoint with the installed set's
    labels, then each cell as ``dimension[/language]: estimate (count, thin?)``."""
    probe_set = load_set()
    labels = probe_set.labels()
    for row in scorecard.rows(conn):
        if row.endpoint_name != endpoint:
            continue
        scorecard.run_pass(conn, row.id, probes=labels)
        for cell in scorecard.cells(conn, row.id):
            dimension = cell.dimension + (f"/{cell.language}" if cell.language else "")
            estimate = "none" if cell.estimate is None else f"{cell.estimate:.2f}"
            thin = ", thin" if cell.thin else ""
            print(f"{dimension}: {estimate} ({cell.count:g}{thin})")


async def _go(reg, conn, args):
    """Start the in-process store and shim the way ``server.main`` does, then run."""
    await server.shim.start()
    server.store.describe_server(config=str(reg.path) if reg.path else None,
                                 transport="calibrate")
    server._ensure_sampler()
    return await run(reg, server.store, conn, args.endpoint, tier=args.tier, reset=args.reset,
                     estimate_only=args.estimate, keep_going=args.keep_going,
                     set_path=args.set_path)


def main(argv=None) -> None:
    import argparse

    ap = argparse.ArgumentParser(
        prog="localagents calibrate",
        description="run a tier of the calibration set against one endpoint",
    )
    ap.add_argument("endpoint", help="the endpoint name from models.yaml")
    ap.add_argument("--tier", default="core", choices=["core", "medium", "large"])
    ap.add_argument("--reset", action="store_true", help="archive the row and start fresh")
    ap.add_argument("--estimate", action="store_true", help="print the estimate and stop")
    ap.add_argument("--recompute", action="store_true",
                    help="run a scorecard pass for the endpoint's rows and exit")
    ap.add_argument("--keep-going", action="store_true",
                    help="do not stop when the revised estimate exceeds twice the original")
    ap.add_argument("--set", dest="set_path", default=None, help="the manifest path")
    ap.add_argument("--config", default=None, help="path to models.yaml (or LOCALAGENTS_CONFIG)")
    a = ap.parse_args(argv)
    if a.config:
        os.environ["LOCALAGENTS_CONFIG"] = str(Path(a.config).expanduser().resolve())
    try:
        reg = Registry.load(Path(a.config).expanduser() if a.config else None)
    except Exception as e:  # a config file is operator input
        print(f"localagents calibrate: cannot read the config: {e}", file=sys.stderr)
        raise SystemExit(2)
    ep = reg.endpoints.get(a.endpoint)
    if ep is None:
        print(f"localagents calibrate: unknown endpoint {a.endpoint!r}", file=sys.stderr)
        raise SystemExit(2)
    if ep.is_router:
        print(f"localagents calibrate: endpoint {a.endpoint!r} is a router; not calibratable",
              file=sys.stderr)
        raise SystemExit(2)
    conn = db.open_db()
    try:
        if a.recompute:
            _recompute(conn, a.endpoint)
            return
        result = asyncio.run(_go(reg, conn, a))
        if result == "estimate":
            return
        if result in ("unknown_endpoint", "not_calibratable"):
            raise SystemExit(2)
        if result in ("model_unavailable", "endpoint_busy"):
            raise SystemExit(1)
        row = conn.execute("SELECT status FROM calibration_runs WHERE id = ?",
                           (result,)).fetchone()
        raise SystemExit(0 if row is not None and row["status"] == "complete" else 1)
    finally:
        conn.close()
