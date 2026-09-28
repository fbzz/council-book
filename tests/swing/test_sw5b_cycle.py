"""SW-5b: the cycle's swing stage (swing-book.md rev 2, §1.1, §1.8, §4.3, §4.4): time-stop exits made
by the cycle, exit_pending never stranded, a rejected / expired entry -> missed, an idea rewritten in a
later cycle carries that cycle, swing legs priced by swing/costs.py (never zero), fail-closed rules."""

# ruff: noqa: F811 - the execution fixtures are imported by name and requested as parameters

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from council import invariants
from council.cycle import _swing_leg_cost, record_swing_decision, run_swing, settle_swing
from council.models.plan import Leg
from council.swing import rules as R
from tests.execution.conftest import fake, fclock, ledger, read_client  # noqa: F401
from tests.execution.helpers import NAV, plan_of
from tests.swing import stubs as s
from tests.swing.test_sw5b_end_to_end import CYCLE, snapshot, swing_ctx, world  # noqa: F401

NVDA_ID = 201


def open_trade(ledger, tid: str = "trade:t0", *, time_stop: str = "2026-10-01", pid: int | None = None):
    ledger.create_swing_trade(tid, ticker="NVDA", side="long", instrument_id=NVDA_ID, sl_rate=94.0, tp_rate=110.0,
                              time_stop_date=time_stop, origin_cycle="2026-09-24T1840Z",
                              detail={"size_nav": 0.08, "stop_pct": 0.06})
    ledger.transition_swing_trade(tid, "entry_executing")
    ledger.transition_swing_trade(tid, "open")
    ledger.update_swing_trade(tid, open_rate=100.0, units=8.0, position_ids=[pid] if pid else [])


def exit_leg(seq: int = 1, tid: str = "trade:t0", pid: int = 9) -> Leg:
    return Leg(seq=seq, kind="close", symbol="NVDA", line="SW_NVDA", instrument_id=NVDA_ID, direction="long",
               settlement="real", weight_before=0.08, weight_after=0.0, risk_increasing=False, reason="exit",
               units=8.0, amount_usd=800.0, position_id=pid, sleeve="swing", swing_trade_id=tid)


def decide(ledger, fclock, *legs: Leg, state: str | None = None) -> str:
    plan = plan_of(*legs)
    did = f"{CYCLE}-rebalance-x{len(ledger.decisions())}"
    ledger.create_decision(decision_id=did, kind="rebalance", valid_until=fclock.now() + timedelta(hours=1),
                           cycle_id=CYCLE, target={"base_w": {}}, plan=plan, now=fclock.now())
    ledger.insert_legs(did, plan.legs)
    if state:
        ledger.transition(did, state, "test")
    return did


def quiet_ctx(ctx):
    """The same swing context with a Scout that proposes nothing (no idea, only the book)."""
    from council.llm.stub import StubGateway

    ctx.gateway = StubGateway(responses={"scout": s.scout()}, model="deepseek-v4.1-flash:cloud")
    return ctx


# ------------------------------------------------------------------------------ exits
def test_the_time_stop_exit_is_created_by_the_cycle(world, fake, fclock, ledger, policy, read_client, monkeypatch):
    monkeypatch.setattr(invariants, "SWING_BOOK_LIVE", True)
    open_trade(ledger)                                              # time stop today
    slot = fclock.now()
    ctx = quiet_ctx(swing_ctx(ledger, policy, read_client, world, slot))
    out = asyncio.run(run_swing(ctx, SimpleNamespace(cycle_id=CYCLE), snapshot=snapshot(read_client, ledger, policy,
                                                                                         world, slot),
                                kill_state="NORMAL", nav=SimpleNamespace(drawdown=0.0), slot=slot, now=slot))
    assert out.exits == {"trade:t0": "time"}
    (order,) = [o for o in out.orders if o.action == "exit"]
    assert order.trade_id == "trade:t0" and order.line == "SW_NVDA"
    (line,) = out.lines
    assert line.action == "exit" and line.pinned_w == 0.0
    # the planned exit leg moves the trade to exit_pending (with the cycle id on the event)
    did = decide(ledger, fclock, exit_leg())
    assert record_swing_decision(ledger, out, plan_of(exit_leg()), did, CYCLE, fclock.now()) == []
    t = ledger.swing_trade("trade:t0")
    assert t.state == "exit_pending" and t.detail["exit_kind"] == "time" and t.detail["exit_decision"] == did
    ev = [e for e in ledger.swing_events("trade:t0") if e["to_state"] == "exit_pending"]
    assert ev and ev[-1]["origin_cycle"] == CYCLE


@pytest.mark.parametrize("final", ["rejected", "expired"])
def test_a_rejected_or_expired_exit_returns_the_trade_to_open(ledger, fclock, final):
    open_trade(ledger)
    did = decide(ledger, fclock, exit_leg())
    ledger.update_swing_trade("trade:t0", detail={"exit_decision": did, "exit_kind": "time", "pre_exit_state": "open"})
    ledger.transition_swing_trade("trade:t0", "exit_pending", cycle_id=CYCLE)
    if final == "rejected":
        ledger.transition(did, "rejected", "operator", actor="operator")
    else:
        ledger.expire_stale(fclock.now() + timedelta(hours=2))
    assert settle_swing(ledger, fclock.now()) == []
    assert ledger.swing_trade("trade:t0").state == "open"          # never stranded in exit_pending


