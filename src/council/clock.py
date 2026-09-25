"""Slot grid and market sessions. All times are UTC; the grid is immune to DST.

Slots: 02:40, 06:40, 10:40, 14:40, 18:40, 22:40 UTC. launchd fires hourly at :40 and the cycle
runs only on slot hours. A cycle that starts late (<= 120 min) runs flagged `late`; later is `missed`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
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
    """Proposals expire 5 minutes before the next slot (the next cycle supersedes anyway)."""
    return next_slot(_as_utc(slot)) - timedelta(minutes=5)


# ---------------------------------------------------------------- market sessions (approximate)
# US cash equities/ETFs: 09:30-16:00 New York, Mon-Fri, excluding the holidays below.
US_HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1),
}


def us_equity_open(ts: datetime) -> bool:
    local = _as_utc(ts).astimezone(NEW_YORK)
    if local.weekday() >= 5 or local.date() in US_HOLIDAYS_2026:
        return False
    return time(9, 30) <= local.time() < time(16, 0)


def fx_open(ts: datetime) -> bool:
    """FX/index/commodity CFDs: roughly Sunday 17:00 to Friday 17:00 New York, with a daily break
    17:00-18:00 NY. Eligibility and live rates are the final word; this only avoids dead proposals."""
    local = _as_utc(ts).astimezone(NEW_YORK)
    wd, t = local.weekday(), local.time()
    if wd == 5:
        return False
    if wd == 6:
        return t >= time(18, 0)
    if wd == 4 and t >= time(17, 0):
        return False
    return not (time(17, 0) <= t < time(18, 0))


LONDON = ZoneInfo("Europe/London")
UK_HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 4), date(2026, 5, 25),
    date(2026, 8, 31), date(2026, 12, 25), date(2026, 12, 28), date(2027, 1, 1),
}


def lse_open(ts: datetime) -> bool:
    """London Stock Exchange cash session 08:00-16:30 London time, weekdays, UK holidays excluded."""
    local = _as_utc(ts).astimezone(LONDON)
    if local.weekday() >= 5 or local.date() in UK_HOLIDAYS_2026:
        return False
    return time(8, 0) <= local.time() < time(16, 30)


def market_open(asset_class: str, ts: datetime, session: str | None = None) -> bool:
    """Is the line's preferred vehicle tradable now? `session` (LineSpec.session) wins when given."""
    if session == "crypto" or (session is None and asset_class == "crypto"):
        return True
    if session == "lse":
        return lse_open(ts)
    if session == "us" or (session is None and asset_class in ("stock", "etf")):
        return us_equity_open(ts)
    return fx_open(ts)
