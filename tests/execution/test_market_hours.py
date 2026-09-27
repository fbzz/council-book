"""Market hours and execution safety (WP-F), against the fake broker.

Approval: per-leg deadlines, legs dropped when their market is closed or closes within 5 min,
the drop rule (and the re-open rule), the re-check after the nonce, held opens counted in the
book, the policy check for rebalances only. Executor: the pre-send session check and broker status
11 (WaitingForMarket) → waiting_for_market, never execution_unknown; polling continues through 11
while the session is open; `resume` never cancels. Watch: the held order resolved read-only
(filled / partial / cancelled / timeout → blocked → `ops resolve`), the timeout also when the
broker read fails."""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from council import watch
from council.broker.fake import OpenScript, eligibility_row, leverage_config
from council.broker.instruments import InstrumentMap
from council.cycle import decision_valid_until, stamp_sessions
from council.execution.executor import DEFAULT_POLL_SCHEDULE, Executor
from council.execution.planner import vehicle_to_line
from council.ledger.states import SATELLITE_BLOCKER_PREFIX
from council.models.plan import Leg
from council.operator.approve import (
    ApprovalDeps,
    ApprovalRefused,
    current_book,
    market_hours_drops,
    resolve_waiting,
)
from council.operator.approve import approve as do_approve
from council.policy import LineSpec, Signal, Vehicle, Vehicles
from council.risk.exposure import snapshot_from_pnl
from tests.cli.operator_sim import simulate_operator
from tests.execution.helpers import INSTRUMENTS, NAV, plan_of

# symbol -> (instrument id, bid, ask, settlement); INSTRUMENTS adds SPX500, NSDQ100, GOLD, EURUSD
EXTRA = {
    "CSPX.L": (301, 600.0, 600.3, "real"), "SGLN.L": (302, 40.0, 40.02, "real"),
    "BTC": (303, 60000.0, 60030.0, "real"), "SPY": (304, 600.0, 600.1, "cfd"),
    "TSTA": (305, 100.0, 100.05, "real"),
}
SLOT_1440 = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)      # Thursday, BST / EDT
SAME = object()


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def at(fclock, when: datetime) -> None:
    fclock.advance((when - fclock.now()).total_seconds())


def _px(symbol: str) -> tuple[int, float, float]:
    if symbol in EXTRA:
        iid, bid, ask, _ = EXTRA[symbol]
        return iid, bid, ask
    return INSTRUMENTS[symbol]


def symbols() -> dict[int, str]:
    return {v[0]: s for s, v in INSTRUMENTS.items()} | {v[0]: s for s, v in EXTRA.items()}


@pytest.fixture
def market(fake, fclock, tmp_path):
    for sym, (iid, bid, ask, settlement) in EXTRA.items():
        configs = ([leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,))]
                   if settlement == "real" else None)
        fake.add_instrument(sym, iid, bid=bid, ask=ask, row=eligibility_row(sym, iid, configs=configs))
    InstrumentMap({}, path=tmp_path / "state" / "instruments.json").merged(
        {s: i for i, s in symbols().items()}, fclock.now()).save()
    return fake


def open_on(seq: int, symbol: str, line: str, units: float, *, settlement: str = "cfd",
            depends_on: tuple[int, ...] = ()) -> Leg:
    iid, _bid, ask = _px(symbol)
    exposure = units * ask
    return Leg(seq=seq, kind="open", symbol=symbol, line=line, instrument_id=iid, direction="long",
               settlement=settlement, weight_before=0.0, weight_after=exposure / NAV,
               stop_distance=0.1, risk_increasing=True, reason=f"test open {symbol}",
               amount_usd=exposure, units=units, sl_rate=ask * 0.9, depends_on=list(depends_on))


def close_on(seq: int, pos: Any, symbol: str, line: str, weight_before: float) -> Leg:
    return Leg(seq=seq, kind="close", symbol=symbol, line=line, instrument_id=pos.instrument_id,
               direction="long", settlement=pos.settlement, weight_before=weight_before,
               weight_after=0.0, risk_increasing=False, reason=f"test close {symbol}",
               amount_usd=pos.units * pos.open_rate, units=pos.units, position_id=pos.position_id)


def book(read_client, policy, fclock, tmp_path) -> dict[str, float]:
    imap = InstrumentMap.load(tmp_path / "state" / "instruments.json")
    return dict(snapshot_from_pnl(read_client.pnl(), vehicle_by_instrument=imap.symbols_by_id(),
                                  line_by_vehicle=vehicle_to_line(policy.universe),
                                  now=fclock.now()).signed_w)


def propose(ledger, policy, read_client, fclock, tmp_path, *legs: Leg, kind: str = "rebalance",
            slot: datetime = SLOT_1440, cycle_id: str | None = "2026-10-01T1440Z",
            policy_sha: Any = SAME, stamp: bool = True, with_pending: bool = False) -> str:
    plan = plan_of(*legs)
    if stamp:
        plan = stamp_sessions(plan, policy.universe, asof=slot)
    decision_id = f"{kind}-{len(ledger.decisions())}"
    base = book(read_client, policy, fclock, tmp_path)
    if with_pending:                    # the engine's base_w counts held opens as held
        base = current_book(base, ledger.pending_open_weights())
    ledger.create_decision(
        decision_id=decision_id, kind=kind, valid_until=decision_valid_until(plan, kind, slot),
        cycle_id=cycle_id, target={"base_w": base}, plan=plan,
        policy_sha=policy.sha256 if policy_sha is SAME else policy_sha, now=fclock.now())
    ledger.insert_legs(decision_id, plan.legs)
    ledger.set_published_commit(decision_id, "c0ffee")
    return decision_id


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


def legs_by_seq(ledger, decision_id: str) -> dict[int, Any]:
    return {r.seq: r for r in ledger.legs(decision_id)}


