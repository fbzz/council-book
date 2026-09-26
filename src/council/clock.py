"""Slot grid and market sessions. All times are UTC; the grid is immune to DST.

Slots: 02:40, 06:40, 10:40, 14:40, 18:40, 22:40 UTC. launchd fires hourly at :40 and the cycle
runs only on slot hours. A cycle that starts late (<= 120 min) runs flagged `late`; later is `missed`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

SLOT_HOURS = (2, 6, 10, 14, 18, 22)
SLOT_MINUTE = 40
LATE_MAX = timedelta(minutes=120)
NEW_YORK = ZoneInfo("America/New_York")

SlotStatus = Literal["on_time", "late", "missed"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime; council code uses aware UTC datetimes only")
    return ts.astimezone(UTC)


def slot_at_or_before(ts: datetime) -> datetime:
    """The most recent slot start at or before `ts`."""
    ts = _as_utc(ts)
    candidates = []
    for day_offset in (0, -1):
        d = (ts + timedelta(days=day_offset)).date()
        for hour in SLOT_HOURS:
            candidates.append(datetime.combine(d, time(hour, SLOT_MINUTE), tzinfo=UTC))
    return max(c for c in candidates if c <= ts)


def next_slot(ts: datetime) -> datetime:
    return slot_at_or_before(ts) + timedelta(hours=4)


@dataclass(frozen=True)
class SlotInfo:
    slot: datetime
    late_by: timedelta
    status: SlotStatus

    @property
    def cycle_id(self) -> str:
        return cycle_id_for(self.slot)


def classify(ts: datetime) -> SlotInfo:
    slot = slot_at_or_before(ts)
    late_by = _as_utc(ts) - slot
    if late_by <= timedelta(minutes=10):
        status: SlotStatus = "on_time"
    elif late_by <= LATE_MAX:
        status = "late"
    else:
        status = "missed"
    return SlotInfo(slot=slot, late_by=late_by, status=status)


def cycle_id_for(slot: datetime) -> str:
    """Public key for a cycle, e.g. 2026-10-01T1440Z."""
    return _as_utc(slot).strftime("%Y-%m-%dT%H%MZ")


def proposal_valid_until(slot: datetime) -> datetime:
    """Proposals expire 5 minutes before the next slot (the next cycle supersedes anyway). A
    rebalance may expire earlier: its deadline is its latest leg's (`leg_valid_until`)."""
    return next_slot(_as_utc(slot)) - NEXT_SLOT_MARGIN


# ---------------------------------------------------------------------------- market sessions
# Exchange calendars (sources: nyse.com hours-calendars; gov.uk bank holidays, England and Wales).
# US cash equities/ETFs: 09:30-16:00 New York, 13:00 on early-close days. LSE: 08:00-16:30 London,
# 12:30 on Christmas Eve and New Year's Eve. Every time is converted through zoneinfo, so the
# weeks in which only one side of the Atlantic is on summer time come out right.
# The calendars end at CALENDAR_LAST_DAY. Past it an OPEN is refused (flag calendar_missing:<year>)
# while a CLOSE uses weekday regular hours (a holiday then ends in broker status 11, which the
# executor handles as waiting_for_market). tests/unit/test_clock.py fails from 1 October of the last
# year unless the next year has been added here.
CALENDAR_LAST_DAY = date(2027, 12, 31)
US_HOLIDAYS: frozenset[date] = frozenset({
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
})
US_EARLY_CLOSES: frozenset[date] = frozenset({   # 13:00 New York
    date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26),
})
UK_HOLIDAYS: frozenset[date] = frozenset({
    date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 4), date(2026, 5, 25),
    date(2026, 8, 31), date(2026, 12, 25), date(2026, 12, 28),
    date(2027, 1, 1), date(2027, 3, 26), date(2027, 3, 29), date(2027, 5, 3), date(2027, 5, 31),
    date(2027, 8, 30), date(2027, 12, 27), date(2027, 12, 28),
})
LSE_EARLY_CLOSES: frozenset[date] = frozenset({  # 12:30 London
    date(2026, 12, 24), date(2026, 12, 31), date(2027, 12, 24), date(2027, 12, 31),
})
US_HOLIDAYS_2026 = US_HOLIDAYS          # former names, kept for importers
UK_HOLIDAYS_2026 = UK_HOLIDAYS

LONDON = ZoneInfo("Europe/London")
US_HOURS = (time(9, 30), time(16, 0))
US_EARLY_CLOSE = time(13, 0)
LSE_HOURS = (time(8, 0), time(16, 30))
LSE_EARLY_CLOSE = time(12, 30)
FX_BREAK = (time(17, 0), time(18, 0))   # New York: daily break; Friday 17:00 closes the week
# FX/index/commodity CFD trade dates with no session (month, day): the session that would open at
# 18:00 New York the evening before and close at 17:00 on that day does not run.
FX_CLOSED_DAYS: frozenset[tuple[int, int]] = frozenset({(12, 25), (1, 1)})

