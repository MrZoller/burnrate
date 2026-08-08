"""Store round-trips and the two JSON endpoints."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from burnrate.app import create_app
from burnrate.config import Config
from burnrate.store import Store
from burnrate.usage import Bucket, UsageSnapshot, parse_usage

NOW = datetime(2026, 8, 8, 21, 45, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "burnrate.db")


def test_round_trips_every_bucket(store, live_response):
    snapshot = parse_usage(live_response, fetched_at=NOW)

    written = store.append_snapshot(snapshot)

    assert written == len(snapshot.buckets)
    assert {s.bucket for s in store.latest_per_bucket()} == {b.key for b in snapshot.buckets}


def test_latest_per_bucket_returns_the_newest_row(store, live_response):
    first = parse_usage(live_response, fetched_at=NOW - timedelta(hours=1))
    live_response["limits"][1]["percent"] = 21
    second = parse_usage(live_response, fetched_at=NOW)

    store.append_snapshot(first)
    store.append_snapshot(second)

    weekly = next(s for s in store.latest_per_bucket() if s.bucket == "seven_day")
    assert weekly.utilization == 21.0


def test_history_respects_the_window(store, live_response):
    store.append_snapshot(parse_usage(live_response, fetched_at=NOW - timedelta(days=9)))
    store.append_snapshot(parse_usage(live_response, fetched_at=datetime.now(UTC)))

    assert len(store.history(hours=168)) == 4


def test_raw_snapshots_are_deduplicated(store, live_response):
    snapshot = parse_usage(live_response, fetched_at=NOW)
    for _ in range(5):
        store.append_snapshot(snapshot, raw_body=live_response)

    with store._connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM raw_snapshots").fetchone()["n"]

    assert count == 1, "identical bodies must not be archived repeatedly"


def test_raw_snapshot_is_kept_when_the_body_changes(store, live_response):
    store.append_snapshot(parse_usage(live_response, fetched_at=NOW), raw_body=live_response)
    live_response["five_hour"]["utilization"] = 41.0
    store.append_snapshot(parse_usage(live_response, fetched_at=NOW), raw_body=live_response)

    with store._connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM raw_snapshots").fetchone()["n"]

    assert count == 2


def test_known_flag_survives_a_round_trip(store, live_response):
    """Regression: seven_day_fable is identified from limits[] and is not in
    KNOWN_LABELS, so recomputing known-ness on read mislabelled it as
    unrecognized whenever the dashboard was served from the store."""
    store.append_snapshot(parse_usage(live_response, fetched_at=NOW))

    by_key = {s.bucket: s for s in store.latest_per_bucket()}

    assert by_key["seven_day_fable"].known is True
    assert by_key["five_hour"].known is True
    assert by_key["nimbus_quill"].known is False


def test_a_database_predating_the_known_column_is_migrated(tmp_path, live_response):
    import sqlite3

    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            bucket TEXT NOT NULL, label TEXT, utilization REAL NOT NULL,
            resets_at TEXT
        );
        INSERT INTO samples (ts, bucket, label, utilization)
        VALUES ('2026-08-01T00:00:00+00:00', 'five_hour', '5-hour session', 12.0);
        """
    )
    legacy.commit()
    legacy.close()

    store = Store(path)  # must not raise

    assert store.latest_per_bucket()[0].known is True
    assert store.append_snapshot(parse_usage(live_response, fetched_at=NOW)) == 4


def test_restored_buckets_keep_the_live_ordering(client):
    """SQL returns these alphabetically; the dashboard must not reorder on
    restart and file an unrecognized bucket in among the real ones."""
    keys = [b["key"] for b in client.get("/api/now").json()["buckets"]]

    assert keys[0] == "five_hour"
    assert keys[-1] == "nimbus_quill"


def test_restored_buckets_report_the_right_known_flag(client):
    by_key = {b["key"]: b for b in client.get("/api/now").json()["buckets"]}

    assert by_key["seven_day_fable"]["known"] is True
    assert by_key["nimbus_quill"]["known"] is False


