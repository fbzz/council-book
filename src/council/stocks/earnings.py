"""Earnings windows of the stock lines (design §10, D12): `EventItem(kind="earnings")` per stock
line, read by R16 (`council.risk.churn.event_block`, per symbol: an earnings event blocks adds only
on the stock it names). SEC EDGAR supplies them before the broker token exists; the broker's news
feed overrides the estimate once it does.

Rules:
- Coverage: every stock line of the policy (the held names are its selected and retiring lines; the
  shortlist is where council adds and swaps-in come from), at most MAX_LINES (25) a cycle: selected,
  then retiring, then shortlist, each by rank. A line beyond the cap gets `earnings_skipped:<line>`.
- Filings: the filer's SEC submissions (`SecClient.submissions`: TTL 20 h, the cache the
  fundamentals facts share). A RELEASE is an 8-K listing Item 2.02 (results of operations). A filer
  with no such 8-K among its visible filings releases its results in the 10-Q/10-K itself, so for it
  those count as releases (the 8-K / 10-Q cadence). Acceptance times are New York wall time
  (`council.stocks.fundamentals.acceptance_times`: later, never earlier, if SEC's stamp were UTC).
- Lookahead: only releases accepted at or before the slot are visible. A later one neither
  confirms the report nor ends the estimate, and the pack drops any event known after the slot.
- Occurred: the latest visible release is an event at its acceptance time (`source="sec_8k"`, or
  `"sec_periodic"`), known from then. R16 blocks adds on the line from then until 30 h later or
  until the first completed daily bar after it is usable, whichever is later.
- Estimate (`source="sec_estimate"`): with L the latest visible release, the next report is expected
  one year after its counterpart a year earlier: the earliest (release + 364 days, same New York
  wall time, so the same weekday) after L + REPORTED_WITHIN (45 days) and no later than
  L + UPCOMING_MAX (150 days). Without one (less than a year of history, or that quarter missing
  from it), L + CADENCE_DAYS (91). A release at most 45 days before an estimated date is that
  quarter's report, so the estimate moves on to the next quarter by construction. R16 blocks adds
  over earnings_estimate_window_days US trading days either side of it (plus the before and after
  hours). An estimate whose whole window has passed without a release gets `earnings_overdue:<line>`
  and no event; a line with no visible release at all gets `earnings_unknown:<line>`.
- Feed override (once the broker news feed exists): the newest feed item with an `earningsDate`
  whose symbols map to exactly one stock line (by line id or vehicle symbol) replaces that line's
  estimate when its date falls where an estimate may fall (after L + 45 days and within 150 days of
  L; without L, from 45 days before the slot to 150 days after it); `source="etoro_feed"`, known
  from the item's availability, which must be before the slot. A date-only stamp (midnight UTC) is
  placed at 08:00 New York on that date for a report before the open, else at 16:00 New York. Only
  the date is used, never the item's text.
- Returned: the events whose R16 window overlaps [start, end]; the pack then applies its own
  admission rule. Severity 2, binary.
- Nothing here raises for a data problem: `earnings_unavailable:no_sec_user_agent`,
  `earnings_failed:<line>`, `earnings_time_budget:<line>`. The SEC user agent never appears in a
  flag, a log line or an error. Event IDs name the line and the date only
  (`E:earnings:<line>@<date>`); SEC data are US public domain.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from council.data.credentials import MissingCredential
from council.data.http import DataError
from council.facts.evidence_ids import event_id
from council.models.facts import EventItem, NewsItem
from council.policy import LineSpec, Policy
from council.risk.churn import EARNINGS_ESTIMATE_SOURCE, event_window
from council.stocks.fundamentals import acceptance_times
from council.stocks.sec import recent_filings

NEW_YORK = ZoneInfo("America/New_York")
SOURCE_RELEASE = "sec_8k"
SOURCE_PERIODIC = "sec_periodic"
SOURCE_ESTIMATE = EARNINGS_ESTIMATE_SOURCE
SOURCE_FEED = "etoro_feed"
SEVERITY = 2
RELEASE_FORM = "8-K"
RELEASE_ITEM = "2.02"
PERIODIC_FORMS = frozenset({"10-Q", "10-K"})
YEAR_DAYS = 364
CADENCE_DAYS = 91
REPORTED_WITHIN = timedelta(days=45)
UPCOMING_MAX = timedelta(days=150)
MAX_LINES = 25
TIME_BUDGET_S = 60.0
ROLE_ORDER: Mapping[str, int] = {"selected": 0, "retiring": 1, "shortlist": 2}
BEFORE_OPEN_NY = dtime(8, 0)
AFTER_CLOSE_NY = dtime(16, 0)


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime; council code uses aware UTC datetimes only")
    return ts.astimezone(UTC)


# ------------------------------------------------------------------------------------ filings


@dataclass(frozen=True)
class FilingHistory:
    """A filer's release history from its recent SEC filings (acceptance times, UTC, ascending)."""

    releases: tuple[datetime, ...]          # 8-K listing Item 2.02
    periodic: tuple[datetime, ...]          # 10-Q and 10-K

    def visible(self, cutoff: datetime) -> tuple[tuple[datetime, ...], str]:
        """(the releases accepted at or before `cutoff`, their event source): the 8-K Item 2.02
        releases, or the periodic reports for a filer with none."""
        releases = tuple(a for a in self.releases if a <= cutoff)
        if releases:
            return releases, SOURCE_RELEASE
        return tuple(p for p in self.periodic if p <= cutoff), SOURCE_PERIODIC


