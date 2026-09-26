"""WP-E: the reference trading rule in the engine (R11 via `reference.sleeve.pending_trades`), the
fee in R14, the risk-reducing exemptions, the trim order and the R21 count, on the re-based fixture
policy (tests/fixtures/policy_sleeve/; `invariants.STOCK_SLEEVE_LIVE` stays False, so no runtime
policy has stock lines). Synthetic numbers only."""

from __future__ import annotations

import shutil

import pytest
import yaml

from council.models.risk import Band
from council.policy import Policy
from council.risk import churn
from council.risk.config import risk_limits
from council.risk.engine import RiskEngine, leg_origins
from council.runtime import engine_quotes, floor_cost_quotes
from tests.conftest import SLEEVE_FIXTURE, make_sleeve_policy_dir
from tests.risk.helpers import NOW, loose, override, row, snapshot, state

FEE = 6.0                                # bps of NAV per real non-crypto leg (synthetic account)
COPY_FLOOR = 3.0 / 2_000.0               # the real-dollar floor as a NAV share (synthetic account)
TODAY = {"NDX": 0.35, "SEMIS": 0.15, "SPX": 0.15, "GOLD": 0.12, "BTC": 0.13, "ETH": 0.05}   # the 0.95 core


# ----------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def ten_name_policy(tmp_path_factory) -> Policy:
    """The re-based fixture universe with a 10-name sleeve at 0.50 (unit 0.05)."""
    overlay = tmp_path_factory.mktemp("overlay10")
    for name in ("universe.yaml", "stock-rank.yaml"):
        shutil.copyfile(SLEEVE_FIXTURE / name, overlay / name)
    sleeve = yaml.safe_load((SLEEVE_FIXTURE / "stock-sleeve.yaml").read_text())
    sleeve["names_target"] = 10
    sleeve["lines"] = [
        {"symbol": f"TS{chr(65 + i)}{chr(65 + i)}", "name": f"Test Name {i}", "role": "selected",
         "sector": "BusEq", "cik": f"{900100 + i:010d}", "rank": i + 1,
         "signal_ticker": f"TS{chr(65 + i)}{chr(65 + i)}", "etoro_symbol": f"TS{chr(65 + i)}{chr(65 + i)}",
         "eligibility_checked_at": "2026-11-20T15:02:00Z", "credited": None, "aliases": []}
        for i in range(10)
    ]
    (overlay / "stock-sleeve.yaml").write_text(yaml.safe_dump(sleeve, sort_keys=False))
    return Policy.load(make_sleeve_policy_dir(tmp_path_factory.mktemp("policy10"), overlay=overlay))


def stocks(pol: Policy) -> list[str]:
    return [ln.symbol for ln in pol.universe.stock_lines()]


def units_of(pol: Policy) -> dict[str, float]:
    return {ln.symbol: ln.base_weight for ln in pol.universe.lines}


def refs(pol: Policy, stock_level: float = 1.0, **levels: float) -> dict[str, float]:
    out = {}
    for ln in pol.universe.lines:
        if ln.asset_class == "stock":
            out[ln.symbol] = stock_level if ln.in_reference else 0.0
        else:
            out[ln.symbol] = 1.0 if ln.in_reference else 0.0
    out.update(levels)
    return out


def pinned(pol: Policy, ref: dict[str, float]) -> dict[str, Band]:
    """The council holds the reference: every band is [ref, ref]."""
    return {s: Band(symbol=s, trend="up", ref_level=v, lo=v, hi=v) for s, v in ref.items()}


def market(pol: Policy, *, us_open: bool = True, **per_line) -> dict:
    out = {}
    for ln in pol.universe.lines:
        kw = {"sigma_ann": 0.20} if ln.asset_class == "stock" else {}
        if ln.asset_class == "stock":
            kw["market_open"] = us_open
        kw.update(per_line.get(ln.symbol, {}))
        out[ln.symbol] = state(ln.symbol, ln.asset_class, **kw)
    return out


def evaluate(pol: Policy, *, current, ref, held=None, units=None, states=None, levels=None,
             bands=None, fee=FEE, **kw):
    units = units or units_of(pol)
    return RiskEngine(pol).evaluate(
        levels=levels or dict(ref), ref=ref, bands=bands or pinned(pol, ref),
        states=states or market(pol), snapshot=snapshot(current), unit_weights=units,
        kill_state="NORMAL",
        cost_quotes=engine_quotes(floor_cost_quotes(pol, quoted_at=NOW, fee_bps=fee)),
        events=[], last_change={}, turnover_7d=0.0, material_changed=True, basis="council", now=NOW,
        held_levels=held, copy_min_share=COPY_FLOOR, **kw)


