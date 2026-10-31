"""Endpoint registry.

The config lists **endpoints**: places that speak the Anthropic ``/v1/messages`` API
(llama.cpp ``llama-server``, vLLM, ...). Nothing about models lives in the config.
What each endpoint is *currently* serving, its context window and its occupancy are
discovered live via ``GET /v1/models`` (plus ``/props``, ``/slots`` or ``/metrics``)
on every call. The human brings models up and down by hand; Claude uses whatever is up.

The YAML file is re-read on every call, so edits take effect immediately.
"""

from __future__ import annotations

import asyncio
import errno
import fnmatch
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

# Below this per-request window a Claude Code session spends most of its turns compacting
# (~20k fixed prompt + summary + re-attached files). See README "Context windows".
MIN_USEFUL_CONTEXT = 128_000

DEFAULT_CONFIG_LOCATIONS = (
    Path.cwd() / "models.yaml",
    Path.home() / ".config" / "localagents" / "models.yaml",
)


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _squash(text: str) -> str:
    return _NON_ALNUM.sub("", text.lower())


class _Dumper(yaml.SafeDumper):
    """safe_dump that keeps multi-line strings readable (``|`` blocks) and never folds lines."""


def backend_kind(backend: str) -> str:
    """Normalise the config's ``backend`` string: llamacpp | vllm | strata | ollama | other."""
    b = backend.replace(".", "").replace("-", "").replace("_", "").lower()
    if b in ("llamacpp", "llama"):
        return "llamacpp"
    if b in ("vllm", "strata", "ollama", "other"):
        return b
    return "other"


def _str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_representer)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return data if isinstance(data, dict) else {}


def config_path() -> Path:
    env = os.environ.get("LOCALAGENTS_CONFIG")
    if env:
        return Path(env).expanduser()
    for p in DEFAULT_CONFIG_LOCATIONS:
        if p.exists():
            return p
    return DEFAULT_CONFIG_LOCATIONS[0]


def _chain_errno(exc: BaseException) -> int | None:
    """First ``errno`` found on ``exc`` or anything it wraps (``__cause__``, ``__context__``,
    exception-group members). httpx buries the kernel's OSError two ConnectErrors deep."""
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        n = getattr(e, "errno", None)
        if isinstance(n, int):
            return n
        stack.extend([e.__cause__, e.__context__, *getattr(e, "exceptions", ())])
    return None


def classify_connect_failure(exc: BaseException, url: str) -> dict[str, Any]:
    """Turn a failed probe into ``{"host": up|down|unknown, "error": <short sentence>,
    "hint": <one-line explanation>}`` — no ``hint`` on the unclassified fallback.

    Story 3.12e: the sentence is what the down card prints (the operator: "I don't need a
    paragraph and explanation line"), the explanation is the ``hint``, which spreads into
    ``list_models`` and ``ModelUnavailable`` but never into the database or the page.

    The kernel already distinguishes the two cases that matter, so no extra ping is needed:
    ECONNREFUSED means the box is up and nothing listens on that port (a wrong port in
    models.yaml); EHOSTUNREACH/ENETUNREACH mean the box is off or unroutable. A timeout
    *before* the connection exists (ConnectTimeout, PoolTimeout) is ambiguous (powered off,
    or a firewall dropping packets) and is reported as unknown; a timeout *after* it
    (ReadTimeout, WriteTimeout) proves the box answered — the engine is stalled or
    overloaded — and is reported as up (ruling 2026-10-29, review 2.3: the port-22 fallback
    used to re-prove this every tick of a stalled engine).
    """
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port
    hostport = f"{host}:{port}" if port else str(host)
    n = _chain_errno(exc)
    if n == errno.ECONNREFUSED:
        short = f"{host} reachable, port {port} no response" if port else f"{host} reachable, no response"
        return {"host": "up", "error": short, "hint": "nothing started there, or the port in models.yaml is wrong"}
    if n in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN):
        return {"host": "down", "error": f"{host} is unreachable ({errno.errorcode[n]})", "hint": "box off, or unroutable from here"}
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout)):
        return {"host": "up", "error": f"{hostport} connected, no reply in time", "hint": "the engine is stalled or overloaded"}
    if isinstance(exc, httpx.TimeoutException):
        return {"host": "unknown", "error": f"{hostport} timed out", "hint": "host off, or a firewall dropping packets"}
    return {"host": "unknown", "error": f"{type(exc).__name__}: {exc}"}


