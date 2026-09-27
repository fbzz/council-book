"""SEC 8-K / 6-K filing METADATA for the stock lines -> public-domain news items (`P:` ids).

The news role sees that a stock line's company filed a current report and which items it lists,
never the filing's content (transparency design §3.4, T-D7, T-D17).

Rules:
- Lines: every stock line of the policy, the held names (selected, retiring) and the shortlist,
  capped as the earnings estimate caps them (`council.stocks.earnings.covered_lines`). No watch list
  of index heavyweights: that is a separate, open decision (design Q7/U3). While
  `invariants.STOCK_SLEEVE_LIVE` is False the runtime policy has no stock lines, so nothing is
  fetched.
- Sources, both through ONE `SecClient` (the caller's, so the SEC limit of <= 7 requests a second
  is shared with every other SEC call made through it, e.g. the earnings estimate):
  1. SEC's current-filings Atom feed (`getcurrent`, forms 8-K and 6-K; 8-K/A arrives with 8-K),
     filtered by the lines' CIKs. It lists only the newest 100 filings of a form, so
  2. a backfill from each line's submissions document (`fetch_submissions`: the same
     `sec-submissions` cache entry as `SecClient.submissions`, TTL 20 h, i.e. about one request a
     day per line) catches filings the feed no longer shows.
  Every request carries the news timeouts (5 s connect / 10 s read) and at most one retry; the
  deadline is checked before each request, so the fetch stops at the 30 s news budget.
  The same accession from both gives one item (the later of the two times is kept).
- Forms: 8-K, 8-K/A (marked as an amendment) and 6-K; a row without an acceptance time is skipped.
  The feed's acceptance time carries its UTC offset; the submissions time is New York wall time
  (`council.stocks.fundamentals.acceptance_times`).
- Title: "<FORM>: " + "; ".join("Item <code> <official title>") from the fixed map of official Form
  8-K item titles (`ITEM_TITLES`; an unknown code shows as "Item <code>"); a 6-K reads
  "6-K: report of a foreign private issuer". The filer's own description is never used. The summary
  is the company's conformed name (SEC metadata).
- Link: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<TICKER>&type=<FORM>`,
  keyed by ticker, so it carries no digit run (an EDGAR Archives path would name the CIK).
- ID: `P:` + sha256("sec" NUL accession number)[:8]. Attribution: "Source: U.S. Securities and
  Exchange Commission" (not "EDGAR", an SEC registered mark).
- Lookahead, cleaning and the leak scan follow `council.data.gov_news`. A missing SEC user agent
  raises `MissingCredential` before any request (the gatherer turns it into
  `news_source_error:sec:no_user_agent`); the user agent never appears in a flag, a log or an error.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from council.data import gov_news, sanitize
from council.data.gov_news import LOOKBACK, SourceResult, admit_time, clean_field, trips_leak_scan
from council.data.http import DataError, get_with_retry, json_body
from council.models.facts import NewsItem, public_news_id
from council.policy import LineSpec, Policy
from council.stocks.earnings import covered_lines
from council.stocks.fundamentals import acceptance_times
from council.stocks.sec import (
    NS_SUBMISSIONS,
    SUBMISSIONS_URL,
    TTL_SUBMISSIONS_S,
    SecClient,
    recent_filings,
    trim_submissions,
)

SOURCE = "sec"
CURRENT_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb="
               "&owner=include&start=0&count=100&output=atom")
CURRENT_FORMS: tuple[str, ...] = ("8-K", "6-K")
FORMS: frozenset[str] = frozenset({"8-K", "8-K/A", "6-K"})
LINK = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type={form}"
FEED_TIMEOUT = gov_news.FETCH_TIMEOUT
SIX_K_TITLE = "6-K: report of a foreign private issuer"
_ITEM = re.compile(r"\bItem\s+(\d\.\d{2})\b")
_ITEM_CODE = re.compile(r"^\d\.\d{2}$")
_ACCESSION = re.compile(r"accession-number=(\d{10}-\d{2}-\d{6})")
_ATOM_TITLE = re.compile(r"^\s*(?P<form>[^\s].*?)\s+-\s+(?P<company>.+?)\s+\((?P<cik>\d{1,10})\)\s*(?:\([^()]*\)\s*)?$")

# The official Form 8-K item titles, from the form's own text. EDGAR's feed labels differ for 2.05,
# 3.03, 5.02 and 5.08 (tests/fixtures/news/README.md); the feed's labels are never used.
ITEM_TITLES: Mapping[str, str] = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety - Reporting of Shutdowns and Patterns of Violations",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": ("Creation of a Direct Financial Obligation or an Obligation under an Off-Balance Sheet "
             "Arrangement of a Registrant"),
    "2.04": ("Triggering Events That Accelerate or Increase a Direct Financial Obligation or an "
             "Obligation under an Off-Balance Sheet Arrangement"),
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": ("Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard; Transfer of "
             "Listing"),
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": ("Non-Reliance on Previously Issued Financial Statements or a Related Audit Report or "
             "Completed Interim Review"),
    "5.01": "Changes in Control of Registrant",
    "5.02": ("Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain "
             "Officers; Compensatory Arrangements of Certain Officers"),
    "5.03": "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year",
    "5.04": "Temporary Suspension of Trading Under Registrant's Employee Benefit Plans",
    "5.05": "Amendments to the Registrant's Code of Ethics, or Waiver of a Provision of the Code of Ethics",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "6.01": "ABS Informational and Computational Material",
    "6.02": "Change of Servicer or Trustee",
    "6.03": "Change in Credit Enhancement or Other External Support",
    "6.04": "Failure to Make a Required Distribution",
    "6.05": "Securities Act Updating Disclosure",
    "6.06": "Static Pool",
    "6.10": "Alternative Filings of Asset-Backed Issuers",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}


# ------------------------------------------------------------------------------------ filings


@dataclass(frozen=True)
class Filing:
    """One current report: SEC metadata only."""

    accession: str
    cik: int
    form: str
    items: tuple[str, ...]
    accepted_at: datetime
    company: str


def item_codes(raw: Iterable[str] | str | None) -> tuple[str, ...]:
    """Item codes ("2.02") in order of first appearance; anything else is dropped."""
    parts = raw.split(",") if isinstance(raw, str) else list(raw or ())
    seen: dict[str, None] = {}
    for part in parts:
        code = str(part).strip()
        if _ITEM_CODE.match(code):
            seen.setdefault(code, None)
    return tuple(seen)


def parse_current_atom(data: bytes) -> list[Filing]:
    """Every 8-K / 8-K/A / 6-K entry of a current-filings Atom document with a CIK, an accession
    number and an acceptance time (all filers; filtering by CIK is the caller's)."""
    out: list[Filing] = []
    for entry in gov_news.parse_feed(data):
        head = _ATOM_TITLE.match(" ".join(entry.title.split()))
        accession = _ACCESSION.search(entry.key or "")
        stamp = gov_news.parse_stamp(entry.stamp)
        if head is None or accession is None or stamp is None or stamp.date_only:
            continue
        form = head.group("form").strip().upper()
        if form not in FORMS:
            continue
        items = item_codes(_ITEM.findall(sanitize.clean_text(entry.summary, gov_news.RAW_TEXT_MAX)))
        out.append(Filing(accession=accession.group(1), cik=int(head.group("cik")), form=form,
                          items=items if form != "6-K" else (), accepted_at=stamp.published_at,
                          company=head.group("company").strip()))
    return out


def backfill_filings(submissions: Mapping[str, Any], cik: int | None = None) -> list[Filing]:
    """The 8-K / 8-K/A / 6-K rows of a (trimmed) submissions document that have an acceptance time,
    attributed to `cik` (default: the document's own)."""
    if cik is None:
        try:
            cik = int(submissions.get("cik") or 0)
        except (TypeError, ValueError):
            cik = 0
    stamps = acceptance_times(submissions)
    company = str(submissions.get("name") or "")
    out: list[Filing] = []
    for row in recent_filings(submissions):
        form = str(row.get("form") or "").strip().upper()
        accession = str(row.get("accessionNumber") or "")
        at = stamps.get(accession)
        if form not in FORMS or not accession or at is None:
            continue
        out.append(Filing(accession=accession, cik=cik, form=form,
                          items=item_codes(row.get("items")) if form != "6-K" else (),
                          accepted_at=at, company=company))
    return out


def filing_title(form: str, items: Iterable[str]) -> str:
    """The item's title from the form and the official item titles (never the filer's words)."""
    if form == "6-K":
        return SIX_K_TITLE
    label = "8-K/A (amendment)" if form == "8-K/A" else form
    parts = [f"Item {code} {ITEM_TITLES[code]}" if code in ITEM_TITLES else f"Item {code}" for code in items]
    return f"{label}: " + "; ".join(parts) if parts else f"{label}: current report"


def filing_link(ticker: str, form: str) -> str:
    """The ticker-keyed EDGAR company page for the form (no CIK, no digit run)."""
    return LINK.format(ticker=quote(ticker, safe=""), form=quote(form, safe=""))


def filing_item(filing: Filing, line: LineSpec, *, now: datetime, slot: datetime, result: SourceResult,
                lookback: timedelta = LOOKBACK) -> NewsItem | None:
    """One filing of a stock line -> NewsItem, or None (the drop is counted on `result`)."""
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
    link, dropped = gov_news.public_link(filing_link(line.signal.ticker, filing.form))
    if dropped:
        assert result.report is not None
        result.report.links_dropped += 1
        result.flag(f"news_link_dropped:{SOURCE}")
    return NewsItem(
        id=public_news_id(SOURCE, filing.accession), title=title, summary=summary, symbols=[line.symbol],
        published_at=stamp.published_at, available_at=stamp.available_at, source=SOURCE,
        licence="public_domain", link=link, form=filing.form,  # type: ignore[arg-type]
        items=list(filing.items),
    )


# ------------------------------------------------------------------------------------ fetch


class _Timed:
    """The client's own paced session with the news timeouts: every request still passes the
    client's request hook, which takes a token from its one rate-limit bucket."""

    def __init__(self, http: httpx.Client, timeout: httpx.Timeout) -> None:
        self._http, self._timeout = http, timeout

    def get(self, url: str, *, params: Any = None, headers: Any = None) -> httpx.Response:
        return self._http.get(url, params=params, headers=headers, timeout=self._timeout)


def fetch_current(client: SecClient, form: str) -> list[Filing]:
    """One current-filings Atom page for `form`, through the client's limiter."""
    http = client._http  # the client's paced session (its request hook is the shared limiter)
    response = get_with_retry(_Timed(http, FEED_TIMEOUT), CURRENT_URL.format(form=quote(form, safe="")),
                              what=f"sec current {form}", retries=gov_news.RETRIES,
                              backoff_s=gov_news.BACKOFF_S, fail_fast_429=True)
    return parse_current_atom(response.content)


def fetch_submissions(client: SecClient, cik: int) -> dict[str, Any]:
    """A line's trimmed submissions document for the backfill: the SAME cache entry as
    `SecClient.submissions` (namespace, key, TTL 20 h, trim), so the earnings estimate and this
    backfill share about one request a day per line; on a miss it is fetched through the client's
    paced session with the news timeouts (5 s connect / 10 s read) and at most one retry, not the
    client's own 20 s / three retries, so one slow request cannot hold the news fetch."""
    number = int(cik)
    if not 0 < number < 10**10:
        raise ValueError("bad CIK")
    url = SUBMISSIONS_URL.format(cik=number)
    what = f"sec submissions {number:010d}"

    def fetch() -> dict[str, Any]:
        response = get_with_retry(_Timed(client._http, FEED_TIMEOUT), url, what=what,
                                  retries=gov_news.RETRIES, backoff_s=gov_news.BACKOFF_S, fail_fast_429=True)
        return trim_submissions(json_body(response, what=what))

    cache = client._cache(NS_SUBMISSIONS)  # the client's own cache root
    return cache.get_or_fetch({"url": url}, TTL_SUBMISSIONS_S, fetch, fmt="json.gz")


def stock_lines_by_cik(policy: Policy) -> dict[int, LineSpec]:
    """{CIK: stock line} for the covered lines (held first, then the shortlist)."""
    covered, _ = covered_lines(policy)
    return {int(line.stock.cik): line for line in covered if line.stock is not None}


def fetch_sec_news(policy: Policy, *, now: datetime, slot: datetime | None = None, sec: Any | None = None,
                   sec_factory: Callable[[], Any] | None = None, state_dir: Path | None = None,
                   deadline: float | None = None, monotonic: Callable[[], float] = time.monotonic,
                   lookback: timedelta = LOOKBACK) -> SourceResult:
    """8-K / 6-K items of the policy's stock lines usable at `slot` (default `now`). The SEC client is
    the caller's `sec`, else `sec_factory()`, else a new `SecClient(cache_root=<state_dir>/cache)`,
    which raises MissingCredential (no request made) when the user agent is not configured."""
    started = monotonic()
    cut = slot if slot is not None else now
    result = SourceResult(SOURCE)
    assert result.report is not None
    lines = stock_lines_by_cik(policy)
    if not lines:
        return result
    client, owned = sec, False
    if client is None:
        client = sec_factory() if sec_factory is not None else SecClient(
            cache_root=state_dir / "cache" if state_dir is not None else None)
        owned = True
    found: dict[str, tuple[Filing, LineSpec]] = {}

    def keep(filing: Filing) -> None:
        line = lines.get(filing.cik)
        if line is None:
            return
        held = found.get(filing.accession)
        if held is None or filing.accepted_at > held[0].accepted_at:
            found[filing.accession] = (filing, line)

    try:
        for form in CURRENT_FORMS:
            if deadline is not None and monotonic() >= deadline:
                result.flag(f"news_source_error:{SOURCE}:budget")
                break
            try:
                filings = fetch_current(client, form)
            except (DataError, httpx.HTTPError) as exc:
                result.report.feeds_failed += 1
                result.report.error = gov_news.error_type(exc)
                result.flag(f"news_source_error:{SOURCE}:{result.report.error}")
                continue
            result.report.feeds_ok += 1
            result.report.entries += len(filings)
            for filing in filings:
                keep(filing)
        for cik in lines:
            if deadline is not None and monotonic() >= deadline:
                result.flag(f"news_source_error:{SOURCE}:budget")
                break
            try:
                filings = backfill_filings(fetch_submissions(client, cik), cik)
            except (DataError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                result.report.feeds_failed += 1
                result.report.error = gov_news.error_type(exc)
                result.flag(f"news_source_error:{SOURCE}:backfill_{result.report.error}")
                continue
            result.report.feeds_ok += 1
            for filing in filings:
                keep(filing)
    finally:
        if owned:
            client.close()
    for filing, line in sorted(found.values(), key=lambda fl: (fl[0].accepted_at, fl[0].accession)):
        item = filing_item(filing, line, now=now, slot=cut, result=result, lookback=lookback)
        if item is not None:
            result.items.append(item)
            result.report.kept += 1
    result.report.seconds = round(monotonic() - started, 3)
    return result
