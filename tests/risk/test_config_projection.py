"""Typed policy views and the signed box + L1 projection (unit and property tests)."""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from council.invariants import InvariantViolation
from council.risk.config import cost_floors, risk_limits
from council.risk.engine import RiskEngine
from council.risk.projection import (
    min_l1_in_box,
    project_or_nearest,
    project_signed_box_l1,
    weighted_l1,
)
from council.risk.stops import DEFAULT_SL_BUFFER_PP
from tests.risk.helpers import override

# ----------------------------------------------------------------------------- config


def test_shipped_policy_parses(policy):
    lim = risk_limits(policy)
    costs = cost_floors(policy)
    assert lim.gross.proposal_max == 1.90 and lim.gross.hard_max == 2.00
    assert lim.killswitch.peak == "lifetime"
    assert costs.slippage_buffer_bps == 10
    assert lim.catastrophe_stop.margin_pct_buffer_pp == DEFAULT_SL_BUFFER_PP


def test_misspelt_risk_key_fails_loudly(policy):
    bad = override(policy, "risk", {"gross.proposl_max": 1.9})
    with pytest.raises(ValueError):
        risk_limits(bad)


def test_engine_refuses_policy_looser_than_invariants(policy):
    with pytest.raises(InvariantViolation):
        RiskEngine(override(policy, "risk", {"gross.hard_max": 2.5}))
    with pytest.raises(InvariantViolation):
        RiskEngine(override(policy, "risk", {"killswitch.halt_at": 0.70}))
    RiskEngine(policy)  # the shipped policy is accepted


# ----------------------------------------------------------------------------- projection


def test_projection_unchanged_when_feasible():
    v = np.array([0.5, -0.3, 0.2])
    out = project_signed_box_l1(v, np.full(3, -1.0), np.full(3, 1.0), 1.0)
    np.testing.assert_array_equal(out, v)


def test_projection_at_exact_cap_is_unchanged():
    v = np.array([0.6, -0.4])
    out = project_signed_box_l1(v, np.full(2, -1.0), np.full(2, 1.0), 1.0)
    np.testing.assert_array_equal(out, v)


def test_projection_soft_thresholds_equally():
    v = np.array([0.8, -0.6, 0.1])
    out = project_signed_box_l1(v, np.full(3, -1.0), np.full(3, 1.0), 1.0)
    # lambda = 0.2: 0.6, -0.4, 0.0
    np.testing.assert_allclose(out, [0.6, -0.4, 0.0], atol=1e-9)
    assert weighted_l1(out) <= 1.0 + 1e-12


def test_projection_respects_box_that_excludes_zero():
    v = np.array([0.9, 0.9])
    lo = np.array([0.5, 0.0])
    out = project_signed_box_l1(v, lo, np.array([1.0, 1.0]), 1.0)
    assert out[0] >= 0.5 - 1e-12 and weighted_l1(out) <= 1.0 + 1e-12


def test_projection_weighted_cost():
    v = np.array([1.0, 1.0])
    cost = np.array([0.5, 1.0])  # e.g. margin 1/L with L = 2 and 1
    out = project_signed_box_l1(v, np.zeros(2), np.full(2, 2.0), 0.9, cost=cost)
    assert weighted_l1(out, cost) == pytest.approx(0.9, abs=1e-9)
    assert out[0] > out[1]  # cheaper coordinate keeps more


def test_projection_infeasible_raises_and_nearest_falls_back():
    lo = np.array([0.6, 0.6])
    with pytest.raises(ValueError):
        project_signed_box_l1(np.array([1.0, 1.0]), lo, np.ones(2), 1.0)
    out, ok = project_or_nearest(np.array([1.0, 1.0]), lo, np.ones(2), 1.0)
    assert not ok
    np.testing.assert_allclose(out, lo)


@pytest.mark.parametrize(
    "v,lo,hi,cost",
    [
        ([1.0], [0.0, 0.0], [1.0], None),          # shape mismatch
        ([1.0], [1.0], [0.0], None),               # lo > hi
        ([np.nan], [0.0], [1.0], None),            # not finite
        ([1.0], [0.0], [1.0], [0.0]),              # non-positive cost
    ],
)
def test_projection_rejects_bad_inputs(v, lo, hi, cost):
    with pytest.raises(ValueError):
        project_signed_box_l1(np.array(v), np.array(lo), np.array(hi), 1.0,
                              cost=None if cost is None else np.array(cost))


_vals = st.floats(min_value=-3.0, max_value=3.0, allow_nan=False, allow_infinity=False)


@st.composite
def projection_case(draw):
    n = draw(st.integers(min_value=1, max_value=9))
    v = np.array(draw(st.lists(_vals, min_size=n, max_size=n)))
    a = np.array(draw(st.lists(_vals, min_size=n, max_size=n)))
    b = np.array(draw(st.lists(_vals, min_size=n, max_size=n)))
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    cost = np.array(draw(st.lists(st.sampled_from([0.5, 1.0, 1.0, 2.0]), min_size=n, max_size=n)))
    floor = min_l1_in_box(lo, hi, cost)
    cap = floor + draw(st.floats(min_value=0.0, max_value=6.0))
    return v, lo, hi, cost, cap


@settings(max_examples=300, deadline=None)
@given(projection_case())
def test_projection_properties(case):
    v, lo, hi, cost, cap = case
    out = project_signed_box_l1(v, lo, hi, cap, cost=cost)
    # feasible
    assert np.all(out >= lo - 1e-9) and np.all(out <= hi + 1e-9)
    assert weighted_l1(out, cost) <= cap + 1e-7
    # idempotent
    np.testing.assert_allclose(project_signed_box_l1(out, lo, hi, cap, cost=cost), out, atol=1e-12)
    # never further from zero than the plain clip
    assert np.all(np.abs(out) <= np.abs(np.clip(v, lo, hi)) + 1e-9)


@settings(max_examples=200, deadline=None)
@given(projection_case())
def test_projection_unchanged_when_already_feasible(case):
    v, lo, hi, cost, cap = case
    clipped = np.clip(v, lo, hi)
    assume(weighted_l1(clipped, cost) <= cap)
    np.testing.assert_array_equal(project_signed_box_l1(v, lo, hi, cap, cost=cost), clipped)


@settings(max_examples=200, deadline=None)
@given(projection_case(), st.floats(min_value=0.0, max_value=1.0))
def test_projection_is_nearest_feasible_point(case, t):
    """No feasible point on the segment from the box-min point to the clip is closer to v."""
    v, lo, hi, cost, cap = case
    out = project_signed_box_l1(v, lo, hi, cap, cost=cost)
    candidate = np.clip(0.0, lo, hi) + t * (np.clip(v, lo, hi) - np.clip(0.0, lo, hi))
    assume(weighted_l1(candidate, cost) <= cap)
    assert np.linalg.norm(out - v) <= np.linalg.norm(candidate - v) + 1e-7
