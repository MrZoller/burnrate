"""Poll-loop behavior: the 401 re-read, backoff, and failing loudly."""

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from burnrate import poller as poller_module
from burnrate.client import (
    UsageAuthError,
    UsageHTTPError,
    UsageProtocolError,
    UsageTransportError,
)
from burnrate.credentials import Credential, CredentialError
from burnrate.poller import MAX_BACKOFF_SECONDS, RETRY_AFTER_MAX_SECONDS, Poller
from burnrate.store import Store
from burnrate.usage import parse_usage


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "poller.db")


def _credentials(monkeypatch, *tokens):
    """Hand out `tokens` in order, repeating the last one forever."""
    calls = {"n": 0}

    def fake_read():
        index = min(calls["n"], len(tokens) - 1)
        calls["n"] += 1
        return Credential(access_token=tokens[index], source="file")

    monkeypatch.setattr(poller_module, "read_credential", fake_read)
    return calls


def _fetches(monkeypatch, handler):
    seen = []

    async def fake_fetch(token, client=None, **kwargs):
        seen.append(token)
        return handler(token, len(seen))

    monkeypatch.setattr(poller_module, "fetch_usage", fake_fetch)
    return seen


def _counting_prune(store, monkeypatch):
    """Count prune calls while still letting the real DELETEs run."""
    seen = {"n": 0}
    real = store.prune

    def counted(*args, **kwargs):
        seen["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "prune", counted)
    return seen


async def test_a_successful_poll_stores_and_clears_error_state(store, monkeypatch, live_response):
    _credentials(monkeypatch, "tok")
    _fetches(monkeypatch, lambda token, n: live_response)
    poller = Poller(store)

    snapshot = await poller.poll_once()

    assert snapshot is not None
    assert poller.status.healthy
    assert poller.status.last_error is None
    assert len(store.latest_per_bucket()) == 4


async def test_the_credential_is_reread_on_every_poll(store, monkeypatch, live_response):
    calls = _credentials(monkeypatch, "tok")
    _fetches(monkeypatch, lambda token, n: live_response)
    poller = Poller(store)

    await poller.poll_once()
    await poller.poll_once()

    assert calls["n"] == 2, "a cached token would miss Claude Code's refresh"


async def test_a_401_rereads_once_and_succeeds_with_the_rotated_token(
    store, monkeypatch, live_response
):
    _credentials(monkeypatch, "stale-token", "fresh-token")

    def handler(token, n):
        if token == "stale-token":
            raise UsageAuthError("HTTP 401")
        return live_response

    seen = _fetches(monkeypatch, handler)
    poller = Poller(store)

    snapshot = await poller.poll_once()

    assert snapshot is not None
    assert seen == ["stale-token", "fresh-token"]
    assert poller.status.healthy


async def test_a_401_with_an_unchanged_token_gives_up_rather_than_refreshing(store, monkeypatch):
    """We never mint or refresh a token; an unchanged credential means stale data."""
    _credentials(monkeypatch, "same-token")

    def handler(token, n):
        raise UsageAuthError("HTTP 401", status_code=401)

    seen = _fetches(monkeypatch, handler)
    poller = Poller(store)

    assert await poller.poll_once() is None
    assert seen == ["same-token"], "must not retry the identical token"
    assert "HTTP 401" in poller.status.last_error
    assert "sign in with Claude Code" in poller.status.last_error
    assert poller.status.last_error_kind == "auth"


async def test_a_403_is_not_reported_as_a_stale_token(store, monkeypatch):
    """Regression: both statuses arrive as UsageAuthError and this message hard-coded
    "HTTP 401 ... sign in with Claude Code", so a permissions or account denial was
    reported as an expired token -- sending the user to do the one thing that cannot
    fix it."""
    _credentials(monkeypatch, "same-token")

    def handler(token, n):
        raise UsageAuthError("HTTP 403", status_code=403)

    _fetches(monkeypatch, handler)
    poller = Poller(store)

    assert await poller.poll_once() is None
    assert "HTTP 403" in poller.status.last_error
    assert "401" not in poller.status.last_error
    assert "sign in with Claude Code" not in poller.status.last_error
    assert "permissions or account" in poller.status.last_error


async def test_an_auth_error_of_unknown_status_still_reads_sensibly(store, monkeypatch):
    """Nothing constructs one without a status today, but the message must not say
    "HTTP None" if something ever does."""
    _credentials(monkeypatch, "same-token")

    def handler(token, n):
        raise UsageAuthError("auth failed")

    _fetches(monkeypatch, handler)
    poller = Poller(store)

    await poller.poll_once()

    assert "None" not in poller.status.last_error
    assert "Authorization failed" in poller.status.last_error


async def test_the_credential_read_does_not_block_the_event_loop(store, monkeypatch, live_response):
    """Regression: `read_credential` shells out to `security` twice at a 10-second
    timeout each, so on a machine waiting for keychain authorization the poll held
    the event loop for up to 20 seconds and every API and static request froze with
    it. The handlers are sync `def` precisely so Starlette threadpools their blocking
    work; blocking the loop from the background task undid that.
    """
    import time

    def slow_read():
        time.sleep(0.3)
        return Credential(access_token="tok", source="file")

    monkeypatch.setattr(poller_module, "read_credential", slow_read)
    _fetches(monkeypatch, lambda token, n: live_response)
    poller = Poller(store)

    ticks = {"n": 0}
    stop = asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks["n"] += 1

    beat = asyncio.create_task(heartbeat())
    await poller.poll_once()
    stop.set()
    await beat

    assert poller.status.healthy
    assert ticks["n"] > 5, f"the loop only ran {ticks['n']} times; the read blocked it"


async def test_a_missing_credential_is_recorded_not_raised(store, monkeypatch):
    def boom():
        raise CredentialError("no credential anywhere")

    monkeypatch.setattr(poller_module, "read_credential", boom)
    poller = Poller(store)

    assert await poller.poll_once() is None
    assert poller.status.last_error_kind == "credential"
    assert poller.status.consecutive_failures == 1


async def test_a_200_with_an_unreadable_body_counts_as_failure_not_success(store, monkeypatch):
    """A schema break must not read as a healthy poll with zero buckets."""
    _credentials(monkeypatch, "tok")
    _fetches(monkeypatch, lambda token, n: {"something": "entirely new"})
    poller = Poller(store)

    assert await poller.poll_once() is None
    assert poller.status.healthy is False
    assert poller.status.last_error_kind == "schema"
    assert store.latest_per_bucket() == []


async def test_the_body_that_broke_the_parser_is_archived(store, monkeypatch):
    """Regression: the raw archive exists to make an endpoint change diagnosable
    after the fact, and the schema break was the one response it never captured
    -- this path returned before the sample write that normally records one."""
    _credentials(monkeypatch, "tok")
    _fetches(monkeypatch, lambda token, n: {"something": "entirely new"})
    poller = Poller(store)

    await poller.poll_once()

    with store._connect() as conn:
        bodies = [row["body"] for row in conn.execute("SELECT body FROM raw_snapshots")]

    assert len(bodies) == 1
    assert "entirely new" in bodies[0]


async def test_an_undecodable_200_is_archived_too(store, monkeypatch):
    """The other unreadable-200 shape, and the likelier one in practice: the JSON
    decode fails inside the client, so the poller never receives a payload. It
    used to record the failure with nothing kept, losing exactly the evidence the
    archive exists for."""
    _credentials(monkeypatch, "tok")

    def handler(token, n):
        raise UsageProtocolError(
            "response was not JSON: char 0",
            body="<html><body>Sign in to continue</body></html>",
        )

    _fetches(monkeypatch, handler)
    poller = Poller(store)

    assert await poller.poll_once() is None
    assert poller.status.last_error_kind == "protocol"

    with store._connect() as conn:
        bodies = [row["body"] for row in conn.execute("SELECT body FROM raw_snapshots")]

    assert len(bodies) == 1
    assert "Sign in to continue" in bodies[0]


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_a_rate_limit_or_server_error_body_is_archived(store, monkeypatch, status):
    """Regression: the archive depended on which exception type the client happened
    to raise. An undecodable 2xx was kept; a 429 or 5xx was not -- and those are the
    responses that explain why the dashboard went quiet."""
    _credentials(monkeypatch, "tok")

    def handler(token, n):
        raise UsageHTTPError(status, "rate limited", body='{"error":"slow down","retry_after":42}')

    _fetches(monkeypatch, handler)
    poller = Poller(store)

    assert await poller.poll_once() is None
    assert poller.status.last_error_kind == "http"

    with store._connect() as conn:
        bodies = [row["body"] for row in conn.execute("SELECT body FROM raw_snapshots")]

    assert len(bodies) == 1
    assert "slow down" in bodies[0]


async def test_an_error_with_no_body_archives_nothing(store, monkeypatch):
    """A transport failure never had a response, so there is nothing to keep."""
    _credentials(monkeypatch, "tok")

    def handler(token, n):
        raise UsageTransportError("connection refused")

    _fetches(monkeypatch, handler)
    poller = Poller(store)

    await poller.poll_once()

    with store._connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM raw_snapshots").fetchone()["n"]

    assert count == 0


async def test_a_protocol_error_without_a_body_archives_nothing(store, monkeypatch):
    """No body means nothing to keep -- it must not write an empty row."""
    _credentials(monkeypatch, "tok")

    def handler(token, n):
        raise UsageProtocolError("response was not JSON: char 0")

    _fetches(monkeypatch, handler)
    poller = Poller(store)

    await poller.poll_once()

    with store._connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM raw_snapshots").fetchone()["n"]

    assert count == 0
    assert poller.status.last_error_kind == "protocol"


async def test_pruning_happens_even_while_every_poll_is_failing(store, monkeypatch):
    """Regression: pruning ran only on the success path, while _archive_unreadable
    added a row on every failure. A body that differs between attempts -- a
    timestamped error page is enough -- grew the archive without bound, so retention
    applied precisely never during the outage that was filling it."""
    _credentials(monkeypatch, "tok")

    def handler(token, n):
        raise UsageProtocolError("not JSON", body=f"<html>error {n}</html>")

    _fetches(monkeypatch, handler)
    poller = Poller(store)
    pruned = _counting_prune(store, monkeypatch)

    await poller.poll_once()

    assert poller.status.consecutive_failures == 1
    assert pruned["n"] == 1, "retention must not depend on a poll ever succeeding"

    with store._connect() as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM raw_snapshots").fetchone()["n"]
    assert rows == 1


async def test_pruning_is_scheduled_by_time_not_by_poll_count(store, monkeypatch, live_response):
    """Regression: tied to a poll count it scaled with the interval -- at the
    supported one-day maximum, "every 60 polls" meant every 60 days, so a 14-day raw
    window could hold bodies for 73 and the 90-day sample window overshot too."""
    _credentials(monkeypatch, "tok")
    _fetches(monkeypatch, lambda token, n: live_response)
    poller = Poller(store, interval=86400.0)
    pruned = _counting_prune(store, monkeypatch)

    await poller.poll_once()
    assert pruned["n"] == 1, "the first attempt prunes"

    await poller.poll_once()
    await poller.poll_once()
    assert pruned["n"] == 1, "and not again until the window elapses"

    poller._last_prune_at -= poller_module.PRUNE_EVERY
    await poller.poll_once()
    assert pruned["n"] == 2, "once PRUNE_EVERY has passed it runs again"


async def test_a_failing_prune_retries_on_schedule_not_on_every_poll(store, monkeypatch):
    """Stamping the attempt rather than the success keeps a broken prune from running
    two indexed DELETEs on every single poll."""
    _credentials(monkeypatch, "tok")
    _fetches(monkeypatch, lambda token, n: {"nothing": "usable"})
    poller = Poller(store)
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise OSError("disk full")

    monkeypatch.setattr(store, "prune", boom)

    for _ in range(5):
        await poller.poll_once()

    assert calls["n"] == 1


async def test_a_failed_archive_does_not_replace_the_schema_diagnosis(store, monkeypatch):
    """Losing the archive copy is a footnote; the reported error must still be
    the schema break, not a database complaint about storing it."""
    _credentials(monkeypatch, "tok")
    _fetches(monkeypatch, lambda token, n: {"something": "entirely new"})
    poller = Poller(store)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "append_raw", boom)

    assert await poller.poll_once() is None
    assert poller.status.last_error_kind == "schema"
    assert poller.status.consecutive_failures == 1


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (UsageTransportError("connection reset"), "transport"),
        (UsageHTTPError(503, "upstream down"), "http"),
    ],
)
async def test_transport_and_server_failures_are_classified(store, monkeypatch, error, kind):
    _credentials(monkeypatch, "tok")

    def handler(token, n):
        raise error

    _fetches(monkeypatch, handler)
    poller = Poller(store)

    await poller.poll_once()

    assert poller.status.last_error_kind == kind


