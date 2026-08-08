"""Store round-trips and the two JSON endpoints."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from burnrate.app import create_app
from burnrate.config import Config
from burnrate.store import Store
from burnrate.usage import parse_usage

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
