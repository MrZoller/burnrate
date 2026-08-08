"""Poll-loop behavior: the 401 re-read, backoff, and failing loudly."""

from datetime import UTC, datetime

import pytest

from burnrate import poller as poller_module
from burnrate.client import UsageAuthError, UsageHTTPError, UsageTransportError
from burnrate.credentials import Credential, CredentialError
from burnrate.poller import BACKOFF_FACTOR, MAX_BACKOFF_SECONDS, Poller
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


def test_backoff_grows_exponentially_and_is_capped():
    interval = 60.0

    def delay(failures):
        return min(interval * (BACKOFF_FACTOR ** (failures - 1)), MAX_BACKOFF_SECONDS)

    assert delay(1) == 60
    assert delay(2) == 120
    assert delay(3) == 240
    assert delay(20) == MAX_BACKOFF_SECONDS


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