async def test_failures_accumulate_and_a_success_resets_them(store, monkeypatch, live_response):
    _credentials(monkeypatch, "tok")
    outcome = {"fail": True}

    def handler(token, n):
        if outcome["fail"]:
            raise UsageTransportError("nope")
        return live_response

    _fetches(monkeypatch, handler)
    poller = Poller(store)

    await poller.poll_once()
    await poller.poll_once()
    assert poller.status.consecutive_failures == 2

    outcome["fail"] = False
    await poller.poll_once()
    assert poller.status.consecutive_failures == 0
    assert poller.status.healthy


def test_steady_state_waits_one_interval(store):
    poller = Poller(store, interval=60.0)

    assert poller.status.consecutive_failures == 0
    assert poller.next_delay() == 60.0


@pytest.mark.parametrize(
    ("failures", "expected"),
    [(1, 60.0), (2, 120.0), (3, 240.0), (4, 480.0), (20, MAX_BACKOFF_SECONDS)],
)
def test_backoff_doubles_per_failure_and_is_capped(store, failures, expected):
    poller = Poller(store, interval=60.0)
    poller.status.consecutive_failures = failures

    assert poller.next_delay() == expected


def test_backoff_honours_a_custom_interval(store):
    poller = Poller(store, interval=5.0)
    poller.status.consecutive_failures = 3

    assert poller.next_delay() == 20.0


