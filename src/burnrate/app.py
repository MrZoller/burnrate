"""FastAPI application: two JSON endpoints and the static dashboard.

The OAuth token lives entirely on this side. Nothing in any response below
carries it, and the browser never talks to api.anthropic.com.

There is deliberately no module-level `app`. Building one at import time makes
`import burnrate.app` create a database and a poller as a side effect, which
polluted $HOME on every test run. Serve it as a factory instead:

    uvicorn burnrate.app:create_app --factory
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .poller import Poller
from .projection import Pace, Projection, pace_for, project
from .store import (
    LARGE_CONTEXT_TOKENS,
    MAX_POINTS_PER_BUCKET,
    Sample,
    Store,
)
from .usage import KNOWN_LABELS, Bucket, UsageSnapshot, group_for, humanize

logger = logging.getLogger("burnrate")

# The handlers below are deliberately `def`, not `async def`. They do blocking
# SQLite reads, and Starlette runs sync handlers in a threadpool -- as
# coroutines they would stall the event loop, and a large history query would
# hold up /api/now and the health check along with it.

STATIC_DIR = Path(__file__).parent / "static"


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()
    store = Store(config.db_path)
    poller = Poller(store, interval=config.poll_interval, projects_dir=config.attribution_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("burnrate starting; db=%s", config.db_path)
        await poller.start()
        try:
            yield
        finally:
            await poller.stop()

    app = FastAPI(title="burnrate", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.store = store
    app.state.poller = poller

    @app.get("/api/now")
    def now() -> JSONResponse:
        """Latest reading per bucket, staleness, and the pace projection."""
        moment = datetime.now(UTC)
        buckets = _current_buckets(poller, store)
        staleness = poller.staleness_seconds(moment)
        weekly = next((b for b in buckets if b.key == "seven_day"), None)
        # Scaled to the poll cadence, not a fixed 180s -- see Config.
        stale_after = config.stale_after_seconds

        stale = (
            staleness is None or staleness > stale_after or poller.status.consecutive_failures > 0
        )

        # The moment the reading was taken, which is what the projection must be
        # anchored to. Measuring a frozen utilization against an advancing clock
        # counts every hour since the last sample as zero usage.
        reading_at = poller.status.last_success_at or store.latest_sample_time()

        return JSONResponse(
            {
                "generated_at": moment.isoformat(),
                "stale": stale,
                "staleness_seconds": staleness,
                "stale_after_seconds": stale_after,
                "poll_interval_seconds": config.poll_interval,
                "buckets": [
                    _bucket_json(b, pace_for(b, now=moment, reading_at=reading_at)) for b in buckets
                ],
                "projection": _projection_json(
                    project(weekly, now=moment, reading_at=reading_at, stale=stale)
                ),
                "status": poller.status.as_dict(),
            }
        )

    @app.get("/api/history")
    def history(
        hours: float = Query(default=168.0, gt=0, le=90 * 24),
    ) -> JSONResponse:
        """Samples from the last `hours`, downsampled, one series per bucket."""
        samples = store.history(hours)
        return JSONResponse(
            {
                "hours": hours,
                "generated_at": datetime.now(UTC).isoformat(),
                "max_points_per_bucket": MAX_POINTS_PER_BUCKET,
                "series": _to_series(samples),
            }
        )

    @app.get("/api/attribution")
    def attribution(
        window: str = Query(default="7d"),
    ) -> JSONResponse:
        """Local token attribution for the selected window (24h or 7d).

        A proxy for what is consuming tokens on THIS machine, computed from Claude
        Code's own session transcripts -- not a reconstruction of the usage meter,
        and not aggregated across devices. Read-only; carries token counts, never a
        credential.
        """
        return JSONResponse(_attribution_payload(store, window))

    @app.get("/api/healthz")
    def healthz() -> JSONResponse:
        """Liveness plus poll health, for launchd/uptime checks."""
        return JSONResponse(
            {"ok": True, "poller_healthy": poller.status.healthy},
            status_code=200 if poller.status.healthy else 503,
        )

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    else:  # pragma: no cover - only when the package is installed incorrectly
        logger.warning("static directory missing at %s; UI disabled", STATIC_DIR)

    return app


def _current_buckets(poller: Poller, store: Store) -> list[Bucket]:
    """Live snapshot when we have one, otherwise the last persisted reading.

    Falling back to the store means a restart shows real numbers immediately
    instead of an empty dashboard until the first poll lands -- and those numbers
    still carry their true age through `staleness_seconds`.
    """
    snapshot: UsageSnapshot | None = poller.snapshot
    if snapshot and snapshot.buckets:
        return list(snapshot.buckets)
    # Sort on the same key the live path uses, so a restart does not reorder the
    # dashboard (SQL hands these back alphabetically, which would file an
    # unrecognized bucket in among the real ones).
    restored = [_sample_to_bucket(s) for s in store.latest_per_bucket()]
    return sorted(restored, key=lambda b: b.sort_key)


def _sample_to_bucket(sample: Sample) -> Bucket:
    return Bucket(
        key=sample.bucket,
        label=sample.label or KNOWN_LABELS.get(sample.bucket) or humanize(sample.bucket),
        utilization=sample.utilization,
        resets_at=sample.resets_at,
        group=group_for(None, sample.bucket),
        known=sample.known,
        source="store",
    )


def _bucket_json(bucket: Bucket, pace: Pace) -> dict[str, Any]:
    return {
        "key": bucket.key,
        "label": bucket.label,
        "utilization": bucket.utilization,
        "resets_at": bucket.resets_at.isoformat() if bucket.resets_at else None,
        "group": bucket.group,
        "severity": bucket.severity,
        "known": bucket.known,
        "source": bucket.source,
        # Item 1: the window's start, surfaced rather than re-derived in JS. Present
        # only for recognized buckets -- inferring a start needs an assumed period
        # length, which an unrecognized bucket does not have -- so those keep the
        # "No reset reported" line.
        "window_opened_at": _iso(pace.window_opened_at),
        # Item 5: pace, not level. Item 2's bar reads `elapsed_fraction` (measured at
        # the reading time, so the marker ages honestly).
        "pace_status": pace.status,
        "pace_label": pace.label,
        "elapsed_fraction": pace.elapsed_fraction,
    }


def _projection_json(projection: Projection) -> dict[str, Any]:
    return {
        "status": projection.status,
        "message": projection.message,
        "bucket_key": projection.bucket_key,
        "utilization": projection.utilization,
        "rate_per_hour": projection.rate_per_hour,
        "elapsed_hours": projection.elapsed_hours,
        "window_start": _iso(projection.window_start),
        "resets_at": _iso(projection.resets_at),
        "hits_cap_at": _iso(projection.hits_cap_at),
        "hours_to_cap": projection.hours_to_cap,
    }


# How many windows the attribution section offers, and the hours each covers. The
# section is deliberately matched to the meter's own windows (issue #16).
_ATTRIBUTION_WINDOWS: dict[str, float] = {"24h": 24.0, "7d": 168.0}
_DEFAULT_WINDOW = "7d"

# Longest list any single panel returns, so a machine with dozens of projects or a
# marathon of sessions does not ship an unbounded response.
_TOP_N = 8

ATTRIBUTION_SCOPE = "This machine only — local token counts, not the usage meter."


def _attribution_payload(store: Store, window: str) -> dict[str, Any]:
    """Assemble the attribution response for one window.

    Kept out of the handler so it is a plain, directly testable function. The
    scope label is always present, whatever the data -- an empty tree still renders
    an honest, correctly-scoped (and empty) section rather than nothing.
    """
    if window not in _ATTRIBUTION_WINDOWS:
        window = _DEFAULT_WINDOW
    hours = _ATTRIBUTION_WINDOWS[window]

    # One reading of the clock for both queries, so the by-project/model window and the
    # active-sessions window share an edge instead of drifting by the call latency.
    now = datetime.now(UTC)
    totals = store.attribution_totals(hours, now=now)
    sessions = store.attribution_sessions(hours, now=now)

    hourly_total = sum(tokens for _, tokens in totals["by_project"])
    by_agent = totals["by_agent"]

    # Genuinely windowed: both numerator and denominator are sums over hours inside the
    # window, so the 24h/7d toggle actually bounds this. large_context_tokens is the
    # subset of in-window tokens from turns that were themselves at large context.
    large_context_tokens = totals["large_context_tokens"]

    # Readable project labels, disambiguated only where two working directories share a
    # basename (/clients/a/app and /clients/b/app -> "a/app", "b/app"). Built from every
    # path in play so the by-project rows and the session list use the same labels.
    display = _project_display_names(
        [name for name, _ in totals["by_project"]] + [s["project"] for s in sessions]
    )

    return {
        "generated_at": now.isoformat(),
        "window": window,
        "hours": hours,
        "scope": ATTRIBUTION_SCOPE,
        "total_tokens": hourly_total,
        "token_breakdown": totals["breakdown"],
        "by_project": _shared_rows(
            [(display[name], tokens) for name, tokens in totals["by_project"]],
            hourly_total,
        ),
        "by_model": _shared_rows(totals["by_model"], hourly_total),
        "by_agent": _shared_rows(
            [("Main", by_agent.get(0, 0)), ("Subagents", by_agent.get(1, 0))],
            hourly_total,
        ),
        "large_context": {
            "threshold_tokens": LARGE_CONTEXT_TOKENS,
            "tokens": large_context_tokens,
            "share": _share(large_context_tokens, hourly_total),
        },
        # Sessions inherently cross windows, so there is no honest "share of the window"
        # here -- these are the longest sessions ACTIVE in the window, each carrying its
        # own span and its LIFETIME token total, labelled as lifetime in the UI. No
        # windowed percentage is reported for them.
        "top_sessions": [
            {
                "project": display[s["project"]],
                "model": s["model"],
                "duration_hours": round(_session_hours(s), 2),
                "lifetime_tokens": s["total_tokens"],
                "max_context_tokens": s["max_turn_context"],
            }
            for s in sorted(sessions, key=_session_hours, reverse=True)[:_TOP_N]
        ],
    }


def _shared_rows(rows: list[tuple[str, int]], total: int) -> list[dict[str, Any]]:
    """Label/token pairs with each row's share of ``total``, longest first, top N."""
    ordered = sorted(rows, key=lambda row: row[1], reverse=True)
    return [
        {"label": label, "tokens": tokens, "share": _share(tokens, total)}
        for label, tokens in ordered[:_TOP_N]
        if tokens > 0
    ]


