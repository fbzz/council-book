"""RSS headlines in the swing Scout's reading list and their data rights (user decision 2026-09-29).

- the real `SwingSources`: the shared news fetch's RSS market items + the per-ticker feed for the
  open trades, carried ideas and movers-screen names (once per slot), ranked next to the SEC items,
  title + description, `via <feed>`; a failing per-ticker feed is a flag;
- the Scout section is licensed (held apart by the capture, purged by `council purge-licensed`);
- public record: an RSS catalyst is its id and feed label only; the core evidence table labels it
  `rss`; the leak scan catches the headline text anywhere in public output.
Offline: recorded fixtures only.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta

import pytest

from council.data import rss_news
from council.deliberation.capture import (
    InputSink,
    calls_path,
    licensed_path,
    load_licensed,
    write_inputs,
)
from council.models.facts import NewsItem
from council.operator.purge import purge_licensed
from council.publish import leakscan, redact
from council.publish.public_models import PublicFact, PublicSwingCatalyst
from council.swing import intake
from council.swing import screen as S
from council.swing import sources as ss
from council.swing.council import OpenTrade, scout_input
from council.swing.record import _catalyst
from council.swing.roles import catalyst_index
from tests.data.test_rss_news import fixture_get
from tests.swing.test_scout_inputs import FakeSec, Recorder

SLOT = datetime(2026, 9, 29, 20, 40, tzinfo=UTC)          # Tuesday 16:40 New York: last screen = Mon 09-28
WALL = SLOT + timedelta(minutes=5)


class Ledger:
    def __init__(self):
        self.pending = [{"ticker": "OGE", "side": "long"}]

    def swing_ideas(self, status=None):
        return self.pending if status == "pending" else []

    def paper_trades(self):
        return []


def Trade():
    return OpenTrade(ref="trade:1", ticker="WCC", side="long", days_held=2)


def _screen(tmp_path):
    scr = S.Screen(session="2026-09-28", built_at=SLOT.isoformat())
    scr.lists["movers"] = [{"id": "M:NVDA:movers", "line_id": "NVDA", "move_pct": 4.1, "move_sigma": 3.1,
                            "vol_ratio": 2.2, "sector": "Chips"}]
    S.save(scr, tmp_path)


@pytest.fixture()
def run(policy, tmp_path):
    cfg = rss_news.rss_config(policy)
    market = rss_news.fetch_market(cfg, now=WALL, slot=SLOT, get=fixture_get)
    calls: list[list[str]] = []

    def tickers(names, now, slot):
        calls.append(list(names))
        return rss_news.fetch_tickers(cfg, names, now=now, slot=slot, get=fixture_get)

    _screen(tmp_path)
    src = ss.real_swing_sources(policy, state_dir=tmp_path, keys_loader=lambda: object(),
                                sec_user_agent=lambda: "Council Test ops@example.org", sec_factory=FakeSec,
                                fetch_bars=lambda *a, **k: {}, fetch_short_interest=lambda *a, **k: {},
                                wall=lambda: WALL, sec_text_get=Recorder(), sec_fetch=lambda _c, _f: [],
                                news=lambda slot: market, ledger=Ledger(), rss_tickers=tickers)
    inputs = src.inputs(SLOT, [Trade()], [])
    cats = catalyst_index(inputs.reading, inputs.screen_rows, slot=SLOT,
                          screen_available_at=inputs.screen_available_at)
    return src, inputs, cats, calls


def test_rss_items_reach_the_scout_ranked_and_capped(run, policy):
    src, inputs, cats, calls = run
    assert calls == [["WCC", "OGE", "NVDA"]]                            # open trade, carried idea, mover
    rss = [i for i in inputs.reading if i.source == "rss"]
    cfg = rss_news.rss_config(policy)
    assert rss and len(rss) <= cfg.scout_max
    assert all(sum(i.feed == f.label for i in rss) <= cfg.scout_per_feed for f in cfg.feeds)
    assert all(i.available_at < SLOT for i in rss)
    by_feed = {i.feed for i in rss}
    assert {"yahoo_ticker", "prnewswire_all", "nasdaq_markets"} <= by_feed
    assert not any(f.startswith("news_source_error") for f in src.flags)
    text = scout_input(inputs, cats)[0][0].items[0].text
    reading = text.split("READING LIST")[1].split("MOVERS SCREEN")[0]
    assert "via prnewswire_all: Sample prnewswire_all headline 1: company update (NYSE:WCC) | CITY" in reading
    # one recorded Yahoo document serves every ticker here, so its items carry all three names
    assert "[NVDA, OGE, WCC] 31h ago via yahoo_ticker: Sample yahoo_ticker headline 3" in reading


def test_rss_lines_are_licensed_in_the_capture(run):
    _src, inputs, cats, _ = run
    [section] = scout_input(inputs, cats)[0]
    assert section.items[0].licence == "broker_licensed" and section.licensed_indices() == [0]


def test_a_failing_per_ticker_feed_is_a_flag(policy, tmp_path):
    def broken(names, now, slot):
        raise RuntimeError("down")

    _screen(tmp_path)
    src = ss.real_swing_sources(policy, state_dir=tmp_path, keys_loader=lambda: object(),
                                sec_user_agent=lambda: "Council Test ops@example.org", sec_factory=FakeSec,
                                fetch_bars=lambda *a, **k: {}, fetch_short_interest=lambda *a, **k: {},
                                wall=lambda: WALL, sec_text_get=Recorder(), sec_fetch=lambda _c, _f: [],
                                news=lambda slot: None, rss_tickers=broken)
    src.inputs(SLOT, [Trade()], [])
    assert "news_source_error:rss:RuntimeError" in src.flags


def test_reading_list_keeps_rss_next_to_sec_without_crowding_it_out():
    at = SLOT - timedelta(hours=1)
    sec = [NewsItem(id=f"P:{n:08x}", title=f"8-K {n}", published_at=at, available_at=at, source="sec")
           for n in range(30)]
    rss = [NewsItem(id=f"N:{n:08x}", title=f"rss {n}", published_at=at, available_at=at, source="rss",
                    licence="third_party_licensed", feed="cnbc_top") for n in range(50)]
    out = intake.reading_list(sec, [], [], slot=SLOT, rss=rss)
    assert sum(i.source == "sec" for i in out) == intake.SEC_QUOTA
    assert sum(i.source == "rss" for i in out) == intake.RSS_QUOTA
    assert intake.feed_pass_through(rss, slot=SLOT) == []                # RSS is not the broker feed


# ------------------------------------------------------------------------------ data rights
def test_an_rss_catalyst_publishes_its_id_and_feed_label_only(run):
    _src, inputs, cats, _ = run
    item = next(i for i in inputs.reading if i.source == "rss")
    row = _catalyst(item.id, cats[item.id], {})
    assert row == {"id": item.id, "source": item.feed}
    chip = redact._swing_catalyst(row, None)
    assert chip.model_dump() == {"id": item.id, "kind": "licensed_news", "source": item.feed}
    assert item.title not in json.dumps(chip.model_dump())
    with pytest.raises(ValueError):
        PublicSwingCatalyst(id=item.id, kind="licensed_news", source=item.feed, title=item.title)
    with pytest.raises(ValueError):
        PublicSwingCatalyst(id=item.id, kind="broker_feed", source=item.feed)


def test_the_core_evidence_table_labels_rss_items_and_never_their_text(run):
    _src, inputs, _cats, _ = run
    item = next(i for i in inputs.reading if i.source == "rss")
    assert redact.news_source(item) == "rss" and not redact.public_news_ok(item)
    fact = PublicFact(id=item.id, kind="news", label="licensed RSS headline", source="rss")
    assert item.title not in fact.model_dump_json()

    class Pack:
        news = [item]

    assert redact.licensed_texts(Pack()) == [f"{item.title} {item.summary}"]


def test_the_leak_scan_catches_rss_text_in_public_output(run):
    _src, inputs, _cats, _ = run
    item = next(i for i in inputs.reading if i.source == "rss" and len(i.title.split()) >= 8)
    public = {"ideas": [{"thesis": f"We think {item.title} matters for the book."}]}
    findings = leakscan.scan(public, licensed_texts=[f"{item.title} {item.summary}"])
    assert any(f.rule == "licensed_text" for f in findings)
    assert leakscan.scan({"ideas": [{"thesis": "A clean thesis."}]}, licensed_texts=[item.title]) == []


def test_the_capture_holds_rss_text_apart_and_the_purge_removes_it(run, tmp_path):
    _src, inputs, cats, _ = run
    item = next(i for i in inputs.reading if i.source == "rss")
    sections = scout_input(inputs, cats)[0]
    sink = InputSink()
    sink.begin(role="scout", replicate=0, attempt=0, seed=1, num_predict=10, prompt_id="p", prompt_sha="0" * 64,
               ctx={}, system="s", sections=sections, input_hash="h")
    cycle = f"{SLOT - timedelta(days=9):%Y-%m-%dT%H%MZ}"
    captured = SLOT - timedelta(days=9)
    root = tmp_path / "state"
    write_inputs(root, *sink.build(cycle_id=cycle, captured_at=captured))
    with gzip.open(calls_path(root, cycle), "rt") as fh:
        assert item.title not in fh.read()                               # never in the main capture
    assert item.title in json.dumps(load_licensed(root, cycle).texts)
    purge_licensed(root, now=SLOT)
    assert not licensed_path(root, cycle).exists()
