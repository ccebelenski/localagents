"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def fresh_server_store(request, monkeypatch, tmp_path):
    """`localagents.server.store` is module-level and caches its sqlite connection, which belongs
    to the state directory open on first use. For any test module that imported the server, point
    XDG_STATE_HOME at this test's directory and install a fresh store, so no test writes into
    another test's database (DeepSeek review of 1.2, finding 4)."""
    import sys

    # Both database variables, unconditionally: db_path() prefers LOCALAGENTS_DB, so a developer
    # with it exported would otherwise have the suite read (and write) the real database.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAGENTS_DB", str(tmp_path / "localagents" / "localagents.db"))
    # and the config: config_path() falls back to ./models.yaml or ~/.config/localagents, and
    # register_endpoint writes models.local.yaml beside it (review of 1.7, item 13)
    cfg = tmp_path / "models.yaml"
    cfg.write_text("")
    monkeypatch.setenv("LOCALAGENTS_CONFIG", str(cfg))
    server = sys.modules.get("localagents.server")
    if server is None:
        yield
        return
    from localagents import jobs

    server.store = jobs.JobStore()
    server._shim_port_recorded = False
    yield
