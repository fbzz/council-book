"""WRITE client contract vs ETORO_ROUTES.md: v3 open by units with stopLossRate, close, PATCH SL,
outcome classification, no retries, and the import guard."""

from __future__ import annotations

import importlib
import json
import sys
import uuid

import httpx
import pytest

from council.broker.http import AmbiguousWriteError, DefiniteRejection, MissingStopLoss

RID = str(uuid.uuid5(uuid.NAMESPACE_URL, "contract-test"))
MODULE = "council.broker.etoro_write"


class Recorder:
    def __init__(self, response):
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        return self.response(request) if callable(self.response) else self.response


@pytest.fixture
def write_mod():
    return importlib.import_module(MODULE)


def _client(write_mod, response=None):
    rec = Recorder(response or httpx.Response(202, json={"token": "t", "orderId": 5001, "referenceId": RID}))
    return write_mod.EtoroWriteClient("app-key", "write-key", transport=httpx.MockTransport(rec)), rec


def _open(c, **overrides):
    kwargs = dict(request_id=RID, instrument_id=101, transaction="buy", settlement="cfd", leverage=2,
                  units=10.5, stop_loss_rate=90.0)
    kwargs.update(overrides)
    return c.open_order(**kwargs)


# ------------------------------------------------------------------------------ open
def test_open_body_is_v3_by_units_with_fixed_stop(write_mod):
    c, rec = _client(write_mod)
    assert _open(c) == {"token": "t", "orderId": 5001, "referenceId": RID}
    (req,) = rec.requests
    assert (req.method, req.url.path) == ("POST", "/api/v3/trading/execution/orders")
    assert json.loads(req.content) == {
        "action": "open", "transaction": "buy", "instrumentId": 101, "settlementType": "cfd",
        "orderType": "mkt", "leverage": 2, "units": 10.5, "stopLossRate": 90.0,
        "stopLossType": "fixed",
    }
    assert req.headers["x-request-id"] == RID
    assert req.headers["x-api-key"] == "app-key" and req.headers["x-user-key"] == "write-key"
    assert "authorization" not in req.headers


def test_short_open_uses_sell_short(write_mod):
    c, rec = _client(write_mod)
    _open(c, transaction="sellShort", stop_loss_rate=110.0)
    assert json.loads(rec.requests[0].content)["transaction"] == "sellShort"


@pytest.mark.parametrize("bad_sl", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_missing_or_invalid_stop_raises_before_sending(write_mod, bad_sl):
    c, rec = _client(write_mod)
    with pytest.raises(MissingStopLoss):
        _open(c, stop_loss_rate=bad_sl)
    assert rec.requests == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"transaction": "sell"}, {"settlement": "swap"}, {"units": 0.0}, {"units": -1.0},
        {"leverage": 0}, {"leverage": 1.5}, {"instrument_id": 0}, {"stop_loss_type": "trailing"},
        {"settlement": "real", "leverage": 2}, {"settlement": "real", "leverage": 1, "transaction": "sellShort"},
        {"request_id": "not-a-uuid"},
    ],
)
def test_invalid_open_arguments_never_send(write_mod, overrides):
    c, rec = _client(write_mod)
    with pytest.raises(ValueError):
        _open(c, **overrides)
    assert rec.requests == []


def test_real_long_1x_is_valid(write_mod):
    c, rec = _client(write_mod)
    _open(c, settlement="real", leverage=1)
    assert json.loads(rec.requests[0].content)["settlementType"] == "real"


# ------------------------------------------------------------------------------ close + patch
def test_partial_and_full_close_bodies(write_mod):
    ok = httpx.Response(200, json={"orderForClose": {"orderID": 7001}, "token": "t"})
    c, rec = _client(write_mod, ok)
    c.close_position(request_id=RID, position_id=1001, instrument_id=1111, units_to_deduct=2.0)
    c.close_position(request_id=RID, position_id=1001, instrument_id=1111)
    partial, full = rec.requests
    assert partial.url.path == "/api/v1/trading/execution/market-close-orders/positions/1001"
    assert json.loads(partial.content) == {"InstrumentId": 1111, "UnitsToDeduct": 2.0}
    assert json.loads(full.content) == {"InstrumentId": 1111, "UnitsToDeduct": None}
    with pytest.raises(ValueError):
        c.close_position(request_id=RID, position_id=1001, instrument_id=1111, units_to_deduct=0.0)
    with pytest.raises(ValueError):
        c.close_position(request_id=RID, position_id=0, instrument_id=1111)


