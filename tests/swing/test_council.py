"""SW-3 acceptance: the swing council with stub gateways (no LLM, no broker)."""

from __future__ import annotations

import asyncio

import pytest

from council.llm.prompts import PromptRegistry
from council.llm.stub import StubFailure, StubGateway
from council.swing import council as sc
from council.swing.roles import CanaryLeak, SwingIdea
from tests.swing import stubs as s

MAIN = "deepseek-v4.1-flash:cloud"
OTHER = "glm-5.3-flash:cloud"


@pytest.fixture
def reg():
    return PromptRegistry()


def gws(main: dict, skeptic: dict | None = None):
    gw = StubGateway(responses=main, model=MAIN)
    sk = StubGateway(responses=skeptic if skeptic is not None else main, model=OTHER)
    return gw, sk


def run(coro):
    return asyncio.run(coro)


def roles_called(*gateways: StubGateway) -> list[str]:
    return [c.role for g in gateways for c in g.log]


def full_pass_responses(n: int = 1, *, pm_votes=("enter", "enter", "enter")):
    ideas = [s.idea("ACME"), s.idea("WIDG", catalysts=[s.P_WIDG]), s.idea("GLOBEX", catalysts=[s.P_GLOBEX])][:n]
    lines = ["ACME", "WIDG", "GLOBEX"]

    def skeptic(user: str, rep: int):
        for k, line in enumerate(lines, start=1):
            if f"IDEA TO REVIEW: idea:{k}\n" in user:
                return s.verdict(f"idea:{k}", line=line)
        raise AssertionError("unknown idea")

    def pm(user: str, rep: int):
        return s.pm(("idea:1", pm_votes[rep]))

    main = {"scout": s.scout(*ideas), "swing_bull": s.case(), "swing_bear": s.case(bear=True), "swing_pm": pm}
    return main, {"skeptic": skeptic}


def test_full_path_enters_on_2_of_3_and_stays_within_budget(reg, policy):
    main, sk = full_pass_responses(3)
    gw, skg = gws(main, sk)
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    assert [a.ref for a in res.entries()] == ["idea:1"]
    assert res.entries()[0].votes_for == 3
    assert res.calls_used <= 9 and res.calls_used == 1 + 3 + 2 + 3
    assert roles_called(skg) == ["skeptic"] * 3            # the Skeptic ran on its own gateway
    same = policy.swing.llm.skeptic_model_family == "same"   # user 2026-09-29: the Scout's model
    assert ("skeptic_same_model" in res.flags) == same and res.skeptic_model == OTHER


def test_pm_one_of_three_is_no_entry(reg, policy):
    main, sk = full_pass_responses(1, pm_votes=("enter", "pass", "pass"))
    gw, skg = gws(main, sk)
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    assert res.entries() == []
    assert any(o.ref == "idea:1" and o.code == "pm_pass" for o in res.outcomes)


def test_zero_calls_after_scout_when_nothing_survives(reg, policy):
    gw, skg = gws({"scout": s.scout()})
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    assert res.calls_used == 1 and roles_called(gw, skg) == ["scout"]
    # an idea that the Skeptic rejects also stops there: 1 + 1 calls, no debate, no PM
    gw, skg = gws({"scout": s.scout(s.idea())}, {"skeptic": s.verdict(verdict="reject")})
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    assert res.calls_used == 2 and "swing_pm" not in roles_called(gw, skg)


def test_paper_only_setup_spends_no_call(reg, policy):
    gw, skg = gws({"scout": s.scout(s.idea(setup="breakout"))})
    gate = s.gate_with({})
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=gate, skeptic_gw=skg))
    assert res.calls_used == 1 and roles_called(skg) == []
    assert [i.ref for i in res.paper_only] == ["idea:1"]
    assert gate.seen == []                                  # not even the code gate


def test_a_mostly_wait_never_reaches_the_pm(reg, policy):
    gw, skg = gws({"scout": s.scout(s.idea()), "swing_pm": s.pm(("idea:1", "enter"))},
                  {"skeptic": s.verdict(verdict="wait", priced_in="mostly")})
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    assert res.entries() == [] and "swing_pm" not in roles_called(gw)
    assert any(o.stage == "skeptic" and o.code == "skeptic_wait" for o in res.outcomes)
    assert all(sc.WAIT_DEBATED not in o.flags for o in res.outcomes)


