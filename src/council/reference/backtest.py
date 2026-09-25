"""Daily mechanical backtest of the reference book and its controls.

Timing (the one-day lag is explicit and tested):
- A decision at close t uses only data at or before t: signals, covariance and held weights.
- The decision is executed at close t+1. The return from close t to close t+1 is earned by the
  weights held after close t, so the signal day's next return can never be captured.

Trading (reference and composition controls):
- The book is rebuilt every day but a line trades only when its level changes (a trend flip, or
  data arriving) or when |target - held| >= max(deadband level x unit weight, min NAV share)
  [risk.deadband: level 0.25, crypto 0.5, min_nav_share 0.02]. A traded line goes to its target.

Costs per side on the traded weight, by vehicle class [costs.per_side_bps]:
- crypto 100 bps; real UCITS/ETC = etf_real plus the fixed commission expressed as a fraction of a
  configurable NAV, charged once per traded leg; 1x CFD fallback = etf_cfd (ETF CFDs) or the line's
  class CFD rate. No carry at 1x. No borrowing, cash earns 0 (Sharpe with rf 0).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from council.models.facts import MarketState
from council.policy import LineSpec, Policy
from council.reference.book import COVARIANCE_LOOKBACK_SPANS, apply_caps, build_reference
from council.reference.signals import TREND_STATES, line_signals

VehicleMode = Literal["listed", "cfd"]
ASOF_TOLERANCE = pd.Timedelta(days=5)   # a close older than this is not used as "latest"
_EPS = 1e-12
_CFD_CLASS = {"index": "index_cfd", "etf": "etf_cfd", "stock": "etf_cfd", "commodity": "commodity_cfd", "fx": "fx_cfd"}


# ------------------------------------------------------------------------------------ costs


@dataclass(frozen=True)
class CostModel:
    """Per column: `per_side` fraction of traded weight, `fixed` fraction of NAV per traded leg."""

    per_side: dict[str, float]
    fixed: dict[str, float]
    classes: dict[str, str]


def vehicle_cost_class(line: LineSpec, mode: VehicleMode) -> str:
    """Cost class of the vehicle a line trades.

    Rule: crypto -> crypto. `listed` takes the first long candidate: real -> etf_real, a CFD ->
    the CFD rate. `cfd` takes the first CFD long candidate. A CFD on the line's own signal ETF
    (e.g. QQQ) is an ETF CFD (etf_cfd); other CFDs use the line's class rate (index_cfd, ...)."""
    if line.asset_class == "crypto":
        return "crypto"
    candidates = list(line.vehicles.long)
    if mode == "listed" and candidates and candidates[0].settlement == "real":
        return "etf_real"
    cfds = [v for v in candidates if v.settlement == "cfd"]
    if cfds and cfds[0].symbol == line.signal.ticker:
        return "etf_cfd"
    return _CFD_CLASS[line.asset_class]


def _class_costs(policy: Policy, cls: str, commission_nav: float, slippage_bps: float) -> tuple[float, float]:
    per_side_bps = policy.costs["per_side_bps"]
    per_side = (float(per_side_bps[cls]) + slippage_bps) / 1e4
    fixed = 0.0
    if cls == "etf_real":
        if commission_nav <= 0:
            raise ValueError("commission_nav must be positive")
        fixed = float(policy.costs["fixed_commission_usd"]["real"]) / commission_nav
    return per_side, fixed


def cost_model(
    lines: Sequence[LineSpec],
    policy: Policy,
    *,
    mode: VehicleMode = "listed",
    commission_nav: float = 10_000.0,
    slippage_bps: float = 0.0,
) -> CostModel:
    """Costs for the reference lines under a vehicle mode (see `vehicle_cost_class`)."""
    per_side, fixed, classes = {}, {}, {}
    for line in lines:
        cls = vehicle_cost_class(line, mode)
        per_side[line.symbol], fixed[line.symbol] = _class_costs(policy, cls, commission_nav, slippage_bps)
        classes[line.symbol] = cls
    return CostModel(per_side=per_side, fixed=fixed, classes=classes)