@dataclass
class Endpoint:
    name: str
    base_url: str
    backend: str = "other"  # llama.cpp | vllm | strata | ollama | other
    host: str = "local"
    notes: str = ""
    env: dict[str, str] = field(default_factory=dict)  # extra env for claude sessions
    api_key: str | None = None  # sent on every call to this endpoint; never handed to the session

    @property
    def url(self) -> str:
        return self.base_url.rstrip("/")

    def auth_headers(self) -> dict[str, str]:
        """Both header forms, because llama.cpp and vLLM check ``Authorization: Bearer`` while
        Anthropic-shaped servers check ``x-api-key``. Without a key the placeholder ``local`` goes
        out, which is what the delegated session itself sends."""
        key = self.api_key or "local"
        return {"x-api-key": key, "authorization": f"Bearer {key}"}


@dataclass
class Defaults:
    model: str | None = None  # optional preference: served id, glob or fuzzy name; else first live endpoint
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "WebFetch"]
    )
    disallowed_tools: list[str] = field(default_factory=lambda: ["Agent", "Task"])
    setting_sources: list[str] = field(default_factory=lambda: ["project", "local"])
    max_turns: int = 60
    timeout_s: int = 1800
    probe_timeout_s: float = 2.5
    system_prompt_append: str = ""
    dump_requests: bool = False
    log_events: bool = False  # write events.jsonl per job (debugging; env LOCALAGENTS_LOG_EVENTS=1 also enables)
    job_retention_days: int = 7  # delete job directories older than this; 0 disables


def local_path(path: Path) -> Path:
    """Sidecar for endpoints added via register_endpoint: ``models.local.yaml``.

    The hand-written file is never rewritten (comments and layout survive); the sidecar is
    merged on top of it at load, entry by entry.
    """
    return path.with_name(path.stem + ".local" + path.suffix)


