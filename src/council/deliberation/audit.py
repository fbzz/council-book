"""The auditor (code): checks one PM replicate and reverts what it cannot stand behind.

Rules (each deviation that breaks one is REVERTED to the reference level):
  - the symbol is a line, admitted this cycle, and allows council deviations (BTC/ETH do not);
  - at most `risk.authority.max_deviations_per_cycle` deviations (extras are reverted) and one per
    line (a repeat is reverted);
  - the level is snapped to the grid (`models.common.snap_level`);
  - every cited ID exists (a pack evidence ID available by the slot, or a card ID);
  - the direction enum matches the snapped move:
      cut: level < reference; add: level > current; short: level < 0;
      cover: current < 0 and current < level <= 0; lever: level > 1.0.
A replicate is INVALID when its decisive fact cites an unknown ID, or when at least half of its
listed deviations were reverted. A missing decision is invalid. Band clipping is not the auditor's
job: `enforce` (risk) clips levels to bands afterwards.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import Field

from council.deliberation.common import admissible_ids
from council.models.cards import EvidenceCard
from council.models.common import Strict, snap_level
from council.models.facts import FactPack
from council.models.pm import PMDecision
from council.models.risk import Band
from council.policy import LineSpec, Policy

EPS = 1e-9


class AuditResult(Strict):
    valid: bool
    violations: list[str] = Field(default_factory=list)
    reverted: list[str] = Field(default_factory=list)       # symbols whose deviation was reverted
    levels: dict[str, float] = Field(default_factory=dict)  # full vector after reverts


def direction_matches(direction: str, level: float, ref: float, current: float) -> bool:
    """Does the named direction describe the move to `level`? (see module rules)"""
    if direction == "cut":
        return level < ref - EPS
    if direction == "add":
        return level > current + EPS
    if direction == "short":
        return level < -EPS
    if direction == "cover":
        return current < -EPS and current + EPS < level <= EPS
    if direction == "lever":
        return level > 1.0 + EPS
    return False


def audit(
    decision: PMDecision | None,
    *,
    pack: FactPack,
    cards: Sequence[EvidenceCard],
    bands: Mapping[str, Band],
    ref_levels: Mapping[str, float],
    current_levels: Mapping[str, float],
    lines: Sequence[LineSpec],
    policy: Policy,
) -> AuditResult:
    """Audit one replicate. `bands` is accepted for the record/interface; clipping is enforce()'s."""
    del bands  # clipping to bands happens in enforce(), after the audit
    base = {sym: float(v) for sym, v in ref_levels.items()}
    if decision is None:
        return AuditResult(valid=False, violations=["no_decision"], levels=base)

    valid_ids = admissible_ids(pack) | {c.card_id for c in cards}
    by_symbol = {ln.symbol: ln for ln in lines}
    admitted = set(pack.admitted)
    max_dev = int(policy.risk["authority"]["max_deviations_per_cycle"])

    violations: list[str] = []
    reverted: list[str] = []
    levels = dict(base)
    decisive_ok = decision.decisive_fact.evidence_id in valid_ids
    if not decisive_ok:
        violations.append(f"decisive_fact: unknown_evidence {decision.decisive_fact.evidence_id}")

    accepted: set[str] = set()
    for idx, dev in enumerate(decision.deviations):
        sym = dev.symbol
        line = by_symbol.get(sym)
        ref = float(base.get(sym, 0.0))
        cur = float(current_levels.get(sym, 0.0))
        level = snap_level(float(dev.level))
        reason: str | None = None
        if idx >= max_dev:
            reason = "over_max_deviations"
        elif line is None:
            reason = "unknown_line"
        elif sym not in admitted:
            reason = "not_admitted"
        elif not line.council_deviations:
            reason = "reference_only"
        elif sym in accepted:
            reason = "duplicate"
        else:
            unknown = [e for e in dev.evidence_ids if e not in valid_ids]
            if unknown:
                reason = f"unknown_evidence {','.join(unknown)}"
            elif not direction_matches(dev.direction, level, ref, cur):
                reason = (
                    f"direction_mismatch {dev.direction} level {level:+.2f} "
                    f"ref {ref:+.2f} now {cur:+.2f}"
                )
        if reason is not None:
            violations.append(f"{sym}: {reason}")
            reverted.append(sym)
            continue
        accepted.add(sym)
        levels[sym] = level

    n = len(decision.deviations)
    mostly_reverted = n > 0 and 2 * len(reverted) >= n
    if mostly_reverted:
        violations.append(f"replicate: {len(reverted)} of {n} deviations reverted")
    return AuditResult(
        valid=decisive_ok and not mostly_reverted,
        violations=violations,
        reverted=reverted,
        levels=levels,
    )
