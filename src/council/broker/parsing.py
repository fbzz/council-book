"""Defensive parsers for eToro read payloads. Key casing differs between routes (`positionID` vs
`positionId`, `instrumentID` vs `instrumentId`), so every lookup is case-insensitive.

Rules:
- equity = credit + Σ position amount (margin) + unrealised PnL (port of the lab's
  `calculate_components`); the per-position PnL sum is used when the total is absent.
- position exposure = `unrealizedPnL.exposureInAccountCurrency` when present, else
  units × closeRate × closeConversionRate, else units × openRate. The first two are the broker's
  figure and are also kept on the Position (`exposure_usd`, `close_rate`) so the planner sizes
  from the same number the risk engine used; the open-rate fallback is flagged in the snapshot.
- `settlementTypeID`: 0 CFD, 1 real, 2 SWAP (treated as CFD), 3 crypto margin trade, 4 future.
- A position flagged `isNoStopLoss` or with a non-positive stop rate has NO stop-loss (None).
- An instrument the caller cannot map becomes `UNMAPPED_<instrumentId>` — locked, never cash.
- Order status ids follow ETORO_ROUTES.md: 3 filled; 5 partially filled (terminal after its poll
  window); 4/7/8 failed; 9/10 failed after a partial fill; 1/2/6/11/12 in flight.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from council.models.broker import ExposureSnapshot, Position, Quote
from council.models.common import Settlement, Strict

_MISSING = object()

SETTLEMENT_BY_TYPE_ID: dict[int, Settlement] = {
    0: "cfd", 1: "real", 2: "cfd", 3: "marginTrade", 4: "realFutures",
}

STATUS_RECEIVED = 1
STATUS_PLACED = 2
STATUS_FILLED = 3
STATUS_REJECTED = 4
STATUS_PARTIALLY_FILLED = 5
STATUS_FAILED = frozenset({4, 7, 8})                 # nothing filled
STATUS_FAILED_AFTER_PARTIAL = frozenset({9, 10})     # some units filled, then cancelled/rejected
STATUS_IN_FLIGHT = frozenset({1, 2, 6, 11, 12})

SymbolFor = Callable[[int], str | None] | Mapping[int, str] | None


def pick(data: Any, *names: str, default: Any = None) -> Any:
    """Case-insensitive key lookup; the first name present wins."""
    if not isinstance(data, Mapping):
        return default
    for name in names:
        if name in data:
            return data[name]
    lowered = {str(k).lower(): v for k, v in data.items()}
    for name in names:
        value = lowered.get(name.lower(), _MISSING)
        if value is not _MISSING:
            return value
    return default


def as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out) or out != int(out):
        return default
    return int(out)


def as_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _symbol_lookup(symbol_for: SymbolFor) -> Callable[[int], str | None]:
    if symbol_for is None:
        return lambda _iid: None
    if isinstance(symbol_for, Mapping):
        return symbol_for.get
    return symbol_for


def unmapped_symbol(instrument_id: int) -> str:
    return f"UNMAPPED_{instrument_id}"


# ------------------------------------------------------------------------------- portfolio / PnL
class PortfolioRead(Strict):
    """A parsed `/real/pnl` read. PRIVATE (USD amounts and position ids)."""

    credit_usd: float
    unrealized_pnl_usd: float
    invested_usd: float
    equity_usd: float
    positions: list[Position]
    exposure_usd: dict[int, float] = Field(default_factory=dict)   # position id -> |exposure|
    pending_orders: int = 0
    pending_close_position_ids: list[int] = Field(default_factory=list)

    def position(self, position_id: int) -> Position | None:
        return next((p for p in self.positions if p.position_id == position_id), None)


def _parse_position(raw: Mapping[str, Any], lookup: Callable[[int], str | None]) -> tuple[Position, float, float] | None:
    position_id = as_int(pick(raw, "positionID", "positionId"))
    instrument_id = as_int(pick(raw, "instrumentID", "instrumentId"))
    units = as_float(pick(raw, "units"))
    open_rate = as_float(pick(raw, "openRate"))
    if position_id is None or instrument_id is None or units is None or open_rate is None:
        return None
    is_buy = bool(pick(raw, "isBuy", default=True))
    no_sl = bool(pick(raw, "isNoStopLoss", default=False))
    sl_rate = as_float(pick(raw, "stopLossRate"))
    if no_sl or sl_rate is None or sl_rate <= 0:
        sl_rate = None
    tp_rate = as_float(pick(raw, "takeProfitRate"))
    settlement = SETTLEMENT_BY_TYPE_ID.get(as_int(pick(raw, "settlementTypeID", "settlementTypeId"), 0) or 0, "cfd")
    symbol = lookup(instrument_id) or unmapped_symbol(instrument_id)
    upnl = pick(raw, "unrealizedPnL", "unrealizedPnl")
    exposure = None
    close_rate = None
    pnl = 0.0
    if isinstance(upnl, Mapping):
        exposure = as_float(pick(upnl, "exposureInAccountCurrency"))
        pnl = as_float(pick(upnl, "pnL", "pnl"), 0.0) or 0.0
        close_rate = as_float(pick(upnl, "closeRate"))
        if close_rate is not None and close_rate <= 0:
            close_rate = None
        if exposure is None and close_rate is not None:
            conversion = as_float(pick(upnl, "closeConversionRate"), 1.0) or 1.0
            exposure = units * close_rate * conversion
    broker_exposure = abs(exposure) if exposure is not None else None
    position = Position(
        position_id=position_id,
        instrument_id=instrument_id,
        symbol=symbol,
        is_buy=is_buy,
        leverage=as_int(pick(raw, "leverage"), 1) or 1,
        units=units,
        open_rate=open_rate,
        amount=as_float(pick(raw, "amount"), 0.0) or 0.0,
        sl_rate=sl_rate,
        tp_rate=tp_rate if tp_rate and tp_rate > 0 else None,
        settlement=settlement,
        opened_at=as_datetime(pick(raw, "openDateTime", "openDate")),
        exposure_usd=broker_exposure,
        close_rate=close_rate,
    )
    if broker_exposure is None:
        broker_exposure = abs(units * open_rate)
    return position, broker_exposure, pnl


def parse_pnl(payload: Any, symbol_for: SymbolFor = None) -> PortfolioRead:
    """Parse `GET /api/v1/trading/info/real/pnl` (envelope `clientPortfolio`, casing-tolerant)."""
    portfolio = pick(payload, "clientPortfolio", default=payload)
    if not isinstance(portfolio, Mapping):
        raise ValueError("pnl payload has no clientPortfolio mapping")
    lookup = _symbol_lookup(symbol_for)
    positions: list[Position] = []
    exposures: dict[int, float] = {}
    pnl_sum = 0.0
    for raw in pick(portfolio, "positions", default=[]) or []:
        parsed = _parse_position(raw, lookup) if isinstance(raw, Mapping) else None
        if parsed is None:
            raise ValueError("pnl payload has a position without id/instrument/units/openRate")
        position, exposure, pnl = parsed
        positions.append(position)
        exposures[position.position_id] = exposure
        pnl_sum += pnl
    credit = as_float(pick(portfolio, "credit"))
    if credit is None:
        raise ValueError("pnl payload has no credit")
    total_upnl = pick(portfolio, "unrealizedPnL", "unrealizedPnl")
    unrealized = as_float(total_upnl) if not isinstance(total_upnl, Mapping) else None
    if unrealized is None:
        unrealized = pnl_sum
    invested = sum(p.amount for p in positions)
    pending_closes = [
        pid
        for raw in (pick(portfolio, "ordersForClose", default=[]) or [])
        if isinstance(raw, Mapping)
        and (pid := as_int(pick(raw, "positionID", "positionId"))) is not None
    ]
    pending = sum(
        len(pick(portfolio, key, default=[]) or [])
        for key in ("orders", "ordersForOpen", "ordersForClose", "ordersForCloseMultiple")
    )
    return PortfolioRead(
        credit_usd=credit,
        unrealized_pnl_usd=unrealized,
        invested_usd=invested,
        equity_usd=credit + invested + unrealized,
        positions=positions,
        exposure_usd=exposures,
        pending_orders=pending,
        pending_close_position_ids=pending_closes,
    )


def snapshot_from_portfolio(read: PortfolioRead, taken_at: datetime) -> ExposureSnapshot:
    """Signed exposure / equity per vehicle symbol. gross = Σ|exposure|/equity (hedged legs add),
    net = Σ signed, margin_use = Σ(|w| / leverage). A position whose exposure fell back to
    units × openRate is flagged (`<symbol>: exposure_from_open_rate`)."""
    equity = read.equity_usd
    if equity <= 0:
        raise ValueError("equity must be positive to express weights")
    signed: dict[str, float] = {}
    sides: dict[str, set[bool]] = {}
    flags: list[str] = []
    gross = 0.0
    margin = 0.0
    for p in read.positions:
        if p.exposure_usd is None:
            note = f"{p.symbol}: exposure_from_open_rate"
            if note not in flags:
                flags.append(note)
        w = read.exposure_usd.get(p.position_id, p.units * p.open_rate) / equity
        signed[p.symbol] = signed.get(p.symbol, 0.0) + (w if p.is_buy else -w)
        sides.setdefault(p.symbol, set()).add(p.is_buy)
        gross += w
        margin += w / max(1, p.leverage)
    return ExposureSnapshot(
        taken_at=taken_at,
        equity_usd=equity,
        credit_usd=read.credit_usd,
        positions=list(read.positions),
        signed_w=signed,
        gross=gross,
        net=sum(signed.values()),
        margin_use=margin,
        unmapped=sorted(s for s in signed if s.startswith("UNMAPPED_")),
        hedged=sorted(s for s, seen in sides.items() if len(seen) == 2),
        flags=flags,
    )


# ------------------------------------------------------------------------------------ rates
def parse_rates(payload: Any, symbol_for: SymbolFor = None, *, at: datetime | None = None) -> dict[str, Quote]:
    """Parse `GET /api/v2/market-data/rates` into quotes keyed by symbol (bid/ask per instrument)."""
    rows = pick(payload, "rates", default=payload) if isinstance(payload, Mapping) else payload
    lookup = _symbol_lookup(symbol_for)
    out: dict[str, Quote] = {}
    for raw in rows or []:
        if not isinstance(raw, Mapping):
            continue
        iid = as_int(pick(raw, "instrumentID", "instrumentId"))
        bid = as_float(pick(raw, "bid"))
        ask = as_float(pick(raw, "ask"))
        if iid is None or bid is None or ask is None or bid <= 0 or ask <= 0:
            continue
        symbol = lookup(iid) or unmapped_symbol(iid)
        when = as_datetime(pick(raw, "date", "timestamp")) or at or datetime.now(UTC)
        out[symbol] = Quote(symbol=symbol, instrument_id=iid, bid=bid, ask=ask, at=when)
    return out


# ------------------------------------------------------------------------------ order lookup
class Execution(Strict):
    position_id: int | None = None
    units: float = 0.0
    avg_price: float | None = None
    exposure_usd: float | None = None
    sl_rate: float | None = None
    remaining_units: float | None = None


class OrderStatus(Strict):
    """A parsed `orders:lookup` result. PRIVATE."""

    order_id: int | None = None
    reference_id: str | None = None
    status_id: int
    status_name: str = ""
    error_code: int | None = None
    error_message: str | None = None
    sl_rate: float | None = None
    executions: list[Execution] = Field(default_factory=list)

    @property
    def filled_units(self) -> float:
        return sum(e.units for e in self.executions)

    @property
    def avg_price(self) -> float | None:
        priced = [(e.units, e.avg_price) for e in self.executions if e.avg_price and e.units > 0]
        units = sum(u for u, _ in priced)
        return sum(u * p for u, p in priced) / units if units > 0 else None

    @property
    def broker_exposure_usd(self) -> float | None:
        values = [e.exposure_usd for e in self.executions if e.exposure_usd is not None]
        return sum(values) if values else None

    @property
    def position_ids(self) -> list[int]:
        return [e.position_id for e in self.executions if e.position_id is not None]


def parse_order_status(payload: Any) -> OrderStatus:
    status = pick(payload, "status", default={})
    if isinstance(status, Mapping):
        status_id = as_int(pick(status, "id", "statusId", "statusID"))
        name = str(pick(status, "name", default="") or "")
        error_code = as_int(pick(status, "errorCode"))
        error_message = pick(status, "errorMessage")
    else:
        status_id = as_int(status)
        name, error_code, error_message = "", None, None
    if status_id is None:
        status_id = as_int(pick(payload, "statusID", "statusId"))
    if status_id is None:
        raise ValueError("order lookup payload has no status id")
    executions = []
    for raw in pick(payload, "positionExecutions", default=[]) or []:
        if not isinstance(raw, Mapping):
            continue
        opening = pick(raw, "openingData", default={}) or {}
        executions.append(
            Execution(
                position_id=as_int(pick(raw, "positionId", "positionID")),
                units=as_float(pick(opening, "units"), None) or as_float(pick(raw, "units"), 0.0) or 0.0,
                avg_price=as_float(pick(opening, "avgPrice")),
                exposure_usd=as_float(pick(raw, "initialExposureAccountCurrency")),
                sl_rate=as_float(pick(raw, "stopLossRate")),
                remaining_units=as_float(pick(raw, "remainingUnits")),
            )
        )
    return OrderStatus(
        order_id=as_int(pick(payload, "orderId", "orderID")),
        reference_id=pick(payload, "referenceId", "referenceID"),
        status_id=status_id,
        status_name=name,
        error_code=error_code,
        error_message=str(error_message) if error_message else None,
        sl_rate=as_float(pick(payload, "openStopLossRate")),
        executions=executions,
    )


# ------------------------------------------------------------------------------ close orders
class ClosedPart(Strict):
    position_id: int
    occurred: datetime | None = None
    rate: float | None = None
    units: float | None = None


class CloseOrderStatus(Strict):
    order_id: int | None = None
    status_id: int | None = None
    error_code: int | None = None
    error_message: str | None = None
    positions: list[ClosedPart] = Field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.error_code)

    def occurred_for(self, position_id: int) -> bool:
        return any(p.position_id == position_id and p.occurred is not None for p in self.positions)


def parse_close_order(payload: Any) -> CloseOrderStatus:
    parts = []
    for raw in pick(payload, "positions", default=[]) or []:
        pid = as_int(pick(raw, "positionID", "positionId")) if isinstance(raw, Mapping) else None
        if pid is None:
            continue
        parts.append(
            ClosedPart(
                position_id=pid,
                occurred=as_datetime(pick(raw, "occurred")),
                rate=as_float(pick(raw, "rate")),
                units=as_float(pick(raw, "units")),
            )
        )
    message = pick(payload, "errorMessage")
    return CloseOrderStatus(
        order_id=as_int(pick(payload, "orderID", "orderId")),
        status_id=as_int(pick(payload, "statusID", "statusId")),
        error_code=as_int(pick(payload, "errorCode")),
        error_message=str(message) if message else None,
        positions=parts,
    )


def close_order_id(payload: Any) -> int | None:
    """orderID from a market-close 200 (`orderForClose` envelope, casing-tolerant)."""
    inner = pick(payload, "orderForClose", default=payload)
    return as_int(pick(inner, "orderID", "orderId"))
