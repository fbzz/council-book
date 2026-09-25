"""Execution legs. Private: amount_usd/units/sl_rate/position_id never reach public documents."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from council.models.common import Direction, Settlement, Strict

LegKind = Literal["open", "close", "partial_close", "modify_sl"]
LegState = Literal[
    "planned", "submitting", "submitted", "in_flight", "filled", "partially_filled",
    "rejected", "rejected_partial", "unknown", "skipped",
]


class Leg(Strict):
    seq: int
    kind: LegKind
    symbol: str                                 # VEHICLE symbol (what the broker trades)
    line: str | None = None                     # exposure line (NDX, SPX, ...)
    instrument_id: int | None = None
    direction: Direction
    settlement: Settlement | None = None
    leverage: int = 1
    weight_before: float
    weight_after: float
    stop_distance: float | None = None          # fraction of price
    sl_margin_pct: float | None = None          # d * L * 100
    cost_bps_nav: float = 0.0                   # this leg's cost as bps of NAV
    carry_bps_day_nav: float = 0.0
    risk_increasing: bool
    reason: str = ""
    # private execution fields
    amount_usd: float | None = None
    units: float | None = None
    sl_rate: float | None = None
    position_id: int | None = None
    depends_on: list[int] = Field(default_factory=list)   # e.g. an open that waits for a close
    whole_units: bool = False


class Plan(Strict):
    legs: list[Leg]
    gross_before: float
    gross_after: float
    net_before: float
    net_after: float
    cost_bps_nav: float
    carry_bps_day_nav: float
    skipped: list[str] = Field(default_factory=list)       # "SYM: below_broker_minimum" etc.

    @property
    def risk_increasing(self) -> bool:
        return any(leg.risk_increasing for leg in self.legs)
