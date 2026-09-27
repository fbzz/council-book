"""WP-E costs: the private fixed-fee scalar, the mirror ratio, the stock floor, the R15 split
(reference legs fee-free, discretionary legs fee-aware), backtest parity and the real-adjusted
kill switch (D19). All numbers synthetic; no network, broker or model."""

from __future__ import annotations

import math
import stat
from datetime import timedelta

import pytest
from typer.testing import CliRunner

from council.models.broker import CostQuote
from council.operator.mirror import MirrorError, load_mirror, mirror_path, resolve_ratio, set_mirror
from council.reference.backtest import cost_model, live_commission_nav
from council.risk import killswitch
from council.risk.config import cost_floors
from council.risk.costs import (
    MIRROR_RATIO_MISSING,
    discretionary_round_trip_bps,
    fee_applies,
    fee_nav_bps,
    floor_key,
    hold_days,
    round_trip_bps,
    srbe,
    trade_economics,
)
from council.risk.nav import NavState
from council.runtime import cycle_trade_economics, engine_quotes, floor_cost_quotes
from tests.risk.helpers import NOW, loose, row, run, states_for

# ----------------------------------------------------------------------------- the scalar


def test_fee_scalar_sums_the_charged_levels(policy):
    assert fee_nav_bps(policy, 10_000.0, 0.2) == pytest.approx(6.0)
    econ = trade_economics(policy, virtual_nav_usd=10_000.0, mirror_ratio=0.2)
    assert econ.real_nav_usd == pytest.approx(2_000.0) and econ.flags == ()
    assert econ.copy_floor_share == pytest.approx(3.0 / 10_000.0 / 0.2)     # 3 x the copy minimum, real
    assert econ.min_amount_usd == pytest.approx(15.0)                        # virtual amount of that floor
    assert econ.real_drag_per_leg == pytest.approx(1 / 2_000 - 1 / 10_000)


def test_fee_scalar_levels_come_from_policy(policy):
    from tests.risk.helpers import override

    real_only = override(policy, "costs", {"fixed_commission_charged_on": ["mirror"]})
    assert fee_nav_bps(real_only, 10_000.0, 0.2) == pytest.approx(5.0)
    econ = trade_economics(real_only, virtual_nav_usd=10_000.0, mirror_ratio=0.2)
    assert econ.real_drag_per_leg == pytest.approx(1 / 2_000)
    virtual_only = override(policy, "costs", {"fixed_commission_charged_on": ["virtual"]})
    assert trade_economics(virtual_only, virtual_nav_usd=10_000.0, mirror_ratio=0.2).real_drag_per_leg == 0.0
    no_fee = override(policy, "costs", {"fixed_commission_usd": {"real": 0.0, "cfd": 0.0}})
    assert fee_nav_bps(no_fee, 10_000.0, 0.2) == 0.0


def test_missing_mirror_uses_the_assumed_ratio_and_flags_it(policy, tmp_path):
    assumed = cost_floors(policy).assumed
    econ = cycle_trade_economics(policy, tmp_path, 10_000.0)
    assert econ.mirror_ratio == assumed.mirror_ratio == 0.1
    assert MIRROR_RATIO_MISSING in econ.flags
    assert econ.fee_nav_bps == pytest.approx(1e4 * (1 / 10_000 + 1 / 1_000))   # conservative: 11 bps
    set_mirror(tmp_path, ratio=0.2, now=NOW)
    econ = cycle_trade_economics(policy, tmp_path, 10_000.0)
    assert econ.mirror_ratio == 0.2 and econ.flags == () and econ.fee_nav_bps == pytest.approx(6.0)
    no_nav = cycle_trade_economics(policy, tmp_path, None)
    assert no_nav.virtual_nav_usd == assumed.virtual_nav_usd and "virtual_nav_assumed" in no_nav.flags


def test_invalid_mirror_file_falls_back_and_flags(policy, tmp_path):
    path = mirror_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "mirror_ratio": -3, "set_at": "2026-10-01T00:00:00+00:00"}')
    with pytest.raises(MirrorError):
        load_mirror(tmp_path)
    econ = cycle_trade_economics(policy, tmp_path, 10_000.0)
    assert econ.mirror_ratio == 0.1 and {"mirror_ratio_invalid", MIRROR_RATIO_MISSING} <= set(econ.flags)


