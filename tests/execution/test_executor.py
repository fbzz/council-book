"""Executor chaos tests against FakeEtoro: every outcome the broker can throw at an approved plan."""

from __future__ import annotations

import os
import sys
import uuid

import pytest

from council.broker.fake import SimulatedCrash
from council.broker.parsing import parse_pnl, snapshot_from_portfolio
from council.execution.executor import (
    ExecutionError,
    ExecutionLocked,
    Executor,
    leg_request_id,
)
from council.execution.planner import build_flatten_plan
from council.models.plan import Leg
from council.operator import guards
from council.operator.guards import GuardError
from tests.execution.helpers import INSTRUMENTS, NAV, close_leg, open_leg, plan_of

OPEN_PATH = "/api/v3/trading/execution/orders"
CLOSE_PATH = "/api/v1/trading/execution/market-close-orders/positions/"
LOOKUP_PATH = "/api/v2/trading/info/orders"


def _open_posts(fake):
    return [r for r in fake.requests if r.method == "POST" and r.path == OPEN_PATH]


# ------------------------------------------------------------------------------ happy path
def test_uuid5_request_id_is_deterministic_and_attempt_specific():
    a = leg_request_id("d1", 1, 0)
    assert a == leg_request_id("d1", 1, 0)
    assert uuid.UUID(a).version == 5
    assert a != leg_request_id("d1", 1, 1) != leg_request_id("d1", 2, 0)


def test_fill_completes_and_persists_request_id_before_sending(fake, make_executor, approve, ledger):
    decision = approve()
    report = make_executor().execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "completed"
    assert ledger.get_decision(decision).state == "completed"
    posts = _open_posts(fake)
    assert len(posts) == 1
    assert posts[0].headers["x-request-id"] == leg_request_id(decision, 1, 0)
    leg = ledger.get_leg(decision, 1)
    assert leg.state == "filled" and leg.request_id == leg_request_id(decision, 1, 0)
    assert leg.order_id is not None and len(leg.position_ids) == 1
    (pos,) = fake.positions_for("SPX500")
    assert pos.sl_rate == pytest.approx(100.1 * 0.9)          # the approved stop rides the fill
    assert report.reconcile is not None and report.reconcile.ok
    assert not ledger.has_blocker()


