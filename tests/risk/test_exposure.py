"""Exposure parsing from /real/pnl payloads shaped like tests/contract/specs/ETORO_ROUTES.md.
All IDs and amounts are synthetic."""

from __future__ import annotations

import copy

import pytest

from council.risk.exposure import (
    exposure_with_source,
    levels_from_weights,
    parse_position,
    position_exposure,
    snapshot_from_pnl,
)

VEHICLES = {101: "EQQQ.L", 102: "SPX500", 103: "BTC"}
LINES = {"EQQQ.L": "NDX", "SPX500": "SPX", "BTC": "BTC"}


def spec_payload() -> dict:
    """The documented example payload (one leveraged long)."""
    return {"clientPortfolio": {
        "credit": 10000.5, "unrealizedPnL": 251.0, "bonusCredit": 0.0, "accountCurrencyId": 1,
        "positions": [{
            "positionID": 9001, "CID": 123, "openDateTime": "2024-01-01T09:00:00Z",
            "openRate": 1.2345, "instrumentID": 101, "isBuy": True, "takeProfitRate": 1.5,
            "stopLossRate": 1.2, "mirrorID": 0, "amount": 1000.0, "leverage": 2, "orderID": 5001,
            "orderType": 1, "units": 10.5, "totalFees": 2.5, "initialAmountInDollars": 1000.0,
            "isTslEnabled": False, "settlementTypeID": 0, "isNoStopLoss": False,
            "isNoTakeProfit": False,
            "unrealizedPnL": {"pnL": 100.25, "exposureInAccountCurrency": 2100.0,
                              "marginInAccountCurrency": 1000.0, "closeRate": 1.25,
                              "closeConversionRate": 1.0, "timestamp": "2024-01-01T12:00:00Z"},
        }],
        "mirrors": [], "orders": [], "ordersForOpen": [], "ordersForClose": [],
        "ordersForCloseMultiple": [],
    }}


def snap(payload, now):
    return snapshot_from_pnl(payload, vehicle_by_instrument=VEHICLES, line_by_vehicle=LINES,
                             now=now)


def test_spec_example_parses(now):
    s = snap(spec_payload(), now)
    equity = 10000.5 + 1000.0 + 251.0
    assert s.equity_usd == pytest.approx(equity)
    assert s.signed_w == {"NDX": pytest.approx(2100.0 / equity)}
    assert s.gross == pytest.approx(2100.0 / equity)
    assert s.net == pytest.approx(2100.0 / equity)
    assert s.margin_use == pytest.approx(1000.0 / equity)
    pos = s.positions[0]
    assert pos.symbol == "EQQQ.L" and pos.leverage == 2 and pos.settlement == "cfd"
    assert pos.sl_rate == 1.2 and pos.opened_at is not None
    assert pos.exposure_usd == 2100.0 and pos.close_rate == 1.25
    assert s.unmapped == [] and s.hedged == [] and s.flags == []


def test_casing_variants_parse_identically(now):
    payload = spec_payload()
    raw = payload["clientPortfolio"]["positions"][0]
    variant = {
        ("positionId" if k == "positionID" else "instrumentId" if k == "instrumentID"
         else "IsBuy" if k == "isBuy" else k): v
        for k, v in raw.items()
    }
    variant["unrealizedPnl"] = {k.lower(): v for k, v in variant.pop("unrealizedPnL").items()}
    other = {"ClientPortfolio": {**payload["clientPortfolio"], "positions": [variant]}}
    assert snap(other, now).signed_w == snap(payload, now).signed_w


def test_payload_without_wrapper(now):
    inner = spec_payload()["clientPortfolio"]
    assert snap(inner, now).signed_w == snap(spec_payload(), now).signed_w


def test_exposure_falls_back_to_units_times_rate():
    raw = copy.deepcopy(spec_payload()["clientPortfolio"]["positions"][0])
    del raw["unrealizedPnL"]["exposureInAccountCurrency"]
    raw["unrealizedPnL"]["closeConversionRate"] = 2.0
    assert position_exposure(raw) == pytest.approx(10.5 * 1.25 * 2.0)


def test_exposure_last_resort_amount_times_leverage():
    raw = {"positionID": 1, "instrumentID": 101, "isBuy": True, "amount": 300.0, "leverage": 2}
    assert position_exposure(raw) == pytest.approx(600.0)
    with pytest.raises(ValueError):
        position_exposure({"positionID": 1})


def test_short_is_negative_and_unmapped_is_locked(now):
    payload = spec_payload()
    port = payload["clientPortfolio"]
    port["positions"].append({
        "positionID": 9002, "instrumentID": 102, "isBuy": False, "amount": 500.0, "leverage": 1,
        "units": 1.0, "settlementTypeID": 0,
        "unrealizedPnL": {"pnL": -10.0, "exposureInAccountCurrency": 500.0},
    })
    port["positions"].append({
        "positionID": 9003, "instrumentID": 777, "isBuy": True, "amount": 200.0, "leverage": 1,
        "units": 2.0, "settlementTypeID": 1, "stopLossRate": 0, "isNoStopLoss": True,
        "unrealizedPnL": {"pnL": 0.0, "exposureInAccountCurrency": 200.0},
    })
    s = snap(payload, now)
    eq = s.equity_usd
    assert s.signed_w["SPX"] == pytest.approx(-500.0 / eq)
    assert s.signed_w["UNMAPPED_777"] == pytest.approx(200.0 / eq)
    assert s.unmapped == ["UNMAPPED_777"]
    assert s.gross == pytest.approx((2100 + 500 + 200) / eq)
    assert s.net == pytest.approx((2100 - 500 + 200) / eq)
    unmapped = next(p for p in s.positions if p.instrument_id == 777)
    assert unmapped.sl_rate is None and unmapped.settlement == "real"


