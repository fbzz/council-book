from datetime import UTC, datetime, timedelta

import pytest

from council import clock
from council.models.common import snap_level
from council.models.facts import FactPack, MarketState
from council.settings import Settings, SettingsError


def test_policy_loads_and_hashes(policy):
    syms = policy.universe.symbols()
    assert syms == ["NDX", "SEMIS", "SPX", "GOLD", "BTC", "ETH", "OIL", "EURUSD", "GBPUSD"]
    assert len(policy.sha256) == 64
    ref = [ln for ln in policy.universe.lines if ln.in_reference]
    assert sum(ln.base_weight for ln in ref) <= policy.universe.reference_gross_max + 1e-9
    assert not policy.universe.by_symbol()["BTC"].council_deviations


def test_invariants_hold_for_shipped_policy(policy):
    from council.invariants import check_policy

    check_policy(policy)


def test_invariants_reject_looser_policy(policy):
    from council.invariants import InvariantViolation, check_policy

    risk = dict(policy.risk)
    risk["gross"] = {**risk["gross"], "hard_max": 2.5}
    with pytest.raises(InvariantViolation):
        check_policy(policy.model_copy(update={"risk": risk}))
    risk = dict(policy.risk)
    risk["killswitch"] = {**risk["killswitch"], "halt_at": 0.6}
    with pytest.raises(InvariantViolation):
        check_policy(policy.model_copy(update={"risk": risk}))


def test_slot_grid_is_utc_and_4h():
    ts = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
    info = clock.classify(ts)
    assert info.slot == datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
    assert info.status == "late" and info.cycle_id == "2026-10-01T1440Z"
    assert clock.next_slot(ts) - info.slot == timedelta(hours=4)
    assert clock.classify(datetime(2026, 10, 1, 17, 30, tzinfo=UTC)).status == "missed"
    assert clock.proposal_valid_until(info.slot) == datetime(2026, 10, 1, 18, 35, tzinfo=UTC)


def test_slot_before_first_slot_of_day_uses_previous_day():
    info = clock.classify(datetime(2026, 10, 2, 1, 0, tzinfo=UTC))
    assert info.slot == datetime(2026, 10, 1, 22, 40, tzinfo=UTC)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        clock.slot_at_or_before(datetime(2026, 10, 1, 14, 40))


def test_market_sessions():
    sat = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)
    assert clock.market_open("crypto", sat)
    assert not clock.market_open("etf", sat)
    assert not clock.market_open("fx", sat)
    weekday_ny_open = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)  # 11:00 New York
    assert clock.market_open("stock", weekday_ny_open)


def test_snap_level():
    assert snap_level(0.6) == 0.5
    assert snap_level(0.13) == 0.25
    assert snap_level(0.125) == 0.0  # tie goes toward zero
    assert snap_level(-0.9) == -0.5
    assert snap_level(1.4) == 1.5


def test_pack_hash_ignores_created_at(slot):
    st = {"QQQ": MarketState(symbol="QQQ", asset_class="etf", trend="up")}
    a = FactPack(cycle_id="x", slot=slot, created_at=slot, admitted=["QQQ"], states=st).sealed()
    b = FactPack(cycle_id="x", slot=slot, created_at=slot + timedelta(minutes=3), admitted=["QQQ"], states=st).sealed()
    assert a.input_hash == b.input_hash


def test_settings_refuse_lab_keys(monkeypatch):
    monkeypatch.setenv("ETORO_USER_KEY", "x")
    with pytest.raises(SettingsError):
        Settings.from_env()


def test_sessions_follow_the_preferred_vehicle(policy):
    by = policy.universe.by_symbol()
    assert by["SEMIS"].session == "lse" and by["NDX"].session == "lse"
    assert by["BTC"].session == "crypto" and by["OIL"].session == "fx24x5"
    london_morning = datetime(2026, 10, 1, 10, 40, tzinfo=UTC)   # 11:40 London, 06:40 New York
    assert clock.market_open("etf", london_morning, "lse")
    assert not clock.market_open("etf", london_morning, "us")
    evening = datetime(2026, 10, 1, 18, 40, tzinfo=UTC)          # London closed at 16:30
    assert not clock.market_open("etf", evening, "lse")