@pytest.mark.parametrize("interval", [901.0, 3600.0, 86400.0])
def test_backoff_never_polls_more_often_than_configured(store, interval):
    """Regression: the ceiling was a flat 900s, so any interval above it got
    SHORTER on the first failure -- an hourly poll became a 15-minute one, asking
    four times as often precisely while the endpoint was failing or rate-limiting.
    Retrying may be no gentler than normal polling; it must never be harsher."""
    poller = Poller(store, interval=interval)

    for failures in (0, 1, 2, 10, 1000):
        poller.status.consecutive_failures = failures
        assert poller.next_delay() >= interval, f"{failures} failures shortened the interval"


def test_a_short_interval_still_backs_off_to_the_usual_ceiling(store):
    """The ordinary case must be untouched by the fix above."""
    poller = Poller(store, interval=60.0)
    poller.status.consecutive_failures = 20

    assert poller.next_delay() == MAX_BACKOFF_SECONDS


@pytest.mark.parametrize("failures", [1024, 1025, 5000, 10**6])
def test_backoff_survives_an_enormous_failure_count(store, failures):
    """Regression: the exponent was raised before min() could cap it, so
    BACKOFF_FACTOR ** 1024 raised OverflowError at 1025 consecutive failures --
    about eleven days at the capped cadence, i.e. a machine left running with an
    expired credential. next_delay() is called outside poll_once's handler, so the
    task died there and would not resume when the credential came back."""
    poller = Poller(store, interval=60.0)
    poller.status.consecutive_failures = failures

    assert poller.next_delay() == MAX_BACKOFF_SECONDS


