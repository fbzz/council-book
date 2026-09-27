"""council.stocks.sec_news: 8-K / 6-K filing metadata of the stock lines as public-domain news items.
No network: the recorded current-filings Atom documents (tests/fixtures/news/feeds/) with the sleeve
fixture's CIKs substituted in, and synthetic submissions, behind httpx.MockTransport."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from council.data import gov_news
from council.data.credentials import SEC_USER_AGENT_ENV, MissingCredential
from council.data.gov_news import SourceResult, gather_public_news
from council.models.facts import check_https_link, public_news_id
from council.paths import REPO_ROOT
from council.stocks import sec_news
from council.stocks.sec import SecClient
from council.stocks.sec_news import (
    ITEM_TITLES,
    Filing,
    backfill_filings,
    fetch_sec_news,
    filing_link,
    filing_title,
    parse_current_atom,
)
from tests.stocks.conftest import UA, FakeClock

NEWS = REPO_ROOT / "tests" / "fixtures" / "news"
MANIFEST = json.loads((NEWS / "licences" / "manifest.json").read_text())
RECORDED = datetime.fromisoformat(MANIFEST["pages"]["sec"]["retrieved"].replace("Z", "+00:00"))
ATOM_8K = (NEWS / "feeds" / "sec-current-8-K.xml").read_bytes()
ATOM_6K = (NEWS / "feeds" / "sec-current-6-K.xml").read_bytes()
# The first entry of each recording, re-assigned to a sleeve fixture line: TSTA (selected) filed the
# 8-K (Item 1.01, accepted 2026-09-25 17:30:12 New York), TSTD (shortlist) the 6-K.
TSTA_ATOM_ACCESSION = "0002071876-26-000224"
ATOM_8K_TSTA = ATOM_8K.replace(b"(0001405513)", b"(0000900001)")
ATOM_6K_TSTD = ATOM_6K.replace(b"(0001888014)", b"(0000900005)")


def submissions(cik: int, rows: list[tuple[str, str, str, str]], name: str = "Test Co") -> dict:
    """rows: (accession, form, acceptanceDateTime as SEC writes it, items)."""
    return {"cik": str(cik), "name": name, "filings": {"recent": {
        "accessionNumber": [r[0] for r in rows], "filingDate": [r[2][:10] for r in rows],
        "reportDate": [""] * len(rows), "acceptanceDateTime": [r[2] for r in rows],
        "form": [r[1] for r in rows], "items": [r[3] for r in rows]}}}


class FakeSec:
    """The current-filings Atom by form, submissions by CIK (empty documents unless given)."""

    def __init__(self, atom: dict[str, bytes] | None = None, subs: dict[int, dict] | None = None) -> None:
        self.atom = {"8-K": ATOM_8K_TSTA, "6-K": ATOM_6K_TSTD} if atom is None else atom
        self.subs = subs or {}
        self.status: dict[str, int] = {}
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        url = request.url
        if url.host == "www.sec.gov" and url.path == "/cgi-bin/browse-edgar":
            form = url.params.get("type", "")
            if form in self.status:
                return httpx.Response(self.status[form], text="error")
            body = self.atom.get(form)
            return httpx.Response(200, content=body) if body is not None else httpx.Response(404)
        if url.host == "data.sec.gov" and url.path.startswith("/submissions/CIK"):
            cik = int(url.path.removeprefix("/submissions/CIK").removesuffix(".json"))
            if f"sub:{cik}" in self.status:
                return httpx.Response(self.status[f"sub:{cik}"], text="error")
            return httpx.Response(200, json=self.subs.get(cik, submissions(cik, [])))
        return httpx.Response(404, text="unknown")

    def client(self, clock: FakeClock | None = None) -> SecClient:
        clock = clock or FakeClock()
        return SecClient(UA, transport=httpx.MockTransport(self), clock=clock, sleep=clock.sleep)


def fetch(policy, fake: FakeSec, **kw) -> SourceResult:
    with fake.client(kw.pop("clock", None)) as client:
        return fetch_sec_news(policy, now=RECORDED, sec=client, **kw)


# ------------------------------------------------------------------------------------ titles, links


def test_official_item_titles_and_forms():
    assert filing_title("8-K", ("2.02", "9.01")) == (
        "8-K: Item 2.02 Results of Operations and Financial Condition; Item 9.01 Financial Statements and Exhibits")
    assert filing_title("8-K/A", ("5.02",)).startswith("8-K/A (amendment): Item 5.02 Departure of Directors")
    assert filing_title("8-K", ("7.99",)) == "8-K: Item 7.99"
    assert filing_title("8-K", ()) == "8-K: current report"
    assert filing_title("6-K", ("2.02",)) == "6-K: report of a foreign private issuer"
    assert len(ITEM_TITLES) == 34 and all(len(code) == 4 for code in ITEM_TITLES)
    assert ITEM_TITLES["1.05"] == "Material Cybersecurity Incidents"


def test_the_recorded_feed_lists_codes_the_title_map_knows():
    codes = {code for f in parse_current_atom(ATOM_8K) for code in f.items}
    assert codes and codes <= set(ITEM_TITLES)


def test_links_are_keyed_by_ticker_and_pass_the_link_type():
    link = filing_link("TSTC-B", "8-K")
    assert link == "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=TSTC-B&type=8-K"
    assert check_https_link(link) == link
    assert check_https_link(filing_link("BRK-B", "8-K/A")).endswith("type=8-K%2FA")


# ------------------------------------------------------------------------------------ parsing


def test_the_recorded_atom_parses_every_entry():
    filings = parse_current_atom(ATOM_8K)
    assert len(filings) == 100
    assert {f.form for f in filings} == {"8-K", "8-K/A"}
    first = filings[0]
    assert first == Filing(accession=TSTA_ATOM_ACCESSION, cik=1405513, form="8-K", items=("1.01",),
                           accepted_at=datetime(2026, 9, 25, 21, 30, 12, tzinfo=UTC),
                           company="United States 12 Month Natural Gas Fund, LP")
    six = parse_current_atom(ATOM_6K)
    assert six and {f.form for f in six} == {"6-K"} and all(f.items == () for f in six)


def test_backfill_reads_current_reports_with_an_acceptance_time():
    doc = submissions(900002, [
        ("0000900002-26-000010", "8-K", "2026-09-25T09:15:00.000Z", "2.02,9.01"),
        ("0000900002-26-000009", "10-Q", "2026-09-25T09:10:00.000Z", ""),
        ("0000900002-26-000008", "8-K", "", "8.01"),
        ("0000900002-26-000007", "6-K", "2026-09-24T09:00:00.000Z", "x"),
    ])
    filings = backfill_filings(doc)
    assert [(f.accession, f.form, f.items) for f in filings] == [
        ("0000900002-26-000010", "8-K", ("2.02", "9.01")), ("0000900002-26-000007", "6-K", ())]
    assert filings[0].accepted_at == datetime(2026, 9, 25, 13, 15, tzinfo=UTC)      # New York wall time


# ------------------------------------------------------------------------------------ fetching


def test_the_atom_is_filtered_by_the_lines_ciks(sleeve_policy):
    result = fetch(sleeve_policy, FakeSec())
    by_line = {tuple(i.symbols): i for i in result.items}
    assert set(by_line) == {("TSTA",), ("TSTD",)}                    # every other filer is ignored
    tsta = by_line[("TSTA",)]
    assert tsta.id == public_news_id("sec", TSTA_ATOM_ACCESSION)
    assert tsta.title == "8-K: Item 1.01 Entry into a Material Definitive Agreement"
    assert tsta.summary == "United States 12 Month Natural Gas Fund, LP"      # the conformed name
    assert (tsta.source, tsta.licence, tsta.form, tsta.items) == ("sec", "public_domain", "8-K", ["1.01"])
    assert tsta.link == "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=TSTA&type=8-K"
    assert tsta.available_at == tsta.published_at == datetime(2026, 9, 25, 21, 30, 12, tzinfo=UTC)
    tstd = by_line[("TSTD",)]
    assert tstd.title == "6-K: report of a foreign private issuer" and tstd.form == "6-K" and tstd.items == []
    assert result.flags == [] and result.report.feeds_ok == 2 + 6


def test_the_backfill_fills_a_gap_and_merges_duplicates(sleeve_policy):
    fake = FakeSec(subs={
        900002: submissions(900002, [
            ("0000900002-26-000010", "8-K", "2026-09-25T09:15:00.000Z", "2.02,9.01"),
            ("0000900002-26-000011", "8-K/A", "2026-09-26T08:00:00.000Z", "5.02"),
            ("0000900002-26-000003", "8-K", "2026-09-20T09:00:00.000Z", "8.01"),      # older than 48 h
        ]),
        900001: submissions(900001, [(TSTA_ATOM_ACCESSION, "8-K", "2026-09-25T17:30:59.000Z", "1.01")]),
    })
    result = fetch(sleeve_policy, fake)
    ids = [i.id for i in result.items]
    assert len(ids) == len(set(ids)) == 4
    tstb = [i for i in result.items if i.symbols == ["TSTB"]]
    assert [(i.form, i.items) for i in tstb] == [("8-K", ["2.02", "9.01"]), ("8-K/A", ["5.02"])]
    assert tstb[1].title.startswith("8-K/A (amendment): Item 5.02")
    assert tstb[1].link.endswith("CIK=TSTB&type=8-K%2FA")
    (tsta,) = [i for i in result.items if i.symbols == ["TSTA"]]
    assert tsta.available_at == datetime(2026, 9, 25, 21, 30, 59, tzinfo=UTC)   # the later of the two times
    assert result.report.dropped == {"stale": 1}


def test_lookahead_only_filings_accepted_before_the_slot(sleeve_policy):
    slot = datetime(2026, 9, 25, 21, 30, 12, tzinfo=UTC)                       # TSTA's acceptance time
    result = fetch(sleeve_policy, FakeSec(), slot=slot)
    assert all(i.available_at < slot for i in result.items)
    assert not [i for i in result.items if i.symbols == ["TSTA"]]
    assert result.report.dropped.get("after_slot", 0) >= 1


def test_sec_calls_go_through_the_limiter_with_the_user_agent(sleeve_policy):
    fake, clock = FakeSec(), FakeClock()
    with fake.client(clock) as client:
        fetch_sec_news(sleeve_policy, now=RECORDED, sec=client)
        assert client.requests == len(fake.seen) == 2 + 6
    assert all(r.headers["User-Agent"] == UA for r in fake.seen)
    assert clock.sleeps and clock.t == pytest.approx((len(fake.seen) - 1) / 7)      # <= 7 a second, no burst
    atom = [r for r in fake.seen if r.url.path == "/cgi-bin/browse-edgar"]
    assert all(r.extensions["timeout"]["connect"] == 5.0 and r.extensions["timeout"]["read"] == 10.0 for r in atom)


def test_the_submissions_backfill_is_cached_for_the_day(sleeve_policy):
    fake = FakeSec()
    fetch(sleeve_policy, fake)
    fetch(sleeve_policy, fake)
    subs = [r for r in fake.seen if r.url.host == "data.sec.gov"]
    assert len(subs) == 6                                   # the second fetch read the 20 h cache
    assert len([r for r in fake.seen if r.url.host == "www.sec.gov"]) == 4


def test_a_missing_user_agent_raises_before_any_request(sleeve_policy, monkeypatch):
    monkeypatch.delenv(SEC_USER_AGENT_ENV, raising=False)
    fake = FakeSec()
    with pytest.raises(MissingCredential):
        fetch_sec_news(sleeve_policy, now=RECORDED, sec_factory=lambda: SecClient(transport=httpx.MockTransport(fake)))
    with pytest.raises(MissingCredential):
        fetch_sec_news(sleeve_policy, now=RECORDED)
    assert fake.seen == []


def test_gathering_without_a_user_agent_flags_sec_and_keeps_the_rest(sleeve_policy, monkeypatch):
    monkeypatch.delenv(SEC_USER_AGENT_ENV, raising=False)
    from tests.data.test_gov_news import LONG, FakeWeb

    news = gather_public_news(sleeve_policy, RECORDED, transport=FakeWeb().transport(), lookback=LONG)
    assert news.flags[0] == "news_source_error:sec:no_user_agent"
    assert "news_source_error:bls:no_user_agent" in news.flags
    assert {i.source for i in news.items} == {"fed_board", "bea", "treasury", "eia"}


def test_the_gatherer_runs_sec_through_the_shared_client(sleeve_policy, monkeypatch):
    monkeypatch.setenv(SEC_USER_AGENT_ENV, UA)
    from tests.data.test_gov_news import FakeWeb

    fake = FakeSec()
    with fake.client() as client:
        news = gather_public_news(sleeve_policy, RECORDED, transport=FakeWeb().transport(), sec=client)
    sec_items = [i for i in news.items if i.source == "sec"]
    assert {tuple(i.symbols) for i in sec_items} == {("TSTA",), ("TSTD",)}
    assert news.sources["sec"].kept == 2 and "sec" in news.sources


def test_a_failing_atom_page_is_flagged_and_the_backfill_still_runs(sleeve_policy):
    fake = FakeSec(subs={900002: submissions(900002, [
        ("0000900002-26-000010", "8-K", "2026-09-25T09:15:00.000Z", "2.02")])})
    fake.status["8-K"] = 503
    result = fetch(sleeve_policy, fake)
    assert result.flags == ["news_source_error:sec:http_503"]
    assert {tuple(i.symbols) for i in result.items} == {("TSTB",), ("TSTD",)}


def test_a_failing_backfill_is_flagged_per_type(sleeve_policy):
    fake = FakeSec()
    fake.status["sub:900003"] = 404
    result = fetch(sleeve_policy, fake)
    assert result.flags == ["news_source_error:sec:backfill_not_found"]
    assert {tuple(i.symbols) for i in result.items} == {("TSTA",), ("TSTD",)}


def test_a_passed_deadline_stops_with_the_budget_flag(sleeve_policy):
    ticks = iter([0.0, 0.0, 100.0] + [100.0] * 20)
    result = fetch(sleeve_policy, FakeSec(), deadline=50.0, monotonic=lambda: next(ticks))
    assert result.flags == ["news_source_error:sec:budget"]


def test_no_stock_lines_means_no_request(policy):
    fake = FakeSec()
    result = fetch(policy, fake)
    assert result.items == [] and fake.seen == []


def test_an_item_that_trips_the_leak_scan_is_dropped(sleeve_policy):
    atom = ATOM_8K_TSTA.replace(b"United States 12 Month Natural Gas Fund, LP (0000900001)",
                                b"Fund 10.20.30.40 LP (0000900001)")
    result = fetch(sleeve_policy, FakeSec(atom={"8-K": atom, "6-K": ATOM_6K_TSTD}))
    assert result.flags == ["news_item_dropped:sec:leak"]
    assert [i.symbols for i in result.items] == [["TSTD"]]


def test_the_sec_source_is_registered_with_its_licence():
    spec = gov_news.SOURCES["sec"]
    assert spec.licence == "public_domain" and spec.feeds == () and spec.contact_ua
    assert spec.key == sec_news.SOURCE
    assert timedelta(hours=48) == gov_news.LOOKBACK


def test_the_backfill_uses_the_news_timeouts_and_one_retry(sleeve_policy):
    fake = FakeSec()
    fake.status["sub:900003"] = 503
    result = fetch(sleeve_policy, fake)
    subs = [r for r in fake.seen if r.url.host == "data.sec.gov"]
    assert all(r.extensions["timeout"]["connect"] == 5.0 and r.extensions["timeout"]["read"] == 10.0 for r in subs)
    assert len([r for r in subs if r.url.path == "/submissions/CIK0000900003.json"]) == 2   # one retry, not three
    assert result.flags == ["news_source_error:sec:backfill_http_503"]


def test_the_backfill_shares_the_clients_submissions_cache(sleeve_policy):
    fake = FakeSec(subs={900002: submissions(900002, [
        ("0000900002-26-000010", "8-K", "2026-09-25T09:15:00.000Z", "2.02")])})
    with fake.client() as client:
        doc = client.submissions(900002)                         # e.g. the earnings estimate, earlier
        before = len(fake.seen)
        assert sec_news.fetch_submissions(client, 900002) == doc
        assert len(fake.seen) == before                          # the same cache entry: no request
        fetch_sec_news(sleeve_policy, now=RECORDED, sec=client)
        again = [r for r in fake.seen[before:] if r.url.path == "/submissions/CIK0000900002.json"]
        assert again == []
        assert client.submissions(900003) == sec_news.fetch_submissions(client, 900003)
    assert len([r for r in fake.seen if r.url.path == "/submissions/CIK0000900003.json"]) == 1


def test_a_bad_cik_is_refused_before_any_request(sleeve_policy):
    fake = FakeSec()
    with fake.client() as client:
        for bad in (0, -1, 10**10):
            with pytest.raises(ValueError):
                sec_news.fetch_submissions(client, bad)
    assert fake.seen == []