def test_set_mirror_is_private_and_validated(tmp_path):
    config = set_mirror(tmp_path, funding_usd=2_000.0, virtual_nav_usd=10_000.0, now=NOW)
    assert config.mirror_ratio == pytest.approx(0.2)
    path = mirror_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert load_mirror(tmp_path).mirror_ratio == pytest.approx(0.2)
    for bad in ({"ratio": 0.0}, {"ratio": math.nan}, {"ratio": 11.0}, {"funding_usd": 2_000.0},
                {"ratio": 0.3, "funding_usd": 2_000.0, "virtual_nav_usd": 10_000.0}, {}):
        with pytest.raises(MirrorError):
            resolve_ratio(**bad)


def test_set_mirror_command_is_operator_only(monkeypatch, tmp_path):
    from council.cli import app

    runner = CliRunner()
    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    refused = runner.invoke(app, ["account", "set-mirror", "--ratio", "0.2"])
    assert refused.exit_code == 2 and not mirror_path(tmp_path / "state").exists()
    from tests.cli.operator_sim import simulate_operator

    simulate_operator(monkeypatch)       # the operator terminal (M5-B guard), not just COUNCIL_ROLE
    done = runner.invoke(app, ["account", "set-mirror", "--ratio", "0.2"])
    assert done.exit_code == 0, done.output
    assert load_mirror(tmp_path / "state").mirror_ratio == pytest.approx(0.2)
    bad = runner.invoke(app, ["account", "set-mirror", "--ratio", "-1"])
    assert bad.exit_code == 2


# ----------------------------------------------------------------------------- floors and quotes


def test_stock_real_floor_and_fee_scope(policy, sleeve_policy):
    assert floor_key("real", "stock") == "stock_real" and floor_key("real", "etf") == "etf_real"
    assert cost_floors(policy).per_side_bps["stock_real"] == 10
    assert fee_applies("real", "etf") and fee_applies("real", "stock")
    assert not fee_applies("real", "crypto") and not fee_applies("cfd", "index")
    quotes = floor_cost_quotes(sleeve_policy, quoted_at=NOW, fee_bps=6.0)
    stock = [k for k in quotes if k[0] in ("TSTA", "TSTC_B", "F", "TSTD")]
    assert sorted({(d, lev) for _, d, lev in stock}) == [("long", 1)]      # real long 1x only
    assert quotes[("TSTA", "long", 1)].per_side_bps == pytest.approx(20.0)  # 10 + 10 slippage
    assert quotes[("TSTA", "long", 1)].fixed_fee_nav_bps == 6.0
    assert quotes[("NDX", "long", 1)].fixed_fee_nav_bps == 6.0             # real UCITS carries the fee
    assert quotes[("NDX", "long", 2)].fixed_fee_nav_bps == 0.0             # levered = CFD
    assert quotes[("NDX", "short", 1)].fixed_fee_nav_bps == 0.0
    assert quotes[("BTC", "long", 1)].fixed_fee_nav_bps == 0.0             # real crypto: % fee only
    assert quotes[("OIL", "long", 1)].fixed_fee_nav_bps == 0.0             # CFD


def test_the_fee_never_becomes_a_cost_fact(sleeve_policy):
    from council.facts.pack import cost_facts_from_quotes

    quotes = floor_cost_quotes(sleeve_policy, quoted_at=NOW, fee_bps=6.0)
    per_line = {line: q for (line, d, lev), q in quotes.items() if d == "long" and lev == 1}
    facts = cost_facts_from_quotes(per_line, slot=NOW)
    assert facts and all(not f.id.endswith("fee_nav_bps") for f in facts)
    assert {f.id.rsplit(":", 1)[1] for f in facts} == {"per_side_bps", "carry_bps_day"}


# ----------------------------------------------------------------------------- R15 split


@pytest.mark.parametrize("line,cls", [("TSTA", "stock"), ("NDX", "etf")])
def test_r15_reference_legs_are_fee_free_and_pass_from_6pct_vol(sleeve_policy, line, cls):
    q = floor_cost_quotes(sleeve_policy, quoted_at=NOW, fee_bps=11.0)[(line, "long", 1)]
    assert q.fixed_fee_nav_bps == 11.0
    value = srbe(round_trip_bps(q), 0.0, 0.06, hold_days(cls, sleeve_policy, toward_reference=True))
    assert value <= 0.30
    assert hold_days("stock", sleeve_policy, toward_reference=True) == 91


def test_r15_reference_leg_in_the_engine_ignores_the_fee(sleeve_policy):
    pol = loose(sleeve_policy)
    quotes = engine_quotes(floor_cost_quotes(pol, quoted_at=NOW, fee_bps=11.0))
    ref = {ln.symbol: 0.0 for ln in pol.universe.lines}
    ref["TSTA"] = 1.0
    units = {ln.symbol: ln.base_weight for ln in pol.universe.lines}
    states = states_for(pol)
    states["TSTA"] = states["TSTA"].model_copy(update={"sigma_ann": 0.06, "sigma_daily": 0.06 / 252 ** 0.5})
    d = run(pol, levels=dict(ref), ref=ref, unit_weights=units, states=states, cost_quotes=quotes,
            bands=_wide(pol))
    assert d.final_w["TSTA"] == pytest.approx(units["TSTA"])
    r15 = row(d, "R15")
    assert r15.passed and isinstance(r15.value, float) and r15.value <= 0.30   # a number: no fee in it


