"""SW-5c review fixes: the executor closes a swing trade when its exit leg fills (idempotent, the
cycle's settle is the fallback); a smoke position on a stock no line owns is expected in reconcile;
flatten / smoke legs off the universe are never priced at a silent zero; at most 2 re-proposals
within 3 sessions; the sector ETF return of a broker close from completed bars; no live swing entry
while SWING_BOOK_LIVE is False (planner and approval); a leg's amount can only shrink."""

# ruff: noqa: F811 - the execution fixtures are imported by name and requested as parameters

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from council import invariants
from council.broker.parsing import parse_pnl, snapshot_from_portfolio
from council.cycle import expire_stale_ideas, reproposal_refused, settle_swing
from council.execution.planner import SwingOrder, floor_cost_fn
from council.execution.reconcile import reconcile
from council.ledger.db import LedgerError
from council.models.broker import ExposureSnapshot, Position
from council.operator.approve import swing_not_live_drops
from council.swing.exits import close_filled_exit, sector_etf_return
from tests.execution.conftest import (  # noqa: F401 - pytest fixtures
    approve,
    fake,
    fclock,
    ledger,
    limiter,
    make_executor,
    read_client,
    write_client,
)
from tests.execution.helpers import NAV, plan_of
from tests.swing.test_execution_swing import (  # noqa: F401 - `_nvda` is an autouse fixture here too
    BASE_CAPS,
    LINE,
    NVDA,
    NVDA_ID,
    SYMBOLS,
    TRADE,
    _entry,
    _executor,
    _nvda,
    _plan,
)


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(invariants, "SWING_BOOK_LIVE", True)


def _open_trade(fake, fclock, policy, ledger, approve, make_executor):
    ledger.create_swing_trade(TRADE, ticker=NVDA, side="long")
    plan = _plan(fake, fclock, policy, caps=BASE_CAPS | {"tp_on_open"}, orders=[_entry()])
    _executor(make_executor).execute(approve("d1"), plan, nav_usd=NAV)
    assert ledger.swing_trade(TRADE).state == "open"


def _exit_plan(fake, fclock, policy, ledger, read_client):
    snap = snapshot_from_portfolio(parse_pnl(read_client.pnl(), SYMBOLS.get), fclock.now())
    order = SwingOrder(line=LINE, trade_id=TRADE, side="long", action="exit", symbol=NVDA, instrument_id=NVDA_ID)
    return _plan(fake, fclock, policy, caps=BASE_CAPS, orders=[order], ledger=ledger, snapshot=snap)


# ------------------------------------------------------------------------------ (a) executor close
def test_executor_closes_the_trade_when_its_exit_leg_fills(live, fake, fclock, policy, ledger, approve,
                                                           make_executor, read_client):
    _open_trade(fake, fclock, policy, ledger, approve, make_executor)
    plan = _exit_plan(fake, fclock, policy, ledger, read_client)
    decision = approve("d2")
    ledger.update_swing_trade(TRADE, detail={"exit_decision": decision, "exit_kind": "time",
                                             "pre_exit_state": "open"}, now=fclock.now())
    ledger.transition_swing_trade(TRADE, "exit_pending", reason="exit_proposed:time", now=fclock.now())
    _executor(make_executor).execute(decision, plan, nav_usd=NAV)
    t = ledger.swing_trade(TRADE)
    assert t.state == "closed_time"
    assert (t.detail or {}).get("exit_kind") == "time"
    # idempotent: the executor's own close again and the cycle's fallback change nothing
    assert close_filled_exit(ledger, TRADE, decision, fclock.now()) is None
    assert settle_swing(ledger, fclock.now()) == []
    assert ledger.swing_trade(TRADE).state == "closed_time"


def test_a_flatten_close_of_a_swing_position_closes_its_trade_as_halt(live, fake, fclock, policy, ledger, approve,
                                                                      make_executor, read_client):
    _open_trade(fake, fclock, policy, ledger, approve, make_executor)
    plan = _exit_plan(fake, fclock, policy, ledger, read_client)
    (leg,) = plan.legs      # the same close, unmarked (as a kill-switch flatten plans it)
    plan = plan_of(leg.model_copy(update={"sleeve": None, "swing_trade_id": None}))
    decision = approve("f1", kind="flatten")
    _executor(make_executor).execute(decision, plan, nav_usd=NAV)
    assert ledger.swing_trade(TRADE).state == "closed_halt"


