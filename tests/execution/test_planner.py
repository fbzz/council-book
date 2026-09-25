"""Planner cases: new, add tranche, 4th-tranche skip, partial, full, flip, minimums, leg cap, stops."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from council.broker.eligibility import parse_eligibility, resolve_vehicle
from council.broker.fake import eligibility_row, leverage_config
from council.execution.planner import _Draft, _order_and_cap, build_plan, vehicle_to_line
from council.models.broker import ExposureSnapshot, Position, Quote

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


def quotes():
    return {s: Quote(symbol=s, instrument_id=i, bid=b, ask=a, at=NOW) for s, (i, b, a) in PRICES.items()}


def pos(pid, symbol, units, *, is_buy=True, age_h=0, leverage=1, sl=1.0):
    iid, bid, ask = PRICES[symbol]
    return Position(
        position_id=pid, instrument_id=iid, symbol=symbol, is_buy=is_buy, leverage=leverage,
        units=units, open_rate=ask if is_buy else bid, amount=units * ask / leverage,
        sl_rate=sl, opened_at=NOW - timedelta(hours=age_h),
    )


def snapshot(*positions):
    return ExposureSnapshot(
        taken_at=NOW, equity_usd=NAV, credit_usd=NAV, positions=list(positions), signed_w={},
        gross=0.0, net=0.0, margin_use=0.0,
    )


def build(policy, target, *positions, eligibility=None, leverage=None, stops=None, **kw):
    elig = eligibility or rows()
    lines = policy.universe.by_symbol()

    def vehicle_for(line, direction, lev):
        return resolve_vehicle(lines[line], direction, lev, elig, lambda v, r, c: 5.0)

    return build_plan(
        snapshot=snapshot(*positions), target_w=target, vehicle_for=vehicle_for, quotes=quotes(),
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


def test_min_stop_bound_applies_with_buffer(policy):
    elig = rows(SPX500=eligibility_row("SPX500", 101, configs=[leverage_config(min_sl_pct=7.6)]))
    assert "SPX: stop_outside_broker_bounds" in build(policy, {"SPX": 0.1}, eligibility=elig).skipped


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
