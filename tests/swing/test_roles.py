"""SW-3: code rules on the swing roles' outputs (H3/H4/H4b/H6/H8/H10/H11, §1.5)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from council.swing import roles as r
from council.swing.models import ScoutOutput, SkepticVerdict, SwingPMDecision
from tests.swing import stubs as s

LIVE = ["news_continuation", "post_earnings_drift", "second_order"]
PAPER = ["gap_fade", "breakout", "mean_reversion", "event_run_up"]


def cats():
    return r.catalyst_index(s.reading(), [{"id": "M:ACME:unmoved", "line_id": "ACME"}], slot=s.SLOT,
                            screen_available_at=s.SLOT - timedelta(hours=16))


def accept(*ideas, **kw):
    return r.accept_ideas(ScoutOutput.model_validate(s.scout(*ideas)), catalysts=cats(), setups_live=LIVE,
                          setups_paper_only=PAPER, **kw)


# ----------------------------------------------------------------------------- accept_ideas
def test_h3_ids_must_be_admitted_this_slot():
    out = accept(s.idea(catalysts=[s.P_ACME, "P:deadbeef"]))
    assert [(d.ref, d.code) for d in out.drops] == [("idea:1", "catalyst_not_admitted")]


def test_late_items_are_not_admitted():
    late = s.news("P:99999999", symbols=["ACME"], hours_ago=-1)
    idx = r.catalyst_index([late], slot=s.SLOT)
    assert idx == {}


def test_h4_catalyst_must_be_about_the_ticker():
    assert accept(s.idea("ACME", catalysts=[s.P_WIDG])).drops[0].code == "catalyst_off_ticker"
    assert accept(s.idea("ACME", catalysts=[s.P_FED])).drops[0].code == "catalyst_off_ticker"   # market-wide alone
    assert accept(s.idea("ACME", catalysts=[s.P_ACME, s.P_FED])).accepted                     # market-wide supports
    assert accept(s.idea("ACME", catalysts=["M:ACME:unmoved"])).accepted                     # screen row
    ok = accept(s.idea("ACME", setup="second_order", catalysts=[s.P_GLOBEX]))
    assert [i.ref for i in ok.accepted] == ["idea:1"]


def test_h10_repitch_needs_newer_news():
    rejected = {"ACME": s.SLOT - timedelta(hours=10)}                  # after the 20h-old filing
    assert accept(s.idea(), recent_rejections=rejected).drops[0].code == "repitch_no_new_news"
    rejected = {"ACME": s.SLOT - timedelta(hours=40)}
    assert accept(s.idea(), recent_rejections=rejected).accepted


def test_paper_only_setup_leaves_the_llm_path_and_duplicates_drop():
    out = accept(s.idea(setup="gap_fade"), s.idea("ACME"), s.idea("WIDG", catalysts=[s.P_WIDG]))
    assert [i.ref for i in out.paper_only] == ["idea:1"]
    assert [d.code for d in out.drops] == ["duplicate_idea"]
    assert [i.ref for i in out.accepted] == ["idea:3"]


# ----------------------------------------------------------------------------- accept_verdict
def swing_idea(card=None, side="long", catalysts=None):
    idea = ScoutOutput.model_validate(s.scout(s.idea(side=side, catalysts=catalysts))).ideas[0]
    return r.SwingIdea(ref="idea:1", idea=idea, line_id="ACME", card=card or s.card("ACME", side))


ADM = {"X:ACME:rev_yoy", "X:ACME:dist_52w_high_pct", "X:ACME:move_since_news_close_sigma",
       "X:ACME:news_age_sessions", s.P_ACME}


def verdict(**kw):
    return SkepticVerdict.model_validate(s.verdict(**kw))


def judge(v, idea=None):
    return r.accept_verdict(v, idea=idea or swing_idea(), admissible=ADM, prior_wait_sigma=2.0)


def test_pass_goes_on():
    out = judge(verdict())
    assert out.status == "pass" and out.code is None


def test_mostly_plus_pass_now_passes():          # user decision 2026-10-01: no `mostly` override
    out = judge(verdict(priced_in="mostly"))
    assert (out.status, out.code) == ("pass", None) and "skeptic_mostly_wait" not in out.flags


def test_stale_or_restated_plus_pass_is_still_wait():
    for status in ("stale", "restated"):
        out = judge(verdict(news_status=status))
        assert (out.status, out.code) == ("wait", "skeptic_wait") and "skeptic_stale_wait" in out.flags


def test_fully_is_reject_and_incoherent_when_it_said_pass():
    out = judge(verdict(priced_in="fully"))
    assert (out.status, out.code) == ("reject", "skeptic_reject") and "skeptic_incoherent" in out.flags
    out = judge(verdict(priced_in="fully", verdict="wait"))
    assert out.status == "reject" and "skeptic_incoherent" not in out.flags


def test_stale_or_restated_pass_is_wait():
    for status in ("stale", "restated"):
        out = judge(verdict(news_status=status))
        assert out.status == "wait" and "skeptic_stale_wait" in out.flags


@pytest.mark.parametrize(("field", "value"), [("supports", False), ("side_ok", False)])
def test_catalyst_misread_drops(field, value):
    out = judge(verdict(**{field: value}))
    assert (out.status, out.code) == ("drop", "catalyst_misread")


def test_sigma_prior_pass_without_a_non_reaction_id_is_wait():
    hot = swing_idea(s.card("ACME", sigma=2.4, age=2))
    reaction_only = verdict(ids=["X:ACME:move_since_news_close_sigma", s.P_ACME])
    out = judge(reaction_only, hot)
    assert out.status == "wait" and "skeptic_prior_wait" in out.flags
    assert judge(verdict(), hot).status == "pass"               # cites rev_yoy: a fact the move lacks
    # the live value counts first; a short's prior uses the downward move
    assert judge(reaction_only, swing_idea(s.card("ACME", sigma=0.5, age=1, live_sigma=2.2))).status == "wait"
    short = swing_idea(s.card("ACME", "short", sigma=-2.5, age=1), side="short")
    assert judge(reaction_only, short).status == "wait"
    # same day as the news (age 0): no prior
    assert judge(reaction_only, swing_idea(s.card("ACME", sigma=2.4, age=0))).status == "pass"


def test_sigma_prior_price_or_market_ids_do_not_escape():
    """Under the prior, price-derived card fields and market rows are already in the move."""
    hot = swing_idea(s.card("ACME", sigma=2.4, age=2))
    adm = ADM | {"X:ACME:trend", "F:SPX:ret_5d", "P:macro1"}
    for escape in ("X:ACME:dist_52w_high_pct", "X:ACME:trend", "F:SPX:ret_5d", "P:macro1"):
        v = verdict(ids=[escape, s.P_ACME])
        out = r.accept_verdict(v, idea=hot, admissible=adm, prior_wait_sigma=2.0)
        assert out.status == "wait" and "skeptic_prior_wait" in out.flags, escape


def test_h6_unknown_ids_stripped_and_no_id_means_failed():
    out = judge(verdict(ids=["X:ACME:rev_yoy", "X:OTHER:rev_yoy"]))
    assert out.status == "pass" and "skeptic_ids_stripped" in out.flags
    assert len(out.verdict.reasons) == 1
    out = judge(verdict(ids=["X:OTHER:a", "X:OTHER:b"]))
    assert (out.status, out.code) == ("drop", "skeptic_failed")


def test_h8_wrong_ref_and_no_verdict_fail():
    assert judge(verdict(ref="idea:2")).code == "skeptic_failed"
    assert judge(None).code == "skeptic_failed"


def test_wait_disposition():
    old = s.card("ACME")
    assert r.wait_disposition(old, [s.P_ACME], s.card("ACME"), [s.P_ACME], sessions_parked=1) == "keep"
    assert r.wait_disposition(old, [s.P_ACME], s.card("ACME"), [s.P_ACME, s.N_ACME], sessions_parked=1) == "return"
    assert r.wait_disposition(old, [s.P_ACME], s.card("ACME", trend="mixed"), [s.P_ACME], sessions_parked=1) == "return"
    assert r.wait_disposition(old, [s.P_ACME], s.card("ACME"), [s.P_ACME], sessions_parked=3) == "expire"


# ------------------------------------------------------------------------ cases and PM actions
def test_accept_actions_refs_ids_and_canary():
    dec = SwingPMDecision.model_validate({
        "actions": [
            {"ref": "idea:1", "action": "enter", "stop_pct": 0.05, "target_pct": 0.1, "time_stop_days": 5,
             "evidence_ids": ["X:ACME:unknown"], "reason": "r"},
            {"ref": "idea:7", "action": "enter", "evidence_ids": ["X:ACME:rev_yoy"], "reason": "r"},
            {"ref": "trade:t1", "action": "exit", "evidence_ids": ["X:ACME:rev_yoy"], "reason": "r"},
        ],
        "decisive_fact": {"text": "t", "evidence_id": "X:ACME:rev_yoy"}, "dismissed": []})
    out = r.accept_actions(dec, idea_refs={"idea:1"}, trade_refs={"trade:t1"}, admissible=ADM)
    assert [(a.ref, a.action) for a in out] == [("idea:1", "pass"), ("trade:t1", "exit")]
    assert out[0].stop_pct is None
    with pytest.raises(r.CanaryLeak):
        r.accept_actions(dec, idea_refs={"idea:1"}, trade_refs=set(), admissible=ADM, canary_refs={"idea:1"})
    assert r.accept_actions(None, idea_refs=set(), trade_refs=set(), admissible=ADM) is None


def test_accept_case_drops_unknown_refs_and_idless_claims():
    from council.swing.models import SwingCase

    case = SwingCase.model_validate({"argument": "a", "strongest_opposing_fact_id": "X:ACME:rev_yoy", "claims": [
        {"claim_id": "c1", "ref": "idea:1", "text": "t", "evidence_ids": ["X:ACME:rev_yoy"]},
        {"claim_id": "c2", "ref": "idea:9", "text": "t", "evidence_ids": ["X:ACME:rev_yoy"]},
        {"claim_id": "c3", "ref": "idea:1", "text": "t", "evidence_ids": ["X:NOPE:x"]}]})
    clean, dropped = r.accept_case(case, refs={"idea:1"}, admissible=ADM)
    assert [c.claim_id for c in clean.claims] == ["c1"] and dropped == 2


def test_guard_no_canary():
    idea = swing_idea()
    r.guard_no_canary([idea])
    idea.canary = True
    with pytest.raises(r.CanaryLeak):
        r.guard_no_canary([idea])
