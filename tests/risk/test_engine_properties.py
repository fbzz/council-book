"""Property tests: random council levels, current books and market states never produce a final book
that breaks R1-R7, HALTED always flattens, WARN never increases |w|, and risk classification is
consistent."""

from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from council.models.common import LEVEL_GRID
from council.policy import Policy
from council.risk import checks as ck
from council.risk.config import risk_limits
from council.risk.engine import ForbiddenLegError, classify_risk_increasing
from tests.risk.helpers import NOW, quotes_for, run, states_for

POLICY = Policy.load()
LIM = risk_limits(POLICY)
LINES = [ln.symbol for ln in POLICY.universe.lines]
CLASS = {ln.symbol: ln.asset_class for ln in POLICY.universe.lines}
IN_REF = {ln.symbol: ln.in_reference for ln in POLICY.universe.lines}
TREND_LEVEL = {"up": 1.0, "mixed": 0.5, "down": 0.25}


@st.composite
def cases(draw, kill_states=("NORMAL", "WARN")):
    trend = {s: draw(st.sampled_from(["up", "mixed", "down"])) for s in LINES}
    units = {s: draw(st.floats(min_value=0.02, max_value=0.9)) for s in LINES}
    levels = {s: draw(st.sampled_from(LEVEL_GRID)) for s in LINES}
    cur_levels = {s: draw(st.sampled_from([-0.5, -0.25, 0.0, 0.25, 0.5, 1.0, 1.25])) for s in LINES}
    per_line = {
        s: {
            "ewma5_60_ratio": draw(st.sampled_from([1.0, 1.0, 2.5, 3.5])),
            "ret1d_sigma": draw(st.sampled_from([0.0, 0.0, 3.0, -3.0])),
            "market_open": draw(st.sampled_from([True, True, True, False])),
            "data_age_h": draw(st.sampled_from([2.0, 2.0, 40.0])),
        }
        for s in LINES
    }
    return {
        "trend": trend,
        "units": units,
        "levels": levels,
        "current": {s: cur_levels[s] * units[s] for s in LINES if cur_levels[s]},
        "per_line": per_line,
        "lever_ok": set(draw(st.lists(st.sampled_from(LINES), unique=True))),
        "short_ok": set(draw(st.lists(st.sampled_from(LINES), unique=True))),
        "kill_state": draw(st.sampled_from(kill_states)),
        "material": draw(st.booleans()),
        "per_side": draw(st.sampled_from([1.0, 10.0, 60.0])),
        "carry": draw(st.sampled_from([0.0, 1.0, 6.0])),
        "turnover_7d": draw(st.sampled_from([0.0, 0.5, 1.2])),
        "last_change_days": draw(st.sampled_from([None, 1, 10])),
    }


def evaluate(case):
    ref = {s: (TREND_LEVEL[case["trend"][s]] if IN_REF[s] else 0.0) for s in LINES}
    last = ({} if case["last_change_days"] is None
            else {s: NOW - timedelta(days=case["last_change_days"]) for s in LINES})
    return run(
        POLICY,
        trend=case["trend"],
        states=states_for(POLICY, case["trend"], **case["per_line"]),
        unit_weights=case["units"],
        ref=ref,
        levels=case["levels"],
        current=case["current"],
        kill_state=case["kill_state"],
        lever_ok=case["lever_ok"],
        short_ok=case["short_ok"],
        material_changed=case["material"],
        cost_quotes=quotes_for(POLICY, per_side=case["per_side"], carry=case["carry"]),
        turnover_7d=case["turnover_7d"],
        last_change=last,
    )


def within_strict_limits(w, units) -> bool:
    caps_ok = all(abs(w.get(s, 0.0)) <= cap + 1e-9 for s, cap in LIM.caps.line.items())
    crypto = ck.group_gross(w, [s for s in LINES if CLASS[s] == "crypto"])
    fx = ck.group_gross(w, [s for s in LINES if CLASS[s] == "fx"])
    beta = ck.group_gross(w, LIM.caps.equity_beta_cluster.members)
    return (
        caps_ok
        and crypto <= LIM.caps.crypto_total + 1e-9
        and fx <= LIM.caps.fx_total + 1e-9
        and beta <= LIM.caps.equity_beta_cluster.max + 1e-9
        and ck.gross(w) <= LIM.gross.proposal_max + 1e-9
        and LIM.net.min - 1e-9 <= ck.net(w) <= LIM.net.max + 1e-9
        and ck.short_gross(w) <= LIM.net.short_gross_max + 1e-9
        and ck.margin_use(w, units) <= LIM.margin_use_max + 1e-9
    )


@settings(max_examples=250, deadline=None)
@given(cases())
def test_every_policy_check_passes_on_the_final_book(case):
    d = evaluate(case)
    failed = [(c.rule_id, c.name, c.value, c.limit, c.detail) for c in d.checks
              if c.kind == "policy" and not c.passed]
    assert d.passed, failed


@settings(max_examples=250, deadline=None)
@given(cases())
def test_feasible_current_book_stays_strictly_within_r1_to_r7(case):
    d = evaluate(case)
    if within_strict_limits(case["current"], case["units"]):
        assert within_strict_limits(d.final_w, case["units"])
        assert all(ck.line_leverage(d.final_w[s] / case["units"][s]) <= LIM.leverage_caps[CLASS[s]]
                   or abs(d.final_w[s]) <= abs(case["current"].get(s, 0.0)) + 1e-9
                   for s in LINES)


@settings(max_examples=150, deadline=None)
@given(cases(kill_states=("WARN",)))
def test_warn_never_increases_any_line(case):
    d = evaluate(case)
    for s in LINES:
        before, after = case["current"].get(s, 0.0), d.final_w[s]
        assert abs(after) <= abs(before) + 1e-9
        assert before * after >= -1e-18  # no flip


@settings(max_examples=100, deadline=None)
@given(cases(kill_states=("HALTED", "FLAT")))
def test_halted_always_flattens(case):
    d = evaluate(case)
    assert d.basis == "halted" and all(v == 0.0 for v in d.final_w.values())


@settings(max_examples=150, deadline=None)
@given(cases())
def test_final_changes_respect_deadband_and_leg_budget(case):
    # compliance de-risking (gross above hard max) is exempt from the deadband and the leg cap
    assume(ck.gross(case["current"]) <= LIM.gross.hard_max)
    d = evaluate(case)
    changed = [s for s in LINES if abs(d.final_w[s] - case["current"].get(s, 0.0)) > 1e-9]
    legs = sum(2 if case["current"].get(s, 0.0) * d.final_w[s] < 0 else 1 for s in changed)
    assert legs <= LIM.proposal.max_legs
    for s in changed:
        step = abs(d.final_w[s] - case["current"].get(s, 0.0)) / case["units"][s]
        need = LIM.deadband.level_crypto if CLASS[s] == "crypto" else LIM.deadband.level
        assert step >= need - 1e-9


_w = st.floats(min_value=-2.0, max_value=2.0, allow_nan=False)


@given(_w, _w)
def test_risk_classification_properties(before, after):
    inc = classify_risk_increasing(before, after)
    if before * after < 0 and abs(after) > 1e-9:
        assert inc  # a flip always increases risk
    if abs(after) < abs(before) - 1e-9 and before * after >= 0:
        assert not inc  # a pure reduction never does
    if abs(before) <= 1e-12 and abs(after) > 1e-9:
        assert inc  # a new position always does
    assert classify_risk_increasing(before, after, sl_widened=True)
    with pytest.raises(ForbiddenLegError):
        classify_risk_increasing(before, after, sl_removed=True)
