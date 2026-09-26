"""Broker eligibility: parse rows, pick a leverage config, resolve a line's vehicle.

Rules:
- `settlementType` / `direction` casing varies (`CFD`/`cfd`, `LONG`/`long`) and is normalised; a
  config with an unknown settlement or direction is dropped (it cannot be traded safely).
- Fail closed on missing permissions: an absent `allowOpenPosition` or `allowPartialClosePosition`
  counts as False.
- `select_config` keeps configs whose direction matches, whose `leverageValues` contain the
  leverage, that are not `isPotential` (questionnaire needed = unavailable) and that allow setting
  a stop-loss (`allowStopLossTakeProfit` and `allowEditStopLoss`: every open carries our own
  stop, see `risk.stops.fit_to_eligibility`). A long at 1x prefers `real` when it is offered.
- `resolve_vehicle` walks a line's ordered candidates for the direction and returns the eligible
  candidate with the LOWEST expected cost; ties go to the earlier candidate.
- Order terms (design §4.3, single stocks): every parsed row is an `EligibilityTerms`, which also
  carries `requiresW8Ben`, `allowedOrderQuantityType` and `tradeUnitType`. They are read fail
  closed: `w8ben_required` is True for any value other than an explicit false or an absent / null
  field (the stock gate refuses the name and the user is asked); `unit_orders_allowed` and
  `trades_in_units` are False when the field is absent or names no units ("Amount", "Contracts").
  The row also keeps `allowClosePosition` and `unitsQuantityType` exactly as sent, because the
  permissive core defaults (an absent close permission is True, an absent quantity type is
  "fractional") would let a single stock through: `close_allowed` is True only for an explicit JSON
  true, and `fractional_units` only when the broker names fractional units (else the stock gate
  applies the whole-unit price test).
  `stock_configs` re-reads the leverage configs with fail-closed defaults for the stop-loss fields
  (absent `allowStopLossTakeProfit` / `allowEditStopLoss` = False, absent `isPotential` = True, absent
  SL bounds = an empty range), for the strict single-stock gate; `leverage_configs` keep the
  permissive defaults the core lines have always used.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from pydantic import Field

from council.broker.parsing import as_float, as_int, pick
from council.models.broker import EligibilityRow, LeverageConfig
from council.models.common import Direction, Frozen, Settlement
from council.policy import LineSpec, Vehicle

_SETTLEMENTS: dict[str, Settlement] = {
    "cfd": "cfd", "real": "real", "realfutures": "realFutures", "margintrade": "marginTrade",
}
_DIRECTIONS: dict[str, Direction] = {"long": "long", "short": "short"}


def normalise_settlement(value: Any) -> Settlement | None:
    return _SETTLEMENTS.get(str(value or "").strip().replace("_", "").lower())


def normalise_direction(value: Any) -> Direction | None:
    return _DIRECTIONS.get(str(value or "").strip().lower())


def _bool(value: Any, default: bool) -> bool:
    return default if value is None else bool(value)


def parse_leverage_config(raw: Mapping[str, Any]) -> LeverageConfig | None:
    settlement = normalise_settlement(pick(raw, "settlementType"))
    direction = normalise_direction(pick(raw, "direction"))
    if settlement is None or direction is None:
        return None
    values = [as_int(v) for v in (pick(raw, "leverageValues", default=[]) or [])]
    return LeverageConfig(
        settlement=settlement,
        direction=direction,
        leverage_values=sorted({v for v in values if v is not None and v >= 1}),
        is_potential=_bool(pick(raw, "isPotential"), False),
        min_position_amount=as_float(pick(raw, "minPositionAmount"), 0.0) or 0.0,
        allow_edit_stop_loss=_bool(pick(raw, "allowEditStopLoss"), True),
        min_sl_pct=as_float(pick(raw, "minStopLossPercentage"), 0.0) or 0.0,
        max_sl_pct=as_float(pick(raw, "maxStopLossPercentage"), 100.0) or 100.0,
        default_sl_pct=as_float(pick(raw, "defaultStopLossPercentage")),
        allow_sl_tp=_bool(pick(raw, "allowStopLossTakeProfit"), True),
    )


class EligibilityTerms(EligibilityRow):
    """An eligibility row with the order-terms fields the single-stock gate reads (design §4.3).

    Raw values are kept as the broker sent them (None when absent); read them through
    `w8ben_required`, `unit_orders_allowed` and `trades_in_units`, which fail closed."""

    requires_w8ben: bool | None = None           # None: absent or null
    allowed_order_quantity_type: str | None = None   # e.g. "Both", "Units", "Amount"
    trade_unit_type: str | None = None           # e.g. "Units", "Contracts"
    stock_configs: list[LeverageConfig] = Field(default_factory=list)   # fail-closed re-read (module doc)
    allow_close_reported: bool | None = None     # allowClosePosition as sent (None: absent / not a boolean)
    units_quantity_reported: str | None = None   # unitsQuantityType as sent (None: absent)


def parse_stock_config(raw: Mapping[str, Any]) -> LeverageConfig | None:
    """A leverage config read for the strict stock gate: every stop-loss field must be present to
    count (absent permissions are False, an absent `isPotential` is True, absent bounds admit no
    stop distance)."""
    base = parse_leverage_config(raw)
    if base is None:
        return None
    missing = object()

    def present(name: str) -> Any:
        value = pick(raw, name, default=missing)
        return None if value is missing else value

    potential, allow_sl, allow_edit = (present(k) for k in ("isPotential", "allowStopLossTakeProfit",
                                                              "allowEditStopLoss"))
    lo, hi = as_float(present("minStopLossPercentage")), as_float(present("maxStopLossPercentage"))
    return base.model_copy(update={
        "is_potential": True if potential is None else bool(potential),
        "allow_sl_tp": allow_sl is True,
        "allow_edit_stop_loss": allow_edit is True,
        "min_sl_pct": 100.0 if lo is None else lo,
        "max_sl_pct": 0.0 if hi is None else hi,
    })


def _w8ben(value: Any) -> bool | None:
    """`requiresW8Ben`: None when absent or null, False only for an explicit false, True otherwise
    (an unknown value counts as required: fail closed)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "0", "no")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _strict_bool(value: Any) -> bool | None:
    """A JSON boolean as sent; anything else (absent, null, a string) is None."""
    return value if isinstance(value, bool) else None


