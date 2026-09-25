from __future__ import annotations

import hashlib
import json

from council.data.feeds import parse_news_feed
from tests.data.synth import fixture, utc

NOW = utc(2026, 10, 1, 14, 40)


def _items():
    return parse_news_feed(fixture("etoro_news_feed.json"), now=NOW)


def test_admits_only_timestamped_non_future_unique_items():
    items = _items()
    assert [i.title for i in items] == ["Fed holds rates steady txt.exe", "Chipmakers rally on demand outlook", "Earnings preview"]
    assert [i.published_at for i in items] == sorted((i.published_at for i in items), reverse=True)


def test_ids_are_stable_hashes_of_post_id_or_title_and_created():
    items = {i.title: i for i in _items()}
    assert items["Fed holds rates steady txt.exe"].id == "N:" + hashlib.sha256(b"d-1001").hexdigest()[:8]
    key = "Earnings preview" + utc(2026, 9, 30, 20, 0).isoformat()
    assert items["Earnings preview"].id == "N:" + hashlib.sha256(key.encode()).hexdigest()[:8]
    assert [i.id for i in _items()] == [i.id for i in _items()]


def test_text_is_sanitised_and_authors_never_kept():
    items = {i.title: i for i in _items()}
    fed = items["Fed holds rates steady txt.exe"]
    assert fed.summary == "Policy rate unchanged; see for more. Ping"
    chips = items["Chipmakers rally on demand outlook"]
    assert chips.summary == "Chipmakers rally on demand outlook"          # aiSummary fallback, ESC gone
    blob = json.dumps([i.model_dump(mode="json") for i in _items()])
    for leaked in ("trader_joe", "insider", "http", "www.", "body text", "‮", "\\u001b"):
        assert leaked not in blob


def test_symbols_from_tags_and_markets_and_earnings_fields():
    items = {i.title: i for i in _items()}
    assert items["Fed holds rates steady txt.exe"].symbols == ["SPX500", "NSDQ100"]
    assert items["Chipmakers rally on demand outlook"].symbols == ["SOXX"]
    earnings = items["Earnings preview"]
    assert earnings.symbols == ["AAPL"]
    assert earnings.earnings_date == utc(2026, 10, 29, 20, 0) and earnings.before_market_open is False
    assert all(i.available_at == i.published_at for i in _items())


def test_future_skew_boundary_pass_and_fail():
    payload = {"discussions": [{"id": 1, "post": {"title": "t", "created": "2026-10-01T14:45:00Z"}}]}
    assert len(parse_news_feed(payload, now=NOW)) == 1                   # +5 min skew tolerated
    payload["discussions"][0]["post"]["created"] = "2026-10-01T14:45:01Z"
    assert parse_news_feed(payload, now=NOW) == []


def test_defensive_shapes():
    assert parse_news_feed(None, now=NOW) == []
    assert parse_news_feed({"paging": {}}, now=NOW) == []
    flat = [{"id": 7, "title": "flat entry", "created": "2026-10-01T10:00:00Z", "tags": ["$qqq", "bad sym!"]}]
    (item,) = parse_news_feed(flat, now=NOW)
    assert item.symbols == ["QQQ"] and item.title == "flat entry"
    assert parse_news_feed([{"id": 8, "created": "2026-10-01T10:00:00Z"}], now=NOW) == []   # no text
