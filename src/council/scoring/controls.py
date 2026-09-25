"""Controls: what simple alternatives would have done over the same period. Pure functions.

Conventions:
- `weights`: DataFrame indexed by decision time, columns = lines, values = signed target weights
  (x of NAV). A row is a rebalance instruction effective from that time; a missing row (or a NaN
  cell) means "no instruction" and the holding drifts with the market.
- `returns`: DataFrame indexed by the END of each period, simple returns (0.01 = +1%). A weight
  decided at t earns the return of the first period ending after t (no lookahead).
- Costs: per-side cost in bps charged on |traded weight|; the deadband skips changes smaller than
  `deadband_x` (a move to exactly 0 always trades).

Controls:
- C2  = executable reference path: the reference book's targets through the same cost floors and
        deadband as the council.
- C2x = the reference scaled to the council's average gross exposure (so C1 - C2x is not simply
        "held less risk").
- C3  = exact hold: the starting book, never traded again.
- C4  = buy-and-hold SPY and BTC.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np
import pandas as pd

BASE = 100.0


def _cost_vector(columns: Iterable[str], cost_bps: float | Mapping[str, float]) -> pd.Series:
    cols = list(columns)
    if isinstance(cost_bps, Mapping):
        return pd.Series({c: float(cost_bps.get(c, 0.0)) for c in cols}, dtype=float)
    return pd.Series(float(cost_bps), index=cols, dtype=float)


def book_index(
    weights: pd.DataFrame,
    returns: pd.DataFrame,
    *,
    cost_bps: float | Mapping[str, float] = 0.0,
    deadband_x: float = 0.0,
    base: float = BASE,
    initial_holding: Mapping[str, float] | None = None,
) -> pd.Series:
    """Base-100 index of a book that trades to `weights` and drifts with `returns` in between.
    `initial_holding` is a book already held before the first period (no entry cost)."""
    cols = sorted(set(weights.columns) | set(returns.columns) | set(initial_holding or {}))
    w = weights.reindex(columns=cols)
    r = returns.reindex(columns=cols).fillna(0.0)
    costs = _cost_vector(cols, cost_bps) / 1e4
    times = sorted(set(w.index) | set(r.index))
    holding = pd.Series({c: float((initial_holding or {}).get(c, 0.0)) for c in cols}, dtype=float)
    level = base
    out: dict = {}
    for t in times:
        if t in r.index:
            period = r.loc[t]
            growth = 1.0 + float((holding * period).sum())
            if growth <= 0:
                level = 0.0
                holding = pd.Series(0.0, index=cols)
            else:
                holding = holding * (1.0 + period) / growth
                level *= growth
        if t in w.index:
            target = w.loc[t]
            if isinstance(target, pd.DataFrame):          # duplicate timestamps: last wins
                target = target.iloc[-1]
            has = target.notna()
            diff = (target.where(has, holding) - holding)
            trade = has & ((diff.abs() >= deadband_x) | ((target == 0) & (holding != 0)))
            traded = diff.where(trade, 0.0)
            level *= 1.0 - float((traded.abs() * costs).sum())
            holding = holding + traded
        out[t] = level
    return pd.Series(out, dtype=float).sort_index()


def gross(weights: pd.DataFrame) -> pd.Series:
    return weights.abs().sum(axis=1)


def c2_reference(
    reference_weights: pd.DataFrame,
    returns: pd.DataFrame,
    *,
    cost_bps: float | Mapping[str, float] = 0.0,
    deadband_x: float = 0.0,
) -> pd.Series:
    """C2: the reference book as it would have been executed (same costs and deadband)."""
    return book_index(reference_weights, returns, cost_bps=cost_bps, deadband_x=deadband_x)


def exposure_scale(council_weights: pd.DataFrame, reference_weights: pd.DataFrame) -> float:
    """Average council gross / average reference gross (1.0 if the reference is always flat)."""
    ref = float(gross(reference_weights).mean()) if len(reference_weights) else 0.0
    council = float(gross(council_weights).mean()) if len(council_weights) else 0.0
    return council / ref if ref > 0 else 1.0


def c2x_exposure_matched(
    reference_weights: pd.DataFrame,
    council_weights: pd.DataFrame,
    returns: pd.DataFrame,
    *,
    cost_bps: float | Mapping[str, float] = 0.0,
    deadband_x: float = 0.0,
) -> pd.Series:
    """C2x: the reference scaled to the council's average gross exposure."""
    scale = exposure_scale(council_weights, reference_weights)
    return book_index(reference_weights * scale, returns, cost_bps=cost_bps, deadband_x=deadband_x)


def c3_hold(initial_weights: Mapping[str, float], returns: pd.DataFrame) -> pd.Series:
    """C3: the starting book held without any trade (no entry cost: the position already exists)."""
    return book_index(pd.DataFrame(), returns, initial_holding=initial_weights)


def c4_buy_and_hold(asset_returns: pd.DataFrame, symbols: Iterable[str] = ("SPY", "BTCUSDT")) -> dict[str, pd.Series]:
    """C4: base-100 buy-and-hold index per symbol (missing periods count as flat)."""
    out = {}
    for sym in symbols:
        if sym in asset_returns.columns:
            series = asset_returns[sym].fillna(0.0)
            out[sym] = BASE * (1.0 + series).cumprod()
    return out


def annualised_vol(index: pd.Series, periods_per_year: float = 252.0) -> float:
    rets = index.pct_change().dropna()
    if len(rets) < 2:
        return float("nan")
    return float(rets.std(ddof=1) * np.sqrt(periods_per_year))
