from __future__ import annotations

import httpx
import pandas as pd
import pytest

from council.data.binance import KLINES_URL, fetch_klines, parse_klines
from council.data.http import DataError
from tests.data.synth import fixture, utc

NOW = utc(2026, 10, 1, 14, 40)


def test_parse_real_shaped_payload_drops_in_progress_bar():
    bars = parse_klines(fixture("binance_klines_btcusdt_1d.json"), "1d", NOW)
    assert list(bars.index) == [pd.Timestamp(f"2026-09-{d}", tz="UTC") for d in (28, 29, 30)]
    assert bars.loc["2026-09-30", "close"].item() == 62750.0          # string prices parsed
    assert bars["high"].max() < 99999.0                                # in-progress spike never seen


def test_bar_becomes_usable_exactly_at_its_close():
    payload = fixture("binance_klines_btcusdt_1d.json")
    assert len(parse_klines(payload, "1d", utc(2026, 10, 1, 0, 0))) == 3
    assert len(parse_klines(payload, "1d", utc(2026, 9, 30, 23, 59))) == 2


def test_fetch_sends_symbol_interval_limit(mock_client):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url).split("?")[0]
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=fixture("binance_klines_btcusdt_1d.json"))

    bars = fetch_klines("BTCUSDT", "1d", 1000, client=mock_client(handler), now=NOW)
    assert seen["url"] == KLINES_URL
    assert seen["params"] == {"symbol": "BTCUSDT", "interval": "1d", "limit": "1000"}
    assert len(bars) == 3


def test_fetch_retries_on_418_is_refused_but_503_is_retried(mock_client):
    attempts = []

    def flaky(request):
        attempts.append(1)
        return httpx.Response(503) if len(attempts) == 1 else httpx.Response(200, json=[])

    assert fetch_klines("ETHUSDT", "4h", 10, client=mock_client(flaky), now=NOW).empty
    assert len(attempts) == 2
    with pytest.raises(DataError):
        fetch_klines("ETHUSDT", client=mock_client(lambda r: httpx.Response(418)), now=NOW)


@pytest.mark.parametrize(
    "payload", [{"code": -1121, "msg": "Invalid symbol."}, [[1, "x"]], [[1, "a", "1", "1", "1", "1"]]]
)
def test_malformed_payloads_raise(payload):
    with pytest.raises(DataError):
        parse_klines(payload, "1d", NOW)


@pytest.mark.parametrize(
    ("ticker", "interval", "limit"), [("btc/usdt", "1d", 10), ("BTCUSDT", "1w", 10), ("BTCUSDT", "1d", 1001)]
)
def test_bad_arguments_rejected(ticker, interval, limit):
    with pytest.raises(ValueError):
        fetch_klines(ticker, interval, limit, now=NOW)
