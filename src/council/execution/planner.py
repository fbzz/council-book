"""Turn target line weights into broker legs.

Rules (every one is tested):
- Weights are per exposure LINE. A position's exposure is the broker's `exposure_usd` from the
  snapshot when present (the same number the risk engine starts from), else units × close rate
  (bid for longs, ask for shorts; the broker close rate, then the open rate, if unquoted). A line's
  current weight is Σ ±exposure / NAV over the positions on its vehicles, so a line the engine
  held (final = base) produces NO legs. Positions on symbols that belong to no line are locked
  (never traded, never cash) except by `build_flatten_plan`.
- ONLY lines present in `target_w` are planned: a line absent from `target_w` never gets a leg,
  whatever its current weight; an explicit 0.0 closes the line. Callers pass only the lines the
  risk engine changed (`changed_targets(decision)`, i.e. `models.risk.changed_lines`).
- Increase with the same sign → a new tranche on the vehicle `vehicle_for` picks; at most
  MAX_TRANCHES positions per line and direction, else the line is skipped (`max_tranches`).
- Decrease → partial close newest-first with UnitsToDeduct. A remainder below the broker's
  minPositionExposure becomes a full close; when partial closes are not allowed the position is
  fully closed and the remainder re-opened as a dependent open.
- Sign flip → close every position on the line, then open the new side (depends_on the closes).
- Opens are sized by UNITS = |Δw| × NAV / price (ask for longs, bid for shorts), floored (whole
  units when the instrument requires them); below minPositionExposure or the config's
  minPositionAmount (margin) → skipped `below_broker_minimum`.
- Every open carries a stop-loss from `risk.stops`: the distance d is fitted with
  `fit_to_eligibility` (SL% = d × L × 100 inside [min + buffer, max − buffer]; below the minimum
  it is WIDENED to the minimum, above the maximum → skipped `stop_outside_broker_bounds`; a config
  that does not allow editing the stop or SL/TP → skipped `stop_not_allowed`), then
  `stop_loss_rate`: long ask × (1 − d), short bid × (1 + d).
- Order: closes first (largest risk reduction first), then stop/target modifications of existing
  positions (`modify_sl`, `set_tp`), then core opens (largest first), then swing opens, then their
  `modify_tp` legs (swing-book §4.4, §4.6); at most
  risk.proposal.max_legs COUNTED legs (risk-increasing or discretionary; a risk-reducing leg of a
  reference-origin line does not count) and at most proposal.max_legs_total legs in all, dropping
  from the tail; a leg whose dependency was dropped is dropped.
- risk_increasing: opens (new, add, flip, re-open) = True; closes that do not flip = False.
- Every leg carries `symbol` (the VEHICLE), `line` (the exposure line) and `whole_units`, and, when
  the caller passes them, its line's `origin` ("reference" | "discretionary"; missing = discretionary)
  and `ref_level` (what a filled reference leg records as the held level).
- Costs: `cost_bps(line, vehicle, direction, leverage)` returns (per side, carry) or (per side,
  carry, fixed fee bps of NAV). `Leg.cost_bps_nav` = per side x |dw| (variable, publishable);
  `Leg.fee_bps_nav` = the fee, charged on every leg that pays it (each tranche close pays its own);
  `Leg.fee_drag` = `economics.real_drag_per_leg` on those legs. Fees and drags are PRIVATE.
- Real-dollar floor (`economics`): an open, or a partial close, whose order amount (exposure /
  leverage) copied at the mirror ratio is below the real trade floor is skipped
  `below_real_minimum`; full closes are never skipped.
- Gap guard (design D20): an open on a line in `gap_ref` (the cycle passes stock lines with the
  pack's last close and daily sigma) is skipped `gap_guard` when |ln(planned price / last close)|
  exceeds risk.anti_chase_sigma daily sigmas, and `gap_guard_no_reference` when either is missing.
- `build_flatten_plan` closes EVERY position (unmapped ones included) with full closes only and
  no leg cap.
- `build_smoke_plan` (m5-readiness §8, M5-D2) turns an operator smoke ticket's intents into legs
  with the SAME construction: an open by exact units through `_Builder.open` (broker minimums,
  real floor, stop fitted to the broker bounds, stop-loss rate), a close / partial close through
  `_Builder.close`, and a stop-loss move through `_Builder.modify_sl`. Capability gates do not
  apply (a smoke ticket is what proves them); anything the builder would skip raises
  SmokePlanError with the skip code. A smoke leg's line is the vehicle's line, else
  `UNMAPPED_<instrument id>` (as the exposure snapshot names it).
- Swing book (swing-book.md rev 2 §4.2/§4.4, SW-5): swing lines (`SW_<ticker>`) are planned ONLY from
  `swing` orders (`SwingOrder`), never from `target_w`; `swing_map` (`swing.book.SwingVehicleMap`)
  maps live swing positions to their lines so they are not locked as unmapped. An `enter` order is
  one market open at 1x (a long is real shares, a short a CFD), sized by units from its NAV share,
  with the approved stop distance (never widened to a broker minimum: a stop the broker bounds would
  move is skipped `stop_outside_broker_bounds`) and target distance turned into rates at the
  plan-time quote. Its vehicle class must be proven: `capabilities.allows_vehicle` of
  `stock_real_long` / `stock_cfd_short`, else skipped `capability_not_proven:<cap>` (no gate when
  `capabilities` is None, as for core opens). The target goes into the open body when the
  `tp_on_open` gate is proven (`tp_mode="body"`), else a dependent `modify_tp` leg follows the open
  (`"patch"`); a target closer than the config's minTakeProfitPercentage goes to the broker not at
  all (`"none"`, noted `<line>: tp_below_broker_minimum`). An `exit` order fully closes the line's
  positions; a `set_tp` order (an `open_tp_missing` trade, `set_tp_orders`) PATCHes the approved
  target onto each position with its current stop. Swing legs are priced from the policy's stock
  cost floors (`per_side_bps` / `carry_bps_day` of a single stock at 1x, the quote's half-spread when
  wider), never from the core `cost_bps` table; an entry whose cost cannot be priced is skipped
  `cost_unavailable` (fail closed, never a zero cost). An entry above the swing size, stop or
  loss-at-stop ceiling (`invariants.SWING_MAX_*`, tightened by `policy.swing`) is skipped
  `swing_size_above_cap` / `swing_stop_above_cap` / `swing_loss_at_stop_above_cap`; a config that
  does not allow editing the take-profit gets no `modify_tp` / `set_tp` (`tp_not_editable`). Swing legs never count toward the core leg caps
  (core legs are never dropped for a swing leg); at most MAX_SWING_OPENS swing opens and as many
  `modify_tp` legs per plan. Every swing leg carries `sleeve="swing"` and its `swing_trade_id`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from council.broker.eligibility import (
    VehicleChoice,
    required_capabilities,
    select_config,
    whole_units_only,
)
from council.models.broker import EligibilityRow, ExposureSnapshot, Position
from council.models.common import Direction, Settlement
from council.models.plan import Leg, LegKind, Plan
from council.models.risk import RiskDecision, changed_lines
from council.operator.capabilities import OPEN_PREREQUISITES
from council.policy import Policy, Universe
from council.risk.costs import TradeEconomics
from council.risk.stops import fit_to_eligibility, sl_margin_pct, stop_loss_rate

MAX_TRANCHES = 3
W_EPS = 1e-6               # weight changes below this are noise
UNITS_SCALE = 1_000_000    # units are floored/ceiled to 6 decimals

UNMAPPED_PREFIX = "UNMAPPED_"
MAX_SWING_OPENS = 6        # swing-book §4.5: <= 6 swing opens + <= 6 modify_tp per decision
TP_ON_OPEN = "tp_on_open"  # capability: the open route accepts takeProfitRate and the position keeps it
# the vehicle-class capability a swing entry needs (swing.resolve.SIDE_CAPABILITIES, same values)
SWING_VEHICLE_CAPS: Mapping[str, tuple[str, ...]] = {"long": ("stock_real_long",), "short": ("stock_cfd_short",)}

VehicleFor = Callable[[str, Direction, int], VehicleChoice | None]
CostFn = Callable[[str, str, Direction, int], tuple[float, ...]]   # (per side, carry[, fee bps NAV])
GapRef = Mapping[str, tuple[float, float] | None]                    # line -> (last close, daily sigma)
REFERENCE_ORIGIN = "reference"
SymbolFor = Mapping[int, str] | Callable[[int], str | None] | None


def vehicle_to_line(universe: Universe) -> dict[str, str]:
    """Every vehicle symbol (and each line symbol itself) → its line.

    Ambiguity is rejected when the policy LOADS (the `Universe` validator enforces one namespace of
    line ids and vehicle symbols), so a cycle never discovers it here. A universe built with
    `model_copy` skips that validator; if it collides, this still raises (the same rule,
    `policy.symbol_owners`) rather than mapping a symbol to the wrong line."""
    return universe.vehicle_map()


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


def changed_targets(decision: RiskDecision, eps: float = W_EPS) -> dict[str, float]:
    """The planner's `target_w` for a risk decision: final weights of the CHANGED lines only
    (`models.risk.changed_lines`); a changed line missing from final_w targets 0."""
    return {s: float(decision.final_w.get(s, 0.0)) for s in changed_lines(decision, eps)}


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
    whole_units: bool = False
    fee_bps_nav: float = 0.0
    fee_drag: float = 0.0
    origin: str | None = None
    ref_level: float | None = None
    sleeve: str | None = None
    swing_trade_id: str | None = None
    tp_rate: float | None = None
    tp_mode: str | None = None
    time_stop_date: str | None = None

    @property
    def is_close(self) -> bool:
        return self.kind in ("close", "partial_close")

    @property
    def counted(self) -> bool:
        """Counts toward risk.proposal.max_legs: every leg except a risk-reducing leg of a
        reference-origin line."""
        return self.risk_increasing or self.origin != REFERENCE_ORIGIN

    @property
    def phase(self) -> int:
        """Plan order: closes, modifications of existing positions, core opens, swing opens, the
        swing opens' modify_tp legs."""
        if self.is_close:
            return 0
        if self.kind in ("modify_sl", "set_tp"):
            return 1
        if self.kind == "open":
            return 3 if self.sleeve == "swing" else 2
        return 4


