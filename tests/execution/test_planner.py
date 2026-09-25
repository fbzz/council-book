"""Planner cases: new, add tranche, 4th-tranche skip, partial, full, flip, minimums, leg cap, stops,
snapshot exposure, target-lines-only, flatten."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from council.broker.eligibility import (
    VehicleChoice,
    parse_eligibility,
    resolve_vehicle,
    select_config,
)
from council.broker.fake import eligibility_row, leverage_config
from council.execution.planner import (
    _Draft,
    _order_and_cap,
    build_flatten_plan,
    build_plan,
    changed_targets,
    vehicle_to_line,
)
from council.models.broker import ExposureSnapshot, Position, Quote
from council.models.risk import RiskDecision

NOW = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
NAV = 10_000.0
PRICES = {  # symbol -> (instrument id, bid, ask)
    "SPX500": (101, 100.0, 100.1),
    "NSDQ100": (102, 200.0, 200.2),
    "GOLD": (103, 50.0, 50.05),
    "EURUSD": (104, 1.1, 1.1002),
    "SOXX": (107, 250.0, 250.25),
    "GBPUSD": (106, 1.3, 1.3002),
}
STOPS = {"SPX": 0.08, "NDX": 0.08, "GOLD": 0.08, "EURUSD": 0.04, "GBPUSD": 0.04, "SEMIS": 0.10}


def rows(**overrides):
    raw = []
    for symbol, (iid, _b, _a) in PRICES.items():
        raw.append(overrides.get(symbol) or eligibility_row(symbol, iid))
    return {r.symbol: r for r in parse_eligibility({"eligibilities": raw}, NOW)}


def quotes(*, without=()):
    return {
        s: Quote(symbol=s, instrument_id=i, bid=b, ask=a, at=NOW)
        for s, (i, b, a) in PRICES.items() if s not in without
    }


def pos(pid, symbol, units, *, is_buy=True, age_h=0, leverage=1, sl=1.0, exposure=None, close_rate=None):
    iid, bid, ask = PRICES[symbol]
    return Position(
        position_id=pid, instrument_id=iid, symbol=symbol, is_buy=is_buy, leverage=leverage,
        units=units, open_rate=ask if is_buy else bid, amount=units * ask / leverage,
        sl_rate=sl, opened_at=NOW - timedelta(hours=age_h), exposure_usd=exposure,
        close_rate=close_rate,
    )


def snapshot(*positions):
    return ExposureSnapshot(
        taken_at=NOW, equity_usd=NAV, credit_usd=NAV, positions=list(positions), signed_w={},
        gross=0.0, net=0.0, margin_use=0.0,
    )


def build(policy, target, *positions, eligibility=None, leverage=None, stops=None, qs=None,
          vehicle_for=None, **kw):
    elig = eligibility or rows()
    lines = policy.universe.by_symbol()

    def default_vehicle_for(line, direction, lev):
        return resolve_vehicle(lines[line], direction, lev, elig, lambda v, r, c: 5.0)

    return build_plan(
        snapshot=snapshot(*positions), target_w=target,
        vehicle_for=vehicle_for or default_vehicle_for, quotes=qs if qs is not None else quotes(),
        stop_distance=stops if stops is not None else STOPS, leverage_for=leverage or {},
        eligibility=elig, cost_bps=lambda line, sym, d, lev: (5.0, 1.0 if d == "short" else 0.0),
        nav_usd=NAV, policy=policy, **kw,
    )


def floor6(x):
    return math.floor(x * 1e6 + 1e-6) / 1e6


# ------------------------------------------------------------------------------ opens
def test_new_position_sized_by_units_with_stop(policy):
    plan = build(policy, {"SPX": 0.15})
    (leg,) = plan.legs
    assert (leg.kind, leg.symbol, leg.direction, leg.instrument_id) == ("open", "SPX500", "long", 101)
    assert leg.units == floor6(0.15 * NAV / 100.1)
    assert leg.sl_rate == pytest.approx(100.1 * (1 - 0.08))
    assert leg.sl_margin_pct == pytest.approx(8.0)
    assert leg.risk_increasing and leg.reason.startswith("SPX:")
    assert leg.weight_before == 0 and leg.weight_after == pytest.approx(0.15, abs=1e-4)
    assert leg.cost_bps_nav == pytest.approx(5.0 * 0.15, rel=1e-3)
    assert plan.gross_after == pytest.approx(0.15, abs=1e-4)


def test_short_open_uses_bid_and_stop_above(policy):
    (leg,) = build(policy, {"EURUSD": -0.2}).legs
    assert leg.direction == "short"
    assert leg.units == floor6(0.2 * NAV / 1.1)
    assert leg.sl_rate == pytest.approx(1.1 * 1.04)
    assert leg.carry_bps_day_nav == pytest.approx(0.2, rel=1e-3)
    assert leg.weight_after == pytest.approx(-0.2, abs=1e-4)


def test_every_open_carries_a_stop_on_the_right_side(policy):
    plan = build(policy, {"SPX": 0.2, "NDX": 0.3, "GOLD": 0.1, "EURUSD": -0.1, "GBPUSD": 0.1})
    opens = [leg for leg in plan.legs if leg.kind == "open"]
    assert len(opens) == 5
    for leg in opens:
        _iid, bid, ask = PRICES[leg.symbol]
        assert leg.sl_rate is not None and leg.stop_distance is not None
        assert (leg.sl_rate < ask) if leg.direction == "long" else (leg.sl_rate > bid)


def test_leverage_multiplies_stop_margin_percentage(policy):
    (leg,) = build(policy, {"SPX": 0.2}, leverage={"SPX": 2}).legs
    assert leg.leverage == 2 and leg.sl_margin_pct == pytest.approx(16.0)


def test_add_tranche_opens_the_difference(policy):
    existing = pos(1, "SPX500", 10)              # 1000 / 10000 at the bid = 0.10
    (leg,) = build(policy, {"SPX": 0.15}, existing).legs
    assert leg.kind == "open" and "tranche 2/3" in leg.reason
    assert leg.units == floor6(0.05 * NAV / 100.1)
    assert leg.weight_before == pytest.approx(0.10)


def test_fourth_tranche_is_skipped(policy):
    three = [pos(i, "SPX500", 5, age_h=i) for i in (1, 2, 3)]
    plan = build(policy, {"SPX": 0.5}, *three)
    assert plan.legs == []
    assert "SPX: max_tranches" in plan.skipped


def test_third_tranche_is_allowed(policy):
    two = [pos(i, "SPX500", 5, age_h=i) for i in (1, 2)]
    (leg,) = build(policy, {"SPX": 0.5}, *two).legs
    assert "tranche 3/3" in leg.reason


# ------------------------------------------------------------------------------ decreases
def test_partial_close_newest_first_with_units_to_deduct(policy):
    older, newer = pos(1, "SPX500", 10, age_h=48), pos(2, "SPX500", 10, age_h=1)
    (leg,) = build(policy, {"SPX": 0.15}, older, newer).legs
    assert (leg.kind, leg.position_id) == ("partial_close", 2)
    assert leg.units == pytest.approx(5.0)           # 0.05 × 10000 / bid 100
    assert not leg.risk_increasing
    assert leg.weight_after == pytest.approx(0.15)


def test_reduce_spans_tranches_newest_first(policy):
    older, newer = pos(1, "SPX500", 10, age_h=48), pos(2, "SPX500", 5, age_h=1)
    plan = build(policy, {"SPX": 0.02}, older, newer)
    kinds = sorted((leg.kind, leg.position_id, round(leg.units, 6)) for leg in plan.legs)
    assert kinds == [("close", 2, 5.0), ("partial_close", 1, 8.0)]
    assert plan.legs[0].position_id == 1            # largest risk reduction first
    assert plan.legs[-1].weight_after == pytest.approx(0.02)


def test_remainder_below_minimum_becomes_full_close(policy):
    elig = rows(SPX500=eligibility_row("SPX500", 101, min_position_exposure=500))
    (leg,) = build(policy, {"SPX": 0.04}, pos(1, "SPX500", 10), eligibility=elig).legs
    assert leg.kind == "close" and leg.units == 10


def test_remainder_at_minimum_stays_partial(policy):
    elig = rows(SPX500=eligibility_row("SPX500", 101, min_position_exposure=400))
    (leg,) = build(policy, {"SPX": 0.04}, pos(1, "SPX500", 10), eligibility=elig).legs
    assert leg.kind == "partial_close" and leg.units == pytest.approx(6.0)


def test_partial_close_not_allowed_closes_and_reopens_remainder(policy):
    elig = rows(SPX500=eligibility_row("SPX500", 101, allow_partial_close=False))
    plan = build(policy, {"SPX": 0.06}, pos(1, "SPX500", 10), eligibility=elig)
    close, reopen = plan.legs
    assert close.kind == "close" and not close.risk_increasing
    assert reopen.kind == "open" and reopen.risk_increasing and reopen.depends_on == [close.seq]
    assert reopen.units == pytest.approx(6.0) and reopen.sl_rate is not None   # 10 - 4 deducted
    assert reopen.symbol == "SPX500" and reopen.weight_after == pytest.approx(0.06, abs=1e-3)


def test_to_zero_closes_everything(policy):
    plan = build(policy, {"SPX": 0.0}, pos(1, "SPX500", 10, age_h=2), pos(2, "SPX500", 3))
    assert sorted((leg.kind, leg.position_id) for leg in plan.legs) == [("close", 1), ("close", 2)]
    assert plan.gross_after == pytest.approx(0.0)


def test_flip_closes_then_opens_with_depends_on(policy):
    plan = build(policy, {"SPX": -0.05}, pos(1, "SPX500", 10))
    close, open_ = plan.legs
    assert close.kind == "close" and not close.risk_increasing
    assert open_.kind == "open" and open_.direction == "short" and open_.risk_increasing
    assert open_.depends_on == [close.seq]
    assert open_.sl_rate == pytest.approx(100.0 * 1.08)
    assert open_.weight_after == pytest.approx(-0.05, abs=1e-4)


def test_lines_absent_from_target_are_held(policy):
    plan = build(policy, {"SPX": 0.1}, pos(1, "NSDQ100", 5))
    assert {leg.symbol for leg in plan.legs} == {"SPX500"}


# ------------------------------------------------------------------------------ skips
def test_below_broker_minimum_is_skipped(policy):
    plan = build(policy, {"SPX": 0.0005})              # 5 < minPositionExposure 10
    assert plan.legs == [] and "SPX: below_broker_minimum" in plan.skipped


def test_at_broker_minimum_passes(policy):
    assert len(build(policy, {"SPX": 0.0011}).legs) == 1   # 11 >= 10


def test_margin_below_min_position_amount_is_skipped(policy):
    configs = [leverage_config(direction="LONG", min_position_amount=1_000.0)]
    elig = rows(SPX500=eligibility_row("SPX500", 101, configs=configs))
    plan = build(policy, {"SPX": 0.05}, eligibility=elig)
    assert "SPX: below_broker_minimum" in plan.skipped


def test_stop_outside_broker_bounds_is_skipped(policy):
    tight = rows(SPX500=eligibility_row("SPX500", 101, configs=[leverage_config(max_sl_pct=5.0)]))
    assert "SPX: stop_outside_broker_bounds" in build(policy, {"SPX": 0.1}, eligibility=tight).skipped
    ok = rows(SPX500=eligibility_row("SPX500", 101, configs=[leverage_config(max_sl_pct=8.5)]))
    assert len(build(policy, {"SPX": 0.1}, eligibility=ok).legs) == 1     # 8 <= 8.5 - 0.5


def test_min_stop_bound_widens_the_stop_with_buffer(policy):
    # behaviour change: below the broker minimum (+ buffer) the stop is WIDENED (risk.stops), not skipped
    elig = rows(SPX500=eligibility_row("SPX500", 101, configs=[leverage_config(min_sl_pct=7.6)]))
    plan = build(policy, {"SPX": 0.1}, eligibility=elig)
    (leg,) = plan.legs
    assert "SPX: stop_outside_broker_bounds" not in plan.skipped
    assert leg.stop_distance == pytest.approx(0.081)                   # (7.6 + 0.5) / 100
    assert leg.sl_margin_pct == pytest.approx(8.1)
    assert leg.sl_rate == pytest.approx(100.1 * (1 - 0.081))
    assert "stop widened" in leg.reason


def test_widened_stop_accounts_for_leverage(policy):
    configs = [leverage_config(min_sl_pct=20.0)]
    elig = rows(SPX500=eligibility_row("SPX500", 101, configs=configs))
    (leg,) = build(policy, {"SPX": 0.1}, eligibility=elig, leverage={"SPX": 2}).legs
    assert leg.leverage == 2
    assert leg.stop_distance == pytest.approx(20.5 / 200)              # d × L × 100 = min + buffer
    assert leg.sl_margin_pct == pytest.approx(20.5)


def test_short_widened_stop_sits_above_the_bid(policy):
    configs = [leverage_config(direction="SHORT", min_sl_pct=6.0)]
    elig = rows(EURUSD=eligibility_row("EURUSD", 104, configs=configs))
    (leg,) = build(policy, {"EURUSD": -0.1}, eligibility=elig).legs
    assert leg.stop_distance == pytest.approx(0.065)
    assert leg.sl_rate == pytest.approx(1.1 * 1.065)


@pytest.mark.parametrize("flags", [{"allow_edit_stop_loss": False}, {"allow_sl_tp": False}])
def test_config_that_cannot_carry_our_stop_is_skipped(policy, flags):
    elig = rows(SPX500=eligibility_row("SPX500", 101, configs=[leverage_config(**flags)]))
    # resolve_vehicle already refuses such a config; a caller-supplied choice is refused by the planner
    assert "SPX: no_eligible_vehicle" in build(policy, {"SPX": 0.1}, eligibility=elig).skipped
    config = elig["SPX500"].leverage_configs[0]
    assert select_config(elig["SPX500"], "long", 1) is None

    def forced(line, direction, lev):
        return VehicleChoice(symbol="SPX500", instrument_id=101, settlement="cfd", leverage=1, config=config)

    plan = build(policy, {"SPX": 0.1}, eligibility=elig, vehicle_for=forced)
    assert plan.legs == [] and "SPX: stop_not_allowed" in plan.skipped


def test_crossed_quote_never_opens(policy):
    qs = quotes()
    qs["SPX500"] = Quote(symbol="SPX500", instrument_id=101, bid=100.2, ask=100.1, at=NOW)
    plan = build(policy, {"SPX": 0.1}, qs=qs)
    assert plan.legs == [] and "SPX: crossed_quote" in plan.skipped


def test_missing_stop_distance_never_opens(policy):
    plan = build(policy, {"SPX": 0.1}, stops={})
    assert plan.legs == [] and "SPX: no_stop_distance" in plan.skipped


def test_no_eligible_vehicle_is_skipped(policy):
    plan = build(policy, {"OIL": 0.05})
    assert plan.legs == [] and "OIL: no_eligible_vehicle" in plan.skipped


def test_whole_units_are_floored(policy):
    elig = rows(SPX500=eligibility_row("SPX500", 101, units_quantity_type="WholeUnits"))
    (leg,) = build(policy, {"SPX": 0.155}, eligibility=elig).legs
    assert leg.units == 15.0


def test_max_units_per_order_caps(policy):
    elig = rows(SPX500=eligibility_row("SPX500", 101, max_units_per_order=5.0))
    plan = build(policy, {"SPX": 0.2}, eligibility=elig)
    assert plan.legs[0].units == 5.0 and "SPX: capped_at_max_units_per_order" in plan.skipped


def test_unmapped_position_is_locked(policy):
    stray = Position(position_id=9, instrument_id=999, symbol="UNMAPPED_999", is_buy=True,
                     units=10, open_rate=10.0, amount=100.0, sl_rate=9.0)
    plan = build(policy, {"SPX": 0.0}, stray)
    assert plan.legs == [] and "UNMAPPED_999: unmapped_position_locked" in plan.skipped
    assert plan.gross_before == pytest.approx(0.01)


def test_hedged_line_is_skipped_unless_target_zero(policy):
    both = (pos(1, "SPX500", 5), pos(2, "SPX500", 3, is_buy=False))
    assert "SPX: hedged_state" in build(policy, {"SPX": 0.1}, *both).skipped
    assert len(build(policy, {"SPX": 0.0}, *both).legs) == 2


def test_unknown_line_is_skipped(policy):
    assert "AAPL: unknown_line" in build(policy, {"AAPL": 0.1}).skipped


# ------------------------------------------------------------------------------ ordering + cap
def test_closes_first_largest_reduction_first_then_opens_largest_first(policy):
    plan = build(
        policy, {"SPX": 0.0, "NDX": 0.0, "GOLD": 0.3, "EURUSD": 0.1},
        pos(1, "SPX500", 5), pos(2, "NSDQ100", 10),
    )
    assert [(leg.kind, leg.symbol) for leg in plan.legs] == [
        ("close", "NSDQ100"), ("close", "SPX500"), ("open", "GOLD"), ("open", "EURUSD"),
    ]
    assert [leg.seq for leg in plan.legs] == [1, 2, 3, 4]


def test_leg_cap_from_policy(policy):
    assert policy.risk["proposal"]["max_legs"] == 8
    nine = [pos(i, s, 2, age_h=i) for i, s in enumerate(["SPX500"] * 3 + ["NSDQ100"] * 3 + ["GOLD"] * 3, 1)]
    plan = build(policy, {"SPX": 0.0, "NDX": 0.0, "GOLD": 0.0}, *nine)
    assert len(plan.legs) == 8 and any(s.endswith("leg_cap") for s in plan.skipped)
    eight = nine[:8]
    plan = build(policy, {"SPX": 0.0, "NDX": 0.0, "GOLD": 0.0}, *eight)
    assert len(plan.legs) == 8 and not any(s.endswith("leg_cap") for s in plan.skipped)


def test_leg_cap_override_and_flip_open_dropped_with_its_budget(policy):
    plan = build(policy, {"SPX": -0.05}, pos(1, "SPX500", 10), max_legs=1)
    assert [leg.kind for leg in plan.legs] == ["close"]
    assert "SPX: leg_cap" in plan.skipped


def test_dependency_on_a_dropped_leg_is_pruned():
    def draft(key, kind, dw, deps=()):
        return _Draft(key=key, kind=kind, line="SPX", symbol="SPX500", instrument_id=101,
                      direction="long", settlement="cfd", leverage=1, delta_w=dw, units=1.0,
                      amount_usd=1.0, risk_increasing=kind == "open", reason="x", depends_on=list(deps))

    notes = []
    kept = _order_and_cap(
        [draft(1, "close", -0.01), draft(2, "open", 0.5, deps=(3,)), draft(3, "close", -0.001),
         draft(4, "open", 0.2, deps=(2,))],
        {"SPX": 0}, 3, lambda line, reason: notes.append(reason),
    )
    # order: close 1, close 3, open 2, open 4 -> cap keeps 1, 3, 2; open 4 depends on 2 (kept)
    assert [d.key for d in kept] == [1, 3, 2]
    notes.clear()
    kept = _order_and_cap(
        [draft(1, "close", -0.01), draft(2, "open", 0.5, deps=(9,)), draft(4, "open", 0.2, deps=(2,))],
        {"SPX": 0}, 8, lambda line, reason: notes.append(reason),
    )
    assert [d.key for d in kept] == [1]          # 2 depends on a missing leg, 4 on 2: both pruned
    assert notes == ["dependency_dropped", "dependency_dropped"]


def test_vehicle_to_line_covers_every_candidate(policy):
    mapping = vehicle_to_line(policy.universe)
    assert mapping["SPX500"] == "SPX" and mapping["EQQQ.L"] == "NDX" and mapping["GOLD"] == "GOLD"


def test_nav_must_be_positive(policy):
    with pytest.raises(ValueError):
        build_plan(snapshot=snapshot(), target_w={}, vehicle_for=lambda *a: None, quotes={},
                   stop_distance={}, leverage_for={}, eligibility={}, cost_bps=lambda *a: (0, 0),
                   nav_usd=0.0, policy=policy)


# ------------------------------------------------------------------------------ snapshot exposure
def test_held_line_at_snapshot_exposure_produces_no_legs(policy):
    # the quote says 10 × 100 = 0.10, the broker says 0.15: the engine held SPX at 0.15
    held = pos(1, "SPX500", 10, exposure=1_500.0)
    plan = build(policy, {"SPX": 0.15}, held)
    assert plan.legs == [] and plan.skipped == []
    assert plan.gross_before == pytest.approx(0.15) and plan.gross_after == pytest.approx(0.15)
    # control: without the broker figure the quote-based weight (0.10) would add a tranche
    assert [leg.kind for leg in build(policy, {"SPX": 0.15}, pos(1, "SPX500", 10)).legs] == ["open"]


def test_reduce_is_sized_from_snapshot_exposure(policy):
    p = pos(1, "SPX500", 10, exposure=2_000.0)            # 200 per unit per the broker
    (leg,) = build(policy, {"SPX": 0.1}, p).legs
    assert (leg.kind, leg.position_id) == ("partial_close", 1)
    assert leg.units == pytest.approx(5.0)                # 1000 / 200, not 1000 / bid 100
    assert leg.weight_before == pytest.approx(0.2) and leg.weight_after == pytest.approx(0.1)


def test_to_zero_lands_exactly_on_zero_with_snapshot_exposure(policy):
    plan = build(policy, {"SPX": 0.0}, pos(1, "SPX500", 10, exposure=1_234.5))
    (leg,) = plan.legs
    assert leg.weight_before == pytest.approx(0.12345) and leg.weight_after == 0.0
    assert leg.amount_usd == pytest.approx(1_234.5)


def test_unquoted_position_falls_back_to_broker_close_rate_then_open_rate(policy):
    qs = quotes(without=("SPX500",))
    plan = build(policy, {"NDX": 0.1}, pos(1, "SPX500", 10, close_rate=120.0), qs=qs)
    assert plan.gross_before == pytest.approx(0.12)       # 10 × close rate 120
    plan = build(policy, {"NDX": 0.1}, pos(1, "SPX500", 10), qs=qs)
    assert plan.gross_before == pytest.approx(10 * 100.1 / NAV)   # open rate


# ------------------------------------------------------------------------------ target lines only
def _decision(base_w, final_w):
    return RiskDecision(
        raw_levels={}, banded_levels={}, base_w=base_w, proposed_w=final_w, final_w=final_w,
        checks=[], gross=0.0, net=0.0, margin_use=0.0, stop_budget_used=0.0,
        stop_budget_limit=0.0, carry_bps_day=0.0, ex_ante_vol=0.0, basis="council",
    )


def test_changed_targets_keeps_only_changed_lines():
    decision = _decision(
        base_w={"SPX": 0.15, "NDX": 0.10, "EURUSD": -0.05},
        final_w={"SPX": 0.15, "NDX": 0.0, "GOLD": 0.05, "EURUSD": -0.05 + 1e-9},
    )
    assert changed_targets(decision) == {"GOLD": 0.05, "NDX": 0.0}
    assert changed_targets(_decision({"SPX": 0.1}, {})) == {"SPX": 0.0}   # dropped from final = 0


def test_only_changed_lines_are_planned_from_a_risk_decision(policy):
    book = (
        pos(1, "SPX500", 10, exposure=1_500.0),           # held at 0.15
        pos(2, "NSDQ100", 5, exposure=1_000.0),           # cut to 0
        pos(3, "EURUSD", 500, is_buy=False, exposure=550.0),   # held short, off its quote value
    )
    decision = _decision(
        base_w={"SPX": 0.15, "NDX": 0.10, "EURUSD": -0.055},
        final_w={"SPX": 0.15, "NDX": 0.0, "GOLD": 0.05, "EURUSD": -0.055},
    )
    plan = build(policy, changed_targets(decision), *book)
    assert [(leg.kind, leg.line) for leg in plan.legs] == [("close", "NDX"), ("open", "GOLD")]


def test_a_line_absent_from_target_never_gets_a_leg(policy):
    book = (pos(1, "SPX500", 30), pos(2, "NSDQ100", 5), pos(3, "GOLD", 10, is_buy=False))
    assert build(policy, {}, *book).legs == []
    for target in ({"NDX": 0.5}, {"NDX": -0.05}, {"NDX": 0.0}):
        plan = build(policy, target, *book)
        assert plan.legs and {leg.line for leg in plan.legs} == {"NDX"}


def test_every_leg_carries_line_and_whole_units(policy):
    elig = rows(GOLD=eligibility_row("GOLD", 103, units_quantity_type="WholeUnits"))
    plan = build(policy, {"SPX": 0.0, "GOLD": 0.1, "EURUSD": -0.05}, pos(1, "SPX500", 10), eligibility=elig)
    assert {(leg.symbol, leg.line, leg.whole_units) for leg in plan.legs} == {
        ("SPX500", "SPX", False), ("GOLD", "GOLD", True), ("EURUSD", "EURUSD", False),
    }
    close = build(policy, {"GOLD": 0.0}, pos(1, "GOLD", 10), eligibility=elig).legs[0]
    assert close.kind == "close" and close.line == "GOLD" and close.whole_units


def test_unmapped_target_line_is_locked_without_a_second_note(policy):
    stray = Position(position_id=9, instrument_id=999, symbol="UNMAPPED_999", is_buy=True,
                     units=10, open_rate=10.0, amount=100.0, sl_rate=9.0)
    plan = build(policy, {"UNMAPPED_999": 0.0}, stray)
    assert plan.legs == [] and plan.skipped == ["UNMAPPED_999: unmapped_position_locked"]


# ------------------------------------------------------------------------------ flatten
def flatten(policy, *positions, eligibility=None, symbol_for=None, qs=None):
    return build_flatten_plan(
        snapshot=snapshot(*positions), quotes=qs if qs is not None else quotes(),
        eligibility=eligibility or rows(), nav_usd=NAV, policy=policy, symbol_for=symbol_for,
    )


def test_flatten_closes_every_position_uncapped_including_unmapped(policy):
    stray = Position(position_id=99, instrument_id=999, symbol="UNMAPPED_999", is_buy=False,
                     units=10, open_rate=10.0, amount=100.0, sl_rate=11.0, exposure_usd=105.0)
    book = [pos(i, s, 2, age_h=i) for i, s in enumerate(["SPX500"] * 3 + ["NSDQ100"] * 3 + ["GOLD"] * 3, 1)]
    book += [pos(20, "EURUSD", 100), pos(21, "EURUSD", 50, is_buy=False), stray]   # hedged + unmapped
    plan = flatten(policy, *book)
    assert policy.risk["proposal"]["max_legs"] < len(book)
    assert len(plan.legs) == len(book) and plan.skipped == []
    assert {leg.kind for leg in plan.legs} == {"close"}
    assert not any(leg.risk_increasing for leg in plan.legs)
    assert sorted(leg.position_id for leg in plan.legs) == sorted(p.position_id for p in book)
    assert [leg.seq for leg in plan.legs] == list(range(1, len(book) + 1))
    unmapped = next(leg for leg in plan.legs if leg.position_id == 99)
    assert (unmapped.symbol, unmapped.line, unmapped.direction) == ("UNMAPPED_999", "UNMAPPED_999", "short")
    assert unmapped.weight_before == pytest.approx(-0.0105) and unmapped.weight_after == 0.0
    assert plan.gross_after == pytest.approx(0.0) and plan.net_after == pytest.approx(0.0)
    assert plan.gross_before == pytest.approx(sum(
        (p.exposure_usd if p.exposure_usd is not None else p.units * (PRICES[p.symbol][1] if p.is_buy else PRICES[p.symbol][2]))
        for p in book
    ) / NAV)


def test_flatten_orders_largest_reduction_first_and_uses_snapshot_exposure(policy):
    plan = flatten(policy, pos(1, "SPX500", 1, exposure=50.0), pos(2, "GOLD", 1, exposure=900.0))
    assert [leg.symbol for leg in plan.legs] == ["GOLD", "SPX500"]
    assert plan.legs[0].weight_before == pytest.approx(0.09)


def test_flatten_resolves_unmapped_symbols_with_symbol_for(policy):
    known_late = Position(position_id=7, instrument_id=103, symbol="UNMAPPED_103", is_buy=True,
                          units=4, open_rate=50.0, amount=200.0, sl_rate=45.0)
    plan = flatten(policy, known_late, symbol_for={103: "GOLD"})
    (leg,) = plan.legs
    assert (leg.symbol, leg.line, leg.instrument_id, leg.position_id) == ("GOLD", "GOLD", 103, 7)
    assert leg.amount_usd == pytest.approx(4 * 50.0)      # GOLD bid
    plan = flatten(policy, known_late, symbol_for=lambda iid: None)
    assert plan.legs[0].line == "UNMAPPED_103"


def test_flatten_plans_a_close_even_when_eligibility_forbids_it(policy):
    elig = rows(SPX500=eligibility_row("SPX500", 101, allow_close=False))
    (leg,) = flatten(policy, pos(1, "SPX500", 3), eligibility=elig).legs
    assert leg.kind == "close" and "close not allowed" in leg.reason


def test_flatten_of_an_empty_book_is_empty(policy):
    plan = flatten(policy)
    assert plan.legs == [] and plan.gross_before == 0.0 and plan.gross_after == 0.0
    with pytest.raises(ValueError):
        build_flatten_plan(snapshot=snapshot(), quotes={}, eligibility={}, nav_usd=0.0, policy=policy)
