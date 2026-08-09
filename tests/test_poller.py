"""Poll-loop behavior: the 401 re-read, backoff, and failing loudly."""

import asyncio
from datetime import UTC, datetime

import pytest

from burnrate import poller as poller_module
from burnrate.client import (
    UsageAuthError,
    UsageHTTPError,
    UsageProtocolError,
    UsageTransportError,
)
from burnrate.credentials import Credential, CredentialError
from burnrate.poller import MAX_BACKOFF_SECONDS, Poller
from burnrate.store import Store


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
        raise UsageAuthError("HTTP 401")

    seen = _fetches(monkeypatch, handler)
    poller = Poller(store)

    assert await poller.poll_once() is None
    assert seen == ["same-token"], "must not retry the identical token"
    assert "sign in with Claude Code" in poller.status.last_error
    assert poller.status.last_error_kind == "auth"


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
