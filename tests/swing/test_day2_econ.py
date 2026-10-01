"""User decision 2026-10-01: day-2 confirmation of carried Skeptic waits, the paper reference at the
slot, the early net reward/risk gate at the declared cost, and 5 Skeptic reviews in a 12-call slot."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from council import invariants
from council.cycle import (
    SwingRun,
    _paper_track,
    _swing_ledger_idea,
    carried_waits,
    paper_slot_price,
    parks_as_wait,
)
from council.ledger.db import Ledger
from council.llm.prompts import PromptRegistry
from council.llm.stub import StubGateway
from council.swing import council as sc
from council.swing import rules as R
from council.swing.models import ScoutIdea
from council.swing.roles import SwingIdea, VerdictOutcome
from tests.swing import stubs as s

MAIN = "deepseek-v4.1-flash:cloud"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def reg():
    return PromptRegistry()


# ------------------------------------------------------------------------- (3) economics early
def test_gate_net_rr_uses_the_declared_cost(policy):
    sw = policy.swing
    assert R.declared_rt_pct(sw) == 2.5
    assert R.min_target_for(0.05, sw) == pytest.approx(0.115)
    assert R.gate_net_rr(0.05, 0.08, atr_pct=0.02, sp=sw) == "net_rr_below_min"     # CAH/KDP geometry
    assert R.gate_net_rr(0.03, 0.092, atr_pct=0.02, sp=sw) is None
    # a stop inside 1 ATR is widened first (as S5 would), so the same target no longer clears
    assert R.gate_net_rr(0.03, 0.092, atr_pct=0.04, sp=sw) == "net_rr_below_min"
    assert R.gate_net_rr(None, 0.1, atr_pct=None, sp=sw) is None
    assert R.public_code("net_rr_below_min") == "S6:net_rr_below_min"


def test_uneconomic_geometry_is_dropped_before_the_skeptic(reg, policy):
    bad = {**s.idea(), "stop_pct": 0.05, "target_pct": 0.08}
    gw = StubGateway(responses={"scout": s.scout(bad)}, model=MAIN)
    skg = StubGateway(responses={"skeptic": s.verdict()}, model=MAIN)
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    assert res.calls_used == 1 and not skg.log
    assert any(o.ref == "idea:1" and (o.stage, o.code) == ("gate", "net_rr_below_min") for o in res.outcomes)


def test_scout_prompt_states_the_minimum_target(reg, policy):
    from council.deliberation.common import prompt_context

    text = reg.render("scout", **prompt_context(policy), **sc.swing_prompt_context(policy))
    assert "declared round-trip cost of 2.5%" in text and "a 5% stop at least 11.5%" in text
    assert "day2_confirmation" in text and reg.prompt_id("scout") == "council-scout/v3"
    sk = reg.render("skeptic", **prompt_context(policy), **sc.swing_prompt_context(policy))
    assert "CONFIRMATION CHECK" in sk and reg.prompt_id("skeptic") == "council-skeptic/v4"


# ------------------------------------------------------------------------- (4) 5 Skeptic reviews
def test_five_skeptic_reviews_fit_a_twelve_call_slot(policy):
    llm = policy.swing.llm
    assert (llm.max_skeptic_calls, llm.max_calls_per_slot, llm.deadline_s) == (5, 12, 450)
    assert invariants.SWING_MAX_LLM_CALLS_PER_SLOT == 12
    invariants.check_policy(policy)
    lim = sc.swing_limits(policy)
    assert (lim.max_ideas, lim.max_skeptic, lim.max_calls) == (5, 5, 12)
    plan = sc.plan_budget(5, 0, max_calls=lim.max_calls, max_skeptic=lim.max_skeptic, pm_replicates=3)
    assert (plan.skeptic_ideas, plan.pm_replicates, plan.debate, plan.flags) == (5, 3, True, ())
    assert plan.calls() == 11                                    # one spare for a fallback retry
    assert sc.swing_limits(policy, 20).max_calls == 1 + 40 + 2 + 3   # wide mode unchanged


# ------------------------------------------------------------------------- (1) day-2 confirmation
def _carried(**kw):
    idea = ScoutIdea.model_validate({**s.idea(), "setup": sc.DAY2_SETUP, **kw})
    return sc.CarriedWait(idea_id="idea:old_1", idea=idea, wait_day="2026-09-28")


def _skeptic_seen(log):
    return [c for c in log if c.role == "skeptic"]


def test_a_carried_wait_is_re_proposed_as_day2_and_the_skeptic_is_told(reg, policy):
    seen: list[str] = []

    def skeptic(user, rep):
        seen.append(user)
        return s.verdict("idea:1")

    def pm(user, rep):
        return s.pm(("idea:1", "enter"))

    gw = StubGateway(responses={"scout": s.scout(), "swing_bull": s.case(), "swing_bear": s.case(bear=True),
                                "swing_pm": pm}, model=MAIN)
    skg = StubGateway(responses={"skeptic": skeptic}, model=MAIN)
    inputs = s.inputs(carried=[_carried()])
    res = run(sc.run_swing_stage(gw, reg, policy, inputs, gate=s.gate_with({}), skeptic_gw=skg))
    idea = res.ideas["idea:1"]
    assert idea.day2 and idea.idea.setup == sc.DAY2_SETUP
    assert [a.ref for a in res.entries()] == ["idea:1"]
    assert "CONFIRMATION CHECK" in seen[0] and s.THESIS not in seen[0]       # still blind
    sections, _ = sc.scout_input(inputs, {})
    assert "CARRIED WAITS" in sections[0].items[0].text
    plain, _ = sc.scout_input(s.inputs(), {})
    assert "CARRIED WAITS" not in plain[0].items[0].text


def test_an_unconfirmed_day2_spends_no_skeptic_call(reg, policy):
    gw = StubGateway(responses={"scout": s.scout()}, model=MAIN)
    skg = StubGateway(responses={"skeptic": s.verdict()}, model=MAIN)
    for card in (s.card("ACME", age=0), s.card("ACME", sigma=-0.4)):
        res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(carried=[_carried()]),
                                     gate=s.gate_with({"ACME": card}), skeptic_gw=skg))
        assert any(o.code == "day2_unconfirmed" and o.stage == "gate" for o in res.outcomes)
    assert not skg.log


def test_scout_twin_and_lost_catalyst(reg, policy):
    check = sc.accept_ideas(sc.ScoutOutput.model_validate(s.scout(s.idea())),
                            catalysts=sc.catalyst_index(s.reading(), slot=s.SLOT),
                            setups_live=["post_earnings_drift"], setups_paper_only=[])
    cats = sc.catalyst_index(s.reading(), slot=s.SLOT)
    result = sc.SwingCouncilResult(slot="x")
    sc.add_carried(check, [_carried()], cats, result, start=2, live=True)    # same side, no new news
    assert [i.ref for i in check.accepted] == ["idea:2"] and check.accepted[0].day2
    assert any(d.code == "duplicate_idea" for d in check.drops)
    check2 = sc.accept_ideas(sc.ScoutOutput.model_validate(s.scout(s.idea(catalysts=[s.P_ACME, s.N_ACME]))),
                             catalysts=cats, setups_live=["post_earnings_drift"], setups_paper_only=[])
    sc.add_carried(check2, [_carried()], cats, result, start=2, live=True)   # a newer catalyst: Scout wins
    assert [i.ref for i in check2.accepted] == ["idea:1"] and "day2_superseded" in result.flags
    check3 = sc.accept_ideas(None, catalysts=cats, setups_live=[], setups_paper_only=[])
    sc.add_carried(check3, [_carried(catalyst_ids=["P:deadbeef"])], cats, result, start=1, live=True)
    assert not check3.accepted and "day2_catalyst_gone" in result.flags
    # the Scout may not pitch the code-only setup itself
    res = run(sc.run_swing_stage(StubGateway(responses={"scout": s.scout(s.idea(setup=sc.DAY2_SETUP))}, model=MAIN),
                                 reg, policy, s.inputs(), gate=s.gate_with({})))
    assert any(o.code == "setup_not_allowed" for o in res.outcomes)


def _swing_idea(ref="idea:1", verdict="wait"):
    i = SwingIdea(ref=ref, idea=ScoutIdea.model_validate(s.idea()), line_id="ACME")
    i.verdict = VerdictOutcome(verdict, "skeptic_wait" if verdict == "wait" else None, None)
    return i


def test_a_wait_parks_pending_and_is_carried_after_a_session(tmp_path, policy):
    t0 = datetime(2026, 10, 1, 18, 40, tzinfo=UTC)                 # Thursday
    led = Ledger(tmp_path / "l.sqlite3", clock=lambda: t0)
    idea = _swing_idea()
    result = sc.SwingCouncilResult(slot="x", ideas={"idea:1": idea},
                                   outcomes=[sc.IdeaOutcome("idea:1", "ACME", "long", "post_earnings_drift",
                                                            "skeptic", "skeptic_wait")])
    assert parks_as_wait(result, "idea:1", idea)
    assert not parks_as_wait(result, "idea:1", _swing_idea(verdict="reject"))
    iid = _swing_ledger_idea(led, "2026-10-01T1840Z", idea, "pending", t0, wait_day="2026-10-01")
    assert carried_waits(led, policy, t0, t0) == []                          # same day: no close yet
    fri = t0 + timedelta(days=1)
    got = carried_waits(led, policy, fri.replace(hour=14), fri)
    assert [c.idea_id for c in got] == [iid] and got[0].idea.setup == sc.DAY2_SETUP
    # a pending missed entry (no parked wait) is not carried
    led.add_swing_idea("idea:m_1", origin_cycle="c0", ticker="WIDG", side="long", status="pending", now=t0)
    assert [c.idea_id for c in carried_waits(led, policy, fri, fri)] == [iid]
    # the re-proposal limit: 2 carries -> expired, never re-proposed again
    led.update_swing_idea(iid, carry_cycle="c1", now=t0)
    led.update_swing_idea(iid, carry_cycle="c2", now=t0)
    assert carried_waits(led, policy, fri, fri) == [] and led.swing_idea(iid)["status"] == "expired"


# ------------------------------------------------------------------------- (2) paper reference
def test_paper_entry_uses_the_slot_price_and_falls_back_to_the_last_close(tmp_path, policy):
    t0 = datetime(2026, 10, 1, 18, 40, tzinfo=UTC)
    led = Ledger(tmp_path / "l.sqlite3", clock=lambda: t0)
    flags: list[str] = []

    def last_close(ticker):
        flags.append("paper_reference_last_close")
        return 100.0

    ctx = SimpleNamespace(ledger=led, sources=SimpleNamespace(swing=SimpleNamespace(reference_price=last_close)))
    result = sc.SwingCouncilResult(slot="x")
    for k, slot_price in enumerate((103.5, None), start=1):
        idea = _swing_idea(ref=f"idea:{k}", verdict="reject")
        idea.card = s.card("ACME")
        idea.card.slot_price = slot_price
        assert paper_slot_price(idea) == slot_price
        out = SwingRun()
        _paper_track(ctx, out, result, idea.ref, idea, f"idea:p_{k}", None, slot=t0, now=t0,
                     cycle_id="c1", skeptic="reject")
    refs = sorted(float(r["entry_ref"]) for r in led.paper_trades())
    assert refs == [100.0, 103.5] and flags == ["paper_reference_last_close"]     # flag only on fallback


def test_the_card_keeps_the_slot_price_private():
    from tests.swing.test_facts import CLOSES, _card

    card = _card(live_price=float(CLOSES[-1]) * 1.01)
    assert card.slot_price == pytest.approx(float(CLOSES[-1]) * 1.01)
    assert "slot_price" not in card.fields and "slot_price" not in str(card.public_view())
    assert _card().slot_price is None
    assert card.content_hash() == _card(live_price=float(CLOSES[-1]) * 1.01).content_hash()
