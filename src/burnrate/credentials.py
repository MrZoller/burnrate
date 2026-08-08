"""Read Claude Code's stored OAuth credential.

Claude Code owns this credential and refreshes it on its own schedule. We are a
read-only consumer: every poll re-reads from scratch so we pick up a refresh the
moment it lands, and we never attempt a refresh ourselves. A 401 means "our view
is stale", never "renew the token".

The token must never be logged, persisted, or returned to a client. Errors
raised here carry only the source and the shape of the failure -- never the
value we were trying to read.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
OAUTH_KEY = "claudeAiOauth"

# `security` exits non-zero when the item is missing (44) or when reading the
# secret needs an interactive authorization the caller cannot satisfy (36).
_KEYCHAIN_TIMEOUT_SECONDS = 10


class CredentialError(RuntimeError):
    """No usable credential could be read."""


@dataclass(frozen=True)
class Credential:
    """An access token plus where it came from. Never log or serialize this."""

    access_token: str
    source: str
    expires_at: datetime | None = None

    @property
    def is_expired(self) -> bool:
        """True only when we positively know the token is past its expiry.

        An absent or unparseable expiry reads as "not known to be expired" --
        we let the API be the judge rather than refusing to try.
        """
        if self.expires_at is None:
            return False
        return self.expires_at <= datetime.now(UTC)


def read_credential() -> Credential:
    """Return the current credential, keychain first, file second.

    Raises CredentialError with both failure reasons if neither source yields a
    token.
    """
    problems: list[str] = []

    for source, reader in (("keychain", read_from_keychain), ("file", read_from_file)):
        try:
            credential = reader()
        except CredentialError as exc:
            problems.append(f"{source}: {exc}")
            continue
        if credential is not None:
            return credential
        problems.append(f"{source}: no credential found")

    raise CredentialError("; ".join(problems))


def read_from_keychain(service: str = KEYCHAIN_SERVICE) -> Credential | None:
    """Read the credential from the macOS login keychain.

    Returns None when the platform has no keychain or the item is absent.
    Raises CredentialError when the item exists but could not be read or parsed.

    The first read from a new process may require the user to authorize keychain
    access; when that authorization is unavailable `security` fails and we fall
    back to the file.
    """
    if os.uname().sysname != "Darwin":
        return None

    account = _current_account()
    # Try the account-scoped lookup first, then an unscoped one -- the account
    # attribute has not always been the local username.
    attempts = [["-a", account], []] if account else [[]]

    last_error: str | None = None
    for extra in attempts:
        argv = ["security", "find-generic-password", "-s", service, *extra, "-w"]
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=_KEYCHAIN_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired:
            last_error = "timed out"
            continue

        if result.returncode != 0 or not result.stdout.strip():
            last_error = _describe_security_failure(result.returncode)
            continue

        token, expires_at = parse_credentials_json(result.stdout)
        return Credential(access_token=token, source="keychain", expires_at=expires_at)

    if last_error:
        raise CredentialError(last_error)
    return None


def read_from_file(path: Path | None = None) -> Credential | None:
    """Read the credential from ~/.claude/.credentials.json.

    Returns None when the file does not exist. Raises CredentialError when it
    exists but cannot be read or parsed.
    """
    path = path or CREDENTIALS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CredentialError(f"unreadable ({exc.strerror})") from exc

    token, expires_at = parse_credentials_json(text)
    return Credential(access_token=token, source="file", expires_at=expires_at)


def parse_credentials_json(text: str) -> tuple[str, datetime | None]:
    """Pull the access token and optional expiry out of a credentials blob.

    Accepts either the documented `{"claudeAiOauth": {...}}` wrapper or a bare
    object holding the token, so a shape change in the wrapper does not lock us
    out. Raises CredentialError if no non-empty token is present.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CredentialError(f"malformed JSON at line {exc.lineno}") from exc

    if not isinstance(payload, dict):
        raise CredentialError(f"expected a JSON object, got {type(payload).__name__}")

    oauth = payload.get(OAUTH_KEY)
    if not isinstance(oauth, dict):
        # Tolerate the wrapper going away, or a differently named one.
        oauth = payload if "accessToken" in payload else _first_dict_with_token(payload)
    if oauth is None:
        raise CredentialError(f"no {OAUTH_KEY}.accessToken field")

    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        raise CredentialError("accessToken missing or empty")

    return token.strip(), _parse_expiry(oauth.get("expiresAt"))


def _first_dict_with_token(payload: dict[str, object]) -> dict[str, object] | None:
    for value in payload.values():
        if isinstance(value, dict) and "accessToken" in value:
            return value
    return None


def _parse_expiry(raw: object) -> datetime | None:
    """Interpret an expiry that may be epoch millis, epoch seconds, or ISO-8601.

    Returns None on anything unrecognized -- the expiry is advisory, so an
    unparseable one must not stop us from using a token that may well work.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int | float):
        # Values this large are milliseconds; Claude Code writes millis today.
        seconds = raw / 1000 if raw > 1e11 else float(raw)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _current_account() -> str | None:
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return None


def _describe_security_failure(returncode: int) -> str:
    if returncode == 44:
        return "item not found"
    if returncode == 36:
        return "keychain read needs interactive authorization (rc 36)"
    return f"security exited {returncode}"
