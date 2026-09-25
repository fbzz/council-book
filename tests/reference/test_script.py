"""scripts/backtest_reference.py: synthetic end-to-end, data adapters, fake data layer, privacy."""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest
from pydantic import BaseModel

from council import paths
from council.reference.report import LABEL, assert_public_safe
from council.reference.synthetic import synthetic_closes

DOCS = paths.REPO_ROOT / "docs" / "reference-backtest.md"


def _docs_fingerprint():
    return DOCS.stat().st_mtime_ns if DOCS.exists() else None


def test_synthetic_run_writes_private_csvs_and_a_labelled_summary(script_module, tmp_path):
    before = _docs_fingerprint()
    rc = script_module.main(["--synthetic", "--start", "2019-01-02", "--end", "2020-06-30"])
    assert rc == 0
    out = paths.state_dir() / "backtests" / "synthetic"
    for name in ("nav.csv", "reference_weights.csv", "reference_targets.csv", "reference_levels.csv",
                 "reference_trades.csv", "trend_states.csv", "metrics.csv", "kill_drill.csv"):
        assert (out / name).exists(), name
    nav = pd.read_csv(out / "nav.csv", index_col=0)
    assert {"reference", "reference_cfd", "no_trend", "static", "spy_bh", "qqq_bh", "sixty_forty"} <= set(nav.columns)
    assert nav["reference"].iloc[0] == 1.0  # an index, not money
    text = (out / "reference-backtest-synthetic.md").read_text()
    assert LABEL in text and "SYNTHETIC" in text
    assert str(tmp_path) not in text and "/private/" not in text
    assert_public_safe(text)
    assert _docs_fingerprint() == before  # the public docs file is never overwritten by synthetic data


def test_private_outputs_refuse_the_repository(script_module):
    with pytest.raises(RuntimeError):
        script_module.main(["--synthetic", "--start", "2019-01-02", "--end", "2019-03-29",
                            "--out-dir", str(paths.REPO_ROOT / "state_leak")])
    assert not (paths.REPO_ROOT / "state_leak").exists()


def test_end_must_follow_start(script_module):
    with pytest.raises(SystemExit):
        script_module.main(["--synthetic", "--start", "2020-01-02", "--end", "2019-01-02"])


# ------------------------------------------------------------------------------------ adapters


def test_call_flexible_maps_parameter_names(script_module):
    seen = {}

    def fetch(symbol, start_date, end_date, api_key=None):
        seen.update(symbol=symbol, start=start_date, end=end_date, key=api_key)
        return "ok"

    assert script_module.call_flexible(fetch, ticker="SPY", start=1, end=2, token="t", interval="1d") == "ok"
    assert seen == {"symbol": "SPY", "start": 1, "end": 2, "key": "t"}

    def only_ticker(ticker):
        return ticker

    assert script_module.call_flexible(only_ticker, ticker="QQQ", start=1) == "QQQ"

    def no_ticker(start):
        return start

    with pytest.raises(TypeError):
        script_module.call_flexible(no_ticker, ticker="QQQ", start=1)


def test_to_close_series_prefers_adjusted_close_and_normalises_dates(script_module):
    raw = pd.DataFrame({"date": ["2021-01-04T00:00:00.000Z", "2021-01-05T00:00:00.000Z"],
                        "close": [10.0, 11.0], "adjClose": [9.0, 10.0]})
    s = script_module.to_close_series(raw, "SPY")
    assert s.tolist() == [9.0, 10.0] and s.index.tz is None
    assert list(s.index) == [pd.Timestamp("2021-01-04"), pd.Timestamp("2021-01-05")]


def test_to_close_series_reads_binance_rows_dicts_models_and_series(script_module):
    rows = [[1609459200000, "1", "2", "0.5", "1.5", "10", 1609545599999], [1609545600000, "1", "2", "0.5", "1.7", "9", 0]]
    assert script_module.to_close_series(rows).tolist() == [1.5, 1.7]
    assert script_module.to_close_series(rows).index[0] == pd.Timestamp("2021-01-01")
    dicts = [{"open_time": datetime(2021, 1, 1, tzinfo=UTC), "close": 3.0}]
    assert script_module.to_close_series(dicts).iloc[0] == 3.0

    class Bar(BaseModel):
        date: date
        close: float

    assert script_module.to_close_series([Bar(date=date(2021, 1, 4), close=5.0)]).iloc[0] == 5.0
    series = pd.Series([1.0], index=pd.DatetimeIndex(["2021-01-04 21:00"], tz="UTC"))
    assert script_module.to_close_series(series).index[0] == pd.Timestamp("2021-01-04")
    with pytest.raises(ValueError):
        script_module.to_close_series(pd.DataFrame({"date": ["2021-01-04"], "price": [1.0]}))


# ------------------------------------------------------------------------------------ fake data layer


