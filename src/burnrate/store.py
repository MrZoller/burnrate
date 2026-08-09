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

from . import attribution
from .attribution import ParseStats, Turn
from .redact import scrub_json
from .usage import UsageSnapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    bucket      TEXT    NOT NULL,
    label       TEXT,
    utilization REAL    NOT NULL,
    resets_at   TEXT,
    -- Whether the parser recognized this bucket. Persisted rather than
    -- recomputed on read: a scoped bucket like seven_day_fable is identified
    -- from limits[] at runtime and is not in KNOWN_LABELS, so recomputing it
    -- would mark a perfectly well-understood bucket "unrecognized" whenever
    -- the dashboard is served from the store.
    known       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_samples_bucket_ts ON samples (bucket, ts);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples (ts);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_snapshots (ts);

-- Local token attribution (issue #16), rolled up from ~/.claude/projects JSONLs.
-- Pre-aggregated rather than one row per assistant turn: the transcript tree is
-- hundreds of MB, and the panels only ever ask for windowed sums.

-- Token totals per hour, split by the dimensions the panels group on. Summed in on
-- each aggregation pass via UPSERT; the per-file watermark below guarantees every
-- turn is folded in exactly once, so the running totals never double-count.
-- large_context_tokens is the subset of this row's tokens contributed by turns whose
-- own context was large, tracked here (not on the session) so the large-context share
-- is genuinely bounded by the 24h/7d window rather than leaking a session's lifetime.
CREATE TABLE IF NOT EXISTS hourly_usage (
    hour_start            TEXT    NOT NULL,
    project               TEXT    NOT NULL,
    model                 TEXT    NOT NULL,
    is_sidechain          INTEGER NOT NULL,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    large_context_tokens  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_start, project, model, is_sidechain)
);
CREATE INDEX IF NOT EXISTS idx_hourly_hour ON hourly_usage (hour_start);

