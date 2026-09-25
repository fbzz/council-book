"""Risk-area fixtures. Helpers live in tests/risk/helpers.py."""

from __future__ import annotations

from datetime import datetime

import pytest

from tests.risk.helpers import NOW


@pytest.fixture
def now() -> datetime:
    return NOW
