"""Scorecard rows and identity, evidence reads, the pass (router-design spec §8, §10
"Complete"/"Ends with a pass"; constants from calibration-set spec §7).

The evidence log is primary and cells are derived: nothing here writes a cell when a job
finishes — jobs write evidence (a ticket, a verdict, gate results, a probe result) and a
**pass** reads the log for one row and recomputes every cell as a new generation with its
constants recorded. ``run_pass`` is the only writer of ``scorecard_cells``.

Row identity is endpoint name × served id (§8): a base URL, backend or host change keeps
the row, a served id change archives it, and the pair's return revives the archived row
with its history.

Wire shapes this module reads that a later phase writes (pinned):

- ``router_tickets.scores`` JSON: ``{"values": {dim: band or option}, "confidences":
  {dim: float}, "undetermined": [dim, ...], "truncated": bool, "defs_version": int}``.
- ``verdicts.accepted`` 0/1; ``verdicts.dimensions`` JSON ``{dim: -1|0|1}`` or NULL, where
  -1 forces an observation rejected and 1 forces it accepted over the job-level verdict;
  ``verdicts.at``.
- ``jobs.gates`` JSON ``{"tests", "stop_rule", "not_capped", "no_tmp", "files_within",
  "lint"}``, each true (clean), false or null (not evaluable), plus ``"files_n": int|null``.
- probe labels arrive with the pass as ``probes``: ``{probe_id: {"labels": {axis: int,
  ..., "language": str}, "nominal_s": float}}``; a pass/fail result whose probe_id is
  absent contributes nothing and is counted in the pass constants as ``unlabelled_probes``.

The capability rule is interval censoring: an accept at demand b says capability ≥ b, so
the cell is the highest band b with FOR + AGAINST > 0 and FOR / (FOR + AGAINST) ≥
ACCEPT_RATE, where FOR is the recency-weighted weight of accepted observations at bands
≥ b and AGAINST that of rejected observations at bands ≤ b; no estimate below MIN_COUNT
observations. An accept at 3 therefore also supports 2, 1 and 0. A cell with enough evidence
but no band that qualifies is 0.0, fail-closed: the row failed what it was given."""

import json
import statistics
import time
from dataclasses import dataclass, replace

from localagents import db
from localagents.router import definitions

CAPABILITY_CELLS = ("software_dev", "tool_use", "computer_use", "complexity", "judgement")
RATE_CELLS = ("completeness", "adherence")
ACCEPT_RATE = 0.6
MIN_COUNT = 3.0
THIN_BELOW = 5.0
PROBE_WEIGHT = 1.0
JOB_WEIGHT = 0.5
RECENCY_HALFLIFE_OBS = 40.0
PASS_PENDING_N = 20

_CONSTANT_NAMES = ("ACCEPT_RATE", "MIN_COUNT", "THIN_BELOW", "PROBE_WEIGHT", "JOB_WEIGHT",
                   "RECENCY_HALFLIFE_OBS", "PASS_PENDING_N")
# completeness reads the did-the-work gates, adherence the did-it-cleanly gates
_RATE_GROUPS = {"completeness": ("tests", "not_capped", "stop_rule"),
                "adherence": ("no_tmp", "files_within", "lint")}


@dataclass
class Row:
    id: int
    endpoint_name: str
    served_id: str
    endpoint_id: int
    model_id: int
    set_version: str | None
    band_times: dict[int, float]
    speed_factor: float | None
    archived: float | None
    facts: dict | None


@dataclass
class Cell:
    dimension: str
    language: str
    estimate: float | None
    count: float
    probes_n: int
    jobs_n: int
    thin: bool
    pass_id: str
    defs_version: int


@dataclass
class Observation:
    dimension: str
    language: str
    band: int
    accepted: bool
    weight: float
    at: float
    source: str  # 'probe' | 'job'


# --- rows (§8, row identity) --------------------------------------------------------------------

_ROW_COLUMNS = ("id, endpoint_name, served_id, endpoint_id, model_id, set_version,"
                " band_times, speed_factor, archived, archive_reason, facts")