def _share(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def _session_hours(session: dict[str, Any]) -> float:
    start = session.get("start_ts")
    end = session.get("end_ts")
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds() / 3600.0)


def _project_segments(path: str) -> list[str]:
    """A working directory's meaningful path segments (drops the root and blanks)."""
    if not path or path == "unknown":
        return ["unknown"]
    return [seg for seg in Path(path).parts if seg not in ("", "/")] or [path]


# Most segments a disambiguated project label may show: the basename plus at most two
# parents. Beyond this the label stops being a readable name and starts being a path.
_MAX_LABEL_SEGMENTS = 3
# Marks a label capped below the full path, so a shared abbreviation reads as truncated
# rather than as the real directory. U+2026 is the ellipsis; kept as an escape so the
# source stays ASCII.
_TRUNCATED_PREFIX = "\u2026/"


def _project_display_names(paths: list[str]) -> dict[str, str]:
    """Map each distinct working directory to a readable, bounded label.

    The privacy-lean default is the basename alone. Two directories that share a
    basename (/clients/a/app and /clients/b/app) would otherwise render identically and
    each claim a top-N slot, so colliding labels grow one parent segment at a time --
    and only those; a unique basename keeps just its basename.

    Growth is capped at ``_MAX_LABEL_SEGMENTS`` so a pathological pair -- one path a
    strict suffix of another (/Users/alice/app vs /mnt/Users/alice/app) -- never
    expands a label into a near-full path chasing a distinction it cannot win. Whatever
    still collides at the cap keeps the shared abbreviated label with a leading marker,
    signalling truncation instead of exposing the whole directory.
    """
    segments = {p: _project_segments(p) for p in set(paths)}
    depth = dict.fromkeys(segments, 1)

    def cap(p: str) -> int:
        return min(len(segments[p]), _MAX_LABEL_SEGMENTS)

    def suffix(p: str) -> str:
        return "/".join(segments[p][-depth[p] :])

    while True:
        collisions = False
        by_label: dict[str, list[str]] = {}
        for p in segments:
            by_label.setdefault(suffix(p), []).append(p)
        for members in by_label.values():
            if len(members) < 2:
                continue
            for p in members:
                if depth[p] < cap(p):  # grow only up to the cap, never past it
                    depth[p] += 1
                    collisions = True
        if not collisions:
            break

    # Anything still sharing a label at the cap gets the truncation marker, so a shared
    # abbreviation is never mistaken for a real, unique directory.
    grouped: dict[str, list[str]] = {}
    for p in segments:
        grouped.setdefault(suffix(p), []).append(p)
    return {
        p: (_TRUNCATED_PREFIX + label if len(members) > 1 else label)
        for label, members in grouped.items()
        for p in members
    }


def _to_series(samples: list[Sample]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for sample in samples:
        series = grouped.setdefault(
            sample.bucket,
            {"key": sample.bucket, "label": sample.label or sample.bucket, "points": []},
        )
        if sample.label:
            series["label"] = sample.label
        series["points"].append({"ts": sample.ts.isoformat(), "utilization": sample.utilization})
    return list(grouped.values())


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
