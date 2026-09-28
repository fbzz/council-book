"""SW-5a: swing execution against the fake broker (swing-book.md rev 2, §4.2, §4.4, §4.5, §4.6).

Entry TP in the open body (no PATCH) behind `tp_on_open`; otherwise a ledgered `modify_tp` leg that
always resends the current stop; a crash between the fill and the PATCH leaves `open_tp_missing` and
a `set_tp` leg at the next slot; sleeve-scoped stops; the `stock_cfd_short` gate; the post-execution
reconcile maps a swing fill to its swing line."""

# ruff: noqa: F811 - the execution fixtures are imported by name and requested as parameters

from __future__ import annotations

import pytest

from council.broker.eligibility import parse_eligibility, resolve_vehicle
from council.broker.fake import SimulatedCrash, eligibility_row, leverage_config
from council.broker.parsing import parse_pnl, snapshot_from_portfolio
from council.execution.executor import leg_request_id
from council.execution.planner import SwingOrder, build_plan, set_tp_orders
from council.models.broker import Quote
from council.models.plan import Leg
from council.operator.capabilities import CAPABILITIES, Capabilities
from council.swing.book import swing_vehicle_map
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
from tests.execution.helpers import INSTRUMENTS, NAV, open_leg, plan_of

NVDA, NVDA_ID, BID, ASK = "NVDA", 201, 100.0, 100.1
LINE = "SW_NVDA"
TRADE = "trade:t1"
SYMBOLS = {**{iid: sym for sym, (iid, _b, _a) in INSTRUMENTS.items()}, NVDA_ID: NVDA}
BASE_CAPS = frozenset(CAPABILITIES) | {"stock_real_long", "stock_cfd_short"}
STOPS = {"SPX": 0.08, "NDX": 0.08, "GOLD": 0.08, "EURUSD": 0.04}


def _nvda_row(min_tp_pct: float = 2.0) -> dict:
    return eligibility_row(NVDA, NVDA_ID, configs=[
        leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,), min_tp_pct=min_tp_pct),
        leverage_config(settlement="CFD", direction="SHORT", leverage_values=(1,), min_tp_pct=min_tp_pct),
    ])


@pytest.fixture(autouse=True)
def _nvda(fake):
    fake.add_instrument(NVDA, NVDA_ID, bid=BID, ask=ASK, row=_nvda_row())


def _executor(make_executor, **kw):
    kw.setdefault("symbol_for", SYMBOLS)
    return make_executor(**kw)


def _plan(fake, fclock, policy, *, caps, orders, target=None, ledger=None, snapshot=None):
    raw = [inst.row for inst in fake.instruments.values()]
    elig = {r.symbol: r for r in parse_eligibility({"eligibilities": raw}, fclock.now())}
    quotes = {i.symbol: Quote(symbol=i.symbol, instrument_id=i.instrument_id, bid=i.bid, ask=i.ask, at=fclock.now())
              for i in fake.instruments.values()}
    lines = policy.universe.by_symbol()
    if snapshot is None:
        snapshot = snapshot_from_portfolio(parse_pnl({"clientPortfolio": {"credit": NAV, "positions": []}},
                                                     SYMBOLS.get), fclock.now())
    return build_plan(
        snapshot=snapshot, target_w=target or {},
        vehicle_for=lambda line, d, lev: resolve_vehicle(lines[line], d, lev, elig, lambda v, r, c: 5.0),
        quotes=quotes, stop_distance=STOPS, leverage_for={}, eligibility=elig,
        cost_bps=lambda line, sym, d, lev: (5.0, 0.0), nav_usd=NAV, policy=policy,
        capabilities=Capabilities(verified=frozenset(caps)), swing=orders,
        swing_map=swing_vehicle_map(ledger, policy) if ledger is not None else None,
    )


def _entry(side: str = "long", *, symbol: str = NVDA, line: str = LINE, trade: str = TRADE) -> SwingOrder:
    return SwingOrder(line=line, trade_id=trade, side=side, action="enter", symbol=symbol,  # type: ignore[arg-type]
                      instrument_id=NVDA_ID, size_nav=0.08, stop_pct=0.06, target_pct=0.10,
                      time_stop_date="2026-10-15")


def _opens(fake):
    return [r for r in fake.requests if r.method == "POST" and r.path == "/api/v3/trading/execution/orders"]


def _patches(fake):
    return [r for r in fake.requests if r.method == "PATCH"]


