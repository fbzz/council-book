"""council.stocks.score: the four-feature score and the name selection frozen by the pre-registered
stock-sleeve study (moved unchanged from scripts/stock_sleeve_study.py before the tag).

The selection digest pins the moved functions' output on the study's synthetic fixture: every date's
order by both scores and every cell's picks with and without the hold buffer, computed with the
pre-move code. It never reads real data or returns."""

from __future__ import annotations

import hashlib
import sys

import numpy as np
import pandas as pd
import pytest
import yaml

from council import paths
from council.stocks import pit, score
from council.stocks.score import FEATURES, POOLED, add_scores, ordered, sector_quotas, select_rule

VARIANTS = paths.REPO_ROOT / "policy" / "variants" / "stock-sleeve-variants-v1.yaml"
# sha256 of the synthetic fixture's orders and selections (seed 11), computed with the pre-move code
SELECTION_DIGEST = "9f04017caac91eced243e828a115466bdf893890209022176c039f2e147bd04d"


@pytest.fixture(scope="module")
def variants():
    return yaml.safe_load(VARIANTS.read_text())["variants"]


@pytest.fixture(scope="module")
def study():
    sys.path.insert(0, str(paths.REPO_ROOT / "scripts"))
    import stock_sleeve_study

    return stock_sleeve_study


