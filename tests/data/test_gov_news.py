"""council.data.gov_news: public-domain federal news -> NewsItem (`P:` ids). No network: recorded
feeds under tests/fixtures/news/feeds/ served by httpx.MockTransport, and the dated licence archives
under tests/fixtures/news/licences/ checked against their manifest."""

from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from council.data import gov_news
from council.data.credentials import SEC_USER_AGENT_ENV, MissingCredential
from council.data.gov_news import (
    GOV_SOURCES,
    OIL_TOPIC,
    SOURCES,
    TOTAL_BUDGET_S,
    Feed,
    NewsFetch,
    SourceResult,
    fetch_source,
    gather_public_news,
    parse_source_feed,
    public_link,
)
from council.models.facts import NEWS_LINK_HOSTS, check_https_link, public_news_id
from council.paths import REPO_ROOT
from council.publish import leakscan

NEWS = REPO_ROOT / "tests" / "fixtures" / "news"
MANIFEST = json.loads((NEWS / "licences" / "manifest.json").read_text())
RECORDED = datetime.fromisoformat(next(iter(MANIFEST["pages"].values()))["retrieved"].replace("Z", "+00:00"))
LONG = timedelta(days=400)                     # the recordings reach back months; the live window is 48 h
UA = "Council Book Tests tests@example.org"


def feed_bytes(source: str, label: str) -> bytes:
    return (NEWS / "feeds" / f"{source}-{label}.xml").read_bytes()


def recorded_routes() -> dict[str, bytes]:
    return {feed.url: feed_bytes(key, feed.label) for key in GOV_SOURCES for feed in SOURCES[key].feeds}


class FakeWeb:
    """MockTransport handler: recorded documents by exact URL; `status` overrides per URL; `raise_for`
    maps a URL to an exception raised instead of answering."""

    def __init__(self, routes: dict[str, bytes] | None = None) -> None:
        self.routes = recorded_routes() if routes is None else routes
        self.status: dict[str, int] = {}
        self.raise_for: dict[str, Exception] = {}
        self.seen: list[httpx.Request] = []
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.seen.append(request)
        url = str(request.url)
        if url in self.raise_for:
            raise self.raise_for[url]
        if url in self.status:
            return httpx.Response(self.status[url], text="error")
        body = self.routes.get(url)
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404, text="nope")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def rss(*items: str, root: str = "rss") -> bytes:
    body = "".join(items)
    return f'<?xml version="1.0"?><{root} version="2.0"><channel><title>t</title>{body}</channel></{root}>'.encode()


def rss_item(title: str, stamp: str, *, link: str = "https://www.federalreserve.gov/x.htm", guid: str | None = None,
             description: str = "") -> str:
    guid_el = f"<guid>{guid}</guid>" if guid else ""
    return (f"<item><title>{html.escape(title)}</title><link>{link}</link>{guid_el}"
            f"<description>{html.escape(description)}</description><pubDate>{stamp}</pubDate></item>")


FED = SOURCES["fed_board"]
FED_FEED = FED.feeds[0]


def parse(data: bytes, *, now: datetime = RECORDED, slot: datetime | None = None, spec=FED, feed=FED_FEED,
          lookback: timedelta = LONG, lines=()) -> SourceResult:
    return parse_source_feed(spec, feed, data, now=now, slot=slot or now, lines=lines, lookback=lookback)


# ------------------------------------------------------------------------------------ recorded feeds


@pytest.mark.parametrize(("source", "label"), [(k, f.label) for k in GOV_SOURCES for f in SOURCES[k].feeds])
def test_every_recorded_feed_produces_clean_public_items(source, label):
    spec = SOURCES[source]
    feed = next(f for f in spec.feeds if f.label == label)
    result = parse(feed_bytes(source, label), spec=spec, feed=feed, lines=("OIL",))
    assert result.items, f"{source}/{label} produced no items"
    for item in result.items:
        assert re.fullmatch(r"P:[0-9a-f]{8}", item.id)
        assert item.source == source and item.licence == spec.licence
        assert item.available_at < RECORDED and item.published_at <= item.available_at
        assert 0 < len(item.title) <= gov_news.TITLE_MAX and len(item.summary) <= spec.summary_max
        assert leakscan.scan(item.title) == [] and leakscan.scan(item.summary) == []
        assert item.link is None or check_https_link(item.link) == item.link
        on_topic = feed.topic is None or feed.topic.search(f"{item.title}\n{item.summary}")
        assert item.symbols == (list(feed.symbols) if on_topic else [])
        assert item.form is None and item.items == []
    assert len({i.id for i in result.items}) == len(result.items)


