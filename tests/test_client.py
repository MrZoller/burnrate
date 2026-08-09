"""The HTTP contract with the usage endpoint.

Everything else in the suite mocks `fetch_usage` away, so this file is the only
place the real request and its error mapping are exercised. A fake transport is
used rather than a live call: the assertions are about what we send and how we
classify what comes back, neither of which needs the network.
"""

import httpx
import pytest

from burnrate.client import (
    ARCHIVE_BODY_LIMIT,
    ERROR_BODY_LIMIT,
    OAUTH_BETA,
    REDACTED,
    USAGE_URL,
    UsageAuthError,
    UsageFetchError,
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


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_an_http_error_carries_its_body_for_the_archive(status):
    """Regression: only the undecodable-2xx path kept a body, so a 429 or a 5xx --
    the responses that actually explain why the dashboard went quiet -- were the ones
    being dropped from the archive."""
    detail = '{"error":{"message":"rate limit exceeded","retry_after":42}}'
    async with _client(lambda r: httpx.Response(status, text=detail)) as client:
        with pytest.raises(UsageHTTPError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    assert "rate limit exceeded" in excinfo.value.body
    assert "retry_after" in excinfo.value.body


async def test_a_429_captures_the_retry_after_header():
    """The rate limiter names the earliest it will answer; the client carries the raw
    header so the poller can honour it (issue #7). Parsing lives in the poller."""

    def handler(request):
        return httpx.Response(429, text="slow down", headers={"Retry-After": "42"})

    async with _client(handler) as client:
        with pytest.raises(UsageHTTPError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    assert excinfo.value.retry_after == "42"


async def test_a_429_without_a_retry_after_header_carries_none():
    """Absent header -> None; the poller then falls back to plain backoff."""
    async with _client(lambda r: httpx.Response(429, text="slow down")) as client:
        with pytest.raises(UsageHTTPError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    assert excinfo.value.retry_after is None


@pytest.mark.parametrize("status", [400, 500, 503])
async def test_retry_after_is_ignored_on_non_429_statuses(status):
    """Scope: only a 429 means "wait this long". A Retry-After on any other status is
    not something we act on, so it is never captured."""

    def handler(request):
        return httpx.Response(status, text="no", headers={"Retry-After": "42"})

    async with _client(handler) as client:
        with pytest.raises(UsageHTTPError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    assert excinfo.value.retry_after is None


async def test_an_http_error_body_is_redacted():
    """It goes to the database like the others, so the same rules apply."""
    leaky = f'{{"error":"bad token {TOKEN}"}}'
    async with _client(lambda r: httpx.Response(500, text=leaky)) as client:
        with pytest.raises(UsageHTTPError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    assert TOKEN not in excinfo.value.body
    assert "sk-ant" not in excinfo.value.body
    assert REDACTED in excinfo.value.body


async def test_an_error_with_no_response_carries_no_body():
    """Nothing to archive when the request never completed."""

    def handler(request):
        raise httpx.ConnectError("connection refused")

    async with _client(handler) as client:
        with pytest.raises(UsageTransportError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    assert excinfo.value.body == ""


async def test_a_200_that_is_not_json_is_a_protocol_error():
    """A captive portal or an HTML error page must not read as usable data."""
    async with _client(lambda r: httpx.Response(200, text="<html>hello</html>")) as client:
        with pytest.raises(UsageProtocolError):
            await fetch_usage(TOKEN, client=client)


async def test_a_protocol_error_carries_the_body_for_the_archive():
    """The decode fails inside the client, so without this the poller has no
    payload to archive and the likeliest shape of a real endpoint change -- an
    HTML error page -- was the one the raw archive never captured."""
    page = "<html><body>Sign in to continue</body></html>"
    async with _client(lambda r: httpx.Response(200, text=page)) as client:
        with pytest.raises(UsageProtocolError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    assert "Sign in to continue" in excinfo.value.body


async def test_the_archived_body_is_redacted():
    """It is written to the database, so the token must not survive in it -- both
    the exact token we sent and anything else credential-shaped."""
    leaky = f"<html>token={TOKEN} other=sk-ant-oat01-somethingelse</html>"
    async with _client(lambda r: httpx.Response(200, text=leaky)) as client:
        with pytest.raises(UsageProtocolError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    body = excinfo.value.body
    assert TOKEN not in body
    assert "sk-ant" not in body
    assert REDACTED in body


async def test_the_archived_body_is_longer_than_the_error_excerpt():
    """200 characters of an HTML error page says nothing useful, and the archive
    exists to be read later; the banner excerpt stays short."""
    long_page = "<html>" + ("x" * 3000) + "</html>"
    async with _client(lambda r: httpx.Response(200, text=long_page)) as client:
        with pytest.raises(UsageProtocolError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    assert len(excinfo.value.body) > ERROR_BODY_LIMIT
    assert len(excinfo.value.body) <= ARCHIVE_BODY_LIMIT


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


async def test_a_credential_with_a_control_character_does_not_leak_into_the_error():
    """Regression: h11 rejects such a header and quotes the offending bytes back --
    "Illegal header value b'Bearer sk-ant-...'" -- and that text became `last_error`,
    which /api/now serves and the logger writes. A real socket is needed: the request
    has to reach h11, so MockTransport cannot exercise this."""
    import http.server
    import socketserver
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        token = f"{TOKEN}\ninjected: yes"
        async with httpx.AsyncClient(timeout=3.0) as client:
            with pytest.raises(UsageFetchError) as excinfo:
                await fetch_usage(token, client=client, url=f"http://127.0.0.1:{port}/x")
    finally:
        server.shutdown()

    message = str(excinfo.value)
    assert TOKEN not in message
    assert "sk-ant" not in message
    assert REDACTED in message


@pytest.mark.parametrize("status", [401, 403])
async def test_an_auth_error_records_which_status_it_was(status):
    """The poller's remediation advice depends on it: 401 means the token we hold is
    no longer good, which signing in fixes; 403 means it was understood and refused,
    which it does not."""
    async with _client(lambda r: httpx.Response(status, json={"error": "nope"})) as client:
        with pytest.raises(UsageAuthError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    assert excinfo.value.status_code == status


@pytest.mark.parametrize("status", [401, 403])
async def test_an_auth_error_carries_its_body_for_the_archive(status):
    """Regression: the auth branch raised with the default empty body, so the poller's
    archive step skipped it. A 403 usually explains itself -- which policy, which
    missing entitlement -- and that explanation is the most useful thing on the whole
    failure path, since unlike a 401 the user cannot fix it by signing in again."""
    denial = '{"error":{"type":"permission_error","message":"organization has disabled this"}}'
    async with _client(lambda r: httpx.Response(status, text=denial)) as client:
        with pytest.raises(UsageAuthError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    assert "permission_error" in excinfo.value.body
    assert "organization has disabled this" in excinfo.value.body


async def test_an_auth_error_body_is_redacted():
    leaky = f'{{"error":"rejected token {TOKEN}"}}'
    async with _client(lambda r: httpx.Response(403, text=leaky)) as client:
        with pytest.raises(UsageAuthError) as excinfo:
            await fetch_usage(TOKEN, client=client)

    assert TOKEN not in excinfo.value.body
    assert "sk-ant" not in excinfo.value.body
    assert REDACTED in excinfo.value.body