def _wait_heard(priced_in, pm_votes):
    def pm(user, rep):
        return s.pm(("idea:1", pm_votes[rep]))
    return gws({"scout": s.scout(s.idea()), "swing_bull": s.case(), "swing_bear": s.case(bear=True),
                "swing_pm": pm}, {"skeptic": s.verdict(verdict="wait", priced_in=priced_in)})


def test_a_supported_wait_is_heard_and_can_enter_on_2_of_3(reg, policy):   # user decision 2026-10-01
    gw, skg = _wait_heard("partly", ("enter", "pass", "enter"))
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    called = roles_called(gw)
    assert "swing_bull" in called and "swing_bear" in called and called.count("swing_pm") == 3
    assert [a.ref for a in res.entries()] == ["idea:1"] and res.entries()[0].votes_for == 2
    o = next(o for o in res.outcomes if o.ref == "idea:1")
    assert (o.stage, o.code) == ("pm", None) and sc.WAIT_DEBATED in o.flags
    assert res.ideas["idea:1"].verdict.status == "wait" and sc.WAIT_DEBATED in res.ideas["idea:1"].verdict.flags


def test_a_heard_wait_needs_the_pm_majority(reg, policy):
    gw, skg = _wait_heard("no", ("enter", "pass", "pass"))
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    assert res.entries() == []
    o = next(o for o in res.outcomes if o.ref == "idea:1")
    assert (o.stage, o.code) == ("pm", "pm_pass") and sc.WAIT_DEBATED in o.flags


def test_a_mostly_pass_now_reaches_the_pm(reg, policy):
    gw, skg = gws({"scout": s.scout(s.idea()), "swing_bull": s.case(), "swing_bear": s.case(bear=True),
                   "swing_pm": s.pm(("idea:1", "enter"))}, {"skeptic": s.verdict(priced_in="mostly")})
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    assert [a.ref for a in res.entries()] == ["idea:1"]


def test_fully_priced_in_is_rejected_before_the_pm(reg, policy):
    gw, skg = gws({"scout": s.scout(s.idea()), "swing_pm": s.pm(("idea:1", "enter"))},
                  {"skeptic": s.verdict(priced_in="fully")})
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    assert res.entries() == [] and "swing_pm" not in roles_called(gw)
    assert any(o.code == "skeptic_reject" and "skeptic_incoherent" in o.flags for o in res.outcomes)


def test_chase_beyond_hard_sigma_is_dropped_before_the_skeptic(reg, policy):
    gw, skg = gws({"scout": s.scout(s.idea())}, {"skeptic": s.verdict()})
    cards = {"ACME": s.card("ACME", sigma=4.1)}
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with(cards), skeptic_gw=skg))
    assert roles_called(skg) == [] and any(o.code == "chased" for o in res.outcomes)


def test_chase_at_3_5_sigma_passes_the_gate(reg, policy):         # hard chase 4 sigma (2026-10-01)
    gw, skg = gws({"scout": s.scout(s.idea())}, {"skeptic": s.verdict(verdict="reject")})
    cards = {"ACME": s.card("ACME", sigma=3.5)}
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with(cards), skeptic_gw=skg))
    assert roles_called(skg) == ["skeptic"] and not any(o.code == "chased" for o in res.outcomes)


def test_gate_failure_drops_with_its_reason(reg, policy):
    gw, skg = gws({"scout": s.scout(s.idea())})
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}, {"ACME": "not_eligible_long"}),
                                 skeptic_gw=skg))
    assert [o.code for o in res.outcomes] == ["not_eligible_long"] and res.calls_used == 1


