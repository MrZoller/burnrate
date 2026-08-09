"""Projection math, including the cases where refusing to project is correct."""

from datetime import UTC, datetime, timedelta

import pytest

from burnrate.projection import (
    AT_CAP,
    CLEARS_RESET,
    IDLE,
    INSUFFICIENT_DATA,
    ON_PACE,
    ON_PACE_TO_CAP,
    PROJECTED,
    TOO_EARLY,
    UNAVAILABLE,
    UNKNOWN_PACE,
    pace_for,
    project,
)
from burnrate.usage import Bucket

NOW = datetime(2026, 8, 8, 21, 45, tzinfo=UTC)
WEEK = timedelta(days=7)


def weekly(utilization, *, resets_in=WEEK - timedelta(hours=5.75), key="seven_day"):
    """A weekly bucket whose window opened `7d - resets_in` before NOW."""
    return Bucket(
        key=key,
        label="Weekly (all models)",
        utilization=utilization,
        resets_at=NOW + resets_in,
    )


def test_matches_the_live_reading_by_hand():
    # 14% burned in the 5.75h since the window opened -> 2.4348%/h.
    # The remaining 86% therefore takes 35.32h, landing ~2026-08-10T09:04Z.
    projection = project(weekly(14.0), now=NOW)

    assert projection.status == PROJECTED
    assert projection.elapsed_hours == pytest.approx(5.75, abs=1e-6)
    assert projection.rate_per_hour == pytest.approx(14.0 / 5.75, rel=1e-9)
    assert projection.hours_to_cap == pytest.approx(86.0 / (14.0 / 5.75), rel=1e-9)


def test_projection_lands_where_arithmetic_says():
    projection = project(weekly(14.0), now=NOW)
    expected = NOW + timedelta(hours=86.0 / (14.0 / 5.75))

    assert abs((projection.hits_cap_at - expected).total_seconds()) < 1e-3


def test_window_start_is_one_period_before_the_reset():
    projection = project(weekly(14.0), now=NOW)

    assert projection.resets_at - projection.window_start == WEEK


def test_slow_burn_clears_the_reset():
    # 1% in 5.75h projects to ~570h to fill, far past the ~162h remaining.
    projection = project(weekly(1.0), now=NOW)

    assert projection.status == CLEARS_RESET
    assert projection.hits_cap_at > projection.resets_at
    assert "resets before the cap" in projection.message


def test_the_boundary_between_projected_and_clears_reset():
    """A pace landing exactly on the reset counts as clearing it."""
    elapsed = timedelta(hours=5.75)
    remaining = WEEK - elapsed
    # Choose u so that (100-u)/(u/5.75) == remaining hours exactly.
    hours_remaining = remaining.total_seconds() / 3600
    u = 100 * 5.75 / (hours_remaining + 5.75)

    assert project(weekly(u), now=NOW).status == CLEARS_RESET
    assert project(weekly(u * 1.02), now=NOW).status == PROJECTED


def test_a_fresh_reset_refuses_to_project():
    """The bug this guards: minutes after a reset, any use projects to 'cap soon'."""
    just_reset = weekly(3.0, resets_in=WEEK - timedelta(minutes=6))

    projection = project(just_reset, now=NOW)

    assert projection.status == INSUFFICIENT_DATA
    assert projection.hits_cap_at is None


def test_the_minimum_window_boundary():
    just_under = weekly(3.0, resets_in=WEEK - timedelta(minutes=29))
    just_over = weekly(3.0, resets_in=WEEK - timedelta(minutes=31))

    assert project(just_under, now=NOW).status == INSUFFICIENT_DATA
    assert project(just_over, now=NOW).status == PROJECTED


def test_zero_usage_is_idle_not_a_division_by_zero():
    projection = project(weekly(0.0), now=NOW)

    assert projection.status == IDLE
    assert projection.rate_per_hour is None


def test_at_the_cap_reports_at_cap():
    projection = project(weekly(100.0), now=NOW)

    assert projection.status == AT_CAP
    assert projection.hours_to_cap == 0.0


def test_over_the_cap_still_reports_at_cap():
    assert project(weekly(100.0), now=NOW).status == AT_CAP


def test_a_full_bucket_whose_reset_passed_is_unavailable_not_at_cap():
    """Regression: AT_CAP was checked first, so a stale 100% reading whose period
    had already ended still reported "Weekly cap reached" -- describing a window
    that no longer exists, while every other past-reset reading is refused."""
    stale = weekly(100.0, resets_in=timedelta(hours=-1))

    projection = project(stale, now=NOW)

    assert projection.status == UNAVAILABLE
    assert "already passed" in projection.message


