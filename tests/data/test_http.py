from __future__ import annotations

import httpx
import pytest

from council.data.http import DataError, backoff_delay, get_with_retry, json_body

SECRET_URL = "https://api.example.test/x?api_key=SUPERSECRET"


def test_backoff_rule_pass_and_cap():
    assert backoff_delay(0, 1.0, None) == 1.0
    assert backoff_delay(2, 1.0, None) == 4.0
    assert backoff_delay(0, 1.0, 7.0) == 7.0          # Retry-After wins
    assert backoff_delay(10, 1.0, None) == 30.0       # capped
    assert backoff_delay(0, 1.0, 120.0) == 30.0


def test_retries_429_then_succeeds(mock_client, _no_sleep):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    resp = get_with_retry(mock_client(handler), SECRET_URL, what="t")
    assert json_body(resp, what="t") == {"ok": True}
    assert len(calls) == 3 and _no_sleep == [2.0, 2.0]


def test_transport_errors_are_retried_then_give_up(mock_client, _no_sleep):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(DataError) as info:
        get_with_retry(mock_client(handler), SECRET_URL, what="probe", retries=2)
    assert "3 attempts" in str(info.value) and "ConnectError" in str(info.value)
    assert "SUPERSECRET" not in str(info.value) and "api.example" not in str(info.value)
    assert _no_sleep == [1.0, 2.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 418])
def test_client_errors_fail_without_retry(mock_client, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    with pytest.raises(DataError) as info:
        get_with_retry(mock_client(handler), SECRET_URL, what="probe")
    assert len(calls) == 1
    assert f"HTTP {status}" in str(info.value) and "SUPERSECRET" not in str(info.value)


def test_non_json_body_is_a_data_error(mock_client):
    resp = get_with_retry(mock_client(lambda r: httpx.Response(200, text="<html>")), SECRET_URL, what="p")
    with pytest.raises(DataError):
        json_body(resp, what="p")
