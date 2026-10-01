"""council.stocks.universe: line ids, SEC identity, sectors through the frozen map, share-class
dedupe, price facts, Wikipedia membership (parsing, as-of revisions, freshness stamps), the AI list,
and the live assembly of the rank's inputs against a fake EDGAR."""

from __future__ import annotations

import copy
import json
import sys
import types
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import numpy as np
import pandas as pd
import pytest

from council.paths import REPO_ROOT
from council.stocks import rank as rank_mod
from council.stocks import universe as U
from council.stocks.sec import SecClient, TickerRow
from council.stocks.universe import (
    Candidate,
    Membership,
    MembershipError,
    PriceFacts,
    build_rank_inputs,
    cross_check,
    dedupe_by_cik,
    fetch_membership,
    is_foreign_filer,
    load_ai_list,
    normalise_id,
    parse_constituents,
    price_facts_from_bars,
    sector_of,
    ticker_map,
)
from tests.stocks.conftest import UA, FakeClock, FakeEdgar, submissions_doc

FIXTURE = json.loads((REPO_ROOT / "tests" / "fixtures" / "sec" / "companyfacts_trimmed.json").read_text())


# ------------------------------------------------------------------------------------ identity


@pytest.mark.parametrize("raw", ["BRK.B", "BF-B", "BRK/B", "brk.b", " BRK_B ", "BRK B"])
def test_class_separators_become_underscore(raw):
    expected = "BF_B" if raw.startswith("BF") else "BRK_B"
    assert normalise_id(raw) == expected


@pytest.mark.parametrize("raw", ["F", "T", "V", "C", "NVDA", "GOOGL"])
def test_plain_and_single_letter_tickers_pass(raw):
    assert normalise_id(raw) == raw


@pytest.mark.parametrize("raw", ["", "BRK.", ".B", "BRK..B", "UNMAPPED_1", "UNMAPPEDX", "TOOLONGTICKER",
                                 "STK:NVDA", "A$B", None, 7])
def test_invalid_line_ids_are_refused(raw):
    with pytest.raises(ValueError):
        normalise_id(raw)
    assert U.try_normalise_id(raw) is None


def test_ticker_map_keys_sec_tickers_by_line_id_first_row_wins():
    rows = [TickerRow(1067983, "BRK-B", "Berkshire B"), TickerRow(1067983, "BRK-A", "Berkshire A"),
            TickerRow(14693, "BF-B", "Brown-Forman"), TickerRow(999, "BRK.B", "Impostor"),
            TickerRow(5, "WAY-TOO-LONG-TICKER", "x")]
    m = ticker_map(rows)
    assert m["BRK_B"].cik == 1067983 and m["BF_B"].cik == 14693 and "BRK_A" in m
    assert len(m) == 3


def test_sector_through_the_frozen_ff12_map():
    assert sector_of("3674") == "BusEq"
    assert sector_of(6021) == U.MONEY == "Money"
    assert sector_of(6798) == "Money"                 # REITs are Money too
    assert sector_of(9999) == "Other"
    assert sector_of(None) is None and sector_of("0") is None and sector_of("n/a") is None


@pytest.mark.parametrize(("forms", "foreign"), [
    (("6-K", "20-F", "10-Q"), True), (("6-K", "40-F/A"), True), (("10-Q", "10-K", "20-F"), False),
    (("8-K", "10-KT"), False), (("8-K", "S-1"), False), ((), False)])
def test_foreign_filer_by_latest_annual_form(forms, foreign):
    assert is_foreign_filer(submissions_doc(1, "3674", forms=forms)) is foreign


def _cand(key: str, cik: int | None, dv: float | None = None, **kw) -> Candidate:
    return Candidate(key=key, symbol=key, cik=cik, dollar_volume=dv, **kw)


