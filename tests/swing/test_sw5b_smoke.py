"""SW-5b: the swing capability gates and their Track S smoke steps (S7 real stock long with SL + TP in
the body, S7t the PATCH fallback, S7p / S7x; S8 a 1x stock CFD short with SL + TP held overnight,
S8x). Every new capability defaults to not proven; the steps go through the same operator approval
as S1-S6 (the real guard function, every re-check)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from council.broker.fake import eligibility_row, leverage_config
from council.operator import capabilities
from council.operator import smoke as sm
from council.publish.smoke_row import is_smoke_id
from tests.operator.test_smoke import World

pytestmark = pytest.mark.capability_gates

US_OPEN = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)          # Thursday, the US session is open
SWING_CAPS = ("stock_real_long", "tp_on_open", "tp_min_pct", "stock_cfd_short", "cfd_short_mirror",
              "stock_short_carry", "closed_trade_route")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    w = World(tmp_path, start=US_OPEN)
    inst = w.fake.instrument("AAPL")
    inst.row = eligibility_row("AAPL", inst.instrument_id, currency="USD", configs=[
        leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,), min_tp_pct=2.0),
        leverage_config(settlement="CFD", direction="SHORT", leverage_values=(1,), min_tp_pct=2.0)])
    # AAPL is no line of the core universe: the post-execution reconcile would call the smoke
    # position "unknown" (a pre-existing S7 limitation, reported to the executor package). A swing
    # trade row on the instrument maps it through the runtime swing map, as a live swing book would.
    w.ledger.create_swing_trade("trade:smoke_map", ticker="AAPL", side="long", instrument_id=inst.instrument_id)
    from council.broker.instruments import InstrumentMap

    path = w.state / "instruments.json"
    InstrumentMap.load(path).merged({"AAPL": inst.instrument_id}, US_OPEN).save()   # resolved, as SW-1 saves it
    return w


def _attest(world, decision_id: str) -> None:
    for item in capabilities.MIRROR_ITEMS:
        capabilities.write_mirror_check(item, decision_id=decision_id, state_dir=world.state,
                                        assert_operator=lambda: None, assert_release=lambda: None)


def test_every_swing_capability_is_listed_and_off_by_default(tmp_path):
    caps = capabilities.load(tmp_path)
    for cap in SWING_CAPS:
        assert cap in capabilities.SMOKE_STEPS and cap in capabilities.CAPABILITIES
        assert not caps.has(cap)
        assert f"capability_missing:{cap}" in caps.flags()
    # the design's tp_on_open_or_patch keeps the name SW-5a's planner checks
    assert "tp_on_open" in capabilities.SMOKE_STEPS and "tp_on_open_or_patch" not in capabilities.CAPABILITIES
    assert capabilities.SMOKE_STEPS["stock_cfd_short"] == ("S8",)
    for code in ("S7", "S7t", "S7p", "S7x", "S8", "S8x"):
        assert code in sm.STEPS and is_smoke_id(sm.smoke_id(US_OPEN, code))


def test_s7_real_long_with_the_take_profit_in_the_body_proves_the_long_and_tp_gates(world):
    s7 = world.run_step("S7")
    legs = world.ledger.legs(s7)
    assert [r.kind for r in legs] == ["open"]
    (pos,) = world.fake.positions.values()
    assert pos.tp_rate and pos.sl_rate and pos.settlement == "real" and pos.is_buy
    data = capabilities.read_file(world.state)["capabilities"]
    assert {c for c in data if data[c]["step"] == "S7"} == {"stock_fractional", "stock_real_long", "tp_on_open",
                                                          "tp_min_pct"}
    _attest(world, s7)
    caps = capabilities.load(world.state)
    assert caps.has("stock_real_long") and caps.has("tp_on_open")
    world.run_step("S7p")
    s7x = world.run_step("S7x")
    assert not world.fake.positions
    # the closed-trade READ route is not modelled: closed_trade_route stays unproven (reported)
    assert "closed_trade_route" not in capabilities.read_file(world.state)["capabilities"]
    assert any("closed_trade_route: not seen" in line for line in world.out)
    assert world.ledger.get_decision(s7x).state == "completed"


def test_s7_falls_back_to_the_patch_when_the_body_drops_the_take_profit(world):
    world.fake.tp_on_open_supported = False
    world.run_step("S7")
    data = capabilities.read_file(world.state)["capabilities"]
    assert "stock_real_long" in data and "tp_on_open" not in data and "tp_min_pct" not in data
    assert any("tp_on_open_not_kept" in line for line in world.out)
    s7t = world.run_step("S7t")
    (leg,) = world.ledger.legs(s7t)
    assert leg.kind == "set_tp" and leg.state == "filled"
    (pos,) = world.fake.positions.values()
    assert pos.tp_rate and pos.sl_rate                       # the stop was resent with the take-profit
    assert capabilities.read_file(world.state)["capabilities"]["tp_min_pct"]["step"] == "S7t"


def test_s8_short_is_held_overnight_before_s8x(world):
    s8 = world.run_step("S8")
    (pos,) = world.fake.positions.values()
    assert not pos.is_buy and pos.settlement == "cfd" and pos.leverage == 1 and pos.tp_rate and pos.sl_rate
    data = capabilities.read_file(world.state)["capabilities"]
    assert data["stock_cfd_short"]["step"] == "S8" and data["cfd_short_mirror"]["step"] == "S8"
    with pytest.raises(sm.SmokeRefused, match="held_overnight_missing"):
        sm.propose("S8x", world.deps())
    world.clock.advance(timedelta(days=1).total_seconds())            # Friday, the US session again
    world.run_step("S8x")
    assert not world.fake.positions
    data = capabilities.read_file(world.state)["capabilities"]
    assert "stock_short_carry" not in data and "closed_trade_route" not in data   # no record: unproven
    _attest(world, s8)
    assert capabilities.load(world.state).has("stock_cfd_short")
