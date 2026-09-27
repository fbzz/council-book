# ruff: noqa: F811  (pytest fixtures imported from test_end_to_end)
"""M5-A: broker-failure containment (V1, V3, V7, V11). Offline: fake broker, local git remote."""

from __future__ import annotations

import importlib
import json
import re
from datetime import timedelta

import pytest

from council.cycle import run_cycle
from council.publish.gitops import Publisher
from council.settings import Settings
from council.watch import run_watch
from tests.integration.test_end_to_end import (  # noqa: F401  (fixtures)
    API_KEY,
    NOW,
    READ_KEY,
    WRITE_KEY,
    _ctx,
    fake_broker,
    remote_clone,
)


class Notifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, title: str, text: str, priority: str = "default") -> None:
        self.sent.append((title, text, priority))

    def urgent(self, needle: str) -> list[str]:
        return [t for _title, t, p in self.sent if p == "urgent" and needle in t]


def _read(fake, fclock):
    from council.broker.etoro_read import EtoroReadClient

    return EtoroReadClient(API_KEY, READ_KEY, transport=fake.transport(), sleep=fclock.sleep)


def _ops_rows(clone) -> list[dict]:
    path = clone / "journal" / "ops" / "cycles.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


# ------------------------------------------------------------------------------ V1
def test_v1_expired_token_skips_the_cycle_with_an_ops_row_and_one_urgent(tmp_path, remote_clone, fake_broker):
    fake, fclock = fake_broker
    fake.inject("GET", "/api/v1/trading/info/real/pnl", status=401, times=50)
    note = Notifier()
    ctx = _ctx(tmp_path, broker=_read(fake, fclock), publisher=Publisher(remote_clone, push=True),
               clock=fclock.now)
    ctx.notifier = note
    out = run_cycle(ctx)
    assert out.status == "skipped_broker" and "broker_error:auth" in out.flags
    assert ctx.ledger.get_cycle(out.cycle_id)["status"] == "skipped_broker"
    rows = _ops_rows(remote_clone)
    assert rows and rows[-1]["status"] == "skipped_broker"
    assert not (remote_clone / "journal" / "status.json").exists()     # status never touched
    assert len(note.urgent("broker_error:auth")) == 1

    fclock.advance(3600)                                               # an hour later: no 2nd URGENT
    w = run_watch(ctx)
    assert w.broker_error == "broker_error:auth"
    assert len(note.urgent("broker_error:auth")) == 1
    fclock.advance(4 * 3600)                                           # after 4 h: one more
    run_watch(ctx)
    assert len(note.urgent("broker_error:auth")) == 2


def test_watch_heartbeat_still_runs_when_broker_checks_fail(tmp_path, fake_broker):
    fake, fclock = fake_broker
    fake.inject("GET", "/api/v1/trading/info/real/pnl", status=500, times=50)
    ctx = _ctx(tmp_path, broker=_read(fake, fclock), clock=fclock.now)
    ctx.notifier = Notifier()
    ctx.ledger.set_runtime("last_cycle", {"cycle_id": "2026-09-30T1440Z",
                                          "at": (NOW - timedelta(days=1)).isoformat()})
    w = run_watch(ctx)
    assert w.broker_error is not None and w.broker_error.startswith("broker_error:")
    assert any("no completed cycle" in a for a in w.alerts)


# ------------------------------------------------------------------------------ V3 / G4
def test_v3_onboarded_live_without_broker_is_skipped_broker_never_awaiting(tmp_path, remote_clone):
    ctx = _ctx(tmp_path, publisher=Publisher(remote_clone, push=True))
    ctx.settings = Settings(role="dev", mode="live")
    (ctx.state_dir / "account").mkdir()
    (ctx.state_dir / "account" / "onboarded.json").write_text("{}")
    note = Notifier()
    ctx.notifier = note
    out = run_cycle(ctx)
    assert out.status == "skipped_broker" and "keychain_unavailable" in out.flags
    assert not (remote_clone / "journal" / "status.json").exists()
    assert len(note.urgent("keychain_unavailable")) == 1
    w = run_watch(ctx)
    assert w.broker_error == "keychain_unavailable"
    assert len(note.urgent("keychain_unavailable")) == 1               # rate-limited across both


def test_not_onboarded_stays_awaiting_account(tmp_path):
    from council.context import broker_expected

    ctx = _ctx(tmp_path)
    ctx.settings = Settings(role="dev", mode="live")
    assert not broker_expected(ctx)