def test_the_loop_survives_a_delay_it_cannot_compute(store, monkeypatch):
    """Both known causes are fixed; this pins the property they violated. The
    delay arithmetic sits outside poll_once, so anything escaping it ends the loop
    for good -- one mistimed poll is the acceptable cost, every future poll is not."""
    poller = Poller(store, interval=60.0)

    def boom():
        raise OverflowError("result too large")

    monkeypatch.setattr(poller, "next_delay", boom)

    assert poller._schedule_next() == 60.0
    assert poller.status.next_attempt_at is None


def test_a_scheduled_delay_is_recorded_for_the_ui(store):
    poller = Poller(store, interval=30.0)

    delay = poller._schedule_next()

    assert delay == 30.0
    assert poller.status.next_attempt_at is not None


async def test_the_loop_backs_off_after_a_failure(store, monkeypatch, live_response):
    """Exercises _run itself, not a re-derivation of its arithmetic."""
    _credentials(monkeypatch, "tok")
    waits: list[float] = []
    outcome = {"fail": True}

    def handler(token, n):
        if outcome["fail"]:
            raise UsageTransportError("nope")
        return live_response

    _fetches(monkeypatch, handler)
    poller = Poller(store, interval=60.0)

    async def fake_wait_for(awaitable, timeout):
        waits.append(timeout)
        awaitable.close()
        if len(waits) >= 3:
            poller._stopping.set()
            return True
        if len(waits) == 2:
            outcome["fail"] = False
        raise TimeoutError

    monkeypatch.setattr(poller_module.asyncio, "wait_for", fake_wait_for)
    await poller._run()

    # First failure -> 60s, second failure -> 120s, then a success drops it back.
    assert waits[:3] == [60.0, 120.0, 60.0]


