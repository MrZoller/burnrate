"""HTTP client for the unofficial usage endpoint.

Errors are typed by what the caller should do about them: re-read the credential
(auth), back off and retry (transport, server), or surface loudly without retry
churn (protocol). Nothing here logs or embeds the token.
"""

from __future__ import annotations

from typing import Any

import httpx

from .redact import REDACTED, scrub

__all__ = [
    "ARCHIVE_BODY_LIMIT",
    "ERROR_BODY_LIMIT",
    "OAUTH_BETA",
    "REDACTED",
    "USAGE_URL",
    "UsageAuthError",
    "UsageFetchError",
    "UsageHTTPError",
    "UsageProtocolError",
    "UsageTransportError",
    "build_headers",
    "fetch_usage",
]

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "burnrate/0.1 (+https://github.com/MrZoller/burnrate)"
REQUEST_TIMEOUT_SECONDS = 20.0

# Body excerpt lengths. The short one is for an error message a human reads in a
# banner; the longer one is for the archived copy of a body that broke the
# parser, where 200 characters of an HTML error page says nothing useful.
ERROR_BODY_LIMIT = 200
ARCHIVE_BODY_LIMIT = 4000


class UsageFetchError(RuntimeError):
    """Base class for a failed usage fetch.

    `body` is a redacted archival excerpt of the response, when there was a response
    worth keeping. The poller archives it on any subclass that carries one, so a new
    error type gets the behaviour by setting the field rather than by someone
    remembering to add a branch. Redacted because it is written to the database.
    """

    def __init__(self, *args: object, body: str = "") -> None:
        self.body = body
        super().__init__(*args)


class UsageAuthError(UsageFetchError):
    """401/403 -- our copy of the credential is stale or rejected.

    Carries the status because the two mean different things and want different
    advice: 401 says the token we hold is no longer good, which signing in again
    fixes; 403 says the credential was understood and refused, which it does not.
    Collapsing them let a permission denial be reported as an expired token.
    """

    def __init__(self, *args: object, status_code: int | None = None, body: str = "") -> None:
        self.status_code = status_code
        super().__init__(*args, body=body)


class UsageHTTPError(UsageFetchError):
    """Non-2xx that is not an auth problem.

    Carries its body for the archive. A 429 or a 5xx is the response most worth
    having later -- it is the one that explains why the dashboard went quiet -- and
    only the unreadable-2xx path was keeping one, so precisely the rate-limit and
    server-error bodies were the ones being dropped.
    """

    def __init__(self, status_code: int, detail: str = "", body: str = "") -> None:
        self.status_code = status_code
        suffix = f": {detail}" if detail else ""
        super().__init__(f"HTTP {status_code}{suffix}", body=body)


class UsageTransportError(UsageFetchError):
    """The request never completed -- DNS, TLS, timeout, connection reset."""


class UsageProtocolError(UsageFetchError):
    """A 2xx whose body was not JSON.

    The likeliest shape a real endpoint change takes -- an HTML error page, a login
    redirect, a truncated response -- and exactly the body the raw archive exists to
    preserve, yet it never reached the store: the decode fails here, so the poller
    only ever saw the exception.
    """


def build_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": OAUTH_BETA,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


async def fetch_usage(
    access_token: str,
    client: httpx.AsyncClient | None = None,
    url: str = USAGE_URL,
) -> Any:
    """GET the usage payload. Raises a UsageFetchError subclass on any failure."""
    owned = client is None
    client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        try:
            response = await client.get(url, headers=build_headers(access_token))
        except httpx.HTTPError as exc:
            # Scrubbed with the token in hand. A credential containing a control
            # character makes h11 reject the header and quote the offending bytes
            # back -- "Illegal header value b'Bearer sk-ant-...'" -- and this text
            # becomes `last_error`, which /api/now serves and the logger writes.
            raise UsageTransportError(scrub(f"{type(exc).__name__}: {exc}", access_token)) from exc

        if response.status_code in (401, 403):
            # Body carried like every other error's. A 403 usually explains itself --
            # which organisation policy, which missing entitlement -- and that
            # explanation is the most useful thing on the whole failure path, since
            # unlike a 401 the user cannot fix it by signing in again.
            raise UsageAuthError(
                f"HTTP {response.status_code}",
                status_code=response.status_code,
                body=_short_body(response, access_token, limit=ARCHIVE_BODY_LIMIT),
            )
        if response.status_code >= 400:
            raise UsageHTTPError(
                response.status_code,
                _short_body(response, access_token),
                body=_short_body(response, access_token, limit=ARCHIVE_BODY_LIMIT),
            )

        try:
            return response.json()
        except ValueError as exc:
            raise UsageProtocolError(
                scrub(f"response was not JSON: {exc}", access_token),
                body=_short_body(response, access_token, limit=ARCHIVE_BODY_LIMIT),
            ) from exc
    finally:
        if owned:
            await client.aclose()


def _short_body(response: httpx.Response, secret: str = "", limit: int = ERROR_BODY_LIMIT) -> str:
    """A trimmed body excerpt for diagnostics, with credentials stripped.

    This text ends up in PollerStatus.last_error, which /api/now serves to the
    browser and the logger writes to disk -- and, at the archive limit, in the
    database. An upstream that echoes the token back in an error body would
    otherwise leak it into all three, so the excerpt is scrubbed of the token we
    sent and of anything else credential-shaped. Every caller goes through here
    for exactly that reason; nothing takes `response.text` directly.
    """
    try:
        excerpt = response.text[:limit].replace("\n", " ").strip()
    except Exception:  # pragma: no cover - defensive
        return ""
    return scrub(excerpt, secret)
