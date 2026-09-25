"""Costs: per-side floors, commission, slippage, carry floors and the R15 SR_be gate.
All USD amounts are synthetic what-if payloads (never published)."""

from __future__ import annotations

import pytest

from council.risk.costs import (
    carry_bps_day,
    commission_bps,
    cost_quote_from_whatif,
    floor_key,
    gate_threshold,
    hold_days,
    passes_cost_gate,
    per_side_bps,
    srbe,
)
from tests.risk.helpers import override, quote

FLOORS = [
    ("real", "crypto", "crypto", 100.0),
    ("cfd", "crypto", "crypto", 100.0),
    ("real", "etf", "etf_real", 5.0),
    ("real", "index", "etf_real", 5.0),   # a UCITS ETF on an index line
    ("cfd", "etf", "etf_cfd", 15.0),
    ("cfd", "stock", "etf_cfd", 15.0),
    ("cfd", "index", "index_cfd", 5.0),
    ("cfd", "commodity", "commodity_cfd", 8.0),
    ("cfd", "fx", "fx_cfd", 3.0),
]


@pytest.mark.parametrize("settlement,cls,key,floor", FLOORS)
def test_per_side_floor_binds_when_whatif_is_lower(policy, settlement, cls, key, floor):
    assert floor_key(settlement, cls) == key
    assert per_side_bps(settlement, cls, 0.0, None, policy) == pytest.approx(floor + 10.0)
    assert per_side_bps(settlement, cls, floor - 0.5, None, policy) == pytest.approx(floor + 10.0)


@pytest.mark.parametrize("settlement,cls,key,floor", FLOORS)
def test_whatif_wins_when_above_floor(policy, settlement, cls, key, floor):
    assert per_side_bps(settlement, cls, floor + 7.0, None, policy) == pytest.approx(floor + 17.0)


def test_half_spread_wins_and_slippage_from_policy(policy):
    assert per_side_bps("cfd", "fx", 1.0, 9.0, policy) == pytest.approx(19.0)
    no_slip = override(policy, "costs", {"slippage_buffer_bps": 0.0})
    assert per_side_bps("cfd", "fx", 1.0, 9.0, no_slip) == pytest.approx(9.0)


def test_commission_real_only(policy):
    assert commission_bps("real", 1000.0, policy) == pytest.approx(10.0)
    assert commission_bps("cfd", 1000.0, policy) == 0.0
    assert commission_bps("real", 1000.0, policy, asset_class="crypto") == 0.0
    with pytest.raises(ValueError):
        commission_bps("real", 0.0, policy)
    assert per_side_bps("real", "etf", 0.0, None, policy, commission=10.0) == pytest.approx(25.0)


YEAR = 365.0
CARRY = [
    ("long", "real", 1, "index", 0.0),
    ("long", "real", 1, "crypto", 0.0),
    ("long", "cfd", 1, "etf", 0.0),
    ("long", "cfd", 1, "stock", 0.0),
    ("long", "cfd", 2, "etf", (0.043 + 0.064) / YEAR * 1e4),
    ("short", "cfd", 1, "etf", 0.029 / YEAR * 1e4),
    ("long", "cfd", 1, "index", 0.035 / YEAR * 1e4),
    ("short", "cfd", 1, "fx", 0.035 / YEAR * 1e4),
    ("long", "cfd", 2, "commodity", (0.043 + 0.064) / YEAR * 1e4),
    ("long", "cfd", 1, "crypto", 0.18 / YEAR * 1e4),
    ("short", "cfd", 1, "crypto", 0.18 / YEAR * 1e4),
]


@pytest.mark.parametrize("direction,settlement,lev,cls,expected", CARRY)
def test_carry_floors(policy, direction, settlement, lev, cls, expected):
    assert carry_bps_day(direction, settlement, lev, cls, None, policy) == pytest.approx(expected)


def test_carry_whatif_wins_when_higher_and_is_never_income(policy):
    floor = 0.035 / YEAR * 1e4
    assert carry_bps_day("long", "cfd", 1, "index", 5.0, policy) == pytest.approx(5.0)
    assert carry_bps_day("long", "cfd", 1, "index", 0.1, policy) == pytest.approx(floor)
    assert carry_bps_day("long", "real", 1, "etf", -3.0, policy) == 0.0
    with pytest.raises(ValueError):
        carry_bps_day("sideways", "cfd", 1, "etf", None, policy)


def test_carry_rates_come_from_policy(policy):
    zero = override(policy, "costs", {"overnight_annual.crypto_cfd": 0.0})
    assert carry_bps_day("short", "cfd", 1, "crypto", None, zero) == pytest.approx(0.029 / YEAR * 1e4)


def test_srbe_units():
    # 100 bps round trip, no carry, 10% vol, held one year -> Sharpe 0.1 just to pay costs
    assert srbe(100.0, 0.0, 0.10, 365.0) == pytest.approx(0.1)
    # plus 1 bp/day carry for a year: (1% + 3.65%) / 10%
    assert srbe(100.0, 1.0, 0.10, 365.0) == pytest.approx(0.465)
    # shorter holds amortise the round trip over less time
    assert srbe(100.0, 0.0, 0.10, 36.5) == pytest.approx(1.0)
    # BTC at the 100 bps floor + 10 slippage per side, sigma 55%, 60-day hold
    assert srbe(220.0, 0.0, 0.55, 60.0) == pytest.approx(0.022 / (0.55 * 60 / 365))
    with pytest.raises(ValueError):
        srbe(10.0, 0.0, 0.0, 20.0)
    with pytest.raises(ValueError):
        srbe(10.0, 0.0, 0.2, 0.0)


