"""Reference book: levels, units, covariance, ex-ante vol, scaling, caps. Every rule: pass + fail."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from council.reference.book import (
    apply_caps,
    book_covariance,
    build_reference,
    ewma_covariance,
    ex_ante_vol,
    reference_levels,
    unit_weights,
    vol_fn,
)
from council.risk.stops import catastrophe_stop_distance
from tests.reference.conftest import all_states, gaussian_returns, state, variant

REF = ["NDX", "SEMIS", "SPX", "GOLD", "BTC", "ETH"]
OVERLAY = ["OIL", "EURUSD", "GBPUSD"]
BASE = {"NDX": 0.35, "SEMIS": 0.15, "SPX": 0.15, "GOLD": 0.12, "BTC": 0.13, "ETH": 0.05,
        "OIL": 0.10, "EURUSD": 0.25, "GBPUSD": 0.25}


# ------------------------------------------------------------------------------------ levels


def test_levels_follow_the_trend_table(policy):
    states = all_states(policy, SEMIS={"trend": "mixed"}, SPX={"trend": "down"})
    levels = reference_levels(policy.universe.lines, states, policy)
    assert levels["NDX"] == 1.0 and levels["SEMIS"] == 0.5 and levels["SPX"] == 0.25
    assert all(levels[s] == 0.0 for s in OVERLAY)  # overlay lines are council-only


def test_missing_trend_gives_zero_and_a_flag(policy):
    states = all_states(policy, GOLD={"trend": None})
    del states["BTC"]
    flags: list[str] = []
    levels = reference_levels(policy.universe.lines, states, policy, flags)
    assert levels["GOLD"] == 0.0 and levels["BTC"] == 0.0
    assert any(f.startswith("GOLD: no trend") for f in flags)
    assert any(f.startswith("BTC: no trend") for f in flags)
    assert not any(f.startswith("NDX") for f in flags)


def test_overlay_line_in_uptrend_still_zero_and_unflagged(policy):
    flags: list[str] = []
    levels = reference_levels(policy.universe.lines, all_states(policy), policy, flags)
    assert levels["OIL"] == 0.0 and flags == []


def test_states_keyed_by_signal_ticker_are_accepted(policy):
    lines = [ln for ln in policy.universe.lines if ln.symbol == "NDX"]
    assert reference_levels(lines, {"QQQ": state("QQQ", trend="mixed")}, policy) == {"NDX": 0.5}


def test_level_table_that_would_short_is_rejected(policy):
    bad = variant(policy, reference=lambda r: r["trend"]["levels"].update(down=-0.25))
    with pytest.raises(ValueError):
        reference_levels(policy.universe.lines, all_states(policy), bad)


def test_level_table_that_would_lever_is_rejected(policy):
    bad = variant(policy, reference=lambda r: r["trend"]["levels"].update(up=1.5))
    with pytest.raises(ValueError):
        reference_levels(policy.universe.lines, all_states(policy), bad)


# ------------------------------------------------------------------------------------ units


@pytest.mark.parametrize("ratio, expected_mult", [(0.5, 1.0), (1.0, 1.0), (2.0, 0.5), (4.0, 0.25)])
def test_unit_is_base_times_vol_cap(policy, ratio, expected_mult):
    units = unit_weights(policy.universe.lines, all_states(policy, NDX={"vol_ratio": ratio}), policy)
    assert units["NDX"] == pytest.approx(BASE["NDX"] * expected_mult)


def test_calm_vol_never_levers_a_line(policy):
    units = unit_weights(policy.universe.lines, all_states(policy, NDX={"vol_ratio": 0.2}), policy)
    assert units["NDX"] == pytest.approx(BASE["NDX"])  # 1/0.2 = 5 would lever; the cap stops at 1


@pytest.mark.parametrize("bad", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_missing_vol_halves_the_unit_with_a_flag(policy, bad):
    flags: list[str] = []
    units = unit_weights(policy.universe.lines, all_states(policy, GOLD={"vol_ratio": bad}), policy, flags)
    assert units["GOLD"] == pytest.approx(BASE["GOLD"] * 0.5)
    assert any(f.startswith("GOLD: no vol ratio") for f in flags)
    assert units["NDX"] == pytest.approx(BASE["NDX"])  # other lines unaffected


def test_overlay_lines_get_units_too(policy):
    units = unit_weights(policy.universe.lines, all_states(policy, OIL={"vol_ratio": 2.0}), policy)
    assert units["OIL"] == pytest.approx(0.05) and units["EURUSD"] == pytest.approx(0.25)


def test_line_cap_ratio_from_policy(policy):
    loose = variant(policy, reference=lambda r: r["vol"].update(line_cap_ratio=1.25))
    states = all_states(policy, NDX={"vol_ratio": 1.25}, SPX={"vol_ratio": 2.5})
    units = unit_weights(loose.universe.lines, states, loose)
    assert units["NDX"] == pytest.approx(0.35) and units["SPX"] == pytest.approx(0.075)
    with pytest.raises(ValueError):
        unit_weights(policy.universe.lines, states, variant(policy, reference=lambda r: r["vol"].update(line_cap_ratio=0)))


# ------------------------------------------------------------------------------------ covariance


def test_covariance_span_one_is_the_last_row_outer_product():
    r = pd.DataFrame({"A": [0.01, 0.03], "B": [0.02, -0.01]})
    cov = ewma_covariance(r, span=1, ann=252)
    assert cov.loc["A", "A"] == pytest.approx(0.03**2 * 252)
    assert cov.loc["A", "B"] == pytest.approx(0.03 * -0.01 * 252)
    assert cov.loc["B", "B"] == pytest.approx(0.01**2 * 252)


def test_covariance_weights_known_data():
    # span 3 -> alpha 0.5 -> row weights 0.25, 0.5, 1.0 (oldest to newest)
    r = pd.DataFrame({"A": [0.01, 0.02, 0.04]})
    cov = ewma_covariance(r, span=3, ann=252)
    expected = (0.25 * 1e-4 + 0.5 * 4e-4 + 1.0 * 16e-4) / 1.75 * 252
    assert cov.loc["A", "A"] == pytest.approx(expected)


def test_covariance_is_zero_mean():
    cov = ewma_covariance(pd.DataFrame({"A": [0.01] * 50}), span=10, ann=252)
    assert cov.loc["A", "A"] == pytest.approx(1e-4 * 252)  # a demeaned estimator would give 0


def test_covariance_is_pairwise_complete():
    r = pd.DataFrame({"A": [0.01, 0.02, 0.03], "B": [np.nan, 0.01, -0.02]})
    cov = ewma_covariance(r, span=3, ann=252)
    assert cov.loc["A", "B"] == pytest.approx((0.5 * 0.02 * 0.01 + 1.0 * 0.03 * -0.02) / 1.5 * 252)
    assert cov.loc["B", "B"] == pytest.approx((0.5 * 1e-4 + 1.0 * 4e-4) / 1.5 * 252)
    assert cov.loc["A", "A"] == pytest.approx((0.25 * 1e-4 + 0.5 * 4e-4 + 1.0 * 9e-4) / 1.75 * 252)


def test_covariance_without_common_rows_is_nan():
    r = pd.DataFrame({"A": [0.01, np.nan], "B": [np.nan, 0.02]})
    cov = ewma_covariance(r, span=5)
    assert math.isnan(cov.loc["A", "B"]) and cov.loc["A", "A"] > 0
    assert ewma_covariance(pd.DataFrame(columns=["A"], dtype=float)).isna().all().all()
    with pytest.raises(ValueError):
        ewma_covariance(r, span=0)


def test_ex_ante_vol_known_cases():
    cov = pd.DataFrame([[0.04, 0.0], [0.0, 0.01]], index=["A", "B"], columns=["A", "B"])
    assert ex_ante_vol({"A": 0.5, "B": 0.5}, cov) == pytest.approx(math.sqrt(0.25 * 0.04 + 0.25 * 0.01))
    perfect = pd.DataFrame([[0.04, 0.02], [0.02, 0.01]], index=["A", "B"], columns=["A", "B"])
    assert ex_ante_vol({"A": 0.5, "B": 0.5}, perfect) == pytest.approx(0.15)
    assert ex_ante_vol({"A": 0.0, "C": 0.0}, cov) == 0.0


def test_ex_ante_vol_refuses_unknown_risk():
    cov = pd.DataFrame([[0.04]], index=["A"], columns=["A"])
    with pytest.raises(KeyError):
        ex_ante_vol({"A": 0.5, "B": 0.1}, cov)
    with pytest.raises(ValueError):
        ex_ante_vol({"A": 0.5}, pd.DataFrame([[np.nan]], index=["A"], columns=["A"]))


# ------------------------------------------------------------------------------------ build


def _calm_returns(cols=REF, sd=0.005):
    return gaussian_returns(list(cols), daily_sd=sd)


def test_calm_uptrend_book_is_the_base_composition(policy):
    book = build_reference(cycle_id="c1", lines=policy.universe.lines, states=all_states(policy),
                           returns=_calm_returns(), policy=policy)
    for sym in REF:
        assert book.entries[sym].weight_ref == pytest.approx(BASE[sym])
        assert book.entries[sym].unit_weight == pytest.approx(BASE[sym])
    for sym in OVERLAY:
        assert book.entries[sym].weight_ref == 0.0 and book.entries[sym].level_ref == 0.0
        assert book.entries[sym].unit_weight == pytest.approx(BASE[sym])
    assert book.k == 1.0 and book.truncations == []
    assert book.gross == pytest.approx(0.95) and book.gross <= policy.universe.reference_gross_max + 1e-9
    assert 0.0 < book.ex_ante_vol < 0.30 and book.target_vol == 0.22
    assert book.weights()["NDX"] == pytest.approx(0.35)


def test_gross_above_limit_is_scaled_down(policy):
    tight = variant(policy, gross_max=0.5)
    book = build_reference(cycle_id="c", lines=tight.universe.lines, states=all_states(tight),
                           returns=_calm_returns(), policy=tight)
    assert book.gross == pytest.approx(0.5)
    assert book.k == pytest.approx(0.5 / 0.95)
    assert book.entries["NDX"].unit_weight == pytest.approx(0.35 * 0.5 / 0.95)
    assert book.entries["OIL"].unit_weight == pytest.approx(0.10 * 0.5 / 0.95)
    assert any(t.startswith("book: gross") for t in book.truncations)


def test_gross_below_limit_is_not_scaled(policy):
    states = all_states(policy, NDX={"trend": "mixed"})
    book = build_reference(cycle_id="c", lines=policy.universe.lines, states=states,
                           returns=_calm_returns(), policy=policy)
    assert book.k == 1.0 and book.gross == pytest.approx(0.95 - 0.175)


def test_ex_ante_vol_above_hard_limit_is_scaled_to_the_limit(policy):
    one = gaussian_returns(["X"], daily_sd=0.05, n=400)["X"]
    returns = pd.DataFrame({s: one for s in REF})  # perfectly correlated, ~79% vol each
    book = build_reference(cycle_id="c", lines=policy.universe.lines, states=all_states(policy),
                           returns=returns, policy=policy)
    assert book.ex_ante_vol == pytest.approx(0.30)
    assert book.k < 0.5
    assert book.entries["NDX"].weight_ref == pytest.approx(0.35 * book.k)
    assert any(t.startswith("book: ex-ante vol") for t in book.truncations)


def test_ex_ante_vol_just_below_limit_is_not_scaled(policy):
    returns = pd.DataFrame({s: [0.3 / 0.95 / math.sqrt(252) * (-1) ** i for i in range(200)] for s in REF})
    returns = returns * 0.999  # book vol a hair under 30%
    book = build_reference(cycle_id="c", lines=policy.universe.lines, states=all_states(policy),
                           returns=returns, policy=policy)
    assert book.k == 1.0 and book.ex_ante_vol == pytest.approx(0.30 * 0.999)


def test_line_and_group_caps_clip(policy):
    def caps(r):
        r["caps"]["line"]["NDX"] = 0.10
        r["caps"]["crypto_total"] = 0.10
        r["caps"]["equity_beta_cluster"]["max"] = 0.30

    capped = variant(policy, risk=caps)
    book = build_reference(cycle_id="c", lines=capped.universe.lines, states=all_states(capped),
                           returns=_calm_returns(), policy=capped)
    w = book.weights()
    # NDX clipped to 0.10 first; then the cluster (0.10 + 0.15 + 0.15 = 0.40) scales to 0.30
    assert w["NDX"] + w["SEMIS"] + w["SPX"] == pytest.approx(0.30)
    assert w["NDX"] == pytest.approx(0.10 * 0.30 / 0.40)
    assert w["BTC"] + w["ETH"] == pytest.approx(0.10)
    assert w["BTC"] / w["ETH"] == pytest.approx(0.13 / 0.05)  # pro rata
    assert book.entries["NDX"].unit_weight == pytest.approx(0.35)  # the unit is not a cap
    kinds = " | ".join(book.truncations)
    assert "NDX: line cap" in kinds and "crypto_total" in kinds and "equity_beta_cluster" in kinds


def test_apply_caps_leaves_compliant_weights_alone(policy):
    w = {"NDX": 0.35, "BTC": 0.13, "ETH": 0.05, "EURUSD": 0.2, "GBPUSD": 0.2}
    notes: list[str] = []
    assert apply_caps(w, policy.universe.lines, policy, notes) == w and notes == []
    w_fx = {"EURUSD": 0.3, "GBPUSD": 0.3}
    out = apply_caps(w_fx, policy.universe.lines, policy, notes)
    assert out["EURUSD"] + out["GBPUSD"] == pytest.approx(0.50) and "fx_total" in notes[0]


def test_capping_a_hedge_that_raises_vol_rescales_the_book(policy):
    c = 1.0 / math.sqrt(252)  # |r| constant -> zero-mean EWMA sigma exactly 100%
    r = pd.Series([c * (-1) ** i for i in range(300)], index=pd.bdate_range("2020-01-01", periods=300))
    returns = pd.DataFrame({"NDX": r, "GOLD": -r})  # a perfect hedge
    states = all_states(policy, **{s: {"trend": None} for s in ["SEMIS", "SPX", "BTC", "ETH"]})
    no_gold = variant(policy, risk=lambda d: d["caps"]["line"].update(GOLD=0.0))
    before = build_reference(cycle_id="c", lines=policy.universe.lines, states=states, returns=returns, policy=policy)
    assert before.ex_ante_vol == pytest.approx(0.23) and before.k == 1.0
    after = build_reference(cycle_id="c", lines=no_gold.universe.lines, states=states, returns=returns, policy=no_gold)
    assert after.entries["GOLD"].weight_ref == 0.0
    assert after.ex_ante_vol == pytest.approx(0.30)
    assert after.entries["NDX"].weight_ref == pytest.approx(0.30)
    assert any("capping raised ex-ante vol" in t for t in after.truncations)


def test_missing_trend_is_recorded_in_truncations_and_flags(policy):
    flags: list[str] = []
    book = build_reference(cycle_id="c", lines=policy.universe.lines,
                           states=all_states(policy, BTC={"trend": None}),
                           returns=_calm_returns(), policy=policy, flags=flags)
    assert book.entries["BTC"].weight_ref == 0.0 and book.entries["BTC"].trend is None
    assert "BTC: no trend state, level 0" in book.truncations and "BTC: no trend state, level 0" in flags


def test_without_returns_sigma_and_correlation_one_are_assumed(policy):
    book = build_reference(cycle_id="c", lines=policy.universe.lines, states=all_states(policy),
                           returns=None, policy=policy)
    assert book.ex_ante_vol == pytest.approx(0.2 * 0.95)  # rho = 1 everywhere: sum of w * sigma
    assert any("variance from sigma_ann" in t for t in book.truncations)


def test_without_any_vol_information_the_book_refuses(policy):
    states = all_states(policy, **{s: {"sigma": None} for s in REF})
    with pytest.raises(ValueError):
        build_reference(cycle_id="c", lines=policy.universe.lines, states=states, returns=None, policy=policy)


def test_returns_keyed_by_signal_ticker_match_symbol_keys(policy):
    by_symbol = _calm_returns()
    tickers = {"NDX": "QQQ", "SEMIS": "SOXX", "SPX": "SPY", "GOLD": "GLD", "BTC": "BTCUSDT", "ETH": "ETHUSDT"}
    by_ticker = by_symbol.rename(columns=tickers)
    a = build_reference(cycle_id="c", lines=policy.universe.lines, states=all_states(policy), returns=by_symbol, policy=policy)
    b = build_reference(cycle_id="c", lines=policy.universe.lines, states=all_states(policy), returns=by_ticker, policy=policy)
    assert a.ex_ante_vol == pytest.approx(b.ex_ante_vol) and a.weights() == b.weights()


_TRENDS = st.sampled_from(["up", "mixed", "down", None])
_RATIOS = st.one_of(st.none(), st.floats(0.05, 8.0))


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(trends=st.lists(_TRENDS, min_size=9, max_size=9), ratios=st.lists(_RATIOS, min_size=9, max_size=9),
       sd=st.floats(0.001, 0.08), seed=st.integers(0, 1000))
def test_book_is_never_short_levered_or_over_vol(policy, trends, ratios, sd, seed):
    overrides = {ln.symbol: {"trend": t, "vol_ratio": r}
                 for ln, t, r in zip(policy.universe.lines, trends, ratios, strict=True)}
    book = build_reference(cycle_id="p", lines=policy.universe.lines, states=all_states(policy, **overrides),
                           returns=gaussian_returns(REF, daily_sd=sd, seed=seed, n=200), policy=policy)
    w = book.weights()
    assert all(v >= 0.0 for v in w.values())
    assert sum(w.values()) <= policy.universe.reference_gross_max + 1e-9
    assert book.ex_ante_vol <= 0.30 + 1e-9 and 0.0 < book.k <= 1.0
    for sym, cap in policy.risk["caps"]["line"].items():
        assert w[sym] <= cap + 1e-12
    assert all(w[s] == 0.0 for s in OVERLAY)


# ------------------------------------------------------------------------------------ engine seams


def test_book_covariance_is_the_one_the_book_scales_with(policy):
    lines, states, returns = policy.universe.lines, all_states(policy), _calm_returns()
    book = build_reference(cycle_id="c", lines=lines, states=states, returns=returns, policy=policy)
    notes: list[str] = []
    cov = book_covariance(returns, lines, states, policy, notes=notes)
    assert list(cov.index) == [ln.symbol for ln in lines] and np.isfinite(cov.to_numpy()).all()
    assert vol_fn(cov)(book.weights()) == pytest.approx(book.ex_ante_vol)
    sub = book_covariance(returns, lines, states, policy, symbols=REF)
    pd.testing.assert_frame_equal(sub, cov.loc[REF, REF])
    assert any(n.startswith("OIL: no return history") for n in notes)       # overlay: sigma_ann
    with pytest.raises(ValueError):
        book_covariance(returns, lines, states, policy, symbols=["UNMAPPED_12"])


def test_vol_fn_ignores_keys_missing_from_the_covariance(policy):
    cov = book_covariance(_calm_returns(), policy.universe.lines, all_states(policy), policy)
    vol = vol_fn(cov)
    w = {"NDX": 0.35, "BTC": 0.13, "EURUSD": -0.1}
    assert vol({**w, "UNMAPPED_77": 0.2, "UNMAPPED_MIRROR_3": 0.1}) == pytest.approx(ex_ante_vol(w, cov))
    assert vol({"UNMAPPED_77": 0.5}) == 0.0
    broken = cov.copy()
    broken.loc["NDX", "BTC"] = np.nan
    with pytest.raises(ValueError):                                          # known lines still checked
        vol_fn(broken)(w)


def test_entries_carry_the_catastrophe_stop_distance(policy):
    states = all_states(policy, GOLD={"sigma": None})
    book = build_reference(cycle_id="c", lines=policy.universe.lines, states=states,
                           returns=_calm_returns(), policy=policy)
    ndx = next(ln for ln in policy.universe.lines if ln.symbol == "NDX")
    assert book.entries["NDX"].stop_distance == pytest.approx(catastrophe_stop_distance(states["NDX"], ndx, policy))
    assert book.entries["NDX"].stop_distance == pytest.approx(3 * 0.2 / math.sqrt(252) * math.sqrt(5))
    assert book.entries["BTC"].stop_distance == pytest.approx(0.20)              # crypto floor
    assert book.entries["OIL"].stop_distance is not None                         # overlay lines too
    assert book.entries["GOLD"].stop_distance is None                            # no vol: no stop
