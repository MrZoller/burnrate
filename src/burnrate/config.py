"""Runtime configuration, all overridable by environment variable."""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "burnrate" / "burnrate.db"
DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - LAN/Tailscale exposure is the point
DEFAULT_PORT = 8377

# A reading older than this is shown as stale rather than presented as current.
# This is the floor, and it is deliberately three times the default interval: the
# question staleness answers is "should a fresh reading have arrived by now", which
# only has a fixed answer while the cadence is fixed.
STALE_AFTER_SECONDS = 180.0

# Missed polls tolerated before a reading is called stale. At the default interval
# this reproduces the 180s above exactly.
STALE_INTERVAL_FACTOR = 3.0

# Longest poll interval we will accept. One day is already far past useful for a
# usage dashboard, and the real job of the ceiling is that everything downstream
# has to survive the value: `timedelta(seconds=1e20)` raises OverflowError, and it
# does so in the poll loop where nothing catches it.
MAX_POLL_INTERVAL_SECONDS = 86400.0


@dataclass(frozen=True)
class Config:
    db_path: Path = DEFAULT_DB_PATH
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    poll_interval: float = 60.0

    @property
    def stale_after_seconds(self) -> float:
        """Age past which a reading is presented as stale rather than current.

        Scaled to the configured cadence, because a fixed window is only right for
        a fixed interval. Against the 180s constant, an hourly poll spent about 57
        minutes of every hour showing the stale banner and withholding the
        projection while nothing whatsoever was wrong -- and a banner that is
        usually lit is a banner nobody reads, which costs the one signal that
        means the data actually went bad.
        """
        return max(STALE_AFTER_SECONDS, self.poll_interval * STALE_INTERVAL_FACTOR)

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            db_path=_db_path_env(),
            host=_str_env("BURNRATE_HOST", DEFAULT_HOST),
            port=_int_env("BURNRATE_PORT", DEFAULT_PORT, 1, 65535),
            poll_interval=_positive_float_env(
                "BURNRATE_POLL_INTERVAL", 60.0, MAX_POLL_INTERVAL_SECONDS
            ),
        )


def _str_env(name: str, default: str) -> str:
    """A non-empty string from the environment, or the default.

    `os.environ.get(name, default)` returns "" for a variable that is set but empty,
    because the key exists -- so an empty override was taken as a value rather than as
    an absence. The numeric readers already degrade to their defaults there, since
    float("") raises; these two did not. Only the truly empty string counts as unset:
    a single space is a legal path component and treating it as absent would undo the
    whitespace handling elsewhere.
    """
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _db_path_env() -> Path:
    """The database path, expanded and absolute.

    Absolute because three different processes have to agree on which file it is:
    the agent, whose cwd is the plist's WorkingDirectory; a foreground `uv run
    burnrate`; and `uninstall.sh --purge`, which resolves whatever the plist
    recorded against its own cwd. A relative path meant purge could delete
    something else and leave the real database behind.

    Expansion happens before that, and it happens here rather than in shell so
    there is one implementation. `install.sh` used to anchor relative paths itself
    and turned a quoted BURNRATE_DB='~/private/burnrate.db' into
    `$PWD/~/private/burnrate.db`, while expanduser() gave `$HOME/private/...` --
    the same configuration naming two different files depending on how it was run.
    Python also handles `~user`, which shell string-matching would not.

    An empty BURNRATE_DB is an absence, not a value. `Path("")` is `Path(".")`, which
    became the current directory once made absolute -- so the store was handed a
    directory and startup died with "unable to open database file", while the
    installer saw a non-empty path and baked it into the plist.
    """
    raw = Path(_str_env("BURNRATE_DB", str(DEFAULT_DB_PATH))).expanduser()
    return raw if raw.is_absolute() else Path.cwd() / raw


def print_effective(stream: TextIO | None = None, separator: str = "\n") -> None:
    """Emit the settings this process would actually use, one record each.

    `deploy/install.sh` reads this to bake the plist and to build its readiness
    URL. It exists so there is one implementation of the validation rules instead
    of a second copy in shell: the installer used to keep whatever was in the
    environment, so a BURNRATE_PORT of "abc" went into the plist and into the probe
    URL while `from_env` quietly rejected it and the agent listened on 8377.

    Order is the contract -- db, host, port, interval -- and is pinned by a test,
    because a silent reordering here would misconfigure the agent rather than fail.

    `separator` exists because a path may legally contain a newline: only `/` and
    NUL are forbidden in a POSIX filename. Newline-delimited records turned such a
    path into two, shifting the host into the port and corrupting everything after
    it, so the installer asks for NUL separators -- the one byte a path cannot hold.
    The default stays newline for reading by eye.
    """
    config = Config.from_env()
    out = stream if stream is not None else sys.stdout
    for value in (config.db_path, config.host, config.port, config.poll_interval):
        out.write(f"{value}{separator}")


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    """An int from the environment, or the default if it is unusable."""
    try:
        value = int(os.environ[name])
    except (KeyError, ValueError):
        return default
    return value if lo <= value <= hi else default


def _positive_float_env(name: str, default: float, maximum: float) -> float:
    """A strictly positive float in (0, maximum], or the default.

    An unvalidated value here is not a cosmetic problem. Zero or a negative
    interval leaves the poll loop with no wait at all, so it hammers the
    endpoint continuously; nan and inf raise out of timedelta() inside the loop
    body, outside any handler, and silently kill the background task after its
    first poll. A typo in the plist must degrade to the default, not to either
    of those.

    `maximum` closes the gap that finiteness alone left open: 1e20 is finite and
    positive, and it reaches the same timedelta() and raises the same
    OverflowError in the same unprotected place. Out of range degrades to the
    default rather than clamping, matching how the port is handled -- a value
    that large is a typo, and a typo should leave polling working.
    """
    try:
        value = float(os.environ[name])
    except (KeyError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0 or value > maximum:
        return default
    return value


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    print_effective(separator="\0" if "--null" in sys.argv[1:] else "\n")
