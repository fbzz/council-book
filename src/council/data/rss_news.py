"""Third-party RSS headlines -> `NewsItem` (`N:` ids, source `rss`) for the swing Scout and the core
news role (user decision 2026-09-29; the feed list is `policy/council.yaml` `news.rss`).

DATA RIGHTS: the headline and description text of these publishers is LICENSED / restricted: the
models read it for the operator's personal use, it is never published, and its private copies are
purged after 7 days. Everything downstream already treats an `N:` item that way (like the broker
feed): the capture holds it apart as licensed text (`licensed/calls/`, `council purge-licensed`),
the final leak scan guards its text, a catalyst or evidence row publishes only the id and the feed
label (docs/data-rights.md). Links are not kept (`NewsItem.link` is for public-domain hosts only).

Rules (the gov_news pattern, `council.data.gov_news`, whose XML guard and cleaners are reused):
- Fetch: each market feed ONCE per slot (the core and the Scout share the memoised news fetch);
  the per-ticker feed (Yahoo, `{TICKER}` in its URL) once per ticker, for at most
  `yahoo_max_tickers` names (the Scout's open trades, carried ideas and movers-screen names). One
  attempt per request, `timeout_s` (15 s) read timeout, requests run concurrently within a budget;
  a browser-like user agent (the feeds refuse library agents). A feed that fails (or is still
  running at the budget) is skipped and flagged `news_source_error:rss:<feed>`; nothing raises.
  Requests go only to the policy's feed hosts (a request hook refuses any other, redirects too).
- XML: `gov_news.parse_xml` (no DOCTYPE / ENTITY declaration, 2 MB cap: no XXE, no entity bomb),
  RSS 2.0, RSS 1.0 and Atom (`gov_news.parse_feed`).
- Time: the entry's latest parseable time (`gov_news.parse_stamp`; a naive time is New York wall
  time unless the feed's `naive_tz` says otherwise); only items available STRICTLY before the slot
  and at most 48 h old are kept (a replay under `--at` never reads past its slot); an item dated
  more than 5 minutes after the real fetch time is dropped as clock skew.
- Text: title (220) and description (300) through `gov_news.clean_field` (prompt hygiene, then the
  public cleaner: money, levels, long numbers); an item that trips the leak scan's value patterns
  is dropped (`news_item_dropped:rss:leak`).
- Tickers: the Yahoo request's ticker, "(NASDAQ: XYZ)" / "(NYSE:XYZ)"-style exchange tags, cashtags
  ($XYZ), Nasdaq's `<nasdaq:tickers>` element and, for feeds marked `category_ticker`, the
  category; line-id spelling (`BRK_B`). An untagged item is market-wide.
- Ids: `N:` + HMAC-SHA256(install key, "rss" NUL stable key)[:8] (`models.facts.rss_news_id`); the
  stable key is the canonical link (host without `www.`, path, no query), else the normalised title,
  so one story in two feeds (or two tickers' Yahoo feeds) is one id; `dedupe` also merges equal
  titles (their tickers are joined).
- Ranking (`rank_rss`, the Scout): items on a priority name (movers screen, open trade, carried
  idea) first, then press releases / earnings / per-ticker items, then market headlines; newest
  first inside a tier; at most `scout_per_feed` per feed and `scout_max` in all.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import httpx

from council.data import gov_news
from council.data.gov_news import (
    ENTRIES_PER_FEED,
    LOOKBACK,
    NewsFetch,
    RawEntry,
    SourceReport,
    SourceResult,
    Stamp,
    admit_time,
    clean_field,
    trips_leak_scan,
)
from council.models.facts import RSS_NEWS_SOURCE, NewsItem, rss_news_id
from council.stocks.universe import try_normalise_id

SOURCE = RSS_NEWS_SOURCE
LICENCE = "third_party_licensed"
TITLE_MAX = 220
SUMMARY_MAX = 300
DEFAULT_TIMEOUT_S = 15.0
CONNECT_TIMEOUT_S = 5.0
MARKET_BUDGET_S = 25.0
TICKER_BUDGET_S = 45.0
MAX_WORKERS = 6
KINDS = ("press", "earnings", "market", "ticker")
TICKER_SLOT = "{TICKER}"
BROWSER_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
_NEW_YORK = "America/New_York"


# ------------------------------------------------------------------------------------ policy


@dataclass(frozen=True)
class RssFeed:
    label: str
    url: str
    kind: str
    naive_tz: str = _NEW_YORK
    category_ticker: bool = False

    @property
    def per_ticker(self) -> bool:
        return TICKER_SLOT in self.url

    def url_for(self, ticker: str | None = None) -> str:
        if not self.per_ticker:
            return self.url
        return self.url.replace(TICKER_SLOT, quote(str(ticker or ""), safe=""))


@dataclass(frozen=True)
class RssConfig:
    feeds: tuple[RssFeed, ...]
    timeout_s: float = DEFAULT_TIMEOUT_S
    scout_max: int = 40
    scout_per_feed: int = 8
    yahoo_max_tickers: int = 30

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset(h for h in (urlsplit(f.url.replace(TICKER_SLOT, "X")).hostname for f in self.feeds) if h)

    def kind_of(self, label: str | None) -> str:
        return next((f.kind for f in self.feeds if f.label == label), "market")

    def market_feeds(self) -> tuple[RssFeed, ...]:
        return tuple(f for f in self.feeds if not f.per_ticker)

    def ticker_feeds(self) -> tuple[RssFeed, ...]:
        return tuple(f for f in self.feeds if f.per_ticker)


_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def rss_config(policy: Any) -> RssConfig | None:
    """`policy.council["news"]["rss"]` parsed, or None when absent or `enabled: false`. A malformed
    feed row raises ValueError (a policy error, caught by the policy tests)."""
    council = getattr(policy, "council", None)
    news = council.get("news") if isinstance(council, Mapping) else None
    raw = news.get("rss") if isinstance(news, Mapping) else None
    if not isinstance(raw, Mapping) or raw.get("enabled", True) is not True:
        return None
    feeds: list[RssFeed] = []
    for row in raw.get("feeds") or []:
        label, url, kind = str(row.get("label", "")), str(row.get("url", "")), str(row.get("kind", ""))
        if not _LABEL.match(label) or kind not in KINDS or not url.startswith("https://"):
            raise ValueError(f"news.rss feed row {label!r} is malformed")
        tz = str(row.get("naive_tz") or _NEW_YORK)
        ZoneInfo(tz)
        feeds.append(RssFeed(label, url, kind, tz, bool(row.get("category_ticker", False))))
    if len({f.label for f in feeds}) != len(feeds):
        raise ValueError("news.rss feed labels must be unique")
    return RssConfig(feeds=tuple(feeds), timeout_s=float(raw.get("timeout_s", DEFAULT_TIMEOUT_S)),
                     scout_max=int(raw.get("scout_max", 40)), scout_per_feed=int(raw.get("scout_per_feed", 8)),
                     yahoo_max_tickers=int(raw.get("yahoo_max_tickers", 30)))


# ------------------------------------------------------------------------------------ parse

_EXCHANGE_TAG = re.compile(
    r"\(\s*(?i:nasdaq(?:\s*(?:gs|gm|cm))?|nyse(?:\s*(?:american|arca|mkt))?|nyseamerican|amex|cboe|otcqx|otcqb)"
    r"\s*:\s*([A-Z][A-Z0-9]{0,4}(?:[.\-][A-Z])?)\s*[),;]")
_CASHTAG = re.compile(r"(?<![\w$])\$([A-Z]{1,5}(?:\.[A-Z])?)(?![\w.])")
_CATEGORY_TICKER = re.compile(r"^[A-Za-z]{1,5}(?:\.[A-Za-z])?$")
_TAG = re.compile(r"<[^<>]{0,500}>")
_JUNK_SUMMARY = re.compile(r"^The post .{0,400} appeared first on ", re.IGNORECASE)
_HAS_TIME = re.compile(r"\d{1,2}:\d{2}")
_HAS_ZONE = re.compile(r"(?:[+-]\d{2}:?\d{2}|Z|\b[A-Z]{2,4})$")
_TITLE_KEY = re.compile(r"[^a-z0-9]+")


def _stamp(raw: str | None, tz: str) -> Stamp | None:
    text = " ".join((raw or "").split())
    if tz != _NEW_YORK and _HAS_TIME.search(text) and not _HAS_ZONE.search(text):
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is None:
            at = parsed.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)
            return Stamp(published_at=at, available_at=at)
    return gov_news.parse_stamp(text)


def entry_stamp(entry: RawEntry, feed: RssFeed) -> Stamp | None:
    """The entry's LATEST parseable time (an updated item is used from its update, never earlier)."""
    parsed = [s for s in (_stamp(raw, feed.naive_tz) for raw in (entry.stamps or (entry.stamp,))) if s is not None]
    return max(parsed, key=lambda s: (s.available_at, s.published_at)) if parsed else None