@dataclass(frozen=True)
class SwingOrder:
    """One swing-book instruction for the planner (built by the cycle from the engine-checked swing
    lines, or by `set_tp_orders`). Distances are fractions of price; `size_nav` a NAV share."""

    line: str                                   # SW_<ticker>
    trade_id: str                               # trade:<id>
    side: Direction
    action: str                                 # "enter" | "exit" | "set_tp"
    symbol: str                                 # the broker vehicle symbol
    instrument_id: int | None = None
    size_nav: float = 0.0
    stop_pct: float | None = None
    target_pct: float | None = None
    time_stop_date: str | None = None
    tp_rate: float | None = None                # set_tp: the approved take-profit rate
    reason: str = ""


def set_tp_orders(trades: Iterable[Any]) -> list[SwingOrder]:
    """`set_tp` orders for every swing trade in `open_tp_missing` that carries its approved target
    (swing-book §4.4.3: the next swing slot's plan re-sends the take-profit for approval)."""
    from council.swing.book import line_id

    out: list[SwingOrder] = []
    for t in trades:
        if getattr(t, "state", None) != "open_tp_missing" or not getattr(t, "tp_rate", None):
            continue
        try:
            line = line_id(t.ticker)
        except ValueError:
            continue
        out.append(SwingOrder(line=line, trade_id=t.trade_id, side=t.side, action="set_tp",
                              symbol=t.ticker, instrument_id=t.instrument_id, tp_rate=float(t.tp_rate),
                              time_stop_date=getattr(t, "time_stop_date", None),
                              reason=f"{line}: re-send the approved take-profit (open_tp_missing)"))
    return out


