"""HTTP client for the unofficial usage endpoint.

Errors are typed by what the caller should do about them: re-read the credential
(auth), back off and retry (transport, server), or surface loudly without retry
churn (protocol). Nothing here logs or embeds the token.
"""

from __future__ import annotations

from typing import Any

import httpx

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "burnrate/0.1 (+https://github.com/MrZoller/burnrate)"
REQUEST_TIMEOUT_SECONDS = 20.0


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
    """A 2xx whose body was not JSON."""


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
            raise UsageHTTPError(response.status_code, _short_body(response))

        try:
            return response.json()
        except ValueError as exc:
            raise UsageProtocolError(f"response was not JSON: {exc}") from exc
    finally:
        if owned:
            await client.aclose()


def _short_body(response: httpx.Response) -> str:
    """A trimmed body excerpt for diagnostics. Never includes request headers."""
    try:
        return response.text[:200].replace("\n", " ").strip()
    except Exception:  # pragma: no cover - defensive
        return ""