def tag_tickers(entry: RawEntry, feed: RssFeed, ticker: str | None = None) -> list[str]:
    """Line ids the entry is about (see the module rules); [] = market-wide."""
    raw = f"{entry.title}\n{_TAG.sub(' ', entry.summary[:gov_news.RAW_TEXT_MAX])}"
    found: list[str] = [ticker] if ticker else []
    found += _EXCHANGE_TAG.findall(raw)
    found += _CASHTAG.findall(raw)
    found += [t.strip() for t in str(entry.extra.get("tickers", "")).split(",") if t.strip()]
    category = str(entry.extra.get("category", "")).strip()
    if feed.category_ticker and _CATEGORY_TICKER.match(category):
        found.append(category)
    out: list[str] = []
    for sym in found:
        lid = try_normalise_id(sym)
        if lid and lid not in out:
            out.append(lid)
    return out


def canonical_link(link: str | None) -> str:
    """host (no `www.`) + path, lower-cased host, no query or fragment ("" when unusable)."""
    text = (link or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.scheme not in ("http", "https") or not host:
        return ""
    path = parts.path.rstrip("/")
    if not path:                                   # a bare host: the query is the identity
        path = "?" + parts.query if parts.query else ""
    return host + path


def title_key(title: str) -> str:
    return _TITLE_KEY.sub(" ", title.lower()).strip()


def entry_item(feed: RssFeed, entry: RawEntry, *, now: datetime, slot: datetime, result: SourceResult,
               install_key: bytes | None, ticker: str | None = None,
               lookback: timedelta = LOOKBACK) -> NewsItem | None:
    """One feed entry -> NewsItem, or None (the drop is counted on `result`)."""
    stamp = entry_stamp(entry, feed)
    reason = admit_time(stamp, now=now, slot=slot, lookback=lookback)
    if reason is not None:
        result.drop(reason)
        return None
    assert stamp is not None
    title = clean_field(entry.title, TITLE_MAX)
    summary = clean_field(entry.summary, SUMMARY_MAX)
    if summary == title or _JUNK_SUMMARY.match(summary):
        summary = ""
    if not title:
        result.drop("empty")
        return None
    if trips_leak_scan(title, summary):
        result.drop("leak")
        return None
    key = canonical_link(entry.link) or "title:" + title_key(title)
    return NewsItem(id=rss_news_id(key, install_key), title=title, summary=summary,
                    symbols=tag_tickers(entry, feed, ticker), published_at=stamp.published_at,
                    available_at=stamp.available_at, source=SOURCE, licence=LICENCE,  # type: ignore[arg-type]
                    feed=feed.label)


def parse_rss(feed: RssFeed, data: bytes, *, now: datetime, slot: datetime, install_key: bytes | None = None,
              ticker: str | None = None, result: SourceResult | None = None,
              lookback: timedelta = LOOKBACK) -> SourceResult:
    """Parse one recorded or fetched feed document into `result` (a new one by default). Raises
    `gov_news.NewsParseError` for a document that is not safe, well-formed RSS / Atom."""
    out = result if result is not None else SourceResult(SOURCE)
    assert out.report is not None
    entries = gov_news.parse_feed(data)[:ENTRIES_PER_FEED]
    out.report.entries += len(entries)
    for entry in entries:
        item = entry_item(feed, entry, now=now, slot=slot, result=out, install_key=install_key, ticker=ticker,
                          lookback=lookback)
        if item is not None:
            out.items.append(item)
            out.report.kept += 1
    return out


def dedupe(items: Iterable[NewsItem]) -> list[NewsItem]:
    """One item per id and per normalised title (the first seen wins; tickers are joined)."""
    by_key: dict[str, int] = {}
    out: list[NewsItem] = []
    for item in items:
        keys = (item.id, "t:" + title_key(item.title))
        hit = next((by_key[k] for k in keys if k in by_key), None)
        if hit is None:
            for k in keys:
                by_key[k] = len(out)
            out.append(item)
            continue
        held = out[hit]
        extra = [s for s in item.symbols if s not in held.symbols]
        if extra:
            out[hit] = held.model_copy(update={"symbols": [*held.symbols, *extra]})
        for k in keys:
            by_key.setdefault(k, hit)
    return out


def rank_rss(items: Iterable[NewsItem], cfg: RssConfig, *, slot: datetime, priority: Iterable[str] = (),
             max_items: int | None = None, per_feed: int | None = None,
             lookback: timedelta = LOOKBACK) -> list[NewsItem]:
    """The Scout's RSS selection (module rules): admitted at `slot`, deduplicated, priority names
    first, then press / earnings / per-ticker items, then market headlines; newest first in a tier;
    per-feed and total caps."""
    names = frozenset(priority)
    cap = cfg.scout_max if max_items is None else max_items
    each = cfg.scout_per_feed if per_feed is None else per_feed
    ok = [i for i in items if i.source == SOURCE and slot - lookback <= i.available_at < slot]
    ok.sort(key=lambda i: (-i.available_at.timestamp(), i.id))

    def tier(item: NewsItem) -> int:
        if names.intersection(item.symbols):
            return 0
        return 1 if cfg.kind_of(item.feed) in ("press", "earnings", "ticker") else 2

    ranked = sorted(dedupe(ok), key=lambda i: (tier(i), -i.available_at.timestamp(), i.id))
    used: dict[str, int] = {}
    out: list[NewsItem] = []
    for item in ranked:
        label = str(item.feed or "")
        if used.get(label, 0) >= each:
            continue
        used[label] = used.get(label, 0) + 1
        out.append(item)
        if len(out) >= cap:
            break
    return out


# ------------------------------------------------------------------------------------ fetch

Getter = Callable[[str], bytes]
"""Fetch one URL's body (raises on any failure); tests pass recorded fixtures."""


class _HostGuard:
    def __init__(self, hosts: frozenset[str]) -> None:
        self.hosts = hosts

    def request(self, request: httpx.Request) -> None:
        if request.url.scheme != "https" or request.url.host not in self.hosts:
            raise gov_news.HostRefused("request to a host outside the RSS allow-list refused")


def http_getter(cfg: RssConfig, *, transport: httpx.BaseTransport | None = None) -> tuple[Getter, httpx.Client]:
    """A getter over one client (browser user agent, host guard, one attempt, `timeout_s`)."""
    guard = _HostGuard(cfg.hosts)
    client = httpx.Client(transport=transport, timeout=httpx.Timeout(cfg.timeout_s, connect=CONNECT_TIMEOUT_S),
                          follow_redirects=True, max_redirects=3,
                          headers={"User-Agent": BROWSER_USER_AGENT, "Accept": gov_news.ACCEPT,
                                   "Accept-Encoding": "gzip, deflate"},
                          event_hooks={"request": [guard.request]})

    def get(url: str) -> bytes:
        response = client.get(url)
        if response.status_code >= 400:
            raise gov_news.DataError(f"rss: HTTP {response.status_code}")
        body = response.content
        if len(body) > gov_news.MAX_BYTES:
            raise gov_news.NewsParseError("feed document is too large")
        return body

    return get, client


@dataclass
class _Job:
    feed: RssFeed
    ticker: str | None = None
    future: Future[bytes] | None = field(default=None, repr=False)


def fetch_jobs(cfg: RssConfig, jobs: Sequence[_Job], *, now: datetime, slot: datetime,
               install_key: bytes | None = None, get: Getter | None = None,
               transport: httpx.BaseTransport | None = None, budget_s: float = MARKET_BUDGET_S,
               monotonic: Callable[[], float] = time.monotonic, lookback: timedelta = LOOKBACK) -> NewsFetch:
    """Run the requests concurrently within `budget_s`, parse what came back. Never raises for a
    data problem: a failed or late feed becomes `news_source_error:rss:<feed>`."""
    result = SourceResult(SOURCE)
    assert result.report is not None
    started = monotonic()
    client = None
    if get is None:
        get, client = http_getter(cfg, transport=transport)
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="rss")
    try:
        for job in jobs:
            job.future = pool.submit(get, job.feed.url_for(job.ticker))
        deadline = monotonic() + float(budget_s)
        for job in jobs:
            assert job.future is not None
            try:
                body = job.future.result(timeout=max(0.0, deadline - monotonic()))
                parse_rss(job.feed, body, now=now, slot=slot, install_key=install_key, ticker=job.ticker,
                          result=result, lookback=lookback)
            except BaseException as exc:  # noqa: BLE001 - one feed's failure never costs another's items
                job.future.cancel()
                result.report.feeds_failed += 1
                result.report.error = "budget" if isinstance(exc, TimeoutError) else gov_news.error_type(exc)
                result.flag(f"news_source_error:{SOURCE}:{job.feed.label}")
                continue
            result.report.feeds_ok += 1
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if client is not None:
            client.close()                      # a request abandoned at the budget fails harmlessly
    result.report.seconds = round(monotonic() - started, 3)
    return NewsFetch(items=dedupe(result.items), flags=list(result.flags),
                     sources={SOURCE: result.report})


