"""News and macro analysts: card checks, code-assigned IDs, corroboration."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from council.deliberation.common import prompt_context
from council.deliberation.officers import vol_cards
from council.deliberation.roles import run_macro, run_news
from council.llm.stub import StubFailure, StubGateway

from .factories import SLOT, build_pack, news_reply, with_late_evidence


def news(gw, reg, pack, lines, policy, corroborators):
    return asyncio.run(run_news(
        gw=gw, reg=reg, pack=pack, desk_text="DESK", ctx=prompt_context(policy), lines=lines,
        policy=policy, corroborators=corroborators, now=SLOT,
    ))


def test_news_keeps_valid_drops_invalid_and_assigns_ids(reg, pack, lines, policy):
    vc = vol_cards(pack, policy)
    run = news(StubGateway({"news": news_reply()}), reg, pack, lines, policy, [(c, SLOT) for c in vc])
    assert [c.card_id for c in run.cards] == ["K:news:1", "K:news:2"]
    assert run.dropped == ["news draft 3: unknown_evidence N:deadbeef"]
    material, context = run.cards
    assert material.qualifying and material.corroborated_by == ["K:vol:1"]
    assert not context.qualifying and context.corroborated_by == []
    assert run.calls[0].status == "ok" and run.calls[0].role == "news"
    assert run.calls[0].prompt_id == "council-news/v1"


def test_news_material_without_vol_card_is_not_qualifying(reg, lines, policy):
    pack = build_pack(ewma={"SEMIS": 1.2})
    run = news(StubGateway({"news": news_reply()}), reg, pack, lines, policy, [])
    assert run.cards[0].card_type == "news_material" and not run.cards[0].qualifying


def test_corroboration_window_is_24h(reg, pack, lines, policy):
    vc = vol_cards(pack, policy)
    old = [(c, SLOT - timedelta(hours=25)) for c in vc]
    assert not news(StubGateway({"news": news_reply()}), reg, pack, lines, policy, old).cards[0].qualifying
    edge = [(c, SLOT - timedelta(hours=24)) for c in vc]
    assert news(StubGateway({"news": news_reply()}), reg, pack, lines, policy, edge).cards[0].qualifying


def test_corroboration_needs_a_shared_line(reg, lines, policy):
    pack = build_pack(ewma={"SEMIS": 1.0, "NDX": 2.5})
    vc = vol_cards(pack, policy)
    run = news(StubGateway({"news": news_reply()}), reg, pack, lines, policy, [(c, SLOT) for c in vc])
    assert not run.cards[0].qualifying  # the SEMIS material card; the vol card is on NDX


def test_news_scope_and_type_checks(reg, lines, policy):
    pack = build_pack(admitted=["NDX", "SPX"])
    reply = {"cards": [
        {"scope": ["SEMIS"], "card_type": "news_context", "direction": "neutral", "claim": "x",
         "evidence_ids": ["N:1a2b3c4d"], "horizon_days": 1},
        {"scope": ["NDX"], "card_type": "macro_context", "direction": "neutral", "claim": "x",
         "evidence_ids": ["N:1a2b3c4d"], "horizon_days": 1},
        {"scope": ["NDX"], "card_type": "news_context", "direction": "neutral", "claim": "ok",
         "evidence_ids": ["N:1a2b3c4d"], "horizon_days": 1},
    ]}
    run = news(StubGateway({"news": reply}), reg, pack, lines, policy, [])
    assert [c.claim for c in run.cards] == ["ok"] and run.cards[0].card_id == "K:news:1"
    assert run.dropped == ["news draft 1: scope_not_admitted SEMIS",
                           "news draft 2: type_not_allowed macro_context"]


def test_news_failure_returns_no_cards(reg, pack, lines, policy):
    run = news(StubGateway({"news": "not json"}), reg, pack, lines, policy, [])
    assert run.cards == [] and run.calls[0].status == "parse_fail"
    run = news(StubGateway({"news": StubFailure("timeout")}), reg, pack, lines, policy, [])
    assert run.cards == [] and run.calls[0].status == "timeout"


def test_news_user_text_has_detail_and_sanitized_feed(reg, pack, lines, policy):
    gw = StubGateway({"news": {"cards": []}})
    news(gw, reg, pack, lines, policy, [])
    user = gw.log[0].user
    assert "NEWS DETAIL" in user and "N:1a2b3c4d" in user
    assert "https://" not in user and "\x1b" not in user and "[amount]" in user


def test_macro_cleans_output(reg, pack, lines, policy):
    reply = {
        "regime": "risk_off",
        "drivers": [
            {"text": "Two-year yield rising.", "evidence_ids": ["M:DGS2@2026-09-30"]},
            {"text": "Invented series.", "evidence_ids": ["M:FAKE@2026-09-30"]},
        ],
        "sleeve_tilts": {"core": -1, "satellite": 1},
        "cards": [
            {"scope": ["EURUSD"], "card_type": "macro_context", "direction": "neutral",
             "claim": "Differential stable.", "evidence_ids": ["M:DGS2@2026-09-30"], "horizon_days": 20},
            {"scope": ["NDX"], "card_type": "news_material", "direction": "risk_down",
             "claim": "Wrong type.", "evidence_ids": ["M:DGS2@2026-09-30"], "horizon_days": 5},
        ],
    }
    run = asyncio.run(run_macro(
        gw=StubGateway({"macro": reply}), reg=reg, pack=pack, desk_text="DESK",
        ctx=prompt_context(policy), lines=lines, policy=policy, now=SLOT,
    ))
    assert run.output.regime == "risk_off"
    assert [d.text for d in run.output.drivers] == ["Two-year yield rising."]
    assert run.output.sleeve_tilts == {"core": -1}
    assert [c.card_id for c in run.cards] == ["K:macro:1"] and not run.cards[0].qualifying
    assert len(run.output.cards) == 1
    assert run.dropped == [
        "macro driver 2: unknown_evidence M:FAKE@2026-09-30",
        "macro tilt: unknown_sleeve satellite",
        "macro draft 2: type_not_allowed news_material",
    ]


def test_macro_failure(reg, pack, lines, policy):
    run = asyncio.run(run_macro(
        gw=StubGateway({}), reg=reg, pack=pack, desk_text="D", ctx=prompt_context(policy),
        lines=lines, policy=policy, now=SLOT,
    ))
    assert run.output is None and run.cards == [] and run.calls[0].status == "parse_fail"


def test_card_citing_late_news_is_dropped(reg, lines, policy):
    pack = with_late_evidence(build_pack())
    reply = {"cards": [{"scope": ["NDX"], "card_type": "news_context", "direction": "neutral",
                        "claim": "x", "evidence_ids": ["N:0badc0de"], "horizon_days": 1}]}
    run = news(StubGateway({"news": reply}), reg, pack, lines, policy, [])
    assert run.cards == [] and run.dropped == ["news draft 1: unknown_evidence N:0badc0de"]
