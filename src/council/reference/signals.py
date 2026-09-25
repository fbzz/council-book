"""Per-line signals from COMPLETED daily closes, each on the line's own calendar.

Every value at row t uses closes at or before t only (rolling windows and EWMAs look backward).

Rules (policy keys in brackets):
- Trend [reference.trend]: close vs SMA-fast and SMA-slow over the latest completed closes.
  Above both = up, above exactly one = mixed, above neither = down (a close equal to an SMA is not
  above it). None until `slow` closes exist. Crypto flips need `crypto_confirm_closes` consecutive
  closes in the new state.
- Sigma [reference.vol]: max(EWMA vol of daily LOG returns with lambda, floor_mult x realised std
  over `window`), annualised by the class's days (crypto 365, default 252). NaN until the window
  is full.
- Vol ratio: sigma now / median sigma over the trailing year (one year of the line's own closes,
  at least 126 estimates).

These mirror the live MarketState rules in council.facts.features so the backtest describes the
same book the cycle builds.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from council.policy import Policy

TREND_STATES = ("up", "mixed", "down")


def trend_states(close: pd.Series, *, fast: int = 50, slow: int = 200, confirm: int = 1) -> pd.Series:
    """Trend state per close (object dtype: "up"/"mixed"/"down"/None).

    Rule: up if close > both SMAs, mixed if above exactly one, down if above neither; None before
    `slow` closes. With confirm > 1 a change of state needs `confirm` consecutive closes in it."""
    if fast < 1 or slow < fast:
        raise ValueError("need 1 <= fast <= slow")
    c = close.astype(float)
    sma_fast = c.rolling(fast, min_periods=fast).mean()
    sma_slow = c.rolling(slow, min_periods=slow).mean()
    valid = (sma_fast.notna() & sma_slow.notna() & c.notna()).to_numpy()
    up = ((c > sma_fast) & (c > sma_slow)).to_numpy()
    down = (~(c > sma_fast) & ~(c > sma_slow)).to_numpy()
    raw: list[str | None] = [
        None if not ok else ("up" if u else "down" if d else "mixed")
        for ok, u, d in zip(valid, up, down, strict=True)
    ]
    if confirm > 1:
        raw = _confirm(raw, confirm)
    return pd.Series(raw, index=close.index, dtype=object, name="trend")


def _confirm(raw: list[str | None], confirm: int) -> list[str | None]:
    """Hysteresis: the confirmed state changes only after `confirm` consecutive new-state closes."""
    out: list[str | None] = []
    state: str | None = None
    candidate: str | None = None
    count = 0
    for value in raw:
        if value is None:
            state, candidate, count = None, None, 0
        elif state is None:
            state, candidate, count = value, None, 0
        elif value == state:
            candidate, count = None, 0
        else:
            count = count + 1 if value == candidate else 1
            candidate = value
            if count >= confirm:
                state, candidate, count = value, None, 0
        out.append(state)
    return out


def sigma_annualised(
    returns: pd.Series,
    *,
    lam: float = 0.94,
    window: int = 60,
    floor_mult: float = 0.8,
    ann: int = 252,
) -> pd.Series:
    """Annualised sigma = max(sqrt(EWMA_lambda(r^2)), floor_mult x realised std over `window`).

    Rule: zero-mean EWMA of the given (log) returns; NaN until `window` returns exist, so the floor
    always applies."""
    r = returns.astype(float)
    ewma_var = (r**2).ewm(alpha=1.0 - lam, adjust=True, ignore_na=True).mean()
    realised = r.rolling(window, min_periods=window).std(ddof=1)
    sigma = np.maximum(np.sqrt(ewma_var), floor_mult * realised) * math.sqrt(ann)
    return sigma.where(realised.notna()).rename("sigma_ann")


MIN_MEDIAN_OBS = 126


def vol_ratio(sigma: pd.Series, window: int, min_obs: int = MIN_MEDIAN_OBS) -> pd.Series:
    """Current sigma / median sigma over the trailing `window` rows (including now); NaN until
    `min(window, min_obs)` estimates exist."""
    median = sigma.rolling(window, min_periods=min(window, min_obs)).median()
    return (sigma / median).rename("vol_ratio_1y")


def signal_params(policy: Policy, asset_class: str) -> dict[str, Any]:
    """The policy numbers a line's signals use, resolved for its asset class."""
    trend = policy.reference["trend"]
    vol = policy.reference["vol"]
    days = vol["annualisation_days"]
    ann = int(days.get(asset_class, days["default"]))
    return {
        "fast": int(trend["fast_sma"]),
        "slow": int(trend["slow_sma"]),
        "confirm": int(trend.get("crypto_confirm_closes", 1)) if asset_class == "crypto" else 1,
        "lam": float(vol["ewma_lambda"]),
        "window": int(vol["realised_window"]),
        "floor_mult": float(vol["realised_floor_mult"]),
        "ann": ann,
    }


def line_signals(close: pd.Series, *, asset_class: str, policy: Policy) -> pd.DataFrame:
    """Trend, sigma_ann and vol_ratio_1y for one line on its own calendar (NaNs dropped first)."""
    p = signal_params(policy, asset_class)
    c = close.astype(float).dropna().sort_index()
    rets = np.log(c).diff()
    sigma = sigma_annualised(rets, lam=p["lam"], window=p["window"], floor_mult=p["floor_mult"], ann=p["ann"])
    return pd.DataFrame(
        {
            "trend": trend_states(c, fast=p["fast"], slow=p["slow"], confirm=p["confirm"]),
            "sigma_ann": sigma,
            "vol_ratio_1y": vol_ratio(sigma, p["ann"]),
        },
        index=c.index,
    )
