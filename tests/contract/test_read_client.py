"""READ client contract vs ETORO_ROUTES.md: headers, routes, bodies, retries, 404 semantics."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx

from council.broker.etoro_read import READ_POST_ALLOWLIST, EtoroReadClient
from council.broker.http import BrokerAuthError, BrokerHTTPError, BrokerUnavailable

BASE = "https://public-api.etoro.com"


class Recorder:
    """MockTransport handler replaying scripted responses and recording requests."""

    def __init__(self, *responses):
        self.responses = list(responses) or [httpx.Response(200, json={})]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return item(request) if callable(item) else item


def client(recorder: Recorder, sleeps: list[float] | None = None) -> EtoroReadClient:
    return EtoroReadClient(
        "app-key", "read-key", transport=httpx.MockTransport(recorder),
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
    )


# ------------------------------------------------------------------------------ headers
def test_every_read_carries_key_pair_and_request_id_never_bearer():
    rec = Recorder(httpx.Response(200, json={"clientPortfolio": {"credit": 1.0, "positions": []}}))
    c = client(rec)
    c.pnl()
    c.rates([1, 2])
    c.post_read("/api/v2/trading/info/costs", {"action": "open"})
    for request in rec.requests:
        assert request.headers["x-api-key"] == "app-key"
        assert request.headers["x-user-key"] == "read-key"
        assert uuid.UUID(request.headers["x-request-id"])
        assert "authorization" not in request.headers
    assert len({r.headers["x-request-id"] for r in rec.requests}) == 3


def test_credentials_never_appear_in_repr():
    text = repr(client(Recorder()))
    assert "app-key" not in text and "read-key" not in text


def test_empty_credentials_refused():
    with pytest.raises(ValueError):
        EtoroReadClient("", "read-key")


# ------------------------------------------------------------------------------ routes
@respx.mock(base_url=BASE, assert_all_called=True)
def test_routes_match_the_spec(respx_mock):
    respx_mock.get("/api/v1/trading/info/real/pnl").respond(200, json={"clientPortfolio": {"credit": 1.0}})
    respx_mock.get("/api/v2/market-data/rates", params={"instrumentIds": "1,2"}).respond(200, json={"rates": []})
    respx_mock.get("/api/v1/market-data/instruments/12/history/candles/desc/OneDay/250").respond(200, json={"candles": []})
    respx_mock.get("/api/v1/feeds/news", params={"take": "50", "offset": "0"}).respond(200, json={"discussions": []})
    respx_mock.get("/api/v1/agent-portfolios").respond(200, json={"agentPortfolios": []})
    respx_mock.get("/api/v2/trading/info/orders:lookup", params={"referenceId": "abc"}).respond(200, json={"orderId": 1})
    respx_mock.get("/api/v1/trading/info/real/close-orders/77").respond(200, json={"orderID": 77})
    c = EtoroReadClient("app-key", "read-key", base_url=BASE, sleep=lambda _s: None)
    c.pnl()
    c.rates([1, 2])
    c.candles(12, "OneDay", 250)
    c.feeds_news()
    c.agent_portfolios()
    assert c.order_lookup(reference_id="abc") == {"orderId": 1}
    assert c.close_order_info(77) == {"orderID": 77}


def test_eligibility_body_and_batching():
    row = {"instrumentId": 1, "symbol": "SPX500", "allowOpenPosition": True, "leverageConfigs": []}
    rec = Recorder(httpx.Response(200, json={"currency": "USD", "eligibilities": [row]}))
    c = client(rec)
    rows = c.eligibility(symbols=["SPX500"])
    assert json.loads(rec.requests[0].content) == {"currency": "USD", "symbols": ["SPX500"]}
    assert rec.requests[0].url.path == "/api/v2/trading/info/eligibility"
    assert rows[0].instrument_id == 1
    rec.requests.clear()
    c.eligibility(symbols=[f"S{i}" for i in range(150)], instrument_ids=[7])
    bodies = [json.loads(r.content) for r in rec.requests]
    assert [len(b.get("symbols", [])) + len(b.get("instrumentIds", [])) for b in bodies] == [100, 51]
    assert bodies[1]["instrumentIds"] == [7]
    with pytest.raises(ValueError):
        c.eligibility()


def test_costs_body_is_passed_through_and_validated():
    rec = Recorder(httpx.Response(200, json={"costs": []}))
    body = {"action": "open", "transaction": "buy", "instrumentId": 101, "settlementType": "cfd",
            "orderType": "mkt", "leverage": 2, "amount": 1000.0, "orderCurrency": "usd"}
    client(rec).costs(body)
    assert json.loads(rec.requests[0].content) == body
    with pytest.raises(ValueError):
        client(rec).costs({"action": "buy"})


def test_post_read_refuses_any_non_allow_listed_route():
    assert frozenset({"/api/v2/trading/info/eligibility", "/api/v2/trading/info/costs"}) == READ_POST_ALLOWLIST
    rec = Recorder()
    c = client(rec)
    for path in ("/api/v3/trading/execution/orders", "/api/v1/trading/execution/market-close-orders/positions/1"):
        with pytest.raises(ValueError):
            c.post_read(path, {})
    assert rec.requests == []


def test_argument_validation():
    c = client(Recorder())
    with pytest.raises(ValueError):
        c.candles(1, "OneYear", 10)
    with pytest.raises(ValueError):
        c.candles(1, "OneDay", 1001)
    with pytest.raises(ValueError):
        c.candles(1, "OneDay", 10, direction="up")
    with pytest.raises(ValueError):
        c.rates([])
    with pytest.raises(ValueError):
        c.feeds_news(take=0)
    with pytest.raises(ValueError):
        c.order_lookup()
    with pytest.raises(ValueError):
        c.order_lookup(reference_id="a", order_id=1)


def test_lookups_return_none_on_404():
    c = client(Recorder(httpx.Response(404, json={"error": "not found"})))
    assert c.order_lookup(order_id=5) is None
    assert c.close_order_info(5) is None


def test_other_404_raises():
    with pytest.raises(BrokerHTTPError) as err:
        client(Recorder(httpx.Response(404))).get_json("/api/v1/nope")
    assert err.value.status == 404


# ------------------------------------------------------------------------------ retries
def test_429_honours_retry_after_then_succeeds():
    sleeps: list[float] = []
    rec = Recorder(httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"ok": 1}))
    assert client(rec, sleeps).get_json("/api/v1/x") == {"ok": 1}
    assert sleeps == [7.0] and len(rec.requests) == 2
    assert rec.requests[0].headers["x-request-id"] == rec.requests[1].headers["x-request-id"]


def test_retry_after_is_capped_and_defaulted():
    sleeps: list[float] = []
    rec = Recorder(httpx.Response(429, headers={"Retry-After": "3600"}), httpx.Response(429), httpx.Response(200, json={}))
    client(rec, sleeps).get_json("/api/v1/x")
    assert sleeps == [60.0, 5.0]


def test_persistent_429_raises_unavailable():
    with pytest.raises(BrokerUnavailable):
        client(Recorder(httpx.Response(429, headers={"Retry-After": "1"}))).get_json("/api/v1/x")


def test_5xx_backs_off_then_succeeds_and_persistent_5xx_raises():
    sleeps: list[float] = []
    rec = Recorder(httpx.Response(502), httpx.Response(503), httpx.Response(200, json={"ok": 1}))
    assert client(rec, sleeps).get_json("/api/v1/x") == {"ok": 1}
    assert sleeps == [1.0, 2.0]
    sleeps.clear()
    with pytest.raises(BrokerUnavailable):
        client(Recorder(httpx.Response(500)), sleeps).get_json("/api/v1/x")
    assert sleeps == [1.0, 2.0, 4.0]


def test_transport_errors_are_retried_for_reads():
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    rec = Recorder(boom, httpx.Response(200, json={"ok": 1}))
    assert client(rec).get_json("/api/v1/x") == {"ok": 1}


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_raise_immediately(status):
    rec = Recorder(httpx.Response(status))
    with pytest.raises(BrokerAuthError):
        client(rec).pnl()
    assert len(rec.requests) == 1