def test_discretionary_r15_depends_on_the_leg_size(policy):
    q = CostQuote(symbol="GOLD", direction="long", settlement="real", leverage=1, per_side_bps=15.0,
                  what_if_bps=None, carry_bps_day=0.0, quoted_at=NOW, fixed_fee_nav_bps=6.0)
    small, big = discretionary_round_trip_bps(q, 0.02), discretionary_round_trip_bps(q, 0.12)
    assert small == pytest.approx(30.0 + 2 * 6.0 / 0.02) and big == pytest.approx(30.0 + 2 * 6.0 / 0.12)
    assert discretionary_round_trip_bps(q.model_copy(update={"fixed_fee_nav_bps": 0.0}), 0.02) == 30.0
    assert discretionary_round_trip_bps(q, 0.0) == math.inf
    # in the engine: the same council add on GOLD passes at a large size and fails at a small one
    # (20-day council hold, sigma 60%: 30 bps + 2 x 6 / 0.45 passes 0.20, 30 + 2 x 6 / 0.05 does not)
    pol = loose(policy)
    quotes = engine_quotes(floor_cost_quotes(pol, quoted_at=NOW, fee_bps=6.0))
    ref = {ln.symbol: 0.0 for ln in pol.universe.lines}
    units = {ln.symbol: 0.1 for ln in pol.universe.lines}
    units["GOLD"] = 0.45
    states = states_for(pol, GOLD={"sigma_ann": 0.60})
    big_add = run(pol, levels={**ref, "GOLD": 1.0}, ref=ref, unit_weights=units, states=states,
                  cost_quotes=quotes, bands=_wide(pol))
    assert big_add.final_w["GOLD"] == pytest.approx(0.45)
    assert row(big_add, "R15").value == "R15_fee"                          # value withheld (D18)
    assert row(big_add, "R14", "cycle_cost_bps").value == "R14_fee"
    small_units = {**units, "GOLD": 0.05}
    small_add = run(pol, levels={**ref, "GOLD": 1.0}, ref=ref, unit_weights=small_units, states=states,
                    cost_quotes=quotes, bands=_wide(pol))
    assert small_add.final_w["GOLD"] == 0.0
    reason = next(r for r in small_add.hold_reasons if r.startswith("GOLD"))
    assert "R15_fee" in reason and not any(ch.isdigit() for ch in reason.split("R15_fee", 1)[1])
    no_fee = run(pol, levels={**ref, "GOLD": 1.0}, ref=ref, unit_weights=small_units, states=states,
                 cost_quotes=engine_quotes(floor_cost_quotes(pol, quoted_at=NOW)), bands=_wide(pol))
    assert no_fee.final_w["GOLD"] == pytest.approx(0.05)
    assert isinstance(row(no_fee, "R15").value, float)                     # no fee: the value is public


# ----------------------------------------------------------------------------- fee vs de-risking
_UCITS = ("NDX", "SEMIS", "SPX", "GOLD")      # real UCITS/ETC lines: every leg pays the fixed fee


def _cut_setup(pol, fee_bps: float = 11.0, **ref_over: float):
    """Every line at its reference level (1.0 in-reference, 0 overlay) unless overridden."""
    quotes = engine_quotes(floor_cost_quotes(pol, quoted_at=NOW, fee_bps=fee_bps))
    units = {ln.symbol: ln.base_weight for ln in pol.universe.lines}
    ref = {ln.symbol: (1.0 if ln.in_reference else 0.0) for ln in pol.universe.lines}
    ref.update(ref_over)
    current = {s: ref[s] * units[s] for s in ref if ref[s]}
    return quotes, units, ref, current


