"""Model/endpoint registry.

Two concepts, deliberately separated because models are brought up and down often:

* **Endpoint** – a place that speaks the Anthropic ``/v1/messages`` API
  (llama.cpp ``llama-server``, vLLM, Ollama, ...).  What it is *currently*
  serving is discovered live via ``GET /v1/models``.
* **Model** – a named entry in the pool: the menu Claude can ask the human for by
  name when nothing suitable is up. A name alone is enough (it is fuzzy-matched
  against served ids); ``host``/``notes`` say where it lives and what it's for.
  ``served_name``, ``endpoint``, ``context`` and ``bring_up`` are optional overrides —
  the context window and endpoint are discovered live, and start commands rot.

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
class ModelSpec:
    name: str
    host: str = ""  # where the human runs it (informational: "local", "dgx", ...)
    notes: str = ""  # what it's for / quirks, relayed to the human with a bring-up request
    served_name: str = ""  # override: exact id or fnmatch glob; default = fuzzy match on name
    endpoint: str | None = None  # override: preferred endpoint name
    bring_up: str = ""  # override: start command to relay (optional; these go stale fast)
    tags: list[str] = field(default_factory=list)
    context: int | None = None  # fallback when the endpoint does not report a window
    env: dict[str, str] = field(default_factory=dict)
    max_turns: int | None = None
    permission_mode: str | None = None


@dataclass
class Defaults:
    model: str | None = None
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
    """Sidecar for entries added via register_model/register_endpoint: ``models.local.yaml``.

    The hand-written file is never rewritten (comments and layout survive); the sidecar is
    merged on top of it at load, entry by entry.
    """
    return path.with_name(path.stem + ".local" + path.suffix)


@dataclass
class Registry:
    path: Path
    endpoints: dict[str, Endpoint]
    models: dict[str, ModelSpec]
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
        merged_models = {**(raw.get("models") or {}), **(local.get("models") or {})}
        endpoints = {name: Endpoint(name=name, **(cfg or {})) for name, cfg in merged_endpoints.items()}
        models = {name: ModelSpec(name=name, **(cfg or {})) for name, cfg in merged_models.items()}
        defaults = Defaults(**{**(raw.get("defaults") or {}), **(local.get("defaults") or {})})
        return cls(path=path, endpoints=endpoints, models=models, defaults=defaults, raw=raw, local=local)

    def save(self) -> None:
        """Persist ``self.local`` to the sidecar; ``models.yaml`` itself is left untouched."""
        lp = local_path(self.path)
        lp.parent.mkdir(parents=True, exist_ok=True)
        header = "# Written by localagents register_model/register_endpoint; merged over models.yaml.\n"
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
    def match(self, spec: ModelSpec, served_ids: list[str]) -> str | None:
        """Served id for a pool entry: ``served_name`` (exact/glob) if set, else fuzzy on the name.

        Fuzzy = alphanumerics only, case-folded, name must be a substring of the id — so
        ``qwen3.8-27b`` matches ``unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL``. A plain served id
        passed as a name (not in the pool) still matches exactly.
        """
        if spec.served_name:
            for sid in served_ids:
                if sid == spec.served_name or fnmatch.fnmatchcase(sid, spec.served_name):
                    return sid
            return None
        for sid in served_ids:
            if sid == spec.name or fnmatch.fnmatchcase(sid, spec.name):
                return sid
        key = _squash(spec.name)
        if len(key) < 3:
            return None
        for sid in served_ids:
            if key in _squash(sid):
                return sid
        return None

    async def resolve(
        self, model: str | None = None, endpoint: str | None = None
    ) -> "Resolution":
        """Find a live endpoint serving the requested model.

        Raises ModelUnavailable with a human-facing hint when nothing fits.
        """
        if endpoint and endpoint not in self.endpoints:
            raise ModelUnavailable(
                f"Unknown endpoint '{endpoint}'. Known: {sorted(self.endpoints)}",
                known_endpoints=sorted(self.endpoints),
            )

        model = model or self.defaults.model
        spec = self.models.get(model) if model else None
        if model and spec is None:
            # Not in the pool: treat the string as a served model id / glob / fuzzy name.
            spec = ModelSpec(name=model)

        candidates = [self.endpoints[endpoint]] if endpoint else list(self.endpoints.values())
        if spec and spec.endpoint and not endpoint and spec.endpoint in self.endpoints:
            # preferred endpoint first
            candidates.sort(key=lambda e: 0 if e.name == spec.endpoint else 1)

        probes = await asyncio.gather(*(self.probe(e) for e in candidates))
        for ep, pr in zip(candidates, probes):
            if not pr["up"]:
                continue
            if spec is None:
                # No model requested: take whatever this endpoint is serving.
                if pr["models"]:
                    sid = pr["models"][0]
                    return Resolution.make(ep, sid, self.models_for_served(sid), pr)
                continue
            served = self.match(spec, pr["models"])
            if served:
                return Resolution.make(ep, served, spec, pr)

        # Nothing matched -> build a helpful hint.
        status = {ep.name: pr for ep, pr in zip(candidates, probes)}
        hint_parts = []
        if spec and spec.name in self.models:
            where = f" on {spec.host}" if spec.host else ""
            hint_parts.append(f"'{spec.name}' is not running anywhere. Ask the user to bring it up{where}.")
            if spec.notes:
                hint_parts.append(f"Notes: {spec.notes}")
            if spec.bring_up:
                hint_parts.append(f"Start command on file (may be stale):\n  {spec.bring_up}")
        elif spec:
            hint_parts.append(
                f"'{spec.name}' is not in the pool and no endpoint is serving a model matching it. "
                "Ask the user to start it, or pick from list_models."
            )
        else:
            hint_parts.append("No endpoint is up. Ask the user which model to bring up.")
        up_now = {n: p["models"] for n, p in status.items() if p["up"]}
        if up_now:
            hint_parts.append(f"Currently serving: {up_now}")
        raise ModelUnavailable(
            "\n".join(hint_parts),
            model=spec.name if spec else None,
            host=spec.host if spec else "",
            notes=spec.notes if spec else "",
            bring_up=spec.bring_up if spec else "",
            endpoints=status,
        )

    def models_for_served(self, served_id: str) -> ModelSpec | None:
        for spec in self.models.values():
            if self.match(spec, [served_id]):
                return spec
        return None


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
    spec: ModelSpec | None
    context: int | None = None  # tokens the backend will actually accept per request
    context_source: str = ""

    @classmethod
    def make(cls, ep: Endpoint, served: str, spec: ModelSpec | None, probe: dict[str, Any]) -> "Resolution":
        probed = (probe.get("context") or {}).get(served)
        if probed:
            return cls(ep, served, spec, probed, probe.get("context_source", "probe"))
        if spec and spec.context:
            return cls(ep, served, spec, spec.context, "models.yaml")
        return cls(ep, served, spec)


class ModelUnavailable(Exception):
    def __init__(self, msg: str, **info: Any):
        super().__init__(msg)
        self.info = info

    def to_dict(self) -> dict[str, Any]:
        return {"error": "model_unavailable", "message": str(self), **self.info}
