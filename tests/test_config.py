"""Environment configuration.

These paths used to be exercised only as a side effect of `app.py` calling
Config.from_env() at import time. That import-time call is gone, so they are
tested directly.
"""

from pathlib import Path

from burnrate.config import DEFAULT_HOST, DEFAULT_PORT, Config


def test_defaults_when_nothing_is_set(monkeypatch):
    for name in ("BURNRATE_DB", "BURNRATE_HOST", "BURNRATE_PORT", "BURNRATE_POLL_INTERVAL"):
        monkeypatch.delenv(name, raising=False)

    config = Config.from_env()

    assert config.host == DEFAULT_HOST
    assert config.port == DEFAULT_PORT
    assert config.poll_interval == 60.0
    assert config.db_path.name == "burnrate.db"


def test_every_field_is_overridable(monkeypatch):
    monkeypatch.setenv("BURNRATE_DB", "/tmp/custom.db")
    monkeypatch.setenv("BURNRATE_HOST", "127.0.0.1")
    monkeypatch.setenv("BURNRATE_PORT", "9999")
    monkeypatch.setenv("BURNRATE_POLL_INTERVAL", "15.5")

    config = Config.from_env()

    assert config.db_path == Path("/tmp/custom.db")
    assert config.host == "127.0.0.1"
    assert config.port == 9999
    assert config.poll_interval == 15.5


def test_a_tilde_in_the_db_path_is_expanded(monkeypatch):
    monkeypatch.setenv("BURNRATE_DB", "~/somewhere/burnrate.db")

    config = Config.from_env()

    assert "~" not in str(config.db_path)
    assert config.db_path.is_absolute()


def test_garbage_numbers_fall_back_rather_than_crashing_at_boot(monkeypatch):
    """A typo in the plist must not stop the service from starting."""
    monkeypatch.setenv("BURNRATE_PORT", "not-a-port")
    monkeypatch.setenv("BURNRATE_POLL_INTERVAL", "")

    config = Config.from_env()

    assert config.port == DEFAULT_PORT
    assert config.poll_interval == 60.0


def test_the_default_binding_is_lan_reachable():
    """0.0.0.0 is deliberate -- the dashboard is meant to be read from other
    machines on the tailnet. Pinned so it cannot be narrowed by accident."""
    assert Config().host == "0.0.0.0"
    assert Config().port == 8377
