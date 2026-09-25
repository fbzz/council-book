"""Broker P&L payload -> signed exposure per exposure LINE (private: holds USD and IDs).

Rules (see tests/contract/specs/ETORO_ROUTES.md, GET /api/v1/trading/info/real/pnl):
- Keys are matched case-insensitively (`positionID`, `positionId`, `PositionId` all parse).
- Exposure of a position = `unrealizedPnL.exposureInAccountCurrency` when present, else
  units x closeRate x closeConversionRate (a positive close rate only), else (last resort)
  amount x leverage. Signed by isBuy. The unsigned exposure used is stored on `Position.exposure_usd` (with `Position.close_rate`),
  and every fallback is recorded in `ExposureSnapshot.flags` as
  `exposure_fallback:<units_x_close_rate|amount_x_leverage>:<line>` (the line is `UNMAPPED` for
  an unmapped instrument, so flags never carry an instrument ID).
- This is the one broker P&L parser the cycle uses for the risk engine (keyed by LINE).
  A position without a boolean isBuy is refused (fail closed; the direction is never guessed).
- Equity = credit + sum(amount) + unrealized P&L, where unrealized P&L is the portfolio-level
  `unrealizedPnL` when present, else the sum of the positions' `unrealizedPnL.pnL`.
- signed_w is keyed by LINE (e.g. NDX), not by broker symbol. An instrument we cannot map becomes
  its own line `UNMAPPED_<instrumentId>`: locked, never treated as cash, always counted in gross.
- gross = sum over POSITIONS of |exposure| / equity (both legs of a hedge count);
  net = sum of signed exposure / equity; margin_use = sum(amount) / equity.
- A line held in both directions at once is reported in `hedged`.
- Mirrors (copy positions) should never exist on an Agent Portfolio; if they do, each mirror is
  an `UNMAPPED_MIRROR_<mirrorId>` line and its money counts in equity.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from council.models.broker import ExposureSnapshot, Position
from council.models.common import Settlement

UNMAPPED_PREFIX = "UNMAPPED_"
# settlementTypeID: 0 CFD, 1 Real asset, 2 SWAP, 3 Crypto MarginTrade, 4 Future contract.
SETTLEMENT_BY_ID: dict[int, Settlement] = {
    0: "cfd",
    1: "real",
    2: "cfd",
    3: "marginTrade",
    4: "realFutures",
}
_EPS = 1e-12


def _ci(obj: Any) -> dict[str, Any]:
    """Case-insensitive view of a JSON object."""
    if not isinstance(obj, Mapping):
        return {}
    return {str(k).lower(): v for k, v in obj.items()}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(value: Any) -> int | None:
    num = _num(value)
    return None if num is None else int(num)


def _portfolio(payload: Mapping[str, Any]) -> dict[str, Any]:
    top = _ci(payload)
    inner = top.get("clientportfolio")
    return _ci(inner) if isinstance(inner, Mapping) else top


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def unmapped_line(instrument_id: int) -> str:
    return f"{UNMAPPED_PREFIX}{instrument_id}"


def is_unmapped(line: str) -> bool:
    return line.startswith(UNMAPPED_PREFIX)


def line_for_instrument(
    instrument_id: int,
    vehicle_by_instrument: Mapping[int, str],
    line_by_vehicle: Mapping[str, str],
) -> tuple[str, str]:
    """(line, vehicle symbol) for an instrument; unknown -> (UNMAPPED_<id>, UNMAPPED_<id>)."""
    vehicle = vehicle_by_instrument.get(instrument_id)
    if vehicle is None or vehicle not in line_by_vehicle:
        tag = unmapped_line(instrument_id)
        return tag, tag
    return line_by_vehicle[vehicle], vehicle


def position_pnl(raw: Mapping[str, Any]) -> float:
    """Unrealized P&L of one position (0 when absent)."""
    upnl = _ci(raw).get("unrealizedpnl")
    if isinstance(upnl, Mapping):
        return _num(_ci(upnl).get("pnl")) or 0.0
    return _num(upnl) or 0.0


EXPOSURE_BROKER = "broker"
EXPOSURE_UNITS_X_RATE = "units_x_close_rate"
EXPOSURE_AMOUNT_X_LEVERAGE = "amount_x_leverage"


def _close_rate(raw: Mapping[str, Any]) -> float | None:
    rate = _num(_ci(_ci(raw).get("unrealizedpnl")).get("closerate"))
    return rate if rate is not None and rate > 0 else None


def exposure_with_source(raw: Mapping[str, Any]) -> tuple[float, str] | None:
    """(unsigned exposure in account currency, how it was obtained), or None when the position
    has no exposure, close rate or amount. Sources: "broker" (exposureInAccountCurrency),
    "units_x_close_rate", "amount_x_leverage" (see module rules for the order)."""
    pos = _ci(raw)
    upnl = _ci(pos.get("unrealizedpnl"))
    exposure = _num(upnl.get("exposureinaccountcurrency"))
    if exposure is not None:
        return abs(exposure), EXPOSURE_BROKER
    units = _num(pos.get("units"))
    close_rate = _close_rate(raw)
    if units is not None and close_rate is not None:
        conversion = _num(upnl.get("closeconversionrate"))
        return abs(units * close_rate * (1.0 if conversion is None else conversion)), EXPOSURE_UNITS_X_RATE
    amount = _num(pos.get("amount"))
    if amount is None:
        return None
    leverage = _num(pos.get("leverage")) or 1.0
    return abs(amount * leverage), EXPOSURE_AMOUNT_X_LEVERAGE


def position_exposure(raw: Mapping[str, Any]) -> float:
    """Unsigned exposure in account currency (see module rules for the fallback order)."""
    found = exposure_with_source(raw)
    if found is None:
        raise ValueError("position has no exposure, rate or amount")
    return found[0]


def exposure_flag(source: str, line: str) -> str:
    """Quality flag for an exposure fallback (no instrument IDs: unmapped lines are `UNMAPPED`)."""
    return f"exposure_fallback:{source}:{'UNMAPPED' if is_unmapped(line) else line}"


def parse_position(raw: Mapping[str, Any], symbol: str) -> Position:
    """One broker position -> Position (private). Requires ids and a direction.

    `exposure_usd` is the unsigned exposure the snapshot uses (None when the payload has no
    exposure, close rate or amount); `close_rate` the broker's current close rate, if any."""
    pos = _ci(raw)
    position_id = _int(pos.get("positionid"))
    instrument_id = _int(pos.get("instrumentid"))
    is_buy = pos.get("isbuy")
    if position_id is None or instrument_id is None or not isinstance(is_buy, bool):
        raise ValueError("position needs positionID, instrumentID and a boolean isBuy")
    sl_rate = _num(pos.get("stoplossrate"))
    if pos.get("isnostoploss") is True or (sl_rate is not None and sl_rate <= 0):
        sl_rate = None
    tp_rate = _num(pos.get("takeprofitrate"))
    if pos.get("isnotakeprofit") is True or (tp_rate is not None and tp_rate <= 0):
        tp_rate = None
    settlement_id = _int(pos.get("settlementtypeid"))
    settlement: Settlement = SETTLEMENT_BY_ID.get(settlement_id, "cfd") if settlement_id is not None else "cfd"
    exposure = exposure_with_source(raw)
    return Position(
        position_id=position_id,
        instrument_id=instrument_id,
        symbol=symbol,
        is_buy=is_buy,
        leverage=_int(pos.get("leverage")) or 1,
        units=abs(_num(pos.get("units")) or 0.0),
        open_rate=_num(pos.get("openrate")) or 0.0,
        amount=_num(pos.get("amount")) or 0.0,
        sl_rate=sl_rate,
        tp_rate=tp_rate,
        settlement=settlement,
        opened_at=_parse_time(pos.get("opendatetime")),
        exposure_usd=None if exposure is None else exposure[0],
        close_rate=_close_rate(raw),
    )


