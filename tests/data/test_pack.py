from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from council.clock import cycle_id_for
from council.facts.evidence_ids import cost_id, event_id, news_id
from council.facts.features import market_states
from council.facts.pack import (
    EVENT_HORIZON,
    NEWS_MAX,
    build_fact_pack,
    cost_facts_from_quotes,
    effective_age_h,
    is_stale,
)
from council.models.broker import CostQuote
from council.models.facts import EventItem, Fact, NewsItem
from tests.data.synth import daily_bars, universe_history, utc

SLOT = utc(2026, 10, 1, 14, 40)          # Thursday 10:40 New York


def _pack(policy, slot=SLOT, history=None, states=None, **kw):
    history = history if history is not None else universe_history(policy, "2026-10-01")
    states = states if states is not None else market_states(policy, history, now=slot)
    return build_fact_pack(
        cycle_id=cycle_id_for(slot), slot=slot, now=slot + timedelta(minutes=2),
        policy=policy, states=states, **kw,
    )


def _facts(pack):
    return {f.id: f for f in pack.facts}


# ------------------------------------------------------------------------------------ basics
def test_weekday_session_admits_every_line_and_seals(policy):
    pack = _pack(policy)
    assert pack.admitted == policy.universe.symbols() and pack.frozen == []
    assert pack.input_hash == pack.compute_hash() and len(pack.input_hash) == 64
    assert [f.id for f in pack.facts] == sorted(f.id for f in pack.facts)
    facts = _facts(pack)
    ndx = pack.states["NDX"]
    assert facts["F:NDX:dist_sma200"].unit == "pct"
    assert facts["F:NDX:dist_sma200"].value == round(ndx.dist_sma200_pct, 3)
    assert facts["V:NDX:sigma_ann"].value == round(ndx.sigma_ann * 100, 2)
    assert facts["V:NDX:vol_ratio"].unit == "ratio" and facts["F:NDX:trend"].unit == "state"
    assert facts["F:NDX:market_open"].value is True and facts["F:NDX:market_open"].available_at == SLOT
    assert facts["F:NDX:dist_sma200"].available_at == utc(2026, 10, 1, 0, 0)   # Sep 30, 20:00 NY
    assert facts["F:BTC:mom10d"].available_at == utc(2026, 10, 1, 0, 0)        # Sep 30 bar close
    assert all(f.available_at <= SLOT for f in pack.facts)
    assert pack.states["NDX"].data_age_h == pytest.approx(14.667, abs=1e-3)


def test_hash_ignores_created_at_but_not_inputs(policy):
    a = _pack(policy)
    b = build_fact_pack(cycle_id=a.cycle_id, slot=SLOT, now=SLOT + timedelta(minutes=9), policy=policy,
                        states=market_states(policy, universe_history(policy, "2026-10-01"), now=SLOT))
    assert a.input_hash == b.input_hash
    assert _pack(policy, quality_flags=["x"]).input_hash != a.input_hash


# ------------------------------------------------------------------------------------ freezing
def test_closed_equity_session_freezes_etf_line_only(policy):
    pack = _pack(policy, slot=utc(2026, 10, 1, 2, 40))            # Wed 22:40 New York
    assert pack.frozen == ["SEMIS"] and pack.states["SEMIS"].frozen_reason == "market_closed"
    assert "NDX" in pack.admitted and "BTC" in pack.admitted       # index CFD / crypto trade
    assert pack.states["SEMIS"].market_open is False
    assert "F:SEMIS:dist_sma200" in _facts(pack)                  # facts kept for context


def test_weekend_freezes_everything_but_crypto(policy):
    pack = _pack(policy, slot=utc(2026, 10, 3, 10, 40), history=universe_history(policy, "2026-10-03"))
    assert pack.admitted == ["BTC", "ETH"]
    assert all("market_closed" in pack.states[s].frozen_reason for s in pack.frozen)


def test_stale_rule_boundary():
    avail = utc(2026, 9, 30, 0, 0)
    assert not is_stale(avail, avail + timedelta(hours=30), asset_class="crypto", max_h=30)
    assert is_stale(avail, avail + timedelta(hours=30, seconds=1), asset_class="crypto", max_h=30)


def test_crypto_goes_stale_after_30h(policy):
    history = universe_history(policy, "2026-10-01")
    history["BTC"] = daily_bars("2026-09-29", 460, seed=1, weekdays_only=False)   # closes Sep 30 00:00
    fresh = _pack(policy, slot=utc(2026, 10, 1, 2, 40), history=history)
    stale = _pack(policy, slot=utc(2026, 10, 1, 6, 40), history=history)
    assert "BTC" in fresh.admitted
    assert "BTC" in stale.frozen and stale.states["BTC"].frozen_reason == "stale"


