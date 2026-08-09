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

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .usage import Bucket

# Below this much elapsed time the rate is dominated by noise.
MIN_WINDOW_HOURS = 0.5

# Furthest out a crossing time is worth expressing. Only reached when the pace
# already clears the reset, where the exact date carries no information but the
# arithmetic still has to produce a representable one.
CLEARS_RESET_HORIZON_HOURS = 100 * 365 * 24.0

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

# Per-bucket pace verdicts. Unlike the projection status these describe *rate*, not
# level: how far burned against how far into the window. The dashboard colours each
# gauge by one of these, so the colour agrees with the word instead of contradicting
# it (a green "Healthy" beside a hero warning of an imminent cap was the bug).
ON_PACE = "on_pace"
AHEAD_OF_PACE = "ahead_of_pace"
ON_PACE_TO_CAP = "on_pace_to_cap"
TOO_EARLY = "too_early"
UNKNOWN_PACE = "unknown"

PACE_LABELS: dict[str, str] = {
    ON_PACE: "On pace",
    AHEAD_OF_PACE: "Ahead of pace",
    ON_PACE_TO_CAP: "On pace to cap",
    TOO_EARLY: "Too early to tell",
    UNKNOWN_PACE: "Unknown",
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


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


def _aware(value: datetime | None) -> datetime | None:
    """A timezone-aware copy, treating a naive value as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def period_hours_for(key: str) -> float:
    """How long the bucket's window is, inferred from its canonical key."""
    return _PERIOD_HOURS.get(key, DEFAULT_PERIOD_HOURS)


def project(
    bucket: Bucket | None,
    now: datetime | None = None,
    *,
    reading_at: datetime | None = None,
    stale: bool = False,
) -> Projection:
    """Project when `bucket` reaches 100% at its average rate so far.

    Two clocks, because two different questions are being asked and one timestamp
    cannot answer both:

    `now` is wall-clock now, and decides whether this period is still running. Asking
    that of the reading time instead let a sample taken shortly before a reset
    project across it -- an 85% bucket whose window had ended a minute ago reported
    "clears the reset", an all-clear for a period that no longer exists, and at a
    longer poll interval it would say so for much longer.

    `reading_at` is when the sample was taken and anchors the rate, defaulting to
    `now`. The rate is utilization over time elapsed since the window opened, so
    measuring a frozen utilization against an advancing clock counts every hour since
    the last sample as zero usage and understates the pace.

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

    now = _aware(now) or datetime.now(UTC)
    reading_at = _aware(reading_at) or now

    if bucket.resets_at is None:
        return Projection(
            status=UNAVAILABLE,
            message="No reset time reported, so the burn window is unknown.",
            bucket_key=bucket.key,
            utilization=bucket.utilization,
        )

    period = timedelta(hours=period_hours_for(bucket.key))
    # Guarded because this runs inside /api/now, where an exception is a 500 for the
    # whole dashboard rather than one bad projection. The parser now refuses resets
    # implausibly far from now, which is where this belongs and where the warning
    # comes from -- but that is the second overflow found in this function, and a
    # reading that cannot be projected should cost the projection, never the page.
    try:
        window_start = bucket.resets_at - period
        elapsed_hours = (reading_at - window_start).total_seconds() / 3600.0
    except (OverflowError, OSError, ValueError):
        return Projection(
            status=UNAVAILABLE,
            message="The reported reset time is out of range, so no window can be derived.",
            bucket_key=bucket.key,
            utilization=bucket.utilization,
            resets_at=bucket.resets_at,
        )

    base = {
        "bucket_key": bucket.key,
        "utilization": bucket.utilization,
        "window_start": window_start,
        "resets_at": bucket.resets_at,
        "elapsed_hours": elapsed_hours,
    }

    # A reset that has already passed invalidates the whole reading, including a
    # 100% one -- the period is over, so "cap reached" would describe a window
    # that no longer exists. This must outrank AT_CAP, not follow it, and it is
    # asked of wall-clock `now`: against the reading time a sample taken just
    # before a reset projected straight across it.
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
            hits_cap_at=reading_at,
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

    # Everything below is ordered so no unrepresentable timestamp is ever built.
    # `project` runs inside /api/now, so an exception here is not a bad projection,
    # it is a 500 for the whole dashboard until a later poll replaces the reading.
    # A utilization of 1e-8 -- a rounding artifact, not an exotic value -- gives a
    # rate small enough that hours_to_cap leaves timedelta's range, and a subnormal
    # underflows the rate to zero and divides by it. The reset is the natural bound:
    # a pace that cannot reach the cap within this period does not need its crossing
    # time computed exactly, only recognised.
    rate = bucket.utilization / elapsed_hours
    hours_remaining = (bucket.resets_at - reading_at).total_seconds() / 3600.0

    if rate <= 0.0 or not math.isfinite(rate):
        return Projection(
            status=CLEARS_RESET,
            message="At this pace the period resets before the cap is reached.",
            rate_per_hour=rate if math.isfinite(rate) else None,
            **base,
        )

    hours_to_cap = (100.0 - bucket.utilization) / rate

    if not math.isfinite(hours_to_cap) or hours_to_cap >= hours_remaining:
        # Clamped only for the timestamp. `hours_to_cap` keeps the real figure,
        # however large; the clamp exists because a century from now is already
        # unambiguously past a reset a week away, and 1e302 hours is not a date.
        horizon = min(hours_to_cap, CLEARS_RESET_HORIZON_HOURS)
        return Projection(
            status=CLEARS_RESET,
            message="At this pace the period resets before the cap is reached.",
            rate_per_hour=rate,
            hits_cap_at=(reading_at + timedelta(hours=horizon) if math.isfinite(horizon) else None),
            hours_to_cap=hours_to_cap if math.isfinite(hours_to_cap) else None,
            **base,
        )

    # Bounded by construction now: hours_to_cap < hours_remaining <= the period.
    return Projection(
        status=PROJECTED,
        message="At this pace the cap is reached before the period resets.",
        rate_per_hour=rate,
        hits_cap_at=reading_at + timedelta(hours=hours_to_cap),
        hours_to_cap=hours_to_cap,
        **base,
    )


@dataclass(frozen=True)
class Pace:
    """A bucket's pace verdict plus what the time-elapsed bar needs to draw itself.

    `elapsed_fraction` is measured at the reading time, not at wall-clock now, so the
    bar's marker ages honestly with the data: a reading that stopped updating freezes
    the marker where it was instead of sliding it toward the reset over dead data.
    """

    status: str
    label: str
    window_opened_at: datetime | None = None
    resets_at: datetime | None = None
    elapsed_fraction: float | None = None
    utilization: float | None = None


def _classify_pace(projection: Projection, bucket: Bucket, elapsed_fraction: float | None) -> str:
    """Turn a projection into a pace verdict, sharing item 2's elapsed/burn math.

    The order is the one the issue spells out: below the elapsed line is "on pace"
    regardless of where the projection lands, and only above it does the projection
    split "ahead" (clears the reset) from "to cap" (crosses it first).
    """
    status = projection.status
    # No usable window at all -- no reset reported, a reset already passed, or one out
    # of range -- is "Unknown", not "Too early". "Too early" is a claim that a window
    # exists and just needs more time; saying it over a reset that has already passed
    # would contradict the same card's "Resetting..." countdown and "unavailable" hero.
    # Ordered before the elapsed_fraction guard: the no-reset case is both UNAVAILABLE
    # and elapsed_fraction is None, and must resolve to Unknown.
    if status == UNAVAILABLE:
        return UNKNOWN_PACE
    if status == INSUFFICIENT_DATA or elapsed_fraction is None:
        # A window we have, but too little elapsed time for the rate to mean anything.
        # Neutral, never a colour-coded verdict on data that cannot support one.
        return TOO_EARLY
    # A still-idle window younger than the floor is also too early to judge. project()
    # returns IDLE before applying its MIN_WINDOW floor (so the hero can say "no usage
    # yet" rather than "too early"), so a fresh idle bucket arrives here with a real
    # elapsed_fraction and would clear the diagonal (0.0 <= anything) as green -- while
    # the same-age window with 3% usage takes the INSUFFICIENT_DATA path to neutral.
    # Mirror that floor here so age, not 0%-vs-3%, decides the verdict.
    if status == IDLE and (projection.elapsed_hours or 0.0) < MIN_WINDOW_HOURS:
        return TOO_EARLY
    if bucket.utilization / 100.0 <= elapsed_fraction:
        return ON_PACE
    if status == CLEARS_RESET:
        return AHEAD_OF_PACE
    # PROJECTED (crosses before the reset) or AT_CAP (already there).
    return ON_PACE_TO_CAP


def pace_for(
    bucket: Bucket | None,
    now: datetime | None = None,
    *,
    reading_at: datetime | None = None,
) -> Pace:
    """Where `bucket` sits against its own window: how far burned vs how far elapsed.

    Deliberately does not take `stale`. This is the factual position of the reading
    within its window; whether that reading is too old to trust is a separate UI
    decision (the gauge greys itself out), and withholding the window here would take
    the time-elapsed bar down with it.
    """
    if bucket is None or not bucket.known:
        return Pace(
            status=UNKNOWN_PACE,
            label=PACE_LABELS[UNKNOWN_PACE],
            resets_at=bucket.resets_at if bucket else None,
            utilization=bucket.utilization if bucket else None,
        )

    projection = project(bucket, now=now, reading_at=reading_at)
    elapsed_fraction: float | None = None
    if projection.elapsed_hours is not None:
        period = period_hours_for(bucket.key)
        elapsed_fraction = _clamp(projection.elapsed_hours / period, 0.0, 1.0)

    status = _classify_pace(projection, bucket, elapsed_fraction)
    return Pace(
        status=status,
        label=PACE_LABELS[status],
        window_opened_at=projection.window_start,
        resets_at=projection.resets_at or bucket.resets_at,
        elapsed_fraction=elapsed_fraction,
        utilization=bucket.utilization,
    )
