from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from council.facts import features as F
from tests.data.synth import alternating, bars_from_closes, day_index, from_log_returns, utc

NOW = utc(2026, 10, 1, 14, 40)      # Thursday; bars ending Sep 30 are usable for every source
LAST = "2026-09-30"


def _line(policy, sym):
    return policy.universe.by_symbol()[sym]


def _state(policy, sym, closes, *, now=NOW, end=LAST):
    line = _line(policy, sym)
    weekdays = line.signal.source == "tiingo"
    bars = bars_from_closes(closes, day_index(end, len(closes), weekdays_only=weekdays))
    return F.market_state(line, bars, now=now, policy=policy)


# ------------------------------------------------------------------------------------ trend
def test_trend_states_up_mixed_down_and_warmup():
    up = pd.Series(100 * 1.001 ** np.arange(250))
    assert F.trend_series(up, 50, 200).iloc[-1] == "up"
    assert F.trend_series(up[::-1].reset_index(drop=True), 50, 200).iloc[-1] == "down"
    mixed = pd.Series([100.0] * 150 + [120.0] * 49 + [110.0])       # < SMA50 (119.8), > SMA200 (104.95)
    assert F.trend_series(mixed, 50, 200).iloc[-1] == "mixed"
    flat = pd.Series([100.0] * 250)                                  # equal to an SMA is not above it
    assert F.trend_series(flat, 50, 200).iloc[-1] == "down"
    warm = F.trend_series(up, 50, 200)
    assert warm.iloc[198] is None and warm.iloc[199] == "up"


@pytest.mark.parametrize(
    ("raw", "n", "expected"),
    [
        (["up", "up", "mixed", "down", "down"], 2, ["up", "up", "up", "up", "down"]),
        (["up", "mixed", "up", "mixed", "up"], 2, ["up"] * 5),       # one-close wobbles never flip
        (["up", "down", "down", "up"], 2, ["up", "up", "down", "down"]),
        (["up", "mixed", "down"], 1, ["up", "mixed", "down"]),        # no confirmation needed
        ([None, None, "down", "up", "up"], 2, [None, None, "down", "down", "up"]),
    ],
)
def test_confirm_trend(raw, n, expected):
    assert F.confirm_trend(pd.Series(raw, dtype=object), n).tolist() == expected


def test_crypto_needs_two_closes_to_flip_equities_flip_at_once(policy):
    base = list(100 * 1.002 ** np.arange(260))
    one_below = base + [base[-1] * 0.8]
    assert _state(policy, "BTC", one_below).trend == "up"             # 1 close below: not confirmed
    assert _state(policy, "SPX", one_below).trend == "down"
    two_below = one_below + [base[-1] * 0.8]
    assert _state(policy, "BTC", two_below).trend == "down"
    s = _state(policy, "SPX", one_below)
    assert s.dist_sma50_pct < 0 and s.dist_sma200_pct < 0


# ------------------------------------------------------------------------------------ vol
def test_sigma_ewma_wins_when_floor_does_not_bind(policy):
    a = 0.01
    s = _state(policy, "SPX", from_log_returns(alternating(100, a)))
    assert s.sigma_daily == pytest.approx(a, rel=1e-6)                # 0.8*1.0084a < a
    assert s.sigma_ann == pytest.approx(a * math.sqrt(252), rel=1e-5)


def test_sigma_floor_binds_after_calm_spell(policy):
    a = 0.02
    rets = np.concatenate([alternating(60, a), np.zeros(30)])
    s = _state(policy, "BTC", from_log_returns(rets))
    floor = 0.8 * np.std(rets[-60:], ddof=1)
    lam = 0.94
    w = lam ** np.arange(len(rets))[::-1]
    ewma = math.sqrt(float((w * rets**2).sum() / w.sum()))
    assert ewma < floor
    assert s.sigma_daily == pytest.approx(floor, rel=1e-5)
    assert s.sigma_ann == pytest.approx(floor * math.sqrt(365), rel=1e-5)   # crypto: 365 days


def test_sigma_needs_a_full_realised_window(policy):
    assert _state(policy, "SPX", from_log_returns(alternating(59, 0.01))).sigma_daily is None
    assert _state(policy, "SPX", from_log_returns(alternating(60, 0.01))).sigma_daily is not None


def test_vol_ratio_constant_vol_is_one(policy):
    s = _state(policy, "SPX", from_log_returns(alternating(300, 0.01)))
    assert s.vol_ratio_1y == pytest.approx(1.0, abs=1e-4)


def test_vol_ratio_after_vol_doubles(policy):
    a = 0.01
    rets = np.concatenate([alternating(400, a), alternating(40, 2 * a)])
    s = _state(policy, "SPX", from_log_returns(rets))
    lam = 0.94
    w = lam ** np.arange(len(rets))[::-1]
    ewma_now = math.sqrt(float((w * rets**2).sum() / w.sum()))
    expected_now = max(ewma_now, 0.8 * np.std(rets[-60:], ddof=1))
    assert s.sigma_daily == pytest.approx(expected_now, rel=1e-5)
    assert s.vol_ratio_1y == pytest.approx(expected_now / a, rel=1e-3)   # 1y median still = a
    assert 1.9 < s.vol_ratio_1y < 2.0


def test_vol_ratio_needs_126_estimates(policy):
    short = from_log_returns(alternating(60 + 124, 0.01))                # 125 sigma estimates
    enough = from_log_returns(alternating(60 + 125, 0.01))               # 126 sigma estimates
    assert _state(policy, "SPX", short).vol_ratio_1y is None
    assert _state(policy, "SPX", enough).vol_ratio_1y == pytest.approx(1.0, abs=1e-4)


