"""RiskEngine: a pass and a fail case for every number in risk.yaml the engine enforces."""

from __future__ import annotations

from datetime import timedelta

import pytest

from council.models.facts import EventItem
from council.models.risk import Band
from council.risk import checks as ck
from council.risk.config import risk_limits
from council.risk.engine import ForbiddenLegError, classify_risk_increasing, toward_reference
from tests.risk.helpers import (
    NOW,
    default_ref,
    default_units,
    loose,
    quote,
    quotes_for,
    row,
    run,
    states_for,
)


def wide(policy, lo=-1.5, hi=1.5):
    return {ln.symbol: Band(symbol=ln.symbol, trend="mixed", ref_level=0.0, lo=lo, hi=hi)
            for ln in policy.universe.lines}


def zeros(policy):
    return {ln.symbol: 0.0 for ln in policy.universe.lines}


def iso(pol, *, levels, units=None, current=None, ref=None, **kw):
    """Only the listed lines are non-zero; every other line sits at 0 with reference 0."""
    u = default_units(pol)
    u.update(units or {})
    r = zeros(pol)
    r.update(ref or {})
    lv = dict(r)
    lv.update(levels)
    kw.setdefault("bands", wide(pol))
    return run(pol, levels=lv, ref=r, unit_weights=u, current=current, **kw)


def approx_w(d, line):
    return pytest.approx(d.final_w.get(line, 0.0), abs=1e-9)


# ----------------------------------------------------------------------------- R1 gross


def test_r1_gross_under_proposal_max_is_untouched(policy):
    pol = loose(policy)
    units = {s: 0.2 for s in zeros(pol)}
    d = iso(pol, levels={s: 1.0 for s in units}, units=units)
    assert d.gross == pytest.approx(1.8) and all(v == pytest.approx(0.2) for v in d.final_w.values())
    assert row(d, "R1").passed


def test_r1_gross_above_proposal_max_is_projected_to_190(policy):
    pol = loose(policy)
    units = {s: 0.3 for s in zeros(pol)}
    d = iso(pol, levels={s: 1.0 for s in units}, units=units)
    assert d.gross == pytest.approx(1.90, abs=1e-9) and d.passed
    assert any("aggregate" in r for r in d.hold_reasons)


def test_r1_between_proposal_and_hard_max_may_be_held(policy):
    pol = loose(policy)
    current = {"NDX": 0.8, "SPX": 0.8, "SEMIS": 0.35}  # gross 1.95 <= hard 2.00
    units = {"NDX": 0.8, "SPX": 0.8, "SEMIS": 0.35}
    d = iso(pol, levels={"NDX": 1.0, "SPX": 1.0, "SEMIS": 1.0}, units=units, current=current)
    assert d.gross == pytest.approx(1.95) and d.compliance == [] and row(d, "R1").passed


def test_r1_above_hard_max_derisks_to_180(policy):
    pol = loose(policy)
    current = {"NDX": 0.8, "SPX": 0.8, "SEMIS": 0.4, "GOLD": 0.1}  # gross 2.10 > 2.00
    units = {"NDX": 0.8, "SPX": 0.8, "SEMIS": 0.4, "GOLD": 0.1}
    d = iso(pol, levels={"NDX": 1.0, "SPX": 1.0, "SEMIS": 1.0, "GOLD": 1.0}, units=units,
            current=current)
    assert d.gross <= 1.80 + 1e-9 and d.compliance and d.compliance[0].startswith("R1")
    assert row(d, "R1").passed


def test_r1_check_rows(policy):
    lim = risk_limits(policy)
    assert ck.check_gross({"A": 1.90}, {"A": 1.0}, lim).passed
    assert not ck.check_gross({"A": 1.91}, {"A": 1.0}, lim).passed
    assert ck.check_gross({"A": 1.99}, {"A": 2.0}, lim).passed          # reduced, base <= hard
    assert not ck.check_gross({"A": 2.01}, {"A": 2.05}, lim).passed     # base above hard
    assert ck.check_gross({"A": 1.80}, {"A": 1.8}, lim, derisk=True).passed
    assert not ck.check_gross({"A": 1.85}, {"A": 1.8}, lim, derisk=True).passed


# ----------------------------------------------------------------------------- R2 net / shorts


