"""The live rank against the pre-registered study script on the SAME inputs (spec header: "The live
rank must reproduce [the universe filters], and a test must pin its eligible set to the tagged
script's on the same inputs").

- Synthetic (always runs): the study's seeded fixture, which exercises every filter. At every
  rebalance date the live eligible set, its features and the funnel equal `Universe.at`, with and
  without the AI list, and the SQ-8 selection chain equals the study's `select_rule` chain.
- Real (runs when the operator's private study bundle is on this machine; read-only): the frozen
  input bundle the study ran on (its sha256 is checked against the variants file) and the study
  run's `selections.csv`. At the last two rebalance dates the live rank reproduces the recorded SQ-8
  names and kept counts, the eligible set and the funnel; at the last date also with the AI list.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

from council import paths
from council.stocks.rank import FRAME_COLUMNS, RankConfig, rank
from council.stocks.score import add_scores, select_rule
from tests.stocks.study_adapter import rank_inputs

sys.path.insert(0, str(paths.REPO_ROOT / "scripts"))
import stock_sleeve_study as S  # noqa: E402

STUDY_DIR = Path(os.environ.get("COUNCIL_STOCK_STUDY_DIR")
                 or Path.home() / "Library" / "Application Support" / "council-book" / "backtests" / "stock-sleeve")
COMPARED = [c for c in FRAME_COLUMNS if c != "key"]


@pytest.fixture(scope="module")
def spec():
    return S.load_spec()


@pytest.fixture(scope="module")
def config(spec):
    return RankConfig.from_variants(spec, "SQ-8")


def _same_frames(study: pd.DataFrame, live: pd.DataFrame) -> None:
    assert set(study.index) == set(live.index)
    a = study[COMPARED].sort_index()
    b = live[COMPARED].sort_index()
    pd.testing.assert_frame_equal(a, b, check_dtype=False)


# ------------------------------------------------------------------------------------ synthetic


@pytest.fixture(scope="module")
def synthetic(spec, config):
    inp = S.synthetic_inputs(seed=11)
    uni = S.Universe(inp, spec)
    sessions = inp.closes.index
    dates = S.rebalance_dates(sessions, spec, start=pd.Timestamp(spec["window"]["first_decision_anchor"]),
                              end=sessions.max())
    study_sel, live_sel, rows = [], [], []
    held_study: list[str] = []
    held_live: list[str] = []
    for d in dates:
        elig, funnel = uni.at(d)
        chosen = select_rule(add_scores(elig, config.min_peer_group), held_study, dict(config.variant), config.n,
                             config.hold_buffer_multiple)
        res = rank(d, rank_inputs(inp, uni, d, held=held_live), config)
        rows.append((d, elig, funnel, res))
        study_sel.append(chosen)
        live_sel.append(list(res.selected))
        held_study, held_live = chosen, list(res.selected)
    return inp, uni, dates, rows, study_sel, live_sel


def test_synthetic_eligible_sets_features_and_funnels_equal_the_study(synthetic):
    _, _, dates, rows, _, _ = synthetic
    assert len(dates) > 20
    for _d, elig, funnel, res in rows:
        _same_frames(elig, res.eligible)
        assert res.funnel == funnel
    assert any(len(e) for _, e, _, _ in rows)


def test_synthetic_sq8_selection_chain_equals_the_study(synthetic):
    _, _, _, rows, study_sel, live_sel = synthetic
    assert live_sel == study_sel
    assert sum(len(set(a) & set(b)) for a, b in zip(live_sel, live_sel[1:], strict=False)) > 0   # the buffer acted
    excluded = set().union(*(set(r.excluded.values()) for *_, r in rows))
    assert {"not_common", "duplicate_cik", "excluded_sector", "not_us_gaap", "stale_filing",
            "revenue_floor", "missing_feature", "not_listed"} <= excluded                   # the fixture's traps fired


def test_synthetic_ai_list_universe_equals_the_study_sensitivity(synthetic, config):
    inp, uni, dates, _, _, _ = synthetic
    for d in dates[::4] + dates[-1:]:
        elig, funnel = uni.at(d, include_ai=True)
        res = rank(d, rank_inputs(inp, uni, d, include_ai=True), config)
        _same_frames(elig, res.eligible)
        assert res.funnel == funnel
        assert res.ai_only and res.ai_only <= {c.key for c in rank_inputs(inp, uni, d, include_ai=True).candidates}


# ------------------------------------------------------------------------------------ real lab data


def _bundle_or_skip(spec) -> tuple[Path, pd.DataFrame]:
    bundle = STUDY_DIR / "inputs" / "inputs.pkl"
    runs = sorted(p.parent for p in (STUDY_DIR / "results").glob("run-*/result.json")) if STUDY_DIR.exists() else []
    if not bundle.exists() or not runs:
        pytest.skip("the private stock-sleeve study bundle and run are not on this machine")
    h = hashlib.sha256()
    with bundle.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    assert h.hexdigest() == spec["frozen_inputs"]["inputs_sha256"], "not the frozen study bundle"
    return STUDY_DIR / "inputs", pd.read_csv(runs[-1] / "selections.csv")


@pytest.fixture(scope="module")
def real(spec):
    inputs_dir, selections = _bundle_or_skip(spec)
    inp = S.Inputs.load(inputs_dir)                      # read-only
    return inp, S.Universe(inp, spec), selections[selections["cell"] == "SQ-8"].reset_index(drop=True)


@pytest.mark.parametrize("back", [2, 1])
def test_real_rank_reproduces_the_studys_sq8_selection(real, config, back):
    inp, uni, sq = real
    i = len(sq) - back
    d, prev = pd.Timestamp(sq.loc[i, "date"]), pd.Timestamp(sq.loc[i - 1, "date"])
    held = [uni._security(s, prev) for s in sq.loc[i - 1, "names"].split()]    # the study's previous selection
    res = rank(d, rank_inputs(inp, uni, d, held=held), config)
    assert res.symbols(list(res.selected)) == sq.loc[i, "names"].split()
    assert len(res.kept) == int(sq.loc[i, "kept"])
    elig, funnel = uni.at(d)
    _same_frames(elig, res.eligible)
    assert res.funnel == funnel
    assert len(res.shortlist) == config.shortlist_size and not set(res.shortlist) & set(res.selected)


def test_real_ai_list_universe_equals_the_study_sensitivity(real, config):
    inp, uni, sq = real
    d = pd.Timestamp(sq.loc[len(sq) - 1, "date"])
    res = rank(d, rank_inputs(inp, uni, d, include_ai=True), config)
    elig, funnel = uni.at(d, include_ai=True)
    _same_frames(elig, res.eligible)
    assert res.funnel == funnel
    assert res.ai_only