def _items(raw: Any) -> set[str]:
    return {part.strip() for part in str(raw or "").split(",") if part.strip()}


def filing_history(submissions: Mapping[str, Any]) -> FilingHistory:
    """The 8-K Item 2.02 and 10-Q/10-K acceptance times in a (trimmed) submissions document.
    Amendments (8-K/A, 10-Q/A) and rows without an acceptance time are left out."""
    stamps = acceptance_times(submissions)
    releases: set[datetime] = set()
    periodic: set[datetime] = set()
    for row in recent_filings(submissions):
        at = stamps.get(str(row.get("accessionNumber") or ""))
        if at is None:
            continue
        form = str(row.get("form") or "").strip().upper()
        if form == RELEASE_FORM and RELEASE_ITEM in _items(row.get("items")):
            releases.add(at)
        elif form in PERIODIC_FORMS:
            periodic.add(at)
    return FilingHistory(tuple(sorted(releases)), tuple(sorted(periodic)))


# ------------------------------------------------------------------------------------ estimate


def plus_days(ts: datetime, days: int) -> datetime:
    """`ts` + `days` calendar days at the same New York wall time (364 keeps the weekday)."""
    return (_utc(ts).astimezone(NEW_YORK) + timedelta(days=days)).astimezone(UTC)


def estimate_next(releases: Sequence[datetime]) -> tuple[datetime, datetime] | None:
    """(the estimated time of the report after the latest release, the release it comes from), or
    None without any release. See the module rules."""
    if not releases:
        return None
    ordered = sorted(_utc(r) for r in releases)
    last = ordered[-1]
    lo, hi = last + REPORTED_WITHIN, last + UPCOMING_MAX
    for release in ordered:
        candidate = plus_days(release, YEAR_DAYS)
        if lo < candidate <= hi:
            return candidate, release
    return plus_days(last, CADENCE_DAYS), last


def upcoming_range(last: datetime | None, cutoff: datetime) -> tuple[datetime, datetime]:
    """(lo, hi]: where the date of the report after `last` may fall."""
    if last is None:
        return cutoff - REPORTED_WITHIN, cutoff + UPCOMING_MAX
    return last + REPORTED_WITHIN, last + UPCOMING_MAX


# ------------------------------------------------------------------------------------ feed


@dataclass(frozen=True)
class FeedDate:
    """A report date from the broker's news feed and when the item became available."""

    at: datetime
    known_at: datetime


def feed_report_time(when: datetime, before_open: bool | None) -> datetime:
    """The feed's `earningsDate` as a report time: kept when it carries a time of day; a date-only
    stamp (midnight UTC, or midnight New York when the feed wrote the date in exchange time)
    becomes 08:00 New York on that date for a report before the open, else 16:00 New York (after
    the close, or not said). Reading a New York midnight as the report time would put the reaction
    bar on the wrong day and end the window up to a day early."""
    utc = _utc(when)
    local = BEFORE_OPEN_NY if before_open is True else AFTER_CLOSE_NY
    if utc.time() == dtime(0):
        return datetime.combine(utc.date(), local, tzinfo=NEW_YORK).astimezone(UTC)
    new_york = utc.astimezone(NEW_YORK)
    if new_york.time() == dtime(0):
        return datetime.combine(new_york.date(), local, tzinfo=NEW_YORK).astimezone(UTC)
    return utc


def feed_dates(policy: Policy, news: Iterable[NewsItem], cutoff: datetime) -> dict[str, FeedDate]:
    """{stock line: the report date of its newest feed item available before `cutoff`}. An item
    counts only when its symbols map to exactly one stock line of the policy."""
    owners = policy.universe.vehicle_map()
    stocks = {line.symbol for line in policy.universe.stock_lines()}
    best: dict[str, tuple[tuple[datetime, datetime, str], FeedDate]] = {}
    for item in news:
        if item.earnings_date is None or _utc(item.available_at) >= cutoff:
            continue
        mapped = {owners[s] for s in item.symbols if s in owners and owners[s] in stocks}
        if len(mapped) != 1:
            continue
        line = mapped.pop()
        at = feed_report_time(item.earnings_date, item.before_market_open)
        key = (_utc(item.available_at), at, item.id)
        if line not in best or key > best[line][0]:
            best[line] = (key, FeedDate(at=at, known_at=_utc(item.available_at)))
    return {line: value for line, (_, value) in best.items()}


# ------------------------------------------------------------------------------------ events


