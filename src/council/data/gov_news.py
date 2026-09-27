"""Public-domain news: U.S. federal release feeds -> `NewsItem` (`P:` ids) for the news role.

The broker's news feed is eToro Licensed Content; these sources are the news path that anyone may
check (transparency design §3). This module parses, cleans and leak-scans them; it is not wired into
the cycle here (the context and pack hooks come later).

Sources (licence status recorded in `SOURCES`, archived pages in tests/fixtures/news/licences/):
- Federal Reserve Board (monetary and other press releases, speeches, testimony), BLS (release
  feeds), BEA (news releases), EIA (press releases, Today in Energy; an item is tagged to the OIL
  line only when the policy has it AND its cleaned title or summary is about oil (`OIL_TOPIC`), so
  an electricity, gas or uranium release stays market-wide): public domain by each site's own
  statement. Every other item is market-wide.
- U.S. Treasury (TreasuryDirect auction announcements and results; no home.treasury.gov
  press-release feed was found): a federal work (17 U.S.C. §105), but no explicit site statement was found,
  so its licence is `federal_work_unverified` and it keeps only title, link and time (no summary,
  not even for the model).
- SEC 8-K / 6-K filing metadata for the stock lines: `council.stocks.sec_news` (it shares the SEC
  client's rate limiter), run from here as one more source.
Regional Reserve Banks, the ECB, the Bank of England and news wires are not in v1.

Rules:
- Lookahead: an item's `available_at` is its publish (or acceptance) time, or its update time
  when the feed gives a later one (the text read is the updated one). Only items available
  strictly before the slot, and at most LOOKBACK old, are returned. An item dated more than 5
  minutes after the fetch is dropped as clock-skewed (`news_item_dropped:<src>:skew`). A date
  without a time of day becomes available at the next 00:00 UTC; a time without a zone is read as
  New York wall time (later, never earlier, than reading it as UTC for these U.S. sources). An item
  without a parseable time is dropped (`news_item_dropped:<src>:no_time`): its availability cannot
  be proven.
- Cleaning at fetch time: title and summary go through `council.data.sanitize.clean_text` (escapes,
  controls, tags, links, e-mails, handles) and then the public cleaner
  `council.publish.redact.clean_text` with the source's caps (220 / 320 characters): money becomes
  "[amount removed]" and large counts "[level removed]". The model sees the cleaned text, which is
  what lets a later public hash be rebuilt by anyone.
- Leak scan at fetch time: the cleaned strings go through the leak scan's value patterns; an item
  that trips it is DROPPED (`news_item_dropped:<src>:leak`), so a false positive can never reach a
  pack or a reveal.
- Links are a typed field (`council.models.facts.HttpsLink`): allow-listed hosts, https only, no
  7+ digit run. An `http://` link on an allow-listed host is upgraded; a link that still fails is
  dropped and the item kept (`news_link_dropped:<src>`).
- IDs: `P:` + sha256(source NUL stable key)[:8]; the key is the feed's guid (or Atom id), else
  feed label + link + time + title (`stable_key`).
- Latency: every request has a 5 s connect / 10 s read timeout and at most one retry; the sources
  run concurrently, and the whole fetch has a 30 s budget. A source still running at the deadline
  is abandoned (`news_source_error:<src>:budget`); a source that fails is skipped
  (`news_source_error:<src>:<type>`). Nothing here raises for a data problem, and one source's
  failure never costs another's items.
- Requests go only to the source hosts (a request hook refuses any other host, redirects included).
  BLS needs a declared user agent: it reuses the SEC contact (`council.data.credentials`: the
  Keychain item, or its env override in tests), which travels in the request header only, never in
  a flag, a log or an error; a missing one costs BLS only (`news_source_error:bls:no_user_agent`).
- XML: documents over 2 MB, or with a DOCTYPE or an entity declaration, are refused before parsing.
"""

from __future__ import annotations

import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field

from council.data import credentials, sanitize
from council.data.credentials import MissingCredential
from council.data.http import DataError, RateLimited, get_with_retry
from council.models.common import Strict
from council.models.facts import NEWS_LINK_HOSTS, NewsItem, check_https_link, public_news_id
from council.publish import leakscan, redact

