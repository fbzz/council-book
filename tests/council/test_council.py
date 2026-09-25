"""End-to-end run_council with the stub gateway: determinism, budget, outage guard, enforce."""

from __future__ import annotations

import asyncio

import pytest

from council.deliberation.council import plan_stages, run_council
from council.deliberation.officers import event_cards, vol_cards
from council.llm.stub import StubFailure, StubGateway
from council.models.cycle import CycleRecord

from .factories import (
    REF_LEVELS,
    SEMIS_CUT,
    SLOT,
    build_bands,
    build_pack,
    build_ref,
    clip_enforce,
    pm_decision,
    stub_responses,
)


class Sleeper:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def council(gw, reg, policy, *, enforce=clip_enforce, sleep=None, bands=None, **kw):
    lines = list(policy.universe.lines)
    hints = {ln.symbol: {"per_side_bps": 5.0, "carry_bps_day": 0.0} for ln in lines}
    return asyncio.run(run_council(
        gw=gw, reg=reg, pack=build_pack(), ref=build_ref(),
        bands=build_bands() if bands is None else bands,
        current_levels={**REF_LEVELS, "OIL": -0.25}, cost_hints=hints, lines=lines, policy=policy,
        enforce=enforce, now=SLOT, sleep=sleep or Sleeper(), **kw,
    ))


def with_council(policy, **over):
    return policy.model_copy(update={"council": {**policy.council, **over}})


# ------------------------------------------------------------------------------- happy path
def test_end_to_end_decision(reg, policy):
    res = council(StubGateway(stub_responses()), reg, policy)
    assert res.basis == "council"
    assert res.aggregate.levels == {**REF_LEVELS, "SEMIS": 0.5}
    assert res.aggregate.medoid_index == 0
    ids = [c.card_id for c in res.cards]
    assert ids == ["K:vol:1", "K:event:1", "K:news:1", "K:news:2"]   # one market-wide event card
    news_material = res.cards[2]
    assert news_material.qualifying and news_material.corroborated_by == ["K:vol:1"]
    assert [c.role for c in res.calls] == [
        "news", "bull_open", "bear", "bull_rebuttal", "pm", "pm", "pm",
        "single_agent", "single_agent", "single_agent",
    ]
    assert all(c.status == "ok" for c in res.calls)
    assert [r.valid for r in res.pm] == [True, True, True]
    assert res.single_agent_aggregate is not None and res.single_agent_aggregate.levels == REF_LEVELS
    assert "news draft 3: unknown_evidence N:deadbeef" in res.dropped
    assert res.prompt_manifest_sha == reg.manifest_sha() and len(res.desk_sha) == 64
    assert res.macro is None


def test_byte_for_byte_deterministic(reg, policy):
    a = council(StubGateway(stub_responses()), reg, policy).model_dump_json()
    b = council(StubGateway(stub_responses()), reg, policy).model_dump_json()
    assert a == b


def test_single_agent_sees_desk_only(reg, policy):
    gw = StubGateway(stub_responses())
    council(gw, reg, policy)
    single = next(e.user for e in gw.log if e.role == "single_agent")
    pm = next(e.user for e in gw.log if e.role == "pm")
    assert "DEBATE TRANSCRIPT" in pm and "K:news:1" in pm
    assert "DEBATE TRANSCRIPT" not in single and "K:news:1" not in single
    assert "K:vol:1 [vol]" in single  # code cards are data, kept


def test_single_agent_can_be_turned_off(reg, policy):
    res = council(StubGateway(stub_responses()), reg, policy, run_single_agent=False)
    assert "single_agent" not in [c.role for c in res.calls] and res.single_agent == []
    assert res.single_agent_aggregate is None


def test_macro_runs_only_when_asked(reg, policy):
    res = council(StubGateway(stub_responses()), reg, policy)
    assert "macro" not in [c.role for c in res.calls]
    res = council(StubGateway(stub_responses()), reg, policy, run_macro=True)
    assert [c.role for c in res.calls][:2] == ["news", "macro"]
    assert res.macro is not None and res.macro.regime == "neutral"