def fetch_market(cfg: RssConfig, *, now: datetime, slot: datetime, **kw: Any) -> NewsFetch:
    """Every market (non-ticker) feed, once."""
    return fetch_jobs(cfg, [_Job(f) for f in cfg.market_feeds()], now=now, slot=slot, **kw)


def yahoo_symbol(line_id: str) -> str:
    """A line id in the per-ticker feed's spelling (`BRK_B` -> `BRK-B`)."""
    return line_id.replace("_", "-")


def fetch_tickers(cfg: RssConfig, tickers: Iterable[str], *, now: datetime, slot: datetime,
                  budget_s: float = TICKER_BUDGET_S, **kw: Any) -> NewsFetch:
    """The per-ticker feeds for at most `yahoo_max_tickers` line ids (first ones win)."""
    names = list(dict.fromkeys(t for t in (try_normalise_id(x) for x in tickers) if t))[:cfg.yahoo_max_tickers]
    jobs = [_Job(f, yahoo_symbol(t)) for f in cfg.ticker_feeds() for t in names]
    return fetch_jobs(cfg, jobs, now=now, slot=slot, budget_s=budget_s, **kw)   # tags come back as line ids


def _install_key(state_dir: Path | None) -> bytes | None:
    if state_dir is None:
        return None
    from council.publish import install_key

    return install_key.load_or_create(state_dir)


