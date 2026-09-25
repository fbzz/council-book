"""Costs: per-side floors, commissions, overnight carry and the R15 net-of-cost gate.

Rules (numbers in policy/costs.yaml and risk.yaml net_of_cost_gate):
- Per side (bps of exposure) = max(broker what-if, class floor + commission, live half-spread)
  + slippage buffer. The what-if returned 0 on an older account, so floors are mandatory.
- Commission = fixed USD per trade / the REAL mirrored amount (real stocks/ETFs only; real crypto
  pays its percentage fee through the crypto floor).
- Carry (bps of exposure per calendar day; the weekend multiplier averages out to 1/365 per day):
  1x long real, and 1x long ETF/stock CFD, pay 0; a leveraged long pays
  (benchmark + long_cfd_markup)/365; a short pays short_cfd_markup/365; index/commodity/FX CFDs
  pay at least index_commodity_fx/365 and crypto CFDs crypto_cfd/365 on either side. The broker
  what-if wins when it is higher. Carry is never income (never negative).
  `asset_class` here is the VEHICLE's class: an ETF CFD on the NDX line is "etf", not "index".
- SR_be (break-even annualised Sharpe) = (round_trip + carry_per_day x hold) / (sigma_ann x hold/365).
  Units: round trip and carry in bps of exposure (converted to fractions), sigma_ann a fraction
  (0.20 = 20%/yr), hold in calendar days. It is the annualised Sharpe ratio a position turned over
  every `hold` days must earn just to pay its costs.
- The gate passes when SR_be <= reference_max_srbe for reference-aligned or toward-reference
  moves, and <= council_max_srbe for council deviations.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from council.models.broker import CostQuote
from council.policy import Policy
from council.risk.config import cost_floors, risk_limits

BPS = 10_000.0
CARRY_COST_TYPES: frozenset[str] = frozenset({"overnightfee", "overweekendfee"})
_ICF_CLASSES = frozenset({"index", "commodity", "fx"})


def floor_key(settlement: str, asset_class: str) -> str:
    """Which per_side_bps floor applies to a vehicle."""
    if asset_class == "crypto":
        return "crypto"
    if settlement == "real":
        return "etf_real"
    if asset_class in _ICF_CLASSES:
        return f"{asset_class}_cfd"
    return "etf_cfd"


def floor_bps(settlement: str, asset_class: str, policy: Policy) -> float:
    floors = cost_floors(policy).per_side_bps
    key = floor_key(settlement, asset_class)
    if key not in floors:
        raise ValueError(f"no per-side floor for {key}")
    return floors[key]


def commission_bps(
    settlement: str, real_amount_usd: float, policy: Policy, *, asset_class: str | None = None
) -> float:
    """Fixed commission as bps of the REAL mirrored amount. It applies to real stocks/ETFs; CFDs
    use the `cfd` entry (0) and real crypto pays its percentage fee through the crypto floor."""
    if asset_class == "crypto":
        return 0.0
    fixed = cost_floors(policy).fixed_commission_usd
    usd = fixed.get("real" if settlement == "real" else "cfd", 0.0)
    if usd <= 0:
        return 0.0
    if real_amount_usd <= 0:
        raise ValueError("commission needs a positive real amount")
    return usd / real_amount_usd * BPS


def per_side_bps(
    settlement: str,
    asset_class: str,
    what_if_bps: float | None,
    half_spread_bps: float | None,
    policy: Policy,
    *,
    commission: float = 0.0,
) -> float:
    """max(what-if, floor + commission, half-spread) + slippage buffer, in bps of exposure."""
    base = max(
        max(what_if_bps or 0.0, 0.0),
        floor_bps(settlement, asset_class, policy) + max(commission, 0.0),
        max(half_spread_bps or 0.0, 0.0),
    )
    return base + cost_floors(policy).slippage_buffer_bps


def carry_bps_day(
    direction: str,
    settlement: str,
    leverage: int,
    asset_class: str,
    what_if_overnight_bps_day: float | None,
    policy: Policy,
) -> float:
    """Overnight carry floor (bps of exposure per day) per the module rules; what-if wins if higher."""
    if direction not in ("long", "short"):
        raise ValueError(f"unknown direction {direction!r}")
    o = cost_floors(policy).overnight_annual
    rates: list[float] = []
    unlevered_long = direction == "long" and leverage <= 1
    if not (unlevered_long and (settlement == "real" or asset_class in ("etf", "stock"))):
        if asset_class == "crypto":
            rates.append(o.crypto_cfd)
        elif asset_class in _ICF_CLASSES:
            rates.append(o.index_commodity_fx)
        if direction == "long" and leverage > 1:
            rates.append(o.benchmark_rate + o.long_cfd_markup)
        if direction == "short":
            rates.append(o.short_cfd_markup)
    floor = max(rates, default=0.0) / 365.0 * BPS
    return max(floor, what_if_overnight_bps_day or 0.0, 0.0)


def hold_days(asset_class: str, policy: Policy, *, toward_reference: bool = False) -> float:
    """Expected hold used to amortise a round trip (R15). Toward-reference legs use the longer
    reference horizon when policy defines one."""
    cfg = risk_limits(policy).net_of_cost_gate
    gate = cfg.reference_hold_days if (toward_reference and cfg.reference_hold_days) else cfg.hold_days
    return gate.get(asset_class, gate["default"])


def srbe(round_trip_bps: float, carry_bps_day_: float, sigma_ann: float, hold_days_: float) -> float:
    """Break-even annualised Sharpe: (RT + carry x hold) / (sigma_ann x hold / 365)."""
    if sigma_ann <= 0 or hold_days_ <= 0:
        raise ValueError("sigma_ann and hold_days must be positive")
    cost = (round_trip_bps + max(carry_bps_day_, 0.0) * hold_days_) / BPS
    return cost / (sigma_ann * hold_days_ / 365.0)


def round_trip_bps(quote: CostQuote) -> float:
    """Entry now plus exit later, both at the quoted per-side cost."""
    return 2.0 * quote.per_side_bps


def srbe_for_quote(
    quote: CostQuote,
    sigma_ann: float,
    asset_class: str,
    policy: Policy,
    *,
    include_carry: bool = True,
    toward_reference: bool = False,
) -> float:
    carry = quote.carry_bps_day if include_carry else 0.0
    return srbe(round_trip_bps(quote), carry, sigma_ann,
                hold_days(asset_class, policy, toward_reference=toward_reference))


def gate_threshold(toward_reference: bool, policy: Policy) -> float:
    gate = risk_limits(policy).net_of_cost_gate
    return gate.reference_max_srbe if toward_reference else gate.council_max_srbe


def passes_cost_gate(
    quote: CostQuote,
    sigma_ann: float,
    asset_class: str,
    policy: Policy,
    *,
    toward_reference: bool,
    include_carry: bool = True,
) -> tuple[bool, float, float]:
    """(passes, SR_be, threshold) for one leg. Used by the engine and to build lever_ok/short_ok."""
    value = srbe_for_quote(quote, sigma_ann, asset_class, policy, include_carry=include_carry,
                           toward_reference=toward_reference)
    limit = gate_threshold(toward_reference, policy)
    return value <= limit + 1e-12, value, limit


def _ci(obj: Any) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in obj.items()} if isinstance(obj, Mapping) else {}


def _amount(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def whatif_components(payload: Mapping[str, Any]) -> tuple[float, float] | None:
    """(per-trade USD, overnight USD per night) from a /costs what-if; None if no cost list."""
    costs = _ci(payload).get("costs")
    if not isinstance(costs, list):
        return None
    trade = 0.0
    overnight = 0.0
    for item in costs:
        row = _ci(item)
        currency = str(row.get("currency") or "USD").upper()
        if currency != "USD":
            raise ValueError(f"cost component in {currency}; expected USD")
        kind = str(row.get("costtype") or "").lower()
        amount = _amount(row.get("amount"))
        if kind == "overnightfee":
            overnight += amount
        elif kind not in CARRY_COST_TYPES:
            trade += amount
    return trade, overnight


def cost_quote_from_whatif(
    payload: Mapping[str, Any],
    amount_usd: float,
    *,
    direction: str,
    settlement: str,
    leverage: int,
    asset_class: str,
    policy: Policy,
    quoted_at: datetime,
    symbol: str | None = None,
    half_spread_bps: float | None = None,
    real_amount_usd: float | None = None,
    exposure_usd: float | None = None,
) -> CostQuote:
    """Turn a /costs what-if into a floored CostQuote (private: the payload holds USD).

    `amount_usd` is the order amount sent to the what-if; exposure defaults to amount x leverage
    (amount read as margin; override with `exposure_usd` once M6 confirms the semantics).
    Per-trade components (markup, spread, fees, taxes) become what-if bps of exposure; the
    overnight fee becomes what-if carry bps/day. Floors, commission and slippage then apply."""
    if quoted_at.tzinfo is None:
        raise ValueError("naive datetime; council code uses aware UTC datetimes only")
    if amount_usd <= 0:
        raise ValueError("what-if amount must be positive")
    exposure = exposure_usd if exposure_usd is not None else amount_usd * max(leverage, 1)
    parts = whatif_components(payload)
    what_if_bps = None if parts is None else parts[0] / exposure * BPS
    overnight_bps = None if parts is None else parts[1] / exposure * BPS
    commission = commission_bps(
        settlement, real_amount_usd or amount_usd, policy, asset_class=asset_class
    )
    floor_total = floor_bps(settlement, asset_class, policy) + commission
    per_side = per_side_bps(
        settlement, asset_class, what_if_bps, half_spread_bps, policy, commission=commission
    )
    floored = floor_total >= max(what_if_bps or 0.0, half_spread_bps or 0.0)
    name = symbol or str(_ci(payload).get("symbol") or "")
    if not name:
        raise ValueError("cost quote needs a symbol")
    return CostQuote(
        symbol=name,
        direction=direction,  # type: ignore[arg-type]
        settlement=settlement,  # type: ignore[arg-type]
        leverage=leverage,
        per_side_bps=per_side,
        what_if_bps=what_if_bps,
        carry_bps_day=carry_bps_day(
            direction, settlement, leverage, asset_class, overnight_bps, policy
        ),
        weekend_multiplier=cost_floors(policy).weekend_multiplier,
        floor_applied=floored,
        quoted_at=quoted_at,
    )