NEW_YORK = ZoneInfo("America/New_York")
FETCH_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
TOTAL_BUDGET_S = 30.0
LOOKBACK = timedelta(hours=48)
FUTURE_SKEW = timedelta(minutes=5)
TITLE_MAX = 220
SUMMARY_MAX = 320
RAW_TEXT_MAX = 4000                  # pre-clean cap: a hostile feed cannot make cleaning slow
MAX_BYTES = 2_000_000
ENTRIES_PER_FEED = 60
RETRIES = 1
BACKOFF_S = 0.5
GENERIC_USER_AGENT = "council-book/0.1 (public-domain news reader)"
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
# Hosts a federal-source request may reach (SEC requests go through `SecClient`).
FETCH_HOSTS: frozenset[str] = NEWS_LINK_HOSTS
# Flags are emitted for these drop reasons; items after the slot or older than LOOKBACK are normal.
FLAGGED_DROPS = frozenset({"leak", "skew", "no_time"})


# ------------------------------------------------------------------------------------ registry


# Oil-specific words: an EIA item is tagged to the OIL line only when its title or summary names one
# (EIA also publishes on electricity, natural gas, coal, uranium and renewables, which are not the
# oil line's news; tagging them would let an unrelated release back an oil-line card).
OIL_TOPIC = re.compile(
    r"\b(?:oil|crude|petroleum|gasoline|diesel|distillates?|refiner(?:y|ies)|refining|jet fuel|opec\+?"
    r"|brent|wti|hormuz|crack spreads?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Feed:
    """One feed of a source. `symbols` are the exposure lines its items are tagged with (kept only
    when the policy has that line, and, when `topic` is set, only when the item's cleaned title or
    summary matches it); market-wide feeds tag none."""

    label: str
    url: str
    symbols: tuple[str, ...] = ()
    topic: re.Pattern[str] | None = None


@dataclass(frozen=True)
class SourceSpec:
    """A news source and its licence status. `public_fields` is the allow-list a publication step
    may use; `licence_archive` names the dated copy under tests/fixtures/news/licences/."""

    key: str
    name: str
    licence: str
    licence_url: str
    licence_quote: str
    licence_archive: str
    attribution: str
    public_fields: tuple[str, ...]
    feeds: tuple[Feed, ...] = ()
    summary_max: int = SUMMARY_MAX         # 0: no summary is kept (not even for the model)
    contact_ua: bool = False               # send the declared contact user agent (BLS, SEC)
    exclusions: str = ""


SOURCES: Mapping[str, SourceSpec] = MappingProxyType({
    "sec": SourceSpec(
        key="sec", name="U.S. Securities and Exchange Commission (8-K / 6-K filing metadata)",
        licence="public_domain",
        licence_url="https://www.sec.gov/about/privacy-information",
        licence_quote=("Information presented on sec.gov is considered public information and may be "
                       "copied or further distributed by users of the web site without the SEC's permission."),
        licence_archive="sec-2026-09-26.html",
        attribution="Source: U.S. Securities and Exchange Commission",
        public_fields=("form", "items", "title", "published", "company", "link"),
        summary_max=SUMMARY_MAX, contact_ua=True,
        exclusions="Filing text is company-authored: metadata only (form, item codes, time, company).",
    ),
    "fed_board": SourceSpec(
        key="fed_board", name="Board of Governors of the Federal Reserve System",
        licence="public_domain",
        licence_url="https://www.federalreserve.gov/disclaimer.htm",
        licence_quote=("Unless otherwise indicated, information on Board's website is in the public domain "
                       "and may be copied and distributed without permission."),
        licence_archive="fed_board-2026-09-26.html",
        attribution="Source: Board of Governors of the Federal Reserve System",
        public_fields=("title", "summary", "link", "published"),
        feeds=(
            Feed("press_monetary", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
            Feed("press_other", "https://www.federalreserve.gov/feeds/press_other.xml"),
            Feed("speeches", "https://www.federalreserve.gov/feeds/speeches.xml"),
            Feed("testimony", "https://www.federalreserve.gov/feeds/testimony.xml"),
        ),
        exclusions="Third-party material, seals and logos are excluded; Regional Reserve Banks are not covered.",
    ),
    "bls": SourceSpec(
        key="bls", name="U.S. Bureau of Labor Statistics",
        licence="public_domain",
        licence_url="https://www.bls.gov/opub/copyright-information.htm",
        licence_quote=("everything that we publish, both in hard copy and electronically, is in the public "
                       "domain, except for previously copyrighted photographs and illustrations"),
        licence_archive="bls-2026-09-26.html",
        attribution="Source: U.S. Bureau of Labor Statistics",
        public_fields=("title", "summary", "link", "published"),
        feeds=(
            Feed("empsit", "https://www.bls.gov/feed/empsit.rss"),
            Feed("cpi", "https://www.bls.gov/feed/cpi.rss"),
            Feed("ppi", "https://www.bls.gov/feed/ppi.rss"),
            Feed("jolts", "https://www.bls.gov/feed/jolts.rss"),
            Feed("eci", "https://www.bls.gov/feed/eci.rss"),
        ),
        contact_ua=True,
        exclusions="Previously copyrighted photographs and illustrations are excluded.",
    ),
    "bea": SourceSpec(
        key="bea", name="U.S. Bureau of Economic Analysis",
        licence="public_domain",
        licence_url="https://www.bea.gov/help/faq/147",
        licence_quote=("Unless stated otherwise, the information posted on this web site is in the public domain "
                       "and may be used or reproduced without specific permission."),
        licence_archive="bea-2026-09-26.html",
        attribution="Source: U.S. Bureau of Economic Analysis",
        public_fields=("title", "summary", "link", "published"),
        feeds=(Feed("news_releases", "https://apps.bea.gov/rss/rss.xml"),),
    ),
    "treasury": SourceSpec(
        key="treasury", name="U.S. Department of the Treasury",
        licence="federal_work_unverified",
        licence_url="https://home.treasury.gov/subfooter/site-policies-and-notices",
        licence_quote="",
        licence_archive="treasury-2026-09-26.html",
        attribution="Source: U.S. Department of the Treasury",
        public_fields=("title", "link", "published"),
        feeds=(
            Feed("auction_announcements", "https://www.treasurydirect.gov/TA_WS/securities/announced/rss"),
            Feed("auction_results", "https://www.treasurydirect.gov/TA_WS/securities/auctioned/rss"),
        ),
        summary_max=0,
        exclusions=("A federal work (17 U.S.C. §105), but no explicit site statement was found (the site "
                    "policies page, archived, has none): title, link and time only. No home.treasury.gov "
                    "press-release feed was found (checked 2026-09-26), so v1 reads the TreasuryDirect "
                    "auction feeds only."),
    ),
    "eia": SourceSpec(
        key="eia", name="U.S. Energy Information Administration",
        licence="public_domain",
        licence_url="https://www.eia.gov/about/copyrights_reuse.php",
        licence_quote=("U.S. government publications are in the public domain and are not subject to copyright "
                       "protection. You may use and/or distribute any of our data, files, databases, reports, "
                       "graphs, charts, and other information products"),
        licence_archive="eia-2026-09-26.html",
        attribution="Source: U.S. Energy Information Administration ({date})",
        public_fields=("title", "summary", "link", "published"),
        feeds=(
            Feed("press_releases", "https://www.eia.gov/rss/press_rss.xml", ("OIL",), OIL_TOPIC),
            Feed("today_in_energy", "https://www.eia.gov/rss/todayinenergy.xml", ("OIL",), OIL_TOPIC),
        ),
        exclusions=("Third-party material is excluded; attribution with the release date is required. The "
                    "This Week in Petroleum feed stopped in October 2025 with unparseable dates, and the "
                    "Weekly Petroleum Status Report has no feed (checked 2026-09-26)."),
    ),
})
GOV_SOURCES: tuple[str, ...] = ("fed_board", "bls", "bea", "treasury", "eia")
SOURCE_ORDER: tuple[str, ...] = ("sec", *GOV_SOURCES)


def attribution(source: str, published: datetime | None = None) -> str:
    """The fixed attribution line an item of `source` carries (EIA's names the release date)."""
    template = SOURCES[source].attribution
    if "{date}" in template:
        day = published.astimezone(UTC).date().isoformat() if published is not None else "undated"
        return template.format(date=day)
    return template


# ------------------------------------------------------------------------------------ results


class SourceReport(Strict):
    """What one source did this fetch (private; the operator's reading-list view)."""

    source: str
    feeds_ok: int = 0
    feeds_failed: int = 0
    entries: int = 0
    kept: int = 0
    dropped: dict[str, int] = Field(default_factory=dict)
    links_dropped: int = 0
    seconds: float = 0.0
    error: str | None = None


class NewsFetch(Strict):
    """Items (newest first, unique by id) and quality flags from one public-news fetch."""

    items: list[NewsItem] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    sources: dict[str, SourceReport] = Field(default_factory=dict)


@dataclass
class SourceResult:
    """One source's output before the merge."""

    source: str
    items: list[NewsItem] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    report: SourceReport | None = None

    def __post_init__(self) -> None:
        if self.report is None:
            self.report = SourceReport(source=self.source)

    def drop(self, reason: str) -> None:
        assert self.report is not None
        self.report.dropped[reason] = self.report.dropped.get(reason, 0) + 1
        if reason in FLAGGED_DROPS:
            self.flag(f"news_item_dropped:{self.source}:{reason}")

    def flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)


class NewsParseError(DataError):
    """A feed document is unusable (too large, not XML, a DOCTYPE/entity declaration)."""


class HostRefused(DataError):
    """A request (or a redirect) to a host outside the news allow-list was refused."""


# ------------------------------------------------------------------------------------ text


def clean_field(raw: object, cap: int) -> str:
    """Prompt hygiene, then the public cleaner (money, levels, long numbers), within `cap`."""
    if cap <= 0:
        return ""
    return redact.clean_text(sanitize.clean_text(raw, RAW_TEXT_MAX), cap)


def trips_leak_scan(*texts: str) -> bool:
    """True when any text matches one of the leak scan's value patterns."""
    return any(text and leakscan.scan(text) for text in texts)


# A bare domain of an allowed host is the same site: "bea.gov/news/..." is read as "www.bea.gov/...".
_APEX_TO_WWW: Mapping[str, str] = MappingProxyType({
    host.removeprefix("www."): host for host in NEWS_LINK_HOSTS if host.startswith("www.")
})


def public_link(raw: str | None, *, base: str | None = None) -> tuple[str | None, bool]:
    """(the link as an allowed `HttpsLink`, or None; whether a present link was dropped). Relative
    links resolve against the feed URL (a scheme-less link that starts with an allowed host is that
    host); on an allowed host (or its bare domain) `http://` becomes `https://` and the host its
    `www.` name."""
    text = (raw or "").strip()
    if not text:
        return None, False
    if "://" not in text and text.split("/", 1)[0].lower() in (NEWS_LINK_HOSTS | set(_APEX_TO_WWW)):
        text = "https://" + text                 # a scheme-less "www.bea.gov/news/..." is not a path
    elif base:
        text = urljoin(base, text)
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return None, True
    if parts.scheme in ("http", "https") and host is not None and parts.netloc == host:
        canonical = host if host in NEWS_LINK_HOSTS else _APEX_TO_WWW.get(host)
        if canonical is not None:
            text = parts._replace(scheme="https", netloc=canonical).geturl()
    try:
        return check_https_link(text), False
    except ValueError:
        return None, True


# ------------------------------------------------------------------------------------ time

_HAS_TIME = re.compile(r"\d{1,2}:\d{2}")
_DATE_FORMATS = ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%a, %d %b %Y", "%d %b %Y")


@dataclass(frozen=True)
class Stamp:
    """A parsed feed time: when the item was published, and when it may be used."""

    published_at: datetime
    available_at: datetime
    date_only: bool = False


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=NEW_YORK)       # U.S. sources: later, never earlier, than UTC
    return value.astimezone(UTC)


