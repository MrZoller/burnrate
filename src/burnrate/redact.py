"""Credential scrubbing, in one place.

"The OAuth token is never logged, never written to the database, and never in an
API response" is a rule about several unrelated code paths -- an HTTP error
excerpt, an archived response body -- so the rule lives here rather than being
restated at each of them. One definition, one set of tests, and a single place to
change when the credential format does.

Scrubbing is a backstop, not the primary defence. Nothing here deliberately
handles the token; this exists for the case where an upstream response contains
it because a future field echoes it back, which is exactly the kind of surprise
the raw archive is designed to capture and would otherwise persist verbatim.
"""

from __future__ import annotations

import re
from typing import Any

# Anything shaped like an Anthropic credential, redacted even though we did not
# put it there. Deliberately broad on the tail: the point is to over-match a
# secret, not to parse one.
SECRET_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
REDACTED = "<redacted>"


def scrub(text: str, secret: str = "") -> str:
    """Remove `secret` and anything credential-shaped from `text`.

    `secret` is the exact token we sent, when the caller happens to know it --
    that catches an echo in a format the pattern would miss. The pattern catches
    the rest, including a token we never held.
    """
    if secret:
        text = text.replace(secret, REDACTED)
    return SECRET_PATTERN.sub(REDACTED, text)


def scrub_json(payload: Any, secret: str = "") -> Any:
    """Scrub every string in a decoded JSON structure, keys included.

    Applied to whole payloads rather than to their serialization so the archive
    keeps storing valid JSON. Keys are scrubbed too: a response that used a token
    as a dictionary key would otherwise persist it just as effectively as a value.
    """
    if isinstance(payload, str):
        return scrub(payload, secret)
    if isinstance(payload, dict):
        return {scrub_json(k, secret): scrub_json(v, secret) for k, v in payload.items()}
    if isinstance(payload, list):
        return [scrub_json(item, secret) for item in payload]
    return payload
