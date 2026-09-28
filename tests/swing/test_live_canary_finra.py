"""OPT-IN live canary for FINRA short interest (one real POST). Never in CI (marker `live`):

    COUNCIL_LIVE_CANARY=1 uv run pytest -m live tests/swing/test_live_canary_finra.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from council.data import finra

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("COUNCIL_LIVE_CANARY") != "1", reason="opt-in: set COUNCIL_LIVE_CANARY=1"),
]


def test_finra_short_interest_canary():
    out = finra.fetch_short_interest(["AAPL", "NVDA"], asof=datetime.now(UTC))
    assert out, "no rows: the dataset name or the filter shape changed"
    for si in out.values():
        assert si.short_shares > 0
