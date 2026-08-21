"""Environment configuration.

These paths used to be exercised only as a side effect of `app.py` calling
Config.from_env() at import time. That import-time call is gone, so they are
tested directly.
"""

import os
from pathlib import Path

import pytest

from burnrate.config import (
    DEFAULT_ATTRIBUTION_DIR,
    DEFAULT_DB_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
    MAX_POLL_INTERVAL_SECONDS,
    Config,
    print_effective,
)


def test_defaults_when_nothing_is_set(monkeypatch):
    for name in (
        "BURNRATE_DB",
        "BURNRATE_HOST",
        "BURNRATE_PORT",
        "BURNRATE_POLL_INTERVAL",
        "BURNRATE_PROJECTS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)

    config = Config.from_env()

    assert config.host == DEFAULT_HOST
    assert config.port == DEFAULT_PORT
    assert config.poll_interval == 60.0
    assert config.db_path.name == "burnrate.db"
    assert config.attribution_dir == DEFAULT_ATTRIBUTION_DIR
    assert config.attribution_dir == Path.home() / ".claude" / "projects"


def test_every_field_is_overridable(monkeypatch):
    monkeypatch.setenv("BURNRATE_DB", "/tmp/custom.db")
    monkeypatch.setenv("BURNRATE_HOST", "127.0.0.1")
    monkeypatch.setenv("BURNRATE_PORT", "9999")
    monkeypatch.setenv("BURNRATE_POLL_INTERVAL", "15.5")
    monkeypatch.setenv("BURNRATE_PROJECTS_DIR", "/tmp/projects")

    config = Config.from_env()

    assert config.db_path == Path("/tmp/custom.db")
    assert config.host == "127.0.0.1"
    assert config.port == 9999
    assert config.poll_interval == 15.5
    assert config.attribution_dir == Path("/tmp/projects").resolve()


def test_a_tilde_in_the_projects_dir_is_expanded(monkeypatch):
    """Like the db path, `BURNRATE_PROJECTS_DIR` is expanduser()'d so `~` names the
    home directory rather than a literal `~` directory next to the cwd."""
    monkeypatch.setenv("BURNRATE_PROJECTS_DIR", "~/somewhere/projects")

    attribution_dir = Config.from_env().attribution_dir

    assert "~" not in str(attribution_dir)
    assert attribution_dir == Path.home() / "somewhere" / "projects"


@pytest.mark.parametrize("raw", ["rel/projects", "projects", "./p"])
def test_a_relative_projects_dir_is_made_absolute(monkeypatch, raw):
    """Same reason `BURNRATE_DB` is absolutized: a relative projects dir would resolve
    against launchd's WorkingDirectory under the agent but against the shell's cwd in a
    foreground run, naming two different trees for one configuration."""
    monkeypatch.setenv("BURNRATE_PROJECTS_DIR", raw)

    assert Config.from_env().attribution_dir.is_absolute()


def test_projects_dir_is_normalized_for_its_persistent_attribution_identity(monkeypatch, tmp_path):
    """Equivalent spellings must not create separate rollup namespaces after restart."""
    root = tmp_path / "projects"
    (root / "nested").mkdir(parents=True)
    monkeypatch.setenv("BURNRATE_PROJECTS_DIR", str(root / "nested" / ".."))

    assert Config.from_env().attribution_dir == root.resolve()


def test_a_tilde_in_the_db_path_is_expanded(monkeypatch):
    monkeypatch.setenv("BURNRATE_DB", "~/somewhere/burnrate.db")

    config = Config.from_env()

    assert "~" not in str(config.db_path)
    assert config.db_path.is_absolute()


@pytest.mark.parametrize("raw", ["rel/sub.db", "sub.db", "./x.db"])
def test_a_relative_db_path_is_made_absolute(monkeypatch, raw):
    """Three processes have to agree which file this is: the agent, a foreground
    run, and `uninstall.sh --purge`, which resolves the recorded path against its
    own cwd. A relative one meant purge could delete something else and leave the
    real database behind."""
    monkeypatch.setenv("BURNRATE_DB", raw)

    assert Config.from_env().db_path.is_absolute()


def test_a_tilde_path_is_absolute_and_not_anchored_to_the_cwd(monkeypatch):
    """Regression: install.sh anchored relative paths itself, so a quoted
    BURNRATE_DB='~/private/burnrate.db' became $PWD/~/private/burnrate.db while
    expanduser() gave $HOME/private/... -- one configuration, two different files
    depending on how the service was started."""
    monkeypatch.setenv("BURNRATE_DB", "~/private/burnrate.db")

    path = Config.from_env().db_path

    assert path == Path.home() / "private" / "burnrate.db"
    assert "~" not in str(path)


def test_garbage_numbers_fall_back_rather_than_crashing_at_boot(monkeypatch):
    """A typo in the plist must not stop the service from starting."""
    monkeypatch.setenv("BURNRATE_PORT", "not-a-port")
    monkeypatch.setenv("BURNRATE_POLL_INTERVAL", "")

    config = Config.from_env()

    assert config.port == DEFAULT_PORT
    assert config.poll_interval == 60.0


@pytest.mark.parametrize("bad", ["0", "-5", "-0.001", "nan", "inf", "-inf"])
def test_an_unusable_poll_interval_falls_back_to_the_default(monkeypatch, bad):
    """Zero or negative leaves the loop with no wait and it hammers the
    endpoint; nan and inf raise out of timedelta() inside the loop body,
    outside any handler, killing the poller after one cycle."""
    monkeypatch.setenv("BURNRATE_POLL_INTERVAL", bad)

    assert Config.from_env().poll_interval == 60.0


def test_a_usable_interval_is_still_honoured(monkeypatch):
    monkeypatch.setenv("BURNRATE_POLL_INTERVAL", "0.5")

    assert Config.from_env().poll_interval == 0.5


@pytest.mark.parametrize("bad", ["1e20", "1e30", "86400.001", "999999999999"])
def test_an_absurdly_large_interval_falls_back_to_the_default(monkeypatch, bad):
    """Finiteness was not enough. 1e20 is finite and positive, so it passed the
    earlier guard, then reached the same timedelta() in the poll loop and raised
    the same OverflowError in the same place with nothing to catch it."""
    monkeypatch.setenv("BURNRATE_POLL_INTERVAL", bad)

    assert Config.from_env().poll_interval == 60.0


def test_the_largest_accepted_interval_is_honoured(monkeypatch):
    monkeypatch.setenv("BURNRATE_POLL_INTERVAL", str(MAX_POLL_INTERVAL_SECONDS))

    assert Config.from_env().poll_interval == MAX_POLL_INTERVAL_SECONDS


@pytest.mark.parametrize("bad", ["0", "-1", "65536", "99999"])
def test_an_out_of_range_port_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setenv("BURNRATE_PORT", bad)

    assert Config.from_env().port == DEFAULT_PORT


def test_every_configured_interval_survives_timedelta(monkeypatch):
    """The property that actually matters: whatever from_env returns must not
    raise where the poll loop uses it."""
    from datetime import timedelta

    for value in ("0", "nan", "inf", "abc", "30", "1e20", "1e309", "-1e20", "86400"):
        monkeypatch.setenv("BURNRATE_POLL_INTERVAL", value)
        timedelta(seconds=Config.from_env().poll_interval)


def test_the_freshness_window_matches_the_old_constant_at_the_default_interval():
    """The scaling must be invisible for anyone who never sets an interval."""
    assert Config().stale_after_seconds == 180.0


@pytest.mark.parametrize(
    ("interval", "expected"),
    [(1.0, 180.0), (30.0, 180.0), (60.0, 180.0), (600.0, 1800.0), (3600.0, 10800.0)],
)
def test_the_freshness_window_scales_with_the_interval(interval, expected):
    """Regression: the window was a fixed 180s while intervals up to 86,400s are
    accepted, so an hourly poll declared its own successful reading stale for about
    57 minutes of every hour -- banner lit and projection withheld with nothing
    wrong. A banner that is usually on is a banner nobody reads."""
    assert Config(poll_interval=interval).stale_after_seconds == expected


def test_the_freshness_window_never_drops_below_the_floor():
    """A sub-second interval must not make the dashboard hair-trigger."""
    assert Config(poll_interval=0.5).stale_after_seconds == 180.0


def test_print_effective_order_is_the_installers_contract(monkeypatch, capsys):
    """install.sh reads these records positionally, so a reordering here would
    silently misconfigure the agent instead of failing. Pinned rather than trusted.
    New fields (the projects dir) append at the end so earlier positions never move.
    The newline form is kept for reading by eye; the installer asks for NUL."""
    monkeypatch.setenv("BURNRATE_HOST", "127.0.0.1")
    monkeypatch.setenv("BURNRATE_PORT", "9999")
    monkeypatch.setenv("BURNRATE_POLL_INTERVAL", "15")
    monkeypatch.setenv("BURNRATE_PROJECTS_DIR", "/tmp/projects")

    print_effective()

    lines = capsys.readouterr().out.splitlines()
    assert lines[1:] == ["127.0.0.1", "9999", "15.0", str(Path("/tmp/projects").resolve())]
    assert Path(lines[0]).is_absolute(), "the db path comes first and is absolute"


@pytest.mark.parametrize("bad_port", ["abc", "0", "99999", "-1"])
def test_the_installer_is_told_the_port_the_app_will_really_use(monkeypatch, capsys, bad_port):
    """The whole point of routing the installer through here: it used to keep the
    raw value, so the plist and the readiness URL said one port while the agent
    listened on 8377 -- a healthy service the installer declared unhealthy."""
    monkeypatch.setenv("BURNRATE_PORT", bad_port)

    print_effective()

    assert capsys.readouterr().out.splitlines()[2] == str(DEFAULT_PORT)


def test_print_effective_is_runnable_as_a_module(monkeypatch):
    """install.sh invokes `python -m burnrate.config`, so that entry point is part
    of the contract, not an implementation detail."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "burnrate.config"],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "BURNRATE_PORT": "abc",
            "BURNRATE_POLL_INTERVAL": "1e20",
            "BURNRATE_PROJECTS_DIR": "/tmp/projects",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[1:] == [
        DEFAULT_HOST,
        str(DEFAULT_PORT),
        "60.0",
        str(Path("/tmp/projects").resolve()),
    ]


def test_the_default_binding_is_lan_reachable():
    """0.0.0.0 is deliberate -- the dashboard is meant to be read from other
    machines on the tailnet. Pinned so it cannot be narrowed by accident."""
    assert Config().host == "0.0.0.0"
    assert Config().port == 8377


def test_null_separated_output_survives_a_newline_in_the_path(monkeypatch, capsys):
    """Regression: only `/` and NUL are forbidden in a POSIX filename, so a path may
    contain a newline. Newline-delimited records split it in two, shifting the host
    into the port and corrupting every value after it. NUL is the one byte the path
    cannot hold."""
    monkeypatch.setenv("BURNRATE_DB", "/tmp/burnrate\nwith a newline.db")
    monkeypatch.setenv("BURNRATE_HOST", "127.0.0.1")
    monkeypatch.setenv("BURNRATE_PORT", "9999")
    monkeypatch.setenv("BURNRATE_POLL_INTERVAL", "15")
    monkeypatch.setenv("BURNRATE_PROJECTS_DIR", "/tmp/projects")

    print_effective(separator="\0")

    records = capsys.readouterr().out.split("\0")[:-1]
    assert records == [
        "/tmp/burnrate\nwith a newline.db",
        "127.0.0.1",
        "9999",
        "15.0",
        str(Path("/tmp/projects").resolve()),
    ]


def test_the_module_emits_nul_records_when_asked(monkeypatch):
    """install.sh invokes it with --null, so that flag is part of the contract."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "burnrate.config", "--null"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "BURNRATE_DB": "/tmp/a\nb.db", "BURNRATE_HOST": "127.0.0.1"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\0")[:2] == ["/tmp/a\nb.db", "127.0.0.1"]
    assert "\n" in result.stdout, "the path's own newline must survive"