def test_open_trade_under_review_runs_debate_and_pm_without_ideas(reg, policy):
    t = s.trade()
    gw, skg = gws({"scout": s.scout(), "swing_bull": s.case("trade:t1", "NVDA"),
                   "swing_bear": s.case("trade:t1", "NVDA", bear=True),
                   "swing_pm": lambda u, r: {"actions": [{"ref": "trade:t1", "action": "exit",
                                                          "evidence_ids": ["X:NVDA:ret_5d"], "reason": "broke"}],
                                             "decisive_fact": {"text": "t", "evidence_id": "X:NVDA:ret_5d"},
                                             "dismissed": []}})
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(open_trades=[t]), gate=s.gate_with({}), skeptic_gw=skg))
    assert res.exits() == ["trade:t1"] and res.calls_used == 1 + 2 + 3


def test_code_exits_survive_and_are_not_reviewed(reg, policy):
    t = s.trade()
    gw, skg = gws({"scout": s.scout()})
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(open_trades=[t], code_exits=["trade:t1"]),
                                 gate=s.gate_with({}), skeptic_gw=skg))
    assert res.exits() == ["trade:t1"] and res.calls_used == 1


# ----------------------------------------------------------------------------- budget
@pytest.mark.parametrize(("ideas", "review", "cap", "expect"), [
    (3, 0, 9, (3, 3, True)),
    (0, 0, 9, (0, 0, False)),
    (0, 1, 9, (0, 3, True)),
    (3, 1, 8, (2, 3, True)),        # Skeptic beyond the first 2 ideas goes first
    (3, 0, 6, (2, 1, True)),        # then PM 3 -> 1
    (3, 0, 4, (2, 1, False)),       # then the debate
    (3, 0, 3, (1, 1, False)),
])
def test_budget_drop_order(ideas, review, cap, expect):
    plan = sc.plan_budget(ideas, review, max_calls=cap, max_skeptic=3, pm_replicates=3)
    assert (plan.skeptic_ideas, plan.pm_replicates, plan.debate) == expect
    assert plan.calls() <= cap or (ideas == 0 and review == 0)


def test_single_pm_replicate_enters_on_its_own_vote(reg, policy):
    """After the drop to one replicate an entry needs that replicate's enter AND the Skeptic's pass."""
    sw = policy.swing.model_copy(update={"llm": policy.swing.llm.model_copy(update={"max_calls_per_slot": 6})})
    pol = policy.model_copy(update={"swing": sw})
    main, sk = full_pass_responses(3)
    gw, skg = gws(main, sk)
    res = run(sc.run_swing_stage(gw, reg, pol, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    assert res.calls_used <= 6 and "budget_pm_1" in res.flags and "budget_skeptic_2" in res.flags
    assert [a.ref for a in res.entries()] == ["idea:1"] and res.entries()[0].replicates == 1
    assert any(o.ref == "idea:3" and o.code == "budget_no_skeptic" for o in res.outcomes)


# ---------------------------------------------------------------------------- deadline / errors
class StallingGateway:
    model = MAIN

    def __init__(self):
        self.calls = 0

    async def complete(self, **kw):
        self.calls += 1
        await asyncio.Event().wait()


def test_stalled_stub_hits_the_deadline_and_the_core_is_unaffected(reg, policy):
    async def scenario():
        core = asyncio.create_task(asyncio.sleep(0.01, result="core-decision"))
        res = await sc.run_swing_stage(StallingGateway(), reg, policy, s.inputs(code_exits=["trade:t9"]),
                                       gate=s.gate_with({}), deadline_s=0.05)
        return res, await core

    res, core = run(scenario())
    assert "swing_error:timeout" in res.flags and core == "core-decision"
    assert res.entries() == [] and res.exits() == ["trade:t9"]      # code exits need no LLM
    assert all(c is not None for c in res.calls)


def test_errors_never_raise(reg, policy):
    async def broken_gate(ideas):
        raise RuntimeError("boom")

    gw, skg = gws({"scout": s.scout(s.idea())})
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=broken_gate, skeptic_gw=skg))
    assert "swing_error:gate:RuntimeError" in res.flags and res.entries() == []


# ------------------------------------------------------------------------------ Skeptic model
def test_skeptic_falls_back_to_the_scout_model_with_a_flag(reg, policy):
    main, sk = full_pass_responses(1)
    gw = StubGateway(responses={**main, **sk}, model=MAIN)
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=None))
    assert "skeptic_same_model" in res.flags and res.skeptic_model == MAIN


