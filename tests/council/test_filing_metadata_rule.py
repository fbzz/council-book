"""T-D17 (transparency-v2 §3.4): 8-K / 6-K filing METADATA alone can never unlock a cut.

The news role sees an SEC item's form and item codes, never the filing's content. A `news_material`
card whose cited news items are all SEC filing items is kept, labelled `filing_metadata_only`, and
is never qualifying, even with a corroborating vol card on the same line. One non-SEC news item
among its evidence lets the usual corroboration rule decide again."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from council.deliberation.common import prompt_context
from council.deliberation.officers import vol_cards
from council.deliberation.roles import FILING_ONLY_NOTE, filing_metadata_only, run_news
from council.llm.stub import StubGateway
from council.models.cards import CardDraft
from council.models.facts import NewsItem, public_news_id

from .factories import SLOT, build_pack

AVAIL = SLOT - timedelta(hours=1)
SEC_ID = public_news_id("sec", "0001045810-26-000077")
FED_ID = public_news_id("fed_board", "guid-monetary-1")


def _pack_with_public_items():
    pack = build_pack()          # SEMIS carries a vol shock (ewma 2.3): a vol card corroborates it
    sec = NewsItem(id=SEC_ID, title="8-K: Item 2.02 Results of Operations and Financial Condition",
                   summary="EXAMPLE SEMICONDUCTOR CORP", symbols=["SEMIS"], published_at=AVAIL,
                   available_at=AVAIL, source="sec", form="8-K", items=["2.02"],
                   link="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=EXS&type=8-K")
    fed = NewsItem(id=FED_ID, title="Federal Reserve issues FOMC statement", summary="Rates unchanged.",
                   symbols=[], published_at=AVAIL, available_at=AVAIL, source="fed_board",
                   link="https://www.federalreserve.gov/newsevents/pressreleases/monetary20260930a.htm")
    return pack.model_copy(update={"news": [*pack.news, sec, fed]})


def _card(evidence: list[str]) -> dict:
    return {"scope": ["SEMIS"], "card_type": "news_material", "direction": "risk_down",
            "claim": "A current report on the semiconductor line.", "evidence_ids": evidence,
            "horizon_days": 5, "falsifier": "", "novel": True}


def _run(reg, pack, lines, policy, cards):
    corroborators = [(c, SLOT) for c in vol_cards(pack, policy)]
    assert any("SEMIS" in c.scope for c, _ in corroborators)
    return asyncio.run(run_news(
        gw=StubGateway({"news": {"cards": cards}}), reg=reg, pack=pack, desk_text="DESK",
        ctx=prompt_context(policy), lines=lines, policy=policy, corroborators=corroborators, now=SLOT,
    ))


def test_an_8k_item_plus_a_vol_card_does_not_qualify(reg, lines, policy):
    pack = _pack_with_public_items()
    run = _run(reg, pack, lines, policy, [_card([SEC_ID]), _card([SEC_ID, "V:SEMIS:ewma5_60"])])
    assert [c.card_id for c in run.cards] == ["K:news:1", "K:news:2"]     # kept, never dropped
    for card in run.cards:
        assert card.corroborated_by == ["K:vol:1"] and not card.qualifying
    assert run.dropped == [f"news card K:news:1: {FILING_ONLY_NOTE}", f"news card K:news:2: {FILING_ONLY_NOTE}"]


def test_a_non_sec_news_item_lets_corroboration_decide(reg, lines, policy):
    pack = _pack_with_public_items()
    run = _run(reg, pack, lines, policy, [_card([SEC_ID, FED_ID]), _card(["N:1a2b3c4d", SEC_ID])])
    assert all(c.qualifying and c.corroborated_by == ["K:vol:1"] for c in run.cards)
    assert run.dropped == []


def test_the_rule_reads_only_the_cited_news_items():
    pack = _pack_with_public_items()

    def draft(ids):
        return CardDraft(**_card(ids))

    assert filing_metadata_only(draft([SEC_ID]), pack)
    assert filing_metadata_only(draft([SEC_ID, "F:SEMIS:trend"]), pack)        # a fact is not news
    assert not filing_metadata_only(draft([SEC_ID, FED_ID]), pack)
    assert not filing_metadata_only(draft(["F:SEMIS:trend"]), pack)            # no news cited: unchanged
    assert not filing_metadata_only(draft(["N:1a2b3c4d"]), pack)
