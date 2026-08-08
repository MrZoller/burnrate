"""Schema tolerance. Every test here is a shape the endpoint could return."""

from datetime import UTC, datetime

import pytest

from burnrate.usage import parse_usage


def test_live_response_yields_all_three_real_buckets(live_response):
    snapshot = parse_usage(live_response)
    keys = [b.key for b in snapshot.buckets]

    assert "five_hour" in keys
    assert "seven_day" in keys
    # The regression that motivated the limits[]-first design: this bucket exists
    # only inside limits[], while seven_day_opus/sonnet sit at null. Reading the
    # top level alone drops a bucket the user is actively burning.
    assert "seven_day_fable" in keys


def test_scoped_weekly_is_labelled_from_the_response_not_hardcoded(live_response):
    bucket = parse_usage(live_response).bucket("seven_day_fable")

    assert bucket.label == "Weekly (Fable)"
    assert bucket.utilization == 13.0
    assert bucket.source == "limits"


def test_scoped_label_follows_a_different_model_without_code_change(live_response):
    live_response["limits"][2]["scope"]["model"]["display_name"] = "Opus"

    bucket = parse_usage(live_response).bucket("seven_day_opus")

    assert bucket is not None
    assert bucket.label == "Weekly (Opus)"


def test_null_buckets_are_omitted_not_rendered_as_zero(live_response):
    keys = [b.key for b in parse_usage(live_response).buckets]

    assert "seven_day_opus" not in keys
    assert "tangelo" not in keys


def test_non_bucket_sections_never_become_gauges(live_response):
    keys = [b.key for b in parse_usage(live_response).buckets]

    assert "extra_usage" not in keys
    assert "spend" not in keys


def test_unknown_bucket_is_surfaced_as_a_notice_not_hidden(live_response):
    snapshot = parse_usage(live_response)
    bucket = snapshot.bucket("nimbus_quill")

    assert bucket is not None, "drift must be visible, never silently dropped"
    assert bucket.known is False
    assert any("nimbus_quill" in n for n in snapshot.notices)


def test_an_unrecognized_bucket_does_not_raise_a_warning(live_response):
    """nimbus_quill is a permanent fixture of the response. Routing it to the
    banner would leave it lit forever and devalue the one signal that matters."""
    snapshot = parse_usage(live_response)

    assert snapshot.warnings == ()
    assert snapshot.notices != ()


def test_a_malformed_field_is_a_warning_not_a_notice():
    snapshot = parse_usage({"five_hour": {"utilization": 10, "resets_at": 12345}})

    assert snapshot.warnings, "a broken field is a real anomaly"
    assert not any("resets_at" in n for n in snapshot.notices)


@pytest.mark.parametrize("bad", ["not-a-number", [], {}, True, float("nan")])
def test_an_unreadable_utilization_warns_even_beside_a_valid_bucket(bad):
    """Regression: a non-null utilization the parser could not read was skipped
    in silence. With one valid bucket left the snapshot was non-empty, so the
    poll recorded success, the banner stayed dark, and the affected gauge just
    disappeared -- a fail-quietly path in a dashboard whose whole posture is to
    fail loudly."""
    snapshot = parse_usage(
        {
            "five_hour": {"utilization": 12, "resets_at": None},
            "seven_day": {"utilization": bad, "resets_at": None},
        }
    )

    assert {b.key for b in snapshot.buckets} == {"five_hour"}
    assert any("seven_day" in w for w in snapshot.warnings), snapshot.warnings


def test_a_null_utilization_stays_quiet():
    """The other half of the same rule. Nulls are how this endpoint says "no
    limit of this kind" and they are on most responses, so warning about them
    would leave the banner permanently lit."""
    snapshot = parse_usage(
        {
            "five_hour": {"utilization": 12, "resets_at": None},
            "seven_day_opus": {"utilization": None, "resets_at": None},
        }
    )

    assert {b.key for b in snapshot.buckets} == {"five_hour"}
    assert snapshot.warnings == ()


def test_an_unreadable_percent_in_limits_also_warns():
    """limits[] is the primary source, so the same silent skip there hides a
    bucket the dashboard is built around."""
    snapshot = parse_usage(
        {
            "limits": [
                {"kind": "session", "percent": 40, "resets_at": None},
                {"kind": "weekly_all", "percent": "?", "resets_at": None},
            ]
        }
    )

    assert {b.key for b in snapshot.buckets} == {"five_hour"}
    assert any("limits[1]" in w for w in snapshot.warnings), snapshot.warnings


def test_a_null_percent_falling_back_to_utilization_is_not_a_warning():
    """Both fields are read, so a null `percent` next to a usable `utilization`
    is a successful parse, not drift."""
    snapshot = parse_usage(
        {"limits": [{"kind": "session", "percent": None, "utilization": 33, "resets_at": None}]}
    )

    assert snapshot.bucket("five_hour").utilization == 33.0
    assert snapshot.warnings == ()


