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
    rule_id: str                    # R1..R21, plus R4d (re-entry cool-off) and MC (material change)
    name: str
    passed: bool
    value: float | str | None = None
    limit: float | str | None = None
    detail: str = ""
    kind: Literal["policy", "execution"] = "policy"


class RiskDecision(Strict):
    raw_levels: dict[str, float]
    banded_levels: dict[str, float]
    base_w: dict[str, float] = Field(default_factory=dict)   # the book the engine started from (snapshot)
    proposed_w: dict[str, float]    # after projection, before execution filters
    final_w: dict[str, float]       # after deadband/min-hold/cost gate etc.
    checks: list[RiskCheck]
    gross: float
    net: float
    margin_use: float
    stop_budget_used: float         # v1: stop-at-risk (reporting only; no stop budget)
    stop_budget_limit: float        # v1: cushion to the halt line
    carry_bps_day: float
    ex_ante_vol: float
    basis: DecisionBasis
    hold_reasons: list[str] = Field(default_factory=list)
    compliance: list[str] = Field(default_factory=list)   # rule-driven risk reductions

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.kind == "policy")


def changed_lines(decision: RiskDecision, eps: float = 1e-6) -> list[str]:
    """Lines whose final weight differs from the base book: the only lines the planner may touch."""
    lines = set(decision.final_w) | set(decision.base_w)
    return sorted(s for s in lines
                  if abs(decision.final_w.get(s, 0.0) - decision.base_w.get(s, 0.0)) > eps)
