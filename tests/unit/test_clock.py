"""Exchange calendars, session closes and per-leg deadlines (WP-F market hours)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import yaml

from council import clock
from council.facts.pack import closed_day
from council.paths import POLICY_DIR


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ------------------------------------------------------------------------------ calendars
def test_calendar_horizon_is_extended_before_october_of_its_last_year():
    """CI guard: from 1 October of the calendars' last year, the next year must exist."""
    today = date.today()
    needed = date(today.year + (1 if today.month >= 10 else 0), 12, 31)
    last = clock.CALENDAR_LAST_DAY
    assert last >= needed, (
        "add next year's NYSE/LSE holidays and early closes to council/clock.py, a "
        "policy/calendar-<year>.yaml record, and move CALENDAR_LAST_DAY")


def test_2027_calendars_match_the_policy_record():
    record = yaml.safe_load((POLICY_DIR / "calendar-2027.yaml").read_text())["exchange_calendar"]

    def days(key: str) -> set[date]:
        return {date.fromisoformat(d) for d in record[key]}

    in_2027 = lambda s: {d for d in s if d.year == 2027}  # noqa: E731
    assert in_2027(clock.US_HOLIDAYS) == days("nyse_holidays")
    assert in_2027(clock.US_EARLY_CLOSES) == days("nyse_early_close_1300_et")
    assert in_2027(clock.UK_HOLIDAYS) == days("lse_holidays")
    assert in_2027(clock.LSE_EARLY_CLOSES) == days("lse_early_close_1230_uk")
    last = clock.CALENDAR_LAST_DAY
    assert last == date(2027, 12, 31)


def test_fomc_2027_record_is_2pm_new_york():
    fomc = yaml.safe_load((POLICY_DIR / "calendar-2027.yaml").read_text())["fomc_decisions_utc"]
    assert len(fomc) == 8
    for raw in fomc:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        local = at.astimezone(clock.NEW_YORK)
        assert (local.hour, local.minute) == (14, 0) and local.weekday() == 2   # Wednesday 2 pm


@pytest.mark.parametrize("day", ["2027-01-18", "2027-03-26", "2027-07-05", "2027-11-25", "2027-12-24"])
def test_2027_us_holidays_are_closed(day):
    d = date.fromisoformat(day)
    assert not clock.us_equity_open(utc(d.year, d.month, d.day, 16, 0))
    assert closed_day(d, "etf") and not closed_day(d, "crypto")


@pytest.mark.parametrize("day", ["2027-03-29", "2027-05-03", "2027-08-30", "2027-12-27", "2027-12-28"])
def test_2027_uk_holidays_close_the_lse(day):
    d = date.fromisoformat(day)
    assert not clock.lse_open(utc(d.year, d.month, d.day, 11, 0))


def test_2027_ordinary_days_are_open():
    assert clock.us_equity_open(utc(2027, 12, 31, 15, 0))          # NYSE open on New Year's Eve 2027
    assert clock.lse_open(utc(2027, 3, 30, 11, 0))                 # Tuesday after Easter Monday


def test_us_early_closes_end_at_1pm_new_york():
    for d in (date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)):
        _, close = clock.session_hours("us", d)
        assert close == utc(d.year, d.month, d.day, 18, 0)          # 13:00 EST = 18:00Z
        assert clock.us_equity_open(utc(d.year, d.month, d.day, 17, 59))
        assert not clock.us_equity_open(utc(d.year, d.month, d.day, 18, 0))


def test_lse_half_days_end_at_1230_london():
    for d in (date(2026, 12, 24), date(2026, 12, 31), date(2027, 12, 24), date(2027, 12, 31)):
        assert clock.lse_open(utc(d.year, d.month, d.day, 12, 29))
        assert not clock.lse_open(utc(d.year, d.month, d.day, 12, 30))


