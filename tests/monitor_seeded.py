"""The seeded world (story 2.4) for the story-3.1 read-model and route tests: a database file
seeded at a fixed ``NOW`` and a ``models.yaml`` naming the seed's three endpoints with the exact
keys the seed wrote (name, base_url, backend, host), so the registry and the database agree."""
from pathlib import Path

from localagents import db
from localagents.monitor import seed
from localagents.registry import Registry

FX = Path(__file__).resolve().parent / "fixtures" / "monitor"
NOW = 1793288065.0  # the seed test's fixed clock

MODELS_YAML = """\
endpoints:
  ai1:
    base_url: http://10.0.0.50:8080
    backend: strata
    host: ai1
  dgx1-a:
    base_url: http://10.0.0.70:8000
    backend: vllm
    host: dgx1
  llamacpp:
    base_url: http://127.0.0.1:8080
    backend: llama.cpp
    host: local
"""


def seed_file(dbpath: Path, now: float = NOW) -> None:
    conn = db.open_db(dbpath)
    try:
        seed.seed(conn, now, FX)
    finally:
        conn.close()


def write_config(path: Path, extra: str = "") -> Path:
    path.write_text(MODELS_YAML + extra)
    return path


def registry(tmp_path: Path) -> Registry:
    return Registry.load(write_config(tmp_path / "monitor-models.yaml"))


def endpoint_id(conn, name: str) -> int:
    return conn.execute("SELECT id FROM endpoints WHERE name = ?", (name,)).fetchone()[0]
