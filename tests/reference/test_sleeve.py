"""council.reference.sleeve: the reference sleeve's trading rule, shared by the pre-registered
stock-sleeve study and the live reference book.

The equivalence tests pin the move: the study's simulator, now deciding its orders through
`pending_trades`, equals the pre-move inline loop (copied below verbatim) on seeded random inputs,
and equals `council.reference.backtest.simulate` when no line is budgeted."""

from __future__ import annotations

import math
import sys

import numpy as np
import pandas as pd
import pytest

from council.paths import REPO_ROOT
from council.policy import Policy
from council.reference import sleeve
from council.reference.backtest import BookPlan, CostModel, SimResult, asof_align, simulate
from council.reference.signals import line_signals

_EPS = 1e-12


@pytest.fixture(scope="module")
def study():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import stock_sleeve_study

    return stock_sleeve_study


def _legacy_simulate_budgeted(plan: BookPlan, returns: pd.DataFrame, costs: CostModel, *,
                              sleeve_cols=(), sleeve_budget: float = math.inf) -> SimResult:
    """scripts/stock_sleeve_study.py::simulate_budgeted as pre-registered before the move (inline
    order decision), kept verbatim as the reference for the equivalence test."""
    idx = plan.targets.index
    syms = list(plan.targets.columns)
    n, m = len(idx), len(syms)
    rets = returns.reindex(index=idx, columns=syms).to_numpy(dtype=float)
    rets = np.where(np.isfinite(rets), rets, 0.0)
    tgt = np.nan_to_num(plan.targets.to_numpy(dtype=float), nan=0.0)
    lvl = np.nan_to_num(plan.levels.to_numpy(dtype=float), nan=0.0)
    thr = np.nan_to_num(plan.thresholds.to_numpy(dtype=float), nan=np.inf)
    per_side = np.array([costs.per_side[s] for s in syms], dtype=float)
    fixed = np.array([costs.fixed[s] for s in syms], dtype=float)
    sleeve_mask = np.array([s in set(sleeve_cols) for s in syms], dtype=bool)

    held = np.zeros(m)
    held_level = np.zeros(m)
    nav = 1.0
    pending = np.zeros(m, dtype=bool)
    pend_target = np.zeros(m)
    pend_level = np.zeros(m)
    nav_out = np.empty(n)
    w_out = np.empty((n, m))
    dw_out = np.zeros((n, m))
    cost_out = np.zeros(n)
    orders_out = np.full((n, m), np.nan)
    for i in range(n):
        if i > 0:
            growth = 1.0 + float(held @ rets[i])
            if growth <= 0.0:
                raise ValueError(f"{plan.name}: book wiped out on {idx[i]}")
            held = held * (1.0 + rets[i]) / growth
            nav *= growth
        if pending.any():
            new = held.copy()
            new[pending] = pend_target[pending]
            dw = new - held
            traded = np.abs(dw) > _EPS
            cost = float(np.abs(dw) @ per_side + fixed[traded].sum())
            held = new
            held_level[pending] = pend_level[pending]
            nav *= 1.0 - cost
            dw_out[i], cost_out[i] = dw, cost
        level_change = ~np.isclose(lvl[i], held_level, rtol=0.0, atol=1e-12)
        drift = np.abs(tgt[i] - held)
        pending = level_change | ((drift >= thr[i]) & (drift > _EPS))
        if sleeve_mask.any():
            projected = np.where(pending, tgt[i], held)
            if projected[sleeve_mask].sum() > sleeve_budget + 1e-9:
                pending = pending | (sleeve_mask & (held > tgt[i] + _EPS))
        pend_target, pend_level = tgt[i].copy(), lvl[i].copy()
        orders_out[i, pending] = pend_target[pending]
        nav_out[i], w_out[i] = nav, held
    nav_s = pd.Series(nav_out, index=idx, name=plan.name)
    return SimResult(
        name=plan.name, nav=nav_s, returns=nav_s.pct_change().fillna(0.0),
        weights=pd.DataFrame(w_out, index=idx, columns=syms),
        trades=pd.DataFrame(dw_out, index=idx, columns=syms),
        costs=pd.Series(cost_out, index=idx, name="cost"),
        orders=pd.DataFrame(orders_out, index=idx, columns=syms), cost_model=costs,
    )


