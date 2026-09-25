from __future__ import annotations

from typing import Literal

from pydantic import Field

from council.models.common import Strict


class Claim(Strict):
    claim_id: str = Field(pattern=r"^c\d+$")
    text: str = Field(max_length=300)
    evidence_ids: list[str] = Field(min_length=1, max_length=6)


class AdvocateCase(Strict):
    """Bull opening / bull rebuttal output."""

    argument: str = Field(max_length=1500)
    proposal: dict[str, float]                 # symbol -> level on the grid
    claims: list[Claim] = Field(max_length=6)
    strongest_opposing_fact_id: str
    concessions: list[str] = Field(default_factory=list, max_length=4)


class Rebuttal(Strict):
    claim_id: str
    verdict: Literal["concede", "refute"]
    text: str = Field(max_length=240)
    evidence_ids: list[str] = Field(default_factory=list, max_length=4)


class BearCase(AdvocateCase):
    rebuttals: list[Rebuttal] = Field(default_factory=list, max_length=6)
