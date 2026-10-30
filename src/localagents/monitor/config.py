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
    refresh_s: float = 5.0  # story 3.7m: the one refresh clock's period (the snapshot push)


def load_settings(path: Path | None = None) -> MonitorSettings:
    """The top-level ``monitor:`` mapping of ``path`` (default: ``registry.config_path()``).

    A missing file, a missing section or a missing key is the default. An unknown key is a
    ``ValueError`` naming it: unlike the MCP server, which ignores unknown top-level keys,
    a typo inside the section is the operator's mistake and must fail loudly, not be
    silently ignored. The same applies to values (review 2.3, ruling 2026-10-29): a quoted
    ``attention_hours`` once reached ``collect.run`` and killed the monitor with a TypeError
    at the first sleep, so every key's type and range is checked here — the one place that
    reads the section — and a bad one is a ``ValueError`` naming the key and the value."""
    raw = _read_yaml(Path(path) if path is not None else config_path())
    section = raw.get("monitor")
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("the monitor: section must be a mapping of key: value")
    known = ("port", "attention_hours", "db", "retention_days", "refresh_s")
    unknown = set(section) - set(known)
    if unknown:
        raise ValueError(f"unknown monitor: key(s): {', '.join(sorted(unknown))}")
    port = section.get("port", MonitorSettings.port)
    attention_hours = section.get("attention_hours", MonitorSettings.attention_hours)
    db = section.get("db")
    retention_days = section.get("retention_days", MonitorSettings.retention_days)
    refresh_s = section.get("refresh_s", MonitorSettings.refresh_s)
    # bool is an int subclass in Python: True would load as port 1 / window 3600 s, never
    # what ``port: true`` or ``attention_hours: true`` was meant to say.
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"monitor: port must be an int in 1..65535, got {port!r}")
    if (isinstance(attention_hours, bool) or not isinstance(attention_hours, (int, float))
            or attention_hours <= 0):
        # <= 0 would silently switch off the down-and-recent alert state (spec §4): a
        # zero-width attention window is a configuration error, not a disabled feature.
        raise ValueError(f"monitor: attention_hours must be a number > 0, got {attention_hours!r}")
    if db is not None and not isinstance(db, str):
        raise ValueError(f"monitor: db must be a string or null, got {db!r}")
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 0:
        raise ValueError(f"monitor: retention_days must be an int >= 0, got {retention_days!r}")
    # story 3.7m: the refresh clock's period. Zero or negative would spin the push loop; an
    # hour is the story's stated ceiling (a page that hears nothing for longer is not a
    # refresh clock).
    if (isinstance(refresh_s, bool) or not isinstance(refresh_s, (int, float))
            or not 0 < refresh_s <= 3600):
        raise ValueError(f"monitor: refresh_s must be a number in (0, 3600], got {refresh_s!r}")
    return MonitorSettings(
        port=port,
        attention_hours=attention_hours,
        db=db,
        retention_days=retention_days,
        refresh_s=refresh_s,
    )
