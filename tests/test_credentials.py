"""Credential retrieval: keychain first, file fallback, and the parsing of both."""

import json
import subprocess
from datetime import UTC, datetime

import pytest

from burnrate import credentials
from burnrate.credentials import (
    CredentialError,
    parse_credentials_json,
    read_credential,
    read_from_file,
    read_from_keychain,
)

TOKEN = "sk-ant-oat01-example-token-value"


def _blob(token=TOKEN, **extra):
    return json.dumps({"claudeAiOauth": {"accessToken": token, **extra}})


class _Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


# ------------------------------------------------------------------ parsing


def test_parses_the_documented_shape():
    token, expires_at = parse_credentials_json(_blob(expiresAt=1_786_000_000_000))

    assert token == TOKEN
    assert expires_at is not None and expires_at.tzinfo is not None


def test_parses_epoch_millis_and_seconds_alike():
    _, millis = parse_credentials_json(_blob(expiresAt=1_786_000_000_000))
    _, seconds = parse_credentials_json(_blob(expiresAt=1_786_000_000))

    assert millis == seconds


def test_parses_an_iso_expiry():
    _, expires_at = parse_credentials_json(_blob(expiresAt="2026-08-09T12:00:00Z"))

    assert expires_at == datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("expiry", [None, "not-a-date", [], {}, True])
def test_unusable_expiry_is_dropped_rather_than_fatal(expiry):
    token, expires_at = parse_credentials_json(_blob(expiresAt=expiry))

    assert token == TOKEN
    assert expires_at is None


def test_tolerates_the_wrapper_key_going_away():
    token, _ = parse_credentials_json(json.dumps({"accessToken": TOKEN}))

    assert token == TOKEN


def test_tolerates_the_wrapper_being_renamed():
    token, _ = parse_credentials_json(json.dumps({"someNewOauthKey": {"accessToken": TOKEN}}))

    assert token == TOKEN


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{",
        "[]",
        '"a string"',
        "{}",
        '{"claudeAiOauth": {}}',
        '{"claudeAiOauth": {"accessToken": ""}}',
        '{"claudeAiOauth": {"accessToken": "   "}}',
        '{"claudeAiOauth": {"accessToken": 12345}}',
        '{"claudeAiOauth": null}',
    ],
)
def test_malformed_credentials_raise_credential_error(text):
    with pytest.raises(CredentialError):
        parse_credentials_json(text)


def test_error_messages_never_leak_the_token():
    blob = json.dumps({"claudeAiOauth": {"accessToken": ""}, "other": TOKEN})

    with pytest.raises(CredentialError) as excinfo:
        parse_credentials_json(blob)

    assert TOKEN not in str(excinfo.value)


def test_whitespace_is_stripped_from_the_token():
    token, _ = parse_credentials_json(_blob(token=f"  {TOKEN}\n"))

    assert token == TOKEN


# --------------------------------------------------------------------- file


