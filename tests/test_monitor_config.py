"""Story 2.3 item 4: the ``monitor:`` section of models.yaml (monitor spec §3). The MCP
server's ``Registry.load`` reads only ``endpoints`` and ``defaults`` and ignores the section;
``monitor.config`` is the monitor's own loader, through the registry's yaml reader."""
import pytest

from localagents.monitor import config
from localagents.registry import Registry


def _write(tmp_path, text):
    p = tmp_path / "models.yaml"
    p.write_text(text)
    return p


def test_a_full_section(tmp_path):
    p = _write(tmp_path, "monitor:\n  port: 9000\n  attention_hours: 12\n  db: /tmp/fleet.db\n  retention_days: 3\n")
    s = config.load_settings(p)
    assert (s.port, s.attention_hours, s.db, s.retention_days) == (9000, 12.0, "/tmp/fleet.db", 3)


def test_missing_keys_are_the_defaults(tmp_path):
    p = _write(tmp_path, "monitor:\n  port: 9000\n")
    s = config.load_settings(p)
    assert s.port == 9000
    assert s.attention_hours == 24.0
    assert s.db is None
    assert s.retention_days == 7


def test_no_section_is_all_defaults(tmp_path):
    p = _write(tmp_path, "endpoints:\n  x:\n    base_url: http://x:8080\n")
    s = config.load_settings(p)
    assert (s.port, s.attention_hours, s.db, s.retention_days) == (8765, 24.0, None, 7)


def test_a_missing_file_is_all_defaults(tmp_path):
    s = config.load_settings(tmp_path / "nope.yaml")
    assert (s.port, s.attention_hours, s.db, s.retention_days) == (8765, 24.0, None, 7)


def test_an_unknown_key_names_it(tmp_path):
    p = _write(tmp_path, "monitor:\n  portt: 80\n")
    with pytest.raises(ValueError, match="portt"):
        config.load_settings(p)


def test_registry_load_ignores_the_monitor_section(tmp_path):
    """Registry.load reads only endpoints and defaults: a monitor: section changes nothing
    for the MCP server (spec §3), and the Registry has no monitor field to read it into."""
    p = _write(tmp_path, "monitor:\n  port: 9000\n  attention_hours: 12\nendpoints:\n  x:\n    base_url: http://x:8080\ndefaults:\n  model: some-model\n")
    reg = Registry.load(p)
    assert reg.defaults.model == "some-model"
    assert set(reg.endpoints) == {"x"}
    assert "monitor" not in vars(reg)
