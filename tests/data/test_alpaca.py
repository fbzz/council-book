"""The Alpaca stock-history adapter (WP-G): symbols, bar days, availability, batching and pages, the
429 fail-fast, and keys that never leave the request headers. No network, no Keychain."""

from __future__ import annotations

import subprocess

import httpx
import pandas as pd
import pytest

from council.data import alpaca
from council.data.http import DataError, RateLimited
from tests.data.synth import utc

NOW = utc(2026, 10, 1, 14, 40)
KEYS = alpaca.AlpacaKeys(key_id="PKTESTKEYID0001", secret="sEcReTvAlUe-do-not-print-0001")


def bar(day: str, close: float, *, hour: int = 4) -> dict:
    """An Alpaca daily bar stamped at New York midnight (04:00Z in summer, 05:00Z in winter)."""
    return {"t": f"{day}T{hour:02d}:00:00Z", "o": close, "h": close * 1.01, "l": close * 0.99, "c": close,
            "v": 1000, "n": 10, "vw": close}


def days(end: str, n: int) -> list[str]:
    return [d.date().isoformat() for d in pd.bdate_range(end=end, periods=n)]


# ------------------------------------------------------------------------------------ symbols, days


@pytest.mark.parametrize(("ticker", "symbol"), [("BRK-B", "BRK.B"), ("BRK/B", "BRK.B"), ("brk.b", "BRK.B"),
                                                ("F", "F"), ("TSTC-B", "TSTC.B"), ("NVDA", "NVDA")])
def test_history_tickers_become_alpaca_symbols(ticker, symbol):
    assert alpaca.alpaca_symbol(ticker) == symbol


@pytest.mark.parametrize("bad", ["", "-B", "BRK B", "A$B", "TOOLONGTICKERSYMBOL"])
def test_bad_symbols_are_refused(bad):
    with pytest.raises(ValueError):
        alpaca.alpaca_symbol(bad)


def test_bar_day_is_the_new_york_trading_date_at_utc_midnight():
    assert alpaca.bar_day("2026-09-30T04:00:00Z") == pd.Timestamp("2026-09-30", tz="UTC")      # EDT
    assert alpaca.bar_day("2026-12-01T05:00:00Z") == pd.Timestamp("2026-12-01", tz="UTC")      # EST
    assert alpaca.bar_day("2026-09-30T00:00:00Z") == pd.Timestamp("2026-09-30", tz="UTC")      # UTC label
    with pytest.raises(DataError):
        alpaca.bar_day("2026-09-30T13:30:00Z")                   # an intraday stamp: never a day
    with pytest.raises(DataError):
        alpaca.bar_day("2026-09-30T04:00:00")                    # naive


def test_parse_bars_applies_the_end_of_day_availability_rule():
    rows = [bar(d, 100.0 + i) for i, d in enumerate(days("2026-10-01", 5))]
    at_1440 = alpaca.parse_bars(rows, now=NOW)                               # 10:40 New York
    assert at_1440.index[-1] == pd.Timestamp("2026-09-30", tz="UTC")
    after_close = alpaca.parse_bars(rows, now=utc(2026, 10, 2, 0, 0))        # Oct 1 20:00 New York
    assert after_close.index[-1] == pd.Timestamp("2026-10-01", tz="UTC")
    assert list(after_close.columns) == ["open", "high", "low", "close", "volume"]
    assert alpaca.parse_bars([{"t": "2026-09-30T04:00:00Z", "c": 0}], now=NOW).empty


def test_parse_page_shapes():
    rows, token = alpaca.parse_page({"bars": {"AAA": [bar("2026-09-30", 1.0)]}, "next_page_token": "x1"})
    assert list(rows) == ["AAA"] and token == "x1"
    assert alpaca.parse_page({"bars": None, "next_page_token": None}) == ({}, None)
    for bad in ([], {"bars": []}, {"bars": {"A": "x"}}):
        with pytest.raises(DataError):
            alpaca.parse_page(bad)


# ------------------------------------------------------------------------------------ fetch


def test_fetch_daily_batches_symbols_and_follows_pages(mock_client):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "page_token" not in request.url.params:
            return httpx.Response(200, json={"bars": {"F": [bar(d, 10.0) for d in days("2026-09-30", 3)],
                                                      "TSTC.B": [bar("2026-09-28", 50.0)]},
                                             "next_page_token": "p2"})
        return httpx.Response(200, json={"bars": {"TSTC.B": [bar(d, 51.0) for d in days("2026-09-30", 2)]},
                                         "next_page_token": None})

    reserved: list[int] = []
    out = alpaca.fetch_daily(["TSTC-B", "F", "TSTA"], "2024-04-01", keys=KEYS, client=mock_client(handler),
                             now=NOW, reserve=lambda: reserved.append(1))
    assert set(out) == {"TSTC-B", "F", "TSTA"} and out["TSTA"].empty        # keyed by the policy ticker
    assert len(out["TSTC-B"]) == 3 and len(out["F"]) == 3
    assert len(seen) == 2 and len(reserved) == 2                             # reserved before each page
    params = seen[0].url.params
    assert params["symbols"] == "F,TSTA,TSTC.B" and params["timeframe"] == "1Day"
    assert params["adjustment"] == "all" and params["feed"] == "sip" and params["start"] == "2024-04-01"
    assert params["end"] == "2026-10-01T14:20:00Z"                           # as-of minus 20 minutes
    assert seen[1].url.params["page_token"] == "p2"
    headers = seen[0].headers
    assert headers["APCA-API-KEY-ID"] == KEYS.key_id and headers["APCA-API-SECRET-KEY"] == KEYS.secret
    assert KEYS.secret not in str(seen[0].url) and KEYS.key_id not in str(seen[0].url)


