"""Post-trade reconcile: did the book land where the plan said, and is every position protected?

Rules:
- achieved line weight = Σ snapshot.signed_w over the symbols that map to the line
  (vehicle symbols and the line symbol itself); drift = Σ over target lines of
  |achieved − target|, and must be ≤ risk.reconcile.drift_max.
- EVERY position must carry a stop-loss (a missing one is listed by symbol).
- A position on a symbol that maps to no line (including UNMAPPED_<id>) is an unknown position.
- Each expected position (from a fill) must exist with the expected direction and leverage, and
  its stop rate must match within risk.reconcile.sl_rate_tolerance (relative).
- ok = drift within limit AND no missing stop AND no unknown position AND no issue. Drift is
  reported separately from `issues` (a partial execution expects drift, not a broken position).
  `protected` = no missing stop, no unknown position, no issue.
Issue strings are public-safe: symbols and rule names only, never ids or amounts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pydantic import Field

from council.execution.planner import vehicle_to_line
from council.models.broker import ExposureSnapshot
from council.models.common import Direction, Strict
from council.policy import Policy

DRIFT_EPS = 1e-9


class ExpectedPosition(Strict):
    """A position the executor saw filled. PRIVATE (position id)."""

    position_id: int
    symbol: str
    direction: Direction
    leverage: int = 1
    sl_rate: float | None = None


class ReconcileResult(Strict):
    ok: bool
    drift: float
    drift_max: float
    missing_sl: list[str] = Field(default_factory=list)
    unknown_positions: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    achieved_w: dict[str, float] = Field(default_factory=dict)

    @property
    def drift_ok(self) -> bool:
        return self.drift <= self.drift_max + DRIFT_EPS

    @property
    def protected(self) -> bool:
        return not self.missing_sl and not self.unknown_positions and not self.issues


def line_weights(snapshot: ExposureSnapshot, policy: Policy) -> tuple[dict[str, float], list[str]]:
    """(achieved weight by line, symbols that map to no line)."""
    v2l = vehicle_to_line(policy.universe)
    achieved: dict[str, float] = {}
    unknown: list[str] = []
    for symbol, w in snapshot.signed_w.items():
        line = v2l.get(symbol)
        if line is None:
            if abs(w) > 0 and symbol not in unknown:
                unknown.append(symbol)
            continue
        achieved[line] = achieved.get(line, 0.0) + w
    for p in snapshot.positions:
        if v2l.get(p.symbol) is None and p.symbol not in unknown:
            unknown.append(p.symbol)
    return achieved, sorted(unknown)


def reconcile(
    snapshot: ExposureSnapshot,
    target_w: Mapping[str, float],
    expected: Iterable[ExpectedPosition],
    policy: Policy,
) -> ReconcileResult:
    cfg = policy.risk["reconcile"]
    drift_max = float(cfg["drift_max"])
    sl_tol = float(cfg["sl_rate_tolerance"])
    achieved, unknown = line_weights(snapshot, policy)
    drift = sum(abs(achieved.get(line, 0.0) - float(w)) for line, w in target_w.items())
    missing_sl = sorted({p.symbol for p in snapshot.positions if p.sl_rate is None or p.sl_rate <= 0})
    issues: list[str] = []
    by_id = {p.position_id: p for p in snapshot.positions}
    for exp in expected:
        pos = by_id.get(exp.position_id)
        if pos is None:
            issues.append(f"{exp.symbol}: expected position missing")
            continue
        if ("long" if pos.is_buy else "short") != exp.direction:
            issues.append(f"{exp.symbol}: direction mismatch")
        if pos.leverage != exp.leverage:
            issues.append(f"{exp.symbol}: leverage mismatch")
        if exp.sl_rate and pos.sl_rate and abs(pos.sl_rate / exp.sl_rate - 1) > sl_tol:
            issues.append(f"{exp.symbol}: stop-loss rate differs from the approved rate")
    ok = drift <= drift_max + DRIFT_EPS and not missing_sl and not unknown and not issues
    return ReconcileResult(
        ok=ok, drift=drift, drift_max=drift_max, missing_sl=missing_sl,
        unknown_positions=unknown, issues=issues, achieved_w=achieved,
    )
