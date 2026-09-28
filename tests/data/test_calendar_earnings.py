"""WP-H wiring: the calendar merges the stock lines' earnings events; `context.data_sources` computes
them at the slot of the cycle's event window, shares one broker feed request per slot between the
earnings override and the news role, and leaves a core-only policy untouched. No network: the
earnings gatherer is replaced by a recorder, the broker by a fake."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from council import context
from council.data.calendar import load_events
from council.models.facts import EventItem
from council.runtime import window
from council.stocks import earnings as EA
from tests.integration.test_news_wiring import attest_licence

SLOT = datetime(2026, 10, 26, 14, 40, tzinfo=UTC)
FEED = json.loads((Path(__file__).parent / "fixtures" / "etoro_news_feed.json").read_text())


def tsta_event(at: datetime = SLOT + timedelta(days=3)) -> EventItem:
    return EventItem(id=f"E:earnings:TSTA@{at.date().isoformat()}", kind="earnings", at_utc=at, symbols=["TSTA"],
                     severity=2, source="sec_estimate")


def test_load_events_merges_earnings_and_survives_their_failure(policy):
    start, end = window(datetime(2026, 10, 27, 14, 40, tzinfo=UTC))      # the FOMC decision of 10-28 is inside
    event = tsta_event(datetime(2026, 11, 20, 21, 0, tzinfo=UTC))        # outside [start, end]: kept as returned
    events, flags = load_events(policy, start, end, earnings=lambda s, e: ([event], ["earnings_unknown:TSTB"]))
    assert [e.kind for e in events] == ["fomc", "earnings"]
    assert "earnings_unknown:TSTB" in flags

    def broken(s, e):
        raise RuntimeError("bug")

    events, flags = load_events(policy, start, end, earnings=broken)
    assert [e.kind for e in events] == ["fomc"] and "calendar:earnings_error:RuntimeError" in flags


def test_events_slot_is_the_slot_of_the_cycle_window():
    assert context.events_slot(*window(SLOT)) == SLOT
    odd = (SLOT - timedelta(hours=3), SLOT + timedelta(days=2))
    assert context.events_slot(*odd) == odd[0]                           # an earlier cutoff, never a later one


class FakeBroker:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def feeds_news(self, take: int = 20):
        self.calls += 1
        if self.fail:
            raise RuntimeError("feed down")
        return FEED


@pytest.fixture
def recorder(monkeypatch):
    calls: list[dict] = []

    def fake(policy, **kw):
        calls.append(kw)
        return [tsta_event()], ["earnings_unknown:TSTB"]

    monkeypatch.setattr(EA, "gather_earnings", fake)
    return calls


def test_data_sources_add_the_stock_earnings_at_the_window_slot(sleeve_policy, recorder, tmp_path):
    sources = context.data_sources(sleeve_policy, state_dir=tmp_path)
    events, flags = sources.events(*window(SLOT))
    assert [e.id for e in events if e.kind == "earnings"] == [tsta_event().id]
    assert "earnings_unknown:TSTB" in flags
    (call,) = recorder
    assert call["slot"] == SLOT and (call["start"], call["end"]) == window(SLOT)
    assert call["news"] == [] and call["state_dir"] == tmp_path          # no broker: the SEC estimate only


def _no_public_news(policy, now, state_dir=None, *, slot=None, **kw):
    from council.data.gov_news import NewsFetch

    return NewsFetch()


def test_one_feed_request_per_slot_serves_the_earnings_and_the_news(sleeve_policy, recorder, tmp_path):
    broker = FakeBroker()
    attest_licence(tmp_path)                                             # LC1: the feed may be read
    sources = context.data_sources(sleeve_policy, broker=broker, state_dir=tmp_path, public_news=_no_public_news)
    sources.events(*window(SLOT))
    items = sources.news(SLOT).items                  # a NewsFetch: the broker items plus the public ones
    assert broker.calls == 1 and recorder[0]["news"] == items and items
    sources.news(SLOT + timedelta(hours=4))
    assert broker.calls == 2                                             # a new slot asks again


def test_a_failing_feed_leaves_the_sec_estimate_in_force(sleeve_policy, recorder, tmp_path):
    attest_licence(tmp_path)
    sources = context.data_sources(sleeve_policy, broker=FakeBroker(fail=True), state_dir=tmp_path)
    events, flags = sources.events(*window(SLOT))
    assert "earnings_feed_failed:RuntimeError" in flags and recorder[0]["news"] == []
    assert any(e.kind == "earnings" for e in events)


def test_a_core_only_policy_never_asks_for_earnings(policy, monkeypatch, tmp_path):
    def boom(*args, **kw):
        raise AssertionError("core-only policy asked for earnings")

    monkeypatch.setattr(EA, "gather_earnings", boom)
    broker = FakeBroker()
    sources = context.data_sources(policy, broker=broker, state_dir=tmp_path)
    events, flags = sources.events(*window(SLOT))
    assert all(e.kind != "earnings" for e in events) and not any("earnings" in f for f in flags)
    assert broker.calls == 0                                             # the calendar never reads the feed
