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

from .config import STALE_AFTER_SECONDS, Config
from .poller import Poller
from .projection import Projection, project
from .store import MAX_POINTS_PER_BUCKET, Sample, Store
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
    poller = Poller(store, interval=config.poll_interval)

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

        stale = (
            staleness is None
            or staleness > STALE_AFTER_SECONDS
            or poller.status.consecutive_failures > 0
        )

        return JSONResponse(
            {
                "generated_at": moment.isoformat(),
                "stale": stale,
                "staleness_seconds": staleness,
                "stale_after_seconds": STALE_AFTER_SECONDS,
                "poll_interval_seconds": config.poll_interval,
                "buckets": [_bucket_json(b) for b in buckets],
                "projection": _projection_json(project(weekly, now=moment)),
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


def _bucket_json(bucket: Bucket) -> dict[str, Any]:
    return {
        "key": bucket.key,
        "label": bucket.label,
        "utilization": bucket.utilization,
        "resets_at": bucket.resets_at.isoformat() if bucket.resets_at else None,
        "group": bucket.group,
        "severity": bucket.severity,
        "known": bucket.known,
        "source": bucket.source,
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
