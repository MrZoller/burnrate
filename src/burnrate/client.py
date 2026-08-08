"""HTTP client for the unofficial usage endpoint.

Errors are typed by what the caller should do about them: re-read the credential
(auth), back off and retry (transport, server), or surface loudly without retry
churn (protocol). Nothing here logs or embeds the token.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

# Anything shaped like an Anthropic credential, redacted from diagnostics even
# when we did not put it there.
_SECRET_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
REDACTED = "<redacted>"

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
    """Base class for a failed usage fetch."""


class UsageAuthError(UsageFetchError):
    """401/403 -- our copy of the credential is stale or rejected."""


class UsageHTTPError(UsageFetchError):
    """Non-2xx that is not an auth problem."""

    def __init__(self, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        suffix = f": {detail}" if detail else ""
        super().__init__(f"HTTP {status_code}{suffix}")


class UsageTransportError(UsageFetchError):
    """The request never completed -- DNS, TLS, timeout, connection reset."""


class UsageProtocolError(UsageFetchError):
    """A 2xx whose body was not JSON.

    Carries the redacted body so the poller can archive it. This is the likeliest
    shape a real endpoint change takes -- an HTML error page, a login redirect, a
    truncated response -- and it is exactly the body the raw archive exists to
    preserve, yet it never reached the store: the decode fails here, so the poller
    only ever saw the exception. `body` is scrubbed by the same rules as the error
    message, because it is written to the database.
    """

    def __init__(self, message: str, body: str = "") -> None:
        self.body = body
        super().__init__(message)


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
            raise UsageTransportError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code in (401, 403):
            raise UsageAuthError(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise UsageHTTPError(response.status_code, _short_body(response, access_token))

        try:
            return response.json()
        except ValueError as exc:
            raise UsageProtocolError(
                f"response was not JSON: {exc}",
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
    if secret:
        excerpt = excerpt.replace(secret, REDACTED)
    return _SECRET_PATTERN.sub(REDACTED, excerpt)
