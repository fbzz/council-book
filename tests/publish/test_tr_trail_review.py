"""Stage-2 review of the decision trail: an approved change whose fill is not resolved yet is not
"awaiting the operator" (the human step says approved)."""

from __future__ import annotations

from council.publish import trail
from tests.fixtures import trail_records as tr


def test_an_approved_change_in_flight_is_not_awaiting_the_operator():
    got = {t.line: t for t in trail.record_trails(tr.cut_executed(), names=tr.NAMES, decision_state="executing",
                                                  leg_states={"SEMIS": ["in_flight"]})}
    semis = got["SEMIS"]
    assert semis.outcome == "pending" and semis.stopped_at == "execution"
    assert "awaiting the operator" not in semis.headline
    assert semis.headline.endswith("approved; execution not resolved")
    assert any(s.stage == "human" and s.words.startswith("approved") for s in semis.steps)


def test_a_change_before_the_decision_still_awaits_the_operator():
    got = {t.line: t for t in trail.record_trails(tr.cut_executed(), names=tr.NAMES, decision_state="proposed")}
    assert got["SEMIS"].headline.endswith("proposed; awaiting the operator")
