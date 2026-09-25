"""Metrics on hand-written paths: drawdown, rolling windows, headline stats, trend stats, kill drill."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from council.reference.metrics import (
    max_drawdown,
    performance,
    rolling_drawdown_share,
    soft_kill_drill,
    trend_stats,
)
from tests.reference.conftest import sim_from_nav


def test_max_drawdown_known_path():
    assert max_drawdown(pd.Series([1, 1.2, 0.9, 1.1, 0.6, 1.3])) == pytest.approx(0.6 / 1.2 - 1)
    assert max_drawdown(pd.Series([1.0, 1.1, 1.2])) == 0.0


def test_rolling_drawdown_share_counts_windows_at_or_below_the_threshold():
    nav = pd.Series([1, 0.7, 0.7, 0.7, 0.7, 1.0, 1.0, 1.0, 1.0])
    assert rolling_drawdown_share(nav, window=3, threshold=-0.25) == pytest.approx(1 / 6)
    assert rolling_drawdown_share(nav, window=3, threshold=-0.35) == 0.0
    assert rolling_drawdown_share(pd.Series([1.0, 0.5]), window=3) is None


def test_performance_on_a_constant_growth_path():
    g = 0.0004
    sim = sim_from_nav([(1 + g) ** i for i in range(253)])
    m = performance(sim)
    assert m["years"] == pytest.approx(1.0)
    assert m["cagr"] == pytest.approx((1 + g) ** 252 - 1)
    assert m["ann_vol"] == pytest.approx(0.0, abs=1e-12)
    assert m["max_drawdown"] == 0.0 and m["calmar"] is None
    assert m["turnover_per_year"] == 0.0 and m["cost_drag_per_year"] == 0.0 and m["mean_gross"] == 1.0


def test_performance_sharpe_calmar_turnover_and_cost_drag():
    rng = np.random.default_rng(1)
    r = rng.standard_normal(504) * 0.01 + 0.0005
    nav = np.concatenate([[1.0], np.cumprod(1 + r)])
    sim = sim_from_nav(list(nav))
    sim.trades.iloc[10, 0] = 0.5
    sim.trades.iloc[300, 0] = -0.5
    sim.costs.iloc[10] = 0.001
    m = performance(sim)
    assert m["years"] == pytest.approx(2.0)
    assert m["sharpe"] == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(252))
    assert m["ann_vol"] == pytest.approx(r.std(ddof=1) * math.sqrt(252))
    assert m["calmar"] == pytest.approx(m["cagr"] / abs(m["max_drawdown"]))
    assert m["turnover_per_year"] == pytest.approx(0.5) and m["cost_drag_per_year"] == pytest.approx(0.0005)


def test_trend_stats_flips_and_time_in_state():
    trend = pd.DataFrame({"X": [None, None, "up", "up", "mixed", "up", "down", "down"]}, dtype=object)
    st = trend_stats(trend)["X"]
    assert st["flips_per_year"] == pytest.approx(3 / (6 / 252))
    assert (st["up"], st["mixed"], st["down"], st["missing"]) == (3 / 8, 1 / 8, 2 / 8, 2 / 8)
    assert trend_stats(pd.DataFrame({"Y": [None, None]}, dtype=object))["Y"]["flips_per_year"] is None


# ------------------------------------------------------------------------------------ soft kill

PATH = [1.0, 0.9, 0.8, 0.85, 0.79, 0.74, 0.9, 1.0, 1.05, 0.83, 0.9]


def test_soft_kill_events_are_first_closes_of_each_episode():
    events = soft_kill_drill(sim_from_nav(PATH), warn_at=0.80, halt_at=0.75, horizon=60)
    kinds = [(e["kind"], str(e["date"])) for e in events]
    dates = pd.bdate_range("2020-01-01", periods=len(PATH))
    assert kinds == [("WARN", str(dates[2].date())), ("HALT", str(dates[5].date())), ("WARN", str(dates[9].date()))]
    warn = events[0]
    assert warn["drawdown"] == pytest.approx(-0.2)
    assert warn["crossings_in_episode"] == 2 and warn["days_below_in_episode"] == 3
    assert warn["return_next"] == pytest.approx(0.9 / 0.8 - 1)      # horizon truncated at the path end
    assert warn["worst_further"] == pytest.approx(0.74 / 0.8 - 1)
    assert warn["recovered_within"] is True and warn["days_observed"] == 8
    halt = events[1]
    assert halt["drawdown"] == pytest.approx(0.74 - 1) and halt["crossings_in_episode"] == 1


def test_soft_kill_horizon_window():
    gross = [1.0] * len(PATH)
    gross[3], gross[4] = 0.5, 0.3
    events = soft_kill_drill(sim_from_nav(PATH, gross), horizon=2)
    warn = events[0]
    assert warn["return_next"] == pytest.approx(0.79 / 0.8 - 1)
    assert warn["mean_gross_next"] == pytest.approx(0.4)
    assert warn["days_observed"] == 2


def test_no_event_above_the_lines():
    assert soft_kill_drill(sim_from_nav([1.0, 0.9, 0.81, 0.85, 0.95, 1.1])) == []


def test_warn_but_not_halt():
    events = soft_kill_drill(sim_from_nav([1.0, 0.78, 0.76, 0.9]))
    assert [e["kind"] for e in events] == ["WARN"]