# ================================================================== deadlines at the cycle
def test_1840_proposal_is_clipped_to_the_us_close(policy):
    slot = utc(2026, 10, 1, 18, 40)
    us_only = stamp_sessions(plan_of(open_on(1, "SPY", "SPX", 1.0)), policy.universe, asof=slot)
    assert us_only.legs[0].session == "us"
    assert us_only.legs[0].valid_until == utc(2026, 10, 1, 19, 50)
    assert decision_valid_until(us_only, "rebalance", slot) == utc(2026, 10, 1, 19, 50)
    mixed = stamp_sessions(plan_of(open_on(1, "SPY", "SPX", 1.0), open_on(2, "BTC", "BTC", 0.01)),
                           policy.universe, asof=slot)
    assert [leg.session for leg in mixed.legs] == ["us", "crypto"]
    assert decision_valid_until(mixed, "rebalance", slot) == utc(2026, 10, 1, 22, 35)  # latest leg
    assert decision_valid_until(us_only, "flatten", slot) == utc(2026, 10, 1, 22, 35)  # never clipped
    assert decision_valid_until(us_only, "compliance", slot) == utc(2026, 10, 1, 22, 35)
    assert mixed.sessions == ["us"]


def test_unmapped_legs_get_no_session_and_the_slot_deadline(policy):
    leg = open_on(1, "SPY", "SPX", 1.0).model_copy(update={"line": "UNMAPPED_9", "kind": "close",
                                                            "risk_increasing": False})
    plan = stamp_sessions(plan_of(leg), policy.universe, asof=SLOT_1440)
    assert plan.legs[0].session is None and plan.legs[0].valid_until == utc(2026, 10, 1, 18, 35)


# ================================================================== approval
def test_approval_after_the_us_close_is_refused(market, ledger, policy, read_client, fclock, tmp_path, deps):
    d, _ = deps
    slot = utc(2026, 10, 1, 18, 40)
    at(fclock, slot)
    rebalance = propose(ledger, policy, read_client, fclock, tmp_path, open_on(1, "SPY", "SPX", 1.0), slot=slot)
    assert ledger.get_decision(rebalance).valid_until == utc(2026, 10, 1, 19, 50)
    at(fclock, utc(2026, 10, 1, 20, 5))                          # 16:05 New York
    with pytest.raises(ApprovalRefused, match="expired"):
        do_approve(rebalance, d)
    assert market.count("POST", "/api/v3/trading/execution/orders") == 0


def test_a_flatten_whose_only_market_is_closed_is_refused(market, ledger, policy, read_client, fclock, tmp_path, deps):
    d, _ = deps
    spy = market.add_position("SPY", units=1.0, sl_rate=500.0)
    w = book(read_client, policy, fclock, tmp_path)["SPX"]
    slot = utc(2026, 10, 1, 18, 40)
    at(fclock, slot)
    flatten = propose(ledger, policy, read_client, fclock, tmp_path, close_on(1, spy, "SPY", "SPX", w),
                      kind="flatten", slot=slot, cycle_id=None)
    at(fclock, utc(2026, 10, 1, 20, 5))
    with pytest.raises(ApprovalRefused, match=r"nothing left to execute: leg 1: .*(us market closed|deadline)"):
        do_approve(flatten, d)
    assert spy.position_id in market.positions


def test_a_leg_whose_market_closes_within_five_minutes_is_dropped(market, ledger, policy, read_client, fclock,
                                                                   tmp_path, deps):
    d, _ = deps
    slot = utc(2026, 10, 1, 18, 40)
    at(fclock, slot)
    legacy = propose(ledger, policy, read_client, fclock, tmp_path, open_on(1, "SPY", "SPX", 1.0),
                     slot=slot, stamp=False)                     # written before legs had deadlines
    at(fclock, utc(2026, 10, 1, 19, 57))
    with pytest.raises(ApprovalRefused, match="us market closes within 5 min"):
        do_approve(legacy, d)


def test_mixed_lse_us_plan_in_bst_drops_the_lse_leg(market, ledger, policy, read_client, fclock, tmp_path, deps):
    d, printed = deps
    spy = market.add_position("SPY", units=1.0, sl_rate=500.0)
    w = book(read_client, policy, fclock, tmp_path)["SPX"]
    decision = propose(ledger, policy, read_client, fclock, tmp_path,
                       close_on(1, spy, "SPY", "SPX", w),
                       open_on(2, "SGLN.L", "GOLD", 10.0, settlement="real"),
                       open_on(3, "BTC", "BTC", 0.005, settlement="real"))
    legs = ledger.get_decision(decision).plan["legs"]
    assert [leg["session"] for leg in legs] == ["us", "lse", "crypto"]
    at(fclock, utc(2026, 10, 1, 15, 25))                         # 16:25 Lisbon: past the LSE deadline
    report = do_approve(decision, d)
    assert report.final_state == "completed_partial"
    rows = legs_by_seq(ledger, decision)
    assert rows[1].state == "filled" and rows[3].state == "filled"
    assert rows[2].state == "skipped" and "dropped at approval: past its deadline 15:20Z" in rows[2].error
    assert any(line.startswith("dropped leg 2: past its deadline") for line in printed)
    assert any("approval deadline 2026-10-01 18:35Z" in line for line in printed)


def test_drop_rule_a_dropped_risk_reducing_leg_drops_every_open(market, ledger, policy, read_client, fclock,
                                                                 tmp_path, deps):
    d, printed = deps
    cspx = market.add_position("CSPX.L", units=1.0, sl_rate=500.0, settlement="real")
    gold = market.add_position("GOLD", units=10.0, sl_rate=40.0)
    weights = book(read_client, policy, fclock, tmp_path)
    decision = propose(ledger, policy, read_client, fclock, tmp_path,
                       close_on(1, cspx, "CSPX.L", "SPX", weights["SPX"]),
                       close_on(2, gold, "GOLD", "GOLD", weights["GOLD"]),
                       open_on(3, "BTC", "BTC", 0.005, settlement="real"))
    at(fclock, utc(2026, 10, 1, 15, 25))
    report = do_approve(decision, d)
    rows = legs_by_seq(ledger, decision)
    assert rows[1].state == "skipped" and "past its deadline" in rows[1].error
    assert rows[3].state == "skipped" and "drop rule" in rows[3].error
    assert rows[2].state == "filled" and report.final_state == "completed_partial"
    assert cspx.position_id in market.positions and not market.positions_for("BTC")