class _Spy:
    """The real writer, checking the ledger at the moment each PATCH leaves."""

    def __init__(self, inner, ledger, decision_id):
        self.inner, self.ledger, self.decision_id = inner, ledger, decision_id
        self.seen: list[tuple[int, str, str | None]] = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def patch_stop_loss(self, **kw):
        leg = self.ledger.leg_by_request_id(kw["request_id"])
        self.seen.append((leg.seq, leg.state, leg.request_id))
        return self.inner.patch_stop_loss(**kw)


# ------------------------------------------------------------------------------ TP in the body
def test_entry_with_tp_in_the_body_sends_no_patch(fake, fclock, policy, ledger, approve, make_executor):
    ledger.create_swing_trade(TRADE, ticker=NVDA, side="long")
    plan = _plan(fake, fclock, policy, caps=BASE_CAPS | {"tp_on_open"}, orders=[_entry()])
    (leg,) = plan.legs
    assert leg.kind == "open" and leg.tp_mode == "body" and leg.settlement == "real"
    assert leg.sl_rate == pytest.approx(ASK * 0.94) and leg.tp_rate == pytest.approx(ASK * 1.10)
    decision = approve()
    report = _executor(make_executor).execute(decision, plan, nav_usd=NAV)

    (post,) = _opens(fake)
    assert post.body["takeProfitRate"] == pytest.approx(ASK * 1.10)
    assert post.body["stopLossRate"] == pytest.approx(ASK * 0.94)
    assert not _patches(fake)
    (pos,) = fake.positions_for(NVDA)
    assert pos.tp_rate == pytest.approx(ASK * 1.10)
    assert ledger.swing_trade(TRADE).state == "open"
    assert report.final_state == "completed" and not ledger.has_blocker()
    # the post-execution reconcile maps the swing fill to its swing line (not an unknown position)
    assert report.reconcile.unknown_positions == [] and report.reconcile.ok
    assert report.reconcile.achieved_w[LINE] == pytest.approx(0.08, abs=1e-3)


def test_tp_body_dropped_by_the_broker_is_open_tp_missing(fake, fclock, policy, ledger, approve, make_executor):
    fake.tp_on_open_supported = False                 # the route accepts takeProfitRate and ignores it
    ledger.create_swing_trade(TRADE, ticker=NVDA, side="long")
    plan = _plan(fake, fclock, policy, caps=BASE_CAPS | {"tp_on_open"}, orders=[_entry()])
    report = _executor(make_executor).execute(approve(), plan, nav_usd=NAV)
    assert ledger.swing_trade(TRADE).state == "open_tp_missing"
    assert report.reconcile.ok                         # the stop is in force; not a book halt
    assert ledger.blockers() == [f"swing:{TRADE}"]


# ------------------------------------------------------------------------------ TP by PATCH
def test_without_the_gate_modify_tp_is_ledgered_submitting_before_send(
        fake, fclock, policy, ledger, approve, make_executor, write_client):
    ledger.create_swing_trade(TRADE, ticker=NVDA, side="long")
    plan = _plan(fake, fclock, policy, caps=BASE_CAPS, orders=[_entry()])
    entry, tp_leg = plan.legs
    assert (entry.kind, entry.tp_mode, tp_leg.kind) == ("open", "patch", "modify_tp")
    assert tp_leg.depends_on == [entry.seq] and tp_leg.sl_rate == entry.sl_rate
    decision = approve()
    spy = _Spy(write_client, ledger, decision)
    fake.script_open("fill", price_factor=1.003)      # filled away from the plan: no stop re-anchor
    report = _executor(make_executor, write=spy).execute(decision, plan, nav_usd=NAV)

    assert spy.seen == [(tp_leg.seq, "submitting", leg_request_id(decision, tp_leg.seq, 0))]
    assert "takeProfitRate" not in _opens(fake)[0].body
    (body,) = fake.patch_bodies
    assert body == {"stopLossRate": pytest.approx(entry.sl_rate), "stopLossType": "fixed",
                    "takeProfitRate": pytest.approx(entry.tp_rate)}
    (pos,) = fake.positions_for(NVDA)
    assert pos.sl_rate == pytest.approx(entry.sl_rate) and pos.tp_rate == pytest.approx(entry.tp_rate)
    for seq in (entry.seq, tp_leg.seq):               # every swing leg is marked at insert time
        detail = ledger.get_leg(decision, seq).detail
        assert detail["sleeve"] == "swing" and detail["swing_trade_id"] == TRADE
    assert ledger.get_leg(decision, tp_leg.seq).state == "filled"
    assert ledger.swing_trade(TRADE).state == "open"
    assert report.final_state == "completed" and report.reconcile.ok