def _date_only(day: date) -> Stamp:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return Stamp(published_at=start, available_at=start + timedelta(days=1), date_only=True)


def parse_stamp(text: str | None) -> Stamp | None:
    """RFC 822 (RSS), ISO 8601 (Atom, dc:date) or a plain date. None when unparseable."""
    raw = " ".join((text or "").split())
    if not raw:
        return None
    if not _HAS_TIME.search(raw):
        for fmt in _DATE_FORMATS:
            try:
                return _date_only(datetime.strptime(raw.rstrip(" ."), fmt).date())
            except ValueError:
                continue
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
    if parsed is None:
        return None
    at = _as_utc(parsed)
    return Stamp(published_at=at, available_at=at)


# ------------------------------------------------------------------------------------ XML


@dataclass(frozen=True)
class RawEntry:
    """One `<item>` / `<entry>` of a feed, as text (nothing cleaned yet)."""

    key: str | None
    title: str
    summary: str
    link: str | None
    stamp: str | None                       # the first time field (part of the fallback id key)
    extra: Mapping[str, str] = field(default_factory=dict)
    stamps: tuple[str, ...] = ()            # every time field (published, updated, ...)


_STAMP_FIELDS = ("pubdate", "published", "date", "updated", "issued", "modified")
_FORBIDDEN_XML = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)