def _toy(n_per_sector: dict[str, int], seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for s, k in n_per_sector.items():
        for i in range(k):
            rows.append({"key": f"{s}{i}", "symbol": f"{s}{i}", "sector": s,
                         **{c: rng.normal() for c in FEATURES}})
    frame = pd.DataFrame(rows).set_index("key", drop=False)
    return add_scores(frame, 5)


def test_the_features_are_the_ported_rule_columns():
    assert tuple(pit.FUNDAMENTAL_COLUMNS) == FEATURES
    expected = ("revenue_growth_yoy", "revenue_growth_acceleration", "gross_margin_change_yoy",
                "operating_margin_change_yoy")
    assert expected == FEATURES


def test_the_study_uses_these_functions(study):
    for name in ("add_scores", "sector_quotas", "select_rule", "FEATURES"):
        assert getattr(study, name) is getattr(score, name), name
    assert study.pit is pit


def test_scores_are_the_ported_rule_globally_and_within_peer_groups(study):
    spec = study.load_spec()
    elig, _ = study.Universe(study.synthetic_inputs(seed=11), spec).at(pd.Timestamp("2021-05-20"))
    scored = add_scores(elig, 5)
    np.testing.assert_allclose(scored["g_score"], pit.rank_average(elig, FEATURES))
    for peer, g in scored.groupby("peer"):
        np.testing.assert_allclose(g["s_score"], pit.rank_average(g, FEATURES))
        if peer != POOLED:
            assert len(g) >= 5
    small = scored["sector"].value_counts()
    assert set(scored.loc[scored["peer"] == POOLED, "sector"]) == set(small[small < 5].index)


def test_an_empty_eligible_set_scores_and_selects_nothing(variants):
    empty = add_scores(pd.DataFrame(columns=["key", "symbol", "sector", *FEATURES]), 5)
    assert {"g_score", "s_score", "peer"} <= set(empty.columns) and empty.empty
    assert select_rule(empty, ["X"], variants["SC"], 10, 2) == []


def test_order_is_score_then_global_score_then_symbol():
    frame = pd.DataFrame({"symbol": ["B", "A", "C"], "s_score": [0.5, 0.5, 0.9], "g_score": [0.1, 0.1, 0.2]},
                         index=["kb", "ka", "kc"])
    assert ordered(frame, "sector") == ["kc", "ka", "kb"]
    assert ordered(frame, "global") == ["kc", "ka", "kb"]


def test_sector_quotas_are_proportional_capped_and_sum_to_n():
    q = sector_quotas({"A": 60, "B": 20, "C": 15, "D": 5}, 10, 3)
    assert sum(q.values()) == 10
    assert q["A"] == 3                                   # 6.0 raw, capped at 3
    assert all(v <= 3 for v in q.values())
    assert sector_quotas({"A": 2, "B": 1}, 10, 3) == {"A": 2, "B": 1}   # not enough names
    assert sector_quotas({"A": 5, "B": 5}, 3, 3) == sector_quotas({"B": 5, "A": 5}, 3, 3)


@pytest.mark.parametrize("vname", ["GC", "SC"])
def test_cap_variants_respect_the_cap_and_the_hold_buffer(variants, vname):
    elig = _toy({"A": 40, "B": 30, "C": 20, "D": 10, "E": 6})
    variant = variants[vname]
    sel = select_rule(elig, [], variant, 10, 2)
    assert len(sel) == 10
    assert elig.loc[sel, "sector"].value_counts().max() <= 3
    order = ordered(elig, variant["score"])
    pos = {k: i for i, k in enumerate(order)}
    inside = [k for k in order[10:20] if k not in sel][:2]           # ranks 11-20: inside the buffer
    outside = order[-1]
    again = select_rule(elig, [*inside, outside], variant, 10, 2)
    assert all(k in again for k in inside if pos[k] < 20)
    assert outside not in again
    assert select_rule(elig, [*inside, outside], variant, 10, 1) == sel   # buffer = N: plain top list


def test_the_cap_is_relaxed_only_when_it_leaves_slots_empty(variants):
    elig = _toy({"A": 20, "B": 2})                         # 3 + 2 names fit under the cap of 3
    sel = select_rule(elig, [], variants["SC"], 8, 2)
    assert len(sel) == 8
    assert (elig.loc[sel, "sector"] == "B").sum() == 2


def test_quota_variant_fills_each_sector_to_its_quota(variants):
    elig = _toy({"A": 40, "B": 30, "C": 20, "D": 10, "E": 6})
    variant = variants["SQ"]
    sel = select_rule(elig, [], variant, 10, 2)
    q = sector_quotas(elig["sector"].value_counts().to_dict(), 10, 3)
    counts = elig.loc[sel, "sector"].value_counts().to_dict()
    assert {s: counts.get(s, 0) for s in q} == q
    for s in q:                                                        # the best names of each sector
        in_s = [k for k in ordered(elig, "sector") if elig.at[k, "sector"] == s]
        assert set(in_s[: q[s]]) == {k for k in sel if elig.at[k, "sector"] == s}


def test_quota_variant_keeps_a_held_name_inside_twice_its_sector_quota(variants):
    elig = _toy({"A": 40, "B": 30, "C": 20, "D": 10, "E": 6})
    variant = variants["SQ"]
    q = sector_quotas(elig["sector"].value_counts().to_dict(), 10, 3)
    in_a = [k for k in ordered(elig, "sector") if elig.at[k, "sector"] == "A"]
    near, far = in_a[q["A"]], in_a[2 * q["A"]]                        # inside / outside the buffer
    sel = select_rule(elig, [near, far], variant, 10, 2)
    assert near in sel and far not in sel
    assert (elig.loc[sel, "sector"] == "A").sum() == q["A"]


def test_selections_on_the_fixture_are_unchanged_by_the_move(study):
    """Every order and pick on the synthetic fixture equals the pre-move code's (digest pinned)."""
    spec = study.load_spec()
    inp = study.synthetic_inputs(seed=11)
    uni = study.Universe(inp, spec)
    sessions = inp.closes.index
    dates = study.rebalance_dates(sessions, spec, start=pd.Timestamp(spec["window"]["first_decision_anchor"]),
                                  end=sessions.max())
    cells = study.cells_of(spec)
    h = hashlib.sha256()
    held = {(cell, b): [] for cell in cells for b in (2.0, 1.0)}
    for d in dates:
        elig = add_scores(uni.at(d)[0], int(spec["rule"]["min_peer_group"]))
        for kind in ("sector", "global"):
            h.update(f"{d.date()} {kind}: {' '.join(ordered(elig, kind))}\n".encode())
        for (cell, b), prev in held.items():
            variant, n = cells[cell]
            sel = select_rule(elig, prev, variant, n, b)
            h.update(f"{d.date()} {cell} x{b}: {' '.join(sel)}\n".encode())
            held[(cell, b)] = sel
    assert h.hexdigest() == SELECTION_DIGEST
