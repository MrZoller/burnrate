"""End-to-end coverage for deploy/uninstall.sh --purge.

The script reads the database path install.sh recorded in the plist, but an
explicit BURNRATE_DB in the environment must override it -- that override is the
escape hatch the relative-path guard's message tells the user to reach for, and
before the precedence fix (`${DB:-...}` extracted-value-wins) the extracted path
won instead, so the advice pointed at a command that did nothing.

plutil and launchctl are stubbed via a directory prepended to PATH so the script
runs without touching launchd or any real plist.
"""

import os
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).parent.parent / "deploy" / "uninstall.sh"
LABEL = "com.mrzoller.burnrate"


def _setup(tmp_path):
    """Build a fake HOME with a plist present and stub plutil/launchctl on PATH.

    The plutil stub emits a RELATIVE path and exits 0, so extraction succeeds
    (EXTRACT_FAILED stays 0) and drives the relative-recorded-path scenario. It
    returns (env, home): env has BURNRATE_DB removed so each case sets its own.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    plutil = bin_dir / "plutil"
    plutil.write_text("#!/bin/sh\nprintf 'relative/burnrate.db\\n'\n")
    plutil.chmod(0o755)

    launchctl = bin_dir / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 0\n")
    launchctl.chmod(0o755)

    home = tmp_path / "home"
    plist = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>\n")

    env = {k: v for k, v in os.environ.items() if k != "BURNRATE_DB"}
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    return env, home


def test_purge_refuses_a_relative_recorded_path_when_burnrate_db_is_unset(tmp_path):
    """Case A: with BURNRATE_DB unset the recorded (relative) path wins, and the
    relative-path guard must refuse rather than delete a file resolved against the
    wrong cwd."""
    env, _ = _setup(tmp_path)

    result = subprocess.run(
        ["bash", str(SCRIPT), "--purge"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "is relative" in result.stderr
    assert "relative/burnrate.db" in result.stderr


def test_purge_honours_an_absolute_burnrate_db_over_the_relative_record(tmp_path):
    """Case B: an explicit absolute BURNRATE_DB overrides the relative recorded
    path, so the guard passes and --purge deletes exactly that file. This is the
    escape hatch; before the precedence fix the extracted relative path won and
    the guard refused despite the override."""
    env, _ = _setup(tmp_path)
    db = tmp_path / "real" / "burnrate.db"
    db.parent.mkdir()
    db.write_text("samples\n")
    env["BURNRATE_DB"] = str(db)

    result = subprocess.run(
        ["bash", str(SCRIPT), "--purge"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not db.exists()