def _legacy_stops_decision(held, held_level, t, lv, thr, budget):
    """The order decision of scripts/stock_sleeve_study.py::simulate_with_stops before the move."""
    level_change = ~np.isclose(lv, held_level, rtol=0.0, atol=1e-12)
    drift = np.abs(t - held)
    pending = level_change | ((drift >= thr) & (drift > _EPS))
    projected = np.where(pending, t, held)
    if projected.sum() > budget + 1e-9:
        pending = pending | (held > t + _EPS)
    return pending


def _random_sleeve_book(seed: int, *, rows: int = 260, names: int = 14, core: int = 3):
    """A core plus a quarterly equal-weight sleeve with an overlay level that steps, random returns
    and costs: exercises level changes, drift trades and the never-borrow trim."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2021-01-04", periods=rows)
    stocks = [f"S{j:02d}" for j in range(names)]
    lines = [f"C{j}" for j in range(core)]
    n = 5
    unit = sleeve.unit_weight(0.5, n)
    level = np.ones(rows)
    for a in range(0, rows, 40):
        level[a:a + 40] = rng.choice([1.0, 0.75, 0.25])
    t_s = np.zeros((rows, names))
    l_s = np.zeros((rows, names))
    for a in range(0, rows, 63):
        pick = rng.choice(names, size=n, replace=False)
        t_s[a:a + 63, pick] = unit * level[a:a + 63, None]
        l_s[a:a + 63, pick] = level[a:a + 63, None]
    base = rng.uniform(0.05, 0.2, core)
    t_c = np.tile(base, (rows, 1)) * rng.choice([1.0, 0.5], size=(rows, 1))
    targets = pd.DataFrame(np.hstack([t_c, t_s]), index=idx, columns=lines + stocks)
    levels = pd.DataFrame(np.hstack([(t_c > 0) * 1.0, l_s]), index=idx, columns=lines + stocks)
    thr = np.full(targets.shape, sleeve.drift_threshold(unit, 0.25, 0.02))
    thr[:, :core] = rng.uniform(0.005, 0.03, core)
    plan = BookPlan(name="book", targets=targets, levels=levels,
                    thresholds=pd.DataFrame(thr, index=idx, columns=lines + stocks))
    rets = pd.DataFrame(rng.normal(0.0004, 0.02, targets.shape), index=idx, columns=lines + stocks)
    rets.iloc[rng.integers(0, rows, 5), rng.integers(0, names + core, 5)] = np.nan   # missing bars
    costs = CostModel(per_side=dict(zip(lines + stocks, rng.uniform(0.0, 0.003, core + names), strict=True)),
                      fixed=dict(zip(lines + stocks, rng.uniform(0.0, 0.001, core + names), strict=True)),
                      classes=dict.fromkeys(lines + stocks, "x"))
    return plan, rets, costs, stocks


def _assert_same(a: SimResult, b: SimResult) -> None:
    pd.testing.assert_series_equal(a.nav, b.nav, check_exact=True)
    pd.testing.assert_frame_equal(a.weights, b.weights, check_exact=True)
    pd.testing.assert_frame_equal(a.trades, b.trades, check_exact=True)
    pd.testing.assert_series_equal(a.costs, b.costs, check_exact=True)
    pd.testing.assert_frame_equal(a.orders, b.orders, check_exact=True)


# ------------------------------------------------------------------------------------ the rule


def test_unit_weight_is_the_equal_share():
    assert sleeve.unit_weight(0.5, 10) == 0.05
    assert sleeve.unit_weight(0.5, 8) == 0.0625
    assert sleeve.unit_weight(1.0, 8) == 0.125


def test_overlay_level_maps_the_spx_trend_state():
    down_only = {"up": 1.0, "mixed": 1.0, "down": 0.25}
    reference = {"up": 1.0, "mixed": 0.75, "down": 0.25}
    assert [sleeve.overlay_level(reference, s) for s in ("up", "mixed", "down")] == [1.0, 0.75, 0.25]
    assert sleeve.overlay_level(down_only, "mixed") == 1.0
    assert sleeve.overlay_level(reference, None) == 1.0           # no trend state yet: full level
    assert sleeve.overlay_level(reference, float("nan")) == 1.0   # as the backtest's missing state


def test_sleeve_targets_give_selected_names_unit_times_level_and_others_zero():
    t = sleeve.sleeve_targets(["A", "B"], 0.5, 10, 0.25, lines=["A", "B", "C"])
    assert t == {"A": 0.05 * 0.25, "B": 0.05 * 0.25, "C": 0.0}
    assert sleeve.sleeve_targets(["A"], 0.5, 8, 1.0) == {"A": 0.0625}   # fewer names: the rest stays cash
    with pytest.raises(ValueError, match="slots"):
        sleeve.sleeve_targets(list("ABC"), 0.5, 2, 1.0)
    with pytest.raises(ValueError, match="twice"):
        sleeve.sleeve_targets(["A", "A"], 0.5, 2, 1.0)


def test_drift_threshold_is_the_larger_of_the_deadband_and_the_floor():
    assert sleeve.drift_threshold(0.05, 0.25, 0.02) == 0.02             # N = 10: the 2% NAV floor binds
    assert sleeve.drift_threshold(0.125, 0.25, 0.02) == 0.03125
    assert sleeve.drift_threshold(1.0 / 8, 0.25, 0.04) == 0.04         # stand-alone: 2% of NAV = 4% of the sleeve


def test_a_level_change_always_trades_and_a_small_drift_does_not():
    held = np.array([0.05, 0.05, 0.05])
    pending = sleeve.pending_trades(held, np.array([1.0, 1.0, 1.0]), np.array([0.05, 0.0125, 0.06]),
                                    np.array([1.0, 0.25, 1.0]), 0.02)
    assert pending.tolist() == [False, True, False]                   # 0.01 drift < 0.02; level 1 -> 0.25


def test_a_drift_at_the_threshold_trades():
    pending = sleeve.pending_trades(np.array([0.07, 0.03]), np.ones(2), np.array([0.05, 0.05]), np.ones(2), 0.02)
    assert pending.tolist() == [True, True]


def test_never_borrow_trims_every_budgeted_line_above_target():
    held = np.array([0.30, 0.30, 0.10, 0.0])           # C drifted up; D is new
    target = np.array([0.25, 0.25, 0.25, 0.25])
    level = np.ones(4)
    held_level = np.array([1.0, 1.0, 1.0, 0.0])
    mask = np.array([True, True, True, True])
    loose = sleeve.pending_trades(held, held_level, target, level, 0.1)
    assert loose.tolist() == [False, False, True, True]               # buying C and D alone would borrow
    tight = sleeve.pending_trades(held, held_level, target, level, 0.1, budget_mask=mask, budget=1.0)
    assert tight.tolist() == [True, True, True, True]
    core = np.array([False, True, True, True])                        # an unbudgeted line is never trimmed
    part = sleeve.pending_trades(held, held_level, target, level, 0.1, budget_mask=core, budget=0.70)
    assert part.tolist() == [False, True, True, True]


def test_pending_trades_equals_the_legacy_stop_decision_on_random_states():
    rng = np.random.default_rng(7)
    for _ in range(500):
        m = int(rng.integers(1, 12))
        held = rng.uniform(0.0, 0.3, m) * (rng.random(m) > 0.3)
        t = rng.uniform(0.0, 0.3, m) * (rng.random(m) > 0.3)
        lv = rng.choice([0.0, 0.25, 0.75, 1.0], m)
        hl = np.where(rng.random(m) > 0.2, lv, rng.choice([0.0, 0.25, 1.0], m))
        thr = rng.choice([0.02, 0.04, np.inf], m)
        budget = float(rng.choice([1.0, 0.8, math.inf]))
        got = sleeve.pending_trades(held, hl, t, lv, thr, budget_mask=np.ones(m, dtype=bool), budget=budget)
        assert got.tolist() == _legacy_stops_decision(held, hl, t, lv, thr, budget).tolist()


# ------------------------------------------------------------------------------------ equivalence with the study


@pytest.mark.parametrize("seed", range(6))
def test_the_study_simulator_equals_the_pre_move_loop(study, seed):
    plan, rets, costs, stocks = _random_sleeve_book(seed)
    for budget in (0.5, 0.35, math.inf):
        got = study.simulate_budgeted(plan, rets, costs, sleeve_cols=stocks, sleeve_budget=budget)
        want = _legacy_simulate_budgeted(plan, rets, costs, sleeve_cols=stocks, sleeve_budget=budget)
        _assert_same(got, want)
    trims = study.simulate_budgeted(plan, rets, costs, sleeve_cols=stocks, sleeve_budget=0.35)
    free = study.simulate_budgeted(plan, rets, costs, sleeve_cols=stocks, sleeve_budget=math.inf)
    assert not trims.orders.equals(free.orders)                        # the budget binds on these inputs


@pytest.mark.parametrize("seed", range(4))
def test_the_study_simulator_is_the_reference_simulation_without_a_sleeve(study, seed):
    plan, rets, costs, _ = _random_sleeve_book(seed)
    _assert_same(study.simulate_budgeted(plan, rets, costs), simulate(plan, rets, costs))


def test_the_study_simulator_is_the_reference_simulation_on_a_plain_book(study):
    rng = np.random.default_rng(4)
    rows = pd.bdate_range("2020-01-01", periods=120)
    cols = ["A", "B", "C"]
    targets = np.tile([0.3, 0.3, 0.3], (120, 1))
    targets[60:, 2] = 0.0
    t = pd.DataFrame(targets, index=rows, columns=cols)
    plan = BookPlan(name="p", targets=t, levels=(t > 0).astype(float),
                    thresholds=pd.DataFrame(0.02, index=rows, columns=cols))
    rets = pd.DataFrame(rng.normal(0, 0.02, (120, 3)), index=rows, columns=cols)
    costs = study.flat_costs(cols, 0.002, 0.001, "x")
    _assert_same(simulate(plan, rets, costs), study.simulate_budgeted(plan, rets, costs))


def test_a_rebalance_never_borrows(study):
    rows = pd.bdate_range("2020-01-01", periods=30)
    cols = ["A", "B", "C"]
    targets = np.zeros((30, 3))
    targets[:, :2] = 0.5
    targets[10:, 2] = 0.5                            # C enters at day 10 while A, B go to 0.25
    targets[10:, :2] = 0.25
    t = pd.DataFrame(targets, index=rows, columns=cols)
    plan = BookPlan(name="p", targets=t, levels=pd.DataFrame(np.where(targets > 0, 1.0, 0.0), index=rows,
                                                               columns=cols),
                    thresholds=pd.DataFrame(np.inf, index=rows, columns=cols))
    rets = pd.DataFrame(0.0, index=rows, columns=cols)
    costs = study.flat_costs(cols, 0.0, 0.0, "x")
    loose = simulate(plan, rets, costs)
    assert loose.weights.sum(axis=1).max() > 1.0 + 1e-9             # the plain rule would borrow
    tight = study.simulate_budgeted(plan, rets, costs, sleeve_cols=cols, sleeve_budget=1.0)
    assert tight.weights.sum(axis=1).max() <= 1.0 + 1e-9
    assert tight.weights.iloc[-1].tolist() == pytest.approx([0.25, 0.25, 0.5])


def test_sleeve_plan_targets_levels_and_thresholds(study):
    rows = pd.bdate_range("2020-01-01", periods=20)
    decisions = {rows[0]: ["A", "B"], rows[10]: ["B", "C"]}
    level = pd.Series(1.0, index=rows)
    level.iloc[15:] = 0.25
    plan = study.sleeve_plan(rows, decisions, share=1.0, n=2, level=level, deadband_level=0.25, min_share=0.04,
                             name="s")
    assert plan.targets.loc[rows[5]].to_dict() == {"A": 0.5, "B": 0.5, "C": 0.0}
    assert plan.targets.loc[rows[12]].to_dict() == {"A": 0.0, "B": 0.5, "C": 0.5}
    assert plan.targets.loc[rows[16], "C"] == pytest.approx(0.125)
    assert plan.levels.loc[rows[16], "B"] == 0.25
    assert (plan.thresholds == max(0.25 * 0.5, 0.04)).all().all()


def test_the_studied_plan_rows_are_the_live_targets(study):
    """Every row of the study's sleeve plan equals `sleeve_targets` for that row's names and level,
    and its thresholds equal `drift_threshold`: the live book gets the targets the study traded."""
    rng = np.random.default_rng(3)
    rows = pd.bdate_range("2021-01-04", periods=200)
    names = [f"S{j}" for j in range(15)]
    decisions = {rows[a]: sorted(rng.choice(names, size=8, replace=False).tolist()) for a in (0, 63, 126, 189)}
    level = pd.Series(rng.choice([1.0, 0.75, 0.25], len(rows)), index=rows)
    for share, n in ((0.5, 8), (1.0, 8), (0.5, 10)):
        plan = study.sleeve_plan(rows, decisions, share=share, n=n, level=level, deadband_level=0.25,
                                 min_share=0.02, name="s")
        dates = sorted(decisions)
        for i, row in enumerate(rows):
            d = max(x for x in dates if x <= row)
            want = sleeve.sleeve_targets(decisions[d], share, n, float(level.iloc[i]), lines=plan.targets.columns)
            assert plan.targets.loc[row].to_dict() == want
        assert (plan.thresholds == sleeve.drift_threshold(sleeve.unit_weight(share, n), 0.25, 0.02)).all().all()


def test_the_studied_overlay_level_is_the_live_mapping(study):
    pol = Policy.load()
    rng = np.random.default_rng(5)
    idx = pd.bdate_range("2015-01-02", periods=700)
    spy = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, len(idx)))), index=idx)
    trend = asof_align(line_signals(spy, asset_class="index", policy=pol)["trend"], idx)
    for table in ({"up": 1.0, "mixed": 1.0, "down": 0.25}, {"up": 1.0, "mixed": 0.75, "down": 0.25}):
        got = study.level_from(spy, idx, table, pol)
        want = [sleeve.overlay_level(table, s if isinstance(s, str) else None) for s in trend]
        assert got.tolist() == want
        assert set(trend.dropna()) <= set(table)


@pytest.mark.parametrize(("drift", "kept_trades"), [(-0.004, False), (0.004, True)])
def test_at_a_rebalance_only_added_and_dropped_names_are_forced_to_trade(study, drift, kept_trades):
    """Spec section 6: at D a kept name is not reset to its equal weight. It trades only past the
    deadband, or when the never-borrow rule trims it because the orders would lift the sleeve above
    its budget; added and dropped names trade through their level change."""
    rows = pd.bdate_range("2021-01-04", periods=60)
    decisions = {rows[0]: ["A", "B"], rows[30]: ["A", "C"]}        # A kept, B dropped, C added
    plan = study.sleeve_plan(rows, decisions, share=1.0, n=2, level=None, deadband_level=0.25, min_share=0.04,
                             name="s")
    rets = pd.DataFrame(0.0, index=rows, columns=plan.targets.columns)
    rets.loc[rows[1]:rows[29], "A"] = drift                         # inside the deadband (0.125 of the sleeve)
    sim = study.simulate_budgeted(plan, rets, study.flat_costs(list(plan.targets.columns), 0.0, 0.0, "x"),
                                  sleeve_cols=list(plan.targets.columns), sleeve_budget=1.0)
    executed = sim.trades.loc[rows[31]]                              # the close after D
    assert executed["B"] < 0 and executed["C"] > 0
    assert bool(abs(executed["A"]) > 1e-12) == kept_trades          # only the never-borrow trim moves A
    assert sim.trades.loc[rows[2]:rows[30]].abs().to_numpy().max() < 1e-12   # no drift trade before D
    assert sim.weights.sum(axis=1).max() <= 1.0 + 1e-9


def test_the_stock_line_cap_cannot_bind_on_the_rule_path(study):
    """A rule name sits at most one unit plus the deadband above zero before it trades back, well under
    the 0.10 single-stock cap the live engine will carry: the cap never alters the studied rule."""
    spec = study.load_spec()
    db = spec["sleeve"]["deadband"]
    for n in spec["n_names"]:
        unit = sleeve.unit_weight(float(spec["sleeve"]["share_of_nav"]), int(n))
        assert unit + sleeve.drift_threshold(unit, float(db["level"]), float(db["min_nav_share"])) < 0.10