def _local(tag: object) -> str:
    text = tag if isinstance(tag, str) else ""
    return text.rsplit("}", 1)[-1].lower()


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_xml(data: bytes) -> ET.Element:
    """The document's root element; refuses oversized documents and DOCTYPE/ENTITY declarations
    (no entity expansion can happen), and anything that is not well-formed XML."""
    if len(data) > MAX_BYTES:
        raise NewsParseError("feed document is too large")
    if _FORBIDDEN_XML.search(data):
        raise NewsParseError("feed document declares a DOCTYPE or an entity")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsParseError("feed document is not well-formed XML") from exc


def parse_feed(data: bytes) -> list[RawEntry]:
    """Entries of an RSS 2.0, RSS 1.0 (RDF) or Atom document, in document order."""
    root = parse_xml(data)
    if _local(root.tag) not in {"rss", "rdf", "feed", "channel"}:
        raise NewsParseError("not an RSS or Atom document")
    out: list[RawEntry] = []
    for el in root.iter():
        if _local(el.tag) not in {"item", "entry"}:
            continue
        children: dict[str, list[ET.Element]] = {}
        for child in el:
            children.setdefault(_local(child.tag), []).append(child)

        def first(*names: str, _children: dict[str, list[ET.Element]] = children) -> str:
            for name in names:
                for child in _children.get(name, []):
                    value = _text(child)
                    if value:
                        return value
            return ""

        link = ""
        for child in children.get("link", []):
            href = child.get("href")
            if href and child.get("rel", "alternate") == "alternate":
                link = href
                break
            if not href and _text(child):
                link = _text(child)
                break
        if not link:
            link = next((c.get("href", "") for c in children.get("link", []) if c.get("href")), "")
        about = el.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about") or ""
        extra = {name: _text(items[0]) for name, items in children.items()
                 if name not in {"title", "link", "description", "summary", "content"} and _text(items[0])}
        out.append(RawEntry(
            key=first("guid", "id") or about or None,
            title=first("title"),
            summary=first("description", "summary", "content", "encoded"),
            link=link or None,
            stamp=first(*_STAMP_FIELDS) or None,
            extra=MappingProxyType(extra),
            stamps=tuple(_text(c) for name in _STAMP_FIELDS for c in children.get(name, []) if _text(c)),
        ))
    return out