def test_r2_net_floor_minus_050(policy):
    pol = loose(policy)
    units = {"NDX": 0.2, "SPX": 0.2}
    ok = iso(pol, levels={"NDX": -1.0, "SPX": -1.0}, units=units)
    assert ok.net == pytest.approx(-0.4)
    units = {"NDX": 0.3, "SPX": 0.3}
    d = iso(pol, levels={"NDX": -1.0, "SPX": -1.0}, units=units)
    assert d.net == pytest.approx(-0.50, abs=1e-9) and d.passed


def test_r2_short_gross_060(policy):
    pol = loose(policy)
    units = {"GOLD": 0.5, "NDX": 0.275, "SPX": 0.275}
    ok = iso(pol, levels={"GOLD": 1.0, "NDX": -1.0, "SPX": -1.0}, units=units)
    assert ck.short_gross(ok.final_w) == pytest.approx(0.55)
    units = {"GOLD": 0.5, "NDX": 0.4, "SPX": 0.4}
    d = iso(pol, levels={"GOLD": 1.0, "NDX": -1.0, "SPX": -1.0}, units=units)
    assert ck.short_gross(d.final_w) == pytest.approx(0.60, abs=1e-9) and d.passed
    assert d.final_w["GOLD"] == pytest.approx(0.5)


def test_r2_cut_that_breaks_the_net_floor_is_held(policy):
    """Shorts pinned by a closed market: cutting the only long would push net below -0.50."""
    pol = loose(policy)
    closed = states_for(pol, NDX={"market_open": False}, SPX={"market_open": False})
    units = {"NDX": 0.3, "SPX": 0.3, "OIL": 0.2}
    current = {"NDX": -0.3, "SPX": -0.3, "OIL": 0.2}                      # net -0.40
    levels = {"NDX": -1.0, "SPX": -1.0, "OIL": 0.0}
    d = iso(pol, levels=levels, units=units, current=current, states=closed)
    assert d.final_w["OIL"] == pytest.approx(0.2) and d.net == pytest.approx(-0.4)
    assert any("R2 net floor" in r for r in d.hold_reasons) and d.passed
    small = {"NDX": -0.2, "SPX": -0.2, "OIL": 0.2}                        # cut -> net -0.40
    d = iso(pol, levels={**levels, "NDX": -0.2 / 0.3, "SPX": -0.2 / 0.3}, units=units,
            current=small, states=closed)
    assert d.final_w["OIL"] == 0.0 and d.net == pytest.approx(-0.4)


def test_r2_net_ceiling_check(policy):
    lim = risk_limits(policy)
    assert ck.check_net({"A": 1.90}, {}, lim).passed
    assert not ck.check_net({"A": 1.95}, {}, lim).passed
    assert not ck.check_net({"A": -0.51}, {}, lim).passed
    assert ck.check_short_gross({"A": -0.6}, {}, lim).passed
    assert not ck.check_short_gross({"A": -0.61}, {}, lim).passed


# ----------------------------------------------------------------------------- R3 kill switch


def test_r3_warn_blocks_every_increase(policy):
    pol = loose(policy)
    current = {"NDX": 0.2, "SPX": -0.05}
    levels = {"NDX": 1.0, "SPX": -1.0, "GOLD": 1.0}
    normal = iso(pol, levels=levels, current=current)
    assert normal.final_w["NDX"] == pytest.approx(0.35) and normal.final_w["GOLD"] > 0
    warn = iso(pol, levels=levels, current=current, kill_state="WARN")
    assert warn.final_w["NDX"] == pytest.approx(0.2)
    assert warn.final_w["SPX"] == pytest.approx(-0.05) and warn.final_w["GOLD"] == 0.0
    assert row(warn, "R3").passed


def test_r3_warn_still_allows_reductions(policy):
    pol = loose(policy)
    d = iso(pol, levels={"NDX": 0.0}, current={"NDX": 0.35}, kill_state="WARN")
    assert d.final_w["NDX"] == 0.0


@pytest.mark.parametrize("state", ["HALTED", "FLAT"])
def test_r3_halted_flattens_everything(policy, state):
    d = run(policy, current={"NDX": 0.3, "UNMAPPED_77": 0.05}, kill_state=state)
    assert d.basis == "halted" and set(d.final_w.values()) == {0.0}
    assert "UNMAPPED_77" in d.final_w and d.compliance[0].startswith("R3")
    assert d.passed


# ----------------------------------------------------------------------------- R4 stops