# --------------------------------------------------------------------------------------- budget
def test_budget_within_limit_keeps_everything(policy):
    kept, flags = plan_stages(policy, run_macro=True, run_single_agent=True)
    assert kept == {"news", "macro", "single_agent_control"} and flags == []


def test_budget_drops_in_policy_order(policy):
    kept, flags = plan_stages(with_council(policy, max_calls_per_cycle=8), run_macro=True,
                              run_single_agent=True)
    assert kept == {"news", "macro"} and flags == ["budget_dropped:single_agent_control"]
    kept, flags = plan_stages(with_council(policy, max_calls_per_cycle=6), run_macro=True,
                              run_single_agent=True)
    assert kept == set()
    assert flags == ["budget_dropped:single_agent_control", "budget_dropped:macro",
                     "budget_dropped:news"]


def test_budget_drop_is_recorded_as_skipped_calls(reg, policy):
    res = council(StubGateway(stub_responses()), reg, with_council(policy, max_calls_per_cycle=7))
    skipped = [(c.role, c.status, c.error) for c in res.calls if c.status == "skipped"]
    assert skipped == [("single_agent", "skipped", "call_budget")] * 3
    made = [c for c in res.calls if c.status != "skipped"]
    assert len(made) <= 7 and res.basis == "council"


# ---------------------------------------------------------------------------------- outage guard
def test_pm_outage_exhausts_retries_then_reference(reg, policy):
    responses = {**stub_responses(), "pm": StubFailure("transport")}
    sleeper = Sleeper()
    res = council(StubGateway(responses), reg, policy, sleep=sleeper)
    assert sleeper.calls == [120.0, 120.0, 120.0]
    assert res.basis == "council_unavailable"
    assert res.aggregate.levels == REF_LEVELS and res.aggregate.medoid_index is None
    assert "stage_unavailable:pm" in res.flags
    assert [c.status for c in res.calls if c.role == "pm"] == ["transport"] * 12
    assert [c.status for c in res.calls if c.role == "single_agent"] == ["skipped"] * 3


def test_outage_recovers_after_one_wait(reg, policy):
    state = {"n": 0}

    def flaky_pm(user, replicate):
        state["n"] += 1
        if state["n"] <= 3:
            return StubFailure("timeout")
        return pm_decision([{"symbol": "SEMIS", "level": 0.5, "direction": "cut",
                             "evidence_ids": ["K:vol:1"], "reason": "Vol card."}])

    sleeper = Sleeper()
    res = council(StubGateway({**stub_responses(), "pm": flaky_pm}), reg, policy, sleep=sleeper)
    assert sleeper.calls == [120.0]
    assert res.basis == "council" and res.aggregate.levels["SEMIS"] == 0.5
    assert "outage:pm:retry1" in res.flags


def test_partial_failure_is_not_an_outage(reg, policy):
    def one_fails(user, replicate):
        return StubFailure("transport") if replicate == 2 else pm_decision(None, fact="F:NDX:trend")

    sleeper = Sleeper()
    res = council(StubGateway({**stub_responses(), "pm": one_fails}), reg, policy, sleep=sleeper)
    assert sleeper.calls == [] and res.basis == "council"


def test_early_outage_skips_later_stages(reg, policy):
    down = StubFailure("transport")
    responses = {k: down for k in stub_responses()}
    sleeper = Sleeper()
    res = council(StubGateway(responses), reg, policy, sleep=sleeper)
    assert sleeper.calls == [120.0, 120.0, 120.0]           # only the first stage retried
    assert res.basis == "council_unavailable" and "stage_unavailable:specialists" in res.flags
    assert {c.status for c in res.calls if c.role != "news"} == {"skipped"}


def test_parse_failures_are_fallback_parse_not_outage(reg, policy):
    responses = {**stub_responses(), "pm": "not json"}
    sleeper = Sleeper()
    res = council(StubGateway(responses), reg, policy, sleep=sleeper)
    assert sleeper.calls == [] and res.basis == "fallback_parse"
    assert res.aggregate.levels == REF_LEVELS


