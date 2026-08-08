"""The HTTP contract with the usage endpoint.

Everything else in the suite mocks `fetch_usage` away, so this file is the only
place the real request and its error mapping are exercised. A fake transport is
used rather than a live call: the assertions are about what we send and how we
classify what comes back, neither of which needs the network.
"""

import httpx
import pytest

from burnrate.client import (
    OAUTH_BETA,
    USAGE_URL,
    UsageAuthError,
    UsageHTTPError,
    UsageProtocolError,
    UsageTransportError,
    build_headers,
    fetch_usage,
)

TOKEN = "sk-ant-oat01-example-token-value"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------- headers


def test_headers_carry_the_bearer_token_and_the_beta_flag():
    headers = build_headers(TOKEN)

    assert headers["Authorization"] == f"Bearer {TOKEN}"
    # Without this header the endpoint does not answer -- it is the integration.
    assert headers["anthropic-beta"] == OAUTH_BETA
    assert headers["Accept"] == "application/json"


def test_user_agent_identifies_this_client():
    assert "burnrate" in build_headers(TOKEN)["User-Agent"]


async def test_the_request_actually_sends_those_headers(live_response):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["beta"] = request.headers.get("anthropic-beta")
        seen["method"] = request.method
        return httpx.Response(200, json=live_response)

    async with _client(handler) as client:
        await fetch_usage(TOKEN, client=client)

    assert seen["method"] == "GET"
    assert seen["url"] == USAGE_URL
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["beta"] == OAUTH_BETA


# -------------------------------------------------------------------- success


async def test_a_200_returns_the_decoded_body(live_response):
    async with _client(lambda r: httpx.Response(200, json=live_response)) as client:
        payload = await fetch_usage(TOKEN, client=client)

    assert payload["five_hour"]["utilization"] == 38.0
    assert len(payload["limits"]) == 3


# --------------------------------------------------------------- error mapping


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_map_to_usage_auth_error(status):
    async with _client(lambda r: httpx.Response(status, json={"error": "nope"})) as client:
        with pytest.raises(UsageAuthError):
            await fetch_usage(TOKEN, client=client)


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
async def test_other_http_failures_map_to_usage_http_error(status):
    async with _client(lambda r: httpx.Response(status, text="upstream said no")) as client:
        with pytest.raises(UsageHTTPError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    assert excinfo.value.status_code == status


async def test_a_200_that_is_not_json_is_a_protocol_error():
    """A captive portal or an HTML error page must not read as usable data."""
    async with _client(lambda r: httpx.Response(200, text="<html>hello</html>")) as client:
        with pytest.raises(UsageProtocolError):
            await fetch_usage(TOKEN, client=client)


async def test_a_network_failure_maps_to_transport_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    async with _client(handler) as client:
        with pytest.raises(UsageTransportError):
            await fetch_usage(TOKEN, client=client)


async def test_a_timeout_maps_to_transport_error():
    def handler(request):
        raise httpx.ReadTimeout("too slow")

    async with _client(handler) as client:
        with pytest.raises(UsageTransportError):
            await fetch_usage(TOKEN, client=client)


# ------------------------------------------------------------------- leakage


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(401, text=f"token {TOKEN} rejected"),
        lambda r: httpx.Response(500, text=f"internal error for {TOKEN}"),
        lambda r: httpx.Response(200, text="not json"),
    ],
)
async def test_no_error_message_echoes_the_token(handler):
    """Even when the upstream reflects it back at us, it must not reach a log."""
    async with _client(handler) as client:
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 - any of our errors
            await fetch_usage(TOKEN, client=client)

    assert TOKEN not in str(excinfo.value)


async def test_the_body_excerpt_is_truncated():
    async with _client(lambda r: httpx.Response(500, text="x" * 5000)) as client:
        with pytest.raises(UsageHTTPError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    assert len(str(excinfo.value)) < 400


# ----------------------------------------------------------- client lifecycle


async def test_a_borrowed_client_is_left_open(live_response):
    client = _client(lambda r: httpx.Response(200, json=live_response))

    await fetch_usage(TOKEN, client=client)

    assert not client.is_closed, "we must not close a client we did not create"
    await client.aclose()
