"""Operator incident commands (m5-readiness M5-B, rehearsal rows V8 and V10).

- V8: a fill without a stop-loss ends `blocked` → `resume_exec` (0 writes) → `review_blocked`
  with a reason → no blocker is left, so the next cycle runs.
- V10: a lost 202 on an open whose lookups also fail ends `execution_unknown` → `resume_exec`
  resolves it by lookup, with 0 writes.
- `review_blocked` refuses while a leg is active ("run resume-exec") or waiting ("use ops
  resolve") and records its reason for publication; `resume_kill_switch` refuses without a reason
  and from NORMAL, and the next check re-halts while equity is below the halt line.
FakeEtoro counts every POST, PUT, PATCH and DELETE it receives; the library functions run with
`guard_fn` stubbed (the CLI guard matrix lives in tests/cli/test_operator_commands.py).
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from council.execution.executor import NoWriteClient, WriteRefused
from council.operator.approve import (
    OPS_ROWS_PENDING,
    ApprovalDeps,
    ApprovalRefused,
    resume_exec,
    resume_kill_switch,
    review_blocked,
)
from tests.execution.helpers import INSTRUMENTS, NAV, open_leg, plan_of

LOOKUP_PATH = "/api/v2/trading/info/orders"
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def writes(fake) -> int:
    return sum(1 for r in fake.requests if r.method in WRITE_METHODS)


def _no_writer():
    raise AssertionError("an operator recovery command built a broker writer")


@pytest.fixture
def deps(ledger, read_client, policy, fclock, tmp_path):
    out: list[str] = []
    d = ApprovalDeps(
        ledger=ledger, policy=policy, read=read_client, write_factory=_no_writer,
        state_dir=tmp_path / "state", print_fn=out.append, now_fn=fclock.now, guard_fn=lambda: None,
        executor_kwargs={"clock": fclock.now, "sleep": fclock.sleep,
                         "symbol_for": {iid: sym for sym, (iid, _b, _a) in INSTRUMENTS.items()}},
    )
    d.out = out  # type: ignore[attr-defined]
    return d


def _drop_stop_on_fill(fake, monkeypatch) -> None:
    """The broker fills the open but the position carries no stop-loss."""
    real = fake._resolve_open

    def resolve(order):
        real(order)
        if order.position_id in fake.positions:
            fake.positions[order.position_id].sl_rate = None

    monkeypatch.setattr(fake, "_resolve_open", resolve)


# ------------------------------------------------------------------------------ V8
def test_v8_fill_without_a_stop_blocks_then_resume_exec_then_review_clears(
        fake, make_executor, approve, ledger, deps, monkeypatch):
    _drop_stop_on_fill(fake, monkeypatch)
    decision = approve()
    report = make_executor().execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "blocked" and ledger.has_blocker()

    before = writes(fake)
    resumed = resume_exec(decision, deps)
    assert writes(fake) == before and resumed.writes_sent == 0          # lookups only
    assert ledger.get_decision(decision).state == "blocked"             # only a review clears it
    assert any("ops review" in line for line in deps.out)

    review_blocked(decision, "checked the broker: stop set by hand, position intact", deps)
    assert ledger.get_decision(decision).state == "reviewed_no_action"
    assert not ledger.has_blocker()                                     # the next cycle runs
    assert writes(fake) == before
    events = ledger.events(decision)
    assert any("operator review: checked the broker" in str(e) for e in events)
    assert "2026-10-01T1440Z" in ledger.get_runtime(OPS_ROWS_PENDING, [])   # queued for the watch


# ------------------------------------------------------------------------------ V10
def test_v10_lost_202_is_unknown_then_resume_exec_resolves_it_by_lookup(
        fake, make_executor, approve, ledger, deps):
    fake.script_open("lost_202")
    fake.inject("GET", LOOKUP_PATH, 503, times=10_000)      # every lookup fails during execution
    decision = approve()
    report = make_executor().execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "execution_unknown"
    assert writes(fake) == 1                                # the one open was sent
    fake._injections.clear()                                # the lookups work again

    before = writes(fake)
    resumed = resume_exec(decision, deps)
    assert writes(fake) == before and resumed.writes_sent == 0
    assert resumed.final_state == "completed"
    assert ledger.get_leg(decision, 1).state == "filled"
    assert not ledger.has_blocker()
    stored = ledger.get_runtime(f"exec_report:{decision}")
    assert stored["report"]["final_state"] == "completed"   # the resumed outcome is what gets published
    assert decision in ledger.get_runtime("execution_reports", [])


def test_resume_of_a_blocked_decision_never_narrows_its_blocker_to_the_satellite(
        fake, make_executor, approve, ledger, deps, monkeypatch):
    """A blocked decision whose resumed run would end waiting (a satellite stock order held for
    its market) stays blocked AND keeps its full blocker: only an operator review clears it."""
    from council.execution.executor import WAITING_STATE, Executor

    _drop_stop_on_fill(fake, monkeypatch)
    decision = approve()
    make_executor().execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert ledger.get_decision(decision).state == "blocked"
    monkeypatch.setattr(Executor, "_final_state", lambda self, run, rec: WAITING_STATE)
    monkeypatch.setattr(Executor, "_blocker_scope", lambda self, decision_id: "satellite")
    resume_exec(decision, deps)
    assert ledger.get_decision(decision).state == "blocked"
    assert ledger.get_decision(decision).blocker_scope in (None, "all")
    assert decision in ledger.blockers()                    # the whole book stays held


def test_resume_exec_refuses_a_decision_with_nothing_to_resume(approve, deps):
    with pytest.raises(ApprovalRefused, match="nothing to resume"):
        resume_exec(approve(), deps)


def test_resume_runs_with_a_no_write_client_even_when_built_with_a_writer(
        fake, make_executor, approve, ledger):
    fake.script_open("in_flight")
    decision = approve()
    executor = make_executor()
    executor.execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    seen: list[object] = []
    real = executor._recover

    def spy(run, leg, row):
        seen.append(executor.write)
        return real(run, leg, row)

    executor._recover = spy  # type: ignore[method-assign]
    before = writes(fake)
    executor.resume(decision)
    assert seen and all(isinstance(w, NoWriteClient) for w in seen)
    assert not isinstance(executor.write, NoWriteClient)    # restored afterwards
    assert writes(fake) == before


@pytest.mark.parametrize("method", ["open_order", "close_position", "patch_stop_loss", "cancel_order"])
def test_no_write_client_refuses_every_write(method):
    with pytest.raises(WriteRefused):
        getattr(NoWriteClient(), method)(request_id="r", order_id=1)
    assert NoWriteClient.CANCEL_ROUTE_VERIFIED is False


# ------------------------------------------------------------------------------ ops review
def test_review_refuses_while_a_leg_is_active(fake, make_executor, approve, ledger, deps):
    fake.script_open("in_flight")
    decision = approve()
    assert make_executor().execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV).final_state == "execution_unknown"
    with pytest.raises(ApprovalRefused, match="run resume-exec"):
        review_blocked(decision, "looked at the broker", deps)
    assert ledger.get_decision(decision).state == "execution_unknown"


def test_review_refuses_while_a_leg_waits_for_its_market(approve, ledger, deps, fclock):
    decision = approve()
    now = fclock.now()
    ledger.insert_legs(decision, plan_of(open_leg(1, "SPX500")).legs, line_of=lambda s: s, now=now)
    ledger.transition(decision, "executing", "test", actor="executor", now=now)
    ledger.update_leg(decision, 1, state="submitting", request_id="r1", attempt=0, submitted_at=now, now=now)
    ledger.update_leg(decision, 1, state="submitted", order_id=1, now=now)
    ledger.update_leg(decision, 1, state="waiting_for_market", now=now)
    ledger.transition(decision, "blocked", "held order timed out", actor="executor", now=now)
    with pytest.raises(ApprovalRefused, match="use ops resolve"):
        review_blocked(decision, "looked at the broker", deps)
    assert ledger.get_decision(decision).state == "blocked"


def test_review_needs_a_reason_and_a_blocked_decision(approve, deps):
    decision = approve()
    with pytest.raises(ApprovalRefused, match="reason is required"):
        review_blocked(decision, "   ", deps)
    with pytest.raises(ApprovalRefused, match="clears only blocked"):
        review_blocked(decision, "checked", deps)


def test_review_refuses_a_reason_that_would_not_publish_as_typed(approve, ledger, deps, fclock):
    decision = approve()
    ledger.transition(decision, "blocked", "test", actor="executor", now=fclock.now())
    for bad in ("paid $1,234 in fees", "order 123456789 was fine", "see https://example.com"):
        with pytest.raises(ApprovalRefused, match="the reason is published"):
            review_blocked(decision, bad, deps)
    assert ledger.get_decision(decision).state == "blocked"


def test_review_writes_the_final_outcome_on_the_cycle_record(approve, ledger, deps, fclock):
    decision = approve()
    cycle_id = "2026-10-01T1440Z"
    ledger.record_cycle({"cycle_id": cycle_id, "slot": "2026-10-01T14:40:00+00:00", "status": "ok",
                         "decision_id": decision, "decision_state": "awaiting_publication"})
    ledger.transition(decision, "blocked", "test", actor="executor", now=fclock.now())
    review_blocked(decision, "position checked in the broker; nothing to do", deps)
    rec = ledger.get_cycle(cycle_id)
    assert rec["decision_state"] == "reviewed_no_action"
    assert rec["decision_reason"] == "operator review: position checked in the broker; nothing to do"


# ------------------------------------------------------------------------------ resume (kill switch)
def test_resume_refuses_without_a_reason_and_from_normal(ledger, deps):
    ledger.set_runtime("kill_state", "HALTED")
    with pytest.raises(ApprovalRefused, match="reason is required"):
        resume_kill_switch("  ", deps)
    ledger.set_runtime("kill_state", "NORMAL")
    with pytest.raises(ApprovalRefused, match="nothing to resume"):
        resume_kill_switch("recovered", deps)
    ledger.set_runtime("kill_state", "WARN")
    with pytest.raises(ApprovalRefused, match="nothing to resume"):
        resume_kill_switch("recovered", deps)


def test_resume_then_the_next_check_halts_again_below_the_halt_line(
        fake, ledger, deps, policy, fclock, read_client, tmp_path):
    from council.cycle import _snapshot_and_kill
    from council.risk.config import risk_limits
    from council.risk.nav import NavState

    cfg = risk_limits(policy).killswitch
    now = fclock.now()
    fake.add_position("GOLD", units=1, sl_rate=1.0)
    peak = NAV * 4                                          # equity is far below the halt line
    ledger.set_runtime("nav_state", NavState(first_equity=peak, peak=peak, last=NAV,
                                             updated_at=now - timedelta(hours=1)).model_dump(mode="json"))
    gap = timedelta(seconds=float(cfg.confirm_gap_s) + 60)
    reads = [[(now - gap * k).isoformat(), NAV] for k in range(int(cfg.confirm_reads), 0, -1)]
    ledger.set_runtime("equity_reads", reads)
    ledger.set_runtime("kill_state", "HALTED")

    assert resume_kill_switch("broker checked, flatten done", deps) == "NORMAL"
    assert ledger.get_runtime("kill_state") == "NORMAL"
    assert ledger.get_runtime("kill_resumes")[-1]["reason"] == "broker checked, flatten done"

    ctx = SimpleNamespace(ledger=ledger, policy=policy, sources=SimpleNamespace(broker=read_client),
                          state_dir=tmp_path / "state")
    _snapshot, state, _nav = _snapshot_and_kill(ctx, now)
    assert state == "HALTED"                                # the lifetime peak did not move
    assert writes(fake) == 0
