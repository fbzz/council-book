"""Pure series arithmetic shared by the after-close screen and the fact card (completed daily bars).

Conventions: bars are the canonical frame (`council.data.bars`, index = trading date at 00:00 UTC,
columns open/high/low/close/volume), already filtered to what is available at the as-of time.
Every function reads only rows up to and including its `upto` position, so bars after the slot can
never change a value (the lookahead tests pin this). Volume is SIP daily volume only: a ratio is
always SIP over SIP (the caller never mixes feeds, and no intraday or partial volume exists here).
"""

from __future__ import annotations

import math
from statistics import median

import numpy as np
import pandas as pd

SIGMA_WINDOW = 20
VOLUME_WINDOW = 20


def closes(bars: pd.DataFrame) -> np.ndarray:
    return bars["close"].to_numpy(dtype=float)


def sigma_daily(close: np.ndarray, *, end: int, window: int = SIGMA_WINDOW) -> float | None:
    """Sample std of the `window` simple daily returns ending at position `end` (inclusive), or
    None with fewer than `window` returns."""
    if end < window or end >= len(close):
        return None
    seg = close[end - window:end + 1]
    rets = seg[1:] / seg[:-1] - 1.0
    value = float(np.std(rets, ddof=1))
    return value if math.isfinite(value) and value > 0 else None


def volume_median(volume: np.ndarray, *, end: int, window: int = VOLUME_WINDOW) -> float | None:
    """Median volume of the `window` sessions ending at position `end` (inclusive)."""
    if end + 1 < window or end >= len(volume):
        return None
    value = float(median(volume[end - window + 1:end + 1]))
    return value if value > 0 else None


def pct(a: float, b: float) -> float:
    """(a / b - 1) x 100."""
    return (a / b - 1.0) * 100.0


def sigma_move(move_pct: float, sigma: float, sessions: int) -> float:
    """A move in % expressed in sigma_daily x sqrt(sessions) (sessions >= 1)."""
    return (move_pct / 100.0) / (sigma * math.sqrt(max(int(sessions), 1)))


def position_on_or_before(index: pd.DatetimeIndex, day: pd.Timestamp) -> int | None:
    """The last row position whose date is <= `day`, or None."""
    pos = int(index.searchsorted(day, side="right")) - 1
    return pos if pos >= 0 else None


def beta(stock: np.ndarray, market: np.ndarray) -> float | None:
    """OLS beta of aligned return arrays (None when degenerate)."""
    if len(stock) < 20 or len(stock) != len(market):
        return None
    var = float(np.var(market, ddof=1))
    if not math.isfinite(var) or var <= 0:
        return None
    return float(np.cov(stock, market, ddof=1)[0, 1] / var)
