"""Pace projection: at the current burn rate, when does a bucket hit its cap?

The model is deliberately the simplest thing that can be explained in one line
of the README: assume usage has accrued at a constant rate since the period
began, extend that line, and report where it crosses 100%.

    rate      = utilization / hours since the period started
    hits_cap  = now + (100 - utilization) / rate

Two things make this honest rather than misleading:

  * Right after a reset the denominator is tiny, so a few minutes of work
    projects to "cap in 3 hours". We refuse to project until the window is wide
    enough to mean something.
  * A projection landing past `resets_at` is not a warning, it is the good case.
    We report it as "clears the reset" instead of a date the user would read as
    a deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .usage import Bucket

# Below this much elapsed time the rate is dominated by noise.
MIN_WINDOW_HOURS = 0.5

# Period length per bucket family, used to locate the start of the window.
DEFAULT_PERIOD_HOURS = 168.0
_PERIOD_HOURS: dict[str, float] = {"five_hour": 5.0}

# Status values, in the order a caller is likely to branch on them.
PROJECTED = "projected"
CLEARS_RESET = "clears_reset"
AT_CAP = "at_cap"
IDLE = "idle"
INSUFFICIENT_DATA = "insufficient_data"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Projection:
    """Where the current pace lands, plus everything needed to explain it."""

    status: str
    message: str
    bucket_key: str | None = None
    utilization: float | None = None
    rate_per_hour: float | None = None
    elapsed_hours: float | None = None
    window_start: datetime | None = None
    resets_at: datetime | None = None
    hits_cap_at: datetime | None = None
    hours_to_cap: float | None = None

    @property
    def is_projected(self) -> bool:
        """True when we produced an actual crossing time."""
        return self.status == PROJECTED


def period_hours_for(key: str) -> float:
    """How long the bucket's window is, inferred from its canonical key."""
    return _PERIOD_HOURS.get(key, DEFAULT_PERIOD_HOURS)


def project(
    bucket: Bucket | None, now: datetime | None = None, *, stale: bool = False
) -> Projection:
    """Project when `bucket` reaches 100% at its average rate so far.

    `now` should be the moment the reading was taken, not wall-clock now. The rate
    is utilization over time elapsed since the window opened, so a frozen
    utilization measured against an advancing clock counts every hour since the
    last sample as zero usage and understates the pace.

    `stale` refuses outright. That case is not a worse estimate, it is a different
    question: a projection is a claim about where usage is heading *now*, and with
    a reading hours old there is no honest answer -- measured, a 30%-in-24h
    reading left frozen for three days drops from 1.25%/h to 0.31%/h and turns a
    cap warning into "clears the reset". An all-clear derived from missing data is
    the worst direction for this to fail in.
    """
    if bucket is None:
        return Projection(status=UNAVAILABLE, message="No weekly bucket in the last response.")

    if stale:
        return Projection(
            status=UNAVAILABLE,
            message=(
                "The last reading is too old to project from -- the pace would be "
                "diluted by time we have no data for."
            ),
            bucket_key=bucket.key,
            utilization=bucket.utilization,
            resets_at=bucket.resets_at,
        )

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    if bucket.resets_at is None:
        return Projection(
            status=UNAVAILABLE,
            message="No reset time reported, so the burn window is unknown.",
            bucket_key=bucket.key,
            utilization=bucket.utilization,
        )

    period = timedelta(hours=period_hours_for(bucket.key))
    window_start = bucket.resets_at - period
    elapsed_hours = (now - window_start).total_seconds() / 3600.0

    base = {
        "bucket_key": bucket.key,
        "utilization": bucket.utilization,
        "window_start": window_start,
        "resets_at": bucket.resets_at,
        "elapsed_hours": elapsed_hours,
    }

    # A reset that has already passed invalidates the whole reading, including a
    # 100% one -- the period is over, so "cap reached" would describe a window
    # that no longer exists. This must outrank AT_CAP, not follow it.
    if now >= bucket.resets_at:
        return Projection(
            status=UNAVAILABLE,
            message="The reported reset time has already passed; waiting for a fresh reading.",
            **base,
        )

    if elapsed_hours <= 0:
        return Projection(
            status=INSUFFICIENT_DATA,
            message="The reported window has not started yet.",
            **base,
        )

    if bucket.utilization >= 100.0:
        return Projection(
            status=AT_CAP,
            message="Already at the cap for this period.",
            hits_cap_at=now,
            hours_to_cap=0.0,
            **base,
        )

    if bucket.utilization <= 0.0:
        return Projection(
            status=IDLE,
            message="No usage recorded this period yet.",
            **base,
        )

    if elapsed_hours < MIN_WINDOW_HOURS:
        minutes = int(MIN_WINDOW_HOURS * 60)
        return Projection(
            status=INSUFFICIENT_DATA,
            message=f"Too early to project -- needs {minutes} minutes past the reset.",
            **base,
        )

    rate = bucket.utilization / elapsed_hours
    hours_to_cap = (100.0 - bucket.utilization) / rate
    hits_cap_at = now + timedelta(hours=hours_to_cap)

    if hits_cap_at >= bucket.resets_at:
        return Projection(
            status=CLEARS_RESET,
            message="At this pace the period resets before the cap is reached.",
            rate_per_hour=rate,
            hits_cap_at=hits_cap_at,
            hours_to_cap=hours_to_cap,
            **base,
        )

    return Projection(
        status=PROJECTED,
        message="At this pace the cap is reached before the period resets.",
        rate_per_hour=rate,
        hits_cap_at=hits_cap_at,
        hours_to_cap=hours_to_cap,
        **base,
    )
