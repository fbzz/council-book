"""PM output = SPARSE deviations from the reference (at most N per cycle, set in policy).

Every line not listed stays at its reference level (or its current level when that is inside the
deadband). One call deciding every line at once is the batching the lab found inadmissible."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from council.models.common import Strict


class Deviation(Strict):
    symbol: str                                # a line symbol, e.g. NDX
    level: float                               # target level; snapped to the grid by code
    direction: Literal["cut", "add", "short", "cover", "lever"]
    evidence_ids: list[str] = Field(min_length=1, max_length=6)
    reason: str = Field(max_length=160)


class DecisiveFact(Strict):
    text: str = Field(max_length=200)
    evidence_id: str


class Dismissal(Strict):
    claim_id: str
    why: str = Field(max_length=160)


class PMDecision(Strict):
    deviations: list[Deviation] = Field(default_factory=list, max_length=3)
    decisive_fact: DecisiveFact
    sided_with: Literal["bull", "bear", "neither", "reference"]
    dismissed: list[Dismissal] = Field(default_factory=list, max_length=6)
    no_change_reason: str = Field(default="", max_length=200)

    def levels(self, reference_levels: dict[str, float]) -> dict[str, float]:
        """Full level vector: reference everywhere except the listed deviations."""
        out = dict(reference_levels)
        for dev in self.deviations:
            out[dev.symbol] = dev.level
        return out
