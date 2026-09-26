"""council.stocks.rank on hand-built inputs: strict available_at < D, every universe filter with its
exclusion reason, membership freshness, the lookahead guard, the config read from the tagged
variants file (SQ-8), the hold buffer, the shortlist, the AI-list count, and proof that the rank
calls the frozen `score.select_rule` and holds no copy of the rule."""

from __future__ import annotations

import ast
import copy
from datetime import date

import numpy as np
import pandas as pd
import pytest
import yaml

from council.paths import REPO_ROOT
from council.stocks import pit, score
from council.stocks import rank as rank_mod
from council.stocks.rank import RankConfig, RankError, rank
from council.stocks.universe import AI, Candidate, RankInputs, SourceStamp

D = pd.Timestamp("2026-08-20")
DAY_BEFORE = D - pd.Timedelta(days=1)
SRC = REPO_ROOT / "src" / "council" / "stocks"
SPEC = yaml.safe_load((REPO_ROOT / "policy" / "variants" / "stock-sleeve-variants-v1.yaml").read_text())


def quarters(cik: int, *, growth: float = 0.02, accel: float = 0.0, gm: float = 0.5, gm_step: float = 0.0,
             om: float = 0.2, rev0: float = 1e9, end: str = "2026-06-30", lag_days: int = 40, form: str = "10-Q",
             filed: dict[str, str] | None = None, gp: dict[str, float] | None = None) -> pd.DataFrame:
    """First-reported quarterly rows (pit.COMPARABLE_COLUMNS) with the year-ago columns filled."""
    rows, rev = [], rev0
    for i, pe in enumerate(pd.date_range("2023-03-31", end, freq="QE")):
        rev *= 1 + growth + accel * i
        g = gm + gm_step * i
        when = pd.Timestamp((filed or {}).get(str(pe.date()), pe + pd.Timedelta(days=lag_days)))
        rows.append({"ticker": str(cik), "cik": str(cik), "period_end": pe, "fy": pe.year, "fp": f"Q{pe.quarter}",
                     "form": form, "accession": f"{cik}-{pe.date()}", "available_at": when, "revenue": rev,
                     "gross_profit": (gp or {}).get(str(pe.date()), rev * g), "operating_income": rev * om,
                     "net_income": rev * om * 0.8, "cfo": np.nan, "capex": np.nan, "cash": np.nan,
                     "debt_total": np.nan, "shares_outstanding": np.nan, "is_derived_q4": False})
    return pit.year_ago_from_rows(pd.DataFrame(rows)[pit.FUNDAMENTALS_COLUMNS])


class Book:
    """Builder for RankInputs: every candidate is a clean, eligible name unless overridden."""

    def __init__(self) -> None:
        self.cands: list[Candidate] = []
        self.sic: dict[int, object] = {}
        self.tax: dict[int, str] = {}
        self.fund: dict[int, pd.DataFrame] = {}
        self._next = 1000

    def add(self, key: str, *, sic: object = "3674", taxonomy: str = "us-gaap", fund: pd.DataFrame | None = None,
            cik: int | None = -1, first: str = "2020-01-02", last: pd.Timestamp = DAY_BEFORE,
            dv: float | None = 1e9, sources: frozenset[str] = frozenset({"sp500"}), **kw) -> int | None:
        if cik == -1:
            self._next += 1
            cik = self._next
        self.cands.append(Candidate(key=key, symbol=key, cik=cik, sources=sources, first_bar=pd.Timestamp(first),
                                    last_bar=last, dollar_volume=dv, line_id=key, **kw))
        if cik is not None:
            self.sic.setdefault(cik, sic)
            self.tax.setdefault(cik, taxonomy)
            self.fund.setdefault(cik, quarters(cik) if fund is None else fund)
        return cik

    def inputs(self, **kw) -> RankInputs:
        return RankInputs(candidates=tuple(self.cands), sic=self.sic, taxonomy=self.tax, fundamentals=self.fund, **kw)


def good_book(n_per_sector: int = 6) -> Book:
    b = Book()
    rng = np.random.default_rng(5)
    for sector, sic in (("B", "3674"), ("H", "2834"), ("S", "5311")):
        for i in range(n_per_sector):
            cik = 5000 + 100 * ord(sector) + i
            b.add(f"{sector}{i:02d}", sic=sic, cik=cik,
                  fund=quarters(cik, growth=float(rng.uniform(0.0, 0.06)), accel=float(rng.normal(0, 0.002)),
                                gm_step=float(rng.normal(0, 0.003))))
    return b


def cfg(**kw) -> RankConfig:
    return RankConfig(**kw)


# ------------------------------------------------------------------------------------ availability


