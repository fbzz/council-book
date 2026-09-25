from __future__ import annotations

from typing import Literal

from pydantic import Field

from council.models.common import Strict, TrendState

DecisionBasis = Literal[
    "council",                     # medoid of agreeing PM replicates, banded
    "council_partial_reference",   # some instruments fell back to reference (no replicate agreement)
    "fallback_parse",              # < 2 valid replicates
    "fallback_disagreement",
    "council_unavailable",         # LLM outage -> reference
    "halted",                      # kill switch: flatten only
    "code_only",                   # blockers / compliance only
]


class Band(Strict):
    symbol: str
    trend: TrendState | None
    ref_level: float
    lo: float
    hi: float
    reasons: list[str] = Field(default_factory=list)
    qualifying_cards: list[str] = Field(default_factory=list)


class RiskCheck(Strict):
    rule_id: str                    # R1..R21
    name: str
    passed: bool
    value: float | str | None = None
    limit: float | str | None = None
    detail: str = ""
    kind: Literal["policy", "execution"] = "policy"


class RiskDecision(Strict):
    raw_levels: dict[str, float]
    banded_levels: dict[str, float]
    proposed_w: dict[str, float]    # after projection, before execution filters
    final_w: dict[str, float]       # after deadband/min-hold/cost gate etc.
    checks: list[RiskCheck]
    gross: float
    net: float
    margin_use: float
    stop_budget_used: float
    stop_budget_limit: float
    carry_bps_day: float
    ex_ante_vol: float
    basis: DecisionBasis
    hold_reasons: list[str] = Field(default_factory=list)
    compliance: list[str] = Field(default_factory=list)   # rule-driven risk reductions

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.kind == "policy")
