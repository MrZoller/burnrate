"""SQLite sample store.

One row per bucket per successful poll, plus a deduplicated archive of raw
response bodies. The raw archive is cheap insurance: when this undocumented
endpoint changes shape, the recordings of what it used to return are what make
the change diagnosable after the fact.

Connections are opened per operation rather than shared. At one poll a minute
the overhead is irrelevant, and it sidesteps SQLite's thread-affinity rules
between the background poller and the request handlers.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .usage import UsageSnapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    bucket      TEXT    NOT NULL,
    label       TEXT,
    utilization REAL    NOT NULL,
    resets_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_bucket_ts ON samples (bucket, ts);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples (ts);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_snapshots (ts);
"""

SAMPLE_RETENTION_DAYS = 90
RAW_RETENTION_DAYS = 14


@dataclass(frozen=True)
class Sample:
    ts: datetime
    bucket: str
    utilization: float
    resets_at: datetime | None = None
    label: str | None = None


class Store:
    """Append-only history of usage samples."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def append_snapshot(self, snapshot: UsageSnapshot, raw_body: Any = None) -> int:
        """Record every bucket in `snapshot`. Returns the number of rows written."""
        ts = snapshot.fetched_at or datetime.now(UTC)
        rows = [
            (
                _iso(ts),
                bucket.key,
                bucket.label,
                bucket.utilization,
                _iso(bucket.resets_at),
            )
            for bucket in snapshot.buckets
        ]
        if not rows:
            return 0

        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO samples (ts, bucket, label, utilization, resets_at)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            if raw_body is not None:
                self._append_raw_if_changed(conn, ts, raw_body)
        return len(rows)

    def _append_raw_if_changed(self, conn: sqlite3.Connection, ts: datetime, raw_body: Any) -> None:
        """Store the body only when it differs from the previous one.

        Utilization moves in whole percents, so most polls return a byte-identical
        body. Skipping the duplicates keeps the archive to a few hundred rows a
        week instead of ten thousand.
        """
        try:
            body = json.dumps(raw_body, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return

        previous = conn.execute(
            "SELECT body FROM raw_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if previous is not None and previous["body"] == body:
            return

        conn.execute("INSERT INTO raw_snapshots (ts, body) VALUES (?, ?)", (_iso(ts), body))

    def latest_per_bucket(self) -> list[Sample]:
        """The most recent sample for each bucket seen."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.ts, s.bucket, s.label, s.utilization, s.resets_at
                FROM samples s
                JOIN (
                    SELECT bucket, MAX(id) AS id FROM samples GROUP BY bucket
                ) latest ON latest.id = s.id
                ORDER BY s.bucket
                """
            ).fetchall()
        return [_row_to_sample(row) for row in rows]

    def history(self, hours: float = 168.0) -> list[Sample]:
        """Every sample newer than `hours` ago, oldest first."""
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, bucket, label, utilization, resets_at FROM samples"
                " WHERE ts >= ? ORDER BY ts ASC",
                (_iso(cutoff),),
            ).fetchall()
        return [_row_to_sample(row) for row in rows]

    def latest_sample_time(self) -> datetime | None:
        """Timestamp of the newest sample, or None when the store is empty."""
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(ts) AS ts FROM samples").fetchone()
        if row is None or row["ts"] is None:
            return None
        return _parse_iso(row["ts"])

    def prune(
        self,
        sample_days: int = SAMPLE_RETENTION_DAYS,
        raw_days: int = RAW_RETENTION_DAYS,
    ) -> None:
        """Drop history past the retention windows."""
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM samples WHERE ts < ?", (_iso(now - timedelta(days=sample_days)),)
            )
            conn.execute(
                "DELETE FROM raw_snapshots WHERE ts < ?", (_iso(now - timedelta(days=raw_days)),)
            )


def _row_to_sample(row: sqlite3.Row) -> Sample:
    return Sample(
        ts=_parse_iso(row["ts"]) or datetime.now(UTC),
        bucket=row["bucket"],
        label=row["label"],
        utilization=row["utilization"],
        resets_at=_parse_iso(row["resets_at"]),
    )


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
