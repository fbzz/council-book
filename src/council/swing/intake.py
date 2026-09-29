"""Market-wide news intake for the Scout (design swing-book.md rev 2, §1.2; SW-1).

The Scout reads FREELY: the whole market-wide reading list, not only held names.

- `sec_market_wide`: SEC's current 8-K / 6-K Atom feeds (`council.stocks.sec_news.fetch_current` and
  `parse_current_atom`) WITHOUT the stock-line filter. A filing is kept when its CIK maps to a
  ticker in SEC's `company_tickers.json` (the first ticker SEC lists for the CIK; line-id spelling,
  `BRK_B`). Ranking: routine filings last (an 8-K / 8-K/A whose items are only 9.01, or 5.07 with
  at most 9.01), then the caller's preferred filers (`prefer`: movers-screen names 0, the screen
  universe 1), then priority items (1.01, 2.02, 5.02, 7.01, 8.01), then newest first; quota
  SEC_QUOTA. Each item keeps its form, item codes and OFFICIAL item titles as its title (never the
  filer's words), so code can attach them to an idea that cites it (`catalyst_items`, §1.4). Ids `P:`.
- Content (`enrich`, `swing.sec_text`): with an enricher the item's summary carries, after the
  company name, the 8-K item text's opening and the EX-99 press release's headline and first
  ~600 characters (public-domain filing text; cleaned by `gov_news.clean_field`, which removes
  money, levels and long numbers, and leak-scanned: a tripped scan keeps only the company name).
  A 6-K without an English headline is dropped (`sec_6k_no_headline`); the enricher's own budget
  bounds the requests per slot.
- `feed_pass_through`: the broker feed's items (`N:`, private text, never published), newest first,
  quota FEED_QUOTA.
- `reading_list`: SEC + feed + gov news (quota GOV_QUOTA), lookback 48 h, newest first, at most
  READING_MAX items.

Admission (`available_at` before the slot, within the lookback), cleaning and the leak scan are
exactly `council.data.gov_news`'s. One `SecClient` (the caller's, so SEC's <= 7 requests a second is
shared); the news timeouts and the caller's deadline bound every request (two feed requests).
"""

from __future__ import annotations

import re
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
ROUTINE_SETS = (frozenset({"9.01"}), frozenset({"5.07", "9.01"}))
TEXT_SUMMARY_MAX = 1200                 # company + item text + exhibit headline + excerpt


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


def routine(filing: Filing) -> bool:
    """An 8-K / 8-K/A with nothing to judge: only exhibits (9.01), or a vote result (5.07)."""
    if filing.form.startswith("6-K"):
        return False
    codes = frozenset(filing.items)
    return not codes or any(codes <= r for r in ROUTINE_SETS)


_US_DATE = re.compile(r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
                      r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2}),?\s+(\d{4})\b")


def day_first(text: str) -> str:
    """"September 25, 2026" -> "25 September 2026": the public cleaner reads "25, 2026" (and a year
    followed by "," or ".") as a level; the year is left out where punctuation follows it."""
    def swap(m: re.Match[str]) -> str:
        nxt = text[m.end():m.end() + 1]
        return f"{m.group(2)} {m.group(1)}" + ("" if nxt in (",", ".") else f" {m.group(3)}")

    return _US_DATE.sub(swap, text)


def text_summary(company: str, text: Any) -> str:
    """The summary of an enriched item: company, then the filing text's parts (plain text)."""
    parts = [company]
    if text is not None:
        if text.item_text:
            parts.append(f"item text: {text.item_text}")
        if text.headline:
            parts.append(f"{text.exhibit or 'report'} headline: {text.headline}")
        if text.excerpt:
            parts.append(f"excerpt: {text.excerpt}")
    return day_first(" | ".join(p for p in parts if p))


def market_item(filing: Filing, line_id: str, *, now: datetime, slot: datetime, result: SourceResult,
                lookback: timedelta = LOOKBACK, text: Any = None) -> NewsItem | None:
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
    if text is not None and not text.empty:
        rich = clean_field(text_summary(filing.company, text), TEXT_SUMMARY_MAX)
        if trips_leak_scan(rich):
            result.flag("sec_text_leak_dropped")
        else:
            summary = rich
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
    enrich: Callable[[Filing], Any] | None = None,
    prefer: Mapping[str, int] | None = None,
) -> tuple[SourceResult, list[Filing]]:
    """(the Scout's SEC items, every admitted filing with a ticker) at `slot` (default `now`). The
    second list feeds the screen's catalyst-but-unmoved list; it is not quota-capped. `enrich`
    (filing -> `sec_text.FilingText` | None) adds content, in rank order, until the quota is
    filled; `prefer` {line id: tier} ranks preferred filers first (module rules)."""
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
    tier = prefer or {}
    ranked = sorted(admitted, key=lambda fi: (routine(fi[0]), tier.get(tickers[fi[0].cik], 2), _priority(fi[0]),
                                              -fi[0].accepted_at.timestamp(), fi[0].accession))
    cap = max(int(quota), 0)
    for f, item in ranked:
        if len(result.items) >= cap:
            result.drop("quota")
            continue
        if enrich is not None:
            text = enrich(f)
            if f.form.startswith("6-K") and (text is None or not (text.english and text.headline)):
                result.drop("sec_6k_no_headline")
                continue
            if text is not None and not text.empty:
                item = market_item(f, tickers[f.cik], now=now, slot=cut, result=result, lookback=lookback,
                                   text=text) or item
        result.items.append(item)
        result.report.kept += 1
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
    """The Scout's reading list: each source within its quota, admitted at `slot` (only items
    available before it: no lookahead), newest first, at most READING_MAX, one item per id. SEC
    items are cut in the caller's rank order (`sec_market_wide`), not by age."""
    def admitted(items: Iterable[NewsItem]) -> list[NewsItem]:
        return _newest(i for i in items if slot - lookback <= i.available_at < slot)

    ranked_sec = [i for i in sec if slot - lookback <= i.available_at < slot]   # the caller's rank order
    chosen = ranked_sec[:SEC_QUOTA] + feed_pass_through(feed, slot=slot, lookback=lookback) \
        + admitted(gov)[:GOV_QUOTA]
    seen: dict[str, NewsItem] = {}
    for item in chosen:
        seen.setdefault(item.id, item)
    return _newest(seen.values())[:READING_MAX]