def test_skeptic_outage_mid_call_falls_back(reg, policy):
    main, sk = full_pass_responses(1)
    gw = StubGateway(responses={**main, **sk}, model=MAIN)
    skg = StubGateway(responses={"skeptic": StubFailure("transport")}, model=OTHER)
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    assert "skeptic_same_model" in res.flags and [a.ref for a in res.entries()] == ["idea:1"]
    assert res.calls_used <= 9


def test_skeptic_retry_never_eats_the_pm_budget(reg, policy):
    """3 ideas plan all 9 calls: a Skeptic transport failure gets no retry (that idea drops), and
    the PM still runs its 3 replicates instead of aborting the slot with BudgetExceeded."""
    main, sk = full_pass_responses(3)
    gw = StubGateway(responses={**main, **sk}, model=MAIN)
    good = sk["skeptic"]

    def flaky(user: str, rep: int):
        if "IDEA TO REVIEW: idea:2\n" in user:
            return StubFailure("transport")
        return good(user, rep)

    skg = StubGateway(responses={"skeptic": flaky}, model=OTHER)
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    assert not any(f.startswith("swing_error") for f in res.flags), res.flags
    assert [a.ref for a in res.entries()] == ["idea:1"] and res.entries()[0].replicates == 3
    assert roles_called(gw).count("skeptic") == 0 and res.calls_used == 9
    assert any(o.ref == "idea:2" and o.code == "skeptic_failed" for o in res.outcomes)


def test_same_family_is_flagged(reg, policy):
    gw = StubGateway(responses={}, model="glm-5.2:cloud")
    skg = StubGateway(responses={}, model=OTHER)
    _, flags = run(sc.choose_skeptic(gw, skg, policy))
    assert flags == ["skeptic_same_model"]


# ---------------------------------------------------------------------------------- canary
def test_canary_never_reaches_the_pm(reg, policy):
    from council.swing.canary import PastEvent
    from council.swing.roles import CatalystMeta

    c = s.card("ACME", sigma=3.6, age=6)
    ev = PastEvent("ACME", "long", (CatalystMeta(s.P_ACME, s.SLOT.replace(day=18), frozenset({"ACME"}),
                                                 title="8-K: Results of Operations", form="8-K", items=("2.02",)),),
                   "8-K item 2.02 results filed", c)
    gw = StubGateway(responses={"skeptic": s.verdict("idea:1", verdict="pass"),
                                "swing_pm": s.pm(("idea:1", "enter"))}, model=MAIN)
    out = run(sc.run_canary(gw, reg, policy, ev, slot=s.SLOT))
    assert [c.role for c in gw.log] == ["skeptic"] and len(out.calls) == 1
    assert out.grade == "missed"
    assert "canary" not in (gw.log[0].user + gw.log[0].system).lower()      # the flag is invisible
    # the code guard: a canary idea handed to a later stage raises
    from council.swing.canary import build_canary
    planted: SwingIdea = build_canary(ev)
    with pytest.raises(CanaryLeak):
        sc.full_input([planted], [], catalysts={}, inputs=s.inputs())


# ------------------------------------------------------------------------------ desk line
def test_core_desk_line_is_one_line(reg, policy):
    main, sk = full_pass_responses(1)
    gw, skg = gws(main, sk)
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    line = sc.core_desk_line([s.trade(), s.trade("trade:t2")], gross_nav_pct=16, last=res)
    assert "\n" not in line and "2 open" in line and "1 new entry" in line and "16% of the book" in line
    assert "$" not in line


def test_a_heard_wait_the_pm_passed_is_its_own_paper_group(reg, policy):
    from council.cycle import _paper_group

    gw, skg = _wait_heard("partly", ("pass", "pass", "pass"))
    res = run(sc.run_swing_stage(gw, reg, policy, s.inputs(), gate=s.gate_with({}), skeptic_gw=skg))
    idea = res.ideas["idea:1"]
    assert _paper_group(res, "idea:1", idea, False, False) == "skeptic_wait_debated"
    assert _paper_group(res, "idea:1", idea, True, False) == "missed"
