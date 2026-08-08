"""Runtime configuration, all overridable by environment variable."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "burnrate" / "burnrate.db"
DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - LAN/Tailscale exposure is the point
DEFAULT_PORT = 8377

# A reading older than this is shown as stale rather than presented as current.
STALE_AFTER_SECONDS = 180.0


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
            port=_int_env("BURNRATE_PORT", DEFAULT_PORT),
            poll_interval=_float_env("BURNRATE_POLL_INTERVAL", 60.0),
        )


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default
