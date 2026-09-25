"""Aligned daily log returns across lines, for covariance (reference book ex-ante vol).

Rule: closes are aligned on the calendar dates EVERY line has (inner join on the UTC date), and
only then log-differenced. A crypto line's Monday return therefore spans Friday->Monday, exactly
like the equity lines it is being correlated with, instead of Sunday->Monday."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd


def _daily_closes(bars: pd.DataFrame) -> pd.Series:
    closes = bars["close"].astype("float64").copy()
    closes.index = pd.DatetimeIndex(closes.index).tz_convert("UTC").normalize()
    closes = closes[~closes.index.duplicated(keep="last")].sort_index()
    return closes[closes > 0]


def returns_matrix(bars_by_line: Mapping[str, pd.DataFrame], *, window: int | None = None) -> pd.DataFrame:
    """DataFrame[date x line] of log returns on common dates; the last `window` rows if given."""
    closes = {
        sym: _daily_closes(bars)
        for sym, bars in bars_by_line.items()
        if bars is not None and not bars.empty
    }
    if not closes:
        return pd.DataFrame()
    aligned = pd.concat(closes, axis=1, join="inner").sort_index()
    returns = np.log(aligned).diff().iloc[1:].dropna(how="any")
    if window is not None:
        if window < 1:
            raise ValueError("window must be >= 1")
        returns = returns.tail(window)
    returns.index.name = "date"
    return returns
