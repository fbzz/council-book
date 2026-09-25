"""FakeEtoro: an in-memory broker behind an httpx.MockTransport, built from ETORO_ROUTES.md.

It exists so the executor can be tortured without a network: scripted open outcomes (filled after
K lookups, partial, rejected, in flight forever, 5xx-but-processed, 5xx-not-processed, lost 202,
crash after processing), scripted close/patch outcomes, 429 with Retry-After, the shared 20/60 s
execution quota, stop-loss hits and price moves. Payload shapes follow the official route specs.

Rules it enforces like the real broker:
- key-pair auth + x-request-id on every call; Authorization together with the key pair → 422.
- v3 opens: exactly one of symbol/instrumentId and of amount/units/contracts; stopLossRate required
  when leverage > 1 or sellShort; a settlement/leverage with no eligible config is accepted (202)
  and rejected afterwards (status 4). The x-request-id is the idempotency key (`referenceId`):
  re-sending it returns the SAME order, never a second one.
- orders:lookup and close-order info return 404 until the handle is known.
All ids are small synthetic integers. Nothing here is a real account.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

OpenOutcome = Literal[
    "fill", "partial", "reject", "rejected_partial", "in_flight",
    "http_4xx", "http_429", "http_5xx_processed", "http_5xx_not_processed",
    "lost_202", "timeout_not_processed", "crash_after_processing",
]
CloseOutcome = Literal[
    "close", "in_flight", "reject_after_submit", "http_4xx", "http_5xx_processed",
    "http_5xx_not_processed", "lost_200", "crash_after_processing",
]
PatchOutcome = Literal["apply", "http_4xx", "http_5xx_processed", "http_5xx_not_processed", "lost_202"]

STATUS_NAMES = {
    1: "Received", 2: "Placed", 3: "Filled", 4: "Rejected", 5: "PartiallyFilled",
    6: "PendingCancel", 7: "Canceled", 8: "Expired", 9: "CanceledPartiallyFilled",
    10: "RejectedPartiallyFilled", 11: "WaitingForMarket", 12: "PendingTriggeredRate",
}
SETTLEMENT_TYPE_IDS = {"cfd": 0, "real": 1, "marginTrade": 3, "realFutures": 4}


class SimulatedCrash(BaseException):  # noqa: N818 - models process death, not an error
    """The process dies mid-request AFTER the broker processed it. BaseException on purpose:
    nothing in the executor may catch it, exactly like a power cut."""


class FakeClock:
    """Deterministic wall clock: `sleep` advances time instead of blocking."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._now.timestamp()

    def sleep(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        self.slept.append(seconds)
        self._now += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@dataclass
class OpenScript:
    outcome: OpenOutcome = "fill"
    after_lookups: int = 0          # lookups that still report in flight before the outcome
    fill_fraction: float = 0.5      # partial / rejected_partial
    units_factor: float = 1.0       # filled units = requested × factor (post-fill mismatch)
    price_factor: float = 1.0       # fill price = touch × factor (slippage / gap)
    exposure_factor: float = 1.0    # broker-reported exposure = units × price × factor
    status_code: int = 400
    retry_after: float = 5.0
    in_flight_status: int = 2


@dataclass
class CloseScript:
    outcome: CloseOutcome = "close"
    after_lookups: int = 0
    status_code: int = 400


@dataclass
class PatchScript:
    outcome: PatchOutcome = "apply"
    status_code: int = 400


@dataclass
class FakeInstrument:
    instrument_id: int
    symbol: str
    bid: float
    ask: float
    row: dict[str, Any]
    cost_bps: float = 5.0


@dataclass
class FakePosition:
    position_id: int
    instrument_id: int
    is_buy: bool
    leverage: int
    units: float
    open_rate: float
    amount: float
    sl_rate: float | None
    settlement: str
    opened_at: datetime
    order_id: int | None = None


@dataclass
class FakeOrder:
    order_id: int
    reference_id: str
    instrument_id: int
    transaction: str
    settlement: str
    leverage: int
    units: float
    sl_rate: float | None
    script: OpenScript
    requested_at: datetime
    lookups: int = 0
    status_id: int = 1
    resolved: bool = False
    eligible: bool = True
    position_id: int | None = None
    filled_units: float = 0.0
    fill_price: float | None = None
    error_message: str | None = None


@dataclass
class FakeCloseOrder:
    order_id: int
    reference_id: str
    position_id: int
    instrument_id: int
    units_to_deduct: float | None
    script: CloseScript
    requested_at: datetime
    lookups: int = 0
    executed: bool = False
    failed: bool = False
    occurred: datetime | None = None
    rate: float | None = None
    units_closed: float | None = None


@dataclass
class Injection:
    method: str
    path_prefix: str
    status: int | None                 # None = transport error
    times: int
    retry_after: float | None = None


@dataclass
class RecordedRequest:
    method: str
    path: str
    params: dict[str, str]
    headers: dict[str, str]
    body: Any


def leverage_config(
    *,
    settlement: str = "CFD",
    direction: str = "LONG",
    leverage_values: Iterable[int] = (1, 2, 5),
    is_potential: bool = False,
    min_position_amount: float = 10.0,
    min_sl_pct: float = 0.0,
    max_sl_pct: float = 100.0,
    allow_sl_tp: bool = True,
) -> dict[str, Any]:
    """A raw eligibility leverageConfigs entry, in broker casing."""
    return {
        "settlementType": settlement, "direction": direction,
        "leverageValues": list(leverage_values), "isPotential": is_potential,
        "minPositionAmount": min_position_amount, "allowEditStopLoss": True,
        "minStopLossPercentage": min_sl_pct, "maxStopLossPercentage": max_sl_pct,
        "defaultStopLossPercentage": max_sl_pct, "allowEditTakeProfit": True,
        "minTakeProfitPercentage": 5.0, "maxTakeProfitPercentage": 1000.0,
        "defaultTakeProfitPercentage": 1000.0, "allowStopLossTakeProfit": allow_sl_tp,
    }


def eligibility_row(
    symbol: str,
    instrument_id: int,
    *,
    configs: list[dict[str, Any]] | None = None,
    min_position_exposure: float = 10.0,
    max_units_per_order: float | None = None,
    allow_open: bool = True,
    allow_close: bool = True,
    allow_partial_close: bool = True,
    units_quantity_type: str = "FractionalUnits",
) -> dict[str, Any]:
    """A raw eligibility row in broker casing (CFD long+short 1/2/5 by default)."""
    if configs is None:
        configs = [leverage_config(direction="LONG"), leverage_config(direction="SHORT")]
    return {
        "instrumentId": instrument_id, "symbol": symbol,
        "minPositionExposure": min_position_exposure, "maxUnitsPerOrder": max_units_per_order,
        "allowOpenPosition": allow_open, "allowClosePosition": allow_close,
        "allowPartialClosePosition": allow_partial_close, "allowMitOrders": True,
        "allowEntryOrders": False, "allowExitOrders": False, "allowTrailingStopLoss": True,
        "requiresW8Ben": None, "unitsQuantityType": units_quantity_type,
        "orderFillBehaviorType": "BestEffort", "allowedOrderQuantityType": "Both",
        "tradeUnitType": "Units", "leverageConfigs": configs,
    }


_ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("GET", re.compile(r"^/api/v1/trading/info/real/pnl$"), "pnl"),
    ("POST", re.compile(r"^/api/v2/trading/info/eligibility$"), "eligibility"),
    ("POST", re.compile(r"^/api/v2/trading/info/costs$"), "costs"),
    ("GET", re.compile(r"^/api/v2/market-data/rates$"), "rates"),
    ("GET", re.compile(
        r"^/api/v1/market-data/instruments/(?P<iid>\d+)/history/candles/"
        r"(?P<direction>asc|desc)/(?P<interval>[A-Za-z]+)/(?P<count>\d+)$"), "candles"),
    ("GET", re.compile(r"^/api/v1/feeds/news$"), "news"),
    ("GET", re.compile(r"^/api/v1/agent-portfolios$"), "agent_portfolios"),
    ("POST", re.compile(r"^/api/v3/trading/execution/orders$"), "open"),
    ("GET", re.compile(r"^/api/v2/trading/info/orders(:|%3A)lookup$"), "lookup"),
    ("POST", re.compile(
        r"^/api/v1/trading/execution/market-close-orders/positions/(?P<pid>\d+)$"), "close"),
    ("GET", re.compile(r"^/api/v1/trading/info/real/close-orders/(?P<oid>\d+)$"), "close_info"),
    ("PATCH", re.compile(r"^/api/v2/trading/positions/(?P<pid>\d+)$"), "patch"),
]
EXECUTION_POOL = frozenset({"open", "close"})
WRITE_ROUTES = frozenset({"open", "close", "patch"})
_INTERVAL_S = {
    "OneMinute": 60, "FiveMinutes": 300, "TenMinutes": 600, "FifteenMinutes": 900,
    "ThirtyMinutes": 1800, "OneHour": 3600, "FourHours": 14400, "OneDay": 86400, "OneWeek": 604800,
}


