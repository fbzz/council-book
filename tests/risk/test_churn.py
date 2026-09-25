"""R11 deadband, R12 minimum hold, R13 churn budgets, R16 event block, R17 anti-chase and
R4d re-entry cool-off, each at its policy number."""

from __future__ import annotations

from datetime import timedelta

import pytest

from council.models.facts import EventItem
from council.risk.churn import (
    anti_chase_block,
    apply_deadband,
    cycle_increase,
    cycle_increase_ok,
    deadband_ok,
    event_block,
    line_increase,
    min_hold_ok,
    reentry_blocked,
    turnover_ok,
)
from tests.risk.helpers import override, state

# ----------------------------------------------------------------------------- R11


@pytest.mark.parametrize("crypto,step", [(False, 0.25), (True, 0.5)])
def test_deadband_level_step(policy, crypto, step):
    kw = {"to_zero": False, "crypto": crypto, "min_share": 0.0, "policy": policy}
    assert deadband_ok(step, 0.1, **kw)
    assert deadband_ok(-step, -0.1, **kw)
    assert not deadband_ok(step - 0.01, 0.1, **kw)


def test_deadband_min_nav_share_002(policy):
    kw = {"to_zero": False, "crypto": False, "min_share": 0.0, "policy": policy}
    assert deadband_ok(0.5, 0.02, **kw)
    assert not deadband_ok(0.5, 0.019, **kw)
    # the broker minimum as a NAV share can be larger than the policy floor
    assert not deadband_ok(0.5, 0.03, **{**kw, "min_share": 0.05})


def test_close_to_zero_skips_size_floor_not_level_step(policy):
    assert deadband_ok(-0.25, -0.005, to_zero=True, crypto=False, min_share=0.0, policy=policy)
    assert not deadband_ok(-0.2, -0.005, to_zero=True, crypto=False, min_share=0.0, policy=policy)


def test_apply_deadband(policy):
    levels, held = apply_deadband(
        {"NDX": 1.0, "BTC": 0.75, "SPX": 0.5, "GOLD": 0.0},
        {"NDX": 0.75, "BTC": 0.5, "SPX": 0.5, "GOLD": 0.25},
        {"NDX": 0.35, "BTC": 0.13, "SPX": 0.15, "GOLD": 0.02},
        {"NDX": 0.0},
        policy,
        crypto_lines=["BTC"],
    )
    assert levels == {"NDX": 1.0, "BTC": 0.5, "SPX": 0.5, "GOLD": 0.0}
    assert held == ["BTC"]  # crypto needs 0.5; GOLD closes to zero below the size floor
    _, held = apply_deadband({"NDX": 1.0}, {"NDX": 0.75}, {"NDX": 0.35}, 0.2, policy,
                             crypto_lines=[])
    assert held == ["NDX"]  # 0.0875 of NAV is below a 20% broker minimum


# ----------------------------------------------------------------------------- R12


@pytest.mark.parametrize("cls,days", [("index", 3), ("fx", 3), ("crypto", 7)])
def test_min_hold_days(policy, now, cls, days):
    kw = {"sign_flip": False, "toward_reference": False, "asset_class": cls, "policy": policy}
    assert not min_hold_ok("X", now - timedelta(days=days) + timedelta(minutes=1), now, **kw)
    assert min_hold_ok("X", now - timedelta(days=days), now, **kw)
    assert min_hold_ok("X", None, now, **kw)


def test_no_flip_within_24h(policy, now):
    kw = {"toward_reference": False, "asset_class": "index", "policy": policy, "reversal": False}
    assert not min_hold_ok("X", now - timedelta(hours=23), now, sign_flip=True, **kw)
    assert min_hold_ok("X", now - timedelta(hours=24), now, sign_flip=True, **kw)
    assert min_hold_ok("X", now - timedelta(hours=1), now, sign_flip=False, **kw)


def test_toward_reference_exempt_from_min_hold(policy, now):
    kw = {"sign_flip": False, "asset_class": "index", "policy": policy}
    recent = now - timedelta(hours=1)
    assert min_hold_ok("X", recent, now, toward_reference=True, **kw)
    strict = override(policy, "risk", {"min_hold_days.toward_reference_exempt": False})
    assert not min_hold_ok("X", recent, now, toward_reference=True,
                           **{**kw, "policy": strict})


# ----------------------------------------------------------------------------- R17


def test_anti_chase_25_sigma(policy):
    hot = state("NDX", "index", ret1d_sigma=2.6)
    warm = state("NDX", "index", ret1d_sigma=2.5)
    cold = state("NDX", "index", ret1d_sigma=-2.6)
    assert anti_chase_block(hot, True, False, policy)
    assert not anti_chase_block(warm, True, False, policy)
    assert not anti_chase_block(hot, False, True, policy)
    assert anti_chase_block(cold, False, True, policy)
    assert not anti_chase_block(cold, True, False, policy)
    assert not anti_chase_block(state("NDX", "index", ret1d_sigma=None), True, True, policy)


# ----------------------------------------------------------------------------- R16


def _fomc(at, symbols=()):
    return EventItem(id="E:fomc", kind="fomc", at_utc=at, symbols=list(symbols), severity=3,
                     source="calendar")


def test_event_block_24h_before_2h_after(policy, now):
    ndx = policy.universe.by_symbol()["NDX"]
    assert event_block(ndx, [_fomc(now + timedelta(hours=24))], now, policy)
    assert not event_block(ndx, [_fomc(now + timedelta(hours=24, minutes=1))], now, policy)
    assert event_block(ndx, [_fomc(now - timedelta(hours=2))], now, policy)
    assert not event_block(ndx, [_fomc(now - timedelta(hours=2, minutes=1))], now, policy)


def test_event_block_scope(policy, now):
    lines = policy.universe.by_symbol()
    soon = now + timedelta(hours=1)
    for sym in ("NDX", "SEMIS", "GOLD", "BTC", "EURUSD"):
        assert event_block(lines[sym], [_fomc(soon)], now, policy)
    assert not event_block(lines["NDX"], [_fomc(soon, ["SPX"])], now, policy)
    earnings = EventItem(id="E:earn", kind="earnings", at_utc=soon, symbols=["NDX"], severity=2,
                         source="feed")
    assert not event_block(lines["NDX"], [earnings], now, policy)


# ----------------------------------------------------------------------------- R4d


@pytest.mark.parametrize("cls,days", [("crypto", 7), ("index", 3)])
def test_reentry_cooloff(policy, now, cls, days):
    assert reentry_blocked(now - timedelta(days=days) + timedelta(minutes=1), now, cls, policy)
    assert not reentry_blocked(now - timedelta(days=days), now, cls, policy)
    assert not reentry_blocked(None, now, cls, policy)


# ----------------------------------------------------------------------------- R13


def test_line_increase_counts_flips_fully():
    assert line_increase(0.2, 0.5) == pytest.approx(0.3)
    assert line_increase(0.5, 0.2) == 0.0
    assert line_increase(0.2, -0.3) == pytest.approx(0.3)
    assert cycle_increase({"A": 0.1, "B": 0.4}, {"A": 0.5, "B": 0.0, "C": -0.2}) == pytest.approx(0.6)


def test_cycle_increase_060(policy):
    assert cycle_increase_ok(0.60, policy)
    assert not cycle_increase_ok(0.61, policy)


@pytest.mark.parametrize("window,limit", [("7d", 1.0), ("30d", 3.0)])
def test_turnover_budgets(policy, window, limit):
    assert turnover_ok(limit - 0.4, 0.4, window, policy)
    assert not turnover_ok(limit - 0.3, 0.4, window, policy)