def _row_from(r) -> Row:
    return Row(id=r["id"], endpoint_name=r["endpoint_name"], served_id=r["served_id"],
               endpoint_id=r["endpoint_id"], model_id=r["model_id"],
               set_version=r["set_version"],
               band_times={int(b): float(s) for b, s in json.loads(r["band_times"] or "{}").items()},
               speed_factor=r["speed_factor"], archived=r["archived"],
               facts=json.loads(r["facts"]) if r["facts"] is not None else None)


def _fetch_row(conn, row_id) -> Row:
    r = conn.execute(f"SELECT {_ROW_COLUMNS} FROM scorecard_rows WHERE id = ?",
                     (row_id,)).fetchone()
    if r is None:
        raise ValueError(f"no scorecard row {row_id}")
    return _row_from(r)


def get_or_create_row(conn, endpoint_name, served_id, endpoint_id, model_id) -> Row:
    """The live row for the pair; else its newest archived row, revived with its history
    (a swap back costs nothing, §8); else a new row. A return revives whatever ids the
    archived row carried: identity is the name × served id, not the endpoint row."""
    with db.immediate(conn):
        r = conn.execute(f"SELECT {_ROW_COLUMNS} FROM scorecard_rows WHERE endpoint_name = ?"
                         " AND served_id = ? AND archived IS NULL",
                         (endpoint_name, served_id)).fetchone()
        if r is not None:
            return _row_from(r)
        r = conn.execute(f"SELECT {_ROW_COLUMNS} FROM scorecard_rows WHERE endpoint_name = ?"
                         " AND served_id = ? ORDER BY archived DESC, id DESC LIMIT 1",
                         (endpoint_name, served_id)).fetchone()
        if r is not None:
            conn.execute("UPDATE scorecard_rows SET archived = NULL, archive_reason = NULL"
                         " WHERE id = ?", (r["id"],))
            r = conn.execute(f"SELECT {_ROW_COLUMNS} FROM scorecard_rows WHERE id = ?",
                             (r["id"],)).fetchone()
            return _row_from(r)
        conn.execute("INSERT INTO scorecard_rows (endpoint_name, served_id, endpoint_id,"
                     " model_id, created) VALUES (?, ?, ?, ?, ?)",
                     (endpoint_name, served_id, endpoint_id, model_id, time.time()))
        new = conn.execute("SELECT id FROM scorecard_rows WHERE endpoint_name = ?"
                           " AND served_id = ? AND archived IS NULL",
                           (endpoint_name, served_id)).fetchone()
        return _fetch_row(conn, new["id"])


def rows(conn, include_archived=False) -> list[Row]:
    sql = f"SELECT {_ROW_COLUMNS} FROM scorecard_rows"
    if not include_archived:
        sql += " WHERE archived IS NULL"
    return [_row_from(r) for r in conn.execute(sql + " ORDER BY id").fetchall()]


def archive_row(conn, row_id, reason):
    """Archive one live row (`calibrate --reset`); an already archived row keeps its moment."""
    with db.immediate(conn):
        conn.execute("UPDATE scorecard_rows SET archived = ?, archive_reason = ?"
                     " WHERE id = ? AND archived IS NULL", (time.time(), reason, row_id))


def archive_stale_rows(conn, endpoint_name, served_ids_now, reason) -> int:
    """Archive the live rows of this endpoint name whose served id is gone; siblings of a
    multi-model endpoint that still serve stay. Returns the number archived."""
    with db.immediate(conn):
        if served_ids_now:
            marks = ",".join("?" * len(served_ids_now))
            stale = conn.execute(
                f"SELECT id FROM scorecard_rows WHERE endpoint_name = ? AND archived IS NULL"
                f" AND served_id NOT IN ({marks})", (endpoint_name, *served_ids_now)).fetchall()
        else:
            stale = conn.execute("SELECT id FROM scorecard_rows WHERE endpoint_name = ?"
                                 " AND archived IS NULL", (endpoint_name,)).fetchall()
        now = time.time()
        for r in stale:
            conn.execute("UPDATE scorecard_rows SET archived = ?, archive_reason = ? WHERE id = ?",
                         (now, reason, r["id"]))
        return len(stale)


