"""S18: the swing budget the council decides each slot, and the core size it implies (user decision
2026-10-01: "the council thinks what is better"; `policy/swing.yaml` `budget`).

The budget
- Each swing PM replicate may output `swing_budget_pct` (0 to `budget.max_pct`, a multiple of
  `budget.step_pct`) with a one-line reason citing admissible evidence ids. A value off the grid or
  without an admissible id is no vote (the replicate's actions still count).
- Aggregation: the median of the valid votes, floored to the step (an even count can fall between
  steps; the lower step is the cautious one), then clamped to [open swing exposure, max_pct]: the
  budget never forces an open swing trade closed, it only limits NEW entries (`rules.book_check`,
  `S18:swing_budget_full`).
- No valid vote (the PM did not run, failed, or gave no usable budget): the last budget is kept
  (`budget.default_pct` on the first run), clamped the same way, flag `swing_budget_fallback`.

The core
- `budget.idle: core`: the core is sized to NAV x (1 - open swing exposure): the room the swing book
  does not use NOW, not the budget, so unused swing money is invested in the core, never idle cash.
  Its reference gross also stays within `reference_gross_max - swing` (R7's cash reserve holds for
  the whole book). Exposure is the SIZED exposure of the active swing trades (their size at entry,
  so a price move of an open trade never re-sizes the core).
- No churn: the exposure the core was last sized for is kept (runtime `CORE_SHARE_KEY`); the core is
  re-sized only when the exposure moved by >= `budget.core_rescale_deadband_nav` (= the smallest
  swing entry, so every entry and every exit re-sizes it, and nothing else does). On that cycle the
  risk engine orders the core lines to their re-sized targets (`core_rescale`, like a reference
  level step); every other gate (minimum trade size, R12 hold, R13-R15) still applies. Between
  re-sizes the normal R11 drift rule governs the core.

Pure: no I/O (the cycle reads and writes the runtime keys).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

FALLBACK_FLAG = "swing_budget_fallback"
BUDGET_KEY = "swing_budget"            # runtime: {"pct": last budget, "at": cycle id}
CORE_SHARE_KEY = "swing_core_share"    # runtime: {"swing_nav": exposure the core was last sized for}
EPS = 1e-9


def valid_vote(pct: Any, budget: Any) -> bool:
    """A replicate's `swing_budget_pct` is a vote when it is an integer on the policy grid."""
    if isinstance(pct, bool) or not isinstance(pct, int):
        return False
    return 0 <= pct <= int(budget.max_pct) and pct % int(budget.step_pct) == 0


@dataclass(frozen=True)
class BudgetDecision:
    pct: float                       # the budget, percent of NAV (after the clamp)
    median_pct: float | None         # the replicates' median on the grid (None: fallback)
    votes: int                       # valid votes
    fallback: bool
    open_pct: float                  # open swing exposure the clamp used, percent of NAV

    @property
    def nav(self) -> float:
        return self.pct / 100.0

    def flags(self) -> list[str]:
        return [FALLBACK_FLAG] if self.fallback else []


def decide_budget(votes: Iterable[Any], *, open_pct: float, last_pct: float | None, budget: Any) -> BudgetDecision:
    valid = [int(v) for v in votes if valid_vote(v, budget)]
    step = int(budget.step_pct)
    if valid:
        med: float | None = math.floor(float(median(valid)) / step + EPS) * step
        base = float(med)
    else:
        med = None
        base = float(last_pct) if isinstance(last_pct, int | float) and math.isfinite(last_pct) \
            else float(budget.default_pct)
    open_pct = max(0.0, float(open_pct))
    pct = max(min(base, float(budget.max_pct)), open_pct)
    return BudgetDecision(pct=round(pct, 4), median_pct=med, votes=len(valid), fallback=not valid,
                          open_pct=round(open_pct, 4))


def open_exposure(sizes: Iterable[float]) -> float:
    """Sized swing exposure as a NAV fraction: sum of |size at entry| of the active trades."""
    return float(sum(abs(float(s)) for s in sizes if isinstance(s, int | float) and math.isfinite(s)))


def sticky_exposure(now: float, applied: float | None, deadband: float) -> tuple[float, bool]:
    """(exposure the core is sized for this cycle, re-sized?). The applied exposure is kept until
    the current one moved by >= the deadband."""
    now = max(0.0, float(now))
    if applied is None or not math.isfinite(applied):
        return now, now > EPS
    if abs(now - applied) >= deadband - EPS:
        return now, abs(now - applied) > EPS
    return float(applied), False


def core_factor(swing_nav: float, *, ref_gross: float, gross_max: float) -> float:
    """The core's scale for a swing exposure: NAV x (1 - swing), with the core's reference gross
    never above gross_max - swing (so core + swing keeps R7's cash reserve). 1.0 when swing is 0."""
    s = min(max(float(swing_nav), 0.0), 1.0)
    f = 1.0 - s
    if ref_gross > EPS and ref_gross * f > gross_max - s + EPS:
        f = max(0.0, (gross_max - s) / ref_gross)
    return f


def scale_unit(unit: dict[str, float], factor: float) -> dict[str, float]:
    return {s: float(u) * factor for s, u in unit.items()}


def scale_reference(ref: Any, factor: float, *, swing_nav: float) -> Any:
    """The reference book with every unit weight and weight scaled by `factor` (levels unchanged)."""
    if abs(factor - 1.0) <= EPS:
        return ref
    entries = {s: e.model_copy(update={"unit_weight": e.unit_weight * factor, "weight_ref": e.weight_ref * factor})
               for s, e in ref.entries.items()}
    note = (f"book: core sized to {round(factor * 100, 2):g}% of its NAV share "
            f"(swing book {round(swing_nav * 100, 2):g}% of NAV, S18)")
    return ref.model_copy(update={"entries": entries, "k": ref.k * factor, "gross": ref.gross * factor,
                                  "ex_ante_vol": ref.ex_ante_vol * factor,
                                  "truncations": [*ref.truncations, note]})


@dataclass(frozen=True)
class Split:
    """The public split, percent of NAV: the budget, the open swing exposure, the core's share."""

    budget_pct: float
    swing_pct: float
    core_pct: float
    fallback: bool

    def public(self) -> dict[str, Any]:
        return {"swing_budget_pct": round(self.budget_pct, 2), "swing_pct": round(self.swing_pct, 2),
                "core_pct": round(self.core_pct, 2), "budget_fallback": self.fallback}


def split(budget_pct: float, swing_nav: float, core_nav_share: float, *, fallback: bool) -> Split:
    return Split(budget_pct=float(budget_pct), swing_pct=float(swing_nav) * 100.0,
                 core_pct=float(core_nav_share) * 100.0, fallback=bool(fallback))


def votes_of(decisions: Sequence[Any]) -> list[Any]:
    return [getattr(d, "swing_budget_pct", None) for d in decisions if d is not None]
