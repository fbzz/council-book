from __future__ import annotations

from datetime import date

import httpx
import pandas as pd
import pytest

from council.data.http import DataError
from council.data.tiingo import adjusted_row, fetch_daily, parse_daily
from tests.data.synth import fixture, utc

NOW = utc(2026, 10, 1, 14, 40)       # 10:40 New York: Oct 1 bar not yet published


def test_adjustment_uses_adjclose_ratio_for_every_field():
    row = {"date": "2026-09-30T00:00:00.000Z", "open": 99.0, "high": 104.0, "low": 96.0,
           "close": 100.0, "volume": 10.0, "adjClose": 50.0, "adjVolume": 20.0}
    start, o, h, low, c, v = adjusted_row(row)
    assert start == pd.Timestamp("2026-09-30", tz="UTC")
    assert (o, h, low, c, v) == (49.5, 52.0, 48.0, 50.0, 20.0)
    no_adj = adjusted_row({**row, "adjClose": None, "adjVolume": None})
    assert no_adj[4] == 100.0 and no_adj[5] == 10.0
    assert adjusted_row({**row, "close": None}) is None


def test_parse_applies_availability_rule():
    payload = fixture("tiingo_qqq_daily.json")
    bars = parse_daily(payload, NOW)
    assert bars.index[-1] == pd.Timestamp("2026-09-30", tz="UTC")       # Oct 1 dropped
    assert bars["close"].iloc[0] == pytest.approx(480.0 * 0.99)
    after_publish = parse_daily(payload, utc(2026, 10, 2, 0, 0))        # Oct 1 20:00 EDT
    assert after_publish.index[-1] == pd.Timestamp("2026-10-01", tz="UTC")
    assert after_publish["close"].iloc[-1] == 999.0
    one_minute_early = parse_daily(payload, utc(2026, 10, 1, 23, 59))
    assert one_minute_early.index[-1] == pd.Timestamp("2026-09-30", tz="UTC")


def test_fetch_puts_token_in_header_not_url(mock_client):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=fixture("tiingo_qqq_daily.json"))

    bars = fetch_daily("QQQ", date(2026, 9, 1), token="t0k3n", client=mock_client(handler), now=NOW)
    assert len(bars) == 3
    assert "t0k3n" not in seen["url"] and seen["auth"] == "Token t0k3n"
    assert "/tiingo/daily/qqq/prices" in seen["url"] and "startDate=2026-09-01" in seen["url"]


def test_error_payloads_and_missing_token(mock_client):
    with pytest.raises(DataError):
        parse_daily({"detail": "Error: Ticker 'ZZZZ' not found"}, NOW)
    with pytest.raises(DataError) as info:
        fetch_daily("QQQ", "2026-09-01", token="t0k3n", client=mock_client(lambda r: httpx.Response(401)), now=NOW)
    assert "t0k3n" not in str(info.value)
    with pytest.raises(DataError):
        fetch_daily("QQQ", "2026-09-01", token="", now=NOW)
    with pytest.raises(ValueError):
        fetch_daily("QQQ/../x", "2026-09-01", token="t", now=NOW)