def test_r4_no_stop_no_increase(policy):
    pol = loose(policy)
    states = states_for(pol, NDX={"sigma_daily": None, "sigma_ann": None})
    d = iso(pol, levels={"NDX": 1.0, "SPX": 1.0}, states=states, current={"NDX": 0.1})
    assert d.final_w["NDX"] == pytest.approx(0.1) and d.final_w["SPX"] == pytest.approx(0.15)
    r4 = row(d, "R4")
    assert r4.passed and r4.value > 0  # stop-at-risk reported


def test_r4d_cooloff_blocks_reentry(policy):
    pol = loose(policy)
    hit = {"BTC": NOW - timedelta(days=6), "NDX": NOW - timedelta(days=3)}
    d = iso(pol, levels={"BTC": 1.0, "NDX": 1.0}, stop_hits=hit)
    assert d.final_w["BTC"] == 0.0 and d.final_w["NDX"] == pytest.approx(0.35)


# ----------------------------------------------------------------------------- R5 caps

CAPS = {"NDX": 0.80, "SEMIS": 0.40, "SPX": 0.80, "GOLD": 0.50, "BTC": 0.40, "ETH": 0.20,
        "OIL": 0.20, "EURUSD": 0.30, "GBPUSD": 0.30}


@pytest.mark.parametrize("line,cap", sorted(CAPS.items()))
def test_r5_line_caps(policy, line, cap):
    pol = loose(policy)
    assert risk_limits(pol).caps.line[line] == cap
    under = iso(pol, levels={line: 1.0}, units={line: cap - 0.05})
    assert under.final_w[line] == pytest.approx(cap - 0.05)
    over = iso(pol, levels={line: 1.0}, units={line: cap + 0.1})
    assert over.final_w[line] == pytest.approx(cap) and over.passed
    short = iso(pol, levels={line: -1.0}, units={line: cap + 0.1})
    assert short.final_w[line] >= -cap - 1e-9


def test_r5_line_over_cap_may_be_held(policy):
    pol = loose(policy)
    d = iso(pol, levels={"ETH": 1.0}, units={"ETH": 0.25}, current={"ETH": 0.25})
    assert d.final_w["ETH"] == pytest.approx(0.25) and row(d, "R5", "line_caps").passed


@pytest.mark.parametrize(
    "name,units,cap",
    [("crypto_total", {"BTC": 0.4, "ETH": 0.2}, 0.5),
     ("fx_total", {"EURUSD": 0.3, "GBPUSD": 0.3}, 0.5),
     ("equity_beta_cluster", {"NDX": 0.8, "SPX": 0.8, "SEMIS": 0.4}, 1.6)],
)
def test_r5_group_caps(policy, name, units, cap):
    key = "caps.equity_beta_cluster.max" if name == "equity_beta_cluster" else f"caps.{name}"
    pol = loose(policy, **{key.replace(".", "__"): cap})
    levels = {s: 1.0 for s in units}
    over = iso(pol, levels=levels, units=units)
    assert ck.group_gross(over.final_w, units) == pytest.approx(cap, abs=1e-9)
    assert row(over, "R5", name).passed
    scale = (cap - 0.01) / sum(units.values())
    under = iso(pol, levels=levels, units={s: u * scale for s, u in units.items()})
    assert ck.group_gross(under.final_w, units) == pytest.approx(cap - 0.01)


def test_r5_reference_only_lines_shrink_under_group_cap(policy):
    """[ref, ref] bands cannot make a hard cap unreachable."""
    pol = loose(policy, caps__crypto_total=0.5)
    ref = default_ref(pol)
    d = run(pol, levels=ref, ref=ref, unit_weights={**default_units(pol), "BTC": 0.4, "ETH": 0.2})
    assert d.final_w["BTC"] + d.final_w["ETH"] == pytest.approx(0.5, abs=1e-9) and d.passed


# ----------------------------------------------------------------------------- R6 / R7 / R8


def test_r6_crypto_leverage_cap_1(policy):
    pol = loose(policy)
    d = iso(pol, levels={"BTC": 1.5, "NDX": 1.5}, units={"BTC": 0.2, "NDX": 0.3})
    assert d.final_w["BTC"] == pytest.approx(0.2)            # level 1.0
    assert d.final_w["NDX"] == pytest.approx(0.45)           # level 1.5 allowed at L = 2
    strict = loose(policy, leverage_caps__index=1)
    d = iso(strict, levels={"NDX": 1.5}, units={"NDX": 0.3})
    assert d.final_w["NDX"] == pytest.approx(0.3) and row(d, "R6").passed


