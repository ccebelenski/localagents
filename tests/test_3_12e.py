"""Story 3.12e: short failure sentences on the down card (EPICS 3.12).

The operator, of the card's paragraph-long refused sentence: "the display is too verbose -
I don't need a paragaph and explantion line. Just 10.0.0.70 reachable, port 8000 no
response." (the verbatim old line is kept in t/REPORT.md, the story's record).
The sentences get short at the source; the explanation moves to a
``hint`` key the classifier adds. The hint reaches ``list_models`` and ``ModelUnavailable``
through ``Registry.probe``'s spread, but never the database or the page: the probes row is
built from ``rows.PROBE_COLUMNS``, so an extra key in the probe dict is ignored.
"""
import asyncio
import errno

import httpx
import pytest

from localagents import db
from localagents.monitor import collect, rows, seed
from localagents.registry import Defaults, Endpoint, Registry, classify_connect_failure

from monitor_fixtures import serve

URL = "http://10.0.0.60:8000"


def _wrapped(oserr: OSError) -> httpx.ConnectError:
    """Mimic httpx: ConnectError -> ConnectError -> OSError (test_probe.py's chain)."""
    inner = httpx.ConnectError("All connection attempts failed")
    inner.__cause__ = oserr
    outer = httpx.ConnectError("All connection attempts failed")
    outer.__cause__ = inner
    return outer


def _refused() -> httpx.ConnectError:
    return _wrapped(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))


# --- the four sentences, verbatim (the operator's form for the refused case) ----------------


def test_refused_is_the_operators_short_sentence():
    out = classify_connect_failure(_refused(), URL)
    assert out["host"] == "up"
    assert out["error"] == "10.0.0.60 reachable, port 8000 no response"


def test_refused_with_no_port_in_the_url():
    out = classify_connect_failure(_refused(), "http://10.0.0.60")
    assert out["host"] == "up"
    assert out["error"] == "10.0.0.60 reachable, no response"


def test_unreachable_sentence_keeps_its_shape():
    out = classify_connect_failure(_wrapped(OSError(errno.EHOSTUNREACH, "No route to host")), URL)
    assert out["host"] == "down"
    assert out["error"] == "10.0.0.60 is unreachable (EHOSTUNREACH)"


def test_read_timeout_short_sentence():
    out = classify_connect_failure(httpx.ReadTimeout("timed out"), URL)
    assert out["host"] == "up"
    assert out["error"] == "10.0.0.60:8000 connected, no reply in time"


def test_write_timeout_short_sentence():
    out = classify_connect_failure(httpx.WriteTimeout("timed out"), URL)
    assert out["host"] == "up"
    assert out["error"] == "10.0.0.60:8000 connected, no reply in time"


def test_other_timeout_short_sentence():
    out = classify_connect_failure(httpx.ConnectTimeout("timed out"), URL)
    assert out["host"] == "unknown"
    assert out["error"] == "10.0.0.60:8000 timed out"


# --- the hint: present on the four, absent on the fallback -----------------------------------


def test_hint_on_the_four_classified_cases():
    for exc, hint in (
        (_refused(), "nothing started there, or the port in models.yaml is wrong"),
        (_wrapped(OSError(errno.EHOSTUNREACH, "No route to host")), "box off, or unroutable from here"),
        (httpx.ReadTimeout("timed out"), "the engine is stalled or overloaded"),
        (httpx.ConnectTimeout("timed out"), "host off, or a firewall dropping packets"),
    ):
        out = classify_connect_failure(exc, URL)
        assert out["hint"] == hint, type(exc).__name__


def test_fallback_carries_no_hint():
    out = classify_connect_failure(ValueError("bad json"), URL)
    assert out == {"host": "unknown", "error": "ValueError: bad json"}


# --- the hint spreads into the probe result, not into the probes row -------------------------


def _refused_probe(monkeypatch) -> dict:
    serve(monkeypatch, URL, {"/v1/models": None})  # the fixture's chained ECONNREFUSED route
    reg = Registry(path=None, endpoints={}, defaults=Defaults(), raw={}, local={})
    return asyncio.run(reg.probe(Endpoint(name="dead", base_url=URL, backend="llama.cpp")))


def test_registry_probe_carries_error_and_hint(monkeypatch):
    """The spread at registry.py:235 passes every classifier key through, so this is already
    what list_models shows and what ModelUnavailable carries as endpoints= (registry.py:378)."""
    out = _refused_probe(monkeypatch)
    assert out["up"] is False and out["models"] == [] and out["host"] == "up"
    assert out["error"] == "10.0.0.60 reachable, port 8000 no response"
    assert out["hint"] == "nothing started there, or the port in models.yaml is wrong"


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "m.db")
    yield c
    c.close()


def test_the_hint_is_ignored_by_the_rows_and_the_probes_row_keeps_the_short_sentence(monkeypatch, conn):
    """probe_to_rows builds the row from its own column list: the extra hint key in the probe
    dict is ignored — not stored, not an error (rows.insert would raise on an unknown column
    in the row dict; the stripped row proves it never gets one)."""
    probe = _refused_probe(monkeypatch)
    assert "hint" in probe  # the extra key this test is about
    r = rows.probe_to_rows(3, None, probe, None, 1793288065.0)
    assert "hint" not in r.probe
    assert r.probe["error"] == "10.0.0.60 reachable, port 8000 no response"
    ep = Endpoint(name="dead", base_url=URL, backend="llama.cpp")
    r.probe["endpoint_id"] = collect.endpoint_id(conn, ep)
    with db.immediate(conn):
        pid = rows.insert(conn, r)
    row = conn.execute("SELECT error FROM probes WHERE id = ?", (pid,)).fetchone()
    assert row["error"] == "10.0.0.60 reachable, port 8000 no response"


def test_the_seeded_world_builds_the_short_refused_sentence():
    assert seed._refused_sentence("http://10.0.0.70:8000") == "10.0.0.70 reachable, port 8000 no response"
