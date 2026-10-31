"""Story 1.15: a worktree never opens the real state directory.

The 2026-10-30 22:44 incident: a lane's worktree code migrated the operator's real database to v9
and renamed 78 real job directories before the story had landed or been reviewed. The conftests'
``XDG_STATE_HOME``/``LOCALAGENTS_DB`` redirect is a convention a subprocess can drop; this guard
is in the code. A package whose own file sits under a ``worktrees`` directory (a lane's
``~/.local/state/localagents/worktrees/<repo>/<id>`` or an Agent's ``.claude/worktrees/agent-*``)
refuses the DEFAULT state directory; with ``XDG_STATE_HOME`` set it behaves as before. The
operator's install (the main checkout) has no such path component and is unaffected."""
from pathlib import Path

import pytest

from localagents import db

LANE = Path("/home/x/.local/state/localagents/worktrees/claudelocalmcp/0123abcd-0000-4000-8000-000000000000/src/localagents/db.py")
AGENT = Path("/home/x/projects/claudelocalmcp/.claude/worktrees/agent-abc123/src/localagents/db.py")
MAIN = Path("/home/x/projects/claudelocalmcp/src/localagents/db.py")


@pytest.mark.parametrize("where", [LANE, AGENT])
def test_a_worktree_checkout_refuses_the_default_state_directory(monkeypatch, tmp_path, where):
    monkeypatch.setattr(db, "PACKAGE_FILE", where)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("LOCALAGENTS_DB", raising=False)  # the suite's conftest sets it; the DEFAULT path is the case
    monkeypatch.setenv("HOME", str(tmp_path))  # even the fallback home is never touched
    with pytest.raises(RuntimeError, match="worktree checkout"):
        db.state_dir()
    with pytest.raises(RuntimeError):
        db.open_db()  # the default path derives from state_dir
    assert not (tmp_path / ".local").exists()


@pytest.mark.parametrize("where", [LANE, AGENT])
def test_a_redirected_worktree_checkout_works_as_before(monkeypatch, tmp_path, where):
    monkeypatch.setattr(db, "PACKAGE_FILE", where)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert db.state_dir() == tmp_path / "localagents"
    assert db.db_path() == tmp_path / "localagents" / "localagents.db" or db.db_path().is_absolute()


def test_the_main_checkout_uses_the_default_state_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "PACKAGE_FILE", MAIN)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert db.state_dir() == tmp_path / ".local" / "state" / "localagents"


def test_the_guard_reads_the_path_components_not_a_substring(monkeypatch, tmp_path):
    """``/home/worktrees-user/...`` is not a worktree; the mark is a whole path component."""
    monkeypatch.setattr(db, "PACKAGE_FILE", Path("/home/worktrees-user/projects/x/src/localagents/db.py"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert db.state_dir() == tmp_path / ".local" / "state" / "localagents"