def test_r6_check_row(policy):
    lim = risk_limits(policy)
    cls = {"BTC": "crypto", "NDX": "index"}
    assert ck.check_leverage({"BTC": 0.2, "NDX": 0.45}, {"BTC": 0.2, "NDX": 0.3}, cls, lim).passed
    assert not ck.check_leverage({"BTC": 0.3}, {"BTC": 0.2}, cls, lim).passed


def test_r7_margin_use_095(policy):
    pol = loose(policy, margin_use_max=0.95)
    units = {"NDX": 0.3, "SEMIS": 0.3, "SPX": 0.3}
    ok = iso(pol, levels={s: 1.0 for s in units}, units=units)
    assert ok.margin_use == pytest.approx(0.9)
    units4 = {**units, "GOLD": 0.3}
    d = iso(pol, levels={s: 1.0 for s in units4}, units=units4)
    assert d.margin_use == pytest.approx(0.95, abs=1e-9) and d.passed
    # the leverage extension counts at L = 2: gross 1.2 but margin 0.3 + 0.6 = 0.9
    lev = iso(pol, levels={"NDX": 1.5, "SPX": 1.0}, units={"NDX": 0.4, "SPX": 0.6})
    assert lev.gross == pytest.approx(1.2) and lev.margin_use == pytest.approx(0.9)


def test_r8_ex_ante_vol_030(policy):
    pol = loose(policy, ex_ante_vol_hard=0.30)

    def vol(w):
        return 0.25 * sum(abs(v) for v in w.values())

    units = {"NDX": 0.5, "SPX": 0.5}
    ok = iso(pol, levels={"NDX": 1.0, "SPX": 1.0}, units=units, ex_ante_vol_fn=vol)
    assert ok.ex_ante_vol == pytest.approx(0.25)
    units = {"NDX": 0.8, "SPX": 0.8}
    d = iso(pol, levels={"NDX": 1.0, "SPX": 1.0}, units=units, ex_ante_vol_fn=vol)
    assert d.ex_ante_vol <= 0.30 + 1e-6 and d.ex_ante_vol == pytest.approx(0.30, abs=1e-6)
    assert d.passed


def test_r8_default_is_the_rho_one_upper_bound(policy):
    pol = loose(policy, ex_ante_vol_hard=0.30)
    d = iso(pol, levels={"SEMIS": 1.0, "BTC": 1.0}, units={"SEMIS": 0.4, "BTC": 0.4})
    assert d.ex_ante_vol == pytest.approx(0.30, abs=1e-6)
    assert "rho = 1" in row(d, "R8").detail


# ----------------------------------------------------------------------------- R9 breakers


def test_r9_instrument_ratio_30(policy):
    pol = loose(policy)
    hot = states_for(pol, NDX={"ewma5_60_ratio": 3.0})
    warm = states_for(pol, NDX={"ewma5_60_ratio": 2.99})
    blocked = iso(pol, levels={"NDX": 1.0, "SPX": 1.0}, current={"NDX": 0.1}, states=hot,
                  book_vol_ratio=1.0)
    assert blocked.final_w["NDX"] == pytest.approx(0.1) and blocked.final_w["SPX"] > 0
    free = iso(pol, levels={"NDX": 1.0}, current={"NDX": 0.1}, states=warm, book_vol_ratio=1.0)
    assert free.final_w["NDX"] == pytest.approx(0.35)


def test_r9_book_ratio_20(policy):
    pol = loose(policy)
    blocked = iso(pol, levels={"NDX": 1.0, "SPX": 1.0}, book_vol_ratio=2.0)
    assert blocked.final_w["NDX"] == 0.0 and blocked.final_w["SPX"] == 0.0
    free = iso(pol, levels={"NDX": 1.0}, book_vol_ratio=1.99)
    assert free.final_w["NDX"] == pytest.approx(0.35)


def test_r9_book_ratio_proxy_from_instruments(policy):
    """Without a book series, the vol-weighted instrument ratios stand in for the book ratio."""
    pol = loose(policy)
    hot = {s: {"ewma5_60_ratio": 2.5} for s in zeros(pol)}
    d = iso(pol, levels={"NDX": 1.0}, ref={"NDX": 1.0}, states=states_for(pol, **hot))
    assert d.final_w["NDX"] == 0.0


# ----------------------------------------------------------------------------- R10 authority