def control_cost_model(
    tickers: Sequence[str],
    policy: Policy,
    *,
    mode: VehicleMode = "listed",
    commission_nav: float = 10_000.0,
    slippage_bps: float = 0.0,
) -> CostModel:
    """Controls are US ETFs held via a real UCITS equivalent (`listed`) or a 1x ETF CFD (`cfd`)."""
    cls = "etf_real" if mode == "listed" else "etf_cfd"
    ps, fx = _class_costs(policy, cls, commission_nav, slippage_bps)
    return CostModel(
        per_side={t: ps for t in tickers}, fixed={t: fx for t in tickers}, classes={t: cls for t in tickers}
    )


# ------------------------------------------------------------------------------------ data panel


def asof_align(series: pd.Series, calendar: pd.DatetimeIndex) -> pd.Series:
    """Latest value at or before each calendar date, at most ASOF_TOLERANCE old; NaN otherwise."""
    s = series[~series.index.duplicated(keep="last")].sort_index()
    return s.reindex(calendar, method="ffill", tolerance=ASOF_TOLERANCE)


@dataclass(frozen=True)
class Panel:
    """Per-line data on the backtest calendar; row t holds only information available at close t."""

    closes: pd.DataFrame
    returns: pd.DataFrame
    trend: pd.DataFrame        # object dtype: "up" / "mixed" / "down" / None
    sigma_ann: pd.DataFrame
    vol_ratio: pd.DataFrame

    @property
    def calendar(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.closes.index)


def _clean_trend(values: pd.Series) -> pd.Series:
    return pd.Series(
        [v if v in TREND_STATES else None for v in values], index=values.index, dtype=object
    )


def aligned_returns(closes: Mapping[str, pd.Series], calendar: pd.DatetimeIndex) -> pd.DataFrame:
    """Simple close-to-close returns on the calendar from as-of aligned closes."""
    frame = pd.DataFrame({k: asof_align(v.astype(float), calendar) for k, v in closes.items()}, index=calendar)
    return frame.pct_change(fill_method=None)


def build_panel(
    closes: Mapping[str, pd.Series],
    lines: Sequence[LineSpec],
    policy: Policy,
    calendar: pd.DatetimeIndex,
) -> Panel:
    """Signals are computed on each line's own calendar (crypto trades every day), then aligned
    as-of to the backtest calendar. Lines without data get an all-missing column."""
    cl, tr, sg, vr = {}, {}, {}, {}
    for line in lines:
        series = closes.get(line.symbol)
        if series is None or series.dropna().empty:
            cl[line.symbol] = pd.Series(np.nan, index=calendar)
            tr[line.symbol] = pd.Series([None] * len(calendar), index=calendar, dtype=object)
            sg[line.symbol] = pd.Series(np.nan, index=calendar)
            vr[line.symbol] = pd.Series(np.nan, index=calendar)
            continue
        native = series.astype(float).dropna().sort_index()
        sig = line_signals(native, asset_class=line.asset_class, policy=policy)
        cl[line.symbol] = asof_align(native, calendar)
        tr[line.symbol] = _clean_trend(asof_align(sig["trend"], calendar))
        sg[line.symbol] = asof_align(sig["sigma_ann"], calendar)
        vr[line.symbol] = asof_align(sig["vol_ratio_1y"], calendar)
    closes_df = pd.DataFrame(cl, index=calendar)
    return Panel(
        closes=closes_df,
        returns=closes_df.pct_change(fill_method=None),
        trend=pd.DataFrame(tr, index=calendar, dtype=object),
        sigma_ann=pd.DataFrame(sg, index=calendar),
        vol_ratio=pd.DataFrame(vr, index=calendar),
    )


# ------------------------------------------------------------------------------------ book plans


@dataclass(frozen=True)
class BookPlan:
    """What a book decides at each close t (from data <= t), before any execution.

    targets: weight of NAV per column. levels: a change from the held level forces a trade.
    thresholds: |target - held| >= threshold forces a trade (inf = never on drift)."""

    name: str
    targets: pd.DataFrame
    levels: pd.DataFrame
    thresholds: pd.DataFrame


@dataclass(frozen=True)
class ReferenceDetail:
    units: pd.DataFrame
    ex_ante_vol: pd.Series
    k: pd.Series
    truncation_days: dict[str, int] = field(default_factory=dict)


def truncation_kind(note: str) -> str:
    """A truncation note without its numbers, e.g. "book: gross above the limit" -> tally key."""
    head = note.split(",")[0].split("(")[0]
    return re.sub(r"\s+", " ", re.sub(r"-?[\d.]+%", "", head)).strip()


def _finite_or_none(x: float) -> float | None:
    return float(x) if x is not None and math.isfinite(float(x)) else None


