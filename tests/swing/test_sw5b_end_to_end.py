"""SW-5b end to end on the fake broker: swing slot -> Scout ideas (stub LLM) -> Skeptic / debate / PM ->
S-rules -> plan (SwingOrder with TP in the body) -> operator approval through the REAL guards ->
execution -> the broker's take-profit fires -> the watch classifies `closed_target` -> paper and
status rows. `SWING_BOOK_LIVE` is patched True for the live path only; with it False the swing book
is paper-only (no leg, no decision)."""

# ruff: noqa: F811 - the execution fixtures are imported by name and requested as parameters

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pandas as pd
import pytest

from council import invariants, watch
from council.broker.eligibility import parse_eligibility, resolve_vehicle
from council.broker.fake import eligibility_row, leverage_config
from council.broker.instruments import InstrumentMap
from council.broker.parsing import parse_rates
from council.cycle import (
    SwingSources,
    decision_valid_until,
    record_swing_decision,
    run_swing,
    stamp_sessions,
    stamp_swing_legs,
)
from council.execution.planner import build_plan
from council.llm.prompts import PromptRegistry
from council.llm.stub import StubGateway
from council.operator.approve import ApprovalDeps
from council.operator.approve import approve as do_approve
from council.operator.capabilities import CAPABILITIES, Capabilities
from council.risk.exposure import snapshot_from_pnl
from council.swing import paper
from council.swing.book import swing_vehicle_map
from council.swing.status import swing_status
from tests.cli.operator_sim import simulate_operator
from tests.execution.conftest import (  # noqa: F401 - pytest fixtures
    fake,
    fclock,
    ledger,
    read_client,
    write_client,
)
from tests.execution.helpers import INSTRUMENTS, NAV
from tests.swing import stubs as s

ACME_ID, BID, ASK = 211, 100.0, 100.1
CYCLE = "2026-10-01T1440Z"
CAPS = frozenset(CAPABILITIES) | {"stock_real_long", "tp_on_open"}


@pytest.fixture
def world(fake, fclock, tmp_path):
    fake.add_instrument("ACME", ACME_ID, bid=BID, ask=ASK, row=eligibility_row("ACME", ACME_ID, configs=[
        leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,), min_tp_pct=2.0)]))
    state = tmp_path / "state"
    InstrumentMap({}, path=state / "instruments.json").merged(
        {**{k: v[0] for k, v in INSTRUMENTS.items()}, "ACME": ACME_ID}, fclock.now()).save()
    (state / "account").mkdir(parents=True, exist_ok=True)
    (state / "account" / "swing.json").write_text(json.dumps({"funded_real_nav_usd": NAV}))
    return state


def gateways():
    def skeptic(user: str, rep: int):
        return s.verdict("idea:1", line="ACME")

    main = {"scout": s.scout(s.idea("ACME")), "swing_bull": s.case(), "swing_bear": s.case(bear=True),
            "swing_pm": lambda user, rep: s.pm(("idea:1", "enter"))}
    return StubGateway(responses=main, model="deepseek-v4.1-flash:cloud"), \
        StubGateway(responses={"skeptic": skeptic}, model="glm-5.3-flash:cloud")


def swing_ctx(ledger, policy, read, state, slot):
    gw, skg = gateways()
    card = s.card("ACME", sigma_daily=2.5, adv_usd_20d=1e9, px_ge_10=True, beta_60d=1.1)
    src = SwingSources(
        inputs=lambda sl, views, exits: s.inputs(slot=sl, open_trades=views, code_exits=exits),
        gate=s.gate_with({"ACME": card}), skeptic_gateway=skg,
        reference_price=lambda ticker: ASK,
        candidate_extras=lambda idea: {"sector": "BusEq", "listing_days": 900},
    )
    return SimpleNamespace(policy=policy, ledger=ledger, gateway=gw, registry=PromptRegistry(),
                           sources=SimpleNamespace(broker=read, swing=src), state_dir=state)


def snapshot(read, ledger, policy, state, now):
    from council.execution.planner import vehicle_to_line

    imap = InstrumentMap.load(state / "instruments.json")
    smap = swing_vehicle_map(ledger, policy)
    return snapshot_from_pnl(read.pnl(), vehicle_by_instrument=smap.merged_symbols(imap.symbols_by_id()),
                             line_by_vehicle=smap.merged_lines(vehicle_to_line(policy.universe)), now=now)


