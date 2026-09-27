"""Stage-1 review: the new corporate-action steps can never break a post-trade finish, a held-order
resolution or a cycle. A failure keeps today's rule (fail closed: an unknown position still blocks)
and leaves a reason or a flag. FakeEtoro only; nothing is approved outside a test executor."""

from __future__ import annotations

from types import SimpleNamespace

from council import cycle, watch
from council.execution.executor import Executor
from council.stocks import corporate
from tests.execution.helpers import INSTRUMENTS, NAV, open_leg, plan_of

SPIN = 950


def _boom(*_a, **_k):
    raise RuntimeError("corporate step broke")


def test_a_failing_corporate_reconcile_keeps_the_plain_reconcile_after_execution(
        fake, write_client, read_client, ledger, limiter, fclock, approve, sleeve_policy, monkeypatch):
    monkeypatch.setattr(corporate, "reconcile_corporate", _boom)
    fake.add_instrument("SPIN", SPIN, bid=20.0, ask=20.01)
    fake.add_position("SPIN", units=3.0, settlement="real")          # credited by the broker, no stop
    symbol_for = {iid: sym for sym, (iid, _b, _a) in INSTRUMENTS.items()}
    ex = Executor(write_client, read_client, ledger, limiter, clock=fclock.now, sleep=fclock.sleep,
                  policy=sleeve_policy, symbol_for=symbol_for, _skip_guard_for_tests=True)
    report = ex.execute(approve("d-fail"), plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "blocked"                             # the unknown position still blocks
    assert report.reconcile.unknown_positions == [f"UNMAPPED_{SPIN}"]
    assert any("corporate-action reconcile unavailable (RuntimeError)" in r for r in report.reasons)


def test_a_failing_corporate_reconcile_leaves_the_watch_on_the_plain_reconcile(ledger, sleeve_policy, monkeypatch):
    monkeypatch.setattr(corporate, "reconcile_corporate", _boom)
    rec = SimpleNamespace(unknown_positions=["UNMAPPED_950"], missing_sl=["UNMAPPED_950"], protected=False)
    ctx = SimpleNamespace(policy=sleeve_policy, ledger=ledger)
    out, notes = watch._corporate_reconcile(ctx, rec, SimpleNamespace(positions=[]))
    assert out is rec
    assert notes == ["corporate-action reconcile unavailable (RuntimeError); the plain reconcile applies"]


class _BrokenLedger:
    def set_runtime(self, *_a, **_k):
        raise OSError("disk full")


def test_recording_the_stock_sigma_never_stops_a_cycle(sleeve_policy, policy):
    states = {}
    assert cycle.record_stock_sigma_4h(_BrokenLedger(), sleeve_policy, states) == [
        "stock_sigma_record_error:OSError"]
    assert cycle.record_stock_sigma_4h(_BrokenLedger(), policy, states) == []   # core-only: no write at all
