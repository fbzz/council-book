"""The fact pack keeps an event while its R16 window contains the slot (phase-3 follow-up (a), the
WP-H known limit): an earnings window is no longer cut short 24 h after a confirmed report or an
estimated date, and an estimated window that has already started is admitted although its date lies
beyond the 7-day horizon. Macro windows (2 h after the event) are unchanged, and an event whose
schedule was known only after the slot is still dropped."""

from __future__ import annotations

from datetime import datetime, timedelta

from council.clock import cycle_id_for
from council.facts.evidence_ids import event_id
from council.facts.pack import EVENT_HORIZON, EVENT_LOOKBACK, admissible_events, build_fact_pack
from council.models.facts import EventItem
from council.risk.churn import EARNINGS_ESTIMATE_SOURCE, event_block, event_window
from tests.data.synth import utc

SLOT = utc(2026, 10, 1, 14, 40)          # Thursday 10:40 New York
MONDAY = utc(2026, 9, 28, 14, 40)         # Monday 10:40 New York


def _earnings(at: datetime, *, line: str = "TSTA", source: str = "sec_8k",
              known_at: datetime | None = None) -> EventItem:
    return EventItem(id=event_id("earnings", at, symbol=line), kind="earnings", at_utc=at, symbols=[line],
                     severity=2, source=source, known_at=known_at if known_at is not None else at)


def _macro(kind: str, at: datetime) -> EventItem:
    return EventItem(id=event_id(kind, at), kind=kind, at_utc=at, severity=3, source="test")


def _pack(policy, events, slot=SLOT, **kw):
    return build_fact_pack(cycle_id=cycle_id_for(slot), slot=slot, now=slot + timedelta(minutes=2),
                           policy=policy, states={}, events=events, **kw)


def _ids(pack) -> list[str]:
    return [e.id for e in pack.events]


def _line(policy, symbol):
    return policy.universe.by_symbol()[symbol]


def test_a_confirmed_report_stays_until_its_window_ends(sleeve_policy):
    report = utc(2026, 9, 30, 12, 0)                      # Wed 08:00 New York, 26 h 40 min before the slot
    event = _earnings(report)
    assert report < SLOT - EVENT_LOOKBACK                  # the old rule dropped it here
    start, end = event_window(event, sleeve_policy)
    assert start <= SLOT <= end and end == report + timedelta(hours=30)
    pack = _pack(sleeve_policy, [event])
    assert _ids(pack) == [event.id]
    # R16 now sees it: no adds on that stock, never on the core or another stock
    assert event_block(_line(sleeve_policy, "TSTA"), pack.events, SLOT, sleeve_policy)
    assert not event_block(_line(sleeve_policy, "TSTB"), pack.events, SLOT, sleeve_policy)
    assert not event_block(_line(sleeve_policy, "NDX"), pack.events, SLOT, sleeve_policy)
    # once the window is over, the event leaves the pack
    after = end + timedelta(minutes=1)
    assert _ids(_pack(sleeve_policy, [event], slot=after)) == []


def test_a_report_after_fridays_close_stays_until_mondays_bar(sleeve_policy):
    report = utc(2026, 9, 25, 20, 30)                     # Friday 16:30 New York
    event = _earnings(report)
    _, end = event_window(event, sleeve_policy)
    assert end == utc(2026, 9, 29, 0, 0)                   # Monday's bar, 20:00 New York
    assert MONDAY - report > EVENT_LOOKBACK
    pack = _pack(sleeve_policy, [event], slot=MONDAY)
    assert _ids(pack) == [event.id]
    assert event_block(_line(sleeve_policy, "TSTA"), pack.events, MONDAY, sleeve_policy)


def test_an_estimated_window_is_kept_from_its_start_to_its_end(sleeve_policy):
    ahead = _earnings(utc(2026, 10, 9, 12, 0), source=EARNINGS_ESTIMATE_SOURCE, known_at=SLOT - timedelta(days=60))
    assert ahead.at_utc > SLOT + EVENT_HORIZON             # beyond the horizon, but its window has begun
    start, _ = event_window(ahead, sleeve_policy)
    assert start == utc(2026, 10, 1, 4, 0)                 # 24 h before Fri Oct 2, 00:00 New York
    passed = _earnings(utc(2026, 9, 28, 12, 0), line="TSTB", source=EARNINGS_ESTIMATE_SOURCE,
                       known_at=SLOT - timedelta(days=60))
    assert passed.at_utc < SLOT - EVENT_LOOKBACK           # the date passed three days ago
    assert event_window(passed, sleeve_policy)[1] > SLOT   # the window runs five trading days after it
    pack = _pack(sleeve_policy, [ahead, passed])
    assert _ids(pack) == [passed.id, ahead.id]              # sorted by time
    for sym in ("TSTA", "TSTB"):
        assert event_block(_line(sleeve_policy, sym), pack.events, SLOT, sleeve_policy)
    before = start - timedelta(minutes=1)
    assert _ids(_pack(sleeve_policy, [ahead], slot=before)) == []   # not begun yet: still beyond the horizon


def test_an_ended_window_macro_windows_and_late_schedules_are_still_dropped(sleeve_policy):
    ended = _earnings(utc(2026, 9, 28, 12, 0))             # confirmed Monday: window ended Tue 18:00 UTC
    assert event_window(ended, sleeve_policy)[1] < SLOT
    cpi = _macro("cpi", SLOT - EVENT_LOOKBACK - timedelta(hours=1))   # macro window ends 2 h after it
    late = _earnings(utc(2026, 10, 9, 12, 0), source=EARNINGS_ESTIMATE_SOURCE,
                     known_at=SLOT + timedelta(hours=1))   # the estimate became known after the slot
    keyword_late = _earnings(utc(2026, 9, 30, 12, 0), line="TSTB").model_copy(update={"known_at": None})
    pack = _pack(sleeve_policy, [ended, cpi, late, keyword_late],
                 event_known_at={keyword_late.id: SLOT + timedelta(minutes=5)})
    assert _ids(pack) == []


def test_without_a_policy_only_the_time_range_applies(sleeve_policy):
    event = _earnings(utc(2026, 9, 30, 12, 0))
    assert admissible_events([event], SLOT) == []
    assert admissible_events([event], SLOT, policy=sleeve_policy) == [event]


def test_the_core_book_is_unchanged(policy):
    """Core-only: macro windows end 2 h after the event, inside the 24 h look-back, so the pack's
    events equal the time-range rule's."""
    events = [_macro(kind, SLOT - timedelta(hours=h)) for kind, h in (("fomc", 1), ("cpi", 23), ("pce", 25))]
    events += [_macro("nfp", SLOT + timedelta(days=d)) for d in (1, 6, 8)]
    assert _pack(policy, events).events == admissible_events(events, SLOT)
