"""WP-G, the engine half: R18 per sleeve (a satellite data freeze holds only the satellite; a core
freeze holds everything, as before), retiring stock lines reduce-only (a data freeze does not pin
them), R9's book-ratio proxy without the satellite, and the per-line material-change rule. On the
re-based fixture policy (tests/fixtures/policy_sleeve/); `invariants.STOCK_SLEEVE_LIVE` stays False,
so no runtime policy has stock lines. Synthetic numbers only."""

from __future__ import annotations

import shutil

import pytest
import yaml

from council.models.risk import Band
from council.policy import Policy
from council.risk.engine import R18_CORE_HOLD, R18_SATELLITE_HOLD, RiskEngine
from council.runtime import engine_quotes, floor_cost_quotes
from tests.conftest import SLEEVE_FIXTURE, make_sleeve_policy_dir
from tests.risk.helpers import NOW, quotes_for, row, snapshot, state

TODAY = {"NDX": 0.35, "SEMIS": 0.15, "SPX": 0.15, "GOLD": 0.12, "BTC": 0.13, "ETH": 0.05}   # the 0.95 core


@pytest.fixture(scope="module")
def retiring_policy(tmp_path_factory) -> Policy:
    """The sleeve fixture with TSTE retiring (a held name the quarter's rank dropped)."""
    overlay = tmp_path_factory.mktemp("overlay_retiring")
    for name in ("universe.yaml", "stock-rank.yaml"):
        shutil.copyfile(SLEEVE_FIXTURE / name, overlay / name)
    sleeve = yaml.safe_load((SLEEVE_FIXTURE / "stock-sleeve.yaml").read_text())
    for line in sleeve["lines"]:
        if line["symbol"] == "TSTE":
            line["role"] = "retiring"
    (overlay / "stock-sleeve.yaml").write_text(yaml.safe_dump(sleeve, sort_keys=False))
    return Policy.load(make_sleeve_policy_dir(tmp_path_factory.mktemp("pol_retiring"), overlay=overlay))


def units_of(pol: Policy) -> dict[str, float]:
    return {ln.symbol: ln.base_weight for ln in pol.universe.lines}


def refs(pol: Policy) -> dict[str, float]:
    return {ln.symbol: (1.0 if ln.in_reference else 0.0) for ln in pol.universe.lines}


def pinned(ref: dict[str, float]) -> dict[str, Band]:
    return {s: Band(symbol=s, trend="up", ref_level=v, lo=v, hi=v) for s, v in ref.items()}


def market(pol: Policy, **per_line) -> dict:
    out = {}
    for ln in pol.universe.lines:
        kw = {"sigma_ann": 0.20} if ln.asset_class == "stock" else {}
        kw.update(per_line.get(ln.symbol, {}))
        out[ln.symbol] = state(ln.symbol, ln.asset_class, **kw)
    return out


NO_DATA = {"frozen": True, "frozen_reason": "no_data", "trend": None, "sigma_ann": None}


def evaluate(pol: Policy, *, current, ref=None, states=None, bands=None, levels=None, held=None, quotes=None,
             **kw):
    ref = ref or refs(pol)
    return RiskEngine(pol).evaluate(
        levels=levels or dict(ref), ref=ref, bands=bands or pinned(ref), states=states or market(pol),
        snapshot=snapshot(current), unit_weights=units_of(pol), kill_state="NORMAL",
        cost_quotes=quotes or engine_quotes(floor_cost_quotes(pol, quoted_at=NOW, fee_bps=0.0)), events=[],
        last_change={}, turnover_7d=0.0, material_changed=kw.pop("material_changed", True), basis="council",
        now=NOW, held_levels=held, **kw)


def notes_of(decision, symbol: str) -> str:
    return " ".join(r for r in decision.hold_reasons if r.startswith(f"{symbol}:"))


# ------------------------------------------------------------------------------------ R18 per sleeve