def test_a_limits_entry_missing_its_reset_borrows_the_top_level_one(live_response):
    """Regression: the top-level twin was skipped wholesale when limits[] had
    already produced the bucket, so a limits entry with a percentage but a null
    reset lost a perfectly good timestamp -- costing the gauge its countdown and
    taking the weekly projection to unavailable."""
    live_response["limits"][1]["resets_at"] = None

    bucket = parse_usage(live_response).bucket("seven_day")

    assert bucket.utilization == 14.0, "the richer limits percentage still wins"
    assert bucket.resets_at is not None, "the top-level reset must fill the gap"
    assert bucket.resets_at.isoformat().startswith("2026-08-15T16:00")


def test_a_malformed_limits_reset_also_falls_back(live_response):
    live_response["limits"][1]["resets_at"] = "whenever"

    bucket = parse_usage(live_response).bucket("seven_day")

    assert bucket.resets_at is not None


def test_a_present_limits_reset_is_not_overwritten(live_response):
    """weekly_scoped resets one second before weekly_all, so a blind overwrite
    would quietly shift it."""
    bucket = parse_usage(live_response).bucket("seven_day")

    assert bucket.resets_at.second == 0


def test_limits_wins_over_top_level_for_the_same_bucket(live_response):
    live_response["five_hour"]["utilization"] = 99.0

    bucket = parse_usage(live_response).bucket("five_hour")

    assert bucket.source == "limits"
    assert bucket.utilization == 38.0


def test_top_level_fills_in_when_limits_disappears(live_response):
    del live_response["limits"]

    snapshot = parse_usage(live_response)
    keys = [b.key for b in snapshot.buckets]

    assert "five_hour" in keys
    assert "seven_day" in keys
    assert snapshot.bucket("five_hour").source == "top_level"


def test_session_sorts_ahead_of_weekly_and_unknown_sorts_last(live_response):
    keys = [b.key for b in parse_usage(live_response).buckets]

    assert keys[0] == "five_hour"
    assert keys[-1] == "nimbus_quill"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "not json at all",
        42,
        {},
        {"limits": "not a list"},
        {"limits": [None, 7, "x"]},
        {"five_hour": None},
        {"five_hour": {"utilization": None}},
        {"five_hour": {}},
    ],
)
def test_hostile_payloads_never_raise(payload):
    snapshot = parse_usage(payload)

    assert snapshot.buckets == ()
    assert snapshot.warnings, "an unreadable response must explain itself"


def test_unparseable_reset_time_keeps_the_bucket_and_warns():
    snapshot = parse_usage({"five_hour": {"utilization": 10, "resets_at": "soon-ish"}})

    bucket = snapshot.bucket("five_hour")
    assert bucket.utilization == 10.0
    assert bucket.resets_at is None
    assert any("soon-ish" in w for w in snapshot.warnings)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (38, 38.0),
        (38.4, 38.4),
        ("38", 38.0),
        ("38%", 38.0),
        (-5, 0.0),  # clamped
        (140, 100.0),  # clamped: over-cap still means "at the cap"
        (True, None),  # bools are not measurements
        ("abc", None),
        (None, None),
    ],
)
def test_utilization_coercion(raw, expected):
    snapshot = parse_usage({"five_hour": {"utilization": raw}})
    bucket = snapshot.bucket("five_hour")

    if expected is None:
        assert bucket is None
    else:
        assert bucket.utilization == expected


def test_naive_reset_timestamp_is_treated_as_utc():
    snapshot = parse_usage({"five_hour": {"utilization": 5, "resets_at": "2026-08-08T23:30:00"}})

    assert snapshot.bucket("five_hour").resets_at == datetime(2026, 8, 8, 23, 30, tzinfo=UTC)


def test_a_brand_new_limit_kind_still_renders():
    payload = {
        "limits": [
            {"kind": "monthly_all", "percent": 22, "resets_at": "2026-09-01T00:00:00Z"},
        ]
    }

    snapshot = parse_usage(payload)
    bucket = snapshot.buckets[0]

    assert bucket.utilization == 22.0
    assert bucket.known is False
    assert bucket.label == "Monthly all"


def test_weekly_scoped_without_a_model_name_still_renders():
    payload = {"limits": [{"kind": "weekly_scoped", "percent": 9, "scope": None}]}

    bucket = parse_usage(payload).bucket("seven_day_scoped")

    assert bucket is not None
    assert bucket.label == "Weekly (scoped)"
    assert bucket.known is False


def test_fetched_at_is_carried_onto_the_snapshot(live_response):
    moment = datetime(2026, 8, 8, 21, 45, tzinfo=UTC)

    assert parse_usage(live_response, fetched_at=moment).fetched_at == moment
