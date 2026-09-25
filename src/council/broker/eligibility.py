"""Broker eligibility: parse rows, pick a leverage config, resolve a line's vehicle.

Rules:
- `settlementType` / `direction` casing varies (`CFD`/`cfd`, `LONG`/`long`) and is normalised; a
  config with an unknown settlement or direction is dropped (it cannot be traded safely).
- Fail closed on missing permissions: an absent `allowOpenPosition` or `allowPartialClosePosition`
  counts as False.
- `select_config` keeps configs whose direction matches, whose `leverageValues` contain the
  leverage, that are not `isPotential` (questionnaire needed = unavailable) and that allow a
  stop-loss (every open carries one). A long at 1x prefers `real` when it is offered.
- `resolve_vehicle` walks a line's ordered candidates for the direction and returns the eligible
  candidate with the LOWEST expected cost; ties go to the earlier candidate.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

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
    return EligibilityRow(
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
