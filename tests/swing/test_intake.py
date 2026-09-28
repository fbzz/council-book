"""SW-1: market-wide SEC intake for the Scout (design §1.2): an 8-K for a non-held ticker reaches the
reading list with its item codes and official titles; priority items first; quota; admission at
the slot; broker-feed pass-through; the reading list's quotas. Recorded Atom fixtures only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from council.data.http import DataError
from council.models.facts import NewsItem
from council.stocks.sec import TickerRow
from council.stocks.sec_news import parse_current_atom
from council.swing import intake

FEEDS = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "feeds"
NOW = datetime(2026, 9, 25, 22, 40, tzinfo=UTC)
ATOM = {f: parse_current_atom((FEEDS / f"sec-current-{f}.xml").read_bytes()) for f in ("8-K", "6-K")}
ALL = [f for fs in ATOM.values() for f in fs]


def _fetch(_client, form):
    return list(ATOM[form])


def _tickers(extra=()):
    rows = [TickerRow(98222, "TDW", "TIDEWATER"), TickerRow(98222, "TDW-WS", "TIDEWATER"),
            *extra]
    return intake.cik_tickers(rows)


def test_non_held_8k_reaches_the_list_with_item_codes():
    result, admitted = intake.sec_market_wide(None, _tickers(), now=NOW, fetch=_fetch)
    tdw = [i for i in result.items if i.symbols == ["TDW"]]
    assert tdw, "Tidewater's 8-K must reach the reading list"
    item = tdw[0]
    assert item.id.startswith("P:") and item.form == "8-K" and item.items == ["1.01", "2.03", "9.01"]
    assert "Item 1.01 Entry into a Material Definitive Agreement" in item.title
    meta = intake.catalyst_item(item)
    assert meta.items == ("1.01", "2.03", "9.01") and meta.titles[0].startswith("Entry into")
    assert admitted and intake.catalyst_filings(admitted, _tickers())[0][0] == "TDW"


def test_priority_first_then_newest_and_quota():
    ciks = {f.cik for f in ALL}
    rows = [TickerRow(c, f"T{i:04d}".replace("0", "A"), "x") for i, c in enumerate(sorted(ciks))]
    tickers = intake.cik_tickers(rows)
    result, admitted = intake.sec_market_wide(None, tickers, now=NOW, fetch=_fetch, quota=25)
    assert len(result.items) == 25 and result.report.dropped["quota"] == len(admitted) - 25
    pri = [any(c in intake.PRIORITY_ITEMS for c in i.items) for i in result.items]
    assert pri == sorted(pri, reverse=True)                     # priority items lead
    lead = [i for i, p in zip(result.items, pri, strict=True) if p]
    assert [i.available_at for i in lead] == sorted((i.available_at for i in lead), reverse=True)


def test_slot_admission_and_lookback():
    early = datetime(2026, 9, 25, 21, 0, tzinfo=UTC)
    result, _ = intake.sec_market_wide(None, _tickers(), now=NOW, slot=early, fetch=_fetch)
    assert all(i.available_at < early for i in result.items)
    assert not [i for i in result.items if i.symbols == ["TDW"]]     # accepted 21:29, after the slot
    assert result.report.dropped.get("after_slot", 0) >= 1


def test_feed_error_is_flagged_not_raised():
    def broken(_c, form):
        if form == "8-K":
            raise DataError("sec current 8-K: HTTP 503")
        return list(ATOM[form])
    result, _ = intake.sec_market_wide(None, _tickers(), now=NOW, fetch=broken)
    assert any(f.startswith("news_source_error:sec:") for f in result.flags)
    assert result.report.feeds_failed == 1 and result.report.feeds_ok == 1


def _item(i: int, prefix: str, age_h: float, source: str) -> NewsItem:
    at = NOW - timedelta(hours=age_h)
    hexid = f"{i:08x}"
    return NewsItem(id=f"{prefix}:{hexid}", title=f"t{i}", published_at=at, available_at=at, source=source,
                    licence="broker_licensed" if prefix == "N" else "public_domain")


def test_feed_pass_through_and_reading_list_quotas():
    feed = [_item(i, "N", i * 0.5, "etoro_feed") for i in range(40)] + [_item(99, "N", 50, "etoro_feed")]
    passed = intake.feed_pass_through(feed, slot=NOW)
    assert len(passed) == intake.FEED_QUOTA and all(i.id.startswith("N:") for i in passed)
    gov = [_item(1000 + i, "P", i + 0.1, "fed_board") for i in range(12)]
    sec = [_item(2000 + i, "P", i * 0.3, "sec") for i in range(30)]
    out = intake.reading_list(sec, feed, gov, slot=NOW)
    assert len(out) <= intake.READING_MAX
    assert sum(1 for i in out if i.source == "fed_board") <= intake.GOV_QUOTA
    assert [i.available_at for i in out] == sorted((i.available_at for i in out), reverse=True)
    assert all(NOW - timedelta(hours=48) <= i.available_at < NOW for i in out)