def plan_for(fake, read, policy, state, snap, orders, ledger):
    imap = InstrumentMap.load(state / "instruments.json")
    raw = [inst.row for inst in fake.instruments.values()]
    elig = {r.symbol: r for r in parse_eligibility({"eligibilities": raw}, snap.taken_at)}
    quotes = parse_rates(read.rates([i.instrument_id for i in fake.instruments.values()]), imap.symbol_for)
    lines = policy.universe.by_symbol()
    return build_plan(
        snapshot=snap, target_w={},
        vehicle_for=lambda line, d, lev: resolve_vehicle(lines[line], d, lev, elig, lambda v, r, c: 5.0),
        quotes=quotes, stop_distance={}, leverage_for={}, eligibility=elig,
        cost_bps=lambda line, sym, d, lev: (10.0, 0.0, 0.0), nav_usd=snap.equity_usd, policy=policy,
        capabilities=Capabilities(verified=CAPS), swing=orders, swing_map=swing_vehicle_map(ledger, policy))


def test_swing_slot_to_closed_target_end_to_end(world, fake, fclock, ledger, policy, read_client, write_client,
                                                monkeypatch):
    monkeypatch.setattr(invariants, "SWING_BOOK_LIVE", True)
    slot = now = fclock.now()                                 # 14:40 UTC, EDT: a swing slot
    snap = snapshot(read_client, ledger, policy, world, now)
    ctx = swing_ctx(ledger, policy, read_client, world, slot)
    out = asyncio.run(run_swing(ctx, SimpleNamespace(cycle_id=CYCLE), snapshot=snap, kill_state="NORMAL",
                                nav=SimpleNamespace(drawdown=0.0), slot=slot, now=now))
    assert out.slot_ok and out.live and not [f for f in out.flags if f.startswith("swing_error")], out.flags
    (entry,) = out.entries
    assert entry.ticker == "ACME" and entry.size_nav == pytest.approx(0.08) and entry.stop_pct == pytest.approx(0.06)
    assert [ln.line_id for ln in out.lines] == ["SW_ACME"] and out.orders[0].action == "enter"

    # plan -> decision (as the cycle does) -> proposed trade
    plan = plan_for(fake, read_client, policy, world, snap, out.orders, ledger)
    (leg,) = plan.legs
    assert leg.tp_mode == "body" and leg.sleeve == "swing" and leg.swing_trade_id == entry.trade_id
    plan = stamp_swing_legs(stamp_sessions(plan, policy.universe, asof=slot), slot)
    decision = f"{CYCLE}-rebalance-e2e001"
    ledger.create_decision(decision_id=decision, kind="rebalance", valid_until=decision_valid_until(plan, "rebalance", slot),
                           cycle_id=CYCLE, target={"base_w": dict(snap.signed_w)}, plan=plan,
                           policy_sha=policy.sha256, now=now)
    ledger.insert_legs(decision, plan.legs)
    ledger.set_published_commit(decision, "c0ffee")
    assert record_swing_decision(ledger, out, plan, decision, CYCLE, now) == []
    assert ledger.swing_trade(entry.trade_id).state == "proposed"

    # the operator approves in the operator terminal: the REAL process guards run
    simulate_operator(monkeypatch)
    printed: list[str] = []
    deps = ApprovalDeps(ledger=ledger, policy=policy, read=read_client, write_factory=lambda: write_client,
                        state_dir=world, print_fn=printed.append, now_fn=fclock.now,
                        input_fn=lambda prompt: re.search(r"Type (\S+) to approve", prompt).group(1),
                        executor_kwargs={"clock": fclock.now, "sleep": fclock.sleep, "_skip_guard_for_tests": True})
    report = do_approve(decision, deps)
    assert report.final_state == "completed", report
    assert any("swing idea:" in line and "to broker: yes (open body)" in line for line in printed)
    (pos,) = fake.positions_for("ACME")
    assert pos.tp_rate == pytest.approx(leg.tp_rate) and pos.sl_rate == pytest.approx(leg.sl_rate)
    t = ledger.swing_trade(entry.trade_id)
    assert t.state == "open" and pos.position_id in t.position_ids

    # the broker's take-profit fires; the watch classifies it from the closed-trade record
    wctx = SimpleNamespace(policy=policy, ledger=ledger, state_dir=world,
                           sources=SimpleNamespace(broker=SimpleNamespace(closed_trade=fake.closed_trade)))
    monkeypatch.setattr(watch, "closed_trade_route_ok", lambda state_dir: True)
    watch._stop_hits(wctx, snapshot(read_client, ledger, policy, world, fclock.now()), fclock.now())
    fake.hit_take_profit(pos.position_id)
    alerts = watch._stop_hits(wctx, snapshot(read_client, ledger, policy, world, fclock.now()), fclock.now())
    assert alerts == ["swing trade closed at its take-profit: SW_ACME"]
    t = ledger.swing_trade(entry.trade_id)
    assert t.state == "closed_target" and t.detail["exit_kind"] == "target" and t.detail["r_declared"] > 0

    # paper: every idea group with its Skeptic verdict, settled on daily bars; status rows
    (row,) = ledger.paper_trades()
    assert row["record"]["group"] == "executed" and row["record"]["skeptic_verdict"] == "pass"
    bars = pd.DataFrame({"open": [ASK, ASK * 1.02], "high": [ASK, ASK * 1.20], "low": [ASK, ASK * 1.01],
                         "close": [ASK, ASK * 1.18]},
                        index=pd.to_datetime(["2026-10-01", "2026-10-02"]).tz_localize("UTC"))
    (outcome,) = paper.settle(ledger, {"ACME": bars})
    assert outcome.exit_reason == "target"
    lines = swing_status(ledger, today=fclock.now().date(), now=fclock.now(), resamples=50).lines()
    assert "open trades: 0" in lines
    assert any(line.startswith("paper executed: n=1") for line in lines)
    assert any(line.startswith("pause rule:") and "1 closed" in line for line in lines)


