import asyncio
import errno

import httpx
import pytest

from localagents.registry import Defaults, Endpoint, Registry, classify_connect_failure

URL = "http://10.0.0.60:8000"


def _wrapped(oserr: OSError) -> httpx.ConnectError:
    """Mimic httpx: ConnectError -> ConnectError -> OSError -> ConnectionRefusedError."""
    inner = httpx.ConnectError("All connection attempts failed")
    inner.__cause__ = oserr
    outer = httpx.ConnectError("All connection attempts failed")
    outer.__cause__ = inner
    return outer


def test_refused_means_host_up_wrong_port():
    out = classify_connect_failure(_wrapped(ConnectionRefusedError(errno.ECONNREFUSED, "refused")), URL)
    assert out["host"] == "up"
    assert "10.0.0.60" in out["error"] and ":8000" in out["error"] and "nothing is listening" in out["error"]


def test_unreachable_means_host_down():
    out = classify_connect_failure(_wrapped(OSError(errno.EHOSTUNREACH, "No route to host")), URL)
    assert out["host"] == "down"
    assert "EHOSTUNREACH" in out["error"]


def test_timeout_is_unknown():
    out = classify_connect_failure(httpx.ConnectTimeout("timed out"), URL)
    assert out["host"] == "unknown"
    assert "timed out" in out["error"]


def test_other_errors_fall_through_verbatim():
    out = classify_connect_failure(ValueError("bad json"), URL)
    assert out == {"host": "unknown", "error": "ValueError: bad json"}


def test_probe_reports_host_state(monkeypatch):
    async def boom(self, url):
        raise _wrapped(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))

    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    reg = Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={})
    out = asyncio.run(reg.probe(Endpoint(name="ai2", base_url=URL, backend="llama.cpp")))
    assert out["up"] is False and out["models"] == [] and out["host"] == "up"