# ---------------------------------------------------------------------------------------- enforce
def test_enforce_is_applied_per_replicate(reg, policy):
    lever_gold = {"symbol": "GOLD", "level": 1.25, "direction": "lever",
                  "evidence_ids": ["F:GOLD:trend"], "reason": "x"}

    def pm(user, replicate):
        return pm_decision([lever_gold], fact="F:GOLD:trend", sided="bull")

    res = council(StubGateway({**stub_responses(), "pm": pm}), reg, policy)
    assert all(r.enforced_levels["GOLD"] == 1.0 for r in res.pm)       # band hi = 1.0
    assert res.enforce_notes["pm:0"] == ["GOLD: clipped +1.25 -> +1.00"]
    assert res.aggregate.levels["GOLD"] == 1.0


def test_broken_enforce_invalidates_replicates(reg, policy):
    def broken(levels, bands):
        raise ValueError("bug")

    res = council(StubGateway(stub_responses()), reg, policy, enforce=broken)
    assert all(not r.valid for r in res.pm) and res.basis == "fallback_parse"
    assert res.aggregate.levels == REF_LEVELS


def test_audit_results_recorded_on_replicates(reg, policy):
    bad = {"symbol": "BTC", "level": 0.5, "direction": "cut", "evidence_ids": ["F:BTC:trend"],
           "reason": "x"}

    def pm(user, replicate):
        return pm_decision([bad], fact="F:BTC:trend")

    res = council(StubGateway({**stub_responses(), "pm": pm}), reg, policy)
    assert all(r.reverted == ["BTC"] and not r.valid for r in res.pm)
    assert res.pm[0].audit_violations[0] == "BTC: reference_only"
    assert res.basis == "fallback_parse"


def test_line_dropped_by_enforcer_goes_to_reference(reg, policy):
    gold_add = {"symbol": "GOLD", "level": 0.75, "direction": "add",
                "evidence_ids": ["F:GOLD:trend"], "reason": "x"}

    def pm(user, replicate):
        return pm_decision([gold_add], fact="F:GOLD:trend", sided="bull")

    def drops_gold(levels, bands):  # like risk.enforce_authority: lines without a band are dropped
        return {s: v for s, v in levels.items() if s != "GOLD"}, ["GOLD"]

    res = council(StubGateway({**stub_responses(), "pm": pm}), reg, policy, enforce=drops_gold)
    assert all(r.enforced_levels["GOLD"] == REF_LEVELS["GOLD"] for r in res.pm)
    assert res.aggregate.levels["GOLD"] == REF_LEVELS["GOLD"]


# ------------------------------------------------------------------------------- code cards
def test_given_code_cards_are_used_not_rebuilt(reg, policy):
    pack = build_pack()
    only_vol = vol_cards(pack, policy)
    res = council(StubGateway(stub_responses()), reg, policy, code_cards=only_vol)
    assert [c.card_id for c in res.cards] == ["K:vol:1", "K:news:1", "K:news:2"]
    assert res.cards[1].qualifying and res.cards[1].corroborated_by == ["K:vol:1"]


def test_empty_code_cards_mean_no_corroboration(reg, policy):
    res = council(StubGateway(stub_responses()), reg, policy, code_cards=[])
    assert [c.card_id for c in res.cards] == ["K:news:1", "K:news:2"]
    assert not res.cards[0].qualifying and res.cards[0].corroborated_by == []


def test_code_cards_equal_rebuild_at_the_slot(reg, policy):
    pack = build_pack()
    cards = vol_cards(pack, policy) + event_cards(pack, SLOT, policy)
    a = council(StubGateway(stub_responses()), reg, policy, code_cards=cards).model_dump_json()
    b = council(StubGateway(stub_responses()), reg, policy).model_dump_json()
    assert a == b


# -------------------------------------------------------------------------------- bands_fn
def pinned_semis_bands(qualifying: list[str] | None = None):
    """Bands where SEMIS may be cut to 0.5 only when a qualifying card is listed."""
    bands = build_bands()
    q = qualifying or []
    bands["SEMIS"] = bands["SEMIS"].model_copy(
        update={"lo": 0.5 if q else 1.0, "qualifying_cards": q})
    return bands