def test_one_security_per_cik_the_more_liquid_wins():
    rows = [_cand("GOOG", 1652044, 2e9), _cand("GOOGL", 1652044, 3e9), _cand("MSFT", 789019, 1e9),
            _cand("FOX", 1754301, None), _cand("FOXA", 1754301, 5e7), _cand("NWS", 1564708, 1e7),
            _cand("NWSA", 1564708, 1e7)]
    kept, dropped = dedupe_by_cik(rows)
    assert [c.key for c in kept] == ["GOOGL", "MSFT", "FOXA", "NWS"]      # a tie goes to the smaller key
    assert {c.key for c in dropped} == {"GOOG", "FOX", "NWSA"}
    with pytest.raises(ValueError):
        dedupe_by_cik([_cand("X", None)])


def test_price_facts_use_only_bars_on_or_before_the_rank_date():
    idx = pd.date_range("2025-01-02", periods=120, freq="B", tz="UTC")
    bars = pd.DataFrame({"close": np.arange(1.0, 121.0), "volume": np.full(120, 10.0)}, index=idx)
    asof = idx[99].date()
    pf = price_facts_from_bars(bars, asof)
    assert pf.first_bar == pd.Timestamp("2025-01-02") and pf.last_bar == pd.Timestamp(idx[99].date())
    assert pf.dollar_volume == pytest.approx(np.median(np.arange(38.0, 101.0)) * 10.0)   # closes 38..100: the last 63 up to D
    assert price_facts_from_bars(bars, "2024-12-31") == PriceFacts()
    assert price_facts_from_bars(None, asof) == PriceFacts()
    naive = bars.tz_convert(None)
    assert price_facts_from_bars(naive, asof) == pf


# ------------------------------------------------------------------------------------ membership

SP_WIKITEXT = """Intro text {{Short description|x}}
{| class="wikitable" id="other"
! Symbol !! Something
|-
| ZZZZ || not the constituents table
|}
{| class="wikitable sortable sticky-header" id="constituents"
|-
! [[Ticker symbol|Symbol]] !! Security !! GICS Sector !! Date added !! {{Abbr|CIK|Central Index Key}}<ref>SEC</ref> !! Founded
|-
|{{NyseSymbol|MMM}}
|[[3M]]
|Industrials
|1957-03-04
|0000066740
|1902
|-
| {{NasdaqSymbol|AAPL}} || [[Apple Inc.]] || Information Technology || 1982-11-30 || 0000320193 || 1977
|-
| style="background:#eee" | [https://www.nyse.com/quote/XNYS:BRK.B BRK.B] || [[Berkshire Hathaway]] || Financials || 2010-02-16 || 0001067983 || 1839
|-
|'''BF.B'''<ref>class B</ref> || [[Brown-Forman]] || Consumer Staples || 1982-10-31 || 0000014693 || 1870
|-
| {{NyseSymbol|F}} || [[Ford Motor Company|Ford]] || Consumer Discretionary || 1957-03-04 || 37996 || 1903
|-
| {{NyseSymbol|MMM}} || duplicate row || x || x || 66740 || x
|-
| not a ticker!! || x || x || x || x || x
|}
"""

NDX_WIKITEXT = """{| class="wikitable sortable"
|-
! Company !! Ticker !! GICS Sector
|-
| [[Adobe Inc.]] || ADBE || Information Technology
|-
| [[Alphabet Inc.]] (Class A) || GOOGL || Communication Services
|-
| [[Alphabet Inc.]] (Class C) || GOOG || Communication Services
|}
"""


def test_parse_the_constituents_table_symbols_and_ciks():
    symbols, ciks = parse_constituents(SP_WIKITEXT)
    assert symbols == ["MMM", "AAPL", "BRK.B", "BF.B", "F"]
    assert ciks == {"MMM": 66740, "AAPL": 320193, "BRK.B": 1067983, "BF.B": 14693, "F": 37996}


ONE_CELL_PER_LINE = """{{sticky header}}
{| class="wikitable sortable mw-collapsible sticky-header" id="constituents"
|-
![[Ticker symbol|Symbol]]
! Security !! [[Global Industry Classification Standard|GICS]] Sector !! [[Central Index Key|CIK]] !! Founded
|-
|| {{NyseSymbol|MMM}}
|| [[3M]]
|| Industrials
|| 0000066740
|| 1902
|-
|| {{NasdaqSymbol|AMD}}
|| [[AMD|Advanced Micro Devices]]
|| Information Technology
|| 0000002488
|| 1969
|-
|| {{NyseSymbol|BRK.B}}
|| [[Berkshire Hathaway]]
|| Financials
|| 0001067983
|| 1839
|}
"""

