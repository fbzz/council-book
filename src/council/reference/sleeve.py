"""The reference sleeve's trading rule: one implementation for the pre-registered stock-sleeve study
and the live reference book. FROZEN at the tag `stock-sleeve-spec` (docs/stock-sleeve-spec.md
sections 6 and 11); a change here needs a new pre-registration.

Rules (variants-file keys in brackets):
- Equal weight [sleeve.share_of_nav, n_names]: every selected name has the unit share / N.
- Overlay [overlay.options]: the sleeve's level is the chosen option's value for the SPX line's
  trend state (up, mixed, down); a line without a trend state holds the level 1.0.
- Target: a selected name targets unit x level, every other sleeve line 0.
- Deadband [sleeve.deadband]: a line trades back to its target when |w - target| >= max(level x
  unit, min NAV share), where `level` is the deadband's fraction of the unit (0.25), not the overlay.
- A level change always trades, whatever the drift.
- Never borrow [sleeve.budget]: when the orders would lift the budgeted lines above their budget,
  every budgeted line held above its target is also ordered back to its target.

`pending_trades` is the per-close order decision of `council.reference.backtest.simulate` plus the
never-borrow rule; with no budgeted line it is exactly that decision (tests/reference/test_sleeve.py).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

import numpy as np

_EPS = 1e-12            # a drift at or below this is no trade (as council.reference.backtest)
LEVEL_ATOL = 1e-12      # levels closer than this are the same level
BUDGET_TOL = 1e-9       # the budget binds only when exceeded by more than this


def overlay_level(option_levels: Mapping[str, float], trend: str | None) -> float:
    """The sleeve's level for the SPX line's trend state under one overlay option; 1.0 when the
    state is missing or not in the option's table."""
    return float(option_levels[trend]) if trend in option_levels else 1.0


def unit_weight(share: float, n: int) -> float:
    """One name's equal weight: 0.05 of NAV at share 0.50 and N = 10, 0.0625 at N = 8."""
    return share / n


def sleeve_targets(selected: Iterable[str], share: float, n: int, level: float, *,
                   lines: Iterable[str] = ()) -> dict[str, float]:
    """Target weights: every selected name unit x level; every other line in `lines` 0.0. At most
    N names; fewer leave the rest of the sleeve in cash (never re-spread)."""
    chosen = list(selected)
    if len(set(chosen)) != len(chosen):
        raise ValueError("a name is selected twice")
    if len(chosen) > n:
        raise ValueError(f"{len(chosen)} names selected for {n} slots")
    unit = unit_weight(share, n)
    out = {k: 0.0 for k in lines}
    out.update({k: unit * level for k in chosen})
    return out


def drift_threshold(unit: float, deadband_level: float, min_share: float) -> float:
    """The drift that trades a line back to its target: max(deadband level x unit, min share)."""
    return max(deadband_level * unit, min_share)


def pending_trades(held_w: np.ndarray, held_level: np.ndarray, target_w: np.ndarray,
                   target_level: np.ndarray, threshold: np.ndarray | float, *,
                   budget_mask: np.ndarray | None = None, budget: float = math.inf) -> np.ndarray:
    """Which lines are ordered to their target at this close (a boolean array over the lines).

    A line trades when its target level differs from the level it was last traded at, or when its
    drift |target - held| reaches `threshold` (and is not nil). When the lines in `budget_mask`
    would end above `budget` after those orders, every one of them held above its target is also
    ordered to its target: a rebalance never borrows."""
    level_change = ~np.isclose(target_level, held_level, rtol=0.0, atol=LEVEL_ATOL)
    drift = np.abs(target_w - held_w)
    pending = level_change | ((drift >= threshold) & (drift > _EPS))
    if budget_mask is not None and budget_mask.any():
        projected = np.where(pending, target_w, held_w)
        if projected[budget_mask].sum() > budget + BUDGET_TOL:
            pending = pending | (budget_mask & (held_w > target_w + _EPS))
    return pending
