"""SW-5c: production swing sources. The offline fixtures drive the swing stage end to end (stub LLM,
paper only while SWING_BOOK_LIVE is False); the real builder fails closed on a missing credential;
its code gate runs resolve + fact card on stubbed SEC / Alpaca / FINRA clients; a paper run (no
broker) passes SEC-resolved ideas unverified and never loads a broker token."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from council import invariants
from council.context import build_context, hold_reference_stub
from council.cycle import run_swing
from council.data.finra import ShortInterest
from council.ledger.db import Ledger
from council.llm.prompts import PromptRegistry
from council.llm.stub import StubGateway
from council.stocks.sec import TickerRow
from council.swing import sources as ss
from council.swing.models import ScoutIdea
from council.swing.roles import SwingIdea
from tests.swing import test_facts as tf

SLOT = datetime(2026, 9, 29, 14, 40, tzinfo=UTC)           # EDT Tuesday 10:40 New York: a swing slot


def ctx_for(policy, tmp_path, src, *, account: bool = False):
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    if account:
        (state / "account").mkdir(exist_ok=True)
        (state / "account" / "swing.json").write_text(json.dumps({"funded_real_nav_usd": 2000.0}))
    ledger = Ledger(state / "ledger.sqlite3")
    ledger.migrate()
    return SimpleNamespace(policy=policy, ledger=ledger, gateway=StubGateway(hold_reference_stub()),
                           registry=PromptRegistry(), sources=SimpleNamespace(broker=None, swing=src),
                           state_dir=state)


def run(ctx, slot=SLOT):
    return asyncio.run(run_swing(ctx, SimpleNamespace(cycle_id="2026-09-29T1440Z"), snapshot=None,
                                 kill_state="NORMAL", nav=None, slot=slot, now=slot))


@pytest.mark.parametrize("account", [False, True])
def test_fixture_sources_run_the_swing_stage_end_to_end_on_paper(policy, tmp_path, account):
    assert invariants.SWING_BOOK_LIVE is False
    src = ss.fixture_swing_sources(skeptic_gateway=ss.fixture_skeptic_gateway(policy))
    ctx = ctx_for(policy, tmp_path, src, account=account)
    out = run(ctx)
    assert out.slot_ok and not out.live and not out.lines and not out.orders
    assert not [f for f in out.flags if f.startswith("swing_error")], out.flags
    roles = [c.role for c in out.calls]
    assert roles[0] == "scout" and "skeptic" in roles and "swing_pm" in roles
    (paper,) = ctx.ledger.paper_trades()
    group = (paper.get("record") or {}).get("group")
    # with the funded NAV the paper book prices S6 and the idea is a would-have-executed entry;
    # without it every entry drops cost_unavailable (fail closed)
    assert group == ("missed" if account else "code_dropped"), (group, out.flags)
    assert "swing_paper_assumed_book" in out.flags
    assert len(out.entries) == (1 if account else 0)


def test_real_sources_fail_closed_without_alpaca_keys(policy, tmp_path):
    src = ss.real_swing_sources(policy, state_dir=tmp_path, keys_loader=lambda: None,
                                sec_user_agent=lambda: "Council Test ops@example.org")
    assert src.unavailable == ("swing_source_unavailable:alpaca",)
    assert src.daily_bars is None and src.benchmark_returns is None
    ctx = ctx_for(policy, tmp_path, src)
    out = run(ctx)
    assert "swing_source_unavailable:alpaca" in out.flags and out.calls == [] and out.entries == []


def test_real_sources_fail_closed_without_the_sec_user_agent(policy, tmp_path):
    def missing():
        raise RuntimeError("no agent")

    src = ss.real_swing_sources(policy, state_dir=tmp_path, keys_loader=lambda: object(), sec_user_agent=missing)
    assert src.unavailable == ("swing_source_unavailable:sec",)


class FakeSec:
    def __init__(self):
        self.calls = 0

    def company_tickers(self):
        return [TickerRow(cik=123, ticker="TSTA", title="Test A")]

    def submissions(self, cik):
        self.calls += 1
        return {"sic": "7372", "filings": {"recent": {}}}

    def companyfacts(self, cik):
        return None


def _idea(ticker="TSTA", side="long"):
    si = ScoutIdea.model_validate({"ticker": ticker, "side": side, "setup": "post_earnings_drift",
                                   "catalyst_ids": [tf.SEC_ITEM.id], "catalyst_claim": "8-K item 2.02",
                                   "thesis": "t", "why_not_priced_in": "w", "entry": "now", "stop_pct": 0.06,
                                   "target_pct": 0.12, "time_stop_days": 10, "invalidation": "i"})
    return SwingIdea(ref="idea:1", idea=si, line_id=ticker)


def _real(policy, tmp_path, *, allow_unverified, bars_fail=False):
    panel = {"TSTA": tf.STOCK, "XLK": tf.SECTOR, "SPY": tf.SPY, "QQQ": tf.QQQ}
    asked: list[list[str]] = []

    def bars(symbols, start, *, keys, now):
        asked.append(list(symbols))
        if bars_fail:
            raise RuntimeError("down")
        return {s: panel[s] for s in symbols if s in panel}

    def si(symbols, *, asof):
        return {"TSTA": ShortInterest(symbol="TSTA", settlement_date=tf.PRE, short_shares=1e6,
                                      avg_daily_volume=1e6, days_to_cover=1.0)}

    src = ss.real_swing_sources(policy, state_dir=tmp_path, keys_loader=lambda: object(),
                                sec_user_agent=lambda: "Council Test ops@example.org", sec_factory=FakeSec,
                                fetch_bars=bars, fetch_short_interest=si, allow_unverified=allow_unverified,
                                wall=lambda: tf.SLOT)
    return src, asked


def _gate(src, idea):
    state = src.gate.__self__                                   # the builder's per-slot state
    state._slot = tf.SLOT
    state._reading = {tf.SEC_ITEM.id: tf.SEC_ITEM}
    return asyncio.run(src.gate([idea]))


def test_real_gate_without_a_broker_is_eligibility_unavailable_unless_a_paper_run(policy, tmp_path):
    src, asked = _real(policy, tmp_path, allow_unverified=False)
    (res,) = _gate(src, _idea()).values()
    assert (res.ok, res.reason) == (False, "eligibility_unavailable") and asked == []

    src, asked = _real(policy, tmp_path, allow_unverified=True)
    (res,) = _gate(src, _idea()).values()
    assert res.ok and res.card is not None and res.card.ok, res
    assert res.card.fields["sector_etf"] == "XLK" and ss.UNVERIFIED_LABEL in res.card.flags
    assert "swing_eligibility_unverified" in src.drain()
    assert asked == [["TSTA", "XLK", "SPY", "QQQ"]]
    assert src.candidate_extras(_idea())["sector"] == "BusEq"
    assert src.reference_price("TSTA") == pytest.approx(float(tf.STOCK["close"].iloc[-1]))


def test_real_gate_unresolved_symbol_and_bar_failure_fail_closed(policy, tmp_path):
    src, asked = _real(policy, tmp_path, allow_unverified=True)
    (res,) = _gate(src, _idea("NVDAA")).values()
    assert (res.ok, res.reason) == (False, "unresolved_symbol") and asked == []

    src, _ = _real(policy, tmp_path, allow_unverified=True, bars_fail=True)
    (res,) = _gate(src, _idea()).values()
    assert not res.ok and res.reason == "no_facts"
    assert "swing_source_error:alpaca:RuntimeError" in src.drain()


def test_stub_context_carries_the_offline_fixture_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    ctx = build_context(mode="stub", publish="none", state_dir=tmp_path / "s")
    src = ctx.sources.swing
    assert src is not None and src.unavailable == () and src.skeptic_gateway is not None
    assert src.reference_price(ss.FIXTURE_TICKER) == ss.FIXTURE_PRICE


def test_paper_context_never_loads_a_broker(tmp_path, monkeypatch):
    import council.context as C
    from council.settings import Settings

    def boom(_settings):
        raise AssertionError("a paper run loaded a broker token")

    monkeypatch.setattr(C, "read_broker", boom)
    monkeypatch.setenv("COUNCIL_MODE", "stub")          # no Keychain read in tests
    settings = Settings(role="dev", mode="dry_run", agent_portfolio_id="x" * 8)
    ctx = build_context(mode="dry_run", publish="none", state_dir=tmp_path / "p", settings=settings,
                        no_broker=True, stub_llm=True)
    assert ctx.sources.broker is None and ctx.publisher is None


def test_cli_paper_refuses_stub_mode(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from council.cli import app

    monkeypatch.setenv("COUNCIL_MODE", "stub")
    monkeypatch.setenv("COUNCIL_STATE_DIR", str(tmp_path))
    res = CliRunner().invoke(app, ["cycle", "--paper"])
    assert res.exit_code == 2 and "COUNCIL_MODE=dry_run" in res.output


def test_run_cycle_at_a_swing_slot_exercises_the_swing_stage_offline(tmp_path):
    """`council cycle` on a stubbed context (as dry run / rehearsal with --stub-llm build it): the
    whole cycle runs, the swing stage calls every swing role on the fixtures, tracks the idea on
    paper and publishes nothing that trips the leak scan; no plan without a broker."""
    from council.cycle import run_cycle
    from council.publish.gitops import Publisher
    from tests.integration import test_end_to_end as e2e

    preview = tmp_path / "preview"
    ctx = e2e._ctx(tmp_path, publisher=Publisher(tmp_path / "state" / "publisher-clone", push=False,
                                                 dry_run_dir=preview))
    ctx.sources.swing = ss.fixture_swing_sources(skeptic_gateway=ss.fixture_skeptic_gateway(ctx.policy))
    out = run_cycle(ctx)
    assert out.status == "on_time" and out.legs == 0
    flags = ctx.ledger.get_cycle(out.cycle_id)["flags"]
    assert not [f for f in flags if f.startswith("swing_error")], flags
    roles = {c["role"] for c in ctx.ledger.role_calls(out.cycle_id)}
    assert {"scout", "skeptic", "swing_bull", "swing_bear", "swing_pm"} <= roles, roles
    assert len(ctx.ledger.paper_trades()) == 1
    e2e._assert_clean(preview)