def _split_cost(value: tuple[float, ...]) -> tuple[float, float, float]:
    """(per side, carry, fee) from a CostFn result of 2 or 3 numbers."""
    per_side, carry = float(value[0]), float(value[1])
    fee = float(value[2]) if len(value) > 2 else 0.0
    return per_side, carry, max(fee, 0.0) if math.isfinite(fee) else 0.0


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
        origin: Mapping[str, str] | None = None,
        ref_levels: Mapping[str, float] | None = None,
        economics: TradeEconomics | None = None,
        gap_ref: GapRef | None = None,
        capabilities: Any | None = None,
    ) -> None:
        self.nav = nav
        self.policy = policy
        self.capabilities = capabilities       # M5-D1 gates (operator.capabilities.Capabilities) or None
        self.by_line = policy.universe.by_symbol()
        self.vehicle_for = vehicle_for
        self.quotes = quotes
        self.stop_distance = stop_distance
        self.leverage_for = leverage_for
        self.eligibility = eligibility
        self.cost_bps = cost_bps
        self.origin = dict(origin or {})
        self.ref_levels = dict(ref_levels or {})
        self.economics = economics
        self.gap_ref = dict(gap_ref or {})
        self.gap_sigma = float(policy.risk["anti_chase_sigma"])
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

    def missing_caps(self, required: Iterable[str]) -> list[str]:
        """The M5-D1 capabilities among `required` that are not verified ([] without gates)."""
        if self.capabilities is None:
            return []
        return [c for c in required if not self.capabilities.has(c)]

    def vehicle_gaps(self, line: str, settlement: str, direction: str, leverage: int) -> list[str]:
        """The unverified capabilities an open of this vehicle class needs ([] without gates). A line
        the universe does not know fails closed (`capability_missing:unknown_line`)."""
        if self.capabilities is None:
            return []
        spec = self.by_line.get(line)
        if spec is None:
            return ["unknown_line"]
        return self.missing_caps(required_capabilities(spec, settlement, direction, leverage))

    # ------------------------------------------------------------------ prices
    def close_price(self, p: Position) -> float:
        """Rate a close would get: bid for longs, ask for shorts; else the broker's close rate,
        else the open rate."""
        quote = bid_ask(self.quotes.get(p.symbol))
        if quote is None:
            if p.close_rate is not None and math.isfinite(p.close_rate) and p.close_rate > 0:
                return p.close_rate
            return p.open_rate
        return quote[0] if p.is_buy else quote[1]

    def exposure(self, p: Position) -> float:
        """Unsigned USD exposure: the broker's figure from the snapshot when present (what the
        risk engine used), else units × close price."""
        return position_exposure(p, self.close_price(p))

    def unit_value(self, p: Position) -> float:
        """USD exposure per unit, consistent with `exposure` (sizes partial closes)."""
        exposure = self.exposure(p)
        if p.units > 0 and exposure > 0:
            return exposure / p.units
        return self.close_price(p)

    def whole(self, symbol: str) -> bool:
        row = self.eligibility.get(symbol)
        return whole_units_only(row) if row is not None else False

    def below_real_minimum(self, exposure: float, leverage: int) -> bool:
        """The order amount (exposure / leverage), copied at the mirror ratio, is below the real
        trade floor (no check without `economics`)."""
        if self.economics is None:
            return False
        return exposure / max(leverage, 1) < self.economics.min_amount_usd * (1 - 1e-9)

    def gap_blocked(self, line: str, price: float) -> str | None:
        """D20: the skip reason when the planned price has gapped from the pack's last close."""
        if line not in self.gap_ref:
            return None
        ref = self.gap_ref[line]
        if ref is None:
            return "gap_guard_no_reference"
        last, sigma_d = ref
        if not (math.isfinite(last) and last > 0 and math.isfinite(sigma_d) and sigma_d > 0):
            return "gap_guard_no_reference"
        if abs(math.log(price / last)) > self.gap_sigma * sigma_d:
            return "gap_guard"
        return None

    def stamp(self, draft: _Draft, fee: float) -> _Draft:
        """Origin, reference level and the private fee fields of one draft."""
        draft.origin = self.origin.get(draft.line)
        level = self.ref_levels.get(draft.line)
        draft.ref_level = float(level) if level is not None else None
        draft.fee_bps_nav = fee
        drag = self.economics.real_drag_per_leg if self.economics is not None else 0.0
        draft.fee_drag = drag if fee > 0 else 0.0
        return draft

    # ------------------------------------------------------------------ closes
    def close(self, line: str, p: Position, reason: str, units: float | None = None) -> _Draft:
        """Full close (units None) or partial close of `units`."""
        closed_units = p.units if units is None else units
        exposure = closed_units * self.unit_value(p)
        direction: Direction = "long" if p.is_buy else "short"
        per_side, _carry, fee = _split_cost(self.cost_bps(line, p.symbol, direction, p.leverage))
        dw = exposure / self.nav
        draft = _Draft(
            key=self._next_key(), kind="close" if units is None else "partial_close", line=line,
            symbol=p.symbol, instrument_id=p.instrument_id, direction=direction,
            settlement=p.settlement, leverage=p.leverage,
            delta_w=-dw if p.is_buy else dw, units=closed_units, amount_usd=exposure,
            risk_increasing=False, reason=reason, position_id=p.position_id,
            cost_bps_nav=per_side * dw, whole_units=self.whole(p.symbol),
        )
        self.drafts.append(self.stamp(draft, fee))
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
            price = self.unit_value(p)
            exposure = self.exposure(p)
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
            elif self.missing_caps(("partial_close",)):
                self.skip(line, "capability_missing:partial_close")     # a trim waits for S3
            elif row is not None and not row.allow_partial_close and self.reopen_gaps(line, p):
                # the re-open would be refused by a capability gate: a trim must not become a full exit
                self.skip(line, f"capability_missing:{self.reopen_gaps(line, p)[0]}")
            elif row is not None and not row.allow_partial_close:
                closing = self.close(line, p, f"{line}: reduce (full close, partial close not allowed)")
                self.reopen_remainder(line, p, remainder_units, closing.key)
            elif self.below_real_minimum(deduct * price, p.leverage):
                self.skip(line, "below_real_minimum")
            else:
                self.close(line, p, f"{line}: reduce (partial close, newest first)", units=deduct)
            remaining = 0.0
            break

    def reopen_gaps(self, line: str, p: Position) -> list[str]:
        """The M5-D1 capabilities a remainder re-open of `p` would lack ([] without gates)."""
        if self.capabilities is None:
            return []
        direction = "long" if p.is_buy else "short"
        return self.missing_caps(OPEN_PREREQUISITES) or self.vehicle_gaps(line, p.settlement, direction,
                                                                          int(p.leverage))

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
        gaps = self.missing_caps(OPEN_PREREQUISITES)
        if gaps:
            self.skip(line, f"capability_missing:{gaps[0]}")
            return None
        if choice is None:
            choice = self.vehicle_for(line, direction, leverage)
            if choice is None:
                self.skip(line, "no_eligible_vehicle")
                return None
        leverage = choice.leverage
        gaps = self.vehicle_gaps(line, choice.settlement, direction, leverage)
        if gaps:
            self.skip(line, f"capability_missing:{gaps[0]}")
            return None
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
        if bid > ask:
            self.skip(line, "crossed_quote")
            return None
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
        if self.below_real_minimum(exposure, leverage):
            self.skip(line, "below_real_minimum")
            return None
        gap = self.gap_blocked(line, price)
        if gap is not None:
            self.skip(line, gap)
            return None
        config = choice.config
        if not (config.allow_edit_stop_loss and config.allow_sl_tp):
            self.skip(line, "stop_not_allowed")
            return None
        fitted = fit_to_eligibility(distance, leverage, config, buffer_pp=self.sl_buffer_pp)
        if fitted is None or (direction == "long" and fitted >= 1):
            self.skip(line, "stop_outside_broker_bounds")
            return None
        if fitted > distance:
            reason = f"{reason} [stop widened to the broker minimum]"
        per_side, carry, fee = _split_cost(self.cost_bps(line, choice.symbol, direction, leverage))
        dw = exposure / self.nav
        draft = _Draft(
            key=self._next_key(), kind="open", line=line, symbol=choice.symbol,
            instrument_id=choice.instrument_id, direction=direction,
            settlement=choice.settlement, leverage=leverage,
            delta_w=dw if direction == "long" else -dw, units=units, amount_usd=exposure,
            risk_increasing=True, reason=reason,
            sl_rate=stop_loss_rate(direction, bid, ask, fitted), stop_distance=fitted,
            sl_margin_pct=sl_margin_pct(fitted, leverage), cost_bps_nav=per_side * dw,
            carry_bps_day_nav=carry * dw, depends_on=list(depends_on or []), whole_units=whole,
        )
        self.drafts.append(self.stamp(draft, fee))
        return draft

    def modify_sl(self, line: str, p: Position, distance: float, reason: str) -> _Draft | None:
        """Move an open position's stop-loss to `distance` (fitted to the broker's SL bounds); the
        rate is taken from the fresh quote exactly as an open's. risk_increasing when the move
        loosens the stop."""
        quote = bid_ask(self.quotes.get(p.symbol))
        if quote is None:
            self.skip(line, "no_quote")
            return None
        row = self.eligibility.get(p.symbol)
        if row is None:
            self.skip(line, "no_eligibility")
            return None
        direction: Direction = "long" if p.is_buy else "short"
        config = next((c for c in row.leverage_configs if c.direction == direction
                       and p.leverage in c.leverage_values and c.settlement == p.settlement), None)
        if config is None or not (config.allow_edit_stop_loss and config.allow_sl_tp):
            self.skip(line, "stop_not_allowed")
            return None
        fitted = fit_to_eligibility(distance, p.leverage, config, buffer_pp=self.sl_buffer_pp)
        if fitted is None or (direction == "long" and fitted >= 1):
            self.skip(line, "stop_outside_broker_bounds")
            return None
        bid, ask = quote
        rate = stop_loss_rate(direction, bid, ask, fitted)
        current = p.sl_rate or 0.0
        looser = rate < current if direction == "long" else (current <= 0 or rate > current)
        draft = _Draft(
            key=self._next_key(), kind="modify_sl", line=line, symbol=p.symbol,
            instrument_id=p.instrument_id, direction=direction, settlement=p.settlement,
            leverage=p.leverage, delta_w=0.0, units=p.units, amount_usd=self.exposure(p),
            risk_increasing=bool(looser), reason=reason, position_id=p.position_id, sl_rate=rate,
            stop_distance=fitted, sl_margin_pct=sl_margin_pct(fitted, p.leverage),
            whole_units=self.whole(p.symbol),
        )
        self.drafts.append(self.stamp(draft, 0.0))
        return draft

    # ------------------------------------------------------------------ swing book (SW-5)
    def _swing_marks(self, draft: _Draft, order: SwingOrder) -> _Draft:
        draft.sleeve, draft.swing_trade_id = "swing", order.trade_id
        draft.time_stop_date = order.time_stop_date
        return draft

    def _swing_cost(self, direction: Direction, settlement: str,
                    half_spread_bps: float | None = None) -> tuple[float, float, float]:
        """(per side, carry, fee) of a swing leg from the policy's stock cost floors (the core
        `cost_bps` table knows only universe lines, so it is never asked about a swing line). A
        single stock at 1x: real long or CFD short. Raises when the floors cannot be read (an entry
        then fails closed with `cost_unavailable`; never a silent zero)."""
        from council.risk.costs import carry_bps_day, fee_applies, per_side_bps

        per_side = per_side_bps(settlement, "stock", None, half_spread_bps, self.policy)
        carry = carry_bps_day(direction, settlement, 1, "stock", None, self.policy)
        fee = self.economics.fee_nav_bps if self.economics is not None and fee_applies(settlement, "stock") else 0.0
        values = (per_side, carry, fee)
        if not all(math.isfinite(v) and v >= 0 for v in values):
            raise ValueError("swing cost floors are not finite and non-negative")
        return values

    def _swing_limits(self, direction: Direction) -> tuple[float, float, float]:
        """(max size NAV, max stop distance, max loss NAV at the stop) of one swing entry: the code
        ceilings of `council.invariants`, tightened by `policy.swing` when it is loaded. The engine
        sized the order already; this is the planner's last structural check before a write."""
        from council import invariants as inv

        size, loss = inv.SWING_MAX_SIZE_NAV, (inv.SWING_MAX_LONG_LOSS_NAV if direction == "long"
                                              else inv.SWING_MAX_SHORT_LOSS_NAV)
        stop = inv.SWING_MAX_LONG_STOP_PCT if direction == "long" else inv.SWING_MAX_SHORT_STOP_PCT
        sp = getattr(self.policy, "swing", None)
        if sp is not None:
            size = min(size, float(sp.size.target_nav))
            loss = min(loss, float(sp.size.max_loss_nav_at_stop if direction == "long"
                                   else sp.size.short_max_loss_nav_at_stop))
            stop = min(stop, float(sp.stops.max_long_pct if direction == "long" else sp.stops.max_short_pct))
        return size, stop, loss

    def _swing_close(self, line: str, p: Position, order: SwingOrder) -> _Draft:
        """A full close of one swing position, priced from the swing cost floors (a risk-reducing
        leg is never refused for a cost it cannot price: zero, noted)."""
        direction: Direction = "long" if p.is_buy else "short"
        try:
            per_side, _carry, fee = self._swing_cost(direction, p.settlement)
        except Exception:  # noqa: BLE001 - an exit is risk-reducing; it goes, the gap is noted
            per_side, fee = 0.0, 0.0
            self.skip(line, "exit_cost_unpriced")
        exposure = p.units * self.unit_value(p)
        dw = exposure / self.nav
        draft = _Draft(
            key=self._next_key(), kind="close", line=line, symbol=p.symbol,
            instrument_id=p.instrument_id, direction=direction, settlement=p.settlement,
            leverage=p.leverage, delta_w=-dw if p.is_buy else dw, units=p.units, amount_usd=exposure,
            risk_increasing=False, reason=order.reason or f"{line}: swing exit",
            position_id=p.position_id, cost_bps_nav=per_side * dw, whole_units=self.whole(p.symbol),
        )
        self.drafts.append(self._swing_marks(self.stamp(draft, fee), order))
        return draft

    def swing_order(self, order: SwingOrder, positions: list[Position]) -> None:
        if order.action == "enter":
            self.swing_entry(order, positions)
        elif order.action == "exit":
            if not positions:
                self.skip(order.line, "swing_exit_no_position")
            for p in positions:
                self._swing_close(order.line, p, order)
        elif order.action == "set_tp":
            self.swing_set_tp(order, positions)
        else:
            self.skip(order.line, "swing_unknown_action")

    def swing_entry(self, order: SwingOrder, positions: list[Position]) -> None:
        line, direction = order.line, order.side
        if positions:
            self.skip(line, "swing_line_already_open")
            return
        gaps = self.missing_caps(OPEN_PREREQUISITES)
        if gaps:
            self.skip(line, f"capability_missing:{gaps[0]}")
            return
        if self.capabilities is not None:
            caps = SWING_VEHICLE_CAPS.get(direction, ("unknown_side",))
            if not self.capabilities.allows_vehicle(caps):
                verified = getattr(self.capabilities, "verified", frozenset())
                self.skip(line, f"capability_not_proven:{next(c for c in caps if c not in verified)}")
                return
        settlement: Settlement = "real" if direction == "long" else "cfd"
        row = self.eligibility.get(order.symbol)
        if row is None:
            self.skip(line, "no_eligibility")
            return
        config = select_config(row, direction, 1, settlement=settlement)
        if config is None:
            self.skip(line, "no_eligible_vehicle")
            return
        quote = bid_ask(self.quotes.get(order.symbol))
        if quote is None:
            self.skip(line, "no_quote")
            return
        bid, ask = quote
        if bid > ask:
            self.skip(line, "crossed_quote")
            return
        stop, target = order.stop_pct, order.target_pct
        if stop is None or not math.isfinite(stop) or not 0 < stop < 1:
            self.skip(line, "no_stop_distance")
            return
        if target is None or not math.isfinite(target) or target <= 0 or (direction == "short" and target >= 1):
            self.skip(line, "no_target_distance")
            return
        size = float(order.size_nav)
        if not (math.isfinite(size) and size > 0):
            self.skip(line, "no_size")
            return
        max_size, max_stop, max_loss = self._swing_limits(direction)
        if size > max_size + 1e-9:
            self.skip(line, "swing_size_above_cap")
            return
        if stop > max_stop + 1e-9:
            self.skip(line, "swing_stop_above_cap")
            return
        if size * stop > max_loss + 1e-9:
            self.skip(line, "swing_loss_at_stop_above_cap")
            return
        price = ask if direction == "long" else bid
        whole = whole_units_only(row)
        units = _floor_units(size * self.nav / price, whole)
        if row.max_units_per_order is not None and units > row.max_units_per_order:
            units = _floor_units(row.max_units_per_order, whole)
            self.skip(line, "capped_at_max_units_per_order")
        exposure = units * price
        if units <= 0 or exposure < row.min_position_exposure or exposure < config.min_position_amount:
            self.skip(line, "below_broker_minimum")
            return
        if self.below_real_minimum(exposure, 1):
            self.skip(line, "below_real_minimum")
            return
        fitted = fit_to_eligibility(stop, 1, config, buffer_pp=self.sl_buffer_pp)
        if fitted is None or fitted > stop * (1 + 1e-9) or fitted >= 1:
            # the approved stop is never widened (it is the thesis's invalidation and S1's loss)
            self.skip(line, "stop_outside_broker_bounds")
            return
        tp_rate = price * (1 + target) if direction == "long" else price * (1 - target)
        if target * 100 < config.min_tp_pct - 1e-9:
            mode = "none"
            self.skip(line, "tp_below_broker_minimum")
        elif self.capabilities is not None and self.capabilities.allows_vehicle((TP_ON_OPEN,)):
            mode = "body"
        elif config.allow_edit_tp:
            mode = "patch"
        else:
            mode = "none"
            self.skip(line, "tp_not_editable")
        mid = (bid + ask) / 2
        try:
            per_side, carry, fee = self._swing_cost(direction, settlement, (ask - bid) / 2 / mid * 1e4)
        except Exception:  # noqa: BLE001 - fail closed: an entry is never priced at zero
            self.skip(line, "cost_unavailable")
            return
        dw = exposure / self.nav
        draft = _Draft(
            key=self._next_key(), kind="open", line=line, symbol=order.symbol,
            instrument_id=order.instrument_id if order.instrument_id is not None else row.instrument_id,
            direction=direction, settlement=settlement, leverage=1,
            delta_w=dw if direction == "long" else -dw, units=units, amount_usd=exposure,
            risk_increasing=True, reason=order.reason or f"{line}: swing {direction} entry",
            sl_rate=stop_loss_rate(direction, bid, ask, stop), stop_distance=stop,
            sl_margin_pct=sl_margin_pct(stop, 1), cost_bps_nav=per_side * dw,
            carry_bps_day_nav=carry * dw, whole_units=whole, tp_rate=tp_rate, tp_mode=mode,
        )
        self.drafts.append(self._swing_marks(self.stamp(draft, fee), order))
        if mode == "patch":
            follow = _Draft(
                key=self._next_key(), kind="modify_tp", line=line, symbol=order.symbol,
                instrument_id=draft.instrument_id, direction=direction, settlement=settlement,
                leverage=1, delta_w=0.0, units=units, amount_usd=exposure, risk_increasing=False,
                reason=f"{line}: take-profit after the entry fill", sl_rate=draft.sl_rate,
                stop_distance=stop, depends_on=[draft.key], whole_units=whole, tp_rate=tp_rate,
            )
            self.drafts.append(self._swing_marks(self.stamp(follow, 0.0), order))

    def swing_set_tp(self, order: SwingOrder, positions: list[Position]) -> None:
        line, rate = order.line, order.tp_rate
        if not positions:
            self.skip(line, "swing_set_tp_no_position")
            return
        if rate is None or not math.isfinite(rate) or rate <= 0:
            self.skip(line, "no_target_rate")
            return
        for p in positions:
            if not p.sl_rate or p.sl_rate <= 0:
                self.skip(line, "set_tp_without_stop")      # a missing stop is its own alarm
                continue
            price = self.close_price(p)
            if (p.is_buy and rate <= price) or (not p.is_buy and rate >= price):
                self.skip(line, "target_already_reached")    # the watch flags it; the cycle exits
                continue
            row = self.eligibility.get(p.symbol)
            direction: Direction = "long" if p.is_buy else "short"
            config = select_config(row, direction, p.leverage, settlement=p.settlement) if row else None
            if config is not None and not config.allow_edit_tp:
                self.skip(line, "tp_not_editable")
                continue
            if config is not None and abs(rate / price - 1) * p.leverage * 100 < config.min_tp_pct - 1e-9:
                self.skip(line, "tp_below_broker_minimum")
                continue
            draft = _Draft(
                key=self._next_key(), kind="set_tp", line=line, symbol=p.symbol,
                instrument_id=p.instrument_id, direction=direction, settlement=p.settlement,
                leverage=p.leverage, delta_w=0.0, units=p.units, amount_usd=self.exposure(p),
                risk_increasing=False, reason=order.reason or f"{line}: set the approved take-profit",
                position_id=p.position_id, sl_rate=p.sl_rate, whole_units=self.whole(p.symbol),
                tp_rate=float(rate),
            )
            self.drafts.append(self._swing_marks(self.stamp(draft, 0.0), order))

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