def test_a_satellite_data_freeze_holds_only_the_satellite(sleeve_policy):
    pol = sleeve_policy
    stocks = [ln.symbol for ln in pol.universe.stock_lines()]
    states = market(pol, **dict.fromkeys(stocks, NO_DATA))      # e.g. no Alpaca keys, or a budget refusal
    held = {s: 1.0 for s in TODAY}
    d = evaluate(pol, current=dict(TODAY), states=states, held=held)
    units = units_of(pol)
    for s in TODAY:                                             # the core re-bases as usual
        assert d.final_w[s] == pytest.approx(units[s]), (s, d.hold_reasons)
    assert all(d.final_w[s] == 0.0 for s in stocks)
    for s in (ln.symbol for ln in pol.universe.stock_lines() if ln.in_reference):
        assert R18_SATELLITE_HOLD in notes_of(d, s)
    r18 = row(d, "R18", "data_freshness")
    assert r18.passed and r18.value == 0.0 and "satellite share 1.00" in r18.detail
    assert d.passed, [(c.rule_id, c.name) for c in d.checks if not c.passed]


def test_one_frozen_stock_below_the_share_limit_holds_only_itself(sleeve_policy):
    pol = sleeve_policy
    units = units_of(pol)
    book = {**{s: units[s] for s in TODAY}}
    d = evaluate(pol, current=book, states=market(pol, TSTA=NO_DATA), held={s: 1.0 for s in TODAY})
    assert d.final_w["TSTA"] == 0.0 and "R18 frozen data" in notes_of(d, "TSTA")
    bought = [s for s in ("TSTB", "TSTC_B", "F") if d.final_w[s] > 0]
    assert bought, d.hold_reasons                               # 1 of 4 frozen: 25% <= 30%


def test_a_core_data_freeze_still_holds_every_line(sleeve_policy):
    pol = sleeve_policy
    units = units_of(pol)
    book = {s: units[s] for s in TODAY}
    states = market(pol, NDX=NO_DATA, SEMIS=NO_DATA)            # 0.237 of the core's 0.45 reference
    ref = {**refs(pol), "SPX": 0.5}                             # SPX would trade without the freeze
    d = evaluate(pol, current=book, states=states, ref=ref, held={s: 1.0 for s in TODAY})
    assert d.final_w["SPX"] == pytest.approx(book["SPX"]) and R18_CORE_HOLD in notes_of(d, "SPX")
    assert all(d.final_w[s] == 0.0 for s in ("TSTA", "TSTB")) and R18_CORE_HOLD in notes_of(d, "TSTA")


def test_core_only_share_is_unchanged(policy):
    """No satellite: the core share is today's single share (every in-reference line)."""
    states = market(policy, NDX=NO_DATA)
    d = evaluate(policy, current={"NDX": 0.35}, states=states, ref={**refs(policy), "SPX": 0.5})
    assert row(d, "R18", "data_freshness").value == pytest.approx(0.35 / (0.95 - 0.075))
    assert R18_CORE_HOLD in notes_of(d, "SPX")


# ------------------------------------------------------------------------------------ retiring lines


def test_a_retiring_line_under_a_data_freeze_is_still_sold(retiring_policy):
    pol = retiring_policy
    units = units_of(pol)
    book = {**{s: units[s] for s in TODAY}, "TSTE": 0.06}
    held = {**{s: 1.0 for s in TODAY}, "TSTE": 1.0}
    stale = {"frozen": True, "frozen_reason": "stale"}          # old bars, a sigma still known
    d = evaluate(pol, current=book, states=market(pol, TSTE=stale), held=held)
    assert d.final_w["TSTE"] == 0.0, d.hold_reasons
    assert row(d, "R18", "data_freshness").passed and d.passed


def test_a_retiring_line_is_never_added_whatever_its_band(retiring_policy):
    pol = retiring_policy
    units = units_of(pol)
    book = {**{s: units[s] for s in TODAY}, "TSTE": 0.03}
    ref = refs(pol)
    bands = {**pinned(ref), "TSTE": Band(symbol="TSTE", trend="up", ref_level=0.0, lo=0.0, hi=1.0)}
    d = evaluate(pol, current=book, ref=ref, bands=bands, levels={**ref, "TSTE": 1.0},
                 held={**{s: 1.0 for s in TODAY}, "TSTE": 0.5})
    assert d.final_w["TSTE"] <= 0.03 + 1e-12 and "retiring line: reduce only" in notes_of(d, "TSTE")


