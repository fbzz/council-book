"""Evidence cards. LLM roles emit *drafts*; code assigns card IDs and validates cited evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from council.models.common import Strict

CardType = Literal[
    "news_material", "news_context", "filing_material", "filing_context",
    "macro_context", "event_binary", "vol_shock", "sector_rank",
]
CardDirection = Literal["risk_up", "risk_down", "neutral"]
# Card types that may justify a CUT in an uptrend. Must match policy risk.authority.qualifying_cut_cards;
# a card also needs qualifying=True (news_material only when a code vol card corroborates it).
# Event cards never qualify: they only block adds.
QUALIFYING_TYPES: frozenset[str] = frozenset({"vol_shock", "news_material"})


class CardDraft(Strict):
    """What an LLM analyst returns for one card (no ID; code assigns it)."""

    scope: list[str] = Field(min_length=1, max_length=9)
    card_type: CardType
    direction: CardDirection
    claim: str = Field(max_length=200)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    horizon_days: Literal[1, 5, 20, 60]
    falsifier: str = Field(default="", max_length=160)
    novel: bool = True


class EvidenceCard(CardDraft):
    card_id: str = Field(pattern=r"^K:[a-z_]+:\d+$")
    role: str
    issued_at: datetime | None = None
    corroborated_by: list[str] = Field(default_factory=list)   # code card IDs (news_material rule)
    qualifying: bool = False


class NewsAnalystOutput(Strict):
    cards: list[CardDraft] = Field(max_length=8)


class FilingAnalystOutput(Strict):
    cards: list[CardDraft] = Field(max_length=3)
    guidance_llm: Literal["raise", "lower", "maintain", "none"] = "none"
    items: list[str] = Field(default_factory=list)


class MacroDriver(Strict):
    text: str = Field(max_length=200)
    evidence_ids: list[str] = Field(min_length=1)


class MacroAnalystOutput(Strict):
    regime: Literal["risk_on", "neutral", "risk_off"]
    drivers: list[MacroDriver] = Field(max_length=4)
    sleeve_tilts: dict[str, Literal[-1, 0, 1]] = Field(default_factory=dict)
    cards: list[CardDraft] = Field(default_factory=list, max_length=3)


class SectorPick(Strict):
    symbol: str
    rank: int = Field(ge=1)
    evidence_ids: list[str] = Field(min_length=1)
    reason: str = Field(max_length=160)


class SectorAnalystOutput(Strict):
    peer_group: str
    ranked: list[SectorPick] = Field(max_length=10)
    excluded: list[str] = Field(default_factory=list)
