"""FakeEtoro enforces the documented auth/idempotency rules, so contract tests against it mean something."""

from __future__ import annotations

import importlib
import uuid

import httpx
import pytest

from council.broker.etoro_read import EtoroReadClient
from council.broker.fake import FakeEtoro
from council.broker.http import BrokerAuthError, DefiniteRejection

BASE = "https://public-api.etoro.com"


@pytest.fixture
def fake():
    broker = FakeEtoro(write_user_keys={"test-write-key"})
    broker.add_instrument("SPX500", 101, bid=100.0, ask=100.1)
    return broker


def _raw(fake, headers):
    with httpx.Client(transport=fake.transport(), base_url=BASE) as c:
        return c.get("/api/v1/trading/info/real/pnl", headers=headers)


def test_fake_rejects_both_auth_modes_missing_request_id_and_bad_keys(fake):
    good = {"x-api-key": "test-app-key", "x-user-key": "test-read-key", "x-request-id": str(uuid.uuid4())}
    assert _raw(fake, good).status_code == 200
    assert _raw(fake, {**good, "Authorization": "Bearer x"}).status_code == 422
    assert _raw(fake, {k: v for k, v in good.items() if k != "x-request-id"}).status_code == 400
    assert _raw(fake, {**good, "x-user-key": "wrong"}).status_code == 401


def test_read_client_surfaces_auth_failure(fake):
    read = EtoroReadClient("test-app-key", "wrong", transport=fake.transport(), sleep=lambda _s: None)
    with pytest.raises(BrokerAuthError):
        read.pnl()


def test_read_token_cannot_write(fake):
    module = importlib.import_module("council.broker.etoro_write")
    writer = module.EtoroWriteClient("test-app-key", "test-read-key", transport=fake.transport())
    with pytest.raises(DefiniteRejection) as err:
        writer.open_order(request_id=str(uuid.uuid4()), instrument_id=101, transaction="buy",
                          settlement="cfd", leverage=1, units=1.0, stop_loss_rate=90.0)
    assert err.value.status == 403 and not fake.orders


def test_same_request_id_is_the_same_order(fake):
    module = importlib.import_module("council.broker.etoro_write")
    writer = module.EtoroWriteClient("test-app-key", "test-write-key", transport=fake.transport())
    rid = str(uuid.uuid4())
    args = dict(request_id=rid, instrument_id=101, transaction="buy", settlement="cfd", leverage=1,
                units=1.0, stop_loss_rate=90.0)
    first, second = writer.open_order(**args), writer.open_order(**args)
    assert first["orderId"] == second["orderId"] and len(fake.orders) == 1
    assert first["referenceId"] == rid


def test_ineligible_settlement_is_accepted_then_rejected(fake):
    module = importlib.import_module("council.broker.etoro_write")
    writer = module.EtoroWriteClient("test-app-key", "test-write-key", transport=fake.transport())
    rid = str(uuid.uuid4())
    writer.open_order(request_id=rid, instrument_id=101, transaction="buy", settlement="cfd",
                      leverage=20, units=1.0, stop_loss_rate=90.0)
    read = EtoroReadClient("test-app-key", "test-read-key", transport=fake.transport())
    assert read.order_lookup(reference_id=rid)["status"]["id"] == 4


def test_fake_payloads_round_trip_through_the_parsers(fake):
    read = EtoroReadClient("test-app-key", "test-read-key", transport=fake.transport())
    fake.add_position("SPX500", units=2, sl_rate=90.0)
    port = read.portfolio({101: "SPX500"})
    assert port.positions[0].symbol == "SPX500" and port.equity_usd == pytest.approx(fake.equity())
    assert read.eligibility(symbols=["SPX500"])[0].leverage_configs
    assert read.costs({"action": "open", "instrumentId": 101, "amount": 100.0, "leverage": 1})["costs"]
    assert read.candles(101, "OneDay", 3)["candles"][0]["candles"][0]["instrumentID"] == 101
    assert read.rates([101])["rates"][0]["bid"] == 100.0
