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


# --- Story 2.3a row 3: a timeout that ANSWERED the connection proves the box up (ruling C) ---


def test_read_timeout_proves_the_box_up():
    """The TCP connect succeeded, so the box is demonstrably up; the endpoint is stalled.
    Words about hosts being off were wrong for this row (review 2.3 probe 2)."""
    out = classify_connect_failure(httpx.ReadTimeout("timed out"), URL)
    assert out["host"] == "up"
    assert "10.0.0.60:8000" in out["error"]
    assert "answered the connection but did not respond in time" in out["error"]
    assert "stalled or overloaded" in out["error"]
    assert "firewall" not in out["error"]


def test_write_timeout_proves_the_box_up():
    """Symmetric to read: the body upload stalled on a connection the box accepted."""
    out = classify_connect_failure(httpx.WriteTimeout("timed out"), URL)
    assert out["host"] == "up"
    assert "answered the connection but did not respond in time" in out["error"]


def test_connect_and_pool_timeouts_stay_unknown():
    """No connection was ever established (connect timeout) or no connection was ever
    borrowed (pool timeout — the local pool is tiny, so this is a local stall, not the
    box's answer): both stay unknown with the existing ambiguous-timeout sentence."""
    for exc in (httpx.ConnectTimeout("timed out"), httpx.PoolTimeout("timed out")):
        out = classify_connect_failure(exc, URL)
        assert out["host"] == "unknown", type(exc).__name__
        assert out["error"] == "10.0.0.60:8000 timed out (host off, or a firewall dropping packets)"


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
