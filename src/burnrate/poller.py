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
from .redact import scrub
from .store import Store
from .usage import UsageSnapshot, parse_usage

logger = logging.getLogger("burnrate.poller")

POLL_INTERVAL_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 900.0
BACKOFF_FACTOR = 2.0
# Retention is scheduled by elapsed time, not by a poll count. Tied to attempts it
# scaled with the interval: at the supported one-day maximum, "every 60 polls" meant
# every 60 days, so a 14-day raw window could hold bodies for 73 and the 90-day
# sample window overshot the same way. Six hours is far finer than either window at
# any interval, and the work is two indexed DELETEs.
PRUNE_EVERY = timedelta(hours=6)

# Doublings past which the backoff ceiling has certainly been reached, so the
# exponent saturates instead of growing until it overflows a float.
MAX_BACKOFF_STEPS = 64


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
        self._last_prune_at: datetime | None = None

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

            delay = self._schedule_next()

    def _schedule_next(self) -> float:
        """The next delay, computed so that nothing here can end the loop.

        `poll_once` is careful to never raise; this arithmetic sat outside it and
        was not, and twice now an OverflowError here has been what killed the poll
        task -- once from a non-finite configured interval, once from the backoff
        exponent after eleven days of failures. Both root causes are fixed, but a
        loop that must run for months should not depend on having enumerated every
        way multiplying two numbers can go wrong. A bad delay costs one mistimed
        poll; an escaping exception costs every poll after it.
        """
        try:
            delay = self.next_delay()
            self.status.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            return delay
        except Exception:  # noqa: BLE001 - the loop outliving this is the point
            logger.exception("could not compute the next poll delay; using the interval")
            self.status.next_attempt_at = None
            return POLL_INTERVAL_SECONDS

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
        # Saturate the exponent before raising to it. Once the ceiling applies the
        # exact value is irrelevant, but the arithmetic still has to happen, and
        # BACKOFF_FACTOR ** 1024 raises OverflowError -- reached after 1025
        # consecutive failures, about eleven days at the capped cadence. That is
        # an ordinary situation, not an exotic one: a machine left running with an
        # expired credential. The failure was that this runs outside poll_once's
        # handler, so the task died and would not resume when the credential came
        # back. MAX_BACKOFF_STEPS is far above what any sane interval needs to
        # reach the ceiling, and far below where the exponent overflows.
        steps = min(failures - 1, MAX_BACKOFF_STEPS)
        # The ceiling can never fall below the configured interval. A flat 900s cap
        # made backoff run backwards for any interval above it: an hourly poll
        # became a 15-minute one on its first failure, so the response to an
        # endpoint that was failing or rate-limiting us was to ask four times as
        # often. Retrying is allowed to be no gentler than normal polling; it must
        # never be more aggressive.
        ceiling = max(MAX_BACKOFF_SECONDS, self.interval)
        return min(self.interval * (BACKOFF_FACTOR**steps), ceiling)

    async def poll_once(self) -> UsageSnapshot | None:
        """One fetch/parse/store cycle. Records outcome in `status`; never raises."""
        now = datetime.now(UTC)
        self.status.last_attempt_at = now
        # Pruned on every attempt, not on the success path. A broken endpoint
        # archives a raw body on every poll, and if those bodies differ -- a
        # timestamped error page is enough -- the archive grows while a success-only
        # trigger never fires, so the advertised retention applied precisely never
        # during the outage that was filling it.
        self._maybe_prune(now)

        try:
            payload = await self._fetch_with_one_auth_retry()
        except CredentialError as exc:
            self._record_failure("credential", str(exc))
            return None
        except UsageFetchError as exc:
            # One branch for every fetch error, archiving whichever of them carried a
            # body. Two branches meant the archive depended on which exception type
            # the client happened to raise: an undecodable 2xx was kept, while a 429
            # or a 5xx -- the responses that actually explain why the dashboard went
            # quiet -- were not. The body arrives already redacted.
            if exc.body:
                self._archive_unreadable(exc.body, now)
            self._record_failure(_error_kind(exc), str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            logger.exception("unexpected poll failure")
            self._record_failure("unexpected", f"{type(exc).__name__}: {exc}")
            return None

        # Guarded for the same reason `_schedule_next` is: this call sits outside the
        # fetch handler above, and `_run` does not catch either, so anything escaping
        # the parser ends polling permanently. `parse_usage` is written never to raise
        # and a float conversion still found a way -- three times now an unhandled
        # exception on this path has killed the task, which is enough to stop relying
        # on having enumerated the ways.
        try:
            snapshot = parse_usage(payload, fetched_at=now)
        except Exception as exc:  # noqa: BLE001 - the loop outliving this is the point
            logger.exception("parser raised on a response")
            self._archive_unreadable(payload, now)
            self._record_failure("schema", f"parser error: {type(exc).__name__}: {exc}")
            return None

        if not snapshot.buckets:
            # A 200 we cannot read is a schema break, not a success. Archive the
            # body on the way past: this is precisely the response the raw
            # archive exists for, and it is the only kind that never reached it,
            # since the sample-writing path below is what normally records one.
            self._archive_unreadable(payload, now)
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

        return snapshot

    def _maybe_prune(self, now: datetime) -> None:
        """Apply the retention windows, at most once per PRUNE_EVERY."""
        if self._last_prune_at is not None and now - self._last_prune_at < PRUNE_EVERY:
            return
        # Stamped before the attempt, so a failing prune retries on the schedule
        # rather than on every single poll.
        self._last_prune_at = now
        try:
            self.store.prune()
        except Exception:  # noqa: BLE001 - pruning is housekeeping, not critical
            logger.exception("prune failed")

    def _archive_unreadable(self, body: Any, ts: datetime) -> None:
        """Keep the body that broke the parser, without letting that failure win.

        `body` is a decoded payload when the JSON parsed but yielded no buckets,
        and a redacted text excerpt when the decode itself failed. Both are the
        same kind of evidence about the same kind of break.

        Swallowed on purpose, and separately from the store failure the sample
        path reports: the caller is already on its way to recording a schema
        break, and losing the archive copy must not overwrite that diagnosis with
        a less useful one about the database.
        """
        try:
            self.store.append_raw(body, ts=ts)
        except Exception:  # noqa: BLE001 - archiving is best-effort by design
            logger.exception("failed to archive the unreadable response body")

    async def _fetch_with_one_auth_retry(self) -> Any:
        """Fetch, and on 401 re-read the credential once before giving up.

        Claude Code may have rotated the token between our read and the request.
        One re-read covers that race. We never mint or refresh a token ourselves;
        a second 401 means the data is stale and we say so.

        The reads go to a worker thread because `read_credential` shells out to
        `security`, twice, at a 10-second timeout each -- so on a machine waiting for
        keychain authorization it could hold the event loop for 20 seconds per poll.
        The request handlers are deliberately sync `def` so Starlette threadpools
        their blocking SQLite reads for exactly this reason; blocking the loop from
        the background task undid that, and every API and static request froze with
        it while the credential read was ultimately going to succeed from the file.
        """
        credential = await asyncio.to_thread(read_credential)
        self.status.credential_source = credential.source
        try:
            return await fetch_usage(credential.access_token, client=self._client)
        except UsageAuthError:
            retry = await asyncio.to_thread(read_credential)
            self.status.credential_source = retry.source
            if retry.access_token == credential.access_token:
                raise UsageAuthError(
                    "HTTP 401 and the stored credential has not changed -- "
                    "sign in with Claude Code to refresh it"
                ) from None
            return await fetch_usage(retry.access_token, client=self._client)

    def _record_failure(self, kind: str, message: str) -> None:
        """Record a failure, scrubbing on the way in.

        This is the one place every error message becomes `last_error`, which
        /api/now serves to the browser and the line below writes to the log -- two of
        the three places the token is forbidden. The messages are built from exception
        text, and exceptions quote what they choked on, so scrubbing here covers every
        current path and any added later. `fetch_usage` also scrubs with the exact
        token where it holds one, which catches an echo the pattern would miss; this
        is the backstop for everything that does not.
        """
        message = scrub(message)
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