# ------------------------------------------------------------------------------------ items


def entry_stamp(entry: RawEntry) -> Stamp | None:
    """The entry's LATEST parseable time: an item updated after it was published is used from the
    update (the text read is the updated one), never earlier."""
    parsed = [s for s in (parse_stamp(raw) for raw in (entry.stamps or (entry.stamp,))) if s is not None]
    return max(parsed, key=lambda s: (s.available_at, s.published_at)) if parsed else None


def admit_time(stamp: Stamp | None, *, now: datetime, slot: datetime,
               lookback: timedelta = LOOKBACK) -> str | None:
    """The drop reason for an item's time, or None when it may be used at `slot`."""
    if stamp is None:
        return "no_time"
    if stamp.published_at > _as_utc(now) + FUTURE_SKEW:
        return "skew"
    if stamp.available_at >= _as_utc(slot):
        return "after_slot"
    if stamp.available_at < _as_utc(slot) - lookback:
        return "stale"
    return None


def stable_key(feed: Feed, entry: RawEntry) -> str:
    """The id key: the feed's guid (or Atom id), else feed label + link + time + title. The fallback
    names the feed and the title because some feeds (TreasuryDirect) give every item the same link
    and the announcement and the result of one auction the same time."""
    guid = " ".join((entry.key or "").split())
    if guid:
        return guid
    parts = (feed.label, entry.link or "", entry.stamp or "", entry.title)
    return "\0".join(" ".join(part.split()) for part in parts)


