"""SW-5b: the swing approval guards against the fake broker (swing-book.md rev 2, §4.1, §4.3).

The S16 guard runs BEFORE the screen with the live rate: moved against -> same stop/target rates and
units; moved in favour 1.0% -> units reduced (the loss at the stop stays the approved one); 1.6% ->
dropped, core legs executed; after the nonce a price change only drops; older than 60 min ->
`expired`; a swing SL that fired inside the window drops that leg and the decision proceeds; a swing
closed at the broker never trips the drift check; `--skip idea:k`; the screen's swing columns; a
dropped swing exit never drops a core leg."""

# ruff: noqa: F811 - the execution fixtures are imported by name and requested as parameters

from __future__ import annotations

import dataclasses
import re
from datetime import timedelta

import pytest

from council import watch
from council.broker.fake import eligibility_row, leverage_config
from council.broker.instruments import InstrumentMap
from council.cycle import decision_valid_until, stamp_sessions, stamp_swing_legs
from council.models.plan import Leg
from council.operator.approve import ApprovalDeps, ApprovalRefused, market_hours_drops
from council.operator.approve import approve as do_approve
from tests.execution.conftest import (  # noqa: F401 - pytest fixtures
    fake,
    fclock,
    ledger,
    read_client,
    write_client,
)
from tests.execution.helpers import INSTRUMENTS, NAV, plan_of

NVDA, NVDA_ID, BID, ASK = "NVDA", 201, 100.0, 100.1
TRADE, IDEA = "trade:t1", "idea:c1_1"
SLOT = None   # the fake clock's start (2026-10-01 14:40 UTC, a Thursday in EDT)


@pytest.fixture(autouse=True)
def _swing_book_live(monkeypatch):
    """These tests model the LIVE swing path: with the switch off the planner never plans a swing
    entry and the approval drops one (`swing_book_not_live`, tests/swing/test_sw5c_review.py)."""
    from council import invariants

    monkeypatch.setattr(invariants, "SWING_BOOK_LIVE", True)


@pytest.fixture
def mkt(fake, fclock, tmp_path):
    fake.add_instrument(NVDA, NVDA_ID, bid=BID, ask=ASK, row=eligibility_row(NVDA, NVDA_ID, configs=[
        leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,)),
        leverage_config(settlement="CFD", direction="SHORT", leverage_values=(1,))]))
    symbols = {s: v[0] for s, v in INSTRUMENTS.items()} | {NVDA: NVDA_ID}
    InstrumentMap({}, path=tmp_path / "state" / "instruments.json").merged(symbols, fclock.now()).save()
    return fake


@pytest.fixture
def deps(ledger, policy, read_client, write_client, fclock, tmp_path):
    printed: list[str] = []
    d = ApprovalDeps(
        ledger=ledger, policy=policy, read=read_client, write_factory=lambda: write_client,
        state_dir=tmp_path / "state",
        input_fn=lambda prompt: re.search(r"Type (\S+) to approve", prompt).group(1),
        print_fn=printed.append, now_fn=fclock.now, guard_fn=lambda: None,
        executor_kwargs={"clock": fclock.now, "sleep": fclock.sleep, "_skip_guard_for_tests": True},
    )
    return d, printed


def core_open(seq: int, units: float = 5.0) -> Leg:
    iid, _bid, ask = INSTRUMENTS["NSDQ100"]
    exposure = units * ask
    return Leg(seq=seq, kind="open", symbol="NSDQ100", line="NDX", instrument_id=iid, direction="long",
               settlement="cfd", weight_before=0.0, weight_after=exposure / NAV, stop_distance=0.1,
               risk_increasing=True, reason="core open", amount_usd=exposure, units=units, sl_rate=ask * 0.9)


def swing_entry(seq: int, *, units: float = 8.0, stop: float = 0.06, target: float = 0.10,
                trade: str = TRADE) -> Leg:
    dw = units * ASK / NAV
    return Leg(seq=seq, kind="open", symbol=NVDA, line="SW_NVDA", instrument_id=NVDA_ID, direction="long",
               settlement="real", weight_before=0.0, weight_after=dw, stop_distance=stop, cost_bps_nav=5.0 * dw,
               risk_increasing=True, reason="swing entry", amount_usd=units * ASK, units=units,
               sl_rate=ASK * (1 - stop), tp_rate=ASK * (1 + target), tp_mode="body", sleeve="swing",
               swing_trade_id=trade, time_stop_date="2026-10-15")