def core_complete(conn, row_id) -> bool:
    """A completed core-tier calibration run for this row (§6: nothing routes to a row
    whose core run is not complete)."""
    return conn.execute("SELECT 1 FROM calibration_runs WHERE row_id = ? AND tier = 'core'"
                        " AND status = 'complete' LIMIT 1", (row_id,)).fetchone() is not None


# --- evidence reads (§8: the log is primary) ------------------------------------------------------

def _row_jobs(conn, row):
    """The row's jobs by identity: endpoint name × served id (a base URL change keeps them)."""
    return conn.execute(
        "SELECT j.id, j.gates, j.started_at, j.finished_at, j.calibration"
        " FROM jobs j JOIN endpoints e ON e.id = j.endpoint_id"
        " JOIN models m ON m.id = j.model_id"
        " WHERE e.name = ? AND m.served_id = ?", (row.endpoint_name, row.served_id)).fetchall()


def _ticketed_jobs(conn, row):
    """The row's finished jobs that carry a ticket, for the band-time medians."""
    return conn.execute(
        "SELECT j.id, j.started_at, j.finished_at, t.scores"
        " FROM jobs j JOIN router_tickets t ON t.job_id = j.id"
        " JOIN endpoints e ON e.id = j.endpoint_id"
        " JOIN models m ON m.id = j.model_id"
        " WHERE e.name = ? AND m.served_id = ?"
        " AND j.started_at IS NOT NULL AND j.finished_at IS NOT NULL",
        (row.endpoint_name, row.served_id)).fetchall()


def _probe_results(conn, row_id):
    return conn.execute(
        "SELECT r.probe_id, r.status, r.job_id, r.seconds, ru.started AS run_started,"
        " j.finished_at AS job_finished"
        " FROM calibration_results r JOIN calibration_runs ru ON ru.id = r.run_id"
        " LEFT JOIN jobs j ON j.id = r.job_id"
        " WHERE ru.row_id = ? ORDER BY r.rowid", (row_id,)).fetchall()


def _labels_for(probes, probe_id) -> dict:
    return ((probes or {}).get(probe_id) or {}).get("labels") or {}


_UNREADABLE = object()


