"""WP-E: held reference levels from fills (stop -> 0, flatten -> 0, migration from the current book),
leg origins in the ledger (R13/R14 count discretionary legs only; legacy legs count) and the real
account's fee drag for the kill switch. Synthetic ledger rows only."""

from __future__ import annotations

from datetime import timedelta

import pytest

from council.models.plan import Leg
from council.policy import Policy
from council.risk.held_levels import (
    MIGRATION_KEY,
    ledger_held_levels,
    migrated_levels,
    resolve_held_levels,
)


def _leg(seq, kind, symbol, before, after, *, origin=None, ref_level=None, cost=0.0, fee=0.0, drag=0.0):
    return Leg(
        seq=seq, kind=kind, symbol=symbol, direction="long", weight_before=before, weight_after=after,
        risk_increasing=kind == "open", cost_bps_nav=cost, units=10.0, instrument_id=1,
        position_id=1 if kind != "open" else None, origin=origin, ref_level=ref_level,
        fee_bps_nav=fee, fee_drag=drag,
    )


def _decision(ledger, fclock, decision_id, legs, *, kind="rebalance"):
    ledger.create_decision(decision_id=decision_id, kind=kind, valid_until=fclock.now() + timedelta(hours=4))
    ledger.transition(decision_id, "approved", "ok", actor="operator")
    ledger.transition(decision_id, "executing", "go")
    ledger.insert_legs(decision_id, legs)


def _fill(ledger, fclock, decision_id, seq, state="filled", **detail):
    ledger.update_leg(decision_id, seq, state="submitting", request_id=f"{decision_id}-{seq}")
    ledger.update_leg(decision_id, seq, state=state, resolved_at=fclock.now(), detail=detail)


def test_held_levels_come_from_reference_fills(ledger, fclock):
    _decision(ledger, fclock, "d1", [
        _leg(1, "open", "SPX500", 0.0, 0.15, origin="reference", ref_level=1.0),
        _leg(2, "open", "NSDQ100", 0.0, 0.2, origin="discretionary", ref_level=1.0),
        _leg(3, "open", "GOLD", 0.0, 0.1, ref_level=0.75),             # legacy: no origin
        _leg(4, "open", "EURUSD", 0.0, 0.1, origin="reference", ref_level=0.5),
    ])
    for seq in (1, 2, 3):
        fclock.advance(60)
        _fill(ledger, fclock, "d1", seq)
    ledger.update_leg("d1", 4, state="skipped")                         # never sent: no held level
    fills = ledger.reference_fills()
    assert set(fills) == {"SPX"} and fills["SPX"][0] == 1.0
    fclock.advance(3600)
    _decision(ledger, fclock, "d2", [_leg(1, "partial_close", "SPX500", 0.15, 0.11,
                                          origin="reference", ref_level=0.75)])
    _fill(ledger, fclock, "d2", 1, state="partially_filled", units_sent=10.0, units_filled=4.0)
    assert ledger.reference_fills()["SPX"][0] == 0.75                  # the latest reference fill wins


def test_a_stop_hit_or_a_flatten_resets_the_held_level_to_zero(ledger, fclock):
    _decision(ledger, fclock, "d1", [
        _leg(1, "open", "SPX500", 0.0, 0.15, origin="reference", ref_level=1.0),
        _leg(2, "open", "NSDQ100", 0.0, 0.2, origin="reference", ref_level=1.0),
    ])
    _fill(ledger, fclock, "d1", 1)
    _fill(ledger, fclock, "d1", 2)
    fclock.advance(3600)
    ledger.record_stop_hit(line="SPX", symbol="SPX500", position_id=1, at=fclock.now())
    fclock.advance(60)
    _decision(ledger, fclock, "f1", [_leg(1, "close", "NSDQ100", 0.2, 0.0)], kind="flatten")
    _fill(ledger, fclock, "f1", 1)
    fills, resets = ledger.reference_fills(), ledger.level_resets()
    held = resolve_held_levels(["SPX", "NDX", "GOLD"], fills=fills, resets=resets, migrated={"GOLD": 0.75})
    assert held == {"SPX": 0.0, "NDX": 0.0, "GOLD": 0.75}
    fclock.advance(4 * 86400)                                          # after the R4d cool-off: re-bought
    _decision(ledger, fclock, "d3", [_leg(1, "open", "SPX500", 0.0, 0.15, origin="reference", ref_level=1.0)])
    _fill(ledger, fclock, "d3", 1)
    held = resolve_held_levels(["SPX"], fills=ledger.reference_fills(), resets=ledger.level_resets(), migrated={})
    assert held == {"SPX": 1.0}