Session = Literal["us", "lse", "fx24x5", "crypto"]
SESSIONS: frozenset[str] = frozenset({"us", "lse", "fx24x5", "crypto"})
CALENDAR_SESSIONS: frozenset[str] = frozenset({"us", "lse"})

# Validity margins. A leg expires LEG_CLOSE_MARGIN before its session closes and every proposal
# NEXT_SLOT_MARGIN before the next slot; approval drops a leg whose market is closed now or closes
# within APPROVAL_CLOSE_MARGIN.
NEXT_SLOT_MARGIN = timedelta(minutes=5)
LEG_CLOSE_MARGIN = timedelta(minutes=10)
APPROVAL_CLOSE_MARGIN = timedelta(minutes=5)


def calendar_known(day: date) -> bool:
    """True while `day` is inside the exchange calendars above."""
    return day <= CALENDAR_LAST_DAY


def _local(session: str, ts: datetime) -> datetime:
    return _as_utc(ts).astimezone(LONDON if session == "lse" else NEW_YORK)


def calendar_missing(session: str | None, ts: datetime) -> str | None:
    """`calendar_missing:<year>` when the session's local day at `ts` is past the calendars."""
    if session not in CALENDAR_SESSIONS:
        return None
    day = _local(session, ts).date()  # type: ignore[arg-type]
    return None if calendar_known(day) else f"calendar_missing:{day.year}"


def session_hours(session: str, day: date, *, closing: bool = False) -> tuple[datetime, datetime] | None:
    """(open, close) in UTC of the US or LSE session on its local `day`, or None when the exchange
    is shut all day. Past CALENDAR_LAST_DAY: weekday regular hours when `closing`, else None."""
    if session not in CALENDAR_SESSIONS:
        raise ValueError(f"no day calendar for session {session!r}")
    if day.weekday() >= 5:
        return None
    zone = LONDON if session == "lse" else NEW_YORK
    hours = LSE_HOURS if session == "lse" else US_HOURS
    if not calendar_known(day):
        if not closing:
            return None
        start, end = hours
    else:
        holidays = UK_HOLIDAYS if session == "lse" else US_HOLIDAYS
        if day in holidays:
            return None
        start, end = hours
        if session == "lse" and day in LSE_EARLY_CLOSES:
            end = LSE_EARLY_CLOSE
        elif session == "us" and day in US_EARLY_CLOSES:
            end = US_EARLY_CLOSE
    return (datetime.combine(day, start, tzinfo=zone).astimezone(UTC),
            datetime.combine(day, end, tzinfo=zone).astimezone(UTC))


def _day_session_open(session: str, ts: datetime, *, closing: bool) -> bool:
    ts = _as_utc(ts)
    hours = session_hours(session, _local(session, ts).date(), closing=closing)
    return hours is not None and hours[0] <= ts < hours[1]


def us_equity_open(ts: datetime, *, closing: bool = False) -> bool:
    return _day_session_open("us", ts, closing=closing)


def lse_open(ts: datetime, *, closing: bool = False) -> bool:
    """London Stock Exchange cash session 08:00-16:30 London time (12:30 on its half days)."""
    return _day_session_open("lse", ts, closing=closing)


def fx_closed_day(trade_date: date) -> bool:
    """An FX/index/commodity CFD trade date without a session (25 December, 1 January)."""
    return (trade_date.month, trade_date.day) in FX_CLOSED_DAYS


def fx_open(ts: datetime) -> bool:
    """FX/index/commodity CFDs: roughly Sunday 18:00 to Friday 17:00 New York, with a daily break
    17:00-18:00 NY, and no session on the trade dates in FX_CLOSED_DAYS (a session from 18:00 NY
    belongs to the next day's trade date). Eligibility and live rates are the final word; this only
    avoids dead proposals."""
    local = _as_utc(ts).astimezone(NEW_YORK)
    wd, t = local.weekday(), local.time()
    trade_date = local.date() + timedelta(days=1) if t >= FX_BREAK[1] else local.date()
    if fx_closed_day(trade_date):
        return False
    if wd == 5:
        return False
    if wd == 6:
        return t >= FX_BREAK[1]
    if wd == 4 and t >= FX_BREAK[0]:
        return False
    return not (FX_BREAK[0] <= t < FX_BREAK[1])


