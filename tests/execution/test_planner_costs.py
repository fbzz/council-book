"""WP-E planner: the private fixed fee per leg (each tranche close pays its own), the real-dollar
floor (`below_real_minimum`), the stock gap guard (D20), leg origins and the counted leg cap, and no
fee in the public legs. Fake eligibility rows and quotes only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from council.broker.eligibility import VehicleChoice, parse_eligibility, select_config
from council.broker.fake import eligibility_row, leverage_config
from council.execution.planner import build_plan
from council.models.broker import ExposureSnapshot, Position, Quote
from council.publish.redact import LineMap
from council.publish.redact import _plan as public_plan
from council.risk.costs import trade_economics

NOW = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
NAV = 10_000.0
FEE = 6.0
REAL = {"CSPX.L": (301, 600.0, 600.3), "SGLN.L": (302, 40.0, 40.02), "TSTA": (303, 100.0, 100.05),
        "BTC": (304, 60_000.0, 60_030.0)}
CFD = {"SPX500": (101, 100.0, 100.1), "EURUSD": (104, 1.1, 1.1002)}
STOPS = {"SPX": 0.08, "GOLD": 0.08, "TSTA": 0.15, "BTC": 0.2, "EURUSD": 0.04}


def rows():
    raw = []
    for symbol, (iid, _b, _a) in REAL.items():
        raw.append(eligibility_row(symbol, iid, min_position_exposure=1.0, configs=[
            leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,), min_position_amount=1.0)]))
    for symbol, (iid, _b, _a) in CFD.items():
        raw.append(eligibility_row(symbol, iid, min_position_exposure=1.0, configs=[
            leverage_config(direction="LONG", min_position_amount=1.0),
            leverage_config(direction="SHORT", min_position_amount=1.0)]))
    return {r.symbol: r for r in parse_eligibility({"eligibilities": raw}, NOW)}


def quotes():
    return {s: Quote(symbol=s, instrument_id=i, bid=b, ask=a, at=NOW) for s, (i, b, a) in {**REAL, **CFD}.items()}


VEHICLE = {"SPX": "CSPX.L", "GOLD": "SGLN.L", "TSTA": "TSTA", "BTC": "BTC", "EURUSD": "EURUSD"}
SETTLEMENT = {**{s: "real" for s in REAL}, **{s: "cfd" for s in CFD}}


def pos(pid, symbol, units, *, age_h=0):
    iid, bid, ask = {**REAL, **CFD}[symbol]
    return Position(position_id=pid, instrument_id=iid, symbol=symbol, is_buy=True, leverage=1, units=units,
                    open_rate=ask, amount=units * ask, sl_rate=1.0, opened_at=NOW - timedelta(hours=age_h),
                    settlement=SETTLEMENT[symbol])


def snapshot(*positions):
    return ExposureSnapshot(taken_at=NOW, equity_usd=NAV, credit_usd=NAV, positions=list(positions),
                            signed_w={}, gross=0.0, net=0.0, margin_use=0.0)


def cost_bps(line, vehicle, direction, leverage):
    """The cycle's shape: (per side, carry, fee); the fee only on real non-crypto vehicles."""
    real = SETTLEMENT.get(vehicle) == "real"
    return (15.0, 0.0, FEE if real and line != "BTC" else 0.0)


def build(policy, target, *positions, **kw):
    elig = rows()

    def vehicle_for(line, direction, lev):
        symbol = VEHICLE[line] if direction == "long" else "EURUSD"
        row = elig[symbol]
        config = select_config(row, direction, lev, settlement=SETTLEMENT[symbol])
        return VehicleChoice(symbol=symbol, instrument_id=row.instrument_id, settlement=config.settlement,
                             leverage=lev, config=config)

    kw.setdefault("economics", trade_economics(policy, virtual_nav_usd=NAV, mirror_ratio=0.2))
    return build_plan(snapshot=snapshot(*positions), target_w=target, vehicle_for=vehicle_for, quotes=quotes(),
                      stop_distance=STOPS, leverage_for={}, eligibility=elig, cost_bps=cost_bps, nav_usd=NAV,
                      policy=policy, **kw)


def test_real_ucits_and_stock_legs_carry_the_fee_privately(sleeve_policy):
    plan = build(sleeve_policy, {"SPX": 0.05, "TSTA": 0.05, "BTC": 0.05, "EURUSD": 0.05})
    by_line = {leg.line: leg for leg in plan.legs}
    assert by_line["SPX"].fee_bps_nav == FEE and by_line["TSTA"].fee_bps_nav == FEE
    assert by_line["BTC"].fee_bps_nav == 0.0 and by_line["EURUSD"].fee_bps_nav == 0.0
    drag = 1 / 2_000 - 1 / 10_000
    assert by_line["SPX"].fee_drag == pytest.approx(drag) and by_line["EURUSD"].fee_drag == 0.0
    variable = sum(leg.cost_bps_nav for leg in plan.legs)
    assert plan.cost_bps_nav == pytest.approx(variable)                  # variable only
    assert plan.fee_bps_nav == pytest.approx(2 * FEE) and plan.fixed_fee_legs == 2
    assert by_line["SPX"].cost_bps_nav == pytest.approx(15.0 * by_line["SPX"].weight_after, rel=1e-3)


