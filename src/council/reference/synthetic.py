"""Synthetic random-walk price history for `--synthetic` runs and tests. No network, seeded.

Equity-like tickers trade on business days; crypto tickers trade every day from `crypto_start`,
so the "crypto sits in cash before its history starts" path is exercised. Returns share a common
factor so the covariance is not diagonal. The numbers mean nothing about real markets.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import numpy as np
import pandas as pd

# annual drift, annual vol, loading on the common factor
_PROFILES: dict[str, tuple[float, float, float]] = {
    "QQQ": (0.12, 0.24, 0.9),
    "SOXX": (0.15, 0.34, 0.9),
    "SPY": (0.09, 0.18, 0.95),
    "GLD": (0.05, 0.15, 0.1),
    "IEF": (0.02, 0.07, -0.2),
    "BTCUSDT": (0.40, 0.70, 0.4),
    "ETHUSDT": (0.45, 0.90, 0.4),
}
_DEFAULT_PROFILE = (0.06, 0.20, 0.5)


def synthetic_closes(
    tickers: Sequence[str],
    *,
    start: date,
    end: date,
    crypto_tickers: Sequence[str] = (),
    crypto_start: date | None = None,
    seed: int = 7,
) -> dict[str, pd.Series]:
    """Seeded geometric random walks per ticker (closes start at 100)."""
    rng = np.random.default_rng(seed)
    days = pd.date_range(start, end, freq="D")
    factor = rng.standard_normal(len(days))
    out: dict[str, pd.Series] = {}
    for ticker in list(tickers) + list(crypto_tickers):
        is_crypto = ticker in crypto_tickers
        drift, vol, beta = _PROFILES.get(ticker, _DEFAULT_PROFILE)
        per_year = 365 if is_crypto else 252
        mu, sd = drift / per_year, vol / np.sqrt(per_year)
        idio = rng.standard_normal(len(days))
        z = beta * factor + np.sqrt(max(0.0, 1.0 - beta**2)) * idio
        # slow regime swings so trend states actually flip
        regime = np.sin(np.arange(len(days)) / (140.0 + 30.0 * rng.random())) * 3.0 * mu
        rets = pd.Series(mu + regime + sd * z, index=days)
        if is_crypto:
            first = pd.Timestamp(crypto_start or start)
            rets = rets[rets.index >= first]
        else:
            rets = rets[rets.index.dayofweek < 5]
        out[ticker] = (100.0 * np.exp(np.log1p(rets.clip(lower=-0.5)).cumsum())).rename(ticker)
    return out