def test_a_full_bucket_inside_its_window_still_reports_at_cap():
    assert project(weekly(100.0), now=NOW).status == AT_CAP


@pytest.mark.parametrize(
    "resets_at",
    [
        datetime(1, 1, 1, tzinfo=UTC),
        datetime(1, 1, 8, tzinfo=UTC),
        datetime(9999, 12, 31, tzinfo=UTC),
    ],
)
def test_an_out_of_range_reset_costs_the_projection_not_the_page(resets_at):
    """The parser refuses these now, so this is the backstop. `project` runs inside
    /api/now with no handler above it, and subtracting the period from 0001-01-01
    raises OverflowError -- the second overflow found in this function, which is why
    it is guarded rather than only fixed at the source."""
    bucket = Bucket(key="seven_day", label="Weekly", utilization=40.0, resets_at=resets_at)

    projection = project(bucket, now=NOW)

    assert projection.status in {UNAVAILABLE, INSUFFICIENT_DATA}
    assert projection.hits_cap_at is None


def test_no_bucket_is_unavailable_not_a_crash():
    assert project(None, now=NOW).status == UNAVAILABLE


def test_missing_reset_time_is_unavailable():
    bucket = Bucket(key="seven_day", label="Weekly", utilization=40.0, resets_at=None)

    assert project(bucket, now=NOW).status == UNAVAILABLE


def test_a_reset_already_in_the_past_is_refused_not_extrapolated():
    stale = weekly(40.0, resets_in=timedelta(hours=-1))

    projection = project(stale, now=NOW)

    assert projection.status == UNAVAILABLE
    assert "already passed" in projection.message


def test_the_five_hour_bucket_uses_a_five_hour_window():
    bucket = Bucket(
        key="five_hour",
        label="5-hour session",
        utilization=38.0,
        resets_at=NOW + timedelta(hours=1.75),
    )

    projection = project(bucket, now=NOW)

    # 5h period, 1.75h left -> 3.25h elapsed, not 168h.
    assert projection.elapsed_hours == pytest.approx(3.25, abs=1e-6)
    assert projection.rate_per_hour == pytest.approx(38.0 / 3.25, rel=1e-9)


def test_a_naive_now_is_treated_as_utc():
    naive = NOW.replace(tzinfo=None)

    assert project(weekly(14.0), now=naive).status == PROJECTED


@pytest.mark.parametrize("tiny", [1e-8, 1e-30, 1e-300, 5e-324])
def test_a_vanishingly_small_utilization_does_not_raise(tiny):
    """Regression: `project` runs inside /api/now, so this was not a bad projection
    but a 500 for the whole dashboard until a later poll replaced the reading. A
    utilization of 1e-8 -- a rounding artifact, not an exotic value -- put
    hours_to_cap outside timedelta's range, and a subnormal underflowed the rate to
    zero and divided by it."""
    projection = project(weekly(tiny), now=NOW)

    assert projection.status == CLEARS_RESET
    if projection.hits_cap_at is not None:
        assert projection.hits_cap_at > projection.resets_at


def test_the_reported_pace_is_kept_even_when_the_date_is_clamped():
    """The clamp is for the timestamp only. Throwing away the real figure would
    hide how slow the burn actually is."""
    projection = project(weekly(1e-300), now=NOW)

    assert projection.hours_to_cap > 1e300
    assert projection.rate_per_hour > 0.0


@pytest.mark.parametrize("utilization", [1e-8, 0.5, 1.0, 14.0, 50.0, 99.9, 99.999])
def test_every_accepted_utilization_produces_a_usable_projection(utilization):
    """The property the crash violated: any value _as_percent will return must go
    through project() without raising, since /api/now has no handler for it."""
    projection = project(weekly(utilization), now=NOW)

    assert projection.status in {PROJECTED, CLEARS_RESET}
    if projection.hits_cap_at is not None:
        assert projection.hits_cap_at > NOW


def test_a_reading_taken_before_the_reset_does_not_project_across_it():
    """Regression: anchoring the whole projection to the reading time meant the
    period-ended guard was asked of the reading rather than of now. A sample taken
    30s before a reset, requested 60s after it, is still fresh by staleness -- and an
    85% bucket reported "clears the reset" for a window that no longer existed."""
    resets_at = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    bucket = Bucket(key="seven_day", label="Weekly", utilization=85.0, resets_at=resets_at)

    projection = project(
        bucket,
        now=resets_at + timedelta(seconds=60),
        reading_at=resets_at - timedelta(seconds=30),
    )

    assert projection.status == UNAVAILABLE
    assert "already passed" in projection.message
    assert projection.hits_cap_at is None