def moved(decision, current) -> dict[str, float]:
    return {s: w for s, w in decision.final_w.items() if abs(w - current.get(s, 0.0)) > 1e-9}


# ----------------------------------------------------------------------------- re-base and build


def test_todays_core_reaches_the_rebased_reference_in_one_lse_cycle(sleeve_policy):
    pol = sleeve_policy
    units = units_of(pol)
    assert sum(units[s] for s in TODAY) == pytest.approx(0.449997)
    ref = refs(pol)
    held = {s: 1.0 for s in TODAY}                    # the core last traded at level 1.0
    d = evaluate(pol, current=dict(TODAY), ref=ref, held=held,
                 states=market(pol, us_open=False))   # 10:40 UTC: LSE open, US closed
    for s in TODAY:
        assert d.final_w[s] == pytest.approx(units[s]), (s, d.hold_reasons)
    assert all(d.final_w[s] == 0.0 for s in stocks(pol))           # stock lines session-frozen
    assert d.passed, [(c.rule_id, c.name, c.value) for c in d.checks if not c.passed]
    # the sells are risk-reducing reference legs: outside the R14 cycle cap and the R21 count
    assert row(d, "R14", "cycle_cost_bps").value == 0.0 and row(d, "R21").value == 0.0
    origins = leg_origins(d.base_w, d.final_w, {s: ref[s] * units[s] for s in units})
    assert {origins[s] for s in TODAY} == {"reference"}


def test_a_ten_name_build_completes_in_two_us_cycles(ten_name_policy):
    pol = ten_name_policy
    units = units_of(pol)
    names = stocks(pol)
    assert len(names) == 10 and all(units[s] == pytest.approx(0.05) for s in names)
    ref = refs(pol)
    book = {s: units[s] for s in TODAY}               # the core is already re-based
    held = {s: 1.0 for s in TODAY}
    first = evaluate(pol, current=book, ref=ref, held=held)
    bought = [s for s in names if first.final_w[s] > 0]
    # each open costs 0.05 x 20 bps + the 6 bps fee = 7 bps: five fit the 40 bps cycle budget
    assert len(bought) == 5 and row(first, "R14", "cycle_cost_bps").value == "R14_fee"
    assert all("R14 cycle cost budget" in r for r in first.hold_reasons if r.split(":")[0] in names)
    book = {**book, **{s: first.final_w[s] for s in bought}}
    held.update({s: 1.0 for s in bought})             # the fills record their reference level
    second = evaluate(pol, current=book, ref=ref, held=held)
    assert all(second.final_w[s] == pytest.approx(0.05) for s in names), second.hold_reasons
    assert second.passed


def test_ten_overlay_down_stock_sells_go_in_one_cycle(ten_name_policy):
    pol = ten_name_policy
    units = units_of(pol)
    names = stocks(pol)
    book = {**{s: units[s] for s in TODAY}, **{s: 0.05 for s in names}}
    held = {**{s: 1.0 for s in TODAY}, **{s: 1.0 for s in names}}
    ref = refs(pol, stock_level=0.5)                  # the overlay halves the sleeve
    d = evaluate(pol, current=book, ref=ref, held=held)
    assert all(d.final_w[s] == pytest.approx(0.025) for s in names), d.hold_reasons
    assert len(moved(d, book)) == 10 > risk_limits(pol).proposal.max_legs
    assert row(d, "R21").value == 0.0 and row(d, "R21").passed    # none of them counts
    assert d.passed


def test_the_hard_total_leg_cap_still_binds(ten_name_policy):
    pol = override(ten_name_policy, "risk", {"proposal.max_legs_total": 8})
    units = units_of(pol)
    names = stocks(pol)
    book = {**{s: units[s] for s in TODAY}, **{s: 0.05 for s in names}}
    held = {**{s: 1.0 for s in TODAY}, **{s: 1.0 for s in names}}
    d = evaluate(pol, current=book, ref=refs(pol, stock_level=0.5), held=held)
    assert len(moved(d, book)) == 8 and any("R21 too many legs in total" in r for r in d.hold_reasons)


# ----------------------------------------------------------------------------- the rule on core lines


def test_rebased_core_level_flips_execute(sleeve_policy):
    pol = sleeve_policy
    units = units_of(pol)
    core = {s: units[s] for s in TODAY}
    core["ETH"] = 0.75 * units["ETH"]
    held = {s: 1.0 for s in TODAY}
    held["ETH"] = 0.75
    ref = refs(pol, SEMIS=0.75, ETH=0.25)             # SEMIS up -> mixed; ETH mixed -> down (0.5 step)
    d = evaluate(pol, current=core, ref=ref, held=held)
    assert d.final_w["SEMIS"] == pytest.approx(0.75 * units["SEMIS"]), d.hold_reasons
    assert d.final_w["ETH"] == pytest.approx(0.25 * units["ETH"]), d.hold_reasons
    # both moves are below today's 2% NAV floor: the old R11 would have held them
    for s, step in (("SEMIS", 0.25), ("ETH", 0.5)):
        dw = abs(d.final_w[s] - core[s])
        assert dw < 0.02
        assert not churn.deadband_ok(step, dw, to_zero=False, crypto=s == "ETH", min_share=0.0, policy=pol)
    assert d.passed