def test_a_bucket_that_vanishes_is_not_resurrected(store):
    """Regression: selecting each bucket's newest row independently revived a
    bucket the API had stopped reporting, and staleness is measured from the
    newest sample overall -- so a three-day-old reading was presented as current
    beside fresh ones."""
    old = datetime.now(UTC) - timedelta(days=3)
    store.append_snapshot(
        UsageSnapshot(
            buckets=(
                Bucket("five_hour", "5-hour session", 10.0, None, "session"),
                Bucket("ghost", "Ghost", 99.0, None, "other"),
            ),
            fetched_at=old,
        )
    )
    store.append_snapshot(
        UsageSnapshot(
            buckets=(Bucket("five_hour", "5-hour session", 42.0, None, "session"),),
            fetched_at=datetime.now(UTC),
        )
    )

    restored = {s.bucket: s for s in store.latest_per_bucket()}

    assert set(restored) == {"five_hour"}
    assert restored["five_hour"].utilization == 42.0


def test_history_is_downsampled_to_a_bounded_number_of_points(store):
    """7 days at 60s is 10,080 points per bucket, refetched every minute."""
    now = datetime.now(UTC)
    for i in range(3000):
        store.append_snapshot(
            UsageSnapshot(
                buckets=(Bucket("five_hour", "5-hour session", float(i % 100), None, "session"),),
                fetched_at=now - timedelta(minutes=3000 - i),
            )
        )

    points = store.history(hours=168)

    assert 0 < len(points) <= 720
    assert all(points[i].ts <= points[i + 1].ts for i in range(len(points) - 1))
    # The last reading in the window survives -- "now" must stay accurate.
    assert points[-1].utilization == float(2999 % 100)


def test_downsampling_keeps_the_last_sample_in_each_slot(store):
    """Last, not max: utilization resets to zero, and max would smear a
    pre-reset peak across the drop and erase the sawtooth.

    Also the case that used to depend on the wall clock: with epoch-aligned
    slots these three samples landed in one slot or two according to where the
    current hour happened to fall, so the assertion below held or failed by luck.
    """
    now = datetime.now(UTC)
    for minutes, value in ((30, 90.0), (20, 95.0), (10, 3.0)):
        store.append_snapshot(
            UsageSnapshot(
                buckets=(Bucket("five_hour", "5h", value, None, "session"),),
                fetched_at=now - timedelta(minutes=minutes),
            )
        )

    points = store.history(hours=1, max_points=1)

    assert [p.utilization for p in points] == [3.0]


@pytest.mark.parametrize("max_points", [1, 2, 3, 7, 10, 60, 719, 720])
def test_downsampling_never_exceeds_the_requested_cap(store, max_points):
    """Regression: slots were aligned to Unix-epoch boundaries rather than to
    the query's cutoff, so the requested window always overlapped one extra
    slot and every one of these returned max_points + 1."""
    now = datetime.now(UTC)
    for i in range(180):
        store.append_snapshot(
            UsageSnapshot(
                buckets=(
                    Bucket("five_hour", "5h", float(i % 100), None, "session"),
                    Bucket("seven_day", "Weekly", float(i % 50), None, "weekly"),
                ),
                fetched_at=now - timedelta(minutes=180 - i),
            )
        )

    points = store.history(hours=3, max_points=max_points)
    per_bucket: dict[str, int] = {}
    for point in points:
        per_bucket[point.bucket] = per_bucket.get(point.bucket, 0) + 1

    assert per_bucket, "the window should not come back empty"
    assert set(per_bucket) == {"five_hour", "seven_day"}
    for bucket, count in per_bucket.items():
        assert count <= max_points, f"{bucket} returned {count} points for a cap of {max_points}"


def test_the_newest_sample_survives_every_cap(store):
    """The point the chart labels "now". A cap that dropped it would put a stale
    number under a current label."""
    now = datetime.now(UTC)
    for i in range(120):
        store.append_snapshot(
            UsageSnapshot(
                buckets=(Bucket("five_hour", "5h", float(i), None, "session"),),
                fetched_at=now - timedelta(minutes=120 - i),
            )
        )

    for max_points in (1, 5, 720):
        points = store.history(hours=3, max_points=max_points)
        assert points[-1].utilization == 119.0