-- One row per session, extended as later turns of the same session arrive. Feeds the
-- "longest sessions active in the window" list, whose durations and lifetime token
-- totals the hourly rollup cannot express (a session spans many hours). These totals
-- are session LIFETIME, not windowed -- the panel labels them as such.
CREATE TABLE IF NOT EXISTS sessions_rollup (
    session_id       TEXT    PRIMARY KEY,
    project          TEXT    NOT NULL,
    model            TEXT    NOT NULL,
    start_ts         TEXT    NOT NULL,
    end_ts           TEXT    NOT NULL,
    total_tokens     INTEGER NOT NULL DEFAULT 0,
    max_turn_context INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_end ON sessions_rollup (end_ts);

-- How far each JSONL has been consumed, so an aggregation pass reads only the bytes
-- appended since last time. `size`/`mtime` are diagnostics; `offset` is the contract.
CREATE TABLE IF NOT EXISTS jsonl_watermarks (
    path   TEXT PRIMARY KEY,
    offset INTEGER NOT NULL,
    size   INTEGER,
    mtime  REAL
);
"""

# Gross tokens for a hourly_usage row, the figure every panel ranks and shares on.
_HOURLY_TOKENS = "(input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens)"

# A single turn is "large context" once its own context -- fresh input plus what it
# read back from cache -- reaches this. 200k is the standard context window, so a turn
# at or above it was working near the top of the window. This is a PER-TURN test
# applied as each turn is folded in, and the qualifying turn's tokens are summed into
# hourly_usage.large_context_tokens, which is what makes the share windowable. Reported
# in the API response so the threshold is never hidden behind the share it produces.
LARGE_CONTEXT_TOKENS = 200_000

# Attribution aggregates are kept this long: comfortably past the 7-day panel window,
# and the fold below drops turns older than this so the first pass over a months-deep
# tree never balloons memory with hours nobody will query.
ATTRIBUTION_RETENTION_DAYS = 30

# Points per bucket returned by history(). At a 60s poll a 7-day window holds
# 10,080 samples per bucket, and the browser refetches every minute; 90 days is
# 129,600. The chart cannot resolve more than a few hundred pixels of width, so
# the rest is pure transfer and render cost.
MAX_POINTS_PER_BUCKET = 720

SAMPLE_RETENTION_DAYS = 90
RAW_RETENTION_DAYS = 14


@dataclass(frozen=True)
class Sample:
    ts: datetime
    bucket: str
    utilization: float
    resets_at: datetime | None = None
    label: str | None = None
    known: bool = True


@dataclass
class AggregateStats:
    """What one attribution pass did, for the log and the tests."""

    files_scanned: int = 0
    files_with_new_data: int = 0
    lines: int = 0
    malformed: int = 0
    emitted: int = 0


@dataclass
class _SessionAcc:
    """A session's running rollup while a single pass folds turns into it."""

    project: str
    model: str
    start_ts: datetime
    end_ts: datetime
    total_tokens: int
    max_turn_context: int


class Store:
    """Append-only history of usage samples."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a database was first created."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(samples)")}
        if "known" not in columns:
            conn.execute("ALTER TABLE samples ADD COLUMN known INTEGER NOT NULL DEFAULT 1")
        # large_context_tokens was added to hourly_usage after the first cut of the
        # attribution rollup; add it to a database that already has the table without it.
        hourly_columns = {row["name"] for row in conn.execute("PRAGMA table_info(hourly_usage)")}
        if hourly_columns and "large_context_tokens" not in hourly_columns:
            conn.execute(
                "ALTER TABLE hourly_usage"
                " ADD COLUMN large_context_tokens INTEGER NOT NULL DEFAULT 0"
            )

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
                1 if bucket.known else 0,
            )
            for bucket in snapshot.buckets
        ]
        if not rows:
            return 0

        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO samples (ts, bucket, label, utilization, resets_at, known)"
                " VALUES (?, ?, ?, ?, ?, ?)",
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
        # Scrubbed on the way in, because this row is the one place the token is
        # forbidden to reach and the only path that was not already covered: an
        # error excerpt is scrubbed by the client, but a decoded payload with
        # buckets in it went to json.dumps untouched. Nothing observed echoes the
        # credential today -- the reason to do it here is that this archive exists
        # to capture response shapes we have not seen, and a field echoing the
        # token back is exactly such a shape.
        try:
            body = json.dumps(scrub_json(raw_body), sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return

        previous = conn.execute(
            "SELECT body FROM raw_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if previous is not None and previous["body"] == body:
            return

        conn.execute("INSERT INTO raw_snapshots (ts, body) VALUES (?, ?)", (_iso(ts), body))

    def append_raw(self, raw_body: Any, ts: datetime | None = None) -> None:
        """Archive a response body with no samples attached.

        `append_snapshot` records the body next to the samples it produced, which
        covers every readable response and none of the unreadable ones -- a
        schema break yields no buckets, so that path writes nothing at all. That
        inverts the archive's whole purpose: the one response worth keeping is
        the one that broke, and it was the only one being dropped. Hence a second
        door in.
        """
        with self._connect() as conn:
            self._append_raw_if_changed(conn, ts or datetime.now(UTC), raw_body)

    def latest_per_bucket(self) -> list[Sample]:
        """Every bucket from the most recent snapshot -- and nothing else.

        Deliberately not "each bucket's newest row": that resurrects a bucket
        the API has stopped reporting, and since staleness is measured from the
        newest sample overall, the ghost is then presented as current beside
        genuinely fresh readings. A bucket that vanished must disappear from the
        dashboard, not linger at whatever value it last held.

        Every bucket in one snapshot shares its fetched_at, so the newest ts
        identifies the snapshot exactly.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, bucket, label, utilization, resets_at, known
                FROM samples
                WHERE ts = (SELECT MAX(ts) FROM samples)
                ORDER BY bucket
                """
            ).fetchall()
        return [_row_to_sample(row) for row in rows]

    def history(
        self, hours: float = 168.0, max_points: int = MAX_POINTS_PER_BUCKET
    ) -> list[Sample]:
        """Samples from the last `hours`, downsampled, oldest first.

        The window is divided into at most `max_points` slots and the LAST
        sample in each slot is kept. Last rather than max: utilization is a step
        function that resets to zero, so the final reading in a slot is what was
        actually true at that time, where max would smear a pre-reset peak
        across the drop and erase the sawtooth the 5-hour bucket is made of.

        Slots are anchored to the query's own cutoff, not to Unix-epoch
        boundaries. Epoch-aligned slots are the same width but sit at an
        arbitrary offset inside the requested window, so the window overlaps one
        extra slot and the cap is exceeded by one -- measured at every
        max_points tried, not just at unlucky ones. It also made the count
        depend on the wall-clock minute the query ran, which is not something a
        test can pin down. The clamp closes the remaining boundary case, where a
        sample landing on the final instant of the window would otherwise earn a
        slot of its own.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        slots = max(1, max_points)
        slot_seconds = max(1.0, (hours * 3600.0) / slots)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, bucket, label, utilization, resets_at, known
                FROM samples
                WHERE id IN (
                    SELECT MAX(id) FROM samples
                    WHERE ts >= ?
                    GROUP BY bucket, MIN(
                        -- COALESCE, because strftime yields NULL on a timestamp
                        -- it cannot parse and a NULL group would be one more
                        -- slot than the cap allows.
                        COALESCE(
                            CAST((CAST(strftime('%s', ts) AS INTEGER) - ?) / ? AS INTEGER),
                            0
                        ),
                        ?
                    )
                )
                ORDER BY ts ASC
                """,
                (_iso(cutoff), int(cutoff.timestamp()), slot_seconds, slots - 1),
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
        attribution_days: int = ATTRIBUTION_RETENTION_DAYS,
    ) -> None:
        """Drop history past the retention windows."""
        now = datetime.now(UTC)
        attribution_cutoff = _iso(now - timedelta(days=attribution_days))
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM samples WHERE ts < ?", (_iso(now - timedelta(days=sample_days)),)
            )
            conn.execute(
                "DELETE FROM raw_snapshots WHERE ts < ?", (_iso(now - timedelta(days=raw_days)),)
            )
            conn.execute("DELETE FROM hourly_usage WHERE hour_start < ?", (attribution_cutoff,))
            # By end_ts: a session that was still active inside the window is kept whole
            # even if it opened before the cutoff, so its span is not truncated.
            conn.execute("DELETE FROM sessions_rollup WHERE end_ts < ?", (attribution_cutoff,))

    # ------------------------------------------------------------ attribution

    def aggregate_jsonl(
        self, root: Path | str, retention_days: int = ATTRIBUTION_RETENTION_DAYS
    ) -> AggregateStats:
        """Fold new lines from every JSONL under ``root`` into the rollup tables.

        Incremental and exactly-once: each file is read only past its stored offset,
        and the offsets advance in the same transaction as the sums they produced, so
        a crash mid-pass commits nothing and the next pass simply re-reads the same
        bytes. Turns older than the retention window are counted but not stored, which
        both matches what ``prune`` would drop and keeps the first pass over a
        months-deep tree from accumulating hours nobody queries. Never re-reads a file
        that has not grown.
        """
        min_ts = datetime.now(UTC) - timedelta(days=retention_days)
        watermarks = self._load_watermarks()

        hourly: dict[tuple[str, str, str, int], list[int]] = {}
        sessions: dict[str, _SessionAcc] = {}
        offsets: dict[str, tuple[int, int | None, float | None]] = {}
        stats = AggregateStats()

        for path in attribution.iter_jsonl_files(root):
            stats.files_scanned += 1
            key = str(path)
            offset = watermarks.get(key, 0)
            lines, new_offset = attribution.read_new_lines(path, offset)
            if not lines:
                continue
            stats.files_with_new_data += 1

            pass_stats = ParseStats()
            for turn in attribution.parse_lines(lines, pass_stats):
                if turn.ts >= min_ts:
                    _fold_turn(hourly, sessions, turn)
            stats.lines += pass_stats.lines
            stats.malformed += pass_stats.malformed
            stats.emitted += pass_stats.emitted

            try:
                info = path.stat()
                offsets[key] = (new_offset, info.st_size, info.st_mtime)
            except OSError:
                offsets[key] = (new_offset, None, None)

        if not offsets:
            return stats

        with self._connect() as conn:
            self._flush_hourly(conn, hourly)
            self._flush_sessions(conn, sessions)
            self._flush_watermarks(conn, offsets)
        return stats

    def _load_watermarks(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT path, offset FROM jsonl_watermarks").fetchall()
        return {row["path"]: row["offset"] for row in rows}

    @staticmethod
    def _flush_hourly(
        conn: sqlite3.Connection, hourly: dict[tuple[str, str, str, int], list[int]]
    ) -> None:
        if not hourly:
            return
        conn.executemany(
            "INSERT INTO hourly_usage"
            " (hour_start, project, model, is_sidechain,"
            "  input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,"
            "  large_context_tokens)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(hour_start, project, model, is_sidechain) DO UPDATE SET"
            "  input_tokens = input_tokens + excluded.input_tokens,"
            "  output_tokens = output_tokens + excluded.output_tokens,"
            "  cache_creation_tokens = cache_creation_tokens + excluded.cache_creation_tokens,"
            "  cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,"
            "  large_context_tokens = large_context_tokens + excluded.large_context_tokens",
            [
                (hour, project, model, sidechain, tok[0], tok[1], tok[2], tok[3], tok[4])
                for (hour, project, model, sidechain), tok in hourly.items()
            ],
        )

    @staticmethod
    def _flush_sessions(conn: sqlite3.Connection, sessions: dict[str, _SessionAcc]) -> None:
        if not sessions:
            return
        conn.executemany(
            "INSERT INTO sessions_rollup"
            " (session_id, project, model, start_ts, end_ts, total_tokens, max_turn_context)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET"
            # start/end widen to cover the turns seen across passes; the token total
            # accumulates, and max_turn_context keeps the deepest turn ever recorded.
            # model follows the latest turn -- a session can switch models mid-way, and
            # this keeps the stored value agreeing with _fold_turn's last-seen choice
            # across passes as well as within one.
            "  model = excluded.model,"
            "  start_ts = MIN(sessions_rollup.start_ts, excluded.start_ts),"
            "  end_ts = MAX(sessions_rollup.end_ts, excluded.end_ts),"
            "  total_tokens = total_tokens + excluded.total_tokens,"
            "  max_turn_context = MAX(sessions_rollup.max_turn_context, excluded.max_turn_context)",
            [
                (
                    session_id,
                    acc.project,
                    acc.model,
                    _iso(acc.start_ts),
                    _iso(acc.end_ts),
                    acc.total_tokens,
                    acc.max_turn_context,
                )
                for session_id, acc in sessions.items()
            ],
        )

    @staticmethod
    def _flush_watermarks(
        conn: sqlite3.Connection, offsets: dict[str, tuple[int, int | None, float | None]]
    ) -> None:
        conn.executemany(
            "INSERT INTO jsonl_watermarks (path, offset, size, mtime) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET"
            "  offset = excluded.offset, size = excluded.size, mtime = excluded.mtime",
            [(path, off, size, mtime) for path, (off, size, mtime) in offsets.items()],
        )

    def attribution_totals(self, hours: float) -> dict[str, Any]:
        """Windowed token totals, grouped for the by-project/model/agent panels."""
        cutoff = _iso(datetime.now(UTC) - timedelta(hours=hours))
        with self._connect() as conn:
            breakdown = conn.execute(
                "SELECT"
                "  COALESCE(SUM(input_tokens), 0) AS input_tokens,"
                "  COALESCE(SUM(output_tokens), 0) AS output_tokens,"
                "  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,"
                "  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,"
                "  COALESCE(SUM(large_context_tokens), 0) AS large_context_tokens"
                " FROM hourly_usage WHERE hour_start >= ?",
                (cutoff,),
            ).fetchone()
            by_project = conn.execute(
                f"SELECT project AS name, SUM({_HOURLY_TOKENS}) AS tokens"
                " FROM hourly_usage WHERE hour_start >= ?"
                " GROUP BY project ORDER BY tokens DESC",
                (cutoff,),
            ).fetchall()
            by_model = conn.execute(
                f"SELECT model AS name, SUM({_HOURLY_TOKENS}) AS tokens"
                " FROM hourly_usage WHERE hour_start >= ?"
                " GROUP BY model ORDER BY tokens DESC",
                (cutoff,),
            ).fetchall()
            by_agent = conn.execute(
                f"SELECT is_sidechain, SUM({_HOURLY_TOKENS}) AS tokens"
                " FROM hourly_usage WHERE hour_start >= ?"
                " GROUP BY is_sidechain",
                (cutoff,),
            ).fetchall()
        # large_context_tokens is a subset of the four token columns, so it is pulled
        # out of the breakdown here to serve the windowed large-context share directly.
        large_context_tokens = breakdown["large_context_tokens"]
        return {
            "breakdown": {k: breakdown[k] for k in breakdown.keys() if k != "large_context_tokens"},
            "large_context_tokens": large_context_tokens,
            "by_project": [(row["name"], row["tokens"] or 0) for row in by_project],
            "by_model": [(row["name"], row["tokens"] or 0) for row in by_model],
            "by_agent": {int(row["is_sidechain"]): row["tokens"] or 0 for row in by_agent},
        }

    def attribution_sessions(self, hours: float) -> list[dict[str, Any]]:
        """Every session active within the window, newest activity first.

        Active means it has a turn inside the window (``end_ts`` at or past the
        cutoff). Totals here are session LIFETIME, not windowed -- the caller uses
        them only for the descriptive "longest sessions active in this window" list,
        which labels its numbers as lifetime. The windowed large-context share does
        NOT come from here; it comes from hourly_usage.large_context_tokens.
        """
        cutoff = _iso(datetime.now(UTC) - timedelta(hours=hours))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id, project, model, start_ts, end_ts, total_tokens,"
                " max_turn_context FROM sessions_rollup"
                " WHERE end_ts >= ? ORDER BY end_ts DESC",
                (cutoff,),
            ).fetchall()
        return [
            {
                "session_id": row["session_id"],
                "project": row["project"],
                "model": row["model"],
                "start_ts": _parse_iso(row["start_ts"]),
                "end_ts": _parse_iso(row["end_ts"]),
                "total_tokens": row["total_tokens"],
                "max_turn_context": row["max_turn_context"],
            }
            for row in rows
        ]


def _fold_turn(
    hourly: dict[tuple[str, str, str, int], list[int]],
    sessions: dict[str, _SessionAcc],
    turn: Turn,
) -> None:
    """Accumulate one turn into the in-memory hourly and session rollups."""
    hour = turn.ts.replace(minute=0, second=0, microsecond=0)
    key = (_iso(hour) or "", turn.project, turn.model, 1 if turn.is_sidechain else 0)
    tokens = hourly.setdefault(key, [0, 0, 0, 0, 0])
    tokens[0] += turn.input_tokens
    tokens[1] += turn.output_tokens
    tokens[2] += turn.cache_creation_tokens
    tokens[3] += turn.cache_read_tokens
    # The large-context subset: this turn's whole token count counts toward the hour's
    # large_context_tokens only when the turn itself worked near the top of the window.
    if turn.context_tokens >= LARGE_CONTEXT_TOKENS:
        tokens[4] += turn.total_tokens

    acc = sessions.get(turn.session_id)
    if acc is None:
        sessions[turn.session_id] = _SessionAcc(
            project=turn.project,
            model=turn.model,
            start_ts=turn.ts,
            end_ts=turn.ts,
            total_tokens=turn.total_tokens,
            max_turn_context=turn.context_tokens,
        )
        return
    acc.start_ts = min(acc.start_ts, turn.ts)
    acc.end_ts = max(acc.end_ts, turn.ts)
    acc.total_tokens += turn.total_tokens
    acc.max_turn_context = max(acc.max_turn_context, turn.context_tokens)
    # Keep the model of the most recent turn seen in this pass; project is stable per
    # session (one working directory), so the first-seen value already stands.
    acc.model = turn.model


def _row_to_sample(row: sqlite3.Row) -> Sample:
    return Sample(
        ts=_parse_iso(row["ts"]) or datetime.now(UTC),
        bucket=row["bucket"],
        label=row["label"],
        utilization=row["utilization"],
        resets_at=_parse_iso(row["resets_at"]),
        known=bool(row["known"]),
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
