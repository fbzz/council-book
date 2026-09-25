"""Signals: trend state, crypto confirmation, sigma with floor, vol ratio, backward-only windows."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from council.reference.signals import (
    line_signals,
    sigma_annualised,
    signal_params,
    trend_states,
    vol_ratio,
)


def _s(values, start="2020-01-01"):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype=float)


def test_trend_up_down_mixed_and_missing():
    # fast 2, slow 4
    up = trend_states(_s([10, 10, 10, 10, 12]), fast=2, slow=4)
    down = trend_states(_s([10, 10, 10, 10, 8]), fast=2, slow=4)
    mixed = trend_states(_s([4, 10, 10, 7, 9]), fast=2, slow=4)  # 9 > sma2 8, 9 == sma4 9
    assert up.tolist() == [None, None, None, "down", "up"]  # 10 equals both SMAs: above neither
    assert down.iloc[-1] == "down" and mixed.iloc[-1] == "mixed"
    assert up.dtype == object


def test_a_close_equal_to_an_sma_is_not_above_it():
    assert trend_states(_s([10, 10, 10, 10]), fast=2, slow=4).iloc[-1] == "down"
    assert trend_states(_s([8, 8, 10, 10]), fast=2, slow=4).iloc[-1] == "mixed"  # above sma4 only


def test_trend_needs_the_full_slow_window():
    s = trend_states(_s(np.linspace(1, 2, 250)), fast=50, slow=200)
    assert s.iloc[:199].isna().all() and s.iloc[199] == "up"


def test_one_close_blip_does_not_flip_crypto():
    # raw: up up mixed up mixed mixed down -> confirmed with 2 closes
    closes = _s([10, 10, 10, 10, 12, 13, 12.2, 14, 13.4, 13.3, 11])
    raw = trend_states(closes, fast=2, slow=4)
    confirmed = trend_states(closes, fast=2, slow=4, confirm=2)
    assert raw.tolist()[3:] == ["down", "up", "up", "mixed", "up", "mixed", "mixed", "down"]
    # down->up needs two ups; the single mixed blip is ignored; two mixed closes flip; one down does not
    assert confirmed.tolist()[3:] == ["down", "down", "up", "up", "up", "up", "mixed", "mixed"]


def test_crypto_confirmation_comes_from_policy(policy):
    assert signal_params(policy, "crypto")["confirm"] == 2
    assert signal_params(policy, "index")["confirm"] == 1
    assert signal_params(policy, "crypto")["ann"] == 365 and signal_params(policy, "etf")["ann"] == 252


def test_trend_rejects_bad_windows():
    with pytest.raises(ValueError):
        trend_states(_s([1, 2, 3]), fast=5, slow=2)


def test_sigma_of_constant_magnitude_returns():
    r = _s([0.01 * (-1) ** i for i in range(100)])
    sigma = sigma_annualised(r, lam=0.94, window=60, floor_mult=0.8, ann=252)
    assert sigma.iloc[:59].isna().all()
    # EWMA of r^2 is exactly 1e-4; the floor 0.8 x realised (~0.01) does not bind
    assert sigma.iloc[-1] == pytest.approx(0.01 * math.sqrt(252))


def test_sigma_floor_binds_after_a_calm_spell():
    rng = np.random.default_rng(3)
    r = _s(np.concatenate([rng.standard_normal(80) * 0.03, np.full(25, 0.0001)]))
    sigma = sigma_annualised(r, lam=0.94, window=60, floor_mult=0.8, ann=252)
    realised = r.rolling(60).std(ddof=1) * math.sqrt(252)
    ewma = np.sqrt((r**2).ewm(alpha=0.06, adjust=True).mean()) * math.sqrt(252)
    valid = realised.notna()
    assert (sigma[valid] >= 0.8 * realised[valid] - 1e-12).all()
    assert sigma.iloc[-1] == pytest.approx(0.8 * realised.iloc[-1])  # floor, not the decayed EWMA
    assert ewma.iloc[-1] < sigma.iloc[-1]


def test_vol_ratio_is_now_over_trailing_median():
    sigma = _s([0.1] * 10 + [0.2])
    ratio = vol_ratio(sigma, 5)
    assert ratio.iloc[:4].isna().all()
    assert ratio.iloc[9] == pytest.approx(1.0) and ratio.iloc[10] == pytest.approx(2.0)


def test_vol_ratio_needs_126_estimates_within_a_one_year_window():
    ratio = vol_ratio(_s([0.2] * 300), 252)
    assert ratio.iloc[:125].isna().all() and ratio.iloc[125] == pytest.approx(1.0)


def test_signals_use_only_data_up_to_each_row(policy):
    rng = np.random.default_rng(11)
    close = pd.Series(100 * np.exp(np.cumsum(rng.standard_normal(700) * 0.01)),
                      index=pd.bdate_range("2018-01-01", periods=700))
    base = line_signals(close, asset_class="etf", policy=policy)
    t = 550
    mutated = close.copy()
    mutated.iloc[t + 1 :] *= np.exp(np.cumsum(rng.standard_normal(700 - t - 1) * 0.2))
    changed = line_signals(mutated, asset_class="etf", policy=policy)
    pd.testing.assert_frame_equal(base.iloc[: t + 1], changed.iloc[: t + 1])
    assert not base.iloc[t + 1 :].equals(changed.iloc[t + 1 :])
