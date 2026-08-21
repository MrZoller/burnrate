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
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import attribution
from .attribution import ParseStats, Turn
from .config import normalize_projects_root
from .redact import scrub_json
from .usage import UsageSnapshot

SCHEMA = """
BEGIN IMMEDIATE;
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
    projects_root         TEXT    NOT NULL,
    hour_start            TEXT    NOT NULL,
    project               TEXT    NOT NULL,
    model                 TEXT    NOT NULL,
    is_sidechain          INTEGER NOT NULL,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    large_context_tokens  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (projects_root, hour_start, project, model, is_sidechain)
);

-- One row per session, extended as later turns of the same session arrive. Feeds the
-- "longest sessions active in the window" list, whose durations and lifetime token
-- totals the hourly rollup cannot express (a session spans many hours). These totals
-- are session LIFETIME, not windowed -- the panel labels them as such.
CREATE TABLE IF NOT EXISTS sessions_rollup (
    projects_root    TEXT    NOT NULL,
    session_id       TEXT    NOT NULL,
    project          TEXT    NOT NULL,
    model            TEXT    NOT NULL,
    start_ts         TEXT    NOT NULL,
    end_ts           TEXT    NOT NULL,
    total_tokens     INTEGER NOT NULL DEFAULT 0,
    max_turn_context INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (projects_root, session_id)
);

-- How far each JSONL has been consumed, so an aggregation pass reads only the bytes
-- appended since last time. `size`/`mtime` are diagnostics; `offset` is the contract.
CREATE TABLE IF NOT EXISTS jsonl_watermarks (
    projects_root TEXT NOT NULL,
    path   TEXT NOT NULL,
    offset INTEGER NOT NULL,
    size   INTEGER,
    mtime  REAL,
    PRIMARY KEY (projects_root, path)
);

-- Stable API identities for assistant responses already included in the additive
-- rollups. Claude Code can copy one response into multiple transcript files during
-- resume/fork/compaction, where per-file watermarks alone cannot prevent duplicates.
CREATE TABLE IF NOT EXISTS response_identities (
    projects_root TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    request_id    TEXT NOT NULL,
    response_ts   TEXT NOT NULL,
    -- An old response remains deduplicable while its still-active session is
    -- visible. Its own timestamp can predate the hourly retention window.
    session_id    TEXT,
    PRIMARY KEY (projects_root, message_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_response_identities_ts
    ON response_identities (response_ts);

-- A pre-dedup database has watermarks but no response identities. Keep the
-- migration marker after upgrading so each configured transcript root can seed
-- its own index the first time it is scanned.
CREATE TABLE IF NOT EXISTS attribution_migrations (
    name TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS response_identity_backfills (
    projects_root TEXT PRIMARY KEY
);

-- Version 1 separates exceptional filesystem byte identities from ordinary TEXT
-- values. A pre-version database is rebuilt through an invisible staging namespace.
CREATE TABLE IF NOT EXISTS attribution_identity_state (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS attribution_rebuilds (
    projects_root BLOB PRIMARY KEY
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

# Rows written before rollups were namespaced have no trustworthy root provenance.
# Keep them rather than deleting user history, but never guess that the root active
# during an upgrade owns them and silently mix them into its totals.
LEGACY_PROJECTS_ROOT = "legacy-unscoped:v1"

_FILESYSTEM_IDENTITY_PREFIX = b"\x00burnrate-filesystem:v1\x00"
_DERIVED_IDENTITY_PREFIX = b"\x00burnrate-attribution:v1\x00"
_ATTRIBUTION_IDENTITY_VERSION = 1
_FILESYSTEM_IDENTITY_REBUILD = "filesystem-identities-v1-rebuild"

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
    scan_succeeded: bool = True


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
            # ``executescript`` creates new tables before ``_migrate`` can inspect
            # them. Remember this beforehand: an existing database without this
            # table has already advanced its watermarks past responses whose
            # identities must be recovered before a later fork can be deduplicated.
            # SCHEMA starts a transaction, so its new tables and this marker commit
            # together. A shutdown cannot leave a table that suppresses the backfill
            # without the marker that requests it.
            had_response_identities = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'response_identities'"
            ).fetchone()
            had_watermarks = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jsonl_watermarks'"
            ).fetchone()
            had_attribution = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'hourly_usage'"
            ).fetchone()
            conn.executescript(SCHEMA)
            if had_response_identities is None and had_watermarks is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO attribution_migrations (name) VALUES (?)",
                    ("response-identities-v1",),
                )
            self._migrate(conn)
            self._migrate_filesystem_identities(conn, had_attribution is not None)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a database was first created."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(samples)")}
        if "known" not in columns:
            conn.execute("ALTER TABLE samples ADD COLUMN known INTEGER NOT NULL DEFAULT 1")
        response_identity_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(response_identities)")
        }
        if response_identity_columns and "session_id" not in response_identity_columns:
            conn.execute("ALTER TABLE response_identities ADD COLUMN session_id TEXT")
        hourly_columns = {row["name"] for row in conn.execute("PRAGMA table_info(hourly_usage)")}
        if hourly_columns and "projects_root" not in hourly_columns:
            large_context = (
                "large_context_tokens" if "large_context_tokens" in hourly_columns else "0"
            )
            conn.execute(
                "CREATE TABLE hourly_usage_namespaced ("
                " projects_root TEXT NOT NULL, hour_start TEXT NOT NULL,"
                " project TEXT NOT NULL, model TEXT NOT NULL, is_sidechain INTEGER NOT NULL,"
                " input_tokens INTEGER NOT NULL DEFAULT 0,"
                " output_tokens INTEGER NOT NULL DEFAULT 0,"
                " cache_creation_tokens INTEGER NOT NULL DEFAULT 0,"
                " cache_read_tokens INTEGER NOT NULL DEFAULT 0,"
                " large_context_tokens INTEGER NOT NULL DEFAULT 0,"
                " PRIMARY KEY (projects_root, hour_start, project, model, is_sidechain))"
            )
            conn.execute(
                "INSERT INTO hourly_usage_namespaced SELECT ?, hour_start, project, model,"
                " is_sidechain, input_tokens, output_tokens, cache_creation_tokens,"
                f" cache_read_tokens, {large_context} FROM hourly_usage",
                (LEGACY_PROJECTS_ROOT,),
            )
            conn.execute("DROP TABLE hourly_usage")
            conn.execute("ALTER TABLE hourly_usage_namespaced RENAME TO hourly_usage")

        session_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(sessions_rollup)")
        }
        if session_columns and "projects_root" not in session_columns:
            conn.execute(
                "CREATE TABLE sessions_rollup_namespaced ("
                " projects_root TEXT NOT NULL, session_id TEXT NOT NULL,"
                " project TEXT NOT NULL, model TEXT NOT NULL, start_ts TEXT NOT NULL,"
                " end_ts TEXT NOT NULL, total_tokens INTEGER NOT NULL DEFAULT 0,"
                " max_turn_context INTEGER NOT NULL DEFAULT 0,"
                " PRIMARY KEY (projects_root, session_id))"
            )
            conn.execute(
                "INSERT INTO sessions_rollup_namespaced SELECT ?, session_id, project, model,"
                " start_ts, end_ts, total_tokens, max_turn_context FROM sessions_rollup",
                (LEGACY_PROJECTS_ROOT,),
            )
            conn.execute("DROP TABLE sessions_rollup")
            conn.execute("ALTER TABLE sessions_rollup_namespaced RENAME TO sessions_rollup")

        watermark_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(jsonl_watermarks)")
        }
        if watermark_columns and "projects_root" not in watermark_columns:
            conn.execute(
                "CREATE TABLE jsonl_watermarks_namespaced ("
                " projects_root TEXT NOT NULL, path TEXT NOT NULL, offset INTEGER NOT NULL,"
                " size INTEGER, mtime REAL, PRIMARY KEY (projects_root, path))"
            )
            conn.execute(
                "INSERT INTO jsonl_watermarks_namespaced SELECT ?, path, offset, size, mtime"
                " FROM jsonl_watermarks",
                (LEGACY_PROJECTS_ROOT,),
            )
            conn.execute("DROP TABLE jsonl_watermarks")
            conn.execute("ALTER TABLE jsonl_watermarks_namespaced RENAME TO jsonl_watermarks")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hourly_root_hour"
            " ON hourly_usage (projects_root, hour_start)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_root_end"
            " ON sessions_rollup (projects_root, end_ts)"
        )

    @staticmethod
    def _migrate_filesystem_identities(conn: sqlite3.Connection, had_attribution: bool) -> None:
        """Quarantine attribution whose old filesystem identities may be ambiguous.

        There is no reversible interpretation of a pre-v1 ``\\udcXX`` spelling: it
        may be an escaped filesystem byte or those literal filename characters. Keep
        every old row under a type-separated quarantine key, then let each configured
        root rebuild from its transcripts. Fresh databases get the version marker but
        no rebuild requirement, so their first scan remains the normal incremental one.
        """
        current = conn.execute("SELECT version FROM attribution_identity_state").fetchone()
        if current is not None:
            return

        if had_attribution:
            tables = (
                "hourly_usage",
                "sessions_rollup",
                "jsonl_watermarks",
                "response_identities",
                "response_identity_backfills",
            )
            roots: set[str | bytes] = set()
            for table in tables:
                roots.update(
                    row[0] for row in conn.execute(f"SELECT DISTINCT projects_root FROM {table}")
                )
            for root in roots:
                quarantined = _derived_identity(b"quarantine", root)
                for table in tables:
                    conn.execute(
                        f"UPDATE {table} SET projects_root = ? WHERE projects_root = ?",
                        (quarantined, root),
                    )
            conn.execute(
                "INSERT OR IGNORE INTO attribution_migrations (name) VALUES (?)",
                (_FILESYSTEM_IDENTITY_REBUILD,),
            )

        conn.execute(
            "INSERT INTO attribution_identity_state (version) VALUES (?)",
            (_ATTRIBUTION_IDENTITY_VERSION,),
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
            # Identities follow the session lifetime, not the hourly window: an old
            # turn remains part of a live session's lifetime total and a later
            # resume/fork copy must not add it again. Pre-session-id rows are from
            # the original index schema, so retain their old bounded behavior.
            conn.execute(
                "DELETE FROM response_identities AS identities"
                " WHERE (session_id IS NULL AND response_ts < ?)"
                " OR (session_id IS NOT NULL AND NOT EXISTS ("
                "   SELECT 1 FROM sessions_rollup AS sessions"
                "   WHERE sessions.projects_root = identities.projects_root"
                "     AND sessions.session_id = identities.session_id"
                "     AND sessions.end_ts >= ?))",
                (attribution_cutoff, attribution_cutoff),
            )

    # ------------------------------------------------------------ attribution

    def aggregate_jsonl(
        self, root: Path | str, retention_days: int = ATTRIBUTION_RETENTION_DAYS
    ) -> AggregateStats:
        """Fold new lines from every JSONL under ``root`` into the rollup tables.

        Incremental and exactly-once: each file is read only past its stored offset,
        and the offsets advance in the same transaction as the sums they produced, so
        a crash mid-pass commits nothing and the next pass simply re-reads the same
        bytes. A file is drained in bounded chunks (see ``read_new_lines``) so a huge
        one never loads whole into memory.

        The retention cutoff is applied to the HOURLY fold ONLY. Sessions are folded
        regardless of a turn's age, because a still-active long session's early turns
        belong in its lifetime span and total -- gating those out truncated the very
        number the "longest sessions" panel labels as lifetime. The sessions dict is
        bounded by session count, not by time, and ``prune`` drops fully-inactive
        sessions by ``end_ts`` anyway. Never re-reads a file that has not grown.
        """
        min_ts = datetime.now(UTC) - timedelta(days=retention_days)
        projects_root = _projects_root_identity(root)
        root = normalize_projects_root(root)
        if self._attribution_rebuild_required(projects_root):
            return self._rebuild_attribution(root, projects_root, min_ts)
        watermarks = self._load_watermarks(projects_root)
        self._backfill_response_identities(root, projects_root, watermarks)
        seen_responses = self._load_response_identities(projects_root, min_ts)

        hourly: dict[tuple[str, str, str, int], list[int]] = {}
        sessions: dict[str | bytes, _SessionAcc] = {}
        offsets: dict[str | bytes, tuple[int, int | None, float | None]] = {}
        new_responses: dict[tuple[str, str], tuple[datetime, str | bytes]] = {}
        stats = AggregateStats()

        # The scan is the iteration: making a second traversal would let a disappearing
        # root turn a successful discovery into an empty, falsely fresh aggregation.
        paths, stats.scan_succeeded = attribution.scan_jsonl_files(root)
        for path in paths:
            stats.files_scanned += 1
            # SQLite cannot bind surrogate-bearing TEXT. Exceptional paths instead use
            # versioned BLOB identities, disjoint from every literal UTF-8 filename.
            key = _filesystem_identity(str(path))
            offset = watermarks.get(key, 0)
            # A per-file identity for turns that carry no sessionId, so they do not all
            # collapse into one fabricated cross-file "unknown" session with a combined
            # project, summed tokens, and a span that floats to the top of the panel.
            session_fallback = _session_fallback(root, path)

            saw_new = False
            pass_stats = ParseStats()
            # Drain this file in bounded chunks; each read_new_lines returns whole lines
            # and advances the offset, and returns none once only a partial line remains.
            while True:
                lines, new_offset, read_succeeded = attribution.read_new_lines_with_health(
                    path, offset
                )
                if not read_succeeded:
                    stats.scan_succeeded = False
                    break
                if not lines:
                    break
                saw_new = True
                for turn in attribution.parse_lines(lines, pass_stats):
                    session_id = turn.session_id
                    if session_id == attribution.UNKNOWN:
                        session_id = session_fallback
                    identity = turn.response_identity
                    if identity is not None:
                        if identity in seen_responses:
                            continue
                        # Claim it before folding so a copy in another file in this same
                        # pass is skipped. Identities track the session's retention: old
                        # turns are still part of a live session's lifetime total, so a
                        # later copied transcript must remain recognizable.
                        seen_responses.add(identity)
                        new_responses[identity] = (turn.ts, session_id)
                    _fold_turn(hourly, sessions, turn, session_id, fold_hourly=turn.ts >= min_ts)
                offset = new_offset

            if not saw_new:
                continue
            stats.files_with_new_data += 1
            stats.lines += pass_stats.lines
            stats.malformed += pass_stats.malformed
            stats.emitted += pass_stats.emitted

            try:
                info = path.stat()
                offsets[key] = (offset, info.st_size, info.st_mtime)
            except OSError:
                # The file disappeared or became unreadable after we consumed it. Do not
                # report this pass as fresh, but retain its consumed offset in the same
                # transaction as its folds so a later retry cannot double-count it.
                offsets[key] = (offset, None, None)
                stats.scan_succeeded = False

        if not offsets:
            return stats

        with self._connect() as conn:
            self._flush_hourly(conn, projects_root, hourly)
            self._flush_sessions(conn, projects_root, sessions)
            self._flush_response_identities(conn, projects_root, new_responses)
            self._flush_watermarks(conn, projects_root, offsets)
        return stats

    def _attribution_rebuild_required(self, projects_root: str | bytes) -> bool:
        with self._connect() as conn:
            required = conn.execute(
                "SELECT 1 FROM attribution_migrations WHERE name = ?",
                (_FILESYSTEM_IDENTITY_REBUILD,),
            ).fetchone()
            complete = conn.execute(
                "SELECT 1 FROM attribution_rebuilds WHERE projects_root = ?",
                (projects_root,),
            ).fetchone()
        return required is not None and complete is None

    def _rebuild_attribution(
        self, root: Path, projects_root: str | bytes, min_ts: datetime
    ) -> AggregateStats:
        """Rebuild one active root behind an invisible, durable staging namespace.

        A healthy transcript commits its folds, response identities, and watermark in
        one transaction. Thus a broken sibling keeps the promotion pending without
        making healthy files start at byte zero on every poll. Only a wholly healthy
        traversal swaps the staged namespace into the API-visible active namespace.
        """
        staging_root = _derived_identity(b"staging", projects_root)
        watermarks = self._load_watermarks(staging_root)
        seen_responses = self._load_response_identities(staging_root, min_ts)
        stats = AggregateStats()
        paths, stats.scan_succeeded = attribution.scan_jsonl_files(root)
        keyed_paths = {_filesystem_identity(str(path)): path for path in paths}
        path_keys = set(keyed_paths)

        # A file disappearing between retries invalidates additive staged sums because
        # they do not retain per-file provenance. Truncation has the same consequence:
        # the reader would restart at zero and add the replacement's turns to the old
        # file's already-staged totals. On a complete traversal, restart the whole
        # staging namespace rather than promote either kind of mixed source history.
        staging_invalid = not set(watermarks).issubset(path_keys)
        if stats.scan_succeeded and not staging_invalid:
            for key, offset in watermarks.items():
                try:
                    if keyed_paths[key].stat().st_size < offset:
                        staging_invalid = True
                        break
                except OSError:
                    # The per-file pass below records the transient health failure. Do
                    # not destroy healthy checkpoints merely because stat failed once.
                    stats.scan_succeeded = False
                    break
        if stats.scan_succeeded and staging_invalid:
            with self._connect() as conn:
                self._delete_attribution_namespace(conn, staging_root)
            watermarks = {}
            seen_responses = set()

        for path in paths:
            stats.files_scanned += 1
            key = _filesystem_identity(str(path))
            offset = watermarks.get(key, 0)
            hourly: dict[tuple[str, str, str, int], list[int]] = {}
            sessions: dict[str | bytes, _SessionAcc] = {}
            responses: dict[tuple[str, str], tuple[datetime, str | bytes]] = {}
            file_seen = set(seen_responses)
            pass_stats = ParseStats()
            saw_new = False
            healthy = True
            session_fallback = _session_fallback(root, path)

            while True:
                lines, new_offset, read_succeeded = attribution.read_new_lines_with_health(
                    path, offset
                )
                if not read_succeeded:
                    healthy = False
                    stats.scan_succeeded = False
                    break
                if not lines:
                    break
                saw_new = True
                for turn in attribution.parse_lines(lines, pass_stats):
                    session_id: str | bytes = turn.session_id
                    if session_id == attribution.UNKNOWN:
                        session_id = session_fallback
                    identity = turn.response_identity
                    if identity is not None:
                        if identity in file_seen:
                            continue
                        file_seen.add(identity)
                        responses[identity] = (turn.ts, session_id)
                    _fold_turn(
                        hourly,
                        sessions,
                        turn,
                        session_id,
                        fold_hourly=turn.ts >= min_ts,
                    )
                offset = new_offset

            if healthy:
                try:
                    info = path.stat()
                except OSError:
                    healthy = False
                    stats.scan_succeeded = False
            if not healthy:
                continue

            # This is the per-transcript checkpoint invariant: no staged contribution
            # can become durable without the exact response claims and byte progress
            # that make replaying it unnecessary and safe.
            with self._connect() as conn:
                self._flush_hourly(conn, staging_root, hourly)
                self._flush_sessions(conn, staging_root, sessions)
                self._flush_response_identities(conn, staging_root, responses)
                self._flush_watermarks(
                    conn, staging_root, {key: (offset, info.st_size, info.st_mtime)}
                )
            watermarks[key] = offset
            seen_responses = file_seen
            if saw_new:
                stats.files_with_new_data += 1
                stats.lines += pass_stats.lines
                stats.malformed += pass_stats.malformed
                stats.emitted += pass_stats.emitted

        if not stats.scan_succeeded:
            return stats

        # Promotion is one transaction across all four forms of active state. Readers
        # therefore see either the prior active namespace or the complete rebuild,
        # never a mixture assembled from only some transcripts.
        with self._connect() as conn:
            self._delete_attribution_namespace(conn, projects_root)
            for table in (
                "hourly_usage",
                "sessions_rollup",
                "jsonl_watermarks",
                "response_identities",
            ):
                conn.execute(
                    f"UPDATE {table} SET projects_root = ? WHERE projects_root = ?",
                    (projects_root, staging_root),
                )
            conn.execute(
                "INSERT OR IGNORE INTO attribution_rebuilds (projects_root) VALUES (?)",
                (projects_root,),
            )
        return stats

    @staticmethod
    def _delete_attribution_namespace(conn: sqlite3.Connection, projects_root: str | bytes) -> None:
        for table in (
            "hourly_usage",
            "sessions_rollup",
            "jsonl_watermarks",
            "response_identities",
        ):
            conn.execute(f"DELETE FROM {table} WHERE projects_root = ?", (projects_root,))

    def _load_watermarks(self, projects_root: str | bytes) -> dict[str | bytes, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT path, offset FROM jsonl_watermarks WHERE projects_root = ?",
                (projects_root,),
            ).fetchall()
        return {row["path"]: row["offset"] for row in rows}

    def _load_response_identities(
        self, projects_root: str | bytes, min_ts: datetime
    ) -> set[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT identities.message_id, identities.request_id"
                " FROM response_identities AS identities"
                " LEFT JOIN sessions_rollup AS sessions"
                "   ON sessions.projects_root = identities.projects_root"
                "  AND sessions.session_id = identities.session_id"
                " WHERE identities.projects_root = ?"
                "   AND (identities.response_ts >= ?"
                "     OR (identities.session_id IS NOT NULL AND sessions.session_id IS NOT NULL))",
                (projects_root, _iso(min_ts)),
            ).fetchall()
        return {(row["message_id"], row["request_id"]) for row in rows}

    def _backfill_response_identities(
        self, root: Path, projects_root: str | bytes, watermarks: dict[str | bytes, int]
    ) -> None:
        """Seed an upgraded database's index from bytes its watermarks already consumed.

        The additive rollups cannot safely be replayed: that would count every old
        response twice. Instead read only through each committed offset and record
        identities, leaving all sums and watermarks untouched. A missing/truncated
        transcript leaves the root pending so a later healthy scan can still recover
        it; marking it complete would silently make a future fork double-count.
        """
        with self._connect() as conn:
            needed = conn.execute(
                "SELECT 1 FROM attribution_migrations WHERE name = ?",
                ("response-identities-v1",),
            ).fetchone()
            complete = conn.execute(
                "SELECT 1 FROM response_identity_backfills WHERE projects_root = ?",
                (projects_root,),
            ).fetchone()
        if needed is None or complete is not None or not watermarks:
            return

        paths, scan_succeeded = attribution.scan_jsonl_files(root)
        responses: dict[tuple[str, str], tuple[datetime, str | bytes]] = {}
        found: set[str | bytes] = set()
        for path in paths:
            key = _filesystem_identity(str(path))
            end_offset = watermarks.get(key)
            if end_offset is None:
                continue
            found.add(key)
            try:
                if path.stat().st_size < end_offset:
                    scan_succeeded = False
                    continue
            except OSError:
                scan_succeeded = False
                continue
            offset = 0
            session_fallback = _session_fallback(root, path)
            while offset < end_offset:
                lines, next_offset, read_succeeded = attribution.read_new_lines_with_health(
                    path, offset, end_offset=end_offset
                )
                if not read_succeeded or next_offset <= offset:
                    scan_succeeded = False
                    break
                for turn in attribution.parse_lines(lines):
                    if turn.response_identity is not None:
                        session_id = turn.session_id
                        if session_id == attribution.UNKNOWN:
                            session_id = session_fallback
                        responses.setdefault(turn.response_identity, (turn.ts, session_id))
                offset = next_offset

        with self._connect() as conn:
            self._flush_response_identities(conn, projects_root, responses)
            # Preserve every identity recovered from a healthy transcript even when
            # another watermark cannot yet be read.  The completion marker remains
            # pending so a later scan can seed the missing identity, but discarding
            # the healthy subset would let a new fork count those responses again.
            if not scan_succeeded or found != set(watermarks):
                return
            conn.execute(
                "INSERT OR IGNORE INTO response_identity_backfills (projects_root) VALUES (?)",
                (projects_root,),
            )

    @staticmethod
    def _flush_hourly(
        conn: sqlite3.Connection,
        projects_root: str | bytes,
        hourly: dict[tuple[str, str, str, int], list[int]],
    ) -> None:
        if not hourly:
            return
        conn.executemany(
            "INSERT INTO hourly_usage"
            " (projects_root, hour_start, project, model, is_sidechain,"
            "  input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,"
            "  large_context_tokens)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(projects_root, hour_start, project, model, is_sidechain)"
            " DO UPDATE SET"
            "  input_tokens = input_tokens + excluded.input_tokens,"
            "  output_tokens = output_tokens + excluded.output_tokens,"
            "  cache_creation_tokens = cache_creation_tokens + excluded.cache_creation_tokens,"
            "  cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,"
            "  large_context_tokens = large_context_tokens + excluded.large_context_tokens",
            [
                (projects_root, hour, project, model, sidechain, *tok)
                for (hour, project, model, sidechain), tok in hourly.items()
            ],
        )

    @staticmethod
    def _flush_sessions(
        conn: sqlite3.Connection,
        projects_root: str | bytes,
        sessions: dict[str | bytes, _SessionAcc],
    ) -> None:
        if not sessions:
            return
        conn.executemany(
            "INSERT INTO sessions_rollup"
            " (projects_root, session_id, project, model, start_ts, end_ts,"
            "  total_tokens, max_turn_context)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(projects_root, session_id) DO UPDATE SET"
            # start/end widen to cover the turns seen across passes; the token total
            # accumulates, and max_turn_context keeps the deepest turn ever recorded.
            # model follows the latest turn by TIMESTAMP -- a session can switch models
            # mid-way, so keep the incoming model only when this batch's latest turn is
            # at or after the stored one, matching _fold_turn's timestamp-guarded choice
            # across passes as well as within one. (SQLite evaluates every SET RHS
            # against the pre-update row, as the MIN/MAX below already rely on.)
            "  model = CASE WHEN excluded.end_ts >= sessions_rollup.end_ts"
            "               THEN excluded.model ELSE sessions_rollup.model END,"
            "  start_ts = MIN(sessions_rollup.start_ts, excluded.start_ts),"
            "  end_ts = MAX(sessions_rollup.end_ts, excluded.end_ts),"
            "  total_tokens = total_tokens + excluded.total_tokens,"
            "  max_turn_context = MAX(sessions_rollup.max_turn_context, excluded.max_turn_context)",
            [
                (
                    projects_root,
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
        conn: sqlite3.Connection,
        projects_root: str | bytes,
        offsets: dict[str | bytes, tuple[int, int | None, float | None]],
    ) -> None:
        conn.executemany(
            "INSERT INTO jsonl_watermarks (projects_root, path, offset, size, mtime)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(projects_root, path) DO UPDATE SET"
            "  offset = excluded.offset, size = excluded.size, mtime = excluded.mtime",
            [
                (projects_root, path, off, size, mtime)
                for path, (off, size, mtime) in offsets.items()
            ],
        )

    @staticmethod
    def _flush_response_identities(
        conn: sqlite3.Connection,
        projects_root: str | bytes,
        responses: dict[tuple[str, str], tuple[datetime, str | bytes]],
    ) -> None:
        if not responses:
            return
        conn.executemany(
            "INSERT OR IGNORE INTO response_identities"
            " (projects_root, message_id, request_id, response_ts, session_id)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (projects_root, message_id, request_id, _iso(ts), session_id)
                for (message_id, request_id), (ts, session_id) in responses.items()
            ],
        )

    def attribution_totals(
        self, root: Path | str, hours: float, now: datetime | None = None
    ) -> dict[str, Any]:
        """Windowed token totals, grouped for the by-project/model/agent panels.

        The cutoff is floored to the hour to match ``hour_start``'s granularity: an
        un-floored ``now - hours`` keeps minutes, and ``hour_start >= cutoff`` then
        drops the whole oldest boundary hour, losing up to ~59 minutes of usage that
        is genuinely inside the window.
        """
        now = now or datetime.now(UTC)
        cutoff_at = (now - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
        cutoff = _iso(cutoff_at)
        # Upper bound at now, so a future-dated hour (clock skew or a bad timestamp)
        # that a lower-bound-only filter would include forever is excluded. This bounds
        # the HOUR, not the turn: a future turn inside the CURRENT partial hour floors
        # to hour_start <= now and is still counted -- the hourly rollup has no per-turn
        # granularity to exclude it -- so its tokens read a little early, and its session
        # (bounded by exact end_ts <= now in attribution_sessions) is briefly absent from
        # that panel. Both self-heal within the hour once wall-clock passes the timestamp;
        # rejecting future turns at ingestion instead would advance the watermark past a
        # legitimately clock-skewed turn and lose it for good. Asymmetric -- no undercount
        # cost, unlike the lower-bound flooring.
        upper = _iso(now)
        window = (_projects_root_identity(root), cutoff, upper)
        with self._connect() as conn:
            # One read transaction so all four SELECTs see a single WAL snapshot. Python's
            # sqlite3 opens no implicit transaction for SELECT, so without this a
            # concurrent aggregation commit (the poller's worker-thread connection)
            # landing between statements could let by_project/by_model count turns the
            # breakdown denominator omitted -- a share transiently over 100%. The
            # _connect() commit on exit closes this read transaction.
            conn.execute("BEGIN")
            breakdown = conn.execute(
                "SELECT"
                "  COALESCE(SUM(input_tokens), 0) AS input_tokens,"
                "  COALESCE(SUM(output_tokens), 0) AS output_tokens,"
                "  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,"
                "  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,"
                "  COALESCE(SUM(large_context_tokens), 0) AS large_context_tokens"
                " FROM hourly_usage"
                " WHERE projects_root = ? AND hour_start >= ? AND hour_start <= ?",
                window,
            ).fetchone()
            by_project = conn.execute(
                f"SELECT project AS name, SUM({_HOURLY_TOKENS}) AS tokens"
                " FROM hourly_usage"
                " WHERE projects_root = ? AND hour_start >= ? AND hour_start <= ?"
                " GROUP BY project ORDER BY tokens DESC",
                window,
            ).fetchall()
            by_model = conn.execute(
                f"SELECT model AS name, SUM({_HOURLY_TOKENS}) AS tokens"
                " FROM hourly_usage"
                " WHERE projects_root = ? AND hour_start >= ? AND hour_start <= ?"
                " GROUP BY model ORDER BY tokens DESC",
                window,
            ).fetchall()
            by_agent = conn.execute(
                f"SELECT is_sidechain, SUM({_HOURLY_TOKENS}) AS tokens"
                " FROM hourly_usage"
                " WHERE projects_root = ? AND hour_start >= ? AND hour_start <= ?"
                " GROUP BY is_sidechain",
                window,
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

    def attribution_sessions(
        self, root: Path | str, hours: float, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Every session active within the window, newest activity first.

        Active means it has a turn inside the window (``end_ts`` at or past the
        cutoff). Totals here are session LIFETIME, not windowed -- the caller uses
        them only for the descriptive "longest sessions active in this window" list,
        which labels its numbers as lifetime. The windowed large-context share does
        NOT come from here; it comes from hourly_usage.large_context_tokens.

        The cutoff floors to the hour to match ``attribution_totals``, so the two
        answer for the same window edge.
        """
        now = now or datetime.now(UTC)
        cutoff_at = (now - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
        cutoff = _iso(cutoff_at)
        # Upper bound at now, as in attribution_totals: a session whose activity is dated
        # in the future is garbage (skew or a bad timestamp), not a session active in the
        # window, and would otherwise sit at the top of the list forever.
        upper = _iso(now)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id, project, model, start_ts, end_ts, total_tokens,"
                " max_turn_context FROM sessions_rollup"
                " WHERE projects_root = ? AND end_ts >= ? AND end_ts <= ?"
                " ORDER BY end_ts DESC",
                (_projects_root_identity(root), cutoff, upper),
            ).fetchall()
        return [
            {
                "session_id": _display_session_identity(row["session_id"]),
                "project": row["project"],
                "model": row["model"],
                "start_ts": _parse_iso(row["start_ts"]),
                "end_ts": _parse_iso(row["end_ts"]),
                "total_tokens": row["total_tokens"],
                "max_turn_context": row["max_turn_context"],
            }
            for row in rows
        ]


