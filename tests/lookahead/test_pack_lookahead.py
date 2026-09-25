"""LOOKAHEAD: a FactPack sealed at slot T must not depend on anything first knowable at or after T.

We build a world that extends days past T, seal the pack, then rewrite every row that was not yet
knowable at T and seal again: the input_hash must not move. The "not yet knowable" rules below are
written from the providers' publication schedules, independently of the production code:
- Binance/eToro bars: not complete at T (start + 1 day > T) — includes the in-progress bar.
- Tiingo bars: trading date D published after T (D 20:00 New York > T) — includes today's bar.
- News: available_at >= T.
- FRED rows: dated D with D+1 12:00 UTC > T (includes every row dated on or after T's date).
- Cost quotes: quoted after T.
- Events: a schedule first known after T (per event_known_at), and events beyond the pack horizon.
  Scheduled events known before T are PUBLIC IN ADVANCE and must stay in the pack even when they
  happen after T: the event block (R16) exists to act before them.
Negative controls prove the hash does react to admissible rows.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from council.clock import cycle_id_for
from council.data.binance import parse_klines
from council.data.etoro_market import parse_candles
from council.data.tiingo import parse_daily
from council.facts.evidence_ids import cost_id, event_id, news_id
from council.facts.features import market_state
from council.facts.pack import build_fact_pack
from council.models.facts import EventItem, Fact, FactPack, NewsItem
from tests.data.synth import (
    bars_from_closes,
    binance_payload,
    day_index,
    tiingo_payload,
    universe_history,
    utc,
)

NY = ZoneInfo("America/New_York")
SLOTS = [
    utc(2026, 10, 1, 14, 40),     # Thursday, US session open
    utc(2026, 10, 1, 2, 40),      # Wednesday evening in New York (Tiingo's Sep 30 bar just out)
    utc(2026, 10, 1, 22, 40),     # 18:40 New York: Oct 1 closed but not yet published
    utc(2026, 10, 3, 10, 40),     # Saturday
    utc(2026, 10, 5, 14, 40),     # Monday
    utc(2026, 11, 2, 14, 40),     # Monday after the DST switch (New York = UTC-5)
]
MACRO_IDS = ("DGS10", "DGS2", "T10Y2Y", "DFF", "DTWEXBGS", "VIXCLS")


@dataclass(frozen=True)
class World:
    history: dict[str, pd.DataFrame]
    news: list[NewsItem]
    events: list[EventItem]
    known_at: dict[str, datetime]
    macro: dict[str, pd.Series]
    costs: list[Fact]


# ------------------------------------------------------------------------------------ world
def make_world(policy, slot: datetime) -> World:
    end = (slot + timedelta(days=4)).date()
    history = universe_history(policy, end, n=470, seed=11)
    news = [
        NewsItem(id=news_id(f"n{k}"), title=f"headline {k}", summary="summary", symbols=["NDX"],
                 published_at=slot + timedelta(hours=k), available_at=slot + timedelta(hours=k))
        for k in range(-40, 30, 3)
    ]
    news.append(NewsItem(id=news_id("at-slot"), title="stamped at T", published_at=slot, available_at=slot))

    def ev(kind, at, sev=2):
        return EventItem(id=event_id(kind, at), kind=kind, at_utc=at, severity=sev, source="test")

    events = [
        ev("nfp", slot - timedelta(hours=10)),
        ev("cpi", slot + timedelta(days=2), 3),          # upcoming, scheduled long ago: admitted
        ev("pce", slot + timedelta(days=20)),            # beyond the horizon
        ev("fomc", slot + timedelta(days=1), 3),         # emergency meeting announced after T
    ]
    known_at = {events[3].id: slot + timedelta(hours=1)}
    days = pd.date_range(end=pd.Timestamp(end), periods=400, freq="D", tz="UTC")
    rng = np.random.default_rng(5)
    macro = {sid: pd.Series(3 + rng.normal(0, 0.05, len(days)).cumsum(), index=days, name=sid)
             for sid in MACRO_IDS}
    macro["DTWEXBGS"] = macro["DTWEXBGS"] + 117

    def cost(sym, at, value):
        return Fact(id=cost_id(sym, "bps_side"), kind="cost", symbol=sym, value=value, unit="bps",
                    available_at=at, source="costs")

    costs = [cost("NDX", slot - timedelta(hours=1), 5.0), cost("SPX", slot + timedelta(minutes=10), 5.0)]
    return World(history, news, events, known_at, macro, costs)


def build(policy, world: World, slot: datetime) -> FactPack:
    states = {
        line.symbol: market_state(line, world.history[line.symbol], now=slot, policy=policy)
        for line in policy.universe.lines
    }
    return build_fact_pack(
        cycle_id=cycle_id_for(slot), slot=slot, now=slot + timedelta(minutes=4), policy=policy,
        states=states, news=world.news, events=world.events, macro=world.macro,
        cost_facts=world.costs, event_known_at=world.known_at,
    )


# ------------------------------------------------------------------------------------ future rules
def bar_unknowable(start: pd.Timestamp, source: str, slot: datetime) -> bool:
    if source == "tiingo":
        published = datetime.combine(start.date(), time(20, 0), tzinfo=NY)
        return published > slot
    return start.to_pydatetime() + timedelta(days=1) > slot


def macro_unknowable(day: pd.Timestamp, slot: datetime) -> bool:
    return datetime.combine(day.date() + timedelta(days=1), time(12, 0), tzinfo=slot.tzinfo) > slot


def mutate_future(policy, world: World, slot: datetime, seed: int = 0) -> World:
    rng = np.random.default_rng(seed)
    history = {}
    for line in policy.universe.lines:
        df = world.history[line.symbol].copy()
        mask = np.array([bar_unknowable(ts, line.signal.source, slot) for ts in df.index])
        assert mask.any(), f"{line.symbol}: the test must rewrite at least one bar"
        for col in ("open", "high", "low", "close"):
            df.loc[mask, col] = df.loc[mask, col].to_numpy() * rng.uniform(0.3, 3.0, mask.sum())
        df.loc[mask, "volume"] = df.loc[mask, "volume"] * 7
        last = df.index[-1]
        extra = bars_from_closes(rng.uniform(1, 1000, 3), day_index(last + pd.Timedelta(days=3), 3))
        history[line.symbol] = pd.concat([df, extra])

    news = [
        n.model_copy(update={"title": "REWRITTEN", "summary": "later", "symbols": ["BTC", "OIL"]})
        if n.available_at >= slot else n
        for n in world.news
    ]
    admissible = [n for n in world.news if n.available_at < slot]
    news += [
        NewsItem(id=news_id("brand-new"), title="breaking after T", published_at=slot + timedelta(minutes=1),
                 available_at=slot + timedelta(minutes=1)),
        admissible[-1].model_copy(update={"title": "edited after T", "available_at": slot + timedelta(hours=2),
                                          "published_at": slot + timedelta(hours=2)}),
    ]

    known_at = dict(world.known_at)
    events = []
    for e in world.events:
        late = known_at.get(e.id) is not None and known_at[e.id] >= slot
        beyond = e.at_utc > slot + timedelta(days=7)
        events.append(e.model_copy(update={"severity": 1, "binary": False}) if late or beyond else e)
    surprise = EventItem(id=event_id("pce", slot + timedelta(hours=30)), kind="pce",
                         at_utc=slot + timedelta(hours=30), severity=3, source="test")
    assert surprise.id not in {e.id for e in world.events}   # IDs are kind@date: keep them distinct
    events.append(surprise)
    known_at[surprise.id] = slot + timedelta(minutes=5)

    macro = {}
    for sid, series in world.macro.items():
        s = series.copy()
        mask = np.array([macro_unknowable(d, slot) for d in s.index])
        assert mask.any()
        s[mask] = s[mask].to_numpy() * rng.uniform(0.5, 2.0, mask.sum())
        future = pd.Series([99.0], index=[s.index[-1] + pd.Timedelta(days=1)], name=sid)
        macro[sid] = pd.concat([s, future])

    costs = [c.model_copy(update={"value": 999.0}) if c.available_at > slot else c for c in world.costs]
    costs.append(Fact(id=cost_id("GOLD", "bps_side"), kind="cost", symbol="GOLD", value=1.0, unit="bps",
                      available_at=slot + timedelta(seconds=1), source="costs"))
    return World(history, news, events, known_at, macro, costs)


# ------------------------------------------------------------------------------------ tests
@pytest.mark.parametrize("slot", SLOTS, ids=lambda s: s.strftime("%a-%m%d-%H%M"))
def test_future_rows_never_change_the_hash(policy, slot):
    world = make_world(policy, slot)
    base = build(policy, world, slot)
    for seed in (0, 1):
        again = build(policy, mutate_future(policy, world, slot, seed), slot)
        assert again.input_hash == base.input_hash
        assert again.model_dump(exclude={"created_at"}) == base.model_dump(exclude={"created_at"})


def test_upcoming_scheduled_events_stay_in_the_pack(policy):
    slot = SLOTS[0]
    pack = build(policy, make_world(policy, slot), slot)
    kinds = {e.kind: e for e in pack.events}
    assert "cpi" in kinds and kinds["cpi"].at_utc > slot            # known in advance: admitted
    assert "fomc" not in kinds and "pce" not in kinds                # announced after T / beyond horizon
    assert all(n.available_at < slot for n in pack.news)
    assert all(f.available_at <= slot for f in pack.facts)


@pytest.mark.parametrize("what", ["bar", "news", "macro", "event", "cost"])
def test_negative_controls_admissible_rows_do_move_the_hash(policy, what):
    slot = SLOTS[0]
    world = make_world(policy, slot)
    base = build(policy, world, slot).input_hash
    if what == "bar":
        df = world.history["NDX"].copy()
        known = [ts for ts in df.index if not bar_unknowable(ts, "tiingo", slot)]
        df.loc[known[-1], "close"] *= 1.01
        world = replace(world, history={**world.history, "NDX": df})
    elif what == "news":
        idx = max(i for i, n in enumerate(world.news) if n.available_at < slot)
        news = list(world.news)
        news[idx] = news[idx].model_copy(update={"title": "changed"})
        world = replace(world, news=news)
    elif what == "macro":
        s = world.macro["DGS10"].copy()
        known = [d for d in s.index if not macro_unknowable(d, slot)]
        s[known[-1]] += 0.5
        world = replace(world, macro={**world.macro, "DGS10": s})
    elif what == "event":
        world = replace(world, events=[e.model_copy(update={"severity": 1}) if e.kind == "cpi" else e
                                       for e in world.events])
    else:
        world = replace(world, costs=[c.model_copy(update={"value": 6.0}) if c.symbol == "NDX" else c
                                      for c in world.costs])
    assert build(policy, world, slot).input_hash != base


@pytest.mark.parametrize("slot", SLOTS[:3], ids=lambda s: s.strftime("%H%M"))
def test_parsers_ignore_future_rows_in_raw_payloads(policy, slot):
    world = make_world(policy, slot)
    mutated = mutate_future(policy, world, slot)
    btc, btc2 = world.history["BTC"], mutated.history["BTC"]
    pd.testing.assert_frame_equal(
        parse_klines(binance_payload(btc), "1d", slot), parse_klines(binance_payload(btc2), "1d", slot)
    )
    ndx, ndx2 = world.history["NDX"], mutated.history["NDX"]
    pd.testing.assert_frame_equal(parse_daily(tiingo_payload(ndx), slot), parse_daily(tiingo_payload(ndx2), slot))

    def candles(df):
        rows = [{"fromDate": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": r.open, "high": r.high,
                 "low": r.low, "close": r.close, "volume": r.volume} for ts, r in df.iloc[::-1].iterrows()]
        return {"interval": "OneDay", "candles": [{"instrumentId": 1, "candles": rows}]}

    pd.testing.assert_frame_equal(parse_candles(candles(btc), "OneDay", slot),
                                  parse_candles(candles(btc2), "OneDay", slot))