def snapshot_from_pnl(
    payload: Mapping[str, Any],
    *,
    vehicle_by_instrument: Mapping[int, str],
    line_by_vehicle: Mapping[str, str],
    now: datetime,
) -> ExposureSnapshot:
    """Parse a /real/pnl payload into signed line weights (rules in the module docstring)."""
    if now.tzinfo is None:
        raise ValueError("naive datetime; council code uses aware UTC datetimes only")
    port = _portfolio(payload)
    credit = _num(port.get("credit"))
    if credit is None:
        raise ValueError("pnl payload has no credit")

    positions: list[Position] = []
    signed: dict[str, float] = {}
    directions: dict[str, set[bool]] = {}
    flags: list[str] = []
    gross_usd = 0.0
    invested = 0.0
    pnl_sum = 0.0

    def exposure_of(raw: Mapping[str, Any], line: str) -> float:
        found = exposure_with_source(raw)
        if found is None:
            raise ValueError("position has no exposure, rate or amount")
        if found[1] != EXPOSURE_BROKER:
            flags.append(exposure_flag(found[1], line))
        return found[0]

    for raw in port.get("positions") or []:
        instrument_id = _int(_ci(raw).get("instrumentid"))
        if instrument_id is None:
            raise ValueError("position without instrumentID")
        line, vehicle = line_for_instrument(instrument_id, vehicle_by_instrument, line_by_vehicle)
        pos = parse_position(raw, vehicle)
        exposure = exposure_of(raw, line)
        sign = 1.0 if pos.is_buy else -1.0
        positions.append(pos)
        signed[line] = signed.get(line, 0.0) + sign * exposure
        directions.setdefault(line, set()).add(pos.is_buy)
        gross_usd += exposure
        invested += pos.amount
        pnl_sum += position_pnl(raw)

    for mirror in port.get("mirrors") or []:
        m = _ci(mirror)
        line = f"{UNMAPPED_PREFIX}MIRROR_{_int(m.get('mirrorid')) or 0}"
        invested += _num(m.get("availableamount")) or 0.0
        for raw in m.get("positions") or []:
            exposure = exposure_of(raw, line)
            is_buy = _ci(raw).get("isbuy") is not False
            signed[line] = signed.get(line, 0.0) + (exposure if is_buy else -exposure)
            directions.setdefault(line, set()).add(is_buy)
            gross_usd += exposure
            invested += _num(_ci(raw).get("amount")) or 0.0
            pnl_sum += position_pnl(raw)

    portfolio_pnl = port.get("unrealizedpnl")
    unrealized = _num(portfolio_pnl) if not isinstance(portfolio_pnl, Mapping) else None
    equity = credit + invested + (pnl_sum if unrealized is None else unrealized)
    if equity <= _EPS:
        raise ValueError("non-positive equity; refusing to compute weights")

    signed_w = {line: value / equity for line, value in sorted(signed.items())}
    return ExposureSnapshot(
        taken_at=now,
        equity_usd=equity,
        credit_usd=credit,
        positions=positions,
        signed_w=signed_w,
        gross=gross_usd / equity,
        net=sum(signed.values()) / equity,
        margin_use=invested / equity,
        unmapped=sorted(line for line in signed if is_unmapped(line)),
        hedged=sorted(line for line, dirs in directions.items() if len(dirs) > 1),
        flags=sorted(set(flags)),
    )


def levels_from_weights(
    signed_w: Mapping[str, float], unit_weights: Mapping[str, float]
) -> dict[str, float]:
    """Level of each line = signed weight / unit weight (not snapped to the grid).

    UNMAPPED lines have no unit and are skipped. A line with a position but no positive unit
    weight cannot be expressed as a level and raises ValueError (the reference must size it)."""
    out: dict[str, float] = {}
    for line, weight in signed_w.items():
        if is_unmapped(line):
            continue
        unit = float(unit_weights.get(line, 0.0))
        if unit > _EPS:
            out[line] = weight / unit
        elif abs(weight) <= _EPS:
            out[line] = 0.0
        else:
            raise ValueError(f"{line}: position without a positive unit weight")
    for line in unit_weights:
        out.setdefault(line, 0.0)
    return out