def test_a_filled_exit_closes_the_trade_with_its_outcome(ledger, fclock):
    open_trade(ledger)
    did = decide(ledger, fclock, exit_leg())
    ledger.update_swing_trade("trade:t0", detail={"exit_decision": did, "exit_kind": "time", "pre_exit_state": "open"})
    ledger.transition_swing_trade("trade:t0", "exit_pending", cycle_id=CYCLE)
    ledger.update_leg(did, 1, state="submitting", request_id="r1")
    ledger.update_leg(did, 1, state="submitted")
    ledger.update_leg(did, 1, state="filled", detail={"fill_price": 104.0})
    settle_swing(ledger, fclock.now())
    t = ledger.swing_trade("trade:t0")
    assert t.state == "closed_time" and t.close_rate == pytest.approx(104.0)
    assert set(t.detail) >= {"r_declared", "net_ret", "size_nav", "beta", "sector_etf_ret", "exit_kind", "days_held"}
    assert t.detail["net_ret"] == pytest.approx(0.04 - 0.025)


@pytest.mark.parametrize("final", ["rejected", "expired"])
def test_a_rejected_or_expired_entry_moves_its_trade_to_missed(ledger, fclock, final):
    entry = Leg(seq=1, kind="open", symbol="NVDA", line="SW_NVDA", instrument_id=NVDA_ID, direction="long",
                settlement="real", weight_before=0.0, weight_after=0.08, risk_increasing=True, reason="e",
                units=8.0, amount_usd=800.0, sl_rate=94.0, tp_rate=110.0, tp_mode="body", sleeve="swing",
                swing_trade_id="trade:e1")
    did = decide(ledger, fclock, entry)
    ledger.add_swing_idea("idea:e1", origin_cycle=CYCLE, ticker="NVDA", side="long", status="proposed")
    ledger.create_swing_trade("trade:e1", ticker="NVDA", side="long", idea_id="idea:e1", origin_cycle=CYCLE,
                              decision_id=did, entry_seq=1)
    if final == "rejected":
        ledger.transition(did, "rejected", "operator", actor="operator")
    else:
        ledger.expire_stale(fclock.now() + timedelta(hours=2))
    settle_swing(ledger, fclock.now())
    assert ledger.swing_trade("trade:e1").state == "missed"
    assert ledger.swing_idea("idea:e1")["status"] == "pending"


def test_an_idea_rewritten_in_a_later_cycle_carries_that_cycle(world, fake, fclock, ledger, policy, read_client):
    slot = fclock.now()
    ctx = swing_ctx(ledger, policy, read_client, world, slot)
    asyncio.run(run_swing(ctx, SimpleNamespace(cycle_id=CYCLE), snapshot=snapshot(read_client, ledger, policy, world, slot),
                          kill_state="NORMAL", nav=SimpleNamespace(drawdown=0.0), slot=slot, now=slot))
    (idea,) = ledger.swing_ideas()
    ledger.update_swing_idea(idea["idea_id"], status="pending")          # a missed entry returns to pending
    later, cycle2 = slot + timedelta(days=1), "2026-10-02T1440Z"
    fclock.advance(timedelta(days=1).total_seconds())
    ctx = swing_ctx(ledger, policy, read_client, world, later)
    out = asyncio.run(run_swing(ctx, SimpleNamespace(cycle_id=cycle2), snapshot=snapshot(read_client, ledger, policy,
                                                                                          world, later),
                                kill_state="NORMAL", nav=SimpleNamespace(drawdown=0.0), slot=later, now=later))
    (again,) = ledger.swing_ideas()
    assert again["idea_id"] == idea["idea_id"] and cycle2 in again["carry_cycles"]
    assert len(ledger.paper_trades()) == 2 and not [f for f in out.flags if f.startswith("paper_track_error")]


# ------------------------------------------------------------------------------ costs, fail closed
def test_swing_legs_are_priced_by_swing_costs_never_zero(policy):
    from council.swing.costs import CostConfig

    cfg = CostConfig.from_policy(policy)
    cost = _swing_leg_cost(policy, 3.0)
    assert cost("long") == (cfg.spread_floor_bps, 0.0, 3.0) and cfg.spread_floor_bps > 0
    per_side, carry, fee = cost("short")
    assert per_side > 0 and carry == pytest.approx(cfg.short_carry_bps_day_floor) and carry > 0 and fee == 0.0


def test_an_unknown_atr_drops_the_entry_and_an_unknown_drawdown_blocks_it(policy):
    from tests.swing.test_rules import NOW, TODAY, book, cand, flat_cost

    sp = policy.swing
    assert R.screen_entry(cand(atr_pct=None), book(), sp, flat_cost()).code == "atr_unknown"
    assert R.public_code("atr_unknown") == "S5:atr_unknown"
    unknown = R.BookState(today=TODAY, now=NOW)                         # drawdown_from_peak=None
    assert R.screen_entry(cand(), unknown, sp, flat_cost()).code == "drawdown_unknown"
    assert R.public_code("drawdown_unknown") == "S17:drawdown_unknown"
    assert R.screen_entry(cand(), replace(unknown, drawdown_from_peak=float("nan")), sp, flat_cost()).code \
        == "drawdown_unknown"
    assert R.screen_entry(cand(), book(), sp, flat_cost()).ok
    assert NAV > 0
