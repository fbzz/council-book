"""Structured drops (transparency-v2 §4.3, T5a-core): every drop code makes from an agent's output is
recorded as a `Drop` next to the old free-text note, and the council result collects them."""

from __future__ import annotations

import asyncio

from council.deliberation.common import prompt_context
from council.deliberation.council import run_council
from council.deliberation.debate import run_debate
from council.deliberation.officers import vol_cards
from council.deliberation.roles import run_macro, run_news
from council.llm.stub import StubGateway
from council.models.drops import DROP_CODES, Drop, drop_from_problem

from .factories import SLOT, build_pack, news_reply, stub_responses


def test_news_drops_mirror_the_notes(reg, pack, lines, policy):
    vc = vol_cards(pack, policy)
    run = asyncio.run(run_news(
        gw=StubGateway({"news": news_reply()}), reg=reg, pack=pack, desk_text="DESK",
        ctx=prompt_context(policy), lines=lines, policy=policy, corroborators=[(c, SLOT) for c in vc], now=SLOT,
    ))
    assert run.dropped == ["news draft 3: unknown_evidence N:deadbeef"]
    (drop,) = run.drops
    assert (drop.role, drop.what, drop.index, drop.code, drop.ids) == (
        "news", "card_draft", 3, "unknown_evidence", ["N:deadbeef"])
    assert drop.draft is not None and drop.lines == list(drop.draft.scope)


def test_scope_and_type_drops(reg, lines, policy):
    pack = build_pack(admitted=["NDX", "SPX"])
    reply = {"cards": [
        {"scope": ["SEMIS"], "card_type": "news_context", "direction": "neutral", "claim": "x",
         "evidence_ids": ["N:1a2b3c4d"], "horizon_days": 1},
        {"scope": ["NDX"], "card_type": "macro_context", "direction": "neutral", "claim": "x",
         "evidence_ids": ["N:1a2b3c4d"], "horizon_days": 1},
    ]}
    run = asyncio.run(run_news(
        gw=StubGateway({"news": reply}), reg=reg, pack=pack, desk_text="DESK", ctx=prompt_context(policy),
        lines=lines, policy=policy, corroborators=[], now=SLOT,
    ))
    assert [(d.code, d.target, d.lines) for d in run.drops] == [
        ("scope_not_admitted", "SEMIS", ["SEMIS"]), ("type_not_allowed", "macro_context", ["NDX"])]


def test_macro_drops(reg, pack, lines, policy):
    reply = {
        "regime": "risk_off",
        "drivers": [{"text": "Invented.", "evidence_ids": ["M:FAKE@2026-09-30"]}],
        "sleeve_tilts": {"core": -1, "satellite": 1},
        "cards": [],
    }
    run = asyncio.run(run_macro(gw=StubGateway({"macro": reply}), reg=reg, pack=pack, desk_text="DESK",
                                ctx=prompt_context(policy), lines=lines, policy=policy, now=SLOT))
    assert [(d.what, d.index, d.code, d.ids, d.target) for d in run.drops] == [
        ("macro_driver", 1, "unknown_evidence", ["M:FAKE@2026-09-30"], ""),
        ("macro_tilt", 2, "unknown_sleeve", [], "satellite"),
    ]


def test_debate_drops(reg, pack, lines, policy):
    responses = stub_responses()
    run = asyncio.run(run_debate(gw=StubGateway(responses), reg=reg, desk_text="DESK",
                                 ctx=prompt_context(policy), pack=pack, lines=lines))
    kinds = {(d.role, d.what, d.code, d.target) for d in run.drops}
    assert ("bull_open", "proposal_entry", "non_admitted_line", "XYZ") in kinds
    assert ("bear", "rebuttal", "unknown_bull_claim", "c9") in kinds
    assert len(run.drops) == len(run.notes)


def test_the_council_result_collects_every_drop(reg, pack, ref, bands, current, lines, policy):
    from .factories import clip_enforce

    async def instant(_s: float) -> None:
        return None

    res = asyncio.run(run_council(
        gw=StubGateway(stub_responses()), reg=reg, pack=pack, ref=ref, bands=bands, current_levels=current,
        cost_hints={ln.symbol: {"per_side_bps": 5.0, "carry_bps_day": 0.0} for ln in lines}, lines=lines,
        policy=policy, enforce=clip_enforce, now=SLOT, sleep=instant,
    ))
    notes = [n for n in res.dropped if "filing_metadata_only" not in n]
    assert res.drops and len(res.drops) == len(notes)
    assert all(d.code in DROP_CODES for d in res.drops)


def test_drop_from_problem():
    d = drop_from_problem("unknown_evidence N:1,N:2", role="news", index=4)
    assert d == Drop(role="news", what="card_draft", index=4, code="unknown_evidence", ids=["N:1", "N:2"])
