"""scripts/stock_sleeve_study.py: the pre-registered stock-sleeve study, on a synthetic fixture.

Nothing here reads real returns. The fixture exercises every universe filter, the selection
variants, the random null, the budget rule, the sealed cost parameters, the tag guard and the
end-to-end run (private outputs only, percent-only summary)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from council import paths
from council.policy import Policy
from council.reference import sleeve
from council.reference.backtest import BookPlan, simulate
from council.reference.report import assert_public_safe
from council.stocks import pit, score, sectors

sys.path.insert(0, str(paths.REPO_ROOT / "scripts"))
import stock_sleeve_study as S  # noqa: E402

FF12_ZIP = Path.home() / "Desktop" / "trading" / "finetune" / "data" / "raw" / "french" / "Siccodes12.zip"


@pytest.fixture(scope="module")
def spec():
    return S.load_spec()


@pytest.fixture(scope="module")
def inp():
    return S.synthetic_inputs(seed=11)


@pytest.fixture(scope="module")
def uni(inp, spec):
    return S.Universe(inp, spec)


# ------------------------------------------------------------------------------------ spec vs policy


def test_spec_numbers_match_the_policy_they_cite(spec):
    pol = Policy.load()
    assert set(spec["variants"]) == {"GC", "SC", "SQ"}
    assert set(S.cells_of(spec, selectable=True)) == {"SC-8", "SC-10", "SQ-8", "SQ-10"}
    assert set(S.cells_of(spec, selectable=False)) == {"GC-8", "GC-10"}      # the user fixed within-sector ranks
    assert spec["n_names"] == [8, 10]
    assert spec["hold_buffer_multiple"] == 2
    db = spec["sleeve"]["deadband"]
    assert db["level"] == pol.risk["deadband"]["level"]
    assert db["min_nav_share"] == pol.risk["deadband"]["min_nav_share"]
    assert spec["costs"]["slippage_bps"] == pol.costs["slippage_buffer_bps"]
    ref_levels = pol.reference["trend"]["levels"]
    assert spec["overlay"]["options"]["reference"] == {k: float(v) for k, v in ref_levels.items()}
    assert spec["whole_book"]["warn_at"] == pol.risk["killswitch"]["warn_at"]
    assert spec["whole_book"]["halt_at"] == pol.risk["killswitch"]["halt_at"]
    assert spec["whole_book"]["core_share"] + spec["whole_book"]["sleeve_share"] == pytest.approx(
        pol.universe.reference_gross_max)
    assert tuple(spec["gate"]) == S.GATE_CHECKS
    assert "G4_index" in spec["reported"] and "G4_index" not in spec["gate"]
    assert spec["baselines"]["random"]["draws"] >= 200
    assert tuple(spec["rule"]["features"]) == S.FEATURES
    assert spec["stops"]["reentry_sessions"] == pol.risk["reentry_cooloff_days"]["default"]
    by = pol.universe.by_symbol()
    mix = S.index_mix(spec, pol)
    assert mix == pytest.approx({"QQQ": by["NDX"].base_weight / (by["NDX"].base_weight + by["SPX"].base_weight),
                                 "SPY": by["SPX"].base_weight / (by["NDX"].base_weight + by["SPX"].base_weight)})
    assert set(spec["sensitivities"]) == set(S.SENSITIVITIES)
    assert set(spec["diagnostics"]) == set(S.DIAGNOSTICS)
    assert "src/council/policy.py" in S.FROZEN_DEPENDENCIES


def test_the_rule_modules_are_frozen_hashed_and_the_ones_the_study_runs():
    rule = ("src/council/stocks/pit.py", "src/council/stocks/score.py", "src/council/stocks/sectors.py",
            "src/council/reference/sleeve.py")
    assert rule == S.RULE_MODULES
    assert set(rule) <= set(S.FROZEN_FILES) and set(rule) <= set(S.CODE_FILES)
    assert "scripts/stock_sleeve_study.py" in S.FROZEN_FILES and "scripts/stock_sleeve_study.py" in S.CODE_FILES
    assert "src/council/stocks/__init__.py" in S.FROZEN_DEPENDENCIES
    frozen = (*S.FROZEN_FILES, *S.FROZEN_DEPENDENCIES)
    assert len(set(frozen)) == len(frozen) and all((paths.REPO_ROOT / rel).is_file() for rel in frozen)
    assert not (paths.REPO_ROOT / "scripts" / "stock_sleeve_pit.py").exists()      # moved, not copied
    for rel, mod in zip(rule, (pit, score, sectors, sleeve), strict=True):
        assert Path(mod.__file__).resolve() == paths.REPO_ROOT / rel
    assert S.pit is pit and S.select_rule is score.select_rule and S.sleeve_rule is sleeve and S.ff12 is sectors.ff12
    assert not hasattr(S, "FF12_RANGES")                                   # moved, not copied
    assert {"pyproject.toml", "uv.lock"} <= set(S.FROZEN_DEPENDENCIES)     # the locked library versions
    assert set(S.code_hashes()) == set(S.CODE_FILES)


def test_a_real_run_refuses_a_council_package_from_another_checkout(monkeypatch, tmp_path):
    S.checkout_check()                                                  # this checkout: fine
    monkeypatch.setattr(S, "REPO", tmp_path / "other-worktree")
    with pytest.raises(SystemExit, match="another checkout"):
        S.checkout_check()
    with pytest.raises(SystemExit, match="another checkout"):
        S.main(["run"])


def test_spec_refuses_an_unknown_or_missing_sensitivity(tmp_path):
    import yaml

    base = yaml.safe_load(S.SPEC_PATH.read_text())
    for sens in ([*base["sensitivities"], "made_up"], base["sensitivities"][1:]):
        path = tmp_path / "v.yaml"
        path.write_text(yaml.safe_dump({**base, "sensitivities": sens}))
        with pytest.raises(ValueError, match="sensitivities"):
            S.load_spec(path)


def test_spec_doc_names_every_frozen_number(spec):
    doc = S.DOC_PATH.read_text()
    for token in ("stock-sleeve-spec", "GC", "SC", "SQ", "G1", "G2", "G3", "G4", "B1", "B2", "2023-05-01",
                  "03-20", "05-20", "08-20", "11-20", "120 days", "1,000", "95th", "Review notes",
                  "change (c)", "common random numbers", "freeze", "git push origin stock-sleeve-spec",
                  *S.RULE_MODULES, "git worktree add"):
        assert token in doc, token
    assert_public_safe(doc)


# ------------------------------------------------------------------------------------ building blocks


def test_the_ticker_loader_leaves_the_safe_loader_alone():
    import yaml

    assert yaml.safe_load("a: true")["a"] is True
    assert yaml.load("t: [ON, NVDA]", Loader=S._TickerSafeLoader)["t"] == ["ON", "NVDA"]
    assert yaml.safe_load("b: false")["b"] is False


def test_ff12_spot_checks():
    assert S.ff12 is sectors.ff12                                     # the frozen module, not a copy
    assert sectors.ff12("3674") == "BusEq"
    assert sectors.ff12(7372) == "BusEq"
    assert sectors.ff12(2834) == "Hlth"
    assert sectors.ff12(6021) == "Money"
    assert sectors.ff12(6798) == "Money"          # REITs are financials in FF12
    assert sectors.ff12(4911) == "Utils"
    assert sectors.ff12(1311) == "Enrgy"
    assert sectors.ff12(8711) == "Other"
    assert sectors.ff12(None) is None and sectors.ff12("") is None and sectors.ff12(0) is None


@pytest.mark.skipif(not FF12_ZIP.exists(), reason="Ken French Siccodes12.zip not available locally")
def test_embedded_ff12_table_equals_the_ken_french_file():
    with zipfile.ZipFile(FF12_ZIP) as z:
        text = z.read(z.namelist()[0]).decode("latin-1")
    parsed = sectors.parse_siccodes12(text)
    embedded = {name: list(ranges) for name, ranges in sectors.FF12_RANGES}
    assert {k: v for k, v in parsed.items() if v} == embedded


def test_rebalance_dates_are_the_first_session_on_or_after_each_anchor(spec):
    sessions = pd.bdate_range("2019-01-01", "2020-12-31")
    dates = S.rebalance_dates(sessions, spec, start=pd.Timestamp("2019-05-20"), end=sessions[-1])
    assert dates[0] == pd.Timestamp("2019-05-20")
    assert pd.Timestamp("2019-08-20") in dates
    assert pd.Timestamp("2020-03-20") in dates
    assert pd.Timestamp("2020-11-20") in dates
    assert pd.Timestamp("2019-03-20") not in dates                  # before the first anchor
    weekend = S.rebalance_dates(pd.bdate_range("2021-03-01", "2021-03-31"), spec,
                                start=pd.Timestamp("2021-01-01"), end=pd.Timestamp("2021-03-31"))
    assert weekend == [pd.Timestamp("2021-03-22")]                   # 20 March 2021 was a Saturday


def test_a_filing_dated_on_the_decision_day_is_not_visible(inp, uni):
    cik = 1011
    f = inp.fund(cik)
    filed = f["available_at"].iloc[20]
    before = uni.features(cik, filed)
    after = uni.features(cik, filed + pd.Timedelta(days=1))
    assert before["latest_available_at"] < filed
    assert after["latest_available_at"] == filed


# ------------------------------------------------------------------------------------ universe


def test_universe_filters_on_the_fixture(uni):
    d = pd.Timestamp("2020-11-20")
    elig, funnel = uni.at(d)
    symbols = set(elig["symbol"])
    assert "S01" not in symbols          # a bank: FF12 Money
    assert "S02" not in symbols          # no gross profit: not all four features
    assert "S03" not in symbols          # a REIT security type
    assert "S04" not in symbols          # an ADR
    assert "S05" not in symbols          # an IFRS filer
    assert "S06" not in symbols          # pre-revenue: below the revenue floor
    assert "S08" not in symbols          # delisted on 2020-09-30: no recent bar
    assert "S09" not in symbols          # stopped filing in 2020: stale
    assert "S13" not in symbols          # gross profit above revenue in 2020: implausible
    assert "S10B" not in symbols and "S10" in symbols   # one security per CIK, the liquid class
    values = list(funnel.values())
    assert values == sorted(values, reverse=True)          # a funnel only narrows
    assert elig[list(S.FEATURES)].notna().all().all()      # no median fill
    early, _ = uni.at(pd.Timestamp("2019-11-20"))
    assert "S07" not in set(early["symbol"])               # listed 2019-06-03: under 290 days
    later, _ = uni.at(pd.Timestamp("2020-05-20"))
    assert "S07" in set(later["symbol"])
    assert "S13" in set(early["symbol"])                   # plausible before 2020


def test_a_reorganised_filer_keeps_its_history_through_the_cik_chain(inp, uni):
    d = pd.Timestamp("2019-05-20")                         # the new CIK has filed one quarter
    assert uni._cik("S1012-1", d) == 9000
    assert uni.cik_chain(9000, d, "S1012-1") == (1012, 9000)
    elig, _ = uni.at(d)
    assert "S12" in set(elig["symbol"])
    alone = uni.features(9000, d)                          # without the chain: one quarter, no P, no acceleration
    assert alone["quarters_of_history"] == 1 and np.isnan(float(alone["revenue_growth_acceleration"]))


def test_unmapped_members_carry_a_reason(inp, spec):
    extra = pd.DataFrame([{"index": "sp500", "symbol": "GONE", "opt_in": pd.Timestamp("2010-01-01"), "opt_out": pd.NaT},
                          {"index": "sp500", "symbol": "LATER", "opt_in": pd.Timestamp("2010-01-01"), "opt_out": pd.NaT}])
    sym = pd.DataFrame([{"security_id": "SX", "symbol": "LATER", "data_symbol": "LATER",
                         "valid_from": pd.Timestamp("2022-01-03"), "valid_to": pd.Timestamp("2024-06-28")}])
    inp2 = S.Inputs(**{k: getattr(inp, k) for k in S.Inputs.BLOB_FIELDS}, manifest={})
    inp2.members = pd.concat([inp.members, extra], ignore_index=True)
    inp2.symbols = pd.concat([inp.symbols, sym], ignore_index=True)
    u = S.Universe(inp2, spec)
    assert dict(u.unmapped(pd.Timestamp("2020-05-20"))) == {"GONE": "no_identity", "LATER": "identity_later"}


def test_a_renamed_member_maps_through_the_ticker_change(uni):
    elig, _ = uni.at(pd.Timestamp("2017-05-22"))
    assert "S20" in set(elig["symbol"])
    assert uni.symbols_at("S20", pd.Timestamp("2017-05-22")) == ["S20", "S20OLD"]
    assert uni.symbols_at("S20", pd.Timestamp("2020-05-20")) == ["S20"]


def test_ai_names_enter_only_the_sensitivity_universe(uni):
    d = pd.Timestamp("2021-05-20")
    head, _ = uni.at(d)
    ai, funnel = uni.at(d, include_ai=True)
    extra = set(ai["symbol"]) - set(head["symbol"])
    assert extra and extra <= {"S60", "S61", "AIX"}
    assert not ({"S60", "S61", "AIX"} & set(head["symbol"]))
    if "AIX" in extra:                                   # priced only outside the panel
        assert ai.loc[ai["symbol"] == "AIX"].index.tolist() == ["AI:AIX"]
    assert list(funnel.values()) == sorted(funnel.values(), reverse=True)


# ------------------------------------------------------------------------------------ selection


def _toy(n_per_sector: dict[str, int], seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for s, k in n_per_sector.items():
        for i in range(k):
            rows.append({"key": f"{s}{i}", "symbol": f"{s}{i}", "sector": s,
                         **{c: rng.normal() for c in S.FEATURES}})
    frame = pd.DataFrame(rows).set_index("key", drop=False)
    return S.add_scores(frame, 5)


def test_random_null_matches_the_kept_count_and_the_constraint(spec):
    elig = _toy({"A": 40, "B": 30, "C": 20, "D": 10, "E": 6})
    d = pd.Timestamp("2020-01-02")
    order = S.crn_orders({d: elig}, [d], 7, 3)[d]
    assert order == S.crn_orders({d: elig}, [d], 7, 3)[d]            # seeded per (seed, draw)
    assert sorted(order) == sorted(elig.index)
    variant = spec["variants"]["GC"]
    held = ["A0", "A1", "B0", "B1", "C0", "C1", "D0", "D1", "E0", "E1"]
    a = S.select_random(elig, held, variant, 10, 6, order)
    assert len(a) == 10 and len(set(a)) == 10
    assert len(set(a) & set(held)) == 6
    assert elig.loc[a, "sector"].value_counts().max() <= 3
    q_variant = spec["variants"]["SQ"]
    c = S.select_random(elig, held, q_variant, 10, 4, order)
    q = S.sector_quotas(elig["sector"].value_counts().to_dict(), 10, 3)
    counts = elig.loc[c, "sector"].value_counts().to_dict()
    assert all(counts.get(s, 0) <= q[s] for s in counts)


def test_common_random_numbers_nest_the_cells_like_the_rule(spec):
    elig = _toy({"A": 40, "B": 30, "C": 20, "D": 10, "E": 6})
    d = pd.Timestamp("2020-01-02")
    order = S.crn_orders({d: elig}, [d], 7, 11)[d]
    sc = spec["variants"]["SC"]
    eight, ten = S.select_random(elig, [], sc, 8, 0, order), S.select_random(elig, [], sc, 10, 0, order)
    assert set(eight) <= set(ten)                                      # N=8 is nested in N=10 under one draw
    other = S.crn_orders({d: elig}, [d], 7, 12)[d]
    assert S.select_random(elig, [], sc, 10, 0, other) != ten


def test_max_percentile_null_corrects_for_the_best_of_several_cells():
    rng = np.random.default_rng(0)
    m = 400
    base = rng.normal(size=m)
    rows = [{"cell": "A", "draw": i, "sharpe": base[i]} for i in range(m)]
    same = pd.DataFrame(rows + [{"cell": "B", "draw": i, "sharpe": base[i]} for i in range(m)])
    w = S.max_percentile_null(same, ["A", "B"])                        # identical cells: no correction
    assert S.percentile(0.9, w) == pytest.approx(0.9, abs=0.01)
    indep = pd.DataFrame(rows + [{"cell": "B", "draw": i, "sharpe": x} for i, x in enumerate(rng.normal(size=m))])
    w2 = S.max_percentile_null(indep, ["A", "B"])                      # two independent cells: 0.9 -> about 0.81
    assert S.percentile(0.9, w2) == pytest.approx(0.81, abs=0.05)
    assert S.percentile(None, [0.1, 0.2]) == 0.0


# ------------------------------------------------------------------------------------ simulation


def test_rebased_policy_scales_the_core_to_its_share():
    pol = Policy.load()
    reb = S.rebased_policy(pol, 0.45)
    ref = [ln for ln in reb.universe.lines if ln.in_reference]
    assert sum(ln.base_weight for ln in ref) == pytest.approx(0.45)
    assert reb.universe.reference_gross_max == 0.45
    ratios = {ln.symbol: ln.base_weight / pol.universe.by_symbol()[ln.symbol].base_weight for ln in ref}
    assert max(ratios.values()) == pytest.approx(min(ratios.values()))


# ------------------------------------------------------------------------------------ rules


def test_choose_cell_by_sharpe_then_cost():
    cells = {"GC-8": {"sharpe": 1.00, "cost_drag_per_year": 0.03},
             "SC-8": {"sharpe": 0.97, "cost_drag_per_year": 0.02},
             "SQ-8": {"sharpe": 0.80, "cost_drag_per_year": 0.01}}
    assert S.choose_cell(cells, 0.05)[0] == "SC-8"
    assert S.choose_cell(cells, 0.01)[0] == "GC-8"


def test_choose_overlay_rule():
    def opt(dd, cagr, sharpe, legs, dd2=None, stress=None):
        return {"max_drawdown": dd, "max_drawdown_x2": dd if dd2 is None else dd2, "cagr": cagr, "sharpe": sharpe,
                "legs_per_year": legs, "stress": stress or {"gfc": -0.1}}

    m = {"none": opt(-0.30, 0.15, 0.9, 40), "down_only": opt(-0.22, 0.12, 0.9, 60),
         "reference": opt(-0.20, 0.118, 0.95, 80)}
    assert S.choose_overlay(m, -0.25) == ("reference", True)      # tie within 0.25 pp, higher Sharpe
    m["reference"]["cagr"] = 0.10
    assert S.choose_overlay(m, -0.25) == ("down_only", True)
    m["down_only"]["max_drawdown_x2"] = -0.26                      # fails at doubled costs
    assert S.choose_overlay(m, -0.25) == ("reference", True)
    m["reference"]["stress"] = {"gfc": -0.31}                     # fails the proxy stress
    m["down_only"]["stress"] = {"gfc": -0.27}
    assert S.choose_overlay(m, -0.25) == ("down_only", False)     # none qualifies: shallowest worst drawdown


def test_gate_needs_every_check(spec):
    good = {"cagr": 0.2, "sharpe": 1.2}
    pool = {"cagr": 0.1, "sharpe": 0.8}
    sub = {"before_split": {"sharpe": 1.0}, "from_split": {"sharpe": 1.4}}
    sub_pool = {"before_split": {"sharpe": 0.9}, "from_split": {"sharpe": 1.0}}
    book = {"sharpe": 1.0, "max_drawdown": -0.20}
    index_book = {"sharpe": 0.95, "max_drawdown": -0.19}
    kw = {"adj_pct": 0.97, "sub_sel": sub, "sub_pool": sub_pool, "book_ok": True, "book": book,
          "index_book": index_book, "spec": spec}
    g1 = {"base": (good, pool), "x2": (good, pool)}
    ok = S.evaluate_gate(g1=g1, **kw)
    assert ok["adopt"] and all(ok["checks"].values()) and set(ok["checks"]) == set(S.GATE_CHECKS)
    assert not S.evaluate_gate(g1=g1, **{**kw, "adj_pct": 0.94})["adopt"]
    bad_sub = {"before_split": {"sharpe": 0.8}, "from_split": {"sharpe": 1.4}}
    r = S.evaluate_gate(g1=g1, **{**kw, "sub_sel": bad_sub})
    assert r["checks"] == {**ok["checks"], "G3_regimes": False}
    assert not S.evaluate_gate(g1={"base": (good, pool), "x2": (pool | {"cagr": 0.05}, pool)}, **kw)["checks"]["G1_pool"]
    assert not S.evaluate_gate(g1=g1, **{**kw, "book_ok": False})["adopt"]
    worse_dd = S.evaluate_gate(g1=g1, **{**kw, "book": {"sharpe": 1.0, "max_drawdown": -0.22}})
    assert not worse_dd["checks"]["B2_index_sleeve"]                  # more than 2 pp deeper than the index book
    lower = S.evaluate_gate(g1=g1, **{**kw, "book": {"sharpe": 0.9, "max_drawdown": -0.10}})
    assert not lower["checks"]["B2_index_sleeve"]


# ------------------------------------------------------------------------------------ sealing and the tag


def test_sealed_parameters_round_trip_and_refuse_tampering(spec, tmp_path):
    pol = Policy.load()
    root = tmp_path / "state-outside"
    digest = S.seal_params(root / "private-params.json", real_nav_usd=12345.0, fixed_fee_usd=1.0)
    patched = {**spec, "costs": {**spec["costs"], "private_params_sha256": digest}}
    assert S.load_sealed_params(patched, pol, root) == {"fixed_fee_usd": 1.0, "real_nav_usd": 12345.0}
    with pytest.raises(SystemExit):
        S.seal_params(root / "private-params.json", real_nav_usd=1.0, fixed_fee_usd=1.0)   # never overwritten
    blob = json.loads((root / "private-params.json").read_text())
    blob["doc"]["real_nav_usd"] = 999.0
    (root / "private-params.json").write_text(json.dumps(blob))
    with pytest.raises(SystemExit, match="do not match"):
        S.load_sealed_params(patched, pol, root)


def test_the_fee_is_charged_per_leg_on_the_sleeve_capital(spec, inp):
    ctx = S.Context(spec=spec, inp=inp, policy=Policy.load(), calendar=pd.DatetimeIndex([]),
                    rows=pd.DatetimeIndex([]), dates=[], eligible={}, funnels={},
                    market=S.Market(core={}, controls={}, long={}), real_nav=4000.0, fee_usd=1.0, draws=0,
                    synthetic=True)
    stand_alone = ctx.stock_costs(["X"], share=0.5)
    assert stand_alone.fixed["X"] == pytest.approx(1.0 / 2000.0)
    assert stand_alone.per_side["X"] == pytest.approx(20e-4)
    in_book = ctx.stock_costs(["X"], share=1.0, var_mult=2.0, fee_mult=2.0)
    assert in_book.fixed["X"] == pytest.approx(2.0 / 4000.0)
    assert in_book.per_side["X"] == pytest.approx(40e-4)
    assert ctx.stock_costs(["X"], share=1.0, fee_mult=0.0).fixed["X"] == 0.0
    pol = Policy.load()
    core = S.core_costs(ctx, S.core_lines(pol), pol)
    assert all(v >= pol.costs["slippage_buffer_bps"] / 1e4 for v in core.per_side.values())   # slippage on every leg


def test_stops_trigger_on_the_low_fill_at_the_gap_and_wait_before_reentry():
    rows = pd.bdate_range("2021-01-04", periods=12)
    plan = BookPlan(name="s", targets=pd.DataFrame({"A": 1.0}, index=rows), levels=pd.DataFrame({"A": 1.0}, index=rows),
                    thresholds=pd.DataFrame({"A": 0.05}, index=rows))
    rets = pd.DataFrame({"A": 0.0}, index=rows)
    rets.iloc[3, 0] = -0.30                                           # closes 30% down; opened 25% down
    open_rel = pd.DataFrame({"A": 0.0}, index=rows)
    open_rel.iloc[3, 0] = -0.25
    low_rel = rets.copy()
    sigma = pd.DataFrame({"A": 0.01}, index=rows)
    rule = S.StopRule(min_distance=0.15, sigma_multiple=3.0, horizon_days=5, max_distance=0.35, sigma_sessions=63,
                      reentry_sessions=3)
    sim, hits = S.simulate_with_stops(plan, rets, open_rel, low_rel, sigma, S.flat_costs(["A"], 0.0, 0.0, "x"), rule,
                                      sleeve_budget=1.0)
    assert hits == 1
    assert sim.nav.iloc[3] == pytest.approx(0.75)                     # filled at the gap open, not at the stop
    assert sim.weights.iloc[3, 0] == 0.0
    assert (sim.weights.iloc[4:7, 0] == 0.0).all()                    # cool-off
    assert sim.weights.iloc[-1, 0] == pytest.approx(1.0)              # back in after the cool-off
    assert S.StopRule(0.15, 3.0, 5, 0.35, 63, 3).distance(np.array([0.001, 0.03, 0.06])) == pytest.approx(
        [0.15, 3 * 0.03 * 5 ** 0.5, 0.35])


def test_execution_lag_shifts_each_decision_by_sessions():
    rows = pd.bdate_range("2021-01-04", periods=30)
    dec = {rows[0]: ["A"], rows[20]: ["B"]}
    assert S.shift_decisions(rows, dec, 4) == {rows[4]: ["A"], rows[24]: ["B"]}
    assert S.shift_decisions(rows, {rows[28]: ["C"]}, 4) == {rows[29]: ["C"]}


def test_concentration_removes_the_top_contributor():
    rows = pd.bdate_range("2021-01-04", periods=60)
    rets = pd.DataFrame({"A": 0.01, "B": 0.0}, index=rows)
    rets.iloc[::2, 1] = 0.002
    plan = BookPlan(name="p", targets=pd.DataFrame({"A": 0.5, "B": 0.5}, index=rows),
                    levels=pd.DataFrame({"A": 1.0, "B": 1.0}, index=rows),
                    thresholds=pd.DataFrame({"A": np.inf, "B": np.inf}, index=rows))
    sim = simulate(plan, rets, S.flat_costs(["A", "B"], 0.0, 0.0, "x"))
    c = S.concentration(sim, rets)
    assert c["top"] == "A" and c["top_share"] > 0.8
    assert c["sharpe_ex_top"] is not None


def _completed(rc: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")


def _repo_git(root: Path, *args: str) -> str:
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@users.noreply.github.com", "-c", "commit.gpgsign=false",
           "-c", "tag.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def tagged_repo(tmp_path, monkeypatch):
    """A throwaway git checkout with one frozen file, one other tracked file and the tag; the study's
    frozen lists point at it."""
    root = tmp_path / "checkout"
    (root / "src").mkdir(parents=True)
    (root / "frozen.py").write_text("RULE = 1\n")
    (root / "src" / "other.py").write_text("HELPER = 1\n")
    (root / ".gitignore").write_text("__pycache__/\n")
    _repo_git(root, "init", "-q")
    _repo_git(root, "add", ".")
    _repo_git(root, "commit", "-q", "-m", "pre-register")
    _repo_git(root, "tag", "stock-sleeve-spec")
    monkeypatch.setattr(S, "REPO", root)
    monkeypatch.setattr(S, "FROZEN_FILES", ("frozen.py",))
    monkeypatch.setattr(S, "FROZEN_DEPENDENCIES", ())
    return root


def test_a_real_run_needs_the_tag_and_unchanged_files(monkeypatch):
    monkeypatch.setattr(S, "_git", lambda *a: _completed(1))
    with pytest.raises(SystemExit, match="does not exist"):
        S.frozen_check("stock-sleeve-spec")


def test_the_frozen_check_passes_only_on_the_clean_tagged_tree(tagged_repo):
    commit = _repo_git(tagged_repo, "rev-parse", "HEAD").strip()
    assert S.frozen_check("stock-sleeve-spec") == commit
    (tagged_repo / "src" / "__pycache__").mkdir()                      # ignored files are fine
    (tagged_repo / "src" / "__pycache__" / "other.pyc").write_bytes(b"x")
    assert S.frozen_check("stock-sleeve-spec") == commit
    assert S.git_blob_sha1(b"RULE = 1\n") == _repo_git(tagged_repo, "rev-parse", "stock-sleeve-spec:frozen.py").strip()

    (tagged_repo / "frozen.py").write_text("RULE = 2\n")
    with pytest.raises(SystemExit, match=r"frozen files differ .*frozen\.py"):
        S.frozen_check("stock-sleeve-spec")
    _repo_git(tagged_repo, "checkout", "--", "frozen.py")

    (tagged_repo / "src" / "other.py").write_text("HELPER = 2\n")    # imported code the lists do not name
    with pytest.raises(SystemExit, match=r"not the tagged tree .*src/other\.py"):
        S.frozen_check("stock-sleeve-spec")
    _repo_git(tagged_repo, "checkout", "--", "src/other.py")

    (tagged_repo / "src" / "shadow.py").write_text("")                  # an untracked module beside the code
    with pytest.raises(SystemExit, match=r"1 not in the tag: src/shadow\.py"):
        S.frozen_check("stock-sleeve-spec")
    (tagged_repo / "src" / "shadow.py").unlink()
    (tagged_repo / "src" / "other.py").unlink()                         # a deleted tracked file
    with pytest.raises(SystemExit, match="not the tagged tree"):
        S.frozen_check("stock-sleeve-spec")
    _repo_git(tagged_repo, "checkout", "--", "src/other.py")
    assert S.frozen_check("stock-sleeve-spec") == commit


def test_index_flags_cannot_hide_an_edit(tagged_repo):
    for rel, flag in (("src/other.py", "--assume-unchanged"), ("frozen.py", "--skip-worktree")):
        _repo_git(tagged_repo, "update-index", flag, rel)
        (tagged_repo / rel).write_text("EDITED = 1\n")
        assert _repo_git(tagged_repo, "status", "--porcelain") == ""     # git itself no longer sees it
        with pytest.raises(SystemExit, match="differ|not the tagged tree"):
            S.frozen_check("stock-sleeve-spec")
        _repo_git(tagged_repo, "update-index", flag.replace("--", "--no-"), rel)
        _repo_git(tagged_repo, "checkout", "--", rel)
    S.frozen_check("stock-sleeve-spec")


def test_a_real_run_refuses_a_head_past_the_tag(tagged_repo):
    (tagged_repo / "src" / "other.py").write_text("HELPER = 2\n")
    _repo_git(tagged_repo, "commit", "-q", "-am", "a later edit to code the lists do not name")
    with pytest.raises(SystemExit, match="HEAD is not the commit tagged"):
        S.frozen_check("stock-sleeve-spec")
    _repo_git(tagged_repo, "checkout", "-q", "stock-sleeve-spec")        # detached at the tag, as the worktree
    S.frozen_check("stock-sleeve-spec")


def test_a_real_run_needs_the_tag_on_the_remote(monkeypatch):
    def git(local: str, remote: str):
        def f(*args):
            if args[0] == "ls-remote":
                return _completed(0, remote)
            return _completed(0, local)
        return f

    monkeypatch.setattr(S, "_git", git("aaa\n", ""))
    with pytest.raises(SystemExit, match="not on origin"):
        S.remote_tag_check("stock-sleeve-spec", "origin")
    monkeypatch.setattr(S, "_git", git("aaa\n", "bbb\trefs/tags/stock-sleeve-spec\n"))
    with pytest.raises(SystemExit, match="differs"):
        S.remote_tag_check("stock-sleeve-spec", "origin")
    monkeypatch.setattr(S, "_git", git("aaa\n", "aaa\trefs/tags/stock-sleeve-spec\n"))
    assert S.remote_tag_check("stock-sleeve-spec", "origin") == "aaa"


def test_freeze_writes_the_hashes_and_run_refuses_any_other_bundle(spec, inp, tmp_path, monkeypatch):
    root = tmp_path / "study"
    bundle = S.Inputs(**{k: getattr(inp, k) for k in S.Inputs.BLOB_FIELDS},
                      manifest={"code_sha256": S.code_hashes(), "sources": {"prices": {"sha256": "ab" * 32}}})
    bundle.save(root / "inputs")
    variants = tmp_path / "variants.yaml"
    pending = S.render_frozen_block("pending", {}, {}) + "\n"        # the variants file before `freeze`
    variants.write_text(re.sub(r"^frozen_inputs:.*?(?=^\S)", pending, S.SPEC_PATH.read_text(), count=1,
                               flags=re.M | re.S))
    with pytest.raises(SystemExit, match="freeze"):
        S.inputs_check(S.load_spec(variants), root)                    # still pending
    monkeypatch.setattr(S, "tag_exists", lambda tag: False)
    S.freeze(spec, root, spec_path=variants)
    frozen = S.load_spec(variants)
    assert frozen["frozen_inputs"]["inputs_sha256"] == S.sha256_file(root / "inputs" / "inputs.pkl")
    assert frozen["frozen_inputs"]["sources"] == {"prices": "ab" * 32}
    S.inputs_check(frozen, root)
    (root / "inputs" / "inputs.pkl").write_bytes(b"other")
    with pytest.raises(SystemExit, match="differs from the frozen"):
        S.inputs_check(frozen, root)
    monkeypatch.setattr(S, "tag_exists", lambda tag: True)
    with pytest.raises(SystemExit, match="tag exists"):
        S.freeze(spec, root, spec_path=variants)


def test_prepare_and_seal_are_refused_once_the_tag_exists(monkeypatch):
    monkeypatch.setattr(S, "tag_exists", lambda tag: True)
    with pytest.raises(SystemExit, match="never rebuilt"):
        S.main(["prepare"])
    with pytest.raises(SystemExit, match="fixed"):
        S.main(["seal", "--real-nav-usd", "1"])


def _pass_the_real_run_guards(monkeypatch) -> Path:
    """Every guard before the once-only rule passes; returns the results folder (in the test state)."""
    monkeypatch.setattr(S, "state_dir_check", lambda: None)
    monkeypatch.setattr(S, "frozen_check", lambda tag: "abc")
    monkeypatch.setattr(S, "remote_tag_check", lambda tag, remote: "abc")
    monkeypatch.setattr(S, "inputs_check", lambda spec, root: {})
    monkeypatch.setattr(S, "load_sealed_params", lambda spec, pol, root: {"real_nav_usd": 1.0, "fixed_fee_usd": 1.0})
    return paths.state_dir() / "backtests" / "stock-sleeve" / "results"


@pytest.mark.parametrize("marker", ["result.json", "gate.json"])
def test_a_second_real_run_is_refused(monkeypatch, marker):
    done = _pass_the_real_run_guards(monkeypatch) / "run-20260101T000000Z"
    done.mkdir(parents=True)
    (done / marker).write_text("{}")                   # gate.json alone: the verdict was known
    with pytest.raises(SystemExit, match="runs once"):
        S.main(["run"])
    assert not (done.parent / "LOCK").exists()         # released on the way out


def test_a_real_run_refuses_a_held_lock(monkeypatch):
    results = _pass_the_real_run_guards(monkeypatch)
    results.mkdir(parents=True)
    (results / "LOCK").write_text("{}")
    with pytest.raises(SystemExit, match="LOCK"):
        S.main(["run"])
    assert (results / "LOCK").exists()                 # someone else's lock is never removed
    with pytest.raises(SystemExit, match="LOCK"), S.run_lock(results):
        pass


def test_a_failed_market_fetch_is_not_an_attempt(monkeypatch):
    results = _pass_the_real_run_guards(monkeypatch)

    def offline(*args, **kwargs):
        raise SystemExit("no Tiingo token")

    monkeypatch.setattr(S, "real_market", offline)
    monkeypatch.setattr(S.Inputs, "load", classmethod(lambda cls, d: S.synthetic_inputs(seed=11)))
    with pytest.raises(SystemExit, match="Tiingo"):
        S.main(["run"])
    assert list(results.iterdir()) == []                # no run folder, no lock


def test_a_real_run_refuses_an_overridden_state_folder(monkeypatch):
    with pytest.raises(SystemExit, match="COUNCIL_STATE_DIR"):     # the test fixture sets it
        S.state_dir_check()
    monkeypatch.setattr(S, "checkout_check", lambda: None)
    with pytest.raises(SystemExit, match="COUNCIL_STATE_DIR"):
        S.main(["run"])
    monkeypatch.delenv("COUNCIL_STATE_DIR")
    monkeypatch.setenv("HOME", "/tmp/elsewhere")
    with pytest.raises(SystemExit, match="HOME"):
        S.state_dir_check()


def test_a_real_run_refuses_a_draws_override():
    with pytest.raises(SystemExit, match="fixed by the spec"):
        S.main(["run", "--draws", "5"])


# ------------------------------------------------------------------------------------ end to end


def test_coverage_counts_only(spec, inp, tmp_path):
    frame = S.coverage(spec, inp, out=paths.state_dir() / "cov", verbose=False)
    assert len(frame) > 20
    assert (frame["member"] >= frame["revenue_floor"]).all()
    assert (paths.state_dir() / "cov" / "coverage.csv").exists()


def test_gate_json_is_written_before_anything_after_the_gate(monkeypatch, tmp_path):
    spec = S.load_spec()
    ctx = S.synthetic_context(spec, Policy.load(), seed=11, draws=2, verbose=False)

    def crash(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(S, "run_current_reference", crash)          # the first step after the gate
    out = paths.state_dir() / "backtests" / "stock-sleeve" / "results" / "run-20260101T000000Z"
    with pytest.raises(KeyboardInterrupt):
        S.run_study(ctx, out, tag_commit=None, verbose=False)
    gate = json.loads((out / "gate.json").read_text())
    assert set(gate) == {"gate", "selected_cell", "overlay", "values"} and not (out / "result.json").exists()
    assert S.completed_runs(out.parent) == [out.name]


def test_synthetic_run_writes_private_outputs_and_a_public_safe_summary(tmp_path):
    before = {p: p.stat().st_mtime_ns for p in (S.DOC_PATH, S.SPEC_PATH)}
    assert S.main(["run", "--synthetic", "--draws", "2"]) == 0
    out = paths.state_dir() / "backtests" / "stock-sleeve" / "synthetic"
    for name in ("result.json", "gate.json", "nav.csv", "selections.csv", "random_null.csv", "cells.csv",
                 "eligibility_funnel.csv", "per_year.csv", "summary.md"):
        assert (out / name).exists(), name
    res = json.loads((out / "result.json").read_text())
    assert res["synthetic"] is True and res["tag_commit"] is None
    assert set(res["cells"]) == {f"{v}-{n}" for v in ("GC", "SC", "SQ") for n in (8, 10)}
    assert res["selected_cell"] in {"SC-8", "SC-10", "SQ-8", "SQ-10"}              # GC is never selected
    assert res["overlay"] in ("none", "down_only", "reference")
    assert set(res["whole_book"]) == {"current_reference", "rebased_none", "rebased_down_only", "rebased_reference",
                                      f"index_book_{res['overlay']}"}
    assert set(res["gate"]["checks"]) == set(S.GATE_CHECKS)
    assert set(res["sensitivities"]) == set(S.SENSITIVITIES)
    assert set(res["diagnostics"]) == set(S.DIAGNOSTICS)
    for k in ("ai_list", "lab_resolver", "stock_stops", "execution_lag", "zero_fixed_fee", "nav_x5"):
        assert "cagr" in res["sensitivities"][k], k
    for opt in res["overlay_options"].values():
        assert set(opt["stress"]) == {"dotcom", "gfc", "y2015_16"} and opt["max_drawdown_x2"] is not None
    assert res["gate_values"]["G2_adjusted_percentile"] <= res["gate_values"]["G2_own_percentile"] + 1e-12
    null = pd.read_csv(out / "random_null.csv")
    assert len(null) == 6 * 2
    text = (out / "summary.md").read_text()
    assert "SYNTHETIC" in text
    assert_public_safe(text)
    assert "per leg" not in text                        # fee drag / legs = fee / funding: never both, per leg
    assert {f"core:{k}" for k in ("BTC", "ETH")} <= set(res["market_sha256"])      # every fetched series
    assert str(tmp_path) not in text
    assert {p: p.stat().st_mtime_ns for p in before} == before        # the repo is never written