def test_weekend_and_holiday_aware_age():
    friday_bar = utc(2026, 10, 3, 0, 0)                            # Fri Oct 2 bar, 20:00 NY
    monday = utc(2026, 10, 5, 14, 40)
    assert effective_age_h(friday_bar, monday, asset_class="index") == pytest.approx(14.667, abs=1e-3)
    assert effective_age_h(friday_bar, monday, asset_class="crypto") == pytest.approx(62.667, abs=1e-3)
    labor_day_tuesday = utc(2026, 9, 8, 14, 40)                    # Mon Sep 7 is a US holiday
    assert effective_age_h(utc(2026, 9, 5, 0, 0), labor_day_tuesday, asset_class="etf") == pytest.approx(14.667, abs=1e-3)


def test_friday_bar_fresh_through_monday_not_tuesday(policy):
    history = universe_history(policy, "2026-10-02")               # last bars: Friday Oct 2
    monday = _pack(policy, slot=utc(2026, 10, 5, 14, 40), history=history)
    assert "SPX" in monday.admitted and "SEMIS" in monday.admitted
    assert monday.states["BTC"].frozen_reason == "stale"          # crypto has no weekend grace
    tuesday = _pack(policy, slot=utc(2026, 10, 6, 14, 40), history=history)
    assert tuesday.states["SPX"].frozen_reason == "stale"


def test_missing_unknown_and_short_history(policy):
    history = universe_history(policy, "2026-10-01")
    history["OIL"] = history["OIL"].iloc[-100:]
    states = market_states(policy, history, now=SLOT)
    states.pop("GBPUSD")
    states["XYZ"] = states["NDX"].model_copy(update={"symbol": "XYZ"})
    pack = _pack(policy, states=states)
    assert "missing_state:GBPUSD" in pack.quality_flags and "unknown_state:XYZ" in pack.quality_flags
    assert "XYZ" not in pack.states
    assert pack.states["GBPUSD"].frozen and pack.states["GBPUSD"].frozen_reason == "no_data"
    assert pack.states["OIL"].frozen_reason == "no_data"
    assert "F:OIL:mom10d" in _facts(pack) and "F:OIL:trend" not in _facts(pack)


def test_upstream_reasons_kept_and_own_reasons_recomputed(policy):
    history = universe_history(policy, "2026-10-01")
    states = market_states(policy, history, now=SLOT)
    states["GOLD"] = states["GOLD"].model_copy(update={"frozen": True, "frozen_reason": "not_eligible"})
    pack = _pack(policy, states=states)
    assert pack.states["GOLD"].frozen_reason == "not_eligible" and "GOLD" not in pack.admitted
    night = _pack(policy, slot=utc(2026, 10, 1, 2, 40), history=history)
    reused = _pack(policy, states=dict(night.states))
    assert "SEMIS" in reused.admitted                              # market_closed not carried over


def test_state_built_from_future_bar_is_frozen(policy):
    states = market_states(policy, universe_history(policy, "2026-10-01"), now=SLOT)
    states["NDX"] = states["NDX"].model_copy(update={"bar_available_at": SLOT + timedelta(hours=1)})
    pack = _pack(policy, states=states)
    ndx = pack.states["NDX"]
    assert ndx.frozen_reason == "future_data" and ndx.data_age_h is None and ndx.bar_available_at is None
    assert not any(f.id.startswith(("F:NDX:", "V:NDX:")) for f in pack.facts)


def test_state_bar_time_wins_and_keyword_is_only_a_fallback(policy):
    states = market_states(policy, universe_history(policy, "2026-10-01"), now=SLOT)
    assert states["NDX"].bar_available_at == utc(2026, 10, 1, 0, 0)
    # the state's own time wins over a (wrong) keyword value
    pack = _pack(policy, states=states, bar_available_at={"NDX": SLOT + timedelta(hours=1)})
    assert "NDX" in pack.admitted and pack.states["NDX"].bar_available_at == utc(2026, 10, 1, 0, 0)
    assert _facts(pack)["F:NDX:dist_sma200"].available_at == utc(2026, 10, 1, 0, 0)
    # a state without one falls back to the keyword
    bare = dict(states, NDX=states["NDX"].model_copy(update={"bar_available_at": None}))
    late = _pack(policy, states=bare, bar_available_at={"NDX": SLOT + timedelta(hours=1)})
    assert late.states["NDX"].frozen_reason == "future_data"
    earlier = _pack(policy, states=bare, bar_available_at={"NDX": utc(2026, 9, 30, 0, 0)})
    assert earlier.states["NDX"].bar_available_at == utc(2026, 9, 30, 0, 0)
    assert earlier.states["NDX"].data_age_h == pytest.approx(38.667, abs=1e-3)


