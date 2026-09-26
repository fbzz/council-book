"""R11 for reference-origin legs, the live-only exit (design §3.5, §17.2 #3-#4): a stock line whose
rule target is 0 and that still holds a position is ordered flat whatever its drift (a corporate
action credit, what a partial stop leaves), so its retiring line can always be pruned. Everything
else is the frozen `pending_trades` decision, untouched: core lines, and stock lines with a target."""

from __future__ import annotations

import numpy as np
import pytest

from council.reference import sleeve as sleeve_rule
from council.risk.churn import reference_pending

UNIT = 0.0625


def pending(policy, held_w, held_level, target_w, target_level, *, stocks=("A", "B", "C")):
    lines = list(held_w)
    return reference_pending(lines, held_w=held_w, held_level=held_level, target_w=target_w,
                             target_level=target_level, unit=dict.fromkeys(lines, UNIT), crypto_lines=(),
                             policy=policy, budget_lines=[s for s in lines if s in stocks], budget=0.5)


@pytest.mark.parametrize("residual", [0.004, 0.0155, 0.03])
def test_a_zero_target_stock_line_exits_whatever_its_drift(policy, residual):
    # held level 0 (a credit, or a stop reset it) and target 0: no level change, drift below 2%
    out = pending(policy, {"A": residual, "B": UNIT}, {"A": 0.0, "B": 1.0},
                  {"A": 0.0, "B": UNIT}, {"A": 0.0, "B": 1.0})
    assert out == {"A"}


def test_nothing_else_changes(policy):
    # a selected stock line drifting below the threshold, a flat zero-target line, a core line at
    # target 0 with a residual (not budgeted: the frozen rule alone)
    out = pending(policy, {"A": UNIT - 0.01, "B": 0.0, "SPX": 0.004}, {"A": 1.0, "B": 0.0, "SPX": 0.0},
                  {"A": UNIT, "B": 0.0, "SPX": 0.0}, {"A": 1.0, "B": 0.0, "SPX": 0.0})
    assert out == set()


def test_in_the_study_domain_the_exit_adds_nothing(policy):
    """Positions the studied rule can reach (every held line carries the non-zero level it last
    traded at) give exactly the frozen decision."""
    rng = np.random.default_rng(7)
    names = [f"S{i}" for i in range(12)]
    for _ in range(300):
        level = rng.choice([0.0, 1.0], size=len(names))
        held_level = rng.choice([0.0, 1.0], size=len(names))
        held = np.where(held_level > 0, UNIT * held_level * rng.uniform(0.4, 1.6, len(names)), 0.0)
        target = UNIT * level
        thr = np.full(len(names), sleeve_rule.drift_threshold(UNIT, 0.25, 0.02))
        frozen = sleeve_rule.pending_trades(held, held_level, target, level, thr,
                                            budget_mask=np.ones(len(names), dtype=bool), budget=0.5)
        live = reference_pending(names, held_w=dict(zip(names, held, strict=True)),
                                 held_level=dict(zip(names, held_level, strict=True)),
                                 target_w=dict(zip(names, target, strict=True)),
                                 target_level=dict(zip(names, level, strict=True)),
                                 unit=dict.fromkeys(names, UNIT), crypto_lines=(), policy=policy,
                                 budget_lines=names, budget=0.5)
        assert live == {s for s, f in zip(names, frozen, strict=True) if f}
