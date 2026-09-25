"""Medoid aggregation with per-line agreement: a real replicate's decision, never an average.

Rules:
  - Only VALID replicates count; each is its full (enforced) level vector, reference elsewhere.
  - Fewer than 2 valid replicates -> basis `fallback_parse`: every line at its reference level.
  - Medoid = the valid replicate with the minimum summed L1 distance to all valid replicates
    (ties -> the lowest replicate number).
  - Per line, the medoid's ACTION CLASS vs the current level is up / hold / down after the line's
    deadband (|move| >= deadband is a move). If no OTHER valid replicate shares that class, the line
    falls back to its reference level. This is the one jitter filter (no two-cycle confirmation).
  - Basis: `council` when no line fell back, else `council_partial_reference`.
  - `agreement[line]` = share of VALID replicates whose action class on that line matches the
    medoid's (the class that decided the line), rounded to 6 places; every line of `ref_levels`
    has an entry. Empty when there is no medoid (fallback_parse). It maps 1:1 onto
    `CycleRecord.agreement: dict[str, float]`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import Field

from council.models.common import Strict
from council.models.cycle import PMReplicate
from council.models.risk import DecisionBasis

EPS = 1e-9
ActionClass = Literal["up", "hold", "down"]


class AggregateResult(Strict):
    levels: dict[str, float]
    medoid_index: int | None                     # the medoid's replicate number
    basis: DecisionBasis
    per_line_fallback: list[str] = Field(default_factory=list)
    agreement: dict[str, float] = Field(default_factory=dict)   # per line: share of valid reps in medoid's class


def action_class(level: float, current: float, deadband: float) -> ActionClass:
    """up / hold / down relative to the current level; moves inside the deadband are holds."""
    move = level - current
    if move >= deadband - EPS:
        return "up"
    if move <= -deadband + EPS:
        return "down"
    return "hold"


def _vector(rep: PMReplicate, symbols: Sequence[str], ref: Mapping[str, float]) -> dict[str, float]:
    return {s: float(rep.enforced_levels.get(s, ref.get(s, 0.0))) for s in symbols}


def _deadband(deadband: float | Mapping[str, float], symbol: str) -> float:
    if isinstance(deadband, Mapping):
        return float(deadband.get(symbol, 0.0))
    return float(deadband)


def aggregate(
    replicates: Sequence[PMReplicate],
    ref_levels: Mapping[str, float],
    current_levels: Mapping[str, float],
    deadband: float | Mapping[str, float],
) -> AggregateResult:
    """Pick the medoid replicate and apply the per-line agreement fallback."""
    symbols = list(ref_levels)
    reference = {s: float(ref_levels[s]) for s in symbols}
    valid = [r for r in replicates if r.valid]
    if len(valid) < 2:
        return AggregateResult(levels=reference, medoid_index=None, basis="fallback_parse")

    vectors = [_vector(r, symbols, reference) for r in valid]

    def total_distance(i: int) -> float:
        return sum(
            sum(abs(vectors[i][s] - vectors[j][s]) for s in symbols) for j in range(len(valid))
        )

    best = min(range(len(valid)), key=lambda i: (round(total_distance(i), 9), valid[i].replicate))
    medoid = vectors[best]

    levels: dict[str, float] = {}
    fallback: list[str] = []
    agreement: dict[str, float] = {}
    for s in symbols:
        cur = float(current_levels.get(s, 0.0))
        db = _deadband(deadband, s)
        cls = action_class(medoid[s], cur, db)
        same = [i for i in range(len(valid)) if action_class(vectors[i][s], cur, db) == cls]
        agreement[s] = round(len(same) / len(valid), 6)
        if len(same) >= 2:
            levels[s] = medoid[s]
        else:
            levels[s] = reference[s]
            fallback.append(s)
    return AggregateResult(
        levels=levels,
        medoid_index=valid[best].replicate,
        basis="council_partial_reference" if fallback else "council",
        per_line_fallback=fallback,
        agreement=agreement,
    )