def entry_item(spec: SourceSpec, feed: Feed, entry: RawEntry, *, now: datetime, slot: datetime,
               lines: frozenset[str], result: SourceResult, lookback: timedelta = LOOKBACK) -> NewsItem | None:
    """One feed entry -> NewsItem, or None (the drop is counted on `result`)."""
    stamp = entry_stamp(entry)
    reason = admit_time(stamp, now=now, slot=slot, lookback=lookback)
    if reason is not None:
        result.drop(reason)
        return None
    assert stamp is not None
    title = clean_field(entry.title, TITLE_MAX)
    summary = clean_field(entry.summary, spec.summary_max)
    if summary == title:                          # feeds that repeat the title add nothing
        summary = ""
    if not title:
        result.drop("empty")
        return None
    if trips_leak_scan(title, summary):
        result.drop("leak")
        return None
    link, link_dropped = public_link(entry.link, base=feed.url)
    if link is not None and trips_leak_scan(link):
        link, link_dropped = None, True
    if link_dropped:
        assert result.report is not None
        result.report.links_dropped += 1
        result.flag(f"news_link_dropped:{spec.key}")
    on_topic = feed.topic is None or feed.topic.search(f"{title}\n{summary}") is not None
    return NewsItem(
        id=public_news_id(spec.key, stable_key(feed, entry)),
        title=title,
        summary=summary,
        symbols=[s for s in feed.symbols if s in lines] if on_topic else [],
        published_at=stamp.published_at,
        available_at=stamp.available_at,
        source=spec.key,  # type: ignore[arg-type]
        licence=spec.licence,  # type: ignore[arg-type]
        link=link,
    )


def parse_source_feed(spec: SourceSpec, feed: Feed, data: bytes, *, now: datetime, slot: datetime,
                      lines: Iterable[str] = (), result: SourceResult | None = None,
                      lookback: timedelta = LOOKBACK) -> SourceResult:
    """Parse one recorded or fetched feed document into `result` (a new one by default)."""
    out = result if result is not None else SourceResult(spec.key)
    assert out.report is not None
    entries = parse_feed(data)[:ENTRIES_PER_FEED]
    out.report.entries += len(entries)
    allowed = frozenset(lines)
    for entry in entries:
        item = entry_item(spec, feed, entry, now=now, slot=slot, lines=allowed, result=out, lookback=lookback)
        if item is not None:
            out.items.append(item)
            out.report.kept += 1
    return out


# ------------------------------------------------------------------------------------ fetch


class _HostGuard:
    """Request hook: refuse any host outside FETCH_HOSTS (redirects included); records the last
    response status for error typing."""

    def __init__(self) -> None:
        self.status: int | None = None

    def request(self, request: httpx.Request) -> None:
        if request.url.scheme != "https" or request.url.host not in FETCH_HOSTS:
            raise HostRefused("request to a host outside the news allow-list refused")

    def response(self, response: httpx.Response) -> None:
        self.status = response.status_code