class UnlockBands:
    """bands_fn stand-in: a qualifying NEWS card on SEMIS unlocks the cut (the code cards alone
    were deemed not to)."""

    def __init__(self):
        self.seen: list[list[str]] = []

    def __call__(self, cards):
        self.seen.append([c.card_id for c in cards])
        q = [c.card_id for c in cards if c.role == "news" and c.qualifying and "SEMIS" in c.scope]
        return pinned_semis_bands(q)


def semis_cutter(user, replicate):
    return pm_decision([SEMIS_CUT], fact="K:vol:1")


def row(text, symbol):
    return next(r for r in text.splitlines() if r.startswith(symbol + " "))


def test_bands_fn_recomputes_bands_after_the_specialists(reg, policy):
    gw = StubGateway({**stub_responses(), "single_agent": semis_cutter})
    fn = UnlockBands()
    res = council(gw, reg, policy, bands=pinned_semis_bands(), bands_fn=fn)
    assert fn.seen == [["K:vol:1", "K:event:1", "K:news:1", "K:news:2"]]    # once, ALL cards
    assert res.bands["SEMIS"].lo == 0.5 and res.bands["SEMIS"].qualifying_cards == ["K:news:1"]
    assert res.aggregate.levels["SEMIS"] == 0.5 and res.basis == "council"   # enforce used them
    by_role = {e.role: e.user for e in gw.log}
    assert "band [+0.50, +1.00] qualifying K:news:1" in row(by_role["pm"], "SEMIS")
    assert "band [+0.50, +1.00]" in row(by_role["bull_open"], "SEMIS")
    assert "band [+1.00, +1.00]" in row(by_role["news"], "SEMIS")      # before the news cards
    # the single-agent control keeps the code-card bands (its information set)
    assert "band [+1.00, +1.00]" in row(by_role["single_agent"], "SEMIS")
    assert all(r.enforced_levels["SEMIS"] == 1.0 for r in res.single_agent)


def test_without_bands_fn_the_given_bands_are_final(reg, policy):
    bands = pinned_semis_bands()
    res = council(StubGateway(stub_responses()), reg, policy, bands=bands)
    assert res.bands == bands
    assert res.aggregate.levels["SEMIS"] == 1.0          # the cut is clipped to the pinned band


def test_bands_fn_sees_code_cards_only_when_news_is_dropped(reg, policy):
    fn = UnlockBands()
    res = council(StubGateway(stub_responses()), reg, with_council(policy, max_calls_per_cycle=6),
                  bands=pinned_semis_bands(), bands_fn=fn)
    assert fn.seen == [["K:vol:1", "K:event:1"]]
    assert res.bands["SEMIS"].lo == 1.0 and res.aggregate.levels["SEMIS"] == 1.0


def test_broken_bands_fn_keeps_given_bands(reg, policy):
    def broken(cards):
        raise ValueError("bug")

    bands = pinned_semis_bands()
    res = council(StubGateway(stub_responses()), reg, policy, bands=bands, bands_fn=broken)
    assert "bands_fn_error ValueError" in res.flags
    assert res.bands == bands and res.aggregate.levels["SEMIS"] == 1.0


# ------------------------------------------------------------------------------- agreement
def test_agreement_is_per_line_and_fits_the_cycle_record(reg, policy):
    res = council(StubGateway(stub_responses()), reg, policy)
    agreement = res.aggregate.agreement
    assert set(agreement) == set(REF_LEVELS)
    assert agreement["SEMIS"] == pytest.approx(2 / 3, abs=1e-6)       # r0, r1 cut; r2 holds
    assert agreement["NDX"] == 1.0
    rec = CycleRecord(cycle_id="c", slot=SLOT, started_at=SLOT, status="dry_run", mode="test",
                      policy_sha="x", model="m", agreement=agreement,
                      medoid_replicate=res.aggregate.medoid_index)
    assert rec.agreement == agreement
