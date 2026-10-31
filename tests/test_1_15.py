"""Story 1.15: a worktree never opens the real state directory.

The 2026-10-30 22:44 incident: a lane's worktree code migrated the operator's real database to v9
and renamed 78 real job directories before the story had landed or been reviewed. The conftests'
``XDG_STATE_HOME``/``LOCALAGENTS_DB`` redirect is a convention a subprocess can drop; this guard
is in the code. A package whose own file sits under a ``worktrees`` directory (a lane's
``~/.local/state/localagents/worktrees/<repo>/<id>`` or an Agent's ``.claude/worktrees/agent-*``)
refuses the DEFAULT state directory; with ``XDG_STATE_HOME`` set it behaves as before. The
operator's install (the main checkout) has no such path component and is unaffected."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from localagents import db

LANE = Path("/home/x/.local/state/localagents/worktrees/claudelocalmcp/0123abcd-0000-4000-8000-000000000000/src/localagents/db.py")
AGENT = Path("/home/x/projects/claudelocalmcp/.claude/worktrees/agent-abc123/src/localagents/db.py")
MAIN = Path("/home/x/projects/claudelocalmcp/src/localagents/db.py")

# The worktree checkout this test suite runs from: its db.py carries the `worktrees` path
# component, so a subprocess that imports it (with no XDG_STATE_HOME) is the 22:44 incident's
# case. The in-process tests above monkeypatch db.PACKAGE_FILE to a fake path; these two run
# the REAL package from the REAL worktree, so the guard reads the real path.
_SRC = Path(__file__).resolve().parent.parent / "src"


def _run_subprocess(child_code: str, env: dict) -> subprocess.CompletedProcess:
    """The child runs this worktree's python (``sys.executable``) with ``cwd=src`` and the
    worktree's src forced to the front of ``sys.path`` in the child, so ``import localagents``
    resolves to THIS checkout's db.py (not a monkeypatched one). The env is built by the
    test: the refusal case strips XDG_STATE_HOME/LOCALAGENTS_DB so the DEFAULT state
    directory is the case, and points HOME at a temp dir so a regression never touches the
    operator's home."""
    return subprocess.run(
        [sys.executable, "-c", child_code],
        capture_output=True, text=True,
        cwd=str(_SRC), env=env,
    )


def test_the_subprocess_route_also_refuses_the_default_state_directory(tmp_path):
    """Story 1.15's reach through a REAL import (the audit's too-narrow finding): the in-process
    tests monkeypatch db.PACKAGE_FILE, but the 22:44 incident was a SUBPROCESS that dropped the
    redirect — the guard's reach through a real route was never exercised. The child runs this
    worktree's package with XDG_STATE_HOME and LOCALAGENTS_DB unset; the guard fires in the
    child (not the test process), so the child exits non-zero and its stderr carries the
    refusal. A self-validating test: if the child ever loaded a non-worktree db.py, state_dir()
    would succeed (exit 0) and the returncode assertion below would fail. HOME is a temp dir,
    so even a guard regression never touches the operator's real home."""
    env = {k: v for k, v in os.environ.items() if k not in ("XDG_STATE_HOME", "LOCALAGENTS_DB")}
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(_SRC)
    code = (
        "import sys; sys.path.insert(0, r'" + str(_SRC) + "')\n"
        "from localagents import db\n"
        # the lane that wrote this ran from a worktree, where the real path carries the mark; on the
        # operator's main checkout it does not, so the child points the guard at a worktree-shaped
        # path when its own is not one — the ROUTE under test (a fresh interpreter, the env, the
        # import) is the same either way
        "from pathlib import Path\n"
        "if 'worktrees' not in db.PACKAGE_FILE.parts: db.PACKAGE_FILE = Path('/w/worktrees/x/src/localagents/db.py')\n"
        "db.state_dir()  # must raise: the worktree guard (XDG_STATE_HOME unset in the child)\n"
    )
    out = _run_subprocess(code, env)
    assert out.returncode != 0, out.stderr
    assert "worktree checkout" in out.stderr, out.stderr
    assert not (tmp_path / ".local" / "state" / "localagents").exists()


def test_the_subprocess_route_works_when_redirected(tmp_path):
    """The redirect is the operator's escape: with XDG_STATE_HOME set, a worktree subprocess
    behaves as before — state_dir() returns the redirected directory and nothing is refused.
    The child runs the same worktree package with XDG_STATE_HOME pointed at a temp dir."""
    env = {k: v for k, v in os.environ.items() if k not in ("XDG_STATE_HOME", "LOCALAGENTS_DB")}
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(_SRC)
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    code = (
        "import sys; sys.path.insert(0, r'" + str(_SRC) + "')\n"
        "from localagents import db\n"
        "print(db.state_dir())  # must NOT raise: XDG_STATE_HOME is set in the child\n"
    )
    out = _run_subprocess(code, env)
    assert out.returncode == 0, out.stderr
    assert str(tmp_path / "state" / "localagents") in out.stdout


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
