"""SW-2a: swing role models and the trade state table (design swing-book.md rev 2, §1.3/§1.5/§1.7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from council.swing import models as m


def _idea(**over) -> dict:
    base = dict(ticker="NVDA", side="long", setup="post_earnings_drift",
                catalyst_ids=["S:0001045810-26-000123", "N:ab12cd34"],
                catalyst_claim="Q3 revenue above prior guide; FY guide raised",
                thesis="Drift after a guide raise tends to persist for days.",
                why_not_priced_in="Move since report is under 1 sigma on average volume.",
                entry="now", stop_pct=0.05, target_pct=0.10, time_stop_days=10,
                invalidation="Price closes back below the pre-report level.")
    base.update(over)
    return base


def _verdict(**over) -> dict:
    base = dict(idea_ref="idea:1", catalyst_supports_claim=True, claim_supports_side=True,
                verdict="pass", priced_in="partly", news_status="new", regime="neutral",
                crowding="unknown",
                reasons=[{"text": "Move since news is small.", "evidence_ids": ["F:NVDA:move_sigma"]},
                         {"text": "Sector flat.", "evidence_ids": ["F:SEMIS:rel_move", "M:VIX@2026-09-25"]}],
                what_would_change_my_mind="A second day of heavy volume up.")
    base.update(over)
    return base


def _action(**over) -> dict:
    base = dict(ref="idea:1", action="enter", evidence_ids=["F:NVDA:atr14_pct"], reason="Skeptic pass.")
    base.update(over)
    return base


# --- Scout -------------------------------------------------------------------------------------

def test_scout_idea_valid_and_alias():
    idea = m.Idea.model_validate(_idea())
    assert isinstance(idea, m.ScoutIdea) and idea.ticker == "NVDA"
    assert m.ScoutIdea.model_validate(_idea(ticker="BRK.B")).ticker == "BRK.B"


@pytest.mark.parametrize("over", [
    {"extra_field": 1},
    {"ticker": "nvda"}, {"ticker": "TOOLONGSYM"},
    {"side": "flat"}, {"setup": "vibes"}, {"entry": "limit"},
    {"catalyst_ids": []}, {"catalyst_ids": ["P:a", "P:b", "P:c", "P:d", "P:e"]},
    {"catalyst_ids": ["X:abc"]}, {"catalyst_ids": ["N:a", "N:a"]},
    {"catalyst_claim": "x" * 121}, {"thesis": "x" * 401}, {"why_not_priced_in": "x" * 241},
    {"invalidation": "x" * 161}, {"catalyst_claim": "   "},
    {"stop_pct": 0.019}, {"stop_pct": 0.13}, {"target_pct": 0.029}, {"target_pct": 0.31},
    {"time_stop_days": 2}, {"time_stop_days": 16}, {"time_stop_days": 5.0},
    {"side": "short", "stop_pct": 0.09},
])
def test_scout_idea_rejects(over):
    with pytest.raises(ValidationError):
        m.ScoutIdea.model_validate(_idea(**over))


def test_short_stop_at_cap_ok():
    assert m.ScoutIdea.model_validate(_idea(side="short", stop_pct=0.08)).stop_pct == 0.08


def test_scout_output_caps():
    assert m.ScoutOutput.model_validate({"ideas": [], "passed": []}).ideas == []
    m.ScoutOutput.model_validate({"ideas": [_idea()] * 5, "passed": ["AMD"] * 10})
    with pytest.raises(ValidationError):
        m.ScoutOutput.model_validate({"ideas": [_idea()] * 6})
    with pytest.raises(ValidationError):
        m.ScoutOutput.model_validate({"passed": ["AMD"] * 11})
    with pytest.raises(ValidationError):
        m.ScoutOutput.model_validate({"ideas": [], "notes": "x"})


# --- Skeptic -----------------------------------------------------------------------------------

def test_skeptic_verdict_valid():
    v = m.SkepticVerdict.model_validate(_verdict())
    assert v.second_order is None
    assert v.evidence_ids() == ["F:NVDA:move_sigma", "F:SEMIS:rel_move", "M:VIX@2026-09-25"]


def test_skeptic_blind_fields_absent():
    fields = set(m.SkepticVerdict.model_fields)
    assert not fields & {"thesis", "why_not_priced_in", "setup", "stop_pct", "target_pct"}


@pytest.mark.parametrize("over", [
    {"idea_ref": "trade:7"}, {"idea_ref": "idea:"}, {"verdict": "maybe"}, {"priced_in": "some"},
    {"news_status": "old"}, {"regime": "bullish"}, {"crowding": "none"},
    {"reasons": [{"text": "one", "evidence_ids": ["F:A:b"]}]},
    {"reasons": [{"text": "r", "evidence_ids": ["F:A:b"]}] * 6},
    {"reasons": [{"text": "r", "evidence_ids": []}] * 2},
    {"reasons": [{"text": "r", "evidence_ids": ["F:A:b"] * 5}] * 2},
    {"reasons": [{"text": "x" * 201, "evidence_ids": ["F:A:b"]}] * 2},
    {"reasons": [{"text": "r", "evidence_ids": ["F:A:b"], "extra": 1}] * 2},
    {"what_would_change_my_mind": "x" * 161}, {"second_order": "x" * 161},
    {"thesis": "leak"},
])
def test_skeptic_verdict_rejects(over):
    with pytest.raises(ValidationError):
        m.SkepticVerdict.model_validate(_verdict(**over))


# --- PM ----------------------------------------------------------------------------------------

def _decision(actions) -> dict:
    return {"actions": actions, "decisive_fact": {"text": "Guide raised.", "evidence_id": "S:x"},
            "dismissed": []}


def test_pm_actions_valid():
    d = m.SwingPMDecision.model_validate(_decision([
        _action(), _action(ref="idea:2", action="pass"),
        _action(ref="trade:abc", action="hold", stop_pct=0.03),
        _action(ref="trade:def", action="exit")]))
    assert [a.action for a in d.actions] == ["enter", "pass", "hold", "exit"]


@pytest.mark.parametrize("over", [
    {"ref": "idea:1", "action": "exit"}, {"ref": "trade:9", "action": "enter"},
    {"ref": "ticker:NVDA"}, {"action": "buy"}, {"evidence_ids": []},
    {"evidence_ids": ["F:a:b"] * 7}, {"reason": "x" * 301},
    {"action": "pass", "stop_pct": 0.05}, {"ref": "trade:9", "action": "exit", "target_pct": 0.1},
    {"stop_pct": 0.2}, {"time_stop_days": 30},
])
def test_pm_action_rejects(over):
    with pytest.raises(ValidationError):
        m.SwingAction.model_validate(_action(**over))


def test_pm_one_action_per_ref():
    with pytest.raises(ValidationError):
        m.SwingPMDecision.model_validate(_decision([_action(), _action(action="pass")]))


def test_aggregate_needs_two_of_three():
    m.AggregatedSwingAction(ref="idea:1", action="enter", votes_for=2, stop_pct=0.05)
    m.AggregatedSwingAction(ref="trade:t1", action="hold", votes_for=1)
    with pytest.raises(ValidationError):
        m.AggregatedSwingAction(ref="idea:1", action="enter", votes_for=1)
    with pytest.raises(ValidationError):
        m.AggregatedSwingAction(ref="trade:t1", action="exit", votes_for=1)
    with pytest.raises(ValidationError):
        m.AggregatedSwingAction(ref="idea:1", action="hold", votes_for=3)
    with pytest.raises(ValidationError):
        m.AggregatedSwingAction(ref="idea:1", action="enter", votes_for=3, replicates=2)


# --- trade state table -------------------------------------------------------------------------

def test_state_table_covers_every_state():
    assert set(m.TRANSITIONS) == set(m.TRADE_STATES)
    for targets in m.TRANSITIONS.values():
        assert targets <= set(m.TRADE_STATES)


@pytest.mark.parametrize("src,dst", [
    ("proposed", "entry_executing"), ("entry_executing", "open"), ("entry_executing", "partial"),
    ("entry_executing", "open_tp_missing"), ("entry_executing", "entry_unknown"),
    ("entry_executing", "missed"), ("entry_unknown", "open"), ("open", "exit_pending"),
    ("exit_pending", "closed_time"), ("exit_pending", "closed_exit"), ("exit_pending", "closed_halt"),
    ("open", "closed_stop"), ("open", "closed_target"), ("partial", "closed_external"),
    ("open_tp_missing", "open"), ("exit_pending", "closed_unclassified"),
    ("exit_pending", "open"),          # SW-5b: a rejected / expired exit never strands the trade
])
def test_legal_transitions(src, dst):
    assert m.check_transition(src, dst) == dst


@pytest.mark.parametrize("src,dst", [
    ("proposed", "open"), ("open", "closed_time"), ("open", "proposed"), ("exit_pending", "missed"),
    ("closed_stop", "open"), ("closed_target", "closed_stop"), ("missed", "entry_executing"),
    ("open", "bogus"), ("bogus", "open"),
])
def test_illegal_transitions_raise(src, dst):
    assert not m.can_transition(src, dst)
    with pytest.raises(m.IllegalTransition):
        m.check_transition(src, dst)


def test_closed_trades_are_immutable():
    for state in m.TERMINAL_STATES:
        assert m.TRANSITIONS[state] == frozenset()
        for dst in m.TRADE_STATES:
            with pytest.raises(m.IllegalTransition):
                m.check_transition(state, dst)


def test_every_open_state_can_close_external_or_unclassified():
    for state in m.OPEN_STATES:
        assert {"closed_external", "closed_unclassified"} <= m.TRANSITIONS[state]


def test_swing_blocking_states():
    assert {"entry_unknown", "open_tp_missing"} == m.SWING_BLOCKING_STATES
    assert m.SWING_BLOCKING_STATES <= m.ACTIVE_STATES
    assert not m.ACTIVE_STATES & m.TERMINAL_STATES


def test_models_import_no_forbidden_modules():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(m))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    forbidden = ("council.ledger", "council.operator", "council.watch", "council.execution",
                 "council.cycle")
    assert not any(mod and mod.startswith(forbidden) for mod in mods)