def test_fetch_daily_fails_fast_on_429_and_retries_5xx(mock_client, _no_sleep):
    calls: list[int] = []

    def limited(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "30"})

    with pytest.raises(RateLimited) as info:
        alpaca.fetch_daily(["F"], "2024-01-01", keys=KEYS, client=mock_client(limited), now=NOW)
    assert len(calls) == 1 and _no_sleep == []                              # no retry, no sleep
    assert KEYS.secret not in str(info.value) and KEYS.key_id not in str(info.value)

    flaky = iter([httpx.Response(503), httpx.Response(200, json={"bars": {"F": [bar("2026-09-30", 9.0)]}})])
    out = alpaca.fetch_daily(["F"], "2024-01-01", keys=KEYS, client=mock_client(lambda r: next(flaky)), now=NOW,
                             retries=1)
    assert len(out["F"]) == 1

    def forbidden(request):
        return httpx.Response(403, json={"message": "subscription does not permit querying recent SIP data"})

    with pytest.raises(DataError) as info:
        alpaca.fetch_daily(["F"], "2024-01-01", keys=KEYS, client=mock_client(forbidden), now=NOW)
    assert "403" in str(info.value) and KEYS.secret not in str(info.value)


def test_fetch_daily_refuses_oversized_batches_and_stops_on_reserve(mock_client):
    with pytest.raises(ValueError):
        alpaca.fetch_daily([f"S{i}" for i in range(alpaca.SYMBOLS_PER_REQUEST + 1)], "2024-01-01", keys=KEYS,
                           client=mock_client(lambda r: httpx.Response(500)), now=NOW)
    sent: list[int] = []

    class Stop(Exception):
        pass

    def refuse():
        raise Stop

    with pytest.raises(Stop):
        alpaca.fetch_daily(["F"], "2024-01-01", keys=KEYS, now=NOW, reserve=refuse,
                           client=mock_client(lambda r: sent.append(1) or httpx.Response(200, json={})))
    assert sent == []                                                        # nothing sent after a refusal


# ------------------------------------------------------------------------------------ keys


def test_keys_are_redacted():
    assert repr(KEYS) == "AlpacaKeys(<redacted>)" and KEYS.secret not in str(KEYS)
    assert KEYS.secret not in f"{KEYS!r} {KEYS}"


def test_load_keys_env_override_and_stub_mode_never_runs_security(monkeypatch):
    def runner(*a, **k):
        raise AssertionError("security must not run in stub mode")

    monkeypatch.delenv(alpaca.KEY_ID_ENV, raising=False)
    monkeypatch.delenv(alpaca.SECRET_ENV, raising=False)
    assert alpaca.load_keys(runner=runner) is None                           # stub mode, no env
    monkeypatch.setenv(alpaca.KEY_ID_ENV, "PKENVKEY00001")
    assert alpaca.load_keys(runner=runner) is None                           # the secret is missing
    monkeypatch.setenv(alpaca.SECRET_ENV, "env-secret-000001")
    keys = alpaca.load_keys(runner=runner)
    assert keys is not None and keys.key_id == "PKENVKEY00001" and keys.secret == "env-secret-000001"
    monkeypatch.setenv(alpaca.SECRET_ENV, "has space inside")
    assert alpaca.load_keys(runner=runner) is None                           # malformed: no keys


def test_load_keys_reads_exactly_the_two_keychain_items(monkeypatch):
    monkeypatch.setenv("COUNCIL_MODE", "dry_run")
    monkeypatch.delenv(alpaca.KEY_ID_ENV, raising=False)
    monkeypatch.delenv(alpaca.SECRET_ENV, raising=False)
    calls: list[list[str]] = []
    values = {alpaca.KEY_ID_SERVICE: "PKCHAINKEY0001\n", alpaca.SECRET_SERVICE: "chain-secret-0001\n"}

    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, values[cmd[3]], "")

    keys = alpaca.load_keys(runner=runner)
    assert keys == alpaca.AlpacaKeys("PKCHAINKEY0001", "chain-secret-0001")
    assert calls == [["security", "find-generic-password", "-s", "council-book.alpaca-key-id", "-a", "council", "-w"],
                     ["security", "find-generic-password", "-s", "council-book.alpaca-secret", "-a", "council", "-w"]]

    def missing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 44, "", "not found")

    assert alpaca.load_keys(runner=missing) is None
    with pytest.raises(ValueError):
        alpaca._keychain("council-book.etoro.write", runner)                # never another item


@pytest.mark.parametrize("mode", ["dry_run", "live"])
def test_outside_stub_mode_the_env_override_is_ignored(monkeypatch, mode):
    """Secrets come from the Keychain only: a stray env variable never becomes a live credential."""
    monkeypatch.setenv("COUNCIL_MODE", mode)
    monkeypatch.setenv(alpaca.KEY_ID_ENV, "PKENVKEY00001")
    monkeypatch.setenv(alpaca.SECRET_ENV, "env-secret-000001")

    def missing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 44, "", "not found")

    assert alpaca.load_keys(runner=missing) is None
    values = {alpaca.KEY_ID_SERVICE: "PKCHAINKEY0001\n", alpaca.SECRET_SERVICE: "chain-secret-0001\n"}

    def chain(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, values[cmd[3]], "")

    assert alpaca.load_keys(runner=chain) == alpaca.AlpacaKeys("PKCHAINKEY0001", "chain-secret-0001")