NDX_LIST = """{| class="wikitable sortable" id="constituents"
|-
! Ticker !! Company !! [[Industry Classification Benchmark|ICB]] Industry<ref name=":1">{{Cite web |date=2020 |title=A |url=https://example.org/a}}</ref>!! [[Industry Classification Benchmark|ICB]] Subsector<ref name=":1" />
|-
| ADBE || [[Adobe Inc.]] || Technology || Software
|-
| GOOGL || [[Alphabet Inc.]] (Class A) || Technology || Consumer Digital Services
|}
"""


def test_parse_the_live_table_layouts():
    """The S&P 500 page writes each cell on its own line after `||` under a two-line header; the
    NASDAQ-100 list page writes a row per line with a reference inside a header cell."""
    assert parse_constituents(ONE_CELL_PER_LINE) == (["MMM", "AMD", "BRK.B"],
                                                     {"MMM": 66740, "AMD": 2488, "BRK.B": 1067983})
    assert parse_constituents(NDX_LIST) == (["ADBE", "GOOGL"], {})
    assert U.MEMBERSHIP_PAGES == {"sp500": "List of S&P 500 companies", "nasdaq100": "List of NASDAQ-100 companies",
                                  "sp400": "List of S&P 400 companies", "sp600": "List of S&P 600 companies"}
    assert U.INDEXES == ("sp500", "nasdaq100")         # the sleeve; the swing screen reads SWING_INDEXES


def test_parse_falls_back_to_the_table_with_a_ticker_column():
    symbols, ciks = parse_constituents(NDX_WIKITEXT)
    assert symbols == ["ADBE", "GOOGL", "GOOG"] and ciks == {}
    assert parse_constituents("no tables here") == ([], {})


def _api_payload(content: str, *, revid: int = 42, ts: str = "2026-09-20T10:00:00Z") -> dict:
    return {"batchcomplete": True, "query": {"pages": [{"pageid": 1, "ns": 0, "title": "List of S&P 500 companies",
                                                         "revisions": [{"revid": revid, "timestamp": ts,
                                                                        "slots": {"main": {"content": content}}}]}]}}


def _big_table(n: int) -> str:
    rows = "\n".join(f"|-\n| {{{{NyseSymbol|T{chr(65 + i // 26)}{chr(65 + i % 26)}}}}} || Co {i}" for i in range(n))
    return '{| class="wikitable" id="constituents"\n! Symbol !! Security\n' + rows + "\n|}\n"


def test_fetch_membership_current_and_as_of_revision_with_cache():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_api_payload(_big_table(503)))

    transport = httpx.MockTransport(handler)
    m = fetch_membership("sp500", transport=transport)
    assert len(m.symbols) == 503 and m.revision == 42 and m.as_of == datetime(2026, 9, 20, 10, tzinfo=UTC)
    assert m.stamp() == U.SourceStamp("mediawiki:sp500", date(2026, 9, 20))
    assert "CC BY-SA" in m.attribution and "revision 42" in m.attribution
    ua = calls[0].headers["user-agent"]
    assert "@" not in ua and "council-book" in ua                  # a project URL, never an e-mail address
    fetch_membership("sp500", transport=transport)
    assert len(calls) == 1                                         # cached
    fetch_membership("sp500", transport=transport, refresh=True)
    assert len(calls) == 2
    fetch_membership("sp500", asof=date(2026, 8, 20), transport=transport)
    q = parse_qs(calls[-1].url.query.decode())
    assert q["rvstart"] == ["2026-08-20T23:59:59Z"] and q["rvdir"] == ["older"] and q["rvlimit"] == ["1"]
    assert "rvstart" not in parse_qs(calls[0].url.query.decode())


