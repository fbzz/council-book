"""R4 catastrophe stops: sigma_mult 3, horizon 5 days, class floors, cap 0.35, SL% of margin with
a 0.5pp buffer."""

from __future__ import annotations

import math

import pytest

from council.models.broker import LeverageConfig
from council.risk.stops import (
    catastrophe_stop_distance,
    fit_to_eligibility,
    sl_margin_pct,
    stop_at_risk,
    stop_loss_rate,
)
from tests.risk.helpers import override, state


def _line(policy, symbol):
    return policy.universe.by_symbol()[symbol]


@pytest.mark.parametrize(
    "symbol,floor",
    [("NDX", 0.08), ("SEMIS", 0.10), ("OIL", 0.10), ("GOLD", 0.08), ("BTC", 0.20),
     ("EURUSD", 0.04)],
)
def test_class_floor_binds_for_quiet_markets(policy, symbol, floor):
    line = _line(policy, symbol)
    quiet = state(symbol, line.asset_class, sigma_daily=0.001)
    assert catastrophe_stop_distance(quiet, line, policy) == pytest.approx(floor)
    # a hair above the floor, sigma takes over
    sigma_d = floor / (3 * math.sqrt(5)) * 1.01
    loud = state(symbol, line.asset_class, sigma_daily=sigma_d)
    assert catastrophe_stop_distance(loud, line, policy) == pytest.approx(floor * 1.01)


def test_sigma_mult_and_horizon_from_policy(policy):
    line = _line(policy, "NDX")
    st = state("NDX", "index", sigma_daily=0.02)
    assert catastrophe_stop_distance(st, line, policy) == pytest.approx(3 * 0.02 * math.sqrt(5))
    two = override(policy, "risk", {"catastrophe_stop.sigma_mult": 2.0})
    assert catastrophe_stop_distance(st, line, two) == pytest.approx(2 * 0.02 * math.sqrt(5))
    one_day = override(policy, "risk", {"catastrophe_stop.horizon_days": 1})
    assert catastrophe_stop_distance(st, line, one_day) == pytest.approx(0.08)  # floor again


def test_cap_035(policy):
    line = _line(policy, "BTC")
    under = state("BTC", "crypto", sigma_daily=0.35 / (3 * math.sqrt(5)) * 0.99)
    assert catastrophe_stop_distance(under, line, policy) < 0.35
    over = state("BTC", "crypto", sigma_daily=0.06)
    assert catastrophe_stop_distance(over, line, policy) == pytest.approx(0.35)


def test_sigma_ann_fallback_and_missing_vol(policy):
    line = _line(policy, "NDX")
    st = state("NDX", "index", sigma_daily=None, sigma_ann=0.20 * math.sqrt(252) / 10)
    assert catastrophe_stop_distance(st, line, policy) == pytest.approx(3 * 0.02 * math.sqrt(5))
    with pytest.raises(ValueError):
        catastrophe_stop_distance(state("NDX", "index", sigma_daily=None, sigma_ann=None),
                                  line, policy)


def _cfg(**kw):
    fields = {"settlement": "cfd", "direction": "long", "leverage_values": [1, 2],
              "min_sl_pct": 5.0, "max_sl_pct": 50.0}
    fields.update(kw)
    return LeverageConfig(**fields)


def test_sl_pct_is_percent_of_margin():
    assert sl_margin_pct(0.10, 2) == pytest.approx(20.0)
    assert fit_to_eligibility(0.10, 2, _cfg()) == pytest.approx(0.10)


def test_widen_to_minimum_plus_buffer():
    # 0.02 x 2 x 100 = 4% < 5% + 0.5pp -> widened to 5.5% / 2 / 100
    assert fit_to_eligibility(0.02, 2, _cfg()) == pytest.approx(0.0275)
    assert fit_to_eligibility(0.0275, 2, _cfg()) == pytest.approx(0.0275)  # exactly at the edge


def test_above_maximum_minus_buffer_has_no_stop():
    assert fit_to_eligibility(0.2475, 2, _cfg()) == pytest.approx(0.2475)  # 49.5% passes
    assert fit_to_eligibility(0.25, 2, _cfg()) is None  # 50% > 49.5%
    assert fit_to_eligibility(0.25, 2, _cfg(), buffer_pp=0.0) == pytest.approx(0.25)


def test_no_editable_stop_means_no_open():
    assert fit_to_eligibility(0.10, 1, _cfg(allow_edit_stop_loss=False)) is None
    assert fit_to_eligibility(0.10, 1, _cfg(allow_sl_tp=False)) is None
    assert fit_to_eligibility(0.10, 1, _cfg(min_sl_pct=49.8, max_sl_pct=50.0)) is None
    with pytest.raises(ValueError):
        fit_to_eligibility(0.0, 1, _cfg())


def test_stop_loss_rate():
    assert stop_loss_rate("long", 99.0, 100.0, 0.10) == pytest.approx(90.0)
    assert stop_loss_rate("short", 99.0, 100.0, 0.10) == pytest.approx(108.9)
    for bad in [("long", 101.0, 100.0, 0.1), ("long", 99.0, 100.0, 1.0), ("x", 99, 100, 0.1),
                ("short", 99.0, 100.0, 0.0)]:
        with pytest.raises(ValueError):
            stop_loss_rate(*bad)


def test_stop_at_risk_reporting():
    assert stop_at_risk({"NDX": 0.5, "SPX": -0.2}, {"NDX": 0.1, "SPX": 0.08}) == pytest.approx(0.066)
    assert stop_at_risk({"UNMAPPED_1": 0.1}, {}) == pytest.approx(0.1)
