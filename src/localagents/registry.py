"""Endpoint registry.

The config lists **endpoints**: places that speak the Anthropic ``/v1/messages`` API
(llama.cpp ``llama-server``, vLLM, ...). Nothing about models lives in the config.
What each endpoint is *currently* serving, its context window and its occupancy are
discovered live via ``GET /v1/models`` (plus ``/props``, ``/slots`` or ``/metrics``)
on every call. The human brings models up and down by hand; Claude uses whatever is up.

One entry type speaks no ``/v1/messages``: ``backend: router`` names a decision server that
picks a real endpoint for each job (see ``docs/router.md``); it serves only its own name.

The YAML file is re-read on every call, so edits take effect immediately.
"""

from __future__ import annotations

import asyncio
import errno
import fnmatch
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

# Below this per-request window a Claude Code session spends most of its turns compacting
# (~20k fixed prompt + summary + re-attached files). See docs/shim.md "Context windows".
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
    """Normalise the config's ``backend`` string: llamacpp | vllm | strata | ollama | router | other."""
    b = backend.replace(".", "").replace("-", "").replace("_", "").lower()
    if b in ("llamacpp", "llama"):
        return "llamacpp"
    if b in ("vllm", "strata", "ollama", "router", "other"):
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
    backend: str = "other"  # llama.cpp | vllm | strata | ollama | router | other
    host: str = "local"
    notes: str = ""
    env: dict[str, str] = field(default_factory=dict)  # extra env for claude sessions
    api_key: str | None = None  # sent on every call to this endpoint; never handed to the session
    # router entries (backend: router): no /v1/messages of their own — the d3 decision
    # server behind decision_url picks a real endpoint for each job (docs/router.md).
    decision_url: str | None = None  # the only key a router entry must carry
    decision_model: str = "vllm-sr/d3-mini"  # reported by the server; a mismatch is flagged on the probe
    decision_timeout_s: float = 20.0  # slower than this is model_unavailable
    decision_max_tokens: int = 12_000  # longer briefs are scored truncated (the measured server limit; docs/router.md)
    tolerance: float = 0.5  # shortfall distance for "acceptable" (the calibration spec's spread)
    divert_after_min: float = 10.0  # normal priority waits this long before diverting
    max_concurrent: int | None = None  # cap below what the capacity model would admit
    # a real endpoint's conditions under which the router must not pick it (spec §5, §12):
    # strings of the form "dimension >= n" ("complexity >= 3"), read off the brief's scores —
    # an endpoint carrying one is skipped as a candidate whenever its condition holds
    exclude: list[str] = field(default_factory=list)
    # router 11 (design §7 "Accepted" 2026-11-04): the operator's name for the hardware this
    # entry shares with others (a GPU behind several endpoints, a UPS behind several boxes).
    # Inert until declared: a name with no ``pools:`` entry is noted at load and reset to None.
    # Never inferred from hosts, ports or probes.
    pool: str | None = None
    # the problem validate() found on this entry, or None. I2: the server re-reads the config
    # on every call, so one bad entry is marked (load prints it) rather than raising into every
    # running job; a marked entry probes down, is never a resolution candidate, and is not real.
    invalid: str | None = None

    @property
    def is_router(self) -> bool:
        return backend_kind(self.backend) == "router"

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
    # A session with a background task outlives its result and holds its slot until the task
    # ends (the 2026-11-15 smoke: to its timeout). The default stays ON, the operator's call on
    # the store's record (2026-11-16): 24 of 209 jobs backgrounded a test suite or a server and
    # all but one succeeded, none stuck after a result; the one stuck job was a smoke whose
    # brief demanded a foreground sleep the harness blocks. False (here, or a run_agent call
    # for its own job) runs a job foreground only: harnesses.claude_code.build_env writes CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 and
    # these two Bash timeouts (BASH_DEFAULT_TIMEOUT_MS / BASH_MAX_TIMEOUT_MS), run_job
    # disallows Monitor and appends harnesses.claude_code.FOREGROUND_NOTE to the system prompt
    background_tasks: bool = True
    bash_timeout_s: int = 600
    bash_max_timeout_s: int = 1800

    def check(self) -> None:
        """The foreground keys' values: a wrong type or a number under 1 is named on stderr and
        the key keeps its default, never coerced (``background_tasks: "false"`` is a typo, as
        ``max_concurrent: "1"`` is on an entry) and never a raise: the server re-reads the
        config on every call (I2). Registry.load calls it."""
        base = Defaults()
        if not isinstance(self.background_tasks, bool):
            print(f"localagents: defaults: background_tasks must be true or false, got "
                  f"{self.background_tasks!r}; using {str(base.background_tasks).lower()}", file=sys.stderr)
            self.background_tasks = base.background_tasks
        for key in ("bash_timeout_s", "bash_max_timeout_s"):
            v = getattr(self, key)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                print(f"localagents: defaults: {key} must be a whole number of seconds, 1 or more, "
                      f"got {v!r}; using {getattr(base, key)}", file=sys.stderr)
                setattr(self, key, getattr(base, key))


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
    # router 11: models.yaml's optional ``pools:`` block, ``{<name>: {"max_concurrent": n}}``,
    # the declared pools with a usable cap only (``_read_pools``); empty when there is no block
    pools: dict[str, dict] = field(default_factory=dict)

    # ---------- loading ----------
    @classmethod
    def load(cls, path: Path | None = None) -> "Registry":
        path = path or config_path()
        raw = _read_yaml(path)
        local = _read_yaml(local_path(path))
        merged_endpoints = {**(raw.get("endpoints") or {}), **(local.get("endpoints") or {})}
        endpoints = {name: cls._build_endpoint(name, cfg or {}) for name, cfg in merged_endpoints.items()}
        defaults = Defaults(**{**(raw.get("defaults") or {}), **(local.get("defaults") or {})})
        defaults.check()
        # A leftover ``models:`` section from older configs is ignored: served models are discovered.
        pools, refused = cls._read_pools(raw.get("pools"))
        reg = cls(path=path, endpoints=endpoints, defaults=defaults, raw=raw, local=local, pools=pools)
        reg.validate()
        for ep in endpoints.values():
            if ep.invalid:
                print(f"localagents: endpoint '{ep.name}': {ep.invalid}, ignored until fixed",
                      file=sys.stderr)
            # a non-string (a YAML list, a number) is no pool name, and a list is unhashable
            if ep.pool is not None and (not isinstance(ep.pool, str) or ep.pool not in pools):
                # inert until declared (the operator, 2026-11-04: "as long as it's ignored until
                # needed, otherwise we do self-harm"): a note, never an invalid entry. A pool the
                # block declared with an unusable cap was named once by _read_pools already.
                if not isinstance(ep.pool, str) or ep.pool not in refused:
                    print(f"localagents: endpoint '{ep.name}': pool '{ep.pool}' is not declared "
                          "under pools:, ignoring it", file=sys.stderr)
                ep.pool = None
        return reg

    @staticmethod
    def _read_pools(block: Any) -> tuple[dict[str, dict], set]:
        """models.yaml's ``pools:`` block as ``({name: {"max_concurrent": n}}, refused names)``.
        Read from the hand-written file only: pools encode GPU sharing and the UPS, which only
        the operator knows (design §7 "Accepted"), and the sidecar is written by a tool. A pool
        whose cap is not a whole number of 1 or more is named once on stderr and left out, never
        coerced (``max_concurrent: "1"`` is a typo, as on an entry) and never an error that
        takes its members down; its members then belong to no pool."""
        if block is None:
            return {}, set()
        if not isinstance(block, dict):
            print("localagents: pools: must map pool names to {max_concurrent: n}, ignoring it",
                  file=sys.stderr)
            return {}, set()
        pools: dict[str, dict] = {}
        refused: set = set()
        for name, cfg in block.items():
            cap = cfg.get("max_concurrent") if isinstance(cfg, dict) else None
            if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
                print(f"localagents: pool '{name}': max_concurrent must be a whole number of 1 or "
                      "more, ignoring the pool", file=sys.stderr)
                refused.add(name)
                continue
            for k in cfg:
                if k != "max_concurrent":
                    print(f"localagents: pool '{name}': ignoring unknown key '{k}'", file=sys.stderr)
            pools[name] = {"max_concurrent": cap}
        return pools, refused

    @staticmethod
    def _build_endpoint(name: str, cfg: dict) -> Endpoint:
        """Build from the dataclass's known fields only: a key from a newer package's config
        is skipped with a line on stderr, never a TypeError in a running server that re-reads
        the file mid-session (§12's upgrade order). ``base_url`` defaults to ``""`` here so a
        missing one reaches ``validate()`` (which marks it invalid) instead of the
        constructor's ``TypeError``; the field itself stays required for direct construction."""
        known = {f.name for f in fields(Endpoint)}
        kwargs: dict = {}
        for k, v in cfg.items():
            if k in known:
                kwargs[k] = v
            else:
                print(f"localagents: endpoint '{name}': ignoring unknown key '{k}'", file=sys.stderr)
        kwargs.setdefault("base_url", "")
        return Endpoint(name=name, **kwargs)

    def validate(self) -> None:
        """Every entry constructible but not every entry usable: the router needs its
        decision server, the real ones their address. Called by ``load``, which prints the
        marked entries.

        I2: this never raises. The server re-reads the config on every call, so a raise here
        turned one bad entry into a 500 on every job. A problem marks the entry (``invalid``
        holds the sentence) and the entry probes down, is never a resolution candidate, and
        is left out of ``real_endpoints`` until the file is fixed."""
        for ep in self.endpoints.values():
            if ep.is_router:
                ep.invalid = None if ep.decision_url else "backend router needs decision_url"
            elif not ep.base_url:
                ep.invalid = "needs base_url"
            else:
                ep.invalid = None
            mc = ep.max_concurrent
            if ep.invalid is None and mc is not None and (
                isinstance(mc, bool) or not isinstance(mc, int) or mc < 1
            ):
                # M7: the value feeds capacity.from_probe's min() directly — a quoted "2" is a
                # TypeError mid-throughput-probe and a 0 is an endpoint that admits nothing.
                # A bool is not an int here: ``max_concurrent: true`` is a typo, not a cap of 1.
                ep.invalid = "max_concurrent must be a whole number of 1 or more"

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

        A router entry's probe is its decision server's ``/health``: it serves only its own
        name; the fleet's windows belong to ``list_models``' ``context_range``.

        An entry ``validate`` marked answers with its problem, without touching the network.
        """
        if ep.invalid:
            return {"up": False, "models": [], "error": ep.invalid}
        if ep.is_router:
            return await self._probe_router(ep)
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

    async def _probe_router(self, ep: Endpoint) -> dict[str, Any]:
        """The decision server's ``/health``: up means ``auto`` is usable; the body's ``model``
        is the decision model the server reports, so a mismatch with the configured one shows
        in ``list_models`` rather than in every job."""
        if not ep.decision_url:
            return {"up": False, "models": [], "router": True, "error": "no decision_url"}
        t0 = time.monotonic()
        url = f"{ep.decision_url.rstrip('/')}/health"
        try:
            async with httpx.AsyncClient(timeout=self.defaults.probe_timeout_s, headers=ep.auth_headers()) as c:
                r = await c.get(url)
        except Exception as e:  # noqa: BLE001
            return {"up": False, "models": [], "router": True, **classify_connect_failure(e, url)}
        if r.status_code != 200:
            return {"up": False, "models": [], "router": True, "error": f"health {r.status_code}"}
        try:
            body = r.json()
        except ValueError:
            body = None
        reported = body.get("model") if isinstance(body, dict) else None
        out = {
            "up": True,
            "models": [ep.name],
            "router": True,
            "decision_model": reported or ep.decision_model,
            "context": {},
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }
        if reported and reported != ep.decision_model:
            out["decision_model_mismatch"] = True
            out["hint"] = f"the d3 server reports {reported}; the entry says {ep.decision_model}"
        return out

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
                    # the cache_config_info labels: the engine's KV capacity in tokens and the
                    # concurrency that capacity admits at full length — the router's capacity model
                    for key in ("kv_cache_size_tokens", "kv_cache_max_concurrency"):
                        if key in mt:
                            load[key] = mt[key]
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

    def pool_members(self, name: str) -> list[Endpoint]:
        """The entries whose ``pool:`` is ``name``, in config order: the endpoints one pool's
        cap counts sessions across (router 11). Invalid entries stay members: a session admitted
        while the entry was good still occupies the shared hardware. Router entries hold no
        sessions and are never members. An undeclared name has no members (load reset them)."""
        return [e for e in self.endpoints.values() if e.pool == name and not e.is_router]

    def real_endpoints(self) -> list[Endpoint]:
        """The entries that serve ``/v1/messages`` themselves, in config order.

        An entry validate marked invalid is not among them: it serves nothing until fixed."""
        return [e for e in self.endpoints.values() if not e.is_router and not e.invalid]

    # ---------- resolution ----------
    @staticmethod
    def match(name: str, served_ids: list[str], exact_only: set[str] | None = None) -> str | None:
        """Served id matching ``name``: exact, then fnmatch glob, then fuzzy.

        Fuzzy = alphanumerics only, case-folded, name must be a substring of the id — so
        ``qwen3.8-27b`` matches ``unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL``.

        ``exact_only`` holds router entries' names: an id in it answers to nothing but an
        exact request, and a request naming one returns nothing but that exact id. A router
        entry is picked by naming it, so ``auto`` must never reach a real server whose id
        happens to contain it, and no glob or substring must reach the router's name.
        """
        exact = exact_only or set()
        for sid in served_ids:
            if sid == name:
                return sid
        if name in exact:
            return None
        for sid in served_ids:
            if sid not in exact and fnmatch.fnmatchcase(sid, name):
                return sid
        key = _squash(name)
        if len(key) < 3:
            return None
        for sid in served_ids:
            if sid not in exact and key in _squash(sid):
                return sid
        return None

    async def resolve(
        self, model: str | None = None, endpoint: str | None = None, real_only: bool = False
    ) -> "Resolution":
        """Find a live endpoint serving ``model`` (served id, glob or fuzzy name).

        With no model (and no ``defaults.model``) the first live endpoint that is serving
        anything wins, in config order. Raises ModelUnavailable with a human-facing hint
        when nothing fits.

        A router entry is a candidate only when ``model`` names it exactly or ``endpoint``
        names it: with nothing named it is skipped (a router is never a "first live"
        default), and its name never answers to a glob or substring. ``real_only`` drops
        router entries from the candidates entirely (``local_complete`` has no routing
        layer — it posts straight to ``/v1/messages``).

        An entry validate marked invalid is never a candidate, and naming it with
        ``endpoint=`` answers with its problem rather than resolving to a broken entry.
        """
        if endpoint and endpoint not in self.endpoints:
            raise ModelUnavailable(
                f"Unknown endpoint '{endpoint}'. Known: {sorted(self.endpoints)}",
                known_endpoints=sorted(self.endpoints),
            )

        model = model or self.defaults.model
        if endpoint:
            pinned = self.endpoints[endpoint]
            if pinned.invalid:
                raise ModelUnavailable(
                    f"Endpoint '{endpoint}' is not usable: {pinned.invalid}. Fix models.yaml.",
                    model=model,
                )
            if pinned.is_router and real_only:
                raise ModelUnavailable(
                    f"Endpoint '{endpoint}' is a router entry: it picks an endpoint for a job, "
                    "it does not serve completions itself. Name a real endpoint, or a model.",
                    model=model,
                )
            candidates = [pinned]
        elif model:
            candidates = [e for e in self.endpoints.values()
                          if not e.invalid and (not e.is_router or e.name == model)]
        else:
            candidates = [e for e in self.endpoints.values() if not e.invalid and not e.is_router]
        if real_only:
            candidates = [e for e in candidates if not e.is_router]
            # a config of nothing but routers: the caller (local_complete) needs something
            # that serves /v1/messages, and the generic "no endpoint is up" hint would send
            # the operator to check servers that do not exist here (M1)
            if not candidates and self.endpoints and all(e.is_router for e in self.endpoints.values()):
                raise ModelUnavailable(
                    "only router entries are configured; name a real endpoint or add one",
                    model=model,
                )
        router_names = {e.name for e in self.endpoints.values() if e.is_router}
        probes = await asyncio.gather(*(self.probe(e) for e in candidates))
        for ep, pr in zip(candidates, probes):
            if not pr["up"] or not pr["models"]:
                continue
            if ep.is_router:
                return Resolution.make(ep, ep.name, pr)  # a candidate only by exact name (above)
            served = self.match(model, pr["models"], exact_only=router_names) if model else pr["models"][0]
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


_PROM_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"')


def parse_metrics(text: str) -> dict[str, float]:
    """Prometheus text -> {short_name: value}; labelled series (vLLM) are summed per metric.

    ``vllm:cache_config_info`` is the exception: its value is a constant, the data is in
    its labels, and two of those labels (KV capacity in tokens and the concurrency it
    admits) become plain short-name entries rather than sums."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition(" ")
        if "{" in name:  # name{labels} value
            lo, hi = name.index("{"), line.rfind("}")
            name, _, _ = name.partition("{")
            rest = line[hi + 1:].strip()
            if name == "vllm:cache_config_info":
                labels = dict(_PROM_LABEL.findall(line[lo:hi + 1]))
                for key in ("kv_cache_size_tokens", "kv_cache_max_concurrency"):
                    if key in labels:
                        try:
                            out[key] = float(labels[key])
                        except ValueError:
                            pass
                continue
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
