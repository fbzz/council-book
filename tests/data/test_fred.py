from __future__ import annotations

from datetime import date

import httpx
import pandas as pd
import pytest

from council.data import fred
from council.data.http import DataError
from tests.data.synth import fixture, utc


@pytest.mark.parametrize("name", ["fred_dgs10.csv", "fred_dgs10_classic.csv"])
def test_parse_both_csv_formats_drops_missing(name):
    s = fred.parse_csv(fixture(name), "DGS10")
    assert list(s.index) == [pd.Timestamp(d, tz="UTC") for d in ("2026-09-21", "2026-09-23", "2026-09-24")]
    assert s.iloc[-1] == 4.18 and s.name == "DGS10"


def test_non_csv_responses_raise():
    with pytest.raises(DataError):
        fred.parse_csv("<!DOCTYPE html><html>", "DGS10")
    with pytest.raises(DataError):
        fred.parse_csv("", "DGS10")
    with pytest.raises(DataError):
        fred.parse_csv("observation_date\n2026-01-01\n", "DGS10")


def test_availability_rule_d_plus_one_noon_utc():
    assert fred.available_at(date(2026, 9, 24)) == utc(2026, 9, 25, 12, 0)
    s = fred.parse_csv(fixture("fred_dgs10.csv"), "DGS10")
    assert fred.available_series(s, utc(2026, 9, 25, 12, 0)).index[-1] == pd.Timestamp("2026-09-24", tz="UTC")
    assert fred.available_series(s, utc(2026, 9, 25, 11, 59)).index[-1] == pd.Timestamp("2026-09-23", tz="UTC")


def test_publishable_registry_fails_closed():
    for sid in ("DGS10", "DGS2", "T10Y2Y", "DFF", "DTWEXBGS"):
        assert fred.is_publishable(sid)
    for sid in ("VIXCLS", "BAMLH0A0HYM2", "SP500", "UNKNOWN"):
        assert not fred.is_publishable(sid)
    assert fred.DEFAULT_MACRO == ("DGS10", "DGS2", "T10Y2Y", "DFF", "DTWEXBGS", "VIXCLS")


def test_fetch_is_keyless(mock_client):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        return httpx.Response(200, text=fixture("fred_dgs10.csv"))

    s = fred.fetch_series("DGS10", client=mock_client(handler))
    assert seen["url"] == "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
    assert len(s) == 3
    with pytest.raises(ValueError):
        fred.fetch_series("dgs10&x=1")