def test_reads_from_the_file_fallback(tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text(_blob())

    credential = read_from_file(path)

    assert credential.access_token == TOKEN
    assert credential.source == "file"


def test_missing_file_is_absence_not_failure(tmp_path):
    assert read_from_file(tmp_path / "nope.json") is None


def test_corrupt_file_is_a_failure_not_silence(tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text("{ truncated")

    with pytest.raises(CredentialError):
        read_from_file(path)


# ----------------------------------------------------------------- keychain


def test_reads_from_the_keychain(monkeypatch):
    monkeypatch.setattr(credentials.os, "uname", lambda: _uname("Darwin"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result(0, _blob()))

    credential = read_from_keychain()

    assert credential.access_token == TOKEN
    assert credential.source == "keychain"


def test_keychain_is_queried_with_the_verified_service_name(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _Result(0, _blob())

    monkeypatch.setattr(credentials.os, "uname", lambda: _uname("Darwin"))
    monkeypatch.setattr(subprocess, "run", fake_run)

    read_from_keychain()

    assert "Claude Code-credentials" in seen["argv"]
    assert seen["argv"][:2] == ["security", "find-generic-password"]


def test_keychain_retries_without_the_account_scope(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "-a" in argv:
            return _Result(44, "")  # item not found under that account
        return _Result(0, _blob())

    monkeypatch.setattr(credentials.os, "uname", lambda: _uname("Darwin"))
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert read_from_keychain().access_token == TOKEN
    assert len(calls) == 2


def test_keychain_needing_authorization_raises_and_explains(monkeypatch):
    # rc 36 is what this machine actually returns: the item exists but reading
    # the secret needs an interactive grant the service cannot give.
    monkeypatch.setattr(credentials.os, "uname", lambda: _uname("Darwin"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result(36, ""))

    with pytest.raises(CredentialError, match="interactive authorization"):
        read_from_keychain()


def test_keychain_skipped_off_darwin(monkeypatch):
    monkeypatch.setattr(credentials.os, "uname", lambda: _uname("Linux"))

    assert read_from_keychain() is None


def test_missing_security_binary_is_absence_not_failure(monkeypatch):
    monkeypatch.setattr(credentials.os, "uname", lambda: _uname("Darwin"))
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    )

    assert read_from_keychain() is None


def test_keychain_timeout_is_reported(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="security", timeout=10)

    monkeypatch.setattr(credentials.os, "uname", lambda: _uname("Darwin"))
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CredentialError, match="timed out"):
        read_from_keychain()


# ------------------------------------------------------------- the fallback


def test_falls_back_to_the_file_when_the_keychain_cannot_be_read(monkeypatch, tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text(_blob())

    monkeypatch.setattr(credentials, "read_from_keychain", lambda: None)
    monkeypatch.setattr(credentials, "CREDENTIALS_FILE", path)

    assert read_credential().source == "file"


def test_keychain_wins_when_both_sources_work(monkeypatch, tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text(_blob(token="file-token"))

    monkeypatch.setattr(
        credentials,
        "read_from_keychain",
        lambda: credentials.Credential(access_token=TOKEN, source="keychain"),
    )
    monkeypatch.setattr(credentials, "CREDENTIALS_FILE", path)

    credential = read_credential()

    assert credential.source == "keychain"
    assert credential.access_token == TOKEN


def test_a_broken_keychain_still_falls_through_to_the_file(monkeypatch, tmp_path):
    """rc 36 on this machine must not stop the service from working."""
    path = tmp_path / ".credentials.json"
    path.write_text(_blob())

    def boom():
        raise CredentialError("keychain read needs interactive authorization (rc 36)")

    monkeypatch.setattr(credentials, "read_from_keychain", boom)
    monkeypatch.setattr(credentials, "CREDENTIALS_FILE", path)

    assert read_credential().source == "file"


def test_both_sources_failing_reports_both_reasons(monkeypatch, tmp_path):
    def boom():
        raise CredentialError("keychain exploded")

    monkeypatch.setattr(credentials, "read_from_keychain", boom)
    monkeypatch.setattr(credentials, "CREDENTIALS_FILE", tmp_path / "absent.json")

    with pytest.raises(CredentialError) as excinfo:
        read_credential()

    assert "keychain exploded" in str(excinfo.value)
    assert "file" in str(excinfo.value)


def test_repr_never_exposes_the_token():
    """A frozen dataclass prints every field by default, so any f-string,
    logger call, or traceback rendering locals would leak the secret."""
    credential = credentials.Credential(TOKEN, "file")

    assert TOKEN not in repr(credential)
    assert TOKEN not in f"{credential}"
    assert TOKEN not in str(RuntimeError(f"failed: {credential}"))
    # The source is still diagnosable, and the token is still readable.
    assert "file" in repr(credential)
    assert credential.access_token == TOKEN


def test_expiry_is_advisory_only(monkeypatch):
    past = credentials.Credential(TOKEN, "file", datetime(2000, 1, 1, tzinfo=UTC))
    unknown = credentials.Credential(TOKEN, "file", None)

    assert past.is_expired is True
    # No expiry means "let the API decide" -- we never refuse to try.
    assert unknown.is_expired is False


def _uname(sysname):
    class _U:
        pass

    u = _U()
    u.sysname = sysname
    return u