def test_ewma5_60_ratio(policy):
    a = 0.01
    calm = _state(policy, "SPX", from_log_returns(alternating(200, a)))
    assert calm.ewma5_60_ratio == pytest.approx(1.0, abs=1e-6)
    rets = np.concatenate([alternating(195, a), alternating(5, 3 * a)])
    shock = _state(policy, "SPX", from_log_returns(rets))
    r2 = pd.Series(rets**2)
    expected = math.sqrt(r2.ewm(span=5).mean().iloc[-1] / r2.ewm(span=60).mean().iloc[-1])
    assert shock.ewma5_60_ratio == pytest.approx(expected, rel=1e-3)
    assert shock.ewma5_60_ratio > 1.5                                 # 3x moves over 5 days
    assert F.shock_ratio(pd.Series(alternating(59, a))) is None
    assert F.shock_ratio(pd.Series(np.zeros(80))) is None


# ------------------------------------------------------------------------------------ returns
def test_momentum_is_simple_return_over_bars(policy):
    closes = 100 * 1.001 ** np.arange(100)
    s = _state(policy, "SPX", closes)
    assert s.mom10d_pct == pytest.approx((1.001**10 - 1) * 100, abs=1e-4)
    assert s.mom63d_pct == pytest.approx((1.001**63 - 1) * 100, abs=1e-4)
    assert _state(policy, "SPX", closes[:10]).mom10d_pct is None


def test_dd52_uses_only_the_last_364_days(policy):
    rise = list(np.linspace(100, 200, 100))
    fall = list(np.linspace(200, 150, 50))[1:]
    s = _state(policy, "BTC", rise + fall)
    assert s.dd52_pct == pytest.approx(-25.0, abs=1e-6)
    old_peak = [300.0] + [100.0] * 380 + rise + fall                  # 300 is > 364 days old
    assert _state(policy, "BTC", old_peak).dd52_pct == pytest.approx(-25.0, abs=1e-6)
    at_high = _state(policy, "BTC", rise)
    assert at_high.dd52_pct == 0.0


def test_ret1d_sigma(policy):
    rets = np.concatenate([alternating(120, 0.01), [math.log(1.05)]])
    s = _state(policy, "SPX", from_log_returns(rets))
    assert s.ret1d_sigma == pytest.approx(math.log(1.05) / s.sigma_daily, abs=1e-3)
    assert s.ret1d_sigma > 2.5
    down = _state(policy, "SPX", from_log_returns(np.concatenate([alternating(120, 0.01), [-0.01]])))
    assert down.ret1d_sigma < 0


# ------------------------------------------------------------------------------------ timing
def test_data_age_by_source(policy):
    btc = _state(policy, "BTC", from_log_returns(alternating(80, 0.01)), now=utc(2026, 10, 1, 5, 0))
    assert btc.data_age_h == pytest.approx(5.0)                        # Sep 30 bar closed 00:00
    spx = _state(policy, "SPX", from_log_returns(alternating(80, 0.01)))
    assert spx.data_age_h == pytest.approx(14.667, abs=1e-3)          # available 20:00 NY = 00:00Z
    assert spx.history_source == "tiingo:SPY" and btc.history_source == "binance:BTCUSDT"


def test_market_open_follows_clock(policy):
    sat = utc(2026, 10, 3, 15, 0)
    closes = from_log_returns(alternating(80, 0.01))
    assert _state(policy, "SEMIS", closes, now=sat, end="2026-10-02").market_open is False
    assert _state(policy, "BTC", closes, now=sat, end="2026-10-02").market_open is True
    assert _state(policy, "SEMIS", closes).market_open is True        # Thursday 10:40 New York


def test_in_progress_and_unpublished_bars_never_matter(policy):
    closes = list(100 * 1.001 ** np.arange(300))
    for sym, end in (("BTC", "2026-10-01"), ("SPX", "2026-10-01")):    # Oct 1 bar: open / unpublished
        clean = _state(policy, sym, closes[:-1] + [closes[-2] * 1.0001], end=end)
        wild = _state(policy, sym, closes[:-1] + [closes[-2] * 10.0], end=end)
        assert clean == wild


def test_short_history_and_empty_input(policy):
    s = _state(policy, "SPX", 100 * 1.001 ** np.arange(120))
    assert s.trend is None and s.dist_sma200_pct is None and s.dist_sma50_pct is not None
    assert s.sigma_daily is not None and s.vol_ratio_1y is None
    empty = F.market_state(_line(policy, "NDX"), pd.DataFrame(), now=NOW, policy=policy)
    assert empty.trend is None and empty.data_age_h is None and empty.history_source == "tiingo:QQQ"


def test_source_override_switches_availability_rule(policy):
    line = _line(policy, "EURUSD")
    idx = pd.date_range("2025-01-01", "2026-10-01", freq="D", tz="UTC")
    bars = bars_from_closes(100 * 1.0005 ** np.arange(len(idx)), idx)
    as_etoro = F.market_state(line, bars, now=NOW, policy=policy, source="etoro")
    assert as_etoro.history_source == "etoro" and as_etoro.data_age_h == pytest.approx(14.667, abs=1e-3)
    assert F.last_bar_available_at(line, bars, now=NOW, source="etoro") == utc(2026, 10, 1, 0, 0)


def test_market_states_skips_lines_without_history(policy):
    bars = bars_from_closes(100 * 1.001 ** np.arange(260), day_index(LAST, 260))
    states = F.market_states(policy, {"BTC": bars, "ETH": bars}, now=NOW)
    assert set(states) == {"BTC", "ETH"} and states["ETH"].trend == "up"