def close_allowed(row: EligibilityRow) -> bool:
    """The broker explicitly allows closing the position (`allowClosePosition` is JSON true). An
    absent or non-boolean field is False (fail closed), unlike the core parser's default."""
    return getattr(row, "allow_close_reported", None) is True


def fractional_units(row: EligibilityRow) -> bool:
    """The broker says the instrument trades in fractional units (`unitsQuantityType` names
    fractions, e.g. "FractionalUnits"). Absent or anything else is False: whole units (fail closed)."""
    text = (getattr(row, "units_quantity_reported", None) or "").strip().lower()
    return "fraction" in text


def w8ben_required(row: EligibilityRow) -> bool:
    """True when the broker says the instrument needs a W-8BEN form (any value but false/absent)."""
    return bool(getattr(row, "requires_w8ben", None))


def unit_orders_allowed(row: EligibilityRow) -> bool:
    """Orders by units are allowed: `allowedOrderQuantityType` is "Both" or names units. Absent or
    anything else ("Amount") is False (fail closed)."""
    text = (getattr(row, "allowed_order_quantity_type", None) or "").strip().lower()
    return text == "both" or "unit" in text


def trades_in_units(row: EligibilityRow) -> bool:
    """The instrument trades in units (shares), not contracts: `tradeUnitType` names units. Absent
    or anything else is False (fail closed)."""
    text = (getattr(row, "trade_unit_type", None) or "").strip().lower()
    return "unit" in text


