"""Phase-3 follow-up (c): the cycle start runs the corporate-action detection (design §3.6) on the
broker snapshot. A position no line owns that no open leg of ours created (spin-off shares) holds
only the stock sleeve through the satellite-scoped R20 blocker `corporate_action_pending` (a code
with no identifier: it reaches the public record), and the operator gets an URGENT alert naming the
instrument and the command (private: never in the record). The core keeps trading. Offline: stub
LLM, synthetic history, FakeEtoro behind the READ client, no publisher; nothing is approved.
`invariants.STOCK_SLEEVE_LIVE` stays False (the sleeve policy is handed to the context directly)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from council.broker.etoro_read import EtoroReadClient
from council.broker.fake import FakeClock, FakeEtoro, eligibility_row, leverage_config
from council.broker.instruments import InstrumentMap
from council.cycle import STOCK_SIGMA_4H_KEY, run_cycle, stock_sigma_4h
from council.models.plan import Leg
from council.runtime import Sources
from council.stocks import corporate
from tests.integration.test_earnings_cycle import _history_for
from tests.integration.test_end_to_end import (
    API_KEY,
    NOW,
    READ_KEY,
    VEHICLES,
    WRITE_KEY,
    _ctx,
    _no_events,
)

SPIN = 950          # an instrument no line owns (not in the instrument map: the snapshot reads UNMAPPED_950)
STOCKS = ("TSTA", "TSTB", "TSTC_B", "F", "TSTD", "TSTE")
SELECTED = ("TSTA", "TSTB", "TSTC_B", "F")                            # the shortlist's target is 0


class Notifier:
    def __init__(self, refuse: str | None = None) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.refuse = refuse

    def send(self, title, body, priority="default", click_url=None):
        if self.refuse and self.refuse in body:
            raise ValueError("notification refused")
        self.sent.append((title, body, priority))


@pytest.fixture
def world(tmp_path):
    fclock = FakeClock(start=NOW)
    fake = FakeEtoro(clock=fclock.now, credit=10_000.0, write_user_keys={WRITE_KEY})
    for sym, (iid, px, settlement) in VEHICLES.items():
        configs = ([leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,))]
                   if settlement == "real" else [leverage_config(direction="LONG"), leverage_config(direction="SHORT")])
        fake.add_instrument(sym, iid, bid=px * 0.9995, ask=px * 1.0005, row=eligibility_row(sym, iid, configs=configs))
    fake.add_instrument("SPIN", SPIN, bid=19.99, ask=20.01,
                        row=eligibility_row("SPIN", SPIN, configs=[
                            leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,))]))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    InstrumentMap({}, path=state / "instruments.json").merged({s: v[0] for s, v in VEHICLES.items()}, NOW).save()
    read = EtoroReadClient(API_KEY, READ_KEY, transport=fake.transport(), sleep=fclock.sleep)
    return fake, read


def _sleeve_ctx(tmp_path, read, policy, notifier):
    ctx = _ctx(tmp_path, broker=read)
    ctx.policy = policy
    ctx.sources = Sources(history=_history_for(policy), events=_no_events, broker=read)
    ctx.notifier = notifier
    return ctx


def _risk(ctx, cycle_id):
    return ctx.ledger.get_cycle(cycle_id)["risk"]


def _r20(risk) -> dict:
    return next(c for c in risk["checks"] if c["rule_id"] == "R20")


def test_a_credit_holds_the_satellite_alerts_the_operator_and_the_core_trades(tmp_path, world, sleeve_policy):
    fake, read = world
    fake.add_position("SPIN", units=3.0, settlement="real")          # credited by the broker, no stop
    notifier = Notifier()
    ctx = _sleeve_ctx(tmp_path, read, sleeve_policy, notifier)
    out = run_cycle(ctx)
    assert out.status == "on_time"
    risk = _risk(ctx, out.cycle_id)
    assert "satellite:corporate_action_pending" in _r20(risk)["detail"]
    assert all(risk["final_w"].get(s, 0.0) == 0.0 for s in STOCKS)   # the stock sleeve is held ...
    for s in SELECTED:                                                  # ... the rule's buys by R20
        assert any(r.startswith(f"{s}:") and "R20" in r for r in risk["hold_reasons"]), s
    assert any(risk["final_w"].get(s, 0.0) > 0 for s in ("NDX", "SPX", "GOLD"))   # the core still re-bases
    assert notifier.sent == [(f"council {out.cycle_id}",
                              f"corporate action: an unknown position on instrument {SPIN}; the stock sleeve "
                              f"is held. Run `council stocks adopt {SPIN}`", "urgent")]
    record = ctx.ledger.get_cycle(out.cycle_id)
    assert str(SPIN) not in " ".join(record["flags"]) and str(SPIN) not in _r20(risk)["detail"]
    # the watch's band for a vanished stock position: each stock line's 4-hour sigma, stored privately
    stored = ctx.ledger.get_runtime(STOCK_SIGMA_4H_KEY)
    assert set(stored) == set(STOCKS) and all(0 < v < 0.1 for v in stored.values())


def test_without_a_pending_action_nothing_is_held_or_sent(tmp_path, world, sleeve_policy):
    fake, read = world
    notifier = Notifier()
    ctx = _sleeve_ctx(tmp_path, read, sleeve_policy, notifier)
    out = run_cycle(ctx)
    risk = _risk(ctx, out.cycle_id)
    assert "corporate_action" not in _r20(risk)["detail"] and notifier.sent == []
    assert any(risk["final_w"].get(s, 0.0) > 0 for s in SELECTED)    # the rule buys


def test_a_position_our_own_leg_opened_is_not_a_corporate_action(tmp_path, world, sleeve_policy):
    fake, read = world
    pos = fake.add_position("SPIN", units=3.0, settlement="real", sl_rate=15.0)
    notifier = Notifier()
    ctx = _sleeve_ctx(tmp_path, read, sleeve_policy, notifier)
    leg = Leg(seq=1, kind="open", symbol="SPIN", line="TSTA", instrument_id=SPIN, direction="long",
              settlement="real", weight_before=0.0, weight_after=0.006, risk_increasing=True, units=3.0,
              amount_usd=60.0, sl_rate=15.0, origin="reference")
    ctx.ledger.create_decision(decision_id="d-ours", kind="rebalance", valid_until=NOW - timedelta(days=1),
                               now=NOW - timedelta(days=2))
    ctx.ledger.insert_legs("d-ours", [leg], now=NOW - timedelta(days=2))
    ctx.ledger.update_leg("d-ours", 1, state="submitting", now=NOW - timedelta(days=2))
    ctx.ledger.update_leg("d-ours", 1, state="filled", position_ids=[pos.position_id],
                          resolved_at=NOW - timedelta(days=2), now=NOW - timedelta(days=2))
    out = run_cycle(ctx)
    assert "corporate_action" not in _r20(_risk(ctx, out.cycle_id))["detail"] and notifier.sent == []


def test_a_failing_check_holds_the_satellite_and_alerts(tmp_path, world, sleeve_policy, monkeypatch):
    _, read = world

    def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(corporate, "detect", broken)
    notifier = Notifier()
    ctx = _sleeve_ctx(tmp_path, read, sleeve_policy, notifier)
    out = run_cycle(ctx)
    assert out.status == "on_time"
    risk = _risk(ctx, out.cycle_id)
    assert "satellite:corporate_action_check_failed" in _r20(risk)["detail"]
    assert all(risk["final_w"].get(s, 0.0) == 0.0 for s in STOCKS)
    assert [(p, "corporate_action_check_failed" in b) for _, b, p in notifier.sent] == [("urgent", True)]


def test_a_refused_alert_falls_back_to_fixed_text(tmp_path, world, sleeve_policy):
    fake, read = world
    fake.add_position("SPIN", units=3.0, settlement="real")
    notifier = Notifier(refuse=str(SPIN))                             # e.g. a 7-digit id the leak scan refuses
    ctx = _sleeve_ctx(tmp_path, read, sleeve_policy, notifier)
    run_cycle(ctx)
    assert [b for _, b, _ in notifier.sent] == ["corporate action pending: the stock sleeve is held. "
                                                "Run `council stocks status`"]


def test_a_core_only_book_runs_no_detection(tmp_path, world):
    fake, read = world
    fake.add_position("SPIN", units=3.0, settlement="real")
    notifier = Notifier()
    ctx = _ctx(tmp_path, broker=read)
    ctx.notifier = notifier
    out = run_cycle(ctx)
    assert "corporate_action" not in _r20(_risk(ctx, out.cycle_id))["detail"] and notifier.sent == []
    assert ctx.ledger.get_runtime(STOCK_SIGMA_4H_KEY) is None


def test_the_4h_sigma_scales_the_daily_sigma_to_four_session_hours(sleeve_policy):
    from tests.risk.helpers import state

    states = {"TSTA": state("TSTA", "stock", sigma_ann=0.3176), "TSTB": state("TSTB", "stock", sigma_ann=None),
              "NDX": state("NDX", "index", sigma_ann=0.2)}
    out = stock_sigma_4h(sleeve_policy, states)
    assert set(out) == {"TSTA"}                                       # no volatility, no core line
    assert out["TSTA"] == pytest.approx(0.3176 / 252 ** 0.5 * (4 / 6.5) ** 0.5, abs=1e-6)