# ------------------------------------------------------------------------------ (b) smoke expected
def _pos(pid, symbol, iid):
    return Position(position_id=pid, instrument_id=iid, symbol=symbol, is_buy=True, units=1.0, leverage=1,
                    open_rate=100.0, amount=100.0, sl_rate=90.0, settlement="real", exposure_usd=100.0, close_rate=100.0)


def test_a_smoke_position_on_a_stock_no_line_owns_is_not_unknown(policy):
    now = datetime(2026, 10, 1, 15, tzinfo=UTC)
    w = 100.0 / NAV
    snap = ExposureSnapshot(taken_at=now, equity_usd=NAV, credit_usd=0.0,
                            positions=[_pos(7, "ACME", 991), _pos(8, "OTHR", 992)],
                            signed_w={"ACME": w, "OTHR": w}, gross=2 * w, net=2 * w, margin_use=0.0)
    plain = reconcile(snap, {}, [], policy)
    assert set(plain.unknown_positions) == {"ACME", "OTHR"}
    rec = reconcile(snap, {}, [], policy, smoke_position_ids=[7])
    assert rec.unknown_positions == ["OTHR"]             # only the smoke position is expected


# ------------------------------------------------------------------------------ (c) cost floors
def test_flatten_and_smoke_price_off_universe_lines_from_the_stock_floors(policy):
    flatten = floor_cost_fn(policy, None, strict=False)
    core = policy.universe.lines[0].symbol
    assert flatten(core, core, "long", 1) == (0.0, 0.0)                 # a core line: unchanged
    per_side, _carry = flatten("UNMAPPED_991", "ACME", "long", 1)[:2]
    assert per_side > 0                                                  # never a silent zero
    short = floor_cost_fn(policy, None, strict=True)("UNMAPPED_991", "ACME", "short", 1)
    assert short[0] > 0 and short[1] > 0                                # a CFD short carries


def test_smoke_cost_fails_closed_when_the_floors_are_unreadable(policy, monkeypatch):
    from council.execution.planner import SmokePlanError
    from council.risk import costs

    monkeypatch.setattr(costs, "per_side_bps", lambda *a, **k: float("nan"))
    with pytest.raises(SmokePlanError, match="smoke_cost_unavailable"):
        floor_cost_fn(policy, None, strict=True)("UNMAPPED_991", "ACME", "long", 1)
    assert floor_cost_fn(policy, None, strict=False)("UNMAPPED_991", "ACME", "long", 1) == (0.0, 0.0)


# ------------------------------------------------------------------------------ (d) re-proposals
def test_at_most_two_reproposals_within_three_sessions(ledger, policy):
    t0 = datetime(2026, 10, 1, 18, 40, tzinfo=UTC)                     # a Thursday
    ledger.add_swing_idea("idea:a_1", origin_cycle="c0", ticker="ACME", side="long", status="pending", now=t0)
    assert not reproposal_refused(ledger, policy, "ACME", "long", t0 + timedelta(days=1))
    ledger.update_swing_idea("idea:a_1", status="pending", carry_cycle="c1", now=t0)
    assert not reproposal_refused(ledger, policy, "ACME", "long", t0 + timedelta(days=1))
    ledger.update_swing_idea("idea:a_1", status="pending", carry_cycle="c2", now=t0)
    assert reproposal_refused(ledger, policy, "ACME", "long", t0 + timedelta(days=1))   # a 3rd re-proposal
    assert ledger.swing_idea("idea:a_1")["status"] == "expired"
    assert not reproposal_refused(ledger, policy, "WIDG", "long", t0)               # a fresh idea


def test_a_pending_idea_older_than_three_sessions_expires(ledger, policy):
    t0 = datetime(2026, 10, 1, 18, 40, tzinfo=UTC)
    ledger.add_swing_idea("idea:b_1", origin_cycle="c0", ticker="ACME", side="long", status="pending", now=t0)
    assert expire_stale_ideas(ledger, t0 + timedelta(days=4)) == []       # Fri, Mon: 2 sessions
    assert reproposal_refused(ledger, policy, "ACME", "long", t0 + timedelta(days=7))  # 5 sessions
    ledger.add_swing_idea("idea:b_2", origin_cycle="c9", ticker="WIDG", side="long", status="pending", now=t0)
    assert expire_stale_ideas(ledger, t0 + timedelta(days=7)) == ["idea:b_2"]