def test_r10_levels_are_clipped_into_bands(policy):
    d = run(policy, levels={**default_ref(policy), "NDX": 1.5, "OIL": -0.5})
    assert d.banded_levels["NDX"] == 1.0 and d.banded_levels["OIL"] == 0.0
    assert any(r.startswith("NDX: R10") for r in d.hold_reasons)
    assert row(d, "R10").passed


def test_r10_at_most_three_deviations(policy):
    pol = loose(policy, authority__max_deviations_per_cycle=3)
    ref = {s: 0.5 for s in zeros(pol)}
    levels = {**ref, "NDX": 1.0, "SPX": 0.0, "SEMIS": 0.75, "GOLD": 0.375}  # smallest dropped
    d = iso(pol, levels=levels, ref=ref, bands=wide(pol, lo=0.0, hi=1.0))
    moved = [s for s in ("NDX", "SPX", "SEMIS", "GOLD") if d.banded_levels[s] != 0.5]
    assert moved == ["NDX", "SPX", "SEMIS"] and row(d, "R10").value == 3.0
    pol4 = loose(policy, authority__max_deviations_per_cycle=4)
    d4 = iso(pol4, levels=levels, ref=ref, bands=wide(pol4, lo=0.0, hi=1.0))
    assert d4.banded_levels["GOLD"] == 0.375


# ----------------------------------------------------------------------------- R11 deadband


def test_r11_level_step_025(policy):
    pol = loose(policy)
    cur = {"NDX": 0.75 * 0.35}
    moves = iso(pol, levels={"NDX": 1.0}, current=cur)
    assert moves.final_w["NDX"] == pytest.approx(0.35)
    held = iso(pol, levels={"NDX": 0.99}, current=cur)
    assert held.final_w["NDX"] == pytest.approx(cur["NDX"])
    assert any("R11" in r for r in held.hold_reasons)


def test_r11_crypto_step_05(policy):
    pol = loose(policy)
    cur = {"BTC": 0.5 * 0.13}
    assert iso(pol, levels={"BTC": 1.0}, current=cur).final_w["BTC"] == pytest.approx(0.13)
    assert iso(pol, levels={"BTC": 0.99}, current=cur).final_w["BTC"] == pytest.approx(cur["BTC"])


def test_r11_min_nav_share_002(policy):
    pol = loose(policy)
    assert iso(pol, levels={"SEMIS": 0.25}, units={"SEMIS": 0.08}).final_w["SEMIS"] == \
        pytest.approx(0.02)
    assert iso(pol, levels={"SEMIS": 0.25}, units={"SEMIS": 0.07}).final_w["SEMIS"] == 0.0
    big_min = iso(pol, levels={"SEMIS": 0.25}, units={"SEMIS": 0.08},
                  broker_min_share={"SEMIS": 0.05})
    assert big_min.final_w["SEMIS"] == 0.0


# ----------------------------------------------------------------------------- R12 / MC


def _cut_case(pol, last_change, material=True, current_level=1.0, target=0.5):
    ref = {"NDX": 1.0}
    return iso(pol, levels={"NDX": target}, ref=ref, current={"NDX": current_level * 0.35},
               last_change={"NDX": last_change}, material_changed=material)


def test_r12_min_hold_3_days(policy):
    pol = loose(policy)
    held = _cut_case(pol, NOW - timedelta(days=2, hours=23))
    assert held.final_w["NDX"] == pytest.approx(0.35)
    assert any("R12" in r for r in held.hold_reasons)
    moved = _cut_case(pol, NOW - timedelta(days=3))
    assert moved.final_w["NDX"] == pytest.approx(0.175)


def test_r12_toward_reference_is_exempt(policy):
    pol = loose(policy)
    d = _cut_case(pol, NOW - timedelta(hours=1), current_level=0.5, target=1.0)
    assert d.final_w["NDX"] == pytest.approx(0.35)


def test_material_change_required_for_deviations_only(policy):
    pol = loose(policy)
    no_news = _cut_case(pol, None, material=False)
    assert no_news.final_w["NDX"] == pytest.approx(0.35) and row(no_news, "MC").passed
    back = _cut_case(pol, None, material=False, current_level=0.5, target=1.0)
    assert back.final_w["NDX"] == pytest.approx(0.35)
    relaxed = loose(policy, material_change_required=False)
    assert _cut_case(relaxed, None, material=False).final_w["NDX"] == pytest.approx(0.175)


# ----------------------------------------------------------------------------- R13 churn