def test_fetch_membership_refuses_implausible_tables_and_bad_payloads():
    few = httpx.MockTransport(lambda r: httpx.Response(200, json=_api_payload(_big_table(60))))
    with pytest.raises(MembershipError, match="60 members"):
        fetch_membership("sp500", transport=few)
    broken = httpx.MockTransport(lambda r: httpx.Response(200, json={"query": {"pages": [{"missing": True}]}}))
    with pytest.raises(MembershipError, match="unexpected"):
        fetch_membership("nasdaq100", transport=broken)
    with pytest.raises(ValueError):
        fetch_membership("dow30")


def test_cross_check_compares_by_line_id():
    now = datetime(2026, 9, 1, tzinfo=UTC)
    a = Membership("sp500", ("AAPL", "BRK.B", "MMM"), now, "mediawiki")
    b = Membership("sp500", ("AAPL", "BRK-B", "XYZ"), now, "index_constitution")
    c = cross_check(a, b)
    assert c.only_primary == ("MMM",) and c.only_secondary == ("XYZ",) and c.overlap == pytest.approx(2 / 4)


def test_index_constitution_is_optional(monkeypatch):
    monkeypatch.setitem(sys.modules, "index_constitution", None)          # not installed
    assert U.index_constitution_membership("sp500") is None
    fake = types.SimpleNamespace(
        latest=lambda index: pd.DataFrame({"symbol": ["AAPL", "BRK.B"], "name": ["a", "b"]}),
        history=lambda index: pd.DataFrame({"opt-in": pd.to_datetime(["1982-11-30", "2026-06-23"]),
                                            "opt-out": pd.to_datetime([None, None])}))
    monkeypatch.setitem(sys.modules, "index_constitution", fake)
    m = U.index_constitution_membership("sp500")
    assert m.symbols == ("AAPL", "BRK.B") and m.as_of.date() == date(2026, 6, 23) and m.source == "index_constitution"


# ------------------------------------------------------------------------------------ the AI list


def test_ai_list_keeps_on_as_a_ticker_and_reads_the_lab_layout(tmp_path: Path):
    flat = tmp_path / "flat.yaml"
    flat.write_text("# hindsight list\ntickers: [NVDA, ON, YES, NO, BRK.B, NVDA]\n")
    assert load_ai_list(flat) == ("BRK.B", "NO", "NVDA", "ON", "YES")
    lab = tmp_path / "lab.yaml"
    lab.write_text("peer_groups:\n  semis:\n    tickers: [NVDA, ON]\n  software: [PLTR, SNOW]\n")
    assert load_ai_list(lab) == ("NVDA", "ON", "PLTR", "SNOW")
    bad = tmp_path / "bad.yaml"
    bad.write_text("tickers: [NVDA, 'UNMAPPED_1']\n")
    with pytest.raises(ValueError, match="UNMAPPED_1"):
        load_ai_list(bad)
    empty = tmp_path / "empty.yaml"
    empty.write_text("tickers: []\n")
    with pytest.raises(ValueError):
        load_ai_list(empty)


# ------------------------------------------------------------------------------------ assembly


def _facts(label: str, cik: int) -> dict:
    doc = copy.deepcopy(FIXTURE["companies"][label]["companyfacts"])
    doc["cik"] = cik
    return doc


