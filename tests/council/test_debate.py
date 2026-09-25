"""Debate: bull opening -> bear (sees bull) -> bull rebuttal (sees bear)."""

from __future__ import annotations

import asyncio

from council.deliberation.common import prompt_context
from council.deliberation.debate import claim_ids, run_debate, transcript
from council.llm.stub import StubFailure, StubGateway

from .factories import bear_reply, bull_reply, rebuttal_reply


def debate(gw, reg, pack, lines, policy):
    return asyncio.run(run_debate(gw=gw, reg=reg, desk_text="DESK", ctx=prompt_context(policy),
                                  pack=pack, lines=lines))


def responses(**over):
    base = {"bull_open": bull_reply(), "bear": bear_reply(), "bull_rebuttal": rebuttal_reply()}
    base.update(over)
    return base


def test_sequence_and_visibility(reg, pack, lines, policy):
    gw = StubGateway(responses())
    run = debate(gw, reg, pack, lines, policy)
    assert [c.role for c in run.calls] == ["bull_open", "bear", "bull_rebuttal"]
    assert [c.seed for c in run.calls] == [42, 43, 42]
    bull_user, bear_user, reb_user = (entry.user for entry in gw.log)
    assert "BULL OPENING" not in bull_user
    assert "claim c1: Nasdaq-100 uptrend intact." in bear_user
    assert "BEAR REPLY" in reb_user and "rebuttal of c2: refute" in reb_user
    assert run.debate.bull_open is not None and run.debate.bull_rebuttal is not None


def test_bear_rebuttals_must_name_bull_claims(reg, pack, lines, policy):
    run = debate(StubGateway(responses()), reg, pack, lines, policy)
    assert [r.claim_id for r in run.debate.bear.rebuttals] == ["c1", "c2"]
    assert "bear: rebuttal of unknown bull claim c9 dropped" in run.notes


def test_proposals_are_normalised(reg, pack, lines, policy):
    run = debate(StubGateway(responses()), reg, pack, lines, policy)
    assert run.debate.bull_open.proposal == {"GBPUSD": 0.25}   # 0.3 snapped; XYZ dropped
    assert "bull_open: proposal for non-admitted XYZ dropped" in run.notes


def test_bear_failure_skips_rebuttal(reg, pack, lines, policy):
    run = debate(StubGateway(responses(bear=StubFailure("timeout"))), reg, pack, lines, policy)
    assert [c.role for c in run.calls] == ["bull_open", "bear"]
    assert run.debate.bear is None and run.debate.bull_rebuttal is None


def test_bull_failure_still_runs_bear(reg, pack, lines, policy):
    gw = StubGateway(responses(bull_open="garbage"))
    run = debate(gw, reg, pack, lines, policy)
    assert run.debate.bull_open is None and run.debate.bear is not None
    assert "unavailable" in gw.log[1].user
    assert run.debate.bear.rebuttals == []  # nothing to rebut


def test_transcript_labels_claims_by_speaker(reg, pack, lines, policy):
    run = debate(StubGateway(responses()), reg, pack, lines, policy)
    text = transcript(run.debate)
    assert "claim bull_open:c1" in text and "claim bear:c1" in text
    assert "rebuttal of bull_open:c2" in text and "claim bull_rebuttal:c1" in text
    assert claim_ids(run.debate) == {"bull_open:c1", "bull_open:c2", "bear:c1", "bull_rebuttal:c1"}