def _deadband_levels(line: LineSpec, policy: Policy) -> float:
    db = policy.risk["deadband"]
    return float(db["level_crypto"] if line.asset_class == "crypto" else db["level"])


def _start_index(calendar: pd.DatetimeIndex, start: date | pd.Timestamp) -> int:
    i0 = int(calendar.searchsorted(pd.Timestamp(start)))
    if i0 >= len(calendar):
        raise ValueError(f"no calendar dates on or after {start}")
    return i0


def reference_plan(
    panel: Panel,
    lines: Sequence[LineSpec],
    policy: Policy,
    *,
    start: date | pd.Timestamp,
    name: str = "reference",
    force_up: bool = False,
) -> tuple[BookPlan, ReferenceDetail]:
    """Rebuild the reference book at every close from `start`, using only rows <= t.

    `force_up` is the no-trend control: every line with a trend state is treated as up (level 1.0),
    keeping the vol cap, scaling and caps, so the difference isolates the trend overlay."""
    cal = panel.calendar
    i0 = _start_index(cal, start)
    span = int(policy.reference["book"]["covariance_ewma_days"])
    lookback = COVARIANCE_LOOKBACK_SPANS * span
    min_share = float(policy.risk["deadband"]["min_nav_share"])
    syms = [line.symbol for line in lines]
    rows = cal[i0:]
    targets = np.zeros((len(rows), len(syms)))
    levels = np.zeros_like(targets)
    units = np.zeros_like(targets)
    thresholds = np.zeros_like(targets)
    vol = np.zeros(len(rows))
    ks = np.zeros(len(rows))
    trunc_days: dict[str, int] = {}
    trend_np = panel.trend.to_numpy(dtype=object)
    sigma_np = panel.sigma_ann.to_numpy(dtype=float)
    ratio_np = panel.vol_ratio.to_numpy(dtype=float)
    col = {s: panel.trend.columns.get_loc(s) for s in syms}
    for r, i in enumerate(range(i0, len(cal))):
        states: dict[str, MarketState] = {}
        for line in lines:
            j = col[line.symbol]
            trend = trend_np[i, j] if trend_np[i, j] in TREND_STATES else None
            if force_up and trend is not None:
                trend = "up"
            states[line.symbol] = MarketState(
                symbol=line.symbol,
                asset_class=line.asset_class,
                trend=trend,
                sigma_ann=_finite_or_none(sigma_np[i, j]),
                vol_ratio_1y=_finite_or_none(ratio_np[i, j]),
            )
        book = build_reference(
            cycle_id=f"bt-{cal[i]:%Y-%m-%d}",
            lines=lines,
            states=states,
            returns=panel.returns.iloc[max(0, i - lookback + 1) : i + 1],
            policy=policy,
        )
        for c, line in enumerate(lines):
            entry = book.entries[line.symbol]
            targets[r, c] = entry.weight_ref
            levels[r, c] = entry.level_ref
            units[r, c] = entry.unit_weight
            thresholds[r, c] = max(_deadband_levels(line, policy) * entry.unit_weight, min_share)
        vol[r], ks[r] = book.ex_ante_vol, book.k
        for key in {truncation_kind(note) for note in book.truncations}:
            trunc_days[key] = trunc_days.get(key, 0) + 1

    def frame(a: np.ndarray) -> pd.DataFrame:
        return pd.DataFrame(a, index=rows, columns=syms)

    plan = BookPlan(name=name, targets=frame(targets), levels=frame(levels), thresholds=frame(thresholds))
    detail = ReferenceDetail(
        units=frame(units),
        ex_ante_vol=pd.Series(vol, index=rows, name="ex_ante_vol"),
        k=pd.Series(ks, index=rows, name="k"),
        truncation_days=trunc_days,
    )
    return plan, detail