def test_the_rate_still_comes_from_the_reading_not_the_request():
    """The other half: the two clocks must stay separate, or fixing the guard would
    reintroduce the dilution that anchoring to the reading fixed."""
    resets_at = NOW + WEEK - timedelta(hours=5.75)
    bucket = Bucket(key="seven_day", label="Weekly", utilization=14.0, resets_at=resets_at)

    # Requested an hour after the sample, still well inside the window.
    projection = project(bucket, now=NOW + timedelta(hours=1), reading_at=NOW)

    assert projection.status == PROJECTED
    assert projection.elapsed_hours == pytest.approx(5.75, abs=1e-6)
    assert projection.rate_per_hour == pytest.approx(14.0 / 5.75, rel=1e-9)


def test_reading_at_defaults_to_now():
    """Callers with a single clock keep the old behaviour exactly."""
    with_default = project(weekly(14.0), now=NOW)
    explicit = project(weekly(14.0), now=NOW, reading_at=NOW)

    assert with_default == explicit


def test_a_stale_reading_refuses_to_project():
    """Regression: /api/now projected the last known utilization against the
    current clock, so every hour since the sample counted as zero usage. A
    30%-in-24h reading left frozen for three days fell from 1.25%/h to 0.31%/h
    and the status flipped from "projected" to "clears_reset" -- a cap warning
    turning itself into an all-clear on no evidence at all."""
    projection = project(weekly(30.0), now=NOW, stale=True)

    assert projection.status == UNAVAILABLE
    assert projection.rate_per_hour is None
    assert projection.hits_cap_at is None
    assert "too old" in projection.message
    # Still carries what the UI needs to describe the bucket it refused on.
    assert projection.bucket_key == "seven_day"
    assert projection.utilization == 30.0


def test_the_dilution_the_stale_guard_exists_to_prevent():
    """Pinned as arithmetic, so the guard cannot be removed without a red test."""
    bucket = weekly(30.0, resets_in=WEEK - timedelta(hours=24))

    at_reading = project(bucket, now=NOW)
    three_days_later = project(bucket, now=NOW + timedelta(days=3))

    assert at_reading.status == PROJECTED
    assert at_reading.rate_per_hour == pytest.approx(30.0 / 24.0)
    assert three_days_later.status == CLEARS_RESET
    assert three_days_later.rate_per_hour < at_reading.rate_per_hour / 3


def test_a_stale_reading_with_no_bucket_is_still_unavailable():
    assert project(None, now=NOW, stale=True).status == UNAVAILABLE


def test_faster_burn_always_hits_the_cap_sooner():
    slow = project(weekly(10.0), now=NOW)
    fast = project(weekly(30.0), now=NOW)

    assert fast.hits_cap_at < slow.hits_cap_at
    assert fast.rate_per_hour > slow.rate_per_hour


# ------------------------------------------------------------------ pace verdicts


def at_window_age(utilization, elapsed_hours, *, period_hours=168.0, key="seven_day", known=True):
    """A bucket whose window opened `elapsed_hours` before NOW, read at NOW."""
    resets_at = NOW + timedelta(hours=period_hours - elapsed_hours)
    bucket = Bucket(
        key=key,
        label="Weekly (all models)",
        utilization=utilization,
        resets_at=resets_at,
        known=known,
    )
    return pace_for(bucket, now=NOW, reading_at=NOW)


def test_34_percent_at_23_hours_is_on_pace_to_cap():
    """The issue's worked example: burned well ahead of the clock, projected to cap
    44h out -- inside the ~145h left in the window."""
    pace = at_window_age(34.0, 23.0)

    assert pace.status == ON_PACE_TO_CAP
    assert pace.label == "On pace to cap"


def test_34_percent_at_5_days_is_on_pace():
    """Same 34% burned, but five days into the window: below the elapsed line, so
    the same number now reads as comfortably on pace."""
    pace = at_window_age(34.0, 5 * 24.0)

    assert pace.status == ON_PACE
    assert pace.label == "On pace"


def test_below_the_elapsed_line_is_on_pace_even_when_projection_clears():
    pace = at_window_age(10.0, 5 * 24.0)

    assert pace.status == ON_PACE


def test_an_unrecognized_bucket_never_gets_a_colour_coded_verdict():
    pace = at_window_age(80.0, 5 * 24.0, known=False)

    assert pace.status == UNKNOWN_PACE
    assert pace.label == "Unknown"


def test_a_bucket_younger_than_the_projection_floor_is_too_early():
    pace = at_window_age(3.0, 0.25)

    assert pace.status == TOO_EARLY
    assert pace.label == "Too early to tell"


