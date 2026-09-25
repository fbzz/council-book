"""Binance public klines (keyless market-data mirror) -> canonical bars, completed bars only.

Payload rows: [openTime ms, open, high, low, close, volume, closeTime ms, quoteVolume, trades, ...]
with prices as strings. Rule: a kline is used only when openTime + interval <= now."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

import httpx
import pandas as pd

from council.clock import utcnow
from council.data.bars import bars_from_rows, completed_only, empty_bars, to_utc
from council.data.http import DataError, client_scope, get_with_retry, json_body

KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
MAX_LIMIT = 1000
BinanceInterval = Literal["1d", "4h"]
_TICKER = re.compile(r"^[A-Z0-9]{2,20}$")


def parse_klines(payload: Any, interval: str, now: datetime) -> pd.DataFrame:
    """Canonical bars from a klines payload; malformed rows fail loudly, the in-progress bar drops."""
    if not isinstance(payload, list):
        raise DataError("binance klines: payload is not a list")
    rows = []
    for raw in payload:
        if not isinstance(raw, list) or len(raw) < 6:
            raise DataError("binance klines: malformed row")
        try:
            start = pd.Timestamp(int(raw[0]), unit="ms", tz="UTC")
            rows.append((start, *(float(raw[i]) for i in range(1, 6))))
        except (TypeError, ValueError) as exc:
            raise DataError("binance klines: non-numeric field") from exc
    bars = bars_from_rows(rows) if rows else empty_bars()
    return completed_only(bars, interval, now)


def fetch_klines(
    ticker: str,
    interval: BinanceInterval = "1d",
    limit: int = MAX_LIMIT,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    """The most recent `limit` klines for `ticker` (e.g. BTCUSDT), completed bars only."""
    if not _TICKER.match(ticker):
        raise ValueError(f"bad binance ticker {ticker!r}")
    if interval not in ("1d", "4h"):
        raise ValueError(f"unsupported binance interval {interval!r}")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be in 1..{MAX_LIMIT}")
    asof = to_utc(now or utcnow())
    what = f"binance klines {ticker} {interval}"
    params = {"symbol": ticker, "interval": interval, "limit": limit}
    with client_scope(client) as http:
        response = get_with_retry(http, KLINES_URL, what=what, params=params)
        payload = json_body(response, what=what)
    return parse_klines(payload, interval, asof.to_pydatetime())
