"""Story 2.2: the MCP server never depends on the monitor (monitor spec §2). A grep of the six
server modules catches a direct import; this catches an indirect one, in a fresh interpreter."""
import subprocess
import sys
from pathlib import Path

_PROBE = """
import sys
import localagents.server, localagents.jobs, localagents.runner, localagents.shim
import localagents.status, localagents.registry, localagents.db
loaded = sorted(m for m in sys.modules if m.startswith("localagents.monitor"))
print(loaded)
sys.exit(1 if loaded else 0)
"""


def test_importing_the_mcp_server_loads_no_monitor_module(tmp_path):
    src = Path(__file__).resolve().parent.parent / "src"
    p = subprocess.run([sys.executable, "-c", _PROBE], cwd=src, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.strip() == "[]"
