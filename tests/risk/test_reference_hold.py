"""Lead addition: toward-reference legs amortise over the longer reference horizon (R15 calibration)."""

from datetime import UTC, datetime

from council.models.broker import CostQuote
from council.risk.costs import hold_days, passes_cost_gate


def _quote(bps: float) -> CostQuote:
    return CostQuote(symbol="CSPX.L", direction="long", settlement="real", leverage=1,
                     per_side_bps=bps, what_if_bps=None, carry_bps_day=0.0,
                     quoted_at=datetime(2026, 10, 1, tzinfo=UTC))


def test_reference_hold_is_longer(policy):
    assert hold_days("index", policy, toward_reference=True) == 90
    assert hold_days("index", policy) == 20
    assert hold_days("crypto", policy, toward_reference=True) == 120


def test_reference_aligned_spx_passes_but_same_leg_as_deviation_fails(policy):
    q = _quote(15.0)  # 5 bps floor + 10 bps slippage per side
    ok_ref, v_ref, _ = passes_cost_gate(q, 0.17, "index", policy, toward_reference=True)
    ok_dev, v_dev, _ = passes_cost_gate(q, 0.17, "index", policy, toward_reference=False)
    assert ok_ref and v_ref < 0.1
    assert not ok_dev and v_dev > 0.2
