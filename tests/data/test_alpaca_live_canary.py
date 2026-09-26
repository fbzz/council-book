"""OPT-IN live canary for the stock-history source: one real Alpaca market-data request (daily bars
for two large US stocks, one of them a class share). Never in CI (marker `live`) and skipped unless
COUNCIL_LIVE_CANARY=1 and the keys are configured. The keys come from the Keychain items
`council-book.alpaca-key-id` / `council-book.alpaca-secret` (account `council`) only (the canary runs
in dry_run mode, where the environment overrides are ignored); they are never printed:

    COUNCIL_LIVE_CANARY=1 uv run pytest -m live tests/data/test_alpaca_live_canary.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from council.data import alpaca
from council.facts.market import expected_day

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("COUNCIL_LIVE_CANARY") != "1", reason="opt-in: set COUNCIL_LIVE_CANARY=1"),
]


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("COUNCIL_MODE", "dry_run")         # lets the adapter read the Keychain items
    found = alpaca.load_keys()
    if found is None:
        pytest.skip("no Alpaca keys configured")
    return found


def test_alpaca_daily_bars_canary(keys):
    now = datetime.now(UTC)
    start = (now - timedelta(days=45)).date()
    out = alpaca.fetch_daily(["AAPL", "BRK-B"], start, keys=keys, now=now, retries=1)
    assert set(out) == {"AAPL", "BRK-B"}
    for ticker, bars in out.items():
        assert len(bars) >= 20, ticker
        idx = pd.DatetimeIndex(bars.index)
        assert (idx == idx.normalize()).all() and (idx.dayofweek < 5).all()   # trading dates at 00:00 UTC
        assert (bars["close"] > 0).all()
        newest = idx[-1].date()
        assert newest <= expected_day("alpaca", now)                          # never a partial day
        assert (expected_day("alpaca", now) - newest).days <= 5