def test_patch_sets_a_fixed_stop_and_never_clears(write_mod):
    c, rec = _client(write_mod, httpx.Response(202, json={"operationId": "o", "positionId": 1001}))
    c.patch_stop_loss(request_id=RID, position_id=1001, stop_loss_rate=145.25)
    (req,) = rec.requests
    assert (req.method, req.url.path) == ("PATCH", "/api/v2/trading/positions/1001")
    body = json.loads(req.content)
    assert body == {"stopLossRate": 145.25, "stopLossType": "fixed"}
    assert "clearStopLoss" not in body
    with pytest.raises(MissingStopLoss):
        c.patch_stop_loss(request_id=RID, position_id=1001, stop_loss_rate=None)  # type: ignore[arg-type]
    assert len(rec.requests) == 1


# ------------------------------------------------------------------------------ outcomes
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_4xx_is_a_definite_rejection(write_mod, status):
    c, rec = _client(write_mod, httpx.Response(status, json={"error": "no"}))
    with pytest.raises(DefiniteRejection) as err:
        _open(c)
    assert err.value.status == status and err.value.retry_after is None
    assert len(rec.requests) == 1                            # never retried


def test_429_is_definite_and_carries_retry_after(write_mod):
    c, rec = _client(write_mod, httpx.Response(429, headers={"Retry-After": "12"}))
    with pytest.raises(DefiniteRejection) as err:
        _open(c)
    assert err.value.status == 429 and err.value.retry_after == 12.0
    assert len(rec.requests) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_ambiguous_and_not_retried(write_mod, status):
    c, rec = _client(write_mod, httpx.Response(status))
    with pytest.raises(AmbiguousWriteError) as err:
        _open(c)
    assert err.value.request_id == RID and err.value.status == status
    assert len(rec.requests) == 1


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
def test_transport_failures_are_ambiguous(write_mod, exc):
    def boom(request):
        raise exc("lost", request=request)

    c, rec = _client(write_mod, boom)
    with pytest.raises(AmbiguousWriteError) as err:
        c.close_position(request_id=RID, position_id=1, instrument_id=2)
    assert err.value.request_id == RID and len(rec.requests) == 1


def test_accepted_write_with_unreadable_body_is_ambiguous(write_mod):
    c, _rec = _client(write_mod, httpx.Response(202, content=b"<html>"))
    with pytest.raises(AmbiguousWriteError):
        _open(c)


# ------------------------------------------------------------------------------ import guard
@pytest.mark.parametrize(
    ("role", "mode", "allowed"),
    [
        ("operator", "live", True), ("operator", "stub", True), ("dev", "stub", True),
        ("dev", "live", False), ("dev", "dry_run", False), ("runner", "live", False),
        ("runner", "stub", False), (None, None, False),
    ],
)
def test_import_guard(monkeypatch, role, mode, allowed):
    for name, value in (("COUNCIL_ROLE", role), ("COUNCIL_MODE", mode)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    saved = sys.modules.pop(MODULE, None)
    try:
        if allowed:
            importlib.import_module(MODULE)
        else:
            with pytest.raises(RuntimeError, match="operator"):
                importlib.import_module(MODULE)
    finally:
        sys.modules.pop(MODULE, None)
        if saved is not None:
            sys.modules[MODULE] = saved


def test_runner_can_import_everything_else_without_loading_the_writer():
    """Everything in broker/, execution/ and ledger/ except etoro_write imports under the runner
    role, and none of it pulls the writer in (the executor takes the writer as a parameter)."""
    import os
    import subprocess

    code = (
        "import sys\n"
        "import council.broker, council.broker.etoro_read, council.broker.eligibility\n"
        "import council.broker.instruments, council.broker.parsing, council.broker.fake\n"
        "import council.execution.executor, council.execution.planner\n"
        "import council.execution.reconcile, council.execution.ratelimit, council.ledger.db\n"
        "assert 'council.broker.etoro_write' not in sys.modules, 'writer leaked'\n"
        "try:\n"
        "    import council.broker.etoro_write\n"
        "except RuntimeError:\n"
        "    print('guarded')\n"
    )
    env = {**os.environ, "COUNCIL_ROLE": "runner", "COUNCIL_MODE": "live"}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "guarded"
