"""Engine seams with the pack and the planner: pack-owned freshness (market_closed never counts in
the R18 frozen share, raw data_age_h is not re-derived), RiskDecision.base_w / changed_lines, and
levered cost quotes keyed (line, direction, leverage). All numbers are synthetic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from council.facts.pack import build_fact_pack
from council.models.risk import Band, changed_lines
from council.risk.engine import data_frozen
from tests.risk.helpers import (
    NOW,
    default_ref,
    default_units,
    loose,
    quote,
    quotes_for,
    row,
    run,
    state,
    states_for,
)

CRYPTO = ("BTC", "ETH")


def _pack_states(policy, slot: datetime, equity_bar: datetime, crypto_bar: datetime):
    """Engine states the way the cycle gets them: synthetic states stamped with realistic bar
    times (raw data_age_h included), passed through the real pack builder at `slot`."""
    raw = {}
    for line in policy.universe.lines:
        at = crypto_bar if line.asset_class == "crypto" else equity_bar
        raw[line.symbol] = state(line.symbol, line.asset_class, bar_available_at=at,
                                 data_age_h=(slot - at).total_seconds() / 3600.0)
    return build_fact_pack(cycle_id="t", slot=slot, now=slot + timedelta(minutes=2),
                           policy=policy, states=raw)


def _toward_reference_from_flat(policy, pack):
    ref = default_ref(policy)
    quotes = {k: q.model_copy(update={"quoted_at": pack.slot}) for k, q in quotes_for(policy).items()}
    return run(policy, states=pack.states, ref=ref, levels=ref, current={}, now=pack.slot,
               cost_quotes=quotes)


def test_monday_slot_with_closed_equity_session_lets_crypto_move(policy):
    """Monday 06:40 UTC: the Friday Tiingo bar is 54.7 h old on the clock (> 30 h) but fresh for
    the pack (weekend skipped); the ETF session is closed. BTC/ETH (and the open index/gold CFDs)
    move toward the reference; only the closed ETF line holds."""
    slot = datetime(2026, 10, 5, 6, 40, tzinfo=UTC)
    pack = _pack_states(policy, slot, equity_bar=datetime(2026, 10, 3, 0, 0, tzinfo=UTC),
                        crypto_bar=datetime(2026, 10, 5, 0, 0, tzinfo=UTC))
    assert pack.states["NDX"].data_age_h > 30 and not pack.states["NDX"].frozen
    assert pack.frozen == ["SEMIS"] and pack.states["SEMIS"].frozen_reason == "market_closed"
    d = _toward_reference_from_flat(policy, pack)
    assert d.final_w["BTC"] == pytest.approx(0.13) and d.final_w["ETH"] == pytest.approx(0.05)
    assert d.final_w["NDX"] == pytest.approx(0.35) and d.final_w["GOLD"] == pytest.approx(0.12)
    assert d.final_w["SEMIS"] == 0.0
    fresh = row(d, "R18", "data_freshness")
    assert fresh.passed and fresh.value == 0.0
    assert any(r.startswith("SEMIS:") for r in d.hold_reasons)            # frozen band: hold
    assert not any("R18" in r for r in d.hold_reasons)


def test_saturday_slot_holds_closed_lines_but_crypto_still_moves(policy):
    """Saturday: every non-crypto line is frozen for market_closed (81% of the reference). That
    is a session freeze, not a data freeze: the R18 share stays 0 and BTC/ETH move."""
    slot = datetime(2026, 10, 3, 14, 40, tzinfo=UTC)
    pack = _pack_states(policy, slot, equity_bar=datetime(2026, 10, 3, 0, 0, tzinfo=UTC),
                        crypto_bar=datetime(2026, 10, 3, 0, 0, tzinfo=UTC))
    assert pack.admitted == ["BTC", "ETH"]
    assert all(pack.states[s].frozen_reason == "market_closed" for s in pack.frozen)
    d = _toward_reference_from_flat(policy, pack)
    assert d.final_w["BTC"] == pytest.approx(0.13) and d.final_w["ETH"] == pytest.approx(0.05)
    assert all(d.final_w[s] == 0.0 for s in d.final_w if s not in CRYPTO)
    assert row(d, "R18", "data_freshness").passed and row(d, "R18", "data_freshness").value == 0.0
    assert row(d, "R19").passed
    assert changed_lines(d) == ["BTC", "ETH"]


def test_data_freezes_still_count_and_mixed_reasons_count(policy):
    ref = default_ref(policy)
    # SPX + SEMIS frozen for data (0.30 / 0.95 > 30%): nothing moves, crypto included
    frozen = {"SEMIS": {"frozen": True, "frozen_reason": "stale,market_closed"},
              "SPX": {"frozen": True, "frozen_reason": "no_data"}}
    d = run(policy, states=states_for(policy, **frozen), ref=ref, levels=ref)
    assert set(d.final_w.values()) == {0.0} and row(d, "R18", "data_freshness").value > 0.30
    assert any("R18 frozen reference share" in r for r in d.hold_reasons)
    # the same lines frozen only for the closed session: held, but crypto and the rest move
    closed = {s: {"frozen": True, "frozen_reason": "market_closed", "market_open": False}
              for s in ("SEMIS", "SPX", "NDX", "GOLD")}
    d = run(policy, states=states_for(policy, **closed), ref=ref, levels=ref)
    assert d.final_w["BTC"] == pytest.approx(0.13) and d.final_w["SPX"] == 0.0
    assert row(d, "R18", "data_freshness").value == 0.0


def test_data_frozen_classification():
    ok = state("NDX", "index")
    assert not data_frozen(ok) and data_frozen(None)
    assert not data_frozen(ok.model_copy(update={"frozen": True, "frozen_reason": "market_closed"}))
    assert data_frozen(ok.model_copy(update={"frozen": True, "frozen_reason": "market_closed,stale"}))
    assert data_frozen(ok.model_copy(update={"frozen": True, "frozen_reason": None}))   # fail closed
    assert data_frozen(ok.model_copy(update={"frozen": True, "frozen_reason": "not_eligible"}))
    assert not data_frozen(ok.model_copy(update={"data_age_h": 90.0}))                   # pack decides


# ----------------------------------------------------------------------------- base_w


def test_base_w_is_the_snapshot_book_and_changed_lines_skip_held_lines(policy):
    pol = loose(policy)
    closed = states_for(pol, SPX={"market_open": False})
    current = {"NDX": 0.20, "SPX": 0.10, "UNMAPPED_7": 0.05}
    ref = default_ref(pol)
    d = run(pol, states=closed, ref=ref, levels=ref, current=current)
    assert d.base_w["NDX"] == 0.20 and d.base_w["SPX"] == 0.10 and d.base_w["UNMAPPED_7"] == 0.05
    assert set(d.base_w) == set(d.final_w) and d.base_w["GOLD"] == 0.0
    changed = changed_lines(d)
    assert "SPX" not in changed and "UNMAPPED_7" not in changed        # held / locked
    assert {"NDX", "GOLD", "BTC"} <= set(changed)
    assert all(abs(d.final_w[s] - d.base_w[s]) > 1e-6 for s in changed)


def test_base_w_zeros_without_a_snapshot_and_before_a_derisk(policy):
    d = run(policy)                                                   # no snapshot: flat
    assert set(d.base_w) == {ln.symbol for ln in policy.universe.lines}
    assert set(d.base_w.values()) == {0.0}
    # R1 de-risk: base_w is the snapshot, not the de-risked compliance base
    units = default_units(policy)
    heavy = {s: 2.0 * u for s, u in units.items()}
    d = run(policy, current=heavy, levels={s: 2.0 for s in units})
    assert d.base_w == pytest.approx(heavy) and d.compliance


def test_flatten_base_w_includes_locked_lines(policy):
    d = run(policy, current={"NDX": 0.3, "UNMAPPED_9": 0.1}, kill_state="HALTED")
    assert d.base_w["NDX"] == 0.3 and d.base_w["UNMAPPED_9"] == 0.1
    assert changed_lines(d) == ["NDX", "UNMAPPED_9"]


# ----------------------------------------------------------------------------- levered quotes


def _lever_case(pol, quotes):
    units = {ln.symbol: ln.base_weight for ln in pol.universe.lines}
    bands = {ln.symbol: Band(symbol=ln.symbol, trend="up", ref_level=0.0, lo=0.0, hi=0.0)
             for ln in pol.universe.lines}
    bands["NDX"] = Band(symbol="NDX", trend="up", ref_level=1.0, lo=1.0, hi=1.25)
    ref = {s: 0.0 for s in units} | {"NDX": 1.0}
    return run(pol, levels={"NDX": 1.25}, ref=ref, bands=bands, unit_weights=units,
               current={"NDX": units["NDX"]}, cost_quotes=quotes)


def test_lever_leg_is_gated_with_the_levered_quote(policy):
    pol = loose(policy)
    cheap = quotes_for(pol)
    ok = _lever_case(pol, cheap)                        # only 1x quotes: fallback, passes
    assert ok.final_w["NDX"] == pytest.approx(1.25 * 0.35)
    dear = dict(cheap)
    dear[("NDX", "long", 2)] = quote("NDX", "long", per_side=500.0, leverage=2)
    d = _lever_case(pol, dear)
    assert d.final_w["NDX"] == pytest.approx(0.35)
    assert any(r.startswith("NDX:") and "R15" in r for r in d.hold_reasons)
    # the levered quote does not price unlevered legs of the same line
    fine = dict(cheap)
    fine[("NDX", "long", 1)] = quote("NDX", "long", per_side=1.0)
    fine[("NDX", "long", 2)] = quote("NDX", "long", per_side=1.0, carry=0.5, leverage=2)
    d = _lever_case(pol, fine)
    assert d.final_w["NDX"] == pytest.approx(1.25 * 0.35)
    assert d.carry_bps_day == pytest.approx(1.25 * 0.35 * 0.5)       # carried at the L=2 rate


def test_line_and_direction_keys_still_work(policy):
    pol = loose(policy)
    by_line = {ln.symbol: quote(ln.symbol, "long") for ln in pol.universe.lines}
    d = run(pol, cost_quotes=by_line, now=NOW)
    assert d.final_w["NDX"] == pytest.approx(0.35) and row(d, "R15").passed


def test_engine_with_book_covariance_vol_fn_and_an_unmapped_position(policy):
    """The cycle's wiring: vol_fn(book_covariance(...)) sees UNMAPPED_* keys and must not raise."""
    from council.reference.book import book_covariance, vol_fn

    states = states_for(policy)
    cov = book_covariance(None, policy.universe.lines, states, policy)   # sigma_ann, rho = 1
    ref = default_ref(policy)
    d = run(policy, states=states, ref=ref, levels=ref, current={"UNMAPPED_5": 0.05},
            ex_ante_vol_fn=vol_fn(cov))
    assert d.final_w["UNMAPPED_5"] == 0.05 and d.base_w["UNMAPPED_5"] == 0.05
    assert "covariance function" in row(d, "R8").detail
    assert d.ex_ante_vol == pytest.approx(vol_fn(cov)(d.final_w))
    # rho = 1 from sigma_ann: the same book as the engine's own upper bound (R7 binds here)
    bound = run(policy, states=states, ref=ref, levels=ref, current={"UNMAPPED_5": 0.05})
    assert d.final_w == pytest.approx(bound.final_w) and d.passed