def market_fetcher(policy: Any, state_dir: Path | None) -> Callable[[datetime, datetime], NewsFetch] | None:
    """`(now, slot) -> NewsFetch` over the policy's market feeds, or None when RSS is off."""
    cfg = rss_config(policy)
    if cfg is None or not cfg.market_feeds():
        return None

    def fetch(now: datetime, slot: datetime) -> NewsFetch:
        return fetch_market(cfg, now=now, slot=slot, install_key=_install_key(state_dir))

    return fetch


def ticker_fetcher(policy: Any, state_dir: Path | None) -> Callable[[Sequence[str], datetime, datetime], NewsFetch] | None:
    """`(line ids, now, slot) -> NewsFetch` over the per-ticker feeds, or None when there are none."""
    cfg = rss_config(policy)
    if cfg is None or not cfg.ticker_feeds():
        return None

    def fetch(tickers: Sequence[str], now: datetime, slot: datetime) -> NewsFetch:
        return fetch_tickers(cfg, tickers, now=now, slot=slot, install_key=_install_key(state_dir))

    return fetch


def merge_fetches(*fetches: NewsFetch) -> NewsFetch:
    items: list[NewsItem] = []
    flags: list[str] = []
    sources: dict[str, SourceReport] = {}
    for f in fetches:
        items += list(f.items)
        flags += list(f.flags)
        sources.update(f.sources)
    return NewsFetch(items=items, flags=list(dict.fromkeys(flags)), sources=sources)


__all__ = ["LICENCE", "SOURCE", "RssConfig", "RssFeed", "canonical_link", "dedupe", "fetch_jobs", "fetch_market",
           "fetch_tickers", "market_fetcher", "merge_fetches", "parse_rss", "rank_rss", "rss_config", "tag_tickers",
           "ticker_fetcher", "yahoo_symbol"]