def test_ids_are_stable_and_follow_the_guid_rule():
    first = parse(feed_bytes("fed_board", "press_monetary")).items
    again = parse(feed_bytes("fed_board", "press_monetary")).items
    assert [i.id for i in first] == [i.id for i in again]
    statement = next(i for i in first if i.link and i.link.endswith("monetary20260916a.htm"))
    guid = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm"
    assert statement.id == "P:" + hashlib.sha256(b"fed_board\0" + guid.encode()).hexdigest()[:8]
    assert statement.published_at == datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
    assert statement.summary == ""                         # the feed repeats the title as description


def test_items_without_a_guid_get_distinct_ids_per_feed_and_title():
    spec = SOURCES["treasury"]
    ann, res = spec.feeds
    announced = parse(feed_bytes("treasury", ann.label), spec=spec, feed=ann).items
    auctioned = parse(feed_bytes("treasury", res.label), spec=spec, feed=res).items
    ids = [i.id for i in announced + auctioned]
    assert len(ids) == len(set(ids))                      # same link and time for both feeds' items
    assert all(i.summary == "" for i in announced + auctioned)        # Treasury: title, link, time only


def test_public_cleaning_removes_levels_and_amounts_from_recorded_text():
    bls = parse(feed_bytes("bls", "empsit"), spec=SOURCES["bls"], feed=SOURCES["bls"].feeds[0]).items
    assert any("[level removed]" in i.title for i in bls)
    bea = parse(feed_bytes("bea", "news_releases"), spec=SOURCES["bea"], feed=SOURCES["bea"].feeds[0]).items
    assert any("[amount removed]" in i.summary for i in bea)
    blob = json.dumps([i.model_dump(mode="json") for i in bls + bea])
    assert "$" not in blob and "<p>" not in blob


def test_bare_domain_links_become_the_allowed_www_host():
    bea = parse(feed_bytes("bea", "news_releases"), spec=SOURCES["bea"], feed=SOURCES["bea"].feeds[0]).items
    assert bea and all(i.link and i.link.startswith("https://www.bea.gov/news/") for i in bea)


def test_relative_links_resolve_against_the_feed():
    spec = SOURCES["eia"]
    items = parse(feed_bytes("eia", "press_releases"), spec=spec, feed=spec.feeds[0], lines=("OIL",)).items
    assert items and all(i.link and i.link.startswith("https://www.eia.gov/pressroom/releases/") for i in items)
    assert all(i.symbols == (["OIL"] if OIL_TOPIC.search(f"{i.title} {i.summary}") else []) for i in items)
    untagged = parse(feed_bytes("eia", "press_releases"), spec=spec, feed=spec.feeds[0], lines=()).items
    assert all(i.symbols == [] for i in untagged)          # the policy has no OIL line: no tag


# ------------------------------------------------------------------------------------ time rules


def test_skew_drop_sets_its_flag_and_five_minutes_are_tolerated():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    data = rss(rss_item("Board announces a", "Sat, 26 Sep 2026 12:04:00 GMT", guid="a"),
               rss_item("Board announces b", "Sat, 26 Sep 2026 12:10:00 GMT", guid="b"))
    result = parse(data, now=now, slot=now + timedelta(hours=1))
    assert [i.title for i in result.items] == ["Board announces a"]
    assert result.flags == ["news_item_dropped:fed_board:skew"]
    assert result.report.dropped == {"skew": 1}