def test_dst_gap_weeks_follow_each_exchange():
    # 2027-03-16: New York on daylight time (since 14 March), London not yet (28 March)
    assert clock.session_close_after("us", utc(2027, 3, 16, 18, 40)) == utc(2027, 3, 16, 20, 0)
    assert clock.session_close_after("lse", utc(2027, 3, 16, 14, 40)) == utc(2027, 3, 16, 16, 30)
    # 2026-10-27: London back on GMT (25 October), New York still on daylight time (1 November)
    assert clock.session_close_after("us", utc(2026, 10, 27, 18, 40)) == utc(2026, 10, 27, 20, 0)
    assert clock.session_close_after("lse", utc(2026, 10, 27, 14, 40)) == utc(2026, 10, 27, 16, 30)
    # summer and winter proper
    assert clock.session_close_after("lse", utc(2026, 10, 1, 14, 40)) == utc(2026, 10, 1, 15, 30)
    assert clock.session_close_after("us", utc(2026, 11, 3, 18, 40)) == utc(2026, 11, 3, 21, 0)


def test_past_the_horizon_opens_are_refused_and_closes_use_weekday_hours():
    monday = utc(2028, 1, 3, 15, 0)
    assert not clock.us_equity_open(monday) and not clock.lse_open(utc(2028, 1, 3, 11, 0))
    assert clock.us_equity_open(monday, closing=True)
    assert clock.lse_open(utc(2028, 1, 3, 11, 0), closing=True)
    assert not clock.us_equity_open(utc(2028, 1, 8, 15, 0), closing=True)   # Saturday
    assert clock.calendar_missing("us", monday) == "calendar_missing:2028"
    assert clock.calendar_missing("fx24x5", monday) is None
    assert clock.calendar_missing("us", utc(2027, 12, 31, 15, 0)) is None
    assert clock.session_close_after("us", monday) == utc(2028, 1, 3, 21, 0)
    assert clock.market_open("etf", monday, "us") is False
    assert clock.market_open("etf", monday, "us", closing=True) is True


# ------------------------------------------------------------------------------ deadlines
def test_1840_us_leg_is_clipped_to_the_close():
    summer = utc(2026, 10, 1, 18, 40)
    assert clock.leg_valid_until("us", summer) == utc(2026, 10, 1, 19, 50)
    winter = utc(2026, 11, 3, 18, 40)
    assert clock.leg_valid_until("us", winter) == utc(2026, 11, 3, 20, 50)
    assert clock.leg_valid_until("crypto", summer) == utc(2026, 10, 1, 22, 35)
    assert clock.leg_valid_until(None, summer) == clock.proposal_valid_until(summer)


def test_early_close_day_deadlines():
    assert clock.leg_valid_until("us", utc(2026, 11, 27, 14, 40)) == utc(2026, 11, 27, 17, 50)
    assert not clock.us_equity_open(utc(2026, 11, 27, 18, 40))       # no 18:40 stock slot
    assert clock.leg_valid_until("lse", utc(2026, 12, 24, 10, 40)) == utc(2026, 12, 24, 12, 20)


def test_lse_leg_in_summer_ends_at_1620_lisbon():
    assert clock.leg_valid_until("lse", utc(2026, 10, 1, 14, 40)) == utc(2026, 10, 1, 15, 20)


def test_fx_legs_clip_at_the_daily_break_and_the_friday_close():
    friday = utc(2026, 10, 2, 18, 40)                                  # 14:40 New York
    assert clock.leg_valid_until("fx24x5", friday) == utc(2026, 10, 2, 20, 50)
    assert not clock.fx_open(utc(2026, 10, 2, 21, 0)) and not clock.fx_open(utc(2026, 10, 4, 21, 0))
    assert clock.fx_open(utc(2026, 10, 4, 22, 0))                      # Sunday 18:00 New York
    thursday = utc(2026, 12, 3, 18, 40)                                # winter: break 22:00-23:00Z
    assert clock.leg_valid_until("fx24x5", thursday) == utc(2026, 12, 3, 21, 50)