def test_halt_at_0240_drops_closed_legs_and_flattens_crypto_and_fx(market, ledger, policy, read_client, fclock,
                                                                    tmp_path, deps):
    d, _ = deps
    cspx = market.add_position("CSPX.L", units=1.0, sl_rate=500.0, settlement="real")
    spy = market.add_position("SPY", units=1.0, sl_rate=500.0)
    btc = market.add_position("BTC", units=0.01, sl_rate=40_000.0, settlement="real")
    eur = market.add_position("EURUSD", units=500.0, sl_rate=1.0)
    slot = utc(2026, 10, 2, 2, 40)
    at(fclock, slot)
    ledger.set_runtime("kill_state", "HALTED")
    weights = book(read_client, policy, fclock, tmp_path)
    flatten = propose(ledger, policy, read_client, fclock, tmp_path,
                      close_on(1, cspx, "CSPX.L", "SPX", weights["SPX"] / 2),
                      close_on(2, spy, "SPY", "SPX", weights["SPX"] / 2),
                      close_on(3, btc, "BTC", "BTC", weights["BTC"]),
                      close_on(4, eur, "EURUSD", "EURUSD", weights["EURUSD"]),
                      kind="flatten", slot=slot, cycle_id=None, policy_sha="f" * 64)   # 2b skipped
    assert ledger.get_decision(flatten).valid_until == utc(2026, 10, 2, 6, 35)
    at(fclock, utc(2026, 10, 2, 2, 50))
    report = do_approve(flatten, d)
    rows = legs_by_seq(ledger, flatten)
    assert rows[1].state == rows[2].state == "skipped"
    assert rows[3].state == rows[4].state == "filled"
    assert report.final_state == "completed_partial"
    assert btc.position_id not in market.positions and eur.position_id not in market.positions
    assert cspx.position_id in market.positions and spy.position_id in market.positions


def test_a_core_rebalance_is_approved_while_a_satellite_order_is_held(market, write_client, read_client, limiter,
                                                                      fclock, approve, ledger, policy, tmp_path,
                                                                      deps):
    pol = _with_stock_line(policy)
    market.script_open("in_flight", in_flight_status=11)
    held = approve("held")
    Executor(write_client, read_client, ledger, limiter, clock=fclock.now, sleep=fclock.sleep, policy=pol,
             symbol_for=symbols(), _skip_guard_for_tests=True).execute(
        held, plan_of(open_on(1, "TSTA", "TSTA", 15.0, settlement="real")), nav_usd=NAV)
    assert ledger.pending_open_weights() == {"TSTA": pytest.approx(0.150075)}   # above drift_l1_max 0.10
    market.script_open("fill")
    decision = propose(ledger, pol, read_client, fclock, tmp_path, open_on(1, "NSDQ100", "NDX", 5.0),
                       with_pending=True)
    d, printed = deps
    report = do_approve(decision, dataclasses.replace(d, policy=pol))
    assert legs_by_seq(ledger, decision)[1].state == "filled" and report.final_state != "blocked"
    assert any("gross after 0.25x · drift since proposal 0.000" in line for line in printed)  # held open counted


def test_the_market_hours_are_checked_again_after_the_nonce(market, ledger, policy, read_client, fclock, tmp_path,
                                                            deps):
    d, _ = deps
    slot = utc(2026, 10, 1, 18, 40)
    at(fclock, slot)

    def typed_at(when: datetime):
        def input_fn(prompt: str) -> str:
            at(fclock, when)                                        # the operator took a while
            return re.search(r"Type (\S+) to approve", prompt).group(1)
        return dataclasses.replace(d, input_fn=input_fn)

    us_only = propose(ledger, policy, read_client, fclock, tmp_path, open_on(1, "SPY", "SPX", 1.0), slot=slot)
    at(fclock, utc(2026, 10, 1, 19, 49))
    with pytest.raises(ApprovalRefused, match="expired while the nonce was typed"):
        do_approve(us_only, typed_at(utc(2026, 10, 1, 19, 59, 30)))
    assert ledger.get_decision(us_only).state == "proposed"          # refused before the transition
    mixed = propose(ledger, policy, read_client, fclock, tmp_path, open_on(1, "SPY", "SPX", 1.0),
                    open_on(2, "BTC", "BTC", 0.005, settlement="real"), slot=slot)
    at(fclock, utc(2026, 10, 1, 19, 49))
    with pytest.raises(ApprovalRefused, match=r"market hours changed while the nonce was typed \(legs \[1\]\)"):
        do_approve(mixed, typed_at(utc(2026, 10, 1, 19, 51)))
    assert market.count("POST", "/api/v3/trading/execution/orders") == 0
    assert ledger.get_decision(mixed).state == "proposed"


def _reduce(seq: int, kind: str, symbol: str, line: str, *, direction: str = "long", risk_increasing: bool = False,
            depends_on: tuple[int, ...] = (), before: float = 0.1, after: float = 0.0) -> Leg:
    iid, _bid, ask = _px(symbol)
    return Leg(seq=seq, kind=kind, symbol=symbol, line=line, instrument_id=iid, direction=direction,
               settlement="cfd", weight_before=before, weight_after=after, risk_increasing=risk_increasing,
               units=1.0, amount_usd=ask, sl_rate=ask * 0.9, position_id=None if kind == "open" else 900 + seq,
               depends_on=list(depends_on))