def test_patch_body_always_carries_the_stop_and_never_clears():
    import importlib

    w = importlib.import_module("council.broker.etoro_write")
    assert w.build_patch_body(stop_loss_rate=90.0, take_profit_rate=110.0) == {
        "stopLossRate": 90.0, "stopLossType": "fixed", "takeProfitRate": 110.0}
    with pytest.raises(w.MissingStopLoss):
        w.build_patch_body(stop_loss_rate=None, take_profit_rate=110.0)  # type: ignore[arg-type]
    body = w.build_open_body(instrument_id=1, transaction="buy", settlement="real", leverage=1, units=1.0,
                             stop_loss_rate=90.0, take_profit_rate=110.0)
    assert body["takeProfitRate"] == 110.0 and "clearTakeProfit" not in body
    with pytest.raises(ValueError):                   # a long's target below its stop
        w.build_open_body(instrument_id=1, transaction="buy", settlement="real", leverage=1, units=1.0,
                          stop_loss_rate=90.0, take_profit_rate=80.0)


def test_rejected_tp_patch_is_open_tp_missing_not_a_book_halt(fake, fclock, policy, ledger, approve, make_executor):
    ledger.create_swing_trade(TRADE, ticker=NVDA, side="long")
    plan = _plan(fake, fclock, policy, caps=BASE_CAPS, orders=[_entry()])
    fake.script_patch("http_4xx")
    report = _executor(make_executor).execute(approve(), plan, nav_usd=NAV)
    assert report.final_state == "completed_partial"
    assert ledger.swing_trade(TRADE).state == "open_tp_missing"
    assert ledger.blockers() == [f"swing:{TRADE}"]    # new swing entries halt, the core does not


def test_crash_between_fill_and_patch_is_open_tp_missing_and_set_tp_next_slot(
        fake, fclock, policy, ledger, approve, make_executor, read_client):
    ledger.create_swing_trade(TRADE, ticker=NVDA, side="long")
    plan = _plan(fake, fclock, policy, caps=BASE_CAPS, orders=[_entry()])
    entry, tp_leg = plan.legs
    decision = approve()
    fake.script_patch("crash_before_processing")
    with pytest.raises(SimulatedCrash):
        _executor(make_executor).execute(decision, plan, nav_usd=NAV)
    assert ledger.get_leg(decision, entry.seq).state == "filled"
    assert ledger.get_leg(decision, tp_leg.seq).state == "submitting"
    assert ledger.swing_trade(TRADE).state == "open"  # recorded at the fill, before the crash

    report = _executor(make_executor, write=None).resume(decision)
    assert ledger.get_leg(decision, tp_leg.seq).state == "skipped"
    trade = ledger.swing_trade(TRADE)
    assert trade.state == "open_tp_missing" and trade.tp_rate == pytest.approx(entry.tp_rate)
    assert report.final_state == "completed_partial"
    assert f"swing:{TRADE}" in ledger.blockers() and ledger.swing_entries_blocked()
    (pos,) = fake.positions_for(NVDA)
    assert pos.sl_rate == pytest.approx(entry.sl_rate) and pos.tp_rate is None   # the stop stays

    # next swing slot: the plan carries a set_tp leg for approval, with the current stop
    orders = set_tp_orders(ledger.swing_trades(states=["open_tp_missing"]))
    snap = snapshot_from_portfolio(parse_pnl(read_client.pnl(), SYMBOLS.get), fclock.now())
    nxt = _plan(fake, fclock, policy, caps=BASE_CAPS, orders=orders, ledger=ledger, snapshot=snap)
    (set_leg,) = nxt.legs
    assert set_leg.kind == "set_tp" and set_leg.position_id == pos.position_id
    assert set_leg.sl_rate == pytest.approx(entry.sl_rate) and set_leg.tp_rate == pytest.approx(entry.tp_rate)
    assert set_leg.sleeve == "swing" and set_leg.swing_trade_id == TRADE
    report2 = _executor(make_executor).execute(approve("d2"), nxt, nav_usd=NAV)
    assert fake.patch_bodies[-1]["stopLossRate"] == pytest.approx(entry.sl_rate)
    assert ledger.swing_trade(TRADE).state == "open"
    assert report2.final_state == "completed" and not ledger.has_blocker()


