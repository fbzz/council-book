"""Backtest engine: one-day lag, lookahead, cost accounting, deadband, controls, vehicle costs."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from council.reference.backtest import (
    BacktestConfig,
    BookPlan,
    CostModel,
    asof_align,
    build_panel,
    control_cost_model,
    cost_model,
    fixed_mix_plan,
    reference_plan,
    run_backtest,
    simulate,
    static_plan,
    vehicle_cost_class,
)
from council.reference.synthetic import synthetic_closes

TICKERS = {"NDX": "QQQ", "SEMIS": "SOXX", "SPX": "SPY", "GOLD": "GLD", "BTC": "BTCUSDT", "ETH": "ETHUSDT"}


def _plan(targets, levels=None, thresholds=None, start="2021-01-04"):
    idx = pd.bdate_range(start, periods=len(targets))
    t = pd.DataFrame({"X": targets}, index=idx, dtype=float)
    lv = pd.DataFrame({"X": levels if levels is not None else [1.0 if v else 0.0 for v in targets]}, index=idx)
    th = pd.DataFrame({"X": thresholds if thresholds is not None else [np.inf] * len(targets)}, index=idx)
    return BookPlan(name="t", targets=t, levels=lv, thresholds=th)


def _returns(plan, values):
    return pd.DataFrame({"X": values}, index=plan.targets.index, dtype=float)


def _costs(per_side=0.0, fixed=0.0):
    return CostModel(per_side={"X": per_side}, fixed={"X": fixed}, classes={"X": "test"})


# ------------------------------------------------------------------------------------ timing


def test_signal_day_return_is_unreachable():
    plan = _plan([0, 0, 0, 0, 0, 1.0, 1.0, 1.0, 1.0])       # decided at close 5
    rets = [0, 0, 0, 0, 0, 0, 0.50, 0.01, 0]                # the jump arrives on day 6
    sim = simulate(plan, _returns(plan, rets), _costs(per_side=0.001))
    assert sim.orders["X"].iloc[5] == 1.0 and sim.weights["X"].iloc[5] == 0.0
    assert sim.nav.iloc[6] == pytest.approx(sim.nav.iloc[5] * (1 - 0.001))   # +50% not captured
    assert sim.trades["X"].iloc[6] == pytest.approx(1.0) and sim.weights["X"].iloc[6] == 1.0
    assert sim.nav.iloc[7] == pytest.approx(sim.nav.iloc[6] * 1.01)          # earned from day 7


def test_accounting_identity_uses_only_yesterdays_holdings():
    rng = np.random.default_rng(5)
    n = 60
    plan = _plan(list(rng.choice([0.0, 0.3, 0.6], size=n)), thresholds=[0.0] * n)
    rets = rng.standard_normal(n) * 0.02
    sim = simulate(plan, _returns(plan, rets), _costs(per_side=0.002, fixed=0.0001))
    w, nav, cost = sim.weights["X"].to_numpy(), sim.nav.to_numpy(), sim.costs.to_numpy()
    for i in range(1, n):
        assert nav[i] / nav[i - 1] == pytest.approx((1 + w[i - 1] * rets[i]) * (1 - cost[i]))


def _pipeline(closes_by_line, policy, start, end):
    lines = [ln for ln in policy.universe.lines if ln.in_reference]
    cal = pd.DatetimeIndex(sorted(set(closes_by_line["NDX"].index)))
    cal = cal[cal <= pd.Timestamp(end)]
    panel = build_panel(closes_by_line, lines, policy, cal)
    plan, _ = reference_plan(panel, lines, policy, start=start)
    sim = simulate(plan, panel.returns, cost_model(lines, policy))
    return plan, sim


def _synthetic_lines(start=date(2017, 1, 2), end=date(2019, 12, 31), crypto_start=date(2017, 8, 17), seed=3):
    eq = [t for s, t in TICKERS.items() if s not in ("BTC", "ETH")]
    raw = synthetic_closes(eq, start=start, end=end, crypto_tickers=["BTCUSDT", "ETHUSDT"],
                           crypto_start=crypto_start, seed=seed)
    return {sym: raw[t] for sym, t in TICKERS.items()}


def test_future_data_never_changes_a_decision(policy):
    closes = _synthetic_lines()
    t = pd.Timestamp("2019-06-14")
    mutated = {}
    rng = np.random.default_rng(9)
    for sym, s in closes.items():
        m = s.copy()
        after = m.index > t
        m[after] = m[after] * np.exp(np.cumsum(rng.standard_normal(after.sum()) * 0.1))
        mutated[sym] = m
    plan_a, sim_a = _pipeline(closes, policy, date(2019, 1, 2), date(2019, 12, 31))
    plan_b, sim_b = _pipeline(mutated, policy, date(2019, 1, 2), date(2019, 12, 31))
    upto = slice(None, t)
    for a, b in ((plan_a.targets, plan_b.targets), (plan_a.levels, plan_b.levels),
                 (plan_a.thresholds, plan_b.thresholds), (sim_a.orders, sim_b.orders),
                 (sim_a.weights, sim_b.weights)):
        pd.testing.assert_frame_equal(a.loc[upto], b.loc[upto])
    pd.testing.assert_series_equal(sim_a.nav.loc[upto], sim_b.nav.loc[upto])
    assert not sim_a.nav.loc[t:].iloc[2:].equals(sim_b.nav.loc[t:].iloc[2:])  # the mutation did bite later


# ------------------------------------------------------------------------------------ costs


def test_a_single_flip_and_back_costs_exactly_two_per_sides_on_the_traded_weight():
    targets = [0.5] * 10 + [0.25] * 10 + [0.5] * 10
    levels = [1.0] * 10 + [0.5] * 10 + [1.0] * 10
    plan = _plan(targets, levels)
    sim = simulate(plan, _returns(plan, [0.0] * 30), _costs(per_side=0.01))
    assert sim.costs.iloc[1] == pytest.approx(0.5 * 0.01)                 # entry
    assert sim.costs.iloc[11] == pytest.approx(0.25 * 0.01)               # flip down, one side
    assert sim.costs.iloc[11] + sim.costs.iloc[21] == pytest.approx(2 * 0.01 * 0.25)
    assert (sim.costs > 0).sum() == 3
    assert sim.nav.iloc[-1] == pytest.approx((1 - 0.005) * (1 - 0.0025) ** 2)


def test_fixed_commission_is_charged_once_per_traded_leg():
    targets = [0.5] * 10 + [0.25] * 10
    plan = _plan(targets, [1.0] * 10 + [0.5] * 10)
    sim = simulate(plan, _returns(plan, [0.0] * 20), _costs(per_side=0.0005, fixed=0.0001))
    assert sim.costs.iloc[1] == pytest.approx(0.5 * 0.0005 + 0.0001)
    assert sim.costs.iloc[11] == pytest.approx(0.25 * 0.0005 + 0.0001)
    assert sim.costs.sum() == pytest.approx(0.75 * 0.0005 + 2 * 0.0001)


def test_vehicle_cost_classes(policy):
    by = policy.universe.by_symbol()
    assert vehicle_cost_class(by["NDX"], "listed") == "etf_real"
    assert vehicle_cost_class(by["NDX"], "cfd") == "etf_cfd"        # QQQ CFD, the signal ETF
    assert vehicle_cost_class(by["GOLD"], "cfd") == "etf_cfd"       # GLD CFD before GOLD
    assert vehicle_cost_class(by["BTC"], "listed") == "crypto" == vehicle_cost_class(by["BTC"], "cfd")
    assert vehicle_cost_class(by["OIL"], "listed") == "commodity_cfd"
    assert vehicle_cost_class(by["EURUSD"], "listed") == "fx_cfd"


def test_cost_model_numbers_come_from_costs_yaml(policy):
    lines = [ln for ln in policy.universe.lines if ln.in_reference]
    listed = cost_model(lines, policy, mode="listed", commission_nav=5_000.0)
    assert listed.per_side["NDX"] == pytest.approx(5e-4) and listed.fixed["NDX"] == pytest.approx(1 / 5_000)
    assert listed.per_side["BTC"] == pytest.approx(0.01) and listed.fixed["BTC"] == 0.0
    cfd = cost_model(lines, policy, mode="cfd", slippage_bps=2)
    assert cfd.per_side["SPX"] == pytest.approx(17e-4) and cfd.fixed["SPX"] == 0.0
    ctrl = control_cost_model(["SPY"], policy, commission_nav=10_000.0)
    assert ctrl.per_side["SPY"] == pytest.approx(5e-4) and ctrl.fixed["SPY"] == pytest.approx(1e-4)
    with pytest.raises(ValueError):
        cost_model(lines, policy, commission_nav=0.0)


# ------------------------------------------------------------------------------------ deadband


def test_drift_inside_the_deadband_does_not_trade():
    plan = _plan([0.5] * 10, thresholds=[0.05] * 10)
    rets = [0, 0, 0, 0.10, 0, 0, 0, 0, 0, 0]  # weight drifts to 0.55/1.05 ~ 0.524
    sim = simulate(plan, _returns(plan, rets), _costs(per_side=0.001))
    assert (sim.trades["X"].abs() > 0).sum() == 1  # entry only
    assert sim.weights["X"].iloc[-1] == pytest.approx(0.55 / 1.05)


def test_drift_beyond_the_deadband_trades_back_to_target():
    plan = _plan([0.5] * 10, thresholds=[0.05] * 10)
    rets = [0, 0, 0, 0.30, 0, 0, 0, 0, 0, 0]  # weight drifts to 0.65/1.15 ~ 0.565
    sim = simulate(plan, _returns(plan, rets), _costs(per_side=0.001))
    assert sim.orders["X"].iloc[3] == 0.5
    assert sim.trades["X"].iloc[4] == pytest.approx(0.5 - 0.65 / 1.15)
    assert sim.weights["X"].iloc[4] == 0.5


def test_a_level_change_trades_even_inside_the_deadband():
    targets = [0.5] * 5 + [0.501] * 5
    plan = _plan(targets, [1.0] * 5 + [0.75] * 5, thresholds=[0.05] * 10)
    sim = simulate(plan, _returns(plan, [0.0] * 10), _costs())
    assert sim.trades["X"].iloc[6] == pytest.approx(0.001)
    no_level_change = _plan(targets, [1.0] * 10, thresholds=[0.05] * 10)
    sim2 = simulate(no_level_change, _returns(no_level_change, [0.0] * 10), _costs())
    assert (sim2.trades["X"].abs() > 0).sum() == 1


def test_reference_thresholds_follow_the_deadband_policy(policy):
    closes = _synthetic_lines(end=date(2019, 3, 29))
    lines = [ln for ln in policy.universe.lines if ln.in_reference]
    cal = pd.DatetimeIndex(sorted(set(closes["NDX"].index)))
    panel = build_panel(closes, lines, policy, cal)
    plan, detail = reference_plan(panel, lines, policy, start=date(2019, 1, 2))
    for line in lines:
        mult = 0.5 if line.asset_class == "crypto" else 0.25
        expected = np.maximum(mult * detail.units[line.symbol], 0.02)
        np.testing.assert_allclose(plan.thresholds[line.symbol], expected)


# ------------------------------------------------------------------------------------ controls + run


def _control_returns(values, start="2021-01-04"):
    idx = pd.bdate_range(start, periods=len(values))
    return pd.DataFrame({"SPY": values}, index=idx, dtype=float)


def test_buy_and_hold_trades_once_and_tracks_the_price():
    rng = np.random.default_rng(2)
    rets = np.concatenate([[np.nan], rng.standard_normal(80) * 0.01])
    returns = _control_returns(rets)
    plan = fixed_mix_plan(returns, {"SPY": 1.0}, start=returns.index[0], name="spy_bh", rebalance="never")
    sim = simulate(plan, returns, CostModel(per_side={"SPY": 5e-4}, fixed={"SPY": 1e-4}, classes={"SPY": "etf_real"}))
    traded = sim.trades["SPY"].abs() > 0
    assert traded.sum() == 1 and traded.iloc[2]      # prices seen at row 1, bought at close 2
    growth = float(np.prod(1 + rets[3:]))
    assert sim.nav.iloc[-1] == pytest.approx((1 - 5e-4 - 1e-4) * growth)


def test_sixty_forty_rebalances_only_after_month_ends(policy):
    idx = pd.bdate_range("2021-01-04", "2021-06-30")
    rng = np.random.default_rng(4)
    returns = pd.DataFrame({"SPY": rng.standard_normal(len(idx)) * 0.01, "IEF": rng.standard_normal(len(idx)) * 0.003},
                           index=idx)
    plan = fixed_mix_plan(returns, {"SPY": 0.6, "IEF": 0.4}, start=idx[0], name="sixty_forty", rebalance="monthly")
    sim = simulate(plan, returns, control_cost_model(["SPY", "IEF"], policy))
    trade_days = sim.trades.index[(sim.trades.abs() > 0).any(axis=1)]
    month_ends = idx[np.append(idx.month[1:] != idx.month[:-1], True)]
    allowed = {idx[1]} | {idx[idx.get_loc(d) + 1] for d in month_ends if idx.get_loc(d) + 1 < len(idx)}
    assert set(trade_days) <= allowed and len(trade_days) >= 5
    assert sim.weights.loc[trade_days[-1]].to_dict() == pytest.approx({"SPY": 0.6, "IEF": 0.4})


def test_asof_alignment_respects_the_staleness_tolerance():
    s = pd.Series([1.0, 2.0], index=pd.to_datetime(["2021-01-04", "2021-01-20"]))
    cal = pd.bdate_range("2021-01-04", "2021-01-20")
    out = asof_align(s, cal)
    assert out.loc["2021-01-08"] == 1.0 and np.isnan(out.loc["2021-01-11"]) and out.loc["2021-01-20"] == 2.0


def test_static_plan_holds_base_weights_and_cash_before_crypto_starts(policy):
    closes = _synthetic_lines(start=date(2016, 1, 4), end=date(2018, 3, 30), crypto_start=date(2017, 8, 17))
    lines = [ln for ln in policy.universe.lines if ln.in_reference]
    cal = pd.DatetimeIndex(sorted(set(closes["NDX"].index)))
    panel = build_panel(closes, lines, policy, cal)
    plan = static_plan(panel, lines, policy, start=date(2017, 6, 1))
    assert plan.targets.loc["2017-07-03", "BTC"] == 0.0
    assert plan.targets.loc["2017-09-01", "BTC"] == pytest.approx(0.13)
    assert plan.targets.loc["2017-09-01"].sum() == pytest.approx(0.95)
    assert plan.targets.loc["2017-07-03"].sum() == pytest.approx(0.95 - 0.18)


def test_full_run_books_are_long_only_unlevered_and_crypto_waits_in_cash(policy):
    closes = _synthetic_lines(start=date(2016, 1, 4), end=date(2019, 6, 28), crypto_start=date(2017, 8, 17))
    controls = synthetic_closes(["SPY", "QQQ", "IEF"], start=date(2016, 1, 4), end=date(2019, 6, 28), seed=8)
    run = run_backtest(closes, controls, policy, BacktestConfig(start=date(2017, 9, 1), end=date(2019, 6, 28)))
    assert set(run.books) == {"reference", "reference_cfd", "no_trend", "static", "spy_bh", "qqq_bh", "sixty_forty"}
    targets = run.plan.targets
    assert (targets >= 0).all().all() and (targets.sum(axis=1) <= 0.95 + 1e-9).all()
    # BTC has no trend state until ~200 daily closes after its start: its weight sits in cash
    no_trend_days = run.panel.trend.loc[targets.index, "BTC"].isna()
    assert no_trend_days.any() and (targets.loc[no_trend_days, "BTC"] == 0).all()
    assert (run.books["reference"].weights.loc[no_trend_days.index[no_trend_days], "BTC"] == 0).all()
    assert (targets.loc[~no_trend_days, "BTC"] > 0).any()
    # the CFD sensitivity trades the same path at a different cost
    pd.testing.assert_frame_equal(run.books["reference"].trades, run.books["reference_cfd"].trades,
                                  check_exact=False, atol=1e-3)
    assert run.books["reference_cfd"].cost_classes["NDX"] == "etf_cfd"
    assert run.data_start["BTC"] == date(2017, 8, 17)


def test_no_trend_control_sets_every_known_trend_to_level_one(policy):
    closes = _synthetic_lines(end=date(2018, 6, 29))
    lines = [ln for ln in policy.universe.lines if ln.in_reference]
    cal = pd.DatetimeIndex(sorted(set(closes["NDX"].index)))
    panel = build_panel(closes, lines, policy, cal)
    plan, _ = reference_plan(panel, lines, policy, start=date(2018, 1, 2), force_up=True)
    real, _ = reference_plan(panel, lines, policy, start=date(2018, 1, 2))
    known = panel.trend.loc[plan.levels.index].notna()
    assert (plan.levels[known] == 1.0).sum().sum() == known.sum().sum()
    assert (plan.levels[~known].fillna(0.0) == 0.0).all().all()
    assert (real.levels[known] < 1.0).any().any()  # the real book does use lower levels