def _order_and_cap(drafts: list[_Draft], line_rank: Mapping[str, int], cap: int,
                   skip: Callable[[str, str], None], *, total_cap: int | None = None) -> list[_Draft]:
    """Order by `_Draft.phase` (closes, modifications, core opens, swing opens, modify_tp), largest
    first inside a phase; keep at most `cap` COUNTED core legs (`_Draft.counted`) and at most
    `total_cap` core legs in all (None: no total cap), dropping from the tail; swing legs never
    count toward those caps and are capped at MAX_SWING_OPENS opens and as many modify_tp legs; then
    drop every leg whose dependency was dropped."""
    ordered = sorted(
        drafts,
        key=lambda d: (d.phase, -abs(d.delta_w), line_rank.get(d.line, 999), d.key),
    )
    kept: list[_Draft] = []
    counted = core = swing_opens = swing_tps = 0
    for d in ordered:
        if d.sleeve == "swing":
            if d.kind == "open":
                if swing_opens >= MAX_SWING_OPENS:
                    skip(d.line, "swing_leg_cap")
                    continue
                swing_opens += 1
            elif d.kind == "modify_tp":
                if swing_tps >= MAX_SWING_OPENS:
                    skip(d.line, "swing_leg_cap")
                    continue
                swing_tps += 1
            kept.append(d)
            continue
        if (d.counted and counted >= cap) or (total_cap is not None and core >= total_cap):
            skip(d.line, "leg_cap")
            continue
        kept.append(d)
        core += 1
        counted += int(d.counted)
    changed = True
    while changed:
        keys = {d.key for d in kept}
        orphaned = [d for d in kept if any(k not in keys for k in d.depends_on)]
        changed = bool(orphaned)
        for d in orphaned:
            skip(d.line, "dependency_dropped")
            kept.remove(d)
    return kept


