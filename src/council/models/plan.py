"""Execution legs. Private: amount_usd/units/sl_rate/position_id never reach public documents.

Market hours: each leg carries the trading `session` of its vehicle ("us", "lse", "fx24x5",
"crypto"; None when unknown, e.g. an unmapped position) and its own approval deadline
`valid_until` = min(next slot - 5 min, the session's next close - 10 min), both stamped by the
cycle (`cycle.stamp_sessions`). Legs written before these fields existed have None in both; the
approval and the executor then derive the session from the policy.

Costs: `cost_bps_nav` is the leg's VARIABLE cost (per side x |dw|), the number the public record may
show; `fee_bps_nav` is the $1 fixed fee of a real non-crypto trade as bps of NAV and `fee_drag` the
extra fraction of the real (mirror) account that fee costs beyond what the virtual book records.
Both are PRIVATE (they encode the NAV). `Plan.cost_bps_nav` is variable-only for the same reason;
`Plan.fee_bps_nav` holds the fees.

Origin: "reference" when the leg moves its line toward the mechanical rule's target (there are no
council anchors before WP-K), "discretionary" otherwise; `ref_level` is the line's reference level
at plan time, which becomes the held reference level when a reference leg fills
(`risk.held_levels`). Legs written before these fields existed count as discretionary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from council.models.common import Direction, Settlement, Strict

LegKind = Literal["open", "close", "partial_close", "modify_sl"]
LegOrigin = Literal["reference", "discretionary"]
LegState = Literal[
    "planned", "submitting", "submitted", "in_flight", "filled", "partially_filled",
    "rejected", "rejected_partial", "unknown", "skipped", "waiting_for_market",
]
# waiting_for_market: the broker accepted the order but holds it until its market opens (order
# status 11). It is neither in flight (the executor stops polling) nor unknown (it is not a halt):
# the watch resolves it read-only, or the operator records the outcome (`council ops resolve`).


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
    cost_bps_nav: float = 0.0                   # this leg's VARIABLE cost as bps of NAV
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
    session: str | None = None                  # the vehicle's trading session (see module doc)
    valid_until: datetime | None = None         # this leg's approval deadline (UTC)
    origin: LegOrigin | None = None             # None = written before origins (discretionary)
    ref_level: float | None = None              # the line's reference level at plan time
    fee_bps_nav: float = 0.0                    # PRIVATE: the fixed fee as bps of NAV
    fee_drag: float = 0.0                       # PRIVATE: extra real-account fraction of the fee


class Plan(Strict):
    legs: list[Leg]
    gross_before: float
    gross_after: float
    net_before: float
    net_after: float
    cost_bps_nav: float                         # variable costs only (see the module doc)
    carry_bps_day_nav: float
    skipped: list[str] = Field(default_factory=list)       # "SYM: below_broker_minimum" etc.
    fee_bps_nav: float = 0.0                    # PRIVATE: the legs' fixed fees as bps of NAV

    @property
    def fixed_fee_legs(self) -> int:
        """How many legs pay the fixed fee (a count, which reveals no NAV)."""
        return sum(1 for leg in self.legs if leg.fee_bps_nav > 0)

    @property
    def risk_increasing(self) -> bool:
        return any(leg.risk_increasing for leg in self.legs)

    @property
    def sessions(self) -> list[str]:
        """The market sessions this plan needs open (crypto never closes; unknown ones excluded)."""
        return sorted({leg.session for leg in self.legs if leg.session and leg.session != "crypto"})

    def latest_valid_until(self) -> datetime | None:
        """The latest leg deadline (a rebalance stays approvable while any leg is), or None when
        no leg carries one."""
        stamps = [leg.valid_until for leg in self.legs if leg.valid_until is not None]
        return max(stamps) if stamps else None