def test_lookahead_only_items_before_the_slot():
    slot = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    data = rss(rss_item("before", "Sat, 26 Sep 2026 11:59:59 GMT", guid="1"),
               rss_item("at the slot", "Sat, 26 Sep 2026 12:00:00 GMT", guid="2"),
               rss_item("after", "Sat, 26 Sep 2026 12:01:00 GMT", guid="3"),
               rss_item("too old", "Wed, 23 Sep 2026 11:59:59 GMT", guid="4"))
    result = parse(data, now=slot + timedelta(minutes=3), slot=slot, lookback=gov_news.LOOKBACK)
    assert [i.title for i in result.items] == ["before"]
    assert result.report.dropped == {"after_slot": 2, "stale": 1}
    assert result.flags == []                              # normal drops are not quality flags


def test_date_only_items_become_available_at_the_next_midnight_utc():
    data = rss(rss_item("Release", "2026-09-24", guid="d"))
    early = parse(data, now=datetime(2026, 9, 25, 3, 0, tzinfo=UTC), slot=datetime(2026, 9, 24, 20, 0, tzinfo=UTC))
    assert early.items == [] and early.report.dropped == {"after_slot": 1}
    late = parse(data, now=datetime(2026, 9, 25, 3, 0, tzinfo=UTC), slot=datetime(2026, 9, 25, 0, 0, 1, tzinfo=UTC))
    (item,) = late.items
    assert item.published_at == datetime(2026, 9, 24, tzinfo=UTC)
    assert item.available_at == datetime(2026, 9, 25, tzinfo=UTC)


@pytest.mark.parametrize(("raw", "expected"), [
    ("Wed, 16 Sep 2026 18:00:00 GMT", datetime(2026, 9, 16, 18, 0, tzinfo=UTC)),
    ("Thu, 24 Sep 2026 08:30:00 EDT", datetime(2026, 9, 24, 12, 30, tzinfo=UTC)),
    ("2026-09-04T07:51:08.695-04:00", datetime(2026, 9, 4, 11, 51, 8, 695000, tzinfo=UTC)),
    ("2026-09-24T09:00:00", datetime(2026, 9, 24, 13, 0, tzinfo=UTC)),       # no zone: New York
    ("Thu, 24 Sep 2026 09:00:00 -0000", datetime(2026, 9, 24, 13, 0, tzinfo=UTC)),
])
def test_stamp_formats(raw, expected):
    stamp = gov_news.parse_stamp(raw)
    assert stamp is not None and stamp.published_at == expected and stamp.available_at == expected


def test_an_item_without_a_parseable_time_is_dropped_with_a_flag():
    result = parse(rss(rss_item("x", "################### EST", guid="n")))
    assert result.items == [] and result.flags == ["news_item_dropped:fed_board:no_time"]


# ------------------------------------------------------------------------------------ cleaning, scan, links


def test_an_item_that_trips_the_leak_scan_is_dropped_with_its_flag():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    data = rss(rss_item("Board note on server 10.20.30.40 outage", "Sat, 26 Sep 2026 11:00:00 GMT", guid="ip"),
               rss_item("Board announces rule", "Sat, 26 Sep 2026 11:00:00 GMT", guid="ok",
                        description="token ghp_abcdefghijklmnop was rotated"),
               rss_item("Board issues statement", "Sat, 26 Sep 2026 11:00:00 GMT", guid="fine"))
    result = parse(data, now=now)
    assert [i.title for i in result.items] == ["Board issues statement"]
    assert result.flags == ["news_item_dropped:fed_board:leak"]
    assert result.report.dropped == {"leak": 2}


def test_text_is_sanitised_before_the_public_cleaner():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    title = "Board fines bank $4.5 million; see https://evil.example/x @handle \u202eok"
    data = rss(rss_item(title, "Sat, 26 Sep 2026 11:00:00 GMT", guid="s",
                        description="<p>Payrolls rose 1,234,567 &amp; more</p>"))
    (item,) = parse(data, now=now).items
    assert item.title == "Board fines bank [amount removed] million; see ok"
    assert item.summary == "Payrolls rose [level removed] & more"


