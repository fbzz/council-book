"""Leg/plan builders shared by the execution tests (no fixtures here)."""

from __future__ import annotations

from council.models.plan import Leg, Plan

API_KEY = "test-app-key"
READ_KEY = "test-read-key"
WRITE_KEY = "test-write-key"
NAV = 10_000.0

# symbol -> (instrument id, bid, ask); lines: SPX500 -> SPX, NSDQ100 -> NDX, GOLD -> GOLD, EURUSD -> EURUSD
INSTRUMENTS = {
    "SPX500": (101, 100.0, 100.1),
    "NSDQ100": (102, 200.0, 200.2),
    "GOLD": (103, 50.0, 50.05),
    "EURUSD": (104, 1.1, 1.1002),
}


def open_leg(
    seq: int,
    symbol: str,
    *,
    units: float = 10.0,
    direction: str = "long",
    leverage: int = 1,
    settlement: str = "cfd",
    sl_rate: float | None = None,
    distance: float = 0.1,
    depends_on: tuple[int, ...] = (),
    weight_before: float = 0.0,
    nav: float = NAV,
) -> Leg:
    iid, bid, ask = INSTRUMENTS[symbol]
    price = ask if direction == "long" else bid
    if sl_rate is None:
        sl_rate = price * (1 - distance) if direction == "long" else price * (1 + distance)
    exposure = units * price
    sign = 1 if direction == "long" else -1
    return Leg(
        seq=seq, kind="open", symbol=symbol, instrument_id=iid, direction=direction,
        settlement=settlement, leverage=leverage, weight_before=weight_before,
        weight_after=weight_before + sign * exposure / nav, stop_distance=distance,
        risk_increasing=True, reason=f"test open {symbol}", amount_usd=exposure, units=units,
        sl_rate=sl_rate, depends_on=list(depends_on),
    )


def close_leg(
    seq: int,
    position,
    *,
    units: float | None = None,
    weight_before: float = 0.0,
    weight_after: float = 0.0,
) -> Leg:
    symbol = next(s for s, v in INSTRUMENTS.items() if v[0] == position.instrument_id)
    return Leg(
        seq=seq, kind="partial_close" if units is not None else "close", symbol=symbol,
        instrument_id=position.instrument_id, direction="long" if position.is_buy else "short",
        settlement="cfd", leverage=position.leverage, weight_before=weight_before,
        weight_after=weight_after, risk_increasing=False, reason=f"test close {symbol}",
        amount_usd=(units or position.units) * position.open_rate,
        units=units if units is not None else position.units, position_id=position.position_id,
    )


def plan_of(*legs: Leg) -> Plan:
    return Plan(
        legs=list(legs), gross_before=0.0, gross_after=0.0, net_before=0.0, net_after=0.0,
        cost_bps_nav=0.0, carry_bps_day_nav=0.0,
    )