def test_a_filing_dated_on_the_rank_date_is_not_visible():
    b = good_book()
    late = b.add("LATE", cik=77, fund=quarters(77, filed={"2026-06-30": str(D.date()), "2026-03-31": "2026-05-10"}))
    assert late == 77
    on_d = rank(D, b.inputs(), cfg())
    assert on_d.eligible.loc["LATE", "latest_period_end"] == pd.Timestamp("2026-03-31")
    assert on_d.eligible.loc["LATE", "latest_available_at"] == pd.Timestamp("2026-05-10")
    next_day = rank(D + pd.Timedelta(days=1), b.inputs(), cfg())
    assert next_day.eligible.loc["LATE", "latest_period_end"] == pd.Timestamp("2026-06-30")
    for day in (D, D + pd.Timedelta(days=1)):
        visible = rank(day, b.inputs(), cfg()).eligible["latest_available_at"]
        assert (visible < day).all()


def test_a_filing_dated_the_day_before_is_visible():
    b = good_book()
    b.add("EVE", cik=78, fund=quarters(78, filed={"2026-06-30": str((D - pd.Timedelta(days=1)).date())}))
    assert rank(D, b.inputs(), cfg()).eligible.loc["EVE", "latest_period_end"] == pd.Timestamp("2026-06-30")


# ------------------------------------------------------------------------------------ filters


def test_every_filter_excludes_with_its_reason_in_the_study_order():
    b = good_book()
    good_cik = b.cands[0].cik
    b.add("REIT", security_type="reit")
    b.add("ADRX", is_adr=True)
    b.add("OLDPX", last=D - pd.Timedelta(days=8))
    b.add("EDGEPX", last=D - pd.Timedelta(days=7))                      # exactly 7 days: priced
    b.add("NEWCO", first=str((D - pd.Timedelta(days=289)).date()))
    b.add("EDGEL", first=str((D - pd.Timedelta(days=290)).date()))    # exactly 290 days: listed
    b.add("NOCIK", cik=None)
    b.add("CLASSB", cik=good_cik, dv=1.0)                              # second class of the first name
    b.add("NOSIC", sic=None)
    b.add("BANK", sic="6021")
    b.add("NOFACTS", taxonomy="no_companyfacts")
    b.add("IFRS", taxonomy="ifrs-full")
    b.add("NOFILING", fund=quarters(1, end="2026-06-30").iloc[0:0])
    b.add("FORM20F", fund=quarters(2, form="20-F"))
    b.add("STALE", fund=quarters(3, end="2026-03-31", filed={"2026-03-31": "2026-04-10"}))
    b.add("TINY", fund=quarters(4, rev0=2e7))
    b.add("NOGP", fund=quarters(5, gp={"2026-06-30": np.nan}))
    b.add("WEIRD", fund=quarters(6, gp={"2025-06-30": 5e9}))           # year-ago gross profit above revenue
    res = rank(D, b.inputs(unmapped=(("GONE", "no_cik"),)), cfg())
    assert {k: v for k, v in res.excluded.items()} == {
        "REIT": "not_common", "ADRX": "not_common", "OLDPX": "not_priced", "NEWCO": "not_listed",
        "NOCIK": "no_cik", "CLASSB": "duplicate_cik", "NOSIC": "no_sector", "BANK": "excluded_sector",
        "NOFACTS": "no_companyfacts", "IFRS": "not_us_gaap", "NOFILING": "no_visible_quarter",
        "FORM20F": "not_domestic_form", "STALE": "stale_filing", "TINY": "revenue_floor",
        "NOGP": "missing_feature", "WEIRD": "implausible"}
    assert {"EDGEPX", "EDGEL", b.cands[0].key} <= set(res.eligible.index)
    f = res.funnel
    assert list(f) == list(rank_mod.FUNNEL)
    assert f["member"] == len(b.cands) + 1 and f["mapped"] == len(b.cands)
    assert list(f.values()) == sorted(f.values(), reverse=True)
    assert f["plausible"] == len(res.eligible) == 18 + 2
    assert res.exclusion_counts["not_common"] == 2


def test_the_filters_read_the_config():
    b = good_book()
    b.add("REIT", security_type="reit")
    b.add("BANK", sic="6021")
    loose = rank(D, b.inputs(), cfg(security_types=("common", "reit"), exclude_sectors=()))
    assert {"REIT", "BANK"} <= set(loose.eligible.index)


def test_money_names_never_reach_the_score_even_when_best():
    b = good_book()
    b.add("BANK", sic="6021", fund=quarters(9, growth=0.5))
    res = rank(D, b.inputs(), cfg())
    assert "BANK" not in res.eligible.index and "BANK" not in res.order