def _session_fallback(root: Path | str, path: Path) -> str | bytes:
    """A per-file session identity for turns that carry no sessionId of their own.

    Keyed on the file's path relative to the tree (its own name is a session UUID),
    so unknown-session turns are attributed to the file they came from rather than
    merged into one machine-wide "unknown" bucket. Distinct from a real sessionId by
    the ``unknown:`` prefix.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return _filesystem_identity(f"unknown:{rel}")


def _projects_root_identity(root: Path | str) -> str | bytes:
    """The SQLite-safe identity shared by aggregation and API queries."""
    return _filesystem_identity(str(normalize_projects_root(root)))


def _filesystem_identity(value: str) -> str | bytes:
    """Return an injective, versioned SQLite identity for a filesystem string.

    Ordinary UTF-8 strings stay byte-for-byte identical as SQLite TEXT. Exceptional
    names become versioned BLOBs, a disjoint SQLite type domain: no literal filename,
    including one spelling ``\\udcXX``, can collide with an escaped filesystem byte.
    ``os.fsencode`` recovers real surrogateescaped directory bytes; surrogatepass is
    only the deterministic fallback for synthetic lone surrogates used by callers.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        try:
            raw = os.fsencode(value)
            codec = b"fs"
        except UnicodeEncodeError:
            raw = value.encode("utf-8", "surrogatepass")
            codec = b"utf8-surrogatepass"
        return _FILESYSTEM_IDENTITY_PREFIX + codec + b"\x00" + raw
    return value