_HTTP_STATUS = re.compile(r"(?:: |\()HTTP (\d{3})\)?$")


def error_type(exc: BaseException, status: int | None = None) -> str:
    """A fixed code for a source failure (never a URL or a message)."""
    if isinstance(exc, MissingCredential):
        return "no_user_agent"
    if isinstance(exc, RateLimited):
        return "rate_limited"
    if isinstance(exc, HostRefused):
        return "host_refused"
    if isinstance(exc, NewsParseError):
        return "parse"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, DataError):
        if status is None:                       # our own messages: "<what>: HTTP <code>"
            found = _HTTP_STATUS.search(str(exc))
            status = int(found.group(1)) if found else None
        if status in (401, 403):
            return "auth"
        if status == 404:
            return "not_found"
        if status == 429:
            return "rate_limited"
        if status is not None and status >= 400:
            return f"http_{status}"
        message = str(exc)
        if "transport" in message:
            return "timeout" if "Timeout" in message else "transport"
        return "http"
    return type(exc).__name__


def request_headers(agent: str) -> dict[str, str]:
    """The headers of every news request (the user agent travels only here)."""
    return {"User-Agent": agent, "Accept": ACCEPT, "Accept-Encoding": "gzip, deflate"}


def _deadline_passed(deadline: float | None, monotonic: Callable[[], float]) -> bool:
    return deadline is not None and monotonic() >= deadline


def fetch_source(spec: SourceSpec, *, now: datetime, slot: datetime, lines: Iterable[str] = (),
                 transport: httpx.BaseTransport | None = None, deadline: float | None = None,
                 monotonic: Callable[[], float] = time.monotonic, lookback: timedelta = LOOKBACK,
                 user_agent: str | None = None) -> SourceResult:
    """Fetch and parse every feed of one federal source. A feed that fails is flagged and skipped;
    a missing contact user agent (BLS) raises MissingCredential before any request."""
    started = monotonic()
    result = SourceResult(spec.key)
    assert result.report is not None
    agent = GENERIC_USER_AGENT
    if spec.contact_ua:
        agent = credentials.sec_user_agent() if user_agent is None else credentials.check_sec_user_agent(user_agent)
    guard = _HostGuard()
    with httpx.Client(transport=transport, timeout=FETCH_TIMEOUT, follow_redirects=True,
                      headers=request_headers(agent),
                      event_hooks={"request": [guard.request], "response": [guard.response]}) as http:
        for feed in spec.feeds:
            if _deadline_passed(deadline, monotonic):
                result.flag(f"news_source_error:{spec.key}:budget")
                break
            guard.status = None
            try:
                response = get_with_retry(http, feed.url, what=f"news {spec.key} {feed.label}",
                                          retries=RETRIES, backoff_s=BACKOFF_S, fail_fast_429=True)
                parse_source_feed(spec, feed, response.content, now=now, slot=slot, lines=lines,
                                  result=result, lookback=lookback)
            except (DataError, httpx.HTTPError) as exc:
                result.report.feeds_failed += 1
                result.report.error = error_type(exc, guard.status)
                result.flag(f"news_source_error:{spec.key}:{result.report.error}")
                continue
            result.report.feeds_ok += 1
    result.report.seconds = round(monotonic() - started, 3)
    return result


SourceTask = Callable[[float], SourceResult]
"""A source to run: called with the monotonic deadline, returns its result (may raise)."""


def default_tasks(policy: Any, *, now: datetime, slot: datetime, state_dir: Path | None = None,
                  transport: httpx.BaseTransport | None = None, sec: Any | None = None,
                  sec_factory: Callable[[], Any] | None = None,
                  monotonic: Callable[[], float] = time.monotonic,
                  lookback: timedelta = LOOKBACK) -> dict[str, SourceTask]:
    """One task per federal source, plus SEC filings when the policy has stock lines."""
    lines = frozenset(line.symbol for line in policy.universe.lines)
    tasks: dict[str, SourceTask] = {}
    if policy.universe.stock_lines():
        from council.stocks import sec_news

        def sec_task(deadline: float) -> SourceResult:
            return sec_news.fetch_sec_news(policy, now=now, slot=slot, sec=sec, sec_factory=sec_factory,
                                           state_dir=state_dir, deadline=deadline, monotonic=monotonic,
                                           lookback=lookback)

        tasks["sec"] = sec_task
    for key in GOV_SOURCES:
        spec = SOURCES[key]

        def gov_task(deadline: float, _spec: SourceSpec = spec) -> SourceResult:
            return fetch_source(_spec, now=now, slot=slot, lines=lines, transport=transport,
                                deadline=deadline, monotonic=monotonic, lookback=lookback)

        tasks[key] = gov_task
    return tasks