@pytest.fixture
def fake_data_layer(monkeypatch):
    """council.data.{tiingo,binance,credentials} replaced by in-memory fakes (no network)."""
    calls: dict[str, list] = {"tiingo": [], "binance": [], "secret": []}
    equity = synthetic_closes(["QQQ", "SOXX", "SPY", "GLD", "IEF"], start=date(2016, 1, 4), end=date(2020, 12, 31))
    crypto = synthetic_closes([], start=date(2016, 1, 4), end=date(2020, 12, 31),
                              crypto_tickers=["BTCUSDT", "ETHUSDT"], crypto_start=date(2017, 8, 17))

    def fetch_daily(ticker, start, *, token, client=None, now=None):
        """Same signature and output shape as council.data.tiingo.fetch_daily (canonical bars)."""
        calls["tiingo"].append((ticker, start, token))
        s = equity[ticker]
        index = pd.DatetimeIndex(s.index, name="start").tz_localize("UTC")
        return pd.DataFrame({"open": s.values, "high": s.values, "low": s.values, "close": s.values,
                             "volume": 0.0}, index=index)

    def fetch_klines(symbol, interval, start_time, end_time):
        calls["binance"].append((symbol, interval))
        s = crypto[symbol]
        ms = (s.index.as_unit("ms").asi8).tolist()
        return [[t, "0", "0", "0", str(v), "0"] for t, v in zip(ms, s.values, strict=True)]

    def secret(name):
        calls["secret"].append(name)
        return "fake-token"

    monkeypatch.setitem(sys.modules, "council.data.tiingo", SimpleNamespace(fetch_daily=fetch_daily))
    monkeypatch.setitem(sys.modules, "council.data.binance", SimpleNamespace(fetch_klines=fetch_klines))
    monkeypatch.setitem(sys.modules, "council.data.credentials", SimpleNamespace(secret=secret))
    return calls


def test_load_history_uses_the_data_layer_and_drops_bars_after_end(script_module, policy, fake_data_layer):
    lines = [ln for ln in policy.universe.lines if ln.in_reference]
    out = script_module.load_history(lines, start=date(2016, 1, 4), end=date(2019, 12, 31))
    assert set(out) == {"QQQ", "SOXX", "SPY", "GLD", "IEF", "BTCUSDT", "ETHUSDT"}
    assert all(s.index.max() <= pd.Timestamp("2019-12-31") for s in out.values())
    assert fake_data_layer["secret"] == ["council-book.tiingo"]
    assert all(call[2] == "fake-token" for call in fake_data_layer["tiingo"])
    assert {c[1] for c in fake_data_layer["binance"]} == {"1d"}
    assert out["BTCUSDT"].index.min() == pd.Timestamp("2017-08-17")


def test_real_mode_end_to_end_with_a_fake_data_layer(script_module, fake_data_layer, tmp_path):
    before = _docs_fingerprint()
    docs = tmp_path / "public" / "reference-backtest.md"
    rc = script_module.main(["--start", "2019-01-02", "--end", "2020-06-30", "--docs-path", str(docs)])
    assert rc == 0
    text = docs.read_text()
    assert LABEL in text and "SYNTHETIC" not in text
    assert (paths.state_dir() / "backtests" / "metrics.csv").exists()
    assert _docs_fingerprint() == before


def test_missing_tiingo_token_stops_with_guidance(script_module, policy, fake_data_layer, monkeypatch):
    monkeypatch.setitem(sys.modules, "council.data.credentials", SimpleNamespace(secret=lambda name: None))
    lines = [ln for ln in policy.universe.lines if ln.in_reference]
    with pytest.raises(SystemExit, match="COUNCIL_MODE"):
        script_module.load_history(lines, start=date(2016, 1, 4), end=date(2019, 12, 31))


def test_binance_without_a_start_parameter_is_paged(script_module, policy, fake_data_layer, monkeypatch):
    def fetch_klines(ticker, interval="1d", limit=1000, *, client=None, now=None):  # the real signature
        raise AssertionError("only returns the latest 1000 bars; must not be used for history")

    paged = []

    def fake_history(ticker, *, start, end):
        paged.append((ticker, start, end))
        idx = pd.date_range("2017-08-17", "2019-12-31", freq="D", tz="UTC", name="start")
        return pd.DataFrame({"close": range(1, len(idx) + 1)}, index=idx, dtype=float)

    monkeypatch.setitem(sys.modules, "council.data.binance", SimpleNamespace(fetch_klines=fetch_klines))
    monkeypatch.setattr(script_module, "fetch_binance_daily_history", fake_history)
    lines = [ln for ln in policy.universe.lines if ln.in_reference]
    out = script_module.load_history(lines, start=date(2016, 1, 4), end=date(2019, 12, 31))
    assert sorted(t for t, _, _ in paged) == ["BTCUSDT", "ETHUSDT"]
    assert out["BTCUSDT"].index.min() == pd.Timestamp("2017-08-17")


def test_binance_history_pages_forward_with_the_data_layer_parser(script_module):
    days = pd.date_range("2017-08-17", "2020-12-31", freq="D", tz="UTC")
    day_ms = [int(d.value // 10**6) for d in days]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        chosen = [ms for ms in day_ms if ms >= int(params["startTime"])][: int(params["limit"])]
        rows = [[ms, "1", "1", "1", "2", "1", ms + 86_399_999] for ms in chosen]
        return httpx.Response(200, json=rows)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    bars = script_module.fetch_binance_daily_history(
        "BTCUSDT", start=date(2015, 1, 1), end=date(2020, 12, 31), client=client,
        now=datetime(2021, 1, 5, tzinfo=UTC),
    )
    assert len(bars) == len(days) and bars.index.min() == days[0] and bars.index.max() == days[-1]
    assert len(seen) == 2 and seen[0]["symbol"] == "BTCUSDT" and seen[0]["interval"] == "1d"
    assert int(seen[1]["startTime"]) == day_ms[1000]
    assert script_module.to_close_series(bars).index[0] == pd.Timestamp("2017-08-17")