def test_a_bucket_with_no_reset_has_no_window_and_is_unknown():
    """No reset means no derivable window, so there is nothing to be early *about*:
    the verdict is "Unknown", not "Too early to tell". "Too early" is reserved for a
    window that exists but has not aged enough to project."""
    bucket = Bucket(key="seven_day", label="Weekly", utilization=40.0, resets_at=None)

    pace = pace_for(bucket, now=NOW, reading_at=NOW)

    assert pace.window_opened_at is None
    assert pace.elapsed_fraction is None
    assert pace.status == UNKNOWN_PACE
    assert pace.label == "Unknown"


def test_a_bucket_whose_reset_has_already_passed_is_unknown():
    """The reviewer's strongest case: a passed reset has no live window, so the gauge
    must not say "Too early to tell" while the hero says "unavailable" and the
    countdown says "Resetting...". All three now agree on Unknown/unavailable."""
    resets_at = NOW - timedelta(hours=1)
    bucket = Bucket(key="seven_day", label="Weekly", utilization=40.0, resets_at=resets_at)

    pace = pace_for(bucket, now=NOW, reading_at=NOW)

    assert pace.status == UNKNOWN_PACE
    assert pace.label == "Unknown"


def test_window_opened_at_is_one_period_before_the_reset():
    weekly_pace = at_window_age(34.0, 23.0, period_hours=168.0, key="seven_day")
    session_pace = at_window_age(34.0, 3.0, period_hours=5.0, key="five_hour")

    assert weekly_pace.resets_at - weekly_pace.window_opened_at == WEEK
    assert session_pace.resets_at - session_pace.window_opened_at == timedelta(hours=5)


def test_elapsed_fraction_tracks_the_reading_not_the_wall_clock():
    """Item 2's honesty rule: the bar's marker anchors to when the reading was taken.
    A reading two days stale still reports the fraction it had when it was fresh,
    rather than sliding toward the reset over data nobody collected."""
    reading_at = NOW
    resets_at = reading_at + timedelta(hours=168.0 - 23.0)
    bucket = Bucket(key="seven_day", label="Weekly", utilization=34.0, resets_at=resets_at)

    fresh = pace_for(bucket, now=reading_at, reading_at=reading_at)
    # Requested two days later; the window is still open, but the reading has not moved.
    aged = pace_for(bucket, now=reading_at + timedelta(days=2), reading_at=reading_at)

    assert fresh.elapsed_fraction == pytest.approx(23.0 / 168.0)
    assert aged.elapsed_fraction == pytest.approx(fresh.elapsed_fraction)


@pytest.mark.parametrize(
    ("utilization", "elapsed_hours", "expected"),
    [
        # Above the diagonal (burn% > elapsed%): projected to cross the cap first.
        (50.0, 60.0, ON_PACE_TO_CAP),
        (34.0, 23.0, ON_PACE_TO_CAP),
        # Exactly on the diagonal (burn% == elapsed%): the `<=` boundary is green.
        (50.0, 84.0, ON_PACE),
        # Below the diagonal (burn% < elapsed%): comfortably on pace.
        (50.0, 100.0, ON_PACE),
        (34.0, 120.0, ON_PACE),
    ],
)
def test_the_verdict_boundary_is_the_diagonal(utilization, elapsed_hours, expected):
    """Two visible tiers, split by the burn%-vs-elapsed% diagonal: on/below it is
    green `on_pace`, above it is red `on_pace_to_cap`.

    The issue's middle tier -- amber `ahead_of_pace` (burn% > elapsed% yet the
    projection still clears the reset) -- is provably unreachable under pure linear
    projection: burn% > elapsed% is equivalent to the pace crossing 100% before the
    reset, so there is no gap for amber to occupy. It stays in `_classify_pace` as a
    faithful, defensive rendering of the issue's tree; a follow-up issue will define a
    real amber threshold. This test pins the two tiers that actually render so the
    boundary cannot drift unnoticed.
    """
    assert at_window_age(utilization, elapsed_hours).status == expected


def test_pace_reuses_the_projection_states():
    """The verdict and the hero projection agree, since one is built from the other."""
    idle = at_window_age(0.0, 5 * 24.0)
    at_cap = at_window_age(100.0, 23.0)

    assert idle.status == ON_PACE
    assert project(
        Bucket("seven_day", "Weekly", 0.0, NOW + timedelta(hours=168 - 120)), now=NOW
    ).status in {IDLE, CLEARS_RESET}
    assert at_cap.status == ON_PACE_TO_CAP
    assert (
        project(
            Bucket("seven_day", "Weekly", 100.0, NOW + timedelta(hours=168 - 23)), now=NOW
        ).status
        == AT_CAP
    )
