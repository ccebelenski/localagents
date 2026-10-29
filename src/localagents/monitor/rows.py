"""One probe result becomes rows: a ``probes`` row, plus ``probe_counters`` and ``probe_hardware``
rows when the backend reported them (monitor spec §3 "Storage", schema version 4).

Built from what ``Registry.probe`` returns and the raw ``/metrics`` document
(``registry.fetch_metrics_doc``). Nothing derived is stored: rates come from consecutive
counters rows at read time (``rates.py``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ..registry import STRATA_HARDWARE_KEYS, parse_metrics, parse_strata_extra, parse_strata_metrics

# Column lists in the spec's order; the insert statements are built from these, never from a
# caller's keys.
PROBE_COLUMNS = (
    "t", "endpoint_id", "model_id", "up", "host", "error", "latency_ms", "context",
    "slots_total", "slots_busy", "requests_running", "requests_waiting", "kv_cache_usage",
    "gen_tps", "since", "state",
)
COUNTER_COLUMNS = (
    "prompt_tokens", "prompt_tokens_cached", "prompt_seconds", "generated_tokens", "generated_seconds",
    "spec_draft_tokens", "spec_accepted_tokens", "prefix_queries", "prefix_hits", "preemptions",
)
HARDWARE_COLUMNS = STRATA_HARDWARE_KEYS


@dataclass
class ProbeRows:
    probe: dict[str, Any]  # the probes columns (no id)
    counters: dict[str, float | None] | None  # the probe_counters columns (no probe_id); None: no row
    hardware: dict[str, float | None] | None  # the probe_hardware columns (no probe_id); None: no row


def parse_doc(doc: tuple[str, Any] | None) -> tuple[dict[str, float], dict[str, Any]]:
    """``fetch_metrics_doc``'s result -> (short-name counters and gauges, strata extras)."""
    if doc is None:
        return {}, {}
    kind, body = doc
    if kind == "strata":
        return parse_strata_metrics(body), parse_strata_extra(body)
    return parse_metrics(body), {}


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _context(probe: dict[str, Any]) -> int | None:
    """The served id's window; on a multi-model endpoint the first served id's, else the first
    window the prober found."""
    ctx = probe.get("context")
    if not isinstance(ctx, dict) or not ctx:
        return None
    models = probe.get("models") or []
    if models and models[0] in ctx:
        return _int(ctx[models[0]])
    return _int(next(iter(ctx.values())))


def probe_to_rows(
    endpoint_id: int,
    model_id: int | None,
    probe: dict[str, Any],
    doc: tuple[str, Any] | None,
    t: float,
) -> ProbeRows:
    """The rows for one probe. A down probe is a row with its host class and failure sentence and
    nothing else (a document passed with it is ignored: a failed probe has no counters). Gauges
    come from the metrics document when it carries them — the parsed counters/gauges, then the
    document's extras; the probe dict's rounded copy (``load``) only when the document does not.
    ``/slots`` occupancy has no document form and always comes from the probe dict."""
    up = bool(probe.get("up"))
    row: dict[str, Any] = dict.fromkeys(PROBE_COLUMNS)
    row.update(t=t, endpoint_id=endpoint_id, model_id=model_id, up=1 if up else 0,
               host=probe.get("host"), error=None if up else probe.get("error"))
    if not up:
        return ProbeRows(row, None, None)
    parsed, extra = parse_doc(doc)
    load = probe.get("load") if isinstance(probe.get("load"), dict) else {}
    slots = probe.get("slots") if isinstance(probe.get("slots"), dict) else {}

    def gauge(key: str) -> Any:
        for source in (parsed, extra, load):
            if source.get(key) is not None:
                return source[key]
        return None

    kv = gauge("kv_cache_usage")
    row.update(
        latency_ms=_int(probe.get("latency_ms")),
        context=_context(probe),
        slots_total=_int(slots.get("total")),
        slots_busy=_int(slots.get("busy")),
        requests_running=_int(gauge("requests_running")),
        requests_waiting=_int(gauge("requests_waiting")),
        kv_cache_usage=float(kv) if isinstance(kv, (int, float)) and not isinstance(kv, bool) else None,
        gen_tps=extra.get("gen_tps"),
        since=extra.get("since"),
    )
    # ruling A (review of 3.1): probes.state is Strata's live.state verbatim; other backends
    # "busy" when requests_running or slots_busy is positive else "idle". A down probe returned
    # earlier, leaving it NULL.
    st = extra.get("state")
    if not isinstance(st, str):
        st = "busy" if (row["requests_running"] or 0) > 0 or (row["slots_busy"] or 0) > 0 else "idle"
    row["state"] = st
    counters = None
    if any(k in parsed for k in COUNTER_COLUMNS):
        counters = {k: (float(parsed[k]) if k in parsed else None) for k in COUNTER_COLUMNS}
    return ProbeRows(row, counters, extra.get("hardware"))


def _insert(conn: sqlite3.Connection, table: str, columns: tuple[str, ...], values: dict[str, Any]) -> int:
    unknown = set(values) - set(columns)
    if unknown:
        raise ValueError(f"{table} has no column(s) {', '.join(sorted(unknown))}")
    cur = conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        [values.get(c) for c in columns],
    )
    return cur.lastrowid


def insert(conn: sqlite3.Connection, rows: ProbeRows) -> int:
    """Insert the rows with no transaction of its own: the caller holds ``immediate()`` (the
    collector's ``tick``), so the probes row, its counters and hardware rows and the tick's
    events commit or roll back together (story 2.2a ruling E)."""
    probe_id = _insert(conn, "probes", PROBE_COLUMNS, rows.probe)
    if rows.counters is not None:
        _insert(conn, "probe_counters", ("probe_id",) + COUNTER_COLUMNS, {"probe_id": probe_id, **rows.counters})
    if rows.hardware is not None:
        _insert(conn, "probe_hardware", ("probe_id",) + HARDWARE_COLUMNS, {"probe_id": probe_id, **rows.hardware})
    return probe_id