@dataclass
class Registry:
    path: Path
    endpoints: dict[str, Endpoint]
    defaults: Defaults
    raw: dict[str, Any]  # hand-written file, read-only
    local: dict[str, Any]  # models.local.yaml, written by register_*

    # ---------- loading ----------
    @classmethod
    def load(cls, path: Path | None = None) -> "Registry":
        path = path or config_path()
        raw = _read_yaml(path)
        local = _read_yaml(local_path(path))
        merged_endpoints = {**(raw.get("endpoints") or {}), **(local.get("endpoints") or {})}
        endpoints = {name: Endpoint(name=name, **(cfg or {})) for name, cfg in merged_endpoints.items()}
        defaults = Defaults(**{**(raw.get("defaults") or {}), **(local.get("defaults") or {})})
        # A leftover ``models:`` section from older configs is ignored: served models are discovered.
        return cls(path=path, endpoints=endpoints, defaults=defaults, raw=raw, local=local)

    def save(self) -> None:
        """Persist ``self.local`` to the sidecar; ``models.yaml`` itself is left untouched."""
        lp = local_path(self.path)
        lp.parent.mkdir(parents=True, exist_ok=True)
        header = "# Written by localagents register_endpoint; merged over models.yaml.\n"
        lp.write_text(header + yaml.dump(self.local, Dumper=_Dumper, sort_keys=False, width=10_000, allow_unicode=True))

    # ---------- probing ----------
    async def probe(self, ep: Endpoint) -> dict[str, Any]:
        """Health + served model ids + context window (per served id where known).

        ``context`` is the backend's *real* per-request window: llama.cpp's per-slot ``n_ctx``
        (from ``/props``; with ``--parallel N`` and no unified KV that is ``-c`` divided by N) or
        vLLM's ``max_model_len``. It is handed to the Claude Code session as
        ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` so auto-compact fires before the backend overflows.
        """
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.defaults.probe_timeout_s, headers=ep.auth_headers()) as c:
                r = await c.get(f"{ep.url}/v1/models")
                r.raise_for_status()
                data = r.json()
                entries = [m for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
                ids = [m["id"] for m in entries]
                context, source, extra = await self._probe_context(c, ep, entries)
            out: dict[str, Any] = {"up": True, "host": "up", "models": ids, "latency_ms": int((time.monotonic() - t0) * 1000)}
            if context:
                out["context"] = context
                out["context_source"] = source
                small = {sid: n for sid, n in context.items() if n < MIN_USEFUL_CONTEXT}
                if small:
                    out["context_warning"] = (
                        f"window under {MIN_USEFUL_CONTEXT // 1000}k for {sorted(small)}: Claude Code will "
                        "auto-compact constantly; poor fit for run_agent (fine for local_complete)"
                    )
            out.update(extra)
            return out
        except Exception as e:  # noqa: BLE001
            return {"up": False, "models": [], **classify_connect_failure(e, ep.url)}

    @staticmethod
    async def _probe_context(
        c: httpx.AsyncClient, ep: Endpoint, entries: list[dict[str, Any]]
    ) -> tuple[dict[str, int], str, dict[str, Any]]:
        """Returns (context per served id, source, extra probe fields such as slot occupancy)."""
        # vLLM: /v1/models entries carry max_model_len
        ctx = {m["id"]: int(m["max_model_len"]) for m in entries if isinstance(m.get("max_model_len"), int)}
        if ctx:
            extra: dict[str, Any] = {}
            try:
                r = await c.get(f"{ep.url}/metrics")
                if r.status_code == 200:
                    mt = parse_metrics(r.text)
                    extra["metrics"] = True
                    # only the series the document actually has: an absent series stays
                    # absent, so rows' fallback (document, then this copy) leaves the column
                    # NULL — a 0.0 default would store a false "empty" (ruling 2026-10-29)
                    load = {}
                    for key in ("requests_running", "requests_waiting"):
                        if key in mt:
                            load[key] = int(mt[key])
                    if "kv_cache_usage" in mt:
                        load["kv_cache_usage"] = round(mt["kv_cache_usage"], 3)
                    extra["load"] = load
            except httpx.HTTPError:
                pass
            return ctx, "vllm:max_model_len", extra
        # llama.cpp: /props -> default_generation_settings.n_ctx (per slot); /slots -> occupancy
        # (only when started with --slots, advertised as endpoint_slots).
        # strata: same /props and /slots shapes, but /slots and /metrics are always on and not
        # advertised, and /metrics is JSON (see parse_strata_metrics) with a live queue depth.
        kind = backend_kind(ep.backend)
        is_llama = kind == "llamacpp" or any(m.get("owned_by") == "llamacpp" for m in entries)
        is_strata = kind == "strata"
        if not (is_llama or is_strata or kind == "other"):
            return {}, "", {}
        extra: dict[str, Any] = {}
        try:
            r = await c.get(f"{ep.url}/props")
            if r.status_code != 200:
                return {}, "", {}
            # 3.11a row 20: a side route's body is the backend's, not ours — a JSON shape this
            # code does not read (an array where an object belongs, a non-object slot) is
            # ignored like an absent route, never an AttributeError that probe() would turn
            # into "down" for an endpoint that just answered /v1/models
            props = r.json()
            if not isinstance(props, dict):
                return {}, "", {}
            gen = props.get("default_generation_settings")
            n_ctx = gen.get("n_ctx") if isinstance(gen, dict) else None
            if props.get("endpoint_slots") or is_strata:
                sl = await c.get(f"{ep.url}/slots")
                body = sl.json() if sl.status_code == 200 else None
                if isinstance(body, list):
                    slots = [x for x in body if isinstance(x, dict)]
                    busy = sum(1 for x in slots if x.get("is_processing"))
                    extra["slots"] = {"total": len(slots), "busy": busy, "free": len(slots) - busy}
            if "slots" not in extra and isinstance(props.get("total_slots"), int):
                extra["slots"] = {"total": props["total_slots"]}
            extra["metrics"] = bool(props.get("endpoint_metrics")) or is_strata
            if is_strata:
                mt = await c.get(f"{ep.url}/metrics")
                if mt.status_code == 200:
                    doc = mt.json()
                    live = doc.get("live") if isinstance(doc, dict) else None
                    live = live if isinstance(live, dict) else {}
                    if isinstance(live.get("queued"), int):
                        extra["load"] = {"requests_waiting": live["queued"]}
            elif is_llama and kind == "llamacpp":
                # An endpoint configured as llama.cpp that is really strata loses nothing today,
                # but the strata branch reads its slots and metrics; say so.
                hint = await _strata_hint(c, ep)
                if hint:
                    extra["hint"] = hint
            source = "strata:/props n_ctx" if is_strata else "llama.cpp:/props n_ctx"
            if isinstance(n_ctx, int) and n_ctx > 0:
                return {m["id"]: n_ctx for m in entries}, source, extra
        except (httpx.HTTPError, ValueError):
            pass
        return {}, "", extra

    async def probe_all(self) -> dict[str, dict[str, Any]]:
        names = list(self.endpoints)
        results = await asyncio.gather(*(self.probe(self.endpoints[n]) for n in names))
        return dict(zip(names, results))

    # ---------- resolution ----------
    @staticmethod
    def match(name: str, served_ids: list[str]) -> str | None:
        """Served id matching ``name``: exact, then fnmatch glob, then fuzzy.

        Fuzzy = alphanumerics only, case-folded, name must be a substring of the id — so
        ``qwen3.8-27b`` matches ``unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL``.
        """
        for sid in served_ids:
            if sid == name or fnmatch.fnmatchcase(sid, name):
                return sid
        key = _squash(name)
        if len(key) < 3:
            return None
        for sid in served_ids:
            if key in _squash(sid):
                return sid
        return None

    async def resolve(
        self, model: str | None = None, endpoint: str | None = None
    ) -> "Resolution":
        """Find a live endpoint serving ``model`` (served id, glob or fuzzy name).

        With no model (and no ``defaults.model``) the first live endpoint that is serving
        anything wins, in config order. Raises ModelUnavailable with a human-facing hint
        when nothing fits.
        """
        if endpoint and endpoint not in self.endpoints:
            raise ModelUnavailable(
                f"Unknown endpoint '{endpoint}'. Known: {sorted(self.endpoints)}",
                known_endpoints=sorted(self.endpoints),
            )

        model = model or self.defaults.model
        candidates = [self.endpoints[endpoint]] if endpoint else list(self.endpoints.values())
        probes = await asyncio.gather(*(self.probe(e) for e in candidates))
        for ep, pr in zip(candidates, probes):
            if not pr["up"] or not pr["models"]:
                continue
            served = self.match(model, pr["models"]) if model else pr["models"][0]
            if served:
                return Resolution.make(ep, served, pr)

        # Nothing matched -> build a helpful hint.
        status = {ep.name: pr for ep, pr in zip(candidates, probes)}
        up_now = {n: p["models"] for n, p in status.items() if p["up"]}
        if not up_now:
            hint = "No endpoint is up. Ask the user to start a model server."
        elif model:
            hint = f"No live endpoint is serving a model matching '{model}'. Ask the user to start one."
        else:
            hint = "Endpoints are up but none is serving a model. Ask the user to load one."
        if up_now:
            hint += f"\nCurrently serving: {up_now}"
        raise ModelUnavailable(hint, model=model, endpoints=status)


# ---------- /metrics (server-wide counters; per-job deltas are computed by the runner)

_METRIC_KEYS = {
    # llama.cpp (--metrics)
    "llamacpp:prompt_tokens_total": "prompt_tokens",
    "llamacpp:prompt_tokens_cached_total": "prompt_tokens_cached",
    "llamacpp:prompt_seconds_total": "prompt_seconds",
    "llamacpp:tokens_predicted_total": "generated_tokens",
    "llamacpp:tokens_predicted_seconds_total": "generated_seconds",
    "llamacpp:spec_decode_num_draft_tokens_total": "spec_draft_tokens",
    "llamacpp:spec_decode_num_accepted_tokens_total": "spec_accepted_tokens",
    # gauges: llama.cpp's names for the probes row's running/waiting columns (story 2.2a D)
    "llamacpp:requests_processing": "requests_running",
    "llamacpp:requests_deferred": "requests_waiting",
    # vLLM (always on); prefix-cache counters are in tokens
    "vllm:prompt_tokens_total": "prompt_tokens",
    "vllm:generation_tokens_total": "generated_tokens",
    "vllm:prefix_cache_queries_total": "prefix_queries",
    "vllm:prefix_cache_hits_total": "prefix_hits",
    "vllm:num_requests_running": "requests_running",
    "vllm:num_requests_waiting": "requests_waiting",
    "vllm:kv_cache_usage_perc": "kv_cache_usage",
    "vllm:spec_decode_num_draft_tokens_total": "spec_draft_tokens",
    "vllm:spec_decode_num_accepted_tokens_total": "spec_accepted_tokens",
    "vllm:num_preemptions_total": "preemptions",
}


def parse_metrics(text: str) -> dict[str, float]:
    """Prometheus text -> {short_name: value}; labelled series (vLLM) are summed per metric."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition(" ")
        if "{" in name:  # name{labels} value
            name, _, _ = name.partition("{")
            rest = line[line.rfind("}") + 1:].strip()
        key = _METRIC_KEYS.get(name)
        if key is None:
            continue
        try:
            out[key] = out.get(key, 0.0) + float(rest.split()[0])
        except (ValueError, IndexError):
            pass
    return out


async def _strata_hint(c: httpx.AsyncClient, ep: Endpoint) -> str | None:
    """``/health`` on strata carries ``"service": "strata"``; llama-server's does not."""
    try:
        r = await c.get(f"{ep.url}/health")
        if r.status_code == 200 and isinstance(r.json(), dict) and r.json().get("service") == "strata":
            return f"/health reports strata; set `backend: strata` for endpoint '{ep.name}' to get slot occupancy and per-job metrics"
    except (httpx.HTTPError, ValueError):
        pass
    return None


def parse_strata_metrics(doc: dict[str, Any]) -> dict[str, float]:
    """strata's JSON ``/metrics`` -> the same short names as llama.cpp's counters.

    ``totals`` is cumulative since the server started: ``prompt_tokens`` counts every prompt token
    including the ``reused`` (prefix-cached) ones, so the processed count is the difference;
    ``prompt_ms``/``decode_ms`` are the phase times; ``drafts_offered``/``drafts_accepted`` are
    the speculative-decode counters. ``live.queued`` is the request queue depth.

    A counter the document does not carry is ABSENT from the result, never 0.0 (story 2.2a
    row 2, review D2, for the schema's "NULL when absent"): a chart or a rate must read "not reported",
    not "reported and zero". So ``prompt_tokens`` processed is ``prompt_tokens - reused`` when
    both are present; with ``reused`` absent it is ``prompt_tokens`` itself and the cached column
    is absent; with ``prompt_tokens`` absent no processed count is reported."""
    totals = doc.get("totals") if isinstance(doc, dict) else None
    if not isinstance(totals, dict):
        return {}

    def num(k: str) -> float | None:
        v = totals.get(k)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    out: dict[str, float] = {}
    prompt, reused = num("prompt_tokens"), num("reused")
    if prompt is not None:
        out["prompt_tokens"] = max(0.0, prompt - reused) if reused is not None else prompt
    if reused is not None:
        out["prompt_tokens_cached"] = reused
    for src, dst, scale in (("prompt_ms", "prompt_seconds", 1000.0),
                            ("output_tokens", "generated_tokens", 1.0),
                            ("decode_ms", "generated_seconds", 1000.0),
                            ("drafts_offered", "spec_draft_tokens", 1.0),
                            ("drafts_accepted", "spec_accepted_tokens", 1.0)):
        v = num(src)
        if v is not None:
            out[dst] = v / scale
    live = doc.get("live") or {}
    if isinstance(live.get("queued"), (int, float)) and not isinstance(live["queued"], bool):
        out["requests_waiting"] = float(live["queued"])
    return out


# The probe_hardware columns (db.py schema version 3, plus the two disk columns appended by
# version 4, story 3.1a ruling A), read under the same names from strata's ``hardware`` section.
STRATA_HARDWARE_KEYS = (
    "gpu_util", "gpu_mem_used", "gpu_mem_total", "gpu_temp", "gpu_power", "gpu_power_limit",
    "gpu_pcie_rx_mb", "gpu_pcie_tx_mb", "cpu", "ram_used", "ram_total",
    "disk_read_mb", "disk_write_mb",
)


def _number(v: Any) -> float | None:
    """A JSON number as a float; anything else (null, a string, a bool) is None."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_strata_extra(doc: Any) -> dict[str, Any]:
    """The parts of strata's JSON ``/metrics`` the counters parser does not read, for the probe rows.

    ``since``: ``totals.since``, the server's start epoch (a change means a restart).
    ``gen_tps``: ``hardware.tok_s``, else ``live.tok_s``, whichever is a number first; else None.
    ``hardware``: the thirteen ``probe_hardware`` columns from the ``hardware`` section as floats,
    a missing or non-numeric one None; None when the section is absent or holds none of them
    (strata before 0.1.5x has no such section).
    ``requests_running``: 0 when ``live.state`` is ``"idle"``, 1 for any other state string
    (strata serves one request at a time), None when there is no state. Every captured state is
    ``"idle"`` (story 2.1's four captures and the 0.1.44.1 one).
    ``state``: ``live.state`` verbatim when it is a string, else None (story 3.1a ruling A:
    ``probes.state`` stores it as the backend writes it, never normalised).
    """
    d = doc if isinstance(doc, dict) else {}
    totals = d.get("totals") if isinstance(d.get("totals"), dict) else {}
    live = d.get("live") if isinstance(d.get("live"), dict) else {}
    hw = d.get("hardware") if isinstance(d.get("hardware"), dict) else {}
    hardware = {k: _number(hw.get(k)) for k in STRATA_HARDWARE_KEYS}
    if all(v is None for v in hardware.values()):
        hardware = None
    tps = _number(hw.get("tok_s"))
    if tps is None:
        tps = _number(live.get("tok_s"))
    state = live.get("state")
    return {
        "since": _number(totals.get("since")),
        "gen_tps": tps,
        "hardware": hardware,
        "requests_running": (0 if state == "idle" else 1) if isinstance(state, str) else None,
        "state": state if isinstance(state, str) else None,
    }


async def fetch_metrics_doc(ep: Endpoint, timeout_s: float = 2.5) -> tuple[str, str | dict[str, Any]] | None:
    """The server's ``/metrics`` document, unparsed: ``("prometheus", text)`` (llama.cpp with
    ``--metrics``; vLLM) or ``("strata", doc)`` for strata's JSON. None if unavailable (not 200,
    an HTTP error, or a body that starts as a JSON object and does not parse as one)."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s, headers=ep.auth_headers()) as c:
            r = await c.get(f"{ep.url}/metrics")
        if r.status_code != 200:
            return None
    except httpx.HTTPError:
        return None
    text = r.text
    if text.lstrip().startswith("{"):
        try:
            doc = json.loads(text)
        except ValueError:
            return None
        return ("strata", doc) if isinstance(doc, dict) else None
    return "prometheus", text