def test_links_are_typed_upgraded_or_dropped_never_text():
    assert public_link("http://www.bls.gov/news.release/empsit.nr0.htm") == (
        "https://www.bls.gov/news.release/empsit.nr0.htm", False)
    assert public_link("https://bea.gov/news/2026/x") == ("https://www.bea.gov/news/2026/x", False)
    assert public_link("/pressroom/releases/press592.php", base="https://www.eia.gov/rss/press_rss.xml") == (
        "https://www.eia.gov/pressroom/releases/press592.php", False)
    assert public_link("https://www.sec.gov/Archives/edgar/data/1045810/x-index.htm") == (None, True)
    assert public_link("https://evil.example/a") == (None, True)
    assert public_link("") == (None, False)
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    data = rss(rss_item("Board issues statement", "Sat, 26 Sep 2026 11:00:00 GMT", guid="l",
                        link="https://evil.example/phish"))
    result = parse(data, now=now)
    (item,) = result.items
    assert item.link is None                                  # the link is dropped, the item kept
    assert result.flags == ["news_link_dropped:fed_board"] and result.report.links_dropped == 1


def test_atom_and_rdf_documents_parse():
    atom = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>'
            b'<entry><title>Atom entry</title><link href="https://www.bls.gov/a.htm"/><id>atom-1</id>'
            b'<content>body</content><updated>2026-09-26T07:00:00-04:00</updated></entry></feed>')
    rdf = (b'<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
           b'xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">'
           b'<item rdf:about="https://www.bls.gov/b.htm"><title>RDF item</title><link>https://www.bls.gov/b.htm</link>'
           b'<dc:date>2026-09-26T11:00:00Z</dc:date></item></rdf:RDF>')
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    (a,) = parse(atom, now=now).items
    (r,) = parse(rdf, now=now).items
    assert (a.title, a.summary, a.link) == ("Atom entry", "body", "https://www.bls.gov/a.htm")
    assert a.id == public_news_id("fed_board", "atom-1")
    assert r.id == public_news_id("fed_board", "https://www.bls.gov/b.htm") and r.title == "RDF item"


@pytest.mark.parametrize("data", [
    b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><rss><channel/></rss>',
    b'<?xml version="1.0"?><!ENTITY x "y"><rss/>',
    b"<rss><channel><item>",
    b"<html><body>not a feed</body></html>",
    b"<rss>" + b" " * (gov_news.MAX_BYTES + 1) + b"</rss>",
], ids=["doctype", "entity", "truncated", "not_a_feed", "oversized"])
def test_unsafe_or_malformed_documents_are_refused(data):
    with pytest.raises(gov_news.NewsParseError):
        gov_news.parse_feed(data)


# ------------------------------------------------------------------------------------ fetching one source


def test_fetch_source_uses_the_news_timeouts_and_a_generic_agent():
    web = FakeWeb()
    result = fetch_source(FED, now=RECORDED, slot=RECORDED, transport=web.transport(), lookback=LONG)
    assert result.report.feeds_ok == len(FED.feeds) and result.flags == [] and result.items
    for request in web.seen:
        assert request.extensions["timeout"] == {"connect": 5.0, "read": 10.0, "write": 10.0, "pool": 10.0}
        assert request.headers["User-Agent"] == gov_news.GENERIC_USER_AGENT
        assert "rss+xml" in request.headers["Accept"]


@pytest.mark.parametrize(("status", "kind"), [(404, "not_found"), (403, "auth"), (401, "auth"), (500, "http_500"),
                                              (429, "rate_limited")])
def test_a_failing_feed_is_flagged_and_the_other_feeds_are_kept(status, kind):
    web = FakeWeb()
    web.status[FED.feeds[0].url] = status
    result = fetch_source(FED, now=RECORDED, slot=RECORDED, transport=web.transport(), lookback=LONG)
    assert result.flags == [f"news_source_error:fed_board:{kind}"]
    assert result.report.feeds_failed == 1 and result.report.feeds_ok == len(FED.feeds) - 1
    assert result.items


def test_a_timeout_is_typed_and_retried_once():
    web = FakeWeb()
    web.raise_for[FED.feeds[1].url] = httpx.ReadTimeout("slow")
    result = fetch_source(FED, now=RECORDED, slot=RECORDED, transport=web.transport(), lookback=LONG)
    assert result.flags == ["news_source_error:fed_board:timeout"]
    assert sum(1 for r in web.seen if str(r.url) == FED.feeds[1].url) == 1 + gov_news.RETRIES