def test_with_the_switch_off_the_swing_book_is_paper_only(world, fake, fclock, ledger, policy, read_client):
    assert invariants.SWING_BOOK_LIVE is False
    slot = now = fclock.now()
    snap = snapshot(read_client, ledger, policy, world, now)
    ctx = swing_ctx(ledger, policy, read_client, world, slot)
    out = asyncio.run(run_swing(ctx, SimpleNamespace(cycle_id=CYCLE), snapshot=snap, kill_state="NORMAL",
                                nav=SimpleNamespace(drawdown=0.0), slot=slot, now=now))
    assert out.slot_ok and not out.live and out.lines == [] and out.orders == []
    assert len(out.entries) == 1                       # the rules accepted it: tracked as missed on paper
    (row,) = ledger.paper_trades()
    assert row["record"]["group"] == "missed" and row["record"]["skeptic_verdict"] == "pass"
    assert ledger.swing_trades() == []                 # no trade, no leg, nothing to approve


def test_an_unknown_drawdown_blocks_every_entry(world, fake, fclock, ledger, policy, read_client, monkeypatch):
    monkeypatch.setattr(invariants, "SWING_BOOK_LIVE", True)
    slot = now = fclock.now()
    snap = snapshot(read_client, ledger, policy, world, now)
    out = asyncio.run(run_swing(swing_ctx(ledger, policy, read_client, world, slot), SimpleNamespace(cycle_id=CYCLE),
                                snapshot=snap, kill_state="NORMAL", nav=None, slot=slot, now=now))
    assert out.entries == [] and "swing_drop:S17:drawdown_unknown" in out.flags


def test_off_a_swing_slot_nothing_runs(world, fake, fclock, ledger, policy, read_client):
    from datetime import timedelta

    slot = fclock.now() + timedelta(hours=2)            # 16:40 UTC is not a swing slot
    ctx = swing_ctx(ledger, policy, read_client, world, slot)
    out = asyncio.run(run_swing(ctx, SimpleNamespace(cycle_id=CYCLE), snapshot=None, kill_state="NORMAL",
                                nav=SimpleNamespace(drawdown=0.0), slot=slot, now=slot))
    assert not out.slot_ok and out.calls == [] and ctx.gateway.log == []