def test_a_dropped_reopen_drops_its_close_so_a_trim_never_becomes_an_exit(policy):
    legs = [_reduce(1, "partial_close", "CSPX.L", "SPX", before=0.15, after=0.10),      # LSE trim
            _reduce(2, "close", "GOLD", "GOLD", before=0.12),                            # full close ...
            _reduce(3, "open", "GOLD", "GOLD", risk_increasing=True, depends_on=(2,),    # ... re-open remainder
                    before=0.0, after=0.08),
            _reduce(4, "close", "BTC", "BTC", before=0.05)]
    plan = stamp_sessions(plan_of(*legs), policy.universe, asof=SLOT_1440)
    dropped = market_hours_drops(plan, "rebalance", policy, utc(2026, 10, 1, 15, 25))
    assert sorted(dropped) == [1, 2, 3]                             # GOLD stays at 0.12, not 0
    assert dropped[1].startswith("past its deadline") and "drop rule" in dropped[3]
    assert "its re-open (leg 3) was dropped" in dropped[2]
    flip = [legs[0], legs[1], _reduce(3, "open", "GOLD", "GOLD", direction="short", risk_increasing=True,
                                      depends_on=(2,), before=0.0, after=-0.05)]
    flipped = market_hours_drops(stamp_sessions(plan_of(*flip), policy.universe, asof=SLOT_1440),
                                 "rebalance", policy, utc(2026, 10, 1, 15, 25))
    assert sorted(flipped) == [1, 3]                                # a flip's close alone is a plain reduction


def test_a_vehicle_switch_keeps_its_close_only_in_compliance(policy):
    legs = [_reduce(1, "close", "SPY", "SPX", before=0.10),                              # US vehicle out ...
            _reduce(2, "open", "CSPX.L", "SPX", risk_increasing=True, depends_on=(1,),   # ... LSE vehicle in
                    before=0.0, after=0.10)]
    plan = stamp_sessions(plan_of(*legs), policy.universe, asof=SLOT_1440)
    late = utc(2026, 10, 1, 15, 25)                                 # the LSE leg is past its deadline
    assert sorted(market_hours_drops(plan, "rebalance", policy, late)) == [1, 2]
    assert sorted(market_hours_drops(plan, "compliance", policy, late)) == [2]


def test_a_rebalance_made_under_another_policy_is_refused(market, ledger, policy, read_client, fclock, tmp_path, deps):
    d, _ = deps
    changed = propose(ledger, policy, read_client, fclock, tmp_path, open_on(1, "NSDQ100", "NDX", 5.0),
                      policy_sha="0" * 64)
    with pytest.raises(ApprovalRefused, match="policy changed since the proposal"):
        do_approve(changed, d)
    unknown = propose(ledger, policy, read_client, fclock, tmp_path, open_on(1, "NSDQ100", "NDX", 5.0),
                      policy_sha=None, cycle_id="2026-10-01T1440Z")
    with pytest.raises(ApprovalRefused, match="without a recorded policy SHA"):
        do_approve(unknown, d)
    assert market.count("POST", "/api/v3/trading/execution/orders") == 0


def test_a_legacy_rebalance_uses_its_cycle_record_policy(market, ledger, policy, read_client, fclock, tmp_path, deps):
    d, _ = deps
    ledger.record_cycle({"cycle_id": "2026-10-01T1440Z", "slot": SLOT_1440, "status": "on_time",
                         "policy_sha": policy.sha256})
    legacy = propose(ledger, policy, read_client, fclock, tmp_path, open_on(1, "NSDQ100", "NDX", 5.0),
                     policy_sha=None)
    assert do_approve(legacy, d).final_state == "completed"


# ================================================================== executor pre-send check
def test_executor_skips_a_leg_whose_market_closed(market, make_executor, approve, ledger, fclock):
    spy = market.add_position("SPY", units=1.0, sl_rate=500.0)
    decision = approve()
    at(fclock, utc(2026, 10, 1, 20, 30))                          # after the US close
    plan = plan_of(close_on(1, spy, "SPY", "SPX", 0.06).model_copy(update={"session": "us"}),
                   open_on(2, "NSDQ100", "NDX", 5.0))
    report = make_executor(symbol_for=symbols()).execute(decision, plan, nav_usd=NAV)
    rows = legs_by_seq(ledger, decision)
    assert rows[1].state == "skipped" and rows[1].error == "market_closed"
    assert rows[2].state == "skipped"                              # the drop rule: opens stopped
    assert report.final_state == "completed_partial" and report.writes_sent == 0
    assert spy.position_id in market.positions


def test_executor_skips_a_closed_open_and_sends_the_rest(market, make_executor, approve, ledger, fclock):
    decision = approve()
    at(fclock, utc(2026, 10, 1, 20, 30))
    plan = plan_of(open_on(1, "SPY", "SPX", 1.0), open_on(2, "NSDQ100", "NDX", 5.0))
    report = make_executor(symbol_for=symbols()).execute(decision, plan, nav_usd=NAV)
    rows = legs_by_seq(ledger, decision)
    assert rows[1].state == "skipped" and rows[1].error == "market_closed"   # session from the policy
    assert rows[2].state == "filled" and report.final_state == "completed_partial"


# ================================================================== status 11
def _held_open(market, make_executor, approve, ledger, **executor_kw):
    market.script_open("in_flight", in_flight_status=11)
    decision = approve()
    plan = plan_of(open_on(1, "NSDQ100", "NDX", 5.0), open_on(2, "GOLD", "GOLD", 10.0))
    report = make_executor(symbol_for=symbols(), **executor_kw).execute(decision, plan, nav_usd=NAV)
    return decision, report


def test_status_11_becomes_waiting_for_market_not_a_halt(market, make_executor, approve, ledger, fclock):
    decision, report = _held_open(market, make_executor, approve, ledger)
    assert report.final_state == "waiting_for_market"
    assert ledger.get_decision(decision).state == "waiting_for_market"
    assert ledger.get_decision(decision).blocker_scope == "all"
    rows = legs_by_seq(ledger, decision)
    assert rows[1].state == "waiting_for_market" and rows[1].broker_status.startswith("11:")
    assert rows[1].detail["waiting_deadline"] == "2026-10-02T22:00:00+00:00"   # next FX session + 1 h
    assert rows[2].state == "skipped"                              # later opens stop
    assert not ledger.legs_in_states(["unknown"])
    assert ledger.blockers() == [decision]                         # a whole-book blocker, not unknown
    assert sum(fclock.slept) >= sum(DEFAULT_POLL_SCHEDULE)          # FX open by our calendar: polled through
    assert ledger.pending_open_weights() == {"NDX": pytest.approx(5.0 * 200.2 / NAV)}