async def fetch_metrics(ep: Endpoint, timeout_s: float = 2.5) -> dict[str, float] | None:
    """Snapshot the server's counters: Prometheus text (llama.cpp needs ``--metrics``; vLLM) or
    strata's JSON, parsed to short names. None if unavailable or nothing in it was understood."""
    got = await fetch_metrics_doc(ep, timeout_s)
    if got is None:
        return None
    kind, body = got
    if kind == "strata":
        return parse_strata_metrics(body) or None
    return parse_metrics(body) or None


def metrics_delta(
    before: dict[str, float] | None,
    after: dict[str, float] | None,
    backend: str | None = None,
) -> dict[str, Any] | None:
    """Cache/throughput summary for the span between two snapshots (llama.cpp or vLLM counters).

    ``backend`` (a ``backend_kind`` value) decides the arithmetic. vLLM's ``prompt_tokens``
    already counts cached tokens, so llama.cpp's cached/(processed+cached) must never be applied
    to a vLLM snapshot — a vLLM without the prefix-cache series reports the prompt delta as
    processed and omits the cache keys rather than guessing them. A backend that names a counter
    format (``llamacpp``, ``strata``) never sniffs. One that names none (``None``, ``other``,
    ``ollama``; ``other`` is the config default and the prober finds a vLLM whatever the
    backend says) is taken as vLLM when ``prefix_queries`` is in both snapshots. The prefix
    arithmetic itself also needs the series in both: present in one only, it is the no-prefix
    arithmetic (review of 1.8)."""
    if not before or not after:
        return None
    # counters in BOTH snapshots only: a series that vanished from `after` is a backend change,
    # not a counter running backwards (the row's reset rule must not fire on it), and a series
    # that only appears in `after` is a since-start value, not a delta (review of 1.8)
    d = {k: after[k] - before[k] for k in set(before) & set(after)}
    vllm = backend == "vllm" or (backend in (None, "other", "ollama") and "prefix_queries" in d)
    # Every counter read below is behind a presence check on `d`: a counter missing from either
    # snapshot gives no output key — never a KeyError (one reached run_job's finally and stranded
    # the job) and never a 0 standing in for an unknown (review of 1.8, construct-it).
    # The deltas keep their precision (ruling A of the 2.4 fold): the counters are integers in
    # production, so ints in give ints out, but a fractional pair gives a fractional delta — the
    # old int() truncated every 5 s interval and made the monitor's t/s read low (F11). The
    # ``jobs`` row rounds its three INTEGER columns; the REAL ones keep the fraction.
    out: dict[str, Any] = {}
    if "generated_tokens" in d:
        out["generated_tokens"] = d["generated_tokens"]
    if vllm and "prefix_queries" in d:  # vLLM: prompt_tokens counts everything; hits are the cached part
        if "prefix_hits" in d:
            hits = d["prefix_hits"]
            if "prompt_tokens" in d:
                out["prompt_tokens_processed"] = max(0.0, d["prompt_tokens"] - hits)
            out["prompt_tokens_cached"] = hits
            if d.get("prefix_queries", 0.0) > 0:
                out["cache_hit_ratio"] = round(hits / d["prefix_queries"], 3)
    elif vllm:  # vLLM without the prefix-cache series: prompt_tokens is itself the processed count
        if "prompt_tokens" in d:
            out["prompt_tokens_processed"] = d["prompt_tokens"]
    else:  # llama.cpp (and strata, whose parser already splits the two): separate counters
        if "prompt_tokens" in d:
            out["prompt_tokens_processed"] = d["prompt_tokens"]
        if "prompt_tokens_cached" in d:
            out["prompt_tokens_cached"] = d["prompt_tokens_cached"]
        if "prompt_tokens" in d and "prompt_tokens_cached" in d:
            processed, cached = d["prompt_tokens"], d["prompt_tokens_cached"]
            if processed + cached > 0:
                out["cache_hit_ratio"] = round(cached / (processed + cached), 3)
        if "prompt_tokens" in d and d.get("prompt_seconds", 0.0) > 0:
            out["prompt_tps"] = round(d["prompt_tokens"] / d["prompt_seconds"], 1)
        if "generated_tokens" in d and d.get("generated_seconds", 0.0) > 0:
            out["generate_tps"] = round(d["generated_tokens"] / d["generated_seconds"], 1)
    if "spec_accepted_tokens" in d and d.get("spec_draft_tokens", 0.0) > 0:
        out["spec_decode_acceptance"] = round(d["spec_accepted_tokens"] / d["spec_draft_tokens"], 3)
    return out


@dataclass
class Resolution:
    endpoint: Endpoint
    served_model: str
    context: int | None = None  # tokens the backend will actually accept per request
    context_source: str = ""

    @classmethod
    def make(cls, ep: Endpoint, served: str, probe: dict[str, Any]) -> "Resolution":
        probed = (probe.get("context") or {}).get(served)
        if probed:
            return cls(ep, served, probed, probe.get("context_source", "probe"))
        return cls(ep, served)


class ModelUnavailable(Exception):
    def __init__(self, msg: str, **info: Any):
        super().__init__(msg)
        self.info = info

    def to_dict(self) -> dict[str, Any]:
        return {"error": "model_unavailable", "message": str(self), **self.info}