# ------------------------------------------------------------------------------ V11
def test_v11_redact_error_is_flagged_not_committed_and_not_approvable(tmp_path, remote_clone, fake_broker,
                                                                      monkeypatch):
    from council.publish import redact

    fake, fclock = fake_broker
    note = Notifier()
    ctx = _ctx(tmp_path, broker=_read(fake, fclock), publisher=Publisher(remote_clone, push=True),
               clock=fclock.now)
    ctx.notifier = note

    def boom(*_a, **_k):
        raise ValueError("injected")

    monkeypatch.setattr(redact, "public_cycle", boom)
    out = run_cycle(ctx)
    assert "redact_error:ValueError" in out.flags
    assert not list((remote_clone / "journal").rglob("commitments/*"))
    assert len(note.urgent("redact_error")) == 1
    assert "redact_error:ValueError" in ctx.ledger.get_cycle(out.cycle_id)["flags"]
    assert any(r["cycle_id"] == out.cycle_id for r in _ops_rows(remote_clone))
    assert out.decision_id, "the fixture must produce a decision, or this test proves nothing"
    d = ctx.ledger.get_decision(out.decision_id)
    assert d.state == "awaiting_publication" and not d.published_commit
    from council.operator.approve import ApprovalDeps, ApprovalRefused, approve

    def no_writes():
        raise AssertionError("approve must refuse before any write client is built")

    deps = ApprovalDeps(ledger=ctx.ledger, policy=ctx.policy, read=ctx.sources.broker,
                        write_factory=no_writes, state_dir=ctx.state_dir,
                        input_fn=lambda _p: "", print_fn=lambda _m: None, now_fn=fclock.now,
                        guard_fn=lambda: None)
    with pytest.raises(ApprovalRefused):
        approve(out.decision_id, deps)
    monkeypatch.undo()
    fclock.advance(4 * 3600)
    nxt = run_cycle(ctx)
    assert nxt.cycle_id != out.cycle_id and nxt.status in ("on_time", "late")


# ------------------------------------------------------------------------------ V7 / G14
def test_v7_halt_flatten_is_keyed_by_minute_and_the_watch_survives(tmp_path, remote_clone, fake_broker,
                                                                   monkeypatch):
    from council.operator.approve import ApprovalDeps, approve

    fake, fclock = fake_broker
    fake.add_position("BTC", is_buy=True, units=0.02, leverage=1, sl_rate=40_000.0, settlement="real")
    read = _read(fake, fclock)
    ctx = _ctx(tmp_path, broker=read, publisher=Publisher(remote_clone, push=True), clock=fclock.now)
    run_watch(ctx)
    fake.credit -= 0.40 * fake.equity()
    fclock.advance(900)
    run_watch(ctx)
    fclock.advance(900)
    run_watch(ctx)
    pending = [d for d in ctx.ledger.pending() if d.kind == "flatten"]
    assert pending, "HALT must issue a flatten proposal"
    flat = pending[0]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d{4}Z-flatten", flat.decision_id)
    assert flat.cycle_id is None and flat.target["decision_ref"] == flat.decision_id

    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    etoro_write = importlib.import_module("council.broker.etoro_write")
    deps = ApprovalDeps(
        ledger=ctx.ledger, policy=ctx.policy, read=read,
        write_factory=lambda: etoro_write.EtoroWriteClient(API_KEY, WRITE_KEY, transport=fake.transport()),
        state_dir=ctx.state_dir, input_fn=lambda p: re.search(r"Type (\S+) to approve", p).group(1),
        print_fn=lambda *_: None, now_fn=fclock.now, guard_fn=lambda: None,
        executor_kwargs={"clock": fclock.now, "sleep": fclock.sleep, "_skip_guard_for_tests": True},
    )
    approve(flat.decision_id, deps)
    payload = ctx.ledger.get_runtime(f"exec_report:{flat.decision_id}")
    assert payload["cycle_id"] is None and payload["decision_ref"] == flat.decision_id
    for _ in range(2):                                    # the next watches do not crash
        fclock.advance(900)
        w = run_watch(ctx)
        assert flat.decision_id not in w.executions_published   # publication itself: M5-N


def test_a_bad_execution_record_is_flagged_and_retried(tmp_path):
    ctx = _ctx(tmp_path)
    note = Notifier()
    ctx.notifier = note
    ctx.ledger.set_runtime("execution_reports", ["bad-1"])
    ctx.ledger.set_runtime("exec_report:bad-1", {"report": {"nonsense": True}, "cycle_id": "2026-10-01T1440Z",
                                                 "nav_usd": 1.0})
    w = run_watch(ctx)
    assert w.executions_published == []
    assert note.urgent("execution_unpublished:")
    assert "bad-1" not in ctx.ledger.get_runtime("executions_published", [])


# ------------------------------------------------------------------------------ token expiry
@pytest.mark.parametrize("days,needle", [(2, "URGENT"), (10, "plan its renewal"), (60, None)])
def test_daily_token_expiry_check(tmp_path, fake_broker, monkeypatch, days, needle):
    fake, fclock = fake_broker
    read = _read(fake, fclock)
    expiry = (NOW + timedelta(days=days, hours=1)).isoformat()
    monkeypatch.setattr(read, "agent_portfolios", lambda: {"agentPortfolios": [{"tokenExpiresAt": expiry}]})
    ctx = _ctx(tmp_path, broker=read, clock=fclock.now)
    alerts = run_watch(ctx).alerts
    hits = [a for a in alerts if "token expires" in a]
    if needle is None:
        assert not hits
    else:
        assert hits and needle in hits[0]
    fclock.advance(900)
    assert not [a for a in run_watch(ctx).alerts if "token expires" in a]   # once per UTC day
