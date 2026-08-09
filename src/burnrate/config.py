"""Runtime configuration, all overridable by environment variable."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "burnrate" / "burnrate.db"
DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - LAN/Tailscale exposure is the point
DEFAULT_PORT = 8377

# A reading older than this is shown as stale rather than presented as current.
STALE_AFTER_SECONDS = 180.0

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

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            db_path=Path(os.environ.get("BURNRATE_DB", str(DEFAULT_DB_PATH))).expanduser(),
            host=os.environ.get("BURNRATE_HOST", DEFAULT_HOST),
            port=_int_env("BURNRATE_PORT", DEFAULT_PORT, 1, 65535),
            poll_interval=_positive_float_env(
                "BURNRATE_POLL_INTERVAL", 60.0, MAX_POLL_INTERVAL_SECONDS
            ),
        )


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
