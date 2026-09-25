"""FRED daily macro series via the keyless fredgraph CSV, with availability and publication rules.

Rules:
- Missing observations ("." in the classic CSV, empty in the current one) are dropped.
- Availability: the value dated D is usable from D+1 12:00 UTC (FRED posts daily series the next
  day). Replays must not see a value before that time.
- Publication: only series in PUBLISHABLE may appear in public documents. VIXCLS (CBOE) and
  BAMLH0A0HYM2 (ICE) are licensed: agents may read them, the journal never shows them.
  Unknown series are unpublishable (fail closed).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

import httpx
import pandas as pd

from council.data.bars import to_utc
from council.data.http import DataError, client_scope, get_with_retry

CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
AVAILABLE_LAG = timedelta(days=1)
AVAILABLE_TIME_UTC = time(12, 0)
_SERIES_ID = re.compile(r"^[A-Z0-9_]{2,30}$")

SeriesKind = Literal["rate", "index", "vol"]


@dataclass(frozen=True)
class SeriesSpec:
    publishable: bool
    kind: SeriesKind      # rate: level in % (changes in bps); index: level (changes in %); vol: level


SERIES: dict[str, SeriesSpec] = {
    "DGS10": SeriesSpec(publishable=True, kind="rate"),
    "DGS2": SeriesSpec(publishable=True, kind="rate"),
    "T10Y2Y": SeriesSpec(publishable=True, kind="rate"),
    "DFF": SeriesSpec(publishable=True, kind="rate"),
    "DTWEXBGS": SeriesSpec(publishable=True, kind="index"),
    "VIXCLS": SeriesSpec(publishable=False, kind="vol"),
    "BAMLH0A0HYM2": SeriesSpec(publishable=False, kind="rate"),
}
PUBLISHABLE: frozenset[str] = frozenset(k for k, v in SERIES.items() if v.publishable)
READ_ONLY: frozenset[str] = frozenset(k for k, v in SERIES.items() if not v.publishable)
DEFAULT_MACRO: tuple[str, ...] = ("DGS10", "DGS2", "T10Y2Y", "DFF", "DTWEXBGS", "VIXCLS")


def is_publishable(series_id: str) -> bool:
    """True only for registered publishable series; unknown series fail closed."""
    return series_id in PUBLISHABLE


def series_kind(series_id: str) -> SeriesKind:
    spec = SERIES.get(series_id)
    return spec.kind if spec else "index"


def available_at(day: date | datetime | pd.Timestamp) -> datetime:
    """When the observation dated `day` became usable: D+1 at 12:00 UTC."""
    d = day.date() if isinstance(day, datetime) else day
    return datetime.combine(d + AVAILABLE_LAG, AVAILABLE_TIME_UTC, tzinfo=UTC)


def available_series(series: pd.Series, as_of: datetime) -> pd.Series:
    """Observations whose availability time is <= as_of (the lookahead guard for macro)."""
    if series.empty:
        return series
    idx = pd.DatetimeIndex(series.index)
    days = (idx.tz_convert("UTC").tz_localize(None) if idx.tz is not None else idx).normalize()
    offset = pd.Timedelta(AVAILABLE_LAG) + pd.Timedelta(
        hours=AVAILABLE_TIME_UTC.hour, minutes=AVAILABLE_TIME_UTC.minute
    )
    avail = (days + offset).tz_localize("UTC")
    return series[avail <= to_utc(as_of)]


def parse_csv(text: str, series_id: str) -> pd.Series:
    """fredgraph CSV -> float Series indexed by observation date (00:00 UTC), missing dropped."""
    if not text.strip() or text.lstrip().startswith("<"):
        raise DataError(f"fred {series_id}: response is not CSV")
    try:
        frame = pd.read_csv(io.StringIO(text), na_values=["."], keep_default_na=True)
    except (ValueError, pd.errors.ParserError) as exc:
        raise DataError(f"fred {series_id}: unparseable CSV") from exc
    if frame.shape[1] < 2:
        raise DataError(f"fred {series_id}: CSV has no value column")
    date_col = frame.columns[0]
    value_col = series_id if series_id in frame.columns else frame.columns[1]
    values = pd.to_numeric(frame[value_col], errors="coerce")
    index = pd.DatetimeIndex(pd.to_datetime(frame[date_col], errors="coerce")).tz_localize("UTC")
    out = pd.Series(values.to_numpy(dtype="float64"), index=index, name=series_id)
    out = out[out.index.notna()].dropna()
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out.index.name = "date"
    return out


def fetch_series(series_id: str, *, client: httpx.Client | None = None) -> pd.Series:
    """One FRED series from the keyless CSV endpoint (no API key needed or sent)."""
    if not _SERIES_ID.match(series_id):
        raise ValueError(f"bad FRED series id {series_id!r}")
    what = f"fred {series_id}"
    with client_scope(client) as http:
        response = get_with_retry(http, CSV_URL, what=what, params={"id": series_id})
    return parse_csv(response.text, series_id)