# ------------------------------------------------------------------------------------ admission
def _news(i, at):
    return NewsItem(id=news_id(str(i)), title=f"t{i}", published_at=at, available_at=at)


def test_news_admission_window_and_cap(policy):
    items = [
        _news("future", SLOT + timedelta(minutes=1)),
        _news("old", SLOT - timedelta(hours=48, seconds=1)),
        _news("edge", SLOT - timedelta(hours=48)),
        _news("at_slot", SLOT),                                  # read during the cycle: excluded
        _news("just_before", SLOT - timedelta(seconds=1)),
    ]
    pack = _pack(policy, news=items + [items[-1]])
    assert [n.title for n in pack.news] == ["tjust_before", "tedge"]
    many = [_news(i, SLOT - timedelta(minutes=i + 1)) for i in range(NEWS_MAX + 5)]
    capped = _pack(policy, news=many)
    assert len(capped.news) == NEWS_MAX and capped.news[0].title == "t0"


def _event(kind, at, severity=3):
    return EventItem(id=event_id(kind, at), kind=kind, at_utc=at, severity=severity, source="test")


def test_event_window_and_known_at(policy):
    inside_ahead = _event("fomc", SLOT + EVENT_HORIZON)
    beyond = _event("cpi", SLOT + EVENT_HORIZON + timedelta(minutes=1))
    recent = _event("nfp", SLOT - timedelta(hours=24), severity=2)
    too_old = _event("pce", SLOT - timedelta(hours=24, minutes=1), severity=2)
    surprise = _event("fomc", SLOT + timedelta(days=1))
    pack = _pack(policy, events=[beyond, inside_ahead, too_old, recent, surprise],
                 event_known_at={surprise.id: SLOT + timedelta(minutes=30)})
    assert [e.id for e in pack.events] == [recent.id, inside_ahead.id]
    known = _pack(policy, events=[surprise], event_known_at={surprise.id: SLOT})
    assert [e.id for e in known.events] == [surprise.id]


def test_event_own_known_at_wins_over_the_keyword(policy):
    surprise = _event("fomc", SLOT + timedelta(days=1))
    late = surprise.model_copy(update={"known_at": SLOT + timedelta(minutes=30)})
    assert _pack(policy, events=[late]).events == []
    assert _pack(policy, events=[late], event_known_at={late.id: SLOT}).events == []
    early = surprise.model_copy(update={"known_at": SLOT - timedelta(days=30)})
    kept = _pack(policy, events=[early], event_known_at={early.id: SLOT + timedelta(hours=1)})
    assert [e.id for e in kept.events] == [early.id]


def _series(sid, values, end="2026-09-30"):
    idx = pd.date_range(end=end, periods=len(values), freq="D", tz="UTC")
    return pd.Series(values, index=idx, name=sid, dtype="float64")


