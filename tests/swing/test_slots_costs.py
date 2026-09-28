"""SW-1: the swing-slot gate (DST-aware) and the private round-trip cost (design §3.3, §4.1)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from council.swing import costs
from council.swing.slots import is_swing_slot, season_of, swing_slot


@pytest.fixture(scope="module")
def slots(policy):
    return policy.swing.slots


def _utc(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


def test_winter_1440_refused_1840_allowed(slots):
    # 2026-11-02 (Monday): New York left DST on 1 Nov; the session opens 14:30 UTC.
    assert swing_slot(_utc(2026, 11, 2, 14, 40), slots).reason == "not_a_swing_slot"
    assert is_swing_slot(_utc(2026, 11, 2, 18, 40), slots)
    assert season_of(_utc(2026, 11, 2, 18, 40)) == "winter"


def test_summer_both_slots(slots):
    # 2026-09-29 (Tuesday), DST: open 13:30 UTC, close 20:00 UTC.
    assert is_swing_slot(_utc(2026, 9, 29, 14, 40), slots)
    assert is_swing_slot(_utc(2026, 9, 29, 18, 40), slots)
    assert not is_swing_slot(_utc(2026, 9, 29, 22, 40), slots)


def test_session_age_and_close_gates():
    from council.swing.policy import Slots
    tight = Slots(summer_utc=["13:40", "19:40"], winter_utc=["18:40"], min_session_age_min=60,
                  min_to_close_min=30)
    assert swing_slot(_utc(2026, 9, 29, 13, 40), tight).reason == "session_too_young"
    assert swing_slot(_utc(2026, 9, 29, 19, 40), tight).reason == "too_close_to_close"


def test_holiday_weekend_early_close_and_naive(slots):
    assert swing_slot(_utc(2026, 11, 26, 18, 40), slots).reason == "us_closed"      # Thanksgiving
    assert swing_slot(_utc(2026, 11, 28, 18, 40), slots).reason == "us_closed"      # Saturday
    assert swing_slot(_utc(2026, 11, 27, 18, 40), slots).reason == "us_closed"      # 13:00 NY close
    assert swing_slot(datetime(2026, 11, 2, 18, 40), slots).reason == "naive_time"
    assert swing_slot(_utc(2028, 1, 4, 18, 40), slots).reason == "calendar_unknown"


def _account(tmp_path, value) -> costs.SwingAccount | None:
    (tmp_path / "account").mkdir(parents=True, exist_ok=True)
    (tmp_path / "account" / "swing.json").write_text(json.dumps({"funded_real_nav_usd": value}))
    return costs.load_account(tmp_path)


def test_account_loader_fails_closed(tmp_path):
    assert costs.load_account(tmp_path) is None
    assert _account(tmp_path, "2000") is None
    assert _account(tmp_path, True) is None
    assert _account(tmp_path, -1) is None
    acct = _account(tmp_path, 2000)
    assert acct is not None and "2000" not in repr(acct)


def test_worked_example_matches_design(policy, tmp_path):
    cfg = costs.CostConfig.from_policy(policy)
    acct = _account(tmp_path, 2000)
    rt = costs.round_trip("long", size_nav=0.08, real_nav_usd=2000, virtual_nav_usd=10_000, account=acct,
                          cfg=cfg, entry_day=date(2026, 9, 29), time_stop_sessions=15)
    assert rt.fee_pct == pytest.approx(200 / 120, rel=1e-9)          # 1.67 %
    assert rt.virtual_fee_pct == pytest.approx(0.25)
    assert rt.spread_pct == pytest.approx(0.20)
    assert rt.carry_pct == 0.0
    assert rt.total_pct == pytest.approx(2.1167, abs=1e-3)
    assert "fee_pct" not in repr(rt)
    # 5 % stop: target needs >= 10.66 %.
    assert costs.econ_ok(rt.total_pct, 5.0, 10.7, min_cost_mult=3.0, min_net_rr=1.2)
    assert not costs.econ_ok(rt.total_pct, 5.0, 10.6, min_cost_mult=3.0, min_net_rr=1.2)
    assert costs.econ_label(False) == "econ: target too small"


def test_three_nav_gate_outcomes_identical(policy, tmp_path):
    cfg = costs.CostConfig.from_policy(policy)
    acct = _account(tmp_path, 2000)
    grid = [(s, t) for s in (2.0, 3.0, 5.0, 8.0) for t in (4.0, 6.0, 8.3, 10.7, 12.0, 20.0)]

    def pattern(real_nav: float) -> list[bool]:
        rt = costs.round_trip("long", size_nav=0.08, real_nav_usd=real_nav, virtual_nav_usd=10_000,
                              account=acct, cfg=cfg, entry_day=date(2026, 9, 29), time_stop_sessions=15)
        return [costs.econ_ok(rt.total_pct, s, t, min_cost_mult=3.0, min_net_rr=1.2) for s, t in grid]

    assert pattern(1500) == pattern(2000) == pattern(20_000)
    assert any(pattern(2000)) and not all(pattern(2000))


def test_short_carry_counts_weekends_and_holidays(policy, tmp_path):
    cfg = costs.CostConfig.from_policy(policy)
    # Friday entry, 1 session -> Monday: one weekend night charged x3.
    assert costs.carry_nights(date(2026, 10, 2), 1, weekend_multiplier=3) == 3
    # Wednesday 25 Nov 2026, 1 session -> Friday 27 (Thanksgiving skipped): x3.
    assert costs.carry_nights(date(2026, 11, 25), 1, weekend_multiplier=3) == 3
    # 5 sessions Mon->Mon: 4 weeknights + 1 weekend.
    assert costs.carry_nights(date(2026, 10, 5), 5, weekend_multiplier=3) == 7
    acct = _account(tmp_path, 2000)
    long_ = costs.round_trip("long", size_nav=0.08, real_nav_usd=2000, virtual_nav_usd=10_000, account=acct,
                             cfg=cfg, entry_day=date(2026, 10, 5), time_stop_sessions=15)
    short = costs.round_trip("short", size_nav=0.08, real_nav_usd=2000, virtual_nav_usd=10_000, account=acct,
                             cfg=cfg, entry_day=date(2026, 10, 5), time_stop_sessions=15)
    assert 0.1 < short.carry_pct < 0.6 and short.total_pct > long_.total_pct
    wider = costs.round_trip("short", size_nav=0.08, real_nav_usd=2000, virtual_nav_usd=10_000, account=acct,
                             cfg=cfg, entry_day=date(2026, 10, 5), time_stop_sessions=15,
                             spread_bps_side=25, carry_bps_day=5)
    assert wider.spread_pct == pytest.approx(0.5) and wider.carry_pct > short.carry_pct


def test_no_account_or_bad_inputs_raise(policy, tmp_path):
    cfg = costs.CostConfig.from_policy(policy)
    with pytest.raises(costs.CostUnavailable):
        costs.round_trip("long", size_nav=0.08, real_nav_usd=2000, virtual_nav_usd=10_000, account=None,
                         cfg=cfg, entry_day=date(2026, 10, 5), time_stop_sessions=15)
    with pytest.raises(costs.CostUnavailable):
        costs.round_trip("long", size_nav=0.0, real_nav_usd=2000, virtual_nav_usd=10_000,
                         account=_account(tmp_path, 2000), cfg=cfg, entry_day=date(2026, 10, 5),
                         time_stop_sessions=15)


def test_declared_cost(policy):
    assert costs.declared_rt_pct(policy.swing.public_record.declared_cost_pct_per_leg) == 2.5