def test_r13_cycle_increase_060(policy):
    pol = loose(policy, churn__cycle_increase_max=0.6)
    units = {"NDX": 0.3, "SPX": 0.3}
    ok = iso(pol, levels={"NDX": 1.0, "SPX": 1.0}, units=units)
    assert ck.gross(ok.final_w) == pytest.approx(0.6)
    units3 = {"NDX": 0.25, "SPX": 0.25, "GOLD": 0.25}
    d = iso(pol, levels={s: 1.0 for s in units3}, units=units3)
    assert ck.gross(d.final_w) == pytest.approx(0.5) and row(d, "R13", "cycle_increase").passed
    assert any("R13 cycle increase" in r for r in d.hold_reasons)
    toward = iso(pol, levels={s: 1.0 for s in units3}, units=units3, ref={s: 1.0 for s in units3})
    assert ck.gross(toward.final_w) == pytest.approx(0.75)  # moves toward the reference are exempt


@pytest.mark.parametrize("window,prior_ok,prior_bad", [("7d", 0.6, 0.7), ("30d", 2.6, 2.7)])
def test_r13_turnover(policy, window, prior_ok, prior_bad):
    pol = loose(policy, churn__turnover_7d_max=1.0, churn__turnover_30d_max=3.0)
    units = {"NDX": 0.4}
    kw = {"turnover_7d": prior_ok} if window == "7d" else {"turnover_30d": prior_ok}
    assert iso(pol, levels={"NDX": 1.0}, units=units, **kw).final_w["NDX"] == pytest.approx(0.4)
    kw = {"turnover_7d": prior_bad} if window == "7d" else {"turnover_30d": prior_bad}
    d = iso(pol, levels={"NDX": 1.0}, units=units, **kw)
    assert d.final_w["NDX"] == 0.0 and d.passed


# ----------------------------------------------------------------------------- R14 costs

GATE_OFF = {"net_of_cost_gate__reference_max_srbe": 100.0,
            "net_of_cost_gate__council_max_srbe": 100.0}


def test_r14_cycle_cost_40_bps(policy):
    pol = loose(policy, cost_budget__cycle_max_bps=40.0, **GATE_OFF)
    quotes = quotes_for(pol, per_side=100.0)
    ok = iso(pol, levels={"NDX": 1.0}, units={"NDX": 0.39}, cost_quotes=quotes)
    assert ok.final_w["NDX"] == pytest.approx(0.39)
    assert row(ok, "R14", "cycle_cost_bps").value == pytest.approx(39.0)
    d = iso(pol, levels={"NDX": 1.0}, units={"NDX": 0.41}, cost_quotes=quotes)
    assert d.final_w["NDX"] == 0.0 and any("R14 cycle cost" in r for r in d.hold_reasons)


def test_r14_discretionary_30d_100_bps(policy):
    pol = loose(policy, cost_budget__discretionary_30d_max_bps=100.0, **GATE_OFF)
    quotes = quotes_for(pol, per_side=10.0)
    ok = iso(pol, levels={"NDX": 1.0}, units={"NDX": 0.4}, cost_quotes=quotes, cost_30d_bps=96.0)
    assert ok.final_w["NDX"] == pytest.approx(0.4)
    d = iso(pol, levels={"NDX": 1.0}, units={"NDX": 0.4}, cost_quotes=quotes, cost_30d_bps=96.5)
    assert d.final_w["NDX"] == 0.0


def test_r14_carry_2_bps_day(policy):
    pol = loose(policy, cost_budget__carry_proposal_max_bps_day=2.0, **GATE_OFF)
    quotes = quotes_for(pol, carry=5.0)
    ok = iso(pol, levels={"NDX": 1.0}, units={"NDX": 0.39}, cost_quotes=quotes)
    assert ok.carry_bps_day == pytest.approx(1.95)
    d = iso(pol, levels={"NDX": 1.0}, units={"NDX": 0.41}, cost_quotes=quotes)
    assert d.final_w["NDX"] == 0.0 and row(d, "R14", "carry_bps_day").passed


# ----------------------------------------------------------------------------- R15 cost gate


