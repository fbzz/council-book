"""Third-party RSS headlines (`council.data.rss_news`, user decision 2026-09-29): the parser on the
recorded fixtures of all 11 feeds (structure and dates as served; text replaced, see each file's
header comment), ticker tagging, dedupe, the lookahead cutoff, the per-feed cap and ranking, the
failure flag, the XML guard and the policy list. Offline: every fetch goes through a fixture getter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from council.data import rss_news
from council.data.gov_news import NewsParseError, RawEntry
from council.models.facts import NewsItem

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "rss"
SLOT = datetime(2026, 9, 29, 21, 0, tzinfo=UTC)
LABELS = ("prnewswire_all", "nasdaq_earnings", "nasdaq_markets", "yahoo_ticker", "fda_press", "cnbc_top",
          "cnbc_earnings", "marketwatch_top", "seekingalpha_currents", "investing_stocks", "benzinga")


@pytest.fixture(scope="module")
def cfg(policy_module):
    return rss_news.rss_config(policy_module)


@pytest.fixture(scope="module")
def policy_module():
    from council.paths import POLICY_DIR
    from council.policy import Policy

    return Policy.load(POLICY_DIR, include_sleeve=False)


def feed(cfg, label):
    return next(f for f in cfg.feeds if f.label == label)


def body(label: str) -> bytes:
    return (FIX / f"{label}.xml").read_bytes()


def fixture_get(url: str) -> bytes:
    for label, marker in (("yahoo_ticker", "feeds.finance.yahoo.com"), ("prnewswire_all", "prnewswire"),
                          ("nasdaq_earnings", "Earnings"), ("nasdaq_markets", "Markets"), ("fda_press", "fda.gov"),
                          ("cnbc_top", "100003114"), ("cnbc_earnings", "15839135"), ("marketwatch_top", "dowjones"),
                          ("seekingalpha_currents", "seekingalpha"), ("investing_stocks", "investing.com"),
                          ("benzinga", "benzinga")):
        if marker in url:
            return body(label)
    raise AssertionError(url)


# ------------------------------------------------------------------------------ policy and parser
def test_the_policy_lists_the_eleven_feeds(cfg):
    assert cfg is not None and tuple(f.label for f in cfg.feeds) == LABELS
    assert [f.label for f in cfg.ticker_feeds()] == ["yahoo_ticker"] and len(cfg.market_feeds()) == 10
    assert cfg.timeout_s == 15 and cfg.scout_max == 40 and cfg.yahoo_max_tickers == 10
    assert feed(cfg, "investing_stocks").naive_tz == "UTC"
    assert "feeds.finance.yahoo.com" in cfg.hosts and "www.benzinga.com" in cfg.hosts


@pytest.mark.parametrize("label", LABELS)
def test_every_recorded_feed_parses_into_licensed_n_items(cfg, label):
    f = feed(cfg, label)
    res = rss_news.parse_rss(f, body(label), now=SLOT, slot=SLOT + timedelta(days=30),
                             ticker="NVDA" if f.per_ticker else None, lookback=timedelta(days=60))
    assert res.report.entries >= 3 and res.items, label
    for item in res.items:
        assert item.id.startswith("N:") and item.source == "rss" and item.licence == "third_party_licensed"
        assert item.feed == label and item.link is None and item.title
        assert len(item.summary) <= rss_news.SUMMARY_MAX and "<" not in item.summary
        assert item.available_at.tzinfo is not None
    assert "published_at" in res.items[0].model_dump(mode="json")


def test_times_are_zoned_and_a_naive_feed_uses_its_policy_zone(cfg):
    res = rss_news.parse_rss(feed(cfg, "investing_stocks"), body("investing_stocks"), now=SLOT, slot=SLOT)
    assert res.items[0].published_at == datetime(2026, 9, 29, 20, 13, tzinfo=UTC)   # naive, read as UTC
    fda = rss_news.parse_rss(feed(cfg, "fda_press"), body("fda_press"), now=SLOT, slot=SLOT)
    assert fda.items[0].published_at == datetime(2026, 9, 28, 21, 43, 18, tzinfo=UTC)  # 17:43:18 EDT


def test_ticker_tagging(cfg):
    prn = rss_news.parse_rss(feed(cfg, "prnewswire_all"), body("prnewswire_all"), now=SLOT, slot=SLOT).items
    assert prn[0].symbols == ["WCC"] and prn[2].symbols == ["OGE"]          # "(NYSE:WCC)", "(NYSE: OGE)"
    nas = rss_news.parse_rss(feed(cfg, "nasdaq_markets"), body("nasdaq_markets"), now=SLOT, slot=SLOT).items
    assert ["GOOGL", "GOOG", "AMZN", "MSFT"] in [i.symbols for i in nas]      # <nasdaq:tickers>
    assert nas[0].symbols == []                                               # untagged: market-wide
    sa = rss_news.parse_rss(feed(cfg, "seekingalpha_currents"), body("seekingalpha_currents"), now=SLOT, slot=SLOT)
    assert sa.items[0].symbols == ["POM"]                                     # category_ticker feed
    cnbc = rss_news.parse_rss(feed(cfg, "cnbc_top"), body("cnbc_top"), now=SLOT, slot=SLOT).items
    assert all(i.symbols == [] for i in cnbc)
    yahoo = rss_news.parse_rss(feed(cfg, "yahoo_ticker"), body("yahoo_ticker"), now=SLOT, slot=SLOT, ticker="BRK-B")
    assert yahoo.items[1].symbols == ["BRK_B", "AMD"]                          # request ticker + cashtag
    entry = RawEntry(key=None, title="Acme (NASDAQ: ACME, TSX: AC) and Beta (Nyse American:BTA) up; $ZZZ rallies",
                     summary="$5 million deal (NYSE: acme)", link=None, stamp=None)
    assert rss_news.tag_tickers(entry, feed(cfg, "benzinga")) == ["ACME", "BTA", "ZZZ"]


def test_benzinga_boilerplate_summary_is_dropped(cfg):
    res = rss_news.parse_rss(feed(cfg, "benzinga"), body("benzinga"), now=SLOT, slot=SLOT)
    assert res.items and all(i.summary == "" for i in res.items)


def test_lookahead_cutoff_is_strictly_before_the_slot(cfg):
    f = feed(cfg, "cnbc_top")
    at = rss_news.parse_rss(f, body("cnbc_top"), now=SLOT, slot=SLOT).items
    latest = max(i.available_at for i in at)
    cut = rss_news.parse_rss(f, body("cnbc_top"), now=SLOT, slot=latest)
    assert all(i.available_at < latest for i in cut.items) and len(cut.items) == len(at) - 1
    assert cut.report.dropped == {"after_slot": 1}
    stale = rss_news.parse_rss(f, body("cnbc_top"), now=SLOT, slot=SLOT + timedelta(hours=49))
    assert stale.items == [] and stale.report.dropped == {"stale": 6}
    skew = rss_news.parse_rss(f, body("cnbc_top"), now=latest - timedelta(hours=1), slot=SLOT)
    assert "news_item_dropped:rss:skew" in skew.flags


def test_money_is_cleaned_and_a_leaking_item_is_dropped(cfg):
    xml = (b"<rss><channel><item><title>Acme wins $12.5 million order</title><description>Shares at "
           b"1234567890 units</description><link>https://www.benzinga.com/a</link>"
           b"<pubDate>Tue, 29 Sep 2026 20:00:00 +0000</pubDate></item></channel></rss>")
    res = rss_news.parse_rss(feed(cfg, "benzinga"), xml, now=SLOT, slot=SLOT)
    assert "$" not in res.items[0].title and "12.5" not in res.items[0].title
    assert "1234567890" not in res.items[0].summary


def test_dedupe_by_link_and_title_joins_tickers(cfg):
    one = (b"<rss><channel><item><title>Chip deal lifts shares</title><link>https://www.nasdaq.com/x?time=1</link>"
           b"<pubDate>Tue, 29 Sep 2026 20:00:00 +0000</pubDate></item></channel></rss>")
    two = one.replace(b"time=1", b"time=2")                                     # same story, other query
    three = (b"<rss><channel><item><title>Chip deal lifts shares!</title><link>https://www.cnbc.com/y</link>"
             b"<pubDate>Tue, 29 Sep 2026 20:01:00 +0000</pubDate></item></channel></rss>")
    a = rss_news.parse_rss(feed(cfg, "nasdaq_markets"), one, now=SLOT, slot=SLOT).items
    b = rss_news.parse_rss(feed(cfg, "yahoo_ticker"), two, now=SLOT, slot=SLOT, ticker="AMD").items
    c = rss_news.parse_rss(feed(cfg, "cnbc_top"), three, now=SLOT, slot=SLOT).items
    assert a[0].id == b[0].id
    [merged] = rss_news.dedupe([*a, *b, *c])
    assert merged.symbols == ["AMD"]


def test_ids_are_keyed_by_the_install_key(cfg):
    f = feed(cfg, "cnbc_top")
    plain = rss_news.parse_rss(f, body("cnbc_top"), now=SLOT, slot=SLOT).items[0].id
    keyed = rss_news.parse_rss(f, body("cnbc_top"), now=SLOT, slot=SLOT, install_key=b"k" * 32).items[0].id
    other = rss_news.parse_rss(f, body("cnbc_top"), now=SLOT, slot=SLOT, install_key=b"j" * 32).items[0].id
    assert len({plain, keyed, other}) == 3 and all(len(x) == 10 for x in (plain, keyed, other))


# ------------------------------------------------------------------------------ ranking
def _item(n: int, feed_label: str, hours: float, symbols=()) -> NewsItem:
    at = SLOT - timedelta(hours=hours)
    return NewsItem(id=f"N:{n:08x}", title=f"headline number {n}", symbols=list(symbols), published_at=at,
                    available_at=at, source="rss", licence="third_party_licensed", feed=feed_label)


def test_ranking_movers_then_press_and_earnings_then_market(cfg):
    items = [_item(1, "cnbc_top", 1), _item(2, "prnewswire_all", 5), _item(3, "benzinga", 9, ["NVDA"]),
             _item(4, "nasdaq_earnings", 2), _item(5, "marketwatch_top", 0.5)]
    out = rss_news.rank_rss(items, cfg, slot=SLOT, priority=["NVDA"])
    assert [i.id for i in out] == ["N:00000003", "N:00000004", "N:00000002", "N:00000005", "N:00000001"]


def test_per_feed_and_total_caps(cfg):
    flood = [_item(n, "benzinga", n / 10) for n in range(1, 30)]
    other = [_item(100 + n, "cnbc_top", 5 + n) for n in range(3)]
    out = rss_news.rank_rss(flood + other, cfg, slot=SLOT)
    assert sum(i.feed == "benzinga" for i in out) == cfg.scout_per_feed and len(out) == cfg.scout_per_feed + 3
    many = [_item(n, f"f{n % 9}", n / 10) for n in range(1, 200)]
    assert len(rss_news.rank_rss(many, cfg, slot=SLOT)) == cfg.scout_max


def test_ranking_admits_only_items_before_the_slot(cfg):
    items = [_item(1, "cnbc_top", -1), _item(2, "cnbc_top", 0), _item(3, "cnbc_top", 49), _item(4, "cnbc_top", 3)]
    assert [i.id for i in rss_news.rank_rss(items, cfg, slot=SLOT)] == ["N:00000004"]


# ------------------------------------------------------------------------------ fetch
def test_market_fetch_reads_each_feed_once(cfg):
    urls: list[str] = []

    def get(url):
        urls.append(url)
        return fixture_get(url)

    got = rss_news.fetch_market(cfg, now=SLOT, slot=SLOT, get=get)
    assert sorted(urls) == sorted(f.url for f in cfg.market_feeds()) and got.flags == []
    assert got.items and len({i.id for i in got.items}) == len(got.items)
    assert got.sources["rss"].feeds_ok == 10


def test_ticker_fetch_is_per_name_and_capped(cfg):
    urls: list[str] = []

    def get(url):
        urls.append(url)
        return fixture_get(url)

    names = [f"T{n}" for n in range(40)] + ["BRK_B"]
    rss_news.fetch_tickers(cfg, names, now=SLOT, slot=SLOT, get=get, sleep=lambda _s: None)
    assert len(urls) == cfg.yahoo_max_tickers and "s=T0&" in urls[0]
    urls.clear()
    got = rss_news.fetch_tickers(cfg, ["BRK_B"], now=SLOT, slot=SLOT, get=get, sleep=lambda _s: None)
    assert "s=BRK-B&" in urls[0] and got.items[0].symbols[0] == "BRK_B"


def test_a_failing_feed_is_flagged_and_the_others_still_count(cfg):
    def get(url):
        if "cnbc" in url:
            raise httpx.ConnectTimeout("slow")
        if "benzinga" in url:
            return b"<!DOCTYPE x [<!ENTITY a 'b'>]><rss/>"
        return fixture_get(url)

    got = rss_news.fetch_market(cfg, now=SLOT, slot=SLOT, get=get)
    assert set(got.flags) == {"news_source_error:rss:cnbc_top", "news_source_error:rss:cnbc_earnings",
                              "news_source_error:rss:benzinga"}
    assert got.sources["rss"].feeds_failed == 3 and got.sources["rss"].feeds_ok == 7 and got.items


def test_a_feed_still_running_at_the_budget_is_flagged(cfg):
    import threading

    gate = threading.Event()

    def get(url):
        if "fda.gov" in url:
            gate.wait(5)
        return fixture_get(url)

    got = rss_news.fetch_market(cfg, now=SLOT, slot=SLOT, get=get, budget_s=0.5)
    gate.set()
    assert "news_source_error:rss:fda_press" in got.flags and got.items


def test_xxe_and_entity_documents_are_refused(cfg):
    xxe = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
           b"<rss><channel><item><title>&x;</title></item></channel></rss>")
    with pytest.raises(NewsParseError):
        rss_news.parse_rss(feed(cfg, "benzinga"), xxe, now=SLOT, slot=SLOT)


def test_the_http_getter_refuses_other_hosts_and_sends_a_browser_agent(cfg):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "www.benzinga.com":
            return httpx.Response(302, headers={"Location": "https://evil.example.org/feed"})
        return httpx.Response(200, content=body("cnbc_top"))

    get, client = rss_news.http_getter(cfg, transport=httpx.MockTransport(handler))
    try:
        assert get(feed(cfg, "cnbc_top").url) == body("cnbc_top")
        assert seen[0].headers["User-Agent"].startswith("Mozilla/5.0")
        with pytest.raises(Exception, match="allow-list"):
            get(feed(cfg, "benzinga").url)
        assert all(r.url.host != "evil.example.org" for r in seen)
    finally:
        client.close()


# ------------------------------------------------------------------------------ the core news path
def test_the_shared_news_fetch_adds_the_market_feeds_and_the_core_quota_caps_them(policy_module, monkeypatch):
    from council import context
    from council.data import gov_news
    from council.data.gov_news import NewsFetch
    from council.facts.pack import admissible_news

    cfg = rss_news.rss_config(policy_module)
    monkeypatch.setattr(gov_news, "gather_public_news", lambda *a, **k: NewsFetch(flags=["gov_ok"]))
    monkeypatch.setattr(rss_news, "market_fetcher", lambda policy, state_dir: (
        lambda now, slot: rss_news.fetch_market(cfg, now=now, slot=slot, get=fixture_get)))
    news, _feed = context.news_sources(policy_module, broker=None, state_dir=None, clock=lambda: SLOT)
    got = news(SLOT)
    rss = [i for i in got.items if i.source == "rss"]
    assert rss and "gov_ok" in got.flags and got.sources["rss"].feeds_ok == 10
    core = admissible_news(got.items, SLOT, policy_module)
    assert sum(i.source == "rss" for i in core) == policy_module.council["news"]["quotas"]["rss"] == 6


def test_a_broken_rss_fetch_never_costs_the_public_items(policy_module, monkeypatch):
    from council import context
    from council.data import gov_news
    from council.data.gov_news import NewsFetch

    item = NewsItem(id="P:00000001", title="Fed release", published_at=SLOT - timedelta(hours=1),
                    available_at=SLOT - timedelta(hours=1), source="fed_board")
    monkeypatch.setattr(gov_news, "gather_public_news", lambda *a, **k: NewsFetch(items=[item]))

    def boom(now, slot):
        raise RuntimeError("x")

    monkeypatch.setattr(rss_news, "market_fetcher", lambda policy, state_dir: boom)
    got = context.public_and_rss(policy_module, SLOT, None, slot=SLOT)
    assert [i.id for i in got.items] == ["P:00000001"] and "news_source_error:rss:RuntimeError" in got.flags