def test_a_bar_after_the_rank_date_is_refused_as_lookahead():
    b = good_book()
    b.add("FUTURE", last=D + pd.Timedelta(days=1))
    with pytest.raises(RankError, match="lookahead"):
        rank(D, b.inputs(), cfg())


def test_candidate_keys_must_be_unique():
    b = good_book()
    b.add("B00", cik=4242)
    with pytest.raises(ValueError, match="unique"):
        rank(D, b.inputs(), cfg())


@pytest.mark.parametrize(("as_of", "ok"), [("2026-04-22", True), ("2026-08-20", True), ("2026-04-21", False),
                                           ("2026-08-21", False)])
def test_membership_sources_must_be_fresh_and_not_from_the_future(as_of, ok):
    inputs = good_book().inputs(sources=(SourceStamp("mediawiki:sp500", date.fromisoformat(as_of)),))
    if ok:
        rank(D, inputs, cfg())
    else:
        with pytest.raises(RankError, match="mediawiki:sp500"):
            rank(D, inputs, cfg())


# ------------------------------------------------------------------------------------ selection


def test_the_rank_calls_the_frozen_select_rule(monkeypatch):
    real = score.select_rule
    calls = []

    def spy(elig, held, variant, n, buffer_mult):
        calls.append((list(elig.index), list(held), dict(variant), n, buffer_mult))
        return list(reversed(real(elig, held, variant, n, buffer_mult)))    # a visibly different answer

    inputs = good_book().inputs(held=("B00", "ZZ_NOT_ELIGIBLE"))
    monkeypatch.setattr(score, "select_rule", spy)
    res = rank(D, inputs, cfg())
    monkeypatch.setattr(score, "select_rule", real)
    expected = real(res.eligible, ["B00", "ZZ_NOT_ELIGIBLE"], {"score": "sector", "constraint": "quota", "cap": 3}, 8, 2.0)
    assert res.selected == tuple(reversed(expected))
    elig, held, variant, n, buf = calls[0]
    assert set(elig) == set(res.eligible.index) and held == ["B00", "ZZ_NOT_ELIGIBLE"]
    assert variant == {"score": "sector", "constraint": "quota", "cap": 3} and n == 8 and buf == 2.0
    elig2, held2, _, n2, _ = calls[1]                                      # the shortlist: same function
    assert held2 == [] and n2 == 8 and set(elig2) == set(res.eligible.index) - set(res.selected)
    assert len(calls) == 2


def test_the_stock_modules_hold_no_copy_of_the_frozen_rule():
    frozen = {"select_rule", "sector_quotas", "ordered", "add_scores", "peer_groups", "rank_average",
              "comparable_features", "fundamental_features", "fundamentals_comparable", "fundamentals_for_company",
              "rule_quarters", "plausible_quarter", "year_ago_from_rows", "ff12", "parse_siccodes12",
              "overlay_level", "unit_weight", "sleeve_targets", "drift_threshold", "pending_trades"}
    for name in ("rank.py", "universe.py", "sec.py"):
        tree = ast.parse((SRC / name).read_text())
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)}
        assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        imported = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                    and (n.module or "").startswith("council.stocks.") for a in n.names}
        assert not (defined | assigned) & frozen, name
        assert not imported & frozen, f"{name} must call the frozen functions through their module"
    tree = ast.parse((SRC / "rank.py").read_text())
    called = {(n.func.value.id, n.func.attr) for n in ast.walk(tree) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)}
    assert {("score", "select_rule"), ("score", "add_scores"), ("score", "ordered"),
            ("pit", "comparable_features"), ("sectors", "ff12")} <= called


def test_the_hold_buffer_keeps_a_held_name_ranked_inside_twice_its_quota():
    b = good_book()
    first = rank(D, b.inputs(), cfg())
    elig = first.eligible
    quotas = score.sector_quotas(elig["sector"].value_counts().to_dict(), 8, 3)
    sector = next(s for s, q in quotas.items() if q >= 1)
    in_s = [k for k in first.order if elig.at[k, "sector"] == sector]
    outside = in_s[quotas[sector]]                      # best name just outside the sector's quota
    assert outside not in first.selected
    again = rank(D, b.inputs(held=(outside,)), cfg())
    assert outside in again.selected and again.kept == (outside,)
    no_buffer = rank(D, b.inputs(held=(outside,)), cfg(hold_buffer_multiple=1.0))
    assert outside not in no_buffer.selected