async def test_the_loop_recovers_after_a_fortnight_of_failures(store, monkeypatch, live_response):
    """The consequence the arithmetic fix exists for, asserted as behaviour: after
    the failure count passes the old overflow point, the credential coming back
    must produce a successful poll. Before the fix the task was already dead and
    nothing after this point ever ran again."""
    _credentials(monkeypatch, "tok")
    waits: list[float] = []
    outcome = {"fail": True}

    def handler(token, n):
        if outcome["fail"]:
            raise UsageTransportError("endpoint down")
        return live_response

    _fetches(monkeypatch, handler)
    poller = Poller(store, interval=60.0)
    # Eleven days of failures already behind us, one past where 2.0**n overflowed.
    poller.status.consecutive_failures = 1025

    async def fake_wait_for(awaitable, timeout):
        waits.append(timeout)
        awaitable.close()
        if len(waits) >= 2:
            poller._stopping.set()
            return True
        outcome["fail"] = False  # the credential comes back
        raise TimeoutError

    monkeypatch.setattr(poller_module.asyncio, "wait_for", fake_wait_for)
    await poller._run()

    assert waits[0] == MAX_BACKOFF_SECONDS, "should still be at the ceiling, not crashed"
    assert poller.status.last_success_at is not None, "the loop must recover"
    assert poller.status.consecutive_failures == 0
    assert store.latest_per_bucket(), "and it must have stored the recovered reading"


async def test_the_loop_polls_then_stops_cleanly(store, monkeypatch, live_response):
    _credentials(monkeypatch, "tok")
    _fetches(monkeypatch, lambda token, n: live_response)
    poller = Poller(store, interval=0.01)

    await poller.start()
    for _ in range(200):  # let at least one poll land
        if poller.status.last_success_at is not None:
            break
        await asyncio.sleep(0.005)
    await poller.stop()

    assert poller.status.last_success_at is not None
    assert poller._task is None
    assert store.latest_per_bucket(), "the loop must persist what it fetched"


async def test_stop_is_safe_before_start_and_twice(store):
    poller = Poller(store)

    await poller.stop()
    await poller.stop()

    assert poller._task is None


async def test_staleness_is_measured_from_the_last_success(store, monkeypatch, live_response):
    _credentials(monkeypatch, "tok")
    _fetches(monkeypatch, lambda token, n: live_response)
    poller = Poller(store)
    await poller.poll_once()

    later = datetime.now(UTC).replace(microsecond=0)
    assert poller.staleness_seconds(now=later) < 5


async def test_staleness_is_unknown_before_any_data(store):
    assert Poller(store).staleness_seconds() is None


async def test_a_store_failure_does_not_kill_the_loop(store, monkeypatch, live_response):
    _credentials(monkeypatch, "tok")
    _fetches(monkeypatch, lambda token, n: live_response)

    def explode(*args, **kwargs):
        raise OSError("disk full")

    poller = Poller(store)
    monkeypatch.setattr(poller.store, "append_snapshot", explode)

    assert await poller.poll_once() is None
    assert poller.status.last_error_kind == "store"


async def test_the_error_message_never_contains_the_token(store, monkeypatch):
    secret = "sk-ant-oat01-secret"
    _credentials(monkeypatch, secret)

    def handler(token, n):
        raise UsageHTTPError(500, "internal error")

    _fetches(monkeypatch, handler)
    poller = Poller(store)

    await poller.poll_once()

    assert secret not in (poller.status.last_error or "")
    assert secret not in str(poller.status.as_dict())


