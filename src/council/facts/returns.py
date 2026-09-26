"""Aligned daily log returns across lines, for covariance (reference book ex-ante vol).

Rules:
- Master calendar: the calendar dates on which EVERY master line has a close (an inner join on the
  UTC date; default: every line). A crypto line's Monday return therefore spans Friday->Monday,
  exactly like the equity lines it is being correlated with, instead of Sunday->Monday.
- With `master` given (the cycle passes the core lines, design §11.3), every other column (a stock
  line) is placed on that calendar without shrinking it: its closes are read on the master dates it
  has, and each return spans consecutive closes it has there; dates before its first close, or a
  missing close, are NaN. A short-history stock therefore never shortens the core's window, and the
  covariance is pairwise-complete (`reference.book.ewma_covariance`), with too-short pairs treated as
  missing there (`reference.book.MIN_COMMON_ROWS`).
- Without `master`, rows with any missing return are dropped (every column shares one window).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np
import pandas as pd


def _daily_closes(bars: pd.DataFrame) -> pd.Series:
    closes = bars["close"].astype("float64").copy()
    closes.index = pd.DatetimeIndex(closes.index).tz_convert("UTC").normalize()
    closes = closes[~closes.index.duplicated(keep="last")].sort_index()
    return closes[closes > 0]


def returns_matrix(bars_by_line: Mapping[str, pd.DataFrame], *, window: int | None = None,
                   master: Iterable[str] | None = None) -> pd.DataFrame:
    """DataFrame[date x line] of log returns on the master calendar (module docstring); the last
    `window` rows if given. `master` names the lines whose common dates form the calendar (lines
    without history are ignored; none present means every line)."""
    closes = {
        sym: _daily_closes(bars)
        for sym, bars in bars_by_line.items()
        if bars is not None and not bars.empty
    }
    if window is not None and window < 1:
        raise ValueError("window must be >= 1")
    if not closes:
        return pd.DataFrame()
    wanted = set(master) if master is not None else None
    core = [s for s in closes if wanted is None or s in wanted] or list(closes)
    aligned = pd.concat({s: closes[s] for s in core}, axis=1, join="inner").sort_index()
    returns = np.log(aligned).diff().iloc[1:].dropna(how="any")
    others = [s for s in closes if s not in core]
    if others:
        calendar = aligned.index
        extra: dict[str, pd.Series] = {}
        for s in others:
            on_calendar = closes[s].reindex(calendar).dropna()
            extra[s] = np.log(on_calendar).diff().reindex(returns.index)
        returns = pd.concat([returns, pd.DataFrame(extra, index=returns.index)], axis=1)
        returns = returns[[s for s in closes if s in returns.columns]]
    if window is not None:
        returns = returns.tail(window)
    returns.index.name = "date"
    return returns