def test_the_fee_never_gates_a_risk_reducing_council_cut_in_r15(policy):
    """A council cut away from the reference (discretionary, risk-reducing) on a real UCITS line is
    priced on its variable round trip, as before the fee existed: the fee must never keep the book
    from de-risking. Priced with the fee paid twice it would fail."""
    pol = loose(policy)
    quotes, units, ref, current = _cut_setup(pol)
    q = quotes[("GOLD", "long", 1)]
    dw = 0.25 * units["GOLD"]
    council_hold = hold_days("etf", pol, toward_reference=False)
    assert srbe(round_trip_bps(q), 0.0, 0.40, council_hold) <= 0.20            # passes on variable costs
    assert srbe(discretionary_round_trip_bps(q, dw), 0.0, 0.40, council_hold) > 0.20   # a fee gate fails it
    d = run(pol, levels={**ref, "GOLD": 0.75}, ref=ref, unit_weights=units, current=current,
            states=states_for(pol, GOLD={"sigma_ann": 0.40}), cost_quotes=quotes, bands=_wide(pol))
    assert d.final_w["GOLD"] == pytest.approx(0.75 * units["GOLD"])
    r15 = row(d, "R15")
    assert r15.passed and isinstance(r15.value, float)                     # no fee priced: a number
    assert d.passed


def test_the_fee_never_holds_a_risk_reducing_leg_in_r14(policy):
    """Four council cuts on real UCITS lines whose fees alone exceed R14's 40 bps cycle cap all go;
    their fees still count, so the risk-increasing council add is held to make room."""
    pol = loose(policy, cost_budget__cycle_max_bps=40.0)
    quotes, units, ref, current = _cut_setup(pol, fee_bps=11.0, BTC=0.5)
    levels = {**ref, **dict.fromkeys(_UCITS, 0.5), "BTC": 1.0}             # cuts away from ref + a BTC add
    volatile = states_for(pol, BTC={"sigma_ann": 0.90},                     # R15 passes on variable costs
                          **{s: {"sigma_ann": 0.60} for s in _UCITS})
    d = run(pol, levels=levels, ref=ref, unit_weights=units, current=current, states=volatile,
            cost_quotes=quotes, bands=_wide(pol))
    for s in _UCITS:
        assert d.final_w[s] == pytest.approx(0.5 * units[s]), s            # 4 x 11 bps > 40: all executed
    assert d.final_w["BTC"] == pytest.approx(current["BTC"])               # the add is held first
    assert any(r.startswith("BTC: R14 cycle cost budget") for r in d.hold_reasons)
    cycle = row(d, "R14", "cycle_cost_bps")
    assert cycle.passed and cycle.value == "R14_fee"
    assert d.passed
    # the 30-day budget: prior fees used it up, prior VARIABLE costs did not -> the cuts still go
    month_pol = loose(policy, cost_budget__discretionary_30d_max_bps=100.0)
    cuts = {**ref, **dict.fromkeys(_UCITS, 0.5)}
    month = run(month_pol, levels=cuts, ref=ref, unit_weights=units, current=current,
                states=volatile, cost_quotes=quotes, bands=_wide(pol),
                cost_30d_bps=95.0, cost_30d_fee_bps=60.0)
    for s in _UCITS:
        assert month.final_w[s] == pytest.approx(0.5 * units[s]), s
    r14m = row(month, "R14", "cost_30d_bps")
    assert r14m.passed and r14m.value == "R14_fee" and month.passed
    # ...while a risk-increasing discretionary leg is still held by the fees already spent
    add = run(month_pol, levels={**ref, "BTC": 1.0}, ref=ref, unit_weights=units, current=current,
              states=volatile, cost_quotes=quotes, bands=_wide(pol), cost_30d_bps=95.0, cost_30d_fee_bps=60.0)
    assert add.final_w["BTC"] == pytest.approx(current["BTC"])
    assert any(r.startswith("BTC: R14 30-day cost budget") for r in add.hold_reasons)
    assert add.passed


def _wide(pol, lo=-1.5, hi=1.5):
    from council.models.risk import Band

    return {ln.symbol: Band(symbol=ln.symbol, trend="mixed", ref_level=0.0, lo=lo, hi=hi)
            for ln in pol.universe.lines}


# ----------------------------------------------------------------------------- backtest parity


def test_backtest_charges_stock_real_and_the_live_fee(sleeve_policy):
    stock = sleeve_policy.universe.by_symbol()["TSTA"]
    ndx = sleeve_policy.universe.by_symbol()["NDX"]
    nav = live_commission_nav(sleeve_policy, virtual_nav_usd=10_000.0, mirror_ratio=0.2)
    assert nav == pytest.approx(1 / (1 / 10_000 + 1 / 2_000))
    costs = cost_model([stock, ndx], sleeve_policy, mode="listed", commission_nav=nav)
    assert costs.classes == {"TSTA": "stock_real", "NDX": "etf_real"}
    assert costs.per_side["TSTA"] == pytest.approx(10 / 1e4)
    live = fee_nav_bps(sleeve_policy, 10_000.0, 0.2) / 1e4
    assert costs.fixed["TSTA"] == pytest.approx(live) and costs.fixed["NDX"] == pytest.approx(live)
    cfd = cost_model([stock], sleeve_policy, mode="cfd", commission_nav=nav)
    assert cfd.classes["TSTA"] == "etf_cfd" and cfd.fixed["TSTA"] == 0.0