async def test_a_parser_crash_does_not_end_polling(store, monkeypatch, live_response):
    """Third time an unhandled exception on this path has killed the task. parse_usage
    is written never to raise and a float conversion still found a way, so the call is
    guarded: a response the parser cannot handle costs one poll, not every future one."""
    _credentials(monkeypatch, "tok")
    calls = {"n": 0}

    def handler(token, n):
        calls["n"] += 1
        return {"five_hour": {"utilization": 30}} if calls["n"] > 1 else {"boom": True}

    _fetches(monkeypatch, handler)

    def exploding_parse(payload, fetched_at=None):
        if "boom" in payload:
            raise RuntimeError("parser blew up")
        return parse_usage(payload, fetched_at=fetched_at)

    monkeypatch.setattr(poller_module, "parse_usage", exploding_parse)
    poller = Poller(store)

    assert await poller.poll_once() is None
    assert poller.status.last_error_kind == "schema"
    assert "parser blew up" in poller.status.last_error

    # The loop survives, so the next poll succeeds.
    assert await poller.poll_once() is not None
    assert poller.status.healthy


async def test_a_recorded_failure_is_scrubbed(store, monkeypatch):
    """`last_error` is served by /api/now and written to the log, and these messages
    are built from exception text -- which quotes whatever the exception choked on."""
    _credentials(monkeypatch, "tok")
    token = "sk-ant-oat01-a-real-looking-token-abc123"

    def handler(token_, n):
        raise UsageTransportError(f"LocalProtocolError: Illegal header value b'Bearer {token}'")

    _fetches(monkeypatch, handler)
    poller = Poller(store)

    await poller.poll_once()

    assert token not in poller.status.last_error
    assert "sk-ant" not in poller.status.last_error


@pytest.mark.parametrize("status", [401, 403])
async def test_an_auth_denial_body_reaches_the_archive(store, monkeypatch, status):
    """Two halves to this: the client has to attach the body, and
    `_unchanged_credential_error` has to carry it forward -- it builds a replacement
    exception, so anything not copied across is lost."""
    _credentials(monkeypatch, "same-token")
    denial = '{"error":{"type":"permission_error","message":"organization has disabled this"}}'

    def handler(token, n):
        raise UsageAuthError(f"HTTP {status}", status_code=status, body=denial)

    _fetches(monkeypatch, handler)
    poller = Poller(store)

    assert await poller.poll_once() is None
    assert poller.status.last_error_kind == "auth"

    with store._connect() as conn:
        bodies = [row["body"] for row in conn.execute("SELECT body FROM raw_snapshots")]

    assert len(bodies) == 1
    assert "organization has disabled this" in bodies[0]


async def test_the_replacement_auth_error_keeps_the_status_and_the_body(store, monkeypatch):
    """The message is rewritten; nothing else may be dropped in the process."""
    _credentials(monkeypatch, "same-token")

    def handler(token, n):
        raise UsageAuthError("HTTP 403", status_code=403, body='{"why":"policy"}')

    _fetches(monkeypatch, handler)
    poller = Poller(store)

    await poller.poll_once()

    with store._connect() as conn:
        bodies = [row["body"] for row in conn.execute("SELECT body FROM raw_snapshots")]

    assert "policy" in bodies[0]
    assert "HTTP 403" in poller.status.last_error


UNPATTERNED_TOKEN = "eyJhbGciOiJIUzI1NiJ9.a-format-the-pattern-cannot-match.sig"


@pytest.mark.parametrize(
    "payload_for",
    [
        pytest.param(
            lambda t: {"five_hour": {"utilization": 30}, "debug": {"echo": t}}, id="value"
        ),
        pytest.param(lambda t: {"five_hour": {"utilization": 30}, t: {"utilization": 5}}, id="key"),
        pytest.param(
            lambda t: {"five_hour": {"utilization": 30}, "d": {"list": ["a", t]}}, id="nested"
        ),
        pytest.param(lambda t: {"five_hour": {"utilization": t}}, id="malformed-utilization"),
    ],
)
async def test_a_token_the_pattern_cannot_match_is_still_scrubbed(store, monkeypatch, payload_for):
    """The redaction guarantee has to be absolute, not heuristic. `scrub` only knows
    `sk-ant-...`, while `parse_credentials_json` accepts any non-empty string on purpose
    -- Claude Code owns the credential's format, and refusing an unrecognised shape would
    mean a future token stops this dashboard dead. So the exact token has to do the work,
    which means scrubbing where it is held rather than downstream."""
    monkeypatch.setattr(
        poller_module,
        "read_credential",
        lambda: Credential(access_token=UNPATTERNED_TOKEN, source="file"),
    )
    _fetches(monkeypatch, lambda token, n: payload_for(token))
    poller = Poller(store)

    snapshot = await poller.poll_once()

    assert UNPATTERNED_TOKEN not in str(store.path.read_bytes())
    with store._connect() as conn:
        archived = " ".join(r["body"] for r in conn.execute("SELECT body FROM raw_snapshots"))
    rendered = " ".join(
        [
            archived,
            *(snapshot.warnings if snapshot else ()),
            *(snapshot.notices if snapshot else ()),
            *((b.key for b in snapshot.buckets) if snapshot else ()),
            *((b.label for b in snapshot.buckets) if snapshot else ()),
        ]
    )
    assert UNPATTERNED_TOKEN not in rendered