def test_hedged_line_is_flagged_and_gross_counts_both_legs(now):
    payload = spec_payload()
    payload["clientPortfolio"]["positions"].append({
        "positionID": 9004, "instrumentID": 101, "isBuy": False, "amount": 100.0, "leverage": 1,
        "unrealizedPnL": {"pnL": 0.0, "exposureInAccountCurrency": 100.0},
    })
    s = snap(payload, now)
    assert s.hedged == ["NDX"]
    assert s.signed_w["NDX"] == pytest.approx(2000.0 / s.equity_usd)
    assert s.gross == pytest.approx(2200.0 / s.equity_usd)


def test_unrealized_falls_back_to_positions(now):
    payload = spec_payload()
    del payload["clientPortfolio"]["unrealizedPnL"]
    assert snap(payload, now).equity_usd == pytest.approx(10000.5 + 1000.0 + 100.25)


def test_mirror_is_unmapped_and_counts_in_equity(now):
    payload = spec_payload()
    payload["clientPortfolio"]["mirrors"] = [{
        "mirrorID": 55, "availableAmount": 50.0,
        "positions": [{"positionID": 1, "instrumentID": 101, "isBuy": True, "amount": 100.0,
                       "unrealizedPnL": {"pnL": 5.0, "exposureInAccountCurrency": 100.0}}],
    }]
    s = snap(payload, now)
    assert "UNMAPPED_MIRROR_55" in s.unmapped
    assert s.equity_usd == pytest.approx(10000.5 + 1000.0 + 50.0 + 100.0 + 251.0)


def test_rejects_bad_inputs(now):
    with pytest.raises(ValueError):
        snap({"clientPortfolio": {"positions": []}}, now)  # no credit
    with pytest.raises(ValueError):
        snap({"clientPortfolio": {"credit": 0.0, "positions": []}}, now)  # zero equity
    payload = spec_payload()
    del payload["clientPortfolio"]["positions"][0]["isBuy"]
    with pytest.raises(ValueError):
        snap(payload, now)
    with pytest.raises(ValueError):
        snap(spec_payload(), now.replace(tzinfo=None))


def test_empty_account_has_no_weights(now):
    s = snap({"clientPortfolio": {"credit": 10000.0, "unrealizedPnL": 0.0, "positions": []}}, now)
    assert s.signed_w == {} and s.gross == 0.0 and s.equity_usd == 10000.0


def test_levels_from_weights():
    levels = levels_from_weights({"NDX": 0.35, "SPX": -0.075, "UNMAPPED_9": 0.1},
                                 {"NDX": 0.35, "SPX": 0.15, "GOLD": 0.12})
    assert levels == {"NDX": pytest.approx(1.0), "SPX": pytest.approx(-0.5), "GOLD": 0.0}
    with pytest.raises(ValueError):
        levels_from_weights({"OIL": 0.1}, {"OIL": 0.0})


def test_positions_carry_the_exposure_used_and_fallbacks_are_flagged(now):
    payload = spec_payload()
    port = payload["clientPortfolio"]
    del port["positions"][0]["unrealizedPnL"]["exposureInAccountCurrency"]      # units x rate
    port["positions"].append({                                                  # amount x leverage
        "positionID": 9005, "instrumentID": 102, "isBuy": False, "amount": 200.0, "leverage": 2,
    })
    port["positions"].append({                                                  # unmapped, amount only
        "positionID": 9006, "instrumentID": 4242, "isBuy": True, "amount": 50.0,
    })
    s = snap(payload, now)
    by_id = {p.position_id: p for p in s.positions}
    assert by_id[9001].exposure_usd == pytest.approx(10.5 * 1.25) and by_id[9001].close_rate == 1.25
    assert by_id[9005].exposure_usd == pytest.approx(400.0) and by_id[9005].close_rate is None
    assert by_id[9006].exposure_usd == pytest.approx(50.0)
    assert s.signed_w["NDX"] == pytest.approx(by_id[9001].exposure_usd / s.equity_usd)
    assert s.signed_w["SPX"] == pytest.approx(-400.0 / s.equity_usd)
    assert s.gross * s.equity_usd == pytest.approx(sum(p.exposure_usd for p in s.positions))
    assert s.flags == [
        "exposure_fallback:amount_x_leverage:SPX",
        "exposure_fallback:amount_x_leverage:UNMAPPED",
        "exposure_fallback:units_x_close_rate:NDX",
    ]
    assert not any("4242" in f for f in s.flags)                                 # no instrument IDs


def test_mirror_fallbacks_are_flagged_too(now):
    payload = spec_payload()
    payload["clientPortfolio"]["mirrors"] = [{
        "mirrorID": 55, "availableAmount": 0.0,
        "positions": [{"positionID": 1, "instrumentID": 101, "isBuy": True, "amount": 100.0}],
    }]
    assert snap(payload, now).flags == ["exposure_fallback:amount_x_leverage:UNMAPPED"]


def test_parse_position_without_any_exposure_leaves_it_unset():
    raw = {"positionID": 1, "instrumentID": 101, "isBuy": True, "units": 2.0}
    pos = parse_position(raw, "EQQQ.L")
    assert pos.exposure_usd is None and pos.close_rate is None
    assert exposure_with_source(raw) is None
    raw["unrealizedPnL"] = {"closeRate": 0.0}                                    # not a rate
    assert parse_position(raw, "EQQQ.L").close_rate is None
    assert exposure_with_source(raw) is None                                     # never exposure 0