def test_fx_has_no_session_on_christmas_and_new_years_day():
    assert clock.fx_open(utc(2026, 12, 24, 21, 59))                    # Thursday 16:59 New York
    assert not clock.fx_open(utc(2026, 12, 24, 23, 30))                # would open into the 25th
    assert not clock.fx_open(utc(2026, 12, 25, 14, 40))
    assert clock.fx_open(utc(2026, 12, 27, 23, 30))                    # Sunday 18:30 New York
    assert not clock.fx_open(utc(2026, 12, 31, 23, 30)) and not clock.fx_open(utc(2027, 1, 1, 14, 40))
    assert clock.fx_open(utc(2027, 1, 3, 23, 30))
    # a proposal on the holiday is frozen; a held order waits for the session after it
    assert clock.session_close_after("fx24x5", utc(2026, 12, 25, 14, 40)) == utc(2026, 12, 28, 22, 0)
    assert clock.next_session_close("fx24x5", utc(2026, 12, 24, 22, 40)) == utc(2026, 12, 28, 22, 0)
    assert clock.next_session_close("fx24x5", utc(2027, 1, 1, 14, 40)) == utc(2027, 1, 4, 22, 0)
    assert clock.session_close_after("fx24x5", utc(2026, 12, 24, 14, 40)) == utc(2026, 12, 24, 22, 0)


def test_a_closed_session_keeps_the_slot_deadline_until_its_next_close():
    # 02:40 slot: LSE opens at 07:00Z, closes 15:30Z → the slot deadline (06:35) comes first
    assert clock.leg_valid_until("lse", utc(2026, 10, 2, 2, 40)) == utc(2026, 10, 2, 6, 35)
    # Saturday: the next US close is Monday's
    assert clock.session_close_after("us", utc(2026, 10, 3, 12, 0)) == utc(2026, 10, 5, 20, 0)


def test_vehicle_sessions(policy):
    by = policy.universe.by_symbol()
    assert clock.vehicle_session(by["NDX"], "EQQQ.L") == "lse"
    assert clock.vehicle_session(by["NDX"], "QQQ") == "us"            # the US ETF behind the signal
    assert clock.vehicle_session(by["NDX"], "NSDQ100") == "fx24x5"    # index CFD
    assert clock.vehicle_session(by["SEMIS"], "SOXX") == "us"
    assert clock.vehicle_session(by["GOLD"], "GOLD") == "fx24x5"
    assert clock.vehicle_session(by["BTC"], "BTC") == "crypto"
    assert clock.vehicle_session(by["EURUSD"], "EURUSD") == "fx24x5"
    for line in policy.universe.lines:          # the preferred vehicle agrees with LineSpec.session
        assert clock.vehicle_session(line, line.vehicles.long[0].symbol) == line.session


def test_proposal_deadline_unchanged():
    slot = utc(2026, 10, 1, 14, 40)
    assert clock.proposal_valid_until(slot) == slot + timedelta(hours=4) - timedelta(minutes=5)


def test_next_session_close_gives_a_held_order_a_full_session():
    assert clock.next_session_close("us", utc(2026, 10, 1, 15, 0)) == utc(2026, 10, 2, 20, 0)
    assert clock.next_session_close("us", utc(2026, 10, 2, 21, 0)) == utc(2026, 10, 5, 20, 0)   # Fri → Mon
    assert clock.next_session_close("us", utc(2026, 11, 25, 21, 0)) == utc(2026, 11, 27, 18, 0)  # early close
    assert clock.next_session_close("lse", utc(2026, 12, 24, 13, 0)) == utc(2026, 12, 29, 16, 30)
    assert clock.next_session_close("fx24x5", utc(2026, 10, 1, 14, 40)) == utc(2026, 10, 2, 21, 0)
    assert clock.next_session_close("fx24x5", utc(2026, 10, 2, 22, 0)) == utc(2026, 10, 5, 21, 0)
    assert clock.next_session_close("crypto", utc(2026, 10, 2, 22, 0)) is None
