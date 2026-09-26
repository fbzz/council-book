"""WP-G acceptance, the covariance half (design §11.3): returns sit on the core lines' calendar, a
stock column never shortens it, the covariance is pairwise-complete with a 60-row minimum, and a
short-history stock leaves the core's covariance untouched. Synthetic prices only."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from council.facts.returns import returns_matrix
from council.models.facts import MarketState
from council.reference.book import MIN_COMMON_ROWS, book_covariance, common_rows, ewma_covariance
from tests.data.synth import daily_bars

CORE = ["NDX", "SEMIS", "SPX", "GOLD", "BTC", "ETH"]


def history(stock_days: int | None = None, *, end="2026-09-30"):
    out = {}
    for k, sym in enumerate(CORE):
        crypto = sym in ("BTC", "ETH")
        out[sym] = daily_bars(end, 500 if not crypto else 700, seed=10 + k, weekdays_only=not crypto)
    if stock_days is not None:
        out["TSTA"] = daily_bars(end, stock_days, seed=99, weekdays_only=True, vol=0.03)
    return out


def test_a_short_history_stock_does_not_shorten_the_core_calendar():
    base = returns_matrix(history(), master=CORE)
    with_stock = returns_matrix(history(40), master=CORE)
    pd.testing.assert_frame_equal(with_stock[CORE], base[CORE])
    assert with_stock["TSTA"].notna().sum() == 39 and with_stock["TSTA"].iloc[:-39].isna().all()
    legacy = returns_matrix(history(40))                                  # no master: one shared window
    assert len(legacy) == 39 and len(base) == 499


def test_stock_returns_span_their_own_consecutive_closes_on_the_calendar():
    h = history(30)
    gap_day = h["TSTA"].index[-10]
    h["TSTA"] = h["TSTA"].drop(index=gap_day)                             # a missing close
    m = returns_matrix(h, master=CORE)
    closes = h["TSTA"]["close"]
    after = h["TSTA"].index[-9]
    assert math.isnan(m.loc[gap_day, "TSTA"])
    before = closes.index[closes.index.get_loc(after) - 1]
    assert m.loc[after, "TSTA"] == pytest.approx(math.log(closes[after] / closes[before]))   # two days
    assert list(m.columns) == [*CORE, "TSTA"]


def test_master_defaults_to_every_line_and_ignores_missing_lines():
    h = history(600)
    pd.testing.assert_frame_equal(returns_matrix(h, master=["NOPE"]), returns_matrix(h))
    assert returns_matrix({}, master=CORE).empty


def test_ewma_covariance_min_periods_and_common_rows():
    r = pd.DataFrame({"A": np.linspace(-0.01, 0.01, 100), "B": [np.nan] * 70 + list(np.linspace(0.01, -0.01, 30))})
    rows = common_rows(r)
    assert rows.loc["A", "A"] == 100 and rows.loc["B", "B"] == 30 and rows.loc["A", "B"] == 30
    cov = ewma_covariance(r, span=20, min_periods=60)
    assert math.isnan(cov.loc["A", "B"]) and math.isnan(cov.loc["B", "B"]) and cov.loc["A", "A"] > 0
    assert not math.isnan(ewma_covariance(r, span=20).loc["A", "B"])        # default: any common row
    with pytest.raises(ValueError):
        ewma_covariance(r, min_periods=0)


def _states(policy, sigma_stock: float = 0.45) -> dict[str, MarketState]:
    out = {}
    for ln in policy.universe.lines:
        sigma = sigma_stock if ln.asset_class == "stock" else 0.2
        out[ln.symbol] = MarketState(symbol=ln.symbol, asset_class=ln.asset_class, trend="up", sigma_ann=sigma)
    return out


def test_a_short_history_stock_leaves_the_core_covariance_untouched(sleeve_policy):
    lines = sleeve_policy.universe.lines
    states = _states(sleeve_policy)
    core = [ln.symbol for ln in lines if ln.sleeve != "satellite" and ln.in_reference]
    without = book_covariance(returns_matrix(history(), master=CORE), lines, states, sleeve_policy, symbols=core)
    notes: list[str] = []
    short = returns_matrix(history(MIN_COMMON_ROWS - 10), master=CORE)
    full = book_covariance(short, lines, states, sleeve_policy, symbols=[*core, "TSTA"], notes=notes)
    pd.testing.assert_frame_equal(full.loc[core, core], without)
    # fewer than 60 rows: variance from sigma_ann, correlation 1 with every core line (no credit)
    assert full.loc["TSTA", "TSTA"] == pytest.approx(0.45 ** 2)
    for s in core:
        assert full.loc["TSTA", s] == pytest.approx(math.sqrt(full.loc["TSTA", "TSTA"] * full.loc[s, s]))
    assert "TSTA: no return history, variance from sigma_ann" in notes
    assert "NDX/TSTA: no common history, correlation 1 assumed" in notes


def test_a_stock_with_enough_history_gets_its_own_covariance(sleeve_policy):
    lines = sleeve_policy.universe.lines
    states = _states(sleeve_policy)
    m = returns_matrix(history(250), master=CORE)
    cov = book_covariance(m, lines, states, sleeve_policy, symbols=["NDX", "TSTA"])
    assert cov.loc["TSTA", "TSTA"] != pytest.approx(0.45 ** 2)            # estimated, not the fallback
    rho = cov.loc["NDX", "TSTA"] / math.sqrt(cov.loc["NDX", "NDX"] * cov.loc["TSTA", "TSTA"])
    assert abs(rho) < 0.5                                                 # independent synthetic series
