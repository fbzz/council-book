"""The strict single-stock onboarding gate (design §4.3): exactly one row, open/close/partial close,
a real long 1x config that allows setting and editing a stop, SL bounds covering [floor, cap],
W-8BEN, units, a quote, the whole-unit price; every missing field fails closed; reasons are codes
without numbers; failing names are replaced within their sector; unchecked lines are refused by the
live preflight. The broker is reached only through the READ client over the FakeEtoro transport."""

from __future__ import annotations

import re

import pytest

from council.broker.fake import leverage_config
from council.stocks import eligibility as gate
from tests.stocks import cli_support as cs

NOW = cs.NOW


@pytest.fixture
def cfg(policy):
    return gate.gate_config(policy, unit_share=0.0625, virtual_nav_usd=12_000.0)


def verdict(cfg, closing: bool = False, price: float = 100.0, **overrides):
    b = cs.broker()
    b.add("TSTA", price=price, **overrides)
    out = gate.check_symbols(b.read, ["TSTA"], cfg, now=NOW, closing_only=["TSTA"] if closing else ())
    assert b.eligibility_posts() == 1 and b.writes() == 0
    return out["TSTA"]


def real_long(**kw):
    base = {"settlement": "REAL", "direction": "LONG", "leverage_values": [1], "min_sl_pct": 0.0, "max_sl_pct": 100.0}
    return leverage_config(**{**base, **kw})


def test_gate_config_hides_the_private_notional(policy, cfg):
    assert (cfg.stop_floor, cfg.stop_cap, cfg.sl_buffer_pp) == (0.15, 0.35, 0.5)
    assert cfg.target_notional_usd == pytest.approx(750.0)
    assert "750" not in repr(cfg) and "target_notional" not in repr(cfg)
    assumed = gate.gate_config(policy, unit_share=0.0625)              # no snapshot: the assumed NAV
    assert assumed.target_notional_usd == pytest.approx(0.0625 * policy.costs["assumed"]["virtual_nav_usd"])
    with pytest.raises(ValueError):
        gate.gate_config(policy, unit_share=0.0)


def test_a_clean_us_stock_passes_with_the_returned_symbol_and_id(cfg):
    v = verdict(cfg)
    assert v.ok and v.reason == "ok" and v.symbol == "TSTA" and v.instrument_id and v.checked_at == NOW
    assert not v.whole_units


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"allowOpenPosition": False}, "open_not_allowed"),
    ({"allowOpenPosition": None}, "open_not_allowed"),
    ({"allowClosePosition": False}, "close_not_allowed"),
    ({"allowPartialClosePosition": False}, "partial_close_not_allowed"),
    ({"requiresW8Ben": True}, "requires_w8ben"),
    ({"requiresW8Ben": "unknown"}, "requires_w8ben"),
    ({"allowedOrderQuantityType": "Amount"}, "units_not_allowed"),
    ({"allowedOrderQuantityType": None}, "units_not_allowed"),
    ({"tradeUnitType": "Contracts"}, "trade_unit_type"),
    ({"tradeUnitType": None}, "trade_unit_type"),
    ({"leverageConfigs": [leverage_config(settlement="CFD", direction="LONG", leverage_values=[1])]},
     "no_real_long_1x"),
    ({"leverageConfigs": [real_long(leverage_values=[2])]}, "no_real_long_1x"),
    ({"leverageConfigs": [real_long(direction="SHORT")]}, "no_real_long_1x"),
    ({"leverageConfigs": [real_long(is_potential=True)]}, "no_real_long_1x"),
    ({"leverageConfigs": [real_long(allow_sl_tp=False)]}, "no_real_long_1x"),
    ({"leverageConfigs": [real_long(allow_edit_stop_loss=False)]}, "no_real_long_1x"),
    ({"leverageConfigs": []}, "no_real_long_1x"),
    ({"leverageConfigs": [real_long(min_sl_pct=20.0)]}, "sl_bounds"),       # the 15% floor would be refused
    ({"leverageConfigs": [real_long(max_sl_pct=30.0)]}, "sl_bounds"),       # the 35% cap would be refused
    ({"leverageConfigs": [real_long(min_sl_pct=14.8)]}, "sl_bounds"),       # inside the 0.5 pp buffer
    ({"symbol": "tsta"}, "bad_symbol"),                                     # not recordable as returned
])
def test_every_condition_fails_closed_with_its_code(cfg, overrides, reason):
    v = verdict(cfg, **overrides)
    assert not v.ok and reason in v.reasons, v.reasons


def test_a_missing_stop_field_in_the_real_config_fails_closed(cfg):
    config = real_long()
    del config["allowEditStopLoss"]
    assert "no_real_long_1x" in verdict(cfg, leverageConfigs=[config]).reasons
    config = real_long()
    del config["maxStopLossPercentage"]
    assert "sl_bounds" in verdict(cfg, leverageConfigs=[config]).reasons


def test_sl_bounds_exactly_at_the_buffer_pass(cfg):
    assert verdict(cfg, leverageConfigs=[real_long(min_sl_pct=14.5, max_sl_pct=35.5)]).ok


def test_no_quote_and_the_whole_unit_price(cfg):
    assert verdict(cfg, price=0.0).reasons == ("no_quote",)
    whole = {"unitsQuantityType": "WholeUnits"}
    assert verdict(cfg, price=370.0, **whole).ok                           # <= half of the unit's notional
    v = verdict(cfg, price=380.0, **whole)
    assert v.reasons == ("whole_unit_price",) and v.whole_units
    assert verdict(cfg, price=5_000.0).ok                                  # fractional shares: price is no gate