def position_exposure(p: Position, close_price: float) -> float:
    """Unsigned USD exposure of one position: the snapshot's broker `exposure_usd` when present
    (finite, >= 0), else units × `close_price`."""
    if p.exposure_usd is not None and math.isfinite(p.exposure_usd) and p.exposure_usd >= 0:
        return float(p.exposure_usd)
    return p.units * close_price


def _symbol_lookup(symbol_for: SymbolFor) -> Callable[[int], str | None]:
    if symbol_for is None:
        return lambda _iid: None
    if isinstance(symbol_for, Mapping):
        return symbol_for.get
    return symbol_for


def _assemble(
    kept: list[_Draft],
    *,
    current: Mapping[str, float],
    line_gross: Mapping[str, float],
    locked_gross: float,
    locked_net: float,
    skipped: list[str],
) -> Plan:
    """Number the kept drafts, walk each line's weight through them and total the plan."""
    seq_of = {d.key: i + 1 for i, d in enumerate(kept)}
    running = dict(current)
    legs: list[Leg] = []
    for d in kept:
        before = running.get(d.line, 0.0)
        after = before + d.delta_w
        running[d.line] = after
        legs.append(
            Leg(
                seq=seq_of[d.key], kind=d.kind, symbol=d.symbol, line=d.line,
                instrument_id=d.instrument_id, direction=d.direction, settlement=d.settlement,
                leverage=d.leverage, weight_before=round(before, 6), weight_after=round(after, 6),
                stop_distance=d.stop_distance, sl_margin_pct=d.sl_margin_pct,
                cost_bps_nav=d.cost_bps_nav, carry_bps_day_nav=d.carry_bps_day_nav,
                risk_increasing=d.risk_increasing, reason=d.reason, amount_usd=d.amount_usd,
                units=d.units, sl_rate=d.sl_rate, position_id=d.position_id,
                depends_on=[seq_of[k] for k in d.depends_on], whole_units=d.whole_units,
                origin=d.origin if d.origin in ("reference", "discretionary") else None,  # type: ignore[arg-type]
                ref_level=d.ref_level, fee_bps_nav=d.fee_bps_nav, fee_drag=d.fee_drag,
                sleeve="swing" if d.sleeve == "swing" else None, swing_trade_id=d.swing_trade_id,
                tp_rate=d.tp_rate, tp_mode=d.tp_mode,  # type: ignore[arg-type]
                time_stop_date=d.time_stop_date,
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
        skipped=skipped,
        fee_bps_nav=sum(leg.fee_bps_nav for leg in legs),
    )


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
    origin: Mapping[str, str] | None = None,
    ref_levels: Mapping[str, float] | None = None,
    economics: TradeEconomics | None = None,
    gap_ref: GapRef | None = None,
    capabilities: Any | None = None,
    swing: Iterable[SwingOrder] | None = None,
    swing_map: Any = None,
) -> Plan:
    """Legs that move the lines in `target_w` from `snapshot` to their targets. See the module
    rules: ONLY lines present in `target_w` are planned (pass `changed_targets(decision)`), and a
    position's current exposure is the snapshot's broker `exposure_usd` when present.

    `nav_usd` should be the snapshot's equity (the denominator the engine used).
    `cost_bps(line, vehicle_symbol, direction, leverage)` → (per-side bps, carry bps/day[, fixed
    fee bps of NAV]); a leg's variable cost in bps of NAV is per_side × |Δw|. `max_legs` overrides
    the policy's counted-leg cap (the total cap stays proposal.max_legs_total). `origin` and
    `ref_levels` (per line) are stamped on the legs; `economics` (private) sets the real trade
    floor and the fee drag; `gap_ref` arms the gap guard. `capabilities` (M5-D1, connected broker only; None = no gate):
    without `partial_close` a trim is skipped (a full exit still works); without `rates_entitled` or
    `price_units` no open is planned; an open's vehicle class must be verified. A flatten uses
    `build_flatten_plan`. `swing` / `swing_map`: the swing-book orders and the live swing vehicle
    map (module rules)."""
    if not (math.isfinite(nav_usd) and nav_usd > 0):
        raise ValueError("nav_usd must be positive")
    from council.swing.book import is_swing_line

    universe = policy.universe
    v2l = vehicle_to_line(universe)
    if swing_map is not None:
        v2l = swing_map.merged_lines(v2l)
    orders = list(swing or [])
    line_rank = {line.symbol: i for i, line in enumerate(universe.lines)}
    proposal = policy.risk["proposal"]
    cap = int(proposal["max_legs"]) if max_legs is None else max_legs
    total_cap = max(int(proposal.get("max_legs_total", cap)), int(proposal["max_legs"]))
    b = _Builder(
        policy=policy, nav=nav_usd, vehicle_for=vehicle_for, quotes=quotes,
        stop_distance=stop_distance, leverage_for=leverage_for, eligibility=eligibility,
        cost_bps=cost_bps, origin=origin, ref_levels=ref_levels, economics=economics,
        gap_ref=gap_ref, capabilities=capabilities,
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
        if is_swing_line(line):             # planned from the swing orders only
            if not any(o.line == line for o in orders):
                b.skip(line, "swing_line_without_order")
            continue
        if line not in line_rank:
            if not line.startswith(UNMAPPED_PREFIX):     # unmapped: already noted as locked
                b.skip(line, "unknown_line")
            continue
        b.plan_line(line, float(target_w[line]), current.get(line, 0.0), by_line.get(line, []))

    for order in orders:
        if not is_swing_line(order.line):
            b.skip(order.line, "swing_order_not_a_swing_line")
            continue
        b.swing_order(order, by_line.get(order.line, []))

    # Structural guarantee: only target lines and swing-order lines were planned.
    kept = _order_and_cap(b.drafts, line_rank, cap, b.skip, total_cap=total_cap)
    return _assemble(
        kept, current=current, line_gross=line_gross, locked_gross=locked_gross,
        locked_net=locked_net, skipped=b.skipped,
    )


def build_flatten_plan(
    *,
    snapshot: ExposureSnapshot,
    quotes: Mapping[str, Any],
    eligibility: Mapping[str, EligibilityRow],
    nav_usd: float,
    policy: Policy,
    symbol_for: SymbolFor = None,
    cost_bps: CostFn | None = None,
) -> Plan:
    """Close EVERY position in `snapshot` (kill-switch flatten): one full `close` per position,
    UNMAPPED ones and hedged lines included; no opens, no partial closes, no leg cap.

    `symbol_for(instrument_id)` (mapping or callable) names a position's vehicle when the snapshot
    only knows it as `UNMAPPED_<id>`; a position that still maps to no line is closed on the line
    `UNMAPPED_<instrument_id>`. Exposure follows the module rule (broker `exposure_usd`, else units
    × close rate). A close the eligibility row says is not allowed is still planned (the broker
    decides; a rejection leaves the decision for operator review) and its reason says so.
    `cost_bps` is optional here (reporting only; a flatten is never cost-gated)."""
    if not (math.isfinite(nav_usd) and nav_usd > 0):
        raise ValueError("nav_usd must be positive")
    universe = policy.universe
    v2l = vehicle_to_line(universe)
    line_rank = {line.symbol: i for i, line in enumerate(universe.lines)}
    lookup = _symbol_lookup(symbol_for)
    b = _Builder(
        policy=policy, nav=nav_usd, vehicle_for=lambda *_: None, quotes=quotes,
        stop_distance={}, leverage_for={}, eligibility=eligibility,
        cost_bps=cost_bps or (lambda *_: (0.0, 0.0)),
    )
    current: dict[str, float] = {}
    line_gross: dict[str, float] = {}
    for p in snapshot.positions:
        symbol = p.symbol
        if symbol.startswith(UNMAPPED_PREFIX) or symbol not in v2l:
            symbol = lookup(p.instrument_id) or symbol
        position = p if symbol == p.symbol else p.model_copy(update={"symbol": symbol})
        line = v2l.get(symbol) or f"{UNMAPPED_PREFIX}{p.instrument_id}"
        w = b.exposure(position) / nav_usd
        current[line] = current.get(line, 0.0) + (w if p.is_buy else -w)
        line_gross[line] = line_gross.get(line, 0.0) + w
        row = eligibility.get(symbol)
        note = " (eligibility reports close not allowed)" if row is not None and not row.allow_close else ""
        b.close(line, position, f"{line}: flatten (close){note}")
    kept = _order_and_cap(b.drafts, line_rank, len(b.drafts), b.skip)
    return _assemble(
        kept, current=current, line_gross=line_gross, locked_gross=0.0, locked_net=0.0,
        skipped=b.skipped,
    )


# ---------------------------------------------------------------------------------- smoke tickets
class SmokePlanError(ValueError):
    """A smoke intent the shared leg construction refuses; the message is the skip code."""


@dataclass(frozen=True)
class SmokeIntent:
    """One leg of an operator smoke ticket (M5-D2): `open` (choice + exact units + stop distance),
    `close` / `partial_close` (position [+ units]) or `modify_sl` (position + stop distance)."""

    kind: LegKind
    reason: str
    choice: VehicleChoice | None = None
    units: float | None = None
    stop_distance: float | None = None
    position: Position | None = None


def smoke_line(universe: Universe, symbol: str, instrument_id: int | None) -> str:
    """The line a smoke leg is booked on: the vehicle's line, else UNMAPPED_<instrument id>."""
    line = vehicle_to_line(universe).get(symbol)
    if line is not None:
        return line
    return f"{UNMAPPED_PREFIX}{instrument_id}"


def build_smoke_plan(
    *,
    intents: Iterable[SmokeIntent],
    snapshot: ExposureSnapshot,
    quotes: Mapping[str, Any],
    eligibility: Mapping[str, EligibilityRow],
    nav_usd: float,
    policy: Policy,
    economics: TradeEconomics | None = None,
    cost_bps: CostFn | None = None,
) -> Plan:
    """The legs of one smoke ticket, built with the planner's own leg and stop-loss construction
    (see the module rules). The weight walk starts from `snapshot`, so the approval's drift and
    gross re-checks read these legs exactly as a rebalance's. Raises SmokePlanError on any skip."""
    if not (math.isfinite(nav_usd) and nav_usd > 0):
        raise ValueError("nav_usd must be positive")
    items = list(intents)
    if not items:
        raise SmokePlanError("smoke_no_legs")
    universe = policy.universe

    def line_of(symbol: str, instrument_id: int | None) -> str:
        return smoke_line(universe, symbol, instrument_id)

    stops: dict[str, float] = {}
    leverage: dict[str, int] = {}
    for it in items:
        if it.kind == "open":
            if it.choice is None or it.units is None or it.stop_distance is None:
                raise SmokePlanError("smoke_open_incomplete")
            line = line_of(it.choice.symbol, it.choice.instrument_id)
            stops[line], leverage[line] = float(it.stop_distance), int(it.choice.leverage)
    b = _Builder(
        policy=policy, nav=nav_usd, vehicle_for=lambda *_: None, quotes=quotes,
        stop_distance=stops, leverage_for=leverage, eligibility=eligibility,
        cost_bps=cost_bps or (lambda *_: (0.0, 0.0)), economics=economics,
    )
    current: dict[str, float] = {}
    line_gross: dict[str, float] = {}
    for p in snapshot.positions:
        line = line_of(p.symbol, p.instrument_id)
        w = b.exposure(p) / nav_usd
        current[line] = current.get(line, 0.0) + (w if p.is_buy else -w)
        line_gross[line] = line_gross.get(line, 0.0) + w
    for it in items:
        before = len(b.skipped)
        if it.kind == "open":
            assert it.choice is not None
            line = line_of(it.choice.symbol, it.choice.instrument_id)
            b.open(line, it.choice.direction, None, reason=it.reason, choice=it.choice, units=it.units)
        else:
            if it.position is None:
                raise SmokePlanError("smoke_position_missing")
            line = line_of(it.position.symbol, it.position.instrument_id)
            if it.kind == "close":
                b.close(line, it.position, it.reason)
            elif it.kind == "partial_close":
                if it.units is None or not 0 < it.units < it.position.units:
                    raise SmokePlanError("smoke_partial_units_invalid")
                b.close(line, it.position, it.reason, units=it.units)
            else:
                if it.stop_distance is None:
                    raise SmokePlanError("smoke_stop_distance_missing")
                b.modify_sl(line, it.position, float(it.stop_distance), it.reason)
        fatal = [note for note in b.skipped[before:] if not note.endswith("capped_at_max_units_per_order")]
        if fatal:
            raise SmokePlanError(fatal[0].split(": ", 1)[-1])
    return _assemble(b.drafts, current=current, line_gross=line_gross, locked_gross=0.0,
                     locked_net=0.0, skipped=b.skipped)
