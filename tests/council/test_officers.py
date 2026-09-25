"""Code officers: vol_shock and event_binary cards, and card expiry."""

from __future__ import annotations

from datetime import timedelta

import pytest

from council.deliberation import officers
from council.deliberation.officers import (
    card_expired,
    card_expires_at,
    event_cards,
    vol_cards,
    vol_fact_id,
)
from council.models.cards import CardDraft, EvidenceCard
from council.models.facts import EventItem

from .factories import SLOT, build_pack


# ------------------------------------------------------------------------------------ vol officer
@pytest.mark.parametrize(("ratio", "fires"), [(2.0, True), (2.5, True), (1.99, False)])
def test_vol_card_threshold(policy, ratio, fires):
    pack = build_pack(ewma={"SEMIS": ratio})
    cards = [c for c in vol_cards(pack, policy) if c.scope == ["SEMIS"]]
    assert bool(cards) is fires


def test_vol_card_fields(policy, pack):
    cards = vol_cards(pack, policy)
    assert [c.card_id for c in cards] == ["K:vol:1"]
    card = cards[0]
    assert card.card_type == "vol_shock" and card.direction == "risk_down" and card.role == "vol"
    assert card.qualifying is True and card.scope == ["SEMIS"]
    assert card.evidence_ids == ["V:SEMIS:ewma5_60"]
    assert card.evidence_ids[0] in pack.evidence_ids()


def test_vol_cards_numbered_in_universe_order(policy):
    pack = build_pack(ewma={"GBPUSD": 3.0, "NDX": 2.1, "SEMIS": 2.3})
    cards = vol_cards(pack, policy)
    assert [(c.card_id, c.scope[0]) for c in cards] == [
        ("K:vol:1", "NDX"), ("K:vol:2", "SEMIS"), ("K:vol:3", "GBPUSD")
    ]


def test_vol_card_skips_non_admitted_lines(policy):
    pack = build_pack(admitted=["NDX"])
    assert vol_cards(pack, policy) == []


def test_vol_fact_id_fallback(policy):
    pack = build_pack()
    pack = pack.model_copy(update={"facts": [f for f in pack.facts if f.id != "V:SEMIS:ewma5_60"]})
    assert vol_fact_id(pack, "SEMIS") == "V:SEMIS:vol_ratio"
    pack = pack.model_copy(update={"facts": [f for f in pack.facts if not f.id.startswith("V:SEMIS")]})
    assert vol_fact_id(pack, "SEMIS") == "V:SEMIS:ewma5_60"


# ---------------------------------------------------------------------------------- event officer
def fomc(hours: float, symbols=None, binary=True) -> EventItem:
    return EventItem(id="E:fomc@x", kind="fomc", at_utc=SLOT + timedelta(hours=hours),
                     symbols=symbols or [], binary=binary, severity=3, source="calendar")


@pytest.mark.parametrize(
    ("hours", "fires"),
    [(20, True), (24, True), (24.5, False), (-1, True), (-2, True), (-2.5, False)],
)
def test_event_window(policy, hours, fires):
    pack = build_pack(events=[fomc(hours)])
    assert bool(event_cards(pack, SLOT, policy)) is fires


def test_max_scope_matches_card_draft_schema():
    limits = [m.max_length for m in CardDraft.model_fields["scope"].metadata
              if getattr(m, "max_length", None) is not None]
    assert limits == [officers.MAX_SCOPE] and officers.MAX_SCOPE == 9


def test_market_wide_event_is_one_card(policy):
    pack = build_pack(events=[fomc(10)])
    cards = event_cards(pack, SLOT, policy)
    assert [c.card_id for c in cards] == ["K:event:1"]
    assert cards[0].scope == [ln.symbol for ln in policy.universe.lines]
    assert not cards[0].qualifying and cards[0].card_type == "event_binary"
    assert cards[0].evidence_ids == ["E:fomc@x"] and cards[0].direction == "neutral"


def test_event_card_splits_scope_wider_than_max(policy, monkeypatch):
    monkeypatch.setattr(officers, "MAX_SCOPE", 6)
    pack = build_pack(events=[fomc(10)])
    cards = event_cards(pack, SLOT, policy)
    assert [c.card_id for c in cards] == ["K:event:1", "K:event:2"]
    assert all(len(c.scope) <= 6 for c in cards)
    assert [s for c in cards for s in c.scope] == [ln.symbol for ln in policy.universe.lines]
    assert all(not c.qualifying and c.card_type == "event_binary" for c in cards)


def test_event_card_symbol_scope_and_non_binary(policy):
    pack = build_pack(events=[fomc(5, symbols=["SEMIS", "AAPL"])])
    cards = event_cards(pack, SLOT, policy)
    assert len(cards) == 1 and cards[0].scope == ["SEMIS"]
    assert event_cards(build_pack(events=[fomc(5, binary=False)]), SLOT, policy) == []
    assert event_cards(build_pack(events=[fomc(5, symbols=["AAPL"])]), SLOT, policy) == []


# ----------------------------------------------------------------------------------------- expiry
def test_event_card_expires_two_hours_after_event(policy):
    pack = build_pack(events=[fomc(3)])
    card = event_cards(pack, SLOT, policy)[0]
    at = card_expires_at(card, created_at=SLOT, pack=pack, policy=policy)
    assert at == SLOT + timedelta(hours=5)
    assert not card_expired(card, now=at - timedelta(minutes=1), created_at=SLOT, pack=pack, policy=policy)
    assert card_expired(card, now=at, created_at=SLOT, pack=pack, policy=policy)
    gone = build_pack(events=[])
    assert card_expired(card, now=SLOT, created_at=SLOT, pack=gone, policy=policy)


@pytest.mark.parametrize(("ratio", "expired"), [(1.49, True), (1.5, False), (2.4, False)])
def test_vol_card_expires_when_ratio_back_under_1_5(policy, ratio, expired):
    card = vol_cards(build_pack(), policy)[0]
    later = build_pack(ewma={"SEMIS": ratio})
    assert card_expired(card, now=SLOT, created_at=SLOT, pack=later, policy=policy) is expired
    assert card_expires_at(card, created_at=SLOT, pack=later, policy=policy) is None


def test_vol_card_with_unknown_ratio_does_not_expire(policy):
    card = vol_cards(build_pack(), policy)[0]
    pack = build_pack()
    states = {k: v for k, v in pack.states.items() if k != "SEMIS"}
    pack = pack.model_copy(update={"states": states})
    assert card_expired(card, now=SLOT, created_at=SLOT, pack=pack, policy=policy) is False


def test_other_cards_expire_after_horizon(policy, pack):
    card = EvidenceCard(card_id="K:news:1", role="news", scope=["NDX"], card_type="news_material",
                        direction="risk_down", claim="x", evidence_ids=["N:1a2b3c4d"], horizon_days=5)
    created = SLOT
    assert not card_expired(card, now=created + timedelta(days=5) - timedelta(seconds=1),
                            created_at=created, pack=pack, policy=policy)
    assert card_expired(card, now=created + timedelta(days=5), created_at=created, pack=pack, policy=policy)
