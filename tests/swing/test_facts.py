"""SW-1: the fact card (design §1.4 step 3): reaction since the news on a synthetic panel matches
hand-computed values; SIP/SIP volume ratios; bars after the slot leave the card hash unchanged;
stale bars -> no_facts; public vs private fields; catalyst metadata is code-attached."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from council.data.bars import bars_from_rows
from council.data.finra import ShortInterest
from council.models.facts import NewsItem, public_news_id
from council.swing import facts
from tests.swing import panel

# Pre-news history ends Thursday 2026-09-24; news after its close; reaction Fri 25 and Mon 28.
PRE = date(2026, 9, 24)
DAYS = panel.sessions(PRE, 260) + [date(2026, 9, 25), date(2026, 9, 28)]
SLOT = datetime(2026, 9, 29, 18, 40, tzinfo=UTC)          # Tuesday: last completed session = Mon 28
NEWS_AT = datetime(2026, 9, 24, 21, 30, tzinfo=UTC)        # 17:30 New York, after Thursday's close


def _bars(rets, *, volume, opens=None, base=100.0):
    closes = base * np.cumprod(1.0 + np.asarray(rets))
    rows = []
    for i, (d, c) in enumerate(zip(DAYS, closes, strict=True)):
        o = opens.get(i, c) if opens else c
        rows.append((pd.Timestamp(d).tz_localize("UTC"), o, max(o, c) * 1.005, min(o, c) * 0.995, c, volume[i]))
    return bars_from_rows(rows), closes


N = len(DAYS)
ALT = [0.0] + [0.01 if i % 2 else -0.01 for i in range(1, N - 2)]
STOCK_RETS = ALT + [0.05, 0.02]
VOL = [1e6] * (N - 2) + [3e6, 2e6]
STOCK, CLOSES = _bars(STOCK_RETS, volume=VOL)
# the gap: Friday opens 4 % above Thursday's close
STOCK.iloc[N - 2, STOCK.columns.get_loc("open")] = CLOSES[N - 3] * 1.04
SECTOR, _ = _bars([r * 0.5 for r in ALT] + [0.01, 0.01], volume=[1e6] * N)
SPY, _ = _bars([r * 0.4 for r in ALT] + [0.005, 0.0], volume=[1e6] * N)
QQQ, _ = _bars([r * 0.6 for r in ALT] + [0.0, 0.01], volume=[1e6] * N)
SEC_ITEM = NewsItem(id=public_news_id("sec", "0000000000-26-000001"), title="8-K: Item 2.02 Results",
                    published_at=NEWS_AT, available_at=NEWS_AT, source="sec", licence="public_domain",
                    form="8-K", items=["2.02", "9.01"], symbols=["TSTA"])
FEED_ITEM = NewsItem(id="N:0000abcd", title="private headline", published_at=NEWS_AT + timedelta(hours=1),
                     available_at=NEWS_AT + timedelta(hours=1), source="etoro_feed", licence="broker_licensed")


def _card(**kw):
    args = {"slot": SLOT, "bars": STOCK, "catalysts": [SEC_ITEM, FEED_ITEM], "sector": "BusEq",
            "sector_bars": SECTOR, "spx_bars": SPY, "ndx_bars": QQQ}
    args.update(kw)
    return facts.build_card("TSTA", "long", **args)


def test_reaction_fields_match_hand_values():
    card = _card(live_price=CLOSES[-1] * 1.01)
    f = card.fields
    assert card.ok and f["news_age_sessions"] == 2
    move = (1.05 * 1.02 - 1) * 100
    assert f["move_since_news_close_pct"] == pytest.approx(move, abs=0.01)
    pre_rets = np.asarray(STOCK_RETS[N - 22:N - 2])
    sig = float(np.std(pre_rets, ddof=1))
    assert f["move_since_news_close_sigma"] == pytest.approx(move / 100 / (sig * math.sqrt(2)), abs=0.01)
    assert f["gap_pct"] == pytest.approx(4.0, abs=0.01)
    assert f["vol_ratio_since"] == pytest.approx(2.5, abs=0.01)
    assert f["vol_ratio_last"] == pytest.approx(2.0, abs=0.01)
    assert f["sector_etf"] == "XLK" and f["sector_move_since_pct"] == pytest.approx(2.01, abs=0.01)
    assert f["spx_move_since_pct"] == pytest.approx(0.5, abs=0.01)
    assert f["ndx_move_since_pct"] == pytest.approx(1.0, abs=0.01)
    # beta vs the sector over the 60 sessions to PRE: stock returns = 2 x sector returns
    assert f["rel_move_since_pct"] == pytest.approx(move - 2.0 * 2.01, abs=0.02)
    assert f["move_today_live_pct"] == pytest.approx(1.0, abs=0.01)
    assert f["move_since_news_live_pct"] == pytest.approx(((1.05 * 1.02 * 1.01) - 1) * 100, abs=0.01)
    live = ((1.05 * 1.02 * 1.01) - 1) * 100
    # two completed sessions since the news plus today's partial one
    assert f["move_since_news_live_sigma"] == pytest.approx(live / 100 / (sig * math.sqrt(3)), abs=0.01)
    assert f["move_today_live_sigma"] == pytest.approx(
        0.01 / float(np.std(np.asarray(STOCK_RETS[-20:]), ddof=1)), abs=0.01)
    rs = np.asarray(STOCK_RETS[-60:])
    rm = np.asarray(([r * 0.4 for r in ALT] + [0.005, 0.0])[-60:])
    assert f["beta_60d"] == pytest.approx(np.cov(rs, rm, ddof=1)[0, 1] / np.var(rm, ddof=1), abs=0.01)
    assert f["ret_5d"] is not None and f["trend"] in ("up", "mixed", "down")
    assert f["px_ge_10"] is True and f["adv_bucket"] == "50-200M"


def test_bars_after_the_slot_leave_the_hash_unchanged():
    base = _card().content_hash()
    extra_day = pd.Timestamp(date(2026, 9, 29)).tz_localize("UTC")
    future = pd.concat([STOCK, pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                                             "volume": [9e9]}, index=pd.DatetimeIndex([extra_day], name="start"))])
    assert _card(bars=future).content_hash() == base


def test_stale_or_short_bars_are_no_facts_and_no_catalyst():
    assert _card(bars=STOCK.iloc[:-1]).reason == "no_facts"
    assert _card(bars=STOCK.iloc[-40:]).reason == "no_facts"
    assert _card(bars=None).reason == "no_facts"
    late = NewsItem(**{**SEC_ITEM.model_dump(), "available_at": SLOT, "published_at": SLOT})
    assert _card(catalysts=[late]).reason == "no_catalyst_id"


def test_intraday_news_uses_the_prior_close_and_next_open():
    during = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)          # Friday 11:00 New York
    item = NewsItem(**{**SEC_ITEM.model_dump(), "available_at": during, "published_at": during})
    f = _card(catalysts=[item]).fields
    assert f["news_age_sessions"] == 2                           # Fri close and Mon close follow it
    assert f["move_since_news_close_pct"] == pytest.approx((1.05 * 1.02 - 1) * 100, abs=0.01)
    assert f["gap_pct"] == pytest.approx(2.0, abs=0.01)          # Monday's open (= its close) vs Friday's close


def test_short_interest_earnings_fundamentals_and_crowding():
    si = ShortInterest("TSTA", date(2026, 9, 15), 12_000_000, 1e6, 12.0)
    card = _card(short_interest=si, shares_outstanding=100_000_000, earnings_next=date(2026, 10, 22),
                 earnings_confirmed=True, last_release_at=NEWS_AT,
                 fundamentals={"rev_yoy": 12.3456, "gm_chg": None})
    f = card.fields
    assert f["short_interest_pct_float"] == 12.0 and f["short_interest_basis"] == "shares_outstanding"
    assert f["crowding"] == "elevated" and f["earnings_confirmed"] is True
    assert f["earnings_last_sessions_ago"] == 2 and f["rev_yoy"] == 12.35 and f["gm_chg"] is None
    assert _card().fields["crowding"] == "unknown"
    assert _card(earnings_next=None, earnings_confirmed=True).fields["earnings_confirmed"] is False


def test_correlation_with_open_trades_and_core_lines():
    f = _card(corr_bars={"SPX": SPY, "SEMIS": SECTOR}).fields
    rs = np.asarray(STOCK_RETS[-60:])
    hand = {name: float(np.corrcoef(rs, np.asarray(r[-60:]))[0, 1])
            for name, r in (("SPX", [x * 0.4 for x in ALT] + [0.005, 0.0]),
                            ("SEMIS", [x * 0.5 for x in ALT] + [0.01, 0.01]))}
    best = max(hand, key=hand.get)
    assert f["corr_60d_with"] == best and f["corr_60d_max"] == pytest.approx(hand[best], abs=0.01)


def test_public_view_withholds_private_and_alpaca_fields():
    card = _card(live_price=CLOSES[-1], short_interest=ShortInterest("TSTA", date(2026, 9, 15), 1e6, None, None),
                 shares_outstanding=1e8)
    public = card.public_view()
    assert set(public["fields"]).isdisjoint(facts.PRIVATE_FIELDS | facts.ALPACA_FIELDS)
    assert "news_age_sessions" in public["fields"] and "earnings_next" in public["fields"]
    widened = card.public_view(alpaca_public=True)["fields"]
    assert "move_since_news_close_pct" in widened and "adv_usd_20d" not in widened
    assert "private headline" not in str(public)
    assert public["catalyst_items"] == [{"id": SEC_ITEM.id, "form": "8-K", "items": ["2.02", "9.01"],
                                         "titles": ["Results of Operations and Financial Condition",
                                                    "Financial Statements and Exhibits"]}]
    assert all(i.startswith("X:TSTA:") for i in card.fact_ids())


def test_sec_fundamentals_without_companyfacts():
    assert facts.sec_fundamentals(1, None, slot=SLOT) == {}


def test_sec_fundamentals_from_the_trimmed_fixture():
    import json
    from pathlib import Path
    doc = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "sec" / "companyfacts_trimmed.json")
                     .read_text())["companies"]["MSFT"]
    out = facts.sec_fundamentals(int(doc["cik"]), doc["companyfacts"], slot=SLOT)
    assert set(out) == {"rev_yoy", "rev_accel", "gm_chg", "om_chg", "fundamentals_age_d"}
    assert out["rev_yoy"] == pytest.approx(18.53, abs=0.01) and out["fundamentals_age_d"] > 365


def test_filing_age_is_the_cited_catalyst_filings_age_not_the_latest_periodic_filing():
    """Regression (paper cycle 2026-09-29T1840Z): an 8-K accepted on the slot day showed
    `filing_age_d` 41-95, the company's latest 10-Q age from companyfacts."""
    same_day = SLOT - timedelta(hours=3)
    today_8k = NewsItem(**{**SEC_ITEM.model_dump(), "published_at": same_day, "available_at": same_day})
    card = _card(catalysts=[today_8k, FEED_ITEM], fundamentals={"fundamentals_age_d": 41.0})
    assert card.fields["filing_age_d"] == 0.12 and card.fields["fundamentals_age_d"] == 41.0
    # the earliest cited SEC filing, by its accepted time (never the feed item)
    older = NewsItem(**{**SEC_ITEM.model_dump(), "id": public_news_id("sec", "0000000000-26-000002"),
                        "published_at": SLOT - timedelta(days=2), "available_at": SLOT - timedelta(days=2)})
    assert _card(catalysts=[today_8k, older]).fields["filing_age_d"] == 2.0
    assert _card(catalysts=[FEED_ITEM]).fields["filing_age_d"] is None