def test_a_unit_drift_below_the_threshold_does_not_trade(sleeve_policy):
    pol = sleeve_policy
    units = units_of(pol)
    book = {s: units[s] for s in TODAY}
    held = {s: 1.0 for s in TODAY}
    shrunk = {**units, "NDX": 0.155}                  # the vol cap moved the unit, not the level
    d = evaluate(pol, current=book, ref=refs(pol), held=held, units=shrunk)
    assert d.final_w["NDX"] == pytest.approx(book["NDX"])
    assert any(r.startswith("NDX: R11 reference rule") for r in d.hold_reasons)
    bigger = {**units, "NDX": 0.12}                   # drift 0.046 >= max(0.25 x 0.12, 2%): trades
    d = evaluate(pol, current=book, ref=refs(pol), held=held, units=bigger)
    assert d.final_w["NDX"] == pytest.approx(0.12)


def test_the_copy_minimum_floors_reference_legs(sleeve_policy):
    pol = sleeve_policy
    units = units_of(pol)
    book = {s: units[s] for s in TODAY}
    book["ETH"] = units["ETH"] - 0.001               # 0.1% NAV away, below the 0.15% floor
    held = {s: 1.0 for s in TODAY}
    held["ETH"] = 0.75                               # a level change: the rule wants the trade
    d = evaluate(pol, current=book, ref=refs(pol), held=held)
    assert d.final_w["ETH"] == pytest.approx(book["ETH"])
    assert any(r.startswith("ETH: R11 below the minimum trade size") for r in d.hold_reasons)


def test_the_rule_is_the_frozen_pending_trades(sleeve_policy, monkeypatch):
    from council.reference import sleeve

    calls = []

    def never(*args, **kwargs):
        calls.append(args)
        return sleeve.np.zeros(len(args[0]), dtype=bool)

    monkeypatch.setattr(sleeve, "pending_trades", never)
    pol = sleeve_policy
    units = units_of(pol)
    d = evaluate(pol, current=dict(TODAY), ref=refs(pol), held={s: 1.0 for s in TODAY},
                 states=market(pol, us_open=False))
    assert calls, "the engine must call council.reference.sleeve.pending_trades"
    assert all(d.final_w[s] == pytest.approx(TODAY[s]) for s in TODAY)     # nothing pending: held
    assert units["NDX"] < TODAY["NDX"]


def test_held_levels_default_to_the_current_book(sleeve_policy):
    """Without a held-level input the engine migrates from the current book (snap to the grid)."""
    pol = sleeve_policy
    units = units_of(pol)
    book = {s: units[s] for s in TODAY}
    book["SEMIS"] = 0.75 * units["SEMIS"] + 0.001    # snaps to 0.75, the new reference level
    d = evaluate(pol, current=book, ref=refs(pol, SEMIS=0.75), held=None)
    assert d.final_w["SEMIS"] == pytest.approx(book["SEMIS"])               # no level change, no drift


def test_never_borrow_trims_a_held_name_above_target(ten_name_policy):
    """A sleeve over its budget orders every name held above its target back to it, even when
    its own drift is below the threshold (the never-borrow rule of pending_trades)."""
    pol = ten_name_policy
    units = units_of(pol)
    names = stocks(pol)
    book = {**{s: units[s] for s in TODAY}, **{s: 0.05 for s in names}}
    book[names[0]] = 0.058                           # +0.8% NAV, below its drift threshold
    book[names[1]] = 0.0                             # a new name to buy: the sleeve would exceed 0.50
    held = {**{s: 1.0 for s in TODAY}, **{s: 1.0 for s in names}}
    held[names[1]] = 0.0
    d = evaluate(pol, current=book, ref=refs(pol), held=held)
    assert d.final_w[names[0]] == pytest.approx(0.05) and d.final_w[names[1]] == pytest.approx(0.05)
    alone = {**book, names[1]: 0.05}                 # nothing to buy, but 0.508 > 0.50: still trimmed
    d = evaluate(pol, current=alone, ref=refs(pol), held={**held, names[1]: 1.0})
    assert d.final_w[names[0]] == pytest.approx(0.05)
    within = {**book, names[1]: 0.042}               # 0.058 + 0.042 + 8 x 0.05 = 0.50: no trim
    d = evaluate(pol, current=within, ref=refs(pol), held={**held, names[1]: 1.0})
    assert d.final_w[names[0]] == pytest.approx(0.058) and d.final_w[names[1]] == pytest.approx(0.042)


