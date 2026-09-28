"""SW-1: FINRA short interest (design §1.4, §3.4): one bounded POST, latest settlement on or before
the slot, stale or future settlements dropped, 429 fails at once. Fixtures only."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest

from council.data import finra
from council.data.http import DataError, RateLimited

ASOF = datetime(2026, 9, 28, 18, 40, tzinfo=UTC)
ROWS = [
    {"symbolCode": "NVDA", "settlementDate": "2026-09-15", "currentShortPositionQuantity": 250_000_000,
     "averageDailyVolumeQuantity": 200_000_000, "daysToCoverQuantity": 1.25},
    {"symbolCode": "NVDA", "settlementDate": "2026-08-29", "currentShortPositionQuantity": 240_000_000},
    {"symbolCode": "NVDA", "settlementDate": "2026-09-30", "currentShortPositionQuantity": 1},   # after asof
    {"symbolCode": "OLD", "settlementDate": "2026-07-15", "currentShortPositionQuantity": 5},     # stale
    {"symbolCode": "BRK.B", "settlementDate": "2026-09-15", "currentShortPositionQuantity": "x"},
    "junk",
]


def _client(status=200, body=ROWS, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, content=json.dumps(body).encode(), headers={"content-type": "application/json"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_latest_on_or_before_asof():
    out = finra.parse_rows(ROWS, asof=ASOF.date())
    assert set(out) == {"NVDA"}
    si = out["NVDA"]
    assert si.settlement_date == date(2026, 9, 15) and si.days_to_cover == 1.25
    assert si.pct_of_shares(24_400_000_000) == pytest.approx(1.0246, abs=1e-3)
    assert si.pct_of_shares(None) is None


def test_one_bounded_post_with_symbol_filter():
    seen: list[httpx.Request] = []
    out = finra.fetch_short_interest(["NVDA", "BRK_B"], asof=ASOF, client=_client(seen=seen))
    assert set(out) == {"NVDA"} and len(seen) == 1
    req = seen[0]
    assert req.method == "POST" and req.url.host == "api.finra.org"
    body = json.loads(req.content)
    assert body["domainFilters"][0]["values"] == ["NVDA", "BRK.B"]
    assert body["dateRangeFilters"][0]["endDate"] == "2026-09-28"


def test_limits_and_errors():
    with pytest.raises(ValueError):
        finra.fetch_short_interest([f"S{i}" for i in range(9)], asof=ASOF, client=_client())
    with pytest.raises(RateLimited):
        finra.fetch_short_interest(["NVDA"], asof=ASOF, client=_client(status=429))
    seen: list[httpx.Request] = []
    with pytest.raises(DataError):
        finra.fetch_short_interest(["NVDA"], asof=ASOF, client=_client(status=503, seen=seen))
    assert len(seen) == finra.RETRIES + 1
    with pytest.raises(DataError):
        finra.fetch_short_interest(["NVDA"], asof=ASOF, client=_client(body={"not": "a list"}))
    assert finra.fetch_short_interest([], asof=ASOF) == {}