def static_plan(
    panel: Panel,
    lines: Sequence[LineSpec],
    policy: Policy,
    *,
    start: date | pd.Timestamp,
    name: str = "static",
) -> BookPlan:
    """The same in-reference lines held static at base weights (scaled to reference_gross_max,
    clipped to caps), with no trend and no vol cap; a line enters when its price history starts.
    Rebalanced by the same deadband rule as the reference."""
    cal = panel.calendar
    rows = cal[_start_index(cal, start) :]
    ref_lines = [line for line in lines if line.in_reference]
    total = sum(line.base_weight for line in ref_lines)
    k = min(1.0, float(policy.universe.reference_gross_max) / total) if total > 0 else 1.0
    base = apply_caps({line.symbol: line.base_weight * k for line in ref_lines}, ref_lines, policy)
    min_share = float(policy.risk["deadband"]["min_nav_share"])
    started = panel.closes.loc[rows, [ln.symbol for ln in ref_lines]].notna().to_numpy()
    syms = [ln.symbol for ln in ref_lines]
    w = np.array([base[s] for s in syms])
    th = np.array([max(_deadband_levels(ln, policy) * base[ln.symbol], min_share) for ln in ref_lines])
    return BookPlan(
        name=name,
        targets=pd.DataFrame(np.where(started, w, 0.0), index=rows, columns=syms),
        levels=pd.DataFrame(np.where(started, 1.0, 0.0), index=rows, columns=syms),
        thresholds=pd.DataFrame(np.broadcast_to(th, started.shape).copy(), index=rows, columns=syms),
    )


def fixed_mix_plan(
    returns: pd.DataFrame,
    mix: Mapping[str, float],
    *,
    start: date | pd.Timestamp,
    name: str,
    rebalance: Literal["never", "monthly"],
) -> BookPlan:
    """A constant mix of control tickers, entered once every ticker has a price.

    `never` = buy and hold (drift freely); `monthly` = back to the mix at each month's last close."""
    cal = pd.DatetimeIndex(returns.index)
    rows = cal[_start_index(cal, start) :]
    syms = list(mix)
    prices_seen = returns[syms].notna().cummax().loc[rows].all(axis=1).to_numpy()
    w = np.array([float(mix[s]) for s in syms])
    targets = np.where(prices_seen[:, None], w, 0.0)
    levels = np.where(prices_seen[:, None], 1.0, 0.0) * np.ones(len(syms))
    if rebalance == "monthly":
        month_end = np.append(rows.month[1:] != rows.month[:-1], True)
        th_row = np.where(month_end, 0.0, np.inf)
    else:
        th_row = np.full(len(rows), np.inf)
    thresholds = np.broadcast_to(th_row[:, None], targets.shape).copy()
    return BookPlan(
        name=name,
        targets=pd.DataFrame(targets, index=rows, columns=syms),
        levels=pd.DataFrame(levels, index=rows, columns=syms),
        thresholds=pd.DataFrame(thresholds, index=rows, columns=syms),
    )


# ------------------------------------------------------------------------------------ simulation


@dataclass(frozen=True)
class SimResult:
    """Execution path of one book. Weights are fractions of NAV after trading at close t; `orders`
    holds the target ordered at close t for execution at close t+1 (NaN = no order)."""

    name: str
    nav: pd.Series
    returns: pd.Series
    weights: pd.DataFrame
    trades: pd.DataFrame
    costs: pd.Series
    orders: pd.DataFrame
    cost_model: CostModel

    @property
    def cost_classes(self) -> dict[str, str]:
        return dict(self.cost_model.classes)

    @property
    def gross(self) -> pd.Series:
        return self.weights.abs().sum(axis=1).rename("gross")

    @property
    def turnover(self) -> pd.Series:
        return self.trades.abs().sum(axis=1).rename("turnover")


def simulate(plan: BookPlan, returns: pd.DataFrame, costs: CostModel) -> SimResult:
    """Execute a plan with a one-day lag.

    Per close t: (1) holdings from close t-1 earn the return t-1 -> t and drift; (2) the order
    decided at close t-1 executes: traded lines jump to their ordered target, paying per-side cost
    on |dw| plus the fixed cost per traded leg (fraction of NAV, paid from cash); (3) a new order is
    decided from the plan's row t and the post-trade holdings."""
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
        pend_target, pend_level = tgt[i].copy(), lvl[i].copy()
        orders_out[i, pending] = pend_target[pending]
        nav_out[i], w_out[i] = nav, held
    nav_s = pd.Series(nav_out, index=idx, name=plan.name)
    return SimResult(
        name=plan.name,
        nav=nav_s,
        returns=nav_s.pct_change().fillna(0.0),
        weights=pd.DataFrame(w_out, index=idx, columns=syms),
        trades=pd.DataFrame(dw_out, index=idx, columns=syms),
        costs=pd.Series(cost_out, index=idx, name="cost"),
        orders=pd.DataFrame(orders_out, index=idx, columns=syms),
        cost_model=costs,
    )