def merge_items(items: Iterable[NewsItem], *, slot: datetime, lookback: timedelta = LOOKBACK) -> list[NewsItem]:
    """Unique by id (the later availability wins: never earlier than any source says), inside
    [slot - lookback, slot), newest first, ties by id."""
    cutoff = _as_utc(slot)
    best: dict[str, NewsItem] = {}
    for item in items:
        at = _as_utc(item.available_at)
        if not cutoff - lookback <= at < cutoff:
            continue
        held = best.get(item.id)
        if held is None or at > _as_utc(held.available_at):
            best[item.id] = item
    return sorted(best.values(), key=lambda n: (-_as_utc(n.published_at).timestamp(), n.id))


def gather_public_news(policy: Any, now: datetime, state_dir: Path | None = None, *,
                       slot: datetime | None = None, budget_s: float = TOTAL_BUDGET_S,
                       sources: Mapping[str, SourceTask] | None = None,
                       transport: httpx.BaseTransport | None = None, sec: Any | None = None,
                       sec_factory: Callable[[], Any] | None = None,
                       monotonic: Callable[[], float] = time.monotonic,
                       lookback: timedelta = LOOKBACK) -> NewsFetch:
    """Every public-domain item usable at `slot` (default: `now`), fetched concurrently within
    `budget_s`. Never raises for a data problem: a failing or late source becomes a flag."""
    if now.tzinfo is None or (slot is not None and slot.tzinfo is None):
        raise ValueError("naive datetime; council code uses aware UTC datetimes only")
    cut = slot if slot is not None else now
    tasks = dict(sources) if sources is not None else default_tasks(
        policy, now=now, slot=cut, state_dir=state_dir, transport=transport, sec=sec,
        sec_factory=sec_factory, monotonic=monotonic, lookback=lookback)
    deadline = monotonic() + float(budget_s)
    outcomes: dict[str, SourceResult | BaseException] = {}
    lock = threading.Lock()

    def run(name: str, task: SourceTask) -> None:
        try:
            value: SourceResult | BaseException = task(deadline)
        except BaseException as exc:  # noqa: BLE001 - one source's failure never costs another's items
            value = exc
        with lock:
            outcomes[name] = value

    threads = {name: threading.Thread(target=run, args=(name, task), name=f"news-{name}", daemon=True)
               for name, task in tasks.items()}
    for thread in threads.values():
        thread.start()
    for thread in threads.values():
        thread.join(timeout=max(0.0, deadline - monotonic()))

    order = {name: i for i, name in enumerate(SOURCE_ORDER)}
    flags: list[str] = []
    reports: dict[str, SourceReport] = {}
    collected: list[NewsItem] = []
    with lock:
        snapshot = dict(outcomes)
    for name in sorted(tasks, key=lambda n: (order.get(n, len(order)), n)):
        outcome = snapshot.get(name)
        if outcome is None:                              # still running at the deadline
            flags.append(f"news_source_error:{name}:budget")
            reports[name] = SourceReport(source=name, error="budget")
        elif isinstance(outcome, BaseException):
            kind = error_type(outcome)
            flags.append(f"news_source_error:{name}:{kind}")
            reports[name] = SourceReport(source=name, error=kind)
        else:
            flags += outcome.flags
            collected += outcome.items
            if outcome.report is not None:
                reports[name] = outcome.report
    return NewsFetch(items=merge_items(collected, slot=cut, lookback=lookback),
                     flags=list(dict.fromkeys(flags)), sources=reports)