def test_shortlist_order_and_roles():
    res = rank(D, good_book().inputs(), cfg())
    assert len(res.selected) == 8 and 0 < len(res.shortlist) <= 8
    assert not set(res.selected) & set(res.shortlist)
    assert set(res.order) == set(res.eligible.index) and len(res.order) == len(res.eligible)
    rest = res.eligible.drop(index=list(res.selected))
    assert list(res.shortlist) == score.select_rule(rest, [], dict(res.config.variant), 8, 2.0)
    assert res.role(res.selected[0]) == "selected" and res.role(res.shortlist[0]) == "shortlist"
    assert res.role("nope") is None
    assert res.symbols(list(res.selected[:2])) == list(res.selected[:2])
    assert rank(D, good_book().inputs(), cfg(shortlist_size=0)).shortlist == ()


def test_ai_only_names_are_ranked_mechanically_and_counted():
    b = good_book()
    b.add("AIHOT", sources=frozenset({AI}), sic="3674",
          fund=quarters(88, growth=0.2, accel=0.01, gm_step=0.01))
    b.add("BOTH", sources=frozenset({AI, "nasdaq100"}), sic="2834", fund=quarters(89, growth=0.2, accel=0.01))
    res = rank(D, b.inputs(), cfg())
    assert "AIHOT" in res.selected and "BOTH" in res.selected
    assert res.ai_selected == ("AIHOT",) and res.ai_only == frozenset({"AIHOT"})
    assert bool(res.eligible.loc["AIHOT", "ai_only"]) and not bool(res.eligible.loc["BOTH", "ai_only"])


def test_an_empty_universe_gives_an_empty_rank():
    res = rank(D, RankInputs(candidates=()), cfg())
    assert res.selected == () and res.shortlist == () and res.order == () and res.eligible.empty


# ------------------------------------------------------------------------------------ config


def test_the_config_is_the_adopted_cell_read_from_the_adoption_record():
    from council.stocks import adopted

    c = RankConfig.selected()                           # through council.stocks.adopted (spec L10)
    assert c == RankConfig() == RankConfig.from_variants(SPEC, "SQ-8")   # defaults, record and spec agree
    assert rank_mod.DEFAULT_CELL == adopted.CELL == "SQ-8" and c.cell == "SQ-8" and c.n == adopted.NAMES == 8
    assert dict(c.variant) == {"score": "sector", "constraint": "quota", "cap": 3}
    u = SPEC["universe"]
    assert c.hold_buffer_multiple == SPEC["hold_buffer_multiple"] and c.min_peer_group == SPEC["rule"]["min_peer_group"]
    assert (c.max_bar_age_days, c.min_listing_days, c.max_filing_age_days) == (
        u["max_bar_age_days"], u["min_listing_days"], u["max_filing_age_days"])
    assert c.min_quarter_revenue_usd == u["min_quarter_revenue_usd"] and list(c.domestic_forms) == u["domestic_forms"]
    assert list(c.exclude_sectors) == u["exclude_sectors"] == ["Money"]
    assert set(c.domestic_forms) == set(pit.DOMESTIC_FORMS)
    assert RankConfig.from_variants(SPEC, "SC-10").variant["constraint"] == "cap"


def test_a_tampered_adoption_record_view_is_refused():
    from dataclasses import replace

    from council.stocks import adopted

    rule = adopted.load_adopted()
    with pytest.raises(ValueError):
        RankConfig.from_adopted(replace(rule, data_layer="lab"))
    with pytest.raises(ValueError):
        RankConfig.from_adopted(replace(rule, features=("revenue_growth_yoy",)))
    with pytest.raises(ValueError):
        RankConfig.from_adopted(replace(rule, cell="SQ-9"))


@pytest.mark.parametrize("cell", ["GC-8", "GC-10", "SQ-9", "XX-8", "SQ", "SQ-eight"])
def test_controls_and_unknown_cells_are_refused(cell):
    with pytest.raises(ValueError):
        RankConfig.from_variants(SPEC, cell)


@pytest.mark.parametrize(("path", "value"), [(("universe", "dedupe"), "ticker"),
                                             (("rebalance", "filings_visible"), "filed_on_or_before_D"),
                                             (("rule", "data_layer"), "lab"),
                                             (("rule", "features"), ["revenue_growth_yoy"])])
def test_spec_values_the_live_rank_does_not_implement_are_refused(path, value):
    spec = copy.deepcopy(SPEC)
    spec[path[0]][path[1]] = value
    with pytest.raises(ValueError):
        RankConfig.from_variants(spec)


def test_config_digest_is_stable_and_sensitive():
    assert RankConfig().digest() == RankConfig.selected().digest()
    assert RankConfig().digest() != RankConfig(n=10).digest()
    with pytest.raises(ValueError):
        RankConfig(variant={"score": "vibes", "constraint": "quota", "cap": 3})
