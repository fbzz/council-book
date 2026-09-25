"""Medoid aggregation and the per-line agreement fallback."""

from __future__ import annotations

import pytest

from council.deliberation.aggregate import action_class, aggregate
from council.models.cycle import PMReplicate

REF = {"A": 1.0, "B": 0.5, "C": 0.0}
CUR = {"A": 1.0, "B": 0.5, "C": 0.0}


def rep(i: int, valid: bool = True, **levels: float) -> PMReplicate:
    return PMReplicate(replicate=i, seed=42 + i, decision=None, valid=valid,
                       enforced_levels={**REF, **levels})


@pytest.mark.parametrize(
    ("level", "cur", "db", "cls"),
    [(0.75, 0.5, 0.25, "up"), (0.7, 0.5, 0.25, "hold"), (0.25, 0.5, 0.25, "down"),
     (0.5, 0.5, 0.25, "hold"), (1.0, 0.5, 0.5, "up"), (0.75, 0.5, 0.5, "hold")],
)
def test_action_class(level, cur, db, cls):
    assert action_class(level, cur, db) == cls


def test_fewer_than_two_valid_is_fallback_parse():
    res = aggregate([rep(0, A=0.5), rep(1, valid=False), rep(2, valid=False)], REF, CUR, 0.25)
    assert res.basis == "fallback_parse" and res.levels == REF and res.medoid_index is None
    assert aggregate([], REF, CUR, 0.25).basis == "fallback_parse"


def test_all_agree_is_council():
    res = aggregate([rep(0, A=0.5), rep(1, A=0.5), rep(2, A=0.5)], REF, CUR, 0.25)
    assert res.basis == "council" and res.levels["A"] == 0.5 and res.per_line_fallback == []
    assert res.agreement == {"A": 1.0, "B": 1.0, "C": 1.0}


def test_medoid_is_min_summed_l1_and_a_real_replicate():
    reps = [rep(0, A=0.0), rep(1, A=0.5, B=0.75), rep(2, A=0.5)]
    # distances: r0 = .5+.75=1.25 ... r1 = .5+.25+.25=1.0, r2 = .5+.25=0.75 -> r2
    res = aggregate(reps, REF, CUR, 0.25)
    assert res.medoid_index == 2
    assert res.levels == {"A": 0.5, "B": 0.5, "C": 0.0}   # never an average (A is not 0.33)


def test_medoid_tie_goes_to_lowest_replicate():
    res = aggregate([rep(0, A=0.5), rep(1, A=0.5)], REF, CUR, 0.25)
    assert res.medoid_index == 0


def test_medoid_scenarios():
    # r0 is closest to both others (D = .75 vs 1.0 and 1.25)
    res = aggregate([rep(0, A=0.5), rep(1, A=0.5, C=0.25), rep(2)], REF, CUR, 0.25)
    assert res.medoid_index == 0 and res.levels == {"A": 0.5, "B": 0.5, "C": 0.0}
    # r2 sits between r0 and r1 (D = .5 vs .75 and .75); every line has agreement
    res = aggregate([rep(0, A=0.5, C=0.25), rep(1, A=0.5, B=0.75), rep(2, A=0.5)], REF, CUR, 0.25)
    assert res.medoid_index == 2 and res.basis == "council" and res.per_line_fallback == []


def test_per_line_fallback_when_no_other_replicate_shares_the_class():
    # medoid r0 holds B while r1 and r2 both cut B: B has no agreement -> reference for B
    reps = [rep(0, A=0.5, C=0.25), rep(1, A=0.5, B=0.25), rep(2, B=0.25, C=0.25)]
    res = aggregate(reps, REF, CUR, 0.25)
    assert res.medoid_index == 0
    assert res.per_line_fallback == ["B"] and res.basis == "council_partial_reference"
    assert res.levels == {"A": 0.5, "B": 0.5, "C": 0.25}


def test_line_falls_back_to_reference():
    # medoid r0 holds A at current 0.5 (not the reference 1.0); the others move A down/up -> A
    # has no agreement and falls back to the reference.
    cur = {"A": 0.5, "B": 0.5, "C": 0.0}
    reps = [rep(0, A=0.5), rep(1, A=0.0), rep(2, A=1.0)]
    res = aggregate(reps, REF, cur, 0.25)
    assert res.medoid_index == 0
    assert res.per_line_fallback == ["A"] and res.levels["A"] == 1.0
    assert res.basis == "council_partial_reference"
    assert res.agreement["A"] == pytest.approx(1 / 3, abs=1e-6)


def test_invalid_replicates_are_ignored():
    reps = [rep(0, valid=False, A=0.0), rep(1, A=0.5), rep(2, A=0.5)]
    res = aggregate(reps, REF, CUR, 0.25)
    assert res.medoid_index == 1 and res.levels["A"] == 0.5 and res.basis == "council"


def test_per_line_deadband_mapping():
    # crypto-like wider deadband: 0.25 moves are holds, so every replicate is in the same class
    reps = [rep(0, A=0.75), rep(1), rep(2)]
    res = aggregate(reps, REF, CUR, {"A": 0.5, "B": 0.25, "C": 0.25})
    assert res.medoid_index == 1 and res.levels["A"] == 1.0 and res.basis == "council"
