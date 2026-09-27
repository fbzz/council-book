"""Phase-3 follow-up (d): the executor's post-execution reconcile and the watch's reconcile of held
orders adopt `stocks.corporate.reconcile_corporate`, and the watch classifies a vanished stock
position with `classify_vanished_positions` / `vanished_alerts` (design §3.6).

- With a stock sleeve, a pending corporate action (a position no line owns that no leg of ours
  opened) is not an unknown position and its missing stop does not block: the decision completes and
  the reason is recorded (the stock sleeve is held at the next cycle's start). A credited line's
  position without a stop is the warning `credited_no_sl:<line>`. Every other unknown position or
  missing stop still blocks; a core-only book keeps today's rule (an unknown position blocks).
- A vanished stock position is a stop hit (R4d) only near or through its stop; far above it is
  `vanished_not_stop` (URGENT, no cool-off); no data counts as a stop hit; a core line is a stop hit.

FakeEtoro only; nothing is approved outside a test executor."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from council import watch
from council.broker.parsing import PortfolioRead
from council.cycle import STOCK_SIGMA_4H_KEY
from council.execution.executor import Executor
from council.ledger.db import Ledger
from council.models.broker import Position
from council.policy import SLEEVE_FILE, Policy
from tests.conftest import make_sleeve_policy_dir
from tests.execution.helpers import INSTRUMENTS, NAV, open_leg, plan_of

SPIN = 950


@pytest.fixture(scope="module")
def credited_policy(tmp_path_factory) -> Policy:
    """The sleeve fixture with TSTD as a credited, retiring line (what `council stocks adopt` proposes
    for spin-off shares)."""
    root = make_sleeve_policy_dir(tmp_path_factory.mktemp("credited") / "policy")
    path = root / SLEEVE_FILE
    text = path.read_text()
    block = '    role: shortlist\n    sector: "BusEq"\n    cik: "0000900005"\n    rank: 5\n'
    assert block in text
    text = text.replace(block, '    role: retiring\n    sector: "BusEq"\n    cik: "0000900005"\n    rank: null\n')
    marker = '    etoro_symbol: "TSTD"\n    eligibility_checked_at: "2026-11-20T15:02:00Z"\n    credited: null\n'
    assert marker in text
    text = text.replace(marker, marker.replace("credited: null", 'credited: "corporate_action"'))
    path.write_text(text)
    policy = Policy.load(root)
    tstd = policy.universe.by_symbol()["TSTD"]
    assert tstd.stock.credited == "corporate_action" and tstd.stock.role == "retiring"
    return policy


def _executor(write_client, read_client, ledger, limiter, fclock, policy, symbols=None) -> Executor:
    symbol_for = {iid: sym for sym, (iid, _b, _a) in INSTRUMENTS.items()} | dict(symbols or {})
    return Executor(write_client, read_client, ledger, limiter, clock=fclock.now, sleep=fclock.sleep,
                    policy=policy, symbol_for=symbol_for, _skip_guard_for_tests=True)


# ------------------------------------------------------------------------------------ executor
def test_a_pending_credit_does_not_block_the_execution_with_a_sleeve(fake, write_client, read_client, ledger, limiter,
                                                                     fclock, approve, sleeve_policy, policy):
    fake.add_instrument("SPIN", SPIN, bid=20.0, ask=20.01)
    fake.add_position("SPIN", units=3.0, settlement="real")          # credited by the broker, no stop
    decision = approve("d-sleeve")
    ex = _executor(write_client, read_client, ledger, limiter, fclock, sleeve_policy)
    report = ex.execute(decision, plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "completed", report.reasons
    assert report.reconcile.unknown_positions == [] and report.reconcile.missing_sl == []
    assert any(r.startswith("corporate action pending") for r in report.reasons)
    assert not ledger.has_blocker()
    # core-only: today's rule, the unknown position blocks the whole book
    core = _executor(write_client, read_client, ledger, limiter, fclock, policy)
    blocked = core.execute(approve("d-core"), plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert blocked.final_state == "blocked"
    assert blocked.reconcile.unknown_positions == [f"UNMAPPED_{SPIN}"]


def test_another_missing_stop_still_blocks(fake, write_client, read_client, ledger, limiter, fclock, approve,
                                           sleeve_policy):
    fake.add_instrument("SPIN", SPIN, bid=20.0, ask=20.01)
    fake.add_position("SPIN", units=3.0, settlement="real")
    fake.add_position("GOLD", units=10, sl_rate=None)                 # one of the core's, unprotected
    ex = _executor(write_client, read_client, ledger, limiter, fclock, sleeve_policy)
    report = ex.execute(approve(), plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "blocked"
    assert report.reconcile.missing_sl == ["GOLD"] and report.reconcile.unknown_positions == []


def test_a_credited_position_without_a_stop_is_a_warning(fake, write_client, read_client, ledger, limiter, fclock,
                                                         approve, credited_policy):
    fake.add_instrument("TSTD", 960, bid=30.0, ask=30.02)
    fake.add_position("TSTD", units=2.0, settlement="real")          # adopted credit, sold at the next session
    ex = _executor(write_client, read_client, ledger, limiter, fclock, credited_policy, symbols={960: "TSTD"})
    report = ex.execute(approve(), plan_of(open_leg(1, "SPX500")), nav_usd=NAV)
    assert report.final_state == "completed", report.reasons
    assert any(r.startswith("warning credited_no_sl:TSTD") for r in report.reasons)


# ------------------------------------------------------------------------ watch: held-order reconcile
def _portfolio(*positions: Position) -> PortfolioRead:
    return PortfolioRead(credit_usd=NAV, unrealized_pnl_usd=0.0, invested_usd=0.0, equity_usd=NAV,
                         positions=list(positions))


def _pos(pid: int, symbol: str, iid: int, *, sl: float | None, bid: float | None = 20.0, units: float = 3.0,
         is_buy: bool = True) -> Position:
    return Position(position_id=pid, instrument_id=iid, symbol=symbol, is_buy=is_buy, units=units,
                    open_rate=20.0, amount=60.0, sl_rate=sl, settlement="real", exposure_usd=units * 20.0,
                    close_rate=bid)


def test_the_watch_reconcile_of_a_held_order_takes_the_credit_out(tmp_path, fclock, sleeve_policy, policy):
    ledger = Ledger(tmp_path / "ledger.sqlite3", clock=fclock.now)
    port = _portfolio(_pos(7001, f"UNMAPPED_{SPIN}", SPIN, sl=None))
    now = fclock.now()
    final, reasons = watch._final_after_wait(SimpleNamespace(policy=sleeve_policy, ledger=ledger), [], port, now, [])
    assert final == "completed" and any(r.startswith("corporate action pending") for r in reasons)
    final, reasons = watch._final_after_wait(SimpleNamespace(policy=policy, ledger=ledger), [], port, now, [])
    assert final == "blocked"
    assert reasons == [f"missing stop-loss: UNMAPPED_{SPIN}", f"unknown position: UNMAPPED_{SPIN}"]


def test_the_watch_reconcile_warns_on_a_credited_position(tmp_path, fclock, credited_policy):
    ledger = Ledger(tmp_path / "ledger.sqlite3", clock=fclock.now)
    port = _portfolio(_pos(7002, "TSTD", 960, sl=None))
    final, reasons = watch._final_after_wait(SimpleNamespace(policy=credited_policy, ledger=ledger), [], port,
                                             fclock.now(), [])
    assert final == "completed" and any(r.startswith("warning credited_no_sl:TSTD") for r in reasons)


# ------------------------------------------------------------------------ watch: vanished positions
def _watch_ctx(tmp_path: Path, fclock, policy) -> SimpleNamespace:
    return SimpleNamespace(policy=policy, ledger=Ledger(tmp_path / "ledger.sqlite3", clock=fclock.now))


def test_a_vanished_stock_position_is_classified(tmp_path, fclock, sleeve_policy):
    ctx = _watch_ctx(tmp_path, fclock, sleeve_policy)
    ctx.ledger.set_runtime(STOCK_SIGMA_4H_KEY, {"TSTA": 0.02, "TSTB": 0.02})
    held = SimpleNamespace(positions=[
        _pos(501, "TSTB", 301, sl=80.0, bid=131.0),                   # a cash takeover at a premium
        _pos(502, "TSTA", 302, sl=80.0, bid=81.0),                    # next to its stop
        _pos(503, "TSTC.B", 303, sl=80.0, bid=131.0),                 # no sigma stored: ambiguous
        _pos(504, "EQQQ.L", 201, sl=80.0, bid=131.0),                 # a core line keeps today's rule
    ])
    now = fclock.now()
    assert watch._stop_hits(ctx, held, now) == []                    # first sight: observations stored
    stored = ctx.ledger.get_runtime("watch_positions")
    assert stored["501"] == {"symbol": "TSTB", "sl_rate": 80.0, "bid": 131.0}
    alerts = watch._stop_hits(ctx, SimpleNamespace(positions=[]), now + timedelta(minutes=15))
    assert sorted(a for a in alerts if "stop-loss hit" in a) == [
        "URGENT stop-loss hit on NDX", "URGENT stop-loss hit on TSTA", "URGENT stop-loss hit on TSTC_B"]
    (takeover,) = [a for a in alerts if "vanished_not_stop" in a]
    assert takeover.startswith("URGENT vanished_not_stop:TSTB") and "council stocks adopt" in takeover
    hits = ctx.ledger.stop_hits_since(now - timedelta(days=1), universe=sleeve_policy.universe)
    assert set(hits) == {"TSTA", "TSTC_B", "NDX"}                    # no R4d cool-off on TSTB
    assert ctx.ledger.get_runtime("watch_positions") == {}


def test_a_close_of_ours_is_not_a_vanished_position(tmp_path, fclock, sleeve_policy):
    ctx = _watch_ctx(tmp_path, fclock, sleeve_policy)
    from council.models.plan import Leg

    leg = Leg(seq=1, kind="close", symbol="TSTA", line="TSTA", instrument_id=302, direction="long",
              settlement="real", weight_before=0.06, weight_after=0.0, risk_increasing=False, units=3.0,
              amount_usd=60.0, position_id=502)
    ctx.ledger.create_decision(decision_id="d-close", kind="rebalance", valid_until=fclock.now() + timedelta(hours=1))
    ctx.ledger.insert_legs("d-close", [leg])
    ctx.ledger.update_leg("d-close", 1, state="submitting")
    ctx.ledger.update_leg("d-close", 1, state="filled", resolved_at=fclock.now())
    watch._stop_hits(ctx, SimpleNamespace(positions=[_pos(502, "TSTA", 302, sl=80.0, bid=81.0)]), fclock.now())
    assert watch._stop_hits(ctx, SimpleNamespace(positions=[]), fclock.now()) == []


def test_observations_stored_before_the_sleeve_still_read(tmp_path, fclock, policy):
    ctx = _watch_ctx(tmp_path, fclock, policy)
    ctx.ledger.set_runtime("watch_positions", {"601": "BTC", "602": {"no": "symbol"}, "bad": "BTC"})
    alerts = watch._stop_hits(ctx, SimpleNamespace(positions=[]), fclock.now())
    assert alerts == ["URGENT stop-loss hit on BTC"]