def swing_close(seq: int, pos, trade: str = "trade:t0") -> Leg:
    w = pos.units * pos.open_rate / NAV
    return Leg(seq=seq, kind="close", symbol=NVDA, line="SW_NVDA", instrument_id=NVDA_ID, direction="long",
               settlement="real", weight_before=w, weight_after=0.0, risk_increasing=False, reason="swing exit",
               amount_usd=pos.units * pos.open_rate, units=pos.units, position_id=pos.position_id,
               sleeve="swing", swing_trade_id=trade)


def propose(ledger, policy, fclock, *legs: Leg, base: dict | None = None, created=None) -> str:
    slot = fclock.now()
    plan = stamp_swing_legs(stamp_sessions(plan_of(*legs), policy.universe, asof=slot), slot)
    decision_id = f"rebalance-{len(ledger.decisions())}"
    ledger.create_decision(decision_id=decision_id, kind="rebalance",
                           valid_until=decision_valid_until(plan, "rebalance", slot), cycle_id="2026-10-01T1440Z",
                           target={"base_w": base or {}}, plan=plan, policy_sha=policy.sha256,
                           now=created or fclock.now())
    ledger.insert_legs(decision_id, plan.legs)
    ledger.set_published_commit(decision_id, "c0ffee")
    for leg in plan.legs:
        if leg.kind == "open" and leg.swing_trade_id:
            idea = f"idea:{leg.swing_trade_id.split(':')[1]}"
            if ledger.swing_idea(idea) is None:
                ledger.add_swing_idea(idea, origin_cycle="2026-10-01T1440Z", ticker=NVDA, side="long",
                                      status="proposed")
            ledger.create_swing_trade(leg.swing_trade_id, ticker=NVDA, side="long", idea_id=idea,
                                      origin_cycle="2026-10-01T1440Z", decision_id=decision_id, entry_seq=leg.seq,
                                      instrument_id=NVDA_ID, sl_rate=leg.sl_rate, tp_rate=leg.tp_rate,
                                      detail={"skeptic": "pass", "priced_in": "partly", "regime": "neutral",
                                              "votes_for": 2, "replicates": 3, "size_nav": 0.08, "stop_pct": 0.06})
    return decision_id


def held_swing(ledger, fake, trade: str = "trade:t0", units: float = 8.0):
    """An open swing trade with its live broker position (stop 6%, target 10%)."""
    pos = fake.add_position(NVDA, units=units, sl_rate=ASK * 0.94, tp_rate=ASK * 1.10, settlement="real")
    ledger.create_swing_trade(trade, ticker=NVDA, side="long", instrument_id=NVDA_ID, sl_rate=ASK * 0.94,
                              tp_rate=ASK * 1.10, detail={"size_nav": 0.08, "stop_pct": 0.06})
    ledger.transition_swing_trade(trade, "entry_executing")
    ledger.transition_swing_trade(trade, "open")
    ledger.update_swing_trade(trade, position_ids=[pos.position_id], open_rate=ASK, units=units)
    return pos


def _opens(fake, symbol: str):
    iid = fake.instrument(symbol).instrument_id
    return [r for r in fake.requests if r.method == "POST" and r.path == "/api/v3/trading/execution/orders"
            and r.body.get("instrumentId") == iid]


def legs(ledger, decision_id):
    return {r.seq: r for r in ledger.legs(decision_id)}


# ------------------------------------------------------------------------------ the S16 price guard
def test_price_moved_against_keeps_rates_and_units(mkt, ledger, policy, fclock, deps):
    decision = propose(ledger, policy, fclock, core_open(1), swing_entry(2))
    mkt.set_price(NVDA, bid=BID * 0.99, ask=ASK * 0.99)                   # 1% against a long
    d, printed = deps
    do_approve(decision, d)
    (post,) = _opens(mkt, NVDA)
    assert post.body["units"] == pytest.approx(8.0)
    assert post.body["stopLossRate"] == pytest.approx(ASK * 0.94)          # the approved stop RATE
    assert post.body["takeProfitRate"] == pytest.approx(ASK * 1.10)        # the approved target RATE
    assert any("guard: kept" in line for line in printed)
    assert legs(ledger, decision)[2].state == "filled" and ledger.swing_trade(TRADE).state == "open"


