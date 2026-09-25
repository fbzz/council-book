"""R10 authority bands: cut 0.5 only with a qualifying card, leverage extension 0.5, mixed [0, 1],
down [-0.5, 0.25], overlay bands, reference-only lines, WARN/event no-adds, frozen holds."""

from __future__ import annotations

from datetime import timedelta

import pytest

from council.models.cards import EvidenceCard
from council.models.risk import Band
from council.risk.authority import (
    CutRecord,
    compute_bands,
    enforce_authority,
    expired_cuts,
    restrict,
)
from tests.risk.helpers import default_ref, override, states_for


def card(card_type="vol_shock", scope=("NDX",), qualifying=True, n=1):
    return EvidenceCard(card_id=f"K:vol:{n}", role="vol", scope=list(scope), card_type=card_type,
                        direction="risk_down", claim="synthetic", evidence_ids=["V:NDX:ratio"],
                        horizon_days=5, qualifying=qualifying)


def bands(policy, *, trend="up", cards=(), current=None, kill="NORMAL", event=(), lever=(),
          short=(), states=None, ref=None):
    return compute_bands(
        lines=policy.universe, ref=ref or default_ref(policy, "up"),
        states=states or states_for(policy, trend), cards=list(cards),
        current_levels=current or {}, kill_state=kill, event_blocked=set(event),
        lever_ok=set(lever), short_ok=set(short), policy=policy)


def lohi(band: Band):
    return band.lo, band.hi


def test_up_without_card_is_pinned_to_reference(policy):
    assert lohi(bands(policy)["NDX"]) == (1.0, 1.0)


def test_up_with_qualifying_card_allows_cut_05(policy):
    b = bands(policy, cards=[card()])["NDX"]
    assert lohi(b) == (0.5, 1.0) and b.qualifying_cards == ["K:vol:1"]
    news = bands(policy, cards=[card("news_material")])["NDX"]
    assert lohi(news) == (0.5, 1.0)
    bigger = override(policy, "risk", {"authority.up.cut_with_qualifying_card": 0.75})
    assert lohi(bands(bigger, cards=[card()])["NDX"]) == (0.25, 1.0)


@pytest.mark.parametrize(
    "c",
    [card("event_binary"), card("news_context"), card(qualifying=False), card(scope=("SPX",))],
)
def test_non_qualifying_cards_do_not_open_a_cut(policy, c):
    assert lohi(bands(policy, cards=[c])["NDX"]) == (1.0, 1.0)


def test_leverage_extension_05_only_when_lever_ok(policy):
    assert lohi(bands(policy, lever=["NDX"])["NDX"]) == (1.0, 1.5)
    assert lohi(bands(policy)["SPX"]) == (1.0, 1.0)


def test_mixed_band_0_to_1(policy):
    assert lohi(bands(policy, trend="mixed", ref=default_ref(policy, "mixed"))["NDX"]) == (0.0, 1.0)


def test_down_band_shorts_only_with_short_ok(policy):
    ref = default_ref(policy, "down")
    assert lohi(bands(policy, trend="down", ref=ref)["NDX"]) == (0.0, 0.25)
    assert lohi(bands(policy, trend="down", ref=ref, short=["NDX"])["NDX"]) == (-0.5, 0.25)


@pytest.mark.parametrize(
    "trend,plain,with_short",
    [("up", (0.0, 0.5), (0.0, 0.5)), ("mixed", (0.0, 0.25), (-0.25, 0.25)),
     ("down", (0.0, 0.0), (-0.5, 0.0))],
)
def test_overlay_bands(policy, trend, plain, with_short):
    assert lohi(bands(policy, trend=trend)["OIL"]) == plain
    assert lohi(bands(policy, trend=trend, short=["OIL"])["OIL"]) == with_short


def test_reference_only_lines_are_pinned(policy):
    b = bands(policy, trend="mixed", ref=default_ref(policy, "mixed"), short=["BTC"], lever=["BTC"])
    assert lohi(b["BTC"]) == (0.5, 0.5) and lohi(b["ETH"]) == (0.5, 0.5)


def test_warn_means_no_adds_both_sides(policy):
    ref = default_ref(policy, "mixed")
    b = bands(policy, trend="mixed", ref=ref, kill="WARN", short=["OIL"],
              current={"NDX": 0.5, "OIL": -0.25})
    assert lohi(b["NDX"]) == (0.0, 0.5)     # long: hi = current
    assert lohi(b["OIL"]) == (-0.25, 0.0)   # short: may cover, may not grow
    assert lohi(b["SPX"]) == (0.0, 0.0)     # flat: nothing new


def test_event_block_means_no_adds(policy):
    b = bands(policy, event=["NDX"], current={"NDX": 0.5})
    assert lohi(b["NDX"]) == (0.5, 0.5)  # up band [1, 1] cannot be reached without adding
    assert lohi(b["SPX"]) == (1.0, 1.0)


def test_frozen_or_missing_state_holds_current(policy):
    states = states_for(policy, "up", NDX={"frozen": True})
    del states["SPX"]
    b = bands(policy, states=states, current={"NDX": 0.75, "SPX": 0.4})
    assert lohi(b["NDX"]) == (0.75, 0.75) and lohi(b["SPX"]) == (0.4, 0.4)


def test_halted_flattens(policy):
    assert all(lohi(b) == (0.0, 0.0) for b in bands(policy, kill="HALTED").values())


def test_band_always_contains_reference(policy):
    # crypto confirmation can leave the reference at 0.5 while the trend reads down
    b = bands(policy, trend="down", ref={**default_ref(policy, "down"), "NDX": 0.5})
    assert b["NDX"].lo <= 0.5 <= b["NDX"].hi


def test_enforce_authority_actually_clips():
    bs = {"NDX": Band(symbol="NDX", trend="up", ref_level=1.0, lo=0.5, hi=1.0),
          "OIL": Band(symbol="OIL", trend="down", ref_level=0.0, lo=0.0, hi=0.0)}
    clipped, reverted = enforce_authority({"NDX": 1.5, "OIL": -0.5, "XYZ": 1.0}, bs)
    assert clipped == {"NDX": 1.0, "OIL": 0.0}
    assert reverted == ["NDX", "OIL", "XYZ"]
    clipped, reverted = enforce_authority({}, bs)
    assert clipped == {"NDX": 1.0, "OIL": 0.0} and reverted == []


def test_restrict_never_returns_an_empty_interval():
    assert restrict(1.0, 1.0, 0.0, 0.5) == (0.5, 0.5)
    assert restrict(-0.5, 0.25, 0.0, 1.0) == (0.0, 0.25)


def test_expired_cuts(now):
    log = [CutRecord(symbol="NDX", card_id="K:vol:1", cut_at=now - timedelta(days=2),
                     expires_at=now - timedelta(hours=1)),
           CutRecord(symbol="SPX", card_id="K:vol:2", cut_at=now - timedelta(days=1),
                     expires_at=now + timedelta(hours=1)),
           CutRecord(symbol="GOLD", card_id="K:vol:3", cut_at=now - timedelta(days=3),
                     expires_at=now - timedelta(days=1))]
    current = {"NDX": 0.5, "SPX": 0.5, "GOLD": 1.0}
    ref = {"NDX": 1.0, "SPX": 1.0, "GOLD": 1.0}
    assert expired_cuts(current, ref, log, now) == {"NDX"}  # GOLD is already back at reference
    with pytest.raises(ValueError):
        expired_cuts(current, ref, log, now.replace(tzinfo=None))