# ------------------------------------------------------------------------------------ full run


@dataclass(frozen=True)
class BacktestConfig:
    start: date
    end: date | None = None
    commission_nav: float = 10_000.0      # NAV the fixed commission is expressed against
    slippage_bps: float = 0.0
    sixty_forty: tuple[str, str] = ("SPY", "IEF")
    buy_and_hold: tuple[str, ...] = ("SPY", "QQQ")


@dataclass(frozen=True)
class BacktestRun:
    config: BacktestConfig
    policy_sha: str
    panel: Panel
    plan: BookPlan
    detail: ReferenceDetail
    books: dict[str, SimResult]
    data_start: dict[str, date | None]


def equity_calendar(closes: Mapping[str, pd.Series], lines: Sequence[LineSpec], end: date | None) -> pd.DatetimeIndex:
    """Trading days = union of the non-crypto lines' dates (the US equity calendar), up to `end`."""
    dates: set[pd.Timestamp] = set()
    for line in lines:
        if line.asset_class != "crypto" and line.symbol in closes:
            dates.update(pd.DatetimeIndex(closes[line.symbol].dropna().index))
    if not dates:
        raise ValueError("no non-crypto history to define the trading calendar")
    cal = pd.DatetimeIndex(sorted(dates))
    if end is not None:
        cal = cal[cal <= pd.Timestamp(end)]
    return cal


def run_backtest(
    closes: Mapping[str, pd.Series],
    control_closes: Mapping[str, pd.Series],
    policy: Policy,
    config: BacktestConfig,
    *,
    lines: Sequence[LineSpec] | None = None,
) -> BacktestRun:
    """Reference book (listed and CFD vehicle costs) plus controls, on one calendar.

    `closes` are keyed by line symbol; only in-reference lines are simulated (overlay lines hold 0
    in the reference by definition). `control_closes` are keyed by ticker."""
    ref_lines = [ln for ln in (lines or policy.universe.lines) if ln.in_reference]
    calendar = equity_calendar(closes, ref_lines, config.end)
    panel = build_panel(closes, ref_lines, policy, calendar)
    plan, detail = reference_plan(panel, ref_lines, policy, start=config.start)
    no_trend, _ = reference_plan(panel, ref_lines, policy, start=config.start, name="no_trend", force_up=True)
    static = static_plan(panel, ref_lines, policy, start=config.start)
    kw = {"commission_nav": config.commission_nav, "slippage_bps": config.slippage_bps}
    listed = cost_model(ref_lines, policy, mode="listed", **kw)
    cfd = cost_model(ref_lines, policy, mode="cfd", **kw)

    books: dict[str, SimResult] = {}
    books["reference"] = simulate(plan, panel.returns, listed)
    cfd_plan = BookPlan(name="reference_cfd", targets=plan.targets, levels=plan.levels, thresholds=plan.thresholds)
    books["reference_cfd"] = simulate(cfd_plan, panel.returns, cfd)
    books["no_trend"] = simulate(no_trend, panel.returns, listed)
    books["static"] = simulate(static, panel.returns, listed)

    ctrl_returns = aligned_returns(control_closes, calendar)
    ctrl_costs = control_cost_model(list(control_closes), policy, mode="listed", **kw)
    for ticker in config.buy_and_hold:
        if ticker in ctrl_returns.columns:
            p = fixed_mix_plan(ctrl_returns, {ticker: 1.0}, start=config.start, name=f"{ticker.lower()}_bh", rebalance="never")
            books[p.name] = simulate(p, ctrl_returns, ctrl_costs)
    a, b = config.sixty_forty
    if a in ctrl_returns.columns and b in ctrl_returns.columns:
        p = fixed_mix_plan(ctrl_returns, {a: 0.6, b: 0.4}, start=config.start, name="sixty_forty", rebalance="monthly")
        books[p.name] = simulate(p, ctrl_returns, ctrl_costs)

    data_start: dict[str, date | None] = {}
    for line in ref_lines:
        s = closes.get(line.symbol)
        first = s.dropna().index.min() if s is not None and not s.dropna().empty else None
        data_start[line.symbol] = pd.Timestamp(first).date() if first is not None else None
    return BacktestRun(
        config=config,
        policy_sha=policy.sha256,
        panel=panel,
        plan=plan,
        detail=detail,
        books=books,
        data_start=data_start,
    )
