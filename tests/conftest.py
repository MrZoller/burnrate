import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# The instant `live_response.json` was recorded. The payload carries no capture
# time of its own, but the suite already declares one: `NOW` in
# test_store_and_api.py and test_projection.py, which those tests pass straight
# to `parse_usage(live_response, fetched_at=NOW)`. It also sits inside the bound
# the capture implies -- the five-hour window it was taken in ends at
# 2026-08-08T23:30Z, so the capture fell between 18:30Z and 23:30Z.
CAPTURED_AT = datetime(2026, 8, 8, 21, 45, tzinfo=UTC)


@pytest.fixture
def live_response() -> dict:
    """The real response captured from the endpoint on 2026-08-08, verbatim."""
    return json.loads((FIXTURES / "live_response.json").read_text())


@pytest.fixture
def live_response_at(live_response: dict) -> Callable[[datetime], dict]:
    """The captured response, rebased so its windows are live at a given instant.

    Every `resets_at` in the capture is now in the past, which takes the
    projection to "unavailable" -- so tests exercising projection or staleness
    would fail by calendar rather than by regression. Shifting the whole payload
    by a single delta moves the windows onto the current clock while keeping
    each bucket's position *within* its window exactly as captured, leaving the
    projection maths unchanged. Tests that assert on the verbatim capture
    (parsing fidelity) keep using `live_response`.
    """

    def _at(fetched_at: datetime) -> dict:
        delta = fetched_at - CAPTURED_AT
        rebased = deepcopy(live_response)

        def shift(entry: object) -> None:
            if isinstance(entry, dict) and isinstance(entry.get("resets_at"), str):
                moved = datetime.fromisoformat(entry["resets_at"]) + delta
                entry["resets_at"] = moved.isoformat()

        for value in rebased.values():
            shift(value)
        for limit in rebased.get("limits") or ():
            shift(limit)

        return rebased

    return _at