# ------------------------------------------------------------------------------ scoped stops
def _swing_leg(seq: int, direction: str, *, trade: str) -> Leg:
    price = ASK if direction == "long" else BID
    sl = price * (0.94 if direction == "long" else 1.06)
    units = 8.0
    sign = 1 if direction == "long" else -1
    return Leg(seq=seq, kind="open", symbol=NVDA, line=LINE, instrument_id=NVDA_ID, direction=direction,
               settlement="real" if direction == "long" else "cfd", weight_before=0.0,
               weight_after=sign * units * price / NAV, risk_increasing=True, amount_usd=units * price,
               units=units, sl_rate=sl, sleeve="swing", swing_trade_id=trade, tp_mode="none",
               tp_rate=price * (1.1 if direction == "long" else 0.9))


def test_rejected_swing_short_does_not_stop_core_opens(fake, ledger, approve, make_executor):
    decision = approve()
    # plan order puts the swing short FIRST; the executor still sends core opens first
    plan = plan_of(_swing_leg(1, "short", trade="trade:s1"), open_leg(2, "SPX500"),
                   _swing_leg(3, "long", trade="trade:s2"))
    fake.script_open("fill")                          # SPX500 (core, sent first)
    fake.script_open("reject")                        # the swing short
    report = _executor(make_executor).execute(decision, plan, nav_usd=NAV)
    assert ledger.get_leg(decision, 2).state == "filled"
    assert ledger.get_leg(decision, 1).state == "rejected"
    assert ledger.get_leg(decision, 3).state == "skipped"   # later swing opens stop
    assert len(_opens(fake)) == 2
    assert report.final_state == "completed_partial"


def test_rejected_core_open_stops_swing_opens(fake, ledger, approve, make_executor):
    decision = approve()
    plan = plan_of(open_leg(1, "SPX500"), _swing_leg(2, "long", trade="trade:s1"))
    fake.script_open("reject")
    _executor(make_executor).execute(decision, plan, nav_usd=NAV)
    assert ledger.get_leg(decision, 1).state == "rejected"
    assert ledger.get_leg(decision, 2).state == "skipped"
    assert len(_opens(fake)) == 1


# ------------------------------------------------------------------------------ planner gates
def test_unproven_stock_cfd_short_drops_shorts_with_capability_not_proven(fake, fclock, policy):
    fake.add_instrument("AMD", 202, bid=50.0, ask=50.05, row=eligibility_row("AMD", 202, configs=[
        leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,), min_tp_pct=2.0)]))
    orders = [_entry("short"), SwingOrder(line="SW_AMD", trade_id="trade:t2", side="long", action="enter",
                                          symbol="AMD", instrument_id=202, size_nav=0.08, stop_pct=0.06,
                                          target_pct=0.10)]
    caps = BASE_CAPS - {"stock_cfd_short"}
    plan = _plan(fake, fclock, policy, caps=caps, orders=orders, target={"SPX": 0.10})
    assert f"{LINE}: capability_not_proven:stock_cfd_short" in plan.skipped
    assert {leg.line for leg in plan.legs} == {"SPX", "SW_AMD"}
    kinds = [(leg.line, leg.kind) for leg in plan.legs]
    assert kinds.index(("SPX", "open")) < kinds.index(("SW_AMD", "open")) < kinds.index(("SW_AMD", "modify_tp"))


def test_target_below_broker_minimum_goes_nowhere(fake, fclock, policy):
    fake.instrument(NVDA).row = _nvda_row(min_tp_pct=15.0)
    plan = _plan(fake, fclock, policy, caps=BASE_CAPS | {"tp_on_open"}, orders=[_entry()])
    (leg,) = plan.legs
    assert leg.tp_mode == "none" and f"{LINE}: tp_below_broker_minimum" in plan.skipped


def test_swing_stop_is_never_widened_to_a_broker_minimum(fake, fclock, policy):
    fake.instrument(NVDA).row = eligibility_row(NVDA, NVDA_ID, configs=[
        leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,), min_sl_pct=10.0)])
    plan = _plan(fake, fclock, policy, caps=BASE_CAPS, orders=[_entry()])
    assert not plan.legs and f"{LINE}: stop_outside_broker_bounds" in plan.skipped


def test_ambiguous_tp_patch_holds_new_swing_entries_only(fake, fclock, policy, ledger, approve, make_executor):
    ledger.create_swing_trade(TRADE, ticker=NVDA, side="long")
    plan = _plan(fake, fclock, policy, caps=BASE_CAPS, orders=[_entry()])
    fake.script_patch("http_5xx_not_processed")
    decision = approve()
    report = _executor(make_executor).execute(decision, plan, nav_usd=NAV)
    assert report.final_state == "execution_unknown"
    assert ledger.swing_trade(TRADE).state == "open_tp_missing"
    assert ledger.blockers() == sorted([f"swing:{decision}", f"swing:{TRADE}"])   # the core is not held
