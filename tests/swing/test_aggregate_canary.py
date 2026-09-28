"""SW-3: PM aggregation (2 of 3) and the Skeptic's honesty checks (canary, pass-rate alarm, brake)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from council.swing import canary as c
from council.swing.aggregate import aggregate_actions
from council.swing.models import SkepticVerdict, SwingAction
from council.swing.roles import CatalystMeta, VerdictOutcome
from tests.swing import stubs as s


def act(ref, action, **kw):
    return SwingAction(ref=ref, action=action, evidence_ids=["X:ACME:rev_yoy"], reason="r", **kw)


def enter(ref, stop=0.05, target=0.1, ts=10):
    return act(ref, "enter", stop_pct=stop, target_pct=target, time_stop_days=ts)


def test_entry_needs_two_of_three_and_takes_medians():
    reps = [[enter("idea:1", 0.04, 0.12, 8)], [enter("idea:1", 0.06, 0.10, 12)], [act("idea:1", "pass")]]
    agg = aggregate_actions(reps, idea_refs=["idea:1"], trade_refs=[])
    (e,) = agg.entries()
    assert e.votes_for == 2 and e.stop_pct == pytest.approx(0.05) and e.target_pct == pytest.approx(0.11)
    assert e.time_stop_days == 10


def test_one_of_three_is_no_entry_and_failed_replicates_count_as_pass():
    agg = aggregate_actions([[enter("idea:1")], None, None], idea_refs=["idea:1"], trade_refs=[])
    assert agg.entries() == [] and agg.actions[0].action == "pass" and agg.actions[0].failed_replicates == 2


def test_exit_needs_two_of_three_else_hold():
    agg = aggregate_actions([[act("trade:t1", "exit")], [act("trade:t1", "hold")], []],
                            idea_refs=[], trade_refs=["trade:t1"])
    assert agg.exits() == [] and agg.actions[0].action == "hold"
    agg = aggregate_actions([[act("trade:t1", "exit")], [act("trade:t1", "exit")], []],
                            idea_refs=[], trade_refs=["trade:t1"])
    assert [a.ref for a in agg.exits()] == ["trade:t1"]


def test_room_caps_entries_by_votes_then_scout_order():
    reps = [[enter("idea:1"), enter("idea:2")], [enter("idea:1"), enter("idea:2")], [enter("idea:2")]]
    agg = aggregate_actions(reps, idea_refs=["idea:1", "idea:2"], trade_refs=[], max_entries=1)
    assert [a.ref for a in agg.entries()] == ["idea:2"] and agg.flags == ["no_room:idea:1"]


def test_single_replicate_majority():
    agg = aggregate_actions([[enter("idea:1")]], idea_refs=["idea:1"], trade_refs=[])
    assert [a.replicates for a in agg.entries()] == [1]


# ----------------------------------------------------------------------------------- canary
def event(sigma=3.5, age=6, side="long"):
    card = s.card("ACME", side, sigma=sigma, age=age)
    meta = CatalystMeta(s.P_ACME, s.SLOT - timedelta(days=9), frozenset({"ACME"}), title="8-K")
    return c.PastEvent("ACME", side, (meta,), "8-K item 2.02 results filed", card)


def test_canary_builder_requires_an_old_moved_event():
    idea = c.build_canary(event())
    assert idea.canary and idea.ref == c.CANARY_REF
    with pytest.raises(c.CanaryError, match="catalyst_too_recent"):
        c.build_canary(event(age=3))
    with pytest.raises(c.CanaryError, match="move_too_small"):
        c.build_canary(event(sigma=2.0))
    with pytest.raises(c.CanaryError, match="move_too_small"):
        c.build_canary(event(sigma=3.5, side="short"))          # moved up: not a short canary
    assert c.build_canary(event(sigma=-3.2, side="short")).canary


def test_canary_due_monday_1040_utc_only():
    monday = s.SLOT.replace(day=28, hour=10, minute=40)
    assert c.canary_due(monday) and not c.canary_due(monday.replace(hour=14))
    assert not c.canary_due(monday + timedelta(days=1))


def _outcome(verdict, priced_in):
    v = SkepticVerdict.model_validate(s.verdict(verdict=verdict, priced_in=priced_in))
    return VerdictOutcome("wait", None, v)


def test_grade_canary():
    assert c.grade_canary(_outcome("wait", "mostly")) == "caught"
    assert c.grade_canary(_outcome("reject", "fully")) == "caught"
    assert c.grade_canary(_outcome("pass", "partly")) == "missed"
    assert c.grade_canary(_outcome("wait", "partly")) == "missed"
    assert c.grade_canary(VerdictOutcome("drop", "skeptic_failed", None)) == "missed"


def test_pass_rate_alarm_fires_on_fixture():
    rubber_stamp = ["pass"] * 13 + ["wait"] * 5 + ["reject"] * 2          # 65% pass over 20
    assert c.pass_rate_alarm(rubber_stamp)
    healthy = (["pass", "wait", "reject", "wait"] * 5)
    assert not c.pass_rate_alarm(healthy)
    no_reject = ["reject"] * 10 + ["wait", "pass"] * 5                     # 25% pass, no reject in last 10
    assert c.pass_rate_alarm(no_reject)
    assert not c.pass_rate_alarm(["pass"] * 5)                            # too few verdicts


def test_brake():
    now = s.SLOT
    assert c.brake_engaged([now - timedelta(days=3), now - timedelta(days=20)], [], now=now)
    assert not c.brake_engaged([now - timedelta(days=3), now - timedelta(days=40)], ["caught"], now=now)
    assert c.brake_engaged([], ["caught", "missed", "missed"], now=now)
    assert not c.brake_engaged([], ["missed", "caught"], now=now)