def test_a_redirect_off_the_allow_list_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/feed.xml"})

    spec = SOURCES["bea"]
    result = fetch_source(spec, now=RECORDED, slot=RECORDED, transport=httpx.MockTransport(handler))
    assert result.flags == ["news_source_error:bea:host_refused"] and result.items == []


def test_bls_sends_the_contact_agent_and_needs_it(monkeypatch):
    spec = SOURCES["bls"]
    web = FakeWeb()
    monkeypatch.delenv(SEC_USER_AGENT_ENV, raising=False)
    with pytest.raises(MissingCredential):
        fetch_source(spec, now=RECORDED, slot=RECORDED, transport=web.transport())
    assert web.seen == []                                    # raised before any request
    monkeypatch.setenv(SEC_USER_AGENT_ENV, UA)
    result = fetch_source(spec, now=RECORDED, slot=RECORDED, transport=web.transport(), lookback=LONG)
    assert result.items and {r.headers["User-Agent"] for r in web.seen} == {UA}


# ------------------------------------------------------------------------------------ gathering


def _ok(source: str, *titles: str, at: datetime = RECORDED - timedelta(hours=1)):
    def task(deadline: float) -> SourceResult:
        out = SourceResult(source)
        for title in titles:
            out.items.append(gov_news.NewsItem(
                id=public_news_id(source, title), title=title, published_at=at, available_at=at,
                source=source, licence=SOURCES[source].licence))
        return out
    return task


def test_a_source_past_the_budget_is_flagged_and_the_others_return():
    release = threading.Event()

    def hang(deadline: float) -> SourceResult:
        release.wait(10)
        return SourceResult("fed_board")

    try:
        started = time.monotonic()
        fetch = gather_public_news(None, RECORDED, budget_s=0.3,
                                   sources={"fed_board": hang, "bls": _ok("bls", "Payrolls")})
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert elapsed < 1.3
    assert fetch.flags == ["news_source_error:fed_board:budget"]
    assert [i.title for i in fetch.items] == ["Payrolls"]
    assert fetch.sources["fed_board"].error == "budget"


def test_the_default_budget_is_thirty_seconds_and_is_enforced_by_the_deadline():
    assert TOTAL_BUDGET_S == 30.0
    assert gather_public_news.__kwdefaults__["budget_s"] == TOTAL_BUDGET_S
    release = threading.Event()
    ticks = iter([0.0] + [31.0] * 50)                          # the clock jumps past 30 s after the start

    def hang(deadline: float) -> SourceResult:
        assert deadline == 30.0
        release.wait(10)
        return SourceResult("eia")

    try:
        started = time.monotonic()
        fetch = gather_public_news(None, RECORDED, sources={"eia": hang, "bea": _ok("bea", "GDP")},
                                   monotonic=lambda: next(ticks))
        assert time.monotonic() - started < 31
    finally:
        release.set()
    assert "news_source_error:eia:budget" in fetch.flags


def test_a_source_that_raises_leaves_the_others_intact():
    def boom(deadline: float) -> SourceResult:
        raise RuntimeError("secret detail that must not become a flag")

    fetch = gather_public_news(None, RECORDED, sources={"treasury": boom, "fed_board": _ok("fed_board", "FOMC")})
    assert fetch.flags == ["news_source_error:treasury:RuntimeError"]
    assert [i.title for i in fetch.items] == ["FOMC"]
    assert "secret" not in json.dumps(fetch.model_dump(mode="json"))


def test_merge_is_unique_newest_first_and_keeps_the_later_time():
    early, late = RECORDED - timedelta(hours=3), RECORDED - timedelta(hours=2)

    def dup(deadline: float) -> SourceResult:
        out = _ok("sec", "8-K", at=early)(deadline)
        out.items += _ok("sec", "8-K", at=late)(deadline).items
        return out

    fetch = gather_public_news(None, RECORDED, sources={
        "sec": dup, "bls": _ok("bls", "CPI", at=RECORDED - timedelta(hours=1)),
        "bea": _ok("bea", "future", at=RECORDED + timedelta(minutes=1))})
    assert [i.title for i in fetch.items] == ["CPI", "8-K"]
    assert fetch.items[1].available_at == late
    assert isinstance(fetch, NewsFetch)


