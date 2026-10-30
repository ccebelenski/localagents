"""Safety net for probe files under t/ (lanes' review probes live here, outside tests/, so
tests/conftest.py never applies to them): every probe runs against a fresh state directory,
database and config, never the real ones. Added after review 3.9B's probe forgot its fixture
and `prune(1)` deleted the real jobs directory (EPICS 3.9B incident, 2026-10-30)."""
import pytest


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAGENTS_DB", str(tmp_path / "localagents" / "localagents.db"))
    cfg = tmp_path / "models.yaml"
    cfg.write_text("")
    monkeypatch.setenv("LOCALAGENTS_CONFIG", str(cfg))
    yield
