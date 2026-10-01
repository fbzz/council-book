"""RSS follow-ups (user decision 2026-10-01): the Yahoo per-ticker feed capped at 10 names, paced
one request per 1.5 s and backed off for the New York day after a 429 (flagged once); a market feed
retried once on a transient answer (PR Newswire's intermittent 404); overnight items first at the
first swing slot of a session. Offline: fixture getters and httpx.MockTransport only."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from council.data import rss_news
from council.models.facts import NewsItem
from council.swing import sources as ss

from .test_rss_news import body, cfg, feed, fixture_get, policy_module  # noqa: F401 - fixtures

SLOT = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)          # Thursday 10:40 New York (EDT)
BACKOFF_FLAG = "news_source_backoff:rss:yahoo_ticker"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _tickers(cfg, names, *, get, state_dir, slot=SLOT, clock=None):  # noqa: F811
    clock = clock or Clock()
    return rss_news.fetch_tickers(cfg, names, now=slot, slot=slot, get=get, state_dir=state_dir,
                                  sleep=clock.sleep, monotonic=clock.monotonic), clock


# ------------------------------------------------------------------------------ Yahoo
def test_the_ticker_feed_is_capped_at_ten_and_paced(cfg, tmp_path):  # noqa: F811
    urls: list[str] = []

    def get(url):
        urls.append(url)
        return fixture_get(url)

    got, clock = _tickers(cfg, [f"T{n}" for n in range(30)], get=get, state_dir=tmp_path)
    assert cfg.yahoo_max_tickers == 10 and len(urls) == 10 and "s=T0&" in urls[0]
    assert clock.sleeps == [pytest.approx(rss_news.TICKER_PACE_S)] * 9      # one request per 1.5 s
    assert got.flags == [] and got.sources["rss"].feeds_ok == 1


def test_a_429_stops_the_feed_for_the_new_york_day_and_flags_once(cfg, tmp_path):  # noqa: F811
    urls: list[str] = []

    def get(url):
        urls.append(url)
        if len(urls) == 3:
            raise rss_news.RssRateLimited("rss: HTTP 429")
        return fixture_get(url)

    got, _ = _tickers(cfg, [f"T{n}" for n in range(10)], get=get, state_dir=tmp_path)
    assert len(urls) == 3 and got.flags == [BACKOFF_FLAG] and got.items          # the first two still count
    assert json.loads((tmp_path / rss_news.BACKOFF_FILE).read_text()) == {"yahoo_ticker": "2026-10-01"}
    urls.clear()
    later, _ = _tickers(cfg, ["T1"], get=get, state_dir=tmp_path, slot=SLOT + timedelta(hours=4))
    assert urls == [] and later.flags == [] and later.items == []                 # silent for the day
    next_day, _ = _tickers(cfg, ["T1"], get=get, state_dir=tmp_path, slot=SLOT + timedelta(days=1))
    assert len(urls) == 1 and next_day.flags == [] and next_day.sources["rss"].feeds_ok == 1


def test_the_http_getter_turns_429_into_rate_limited_without_retry(cfg):  # noqa: F811
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(429)

    get, client = rss_news.http_getter(cfg, transport=httpx.MockTransport(handler), retry=True,
                                       sleep=lambda _s: None)
    try:
        with pytest.raises(rss_news.RssRateLimited):
            get(feed(cfg, "yahoo_ticker").url_for("NVDA"))
        assert len(seen) == 1
    finally:
        client.close()


# ------------------------------------------------------------------------------ PR Newswire
@pytest.mark.parametrize("first", [404, 503, "transport"])
def test_a_market_feed_is_retried_once_on_a_transient_answer(cfg, first):  # noqa: F811
    calls: list[int] = []
    sleeps: list[float] = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            if first == "transport":
                raise httpx.ConnectError("reset")
            return httpx.Response(first)
        return httpx.Response(200, content=body("prnewswire_all"))

    get, client = rss_news.http_getter(cfg, transport=httpx.MockTransport(handler), retry=True, sleep=sleeps.append)
    try:
        assert get(feed(cfg, "prnewswire_all").url) == body("prnewswire_all")
        assert len(calls) == 2 and sleeps == [rss_news.RETRY_DELAY_S]
    finally:
        client.close()


def test_a_persistent_404_is_still_one_flag_and_without_retry_one_attempt(cfg):  # noqa: F811
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    get, client = rss_news.http_getter(cfg, transport=httpx.MockTransport(handler), retry=True, sleep=lambda _s: None)
    try:
        with pytest.raises(Exception, match="HTTP 404"):
            get(feed(cfg, "prnewswire_all").url)
        assert len(calls) == 2
    finally:
        client.close()
    calls.clear()
    get, client = rss_news.http_getter(cfg, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(Exception, match="HTTP 404"):
            get(feed(cfg, "prnewswire_all").url)
        assert len(calls) == 1
    finally:
        client.close()


# ------------------------------------------------------------------------------ overnight first
def _item(n: int, label: str, at: datetime, symbols=()) -> NewsItem:
    return NewsItem(id=f"N:{n:08x}", title=f"headline number {n}", symbols=list(symbols), published_at=at,
                    available_at=at, source="rss", licence="third_party_licensed", feed=label)


def test_the_overnight_window_is_previous_close_to_open():
    lo, hi = rss_news.overnight_window(SLOT)
    assert lo == datetime(2026, 9, 30, 20, 0, tzinfo=UTC) and hi == datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
    monday = datetime(2026, 10, 5, 14, 40, tzinfo=UTC)
    assert rss_news.overnight_window(monday)[0] == datetime(2026, 10, 2, 20, 0, tzinfo=UTC)   # Friday's close
    assert rss_news.overnight_window(datetime(2026, 10, 3, 15, 0, tzinfo=UTC)) is None         # Saturday
    assert rss_news.overnight_window(datetime(2026, 10, 1, 12, 0, tzinfo=UTC)) is None         # before the open


def test_overnight_items_rank_first_at_the_first_slot(cfg):  # noqa: F811
    window = rss_news.overnight_window(SLOT)
    same = _item(1, "cnbc_top", SLOT - timedelta(minutes=20), ["NVDA"])            # same session, priority name
    pre = _item(2, "benzinga", datetime(2026, 10, 1, 12, 0, tzinfo=UTC))           # pre-market
    after = _item(3, "cnbc_top", datetime(2026, 9, 30, 21, 30, tzinfo=UTC))        # after-hours
    old = _item(4, "prnewswire_all", datetime(2026, 9, 30, 15, 0, tzinfo=UTC))     # yesterday's session
    items = [same, pre, after, old]
    plain = rss_news.rank_rss(items, cfg, slot=SLOT, priority=["NVDA"])
    assert [i.id for i in plain] == [same.id, old.id, pre.id, after.id]
    first = rss_news.rank_rss(items, cfg, slot=SLOT, priority=["NVDA"], overnight=window)
    assert [i.id for i in first] == [pre.id, after.id, same.id, old.id]


def test_the_reading_list_puts_overnight_first_only_at_the_first_swing_slot(policy_module):  # noqa: F811
    slots = policy_module.swing.slots
    assert ss.is_first_swing_slot(SLOT, slots)                                       # summer 14:40
    assert not ss.is_first_swing_slot(SLOT.replace(hour=18), slots)                  # summer 18:40
    winter = datetime(2026, 11, 3, 18, 40, tzinfo=UTC)
    assert ss.is_first_swing_slot(winter, slots)                                     # winter: 18:40 only
    assert not ss.is_first_swing_slot(datetime(2026, 10, 3, 14, 40, tzinfo=UTC), slots)   # Saturday
    window = rss_news.overnight_window(SLOT)
    same = _item(1, "cnbc_top", SLOT - timedelta(minutes=20))
    pre = _item(2, "benzinga", datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    old = _item(3, "cnbc_top", datetime(2026, 9, 30, 15, 0, tzinfo=UTC))
    newest_first = [same, pre, old]
    assert [i.id for i in ss.overnight_first(newest_first, window)] == [pre.id, same.id, old.id]
    assert ss.overnight_first(newest_first, None) == newest_first
