"""Story 2.2 test support: the story-2.1 captures under tests/fixtures/monitor/, served as HTTP.

Every monitor parser and rate test runs over these real documents, never a typed one (monitor
spec §7). ``serve`` answers ``httpx.AsyncClient.get`` from a route table of capture files, so
``Registry.probe`` and ``fetch_metrics_doc`` run their real code over the captured bytes and no
test touches the network.
"""
from __future__ import annotations

import calendar
import errno
import json
import time
from pathlib import Path

import httpx

MONITOR = Path(__file__).parent / "fixtures" / "monitor"
_MANIFEST = {r["path"]: r for r in json.loads((MONITOR / "manifest.json").read_text())}


def raw(path: str) -> bytes:
    return (MONITOR / path).read_bytes()


def doc(path: str) -> dict:
    return json.loads(raw(path))


def captured_at(path: str) -> float:
    """The manifest's ``captured_at`` (UTC, whole seconds) as epoch seconds."""
    return float(calendar.timegm(time.strptime(_MANIFEST[path]["captured_at"], "%Y-%m-%dT%H:%M:%SZ")))


def dt(first: str, second: str) -> float:
    return captured_at(second) - captured_at(first)


def serve(monkeypatch, base_url: str, routes: dict[str, str | None]) -> list[str]:
    """Answer GETs under ``base_url`` with the capture file named per path (status 200), a
    404 for an unrouted path, and a refused connection for a route mapped to None: an
    ``httpx.ConnectError`` chained from the kernel's ECONNREFUSED, as httpx raises it, so
    ``classify_connect_failure`` reads the errno. Returns the paths asked for, in order."""
    seen: list[str] = []

    async def get(self, url, **kw):
        req = httpx.Request("GET", url)
        path = url[len(base_url):]
        seen.append(path)
        if path not in routes:
            return httpx.Response(404, json={"error": "not found"}, request=req)
        name = routes[path]
        if name is None:
            try:
                raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
            except OSError as e:
                raise httpx.ConnectError("All connection attempts failed", request=req) from e
        return httpx.Response(200, content=raw(name), request=req)

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    return seen


# Route tables per captured endpoint, pass 1 (``_1``) of story 2.1's two passes. The vLLM box
# has no /props capture: the prober's vLLM branch never asks for it (max_model_len decides).
STRATA_AI2 = {
    "/v1/models": "strata/ai2_models_1.json",
    "/props": "strata/ai2_props_1.json",
    "/slots": "strata/ai2_slots_1.json",
    "/metrics": "strata/ai2_metrics_1.json",
    "/health": "strata/ai2_health_1.json",
}
VLLM_DGX1A = {
    "/v1/models": "vllm/dgx1-a_models_1.json",
    "/metrics": "vllm/dgx1-a_metrics_1.txt",
}
LLAMACPP = {
    "/v1/models": "llamacpp/llamacpp_models_1.json",
    "/props": "llamacpp/llamacpp_props_1.json",
    "/slots": "llamacpp/llamacpp_slots_1.json",
    "/metrics": "llamacpp/llamacpp_metrics_1.txt",
    "/health": "llamacpp/llamacpp_health_1.json",
}