def _decode(text):
    """The JSON an evidence row carries, or ``_UNREADABLE``. A row whose JSON cannot be read
    is skipped by the readers and counted by the pass: one corrupt row must not stop every
    future pass (it never becomes readable, but the rest of the log still speaks)."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return _UNREADABLE


def _note_unreadable(errors, key):
    if errors is not None:
        errors.add(key)


def observations(conn, row_id, probes=None, errors=None) -> list[Observation]:
    """Every observation the row's log holds, from the three evidence sources. Labels come
    with the caller (the pass carries the set's mapping); a probe whose id is not in
    ``probes`` contributes nothing. A software_dev observation is emitted twice — at its
    language and at the aggregate ``''`` — so one accept feeds both cells. An unreadable
    evidence row is skipped; its key is added to ``errors`` when the caller keeps one."""
    row = _fetch_row(conn, row_id)
    obs: list[Observation] = []

    for pr in _probe_results(conn, row_id):
        if pr["status"] not in ("pass", "fail"):
            continue  # skipped and unreliable rows contribute nothing
        labels = _labels_for(probes, pr["probe_id"])
        if not labels:
            continue
        accepted = pr["status"] == "pass"
        at = pr["job_finished"] if pr["job_finished"] is not None else pr["run_started"]
        for axis in CAPABILITY_CELLS:
            if axis not in labels:
                continue
            band = int(labels[axis])
            lang = ""
            if axis == "software_dev":
                lv = labels.get("language")
                lang = lv if lv and lv != "none" else ""   # 'none' is the aggregate's label too
            obs.append(Observation(axis, lang, band, accepted, PROBE_WEIGHT, at, "probe"))
            if lang:
                obs.append(Observation(axis, "", band, accepted, PROBE_WEIGHT, at, "probe"))

    for v in conn.execute(
            "SELECT v.job_id, v.accepted, v.dimensions, v.at, t.scores"
            " FROM verdicts v JOIN router_tickets t ON t.job_id = v.job_id"
            " JOIN jobs j ON j.id = v.job_id"
            " JOIN endpoints e ON e.id = j.endpoint_id"
            " JOIN models m ON m.id = j.model_id"
            " WHERE e.name = ? AND m.served_id = ?", (row.endpoint_name, row.served_id)):
        scores = _decode(v["scores"])
        if not isinstance(scores, dict):
            _note_unreadable(errors, ("ticket", v["job_id"]))
            continue
        values = scores.get("values") or {}
        undetermined = set(scores.get("undetermined") or ())
        dims = {}
        if v["dimensions"] is not None:
            dims = _decode(v["dimensions"])
            if dims is _UNREADABLE:
                _note_unreadable(errors, ("verdict", v["job_id"]))
                continue
            if not isinstance(dims, dict):
                dims = {}   # the JSON string "null": no per-dimension detail, verdict still reads
        for dim in CAPABILITY_CELLS:
            if dim not in values or dim in undetermined:
                continue  # an undetermined axis contributes no observation (judgement never is)
            band = int(values[dim])
            flip = dims.get(dim, 0)
            accepted = bool(v["accepted"]) if flip == 0 else flip == 1
            lang = ""
            if dim == "software_dev":
                lv = values.get("language")
                lang = lv if lv and lv != "none" else ""
            obs.append(Observation(dim, lang, band, accepted, JOB_WEIGHT, v["at"], "job"))
            if lang:
                obs.append(Observation(dim, "", band, accepted, JOB_WEIGHT, v["at"], "job"))

    for j in _row_jobs(conn, row):
        if j["gates"] is None or j["finished_at"] is None:
            continue  # a gates row with no finish time cannot be placed on the recency curve
        gates = _decode(j["gates"])
        if not isinstance(gates, dict):
            _note_unreadable(errors, ("gates", j["id"]))
            continue
        for dim, group in _RATE_GROUPS.items():
            evaluable = [g for g in group if gates.get(g) is not None]
            if not evaluable:
                continue
            accepted = all(gates[g] is True for g in evaluable)
            if j["calibration"]:
                obs.append(Observation(dim, "", 0, accepted, PROBE_WEIGHT, j["finished_at"], "probe"))
            else:
                obs.append(Observation(dim, "", 0, accepted, JOB_WEIGHT, j["finished_at"], "job"))
    return obs


# --- the cell kinds (§8) --------------------------------------------------------------------------

def _weighted(obs, halflife) -> list[tuple[Observation, float]]:
    """Newest first, the k-th reading's weight scaled by the recency curve (a weight, not
    a cutoff). Ties keep their read order; the multiplier is by position, so they differ."""
    ordered = sorted(obs, key=lambda o: o.at, reverse=True)
    return [(o, o.weight * 0.5 ** (k / halflife)) for k, o in enumerate(ordered)]


def _capability_cell(weighted, accept_rate, min_count, thin_below):
    # count is evidence seen, so it sums the source weights; recency scales only the rate
    count = sum(o.weight for o, _ in weighted)
    if count < min_count:
        return None, count, count < thin_below
    for band in (4, 3, 2, 1, 0):
        for_w = sum(w for o, w in weighted if o.accepted and o.band >= band)
        against_w = sum(w for o, w in weighted if not o.accepted and o.band <= band)
        if for_w + against_w > 0 and for_w / (for_w + against_w) >= accept_rate:
            return float(band), count, count < thin_below
    return 0.0, count, count < thin_below  # enough evidence, no band qualifies: fail-closed


def _rate_cell(weighted, min_count, thin_below):
    count = sum(o.weight for o, _ in weighted)   # source weights, as the capability cells
    if count < min_count:
        return None, count, count < thin_below
    total_w = sum(w for _, w in weighted)        # the rate itself reads the recency curve
    clean = sum(w for o, w in weighted if o.accepted)
    return 4.0 * clean / total_w, count, count < thin_below


def _ceiling(conn, row, thin_below, errors=None):
    """The largest uncapped file count less any cap seen since (§8): a cap lowers the
    successes before it, an unknown cap lowers nothing, and a later uncapped success raises
    the ceiling again. Count is the raw jobs carrying files_n."""
    jobs, capped = [], []
    count = 0
    for j in _row_jobs(conn, row):
        if j["gates"] is None:
            continue
        gates = _decode(j["gates"])
        if not isinstance(gates, dict):
            _note_unreadable(errors, ("gates", j["id"]))
            continue
        files_n = gates.get("files_n")
        if files_n is None:
            continue
        count += 1
        if j["finished_at"] is None:
            continue  # no finish time: it cannot be a cap, and cannot be placed under one
        if gates.get("not_capped") is True:
            jobs.append((int(files_n), j["finished_at"]))
        elif gates.get("not_capped") is False and int(files_n) >= 1:
            # a cap that committed nothing (a timeout before any file) says at what count
            # the row capped no more than a NULL does: it constrains nothing
            capped.append((int(files_n), j["finished_at"]))
    if not jobs:
        return None, float(count), count < thin_below
    # each uncapped success speaks for the smallest cap seen since it; the largest of those
    # readings is the ceiling, so a later success raises what an earlier cap lowered. A cap
    # with a NULL or zero files_n never reaches the capped list and constrains nothing.
    estimates = [min([files_n, *(c for c, at in capped if at > finished)])
                 for files_n, finished in jobs]
    return float(max(estimates)), float(count), count < thin_below


# --- the pass (§8: derived cells, recorded constants) ----------------------------------------------

def run_pass(conn, row_id, constants=None, probes=None) -> str:
    """Recompute every cell of the row from its evidence log as a new generation, write
    the row's band times and speed factor, and return the pass id. ``constants`` overrides
    any module value for this pass and is recorded with it — that is the replay."""
    row = _fetch_row(conn, row_id)
    eff = {name: globals()[name] for name in _CONSTANT_NAMES}
    for key, value in (constants or {}).items():
        if key not in eff:
            raise ValueError(f"unknown pass constant {key!r} (one of {', '.join(_CONSTANT_NAMES)})")
        eff[key] = value
    # outside the pass transaction: the definitions seed opens a transaction of its own
    defs_version = definitions.load(conn).version
    errors: set = set()   # distinct evidence rows the readers had to skip
    obs = observations(conn, row_id, probes, errors)
    # observations stamps the module weights; the replay's overrides take them over by source
    obs = [replace(o, weight=eff["PROBE_WEIGHT"] if o.source == "probe" else eff["JOB_WEIGHT"])
           for o in obs]
    # a probe the caller names with EMPTY labels (the facts probe) is labelled-with-no-axes:
    # no observation and not unlabelled; only an id absent from ``probes`` is unlabelled
    unlabelled = sum(1 for pr in _probe_results(conn, row_id)
                     if pr["status"] in ("pass", "fail")
                     and pr["probe_id"] not in (probes or {}))

    languages = sorted({o.language for o in obs if o.dimension == "software_dev" and o.language})
    cell_keys = [(dim, "") for dim in CAPABILITY_CELLS]
    cell_keys += [("software_dev", lang) for lang in languages]
    cell_keys += [(dim, "") for dim in RATE_CELLS]

    cells: list[tuple] = []
    disagreement = 0.0
    for dimension, language in cell_keys:
        subset = [o for o in obs if o.dimension == dimension and o.language == language]
        weighted = _weighted(subset, eff["RECENCY_HALFLIFE_OBS"])
        if dimension in CAPABILITY_CELLS:
            estimate, count, thin = _capability_cell(weighted, eff["ACCEPT_RATE"],
                                                     eff["MIN_COUNT"], eff["THIN_BELOW"])
            # traffic against probes: the same rule, one source at a time (§8 notice margin)
            sides = {}
            for side, source in (("probe", "probe"), ("job", "job")):
                side_weighted = _weighted([o for o in subset if o.source == source],
                                          eff["RECENCY_HALFLIFE_OBS"])
                sides[side] = _capability_cell(side_weighted, eff["ACCEPT_RATE"],
                                               eff["MIN_COUNT"], eff["THIN_BELOW"])
            # compare counts, not estimates: a side with enough evidence always has one, and a
            # fail-closed 0.0 is evidence of disagreement, not an absence of it
            if (sides["probe"][1] >= eff["MIN_COUNT"]
                    and sides["job"][1] >= eff["MIN_COUNT"]):
                disagreement = max(disagreement,
                                   abs(sides["probe"][0] - sides["job"][0]) / 4.0)
        else:
            estimate, count, thin = _rate_cell(weighted, eff["MIN_COUNT"], eff["THIN_BELOW"])
        probes_n = sum(1 for o in subset if o.source == "probe")
        jobs_n = sum(1 for o in subset if o.source == "job")
        cells.append((dimension, language, estimate, count, probes_n, jobs_n, int(thin)))

    estimate, count, thin = _ceiling(conn, row, eff["THIN_BELOW"], errors)
    cells.append(("ceiling", "", estimate, count, 0, count, int(thin)))

    # a pass without the set's labels cannot attribute probe timings; it must not say they are
    # gone (the row keeps what the last pass that could see them wrote)
    wrote_timings = probes is not None
    if wrote_timings:
        band_times = _band_times(conn, row, probes, errors)
        speed_factor = _speed_factor(conn, row, probes)

    recorded = {name: eff[name] for name in _CONSTANT_NAMES}
    recorded["disagreement"] = disagreement
    recorded["unlabelled_probes"] = unlabelled
    recorded["unreadable"] = len(errors)

    pass_id = db.new_uuid()
    with db.immediate(conn):
        conn.execute("INSERT INTO scorecard_passes (id, row_id, at, constants, observations)"
                     " VALUES (?, ?, ?, ?, ?)",
                     (pass_id, row_id, time.time(), json.dumps(recorded), len(obs)))
        conn.executemany(
            "INSERT INTO scorecard_cells (pass_id, dimension, language, estimate, count,"
            " probes_n, jobs_n, thin, defs_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(pass_id, *c, defs_version) for c in cells])
        if wrote_timings:
            conn.execute("UPDATE scorecard_rows SET band_times = ?, speed_factor = ? WHERE id = ?",
                         (json.dumps({str(b): s for b, s in band_times.items()}),
                          speed_factor, row_id))
    return pass_id


def _band_times(conn, row, probes, errors=None) -> dict[int, float]:
    """Per complexity band the median of the kept probe seconds and the row's ticketed job
    durations (§5's time per complexity band). Probe timings read pass/fail only: an
    unreliable reading is a rerun's business, not the median's."""
    times: dict[int, list[float]] = {}
    for pr in _probe_results(conn, row.id):
        if pr["status"] not in ("pass", "fail") or pr["seconds"] is None:
            continue
        labels = _labels_for(probes, pr["probe_id"])
        if "complexity" in labels:
            times.setdefault(int(labels["complexity"]), []).append(float(pr["seconds"]))
    for j in _ticketed_jobs(conn, row):
        scores = _decode(j["scores"])
        if not isinstance(scores, dict):
            _note_unreadable(errors, ("ticket", j["id"]))
            continue
        values = scores.get("values") or {}
        if "complexity" in values and "complexity" not in set(scores.get("undetermined") or ()):
            times.setdefault(int(values["complexity"]), []).append(
                float(j["finished_at"] - j["started_at"]))
    return {band: float(statistics.median(secs)) for band, secs in sorted(times.items())}


def _speed_factor(conn, row, probes) -> float | None:
    """The median seconds / nominal over the row's passing probe results (§10: the estimate
    carries on this factor); None when the log holds none."""
    factors = []
    for pr in _probe_results(conn, row.id):
        if pr["status"] != "pass" or pr["seconds"] is None:
            continue
        entry = (probes or {}).get(pr["probe_id"]) or {}
        if not entry.get("labels"):
            continue  # a probe with no axes (the facts probe runs five jobs) is not a timing
        nominal = entry.get("nominal_s")
        if nominal:
            factors.append(pr["seconds"] / nominal)
    return float(statistics.median(factors)) if factors else None


# --- cell reads and notices ------------------------------------------------------------------------

def cells(conn, row_id, pass_id=None) -> list[Cell]:
    """The cells of ``pass_id``, or of the row's latest pass; empty before the first pass."""
    if pass_id is None:
        p = conn.execute("SELECT id FROM scorecard_passes WHERE row_id = ?"
                         " ORDER BY at DESC, rowid DESC LIMIT 1", (row_id,)).fetchone()
        if p is None:
            return []
        pass_id = p["id"]
    return [Cell(dimension=r["dimension"], language=r["language"], estimate=r["estimate"],
                 count=r["count"], probes_n=r["probes_n"], jobs_n=r["jobs_n"],
                 thin=bool(r["thin"]), pass_id=r["pass_id"], defs_version=r["defs_version"])
            for r in conn.execute("SELECT * FROM scorecard_cells WHERE pass_id = ?"
                                  " ORDER BY dimension, language", (pass_id,)).fetchall()]


