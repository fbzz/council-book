from __future__ import annotations

from typing import Literal

from pydantic import Field

from council.models.common import Strict


class PMTarget(Strict):
    symbol: str
    level: float                               # snapped to the grid by code
    evidence_ids: list[str] = Field(default_factory=list, max_length=6)
    reason: str = Field(default="", max_length=160)


class DecisiveFact(Strict):
    text: str = Field(max_length=200)
    evidence_id: str


class Dismissal(Strict):
    claim_id: str
    why: str = Field(max_length=160)


class PMDecision(Strict):
    targets: list[PMTarget]
    decisive_fact: DecisiveFact
    sided_with: Literal["bull", "bear", "neither", "reference"]
    dismissed: list[Dismissal] = Field(default_factory=list, max_length=6)
    no_change_reason: str = Field(default="", max_length=200)

    def levels(self) -> dict[str, float]:
        return {t.symbol: t.level for t in self.targets}