def _edgar() -> FakeEdgar:
    return FakeEdgar(
        tickers=[{"cik_str": 46080, "ticker": "HAS", "title": "Hasbro"},
                 {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"},
                 {"cik_str": 1413329, "ticker": "PM", "title": "Philip Morris"},
                 {"cik_str": 1067983, "ticker": "BRK-B", "title": "Berkshire"},
                 {"cik_str": 937966, "ticker": "ASML", "title": "ASML"}],
        submissions={46080: submissions_doc(46080, "3944"), 789019: submissions_doc(789019, "7372"),
                     1413329: submissions_doc(1413329, "2111"), 1067983: submissions_doc(1067983, "6331"),
                     937966: submissions_doc(937966, "3559", forms=("6-K", "20-F")),
                     2488: submissions_doc(2488, "3674")},
        companyfacts={46080: _facts("HAS", 46080), 789019: _facts("MSFT", 789019), 1413329: _facts("PM", 1413329)},
    )


def _price(first: str = "2010-01-04", last: str = "2018-12-31", dv: float = 1e9) -> PriceFacts:
    return PriceFacts(pd.Timestamp(first), pd.Timestamp(last), dv)


def test_build_rank_inputs_end_to_end_with_forced_companyfacts_refresh():
    edgar = _edgar()
    clock = FakeClock()
    stamp = datetime(2018, 12, 28, tzinfo=UTC)
    sp = Membership("sp500", ("HAS", "MSFT", "BRK.B", "PM", "GONE"), stamp, "mediawiki", revision=1,
                    ciks={"MSFT": 789019, "PM": 1111})
    ndx = Membership("nasdaq100", ("MSFT", "ASML", "AMD"), stamp, "mediawiki", revision=2, ciks={"AMD": 2488})
    prices = {k: _price() for k in ("HAS", "MSFT", "BRK_B", "PM", "ASML", "AMD")}
    with SecClient(UA, transport=edgar.transport(), clock=clock, sleep=clock.sleep) as client:
        inputs = build_rank_inputs(date(2019, 1, 2), memberships=[sp, ndx], sec=client, price_facts=prices,
                                   ai_symbols=("MSFT", "ON"), held=("MSFT",))
        by = {c.key: c for c in inputs.candidates}
        assert set(by) == {"HAS", "MSFT", "BRK_B", "PM", "ASML", "AMD"}
        assert by["MSFT"].sources == frozenset({"sp500", "nasdaq100", "ai"}) and not by["MSFT"].ai_only
        assert by["BRK_B"].symbol == "BRK.B" and by["BRK_B"].line_id == "BRK_B" and by["BRK_B"].cik == 1067983
        assert by["AMD"].cik == 2488 and by["AMD"].name == ""          # the table's CIK when SEC has no ticker
        assert by["ASML"].is_adr and not by["HAS"].is_adr
        assert dict(inputs.unmapped) == {"GONE": "no_cik", "ON": "no_cik"}
        assert inputs.notes == ("cik_from_table:AMD:2488", "cik_mismatch:PM:sec=1413329:table=1111")
        assert by["PM"].cik == 1413329                                 # SEC's mapping wins
        assert inputs.sources == (U.SourceStamp("mediawiki:sp500", date(2018, 12, 28)),
                                  U.SourceStamp("mediawiki:nasdaq100", date(2018, 12, 28)))
        facts_paths = [p for p in edgar.paths() if "companyfacts" in p]
        assert sorted(facts_paths) == ["/api/xbrl/companyfacts/CIK0000002488.json",   # AMD: 404 -> no facts
                                       "/api/xbrl/companyfacts/CIK0000046080.json",
                                       "/api/xbrl/companyfacts/CIK0000789019.json",
                                       "/api/xbrl/companyfacts/CIK0001413329.json"]   # not Money, not foreign
        assert inputs.taxonomy == {46080: "us-gaap", 789019: "us-gaap", 1413329: "us-gaap", 2488: "no_companyfacts"}
        assert inputs.sic[1067983] == "6331"
        build_rank_inputs(date(2019, 1, 2), memberships=[sp], sec=client, price_facts=prices)
        assert len([p for p in edgar.paths() if "companyfacts" in p]) == 4 + 3          # refetched: forced
        assert edgar.paths().count("/files/company_tickers.json") == 2

    result = rank_mod.rank(date(2019, 1, 2), inputs, rank_mod.RankConfig(n=2, shortlist_size=1, min_peer_group=1))
    assert result.excluded["BRK_B"] == "excluded_sector" and result.excluded["ASML"] == "not_common"
    assert result.excluded["AMD"] == "no_companyfacts"
    assert result.funnel["member"] == 8 and result.funnel["mapped"] == 6
    visible = result.eligible.index.tolist() + [k for k, r in result.excluded.items()
                                                if r in ("missing_feature", "revenue_floor", "implausible", "stale_filing")]
    assert {"HAS", "MSFT", "PM"} <= set(visible)
    assert all(result.eligible.loc[k, "latest_available_at"] < pd.Timestamp("2019-01-02") for k in result.eligible.index)