def test_status_11_stops_polling_once_the_session_is_closed(market, make_executor, approve, ledger, fclock):
    market.script_open("in_flight", in_flight_status=11)
    at(fclock, utc(2026, 10, 1, 19, 59, 58))                       # sent 2 s before the US close
    decision = approve()
    report = make_executor(symbol_for=symbols()).execute(decision, plan_of(open_on(1, "SPY", "SPX", 1.0)),
                                                         nav_usd=NAV)
    row = legs_by_seq(ledger, decision)[1]
    assert report.final_state == "waiting_for_market" and row.state == "waiting_for_market"
    assert sum(fclock.slept) < 10                                   # stopped once 20:00Z passed
    assert row.detail["waiting_deadline"] == "2026-10-02T21:00:00+00:00"   # next US close + 1 h


def test_status_11_while_the_session_is_open_keeps_polling_until_the_fill(market, read_client, write_client,
                                                                          ledger, limiter, fclock, policy,
                                                                          approve):
    class HaltedThenFilled:                                         # a halt: 11 three times, then filled
        calls = 0

        def __getattr__(self, name: str) -> Any:
            return getattr(read_client, name)

        def order_lookup(self, **kw: Any) -> Any:
            payload = read_client.order_lookup(**kw)
            HaltedThenFilled.calls += 1
            if payload is not None and HaltedThenFilled.calls <= 3:
                payload = {**payload, "status": {**payload["status"], "id": 11, "name": "WaitingForMarket"}}
            return payload

    decision = approve()
    ex = Executor(write_client, HaltedThenFilled(), ledger, limiter, clock=fclock.now, sleep=fclock.sleep,
                  policy=policy, symbol_for=symbols(), _skip_guard_for_tests=True)
    report = ex.execute(decision, plan_of(open_on(1, "NSDQ100", "NDX", 5.0), open_on(2, "GOLD", "GOLD", 10.0)),
                        nav_usd=NAV)
    rows = legs_by_seq(ledger, decision)
    assert rows[1].state == "filled" and rows[2].state == "filled"  # the second open was not stopped
    assert report.final_state == "completed" and not ledger.blockers()


def test_status_11_with_a_cancel_route_cancels_the_order(market, make_executor, approve, ledger, write_client):
    class CancellingWriter:
        CANCEL_ROUTE_VERIFIED = True

        def __init__(self) -> None:
            self.cancelled: list[int] = []

        def __getattr__(self, name: str) -> Any:
            return getattr(write_client, name)

        def cancel_order(self, *, request_id: str, order_id: int) -> dict[str, Any]:
            order = market.orders[order_id]
            order.script, order.status_id, order.resolved = OpenScript("fill"), 7, True
            self.cancelled.append(order_id)
            return {"ok": True}

    writer = CancellingWriter()
    decision, report = _held_open(market, make_executor, approve, ledger, write=writer)
    rows = legs_by_seq(ledger, decision)
    assert writer.cancelled and rows[1].state == "rejected" and rows[1].error == "cancelled_market_closed"
    assert report.final_state == "completed_partial" and not ledger.blockers()
    kinds = [e["kind"] for e in ledger.broker_events(decision)]
    assert "cancel_accepted" in kinds


class _UnverifiedCanceller:
    """A writer that has `cancel_order` but does not declare the route verified."""

    def __init__(self, inner: Any, *, verified: bool = False) -> None:
        self.inner, self.cancelled = inner, []
        if verified:
            self.CANCEL_ROUTE_VERIFIED = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def cancel_order(self, *, request_id: str, order_id: int) -> dict[str, Any]:
        self.cancelled.append(order_id)
        return {"ok": True}


def test_a_cancel_needs_the_verified_route_flag(market, make_executor, approve, ledger, write_client):
    writer = _UnverifiedCanceller(write_client)
    decision, report = _held_open(market, make_executor, approve, ledger, write=writer)
    assert writer.cancelled == [] and report.final_state == "waiting_for_market"
    assert not [e for e in ledger.broker_events(decision) if e["kind"].startswith("cancel")]


def test_resume_never_cancels_a_held_order(market, make_executor, approve, ledger, write_client, fclock):
    market.script_open("in_flight")                                 # status 2 through the window → unknown
    decision = approve()
    first = make_executor(symbol_for=symbols()).execute(decision, plan_of(open_on(1, "NSDQ100", "NDX", 5.0)),
                                                        nav_usd=NAV)
    assert first.final_state == "execution_unknown"
    _order_of(market, ledger, decision).script.in_flight_status = 11  # now held for its market
    writer = _UnverifiedCanceller(write_client, verified=True)
    report = make_executor(symbol_for=symbols(), write=writer).resume(decision)
    assert writer.cancelled == [] and report.writes_sent == 0
    assert legs_by_seq(ledger, decision)[1].state == "waiting_for_market"
    assert ledger.get_decision(decision).state == "waiting_for_market"


def test_a_close_held_for_the_market_waits_and_stops_the_opens(market, read_client, write_client, ledger,
                                                               limiter, fclock, policy, approve):
    class HeldCloses:
        def __getattr__(self, name: str) -> Any:
            return getattr(read_client, name)

        def close_order_info(self, order_id: int) -> dict[str, Any]:
            return {**read_client.close_order_info(order_id), "statusID": 11}

    pos = market.add_position("NSDQ100", units=5.0, sl_rate=150.0)
    market.script_close("in_flight")
    decision = approve()
    plan = plan_of(close_on(1, pos, "NSDQ100", "NDX", 0.1), open_on(2, "GOLD", "GOLD", 10.0))
    ex = Executor(write_client, HeldCloses(), ledger, limiter, clock=fclock.now, sleep=fclock.sleep,
                  policy=policy, symbol_for=symbols(), _skip_guard_for_tests=True)
    report = ex.execute(decision, plan, nav_usd=NAV)
    rows = legs_by_seq(ledger, decision)
    assert rows[1].state == "waiting_for_market" and rows[2].state == "skipped"
    assert report.final_state == "waiting_for_market" and not ledger.legs_in_states(["unknown"])
    assert ledger.pending_open_weights() == {}                      # a held close is not new exposure