def test_macro_facts_availability_units_and_publication(policy):
    macro = {
        "DGS10": _series("DGS10", [4.0 + 0.01 * i for i in range(30)]),
        "DTWEXBGS": _series("DTWEXBGS", [120.0] * 10 + [121.2] * 20),
        "VIXCLS": _series("VIXCLS", [18.5] * 30),
        "DGS2": _series("DGS2", []),
        "DFF": _series("DFF", [4.33] * 5, end="2026-10-05"),        # every value after the slot
        "T10Y2Y": _series("T10Y2Y", [0.5] * 5),                      # too short for a change
    }
    facts = _facts(_pack(policy, macro=macro))
    assert facts["M:DGS10@2026-09-30"].value == pytest.approx(4.29) and facts["M:DGS10@2026-09-30"].unit == "pct"
    assert facts["M:DGS10.chg20@2026-09-30"].value == pytest.approx(20.0) and facts["M:DGS10.chg20@2026-09-30"].unit == "bps"
    assert facts["M:DGS10@2026-09-30"].available_at == utc(2026, 10, 1, 12, 0)
    assert "M:DTWEXBGS@2026-09-30" not in facts                            # index level is not a %
    assert facts["M:DTWEXBGS.chg20@2026-09-30"].value == pytest.approx(1.0)
    assert facts["M:VIXCLS@2026-09-30"].source == "fred" and facts["M:DGS10@2026-09-30"].source == "fred"
    assert not facts["M:VIXCLS@2026-09-30"].publishable                   # licensed: read-only
    assert facts["M:DGS10@2026-09-30"].publishable and facts["M:DGS10.chg20@2026-09-30"].publishable
    assert facts["M:DTWEXBGS.chg20@2026-09-30"].publishable
    assert all(f.publishable for f in facts.values() if not f.id.startswith("M:VIXCLS"))
    licensed = _facts(_pack(policy, macro={"BAMLH0A0HYM2": _series("BAMLH0A0HYM2", [3.1] * 30),
                                          "NEWSERIES": _series("NEWSERIES", [1.0] * 30)}))
    assert not licensed["M:BAMLH0A0HYM2@2026-09-30"].publishable
    assert not licensed["M:BAMLH0A0HYM2.chg20@2026-09-30"].publishable
    assert not licensed["M:NEWSERIES.chg20@2026-09-30"].publishable        # unknown: fail closed
    assert "M:T10Y2Y@2026-09-30" in facts and "M:T10Y2Y.chg20@2026-09-30" not in facts
    flags = _pack(policy, macro=macro).quality_flags
    assert "macro_missing:DGS2" in flags and "macro_missing:DFF" in flags
    before_noon = _facts(_pack(policy, slot=utc(2026, 10, 1, 10, 40), macro=macro))
    assert "M:DGS10@2026-09-29" in before_noon and "M:DGS10@2026-09-30" not in before_noon
    short_index = _pack(policy, macro={"DTWEXBGS": _series("DTWEXBGS", [120.0] * 5)})
    assert "macro_short:DTWEXBGS" in short_index.quality_flags


def test_cost_facts_admission(policy):
    ok = Fact(id=cost_id("NDX", "bps_side"), kind="cost", symbol="NDX", value=5.0, unit="bps",
              available_at=SLOT - timedelta(hours=1), source="costs")
    late = ok.model_copy(update={"id": cost_id("SPX", "bps_side"), "available_at": SLOT + timedelta(seconds=1)})
    facts = _facts(_pack(policy, cost_facts=[ok, late]))
    assert "C:NDX:bps_side" in facts and "C:SPX:bps_side" not in facts
    with pytest.raises(ValueError):
        _pack(policy, cost_facts=[ok.model_copy(update={"id": "F:NDX:bps_side"})])
    with pytest.raises(ValueError):
        _pack(policy, cost_facts=[ok, ok])


def test_cost_facts_from_quotes_are_citable_and_admitted(policy):
    quoted = SLOT + timedelta(minutes=3)
    quotes = {
        "NDX": CostQuote(symbol="EQQQ.L", direction="long", settlement="real", leverage=1,
                         per_side_bps=7.254, what_if_bps=None, carry_bps_day=0.0,
                         floor_applied=True, quoted_at=quoted),
        "GOLD": {"per_side_bps": 12.0, "carry_bps_day": 0.41096, "floor_applied": False},
        "OIL": {"per_side_bps": float("nan"), "carry_bps_day": 1.2},
        "SPX": None,
    }
    facts = cost_facts_from_quotes(quotes, slot=SLOT)
    by_id = {f.id: f for f in facts}
    assert sorted(by_id) == ["C:GOLD:carry_bps_day", "C:GOLD:per_side_bps", "C:NDX:carry_bps_day",
                             "C:NDX:per_side_bps", "C:OIL:carry_bps_day"]
    ndx = by_id["C:NDX:per_side_bps"]
    assert ndx.kind == "cost" and ndx.unit == "bps" and ndx.value == 7.25 and ndx.symbol == "NDX"
    assert ndx.available_at == SLOT and ndx.source == "costs:floor"
    assert by_id["C:GOLD:carry_bps_day"].unit == "bps_day" and by_id["C:GOLD:carry_bps_day"].value == 0.411
    assert by_id["C:GOLD:per_side_bps"].source == "costs:whatif" and by_id["C:OIL:carry_bps_day"].source == "costs"
    pack = _pack(policy, cost_facts=facts)
    assert {"C:NDX:per_side_bps", "C:GOLD:carry_bps_day"} <= pack.evidence_ids()
    with pytest.raises(ValueError):
        cost_facts_from_quotes({("NDX", "long"): quotes["NDX"]}, slot=SLOT)
