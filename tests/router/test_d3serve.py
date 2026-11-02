"""router 4 (docs/superpowers/specs/2026-11-02-router-design.md §3, the d3 server row):
``localagents d3 serve``, the package's own small server for a pinned Decision 3.0 model.
``GET /health`` is the router entry's probe; ``POST /v1/systemone`` answers typed questions
over ``AutoModel.system_one``.

The unit tests run against a fake model: torch and transformers are the router extra (a pip
install away), never a test dependency, and the model's Python interface is one method
(``system_one(state=<text>, questions=<dict>)``, docs/spikes/2026-11-01-d3-categorizer.md).
The live contract test is test_d3serve_live.py (the ``live`` marker, LOCALAGENTS_D3_URL)."""

import builtins
import sys
import threading
import time

import pytest
from starlette.testclient import TestClient

import localagents
import localagents.server
from localagents.router import d3serve

MODEL_ID = "vllm-sr/d3-mini"
DEVICE = "cuda:0"


class FakeModel:
    """The model's Python interface, recorded: ``system_one`` takes the state and the
    questions and answers one noul question, with the usage the client parses."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def system_one(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"answers": {"q": {"type": "noul", "noul": 0.9}}, "usage": {}}


def _client(loader):
    return TestClient(d3serve.app(loader, MODEL_ID, DEVICE))


def _wait_until_loaded(client) -> None:
    """Poll /health until the loader's thread has finished: the load runs in its own thread,
    so the test must not race it."""
    deadline = time.time() + 5
    while True:
        r = client.get("/health")
        if r.status_code == 200:
            return
        assert time.time() < deadline, f"the model never finished loading: {r.status_code} {r.text}"
        time.sleep(0.01)


def test_pins_the_revision_and_max_chars():
    assert d3serve.D3_REVISION == "df99bf4ca4418bc6908ee68e9b7479af1f3b8800"
    assert d3serve.D3_MAX_CHARS == 120_000


def test_health_before_load_is_503():
    gate = threading.Event()

    def loader():
        gate.wait()
        return FakeModel()

    with _client(loader) as c:
        r = c.get("/health")
        assert r.status_code == 503
        assert r.json() == {"ok": False, "loading": True}
    gate.set()  # let the daemon loader thread end


def test_health_after_load_reports_model_and_device():
    gate = threading.Event()

    def loader():
        gate.wait()
        return FakeModel()

    with _client(loader) as c:
        assert c.get("/health").status_code == 503
        gate.set()
        _wait_until_loaded(c)
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "model": MODEL_ID, "device": DEVICE}


def test_systemone_passes_state_and_questions_to_the_model():
    fake = FakeModel()
    with _client(lambda: fake) as c:
        _wait_until_loaded(c)
        body = {"model": MODEL_ID, "state": "fix the flaky test in jobs.py",
                "questions": {"q": {"type": "noul", "instructions": "does the brief require writing no code?"}}}
        r = c.post("/v1/systemone", json=body)
        assert r.status_code == 200
        assert r.json() == {"answers": {"q": {"type": "noul", "noul": 0.9}}, "usage": {}}
        assert fake.calls == [{"state": body["state"], "questions": body["questions"]}]


def test_missing_state_is_400():
    with _client(lambda: FakeModel()) as c:
        _wait_until_loaded(c)
        r = c.post("/v1/systemone", json={"model": MODEL_ID, "questions": {"q": {"type": "noul"}}})
        assert r.status_code == 400
        assert r.json() == {"error": "missing state or questions"}
        # the other missing field is the same one-line refusal
        r = c.post("/v1/systemone", json={"model": MODEL_ID, "state": "a brief"})
        assert r.status_code == 400
        assert r.json() == {"error": "missing state or questions"}


def test_oversize_state_is_413():
    with _client(lambda: FakeModel()) as c:
        _wait_until_loaded(c)
        r = c.post("/v1/systemone", json={"model": MODEL_ID,
                                           "state": "x" * (d3serve.D3_MAX_CHARS + 1),
                                           "questions": {"q": {"type": "noul"}}})
        assert r.status_code == 413
        assert r.json() == {"error": "state too long", "max_chars": 120000}


def test_cli_without_torch_says_install_the_extra(monkeypatch, capsys):
    real_import = builtins.__import__

    def no_torch(name, *args, **kwargs):
        if name == "torch" or name.startswith("torch."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_torch)
    with pytest.raises(SystemExit) as e:
        d3serve.load_model(MODEL_ID, DEVICE)
    assert e.value.code == 2
    assert "install the router extra" in capsys.readouterr().err


def test_cli_branches_d3_router_calibrate_come_before_the_server_fallthrough(monkeypatch, capsys):
    def fallthrough_reached():
        raise AssertionError("an unknown word reached the MCP server fallthrough")

    monkeypatch.setattr(localagents.server, "main", fallthrough_reached)

    monkeypatch.setattr(sys, "argv", ["localagents", "router"])
    with pytest.raises(SystemExit) as e:
        localagents.main()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "not built yet" in err and "phase 1 task 5" in err

    monkeypatch.setattr(sys, "argv", ["localagents", "calibrate"])
    with pytest.raises(SystemExit) as e:
        localagents.main()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "not built yet" in err and "phase 2 task 9" in err

    # d3 without a subcommand: usage on stderr, exit 2, and no fallthrough
    monkeypatch.setattr(sys, "argv", ["localagents", "d3"])
    with pytest.raises(SystemExit) as e:
        localagents.main()
    assert e.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()
