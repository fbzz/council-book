"""Gather each line's signal history (Tiingo/Binance) and the FRED macro set, through the cache.

Rules:
- Each line uses its policy signal source; eToro-sourced signals are fetched by the broker read
  client instead (flagged `history_unsupported` here).
- Cache entries are keyed by the request AND the slot, so every cycle fetches once and a re-run in
  the same slot reuses it. Cached bars are re-filtered for availability at `now`.
- A failed or skipped line is omitted with a quality flag; it never raises (the pack freezes it).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import pandas as pd

from council import clock
from council.data import binance, fred, tiingo
from council.data.bars import available_only, bars_from_json, bars_to_json, to_utc
from council.data.cache import FileCache
from council.data.http import DataError, client_scope
from council.policy import LineSpec, Policy

HISTORY_TTL_S = 4 * 3600
MACRO_TTL_S = 6 * 3600
DEFAULT_HISTORY_DAYS = 900       # >= 200-day SMA + one year of sigma estimates, with slack


def default_start(now: datetime) -> date:
    return (to_utc(now) - timedelta(days=DEFAULT_HISTORY_DAYS)).date()


def fetch_line_history(
    line: LineSpec, *, now: datetime, tiingo_token: str | None, client: httpx.Client, start: date
) -> pd.DataFrame:
    """Raw daily signal bars for one line (no cache). Raises DataError on provider failure."""
    source, ticker = line.signal.source, line.signal.ticker
    if source == "tiingo":
        if not tiingo_token:
            raise DataError("tiingo: no API token configured")
        return tiingo.fetch_daily(ticker, start, token=tiingo_token, client=client, now=now)
    if source == "binance":
        return binance.fetch_klines(ticker, "1d", binance.MAX_LIMIT, client=client, now=now)
    raise ValueError(f"history source {source!r} is not fetched by the data layer")


def gather_history(
    policy: Policy,
    *,
    now: datetime,
    tiingo_token: str | None,
    client: httpx.Client | None = None,
    start: date | None = None,
    cache: FileCache | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """({line: daily bars available at now}, quality flags)."""
    asof = to_utc(now).to_pydatetime()
    begin = start or default_start(asof)
    slot_key = clock.slot_at_or_before(asof).isoformat()
    store = cache or FileCache("history")
    history: dict[str, pd.DataFrame] = {}
    flags: list[str] = []
    with client_scope(client) as http:
        for line in policy.universe.lines:
            source = line.signal.source
            if source == "etoro":
                flags.append(f"history_unsupported:{line.symbol}")
                continue
            if source == "tiingo" and not tiingo_token:
                flags.append(f"history_missing:{line.symbol}:no_tiingo_token")
                continue
            request = {
                "source": source,
                "ticker": line.signal.ticker,
                "interval": "1d",
                "start": begin.isoformat(),
                "slot": slot_key,
            }

            def fetch(line: LineSpec = line) -> dict[str, Any]:
                bars = fetch_line_history(
                    line, now=asof, tiingo_token=tiingo_token, client=http, start=begin
                )
                return bars_to_json(bars)

            try:
                raw = store.get_or_fetch(request, HISTORY_TTL_S, fetch, now=asof)
            except DataError:
                flags.append(f"history_failed:{line.symbol}")
                continue
            bars = available_only(bars_from_json(raw), source=source, interval="1d", now=asof)
            if bars.empty:
                flags.append(f"history_empty:{line.symbol}")
            history[line.symbol] = bars
    return history, flags


def _series_to_json(series: pd.Series) -> dict[str, list[Any]]:
    return {
        "date": [pd.Timestamp(d).date().isoformat() for d in series.index],
        "value": [float(v) for v in series.to_numpy()],
    }


def _series_from_json(obj: dict[str, list[Any]], series_id: str) -> pd.Series:
    index = pd.DatetimeIndex(pd.to_datetime(obj.get("date", [])), name="date").tz_localize("UTC")
    return pd.Series([float(v) for v in obj.get("value", [])], index=index, name=series_id, dtype="float64")


def gather_macro(
    series_ids: Iterable[str] = fred.DEFAULT_MACRO,
    *,
    now: datetime,
    client: httpx.Client | None = None,
    cache: FileCache | None = None,
) -> tuple[dict[str, pd.Series], list[str]]:
    """({series_id: observations}, quality flags). Availability is applied by the pack builder."""
    asof = to_utc(now).to_pydatetime()
    slot_key = clock.slot_at_or_before(asof).isoformat()
    store = cache or FileCache("macro")
    out: dict[str, pd.Series] = {}
    flags: list[str] = []
    with client_scope(client) as http:
        for sid in series_ids:

            def fetch(sid: str = sid) -> dict[str, list[Any]]:
                return _series_to_json(fred.fetch_series(sid, client=http))

            try:
                raw = store.get_or_fetch({"fred": sid, "slot": slot_key}, MACRO_TTL_S, fetch, now=asof)
            except DataError:
                flags.append(f"macro_failed:{sid}")
                continue
            out[sid] = _series_from_json(raw, sid)
    return out, flags