# ----------------------------------------------------------------------------- R14, trim order, R21


def test_r14_includes_the_fixed_fee(policy):
    pol = loose(policy, cost_budget__cycle_max_bps=40.0,
                net_of_cost_gate__reference_max_srbe=100.0, net_of_cost_gate__council_max_srbe=100.0)
    ref = {ln.symbol: 0.0 for ln in pol.universe.lines}
    ref["NDX"] = 1.0
    units = {ln.symbol: 0.3 for ln in pol.universe.lines}   # 0.3 x 15 bps = 4.5 bps variable
    kw = {"current": {}, "ref": ref, "units": units, "held": {}, "states": market(pol)}
    assert evaluate(pol, fee=30.0, **kw).final_w["NDX"] == pytest.approx(0.3)     # 34.5 <= 40
    held = evaluate(pol, fee=36.0, **kw)                                          # 40.5 > 40
    assert held.final_w["NDX"] == 0.0 and any("R14 cycle cost" in r for r in held.hold_reasons)
    assert evaluate(pol, fee=0.0, **kw).final_w["NDX"] == pytest.approx(0.3)


def test_risk_increasing_legs_are_trimmed_first(policy):
    pol = loose(policy, cost_budget__cycle_max_bps=40.0,
                net_of_cost_gate__reference_max_srbe=100.0, net_of_cost_gate__council_max_srbe=100.0,
                material_change_required=False)
    ref = {ln.symbol: 0.0 for ln in pol.universe.lines}
    ref["GOLD"] = 1.0
    units = {ln.symbol: 0.1 for ln in pol.universe.lines}
    units["GOLD"] = 0.5
    current = {"GOLD": 0.5}
    # a council cut on GOLD (risk-reducing, away from the reference: discretionary) costs
    # 0.3 x 15 bps + a 30 bps fee = 34.5; a council add on OIL (risk-increasing CFD) 0.1 x 18 = 1.8.
    # Together 36.3 > 36: the add waits although the cut is larger and has the higher SR_be (the
    # order before WP-E would have held the cut)
    lv = {**ref, "GOLD": 0.4, "OIL": 1.0}
    budget = override(pol, "risk", {"cost_budget.cycle_max_bps": 36.0})
    d = evaluate(budget, current=current, ref=ref, units=units, held={"GOLD": 1.0}, levels=lv,
                 bands={s: Band(symbol=s, trend="up", ref_level=ref[s], lo=-1.0, hi=1.0) for s in ref},
                 fee=30.0)
    assert d.final_w["OIL"] == 0.0, d.hold_reasons                 # the risk-increasing leg waits
    assert d.final_w["GOLD"] == pytest.approx(0.2)                 # the risk-reducing cut goes
    assert any(r.startswith("OIL: R14 cycle cost budget") for r in d.hold_reasons)
    # R21 in the same order: with room for one counted leg, the cut goes and the add waits
    one_leg = override(pol, "risk", {"proposal.max_legs": 1})
    d = evaluate(one_leg, current=current, ref=ref, units=units, held={"GOLD": 1.0}, levels=lv,
                 bands={s: Band(symbol=s, trend="up", ref_level=ref[s], lo=-1.0, hi=1.0) for s in ref},
                 fee=0.0)
    assert d.final_w["OIL"] == 0.0 and d.final_w["GOLD"] == pytest.approx(0.2)


def test_r21_counts_only_risk_increasing_and_discretionary_legs(ten_name_policy):
    pol = override(ten_name_policy, "risk", {"proposal.max_legs": 2, "cost_budget.cycle_max_bps": 1e6,
                                             "churn.cycle_increase_max": 10.0})
    units = units_of(pol)
    names = stocks(pol)
    book = {**{s: units[s] for s in TODAY}, **{s: 0.05 for s in names[:6]}}
    held = {**{s: 1.0 for s in TODAY}, **{s: 1.0 for s in names[:6]}}
    ref = refs(pol, stock_level=0.5)                 # 6 reference sells (uncounted) and 4 opens
    d = evaluate(pol, current=book, ref=ref, held=held)
    sells = [s for s in names[:6] if d.final_w[s] == pytest.approx(0.025)]
    buys = [s for s in names[6:] if d.final_w[s] > 0]
    assert len(sells) == 6 and len(buys) == 2 and row(d, "R21").value == 2.0
    assert sum(1 for r in d.hold_reasons if "R21 too many legs" in r) == 2