def test_price_moved_in_favour_one_percent_reduces_units(mkt, ledger, policy, fclock, deps):
    decision = propose(ledger, policy, fclock, core_open(1), swing_entry(2))
    live = ASK * 1.01
    mkt.set_price(NVDA, bid=BID * 1.01, ask=live)
    d, printed = deps
    do_approve(decision, d)
    (post,) = _opens(mkt, NVDA)
    sl = ASK * 0.94
    assert post.body["stopLossRate"] == pytest.approx(sl) and post.body["takeProfitRate"] == pytest.approx(ASK * 1.10)
    assert post.body["units"] < 8.0
    # the loss at the stop is never above the approved one; units never raised
    assert post.body["units"] * (live - sl) <= 8.0 * (ASK - sl) + 1e-6
    assert any("guard: resized" in line for line in printed)


def test_price_ran_1_6_percent_drops_the_entry_before_the_screen_and_core_executes(mkt, ledger, policy, fclock, deps):
    decision = propose(ledger, policy, fclock, core_open(1), swing_entry(2))
    mkt.set_price(NVDA, bid=BID * 1.016, ask=ASK * 1.016)
    d, printed = deps
    do_approve(decision, d)
    screen_end = next(i for i, line in enumerate(printed) if line.startswith("gross after"))
    assert not any(line.startswith("  2  open") for line in printed[:screen_end])      # not on the screen
    assert "dropped leg 2: swing_entry_ran" in printed
    assert not _opens(mkt, NVDA) and _opens(mkt, "NSDQ100")
    assert legs(ledger, decision)[1].state == "filled"
    assert ledger.swing_trade(TRADE).state == "missed"
    assert ledger.swing_idea("idea:t1")["status"] == "pending"                          # may be re-proposed


def test_after_the_nonce_a_price_change_only_drops(mkt, ledger, policy, fclock, deps):
    decision = propose(ledger, policy, fclock, core_open(1), swing_entry(2))
    d, printed = deps

    def input_fn(prompt: str) -> str:
        mkt.set_price(NVDA, bid=BID * 1.02, ask=ASK * 1.02)              # runs away while typing
        return re.search(r"Type (\S+) to approve", prompt).group(1)

    do_approve(decision, dataclasses.replace(d, input_fn=input_fn))
    assert any("after the nonce: swing_entry_ran" in line for line in printed)
    assert not _opens(mkt, NVDA) and _opens(mkt, "NSDQ100")               # core still sent


def test_after_the_nonce_a_favourable_small_move_changes_nothing(mkt, ledger, policy, fclock, deps):
    decision = propose(ledger, policy, fclock, core_open(1), swing_entry(2))
    d, _printed = deps

    def input_fn(prompt: str) -> str:
        mkt.set_price(NVDA, bid=BID * 1.01, ask=ASK * 1.01)              # would resize before the nonce
        return re.search(r"Type (\S+) to approve", prompt).group(1)

    do_approve(decision, dataclasses.replace(d, input_fn=input_fn))
    (post,) = _opens(mkt, NVDA)
    assert post.body["units"] == pytest.approx(8.0)                      # no unit change after the nonce


def test_an_entry_older_than_60_minutes_is_expired(mkt, ledger, policy, fclock, deps):
    decision = propose(ledger, policy, fclock, core_open(1), swing_entry(2),
                       created=fclock.now() - timedelta(minutes=61))
    d, printed = deps
    do_approve(decision, d)
    assert "dropped leg 2: expired" in printed and not _opens(mkt, NVDA)
    assert ledger.swing_trade(TRADE).state == "missed"


def test_the_entry_leg_carries_its_own_60_minute_deadline(policy, fclock):
    slot = fclock.now()
    plan = stamp_swing_legs(stamp_sessions(plan_of(core_open(1), swing_entry(2)), policy.universe, asof=slot), slot)
    entry = plan.legs[1]
    assert entry.session == "us" and entry.valid_until <= slot + timedelta(minutes=60)
    assert market_hours_drops(plan, "rebalance", policy, slot + timedelta(minutes=61)) .get(2, "").startswith("past")


# ------------------------------------------------------------------------------ scoped refusals
def test_a_swing_sl_fired_inside_the_window_drops_that_leg_and_the_decision_proceeds(
        mkt, ledger, policy, fclock, deps, monkeypatch):
    pos = held_swing(ledger, mkt)
    decision = propose(ledger, policy, fclock, swing_close(1, pos), core_open(2),
                       base={"SW_NVDA": 8 * ASK / NAV})
    mkt.hit_stop(pos.position_id)                                        # the broker SL fires meanwhile
    monkeypatch.setattr(watch, "closed_trade_route_ok", lambda state_dir: True)
    d, printed = deps

    class Read:                     # the READ client plus the (proven) closed-trade route
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def closed_trade(self, pid):
            return mkt.closed_trade(pid)

    report = do_approve(decision, dataclasses.replace(d, read=Read(d.read)))
    assert "dropped leg 1: swing_position_closed_at_broker" in printed
    assert legs(ledger, decision)[2].state == "filled" and report.final_state != "blocked"
    assert ledger.swing_trade("trade:t0").state == "closed_stop"