def test_migration_from_current_weights_is_stored_once(ledger, fclock, policy: Policy):
    units = {ln.symbol: ln.base_weight for ln in policy.universe.lines}
    current = {"NDX": 0.35 * 0.97, "SEMIS": 0.75 * 0.15, "OIL": 0.05}      # OIL: council-only line
    levels = migrated_levels(policy.universe.lines, current, units)
    assert levels["NDX"] == 1.0 and levels["SEMIS"] == 0.75 and levels["OIL"] == 0.0
    assert levels["GOLD"] == 0.0
    held = ledger_held_levels(ledger, policy.universe.lines, current_w=current, units=units,
                              now=fclock.now(), persist=True)
    assert held["NDX"] == 1.0 and held["SEMIS"] == 0.75
    stored = ledger.get_runtime(MIGRATION_KEY)["levels"]
    assert stored["NDX"] == 1.0
    # a later unit change (the vol cap) is not a level change: the stored level is kept
    shrunk = {**units, "NDX": 0.25}
    again = ledger_held_levels(ledger, policy.universe.lines, current_w=current, units=shrunk,
                               now=fclock.now(), persist=True)
    assert again["NDX"] == 1.0
    # without a broker snapshot nothing is stored and the book is flat
    fresh_ledger_levels = ledger_held_levels(ledger, policy.universe.lines, current_w=None, units=units,
                                             now=fclock.now(), persist=False)
    assert fresh_ledger_levels["NDX"] == 1.0                           # stored migration still applies


def test_discretionary_origin_filters_for_r13_and_r14(ledger, fclock):
    t0 = fclock.now()
    _decision(ledger, fclock, "d1", [
        _leg(1, "open", "SPX500", 0.0, 0.1, origin="reference", ref_level=1.0, cost=1.0, fee=6.0),
        _leg(2, "open", "GOLD", 0.0, 0.2, origin="discretionary", cost=2.0, fee=6.0),
        _leg(3, "open", "NSDQ100", 0.0, 0.3, cost=3.0),                # legacy leg: discretionary
    ])
    for seq in (1, 2, 3):
        _fill(ledger, fclock, "d1", seq)
    assert ledger.turnover_since(t0) == pytest.approx(0.6)
    assert ledger.turnover_since(t0, origins=("discretionary",)) == pytest.approx(0.5)
    assert ledger.turnover_since(t0, origins=("reference",)) == pytest.approx(0.1)
    assert ledger.cost_bps_since(t0, origins=("discretionary",)) == pytest.approx(2.0 + 6.0 + 3.0)
    assert ledger.cost_bps_since(t0, origins=("discretionary",), include_fee=False) == pytest.approx(5.0)
    assert ledger.fee_bps_since(t0, origins=("discretionary",)) == pytest.approx(6.0)
    assert ledger.cost_bps_since(t0) == pytest.approx(1.0 + 6.0 + 2.0 + 6.0 + 3.0)


def test_real_fee_drag_accumulates_over_filled_legs(ledger, fclock):
    _decision(ledger, fclock, "d1", [
        _leg(1, "open", "SPX500", 0.0, 0.1, fee=6.0, drag=0.0004),
        _leg(2, "open", "GOLD", 0.0, 0.1, fee=6.0, drag=0.0004),
        _leg(3, "open", "EURUSD", 0.0, 0.1),
    ])
    assert ledger.real_fee_drag() == 0.0
    _fill(ledger, fclock, "d1", 1)
    _fill(ledger, fclock, "d1", 2, state="partially_filled", units_sent=10.0, units_filled=5.0)
    _fill(ledger, fclock, "d1", 3)
    assert ledger.real_fee_drag() == pytest.approx(1 - (1 - 0.0004) * (1 - 0.0002))


def test_leg_origin_and_fees_are_stored_privately(ledger, fclock):
    _decision(ledger, fclock, "d1", [_leg(1, "open", "SPX500", 0.0, 0.1, origin="reference",
                                          ref_level=0.75, fee=6.0, drag=0.0004)])
    row = ledger.legs("d1")[0]
    assert row.detail["origin"] == "reference" and row.detail["ref_level"] == 0.75
    assert row.detail["fee_bps_nav"] == 6.0 and row.detail["fee_drag"] == 0.0004
