"""READ-ONLY eToro client for the Agent Portfolio READ token (runner, watch, console).

Rules:
- This module has NO write methods. The only POSTs it can send go to the allow-listed read routes
  (eligibility and the costs what-if); anything else raises before a request is built. An AST
  test in tests/contract pins the public method set.
- Auth is the key pair with an x-request-id on every call (see broker/http.py); reads retry on
  429/5xx/transport errors and raise BrokerAuthError on 401/403 at once.
- Order and close-order lookups return None on 404 ("not found (yet)"), never an error.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from council.broker.eligibility import parse_eligibility
from council.broker.http import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, BrokerHTTP, Sleep
from council.broker.parsing import PortfolioRead, SymbolFor, parse_pnl
from council.models.broker import EligibilityRow

ELIGIBILITY_PATH = "/api/v2/trading/info/eligibility"
COSTS_PATH = "/api/v2/trading/info/costs"
READ_POST_ALLOWLIST = frozenset({ELIGIBILITY_PATH, COSTS_PATH})

PNL_PATH = "/api/v1/trading/info/real/pnl"
RATES_PATH = "/api/v2/market-data/rates"
NEWS_PATH = "/api/v1/feeds/news"
AGENT_PORTFOLIOS_PATH = "/api/v1/agent-portfolios"
ORDER_LOOKUP_PATH = "/api/v2/trading/info/orders:lookup"
CLOSE_ORDER_PATH = "/api/v1/trading/info/real/close-orders/{order_id}"
CANDLES_PATH = "/api/v1/market-data/instruments/{instrument_id}/history/candles/{direction}/{interval}/{count}"

CANDLE_INTERVALS = frozenset({
    "OneMinute", "FiveMinutes", "TenMinutes", "FifteenMinutes", "ThirtyMinutes",
    "OneHour", "FourHours", "OneDay", "OneWeek",
})
MAX_CANDLES = 1000
ELIGIBILITY_BATCH = 100      # ≤ 100 symbols + ids per eligibility call
MAX_NEWS_TAKE = 100


class EtoroReadClient:
    """Read routes only. Construct with the READ token; it cannot place, close or modify."""

    def __init__(
        self,
        api_key: str,
        user_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        sleep: Sleep = time.sleep,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    ) -> None:
        self._http = BrokerHTTP(
            api_key, user_key, base_url=base_url, transport=transport, timeout=timeout, sleep=sleep
        )

    def __repr__(self) -> str:
        return f"EtoroReadClient({self._http!r})"

    def disconnect(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------------ generic reads
    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET any read route; retries 429/5xx; raises on other errors."""
        return self._http.read("GET", path, params=params)

    def post_read(self, path: str, body: Mapping[str, Any]) -> Any:
        """POST to an allow-listed READ route (eligibility, costs what-if). Anything else raises."""
        if path not in READ_POST_ALLOWLIST:
            raise ValueError(f"POST {path} is not an allow-listed read route")
        return self._http.read("POST", path, json_body=dict(body))

    # ------------------------------------------------------------------------ portfolio
    def pnl(self) -> dict[str, Any]:
        """Raw `/real/pnl` payload (PRIVATE: USD amounts and position ids)."""
        return self.get_json(PNL_PATH)

    def portfolio(self, symbol_for: SymbolFor = None) -> PortfolioRead:
        """Parsed `/real/pnl`: positions, exposures and equity."""
        return parse_pnl(self.pnl(), symbol_for)

    # ------------------------------------------------------------------------ eligibility / costs
    def eligibility(
        self,
        symbols: Sequence[str] | None = None,
        instrument_ids: Sequence[int] | None = None,
        *,
        currency: str = "USD",
        fetched_at: datetime | None = None,
    ) -> list[EligibilityRow]:
        """Eligibility rows by exact symbols and/or instrument ids, batched ≤ 100 per call."""
        items: list[tuple[str, Any]] = [("symbols", s) for s in (symbols or [])]
        items += [("instrumentIds", int(i)) for i in (instrument_ids or [])]
        if not items:
            raise ValueError("eligibility needs at least one symbol or instrument id")
        when = fetched_at or datetime.now(UTC)
        rows: list[EligibilityRow] = []
        for start in range(0, len(items), ELIGIBILITY_BATCH):
            batch = items[start : start + ELIGIBILITY_BATCH]
            body: dict[str, Any] = {"currency": currency}
            syms = [v for k, v in batch if k == "symbols"]
            ids = [v for k, v in batch if k == "instrumentIds"]
            if ids:
                body["instrumentIds"] = ids
            if syms:
                body["symbols"] = syms
            rows.extend(parse_eligibility(self.post_read(ELIGIBILITY_PATH, body), when))
        return rows

    def costs(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Costs what-if for a hypothetical open or close (read-only; nothing is placed)."""
        if body.get("action") not in ("open", "close"):
            raise ValueError("costs body needs action 'open' or 'close'")
        return self.post_read(COSTS_PATH, body)

    # ------------------------------------------------------------------------ market data / feeds
    def candles(
        self, instrument_id: int, interval: str, count: int, direction: str = "desc"
    ) -> dict[str, Any]:
        if interval not in CANDLE_INTERVALS:
            raise ValueError(f"unknown candle interval {interval!r}")
        if direction not in ("asc", "desc"):
            raise ValueError("candle direction must be 'asc' or 'desc'")
        if not 1 <= int(count) <= MAX_CANDLES:
            raise ValueError(f"candle count must be 1..{MAX_CANDLES}")
        path = CANDLES_PATH.format(
            instrument_id=int(instrument_id), direction=direction, interval=interval, count=int(count)
        )
        return self.get_json(path)

    def rates(self, ids: Iterable[int]) -> dict[str, Any]:
        id_list = [int(i) for i in ids]
        if not id_list:
            raise ValueError("rates needs at least one instrument id")
        return self.get_json(RATES_PATH, {"instrumentIds": ",".join(str(i) for i in id_list)})

    def feeds_news(self, take: int = 50, offset: int = 0) -> dict[str, Any]:
        if not 1 <= int(take) <= MAX_NEWS_TAKE:
            raise ValueError(f"take must be 1..{MAX_NEWS_TAKE}")
        return self.get_json(NEWS_PATH, {"take": int(take), "offset": int(offset)})

    def agent_portfolios(self) -> dict[str, Any]:
        """Agent Portfolio metadata (token names, scopes, expiry). Never contains token secrets."""
        return self.get_json(AGENT_PORTFOLIOS_PATH)

    # ------------------------------------------------------------------------ order outcomes
    def order_lookup(
        self, reference_id: str | None = None, order_id: int | None = None
    ) -> dict[str, Any] | None:
        """Outcome of an open order by referenceId (= the x-request-id it was sent with) or
        orderId. Exactly one handle. None = not found (yet)."""
        if (reference_id is None) == (order_id is None):
            raise ValueError("pass exactly one of reference_id / order_id")
        params = {"referenceId": str(reference_id)} if reference_id is not None else {"orderId": int(order_id)}  # type: ignore[arg-type]
        return self._http.read("GET", ORDER_LOOKUP_PATH, params=params, allow_404=True)

    def close_order_info(self, order_id: int) -> dict[str, Any] | None:
        """Outcome of a market-close order. None = not found (yet)."""
        path = CLOSE_ORDER_PATH.format(order_id=int(order_id))
        return self._http.read("GET", path, allow_404=True)
