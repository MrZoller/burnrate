"""Credential scrubbing.

The project rule is that the OAuth token is never logged, never written to the
database, and never in an API response. These are the unit tests for the backstop
that makes the last two hold even when an upstream response hands the token back
to us in a field we have never seen.
"""

import pytest

from burnrate.redact import REDACTED, scrub, scrub_json

TOKEN = "sk-ant-oat01-example-token-value"


def test_the_exact_token_is_removed():
    assert TOKEN not in scrub(f"error: bad token {TOKEN}", TOKEN)


def test_a_credential_we_never_held_is_still_removed():
    """The pattern matters on its own: the store scrubs without knowing the token,
    and a response could echo a different credential than the one we sent."""
    other = "sk-ant-oat01-someone-elses-token"

    assert other not in scrub(f"echo: {other}")


@pytest.mark.parametrize(
    "text",
    [
        "plain text",
        "",
        "an api_key field with no value",
        "sk-something-else-entirely",
    ],
)
def test_innocent_text_is_left_alone(text):
    assert scrub(text) == text


def test_scrub_json_walks_values():
    payload = {"limits": [{"note": f"token {TOKEN}"}], "ok": True}

    scrubbed = scrub_json(payload, TOKEN)

    assert TOKEN not in str(scrubbed)
    assert scrubbed["ok"] is True
    assert REDACTED in scrubbed["limits"][0]["note"]


def test_scrub_json_walks_keys():
    """A token used as a key persists just as well as one used as a value."""
    scrubbed = scrub_json({TOKEN: "value"}, TOKEN)

    assert TOKEN not in str(scrubbed)
    assert scrubbed == {REDACTED: "value"}


def test_scrub_json_preserves_non_string_types():
    """The archive has to stay valid, comparable JSON, so nothing may change shape."""
    payload = {"a": 1, "b": 2.5, "c": None, "d": False, "e": [1, "x", None]}

    assert scrub_json(payload) == payload


def test_scrub_json_handles_a_bare_value():
    assert scrub_json(f"body {TOKEN}", TOKEN) == f"body {REDACTED}"
    assert scrub_json(42) == 42