def test_an_absent_quantity_type_counts_as_whole_units(cfg):
    # the core parser defaults an absent unitsQuantityType to "fractional"; the stock gate must not
    assert verdict(cfg, price=380.0, unitsQuantityType="FractionalUnits").ok
    for missing in (None, "", "Unknown"):
        v = verdict(cfg, price=380.0, unitsQuantityType=missing)
        assert v.reasons == ("whole_unit_price",) and v.whole_units, (missing, v.reasons)
    assert verdict(cfg, price=370.0, unitsQuantityType=None).ok             # a cheap share still passes


@pytest.mark.parametrize("raw", [None, "true", 1])
def test_the_close_permission_must_be_an_explicit_true(cfg, raw):
    # the core parser reads an absent allowClosePosition as True; the stock gate fails it closed
    assert "close_not_allowed" in verdict(cfg, allowClosePosition=raw).reasons
    assert verdict(cfg, closing=True, allowClosePosition=raw).reasons == ("close_not_allowed",)


def test_reasons_are_codes_never_numbers(cfg):
    v = verdict(cfg, price=400.0, unitsQuantityType="WholeUnits", requiresW8Ben=True,
                leverageConfigs=[real_long(min_sl_pct=20.0)])
    assert set(v.reasons) <= set(gate.REASONS) and not re.search(r"\d", v.reason.replace("w8ben", ""))


def test_a_closing_only_gate_asks_only_for_the_close(cfg):
    closed = {"allowOpenPosition": False, "leverageConfigs": [], "requiresW8Ben": True}
    assert verdict(cfg, closing=True, **closed).ok
    assert verdict(cfg, closing=True, allowClosePosition=False).reasons == ("close_not_allowed",)


def test_one_request_for_many_symbols_and_not_found(cfg):
    b = cs.broker(["AAA", "BBB", "CCC"])
    out = gate.check_symbols(b.read, ["AAA", "BBB", "ZZZ", "AAA"], cfg, now=NOW)
    assert list(out) == ["AAA", "BBB", "ZZZ"] and out["AAA"].ok and out["ZZZ"].reasons == ("not_found",)
    assert b.eligibility_posts() == 1 and b.fake.count("GET", "/api/v2/market-data/rates") == 1


def test_an_ambiguous_symbol_fails(cfg):
    from council.broker.etoro_read import EtoroReadClient
    from tests.stocks.test_broker_stock_terms import DuplicatingEtoro

    fake = DuplicatingEtoro({"DUP": 902}, clock=lambda: NOW)
    fake.add_instrument("DUP", 901, bid=10.0, ask=10.01, row=cs.stock_row("DUP", 901))
    read = EtoroReadClient("test-app-key", "test-read-key", transport=fake.transport(), sleep=lambda _s: None)
    assert gate.check_symbols(read, ["DUP"], cfg, now=NOW)["DUP"].reasons == ("ambiguous",)


def test_the_instrument_gate_needs_exactly_one_row_with_that_id(cfg):
    b = cs.broker()
    iid = b.add("TSTA", allowOpenPosition=False)
    found = gate.instrument_verdicts(b.read, iid, cfg, now=NOW)
    assert found.row.symbol == "TSTA" and not found.full.ok and found.closing.ok
    missing = gate.instrument_verdicts(b.read, 999_999, cfg, now=NOW)
    assert missing.row is None and missing.full.reasons == ("not_found",) == missing.closing.reasons
    v, row = gate.check_instrument(b.read, iid, cfg, now=NOW, closing_only=True)
    assert v.ok and row.instrument_id == iid


# ------------------------------------------------------------------------------------ replacements


def test_failures_are_replaced_by_the_next_eligible_name_of_their_sector():
    order = ["A1", "B1", "A2", "B2", "A3", "C1"]
    sector = {"A1": "A", "A2": "A", "A3": "A", "B1": "B", "B2": "B", "C1": "C"}
    ok = {"A1": False, "B1": True, "A2": False, "B2": True, "A3": True, "C1": True}.get
    final, replaced = gate.replace_failures(["A1", "B1"], order, sector, ok)
    assert final == ["A3", "B1"] and replaced == [("A1", "A3")]
    none_left = {**{k: False for k in sector}, "C1": True}.get              # no eligible name in A or B: any sector
    assert gate.replace_failures(["A1"], order, sector, none_left) == (["C1"], [("A1", "C1")])
    final, replaced = gate.replace_failures(["B2", "A3"], order, sector, ok, used=["A3"])
    assert final == ["B2", "B1"] and replaced == [("A3", "B1")]              # a used name is taken, not kept
    assert gate.replace_failures(["A1"], order, sector, lambda _k: False) == ([], [])


# ------------------------------------------------------------------------------------ preflight and doctor


def test_unchecked_lines_are_refused_by_the_live_preflight(policy, sleeve_policy):
    assert gate.preflight_errors(policy) == [] and gate.preflight_blockers(policy) == []
    assert gate.preflight_errors(sleeve_policy) == ["stock_eligibility_unchecked:TSTE"]   # the fixture's null stamp
    assert gate.preflight_blockers(sleeve_policy) == ["satellite:stock_eligibility_unchecked"]


def test_the_doctor_sample_reports_codes(cfg):
    b = cs.broker(["AAPL", "MSFT"], MSFT={"requiresW8Ben": True})
    rows = gate.doctor_sample(b.read, ["AAPL", "MSFT", "JNJ"], cfg, now=NOW)
    assert [(v.requested, v.ok, v.reason) for v in rows] == [
        ("AAPL", True, "ok"), ("MSFT", False, "requires_w8ben"), ("JNJ", False, "not_found")]
