"""The ``monitor:`` section of models.yaml (monitor spec §3): the monitor's own settings,
read through the registry's yaml reader. The MCP server never reads this section:
``Registry.load`` takes only the ``endpoints`` and ``defaults`` mappings and ignores every
other top-level key (registry.py), so the section is monitor-only by construction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..registry import _read_yaml, config_path


@dataclass
class MonitorSettings:
    port: int = 8765
    attention_hours: float = 24.0  # spec §4: the attention window's default
    db: str | None = None  # overrides db.db_path() when set
    retention_days: int = 7  # spec §3 "Storage": seven days of probes


def load_settings(path: Path | None = None) -> MonitorSettings:
    """The top-level ``monitor:`` mapping of ``path`` (default: ``registry.config_path()``).

    A missing file, a missing section or a missing key is the default. An unknown key is a
    ``ValueError`` naming it: unlike the MCP server, which ignores unknown top-level keys,
    a typo inside the section is the operator's mistake and must fail loudly, not be
    silently ignored."""
    raw = _read_yaml(Path(path) if path is not None else config_path())
    section = raw.get("monitor")
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("the monitor: section must be a mapping of key: value")
    known = ("port", "attention_hours", "db", "retention_days")
    unknown = set(section) - set(known)
    if unknown:
        raise ValueError(f"unknown monitor: key(s): {', '.join(sorted(unknown))}")
    return MonitorSettings(
        port=section.get("port", MonitorSettings.port),
        attention_hours=section.get("attention_hours", MonitorSettings.attention_hours),
        db=section.get("db"),
        retention_days=section.get("retention_days", MonitorSettings.retention_days),
    )
