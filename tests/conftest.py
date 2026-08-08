import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def live_response() -> dict:
    """The real response captured from the endpoint on 2026-08-08."""
    return json.loads((FIXTURES / "live_response.json").read_text())