# ================================================================== watch resolution
def _watch_ctx(ledger, read_client, policy, tmp_path):
    return SimpleNamespace(ledger=ledger, sources=SimpleNamespace(broker=read_client),
                           state_dir=tmp_path / "state", policy=policy, notifier=None)


def _order_of(market, ledger, decision):
    return market.orders_by_ref[legs_by_seq(ledger, decision)[1].request_id]


def test_watch_resolves_a_held_order_that_filled(market, make_executor, approve, ledger, read_client, policy,
                                                 fclock, tmp_path):
    decision, _ = _held_open(market, make_executor, approve, ledger)
    ctx = _watch_ctx(ledger, read_client, policy, tmp_path)
    assert watch._resolve_waiting(ctx, fclock.now()) == []         # still held: nothing changes
    assert ledger.get_decision(decision).state == "waiting_for_market"
    _order_of(market, ledger, decision).script = OpenScript("fill")  # the market opened
    fclock.advance(900)
    alerts = watch._resolve_waiting(ctx, fclock.now())
    rows = legs_by_seq(ledger, decision)
    assert rows[1].state == "filled" and rows[1].position_ids
    assert ledger.get_decision(decision).state == "completed_partial"   # the second open was skipped
    assert not ledger.blockers() and not any(a.startswith("URGENT") for a in alerts)
    assert ledger.pending_open_weights() == {}


def test_watch_resolves_a_cancelled_hold_as_skipped(market, make_executor, approve, ledger, read_client, policy,
                                                    fclock, tmp_path):
    decision, _ = _held_open(market, make_executor, approve, ledger)
    order = _order_of(market, ledger, decision)
    order.script, order.status_id, order.resolved = OpenScript("fill"), 7, True
    watch._resolve_waiting(_watch_ctx(ledger, read_client, policy, tmp_path), fclock.now())
    assert legs_by_seq(ledger, decision)[1].state == "skipped"
    assert ledger.get_decision(decision).state == "completed_partial" and not ledger.blockers()


def test_a_gap_fill_completes_and_asks_for_a_stop_refit(market, make_executor, approve, ledger, read_client,
                                                        policy, fclock, tmp_path):
    decision, _ = _held_open(market, make_executor, approve, ledger)
    market.set_price("NSDQ100", bid=184.9, ask=185.0)              # opened 7.6% below the plan
    _order_of(market, ledger, decision).script = OpenScript("fill")
    alerts = watch._resolve_waiting(_watch_ctx(ledger, read_client, policy, tmp_path), fclock.now())
    assert any("sl_refit_needed:NDX" in a and a.startswith("URGENT") for a in alerts)
    d = ledger.get_decision(decision)
    assert d.state == "completed_partial"                          # a gap is not a broken fill
    assert "sl_refit_needed:NDX" in ledger.events(decision)[-1]["reason"]
    row = legs_by_seq(ledger, decision)[1]
    assert row.state == "filled" and not row.error and not ledger.blockers()


def test_a_fill_whose_units_differ_blocks_the_whole_book(market, make_executor, approve, ledger, read_client,
                                                         policy, fclock, tmp_path):
    decision, _ = _held_open(market, make_executor, approve, ledger)
    _order_of(market, ledger, decision).script = OpenScript("fill", units_factor=0.8)
    alerts = watch._resolve_waiting(_watch_ctx(ledger, read_client, policy, tmp_path), fclock.now())
    d = ledger.get_decision(decision)
    assert d.state == "blocked" and d.blocker_scope == "all" and ledger.blockers() == [decision]
    assert legs_by_seq(ledger, decision)[1].error == "filled units differ from the units sent"
    assert any(a.startswith("URGENT") and "blocked" in a for a in alerts)


def test_a_broker_exposure_mismatch_blocks(market, make_executor, approve, ledger, read_client, policy, fclock,
                                           tmp_path):
    decision, _ = _held_open(market, make_executor, approve, ledger)
    _order_of(market, ledger, decision).script = OpenScript("fill", exposure_factor=1.2)
    watch._resolve_waiting(_watch_ctx(ledger, read_client, policy, tmp_path), fclock.now())
    assert ledger.get_decision(decision).state == "blocked"
    assert "broker-reported exposure" in legs_by_seq(ledger, decision)[1].error


def test_drift_from_old_targets_is_a_reason_not_a_block(market, make_executor, approve, ledger, read_client,
                                                        policy, fclock, tmp_path):
    decision, _ = _held_open(market, make_executor, approve, ledger)
    market.add_position("SPX500", units=30.0, sl_rate=80.0)         # a later core trade moved the book
    _order_of(market, ledger, decision).script = OpenScript("fill")
    watch._resolve_waiting(_watch_ctx(ledger, read_client, policy, tmp_path), fclock.now())
    assert ledger.get_decision(decision).state == "completed_partial"
    assert "drift above reconcile.drift_max" in ledger.events(decision)[-1]["reason"]


def test_a_partial_fill_keeps_waiting_for_its_remainder_until_the_deadline(market, make_executor, approve, ledger,
                                                                          read_client, policy, fclock, tmp_path):
    decision, _ = _held_open(market, make_executor, approve, ledger)
    ctx = _watch_ctx(ledger, read_client, policy, tmp_path)
    _order_of(market, ledger, decision).script = OpenScript("partial")   # status 5, half the units
    watch._resolve_waiting(ctx, fclock.now())
    row = legs_by_seq(ledger, decision)[1]
    assert row.state == "waiting_for_market" and row.detail["units_filled"] == pytest.approx(2.5)
    assert ledger.get_decision(decision).state == "waiting_for_market"
    at(fclock, utc(2026, 10, 2, 22, 5))                             # past the leg's deadline
    watch._resolve_waiting(ctx, fclock.now())
    assert legs_by_seq(ledger, decision)[1].state == "partially_filled"
    assert ledger.get_decision(decision).state == "completed_partial" and not ledger.blockers()


