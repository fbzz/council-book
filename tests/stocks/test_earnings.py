"""WP-H, the data half: the stock lines' earnings events (design §10, D12). The next report is
estimated from a synthetic SEC 8-K Item 2.02 history (the release a year earlier + 364 days, a
91-day cadence without one, the 10-Q/10-K cadence for a filer that never files Item 2.02); a new
release confirms the report; the broker feed's date overrides the estimate; only filings accepted
by the slot are visible. On the sleeve fixture policy; no network, no Keychain (a fake EDGAR)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from council.data.credentials import MissingCredential
from council.data.http import DataError
from council.models.facts import NewsItem
from council.policy import Policy
from council.risk.churn import event_block, event_window
from council.stocks import earnings as EA
from council.stocks.sec import SecClient
from tests.conftest import make_sleeve_policy_dir
from tests.stocks.conftest import UA, FakeClock, FakeEdgar

NY = ZoneInfo("America/New_York")
CIK = {"TSTA": 900001, "TSTB": 900002, "TSTC_B": 900003, "F": 900004, "TSTD": 900005, "TSTE": 900006}
# TSTA reports after the close on a Thursday each quarter; its 10-Q follows a few days later.
TSTA_RELEASES = ("2025-07-31T16:05:12", "2025-10-30T16:05:40", "2026-01-29T16:06:01", "2026-04-30T16:05:09",
                 "2026-07-30T16:05:30")
Q3_RELEASE = "2026-10-29T16:05:20"            # the report the estimate is about


def ny(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).replace(tzinfo=NY).astimezone(UTC)


def utc(y, m, d, h=0, mi=0, s=0) -> datetime:
    return datetime(y, m, d, h, mi, s, tzinfo=UTC)


def submissions(rows: list[tuple[str, str, str]]) -> dict:
    """A trimmed submissions document from (form, items, acceptance as New York wall time) rows,
    newest first as SEC lists them; SEC writes the stamp with a Z suffix."""
    rows = sorted(rows, key=lambda r: r[2], reverse=True)
    return {"cik": "1", "name": "Co", "filings": {"recent": {
        "accessionNumber": [f"0000000000-26-{i:06d}" for i in range(len(rows))],
        "filingDate": [r[2][:10] for r in rows], "reportDate": [r[2][:10] for r in rows],
        "acceptanceDateTime": [f"{r[2]}.000Z" for r in rows],
        "form": [r[0] for r in rows], "items": [r[1] for r in rows]}}}


def releases(*stamps: str, tenq_lag_days: int = 5) -> list[tuple[str, str, str]]:
    out = []
    for stamp in stamps:
        out.append(("8-K", "2.02,9.01", stamp))
        tenq = (datetime.fromisoformat(stamp) + timedelta(days=tenq_lag_days)).replace(hour=17).isoformat()
        out.append(("10-Q", "", tenq))
    return out


TSTA_DOC = submissions([*releases(*TSTA_RELEASES, Q3_RELEASE),
                        ("8-K", "5.02", "2026-09-15T08:00:00"),          # not results: ignored
                        ("8-K/A", "2.02", "2026-08-10T09:00:00"),        # an amendment: ignored
                        ("4", "", "2026-09-01T18:00:00")])


class FakeSec:
    """`submissions(cik)` from canned documents; a CIK in `broken` raises DataError."""

    def __init__(self, docs: dict[int, dict], broken: tuple[int, ...] = ()) -> None:
        self.docs, self.broken = docs, set(broken)
        self.calls: list[int] = []
        self.closed = False

    def submissions(self, cik: int) -> dict:
        self.calls.append(cik)
        if cik in self.broken:
            raise DataError("sec submissions: HTTP 503")
        return self.docs.get(cik, submissions([]))

    def close(self) -> None:
        self.closed = True


def by_line(events) -> dict[str, list]:
    out: dict[str, list] = {}
    for e in events:
        out.setdefault(e.symbols[0], []).append(e)
    return out


def news_item(symbols, earnings_date: str, available: datetime, *, before_open=None, key="n1") -> NewsItem:
    import hashlib

    return NewsItem(id="N:" + hashlib.sha256(key.encode()).hexdigest()[:8], title="Earnings date",
                    symbols=list(symbols), published_at=available, available_at=available,
                    earnings_date=datetime.fromisoformat(earnings_date.replace("Z", "+00:00")),
                    before_market_open=before_open)


# ------------------------------------------------------------------------------------ filings


def test_filing_history_keeps_item_202_8ks_and_periodic_reports_in_new_york_time():
    history = EA.filing_history(TSTA_DOC)
    assert history.releases == tuple(ny(s) for s in (*TSTA_RELEASES, Q3_RELEASE))
    assert history.releases[-1] == utc(2026, 10, 29, 20, 5, 20)          # 16:05 New York (EDT), not 16:05Z
    assert len(history.periodic) == 6                                    # 10-Qs only; 8-K/A, 5.02 and Form 4 left out
    assert all(p.astimezone(NY).hour == 17 for p in history.periodic)


# ------------------------------------------------------------------------------------ estimate


def test_estimate_is_the_release_a_year_earlier_plus_364_days_same_weekday():
    past = [ny(s) for s in TSTA_RELEASES]
    at, basis = EA.estimate_next(past)
    assert basis == ny("2025-10-30T16:05:40")
    assert at == ny("2026-10-29T16:05:40")                               # EDT to EDT: same wall time
    assert at.astimezone(NY).weekday() == basis.astimezone(NY).weekday() == 3


def test_a_short_history_or_a_missing_quarter_falls_back_to_a_91_day_cadence():
    short = [ny("2026-04-30T16:05:00"), ny("2026-07-30T16:05:00")]
    assert EA.estimate_next(short) == (ny("2026-10-29T16:05:00"), short[-1])
    gap = [ny(s) for s in ("2025-07-31T16:05:00", "2026-01-29T16:05:00", "2026-04-30T16:05:00",
                           "2026-07-30T16:05:00")]                        # the October 2025 report is missing
    assert EA.estimate_next(gap) == (ny("2026-10-29T16:05:00"), gap[-1])
    assert EA.estimate_next([]) is None


def test_an_early_report_ends_the_quarter_and_the_estimate_moves_to_the_next_one():
    past = [*(ny(s) for s in TSTA_RELEASES), ny("2026-10-15T16:05:00")]   # two weeks before the estimate
    at, basis = EA.estimate_next(past)
    assert basis == ny("2026-01-29T16:06:01") and at.date().isoformat() == "2027-01-28"


def test_a_filer_without_item_202_uses_its_10q_10k_cadence():
    doc = submissions([("10-Q", "", "2025-11-04T16:30:00"), ("10-K", "", "2026-02-10T16:30:00"),
                       ("10-Q", "", "2026-05-05T16:30:00"), ("10-Q", "", "2026-08-04T16:30:00")])
    history = EA.filing_history(doc)
    visible, source = history.visible(utc(2026, 10, 26, 14, 40))
    assert source == EA.SOURCE_PERIODIC and len(visible) == 4
    assert EA.estimate_next(visible)[0] == ny("2026-11-03T16:30:00")      # 2025-11-04 + 364 days


# ------------------------------------------------------------------------------------ line events


@pytest.fixture(scope="module")
def pol(tmp_path_factory) -> Policy:
    return Policy.load(make_sleeve_policy_dir(tmp_path_factory.mktemp("pol_earnings")))


def test_before_the_report_the_estimate_window_blocks_adds_on_that_stock_only(pol):
    history = EA.filing_history(TSTA_DOC)
    slot = utc(2026, 10, 26, 14, 40)                                     # Monday before the Thursday report
    events, flags = EA.line_events("TSTA", history, cutoff=slot, policy=pol)
    assert flags == []
    occurred, estimate = events
    assert (occurred.source, occurred.at_utc, occurred.known_at) == ("sec_8k", ny("2026-07-30T16:05:30"),
                                                                      ny("2026-07-30T16:05:30"))
    assert estimate.id == "E:earnings:TSTA@2026-10-29" and estimate.source == "sec_estimate"
    assert estimate.known_at == ny("2025-10-30T16:05:40") and estimate.symbols == ["TSTA"]
    start, end = event_window(estimate, pol)
    assert start == datetime(2026, 10, 21, 0, 0, tzinfo=NY)              # 5 trading days before, less 24 h
    assert end == datetime(2026, 11, 6, 0, 0, tzinfo=NY) + timedelta(hours=30)
    lines = pol.universe.by_symbol()
    assert event_block(lines["TSTA"], events, slot, pol)
    assert not event_block(lines["TSTB"], events, slot, pol)
    assert not event_block(lines["NDX"], events, slot, pol)


def test_a_release_after_the_slot_is_invisible_then_confirms_the_report_with_a_30h_window(pol):
    history = EA.filing_history(TSTA_DOC)
    before = utc(2026, 10, 29, 18, 40)                                   # the 8-K is accepted at 20:05 UTC
    events, _ = EA.line_events("TSTA", history, cutoff=before, policy=pol)
    assert [e.source for e in events] == ["sec_8k", "sec_estimate"]
    assert events[0].at_utc < utc(2026, 8, 1)                            # still July's release
    after = utc(2026, 10, 29, 22, 40)
    events, flags = EA.line_events("TSTA", history, cutoff=after, policy=pol)
    assert flags == [] and len(events) == 2                              # occurred + next quarter's estimate
    occurred = events[0]
    assert (occurred.source, occurred.at_utc) == ("sec_8k", utc(2026, 10, 29, 20, 5, 20))
    assert events[1].at_utc.date().isoformat() == "2027-01-28"
    tsta = pol.universe.by_symbol()["TSTA"]
    at = occurred.at_utc
    assert event_block(tsta, [occurred], at + timedelta(hours=29, minutes=59), pol)
    assert not event_block(tsta, [occurred], at + timedelta(hours=30, minutes=1), pol)


def test_the_feed_date_overrides_the_estimate(pol):
    history = EA.filing_history(TSTA_DOC)
    slot = utc(2026, 10, 26, 14, 40)
    item = news_item(["TSTA"], "2026-11-03T21:00:00Z", utc(2026, 10, 20, 12))
    feed = EA.feed_dates(pol, [item], slot)["TSTA"]
    events, flags = EA.line_events("TSTA", history, cutoff=slot, policy=pol, feed=feed)
    confirmed = events[-1]
    assert flags == [] and [e.source for e in events] == ["sec_8k", "etoro_feed"]
    assert confirmed.at_utc == utc(2026, 11, 3, 21) and confirmed.known_at == utc(2026, 10, 20, 12)
    tsta = pol.universe.by_symbol()["TSTA"]
    assert not event_block(tsta, events, slot, pol)                      # the estimate's window is gone
    assert event_block(tsta, events, utc(2026, 11, 3, 14, 40), pol)      # 24 h before the confirmed date


def test_feed_items_are_mapped_by_vehicle_dated_and_filtered(pol):
    slot = utc(2026, 10, 26, 14, 40)
    items = [
        news_item(["TSTC.B", "SPX500"], "2026-11-03T00:00:00Z", utc(2026, 10, 20), before_open=True, key="a"),
        news_item(["TSTA", "TSTB"], "2026-11-04T21:00:00Z", utc(2026, 10, 20), key="b"),    # two stocks
        news_item(["F"], "2026-11-05T21:00:00Z", slot, key="c"),                            # not before the slot
        news_item(["F"], "2026-11-06T00:00:00Z", utc(2026, 10, 21), key="d"),
        news_item(["F"], "2026-11-09T00:00:00Z", utc(2026, 10, 22), key="e"),               # the newest wins
    ]
    dates = EA.feed_dates(pol, items, slot)
    assert set(dates) == {"TSTC_B", "F"}
    assert dates["TSTC_B"].at == datetime(2026, 11, 3, 8, 0, tzinfo=NY)  # date-only, before the open
    assert dates["F"].at == datetime(2026, 11, 9, 16, 0, tzinfo=NY)      # date-only, after the close


def test_a_feed_date_at_new_york_midnight_is_a_date_not_a_report_time():
    midnight_ny = datetime(2026, 11, 5, 0, 0, tzinfo=NY)                 # the date written in exchange time
    assert EA.feed_report_time(midnight_ny, None) == datetime(2026, 11, 5, 16, 0, tzinfo=NY)
    assert EA.feed_report_time(midnight_ny, True) == datetime(2026, 11, 5, 8, 0, tzinfo=NY)
    assert EA.feed_report_time(utc(2026, 11, 5), None) == datetime(2026, 11, 5, 16, 0, tzinfo=NY)
    timed = utc(2026, 11, 5, 21, 5)
    assert EA.feed_report_time(timed, True) == timed                     # a time of day is kept


def test_a_stale_feed_date_for_the_last_report_does_not_replace_the_estimate(pol):
    history = EA.filing_history(TSTA_DOC)
    slot = utc(2026, 10, 26, 14, 40)
    stale = EA.FeedDate(at=ny("2026-07-30T16:05:00"), known_at=utc(2026, 7, 20))
    events, _ = EA.line_events("TSTA", history, cutoff=slot, policy=pol, feed=stale)
    assert [e.source for e in events] == ["sec_8k", "sec_estimate"]


def test_overdue_and_unknown_are_flagged_without_an_event(pol):
    history = EA.filing_history(submissions(releases(*TSTA_RELEASES)))
    events, flags = EA.line_events("TSTA", history, cutoff=utc(2026, 11, 20, 14, 40), policy=pol)
    assert flags == ["earnings_overdue:TSTA"] and [e.source for e in events] == ["sec_8k"]
    events, flags = EA.line_events("TSTB", EA.filing_history(submissions([])), cutoff=utc(2026, 11, 20), policy=pol)
    assert (events, flags) == ([], ["earnings_unknown:TSTB"])
    assert EA.line_events("TSTB", None, cutoff=utc(2026, 11, 20), policy=pol) == ([], [])   # not fetched


# ------------------------------------------------------------------------------------ gather


def test_gather_covers_selected_then_retiring_then_shortlist_up_to_the_cap(pol, monkeypatch):
    assert [ln.symbol for ln in EA.covered_lines(pol)[0]] == ["TSTA", "TSTB", "TSTC_B", "F", "TSTD", "TSTE"]
    monkeypatch.setattr(EA, "MAX_LINES", 3)
    sec = FakeSec({CIK["TSTA"]: TSTA_DOC})
    slot = utc(2026, 10, 26, 14, 40)
    events, flags = EA.gather_earnings(pol, slot=slot, start=slot - timedelta(hours=24),
                                       end=slot + timedelta(days=7), sec=sec)
    assert sec.calls == [CIK["TSTA"], CIK["TSTB"], CIK["TSTC_B"]] and not sec.closed   # the caller's client
    assert {"earnings_skipped:F", "earnings_skipped:TSTD", "earnings_skipped:TSTE"} <= set(flags)
    assert {"earnings_unknown:TSTB", "earnings_unknown:TSTC_B"} <= set(flags)
    assert [(e.symbols, e.source) for e in events] == [(["TSTA"], "sec_estimate")]    # July's release has expired


def test_gather_orders_a_retiring_line_before_the_shortlist(tmp_path):
    import shutil

    import yaml

    from tests.conftest import SLEEVE_FIXTURE

    overlay = tmp_path / "overlay"
    overlay.mkdir()
    for name in ("universe.yaml", "stock-rank.yaml"):
        shutil.copyfile(SLEEVE_FIXTURE / name, overlay / name)
    sleeve = yaml.safe_load((SLEEVE_FIXTURE / "stock-sleeve.yaml").read_text())
    for line in sleeve["lines"]:
        if line["symbol"] == "TSTE":
            line["role"] = "retiring"
    (overlay / "stock-sleeve.yaml").write_text(yaml.safe_dump(sleeve, sort_keys=False))
    pol = Policy.load(make_sleeve_policy_dir(tmp_path / "pol", overlay=overlay))
    assert [ln.symbol for ln in EA.covered_lines(pol)[0]] == ["TSTA", "TSTB", "TSTC_B", "F", "TSTE", "TSTD"]


def test_gather_never_raises_for_data_problems(pol):
    slot = utc(2026, 10, 26, 14, 40)
    sec = FakeSec({CIK["TSTA"]: TSTA_DOC}, broken=(CIK["TSTB"],))
    events, flags = EA.gather_earnings(pol, slot=slot, sec=sec)
    assert "earnings_failed:TSTB" in flags and "earnings_unknown:TSTB" not in flags
    assert [e.symbols for e in events] == [["TSTA"]]                      # the estimate window contains the slot

    def no_agent():
        raise MissingCredential("SEC user agent missing")

    item = news_item(["TSTB"], "2026-10-27T12:00:00Z", utc(2026, 10, 20))
    events, flags = EA.gather_earnings(pol, slot=slot, start=slot, end=slot + timedelta(days=7), sec_factory=no_agent,
                                       news=[item])
    assert flags == ["earnings_unavailable:no_sec_user_agent"]           # once, not per line
    assert [(e.symbols, e.source) for e in events] == [(["TSTB"], "etoro_feed")]   # the feed still applies

    ticks = iter(range(0, 1000, 40))
    events, flags = EA.gather_earnings(pol, slot=slot, sec=FakeSec({}), time_budget_s=100,
                                       monotonic=lambda: float(next(ticks)))
    assert [f for f in flags if f.startswith("earnings_time_budget:")] == [
        "earnings_time_budget:TSTC_B", "earnings_time_budget:F", "earnings_time_budget:TSTD",
        "earnings_time_budget:TSTE"]


def test_gather_through_the_sec_client_uses_its_cache_and_never_leaks_the_user_agent(pol, tmp_path):
    edgar = FakeEdgar(submissions={CIK["TSTA"]: TSTA_DOC})
    made: list[SecClient] = []

    clock = FakeClock()

    def factory() -> SecClient:
        client = SecClient(user_agent=UA, transport=edgar.transport(), cache_root=tmp_path / "cache",
                           clock=clock, sleep=clock.sleep)
        made.append(client)
        return client

    slot = utc(2026, 10, 26, 14, 40)
    events, flags = EA.gather_earnings(pol, slot=slot, sec_factory=factory)
    assert [e.id for e in events] == ["E:earnings:TSTA@2026-10-29"]
    assert len(made) == 1 and edgar.paths().count("/submissions/CIK0000900001.json") == 1
    assert UA not in json.dumps(flags) and UA not in repr(made[0])
    EA.gather_earnings(pol, slot=slot + timedelta(hours=4), sec_factory=factory)
    assert edgar.paths().count("/submissions/CIK0000900001.json") == 1   # the 20 h cache answered
    assert all(r.headers["User-Agent"] == UA for r in edgar.seen)


def test_a_core_only_policy_has_no_earnings(policy):
    def boom():
        raise AssertionError("no SEC client for a core-only policy")

    assert EA.gather_earnings(policy, slot=utc(2026, 10, 26, 14, 40), sec_factory=boom) == ([], [])
