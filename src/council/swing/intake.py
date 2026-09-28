"""Market-wide news intake for the Scout (design swing-book.md rev 2, §1.2; SW-1).

The Scout reads FREELY: the whole market-wide reading list, not only held names.

- `sec_market_wide`: SEC's current 8-K / 6-K Atom feeds (`council.stocks.sec_news.fetch_current` and
  `parse_current_atom`) WITHOUT the stock-line filter. A filing is kept when its CIK maps to a
  ticker in SEC's `company_tickers.json` (the first ticker SEC lists for the CIK; line-id spelling,
  `BRK_B`). Priority items (1.01, 2.02, 5.02, 7.01, 8.01) first, then newest first; quota
  SEC_QUOTA. Each item keeps its form, item codes and OFFICIAL item titles (never the filer's
  words), so code can attach them to an idea that cites it (`catalyst_items`, §1.4). Ids `P:`.
- `feed_pass_through`: the broker feed's items (`N:`, private text, never published), newest first,
  quota FEED_QUOTA.
- `reading_list`: SEC + feed + gov news (quota GOV_QUOTA), lookback 48 h, newest first, at most
  READING_MAX items.

Admission (`available_at` before the slot, within the lookback), cleaning and the leak scan are
exactly `council.data.gov_news`'s. One `SecClient` (the caller's, so SEC's <= 7 requests a second is
shared); the news timeouts and the caller's deadline bound every request (two feed requests).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from council.data import gov_news
from council.data.gov_news import LOOKBACK, SourceResult, admit_time, clean_field, trips_leak_scan
from council.data.http import DataError
from council.models.facts import NewsItem, public_news_id
from council.stocks import sec_news
from council.stocks.sec import TickerRow
from council.stocks.sec_news import CURRENT_FORMS, ITEM_TITLES, Filing, filing_link, filing_title
from council.stocks.universe import try_normalise_id

SOURCE = "sec"
SEC_QUOTA = 25
FEED_QUOTA = 25
GOV_QUOTA = 8
READING_MAX = 60
PRIORITY_ITEMS = ("1.01", "2.02", "5.02", "7.01", "8.01")


@dataclass(frozen=True)
class CatalystItem:
    """The code-attached, publishable metadata of a cited SEC item (never model text)."""

    id: str
    form: str
    items: tuple[str, ...]
    titles: tuple[str, ...]


def cik_tickers(rows: Iterable[TickerRow]) -> dict[int, str]:
    """{CIK: line id of the first usable ticker SEC lists for it}."""
    out: dict[int, str] = {}
    for row in rows:
        lid = try_normalise_id(row.ticker)
        if lid is not None:
            out.setdefault(int(row.cik), lid)
    return out


def market_item(filing: Filing, line_id: str, *, now: datetime, slot: datetime, result: SourceResult,
                lookback: timedelta = LOOKBACK) -> NewsItem | None:
    """One filing of any SEC filer with a ticker -> NewsItem, or None (the drop is counted)."""
    stamp = gov_news.Stamp(published_at=filing.accepted_at.astimezone(UTC),
                           available_at=filing.accepted_at.astimezone(UTC))
    reason = admit_time(stamp, now=now, slot=slot, lookback=lookback)
    if reason is not None:
        result.drop(reason)
        return None
    title = clean_field(filing_title(filing.form, filing.items), gov_news.TITLE_MAX)
    summary = clean_field(filing.company, gov_news.SUMMARY_MAX)
    if trips_leak_scan(title, summary):
        result.drop("leak")
        return None
    link, dropped = gov_news.public_link(filing_link(line_id.replace("_", "-"), filing.form))
    if dropped:
        assert result.report is not None
        result.report.links_dropped += 1
        result.flag(f"news_link_dropped:{SOURCE}")
    return NewsItem(
        id=public_news_id(SOURCE, filing.accession), title=title, summary=summary, symbols=[line_id],
        published_at=stamp.published_at, available_at=stamp.available_at, source=SOURCE,
        licence="public_domain", link=link, form=filing.form,  # type: ignore[arg-type]
        items=list(filing.items),
    )


def _priority(filing: Filing) -> int:
    return 0 if any(code in PRIORITY_ITEMS for code in filing.items) else 1


def sec_market_wide(
    sec: Any,
    tickers: Mapping[int, str],
    *,
    now: datetime,
    slot: datetime | None = None,
    quota: int = SEC_QUOTA,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    lookback: timedelta = LOOKBACK,
    fetch: Callable[[Any, str], list[Filing]] = sec_news.fetch_current,
) -> tuple[SourceResult, list[Filing]]:
    """(the Scout's SEC items, every admitted filing with a ticker) at `slot` (default `now`). The
    second list feeds the screen's catalyst-but-unmoved list; it is not quota-capped."""
    started = monotonic()
    cut = slot if slot is not None else now
    result = SourceResult(SOURCE)
    assert result.report is not None
    found: dict[str, Filing] = {}
    for form in CURRENT_FORMS:
        if deadline is not None and monotonic() >= deadline:
            result.flag(f"news_source_error:{SOURCE}:budget")
            break
        try:
            filings = fetch(sec, form)
        except (DataError, httpx.HTTPError) as exc:
            result.report.feeds_failed += 1
            result.report.error = gov_news.error_type(exc)
            result.flag(f"news_source_error:{SOURCE}:{result.report.error}")
            continue
        result.report.feeds_ok += 1
        result.report.entries += len(filings)
        for f in filings:
            if f.cik in tickers:
                held = found.get(f.accession)
                if held is None or f.accepted_at > held.accepted_at:
                    found[f.accession] = f
    admitted: list[tuple[Filing, NewsItem]] = []
    for f in sorted(found.values(), key=lambda f: (f.accepted_at, f.accession), reverse=True):
        item = market_item(f, tickers[f.cik], now=now, slot=cut, result=result, lookback=lookback)
        if item is not None:
            admitted.append((f, item))
    ranked = sorted(admitted, key=lambda fi: (_priority(fi[0]), -fi[0].accepted_at.timestamp(), fi[0].accession))
    for _f, item in ranked[:max(int(quota), 0)]:
        result.items.append(item)
        result.report.kept += 1
    if len(ranked) > quota:
        result.report.dropped["quota"] = len(ranked) - quota
    result.report.seconds = round(monotonic() - started, 3)
    return result, [f for f, _ in admitted]


def catalyst_filings(filings: Iterable[Filing], tickers: Mapping[int, str]) -> list[tuple[str, datetime]]:
    """(line id, acceptance time) pairs for the screen's catalyst list."""
    return [(tickers[f.cik], f.accepted_at) for f in filings if f.cik in tickers]


def catalyst_item(item: NewsItem) -> CatalystItem:
    """The code-attached metadata of an SEC item (form, item codes, official titles)."""
    codes = tuple(item.items)
    return CatalystItem(id=item.id, form=str(item.form or ""), items=codes,
                        titles=tuple(ITEM_TITLES.get(c, f"Item {c}") for c in codes))


def _newest(items: Iterable[NewsItem]) -> list[NewsItem]:
    return sorted(items, key=lambda i: (i.available_at, i.id), reverse=True)


def feed_pass_through(items: Iterable[NewsItem], *, slot: datetime, quota: int = FEED_QUOTA,
                      lookback: timedelta = LOOKBACK) -> list[NewsItem]:
    """Broker-feed items (`N:` only) available before `slot` within the lookback, newest first."""
    ok = [i for i in items if i.id.startswith("N:") and slot - lookback <= i.available_at < slot]
    return _newest(ok)[:quota]


def reading_list(sec: Sequence[NewsItem], feed: Sequence[NewsItem], gov: Sequence[NewsItem], *,
                 slot: datetime, lookback: timedelta = LOOKBACK) -> list[NewsItem]:
    """The Scout's reading list: each source within its quota, admitted at `slot`, newest first,
    at most READING_MAX, one item per id."""
    def admitted(items: Iterable[NewsItem]) -> list[NewsItem]:
        return _newest(i for i in items if slot - lookback <= i.available_at < slot)

    chosen = admitted(sec)[:SEC_QUOTA] + feed_pass_through(feed, slot=slot, lookback=lookback) \
        + admitted(gov)[:GOV_QUOTA]
    seen: dict[str, NewsItem] = {}
    for item in chosen:
        seen.setdefault(item.id, item)
    return _newest(seen.values())[:READING_MAX]