def test_each_tranche_close_pays_its_own_fee(sleeve_policy):
    plan = build(sleeve_policy, {"SPX": 0.0}, pos(1, "CSPX.L", 1.0, age_h=2), pos(2, "CSPX.L", 1.0, age_h=1))
    assert [leg.kind for leg in plan.legs] == ["close", "close"]
    assert [leg.fee_bps_nav for leg in plan.legs] == [FEE, FEE] and plan.fee_bps_nav == 2 * FEE


def test_below_real_minimum_is_skipped_but_full_closes_are_not(sleeve_policy):
    # mirror 0.2: $3 real = a $15 virtual order; 0.0012 NAV = $12
    plan = build(sleeve_policy, {"GOLD": 0.0012})
    assert not plan.legs and "GOLD: below_real_minimum" in plan.skipped
    assert build(sleeve_policy, {"GOLD": 0.002}).legs                          # $20: planned
    # a partial close of $12 is skipped; a full close of a $12 position still goes
    held = pos(1, "SGLN.L", 1.0)                                               # $40
    partial = build(sleeve_policy, {"GOLD": 0.0028}, held)
    assert not partial.legs and "GOLD: below_real_minimum" in partial.skipped
    tiny = pos(2, "SGLN.L", 0.3)                                               # $12
    full = build(sleeve_policy, {"GOLD": 0.0}, tiny)
    assert [leg.kind for leg in full.legs] == ["close"]
    assert not build(sleeve_policy, {"GOLD": 0.0012}, economics=None).skipped  # no economics: no check


def test_gap_guard_skips_a_stock_open_far_from_the_last_close(sleeve_policy):
    sigma_d = 0.02
    calm = build(sleeve_policy, {"TSTA": 0.05}, gap_ref={"TSTA": (99.0, sigma_d)})     # ln(100.05/99) = 1.1%
    assert [leg.line for leg in calm.legs] == ["TSTA"]
    gapped = build(sleeve_policy, {"TSTA": 0.05}, gap_ref={"TSTA": (94.0, sigma_d)})   # 6.2% > 2.5 x 2%
    assert not gapped.legs and "TSTA: gap_guard" in gapped.skipped
    unknown = build(sleeve_policy, {"TSTA": 0.05}, gap_ref={"TSTA": None})
    assert not unknown.legs and "TSTA: gap_guard_no_reference" in unknown.skipped
    # closes are never gap-guarded
    close = build(sleeve_policy, {"TSTA": 0.0}, pos(1, "TSTA", 5.0), gap_ref={"TSTA": (50.0, sigma_d)})
    assert [leg.kind for leg in close.legs] == ["close"]


def test_origin_and_reference_level_are_stamped(sleeve_policy):
    plan = build(sleeve_policy, {"SPX": 0.05, "EURUSD": 0.05}, origin={"SPX": "reference", "EURUSD": "discretionary"},
                 ref_levels={"SPX": 1.0, "EURUSD": 0.0})
    by_line = {leg.line: leg for leg in plan.legs}
    assert by_line["SPX"].origin == "reference" and by_line["SPX"].ref_level == 1.0
    assert by_line["EURUSD"].origin == "discretionary"
    assert build(sleeve_policy, {"SPX": 0.05}).legs[0].origin is None       # no origin given: legacy


def test_risk_reducing_reference_legs_do_not_count_toward_the_leg_cap(sleeve_policy):
    positions = [pos(i, "CSPX.L", 0.5, age_h=i) for i in range(1, 11)]   # 10 tranches on SPX ($300 each)
    reference = build(sleeve_policy, {"SPX": 0.0, "GOLD": 0.05}, *positions, origin={"SPX": "reference"})
    assert len(reference.legs) == 11 and not any(s.endswith("leg_cap") for s in reference.skipped)
    counted = build(sleeve_policy, {"SPX": 0.0, "GOLD": 0.05}, *positions, origin={"SPX": "discretionary"})
    assert len(counted.legs) == 8 and "SPX: leg_cap" in counted.skipped
    many = [pos(i, "CSPX.L", 0.5, age_h=i) for i in range(1, 18)]         # 17 closes: the total cap binds
    capped = build(sleeve_policy, {"SPX": 0.0}, *many, origin={"SPX": "reference"})
    assert len(capped.legs) == 16 and "SPX: leg_cap" in capped.skipped


def test_no_fee_in_public_legs(sleeve_policy):
    plan = build(sleeve_policy, {"SPX": 0.05, "TSTA": 0.05, "EURUSD": 0.05})
    assert plan.fee_bps_nav > 0
    public = public_plan(plan, LineMap(sleeve_policy.universe))
    for pub, leg in zip(public.legs, plan.legs, strict=True):
        assert pub.cost_bp == pytest.approx(leg.cost_bps_nav, abs=0.05)       # variable cost only
        assert pub.cost_bp < leg.cost_bps_nav + leg.fee_bps_nav - 1.0 or leg.fee_bps_nav == 0
    assert public.cost_bp_total == pytest.approx(plan.cost_bps_nav, abs=0.05)
    dumped = public.model_dump_json()
    assert "fee" not in dumped and "origin" not in dumped and "ref_level" not in dumped
