"""OPT-IN live canaries for the stock data layer: real SEC EDGAR and Wikipedia requests (public data).

Never in CI (marker `live`) and skipped unless COUNCIL_LIVE_CANARY=1. The SEC user agent comes from
COUNCIL_SEC_USER_AGENT or the Keychain item `council-book.sec-user-agent` (never printed):

    COUNCIL_LIVE_CANARY=1 uv run pytest -m live tests/stocks/test_live_canary.py
"""

from __future__ import annotations

import os
from datetime import date

import pandas as pd
import pytest

from council.data.credentials import MissingCredential, sec_user_agent
from council.stocks import pit
from council.stocks.sec import SecClient
from council.stocks.universe import (
    cross_check,
    fetch_membership,
    index_constitution_membership,
    is_foreign_filer,
    sector_of,
    ticker_map,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("COUNCIL_LIVE_CANARY") != "1", reason="opt-in: set COUNCIL_LIVE_CANARY=1"),
]


@pytest.fixture
def sec(monkeypatch):
    monkeypatch.setenv("COUNCIL_MODE", "dry_run")         # lets credentials read the Keychain item
    try:
        sec_user_agent()
    except MissingCredential:
        pytest.skip("no SEC user agent configured")
    with SecClient() as client:
        yield client


def test_sec_edgar_canary(sec):
    tickers = ticker_map(sec.company_tickers(refresh=True))
    assert tickers["AAPL"].cik == 320193 and tickers["BRK_B"].cik == 1067983
    sub = sec.submissions(320193, refresh=True)
    assert sector_of(sub["sic"]) == "BusEq" and not is_foreign_filer(sub)
    assert is_foreign_filer(sec.submissions(937966, refresh=True))          # ASML files 20-F
    doc = sec.companyfacts(320193, refresh=True)
    rows, stats = pit.fundamentals_comparable("320193", "320193", doc)
    assert stats["taxonomy"] == "us-gaap" and len(rows) > 20
    today = pd.Timestamp(date.today())
    feats = pit.comparable_features(rows[rows["available_at"] < today].assign(ticker="X"), "X", today)
    assert all(pd.notna(feats[k]) for k in pit.FUNDAMENTAL_COLUMNS)
    assert (today - feats["latest_available_at"]).days <= 120
    assert sec.requests <= 5


def test_wikipedia_membership_canary():
    sp = fetch_membership("sp500", refresh=True)
    ndx = fetch_membership("nasdaq100", refresh=True)
    assert {"AAPL", "MSFT", "BRK.B"} <= set(sp.symbols) and {"NVDA", "MSFT"} <= set(ndx.symbols)
    assert (pd.Timestamp.now(tz="UTC") - pd.Timestamp(sp.as_of)).days <= 120
    past = fetch_membership("sp500", asof=date(2026, 8, 20))
    assert past.as_of.date() <= date(2026, 8, 20) and len(past.symbols) >= 480
    other = index_constitution_membership("sp500")
    if other is not None:
        assert cross_check(sp, other).overlap > 0.9