def test_an_unresolved_hold_times_out_to_blocked_then_ops_resolve(market, make_executor, approve, ledger,
                                                                  read_client, policy, fclock, tmp_path, deps):
    decision, _ = _held_open(market, make_executor, approve, ledger)
    ctx = _watch_ctx(ledger, read_client, policy, tmp_path)
    at(fclock, utc(2026, 10, 2, 21, 55))
    assert watch._resolve_waiting(ctx, fclock.now()) == []
    at(fclock, utc(2026, 10, 2, 22, 5))                             # next FX session close + 1 h
    alerts = watch._resolve_waiting(ctx, fclock.now())
    assert ledger.get_decision(decision).state == "blocked"
    assert any(a.startswith("URGENT") and "council ops resolve" in a for a in alerts)
    assert ledger.blockers() == [decision]
    d, printed = deps
    with pytest.raises(ApprovalRefused):
        resolve_waiting(decision, "maybe", d)
    resolve_waiting(decision, "cancelled", d)
    assert ledger.get_decision(decision).state == "reviewed_no_action"
    assert legs_by_seq(ledger, decision)[1].state == "skipped" and not ledger.blockers()
    assert ledger.events(decision)[-1]["actor"] == "operator"
    with pytest.raises(ApprovalRefused, match="nothing waits"):
        resolve_waiting(decision, "filled", d)


class _BrokenRead:
    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("broker down")


@pytest.mark.parametrize("broker", ["failing", "none"])
def test_the_timeout_applies_when_the_broker_read_fails_or_is_missing(broker, market, make_executor, approve,
                                                                      ledger, policy, fclock, tmp_path):
    decision, _ = _held_open(market, make_executor, approve, ledger)
    ctx = _watch_ctx(ledger, _BrokenRead() if broker == "failing" else None, policy, tmp_path)
    at(fclock, utc(2026, 10, 2, 21, 55))
    early = watch._resolve_waiting(ctx, fclock.now())
    assert ledger.get_decision(decision).state == "waiting_for_market"
    assert not any(a.startswith("URGENT") for a in early)
    at(fclock, utc(2026, 10, 4, 12, 0))                             # the deadline passed two days ago
    alerts = watch._resolve_waiting(ctx, fclock.now())
    assert ledger.get_decision(decision).state == "blocked" and ledger.blockers() == [decision]
    assert any(a.startswith("URGENT") and "council ops resolve" in a for a in alerts)
    if broker == "failing":
        assert any(a.startswith("waiting_check_error:") for a in alerts)


def test_the_cycle_settles_held_orders_before_its_snapshot(market, make_executor, approve, ledger, read_client,
                                                           policy, fclock, tmp_path):
    from council.cycle import _settle_held_orders

    decision, _ = _held_open(market, make_executor, approve, ledger)
    _order_of(market, ledger, decision).script = OpenScript("fill")  # filled before the watch ran
    _settle_held_orders(_watch_ctx(ledger, read_client, policy, tmp_path), fclock.now())
    assert ledger.get_decision(decision).state == "completed_partial"
    assert ledger.pending_open_weights() == {}                      # the snapshot alone holds it now


def test_a_flatten_adds_no_second_close_to_a_held_close(market, read_client, write_client, ledger, limiter,
                                                       fclock, policy, approve):
    from council.cycle import without_held_closes

    class HeldCloses:
        def __getattr__(self, name: str) -> Any:
            return getattr(read_client, name)

        def close_order_info(self, order_id: int) -> dict[str, Any]:
            return {**read_client.close_order_info(order_id), "statusID": 11}

    ndx = market.add_position("NSDQ100", units=5.0, sl_rate=150.0)
    gold = market.add_position("GOLD", units=10.0, sl_rate=40.0)
    market.script_close("in_flight")
    decision = approve()
    ex = Executor(write_client, HeldCloses(), ledger, limiter, clock=fclock.now, sleep=fclock.sleep,
                  policy=policy, symbol_for=symbols(), _skip_guard_for_tests=True)
    ex.execute(decision, plan_of(close_on(1, ndx, "NSDQ100", "NDX", 0.1)), nav_usd=NAV)
    assert legs_by_seq(ledger, decision)[1].state == "waiting_for_market"
    flatten = plan_of(close_on(1, ndx, "NSDQ100", "NDX", 0.1), close_on(2, gold, "GOLD", "GOLD", 0.05))
    kept = without_held_closes(flatten, ledger)
    assert [leg.position_id for leg in kept.legs] == [gold.position_id]


def test_the_execution_record_is_published_only_once_resolved(market, make_executor, approve, ledger,
                                                              read_client, policy, fclock, tmp_path):
    decision, report = _held_open(market, make_executor, approve, ledger)
    ledger.set_runtime(f"exec_report:{decision}", {"report": report.model_dump(mode="json"),
                                                   "cycle_id": "2026-10-01T1440Z", "nav_usd": NAV,
                                                   "plan": None})
    ledger.set_runtime("execution_reports", [decision])
    ctx = _watch_ctx(ledger, read_client, policy, tmp_path)
    assert watch._executions(ctx, {}) == []
    _order_of(market, ledger, decision).script = OpenScript("fill")
    watch._resolve_waiting(ctx, fclock.now())
    stored = ledger.get_runtime(f"exec_report:{decision}")["report"]
    assert stored["final_state"] == "completed_partial" and stored["legs"][0]["state"] == "filled"
    files: dict[str, bytes] = {}
    assert watch._executions(ctx, files) == [decision] and files


# ================================================================== scope
def _with_stock_line(policy):
    stock = LineSpec(symbol="TSTA", name="Test stock A", asset_class="stock", sleeve="satellite",
                     in_reference=False, base_weight=0.05, signal=Signal(source="tiingo", ticker="TSTA"),
                     vehicles=Vehicles(long=[Vehicle(symbol="TSTA", settlement="real")]))
    universe = policy.universe.model_copy(update={"lines": [*policy.universe.lines, stock]})
    return policy.model_copy(update={"universe": universe})