# ------------------------------------------------------------------------------ (e) sector ETF
def test_sector_etf_return_from_completed_bars():
    idx = pd.to_datetime(["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"], utc=True)
    frame = pd.DataFrame({"close": [100.0, 101.0, 102.0, 104.0]}, index=idx)
    bars = {"XLK": frame}
    opened = datetime(2026, 10, 1, 15, tzinfo=UTC)                   # entry session 2026-10-01
    closed = datetime(2026, 10, 2, 21, tzinfo=UTC)
    got = sector_etf_return({"sector_etf": "XLK"}, opened, closed, lambda tickers, day: bars)
    assert got == pytest.approx(104.0 / 101.0 - 1)                  # prior close -> close day
    assert sector_etf_return({}, opened, closed, lambda t, d: bars) is None
    assert sector_etf_return({"sector_etf": "XLK"}, opened, closed, None) is None


def test_broker_close_records_the_sector_etf_return(live, fake, fclock, policy, ledger, approve, make_executor,
                                                     read_client):
    from council import watch

    _open_trade(fake, fclock, policy, ledger, approve, make_executor)
    ledger.update_swing_trade(TRADE, detail={"sector_etf": "XLK"}, now=fclock.now())
    opened = ledger.swing_trade(TRADE).opened_at
    day0 = pd.Timestamp(opened.date(), tz=UTC)
    frame = pd.DataFrame({"close": [100.0, 110.0]}, index=[day0 - pd.Timedelta(days=1), day0 + pd.Timedelta(days=1)])
    later = fclock.now() + timedelta(days=2)
    state = watch.record_swing_close(ledger, read_client, TRADE, None, now=later, route_ok=False,
                                     sector_bars=lambda t, d: {"XLK": frame})
    assert state == "closed_unclassified"
    assert ledger.swing_trade(TRADE).detail["sector_etf_ret"] == pytest.approx(0.1)


# ------------------------------------------------------------------------------ SWING_BOOK_LIVE False
def test_no_live_swing_entry_is_planned_while_the_switch_is_off(fake, fclock, policy):
    assert invariants.SWING_BOOK_LIVE is False
    plan = _plan(fake, fclock, policy, caps=BASE_CAPS | {"tp_on_open"}, orders=[_entry()])
    assert not plan.legs
    assert any("swing_book_not_live" in s for s in plan.skipped)


def test_approval_drops_swing_entries_while_the_switch_is_off():
    legs = [SimpleNamespace(seq=1, is_swing=True, kind="open", risk_increasing=True, depends_on=[]),
            SimpleNamespace(seq=2, is_swing=True, kind="modify_tp", risk_increasing=False, depends_on=[1]),
            SimpleNamespace(seq=3, is_swing=True, kind="close", risk_increasing=False, depends_on=[]),
            SimpleNamespace(seq=4, is_swing=False, kind="open", risk_increasing=True, depends_on=[])]
    plan = SimpleNamespace(legs=legs)
    assert swing_not_live_drops(plan) == {1: "swing_book_not_live", 2: "swing_book_not_live"}


def test_approval_keeps_swing_entries_when_live(live):
    plan = SimpleNamespace(legs=[SimpleNamespace(seq=1, is_swing=True, kind="open", risk_increasing=True,
                                                 depends_on=[])])
    assert swing_not_live_drops(plan) == {}


# ------------------------------------------------------------------------------ ledger guard
def test_a_leg_amount_can_only_shrink(live, fake, fclock, policy, ledger, approve):
    plan = _plan(fake, fclock, policy, caps=BASE_CAPS | {"tp_on_open"}, orders=[_entry()])
    (leg,) = plan.legs
    decision = approve("g1")
    ledger.insert_legs(decision, plan.legs)
    ledger.update_leg(decision, leg.seq, amount_usd=leg.amount_usd * 0.5)
    with pytest.raises(LedgerError):
        ledger.update_leg(decision, leg.seq, amount_usd=leg.amount_usd)