def test_flags_follow_the_fixed_source_order_whatever_finishes_first():
    def fail(source: str, delay: float):
        def task(deadline: float) -> SourceResult:
            time.sleep(delay)
            raise ValueError("x")
        return task

    fetch = gather_public_news(None, RECORDED, sources={"eia": fail("eia", 0.0), "sec": fail("sec", 0.05),
                                                        "bls": fail("bls", 0.02)})
    assert fetch.flags == ["news_source_error:sec:ValueError", "news_source_error:bls:ValueError",
                           "news_source_error:eia:ValueError"]


def test_the_default_sources_on_recorded_feeds(policy, monkeypatch):
    monkeypatch.setenv(SEC_USER_AGENT_ENV, UA)
    web = FakeWeb()
    fetch = gather_public_news(policy, RECORDED, transport=web.transport(), lookback=LONG)
    assert set(fetch.sources) == set(GOV_SOURCES)            # the core policy has no stock lines: no SEC
    assert fetch.flags == []
    assert {i.source for i in fetch.items} == set(GOV_SOURCES)
    assert fetch.items == sorted(fetch.items, key=lambda n: (-n.published_at.timestamp(), n.id))
    assert all(i.available_at < RECORDED for i in fetch.items)
    assert {r.url.host for r in web.seen} <= NEWS_LINK_HOSTS


def test_without_a_contact_agent_only_the_sources_that_need_it_fail(policy, monkeypatch):
    monkeypatch.delenv(SEC_USER_AGENT_ENV, raising=False)
    fetch = gather_public_news(policy, RECORDED, transport=FakeWeb().transport(), lookback=LONG)
    assert fetch.flags == ["news_source_error:bls:no_user_agent"]
    assert {i.source for i in fetch.items} == set(GOV_SOURCES) - {"bls"}


def test_the_default_sources_include_sec_only_with_stock_lines(policy, sleeve_policy):
    assert "sec" not in gov_news.default_tasks(policy, now=RECORDED, slot=RECORDED)
    assert list(gov_news.default_tasks(sleeve_policy, now=RECORDED, slot=RECORDED)) == ["sec", *GOV_SOURCES]


def test_naive_times_are_refused():
    with pytest.raises(ValueError):
        gather_public_news(None, datetime(2026, 9, 26, 12, 0), sources={})


# ------------------------------------------------------------------------------------ licences


def _page_text(raw: bytes) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "replace")))
    return " ".join(text.replace("’", "'").split())