def parse_eligibility_row(raw: Mapping[str, Any], fetched_at: datetime) -> EligibilityRow | None:
    instrument_id = as_int(pick(raw, "instrumentId", "instrumentID"))
    symbol = pick(raw, "symbol")
    if instrument_id is None or not symbol:
        return None
    configs = [
        cfg
        for cfg in (
            parse_leverage_config(c) for c in (pick(raw, "leverageConfigs", default=[]) or [])
            if isinstance(c, Mapping)
        )
        if cfg is not None
    ]
    return EligibilityTerms(
        instrument_id=instrument_id,
        symbol=str(symbol),
        min_position_exposure=as_float(pick(raw, "minPositionExposure"), 0.0) or 0.0,
        max_units_per_order=as_float(pick(raw, "maxUnitsPerOrder")),
        allow_open=_bool(pick(raw, "allowOpenPosition"), False),
        allow_close=_bool(pick(raw, "allowClosePosition"), True),
        allow_partial_close=_bool(pick(raw, "allowPartialClosePosition"), False),
        allow_trailing_sl=_bool(pick(raw, "allowTrailingStopLoss"), False),
        allow_entry_orders=_bool(pick(raw, "allowEntryOrders"), False),
        units_quantity_type=str(pick(raw, "unitsQuantityType", default="fractional") or "fractional"),
        leverage_configs=configs,
        fetched_at=fetched_at,
        requires_w8ben=_w8ben(pick(raw, "requiresW8Ben")),
        allowed_order_quantity_type=_text(pick(raw, "allowedOrderQuantityType")),
        trade_unit_type=_text(pick(raw, "tradeUnitType")),
        allow_close_reported=_strict_bool(pick(raw, "allowClosePosition")),
        units_quantity_reported=_text(pick(raw, "unitsQuantityType")),
        stock_configs=[
            cfg for cfg in (parse_stock_config(c) for c in (pick(raw, "leverageConfigs", default=[]) or [])
                            if isinstance(c, Mapping))
            if cfg is not None
        ],
    )


def parse_eligibility(payload: Any, fetched_at: datetime) -> list[EligibilityRow]:
    """Parse `POST /api/v2/trading/info/eligibility`. Rows without id or symbol are dropped."""
    raw_rows = pick(payload, "eligibilities", default=None) if isinstance(payload, Mapping) else payload
    rows = []
    for raw in raw_rows or []:
        if isinstance(raw, Mapping):
            row = parse_eligibility_row(raw, fetched_at)
            if row is not None:
                rows.append(row)
    return rows


def whole_units_only(row: EligibilityRow) -> bool:
    return "whole" in row.units_quantity_type.lower()


def select_config(
    row: EligibilityRow,
    direction: Direction,
    leverage: int,
    settlement: Settlement | None = None,
) -> LeverageConfig | None:
    """The usable leverage config for (direction, leverage[, settlement]) or None."""
    if not row.allow_open:
        return None
    usable = [
        c
        for c in row.leverage_configs
        if c.direction == direction
        and leverage in c.leverage_values
        and not c.is_potential
        and c.allow_sl_tp
        and c.allow_edit_stop_loss
        and (settlement is None or c.settlement == settlement)
    ]
    if not usable:
        return None
    if direction == "long" and leverage == 1:
        real = [c for c in usable if c.settlement == "real"]
        if real:
            return real[0]
    cfd = [c for c in usable if c.settlement == "cfd"]
    return cfd[0] if cfd else usable[0]


class VehicleChoice(Frozen):
    """The broker instrument that carries a line's exposure for one direction and leverage."""

    symbol: str
    instrument_id: int
    settlement: Settlement
    leverage: int
    config: LeverageConfig
    expected_cost_bps: float | None = None

    @property
    def direction(self) -> Direction:
        return self.config.direction


ExpectedCost = Callable[[Vehicle, EligibilityRow, LeverageConfig], float | None]


def resolve_vehicle(
    line: LineSpec,
    direction: Direction,
    leverage: int,
    rows_by_symbol: Mapping[str, EligibilityRow],
    expected_cost: ExpectedCost,
) -> VehicleChoice | None:
    """First eligible candidate with the lowest expected cost (bps over the hold).

    `expected_cost(vehicle, row, config)` returns the expected cost in bps; None or a non-finite
    value excludes the candidate."""
    candidates = line.vehicles.long if direction == "long" else line.vehicles.short
    upper = {k.upper(): v for k, v in rows_by_symbol.items()}
    best: tuple[float, int, VehicleChoice] | None = None
    for index, vehicle in enumerate(candidates):
        row = rows_by_symbol.get(vehicle.symbol) or upper.get(vehicle.symbol.upper())
        if row is None:
            continue
        config = select_config(row, direction, leverage, settlement=vehicle.settlement)
        if config is None:
            continue
        cost = expected_cost(vehicle, row, config)
        if cost is None or not math.isfinite(cost):
            continue
        choice = VehicleChoice(
            symbol=vehicle.symbol,
            instrument_id=row.instrument_id,
            settlement=config.settlement,
            leverage=leverage,
            config=config,
            expected_cost_bps=float(cost),
        )
        if best is None or (cost, index) < (best[0], best[1]):
            best = (float(cost), index, choice)
    return best[2] if best else None