async def test_the_rotated_token_is_scrubbed_on_the_retry_path_too(
    store, monkeypatch, live_response
):
    """The 401 re-read fetches with a different credential, so that branch needs the
    same treatment -- it is a second call site, which is exactly how these get missed."""
    tokens = iter([UNPATTERNED_TOKEN, UNPATTERNED_TOKEN + "-rotated"])
    seen = []

    def read():
        value = next(tokens, UNPATTERNED_TOKEN + "-rotated")
        seen.append(value)
        return Credential(access_token=value, source="file")

    monkeypatch.setattr(poller_module, "read_credential", read)

    def handler(token, n):
        if n == 1:
            raise UsageAuthError("HTTP 401", status_code=401)
        return {"five_hour": {"utilization": 30}, "debug": {"echo": token}}

    _fetches(monkeypatch, handler)
    poller = Poller(store)

    await poller.poll_once()

    with store._connect() as conn:
        archived = " ".join(r["body"] for r in conn.execute("SELECT body FROM raw_snapshots"))
    assert seen[-1] not in archived
    assert "<redacted>" in archived


async def test_an_ordinary_payload_is_unchanged_by_the_scrub(store, monkeypatch, live_response):
    """It runs on every successful poll, so it must be a no-op on real data."""
    _credentials(monkeypatch, "sk-ant-oat01-tok")
    _fetches(monkeypatch, lambda token, n: live_response)
    poller = Poller(store)

    snapshot = await poller.poll_once()

    assert [b.key for b in snapshot.buckets] == [
        "five_hour",
        "seven_day",
        "seven_day_fable",
        "nimbus_quill",
    ]
    assert snapshot.warnings == ()


# ----------------------------------------------------- Retry-After on a 429 (#7)


def test_retry_after_parses_integer_delta_seconds():
    assert poller_module._parse_retry_after("120") == 120.0


def test_retry_after_parses_an_http_date():
    """RFC 7231's other form: an absolute HTTP-date, honoured as the seconds from now
    until that moment."""
    header = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)

    seconds = poller_module._parse_retry_after(header)

    assert seconds is not None
    assert 110 <= seconds <= 120, f"expected ~120s until the date, got {seconds}"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("not-a-date", id="malformed"),
        pytest.param("-5", id="negative"),
    ],
)
def test_retry_after_falls_back_to_none_for_unusable_values(value):
    """Absent, malformed, or negative each reads as "no Retry-After", so a bad header
    can only ever fall back to plain backoff -- never shorten it."""
    assert poller_module._parse_retry_after(value) is None


def test_a_429_retry_after_larger_than_the_backoff_wins(store):
    """Retrying sooner than the rate limiter asked prolongs the limit, so a larger
    Retry-After is a hard floor on the next attempt."""
    poller = Poller(store, interval=60.0)
    poller.status.consecutive_failures = 1  # exponential term would be 60s
    poller._retry_after_seconds = 300.0

    assert poller.next_delay() == 300.0