def test_licence_manifest_hashes_match_the_archived_pages():
    assert MANIFEST["checked"] == "2026-09-26"
    assert set(MANIFEST["pages"]) == set(SOURCES)
    for key, page in MANIFEST["pages"].items():
        raw = (NEWS / "licences" / page["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == page["sha256"], key
        assert len(raw) == page["bytes"] and page["file"] == SOURCES[key].licence_archive
        assert page["url"] == SOURCES[key].licence_url and page["licence"] == SOURCES[key].licence
        assert not re.search(rb"(?i)<script\b", raw)           # archived without scripts (manifest transform)


def test_each_recorded_licence_statement_is_in_its_archived_page():
    for key, spec in SOURCES.items():
        text = _page_text((NEWS / "licences" / spec.licence_archive).read_bytes())
        if spec.licence == "public_domain":
            assert spec.licence_quote and " ".join(spec.licence_quote.split()) in text, key
        else:                                                   # Treasury: no statement, recorded as such
            assert spec.licence_quote == "" and "public domain" not in text.lower(), key


def test_licence_status_and_public_fields_per_source():
    assert {k: s.licence for k, s in SOURCES.items()} == {
        "sec": "public_domain", "fed_board": "public_domain", "bls": "public_domain",
        "bea": "public_domain", "treasury": "federal_work_unverified", "eia": "public_domain"}
    assert "summary" not in SOURCES["treasury"].public_fields and SOURCES["treasury"].summary_max == 0
    assert SOURCES["sec"].attribution == "Source: U.S. Securities and Exchange Commission"
    assert "EDGAR" not in json.dumps([s.attribution for s in SOURCES.values()])
    assert gov_news.attribution("eia", datetime(2026, 9, 9, 17, tzinfo=UTC)) == (
        "Source: U.S. Energy Information Administration (2026-09-09)")
    for spec in SOURCES.values():
        for feed in spec.feeds:
            assert isinstance(feed, Feed) and feed.url.startswith("https://")
            assert httpx.URL(feed.url).host in gov_news.FETCH_HOSTS


def test_the_fixture_readme_cites_every_archived_hash():
    readme = (NEWS / "README.md").read_text()
    for key, page in MANIFEST["pages"].items():
        assert f"`{page['sha256']}`" in readme, key


def test_an_updated_entry_is_available_from_its_update():
    atom = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>'
            b'<entry><title>Revised release</title><link href="https://www.bls.gov/a.htm"/><id>rev-1</id>'
            b'<published>2026-09-26T07:00:00-04:00</published><updated>2026-09-26T09:30:00-04:00</updated>'
            b'</entry></feed>')
    (item,) = parse(atom, now=datetime(2026, 9, 26, 14, 0, tzinfo=UTC)).items
    assert item.available_at == item.published_at == datetime(2026, 9, 26, 13, 30, tzinfo=UTC)
    early = parse(atom, now=datetime(2026, 9, 26, 14, 0, tzinfo=UTC), slot=datetime(2026, 9, 26, 12, 0, tzinfo=UTC))
    assert early.items == [] and early.report.dropped == {"after_slot": 1}


def test_items_published_after_the_slot_cannot_change_the_fetch(policy, monkeypatch):
    monkeypatch.setenv(SEC_USER_AGENT_ENV, UA)
    slot = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    base = gather_public_news(policy, RECORDED, slot=slot, transport=FakeWeb().transport(), lookback=LONG)
    web = FakeWeb()
    url = FED.feeds[0].url
    late = rss_item("Board announces an emergency action", "Thu, 24 Sep 2026 12:00:00 GMT", guid="late").encode()
    web.routes[url] = web.routes[url].replace(b"<item>", late + b"<item>", 1)
    mutated = gather_public_news(policy, RECORDED, slot=slot, transport=web.transport(), lookback=LONG)
    assert base.items and mutated.items == base.items
    assert all(i.available_at < slot for i in base.items)


def test_the_sources_doc_matches_the_register_in_code():
    """docs/news-sources.md records each source's licence status as the code does (and passes the
    public leak scan the pre-commit hook runs on docs/)."""
    doc = (REPO_ROOT / "docs" / "news-sources.md").read_text()
    rows = {line.split("|")[1].strip(): line for line in doc.splitlines() if line.startswith("| ") and "`" in line}
    names = {"sec": "SEC (Form 8-K and 6-K metadata)", "fed_board": "Federal Reserve Board", "bls": "BLS",
             "bea": "BEA", "treasury": "U.S. Treasury", "eia": "EIA"}
    assert set(names) == set(SOURCES)
    for key, spec in SOURCES.items():
        row = rows[names[key]]
        assert f"`{spec.licence}`" in row, key
        assert f"`{MANIFEST['pages'][key]['sha256']}`" in row, key
        assert spec.attribution.replace("{date}", "release date") in row, key
        if spec.licence_quote:
            assert f'"{spec.licence_quote}"' in row, key
    assert "EDGAR" not in " ".join(row.split("|")[6] for row in rows.values())
    for host in NEWS_LINK_HOSTS:
        assert host in doc
    assert leakscan.scan(doc) == []


def test_eia_items_are_tagged_to_oil_only_when_they_are_about_oil():
    spec = SOURCES["eia"]
    feed = next(f for f in spec.feeds if f.label == "today_in_energy")
    items = parse(feed_bytes("eia", "today_in_energy"), spec=spec, feed=feed, lines=("OIL",),
                  lookback=timedelta(days=400)).items
    by_title = {i.title: i.symbols for i in items}
    assert by_title["United States on track for record crude oil production in 2026"] == ["OIL"]
    assert by_title["What goes into diesel prices?"] == ["OIL"]
    assert by_title["Weekly average load in ERCOT continues near record high"] == []
    assert by_title["U.S. uranium production more than tripled in 2025 and was the highest since 2017"] == []
    assert any(s == [] for s in by_title.values()) and any(s == ["OIL"] for s in by_title.values())