def session_open(session: str, ts: datetime, *, closing: bool = False) -> bool:
    """Is `session` trading at `ts`? `closing=True` asks for an order that only reduces a position:
    past the calendars it gets weekday regular hours instead of a refusal."""
    if session == "crypto":
        return True
    if session == "us":
        return us_equity_open(ts, closing=closing)
    if session == "lse":
        return lse_open(ts, closing=closing)
    if session == "fx24x5":
        return fx_open(ts)
    raise ValueError(f"unknown session {session!r}")


def session_close_after(session: str, ts: datetime) -> datetime | None:
    """The first close of `session` strictly after `ts`: the end of the session in progress, else
    of the next one (the FX daily break counts as a close; FX_CLOSED_DAYS have none). None for
    crypto, which never closes. Past the calendars the weekday regular close is used."""
    ts = _as_utc(ts)
    if session == "crypto":
        return None
    if session not in SESSIONS:
        raise ValueError(f"unknown session {session!r}")
    zone = NEW_YORK if session in ("us", "fx24x5") else LONDON
    day = ts.astimezone(zone).date()
    for offset in range(14):
        d = day + timedelta(days=offset)
        if session == "fx24x5":
            if d.weekday() >= 5 or fx_closed_day(d):
                continue
            close = datetime.combine(d, FX_BREAK[0], tzinfo=NEW_YORK).astimezone(UTC)
        else:
            hours = session_hours(session, d, closing=True)
            if hours is None:
                continue
            close = hours[1]
        if close > ts:
            return close
    raise ValueError(f"no {session} close within two weeks of {ts.isoformat()}")  # pragma: no cover


def next_session_close(session: str, ts: datetime) -> datetime | None:
    """The close of the first session that OPENS strictly after `ts` (weekday hours past the
    calendars; FX reopens at 18:00 New York Sunday to Thursday, except into a trade date in
    FX_CLOSED_DAYS). None for crypto. An order the broker holds for a closed market has had a full
    session to fill by then."""
    ts = _as_utc(ts)
    if session == "crypto":
        return None
    if session not in SESSIONS:
        raise ValueError(f"unknown session {session!r}")
    zone = NEW_YORK if session in ("us", "fx24x5") else LONDON
    day = ts.astimezone(zone).date()
    for offset in range(15):
        d = day + timedelta(days=offset)
        if session == "fx24x5":
            if d.weekday() not in (6, 0, 1, 2, 3) or fx_closed_day(d + timedelta(days=1)):
                continue
            opens = datetime.combine(d, FX_BREAK[1], tzinfo=NEW_YORK).astimezone(UTC)
            if opens > ts:
                return datetime.combine(d + timedelta(days=1), FX_BREAK[0], tzinfo=NEW_YORK).astimezone(UTC)
            continue
        hours = session_hours(session, d, closing=True)
        if hours is not None and hours[0] > ts:
            return hours[1]
    raise ValueError(f"no {session} session within two weeks of {ts.isoformat()}")  # pragma: no cover


def leg_valid_until(session: str | None, asof: datetime) -> datetime:
    """A leg's approval deadline: min(next slot - 5 min, its session's next close - 10 min).
    `asof` is the cycle's slot (or the time a standing proposal is made). A leg without a known
    session (an unmapped position) and a crypto leg keep the slot deadline."""
    limit = next_slot(_as_utc(asof)) - NEXT_SLOT_MARGIN
    if session is None or session == "crypto":
        return limit
    close = session_close_after(session, asof)
    return limit if close is None else min(limit, close - LEG_CLOSE_MARGIN)


def vehicle_session(line: Any, vehicle_symbol: str) -> Session:
    """Trading session of one VEHICLE of a line (a `policy.LineSpec`): London-listed ('.L') on
    LSE hours, crypto 24/7, stocks and ETFs on US hours, the US-listed ETF a line's Tiingo signal
    comes from (QQQ, SPY, GLD traded as CFDs) on US hours, other index/commodity/FX CFDs 24/5."""
    if line.asset_class == "crypto":
        return "crypto"
    if vehicle_symbol.upper().endswith(".L"):
        return "lse"
    if line.asset_class in ("stock", "etf"):
        return "us"
    signal = getattr(line, "signal", None)
    if signal is not None and signal.source == "tiingo" and vehicle_symbol == signal.ticker:
        return "us"
    return "fx24x5"


def market_open(asset_class: str, ts: datetime, session: str | None = None, *, closing: bool = False) -> bool:
    """Is the line's preferred vehicle tradable now? `session` (LineSpec.session) wins when given."""
    if session == "crypto" or (session is None and asset_class == "crypto"):
        return True
    if session == "lse":
        return lse_open(ts, closing=closing)
    if session == "us" or (session is None and asset_class in ("stock", "etf")):
        return us_equity_open(ts, closing=closing)
    return fx_open(ts)