def test_a_core_position_that_vanished_still_refuses(mkt, ledger, policy, fclock, deps):
    pos = mkt.add_position("NSDQ100", units=5.0, sl_rate=180.0)
    w = 5.0 * pos.open_rate / NAV
    leg = Leg(seq=1, kind="close", symbol="NSDQ100", line="NDX", instrument_id=102, direction="long",
              settlement="cfd", weight_before=w, weight_after=0.0, risk_increasing=False, reason="close",
              amount_usd=5.0 * pos.open_rate, units=5.0, position_id=pos.position_id)
    decision = propose(ledger, policy, fclock, leg, base={"NDX": w})
    mkt.hit_stop(pos.position_id)
    with pytest.raises(ApprovalRefused, match="no longer exists"):
        do_approve(decision, deps[0])


def test_one_8pct_swing_closed_at_the_broker_does_not_trip_the_drift_check(mkt, ledger, policy, fclock, deps):
    pos = held_swing(ledger, mkt)
    decision = propose(ledger, policy, fclock, core_open(1), base={"SW_NVDA": 8 * ASK / NAV})
    mkt.hit_take_profit(pos.position_id)                    # gone from the book, still `open` in the ledger
    d, printed = deps
    do_approve(decision, d)
    assert any("drift since proposal 0.000" in line for line in printed)
    assert legs(ledger, decision)[1].state == "filled"


def test_skip_idea_drops_that_entry_only(mkt, ledger, policy, fclock, deps):
    decision = propose(ledger, policy, fclock, core_open(1), swing_entry(2))
    d, printed = deps
    do_approve(decision, d, skip=("idea:t1",))
    assert "dropped leg 2: operator_skip:idea:t1" in printed
    assert not _opens(mkt, NVDA) and _opens(mkt, "NSDQ100")
    assert ledger.swing_trade(TRADE).state == "missed"


def test_skip_refuses_an_unknown_ref_and_an_exit(mkt, ledger, policy, fclock, deps):
    pos = held_swing(ledger, mkt)
    decision = propose(ledger, policy, fclock, swing_close(1, pos), core_open(2), base={"SW_NVDA": 8 * ASK / NAV})
    with pytest.raises(ApprovalRefused, match="no swing leg"):
        do_approve(decision, deps[0], skip=("idea:nope",))
    with pytest.raises(ApprovalRefused, match="exit cannot be skipped"):
        do_approve(decision, deps[0], skip=("trade:t0",))
    assert not mkt.requests or not any(r.method == "POST" for r in mkt.requests)


def test_the_screen_shows_the_swing_columns(mkt, ledger, policy, fclock, deps):
    decision = propose(ledger, policy, fclock, core_open(1), swing_entry(2))
    d, printed = deps
    do_approve(decision, d)
    row = next(line for line in printed if line.strip().startswith("swing idea:t1 / trade:t1"))
    for part in ("long", "size 8.0%", "stop 6.0%", "target 10.0%", "to broker: yes (open body)",
                 "time stop 2026-10-15", "skeptic pass/partly/neutral", "PM 2/3", "guard: kept"):
        assert part in row, part
    assert "$" not in row


def test_a_dropped_swing_exit_drops_swing_entries_but_never_a_core_leg(policy, fclock):
    from types import SimpleNamespace

    slot = fclock.now()
    pos = SimpleNamespace(units=8.0, open_rate=ASK, position_id=77)
    plan = plan_of(swing_close(1, pos), core_open(2), swing_entry(3))
    plan = stamp_swing_legs(stamp_sessions(plan, policy.universe, asof=slot), slot)
    exit_ = plan.legs[0].model_copy(update={"valid_until": slot - timedelta(minutes=1)})   # past its deadline
    plan = plan.model_copy(update={"legs": [exit_, *plan.legs[1:]]})
    dropped = market_hours_drops(plan, "rebalance", policy, slot)
    assert 1 in dropped and 3 in dropped and 2 not in dropped
