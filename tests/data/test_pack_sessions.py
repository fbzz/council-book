"""Session freezing in the fact pack: early closes, half days and the calendar horizon (WP-F)."""

from __future__ import annotations

from datetime import timedelta

from council.clock import cycle_id_for
from council.facts.features import market_states
from council.facts.pack import build_fact_pack, session_admits
from council.policy import Vehicle, Vehicles
from tests.data.synth import universe_history, utc


def _pack(policy, slot, end):
    history = universe_history(policy, end)
    return build_fact_pack(cycle_id=cycle_id_for(slot), slot=slot, now=slot + timedelta(minutes=2),
                           policy=policy, states=market_states(policy, history, now=slot))


def _us_semis(policy):
    """SEMIS traded through its US ETF CFD (session "us"), as when the UCITS is not eligible."""
    lines = []
    for line in policy.universe.lines:
        if line.symbol == "SEMIS":
            line = line.model_copy(update={"vehicles": Vehicles(
                long=[Vehicle(symbol="SOXX", settlement="cfd")], short=line.vehicles.short)})
        lines.append(line)
    return policy.model_copy(update={"universe": policy.universe.model_copy(update={"lines": lines})})


def test_lse_half_day_freezes_london_lines_at_1440(policy):
    pack = _pack(policy, utc(2026, 12, 24, 14, 40), "2026-12-24")      # LSE closed at 12:30
    assert {"NDX", "SEMIS", "SPX", "GOLD"} <= set(pack.frozen)
    assert all("market_closed" in pack.states[s].frozen_reason for s in ("NDX", "GOLD"))
    normal = _pack(policy, utc(2026, 12, 22, 14, 40), "2026-12-22")    # an ordinary Tuesday
    assert {"NDX", "SEMIS", "SPX", "GOLD"} <= set(normal.admitted)


def test_us_early_close_freezes_us_lines_at_1840_only(policy):
    pol = _us_semis(policy)
    assert pol.universe.by_symbol()["SEMIS"].session == "us"
    late = _pack(pol, utc(2026, 11, 27, 18, 40), "2026-11-27")         # closed at 13:00 New York
    assert "SEMIS" in late.frozen and "market_closed" in late.states["SEMIS"].frozen_reason
    early = _pack(pol, utc(2026, 11, 27, 14, 40), "2026-11-27")
    assert "SEMIS" in early.admitted
    assert session_admits("etf", "us", utc(2026, 11, 27, 14, 40))
    assert not session_admits("etf", "us", utc(2026, 11, 27, 17, 52))  # closes within 10 min


def test_2027_holiday_freezes_and_does_not_age_bars(policy):
    pack = _pack(policy, utc(2027, 3, 29, 10, 40), "2027-03-29")       # Easter Monday: LSE shut
    assert {"NDX", "SEMIS", "SPX", "GOLD"} <= set(pack.frozen)
    assert {"BTC", "ETH"} <= set(pack.admitted)


def test_past_the_calendars_london_lines_freeze_with_a_flag(policy):
    pack = _pack(policy, utc(2028, 1, 4, 10, 40), "2028-01-04")        # an ordinary Tuesday
    assert {"NDX", "SEMIS", "SPX", "GOLD"} <= set(pack.frozen)
    assert "calendar_missing:2028" in pack.quality_flags
    assert {"BTC", "ETH", "OIL", "EURUSD", "GBPUSD"} <= set(pack.admitted)   # no exchange calendar
    inside = _pack(policy, utc(2027, 12, 21, 10, 40), "2027-12-21")
    assert not any(f.startswith("calendar_missing") for f in inside.quality_flags)
