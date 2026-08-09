"""Projection math, including the cases where refusing to project is correct."""

from datetime import UTC, datetime, timedelta

import pytest

from burnrate.projection import (
    AT_CAP,
    CLEARS_RESET,
    IDLE,
    INSUFFICIENT_DATA,
    PROJECTED,
    UNAVAILABLE,
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