def _event(line: str, at: datetime, source: str, known_at: datetime) -> EventItem:
    return EventItem(id=event_id("earnings", at, symbol=line), kind="earnings", at_utc=at, symbols=[line],
                     binary=True, severity=SEVERITY, source=source, known_at=known_at)


def line_events(line: str, history: FilingHistory | None, *, cutoff: datetime, policy: Policy,
                feed: FeedDate | None = None) -> tuple[list[EventItem], list[str]]:
    """The earnings events of one stock line at `cutoff` (the slot): the latest visible release,
    and the next report (the feed's date when it may be that report, else the estimate), with the
    line's flags. `history=None` (not fetched) gives no estimate and no flag. Window filtering is
    the caller's (`gather_earnings`)."""
    cutoff = _utc(cutoff)
    releases, source = history.visible(cutoff) if history is not None else ((), SOURCE_RELEASE)
    last = releases[-1] if releases else None
    out: list[EventItem] = []
    if last is not None:
        out.append(_event(line, last, source, last))
    lo, hi = upcoming_range(last, cutoff)
    if feed is not None and lo < _utc(feed.at) <= hi:
        out.append(_event(line, _utc(feed.at), SOURCE_FEED, feed.known_at))
        return out, []
    estimate = estimate_next(releases)
    if estimate is None:
        return out, ([f"earnings_unknown:{line}"] if history is not None else [])
    at, basis = estimate
    event = _event(line, at, SOURCE_ESTIMATE, basis)
    window = event_window(event, policy)
    if window is not None and window[1] < cutoff:
        return out, [f"earnings_overdue:{line}"]
    return [*out, event], []


def covered_lines(policy: Policy) -> tuple[list[LineSpec], list[LineSpec]]:
    """(the stock lines checked this cycle, those beyond MAX_LINES): selected, retiring, then the
    shortlist, each by rank, then by line id."""

    def key(line: LineSpec) -> tuple[int, int, str]:
        meta = line.stock
        role = ROLE_ORDER.get(meta.role, len(ROLE_ORDER)) if meta is not None else len(ROLE_ORDER)
        rank = meta.rank if meta is not None and meta.rank is not None else 10**9
        return role, rank, line.symbol

    ordered = sorted(policy.universe.stock_lines(), key=key)
    return ordered[:MAX_LINES], ordered[MAX_LINES:]


def _overlaps(event: EventItem, policy: Policy, lo: datetime, hi: datetime) -> bool:
    window = event_window(event, policy)
    return window is not None and window[0] <= hi and window[1] >= lo


def gather_earnings(
    policy: Policy,
    *,
    slot: datetime,
    start: datetime | None = None,
    end: datetime | None = None,
    news: Iterable[NewsItem] = (),
    sec: Any | None = None,
    sec_factory: Callable[[], Any] | None = None,
    state_dir: Path | None = None,
    time_budget_s: float = TIME_BUDGET_S,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[list[EventItem], list[str]]:
    """(earnings events, flags) of the policy's stock lines at `slot`, keeping the events whose R16
    window overlaps [start, end] (default: the slot). `news` is the broker feed (empty before the
    token). The SEC client (default `SecClient(cache_root=<state_dir>/cache)`, which needs the SEC
    user agent) is created at the first line that needs it. Never raises for a data problem."""
    if not policy.universe.stock_lines():
        return [], []
    cutoff = _utc(slot)
    lo = _utc(start) if start is not None else cutoff
    hi = _utc(end) if end is not None else cutoff
    covered, skipped = covered_lines(policy)
    flags = [f"earnings_skipped:{line.symbol}" for line in skipped]
    feed = feed_dates(policy, news, cutoff)
    cache_root = state_dir / "cache" if state_dir is not None else None
    deadline = monotonic() + float(time_budget_s)
    client = sec
    owned = unavailable = False
    events: list[EventItem] = []
    try:
        for line in covered:
            assert line.stock is not None
            history: FilingHistory | None = None
            if unavailable:
                pass
            elif monotonic() >= deadline:
                flags.append(f"earnings_time_budget:{line.symbol}")
            else:
                if client is None:
                    try:
                        if sec_factory is not None:
                            client = sec_factory()
                        else:
                            from council.stocks.sec import SecClient

                            client = SecClient(cache_root=cache_root)
                        owned = True
                    except MissingCredential:
                        flags.append("earnings_unavailable:no_sec_user_agent")
                        unavailable = True
                if client is not None:
                    try:
                        history = filing_history(client.submissions(int(line.stock.cik)))
                    except (DataError, ValueError, KeyError, TypeError):
                        flags.append(f"earnings_failed:{line.symbol}")
            found, line_flags = line_events(line.symbol, history, cutoff=cutoff, policy=policy,
                                            feed=feed.get(line.symbol))
            flags += line_flags
            events += [e for e in found if _overlaps(e, policy, lo, hi)]
    finally:
        if owned and client is not None:
            client.close()
    return sorted(events, key=lambda e: (e.at_utc, e.id)), flags