def test_hold_days_60_crypto_20_default(policy):
    assert hold_days("crypto", policy) == 60
    assert hold_days("index", policy) == 20 and hold_days("fx", policy) == 20


def _gate_quote(per_side):
    return quote("NDX", per_side=per_side)


def test_reference_threshold_030(policy):
    # toward the reference: sigma 0.365 and the 90-day reference hold -> denominator 0.09;
    # 135 bps/side -> SR_be 0.30
    ok, value, limit = passes_cost_gate(_gate_quote(135.0), 0.365, "index", policy,
                                        toward_reference=True)
    assert ok and value == pytest.approx(0.30) and limit == 0.30
    ok, value, _ = passes_cost_gate(_gate_quote(135.1), 0.365, "index", policy,
                                    toward_reference=True)
    assert not ok and value > 0.30


def test_council_threshold_020(policy):
    assert passes_cost_gate(_gate_quote(20.0), 0.365, "index", policy, toward_reference=False)[0]
    assert not passes_cost_gate(_gate_quote(20.1), 0.365, "index", policy,
                                toward_reference=False)[0]
    assert gate_threshold(False, policy) == 0.20 and gate_threshold(True, policy) == 0.30


def test_crypto_hold_is_longer(policy):
    # the same quote passes the council gate as crypto (60d) but not as an index (20d)
    q = quote("BTC", per_side=40.0)
    assert passes_cost_gate(q, 0.365, "crypto", policy, toward_reference=False)[0]
    assert not passes_cost_gate(q, 0.365, "index", policy, toward_reference=False)[0]


def test_carry_counts_in_the_gate(policy):
    q = quote("NDX", per_side=10.0, carry=3.0)
    with_carry = passes_cost_gate(q, 0.20, "index", policy, toward_reference=True)
    without = passes_cost_gate(q, 0.20, "index", policy, toward_reference=True, include_carry=False)
    assert with_carry[1] > without[1]


WHATIF = {
    "instrumentId": 101, "symbol": "SPX500",
    "costs": [
        {"costType": "markup", "amount": 0.15, "currency": "USD"},
        {"costType": "marketSpread", "amount": 0.03, "currency": "USD"},
        {"costType": "transactionFee", "amount": 1.0, "currency": "USD"},
        {"costType": "overnightFee", "amount": 0.25, "currency": "USD"},
        {"costType": "overWeekendFee", "amount": 0.75, "currency": "USD"},
        {"costType": "sdrt", "amount": 0.5, "currency": "USD"},
    ],
    "lastUpdated": "2026-05-25T08:30:00Z",
}


def test_cost_quote_from_whatif(policy, now):
    q = cost_quote_from_whatif(WHATIF, 1000.0, direction="long", settlement="cfd", leverage=2,
                               asset_class="index", policy=policy, quoted_at=now)
    # exposure 2000: per-trade 1.68 USD = 8.4 bps > 5 bps floor
    assert q.what_if_bps == pytest.approx(8.4)
    assert q.per_side_bps == pytest.approx(18.4) and not q.floor_applied
    assert q.carry_bps_day == pytest.approx((0.043 + 0.064) / YEAR * 1e4)  # floor > 1.25 what-if
    assert q.weekend_multiplier == 3 and q.symbol == "SPX500" and q.quoted_at == now


def test_zero_whatif_hits_the_crypto_floor(policy, now):
    zeros = {"symbol": "BTC", "costs": [{"costType": "markup", "amount": 0.0}]}
    q = cost_quote_from_whatif(zeros, 500.0, direction="long", settlement="real", leverage=1,
                               asset_class="crypto", policy=policy, quoted_at=now)
    assert q.what_if_bps == 0.0 and q.per_side_bps == pytest.approx(110.0) and q.floor_applied
    assert q.carry_bps_day == 0.0


def test_real_commission_uses_the_mirrored_amount(policy, now):
    payload = {"symbol": "CSPX.L", "costs": []}
    q = cost_quote_from_whatif(payload, 1000.0, direction="long", settlement="real", leverage=1,
                               asset_class="etf", policy=policy, quoted_at=now,
                               real_amount_usd=50.0)
    assert q.per_side_bps == pytest.approx(5.0 + 200.0 + 10.0)


def test_missing_cost_list_and_bad_currency(policy, now):
    q = cost_quote_from_whatif({"symbol": "OIL"}, 100.0, direction="short", settlement="cfd",
                               leverage=1, asset_class="commodity", policy=policy, quoted_at=now)
    assert q.what_if_bps is None and q.per_side_bps == pytest.approx(18.0)
    eur = {"symbol": "OIL", "costs": [{"costType": "markup", "amount": 1.0, "currency": "EUR"}]}
    with pytest.raises(ValueError):
        cost_quote_from_whatif(eur, 100.0, direction="long", settlement="cfd", leverage=1,
                               asset_class="commodity", policy=policy, quoted_at=now)
    with pytest.raises(ValueError):
        cost_quote_from_whatif({"costs": []}, 100.0, direction="long", settlement="cfd",
                               leverage=1, asset_class="commodity", policy=policy, quoted_at=now)
