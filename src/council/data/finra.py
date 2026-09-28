"""FINRA equity short interest (design swing-book.md rev 2, §1.4 fact card and §3.4; on in v1).

Rules:
- Source: FINRA's public Query API, dataset `otcMarket/consolidatedShortInterest` (the bi-monthly
  consolidated short interest for exchange-listed and OTC equities; no credential for public
  datasets). One POST per slot for at most MAX_SYMBOLS exact symbols (a `domainFilters` IN filter on
  `symbolCode`, settlement dates within LOOKBACK_DAYS), connect 5 s / read 10 s, at most one retry
  on a transport error or a 5xx, a 429 fails at once. The endpoint shape is unverified until the
  opt-in canary runs (`COUNCIL_LIVE_CANARY=1`); a parse failure is `unknown`, never a guess.
- Output per symbol: the latest settlement's short position and FINRA's average daily volume and
  days to cover. `pct_of_shares` needs a share count from the caller (SEC `dei` shares outstanding):
  it is a LOWER bound of the percentage of the float (float <= shares outstanding), so the fact
  card labels its basis. Absent -> `unknown` (a short is then halved, S1/S13; never read as `low`).
- Private: never published (data-rights draft row). Percent-only once it reaches the card.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from council.data.http import USER_AGENT, DataError, RateLimited, client_scope, json_body

SOURCE = "finra"
URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_SYMBOLS = 8
LOOKBACK_DAYS = 45
RETRIES = 1
STALE_DAYS = 40                      # a settlement older than this at the slot is `unknown`


@dataclass(frozen=True)
class ShortInterest:
    symbol: str
    settlement_date: date
    short_shares: float
    avg_daily_volume: float | None
    days_to_cover: float | None

    def pct_of_shares(self, shares_outstanding: float | None) -> float | None:
        """Short position in % of shares outstanding (a lower bound of % of float), or None."""
        if not shares_outstanding or shares_outstanding <= 0:
            return None
        return self.short_shares / shares_outstanding * 100.0


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out >= 0 else None


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_rows(payload: Any, *, asof: date) -> dict[str, ShortInterest]:
    """{symbol: the latest settlement on or before `asof`} from the dataset's JSON rows. Rows
    without a symbol, a date or a short position are skipped; a settlement after `asof` (lookahead)
    or older than STALE_DAYS is skipped."""
    if not isinstance(payload, list):
        raise DataError("finra: payload is not a list")
    out: dict[str, ShortInterest] = {}
    for raw in payload:
        if not isinstance(raw, Mapping):
            continue
        symbol = str(raw.get("symbolCode") or "").strip().upper()
        day = _day(raw.get("settlementDate"))
        shares = _num(raw.get("currentShortPositionQuantity"))
        if not symbol or day is None or shares is None:
            continue
        if day > asof or (asof - day).days > STALE_DAYS:
            continue
        held = out.get(symbol)
        if held is None or day > held.settlement_date:
            out[symbol] = ShortInterest(symbol, day, shares, _num(raw.get("averageDailyVolumeQuantity")),
                                        _num(raw.get("daysToCoverQuantity")))
    return out


def request_body(symbols: Sequence[str], asof: date) -> dict[str, Any]:
    start = asof - timedelta(days=LOOKBACK_DAYS)
    return {
        "limit": 4 * len(symbols),
        "fields": ["symbolCode", "settlementDate", "currentShortPositionQuantity",
                   "averageDailyVolumeQuantity", "daysToCoverQuantity"],
        "domainFilters": [{"fieldName": "symbolCode", "values": list(symbols)}],
        "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": start.isoformat(),
                              "endDate": asof.isoformat()}],
        "sortFields": ["-settlementDate"],
    }


def fetch_short_interest(
    symbols: Iterable[str],
    *,
    asof: datetime,
    client: httpx.Client | None = None,
    reserve: Callable[[], None] | None = None,
) -> dict[str, ShortInterest]:
    """The latest short interest for up to MAX_SYMBOLS exact symbols (module rules)."""
    wanted = list(dict.fromkeys(s.strip().upper().replace("_", ".") for s in symbols if s.strip()))
    if not wanted:
        return {}
    if len(wanted) > MAX_SYMBOLS:
        raise ValueError(f"at most {MAX_SYMBOLS} symbols per finra request")
    day = asof.date()
    body = request_body(wanted, day)
    what = f"finra short interest ({len(wanted)} symbols)"
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT}
    last = "no attempt"
    with client_scope(client) as http:
        for _attempt in range(RETRIES + 1):
            if reserve is not None:
                reserve()
            try:
                response = http.post(URL, json=body, headers=headers, timeout=TIMEOUT)
            except httpx.TransportError as exc:
                last = f"transport {type(exc).__name__}"
                continue
            if response.status_code == 429:
                raise RateLimited(f"{what}: HTTP 429")
            if response.status_code >= 500:
                last = f"HTTP {response.status_code}"
                continue
            if response.status_code >= 400:
                raise DataError(f"{what}: HTTP {response.status_code}")
            rows = parse_rows(json_body(response, what=what), asof=day)
            return {s: v for s, v in rows.items() if s in set(wanted)}
    raise DataError(f"{what}: gave up after {RETRIES + 1} attempts ({last})")