def test_a_429_retry_after_may_exceed_the_backoff_ceiling(store):
    """The 900s ceiling clamps the exponential term, not a delay the server explicitly
    demanded -- retrying before it is pointless. 1800s is above the ceiling but below
    the RETRY_AFTER_MAX_SECONDS sanity cap, so it shows R beating the ceiling cleanly."""
    poller = Poller(store, interval=60.0)
    poller.status.consecutive_failures = 20  # exponential term is capped at 900s
    poller._retry_after_seconds = 1800.0

    assert poller.next_delay() == 1800.0
    assert poller.next_delay() > MAX_BACKOFF_SECONDS


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("99999999", id="huge-integer"),
        pytest.param(
            format_datetime(datetime.now(UTC) + timedelta(days=365), usegmt=True),
            id="far-future-date",
        ),
    ],
)
def test_a_pathological_retry_after_is_clamped_to_the_cap(value):
    """A header far above any legitimate value -- Retry-After: 99999999, a date a year
    out -- must not stall an unattended dashboard until restart; it resolves to exactly
    the sanity cap, not the raw value."""
    assert poller_module._parse_retry_after(value) == RETRY_AFTER_MAX_SECONDS


def test_a_clamped_retry_after_still_beats_the_backoff(store):
    """The capped value is a real floor: when it exceeds the exponential term it is
    what next_delay returns."""
    poller = Poller(store, interval=60.0)
    poller.status.consecutive_failures = 1  # exponential term would be 60s
    poller._retry_after_seconds = poller_module._parse_retry_after("99999999")

    assert poller.next_delay() == RETRY_AFTER_MAX_SECONDS


def test_a_429_retry_after_smaller_than_the_backoff_is_ignored(store):
    """Waiting longer than asked is harmless, so when backoff already dictates longer
    we keep that."""
    poller = Poller(store, interval=60.0)
    poller.status.consecutive_failures = 4  # exponential term is 480s
    poller._retry_after_seconds = 100.0

    assert poller.next_delay() == 480.0


async def test_a_429_retry_after_sets_the_next_delay_floor(store, monkeypatch):
    """End to end through the client's error: a 429 carrying Retry-After makes the
    delay that follows at least that long."""
    _credentials(monkeypatch, "tok")

    def handler(token, n):
        raise UsageHTTPError(429, "slow down", retry_after="300")

    _fetches(monkeypatch, handler)
    poller = Poller(store, interval=60.0)

    await poller.poll_once()

    assert poller.status.consecutive_failures == 1
    assert poller.next_delay() == 300.0  # max(60s backoff, 300s Retry-After)


async def test_a_429_without_a_retry_after_uses_plain_backoff(store, monkeypatch):
    """Scope check: 429 is the honoured status, but only when it actually carried a
    usable header. Without one it is ordinary exponential backoff."""
    _credentials(monkeypatch, "tok")

    def handler(token, n):
        raise UsageHTTPError(429, "slow down")  # no Retry-After

    _fetches(monkeypatch, handler)
    poller = Poller(store, interval=60.0)

    await poller.poll_once()

    assert poller._retry_after_seconds is None
    assert poller.next_delay() == 60.0


async def test_a_429_retry_after_does_not_persist_past_a_success(store, monkeypatch, live_response):
    """The floor applied to the backoff after that 429 alone; a success ends the streak
    and must clear it so it never colours a later one."""
    _credentials(monkeypatch, "tok")
    outcome = {"fail": True}

    def handler(token, n):
        if outcome["fail"]:
            raise UsageHTTPError(429, "slow down", retry_after="300")
        return live_response

    _fetches(monkeypatch, handler)
    poller = Poller(store, interval=60.0)

    await poller.poll_once()
    assert poller._retry_after_seconds == 300.0

    outcome["fail"] = False
    await poller.poll_once()

    assert poller._retry_after_seconds is None
    assert poller.next_delay() == 60.0  # steady state, no lingering floor


async def test_a_non_429_failure_clears_a_prior_retry_after(store, monkeypatch):
    """A 429's Retry-After must not colour an unrelated later backoff: a following
    non-429 failure falls back to pure exponential."""
    _credentials(monkeypatch, "tok")
    outcome = {"status": 429}

    def handler(token, n):
        if outcome["status"] == 429:
            raise UsageHTTPError(429, "slow down", retry_after="300")
        raise UsageHTTPError(500, "upstream down")

    _fetches(monkeypatch, handler)
    poller = Poller(store, interval=60.0)

    await poller.poll_once()
    assert poller._retry_after_seconds == 300.0

    outcome["status"] = 500
    await poller.poll_once()

    assert poller._retry_after_seconds is None
    assert poller.status.consecutive_failures == 2
    assert poller.next_delay() == 120.0  # pure exponential for two failures
