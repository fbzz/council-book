"""The FactPack builder: everything the council may see this cycle, percentage-only and sealed.

Admission rules (nothing later than the slot is admissible):
- Market/vol facts carry the availability time of the bar they were computed from. States must be
  computed as of the slot (features.market_state(now=slot)). That time is the state's
  `bar_available_at`; the `bar_available_at` keyword is a fallback for states without one, and
  the last resort is slot - data_age_h.
- News: available_at strictly before the slot (an item stamped at the slot instant can only have
  been read during the cycle) and at most 48 h old; newest 40, de-duplicated by ID.
- Macro: an observation is used only once fred.available_at(D) <= slot; the newest such value.
  Licensed series (fred.READ_ONLY) and unknown series get `Fact.publishable = False`: agents
  read them, public documents never show their values.
- Events: scheduled events in [slot - 24 h, slot + 7 d] (upcoming events are public by schedule,
  and the event block needs them). An event whose schedule became known after the slot (per
  `EventItem.known_at`, else the `event_known_at` keyword) is dropped.
- Cost facts: available_at <= slot, and each must be a C: fact of kind "cost"
  (`cost_facts_from_quotes` builds them from the cycle's floored quotes).

Freezing rules (a frozen line is not in `admitted` and gets no legs):
- no_data: no usable daily bar, or no trend / sigma (history too short).
- stale: the newest daily bar is older than risk.freshness.daily_bar_max_h. For non-crypto lines,
  weekend days and US exchange holidays do not count, so a Friday bar stays fresh through
  Monday's session.
- market_closed: clock.market_open(asset_class, slot) is False.
- future_data: the caller's state used a bar that became available after the slot.

Facts are percent-denominated (dist/mom/dd in %, sigma_ann in % a year); ratios are unitless.
Nothing that depends on data after the slot (not even a count of dropped items) enters the pack.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta

import pandas as pd

from council import clock
from council.data import fred
from council.data.bars import to_utc
from council.data.feeds import sort_news
from council.facts.evidence_ids import cost_id, fact_id, macro_id, vol_id
from council.models.facts import EventItem, Fact, FactPack, MarketState, NewsItem
from council.policy import Policy

NEWS_LOOKBACK = timedelta(hours=48)
NEWS_MAX = 40
EVENT_LOOKBACK = timedelta(hours=24)
EVENT_HORIZON = timedelta(days=7)
MACRO_CHANGE_OBS = 20
OWN_FREEZE_REASONS = frozenset({"no_data", "stale", "market_closed", "future_data"})


# ------------------------------------------------------------------------------------ freshness
def closed_day(day: date, asset_class: str) -> bool:
    """A day that does not age a non-crypto daily bar: Saturday, Sunday or a US exchange holiday."""
    if asset_class == "crypto":
        return False
    return day.weekday() >= 5 or day in clock.US_HOLIDAYS_2026


def effective_age_h(available_at: datetime, asof: datetime, *, asset_class: str) -> float:
    """Hours from `available_at` to `asof`, not counting closed days (UTC dates) for non-crypto."""
    start, end = to_utc(available_at).to_pydatetime(), to_utc(asof).to_pydatetime()
    raw = (end - start).total_seconds()
    if raw <= 0 or asset_class == "crypto":
        return raw / 3600.0
    skipped = 0.0
    day = start.date()
    while day <= end.date():
        if closed_day(day, asset_class):
            lo = max(start, datetime.combine(day, time(0), tzinfo=UTC))
            hi = min(end, datetime.combine(day + timedelta(days=1), time(0), tzinfo=UTC))
            skipped += max(0.0, (hi - lo).total_seconds())
        day += timedelta(days=1)
    return (raw - skipped) / 3600.0


def is_stale(available_at: datetime, asof: datetime, *, asset_class: str, max_h: float) -> bool:
    """Rule R18 (daily bars): stale when the effective age exceeds max_h hours."""
    return effective_age_h(available_at, asof, asset_class=asset_class) > max_h


# ------------------------------------------------------------------------------------ facts
_MARKET_SPECS: tuple[tuple[str, str, str, int], ...] = (
    # (id field, MarketState attribute, unit, digits)
    ("dist_sma50", "dist_sma50_pct", "pct", 3),
    ("dist_sma200", "dist_sma200_pct", "pct", 3),
    ("mom10d", "mom10d_pct", "pct", 3),
    ("mom63d", "mom63d_pct", "pct", 3),
    ("dd52", "dd52_pct", "pct", 3),
    ("ret1d_sigma", "ret1d_sigma", "sigma", 2),
    ("data_age_h", "data_age_h", "hours", 1),
)
_VOL_SPECS: tuple[tuple[str, str, str, float, int], ...] = (
    # (id field, MarketState attribute, unit, scale, digits)
    ("sigma_ann", "sigma_ann", "pct", 100.0, 2),
    ("vol_ratio", "vol_ratio_1y", "ratio", 1.0, 3),
    ("ewma5_60", "ewma5_60_ratio", "ratio", 1.0, 3),
)


def market_facts(state: MarketState, *, available_at: datetime, slot: datetime) -> list[Fact]:
    """F:/V: facts for one line (None fields are skipped). market_open is a slot-time fact."""
    sym, source = state.symbol, state.history_source or "unknown"
    facts: list[Fact] = []

    def add(fid: str, kind: str, value: float | str | bool, unit: str, at: datetime, src: str) -> None:
        facts.append(
            Fact(id=fid, kind=kind, symbol=sym, value=value, unit=unit, available_at=at, source=src)  # type: ignore[arg-type]
        )

    if state.trend is not None:
        add(fact_id(sym, "trend"), "market", state.trend, "state", available_at, source)
    for field, attr, unit, digits in _MARKET_SPECS:
        value = getattr(state, attr)
        if value is not None and math.isfinite(value):
            add(fact_id(sym, field), "market", round(float(value), digits), unit, available_at, source)
    add(fact_id(sym, "market_open"), "market", bool(state.market_open), "state", slot, "clock")
    for field, attr, unit, scale, digits in _VOL_SPECS:
        value = getattr(state, attr)
        if value is not None and math.isfinite(value):
            add(vol_id(sym, field), "vol", round(float(value) * scale, digits), unit, available_at, source)
    return facts


def macro_facts(macro: Mapping[str, pd.Series], *, slot: datetime) -> tuple[list[Fact], list[str]]:
    """M: facts from the newest available observation of each series, plus a 20-observation
    change (bps for rates, % for indices). Licensed and unknown series carry
    `publishable=False` (fred.is_publishable fails closed); the source stays "fred"."""
    facts: list[Fact] = []
    flags: list[str] = []
    for sid in sorted(macro):
        series = macro[sid]
        if series is None or series.empty:
            flags.append(f"macro_missing:{sid}")
            continue
        usable = fred.available_series(series.dropna().sort_index(), slot)
        if usable.empty:
            flags.append(f"macro_missing:{sid}")
            continue
        day = pd.Timestamp(usable.index[-1]).date()
        value = float(usable.iloc[-1])
        at = fred.available_at(day)
        source = "fred"
        publishable = fred.is_publishable(sid)
        kind = fred.series_kind(sid)
        if kind in ("rate", "vol"):
            facts.append(
                Fact(id=macro_id(sid, day), kind="macro", value=round(value, 3), unit="pct",
                     available_at=at, source=source, publishable=publishable)
            )
        if len(usable) > MACRO_CHANGE_OBS:
            prev = float(usable.iloc[-1 - MACRO_CHANGE_OBS])
            if kind == "rate":
                facts.append(
                    Fact(id=macro_id(f"{sid}.chg20", day), kind="macro",
                         value=round((value - prev) * 100.0, 1), unit="bps",
                         available_at=at, source=source, publishable=publishable)
                )
            elif kind == "index" and prev > 0:
                facts.append(
                    Fact(id=macro_id(f"{sid}.chg20", day), kind="macro",
                         value=round((value / prev - 1.0) * 100.0, 3), unit="pct",
                         available_at=at, source=source, publishable=publishable)
                )
        elif kind == "index":
            flags.append(f"macro_short:{sid}")
    return facts, flags


# ------------------------------------------------------------------------------------ admission
def admissible_news(news: Iterable[NewsItem], slot: datetime) -> list[NewsItem]:
    """News available strictly before the slot and at most 48 h old: newest 40, unique by ID."""
    lo = slot - NEWS_LOOKBACK
    unique: dict[str, NewsItem] = {}
    for item in sort_news(n for n in news if lo <= to_utc(n.available_at) < slot):
        unique.setdefault(item.id, item)
    return list(unique.values())[:NEWS_MAX]


def admissible_events(
    events: Iterable[EventItem], slot: datetime, known_at: Mapping[str, datetime] | None = None
) -> list[EventItem]:
    """Scheduled events inside [slot - 24 h, slot + 7 d] whose schedule was known by the slot
    (the event's own `known_at` first, else `known_at[event.id]`; unknown = public in advance)."""
    lo, hi = slot - EVENT_LOOKBACK, slot + EVENT_HORIZON
    known = known_at or {}
    unique: dict[str, EventItem] = {}
    for event in sorted(events, key=lambda e: (to_utc(e.at_utc), e.id)):
        when = to_utc(event.at_utc)
        if not lo <= when <= hi:
            continue
        seen = event.known_at if event.known_at is not None else known.get(event.id)
        if seen is not None and to_utc(seen) > slot:
            continue
        unique.setdefault(event.id, event)
    return list(unique.values())


def admissible_costs(cost_facts: Iterable[Fact], slot: datetime) -> list[Fact]:
    """Cost facts known by the slot; anything that is not a C: cost fact is a bug and raises."""
    out = []
    for fact in cost_facts:
        if not fact.id.startswith("C:") or fact.kind != "cost":
            raise ValueError(f"not a cost fact: {fact.id}")
        if to_utc(fact.available_at) <= slot:
            out.append(fact)
    return out


_COST_FIELDS: tuple[tuple[str, str, int], ...] = (
    # (quote attribute = id field, unit, digits)
    ("per_side_bps", "bps", 2),
    ("carry_bps_day", "bps_day", 3),
)


def _quote_attr(quote: object, name: str) -> object:
    return quote.get(name) if isinstance(quote, Mapping) else getattr(quote, name, None)


def _finite(raw: object) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def cost_facts_from_quotes(quotes: Mapping[str, object], *, slot: datetime) -> list[Fact]:
    """C:<line>:per_side_bps (bps) and C:<line>:carry_bps_day (bps_day) facts from the cycle's
    floored cost quotes, keyed by LINE (a CostQuote, or any object or mapping with those fields).

    Every fact is available at the slot. Missing or non-finite values are skipped; a key that is
    not a line name raises. The source says what set the price when the quote knows it:
    "costs:floor" only for a pure policy quote (floored and no broker what-if at all: its carry
    and spread are then the public floors), "costs:whatif" when a broker what-if took part (a
    floored spread can still carry the broker's overnight cost), else "costs"."""
    at = to_utc(slot).to_pydatetime()
    facts: list[Fact] = []
    for line in sorted(quotes, key=str):
        if not isinstance(line, str):
            raise ValueError(f"cost quotes must be keyed by line, got {line!r}")
        quote = quotes[line]
        if quote is None:
            continue
        floor = _quote_attr(quote, "floor_applied")
        pure_policy = bool(floor) and _quote_attr(quote, "what_if_bps") is None
        source = "costs" if floor is None else ("costs:floor" if pure_policy else "costs:whatif")
        for field, unit, digits in _COST_FIELDS:
            value = _finite(_quote_attr(quote, field))
            if value is not None:
                facts.append(Fact(id=cost_id(line, field), kind="cost", symbol=line,
                                  value=round(value, digits), unit=unit,  # type: ignore[arg-type]
                                  available_at=at, source=source))
    return facts


def _bar_time(sym: str, state: MarketState, slot: datetime, given: Mapping[str, datetime] | None) -> datetime | None:
    """Newest bar's availability: the state's `bar_available_at`, else from `given`, else
    reconstructed as slot - data_age_h (rounded to the minute; valid because states are computed
    as of the slot)."""
    if state.bar_available_at is not None:
        return to_utc(state.bar_available_at).to_pydatetime()
    if given and given.get(sym) is not None:
        return to_utc(given[sym]).to_pydatetime()
    if state.data_age_h is None:
        return None
    stamp = pd.Timestamp(slot - timedelta(hours=state.data_age_h)).round("min")
    return min(stamp.to_pydatetime(), slot)


# ------------------------------------------------------------------------------------ builder
def build_fact_pack(
    *,
    cycle_id: str,
    slot: datetime,
    now: datetime,
    policy: Policy,
    states: Mapping[str, MarketState],
    news: Iterable[NewsItem] = (),
    events: Iterable[EventItem] = (),
    macro: Mapping[str, pd.Series] | None = None,
    cost_facts: Iterable[Fact] = (),
    quality_flags: Iterable[str] = (),
    bar_available_at: Mapping[str, datetime] | None = None,
    event_known_at: Mapping[str, datetime] | None = None,
) -> FactPack:
    """Assemble and seal the cycle's FactPack (see the module docstring for every rule)."""
    slot_t = to_utc(slot).to_pydatetime()
    max_h = float(policy.risk["freshness"]["daily_bar_max_h"])
    flags: set[str] = set(quality_flags)
    universe = policy.universe.by_symbol()
    flags.update(f"unknown_state:{sym}" for sym in states if sym not in universe)

    out_states: dict[str, MarketState] = {}
    facts: list[Fact] = []
    admitted: list[str] = []
    frozen: list[str] = []
    for line in policy.universe.lines:
        sym = line.symbol
        state = states.get(sym)
        if state is None:
            flags.add(f"missing_state:{sym}")
            state = MarketState(symbol=sym, asset_class=line.asset_class)
        upstream = [r for r in (state.frozen_reason or "").split(",") if r and r not in OWN_FREEZE_REASONS]
        if state.frozen and not state.frozen_reason:
            upstream.append("upstream")
        reasons = list(upstream)
        bar_at = _bar_time(sym, state, slot_t, bar_available_at)
        if bar_at is not None and bar_at > slot_t:
            reasons.append("future_data")
            bar_at = None
        elif bar_at is None or state.trend is None or state.sigma_ann is None:
            reasons.append("no_data")
        elif is_stale(bar_at, slot_t, asset_class=line.asset_class, max_h=max_h):
            reasons.append("stale")
        is_open = clock.market_open(line.asset_class, slot_t, line.session)
        if not is_open:
            reasons.append("market_closed")
        update: dict[str, object] = {
            "asset_class": line.asset_class,
            "market_open": is_open,
            "frozen": bool(reasons),
            "frozen_reason": ",".join(dict.fromkeys(reasons)) or None,
        }
        if bar_at is not None:
            update["data_age_h"] = round((slot_t - bar_at).total_seconds() / 3600.0, 3)
            update["bar_available_at"] = bar_at
        elif "future_data" in reasons:
            update["data_age_h"] = None
            update["bar_available_at"] = None
        new_state = state.model_copy(update=update)
        out_states[sym] = new_state
        if bar_at is not None:
            facts.extend(market_facts(new_state, available_at=bar_at, slot=slot_t))
        (frozen if new_state.frozen else admitted).append(sym)

    macro_list, macro_flags = macro_facts(macro or {}, slot=slot_t)
    facts.extend(macro_list)
    flags.update(macro_flags)
    facts.extend(admissible_costs(cost_facts, slot_t))

    ids = [f.id for f in facts]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"duplicate fact ids: {dupes}")

    pack = FactPack(
        cycle_id=cycle_id,
        slot=slot_t,
        created_at=to_utc(now).to_pydatetime(),
        admitted=admitted,
        states=out_states,
        facts=sorted(facts, key=lambda f: f.id),
        news=admissible_news(news, slot_t),
        events=admissible_events(events, slot_t, event_known_at),
        quality_flags=sorted(flags),
        frozen=frozen,
    )
    return pack.sealed()
