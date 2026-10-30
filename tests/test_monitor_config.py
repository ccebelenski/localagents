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


# --- Story 2.3a row 1: values are validated, not just keys (review 2.3 probe 3) ---


def test_quoted_attention_hours_is_a_config_error(tmp_path):
    """attention_hours: "24" — a quoted number used to load as the string '24', whose
    * 3600 is string repetition and kills collect.run at the first sleep."""
    p = _write(tmp_path, "monitor:\n  attention_hours: \"24\"\n")
    with pytest.raises(ValueError, match=r"attention_hours.*'24'"):
        config.load_settings(p)


def test_attention_hours_must_be_positive(tmp_path):
    """0 and negatives silently delete the down-and-recent alert state (a zero window):
    ruling 2026-10-29 — a configuration error."""
    for bad in ("0", "-5"):
        p = _write(tmp_path, f"monitor:\n  attention_hours: {bad}\n")
        with pytest.raises(ValueError, match=rf"attention_hours.*{bad}"):
            config.load_settings(p)


def test_a_fractional_attention_hours_loads(tmp_path):
    p = _write(tmp_path, "monitor:\n  attention_hours: 0.5\n")
    assert config.load_settings(p).attention_hours == 0.5


def test_quoted_port_is_a_config_error(tmp_path):
    """yaml users write port: "8765"; uvicorn tolerates numeric strings and dies on
    anything else with a socket.gaierror at startup — the error belongs here."""
    p = _write(tmp_path, "monitor:\n  port: \"8765\"\n")
    with pytest.raises(ValueError, match=r"port.*'8765'"):
        config.load_settings(p)


def test_port_must_be_in_range(tmp_path):
    for bad in ("70000", "0"):
        p = _write(tmp_path, f"monitor:\n  port: {bad}\n")
        with pytest.raises(ValueError, match=rf"port.*{bad}"):
            config.load_settings(p)


def test_retention_days_must_be_an_int(tmp_path):
    p = _write(tmp_path, "monitor:\n  retention_days: 2.5\n")
    with pytest.raises(ValueError, match=r"retention_days.*2\.5"):
        config.load_settings(p)


def test_db_must_be_a_string_or_null(tmp_path):
    p = _write(tmp_path, "monitor:\n  db: 3\n")
    with pytest.raises(ValueError, match=r"db.*\b3\b"):
        config.load_settings(p)


def test_refresh_s_loads_and_defaults_to_five_seconds(tmp_path):
    """Story 3.7m: the refresh clock's period is a monitor: setting; the default is 5 s."""
    p = _write(tmp_path, "monitor:\n  refresh_s: 0.5\n")
    assert config.load_settings(p).refresh_s == 0.5
    assert config.load_settings(_write(tmp_path, "monitor:\n  port: 9000\n")).refresh_s == 5.0


def test_refresh_s_must_be_a_positive_number_within_an_hour(tmp_path):
    for bad in ("0", "-1", "3601", '"5"', "true"):
        p = _write(tmp_path, f"monitor:\n  refresh_s: {bad}\n")
        with pytest.raises(ValueError, match=r"refresh_s"):
            config.load_settings(p)


def test_a_valid_section_still_loads(tmp_path):
    """The validation addition must not touch valid values — ints, a float, null, a str."""
    p = _write(tmp_path, "monitor:\n  port: 9000\n  attention_hours: 12.5\n"
                        "  db: /srv/fleet.db\n  retention_days: 0\n")
    s = config.load_settings(p)
    assert (s.port, s.attention_hours, s.db, s.retention_days) == (9000, 12.5, "/srv/fleet.db", 0)


def test_registry_load_ignores_the_monitor_section(tmp_path):
    """Registry.load reads only endpoints and defaults: a monitor: section changes nothing
    for the MCP server (spec §3), and the Registry has no monitor field to read it into."""
    p = _write(tmp_path, "monitor:\n  port: 9000\n  attention_hours: 12\nendpoints:\n  x:\n    base_url: http://x:8080\ndefaults:\n  model: some-model\n")
    reg = Registry.load(p)
    assert reg.defaults.model == "some-model"
    assert set(reg.endpoints) == {"x"}
    assert "monitor" not in vars(reg)
