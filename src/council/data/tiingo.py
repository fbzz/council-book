"""Tiingo end-of-day prices -> canonical daily bars (adjusted), available bars only.

Rules:
- Adjustment: every OHLC field is scaled by adjClose / close, so the adjusted close is exact and
  the bar keeps its shape; volume uses adjVolume when present.
- Index: trading date D at 00:00 UTC (the bar START label).
- Availability: the bar for D is usable from D 20:00 America/New_York (bars.tiingo rule), so a
  partial or same-day row is never returned before then.
- The token travels in the Authorization header, never in the URL or an error message.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

import httpx
import pandas as pd

from council.clock import utcnow
from council.data.bars import available_only, bars_from_rows, empty_bars, to_utc
from council.data.http import DataError, client_scope, get_with_retry, json_body

PRICES_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
_TICKER = re.compile(r"^[A-Za-z0-9.\-]{1,15}$")


def _num(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def adjusted_row(row: dict[str, Any]) -> tuple[pd.Timestamp, float, float, float, float, float] | None:
    """One Tiingo row -> (start, o, h, l, c, v) adjusted by the adjClose ratio; None if unusable."""
    raw_date = row.get("date")
    close = _num(row, "close")
    if not raw_date or close is None or close <= 0:
        return None
    adj_close = _num(row, "adjClose")
    ratio = adj_close / close if adj_close is not None and adj_close > 0 else 1.0
    day = pd.Timestamp(str(raw_date)[:10]).tz_localize("UTC")
    o = _num(row, "open") or close
    h = _num(row, "high") or max(o, close)
    low = _num(row, "low") or min(o, close)
    volume = _num(row, "adjVolume")
    if volume is None:
        volume = _num(row, "volume") or 0.0
    return (day, o * ratio, h * ratio, low * ratio, close * ratio, volume)


def parse_daily(payload: Any, now: datetime) -> pd.DataFrame:
    """Canonical adjusted bars from a /prices payload, filtered to bars available at `now`."""
    if isinstance(payload, dict):
        detail = payload.get("detail")
        raise DataError(f"tiingo: error payload ({'detail' if detail else 'object'})")
    if not isinstance(payload, list):
        raise DataError("tiingo: payload is not a list")
    rows = [r for r in (adjusted_row(x) for x in payload if isinstance(x, dict)) if r is not None]
    bars = bars_from_rows(rows) if rows else empty_bars()
    return available_only(bars, source="tiingo", interval="1d", now=now)


def fetch_daily(
    ticker: str,
    start: date | datetime | str,
    *,
    token: str,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Adjusted daily bars for `ticker` from `start` (inclusive), available bars only."""
    if not _TICKER.match(ticker):
        raise ValueError(f"bad tiingo ticker {ticker!r}")
    if not token:
        raise DataError("tiingo: no API token configured")
    start_s = start.isoformat()[:10] if isinstance(start, date | datetime) else str(start)[:10]
    asof = to_utc(now or utcnow())
    what = f"tiingo daily {ticker}"
    url = PRICES_URL.format(ticker=quote(ticker.lower(), safe=""))
    headers = {"Authorization": f"Token {token}", "Content-Type": "application/json"}
    with client_scope(client) as http:
        response = get_with_retry(
            http, url, what=what, params={"startDate": start_s, "format": "json"}, headers=headers
        )
        payload = json_body(response, what=what)
    return parse_daily(payload, asof.to_pydatetime())
