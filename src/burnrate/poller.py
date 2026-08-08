"""Background poll loop.

Every 60 seconds: re-read the credential from scratch, fetch, parse, append. The
re-read is the whole reason we never need a refresh flow -- Claude Code rotates
the token underneath us and the next poll simply picks up the new value.

On failure the interval backs off exponentially to a ceiling, so a broken
endpoint costs one request every few minutes rather than one a minute forever.
The failure is never swallowed: `status` carries the last error and the age of
the last success, and the UI turns that into a banner.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .client import UsageAuthError, UsageFetchError, fetch_usage
from .credentials import CredentialError, read_credential
from .store import Store
from .usage import UsageSnapshot, parse_usage

logger = logging.getLogger("burnrate.poller")

POLL_INTERVAL_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 900.0
BACKOFF_FACTOR = 2.0
PRUNE_EVERY_N_POLLS = 60


@dataclass
class PollerStatus:
    """Everything the UI needs to decide between 'live' and 'broken'."""

    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    last_error_kind: str | None = None
    consecutive_failures: int = 0
    next_attempt_at: datetime | None = None
    credential_source: str | None = None
    warnings: tuple[str, ...] = field(default=())
    notices: tuple[str, ...] = field(default=())

    @property
    def healthy(self) -> bool:
        return self.consecutive_failures == 0 and self.last_success_at is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "last_success_at": _iso(self.last_success_at),
            "last_attempt_at": _iso(self.last_attempt_at),
            "last_error": self.last_error,
            "last_error_kind": self.last_error_kind,
            "consecutive_failures": self.consecutive_failures,
            "next_attempt_at": _iso(self.next_attempt_at),
            "credential_source": self.credential_source,
            "warnings": list(self.warnings),
            "notices": list(self.notices),
        }


class Poller:
    """Owns the poll loop and the most recent snapshot."""

    def __init__(
        self,
        store: Store,
        interval: float = POLL_INTERVAL_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.store = store
        self.interval = interval
        self.status = PollerStatus()
        self.snapshot: UsageSnapshot | None = None
        self._client = client
        self._owns_client = client is None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._poll_count = 0

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="burnrate-poller")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown path
                pass
            self._task = None
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        delay = 0.0
        while not self._stopping.is_set():
            if delay:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                    return
                except TimeoutError:
                    pass

            await self.poll_once()

            delay = self.next_delay()
            self.status.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)

    def next_delay(self) -> float:
        """Seconds to wait before the next attempt.

        Steady state is the poll interval; each consecutive failure doubles it up
        to a ceiling, so a broken endpoint costs one request every few minutes
        rather than one a minute forever. Lives on its own so the loop's timing
        can be asserted directly instead of re-derived by a test.
        """
        failures = self.status.consecutive_failures
        if not failures:
            return self.interval
        return min(self.interval * (BACKOFF_FACTOR ** (failures - 1)), MAX_BACKOFF_SECONDS)

    async def poll_once(self) -> UsageSnapshot | None:
        """One fetch/parse/store cycle. Records outcome in `status`; never raises."""
        now = datetime.now(UTC)
        self.status.last_attempt_at = now

        try:
            payload = await self._fetch_with_one_auth_retry()
        except CredentialError as exc:
            self._record_failure("credential", str(exc))
            return None
        except UsageFetchError as exc:
            self._record_failure(_error_kind(exc), str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            logger.exception("unexpected poll failure")
            self._record_failure("unexpected", f"{type(exc).__name__}: {exc}")
            return None

        snapshot = parse_usage(payload, fetched_at=now)
        if not snapshot.buckets:
            # A 200 we cannot read is a schema break, not a success.
            self._record_failure(
                "schema",
                "; ".join(snapshot.warnings) or "response contained no usable buckets",
            )
            self.status.warnings = snapshot.warnings
            return None

        try:
            self.store.append_snapshot(snapshot, raw_body=payload)
        except Exception as exc:  # noqa: BLE001 - a write failure must not kill polling
            logger.exception("failed to persist samples")
            self._record_failure("store", f"{type(exc).__name__}: {exc}")
            return None

        self.snapshot = snapshot
        self.status.last_success_at = now
        self.status.last_error = None
        self.status.last_error_kind = None
        self.status.consecutive_failures = 0
        self.status.warnings = snapshot.warnings
        self.status.notices = snapshot.notices

        self._poll_count += 1
        if self._poll_count % PRUNE_EVERY_N_POLLS == 0:
            try:
                self.store.prune()
            except Exception:  # noqa: BLE001 - pruning is housekeeping, not critical
                logger.exception("prune failed")

        return snapshot

    async def _fetch_with_one_auth_retry(self) -> Any:
        """Fetch, and on 401 re-read the credential once before giving up.

        Claude Code may have rotated the token between our read and the request.
        One re-read covers that race. We never mint or refresh a token ourselves;
        a second 401 means the data is stale and we say so.
        """
        credential = read_credential()
        self.status.credential_source = credential.source
        try:
            return await fetch_usage(credential.access_token, client=self._client)
        except UsageAuthError:
            retry = read_credential()
            self.status.credential_source = retry.source
            if retry.access_token == credential.access_token:
                raise UsageAuthError(
                    "HTTP 401 and the stored credential has not changed -- "
                    "sign in with Claude Code to refresh it"
                ) from None
            return await fetch_usage(retry.access_token, client=self._client)

    def _record_failure(self, kind: str, message: str) -> None:
        self.status.consecutive_failures += 1
        self.status.last_error = message
        self.status.last_error_kind = kind
        logger.warning("poll failed (%s): %s", kind, message)

    def staleness_seconds(self, now: datetime | None = None) -> float | None:
        """Seconds since the last successful fetch, from memory or the store."""
        reference = self.status.last_success_at or self.store.latest_sample_time()
        if reference is None:
            return None
        now = now or datetime.now(UTC)
        return max(0.0, (now - reference).total_seconds())


def _error_kind(exc: UsageFetchError) -> str:
    return type(exc).__name__.removeprefix("Usage").removesuffix("Error").lower() or "fetch"


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None
