"""The suite never reads or writes the developer's real state, database or config (conftest)."""

from localagents import db, jobs
from localagents.registry import config_path, local_path


def test_every_path_the_code_resolves_is_under_tmp(tmp_path):
    """Review of 1.7, item 13: conftest pins XDG_STATE_HOME, LOCALAGENTS_DB and LOCALAGENTS_CONFIG
    (an empty models.yaml) per test, so a register_endpoint or a config read in any test lands in
    tmp_path, never ~/.config/localagents or ./models.yaml."""
    assert config_path() == tmp_path / "models.yaml"
    assert config_path().read_text() == ""
    assert local_path(config_path()).parent == tmp_path
    assert db.db_path().is_relative_to(tmp_path)
    assert jobs.state_dir().is_relative_to(tmp_path)
