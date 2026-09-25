"""Canonical bars: DataFrame[open, high, low, close, volume] (float64) indexed by the UTC bar START.

Availability rules (the only way a bar may enter a fact pack):
- Binance and eToro: a bar is usable once COMPLETE, i.e. start + interval <= now.
- Tiingo daily: the bar for trading date D is usable from D 20:00 America/New_York (EOD files are
  published after the close), whatever the UTC offset of the day.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
INDEX_NAME = "start"
NEW_YORK = "America/New_York"
TIINGO_AVAILABLE_HOURS = 20  # local New York time on the bar's own date

_INTERVALS: dict[str, pd.Timedelta] = {
    "1d": pd.Timedelta(days=1),
    "4h": pd.Timedelta(hours=4),
    "OneDay": pd.Timedelta(days=1),
    "FourHours": pd.Timedelta(hours=4),
}


def interval_delta(interval: str) -> pd.Timedelta:
    """Length of a supported bar interval ("1d"/"4h", or eToro's "OneDay"/"FourHours")."""
    try:
        return _INTERVALS[interval]
    except KeyError:
        raise ValueError(f"unsupported bar interval {interval!r}") from None


def to_utc(ts: datetime | pd.Timestamp | str) -> pd.Timestamp:
    """An aware timestamp in UTC. Naive inputs are refused: council code is UTC-aware only."""
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is None:
        raise ValueError("naive timestamp; council code uses aware UTC datetimes only")
    return stamp.tz_convert("UTC")


def empty_bars() -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz="UTC", name=INDEX_NAME)
    return pd.DataFrame({c: pd.Series([], dtype="float64") for c in COLUMNS}, index=index)


def normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Canonical form: UTC DatetimeIndex named `start`, sorted, unique (last row wins), float64
    columns in COLUMNS order, missing volume = 0, rows without a positive finite close dropped."""
    if df.empty:
        return empty_bars()
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("bars need a DatetimeIndex of bar start times")
    if df.index.tz is None:
        raise ValueError("bars index must be timezone-aware (UTC)")
    out = df.copy()
    if "volume" not in out.columns:
        out["volume"] = 0.0
    missing = [c for c in COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"bars missing columns {missing}")
    out = out[list(COLUMNS)].astype("float64")
    out.index = out.index.tz_convert("UTC")
    out.index.name = INDEX_NAME
    out = out[~out.index.duplicated(keep="last")].sort_index()
    close = out["close"].to_numpy()
    keep = np.isfinite(close) & (close > 0)
    out["volume"] = out["volume"].fillna(0.0)
    return out[keep]


def bars_from_rows(rows: Iterable[Sequence[Any]]) -> pd.DataFrame:
    """Build bars from (start, open, high, low, close, volume) rows; start must be tz-aware."""
    data = list(rows)
    if not data:
        return empty_bars()
    index = pd.DatetimeIndex([to_utc(r[0]) for r in data], name=INDEX_NAME)
    frame = pd.DataFrame(
        [[float(v) for v in r[1:6]] for r in data], index=index, columns=list(COLUMNS)
    )
    return normalize_bars(frame)


def completed_only(df: pd.DataFrame, interval: str, now: datetime | pd.Timestamp) -> pd.DataFrame:
    """Rule: keep a bar only when start + interval <= now (the in-progress bar is never used)."""
    if df.empty:
        return df
    cutoff = to_utc(now) - interval_delta(interval)
    return df[df.index <= cutoff]


def tiingo_available_at(start: datetime | pd.Timestamp) -> pd.Timestamp:
    """Tiingo EOD bar for date D (index = D 00:00 UTC) is available at D 20:00 New York."""
    day = to_utc(start).tz_localize(None).normalize()
    return (day + pd.Timedelta(hours=TIINGO_AVAILABLE_HOURS)).tz_localize(NEW_YORK).tz_convert("UTC")


def available_times(index: pd.DatetimeIndex, *, source: str, interval: str) -> pd.DatetimeIndex:
    """When each bar became usable, under the source's availability rule (module docstring)."""
    if index.tz is None:
        raise ValueError("bars index must be timezone-aware (UTC)")
    idx = index.tz_convert("UTC")
    if source == "tiingo":
        if interval_delta(interval) != pd.Timedelta(days=1):
            raise ValueError("tiingo provides daily bars only")
        days = idx.tz_localize(None).normalize()
        local = (days + pd.Timedelta(hours=TIINGO_AVAILABLE_HOURS)).tz_localize(NEW_YORK)
        return local.tz_convert("UTC")
    if source in ("binance", "etoro"):
        return idx + interval_delta(interval)
    raise ValueError(f"unknown history source {source!r}")


def available_only(
    df: pd.DataFrame, *, source: str, interval: str, now: datetime | pd.Timestamp
) -> pd.DataFrame:
    """Keep bars whose availability time is <= now (the lookahead guard for every consumer)."""
    if df.empty:
        return df
    avail = available_times(df.index, source=source, interval=interval)
    return df[avail <= to_utc(now)]


def last_available_at(df: pd.DataFrame, *, source: str, interval: str) -> pd.Timestamp | None:
    """Availability time of the most recent bar (None for empty bars)."""
    if df.empty:
        return None
    return available_times(df.index[-1:], source=source, interval=interval)[0]


def bars_to_json(df: pd.DataFrame) -> dict[str, list[Any]]:
    """Exact JSON form (Python float repr round-trips); used by the file cache instead of pickle."""
    out: dict[str, list[Any]] = {"start": [ts.isoformat() for ts in df.index]}
    for col in COLUMNS:
        out[col] = [float(v) for v in df[col].to_numpy()]
    return out


def bars_from_json(obj: dict[str, list[Any]]) -> pd.DataFrame:
    starts = obj.get("start", [])
    if not starts:
        return empty_bars()
    index = pd.DatetimeIndex(pd.to_datetime(starts, utc=True), name=INDEX_NAME)
    frame = pd.DataFrame({c: [float(v) for v in obj[c]] for c in COLUMNS}, index=index)
    return normalize_bars(frame)
