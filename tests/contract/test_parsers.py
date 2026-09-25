"""Parsers accept the documented payloads and their casing variants (ETORO_ROUTES.md)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from council.broker.eligibility import parse_eligibility
from council.broker.parsing import (
    close_order_id,
    parse_close_order,
    parse_order_status,
    parse_pnl,
    parse_rates,
    snapshot_from_portfolio,
)

NOW = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)

# The spec's example shapes (small synthetic ids only).
PNL = {"clientPortfolio": {
    "credit": 10000.5, "unrealizedPnL": 251.0, "bonusCredit": 0.0, "accountCurrencyId": 1,
    "positions": [{
        "positionID": 9001, "CID": 123, "openDateTime": "2024-01-01T09:00:00Z", "openRate": 1.2345,
        "instrumentID": 101, "isBuy": True, "takeProfitRate": 1.5, "stopLossRate": 1.2, "mirrorID": 0,
        "amount": 1000.0, "leverage": 2, "orderID": 5001, "orderType": 1, "units": 10.5,
        "totalFees": 2.5, "initialAmountInDollars": 1000.0, "isTslEnabled": False,
        "settlementTypeID": 0, "isNoStopLoss": False, "isNoTakeProfit": False,
        "unrealizedPnL": {"pnL": 100.25, "exposureInAccountCurrency": 2100.0,
                          "marginInAccountCurrency": 1000.0, "closeRate": 1.25,
                          "closeConversionRate": 1.0, "timestamp": "2024-01-01T12:00:00Z"},
    }],
    "mirrors": [], "orders": [], "ordersForOpen": [], "ordersForClose": [], "ordersForCloseMultiple": [],
}}

LOOKUP = {
    "orderId": 5001, "action": "open", "transaction": "buy", "type": "mkt",
    "status": {"id": 3, "name": "Filled", "errorCode": 0, "errorMessage": None},
    "asset": {"symbol": "AAPL", "instrumentId": 101, "currency": "USD", "settlementType": "cfd",
              "leverage": 2, "side": "long"},
    "requestedAmount": 1000.0, "frozenAmount": 1002.5, "openStopLossRate": 1.2, "stopLossType": "fixed",
    "totalCosts": 2.5,
    "positionExecutions": [{
        "positionId": 9001, "state": "open", "investedAmountCurrency": 1000,
        "initialExposureAccountCurrency": 1000.0, "marginAccountCurrency": 1000.0,
        "remainingUnits": 10.5, "stopLossRate": 1.2,
        "openingData": {"executionTime": "2024-01-01T09:00:01Z", "units": 10.5, "avgPrice": 95.238095,
                        "marketSpread": 0.0002, "markup": 0.0, "fees": 2.5, "taxes": 0.0},
    }],
    "requestTime": "2024-01-01T09:00:00Z", "lastUpdate": "2024-01-01T09:00:01Z", "requestType": "byAmount",
}


# ------------------------------------------------------------------------------ eligibility
def test_eligibility_spec_example_and_casing_variants():
    payload = {"currency": "USD", "eligibilities": [
        {"instrumentId": 1001, "symbol": "AAPL", "minPositionExposure": 50.0, "maxUnitsPerOrder": 10000.0,
         "allowOpenPosition": True, "allowClosePosition": True, "allowPartialClosePosition": True,
         "allowTrailingStopLoss": True, "unitsQuantityType": "FractionalUnits",
         "leverageConfigs": [
             {"settlementType": "CFD", "direction": "LONG", "leverageValues": [1, 2, 5], "isPotential": False,
              "minPositionAmount": 50.0, "allowEditStopLoss": True, "minStopLossPercentage": 5.0,
              "maxStopLossPercentage": 50.0, "defaultStopLossPercentage": 50.0, "allowStopLossTakeProfit": True},
             {"settlementType": "cfd", "direction": "short", "leverageValues": [1.0, 2.0], "isPotential": True},
             {"settlementType": "Real", "direction": "Long", "leverageValues": [1]},
             {"settlementType": "SWAP", "direction": "LONG", "leverageValues": [1]},   # unknown: dropped
         ]},
        {"InstrumentID": 1002, "Symbol": "SPY", "leverageConfigs": []},             # PascalCase keys
        {"symbol": "NOID"},                                                          # no id: dropped
    ]}
    rows = parse_eligibility(payload, NOW)
    assert [r.symbol for r in rows] == ["AAPL", "SPY"]
    aapl, spy = rows
    assert aapl.min_position_exposure == 50.0 and aapl.allow_partial_close
    assert [(c.settlement, c.direction) for c in aapl.leverage_configs] == [
        ("cfd", "long"), ("cfd", "short"), ("real", "long"),
    ]
    assert aapl.leverage_configs[0].min_sl_pct == 5.0 and aapl.leverage_configs[0].max_sl_pct == 50.0
    assert aapl.leverage_configs[1].leverage_values == [1, 2] and aapl.leverage_configs[1].is_potential
    assert spy.instrument_id == 1002
    assert not spy.allow_open and not spy.allow_partial_close        # absent permissions fail closed
    assert spy.fetched_at == NOW


# ------------------------------------------------------------------------------ pnl
def test_pnl_spec_example():
    read = parse_pnl(PNL, {101: "AAPL"})
    (p,) = read.positions
    assert (p.position_id, p.instrument_id, p.symbol, p.is_buy, p.leverage) == (9001, 101, "AAPL", True, 2)
    assert (p.units, p.open_rate, p.amount, p.sl_rate, p.settlement) == (10.5, 1.2345, 1000.0, 1.2, "cfd")
    assert p.opened_at == datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    assert read.exposure_usd == {9001: 2100.0}
    assert read.equity_usd == pytest.approx(10000.5 + 1000.0 + 251.0)


def test_pnl_casing_variants_and_fallbacks():
    payload = {"ClientPortfolio": {"Credit": 500.0, "Positions": [
        {"positionId": 1, "instrumentId": 7, "isBuy": False, "openRate": 10.0, "units": 3.0,
         "amount": 30.0, "leverage": 1, "settlementTypeId": 1, "isNoStopLoss": True, "stopLossRate": 0.0001,
         "unrealizedPnL": {"pnL": -1.5, "closeRate": 10.5, "closeConversionRate": 2.0}},
        {"positionID": 2, "instrumentID": 8, "isBuy": True, "openRate": 4.0, "units": 5.0, "amount": 20.0,
         "stopLossRate": 0.0},
    ]}}
    read = parse_pnl(payload)
    short, other = read.positions
    assert short.symbol == "UNMAPPED_7" and short.sl_rate is None and short.settlement == "real"
    assert read.exposure_usd[1] == pytest.approx(3.0 * 10.5 * 2.0)   # units × closeRate × conversion
    assert read.exposure_usd[2] == pytest.approx(20.0)                # units × openRate
    assert other.sl_rate is None
    assert read.unrealized_pnl_usd == pytest.approx(-1.5)             # summed when no total
    assert read.equity_usd == pytest.approx(500.0 + 50.0 - 1.5)


def test_pnl_keeps_the_broker_exposure_and_close_rate_on_positions():
    (p,) = parse_pnl(PNL, {101: "AAPL"}).positions
    assert (p.exposure_usd, p.close_rate) == (2100.0, 1.25)
    payload = {"clientPortfolio": {"credit": 500.0, "positions": [
        {"positionId": 1, "instrumentId": 7, "isBuy": False, "openRate": 10.0, "units": 3.0,
         "amount": 30.0, "unrealizedPnL": {"closeRate": 10.5, "closeConversionRate": 2.0}},
        {"positionID": 2, "instrumentID": 8, "isBuy": True, "openRate": 4.0, "units": 5.0, "amount": 20.0},
        {"positionID": 3, "instrumentID": 9, "isBuy": True, "openRate": 4.0, "units": 5.0, "amount": 20.0,
         "unrealizedPnL": {"exposureInAccountCurrency": -21.0, "closeRate": 0.0}},
    ]}}
    read = parse_pnl(payload, {8: "SPX500"})
    converted, fallback, negative = read.positions
    assert converted.exposure_usd == pytest.approx(63.0) and converted.close_rate == 10.5
    assert fallback.exposure_usd is None and fallback.close_rate is None      # open-rate fallback
    assert read.exposure_usd[2] == pytest.approx(20.0)
    assert negative.exposure_usd == pytest.approx(21.0) and negative.close_rate is None
    snap = snapshot_from_portfolio(read, NOW)
    assert snap.flags == ["SPX500: exposure_from_open_rate"]
    assert snapshot_from_portfolio(parse_pnl(PNL, {101: "AAPL"}), NOW).flags == []


def test_pnl_rejects_malformed_payloads():
    with pytest.raises(ValueError):
        parse_pnl({"clientPortfolio": {"positions": []}})             # no credit
    with pytest.raises(ValueError):
        parse_pnl({"clientPortfolio": {"credit": 1.0, "positions": [{"positionID": 1}]}})


def test_snapshot_weights_hedges_and_margin():
    payload = {"clientPortfolio": {"credit": 800.0, "unrealizedPnL": 0.0, "positions": [
        {"positionID": 1, "instrumentID": 1, "isBuy": True, "openRate": 10.0, "units": 10, "amount": 50.0, "leverage": 2,
         "stopLossRate": 9.0, "unrealizedPnL": {"exposureInAccountCurrency": 100.0}},
        {"positionID": 2, "instrumentID": 1, "isBuy": False, "openRate": 10.0, "units": 5, "amount": 50.0, "leverage": 1,
         "stopLossRate": 11.0, "unrealizedPnL": {"exposureInAccountCurrency": 50.0}},
    ]}}
    snap = snapshot_from_portfolio(parse_pnl(payload, {1: "SPX500"}), NOW)
    assert snap.equity_usd == 900.0
    assert snap.signed_w == {"SPX500": pytest.approx((100 - 50) / 900)}
    assert snap.gross == pytest.approx(150 / 900) and snap.net == pytest.approx(50 / 900)
    assert snap.margin_use == pytest.approx((100 / 2 + 50) / 900)
    assert snap.hedged == ["SPX500"] and snap.unmapped == []
    broke = parse_pnl({"clientPortfolio": {"credit": 0.0, "positions": []}})
    with pytest.raises(ValueError):
        snapshot_from_portfolio(broke, NOW)


# ------------------------------------------------------------------------------ orders
def test_order_lookup_spec_example():
    status = parse_order_status(LOOKUP)
    assert (status.order_id, status.status_id, status.status_name) == (5001, 3, "Filled")
    assert status.filled_units == 10.5 and status.avg_price == pytest.approx(95.238095)
    assert status.position_ids == [9001] and status.broker_exposure_usd == 1000.0
    assert status.sl_rate == 1.2


def test_order_lookup_variants():
    status = parse_order_status({"orderID": 1, "statusID": 11, "positionExecutions": []})
    assert status.status_id == 11 and status.filled_units == 0 and status.avg_price is None
    status = parse_order_status({"orderId": 2, "status": 4})
    assert status.status_id == 4
    with pytest.raises(ValueError):
        parse_order_status({"orderId": 3})


def test_close_order_info_and_close_response():
    info = parse_close_order({
        "orderID": 7001, "CID": 1, "statusID": 3, "referenceID": None, "orderType": 19, "errorCode": None,
        "instrumentID": 1111, "positions": [{"positionID": 1001, "occurred": "2026-10-01T14:41:00Z",
                                             "rate": 10.0, "units": 2.0}],
    })
    assert info.order_id == 7001 and not info.failed and info.occurred_for(1001)
    assert not info.occurred_for(1002)
    assert parse_close_order({"orderID": 1, "errorCode": 12, "positions": []}).failed
    assert close_order_id({"orderForClose": {"orderID": 7001}, "token": "t"}) == 7001
    assert close_order_id({"orderForClose": {"orderId": 7002}}) == 7002


def test_rates_casing_variants():
    quotes = parse_rates({"rates": [
        {"instrumentID": 1, "bid": 99.0, "ask": 99.1, "date": "2026-10-01T14:40:00Z"},
        {"instrumentId": 2, "Bid": 5.0, "Ask": 5.1},
        {"instrumentId": 3, "bid": 0.0, "ask": 1.0},                    # unusable
    ]}, {1: "SPX500"}, at=NOW)
    assert set(quotes) == {"SPX500", "UNMAPPED_2"}
    assert quotes["SPX500"].mid == pytest.approx(99.05) and quotes["UNMAPPED_2"].at == NOW
    assert parse_rates([{"instrumentID": 1, "bid": 1.0, "ask": 1.1}], {1: "X"})["X"].bid == 1.0
