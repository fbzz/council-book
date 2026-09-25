"""eToro market data (candles, rates) through a READ-only `get_json(path, params)` callable.

The broker read client (auth headers, request ids, rate-limit pools) is owned by the broker module;
this module only shapes paths and parses payloads. Rules:
- Candles: `fromDate` is the candle START; a candle is used only when fromDate + interval <= now.
- Rates: a quote needs a positive bid and an ask >= bid; anything else is dropped, not guessed.
- Payload key casing varies between routes (instrumentId/instrumentID/InstrumentID): parse all.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any, Literal

import pandas as pd

from council.clock import utcnow
from council.data.bars import bars_from_rows, completed_only, empty_bars, to_utc
from council.data.http import DataError
from council.models.broker import Quote

GetJson = Callable[[str, dict[str, Any] | None], Any]
EtoroInterval = Literal["OneDay", "FourHours"]

CANDLES_PATH = "/api/v1/market-data/instruments/{instrument_id}/history/candles/desc/{interval}/{count}"
RATES_PATH = "/api/v2/market-data/rates"
MAX_CANDLES = 1000
RATES_BATCH = 50

_ID_KEYS = ("instrumentId", "instrumentID", "InstrumentID", "InstrumentId")
_BID_KEYS = ("bid", "Bid", "bidRate", "BidRate")
_ASK_KEYS = ("ask", "Ask", "askRate", "AskRate")
_TIME_KEYS = ("date", "Date", "timestamp", "Timestamp", "lastUpdated", "time", "rateTime")
_SYMBOL_KEYS = ("symbol", "symbolFull", "SymbolFull", "Symbol")


def _first(row: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _candle_rows(payload: Any) -> list[dict[str, Any]]:
    """Flatten {candles:[{instrumentId, candles:[...]}]} (or a bare list) into candle dicts."""
    groups = payload.get("candles") if isinstance(payload, dict) else payload
    if not isinstance(groups, list):
        raise DataError("etoro candles: no candle list in payload")
    rows: list[dict[str, Any]] = []
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("candles"), list):
            rows.extend(c for c in group["candles"] if isinstance(c, dict))
        elif isinstance(group, dict) and "fromDate" in group:
            rows.append(group)
    return rows


def parse_candles(payload: Any, interval: str, now: datetime) -> pd.DataFrame:
    """Canonical bars from a candles payload, completed candles only."""
    rows = []
    for candle in _candle_rows(payload):
        try:
            start = to_utc(str(candle["fromDate"]))
            rows.append(
                (
                    start,
                    float(candle["open"]),
                    float(candle["high"]),
                    float(candle["low"]),
                    float(candle["close"]),
                    float(candle.get("volume") or 0.0),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataError("etoro candles: malformed candle") from exc
    bars = bars_from_rows(rows) if rows else empty_bars()
    return completed_only(bars, interval, now)


def _rate_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("rates", "Rates", "instrumentRates", "data", "items"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
    raise DataError("etoro rates: no rate list in payload")


def parse_rates(
    payload: Any,
    *,
    wanted: Iterable[int] | None = None,
    symbols: Mapping[int, str] | None = None,
    now: datetime,
) -> dict[int, Quote]:
    """Quotes keyed by instrument id. Invalid rows (bid <= 0, ask < bid) are dropped."""
    wanted_set = set(wanted) if wanted is not None else None
    names = dict(symbols or {})
    out: dict[int, Quote] = {}
    for row in _rate_rows(payload):
        try:
            iid = int(_first(row, _ID_KEYS))
            bid = float(_first(row, _BID_KEYS))
            ask = float(_first(row, _ASK_KEYS))
        except (TypeError, ValueError):
            continue
        if wanted_set is not None and iid not in wanted_set:
            continue
        if not (bid > 0 and ask >= bid):
            continue
        raw_at = _first(row, _TIME_KEYS)
        try:
            at = to_utc(str(raw_at)).to_pydatetime() if raw_at else now
        except (TypeError, ValueError):
            at = now
        symbol = names.get(iid) or str(_first(row, _SYMBOL_KEYS) or iid)
        out[iid] = Quote(symbol=symbol, instrument_id=iid, bid=bid, ask=ask, at=at)
    return out


class EtoroMarketData:
    """Market-data reads over an injected READ client (`get_json(path, params) -> decoded JSON`)."""

    def __init__(self, get_json: GetJson, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._get_json = get_json
        self._clock = clock

    def candles(
        self,
        instrument_id: int,
        interval: EtoroInterval,
        count: int,
        *,
        now: datetime | None = None,
    ) -> pd.DataFrame:
        """The latest `count` candles (newest-first request, returned oldest-first), completed only."""
        if interval not in ("OneDay", "FourHours"):
            raise ValueError(f"unsupported eToro interval {interval!r}")
        if not 1 <= count <= MAX_CANDLES:
            raise ValueError(f"count must be in 1..{MAX_CANDLES}")
        if int(instrument_id) <= 0:
            raise ValueError("instrument_id must be positive")
        path = CANDLES_PATH.format(instrument_id=int(instrument_id), interval=interval, count=count)
        payload = self._get_json(path, None)
        asof = to_utc(now or self._clock()).to_pydatetime()
        return parse_candles(payload, interval, asof)

    def rates(
        self,
        instrument_ids: Iterable[int],
        *,
        symbols: Mapping[int, str] | None = None,
        now: datetime | None = None,
    ) -> dict[int, Quote]:
        """Bid/ask per instrument, batched; ids the broker does not return are simply absent."""
        ids = sorted({int(i) for i in instrument_ids})
        if not ids:
            return {}
        asof = to_utc(now or self._clock()).to_pydatetime()
        out: dict[int, Quote] = {}
        for i in range(0, len(ids), RATES_BATCH):
            batch = ids[i : i + RATES_BATCH]
            payload = self._get_json(RATES_PATH, {"instrumentIds": ",".join(map(str, batch))})
            out.update(parse_rates(payload, wanted=batch, symbols=symbols, now=asof))
        return out
