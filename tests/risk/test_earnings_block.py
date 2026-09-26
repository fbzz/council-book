"""WP-H, the rule half: R16 with stocks (design §10, D12). Stock lines join the macro-sensitive
classes; an earnings event blocks adds only on the stock line it names; a known report time blocks
from 24 h before to the later of 30 h after and the first completed daily bar after it; an
estimated date blocks ± 5 US trading days around it; R16 never forces a sale. On the re-based
sleeve fixture policy (`invariants.STOCK_SLEEVE_LIVE` stays False). Synthetic numbers only."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from council.models.facts import EventItem
from council.models.risk import Band
from council.risk.authority import compute_bands
from council.risk.churn import (
    EARNINGS_ESTIMATE_SOURCE,
    event_applies,
    event_block,
    event_window,
    reaction_bar_at,
    shift_us_trading_days,
)
from council.risk.config import risk_limits
from council.risk.engine import RiskEngine
from council.runtime import engine_quotes, floor_cost_quotes
from tests.risk.helpers import NOW, row, snapshot, state

NY = ZoneInfo("America/New_York")


def earnings(symbol: str, at: datetime, source: str = "sec_8k") -> EventItem:
    return EventItem(id=f"E:earnings:{symbol}@{at.date().isoformat()}", kind="earnings", at_utc=at,
                     symbols=[symbol] if symbol else [], severity=2, source=source)


def fomc(at: datetime) -> EventItem:
    return EventItem(id="E:fomc@x", kind="fomc", at_utc=at, severity=3, source="policy_calendar")


def ny(*args: int) -> datetime:
    return datetime(*args, tzinfo=NY).astimezone(UTC)


# ------------------------------------------------------------------------------------ config


def test_the_policy_numbers(sleeve_policy):
    cfg = risk_limits(sleeve_policy).event_block
    assert (cfg.macro_before_h, cfg.macro_after_h) == (24, 2)
    assert (cfg.earnings_before_h, cfg.earnings_after_h, cfg.earnings_estimate_window_days) == (24, 30, 5)


# ------------------------------------------------------------------------------------ scope


def test_stocks_are_macro_sensitive(sleeve_policy):
    tsta = sleeve_policy.universe.by_symbol()["TSTA"]
    assert event_block(tsta, [fomc(NOW + timedelta(hours=23))], NOW, sleeve_policy)
    assert not event_block(tsta, [fomc(NOW + timedelta(hours=25))], NOW, sleeve_policy)


def test_earnings_are_per_symbol_and_only_on_stock_lines(sleeve_policy):
    lines = sleeve_policy.universe.by_symbol()
    soon = NOW + timedelta(hours=6)
    event = earnings("TSTA", soon)
    assert event_block(lines["TSTA"], [event], NOW, sleeve_policy)
    for other in ("TSTB", "TSTC_B", "F", "TSTD", "NDX", "SEMIS", "SPX", "BTC"):
        assert not event_block(lines[other], [event], NOW, sleeve_policy), other
    assert not event_applies(lines["TSTA"], earnings("", soon))           # never market-wide
    assert not event_block(lines["NDX"], [earnings("NDX", soon)], NOW, sleeve_policy)
    assert event_applies(lines["TSTC_B"], earnings("TSTC_B", soon))


# ------------------------------------------------------------------------------------ windows


def test_a_known_report_blocks_24h_before_to_30h_after(sleeve_policy):
    tsta = sleeve_policy.universe.by_symbol()["TSTA"]
    at = ny(2026, 10, 27, 16, 5)                                          # Tuesday after the close
    event = earnings("TSTA", at)
    assert event_window(event, sleeve_policy) == (at - timedelta(hours=24), at + timedelta(hours=30))
    assert event_block(tsta, [event], at - timedelta(hours=24), sleeve_policy)
    assert not event_block(tsta, [event], at - timedelta(hours=24, minutes=1), sleeve_policy)
    assert event_block(tsta, [event], at + timedelta(hours=30), sleeve_policy)
    assert not event_block(tsta, [event], at + timedelta(hours=30, minutes=1), sleeve_policy)


def test_the_window_lasts_until_the_reaction_bar_is_usable(sleeve_policy):
    friday = ny(2026, 10, 30, 16, 5)                                      # reacted to on Monday
    assert reaction_bar_at(friday) == ny(2026, 11, 2, 20, 0)
    tsta = sleeve_policy.universe.by_symbol()["TSTA"]
    event = earnings("TSTA", friday)
    assert event_window(event, sleeve_policy)[1] == ny(2026, 11, 2, 20, 0)
    assert event_block(tsta, [event], datetime(2026, 11, 2, 18, 40, tzinfo=UTC), sleeve_policy)
    assert not event_block(tsta, [event], datetime(2026, 11, 3, 2, 40, tzinfo=UTC), sleeve_policy)
    before_open = ny(2026, 10, 28, 7, 0)                                  # the same day's bar, then 30 h
    assert reaction_bar_at(before_open) == ny(2026, 10, 28, 20, 0)
    assert event_window(earnings("TSTA", before_open), sleeve_policy)[1] == before_open + timedelta(hours=30)
    holiday_eve = ny(2026, 11, 25, 16, 5)                                 # Thanksgiving: Friday's half day reacts
    assert reaction_bar_at(holiday_eve) == ny(2026, 11, 27, 20, 0)


def test_us_trading_days_skip_weekends_and_holidays():
    assert shift_us_trading_days(date(2026, 10, 29), -5) == date(2026, 10, 22)
    assert shift_us_trading_days(date(2026, 10, 29), 5) == date(2026, 11, 5)
    assert shift_us_trading_days(date(2026, 11, 30), -5) == date(2026, 11, 20)   # Thanksgiving skipped
    assert shift_us_trading_days(date(2026, 11, 30), 0) == date(2026, 11, 30)


def test_an_estimated_date_blocks_five_trading_days_either_side(sleeve_policy):
    tsta = sleeve_policy.universe.by_symbol()["TSTA"]
    estimate = earnings("TSTA", ny(2026, 10, 29, 16, 5), EARNINGS_ESTIMATE_SOURCE)
    start, end = event_window(estimate, sleeve_policy)
    assert start == ny(2026, 10, 21, 0, 0)                                # 00:00 NY on 10-22, less 24 h
    assert end == ny(2026, 11, 7, 6, 0)                                   # the end of 11-05, plus 30 h
    assert not event_block(tsta, [estimate], start - timedelta(minutes=1), sleeve_policy)
    for day in range(21, 32):                                            # every slot of the window
        assert event_block(tsta, [estimate], datetime(2026, 10, day, 14, 40, tzinfo=UTC), sleeve_policy)
    assert event_block(tsta, [estimate], end, sleeve_policy)
    assert not event_block(tsta, [estimate], end + timedelta(minutes=1), sleeve_policy)
    known = earnings("TSTA", ny(2026, 10, 29, 16, 5))                     # the same date, confirmed
    assert event_window(known, sleeve_policy)[0] == ny(2026, 10, 28, 16, 5)


# ------------------------------------------------------------------------------------ engine


def _evaluate(pol, *, current, ref, events, levels=None, bands=None):
    units = {ln.symbol: ln.base_weight for ln in pol.universe.lines}
    states = {ln.symbol: state(ln.symbol, ln.asset_class, **({"sigma_ann": 0.20} if ln.asset_class == "stock" else {}))
              for ln in pol.universe.lines}
    bands = bands or {s: Band(symbol=s, trend="up", ref_level=v, lo=min(v, 0.0), hi=max(v, 1.0))
                      for s, v in ref.items()}
    return RiskEngine(pol).evaluate(
        levels=levels or dict(ref), ref=ref, bands=bands, states=states, snapshot=snapshot(current),
        unit_weights=units, kill_state="NORMAL",
        cost_quotes=engine_quotes(floor_cost_quotes(pol, quoted_at=NOW, fee_bps=0.0)), events=events,
        last_change={}, turnover_7d=0.0, material_changed=True, basis="council", now=NOW,
        held_levels=None)


@pytest.fixture(scope="module")
def book(sleeve_policy):
    units = {ln.symbol: ln.base_weight for ln in sleeve_policy.universe.lines}
    ref = {ln.symbol: (1.0 if ln.in_reference else 0.0) for ln in sleeve_policy.universe.lines}
    current = {s: units[s] * ref[s] for s in ref}          # everything at the reference ...
    current["TSTA"] = 0.0                                   # ... except TSTA, not bought yet
    return units, ref, current


def test_an_earnings_window_blocks_the_add_and_never_forces_a_sale(sleeve_policy, book):
    units, ref, current = book
    window = [earnings("TSTA", NOW + timedelta(days=2), EARNINGS_ESTIMATE_SOURCE),
              earnings("TSTB", NOW + timedelta(days=1), EARNINGS_ESTIMATE_SOURCE)]
    free = _evaluate(sleeve_policy, current=current, ref=ref, events=[])
    assert free.final_w["TSTA"] == pytest.approx(units["TSTA"])          # without the window: bought
    held = _evaluate(sleeve_policy, current=current, ref=ref, events=window)
    assert held.final_w["TSTA"] == 0.0                                   # no add inside its window
    assert any(r.startswith("TSTA:") and "R16 event window" in r for r in held.hold_reasons)
    assert held.final_w["TSTB"] == pytest.approx(current["TSTB"])        # held through its window: no sale
    assert held.final_w["TSTC_B"] == pytest.approx(current["TSTC_B"])
    assert row(held, "R16", "event_block").passed


def test_a_sale_inside_the_window_still_executes(sleeve_policy, book):
    units, ref, current = book
    window = [earnings("TSTB", NOW + timedelta(days=1), EARNINGS_ESTIMATE_SOURCE)]
    rotated = {**ref, "TSTB": 0.0}                                        # the rule sells TSTB
    sold = _evaluate(sleeve_policy, current=current, ref=rotated, events=window)
    assert sold.final_w["TSTB"] == 0.0 and row(sold, "R16", "event_block").passed


def test_the_bands_give_the_council_no_add_on_a_stock_in_its_window(sleeve_policy, book):
    units, ref, current = book
    events = [earnings("TSTA", NOW + timedelta(days=2), EARNINGS_ESTIMATE_SOURCE)]
    lines = sleeve_policy.universe.lines
    blocked = {ln.symbol for ln in lines if event_block(ln, events, NOW, sleeve_policy)}   # as the cycle does
    assert blocked == {"TSTA"}
    states = {ln.symbol: state(ln.symbol, ln.asset_class) for ln in lines}
    levels = {s: (w / units[s] if units[s] else 0.0) for s, w in current.items()}
    bands = compute_bands(lines=sleeve_policy.universe, ref=ref, states=states, cards=[], current_levels=levels,
                          kill_state="NORMAL", event_blocked=blocked, lever_ok=set(), short_ok=set(),
                          policy=sleeve_policy)
    assert (bands["TSTA"].lo, bands["TSTA"].hi) == (0.0, 0.0) and "event window: no adds" in bands["TSTA"].reasons
    assert bands["TSTD"].hi > 0.0                                         # a shortlist name outside any window
