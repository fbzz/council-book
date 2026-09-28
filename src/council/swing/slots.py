"""The swing-slot gate (design swing-book.md rev 2, §1.1 and §4.1; `policy/swing.yaml` `slots`).

A cycle slot may carry swing proposals only when ALL of these hold:
- its UTC wall time (HH:MM) is one of the season's slots: `summer_utc` while New York observes
  daylight saving time on that day, else `winter_utc` (DST-aware through the tz database, so the
  switch dates are never hard-coded);
- the US cash session is open that day (the exchange calendar in `council.clock`, holidays and
  13:00 early closes included) and the slot is at least `min_session_age_min` after the open;
- at least `min_to_close_min` remain before that day's close (an 18:40 UTC slot on an early-close
  day is therefore refused: the session ended at 18:00 UTC);
- the day is inside the known exchange calendar (past it, the gate says no: fail closed).

Pure code: no clock read, no I/O. The answer carries a fixed reason code for the cycle record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from council.clock import NEW_YORK, calendar_known, session_hours
from council.swing.policy import Slots

REASONS = ("ok", "naive_time", "not_a_swing_slot", "calendar_unknown", "us_closed",
           "session_too_young", "too_close_to_close")


@dataclass(frozen=True)
class SlotVerdict:
    ok: bool
    reason: str
    season: str                       # "summer" (New York DST) or "winter"
    session_open: datetime | None = None
    session_close: datetime | None = None


def ny_dst(ts: datetime) -> bool:
    """True while New York observes daylight saving time at `ts`."""
    local = ts.astimezone(NEW_YORK)
    return bool(local.dst())


def season_of(ts: datetime) -> str:
    return "summer" if ny_dst(ts) else "winter"


def swing_slot(ts: datetime, slots: Slots) -> SlotVerdict:
    """The gate for a slot at `ts` (a timezone-aware time)."""
    if ts.tzinfo is None:
        return SlotVerdict(False, "naive_time", "winter")
    utc = ts.astimezone(UTC)
    season = season_of(utc)
    allowed = slots.summer_utc if season == "summer" else slots.winter_utc
    if utc.strftime("%H:%M") not in allowed:
        return SlotVerdict(False, "not_a_swing_slot", season)
    day = utc.astimezone(NEW_YORK).date()
    if not calendar_known(day):
        return SlotVerdict(False, "calendar_unknown", season)
    hours = session_hours("us", day)
    if hours is None:
        return SlotVerdict(False, "us_closed", season)
    open_, close = hours
    if not open_ <= utc < close:
        return SlotVerdict(False, "us_closed", season, open_, close)
    if utc - open_ < timedelta(minutes=slots.min_session_age_min):
        return SlotVerdict(False, "session_too_young", season, open_, close)
    if close - utc < timedelta(minutes=slots.min_to_close_min):
        return SlotVerdict(False, "too_close_to_close", season, open_, close)
    return SlotVerdict(True, "ok", season, open_, close)


def is_swing_slot(ts: datetime, slots: Slots) -> bool:
    return swing_slot(ts, slots).ok