def test_fill_after_k_lookups_polls_on_schedule(fake, make_executor, approve, fclock):
    fake.script_open("fill", after_lookups=3)
    report = make_executor().execute(approve(), plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "completed"
    assert fclock.slept[:4] == [1.0, 2.0, 4.0, 8.0]
    assert fake.count("GET", LOOKUP_PATH) == 4


def test_execute_requires_an_approved_decision(make_executor, ledger, fclock):
    from datetime import timedelta

    ledger.create_decision(decision_id="d1", kind="rebalance", valid_until=fclock.now() + timedelta(hours=1))
    with pytest.raises(ExecutionError):
        make_executor().execute("d1", plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert ledger.get_decision("d1").state == "proposed"


# ------------------------------------------------------------------------------ open outcomes
def test_partial_fill_polls_to_the_window_then_completed_partial(fake, make_executor, approve, ledger, fclock):
    fake.script_open("partial", fill_fraction=0.5)
    decision = approve()
    report = make_executor().execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert ledger.get_leg(decision, 1).state == "partially_filled"
    assert report.final_state == "completed_partial"
    assert sum(fclock.slept) >= 60


def test_rejected_open_stops_remaining_opens(fake, make_executor, approve, ledger):
    fake.script_open("reject")
    decision = approve()
    plan = plan_of(open_leg(1, "SPX500"), open_leg(2, "GOLD"))
    report = make_executor().execute(decision, plan, nav_usd=NAV)
    assert report.final_state == "completed_partial"
    assert [leg.state for leg in ledger.legs(decision)] == ["rejected", "skipped"]
    assert len(_open_posts(fake)) == 1
    assert not fake.positions


def test_definite_4xx_on_open_is_rejected_without_retry(fake, make_executor, approve, ledger):
    fake.script_open("http_4xx", status_code=422)
    decision = approve()
    report = make_executor().execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert ledger.get_leg(decision, 1).state == "rejected"
    assert report.final_state == "completed_partial"
    assert len(_open_posts(fake)) == 1


def test_in_flight_forever_becomes_unknown_and_blocks(fake, make_executor, approve, ledger, fclock):
    fake.script_open("in_flight", after_lookups=10_000)
    decision = approve()
    report = make_executor().execute(decision, plan_of(open_leg(1, "SPX500"), open_leg(2, "GOLD")), nav_usd=NAV)
    assert report.final_state == "execution_unknown"
    assert ledger.get_leg(decision, 1).state == "unknown"
    assert ledger.get_leg(decision, 2).state == "skipped"
    assert len(_open_posts(fake)) == 1                     # never resubmitted
    assert sum(fclock.slept) == pytest.approx(120)
    assert ledger.has_blocker()


def test_5xx_on_post_is_never_resubmitted_and_found_by_lookup(fake, make_executor, approve, ledger):
    fake.script_open("http_5xx_processed")
    decision = approve()
    report = make_executor().execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "completed"
    assert len(_open_posts(fake)) == 1
    assert len(fake.orders) == 1
    lookups = [r for r in fake.requests if r.path.startswith(LOOKUP_PATH)]
    assert lookups[0].params == {"referenceId": leg_request_id(decision, 1, 0)}


def test_5xx_not_processed_is_unknown_after_60s_of_lookups(fake, make_executor, approve, ledger, fclock):
    fake.script_open("http_5xx_not_processed")
    decision = approve()
    report = make_executor().execute(decision, plan_of(open_leg(1, "SPX500"), open_leg(2, "GOLD")), nav_usd=NAV)
    assert report.final_state == "execution_unknown"
    assert ledger.get_leg(decision, 1).state == "unknown"
    assert len(_open_posts(fake)) == 1                     # no resubmit, no second leg
    assert sum(fclock.slept) == pytest.approx(60)
    assert not fake.orders


def test_lost_202_is_found_by_reference_id(fake, make_executor, approve, ledger):
    fake.script_open("lost_202")
    decision = approve()
    report = make_executor().execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "completed"
    assert ledger.get_leg(decision, 1).order_id == next(iter(fake.orders))
    assert len(_open_posts(fake)) == 1


def test_timeout_without_processing_is_unknown(fake, make_executor, approve):
    fake.script_open("timeout_not_processed")
    report = make_executor().execute(approve(), plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "execution_unknown"
    assert len(_open_posts(fake)) == 1


# ------------------------------------------------------------------------------ rate limits
def test_write_429_waits_retry_after_and_retries_with_a_new_attempt(fake, make_executor, approve, ledger, fclock):
    fake.script_open("http_429", retry_after=7)
    decision = approve()
    report = make_executor().execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "completed"
    posts = _open_posts(fake)
    assert [p.headers["x-request-id"] for p in posts] == [
        leg_request_id(decision, 1, 0), leg_request_id(decision, 1, 1)
    ]
    assert 7.0 in fclock.slept
    assert ledger.get_leg(decision, 1).attempt == 1
    assert len(fake.orders) == 1


def test_shared_execution_quota_exhausted_is_waited_out(fake, make_executor, approve, fclock):
    fake.consume_execution_quota()
    report = make_executor().execute(approve(), plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "completed"
    assert max(fclock.slept) >= 59


def test_429_on_lookup_reads_honours_retry_after(fake, make_executor, approve, fclock):
    fake.script_open("fill", after_lookups=1)
    fake.inject("GET", LOOKUP_PATH, 429, retry_after=3)
    report = make_executor().execute(approve(), plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "completed"
    assert 3.0 in fclock.slept


# ------------------------------------------------------------------------------ crash + resume
def test_crash_between_submit_and_persist_is_found_by_resume(fake, make_executor, approve, ledger):
    fake.script_open("crash_after_processing")
    decision = approve()
    with pytest.raises(SimulatedCrash):
        make_executor().execute(decision, plan_of(open_leg(1, "SPX500"), open_leg(2, "GOLD")), nav_usd=NAV)
    assert ledger.get_decision(decision).state == "executing"
    leg = ledger.get_leg(decision, 1)
    assert leg.state == "submitting" and leg.request_id == leg_request_id(decision, 1, 0)

    report = make_executor(write=None).resume(decision)
    assert ledger.get_leg(decision, 1).state == "filled"
    assert ledger.get_leg(decision, 2).state == "skipped"     # never sent; needs a fresh approval
    assert report.final_state == "completed_partial"
    assert len(_open_posts(fake)) == 1                        # resume sent nothing


class _CrashBeforeSend:
    """A write client whose process dies before the request leaves the machine."""

    def open_order(self, **_):
        raise SimulatedCrash("died before sending")


def test_crash_before_send_resume_marks_leg_skipped(fake, make_executor, approve, ledger):
    decision = approve()
    with pytest.raises(SimulatedCrash):
        make_executor(write=_CrashBeforeSend()).execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    report = make_executor(write=None).resume(decision)
    assert ledger.get_leg(decision, 1).state == "skipped"
    assert report.final_state == "completed_partial"
    assert not fake.orders


class _ExplodingWrite:
    def __getattr__(self, name):
        raise AssertionError(f"resume must never call the writer ({name})")


def test_resume_of_unknown_resolves_by_lookup_without_sending(fake, make_executor, approve, ledger):
    script = fake.script_open("in_flight")
    decision = approve()
    assert make_executor().execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV).final_state == "execution_unknown"
    script.outcome = "fill"                     # the broker finally fills it
    fake.orders[next(iter(fake.orders))].resolved = False
    report = make_executor(write=_ExplodingWrite()).resume(decision)
    assert report.final_state == "completed"
    assert ledger.get_leg(decision, 1).state == "filled"
    assert not ledger.has_blocker()


def test_resume_still_in_flight_stays_unknown(fake, make_executor, approve, ledger):
    fake.script_open("in_flight")
    decision = approve()
    make_executor().execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    report = make_executor(write=_ExplodingWrite()).resume(decision)
    assert report.final_state == "execution_unknown"
    assert ledger.get_decision(decision).state == "execution_unknown"
    assert ledger.has_blocker()


def test_resume_refuses_a_decision_that_is_not_executing(make_executor, approve):
    with pytest.raises(ExecutionError):
        make_executor().resume(approve())


def test_execution_lock_is_exclusive(make_executor, approve, tmp_path):
    import fcntl

    lock = tmp_path / "state" / "exec.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        with pytest.raises(ExecutionLocked):
            make_executor().execute(approve(), plan_of(open_leg(1, "SPX500")), nav_usd=NAV)


# ------------------------------------------------------------------------------ post-fill check
def test_post_fill_units_mismatch_blocks_and_stops(fake, make_executor, approve, ledger):
    fake.script_open("fill", units_factor=1.2)
    decision = approve()
    report = make_executor().execute(decision, plan_of(open_leg(1, "SPX500"), open_leg(2, "GOLD")), nav_usd=NAV)
    assert report.final_state == "blocked"
    assert ledger.get_leg(decision, 2).state == "skipped"
    assert len(_open_posts(fake)) == 1
    assert ledger.has_blocker()


def test_post_fill_broker_exposure_mismatch_blocks(fake, make_executor, approve):
    fake.script_open("fill", exposure_factor=2.0)       # e.g. exposure reported with leverage applied
    report = make_executor().execute(approve(), plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "blocked"


def test_post_fill_within_tolerance_passes_and_just_outside_fails(fake, make_executor, approve):
    fake.script_open("fill", price_factor=1.04)          # 4% slippage < 5% tolerance
    assert make_executor().execute(approve("d1"), plan_of(open_leg(1, "SPX500")), nav_usd=NAV).final_state == "completed"
    fake.script_open("fill", price_factor=1.06)          # 6% > 5%
    assert make_executor().execute(approve("d2"), plan_of(open_leg(1, "GOLD")), nav_usd=NAV).final_state == "blocked"


# ------------------------------------------------------------------------------ closes
def test_closes_run_before_opens_and_units_rederive_from_fresh_equity(fake, make_executor, approve, ledger):
    pos = fake.add_position("NSDQ100", units=10, sl_rate=150.0)
    decision = approve()
    plan = plan_of(close_leg(1, pos, weight_before=0.1), open_leg(2, "SPX500", units=10, nav=2 * NAV))
    report = make_executor().execute(decision, plan, nav_usd=2 * NAV)   # equity ~0.6x approval NAV
    assert report.final_state == "completed"
    writes = [r for r in fake.requests if r.method == "POST" and "/execution/" in r.path]
    assert writes[0].path.startswith(CLOSE_PATH) and writes[1].path == OPEN_PATH
    sent = writes[1].body["units"]
    assert sent == pytest.approx(10 * fake.equity() / (2 * NAV), rel=1e-3)
    assert sent < 10


def test_open_units_never_exceed_approved_times_1_02(fake, make_executor, approve):
    make_executor().execute(approve(), plan_of(open_leg(1, "SPX500", units=10)), nav_usd=NAV / 2)
    (post,) = _open_posts(fake)
    assert post.body["units"] == pytest.approx(10.2)          # equity is 2x the approval NAV


def test_open_units_floor_to_whole_units_when_required(fake, make_executor, approve):
    make_executor(whole_units={"SPX500": True}).execute(
        approve(), plan_of(open_leg(1, "SPX500", units=10)), nav_usd=NAV / 2
    )
    (post,) = _open_posts(fake)
    assert post.body["units"] == 10.0


def test_open_units_floor_to_whole_units_from_the_leg(fake, make_executor, approve, ledger):
    leg = open_leg(1, "SPX500", units=10).model_copy(update={"whole_units": True})
    decision = approve()
    make_executor().execute(decision, plan_of(leg), nav_usd=NAV / 2)
    (post,) = _open_posts(fake)
    assert post.body["units"] == 10.0                       # 10.2 floored because the leg says so
    assert ledger.get_leg(decision, 1).detail["whole_units"] is True


def test_leg_line_is_stored_and_reported(fake, make_executor, approve, ledger):
    leg = open_leg(1, "SPX500").model_copy(update={"line": "SPX"})
    decision = approve()
    report = make_executor().execute(decision, plan_of(leg), nav_usd=NAV)
    assert ledger.get_leg(decision, 1).line == "SPX" and report.legs[0].line == "SPX"
    assert report.final_state == "completed"


def test_partial_close_deducts_units(fake, make_executor, approve, ledger):
    pos = fake.add_position("SPX500", units=10, sl_rate=90.0)
    decision = approve()
    report = make_executor().execute(decision, plan_of(close_leg(1, pos, units=4, weight_before=0.1, weight_after=0.06)), nav_usd=NAV)
    assert report.final_state == "completed"
    (close,) = [r for r in fake.requests if r.path.startswith(CLOSE_PATH)]
    assert close.body == {"InstrumentId": 101, "UnitsToDeduct": 4.0}
    assert fake.positions[pos.position_id].units == pytest.approx(6)


@pytest.mark.parametrize("outcome", ["http_5xx_processed", "lost_200"])
def test_ambiguous_close_is_confirmed_by_portfolio_reread(fake, make_executor, approve, ledger, outcome):
    pos = fake.add_position("SPX500", units=10, sl_rate=90.0)
    fake.script_close(outcome)
    decision = approve()
    report = make_executor().execute(decision, plan_of(close_leg(1, pos, weight_before=0.1)), nav_usd=NAV)
    assert report.final_state == "completed"
    assert fake.count("POST", CLOSE_PATH) == 1


def test_ambiguous_close_not_processed_is_unknown(fake, make_executor, approve, ledger):
    pos = fake.add_position("SPX500", units=10, sl_rate=90.0)
    fake.script_close("http_5xx_not_processed")
    decision = approve()
    plan = plan_of(close_leg(1, pos, weight_before=0.1), open_leg(2, "GOLD"))
    report = make_executor().execute(decision, plan, nav_usd=NAV)
    assert report.final_state == "execution_unknown"
    assert fake.count("POST", CLOSE_PATH) == 1
    assert not _open_posts(fake)


def test_close_in_flight_confirms_after_info_lookups(fake, make_executor, approve):
    pos = fake.add_position("SPX500", units=10, sl_rate=90.0)
    fake.script_close("close", after_lookups=2)
    report = make_executor().execute(approve(), plan_of(close_leg(1, pos, weight_before=0.1)), nav_usd=NAV)
    assert report.final_state == "completed"


def test_rejected_close_stops_opens(fake, make_executor, approve, ledger):
    pos = fake.add_position("SPX500", units=10, sl_rate=90.0)
    fake.script_close("http_4xx")
    decision = approve()
    plan = plan_of(close_leg(1, pos, weight_before=0.1), open_leg(2, "GOLD"))
    report = make_executor().execute(decision, plan, nav_usd=NAV)
    assert report.final_state == "completed_partial"
    assert [leg.state for leg in ledger.legs(decision)] == ["rejected", "skipped"]
    assert not _open_posts(fake)


def test_flip_open_depends_on_its_close(fake, make_executor, approve, ledger):
    pos = fake.add_position("SPX500", units=10, sl_rate=90.0)
    decision = approve()
    plan = plan_of(
        close_leg(1, pos, weight_before=0.1),
        open_leg(2, "SPX500", direction="short", depends_on=(1,)),
    )
    report = make_executor().execute(decision, plan, nav_usd=NAV)
    assert report.final_state == "completed"
    (short,) = fake.positions_for("SPX500")
    assert not short.is_buy and short.sl_rate == pytest.approx(100.0 * 1.1)
    assert _open_posts(fake)[0].body["transaction"] == "sellShort"


def test_stop_hit_before_execution_skips_close_and_stops_opens(fake, make_executor, approve, ledger):
    pos = fake.add_position("SPX500", units=10, sl_rate=95.0)
    fake.move_price("SPX500", -0.06)                    # bid 94 < stop 95: the broker stop fires
    assert fake.stop_hits == [pos.position_id]
    decision = approve()
    report = make_executor().execute(decision, plan_of(close_leg(1, pos, weight_before=0.1), open_leg(2, "GOLD")), nav_usd=NAV)
    assert report.final_state == "completed_partial"
    assert not _open_posts(fake)


# ------------------------------------------------------------------------------ SL + reconcile
def test_modify_stop_loss_leg_is_confirmed_by_reread(fake, make_executor, approve, ledger):
    pos = fake.add_position("SPX500", units=10, sl_rate=90.0)
    decision = approve()
    leg = Leg(
        seq=1, kind="modify_sl", symbol="SPX500", instrument_id=101, direction="long",
        weight_before=0.1, weight_after=0.1, risk_increasing=False, reason="tighten stop",
        sl_rate=92.0, position_id=pos.position_id,
    )
    report = make_executor().execute(decision, plan_of(leg), nav_usd=NAV)
    assert report.final_state == "completed"
    assert fake.positions[pos.position_id].sl_rate == 92.0
    (patch,) = [r for r in fake.requests if r.method == "PATCH"]
    assert patch.body == {"stopLossRate": 92.0, "stopLossType": "fixed"}


def test_existing_position_without_stop_blocks_reconcile(fake, make_executor, approve):
    fake.add_position("GOLD", units=10, sl_rate=None)
    report = make_executor().execute(approve(), plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "blocked"
    assert report.reconcile is not None and report.reconcile.missing_sl == ["GOLD"]


def test_unknown_instrument_position_blocks_reconcile(fake, make_executor, approve):
    fake.add_instrument("MYSTERY", 999, bid=10.0, ask=10.01)
    fake.add_position("MYSTERY", units=5, sl_rate=9.0)
    report = make_executor(symbol_for={}).execute(approve(), plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "blocked"
    assert report.reconcile is not None and report.reconcile.unknown_positions == ["UNMAPPED_999"]


def test_flatten_plan_closes_everything_including_unmapped(fake, make_executor, approve, ledger, read_client, fclock, policy):
    fake.add_instrument("MYSTERY", 999, bid=10.0, ask=10.01)
    fake.add_position("MYSTERY", units=5, sl_rate=9.0)
    for _ in range(3):
        fake.add_position("SPX500", units=2, sl_rate=90.0)
        fake.add_position("NSDQ100", units=1, sl_rate=180.0)
        fake.add_position("GOLD", units=3, is_buy=False, sl_rate=55.0)
    known = {iid: sym for sym, (iid, _b, _a) in INSTRUMENTS.items()}
    snap = snapshot_from_portfolio(parse_pnl(read_client.pnl(), known), fclock.now())
    plan = build_flatten_plan(snapshot=snap, quotes={}, eligibility={}, nav_usd=snap.equity_usd, policy=policy)
    assert len(plan.legs) == 10 > policy.risk["proposal"]["max_legs"]
    decision = approve(kind="flatten")
    report = make_executor(symbol_for=known).execute(decision, plan, nav_usd=snap.equity_usd)
    assert report.final_state == "completed", report.reasons
    assert fake.positions == {}
    assert {row.line for row in ledger.legs(decision)} == {"SPX", "NDX", "GOLD", "UNMAPPED_999"}


def test_legs_written_at_proposal_are_reused(fake, make_executor, approve, ledger):
    plan = plan_of(open_leg(1, "SPX500"))
    decision = approve()
    ledger.insert_legs(decision, plan.legs)                   # what the cycle does at proposal
    report = make_executor().execute(decision, plan, nav_usd=NAV)
    assert report.final_state == "completed" and len(ledger.legs(decision)) == 1


def test_legs_that_differ_from_the_proposal_are_never_sent(fake, make_executor, approve, ledger):
    from council.ledger.db import LedgerError

    decision = approve()
    ledger.insert_legs(decision, [open_leg(1, "SPX500", units=10)])
    with pytest.raises(LedgerError):
        make_executor().execute(decision, plan_of(open_leg(1, "SPX500", units=20)), nav_usd=NAV)
    assert ledger.get_decision(decision).state == "approved"   # nothing started, nothing sent
    assert not _open_posts(fake)


def test_empty_plan_completes(make_executor, approve):
    assert make_executor().execute(approve(), plan_of(), nav_usd=NAV).final_state == "completed"


def test_invalid_open_without_stop_is_never_sent(fake, make_executor, approve, ledger):
    leg = open_leg(1, "SPX500").model_copy(update={"sl_rate": None})
    decision = approve()
    report = make_executor().execute(decision, plan_of(leg), nav_usd=NAV)
    assert ledger.get_leg(decision, 1).state == "skipped"
    assert report.final_state == "completed_partial"
    assert not _open_posts(fake)


# ------------------------------------------------------------------------------ operator guard
_AGENT_ENV = ("CI", "GITHUB_ACTIONS", "CLAUDECODE", "COUNCIL_AGENT_CONTEXT", "XPC_SERVICE_NAME")


def test_write_executor_refuses_outside_an_operator_terminal(
    fake, write_client, read_client, ledger, limiter, fclock, policy, monkeypatch,
):
    monkeypatch.setattr(guards, "process_ancestors", lambda: ["zsh"])
    with pytest.raises(GuardError, match="COUNCIL_ROLE"):
        Executor(write_client, read_client, ledger, limiter, clock=fclock.now, sleep=fclock.sleep, policy=policy)
    assert fake.requests == []                              # nothing reached the broker


def test_write_executor_runs_the_guard_on_the_real_process_context(
    write_client, read_client, ledger, limiter, fclock, policy, monkeypatch,
):
    calls = []
    monkeypatch.setattr(guards, "assert_operator_context", lambda **kw: calls.append(kw))
    monkeypatch.setattr(guards, "process_ancestors", lambda: ["zsh", "Terminal"])
    Executor(write_client, read_client, ledger, limiter, clock=fclock.now, policy=policy)
    (call,) = calls
    assert call["env"] is os.environ and call["ancestors"] == ["zsh", "Terminal"]
    assert isinstance(call["stdin_isatty"], bool) and isinstance(call["stdout_isatty"], bool)
    Executor(None, read_client, ledger, limiter, clock=fclock.now, policy=policy)
    Executor(write_client, read_client, ledger, limiter, policy=policy, _skip_guard_for_tests=True)
    assert len(calls) == 1                                  # no writer or the test flag: no guard


def test_write_executor_is_allowed_in_an_operator_terminal(
    write_client, read_client, ledger, limiter, fclock, policy, monkeypatch,
):
    class _Tty:
        def isatty(self):
            return True

    for name in list(os.environ):
        if name in _AGENT_ENV or name.startswith("CLAUDE_CODE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("COUNCIL_ROLE", "operator")
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(sys, "stdout", _Tty())
    monkeypatch.setattr(guards, "process_ancestors", lambda: ["-zsh", "login", "Terminal", "launchd"])
    executor = Executor(write_client, read_client, ledger, limiter, clock=fclock.now, policy=policy)
    assert executor.write is write_client
    monkeypatch.setattr(guards, "process_ancestors", lambda: ["python", "node", "zsh"])
    with pytest.raises(GuardError, match="agent runtime"):
        Executor(write_client, read_client, ledger, limiter, clock=fclock.now, policy=policy)


def test_resume_is_writer_free_by_construction(read_client, ledger, limiter, fclock, policy):
    Executor(None, read_client, ledger, limiter, clock=fclock.now, sleep=fclock.sleep, policy=policy)
    with pytest.raises(ExecutionError):
        Executor(None, read_client, ledger, limiter, policy=policy).execute("x", plan_of(), nav_usd=NAV)
