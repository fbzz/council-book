"""Scheduled macro events: FOMC from the frozen policy calendar; CPI/NFP/PCE from FRED release dates.

Rules:
- FOMC decisions come from policy/calendar-2026.yaml (severity 3), always.
- CPI (FRED release 10, severity 3), NFP (release 50, severity 2) and PCE (release 54, severity 2)
  are loaded ONLY when a FRED API key is given; otherwise they are skipped with a quality flag.
  All three are released at 08:30 America/New_York.
- A release that fails to load is skipped with a quality flag; FOMC events still apply.
- The FRED key is a query parameter, so no URL and no key ever reaches an error or a flag.
- Scheduled events are public in advance; they are admitted by their schedule, not their time.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Literal

import httpx
import pandas as pd

from council.data.bars import NEW_YORK, to_utc
from council.data.http import DataError, client_scope, get_with_retry, json_body
from council.facts.evidence_ids import event_id
from council.models.facts import EventItem
from council.policy import Policy

RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"
ReleaseKind = Literal["cpi", "nfp", "pce"]
RELEASES: dict[int, tuple[ReleaseKind, int]] = {10: ("cpi", 3), 50: ("nfp", 2), 54: ("pce", 2)}
RELEASE_TIME_NY = time(8, 30)
FOMC_SEVERITY = 3
RELEASE_DATES_LIMIT = 120


def fomc_events(policy: Policy, start: datetime, end: datetime) -> list[EventItem]:
    """FOMC decision times from the policy calendar within [start, end]."""
    lo, hi = to_utc(start), to_utc(end)
    out = []
    for raw in policy.calendar.get("fomc_decisions_utc", []) or []:
        at = to_utc(str(raw))
        if lo <= at <= hi:
            out.append(
                EventItem(
                    id=event_id("fomc", at.to_pydatetime()),
                    kind="fomc",
                    at_utc=at.to_pydatetime(),
                    symbols=[],
                    binary=True,
                    severity=FOMC_SEVERITY,
                    source="policy_calendar",
                )
            )
    return out


def release_time_utc(day: date) -> datetime:
    """08:30 New York on `day`, in UTC (12:30Z in summer time, 13:30Z in winter)."""
    local = pd.Timestamp(datetime.combine(day, RELEASE_TIME_NY)).tz_localize(NEW_YORK)
    return local.tz_convert("UTC").to_pydatetime()


def parse_release_dates(payload: Any) -> list[date]:
    """Dates from a fred/release/dates JSON payload; malformed rows are skipped."""
    if not isinstance(payload, dict) or not isinstance(payload.get("release_dates"), list):
        raise DataError("fred release dates: unexpected payload")
    out: set[date] = set()
    for row in payload["release_dates"]:
        try:
            out.add(date.fromisoformat(str(row["date"])[:10]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def fetch_release_dates(release_id: int, *, fred_key: str, client: httpx.Client) -> list[date]:
    """Recent and scheduled (future) dates of one FRED release (needs an API key)."""
    params = {
        "release_id": release_id,
        "api_key": fred_key,
        "file_type": "json",
        "include_release_dates_with_no_data": "true",
        "sort_order": "desc",
        "limit": RELEASE_DATES_LIMIT,
    }
    what = f"fred release dates {release_id}"
    response = get_with_retry(client, RELEASE_DATES_URL, what=what, params=params)
    return parse_release_dates(json_body(response, what=what))


def load_events(
    policy: Policy,
    start: datetime,
    end: datetime,
    *,
    fred_key: str | None = None,
    client: httpx.Client | None = None,
) -> tuple[list[EventItem], list[str]]:
    """(events sorted by time then ID, quality flags)."""
    events = fomc_events(policy, start, end)
    flags: list[str] = []
    if not fred_key:
        flags.append("calendar:release_dates_skipped_no_fred_key")
    else:
        lo, hi = to_utc(start), to_utc(end)
        with client_scope(client) as http:
            for release_id, (kind, severity) in RELEASES.items():
                try:
                    days = fetch_release_dates(release_id, fred_key=fred_key, client=http)
                except DataError:
                    flags.append(f"calendar:release_dates_failed:{kind}")
                    continue
                for day in days:
                    at = release_time_utc(day)
                    if lo <= to_utc(at) <= hi:
                        events.append(
                            EventItem(
                                id=event_id(kind, at),
                                kind=kind,
                                at_utc=at,
                                symbols=[],
                                binary=True,
                                severity=severity,
                                source=f"fred_release_{release_id}",
                            )
                        )
    unique = {e.id: e for e in events}
    return sorted(unique.values(), key=lambda e: (e.at_utc, e.id)), flags


def macro_events(
    policy: Policy,
    start: datetime,
    end: datetime,
    *,
    fred_key: str | None = None,
    client: httpx.Client | None = None,
) -> list[EventItem]:
    """Scheduled macro events in [start, end]; see load_events for the quality flags."""
    events, _ = load_events(policy, start, end, fred_key=fred_key, client=client)
    return events
