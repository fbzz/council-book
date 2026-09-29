"""The Scout's input after the first paper run (2026-09-28): the movers screen reaches it when the
slot's screen was written in the same cycle (or later, under `--at`); SEC items carry public-domain
filing text (recorded EDGAR fixtures, no network), routine filings rank last, a 6-K without an
English headline is dropped, preferred filers lead; market context comes from the screen's ETF
moves and the policy calendar; ages and admission are relative to the slot, never the wall clock;
a missing AI list is optional."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from council.models.facts import EventItem
from council.stocks.sec import TickerRow
from council.stocks.sec_news import Filing
from council.swing import intake, sec_text
from council.swing import screen as S
from council.swing import sources as ss
from council.swing.council import scout_input
from council.swing.roles import catalyst_index

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "sec" / "filings"
SLOT = datetime(2026, 9, 28, 18, 40, tzinfo=UTC)       # Monday 14:40 New York: last screen = Fri 09-25
WALL = datetime(2026, 9, 28, 22, 30, tzinfo=UTC)       # the replay ran hours later

POLAR = Filing("0001493152-26-044656", 1622345, "8-K", ("1.01", "3.02", "7.01", "9.01"),
               SLOT - timedelta(hours=3), "Polar Power, Inc.")
WALD = Filing("0001840199-26-000108", 1840199, "6-K", (), SLOT - timedelta(hours=2), "Waldencast plc")
WESHOP = Filing("0001493152-26-044622", 2048271, "6-K", (), SLOT - timedelta(hours=4), "WeShop Holdings Ltd")
FOREIGN = Filing("0000000001-26-000001", 1, "6-K", (), SLOT - timedelta(hours=1), "Foreign Co")
VOTE = Filing("0000000002-26-000002", 2, "8-K", ("5.07",), SLOT - timedelta(minutes=30), "Vote Co")
EXHIBITS = Filing("0000000003-26-000003", 3, "8-K/A", ("9.01",), SLOT - timedelta(minutes=20), "Amend Co")
LATE = Filing("0000000004-26-000004", 4, "8-K", ("8.01",), SLOT + timedelta(hours=1), "Late Co")
CVS = Filing("0000000005-26-000005", 5, "8-K", ("7.01",), SLOT - timedelta(hours=6), "CVS Health Corp")
ALL = [POLAR, WALD, WESHOP, FOREIGN, VOTE, EXHIBITS, LATE, CVS]
TICKERS = {1622345: "POLA", 1840199: "WALD", 2048271: "WSHP", 1: "FRGN", 2: "VOTE", 3: "AMND", 4: "LATE",
           5: "CVS"}

_FOREIGN_DIR = "/Archives/edgar/data/1/000000000126000001/"
_FOREIGN_INDEX = (f'<table class="tableFile"><tr><td scope="row">1</td><td scope="row">6-K</td>'
                  f'<td scope="row"><a href="{_FOREIGN_DIR}f6k.htm">f6k.htm</a></td><td scope="row">6-K</td>'
                  '<td scope="row">1</td></tr></table>')
_FOREIGN_DOC = ("<p>FORM 6-K</p><p>Indicate by check mark ... Form 20-F [X] Form 40-F [ ]</p>"
                "<p>公司宣布二零二六年中期业绩及董事会变动公告</p><p>本公司董事会欣然宣布截至六月三十日止六个月之未经审核综合业绩。</p>")
DOCS = {
    "0001493152-26-044656-index.htm": "polar-8k-index.htm", "form8-k.htm": "polar-8k.htm",
    "ex99-1.htm": "polar-ex99-1.htm",
    "0001840199-26-000108-index.htm": "wald-6k-index.htm", "a1wald6-k_earningsreleasex.htm": "wald-6k.htm",
    "a1wald_earningsxex991xq220.htm": "wald-ex99-1.htm",
    "0001493152-26-044622-index.htm": "weshop-6k-index.htm", "form6-k.htm": "weshop-6k.htm",
}


class Recorder:
    """`get(url)` over the recorded EDGAR documents; counts requests."""

    def __init__(self):
        self.urls: list[str] = []

    def __call__(self, url: str) -> str:
        assert url.startswith("https://www.sec.gov/Archives/edgar/data/"), url
        self.urls.append(url)
        name = url.rsplit("/", 1)[-1]
        if _FOREIGN_DIR in url or name == "0000000001-26-000001-index.htm":
            return _FOREIGN_INDEX if name.endswith("-index.htm") else _FOREIGN_DOC
        if name in DOCS:
            return (FIX / DOCS[name]).read_text()
        raise FileNotFoundError(name)            # the synthetic filers have no recorded documents


# --------------------------------------------------------------------------- filing text
def test_filing_text_reads_the_8k_item_and_the_press_release_headline():
    text = sec_text.filing_text(POLAR, Recorder())
    assert text.headline.startswith("Polar Power CEO Arthur Sams Converts") and text.exhibit == "EX-99.1"
    assert text.excerpt.startswith("GARDENA, Calif.") and len(text.excerpt) <= sec_text.EXCERPT_CHARS + 3
    assert text.item_text.startswith("On September 25, 2026, Polar Power") and text.english


def test_filing_text_of_6ks_skips_the_cover_and_detects_english():
    wald = sec_text.filing_text(WALD, Recorder())
    assert wald.exhibit == "EX-99.1" and "First Half 2026 Financial Results" in wald.headline and wald.english
    weshop = sec_text.filing_text(WESHOP, Recorder())     # no EX-99: the report's own text after its cover
    assert weshop.exhibit == "" and "Purchase Agreement" in weshop.excerpt and weshop.english
    assert "FOREIGN PRIVATE ISSUER" not in weshop.headline.upper()
    foreign = sec_text.filing_text(FOREIGN, Recorder())
    assert foreign.headline and not foreign.english


def test_enricher_is_cached_and_bounded(tmp_path):
    rec = Recorder()
    flags: list[str] = []
    enrich = sec_text.Enricher(rec, cache_root=tmp_path, max_fetches=1, flags=flags)
    assert enrich(POLAR) is not None and len(rec.urls) <= sec_text.MAX_REQUESTS_PER_FILING
    assert enrich(WALD) is None                               # the slot's budget is spent
    again = sec_text.Enricher(rec, cache_root=tmp_path, max_fetches=0)
    n = len(rec.urls)
    assert again(POLAR).headline.startswith("Polar Power")    # served from the cache, no request
    assert len(rec.urls) == n
    broken = sec_text.Enricher(rec, cache_root=tmp_path, max_fetches=5, flags=flags)
    assert broken(VOTE) is None and flags == ["swing_sec_text_error:FileNotFoundError"]


def test_market_wide_items_carry_content_rank_routine_last_and_drop_non_english_6ks(tmp_path):
    enrich = sec_text.Enricher(Recorder(), cache_root=tmp_path, max_fetches=10)
    result, admitted = intake.sec_market_wide(None, TICKERS, now=WALL, slot=SLOT, fetch=lambda _c, _f: ALL,
                                              enrich=enrich, prefer={"CVS": 0})
    by = {i.symbols[0]: i for i in result.items}
    assert "LATE" not in by and LATE not in admitted            # accepted after the slot: never read
    assert "FRGN" not in by and result.report.dropped["sec_6k_no_headline"] == 1
    assert [i.symbols[0] for i in result.items][0] == "CVS"      # a movers name leads
    assert [i.symbols[0] for i in result.items][-2:] in (["VOTE", "AMND"], ["AMND", "VOTE"])   # routine last
    polar = by["POLA"]
    assert polar.title.startswith("8-K: Item 1.01")              # the official title is unchanged
    assert polar.id.startswith("P:") and polar.licence == "public_domain"
    assert "EX-99.1 headline: Polar Power CEO" in polar.summary
    assert "item text: On 25 September, Polar Power" in polar.summary       # dates read day first
    assert "614,700" not in polar.summary                        # money is redacted by the cleaner
    assert "First Half 2026" in by["WALD"].summary
    assert by["CVS"].summary == "CVS Health Corp"                # no recorded text: header only


# --------------------------------------------------------------------------- the real sources
class FakeSec:
    def company_tickers(self):
        return [TickerRow(cik=c, ticker=t, title=t) for c, t in TICKERS.items()]

    def submissions(self, cik):
        return {"sic": "5912", "filings": {"recent": {}}}

    def companyfacts(self, cik):
        return None


def _screen(tmp_path, built_at):
    scr = S.Screen(session="2026-09-25", built_at=built_at.isoformat())
    scr.lists["movers"] = [{"id": "M:CVS:movers", "line_id": "CVS", "move_pct": 4.8, "move_sigma": 3.48,
                            "vol_ratio": 2.74, "sector": "Shops"}]
    scr.context = {"SPY": {"move_pct": 0.62, "move_sigma": 0.71}, "VIXY": {"move_pct": -3.1, "move_sigma": -1.2},
                   "XLV": {"move_pct": 1.9, "move_sigma": 2.3}}
    S.save(scr, tmp_path)


def _real(policy, tmp_path, rec):
    return ss.real_swing_sources(policy, state_dir=tmp_path, keys_loader=lambda: object(),
                                 sec_user_agent=lambda: "Council Test ops@example.org", sec_factory=FakeSec,
                                 fetch_bars=lambda *a, **k: {}, fetch_short_interest=lambda *a, **k: {},
                                 wall=lambda: WALL, sec_text_get=rec, sec_fetch=lambda _c, _f: ALL)


@pytest.mark.parametrize("built_at", [SLOT, WALL])
def test_friday_screen_written_at_or_after_a_monday_slot_reaches_the_scout(policy, tmp_path, built_at):
    """Regression: the cycle writes the screen at its own clock (>= the slot), so `built_at < slot`
    hid every screen built in the same cycle; availability is D 20:30 New York."""
    _screen(tmp_path, built_at)
    src = _real(policy, tmp_path, Recorder())
    assert src.prepare(SLOT, SLOT) == []                          # cached: no rebuild
    inputs = src.inputs(SLOT, [], [])
    assert inputs.screen_available_at == S.ready_at(datetime(2026, 9, 25).date()) < SLOT
    cats = catalyst_index(inputs.reading, inputs.screen_rows, slot=SLOT,
                          screen_available_at=inputs.screen_available_at)
    text = scout_input(inputs, cats)[0][0].items[0].text
    movers = text.split("MOVERS SCREEN")[1].split("MARKET CONTEXT")[0]
    assert "- M:CVS:movers: move 3.48 sigma (4.8%)" in movers
    market = text.split("MARKET CONTEXT")[1].split("OPEN SWING TRADES")[0]
    assert "- F:SPY:ret1d_sigma: SPY (S&P 500 ETF) moved +0.62%" in market
    assert "F:VIXY:ret1d_sigma" in market and "health care sector ETF" in market
    assert "- E:fomc@2026-10-28: FOMC decision scheduled 2026-10-28 18:00 UTC (30 days after the slot)" in market
    reading = text.split("READING LIST")[1].split("MOVERS SCREEN")[0]
    assert "EX-99.1 headline: Polar Power CEO" in reading
    assert "[POLA] 8-K items 1.01, 3.02, 7.01, 9.01 3h ago" in reading   # age from the slot, not the wall
    assert "[WALD] 6-K 2h ago" in reading and "LATE" not in reading and "FRGN" not in reading
    assert "swing_screen_missing" not in src.flags


def test_a_screen_of_another_session_or_not_ready_is_missing(policy, tmp_path):
    scr = S.Screen(session="2026-09-24", built_at=SLOT.isoformat())
    S.save(scr, tmp_path)
    src = _real(policy, tmp_path, Recorder())
    inputs = src.inputs(SLOT, [], [])
    assert inputs.screen_rows == [] and inputs.screen_available_at is None
    assert "swing_screen_missing" in src.drain()


def test_event_rows_are_future_macro_events_within_the_horizon():
    ev = [EventItem(id="E:cpi@2026-10-14", kind="cpi", at_utc=datetime(2026, 10, 14, 12, 30, tzinfo=UTC),
                    severity=3, source="fred"),
          EventItem(id="E:fomc@2026-09-16", kind="fomc", at_utc=datetime(2026, 9, 16, 18, tzinfo=UTC),
                    severity=3, source="policy_calendar"),
          EventItem(id="E:earnings:AAPL@2026-10-01", kind="earnings", symbols=["AAPL"],
                    at_utc=datetime(2026, 10, 1, 20, tzinfo=UTC), severity=2, source="sec")]
    rows = ss.event_rows(ev, SLOT)
    assert [r.id for r in rows] == ["E:cpi@2026-10-14"] and "CPI release scheduled 2026-10-14" in rows[0].text


def test_missing_ai_list_is_optional(policy, tmp_path, monkeypatch):
    from council.stocks import universe as U

    def absent(*_a, **_k):
        raise FileNotFoundError("policy/stock-universe-extra.yaml")

    monkeypatch.setattr(U, "load_ai_list", absent)
    monkeypatch.setattr(U, "fetch_membership", lambda index, **_k: type("M", (), {"symbols": ("CVS",)})())
    src = _real(policy, tmp_path, Recorder())
    state = src.gate.__self__
    names = state.screen_universe(SLOT)
    assert [n.line_id for n in names] == ["CVS"]
    flags = src.drain()
    assert "swing_ai_list_absent" in flags and not [f for f in flags if f.startswith("swing_source_error")]


def test_reviewers_read_the_same_filing_text_as_the_scout_and_unscreened_names_are_marked(policy, tmp_path):
    """2026-09-28 fact parity: the Scout read the EX-99 headline and excerpt, the Skeptic saw only
    "6-K: report of a foreign private issuer" and so found nothing confirming the claim. Every
    reviewer (Skeptic, debate, PM) now gets the summary for each cited P: id. The Scout's reading
    list marks names outside the screen universe, whose absence from the screen means nothing."""
    from council.swing.council import full_input, skeptic_input
    from council.swing.models import ScoutIdea
    from council.swing.roles import SwingIdea

    _screen(tmp_path, SLOT)
    (tmp_path / "swing" / "universe.json").write_text('{"asof": "2026-09-28T00:00:00+00:00", "tickers": ["CVS"]}')
    src = _real(policy, tmp_path, Recorder())
    inputs = src.inputs(SLOT, [], [])
    assert inputs.screened == frozenset({"CVS"})
    cats = catalyst_index(inputs.reading, inputs.screen_rows, slot=SLOT,
                          screen_available_at=inputs.screen_available_at)
    reading = scout_input(inputs, cats)[0][0].items[0].text.split("READING LIST")[1].split("MOVERS SCREEN")[0]
    assert "[POLA] [not screened: POLA] 8-K items" in reading
    cvs_line = next(line for line in reading.splitlines() if "[CVS" in line)
    assert "not screened" not in cvs_line
    polar = next(i for i in inputs.reading if i.symbols == ["POLA"])
    assert cats[polar.id].summary == polar.summary and "EX-99.1 headline: Polar Power CEO" in polar.summary
    idea = SwingIdea(ref="idea:1", line_id="POLA", idea=ScoutIdea.model_validate({
        "ticker": "POLA", "side": "long", "setup": "news_continuation", "catalyst_ids": [polar.id],
        "catalyst_claim": "8-K item 1.01 agreement", "thesis": "t", "why_not_priced_in": "w", "entry": "now",
        "stop_pct": 0.06, "target_pct": 0.12, "time_stop_days": 10, "invalidation": "i"}))
    sk = skeptic_input(idea, catalysts=cats, inputs=inputs)[0][0].items[0].text
    assert f"- {polar.id}: 8-K: Item 1.01" in sk and "EX-99.1 headline: Polar Power CEO" in sk
    full = full_input([idea], [], catalysts=cats, inputs=inputs)[0][0].items[0].text
    assert "EX-99.1 headline: Polar Power CEO" in full
    # a movers-screen id or a feed id carries no summary
    assert all(m.summary == "" for k, m in cats.items() if not k.startswith("P:"))