def test_r15_reference_030_vs_council_020(policy):
    pol = loose(policy)
    quotes = quotes_for(pol, per_side=15.0)
    # toward the reference: 90-day reference horizon -> SR_be ~0.055; as a deviation: 20 days -> ~0.25
    to_ref = iso(pol, levels={"NDX": 1.0}, ref={"NDX": 1.0}, cost_quotes=quotes)
    assert to_ref.final_w["NDX"] == pytest.approx(0.35)
    assert row(to_ref, "R15").value == pytest.approx(0.0553, abs=1e-3)
    deviation = iso(pol, levels={"NDX": 1.0}, cost_quotes=quotes)
    assert deviation.final_w["NDX"] == 0.0
    assert any("R15 SR_be" in r for r in deviation.hold_reasons)


def test_r15_missing_quote_or_vol_holds(policy):
    pol = loose(policy)
    quotes = {k: v for k, v in quotes_for(pol).items() if k[0] != "NDX"}
    d = iso(pol, levels={"NDX": 1.0, "SPX": 1.0}, cost_quotes=quotes)
    assert d.final_w["NDX"] == 0.0 and d.final_w["SPX"] == pytest.approx(0.15)
    wrong_side = {("NDX", "long"): quote("NDX", "short")}
    d = iso(pol, levels={"NDX": 1.0}, cost_quotes=wrong_side)
    assert d.final_w["NDX"] == 0.0


def test_r15_hold_days_crypto_60(policy):
    pol = loose(policy)
    # the same 60 bps/side quote on council deviations: BTC (60-day hold, sigma 55%) passes,
    # NDX (20-day) does not; toward the reference both pass on the longer reference horizon
    quotes = quotes_for(pol, per_side=60.0)
    d = iso(pol, levels={"BTC": 1.0, "NDX": 1.0}, cost_quotes=quotes)
    assert d.final_w["BTC"] == pytest.approx(0.13) and d.final_w["NDX"] == 0.0
    d = iso(pol, levels={"BTC": 1.0, "NDX": 1.0}, ref={"BTC": 1.0, "NDX": 1.0}, cost_quotes=quotes)
    assert d.final_w["BTC"] == pytest.approx(0.13) and d.final_w["NDX"] == pytest.approx(0.35)


# ----------------------------------------------------------------------------- R16 / R17


def test_r16_event_window_blocks_adds_only(policy):
    pol = loose(policy)

    def fomc(hours):
        return [EventItem(id="E:fomc", kind="fomc", at_utc=NOW + timedelta(hours=hours),
                          symbols=[], severity=3, source="calendar")]

    blocked = iso(pol, levels={"NDX": 1.0, "SPX": 0.0}, current={"SPX": 0.15}, events=fomc(12))
    assert blocked.final_w["NDX"] == 0.0 and blocked.final_w["SPX"] == 0.0  # the sale still runs
    free = iso(pol, levels={"NDX": 1.0}, events=fomc(25))
    assert free.final_w["NDX"] == pytest.approx(0.35)


def test_r17_anti_chase_25(policy):
    pol = loose(policy)
    hot = states_for(pol, NDX={"ret1d_sigma": 2.6}, SPX={"ret1d_sigma": -2.6})
    d = iso(pol, levels={"NDX": 1.0, "SPX": -1.0}, states=hot)
    assert d.final_w["NDX"] == 0.0 and d.final_w["SPX"] == 0.0
    calm = states_for(pol, NDX={"ret1d_sigma": 2.4})
    assert iso(pol, levels={"NDX": 1.0}, states=calm).final_w["NDX"] == pytest.approx(0.35)
    # a hot up-move does not stop a new short
    assert iso(pol, levels={"NDX": -1.0}, states=hot).final_w["NDX"] == pytest.approx(-0.35)


# ----------------------------------------------------------------------------- R18 - R21


def test_r18_frozen_line_holds_and_raw_age_is_not_rederived(policy):
    """The pack decides freshness (weekend/holiday-aware, 30 h): a frozen line holds; a raw
    data_age_h above 30 h on an unfrozen state (e.g. a Friday bar on Monday) does not."""
    pol = loose(policy)
    stale = states_for(pol, NDX={"data_age_h": 31.0, "frozen": True, "frozen_reason": "stale"})
    raw_old = states_for(pol, NDX={"data_age_h": 62.0})
    d = iso(pol, levels={"NDX": 1.0}, states=stale)
    assert d.final_w["NDX"] == 0.0 and any("R18 frozen data" in r for r in d.hold_reasons)
    assert iso(pol, levels={"NDX": 1.0}, states=raw_old).final_w["NDX"] == pytest.approx(0.35)


