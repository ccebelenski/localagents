"""The router tests' fixtures.

The three env redirects are the ones ``tests/conftest.py``'s autouse fixture sets; they are
restated here so this directory names its own isolation (a lane must never open the real
database from a worktree checkout — story 1.15)."""

import pytest

from localagents import db


def pytest_configure(config):
    """router 4: the ``live`` marker is registered here. pyproject.toml's [tool.pytest.ini_options]
    section sets testpaths only; a marker can be registered in either place and this one stays."""
    config.addinivalue_line("markers",
                            "live: runs against a running d3 server (LOCALAGENTS_D3_URL)")


@pytest.fixture(autouse=True)
def env_redirects(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAGENTS_DB", str(tmp_path / "localagents" / "localagents.db"))
    cfg = tmp_path / "models.yaml"
    cfg.write_text("")
    monkeypatch.setenv("LOCALAGENTS_CONFIG", str(cfg))


@pytest.fixture
def conn(tmp_path):
    """A current-schema router file (v10: dimension_definitions and the router tables exist)."""
    c = db.open_db(tmp_path / "r.db")
    yield c
    c.close()
