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
import fnmatch
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


@dataclass
class Endpoint:
    name: str
    base_url: str
    backend: str = "other"  # llama.cpp | vllm | ollama | other
    host: str = "local"
    notes: str = ""
    env: dict[str, str] = field(default_factory=dict)  # extra env for claude sessions

    @property
    def url(self) -> str:
        return self.base_url.rstrip("/")


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
            async with httpx.AsyncClient(timeout=self.defaults.probe_timeout_s) as c:
                r = await c.get(f"{ep.url}/v1/models")
                r.raise_for_status()
                data = r.json()
                entries = [m for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
                ids = [m["id"] for m in entries]
                context, source, extra = await self._probe_context(c, ep, entries)
            out: dict[str, Any] = {"up": True, "models": ids, "latency_ms": int((time.monotonic() - t0) * 1000)}
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
            return {"up": False, "models": [], "error": f"{type(e).__name__}: {e}"}

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
                    extra["load"] = {
                        "requests_running": int(mt.get("requests_running", 0)),
                        "requests_waiting": int(mt.get("requests_waiting", 0)),
                        "kv_cache_usage": round(mt.get("kv_cache_usage", 0.0), 3),
                    }
            except httpx.HTTPError:
                pass
            return ctx, "vllm:max_model_len", extra
        # llama.cpp: /props -> default_generation_settings.n_ctx (per slot); /slots -> occupancy
        is_llama = ep.backend.replace(".", "").replace("-", "").lower() in ("llamacpp", "llama") or any(
            m.get("owned_by") == "llamacpp" for m in entries
        )
        if not (is_llama or ep.backend == "other"):
            return {}, "", {}
        extra: dict[str, Any] = {}
        try:
            r = await c.get(f"{ep.url}/props")
            if r.status_code != 200:
                return {}, "", {}
            props = r.json()
            n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
            if props.get("endpoint_slots"):
                sl = await c.get(f"{ep.url}/slots")
                if sl.status_code == 200 and isinstance(sl.json(), list):
                    slots = sl.json()
                    busy = sum(1 for x in slots if x.get("is_processing"))
                    extra["slots"] = {"total": len(slots), "busy": busy, "free": len(slots) - busy}
            elif isinstance(props.get("total_slots"), int):
                extra["slots"] = {"total": props["total_slots"]}
            extra["metrics"] = bool(props.get("endpoint_metrics"))
            if isinstance(n_ctx, int) and n_ctx > 0:
                return {m["id"]: n_ctx for m in entries}, "llama.cpp:/props n_ctx", extra
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


async def fetch_metrics(base_url: str, timeout_s: float = 2.5) -> dict[str, float] | None:
    """Snapshot the server's Prometheus counters (llama.cpp needs ``--metrics``). None if unavailable."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as c:
            r = await c.get(f"{base_url.rstrip('/')}/metrics")
        if r.status_code != 200:
            return None
    except httpx.HTTPError:
        return None
    return parse_metrics(r.text) or None


def metrics_delta(before: dict[str, float] | None, after: dict[str, float] | None) -> dict[str, Any] | None:
    """Cache/throughput summary for the span between two snapshots (llama.cpp or vLLM counters)."""
    if not before or not after:
        return None
    d = {k: after.get(k, 0.0) - before.get(k, 0.0) for k in set(before) | set(after)}
    out: dict[str, Any] = {"generated_tokens": int(d.get("generated_tokens", 0))}
    if "prefix_queries" in after:  # vLLM: prompt_tokens counts everything; hits are the cached part
        total, hits = d.get("prompt_tokens", 0.0), d.get("prefix_hits", 0.0)
        out["prompt_tokens_processed"] = int(max(0.0, total - hits))
        out["prompt_tokens_cached"] = int(hits)
        if d.get("prefix_queries", 0.0) > 0:
            out["cache_hit_ratio"] = round(hits / d["prefix_queries"], 3)
        if d.get("spec_draft_tokens", 0.0) > 0:
            out["spec_decode_acceptance"] = round(d["spec_accepted_tokens"] / d["spec_draft_tokens"], 3)
    else:  # llama.cpp: processed and cached are separate counters
        processed, cached = d.get("prompt_tokens", 0.0), d.get("prompt_tokens_cached", 0.0)
        out["prompt_tokens_processed"] = int(processed)
        out["prompt_tokens_cached"] = int(cached)
        if processed + cached > 0:
            out["cache_hit_ratio"] = round(cached / (processed + cached), 3)
        if d.get("prompt_seconds", 0.0) > 0:
            out["prompt_tps"] = round(processed / d["prompt_seconds"], 1)
        if d.get("generated_seconds", 0.0) > 0:
            out["generate_tps"] = round(d["generated_tokens"] / d["generated_seconds"], 1)
        if d.get("spec_draft_tokens", 0.0) > 0:
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