def test_r18_frozen_reference_share_030(policy):
    ref = default_ref(policy)
    # SEMIS alone is 0.15 / 0.95 = 16% of the reference: only SEMIS holds
    one = run(policy, states=states_for(policy, SEMIS={"frozen": True}), ref=ref, levels=ref)
    assert one.final_w["SEMIS"] == 0.0 and one.final_w["NDX"] == pytest.approx(0.35)
    # SEMIS + SPX = 0.30 / 0.95 = 32% > 30%: no proposal at all
    frozen = {"SEMIS": {"frozen": True}, "SPX": {"frozen": True}}
    two = run(policy, states=states_for(policy, **frozen), ref=ref, levels=ref)
    assert set(two.final_w.values()) == {0.0} and row(two, "R18", "data_freshness").passed


def test_r19_market_closed_holds(policy):
    pol = loose(policy)
    closed = states_for(pol, NDX={"market_open": False})
    d = iso(pol, levels={"NDX": 1.0, "BTC": 1.0}, states=closed)
    assert d.final_w["NDX"] == 0.0 and d.final_w["BTC"] == pytest.approx(0.13)
    assert row(d, "R19").kind == "execution"


def test_r20_blockers_hold_all_but_compliance(policy):
    pol = loose(policy)
    current = {"NDX": 0.8, "SPX": 0.8, "SEMIS": 0.4, "GOLD": 0.1}
    units = {"NDX": 0.8, "SPX": 0.8, "SEMIS": 0.4, "GOLD": 0.1, "BTC": 0.13}
    d = iso(pol, levels={**{s: 1.0 for s in units}}, units=units, current=current,
            blockers=["execution_unknown"])
    assert d.final_w["BTC"] == 0.0 and d.gross <= 1.80 + 1e-9 and d.compliance
    assert row(d, "R20").passed


def test_r21_at_most_8_legs(policy):
    pol = loose(policy, proposal__max_legs=8)
    units = {s: 0.1 for s in zeros(pol)}
    d = iso(pol, levels={s: 1.0 for s in units}, units=units)
    changed = [s for s, w in d.final_w.items() if w != 0.0]
    assert len(changed) == 8 and row(d, "R21").passed
    units8 = dict(list(units.items())[:8])
    d8 = iso(pol, levels={s: 1.0 for s in units8}, units=units8)
    assert sum(1 for w in d8.final_w.values() if w != 0.0) == 8


# ----------------------------------------------------------------------------- misc


def test_unmapped_lines_are_locked_and_counted(policy):
    pol = loose(policy)
    d = iso(pol, levels={"NDX": 1.0}, current={"UNMAPPED_42": 0.1})
    assert d.final_w["UNMAPPED_42"] == pytest.approx(0.1)
    assert d.gross == pytest.approx(0.45)


def test_hedged_line_cannot_grow(policy):
    pol = loose(policy)
    d = iso(pol, levels={"NDX": 1.0}, current={"NDX": 0.1}, hedged=["NDX"])
    assert d.final_w["NDX"] == pytest.approx(0.1)


def test_unknown_line_in_levels_raises(policy):
    with pytest.raises(ValueError):
        run(policy, levels={"XYZ": 1.0})
    with pytest.raises(ValueError):
        run(policy, now=NOW.replace(tzinfo=None))


def test_default_reference_build_passes_every_check(policy):
    d = run(policy)
    assert d.passed and d.gross == pytest.approx(0.95)
    assert [c.rule_id for c in d.checks][0] == "R1" and d.checks[-1].rule_id == "MC"


def test_classify_risk_increasing():
    assert classify_risk_increasing(0.0, 0.1)          # new position
    assert classify_risk_increasing(0.1, -0.1)         # flip
    assert classify_risk_increasing(0.1, 0.2)
    assert not classify_risk_increasing(0.2, 0.1)
    assert not classify_risk_increasing(-0.2, 0.0)
    assert not classify_risk_increasing(0.2, 0.2)
    assert classify_risk_increasing(0.2, 0.1, sl_widened=True)
    with pytest.raises(ForbiddenLegError):
        classify_risk_increasing(0.2, 0.1, sl_removed=True)


def test_toward_reference():
    assert toward_reference(0.5, 1.0, 1.0) and toward_reference(1.0, 0.75, 0.5)
    assert not toward_reference(0.5, 1.2, 1.0)   # overshoots
    assert not toward_reference(0.5, 0.25, 1.0)  # away
    assert not toward_reference(0.5, 0.5, 1.0)   # no move
