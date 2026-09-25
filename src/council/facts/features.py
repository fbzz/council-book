"""Per-line market state from PRIOR COMPLETED daily closes only.

Every input bar first passes the source's availability rule at `now` (data/bars.py), so the
in-progress bar and anything published after `now` can never shape a number here. The cycle passes
its SLOT as `now` (not the wall clock), so a late cycle sees exactly what an on-time one would.

Rules (numbers from policy/reference.yaml):
- Trend: last close vs SMA-50 and SMA-200 of completed closes: above both = up, above exactly one
  = mixed, above neither = down (a close equal to an SMA is not above it). Crypto lines flip only
  after `crypto_confirm_closes` consecutive closes in the new raw state; others flip at once.
- sigma_daily = max(EWMA(lambda=0.94) of squared daily log returns ** 0.5,
                    0.8 * sample std of the last 60 daily log returns);
  sigma_ann = sigma_daily * sqrt(365 for crypto, else 252).
- vol_ratio_1y = sigma_daily / median of the rolling sigma_daily estimate over one year of bars
  (the annualisation length: 252 bars, or 365 for crypto; at least 126 estimates, else None).
- ewma5_60_ratio = sqrt(EWMA span-5 / EWMA span-60 of squared daily log returns).
- mom10d_pct / mom63d_pct: simple return over the last 10 / 63 completed bars, in percent.
- dd52_pct: last close vs the highest close of the last 364 days, in percent (<= 0).
- ret1d_sigma: last completed daily log return / sigma_daily.
- data_age_h: hours from the last bar's availability time to `now` (raw clock hours; the pack
  applies the weekend/holiday-aware freshness rule and sets `frozen`).
- bar_available_at: the availability time of that last bar (None when no bar is usable).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from council import clock
from council.data.bars import available_only, last_available_at, normalize_bars, to_utc
from council.models.facts import MarketState
from council.policy import LineSpec, Policy
from council.reference.signals import trend_states

MIN_MEDIAN_OBS = 126
MOM_SHORT = 10
MOM_LONG = 63
DD_WINDOW = pd.Timedelta(days=364)
SHOCK_FAST_SPAN = 5
SHOCK_SLOW_SPAN = 60


@dataclass(frozen=True)
class VolParams:
    ewma_lambda: float
    realised_window: int
    floor_mult: float
    ann_days: int


def vol_params(policy: Policy, asset_class: str) -> VolParams:
    vol = policy.reference["vol"]
    days = vol["annualisation_days"]
    ann = int(days["crypto"] if asset_class == "crypto" else days["default"])
    return VolParams(
        ewma_lambda=float(vol["ewma_lambda"]),
        realised_window=int(vol["realised_window"]),
        floor_mult=float(vol["realised_floor_mult"]),
        ann_days=ann,
    )


def trend_params(policy: Policy, asset_class: str) -> tuple[int, int, int]:
    """(fast SMA, slow SMA, closes needed to confirm a flip) for this asset class."""
    trend = policy.reference["trend"]
    confirm = (int(trend.get("crypto_confirm_closes", 1)) if asset_class == "crypto"
               else int(trend.get("confirm_closes", 1)))
    return int(trend["fast_sma"]), int(trend["slow_sma"]), max(1, confirm)


def log_returns(closes: pd.Series) -> pd.Series:
    """Daily log returns of consecutive completed closes."""
    return np.log(closes.astype("float64")).diff().iloc[1:]


def trend_series(closes: pd.Series, fast: int, slow: int) -> pd.Series:
    """Raw trend state at every close (None until both SMAs have a full window)."""
    sma_fast = closes.rolling(fast, min_periods=fast).mean()
    sma_slow = closes.rolling(slow, min_periods=slow).mean()
    above = (closes > sma_fast).astype(int) + (closes > sma_slow).astype(int)
    labels = np.where(above == 2, "up", np.where(above == 1, "mixed", "down"))
    state = pd.Series(labels, index=closes.index, dtype=object)
    state[sma_fast.isna() | sma_slow.isna()] = None
    return state


def confirm_trend(raw: pd.Series, closes_needed: int) -> pd.Series:
    """Confirmed state: the first valid raw state stands; later it flips to a new raw state only
    once that state has held for `closes_needed` consecutive closes (1 = no confirmation)."""
    out: list[str | None] = []
    confirmed: str | None = None
    run_state: str | None = None
    run_len = 0
    for value in raw.tolist():
        if not isinstance(value, str):
            confirmed, run_state, run_len = None, None, 0
            out.append(None)
            continue
        run_len = run_len + 1 if value == run_state else 1
        run_state = value
        if confirmed is None or (value != confirmed and run_len >= closes_needed):
            confirmed = value
        out.append(confirmed)
    return pd.Series(out, index=raw.index, dtype=object)


def sigma_daily_series(returns: pd.Series, params: VolParams) -> pd.Series:
    """Rolling daily sigma estimate (NaN until the realised window is full)."""
    ewma = np.sqrt((returns**2).ewm(alpha=1.0 - params.ewma_lambda, adjust=True).mean())
    realised = returns.rolling(params.realised_window, min_periods=params.realised_window).std(ddof=1)
    return np.maximum(ewma, params.floor_mult * realised)


def shock_ratio(returns: pd.Series, fast: int = SHOCK_FAST_SPAN, slow: int = SHOCK_SLOW_SPAN) -> float | None:
    """sqrt(EWMA_fast / EWMA_slow) of squared returns; None with < `slow` returns or zero vol."""
    if len(returns) < slow:
        return None
    r2 = returns**2
    fast_v = float(r2.ewm(span=fast, adjust=True).mean().iloc[-1])
    slow_v = float(r2.ewm(span=slow, adjust=True).mean().iloc[-1])
    if not slow_v > 0:
        return None
    return math.sqrt(fast_v / slow_v)


def _r(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def _pct_change(closes: pd.Series, bars_back: int) -> float | None:
    if len(closes) <= bars_back:
        return None
    return (float(closes.iloc[-1]) / float(closes.iloc[-1 - bars_back]) - 1.0) * 100.0


def _dd52(closes: pd.Series) -> float:
    window = closes[closes.index > closes.index[-1] - DD_WINDOW]
    return min(0.0, (float(closes.iloc[-1]) / float(window.max()) - 1.0) * 100.0)


def usable_daily(line: LineSpec, daily: pd.DataFrame, *, now: datetime, source: str | None = None) -> pd.DataFrame:
    """The daily bars this line may use at `now` (normalised, availability-filtered)."""
    src = source or line.signal.source
    bars = normalize_bars(daily) if not daily.empty else daily
    return available_only(bars, source=src, interval="1d", now=now)


def last_bar_available_at(
    line: LineSpec, daily: pd.DataFrame, *, now: datetime, source: str | None = None
) -> datetime | None:
    """Availability time of the newest usable daily bar (feeds FactPack fact timestamps)."""
    src = source or line.signal.source
    bars = usable_daily(line, daily, now=now, source=src)
    stamp = last_available_at(bars, source=src, interval="1d")
    return None if stamp is None else stamp.to_pydatetime()


def market_state(
    line: LineSpec,
    daily: pd.DataFrame,
    *,
    now: datetime,
    policy: Policy,
    four_hour: pd.DataFrame | None = None,
    source: str | None = None,
) -> MarketState:
    """MarketState for one exposure line as of `now` (the cycle slot).

    `four_hour` is accepted for interface stability; v1 derives every field from daily bars.
    `source` overrides the line's signal source when the history came from elsewhere (e.g. eToro
    candles), which also switches the availability rule."""
    del four_hour  # reserved (see docstring)
    src = source or line.signal.source
    asof = to_utc(now)
    label = f"{src}:{line.signal.ticker}" if src == line.signal.source else src
    base = {
        "symbol": line.symbol,
        "asset_class": line.asset_class,
        "market_open": clock.market_open(line.asset_class, asof.to_pydatetime(), line.session),
        "history_source": label,
    }
    bars = usable_daily(line, daily, now=asof.to_pydatetime(), source=src)
    if bars.empty:
        return MarketState(**base)

    avail = last_available_at(bars, source=src, interval="1d")
    age_h = (asof - avail).total_seconds() / 3600.0
    closes = bars["close"]
    last = float(closes.iloc[-1])

    fast, slow, confirm = trend_params(policy, line.asset_class)
    band = float(policy.reference["trend"].get("band_pct", 0.0)) / 100.0
    # the ONE trend implementation shared with the backtest (reference.signals.trend_states)
    trend = trend_states(closes, fast=fast, slow=slow, confirm=confirm, band=band).iloc[-1]
    sma_fast = float(closes.iloc[-fast:].mean()) if len(closes) >= fast else None
    sma_slow = float(closes.iloc[-slow:].mean()) if len(closes) >= slow else None

    params = vol_params(policy, line.asset_class)
    returns = log_returns(closes)
    sigma = sigma_daily_series(returns, params).dropna() if len(returns) else pd.Series(dtype="float64")
    sigma_d = float(sigma.iloc[-1]) if len(sigma) and sigma.index[-1] == returns.index[-1] else None
    median = None
    if sigma_d is not None and len(sigma) >= MIN_MEDIAN_OBS:
        median = float(sigma.iloc[-params.ann_days :].median())
    ret_last = float(returns.iloc[-1]) if len(returns) else None

    return MarketState(
        **base,
        trend=trend if isinstance(trend, str) else None,
        dist_sma50_pct=_r((last / sma_fast - 1.0) * 100.0, 4) if sma_fast else None,
        dist_sma200_pct=_r((last / sma_slow - 1.0) * 100.0, 4) if sma_slow else None,
        sigma_ann=_r(sigma_d * math.sqrt(params.ann_days), 8) if sigma_d else None,
        sigma_daily=_r(sigma_d, 8) if sigma_d else None,
        vol_ratio_1y=_r(sigma_d / median, 4) if sigma_d and median else None,
        ewma5_60_ratio=_r(shock_ratio(returns), 4),
        mom10d_pct=_r(_pct_change(closes, MOM_SHORT), 4),
        mom63d_pct=_r(_pct_change(closes, MOM_LONG), 4),
        dd52_pct=_r(_dd52(closes), 4),
        ret1d_sigma=_r(ret_last / sigma_d, 4) if sigma_d and ret_last is not None else None,
        data_age_h=_r(age_h, 3),
        bar_available_at=avail.to_pydatetime(),
    )


def market_states(
    policy: Policy,
    history: Mapping[str, pd.DataFrame],
    *,
    now: datetime,
    sources: Mapping[str, str] | None = None,
) -> dict[str, MarketState]:
    """States for every universe line that has history (lines without history are left out; the
    pack builder freezes them as no_data)."""
    out: dict[str, MarketState] = {}
    for line in policy.universe.lines:
        daily = history.get(line.symbol)
        if daily is None:
            continue
        src = (sources or {}).get(line.symbol)
        out[line.symbol] = market_state(line, daily, now=now, policy=policy, source=src)
    return out