def test_a_raw_body_can_be_archived_without_samples(store):
    """The schema-break path: no buckets parsed, so nothing to write to samples,
    but the body that broke the parser is the one worth keeping."""
    store.append_raw({"unrecognizable": True}, ts=NOW)

    with store._connect() as conn:
        rows = conn.execute("SELECT ts, body FROM raw_snapshots").fetchall()

    assert len(rows) == 1
    assert "unrecognizable" in rows[0]["body"]
    assert store.latest_per_bucket() == []


def test_an_empty_snapshot_writes_nothing(store):
    assert store.append_snapshot(parse_usage({})) == 0


def test_prune_drops_only_old_rows(store, live_response):
    store.append_snapshot(parse_usage(live_response, fetched_at=NOW - timedelta(days=120)))
    store.append_snapshot(parse_usage(live_response, fetched_at=datetime.now(UTC)))

    store.prune()

    assert len(store.history(hours=90 * 24)) == 4


# ------------------------------------------------------------------ endpoints


@pytest.fixture
def make_client(tmp_path, live_response, monkeypatch):
    """Builds an app whose poller never runs, seeded at a chosen sample age."""
    monkeypatch.setattr("burnrate.poller.Poller.start", _noop)
    monkeypatch.setattr("burnrate.poller.Poller.stop", _noop)
    counter = {"n": 0}

    def _build(age_seconds: float = 0.0):
        counter["n"] += 1
        config = Config(db_path=tmp_path / f"api{counter['n']}.db", poll_interval=60.0)
        app = create_app(config)
        fetched_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
        app.state.store.append_snapshot(parse_usage(live_response, fetched_at=fetched_at))
        return TestClient(app)

    return _build


@pytest.fixture
def client(make_client):
    with make_client() as test_client:
        yield test_client


async def _noop(*args, **kwargs):
    return None


def test_now_lists_every_bucket(client):
    body = client.get("/api/now").json()

    assert [b["key"] for b in body["buckets"]] == [
        "five_hour",
        "seven_day",
        "seven_day_fable",
        "nimbus_quill",
    ]


def test_a_recent_stored_sample_survives_a_restart_as_fresh(client):
    """Samples are only written on success, so a fresh row means a fresh fetch --
    even if this process has not polled yet."""
    body = client.get("/api/now").json()

    assert body["staleness_seconds"] < 5
    assert body["stale"] is False


def test_an_old_sample_is_reported_stale(make_client):
    with make_client(age_seconds=600) as client:
        body = client.get("/api/now").json()

    assert body["staleness_seconds"] == pytest.approx(600, abs=5)
    assert body["stale"] is True
    # The numbers are still served -- flagged, not withheld.
    assert body["buckets"]


def test_now_includes_a_projection(client):
    body = client.get("/api/now").json()

    assert body["projection"]["bucket_key"] == "seven_day"
    assert body["projection"]["status"] in {"projected", "clears_reset", "insufficient_data"}


def test_no_endpoint_leaks_the_token(client):
    for path in ("/api/now", "/api/history?hours=24", "/api/healthz"):
        text = client.get(path).text
        assert "sk-ant" not in text
        assert "Authorization" not in text
        assert "accessToken" not in text


def test_history_advertises_its_downsampling_cap(client):
    body = client.get("/api/history?hours=168").json()

    assert body["max_points_per_bucket"] == 720


def test_history_groups_points_by_bucket(client):
    body = client.get("/api/history?hours=168").json()

    assert {s["key"] for s in body["series"]} == {
        "five_hour",
        "seven_day",
        "seven_day_fable",
        "nimbus_quill",
    }
    assert all(len(s["points"]) == 1 for s in body["series"])


@pytest.mark.parametrize("hours", [0, -5, 90 * 24 + 1, "abc"])
def test_history_rejects_out_of_range_windows(client, hours):
    assert client.get(f"/api/history?hours={hours}").status_code == 422


def test_index_is_served_at_the_root(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "burnrate" in response.text
