"""Broker-side facts. These hold USD amounts and IDs: PRIVATE. Public documents never embed them."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from council.models.common import Settlement, Strict


class LeverageConfig(Strict):
    settlement: Settlement
    direction: Literal["long", "short"]
    leverage_values: list[int]
    is_potential: bool = False
    min_position_amount: float = 0.0
    allow_edit_stop_loss: bool = True
    min_sl_pct: float = 0.0          # percent of margin
    max_sl_pct: float = 100.0
    default_sl_pct: float | None = None
    allow_sl_tp: bool = True


class EligibilityRow(Strict):
    instrument_id: int
    symbol: str
    min_position_exposure: float = 0.0
    max_units_per_order: float | None = None
    allow_open: bool = True
    allow_close: bool = True
    allow_partial_close: bool = True
    allow_trailing_sl: bool = False
    allow_entry_orders: bool = False
    units_quantity_type: str = "fractional"
    leverage_configs: list[LeverageConfig] = Field(default_factory=list)
    fetched_at: datetime


class Position(Strict):
    position_id: int
    instrument_id: int
    symbol: str
    is_buy: bool
    leverage: int = 1
    units: float
    open_rate: float
    amount: float                    # invested margin, USD
    sl_rate: float | None = None
    tp_rate: float | None = None
    settlement: Settlement = "cfd"
    opened_at: datetime | None = None


class Quote(Strict):
    symbol: str
    instrument_id: int
    bid: float
    ask: float
    at: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


class ExposureSnapshot(Strict):
    taken_at: datetime
    equity_usd: float
    credit_usd: float
    positions: list[Position]
    signed_w: dict[str, float]        # signed notional / equity per symbol
    gross: float
    net: float
    margin_use: float
    unmapped: list[str] = Field(default_factory=list)
    hedged: list[str] = Field(default_factory=list)


class CostQuote(Strict):
    symbol: str
    direction: Literal["long", "short"]
    settlement: Settlement
    leverage: int
    per_side_bps: float              # after floors
    what_if_bps: float | None        # raw broker what-if (None if unavailable)
    carry_bps_day: float             # overnight on exposure; 0 for real 1x
    weekend_multiplier: float = 3.0
    floor_applied: bool = False
    quoted_at: datetime