@pytest.mark.parametrize("name", ["BURNRATE_DB", "BURNRATE_HOST"])
def test_an_empty_override_is_treated_as_unset(monkeypatch, name):
    """Regression: os.environ.get returns "" for a variable that is set but empty,
    because the key exists. For the database that made Path("") the current directory
    once absolutised, so the store was handed a directory and startup died with
    "unable to open database file" -- while the installer saw a non-empty path and
    baked it into the plist. The numeric readers already degraded to their defaults
    here, since float("") raises; these two did not."""
    monkeypatch.setenv(name, "")

    config = Config.from_env()

    assert config.db_path == DEFAULT_DB_PATH
    assert config.host == DEFAULT_HOST


def test_an_empty_db_override_yields_a_file_not_a_directory(monkeypatch, tmp_path):
    """The consequence, asserted rather than inferred: the store must be able to open
    whatever from_env hands it."""
    monkeypatch.setenv("BURNRATE_DB", "")

    path = Config.from_env().db_path

    assert not path.is_dir()
    assert path.name == "burnrate.db"


def test_a_single_space_is_still_a_real_path(monkeypatch):
    """Only the truly empty string counts as unset. A space is a legal path component,
    and treating it as absent would undo the whitespace handling elsewhere."""
    monkeypatch.setenv("BURNRATE_DB", " ")

    assert Config.from_env().db_path != DEFAULT_DB_PATH
