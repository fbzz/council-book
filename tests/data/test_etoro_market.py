from __future__ import annotations

import pandas as pd
import pytest

from council.data.etoro_market import EtoroMarketData, parse_candles, parse_rates
from council.data.http import DataError
from tests.data.synth import fixture, utc

NOW = utc(2026, 10, 1, 14, 40)


class FakeRead:
    """Stands in for the broker READ client's get_json(path, params)."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, path, params):
        self.calls.append((path, params))
        return self.payload


def test_candles_path_order_and_completed_only():
    read = FakeRead(fixture("etoro_candles_oneday.json"))
    bars = EtoroMarketData(read).candles(12, "OneDay", 4, now=NOW)
    assert read.calls == [("/api/v1/market-data/instruments/12/history/candles/desc/OneDay/4", None)]
    assert list(bars.index) == [pd.Timestamp(f"2026-09-{d}", tz="UTC") for d in (28, 29, 30)]
    assert bars.index.is_monotonic_increasing                  # desc payload re-sorted


def test_candle_completion_boundary():
    payload = fixture("etoro_candles_oneday.json")
    assert len(parse_candles(payload, "OneDay", utc(2026, 10, 2, 0, 0))) == 4
    assert len(parse_candles(payload, "OneDay", utc(2026, 10, 1, 23, 59))) == 3


@pytest.mark.parametrize(
    ("interval", "count"), [("OneWeek", 10), ("OneDay", 0), ("OneDay", 1001)]
)
def test_candle_arguments_validated(interval, count):
    with pytest.raises(ValueError):
        EtoroMarketData(FakeRead({})).candles(12, interval, count, now=NOW)


def test_malformed_candles_raise():
    with pytest.raises(DataError):
        parse_candles({"interval": "OneDay"}, "OneDay", NOW)
    with pytest.raises(DataError):
        parse_candles({"candles": [{"candles": [{"fromDate": "2026-09-28T00:00:00Z"}]}]}, "OneDay", NOW)


def test_rates_parse_key_variants_and_drop_invalid_quotes():
    read = FakeRead(fixture("etoro_rates.json"))
    quotes = EtoroMarketData(read).rates([12, 27, 100000, 55, 56, 999], symbols={12: "EURUSD"}, now=NOW)
    assert read.calls == [("/api/v2/market-data/rates", {"instrumentIds": "12,27,55,56,999,100000"})]
    assert set(quotes) == {12, 27, 100000}                       # 55 crossed, 56 zero bid, 999 absent
    assert quotes[12].symbol == "EURUSD" and quotes[12].mid == pytest.approx(1.1701)
    assert quotes[27].at == utc(2026, 10, 1, 14, 39, 59)
    assert quotes[100000].at == NOW and quotes[100000].symbol == "100000"   # no timestamp -> fetch time


def test_rates_batches_and_filters_unrequested():
    read = FakeRead({"rates": [{"instrumentID": 12, "bid": 1.0, "ask": 1.1}]})
    md = EtoroMarketData(read)
    assert md.rates([], now=NOW) == {}
    md.rates(range(1, 121), now=NOW)
    assert len(read.calls) == 3                                  # 50 + 50 + 20
    assert parse_rates({"rates": [{"instrumentID": 12, "bid": 1, "ask": 2}]}, wanted=[13], now=NOW) == {}
    with pytest.raises(DataError):
        parse_rates({"unexpected": True}, now=NOW)