def _derived_identity(kind: bytes, identity: str | bytes) -> bytes:
    """Derive an injective internal namespace without entering the public TEXT domain."""
    if isinstance(identity, str):
        payload = b"text\x00" + identity.encode("utf-8")
    else:
        payload = b"blob\x00" + identity
    return _DERIVED_IDENTITY_PREFIX + kind + b"\x00" + payload


def _display_session_identity(identity: str | bytes) -> str:
    """Keep the JSON API textual while preserving BLOB separation in persistence."""
    if isinstance(identity, str):
        return identity
    return f"unknown:filesystem:v1:{identity.hex()}"


def _fold_turn(
    hourly: dict[tuple[str, str, str, int], list[int]],
    sessions: dict[str | bytes, _SessionAcc],
    turn: Turn,
    session_id: str | bytes,
    *,
    fold_hourly: bool,
) -> None:
    """Accumulate one turn into the in-memory rollups.

    ``session_id`` is the effective id (the turn's own, or a per-file fallback when it
    had none). ``fold_hourly`` gates only the hourly rollup: an old turn still extends
    its session's lifetime span and total but adds nothing to a window nobody queries.
    """
    if fold_hourly:
        hour = turn.ts.replace(minute=0, second=0, microsecond=0)
        key = (_iso(hour) or "", turn.project, turn.model, 1 if turn.is_sidechain else 0)
        tokens = hourly.setdefault(key, [0, 0, 0, 0, 0])
        tokens[0] += turn.input_tokens
        tokens[1] += turn.output_tokens
        tokens[2] += turn.cache_creation_tokens
        tokens[3] += turn.cache_read_tokens
        # The large-context subset: this turn's whole token count counts toward the
        # hour's large_context_tokens only when the turn itself was at large context.
        if turn.context_tokens >= LARGE_CONTEXT_TOKENS:
            tokens[4] += turn.total_tokens

    acc = sessions.get(session_id)
    if acc is None:
        sessions[session_id] = _SessionAcc(
            project=turn.project,
            model=turn.model,
            start_ts=turn.ts,
            end_ts=turn.ts,
            total_tokens=turn.total_tokens,
            max_turn_context=turn.context_tokens,
        )
        return
    # Update the model only when this turn is at or after the latest seen so far, so a
    # later-folded but older-timestamped turn (clock skew, or a fold order that differs
    # from timestamp order across files/passes) cannot relabel a session whose end_ts
    # stays newer. Compared against end_ts BEFORE it widens below. project is stable per
    # session (one working directory), so its first-seen value already stands.
    if turn.ts >= acc.end_ts:
        acc.model = turn.model
    acc.start_ts = min(acc.start_ts, turn.ts)
    acc.end_ts = max(acc.end_ts, turn.ts)
    acc.total_tokens += turn.total_tokens
    acc.max_turn_context = max(acc.max_turn_context, turn.context_tokens)


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