def _route(method: str, path: str) -> tuple[str, re.Match[str]] | None:
    for route_method, pattern, name in _ROUTES:
        match = pattern.match(path)
        if method == route_method and match:
            return name, match
    return None


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(status: int, payload: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


class FakeEtoro:
    """In-memory broker. Use `transport()` with the real clients."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        credit: float = 10_000.0,
        api_key: str = "test-app-key",
        user_keys: Iterable[str] = ("test-read-key", "test-write-key"),
        write_user_keys: Iterable[str] | None = None,
        execution_limit: int = 20,
        execution_window_s: float = 60.0,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self.credit = credit
        self.api_key = api_key
        self.user_keys = set(user_keys)
        self.write_user_keys = set(write_user_keys) if write_user_keys is not None else None
        self.execution_limit = execution_limit
        self.execution_window_s = execution_window_s
        self.instruments: dict[int, FakeInstrument] = {}
        self.positions: dict[int, FakePosition] = {}
        self.orders: dict[int, FakeOrder] = {}
        self.orders_by_ref: dict[str, FakeOrder] = {}
        self.close_orders: dict[int, FakeCloseOrder] = {}
        self.news: list[dict[str, Any]] = []
        self.requests: list[RecordedRequest] = []
        self.stop_hits: list[int] = []
        self.patches: list[tuple[int, float | None]] = []
        self._open_scripts: deque[OpenScript] = deque()
        self._close_scripts: deque[CloseScript] = deque()
        self._patch_scripts: deque[PatchScript] = deque()
        self._injections: list[Injection] = []
        self._execution_log: deque[float] = deque()
        self._next_position = 1001
        self._next_order = 5001
        self._next_close = 7001

    # ============================================================== setup / inspection helpers
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def now(self) -> datetime:
        return self._clock()

    def add_instrument(
        self,
        symbol: str,
        instrument_id: int,
        *,
        bid: float,
        ask: float,
        row: dict[str, Any] | None = None,
        cost_bps: float = 5.0,
        **row_kwargs: Any,
    ) -> FakeInstrument:
        inst = FakeInstrument(
            instrument_id=instrument_id, symbol=symbol, bid=bid, ask=ask,
            row=row or eligibility_row(symbol, instrument_id, **row_kwargs), cost_bps=cost_bps,
        )
        self.instruments[instrument_id] = inst
        return inst

    def instrument(self, symbol_or_id: str | int) -> FakeInstrument:
        if isinstance(symbol_or_id, int):
            return self.instruments[symbol_or_id]
        return next(i for i in self.instruments.values() if i.symbol == symbol_or_id)

    def add_position(
        self,
        symbol: str,
        *,
        is_buy: bool = True,
        units: float,
        leverage: int = 1,
        open_rate: float | None = None,
        sl_rate: float | None = None,
        settlement: str = "cfd",
        opened_at: datetime | None = None,
    ) -> FakePosition:
        inst = self.instrument(symbol)
        rate = open_rate if open_rate is not None else (inst.ask if is_buy else inst.bid)
        pos = FakePosition(
            position_id=self._next_position, instrument_id=inst.instrument_id, is_buy=is_buy,
            leverage=leverage, units=units, open_rate=rate, amount=units * rate / leverage,
            sl_rate=sl_rate, settlement=settlement, opened_at=opened_at or self.now(),
        )
        self._next_position += 1
        self.positions[pos.position_id] = pos
        self.credit -= pos.amount          # the margin leaves cash, like a real open
        return pos

    def script_open(self, outcome: OpenOutcome = "fill", **kwargs: Any) -> OpenScript:
        script = OpenScript(outcome=outcome, **kwargs)
        self._open_scripts.append(script)
        return script

    def script_close(self, outcome: CloseOutcome = "close", **kwargs: Any) -> CloseScript:
        script = CloseScript(outcome=outcome, **kwargs)
        self._close_scripts.append(script)
        return script

    def script_patch(self, outcome: PatchOutcome = "apply", **kwargs: Any) -> PatchScript:
        script = PatchScript(outcome=outcome, **kwargs)
        self._patch_scripts.append(script)
        return script

    def inject(
        self, method: str, path_prefix: str, status: int | None, *, times: int = 1,
        retry_after: float | None = None,
    ) -> None:
        """The next `times` matching requests fail with `status` (None = transport error) and
        are NOT processed."""
        self._injections.append(Injection(method.upper(), path_prefix, status, times, retry_after))

    def consume_execution_quota(self, n: int | None = None) -> None:
        now = self.now().timestamp()
        for _ in range(self.execution_limit if n is None else n):
            self._execution_log.append(now)

    def set_price(self, symbol: str, *, bid: float, ask: float) -> None:
        inst = self.instrument(symbol)
        inst.bid, inst.ask = bid, ask
        self._check_stops(inst)

    def move_price(self, symbol: str, pct: float) -> None:
        inst = self.instrument(symbol)
        self.set_price(symbol, bid=inst.bid * (1 + pct), ask=inst.ask * (1 + pct))

    def hit_stop(self, position_id: int) -> None:
        pos = self.positions[position_id]
        inst = self.instruments[pos.instrument_id]
        rate = pos.sl_rate or (inst.bid if pos.is_buy else inst.ask)
        self._realise(pos, pos.units, rate)
        self.stop_hits.append(position_id)

    def count(self, method: str, path_prefix: str) -> int:
        return sum(
            1 for r in self.requests if r.method == method.upper() and r.path.startswith(path_prefix)
        )

    def equity(self) -> float:
        return self.credit + sum(p.amount for p in self.positions.values()) + sum(
            self._pnl(p) for p in self.positions.values()
        )

    def positions_for(self, symbol: str) -> list[FakePosition]:
        iid = self.instrument(symbol).instrument_id
        return [p for p in self.positions.values() if p.instrument_id == iid]

    # ============================================================== accounting
    def _close_rate(self, pos: FakePosition) -> float:
        inst = self.instruments[pos.instrument_id]
        return inst.bid if pos.is_buy else inst.ask

    def _pnl(self, pos: FakePosition) -> float:
        sign = 1.0 if pos.is_buy else -1.0
        return sign * (self._close_rate(pos) - pos.open_rate) * pos.units

    def _realise(self, pos: FakePosition, units: float, rate: float) -> None:
        units = min(units, pos.units)
        share = units / pos.units if pos.units else 1.0
        margin = pos.amount * share
        sign = 1.0 if pos.is_buy else -1.0
        self.credit += margin + sign * (rate - pos.open_rate) * units
        pos.units -= units
        pos.amount -= margin
        if pos.units <= 1e-12:
            del self.positions[pos.position_id]

    def _check_stops(self, inst: FakeInstrument) -> None:
        for pos in list(self.positions.values()):
            if pos.instrument_id != inst.instrument_id or pos.sl_rate is None:
                continue
            if (pos.is_buy and inst.bid <= pos.sl_rate) or (not pos.is_buy and inst.ask >= pos.sl_rate):
                self._realise(pos, pos.units, pos.sl_rate)
                self.stop_hits.append(pos.position_id)

    def _config_for(self, inst: FakeInstrument, settlement: str, is_buy: bool, leverage: int) -> dict[str, Any] | None:
        direction = "long" if is_buy else "short"
        for cfg in inst.row.get("leverageConfigs", []):
            if (
                str(cfg.get("settlementType", "")).lower() == settlement.lower()
                and str(cfg.get("direction", "")).lower() == direction
                and leverage in cfg.get("leverageValues", [])
                and not cfg.get("isPotential")
            ):
                return cfg
        return None

    # ============================================================== transport
    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = request.content.decode(errors="replace")
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.requests.append(
            RecordedRequest(request.method, path, dict(request.url.params), headers, body)
        )
        if "authorization" in headers and ("x-api-key" in headers or "x-user-key" in headers):
            return _json(422, {"error": "use either the key pair or a bearer token, not both"})
        if "x-request-id" not in headers:
            return _json(400, {"error": "x-request-id is required"})
        if headers.get("x-api-key") != self.api_key or headers.get("x-user-key") not in self.user_keys:
            return _json(401, {"error": "unauthorised"})
        routed = _route(request.method, path)
        if routed is None:
            return _json(404, {"error": "no such route"})
        name, match = routed
        if (
            name in WRITE_ROUTES
            and self.write_user_keys is not None
            and headers.get("x-user-key") not in self.write_user_keys
        ):
            return _json(403, {"error": "token lacks trade.real:write"})
        injected = self._take_injection(request.method, path)
        if injected is not None:
            if injected.status is None:
                raise httpx.ConnectError("injected transport failure", request=request)
            extra = {"Retry-After": str(injected.retry_after)} if injected.retry_after is not None else None
            return _json(injected.status, {"error": "injected"}, extra)
        if name in EXECUTION_POOL:
            throttled = self._execution_quota()
            if throttled is not None:
                return throttled
        handler = getattr(self, f"_route_{name}")
        return handler(request, match, body, headers)

    def _take_injection(self, method: str, path: str) -> Injection | None:
        for inj in self._injections:
            if inj.times > 0 and inj.method == method and path.startswith(inj.path_prefix):
                inj.times -= 1
                return inj
        return None

    def _execution_quota(self) -> httpx.Response | None:
        now = self.now().timestamp()
        while self._execution_log and now - self._execution_log[0] >= self.execution_window_s:
            self._execution_log.popleft()
        if len(self._execution_log) >= self.execution_limit:
            wait = max(1, math.ceil(self._execution_log[0] + self.execution_window_s - now))
            return _json(429, {"error": "execution rate limit"}, {"Retry-After": str(wait)})
        self._execution_log.append(now)
        return None

    # ============================================================== read routes
    def _position_payload(self, pos: FakePosition) -> dict[str, Any]:
        close_rate = self._close_rate(pos)
        exposure = pos.units * close_rate
        return {
            "positionID": pos.position_id, "CID": 1, "openDateTime": _iso(pos.opened_at),
            "openRate": pos.open_rate, "instrumentID": pos.instrument_id, "isBuy": pos.is_buy,
            "takeProfitRate": 0.0, "stopLossRate": pos.sl_rate or 0.0, "mirrorID": 0,
            "amount": pos.amount, "leverage": pos.leverage, "orderID": pos.order_id or 0,
            "orderType": 17, "units": pos.units, "totalFees": 0.0,
            "initialAmountInDollars": pos.amount, "isTslEnabled": False,
            "settlementTypeID": SETTLEMENT_TYPE_IDS.get(pos.settlement, 0),
            "isNoStopLoss": pos.sl_rate is None, "isNoTakeProfit": True,
            "unrealizedPnL": {
                "pnL": self._pnl(pos), "exposureInAccountCurrency": exposure,
                "marginInAccountCurrency": pos.amount, "closeRate": close_rate,
                "closeConversionRate": 1.0, "timestamp": _iso(self.now()),
            },
        }

    def _route_pnl(self, *_: Any) -> httpx.Response:
        positions = [self._position_payload(p) for p in self.positions.values()]
        pending_closes = [
            {"orderID": c.order_id, "positionID": c.position_id}
            for c in self.close_orders.values() if not c.executed and not c.failed
        ]
        return _json(200, {"clientPortfolio": {
            "credit": self.credit,
            "unrealizedPnL": sum(self._pnl(p) for p in self.positions.values()),
            "bonusCredit": 0.0, "accountCurrencyId": 1, "positions": positions,
            "mirrors": [], "orders": [], "ordersForOpen": [], "ordersForClose": pending_closes,
            "ordersForCloseMultiple": [],
        }})

    def _route_eligibility(self, _req: httpx.Request, _m: Any, body: Any, _h: Any) -> httpx.Response:
        body = body or {}
        symbols = list(body.get("symbols") or [])
        ids = list(body.get("instrumentIds") or [])
        if len(symbols) + len(ids) > 100:
            return _json(400, {"error": "at most 100 instruments"})
        rows, missing_syms, missing_ids = [], [], []
        by_symbol = {i.symbol.upper(): i for i in self.instruments.values()}
        for sym in symbols:
            inst = by_symbol.get(str(sym).upper())
            if inst is None:
                missing_syms.append(sym)
            else:
                rows.append(inst.row)
        for iid in ids:
            inst = self.instruments.get(int(iid))
            if inst is None:
                missing_ids.append(iid)
            else:
                rows.append(inst.row)
        return _json(200, {
            "currency": body.get("currency", "USD"), "eligibilities": rows,
            "notFoundInstrumentIds": missing_ids, "notFoundSymbols": missing_syms,
        })

    def _route_costs(self, _req: httpx.Request, _m: Any, body: Any, _h: Any) -> httpx.Response:
        body = body or {}
        if body.get("action") == "close":
            pos = self.positions.get(int((body.get("positionIds") or [0])[0]))
            if pos is None:
                return _json(404, {"error": "position not found"})
            inst = self.instruments[pos.instrument_id]
            notional = pos.units * self._close_rate(pos)
        else:
            inst = self.instruments.get(int(body.get("instrumentId") or 0))
            if inst is None:
                return _json(404, {"error": "instrument not found"})
            notional = float(body.get("amount") or 0.0) * int(body.get("leverage") or 1)
        fee = notional * inst.cost_bps / 1e4
        return _json(200, {
            "instrumentId": inst.instrument_id, "symbol": inst.symbol,
            "costs": [
                {"costType": "markup", "amount": fee * 0.5, "currency": "USD"},
                {"costType": "marketSpread", "amount": fee * 0.5, "currency": "USD"},
                {"costType": "transactionFee", "amount": 0.0, "currency": "USD"},
                {"costType": "overnightFee", "amount": 0.0, "currency": "USD"},
            ],
            "lastUpdated": _iso(self.now()),
        })

    def _route_rates(self, req: httpx.Request, *_: Any) -> httpx.Response:
        ids = [int(x) for x in str(req.url.params.get("instrumentIds", "")).split(",") if x.strip()]
        rates = [
            {"instrumentID": i, "bid": self.instruments[i].bid, "ask": self.instruments[i].ask,
             "lastExecution": self.instruments[i].ask, "date": _iso(self.now())}
            for i in ids if i in self.instruments
        ]
        return _json(200, {"rates": rates})

    def _route_candles(self, _req: httpx.Request, match: re.Match[str], *_: Any) -> httpx.Response:
        iid = int(match["iid"])
        inst = self.instruments.get(iid)
        if inst is None or match["interval"] not in _INTERVAL_S:
            return _json(404, {"error": "unknown instrument or interval"})
        step = _INTERVAL_S[match["interval"]]
        mid = (inst.bid + inst.ask) / 2
        now = self.now()
        candles = []
        for k in range(1, min(int(match["count"]), 1000) + 1):
            start = now - timedelta(seconds=step * k)
            candles.append({
                "instrumentID": iid, "fromDate": _iso(start), "open": mid, "high": mid,
                "low": mid, "close": mid, "volume": 0.0,
            })
        if match["direction"] == "asc":
            candles.reverse()
        return _json(200, {"interval": match["interval"], "candles": [
            {"instrumentId": iid, "candles": candles, "rangeOpen": mid, "rangeClose": mid}
        ]})

    def _route_news(self, req: httpx.Request, *_: Any) -> httpx.Response:
        take = int(req.url.params.get("take", 20))
        offset = int(req.url.params.get("offset", 0))
        return _json(200, {"discussions": self.news[offset : offset + take], "paging": {}})

    def _route_agent_portfolios(self, *_: Any) -> httpx.Response:
        return _json(200, {"agentPortfolios": [{
            "agentPortfolioName": "FakePort", "agentPortfolioVirtualBalance": self.credit,
            "userTokens": [{"userTokenName": "fake-read", "scopeNames": ["etoro-public:trade.real:read"]}],
        }]})

    # ============================================================== opens
    def _route_open(self, request: httpx.Request, _m: Any, body: Any, headers: dict[str, str]) -> httpx.Response:
        body = body if isinstance(body, dict) else {}
        reference_id = headers["x-request-id"]
        existing = self.orders_by_ref.get(reference_id)
        if existing is not None:  # idempotency: the same request id is the same order
            return _json(202, {"token": str(uuid.uuid4()), "orderId": existing.order_id,
                               "referenceId": reference_id})
        error = self._validate_open(body)
        if error:
            return _json(400, {"error": error})
        script = self._open_scripts.popleft() if self._open_scripts else OpenScript()
        if script.outcome == "http_4xx":
            return _json(script.status_code, {"error": "rejected by fake"})
        if script.outcome == "http_429":
            return _json(429, {"error": "throttled"}, {"Retry-After": str(script.retry_after)})
        if script.outcome == "http_5xx_not_processed":
            return _json(503, {"error": "unavailable"})
        if script.outcome == "timeout_not_processed":
            raise httpx.ReadTimeout("fake timeout before processing", request=request)
        inst = self.instruments[int(body["instrumentId"])]
        is_buy = body["transaction"] == "buy"
        order = FakeOrder(
            order_id=self._next_order, reference_id=reference_id,
            instrument_id=inst.instrument_id, transaction=body["transaction"],
            settlement=str(body["settlementType"]), leverage=int(body.get("leverage") or 1),
            units=float(body["units"]), sl_rate=body.get("stopLossRate"), script=script,
            requested_at=self.now(),
            eligible=self._config_for(inst, str(body["settlementType"]), is_buy, int(body.get("leverage") or 1)) is not None,
        )
        self._next_order += 1
        self.orders[order.order_id] = order
        self.orders_by_ref[reference_id] = order
        if script.after_lookups == 0:
            self._resolve_open(order)
        if script.outcome == "http_5xx_processed":
            return _json(500, {"error": "internal error"})
        if script.outcome == "lost_202":
            raise httpx.ReadTimeout("fake: 202 lost on the way back", request=request)
        if script.outcome == "crash_after_processing":
            raise SimulatedCrash("process died after the broker accepted the order")
        return _json(202, {"token": str(uuid.uuid4()), "orderId": order.order_id,
                           "referenceId": reference_id})

    def _validate_open(self, body: dict[str, Any]) -> str | None:
        if body.get("action") != "open":
            return "action must be open"
        if body.get("transaction") not in ("buy", "sellShort"):
            return "transaction must be buy or sellShort"
        if ("symbol" in body) == ("instrumentId" in body):
            return "exactly one of symbol / instrumentId"
        if sum(k in body for k in ("amount", "units", "contracts")) != 1:
            return "exactly one of amount / units / contracts"
        if "units" not in body:
            return "fake supports units only"
        if not body.get("settlementType"):
            return "settlementType is mandatory"
        if int(body.get("instrumentId") or 0) not in self.instruments:
            return "unknown instrument"
        leverage = int(body.get("leverage") or 1)
        if (leverage > 1 or body["transaction"] == "sellShort") and not body.get("stopLossRate"):
            return "stopLossRate required"
        return None

    def _resolve_open(self, order: FakeOrder) -> None:
        order.resolved = True
        script = order.script
        if not order.eligible:
            order.status_id, order.error_message = 4, "no eligible leverage config"
            return
        if script.outcome == "reject":
            order.status_id, order.error_message = 4, "rejected by fake"
            return
        if script.outcome == "in_flight":
            order.status_id = script.in_flight_status
            order.resolved = False
            return
        fraction = script.fill_fraction if script.outcome in ("partial", "rejected_partial") else 1.0
        inst = self.instruments[order.instrument_id]
        is_buy = order.transaction == "buy"
        price = (inst.ask if is_buy else inst.bid) * script.price_factor
        units = order.units * fraction * script.units_factor
        margin = units * price / order.leverage
        if margin > self.credit:
            order.status_id, order.error_message = 4, "insufficient funds"
            return
        self.credit -= margin
        pos = FakePosition(
            position_id=self._next_position, instrument_id=order.instrument_id, is_buy=is_buy,
            leverage=order.leverage, units=units, open_rate=price, amount=margin,
            sl_rate=order.sl_rate, settlement=order.settlement, opened_at=self.now(),
            order_id=order.order_id,
        )
        self._next_position += 1
        self.positions[pos.position_id] = pos
        order.position_id, order.filled_units, order.fill_price = pos.position_id, units, price
        order.status_id = {"partial": 5, "rejected_partial": 10}.get(script.outcome, 3)

    def _route_lookup(self, req: httpx.Request, *_: Any) -> httpx.Response:
        ref = req.url.params.get("referenceId")
        oid = req.url.params.get("orderId")
        order = self.orders_by_ref.get(ref) if ref else self.orders.get(int(oid)) if oid else None
        if order is None:
            return _json(404, {"error": "order not found"})
        order.lookups += 1
        if order.script.outcome == "in_flight":
            order.status_id = order.script.in_flight_status
        elif not order.resolved:
            if order.lookups > order.script.after_lookups:
                self._resolve_open(order)
            else:
                order.status_id = 1 if order.lookups == 1 else 2
        return _json(200, self._lookup_payload(order))

    def _lookup_payload(self, order: FakeOrder) -> dict[str, Any]:
        inst = self.instruments[order.instrument_id]
        executions = []
        if order.position_id is not None and order.fill_price is not None:
            pos = self.positions.get(order.position_id)
            exposure = order.filled_units * order.fill_price * order.script.exposure_factor
            executions.append({
                "positionId": order.position_id, "state": "open" if pos else "closed",
                "investedAmountCurrency": order.filled_units * order.fill_price / order.leverage,
                "initialExposureAccountCurrency": exposure,
                "marginAccountCurrency": order.filled_units * order.fill_price / order.leverage,
                "remainingUnits": pos.units if pos else 0.0,
                "stopLossRate": order.sl_rate,
                "openingData": {
                    "executionTime": _iso(order.requested_at), "units": order.filled_units,
                    "avgPrice": order.fill_price, "marketSpread": 0.0, "markup": 0.0,
                    "fees": 0.0, "taxes": 0.0,
                },
            })
        failed = order.status_id in (4, 7, 8, 9, 10)
        return {
            "orderId": order.order_id, "referenceId": order.reference_id, "action": "open",
            "transaction": order.transaction, "type": "mkt",
            "status": {"id": order.status_id, "name": STATUS_NAMES.get(order.status_id, ""),
                       "errorCode": 1 if failed else 0,
                       "errorMessage": order.error_message if failed else None},
            "asset": {"symbol": inst.symbol, "instrumentId": inst.instrument_id, "currency": "USD",
                      "settlementType": order.settlement, "leverage": order.leverage,
                      "side": "long" if order.transaction == "buy" else "short"},
            "requestedUnits": order.units, "openStopLossRate": order.sl_rate,
            "stopLossType": "fixed", "totalCosts": 0.0, "positionExecutions": executions,
            "requestTime": _iso(order.requested_at), "lastUpdate": _iso(self.now()),
            "requestType": "byUnits",
        }

    # ============================================================== closes
    def _route_close(self, request: httpx.Request, match: re.Match[str], body: Any, headers: dict[str, str]) -> httpx.Response:
        body = body if isinstance(body, dict) else {}
        pos = self.positions.get(int(match["pid"]))
        if pos is None:
            return _json(404, {"error": "position not found"})
        if int(body.get("InstrumentId") or 0) != pos.instrument_id:
            return _json(400, {"error": "InstrumentId does not match the position"})
        deduct = body.get("UnitsToDeduct")
        if deduct is not None:
            deduct = float(deduct)
            if deduct <= 0 or deduct > pos.units + 1e-9:
                return _json(400, {"error": "invalid UnitsToDeduct"})
            row = self.instruments[pos.instrument_id].row
            if deduct < pos.units - 1e-9 and not row.get("allowPartialClosePosition", True):
                return _json(400, {"error": "partial close not allowed"})
        script = self._close_scripts.popleft() if self._close_scripts else CloseScript()
        if script.outcome == "http_4xx":
            return _json(script.status_code, {"error": "rejected by fake"})
        if script.outcome == "http_5xx_not_processed":
            return _json(503, {"error": "unavailable"})
        order = FakeCloseOrder(
            order_id=self._next_close, reference_id=headers["x-request-id"],
            position_id=pos.position_id, instrument_id=pos.instrument_id,
            units_to_deduct=deduct, script=script, requested_at=self.now(),
        )
        self._next_close += 1
        self.close_orders[order.order_id] = order
        if script.outcome == "reject_after_submit":
            order.failed = True
        elif script.outcome != "in_flight" and script.after_lookups == 0:
            self._execute_close(order)
        if script.outcome == "http_5xx_processed":
            return _json(500, {"error": "internal error"})
        if script.outcome == "lost_200":
            raise httpx.ReadTimeout("fake: 200 lost on the way back", request=request)
        if script.outcome == "crash_after_processing":
            raise SimulatedCrash("process died after the broker accepted the close")
        return _json(200, {"orderForClose": {
            "positionID": pos.position_id, "instrumentID": pos.instrument_id,
            "unitsToDeduct": deduct, "orderID": order.order_id, "orderType": 19, "statusID": 1,
            "CID": 1, "openDateTime": _iso(pos.opened_at), "lastUpdate": _iso(self.now()),
        }, "token": str(uuid.uuid4())})

    def _execute_close(self, order: FakeCloseOrder) -> None:
        pos = self.positions.get(order.position_id)
        order.executed = True
        order.occurred = self.now()
        if pos is None:  # already gone (stop hit): nothing to do
            order.units_closed = 0.0
            return
        units = order.units_to_deduct if order.units_to_deduct is not None else pos.units
        order.rate = self._close_rate(pos)
        order.units_closed = min(units, pos.units)
        self._realise(pos, units, order.rate)

    def _route_close_info(self, _req: httpx.Request, match: re.Match[str], *_: Any) -> httpx.Response:
        order = self.close_orders.get(int(match["oid"]))
        if order is None:
            return _json(404, {"error": "close order not found"})
        order.lookups += 1
        if (
            not order.executed and not order.failed and order.script.outcome != "in_flight"
            and order.lookups > order.script.after_lookups
        ):
            self._execute_close(order)
        parts = [{
            "positionID": order.position_id, "occurred": _iso(order.occurred) if order.occurred else None,
            "rate": order.rate, "units": order.units_closed, "conversionRate": 1.0, "amount": None,
        }]
        return _json(200, {
            "orderID": order.order_id, "CID": 1,
            "statusID": 4 if order.failed else (3 if order.executed else 1),
            "referenceID": order.reference_id, "orderType": 19, "operationType": 1,
            "errorCode": 1 if order.failed else None,
            "errorMessage": "rejected by fake" if order.failed else None,
            "instrumentID": order.instrument_id, "requestOccurred": _iso(order.requested_at),
            "proceeds": None, "externalOperationType": 0, "positions": parts,
        })

    # ============================================================== stop-loss PATCH
    def _route_patch(self, request: httpx.Request, match: re.Match[str], body: Any, headers: dict[str, str]) -> httpx.Response:
        body = body if isinstance(body, dict) else {}
        pos = self.positions.get(int(match["pid"]))
        if pos is None:
            return _json(409, {"error": "position closed"})
        script = self._patch_scripts.popleft() if self._patch_scripts else PatchScript()
        if script.outcome == "http_4xx":
            return _json(script.status_code, {"error": "rejected by fake"})
        if script.outcome == "http_5xx_not_processed":
            return _json(503, {"error": "unavailable"})
        if body.get("clearStopLoss"):
            pos.sl_rate = None
        elif body.get("stopLossRate") is not None:
            pos.sl_rate = float(body["stopLossRate"])
        self.patches.append((pos.position_id, pos.sl_rate))
        if script.outcome == "http_5xx_processed":
            return _json(500, {"error": "internal error"})
        if script.outcome == "lost_202":
            raise httpx.ReadTimeout("fake: 202 lost on the way back", request=request)
        return _json(202, {"operationId": str(uuid.uuid4()), "positionId": pos.position_id,
                           "referenceId": headers["x-request-id"]})


__all__ = [
    "CloseScript", "FakeClock", "FakeEtoro", "FakeInstrument", "FakePosition", "OpenScript",
    "PatchScript", "RecordedRequest", "SimulatedCrash", "eligibility_row", "leverage_config",
]