def software_dev_for(cells, language) -> Cell | None:
    """The language's cell when it carries evidence, else the aggregate cell (``''``)."""
    if language:
        for c in cells:
            if c.dimension == "software_dev" and c.language == language and c.count > 0:
                return c
    for c in cells:
        if c.dimension == "software_dev" and c.language == "":
            return c
    return None


def _version_newer(installed, row_version) -> bool:
    """Compare dotted versions component by component: numerically when both are digits,
    as strings otherwise; a longer version with an equal prefix is the newer one."""
    left, right = str(installed).split("."), str(row_version).split(".")
    for a, b in zip(left, right):
        if a.isdigit() and b.isdigit():
            if int(a) != int(b):
                return int(a) > int(b)
        elif a != b:
            return a > b
    return len(left) > len(right)


def _evidence_items(conn, row, since) -> int:
    """The log's evidence items newer than ``since``: one verdict, one job carrying gates,
    one pass/fail probe result. An item is what the operator accumulated, not a per-axis
    observation — a verdict that determines five axes is still one thing to pass over."""
    n = 0
    for v in conn.execute(
            "SELECT v.at AS at FROM verdicts v"
            " JOIN jobs j ON j.id = v.job_id"
            " JOIN endpoints e ON e.id = j.endpoint_id"
            " JOIN models m ON m.id = j.model_id"
            " WHERE e.name = ? AND m.served_id = ?", (row.endpoint_name, row.served_id)):
        if v["at"] > since:
            n += 1
    for j in conn.execute(
            "SELECT COALESCE(j.finished_at, j.started_at) AS at FROM jobs j"
            " JOIN endpoints e ON e.id = j.endpoint_id"
            " JOIN models m ON m.id = j.model_id"
            " WHERE e.name = ? AND m.served_id = ? AND j.gates IS NOT NULL",
            (row.endpoint_name, row.served_id)):
        if j["at"] is not None and j["at"] > since:
            n += 1
    for r in conn.execute(
            "SELECT COALESCE(j.finished_at, ru.started) AS at"
            " FROM calibration_results r JOIN calibration_runs ru ON ru.id = r.run_id"
            " LEFT JOIN jobs j ON j.id = r.job_id"
            " WHERE ru.row_id = ? AND r.status IN ('pass', 'fail')", (row.id,)):
        if r["at"] is not None and r["at"] > since:
            n += 1
    return n


def notices(conn, row_id, set_version_installed) -> list[str]:
    """The row's two notices (§8, notices not triggers): a pass the log has outgrown, and a
    recalibration a pass or a set upgrade is asking for. Pending counts evidence items since
    the latest pass — verdicts, gated jobs and probe results — so it never needs labels,
    where the pass itself does."""
    row = _fetch_row(conn, row_id)
    latest = conn.execute("SELECT at, constants FROM scorecard_passes WHERE row_id = ?"
                          " ORDER BY at DESC, rowid DESC LIMIT 1", (row_id,)).fetchone()
    out = []
    since = latest["at"] if latest is not None else float("-inf")
    if _evidence_items(conn, row, since) >= PASS_PENDING_N:
        out.append("pass pending")
    recal = False
    if latest is not None:
        consts = _decode(latest["constants"])
        if isinstance(consts, dict) and (consts.get("disagreement") or 0) > 0.25:
            recal = True
    if (row.set_version is not None and set_version_installed is not None
            and _version_newer(set_version_installed, row.set_version)):
        recal = True
    if recal:
        out.append("needs recalibration")
    return out