def test_a_held_stock_order_holds_only_the_satellite(market, write_client, read_client, limiter, fclock, approve,
                                                    ledger, policy):
    pol = _with_stock_line(policy)
    market.script_open("in_flight", in_flight_status=11)
    decision = approve()
    plan = plan_of(open_on(1, "TSTA", "TSTA", 5.0, settlement="real"))
    ex = Executor(write_client, read_client, ledger, limiter, clock=fclock.now, sleep=fclock.sleep,
                  policy=pol, symbol_for=symbols(), _skip_guard_for_tests=True)
    report = ex.execute(decision, plan, nav_usd=NAV)
    assert report.final_state == "waiting_for_market"
    assert ledger.get_decision(decision).blocker_scope == "satellite"
    assert ledger.blockers() == [f"{SATELLITE_BLOCKER_PREFIX}{decision}"]
    # a timed-out stock-only hold keeps its satellite scope: the core stays tradable
    ctx = SimpleNamespace(ledger=ledger, sources=SimpleNamespace(broker=None), notifier=None)
    at(fclock, utc(2026, 10, 3, 12, 0))
    watch._resolve_waiting(ctx, fclock.now())
    assert ledger.get_decision(decision).state == "blocked"
    assert ledger.blockers() == [f"{SATELLITE_BLOCKER_PREFIX}{decision}"]


def test_a_waiting_decision_without_a_scope_holds_the_whole_book(ledger, approve):
    decision = approve()
    ledger.transition(decision, "executing", "test", actor="executor")
    ledger.transition(decision, "waiting_for_market", "test", actor="executor")   # scope never set (NULL)
    assert ledger.get_decision(decision).blocker_scope is None
    assert ledger.blockers() == [decision]


def test_a_satellite_scoped_blocker_leaves_core_lines_tradable(policy):
    from tests.risk.helpers import loose, run

    pol = loose(_with_stock_line(policy))
    units = {line.symbol: line.base_weight for line in pol.universe.lines}
    satellite = run(pol, unit_weights=units, current={"TSTA": 0.05}, blockers=["satellite:d9"],
                    pending_w={"TSTA": 0.05})
    assert satellite.final_w["NDX"] > 0.0 and satellite.final_w["BTC"] > 0.0   # the core trades
    assert satellite.final_w["TSTA"] == pytest.approx(0.10)                    # held, pending counted
    assert satellite.base_w["TSTA"] == pytest.approx(0.10)
    whole = run(pol, unit_weights=units, current={"TSTA": 0.05}, blockers=["d9"])
    assert whole.final_w["NDX"] == 0.0 and whole.final_w["TSTA"] == pytest.approx(0.05)
    r20 = next(c for c in satellite.checks if c.rule_id == "R20")
    assert r20.passed


# ================================================================== operator display and commands
def test_inbox_line_shows_the_approval_deadline(market, ledger, policy, read_client, fclock, tmp_path):
    from council.cli import _deadline

    decision = propose(ledger, policy, read_client, fclock, tmp_path,
                       open_on(1, "SGLN.L", "GOLD", 10.0, settlement="real"),
                       open_on(2, "BTC", "BTC", 0.005, settlement="real"))
    text = _deadline(ledger.get_decision(decision))
    assert text == "approve by 2026-10-01 18:35Z  markets lse  (first leg expires 15:20Z)"


def test_ops_resolve_needs_the_operator_terminal(monkeypatch):
    from typer.testing import CliRunner

    from council.cli import app

    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    result = CliRunner().invoke(app, ["ops", "resolve", "d1", "--filled"])
    assert result.exit_code == 2 and "COUNCIL_ROLE must be 'operator'" in result.output
    both = CliRunner().invoke(app, ["ops", "resolve", "d1", "--filled", "--cancelled"])
    assert both.exit_code == 2


def test_approve_runs_under_the_committed_head_policy(monkeypatch):
    from typer.testing import CliRunner

    import council.context as context
    from council.cli import app

    seen: dict[str, Any] = {}

    def fake_build_context(**kwargs: Any) -> Any:
        seen.update(kwargs)
        raise RuntimeError("stop before the broker")

    monkeypatch.setattr(context, "build_context", fake_build_context)
    simulate_operator(monkeypatch)                  # operator terminal, installed release (M5-B)
    result = CliRunner().invoke(app, ["approve", "d1"])
    assert isinstance(result.exception, RuntimeError)
    assert seen["policy_from_head"] is True and seen["mode"] == "stub"


def test_inbox_and_ops_resolve_need_only_the_ledger(market, make_executor, approve, ledger, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import council.context as context
    from council.cli import app
    from council.operator import guards

    decision, _ = _held_open(market, make_executor, approve, ledger)

    def no_policy(**kwargs: Any) -> Any:
        raise AssertionError("a bookkeeping command loaded policy")

    monkeypatch.setattr(context, "build_context", no_policy)
    monkeypatch.setenv("COUNCIL_STATE_DIR", str(tmp_path / "state"))
    simulate_operator(monkeypatch)                  # operator terminal, installed release (M5-B)
    inbox = CliRunner().invoke(app, ["inbox"])
    assert inbox.exit_code == 0, inbox.output
    assert decision in inbox.output and "held until the market opens" in inbox.output
    monkeypatch.setattr(guards, "assert_operator_context", lambda **kwargs: None)
    monkeypatch.setattr(guards, "process_ancestors", lambda: [])
    result = CliRunner().invoke(app, ["ops", "resolve", decision, "--cancelled"])
    assert result.exit_code == 0, result.output
    assert ledger.get_decision(decision).state == "reviewed_no_action"


def test_policy_blockers_reach_the_engine_with_their_scope(ledger):
    from council.cycle import engine_blockers

    ledger.create_decision(decision_id="x1", kind="rebalance", valid_until=utc(2026, 10, 1, 18, 35))
    ledger.transition("x1", "blocked", "test")
    ctx = SimpleNamespace(ledger=ledger, policy_blockers=(
        SimpleNamespace(code="sleeve_policy_untagged", scope="satellite"),
        SimpleNamespace(code="odd", scope="all")))
    assert engine_blockers(ctx) == ["x1", "satellite:sleeve_policy_untagged", "odd"]
    assert engine_blockers(SimpleNamespace(ledger=ledger)) == ["x1"]
