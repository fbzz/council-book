"""Turn target line weights into broker legs.

Rules (every one is tested):
- Weights are per exposure LINE; a line's current weight is Σ ±units × close rate / NAV over the
  positions on its vehicles (close rate: bid for longs, ask for shorts; open rate if unquoted).
  Positions on symbols that belong to no line are locked (never traded, never cash).
- Lines absent from `target_w` are held (no legs); an explicit 0.0 closes the line.
- Increase with the same sign → a new tranche on the vehicle `vehicle_for` picks; at most
  MAX_TRANCHES positions per line and direction, else the line is skipped (`max_tranches`).
- Decrease → partial close newest-first with UnitsToDeduct. A remainder below the broker's
  minPositionExposure becomes a full close; when partial closes are not allowed the position is
  fully closed and the remainder re-opened as a dependent open.
- Sign flip → close every position on the line, then open the new side (depends_on the closes).
- Opens are sized by UNITS = |Δw| × NAV / price (ask for longs, bid for shorts), floored (whole
  units when the instrument requires them); below minPositionExposure or the config's
  minPositionAmount (margin) → skipped `below_broker_minimum`.
- Every open carries a stop-loss rate: long ask × (1 − d), short bid × (1 + d); its margin
  percentage d × L × 100 must sit inside the eligibility SL bounds (± the policy buffer).
- Order: closes first (largest risk reduction first), then opens (largest first); at most
  risk.proposal.max_legs legs, dropping from the tail; a leg whose dependency was dropped is dropped.
- risk_increasing: opens (new, add, flip, re-open) = True; closes that do not flip = False.
- `Leg.symbol` is the VEHICLE symbol; the line is the prefix of `Leg.reason` and is recoverable
  with `vehicle_to_line`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from council.broker.eligibility import VehicleChoice, select_config, whole_units_only
from council.models.broker import EligibilityRow, ExposureSnapshot, Position
from council.models.common import Direction, Settlement
from council.models.plan import Leg, LegKind, Plan
from council.policy import Policy, Universe

MAX_TRANCHES = 3
W_EPS = 1e-6               # weight changes below this are noise
UNITS_SCALE = 1_000_000    # units are floored/ceiled to 6 decimals

VehicleFor = Callable[[str, Direction, int], VehicleChoice | None]
CostFn = Callable[[str, str, Direction, int], tuple[float, float]]


def vehicle_to_line(universe: Universe) -> dict[str, str]:
    """Every vehicle symbol (and each line symbol itself) → its line. Ambiguity is an error."""
    mapping: dict[str, str] = {}
    for line in universe.lines:
        symbols = {line.symbol, *(v.symbol for v in line.vehicles.long), *(v.symbol for v in line.vehicles.short)}
        for symbol in symbols:
            owner = mapping.setdefault(symbol, line.symbol)
            if owner != line.symbol:
                raise ValueError(f"symbol {symbol} belongs to lines {owner} and {line.symbol}")
    return mapping


def bid_ask(quote: Any) -> tuple[float, float] | None:
    """(bid, ask) from a Quote, a {"bid","ask"} mapping or a (bid, ask) pair; None if unusable."""
    if quote is None:
        return None
    if isinstance(quote, Mapping):
        bid, ask = quote.get("bid"), quote.get("ask")
    elif isinstance(quote, tuple | list) and len(quote) == 2:
        bid, ask = quote
    else:
        bid, ask = getattr(quote, "bid", None), getattr(quote, "ask", None)
    try:
        bid_f, ask_f = float(bid), float(ask)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(bid_f) and math.isfinite(ask_f)) or bid_f <= 0 or ask_f <= 0:
        return None
    return bid_f, ask_f


def stop_loss_rate(direction: Direction, bid: float, ask: float, distance: float) -> float:
    """Long: ask × (1 − d). Short: bid × (1 + d)."""
    return ask * (1 - distance) if direction == "long" else bid * (1 + distance)


def _floor_units(units: float, whole: bool) -> float:
    if whole:
        return float(math.floor(units + 1e-9))
    return math.floor(units * UNITS_SCALE + 1e-6) / UNITS_SCALE


def _ceil_units(units: float, whole: bool) -> float:
    if whole:
        return float(math.ceil(units - 1e-9))
    return math.ceil(units * UNITS_SCALE - 1e-6) / UNITS_SCALE


@dataclass
class _Draft:
    key: int
    kind: LegKind
    line: str
    symbol: str
    instrument_id: int | None
    direction: Direction
    settlement: Settlement | None
    leverage: int
    delta_w: float                  # signed change of the LINE weight
    units: float
    amount_usd: float               # notional = units × price
    risk_increasing: bool
    reason: str
    position_id: int | None = None
    sl_rate: float | None = None
    stop_distance: float | None = None
    sl_margin_pct: float | None = None
    cost_bps_nav: float = 0.0
    carry_bps_day_nav: float = 0.0
    depends_on: list[int] = field(default_factory=list)

    @property
    def is_close(self) -> bool:
        return self.kind in ("close", "partial_close")


class _Builder:
    def __init__(
        self,
        *,
        policy: Policy,
        nav: float,
        vehicle_for: VehicleFor,
        quotes: Mapping[str, Any],
        stop_distance: Mapping[str, float],
        leverage_for: Mapping[str, int],
        eligibility: Mapping[str, EligibilityRow],
        cost_bps: CostFn,
    ) -> None:
        self.nav = nav
        self.vehicle_for = vehicle_for
        self.quotes = quotes
        self.stop_distance = stop_distance
        self.leverage_for = leverage_for
        self.eligibility = eligibility
        self.cost_bps = cost_bps
        self.sl_buffer_pp = float(policy.risk["catastrophe_stop"]["margin_pct_buffer_pp"])
        self.drafts: list[_Draft] = []
        self.skipped: list[str] = []
        self._key = 0

    def _next_key(self) -> int:
        self._key += 1
        return self._key

    def skip(self, line: str, reason: str) -> None:
        note = f"{line}: {reason}"
        if note not in self.skipped:
            self.skipped.append(note)

    # ------------------------------------------------------------------ prices
    def close_price(self, p: Position) -> float:
        quote = bid_ask(self.quotes.get(p.symbol))
        if quote is None:
            return p.open_rate
        return quote[0] if p.is_buy else quote[1]

    def exposure(self, p: Position) -> float:
        return p.units * self.close_price(p)

    # ------------------------------------------------------------------ closes
    def close(self, line: str, p: Position, reason: str, units: float | None = None) -> _Draft:
        """Full close (units None) or partial close of `units`."""
        price = self.close_price(p)
        closed_units = p.units if units is None else units
        exposure = closed_units * price
        direction: Direction = "long" if p.is_buy else "short"
        per_side, _carry = self.cost_bps(line, p.symbol, direction, p.leverage)
        dw = exposure / self.nav
        draft = _Draft(
            key=self._next_key(), kind="close" if units is None else "partial_close", line=line,
            symbol=p.symbol, instrument_id=p.instrument_id, direction=direction,
            settlement=p.settlement, leverage=p.leverage,
            delta_w=-dw if p.is_buy else dw, units=closed_units, amount_usd=exposure,
            risk_increasing=False, reason=reason, position_id=p.position_id,
            cost_bps_nav=per_side * dw,
        )
        self.drafts.append(draft)
        return draft

    def reduce(self, line: str, positions: list[Position], reduce_w: float) -> None:
        """Partial close newest-first until the line weight dropped by `reduce_w`."""
        remaining = reduce_w * self.nav
        newest_first = sorted(
            positions,
            key=lambda p: (p.opened_at or datetime.min.replace(tzinfo=UTC), p.position_id),
            reverse=True,
        )
        for p in newest_first:
            if remaining <= 1e-9:
                break
            price = self.close_price(p)
            exposure = p.units * price
            if remaining >= exposure * (1 - 1e-9):
                self.close(line, p, f"{line}: reduce (full close, newest first)")
                remaining -= exposure
                continue
            row = self.eligibility.get(p.symbol)
            whole = whole_units_only(row) if row else False
            deduct = min(p.units, _ceil_units(remaining / price, whole))
            remainder_units = p.units - deduct
            remainder_exposure = remainder_units * price
            minimum = row.min_position_exposure if row else 0.0
            if remainder_units <= 1e-9 or remainder_exposure < minimum:
                self.close(line, p, f"{line}: reduce (full close, remainder below broker minimum)")
            elif row is not None and not row.allow_partial_close:
                closing = self.close(line, p, f"{line}: reduce (full close, partial close not allowed)")
                self.reopen_remainder(line, p, remainder_units, closing.key)
            else:
                self.close(line, p, f"{line}: reduce (partial close, newest first)", units=deduct)
            remaining = 0.0
            break

    def reopen_remainder(self, line: str, p: Position, units: float, close_key: int) -> None:
        direction: Direction = "long" if p.is_buy else "short"
        row = self.eligibility.get(p.symbol)
        config = select_config(row, direction, p.leverage, settlement=p.settlement) if row else None
        if row is None or config is None:
            self.skip(line, "reopen_ineligible")
            return
        choice = VehicleChoice(
            symbol=p.symbol, instrument_id=p.instrument_id, settlement=config.settlement,
            leverage=p.leverage, config=config,
        )
        self.open(
            line, direction, None, choice=choice, units=units, depends_on=[close_key],
            reason=f"{line}: re-open remainder (partial close not allowed)",
        )

    # ------------------------------------------------------------------ opens
    def open(
        self,
        line: str,
        direction: Direction,
        delta_abs_w: float | None,
        *,
        reason: str,
        depends_on: list[int] | None = None,
        choice: VehicleChoice | None = None,
        units: float | None = None,
    ) -> _Draft | None:
        leverage = int(self.leverage_for.get(line, 1))
        if choice is None:
            choice = self.vehicle_for(line, direction, leverage)
            if choice is None:
                self.skip(line, "no_eligible_vehicle")
                return None
        leverage = choice.leverage
        quote = bid_ask(self.quotes.get(choice.symbol))
        if quote is None:
            self.skip(line, "no_quote")
            return None
        row = self.eligibility.get(choice.symbol)
        if row is None:
            self.skip(line, "no_eligibility")
            return None
        distance = self.stop_distance.get(line)
        if distance is None or not math.isfinite(distance) or not 0 < distance < 1:
            self.skip(line, "no_stop_distance")
            return None
        bid, ask = quote
        price = ask if direction == "long" else bid
        whole = whole_units_only(row)
        raw_units = units if units is not None else (delta_abs_w or 0.0) * self.nav / price
        units = _floor_units(raw_units, whole)
        if row.max_units_per_order is not None and units > row.max_units_per_order:
            units = _floor_units(row.max_units_per_order, whole)
            self.skip(line, "capped_at_max_units_per_order")
        exposure = units * price
        if (
            units <= 0
            or exposure < row.min_position_exposure
            or exposure / leverage < choice.config.min_position_amount
        ):
            self.skip(line, "below_broker_minimum")
            return None
        sl_pct = distance * leverage * 100
        lo = choice.config.min_sl_pct + self.sl_buffer_pp if choice.config.min_sl_pct > 0 else 0.0
        hi = choice.config.max_sl_pct - self.sl_buffer_pp
        if not lo <= sl_pct <= hi:
            self.skip(line, "stop_outside_broker_bounds")
            return None
        per_side, carry = self.cost_bps(line, choice.symbol, direction, leverage)
        dw = exposure / self.nav
        draft = _Draft(
            key=self._next_key(), kind="open", line=line, symbol=choice.symbol,
            instrument_id=choice.instrument_id, direction=direction,
            settlement=choice.settlement, leverage=leverage,
            delta_w=dw if direction == "long" else -dw, units=units, amount_usd=exposure,
            risk_increasing=True, reason=reason,
            sl_rate=stop_loss_rate(direction, bid, ask, distance), stop_distance=distance,
            sl_margin_pct=sl_pct, cost_bps_nav=per_side * dw, carry_bps_day_nav=carry * dw,
            depends_on=list(depends_on or []),
        )
        self.drafts.append(draft)
        return draft

    # ------------------------------------------------------------------ one line
    def plan_line(self, line: str, target: float, current: float, positions: list[Position]) -> None:
        longs = [p for p in positions if p.is_buy]
        shorts = [p for p in positions if not p.is_buy]
        if longs and shorts:
            if abs(target) < W_EPS:
                for p in positions:
                    self.close(line, p, f"{line}: to zero (hedged state)")
            else:
                self.skip(line, "hedged_state")
            return
        if abs(target - current) < W_EPS:
            return
        cur_dir: Direction | None = "long" if current > 0 else "short" if current < 0 else None
        tgt_dir: Direction | None = "long" if target > 0 else "short" if target < 0 else None
        if tgt_dir is None:
            for p in positions:
                self.close(line, p, f"{line}: to zero")
        elif cur_dir is None or not positions:
            self.open(line, tgt_dir, abs(target), reason=f"{line}: new {tgt_dir} position")
        elif cur_dir != tgt_dir:
            keys = [self.close(line, p, f"{line}: flip {cur_dir}->{tgt_dir} (close)").key for p in positions]
            self.open(
                line, tgt_dir, abs(target), depends_on=keys,
                reason=f"{line}: flip {cur_dir}->{tgt_dir} (open)",
            )
        elif abs(target) > abs(current):
            tranches = len(positions)
            if tranches >= MAX_TRANCHES:
                self.skip(line, "max_tranches")
                return
            self.open(
                line, tgt_dir, abs(target) - abs(current),
                reason=f"{line}: add tranche {tranches + 1}/{MAX_TRANCHES}",
            )
        else:
            self.reduce(line, positions, abs(current) - abs(target))


def _order_and_cap(drafts: list[_Draft], line_rank: Mapping[str, int], cap: int, skip: Callable[[str, str], None]) -> list[_Draft]:
    ordered = sorted(
        drafts,
        key=lambda d: (0 if d.is_close else 1, -abs(d.delta_w), line_rank.get(d.line, 999), d.key),
    )
    kept = ordered[:cap]
    for dropped in ordered[cap:]:
        skip(dropped.line, "leg_cap")
    changed = True
    while changed:
        keys = {d.key for d in kept}
        orphaned = [d for d in kept if any(k not in keys for k in d.depends_on)]
        changed = bool(orphaned)
        for d in orphaned:
            skip(d.line, "dependency_dropped")
            kept.remove(d)
    return kept


def build_plan(
    *,
    snapshot: ExposureSnapshot,
    target_w: Mapping[str, float],
    vehicle_for: VehicleFor,
    quotes: Mapping[str, Any],
    stop_distance: Mapping[str, float],
    leverage_for: Mapping[str, int],
    eligibility: Mapping[str, EligibilityRow],
    cost_bps: CostFn,
    nav_usd: float,
    policy: Policy,
    max_legs: int | None = None,
) -> Plan:
    """Legs that move the book from `snapshot` to `target_w` (by line). See the module rules.

    `cost_bps(line, vehicle_symbol, direction, leverage)` → (per-side bps, carry bps/day), both of
    notional; a leg's cost in bps of NAV is per_side × |Δw|. `max_legs` overrides the policy cap
    (a flatten is uncapped)."""
    if not (math.isfinite(nav_usd) and nav_usd > 0):
        raise ValueError("nav_usd must be positive")
    universe = policy.universe
    v2l = vehicle_to_line(universe)
    line_rank = {line.symbol: i for i, line in enumerate(universe.lines)}
    cap = int(policy.risk["proposal"]["max_legs"]) if max_legs is None else max_legs
    b = _Builder(
        policy=policy, nav=nav_usd, vehicle_for=vehicle_for, quotes=quotes,
        stop_distance=stop_distance, leverage_for=leverage_for, eligibility=eligibility,
        cost_bps=cost_bps,
    )

    by_line: dict[str, list[Position]] = {}
    locked_gross = locked_net = 0.0
    for p in snapshot.positions:
        w = b.exposure(p) / nav_usd
        line = v2l.get(p.symbol)
        if line is None:
            locked_gross += w
            locked_net += w if p.is_buy else -w
            b.skip(p.symbol, "unmapped_position_locked")
            continue
        by_line.setdefault(line, []).append(p)
    current = {
        line: sum((b.exposure(p) if p.is_buy else -b.exposure(p)) / nav_usd for p in ps)
        for line, ps in by_line.items()
    }
    line_gross = {line: sum(b.exposure(p) / nav_usd for p in ps) for line, ps in by_line.items()}

    for line in sorted(target_w, key=lambda s: (line_rank.get(s, 999), s)):
        if line not in line_rank:
            b.skip(line, "unknown_line")
            continue
        b.plan_line(line, float(target_w[line]), current.get(line, 0.0), by_line.get(line, []))

    kept = _order_and_cap(b.drafts, line_rank, cap, b.skip)
    seq_of = {d.key: i + 1 for i, d in enumerate(kept)}
    running = dict(current)
    legs: list[Leg] = []
    for d in kept:
        before = running.get(d.line, 0.0)
        after = before + d.delta_w
        running[d.line] = after
        legs.append(
            Leg(
                seq=seq_of[d.key], kind=d.kind, symbol=d.symbol, instrument_id=d.instrument_id,
                direction=d.direction, settlement=d.settlement, leverage=d.leverage,
                weight_before=round(before, 6), weight_after=round(after, 6),
                stop_distance=d.stop_distance, sl_margin_pct=d.sl_margin_pct,
                cost_bps_nav=d.cost_bps_nav, carry_bps_day_nav=d.carry_bps_day_nav,
                risk_increasing=d.risk_increasing, reason=d.reason, amount_usd=d.amount_usd,
                units=d.units, sl_rate=d.sl_rate, position_id=d.position_id,
                depends_on=[seq_of[k] for k in d.depends_on],
            )
        )
    touched = {d.line for d in kept}
    gross_before = locked_gross + sum(line_gross.values())
    gross_after = locked_gross + sum(
        abs(running.get(line, 0.0)) if line in touched else g for line, g in line_gross.items()
    ) + sum(abs(running[line]) for line in touched if line not in line_gross)
    net_before = locked_net + sum(current.values())
    net_after = locked_net + sum(running.values())
    return Plan(
        legs=legs,
        gross_before=gross_before,
        gross_after=gross_after,
        net_before=net_before,
        net_after=net_after,
        cost_bps_nav=sum(leg.cost_bps_nav for leg in legs),
        carry_bps_day_nav=sum(leg.carry_bps_day_nav for leg in legs),
        skipped=b.skipped,
    )