def test_a_closed_session_still_holds_a_retiring_line(retiring_policy):
    pol = retiring_policy
    units = units_of(pol)
    book = {**{s: units[s] for s in TODAY}, "TSTE": 0.06}
    closed = {"market_open": False}
    d = evaluate(pol, current=book, states=market(pol, TSTE=closed), held={**{s: 1.0 for s in TODAY}, "TSTE": 1.0})
    assert d.final_w["TSTE"] == pytest.approx(0.06) and "R19 market closed" in notes_of(d, "TSTE")


def test_a_satellite_share_freeze_keeps_a_retiring_line_reduce_only(retiring_policy):
    pol = retiring_policy
    units = units_of(pol)
    selected = [ln.symbol for ln in pol.universe.stock_lines() if ln.in_reference]
    book = {**{s: units[s] for s in TODAY}, "TSTE": 0.06}
    states = market(pol, **dict.fromkeys(selected, NO_DATA))
    d = evaluate(pol, current=book, states=states, held={**{s: 1.0 for s in TODAY}, "TSTE": 1.0})
    assert d.final_w["TSTE"] == 0.0, d.hold_reasons             # sold, although the satellite is held
    assert all(d.final_w[s] == 0.0 and R18_SATELLITE_HOLD in notes_of(d, s) for s in selected)


# ------------------------------------------------------------------------------------ R9 proxy


def test_the_book_breaker_proxy_leaves_the_satellite_out(sleeve_policy):
    pol = sleeve_policy
    stocks = [ln.symbol for ln in pol.universe.stock_lines() if ln.in_reference]
    shocked = market(pol, **{s: {"ewma5_60_ratio": 5.0} for s in stocks})
    d = evaluate(pol, current={}, states=shocked, held={})
    bought_core = [s for s in TODAY if d.final_w[s] > 0]
    assert bought_core, d.hold_reasons                          # the book breaker did not fire
    assert all(d.final_w[s] == 0.0 and "R9 vol breaker" in notes_of(d, s) for s in stocks), d.hold_reasons
    core_shock = market(pol, **{s: {"ewma5_60_ratio": 2.5} for s in TODAY})
    d = evaluate(pol, current={}, states=core_shock, held={})
    assert all(d.final_w[s] == 0.0 for s in TODAY)              # a core shock still blocks every add


# ------------------------------------------------------------------------------------ MC per line


def test_the_material_change_rule_is_per_line(policy):
    ref = refs(policy)
    units = units_of(policy)
    current = {s: units[s] for s in ("NDX", "SPX", "SEMIS", "GOLD")}
    bands = {**pinned(ref), "NDX": Band(symbol="NDX", trend="up", ref_level=1.0, lo=0.5, hi=1.0),
             "SPX": Band(symbol="SPX", trend="up", ref_level=1.0, lo=0.5, hi=1.0)}
    levels = {**ref, "NDX": 0.5, "SPX": 0.5}                    # two discretionary cuts
    mc = {s: s == "SPX" for s in units}
    cheap = quotes_for(policy, per_side=0.5)
    d = evaluate(policy, current=current, bands=bands, levels=levels, material_changed=mc,
                 held={s: 1.0 for s in current}, quotes=cheap)
    assert d.final_w["SPX"] == pytest.approx(0.5 * units["SPX"]), d.hold_reasons
    assert d.final_w["NDX"] == pytest.approx(units["NDX"]) and "MC no new material evidence" in notes_of(d, "NDX")
    assert row(d, "MC").passed
    same = evaluate(policy, current=current, bands=bands, levels=levels, material_changed=True,
                    held={s: 1.0 for s in current}, quotes=cheap)
    assert same.final_w["NDX"] == pytest.approx(0.5 * units["NDX"])     # a single flag: as before