# ----------------------------------------------------------------------------- kill switch (D19)


def test_real_adjusted_halt_fires_earlier_than_virtual(policy):
    nav = NavState(first_equity=10_000.0, peak=10_000.0, last=10_000.0, updated_at=NOW - timedelta(hours=1))
    reads = [(NOW - timedelta(minutes=5), 7_700.0), (NOW, 7_700.0)]    # 77% of peak: above the halt
    virtual = killswitch.evaluate(nav=nav, equity_reads=reads, prev_state="NORMAL",
                                  has_positions=True, policy=policy)
    assert virtual.state == "WARN"
    real = killswitch.evaluate(nav=nav, equity_reads=reads, prev_state="NORMAL", has_positions=True,
                               policy=policy, real_drag=0.03)          # 77% x 0.97 = 74.7% < 75%
    assert real.state == "HALTED" and real.drawdown > virtual.drawdown
    assert "real-adjusted" in real.reason
    calm = [(NOW - timedelta(minutes=5), 9_000.0), (NOW, 9_000.0)]
    assert killswitch.evaluate(nav=nav, equity_reads=calm, prev_state="NORMAL", has_positions=True,
                               policy=policy, real_drag=0.03).state == "NORMAL"
    warn = killswitch.evaluate(nav=nav, equity_reads=[(NOW, 8_100.0)], prev_state="NORMAL",
                               has_positions=True, policy=policy, real_drag=0.02)
    assert warn.state == "WARN"                                        # 81% x 0.98 = 79.4% < 80%


def test_accumulated_fee_drag_is_measured_from_the_real_adjusted_peak(policy):
    """Years of fee drag must not become a permanent drawdown: the real-adjusted equity is measured
    from its own lifetime peak (stored by the cycle), so a book at a new high stays NORMAL, while a
    fall from the adjusted peak still trips the switch as the virtual rule would."""
    nav = NavState(first_equity=10_000.0, peak=20_000.0, last=20_000.0, updated_at=NOW - timedelta(hours=1))
    at_high = [(NOW, 20_000.0)]
    # 22% cumulative drag, adjusted peak stored along the way (virtual 20k x 0.78)
    kd = killswitch.evaluate(nav=nav, equity_reads=at_high, prev_state="NORMAL", has_positions=True,
                             policy=policy, real_drag=0.22, real_peak=15_600.0)
    assert kd.state == "NORMAL" and kd.drawdown == pytest.approx(0.0)
    assert kd.real_peak == pytest.approx(15_600.0)
    # a 21% fall of the virtual book from there: WARN on both series, as before the fee existed
    fall = [(NOW, 15_800.0)]
    assert killswitch.evaluate(nav=nav, equity_reads=fall, prev_state="NORMAL", has_positions=True,
                               policy=policy, real_drag=0.22, real_peak=15_600.0).state == "WARN"
    # the adjusted peak only rises: a new virtual high after more drag lifts it
    higher = killswitch.evaluate(nav=nav.model_copy(update={"peak": 21_000.0}), equity_reads=[(NOW, 21_000.0)],
                                 prev_state="NORMAL", has_positions=True, policy=policy, real_drag=0.22,
                                 real_peak=15_600.0)
    assert higher.real_peak == pytest.approx(21_000.0 * 0.78) and higher.state == "NORMAL"
    # a stored peak above the virtual one is impossible and is clamped (never a laxer switch)
    clamped = killswitch.evaluate(nav=nav, equity_reads=at_high, prev_state="NORMAL", has_positions=True,
                                  policy=policy, real_drag=0.0, real_peak=99_000.0)
    assert clamped.real_peak == pytest.approx(20_000.0) and clamped.state == "NORMAL"
    # extra drag since the adjusted peak deepens the adjusted drawdown: it trips before the virtual one
    early = killswitch.evaluate(nav=nav, equity_reads=[(NOW, 16_600.0)], prev_state="NORMAL",
                                has_positions=True, policy=policy, real_drag=0.05, real_peak=20_000.0)
    virtual = killswitch.evaluate(nav=nav, equity_reads=[(NOW, 16_600.0)], prev_state="NORMAL",
                                  has_positions=True, policy=policy)
    assert virtual.state == "NORMAL" and early.state == "WARN"         # 83% vs 83% x 0.95 = 78.9%
    assert "real-adjusted" in early.reason
