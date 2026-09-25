from __future__ import annotations

import httpx

from council.data.calendar import load_events, macro_events, release_time_utc
from tests.data.synth import fixture, utc

START, END = utc(2026, 10, 1), utc(2026, 12, 31)


def test_fomc_only_without_fred_key(policy):
    events, flags = load_events(policy, START, END)
    assert [(e.id, e.kind, e.severity) for e in events] == [
        ("E:fomc@2026-10-28", "fomc", 3),
        ("E:fomc@2026-12-09", "fomc", 3),
    ]
    assert events[0].at_utc == utc(2026, 10, 28, 18, 0) and events[0].symbols == []
    assert flags == ["calendar:release_dates_skipped_no_fred_key"]
    assert macro_events(policy, START, utc(2026, 11, 1)) == events[:1]      # window respected


def test_release_dates_with_key(policy, mock_client):
    requested = []

    def handler(request: httpx.Request):
        requested.append(dict(request.url.params))
        rid = request.url.params["release_id"]
        if rid == "10":
            return httpx.Response(200, json=fixture("fred_release_dates_cpi.json"))
        if rid == "50":
            return httpx.Response(200, json={"release_dates": [{"release_id": 50, "date": "2026-11-06"}]})
        return httpx.Response(200, json={"release_dates": [{"release_id": 54, "date": "2026-10-30"}]})

    events, flags = load_events(policy, START, END, fred_key="k3y", client=mock_client(handler))
    assert flags == []
    by_id = {e.id: e for e in events}
    assert set(by_id) == {
        "E:fomc@2026-10-28", "E:fomc@2026-12-09", "E:cpi@2026-10-14", "E:cpi@2026-11-12",
        "E:cpi@2026-12-10", "E:nfp@2026-11-06", "E:pce@2026-10-30",
    }                                                                 # Sep 11 CPI outside window
    assert by_id["E:cpi@2026-10-14"].severity == 3 and by_id["E:nfp@2026-11-06"].severity == 2
    assert by_id["E:cpi@2026-10-14"].at_utc == utc(2026, 10, 14, 12, 30)   # 08:30 EDT
    assert by_id["E:cpi@2026-12-10"].at_utc == utc(2026, 12, 10, 13, 30)   # 08:30 EST
    assert [e.at_utc for e in events] == sorted(e.at_utc for e in events)
    assert all(p["include_release_dates_with_no_data"] == "true" for p in requested)


def test_failed_release_is_flagged_without_leaking_key(policy, mock_client):
    def handler(request):
        return httpx.Response(500) if request.url.params["release_id"] == "50" else httpx.Response(
            200, json={"release_dates": []}
        )

    events, flags = load_events(policy, START, END, fred_key="k3y", client=mock_client(handler))
    assert flags == ["calendar:release_dates_failed:nfp"]
    assert {e.kind for e in events} == {"fomc"}
    assert not any("k3y" in f for f in flags)


def test_release_time_conversion():
    assert release_time_utc(utc(2026, 7, 15).date()) == utc(2026, 7, 15, 12, 30)
    assert release_time_utc(utc(2026, 1, 13).date()) == utc(2026, 1, 13, 13, 30)
