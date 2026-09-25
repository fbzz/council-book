from __future__ import annotations

import httpx
import pandas as pd

from council.data.cache import FileCache
from council.facts.market import gather_history, gather_macro
from tests.data.synth import binance_payload, daily_bars, fixture, tiingo_payload, utc

NOW = utc(2026, 10, 1, 14, 40)


def _router(calls, *, fail_hosts=()):
    def handler(request: httpx.Request):
        host = request.url.host
        calls.append((host, request.url.path))
        if host in fail_hosts:
            return httpx.Response(500)
        if host == "api.tiingo.com":
            return httpx.Response(200, json=tiingo_payload(daily_bars("2026-10-01", 300, seed=3, weekdays_only=True)))
        if host == "data-api.binance.vision":
            return httpx.Response(200, json=binance_payload(daily_bars("2026-10-01", 300, seed=4, weekdays_only=False)))
        if host == "fred.stlouisfed.org":
            return httpx.Response(200, text=fixture("fred_dgs10.csv").replace("DGS10", request.url.params["id"]))
        return httpx.Response(404)

    return handler


def test_gather_history_uses_signal_sources_and_availability(policy, mock_client):
    calls = []
    history, flags = gather_history(policy, now=NOW, tiingo_token="tok", client=mock_client(_router(calls)))
    assert flags == [] and set(history) == set(policy.universe.symbols())
    assert history["NDX"].index[-1] == pd.Timestamp("2026-09-30", tz="UTC")     # Oct 1 not published
    assert history["BTC"].index[-1] == pd.Timestamp("2026-09-30", tz="UTC")     # Oct 1 in progress
    paths = sorted(p for _, p in calls)
    assert "/tiingo/daily/qqq/prices" in paths and "/api/v3/klines" in paths
    assert len(calls) == 9


def test_gather_history_cache_is_per_slot(policy, mock_client):
    calls = []
    client = mock_client(_router(calls))
    gather_history(policy, now=NOW, tiingo_token="tok", client=client)
    gather_history(policy, now=utc(2026, 10, 1, 15, 30), tiingo_token="tok", client=client)   # same slot
    assert len(calls) == 9
    gather_history(policy, now=utc(2026, 10, 1, 18, 40), tiingo_token="tok", client=client)   # next slot
    assert len(calls) == 18


def test_missing_token_and_failures_are_flags_not_errors(policy, mock_client):
    calls = []
    history, flags = gather_history(policy, now=NOW, tiingo_token=None, client=mock_client(_router(calls)))
    assert set(history) == {"BTC", "ETH"}
    assert "history_missing:NDX:no_tiingo_token" in flags and len(flags) == 7
    history, flags = gather_history(
        policy, now=NOW, tiingo_token="tok", client=mock_client(_router([], fail_hosts={"data-api.binance.vision"})),
        cache=FileCache("history_fail"),
    )
    assert flags == ["history_failed:BTC", "history_failed:ETH"] and "BTC" not in history


def test_gather_macro_caches_and_flags(mock_client):
    calls = []
    series, flags = gather_macro(("DGS10", "VIXCLS"), now=NOW, client=mock_client(_router(calls)))
    assert flags == [] and series["VIXCLS"].iloc[-1] == 4.18 and series["DGS10"].name == "DGS10"
    assert series["DGS10"].index.tz is not None
    gather_macro(("DGS10", "VIXCLS"), now=NOW, client=mock_client(_router(calls)))
    assert len(calls) == 2
    _, flags = gather_macro(("DGS2",), now=NOW, client=mock_client(_router([], fail_hosts={"fred.stlouisfed.org"})))
    assert flags == ["macro_failed:DGS2"]
