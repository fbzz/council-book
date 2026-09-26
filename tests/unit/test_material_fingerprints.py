"""WP-G: the per-line material fingerprint (design §11.4) and its comparison with the ledger's stored
map, including the one-off read of a ledger that kept only the legacy single fingerprint."""

from __future__ import annotations

from datetime import UTC, datetime

from council.ledger.db import Ledger
from council.models.cards import EvidenceCard
from council.models.facts import EventItem, Fact, FactPack, MarketState
from council.runtime import (
    MATERIAL_GLOBAL,
    consume_fingerprints,
    fingerprints_digest,
    material_changes,
    material_fingerprint,
    material_fingerprints,
)

SLOT = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)


def pack(*, admitted=("NDX", "SPX", "TSTA"), trend=None, frozen=None, events=(), facts=()) -> FactPack:
    trend = trend or {}
    frozen = frozen or {}
    states = {s: MarketState(symbol=s, asset_class="stock" if s.startswith("TST") else "index",
                             trend=trend.get(s, "up"), frozen=s in frozen, frozen_reason=frozen.get(s))
              for s in ("NDX", "SPX", "TSTA")}
    return FactPack(cycle_id="c", slot=SLOT, created_at=SLOT, admitted=list(admitted), states=states,
                    facts=list(facts), events=list(events))


def card(scope, kind="vol_shock", qualifying=True) -> EvidenceCard:
    return EvidenceCard(scope=list(scope), card_type=kind, direction="risk_down", claim="x",
                        evidence_ids=["F:NDX:trend"], horizon_days=5, card_id="K:vol:1", role="vol",
                        qualifying=qualifying)


def fund(field: str, value, symbol="TSTA") -> Fact:
    return Fact(id=f"F:{symbol}:{field}", kind="fundamental", symbol=symbol, value=value, unit="pct",
                available_at=SLOT, source="sec")


def changed(a: FactPack, b: FactPack, cards_a=(), cards_b=(), kill_a="NORMAL", kill_b="NORMAL") -> set[str]:
    before = consume_fingerprints(material_fingerprints(a, list(cards_a), kill_a), None, [])
    after = material_fingerprints(b, list(cards_b), kill_b)
    return {s for s, moved in material_changes(after, before).items() if moved}


def test_session_admission_is_not_material():
    assert changed(pack(), pack(admitted=("NDX",))) == set()
    assert changed(pack(), pack(frozen={"TSTA": "market_closed"})) == set()


def test_each_input_moves_only_its_own_line():
    assert changed(pack(), pack(trend={"NDX": "mixed"})) == {"NDX"}
    assert changed(pack(), pack(frozen={"SPX": "stale"})) == {"SPX"}
    assert changed(pack(), pack(), cards_b=[card(["SPX"])]) == {"SPX"}
    assert changed(pack(), pack(), cards_b=[card(["NDX", "SPX"])]) == {"NDX", "SPX"}
    assert changed(pack(), pack(), cards_b=[card(["SPX"], qualifying=False)]) == set()
    earnings = EventItem(id="E:earnings:TSTA@2026-10-02", kind="earnings", at_utc=SLOT, symbols=["TSTA"],
                         severity=2, source="sec_estimate")
    assert changed(pack(), pack(events=[earnings])) == {"TSTA"}
    assert changed(pack(facts=[fund("rev_yoy", 10.0)]), pack(facts=[fund("rev_yoy", 12.0)])) == {"TSTA"}


def test_calendar_driven_filing_age_is_not_material():
    assert changed(pack(facts=[fund("filing_age_d", 3.0)]), pack(facts=[fund("filing_age_d", 4.0)])) == set()


def test_market_wide_inputs_move_every_line():
    fomc = EventItem(id="E:fomc@2026-10-28", kind="fomc", at_utc=SLOT, severity=3, source="policy")
    assert changed(pack(), pack(events=[fomc])) == {"NDX", "SPX", "TSTA"}
    assert changed(pack(), pack(), kill_b="WARN") == {"NDX", "SPX", "TSTA"}
    assert changed(pack(), pack(), cards_b=[card(["book"])]) == {"NDX", "SPX", "TSTA"}


def test_comparison_without_a_stored_map():
    fps = material_fingerprints(pack(), [], "NORMAL")
    assert MATERIAL_GLOBAL in fps and set(fps) == {MATERIAL_GLOBAL, "NDX", "SPX", "TSTA"}
    assert set(material_changes(fps, None).values()) == {True}                    # the first cycle
    assert set(material_changes(fps, None, legacy_equal=True).values()) == {False}  # legacy still matches
    assert set(material_changes(fps, None, legacy_equal=False).values()) == {True}
    assert fingerprints_digest(fps) == fingerprints_digest(dict(reversed(list(fps.items()))))


def test_evidence_is_consumed_only_by_the_lines_that_could_act_on_it():
    """New daily bars land at the 02:40 slot, when the London and US sessions are closed: a proposal
    issued then (for the lines that are open) must not use up the closed lines' new evidence, and
    market-wide evidence is consumed line by line too."""
    first = material_fingerprints(pack(), [], "NORMAL")
    stored = consume_fingerprints(first, None, [])                     # first write: every line consumes
    assert set(stored) == {MATERIAL_GLOBAL, "NDX", "SPX", "TSTA"}
    assert not any(material_changes(first, stored).values())
    night = material_fingerprints(pack(admitted=("SPX",), trend={"NDX": "mixed", "TSTA": "down"}), [], "WARN")
    assert material_changes(night, stored) == {"NDX": True, "SPX": True, "TSTA": True}
    stored = consume_fingerprints(night, stored, ["SPX"])                # a proposal while NDX, TSTA are closed
    assert material_changes(night, stored) == {"NDX": True, "SPX": False, "TSTA": True}
    stored = consume_fingerprints(night, stored, ["NDX", "SPX"])         # London opens: NDX acts on it once
    assert material_changes(night, stored) == {"NDX": False, "SPX": False, "TSTA": True}
    stored = consume_fingerprints(night, stored, ["NDX", "SPX"])         # a later open slot: not new again
    assert material_changes(night, stored)["NDX"] is False
    gone = {k: v for k, v in night.items() if k != "TSTA"}
    assert "TSTA" not in consume_fingerprints(gone, stored, ["NDX"])     # a line that left is dropped


def test_the_ledger_keeps_the_map(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    ledger.migrate()
    assert ledger.get_material_fingerprints() is None
    fps = material_fingerprints(pack(), [], "NORMAL")
    ledger.set_material_fingerprints(fps)
    assert ledger.get_material_fingerprints() == fps
    ledger.set_runtime("last_material_fingerprints", {"NDX": 3})                  # corrupt: ignored
    assert ledger.get_material_fingerprints() is None
    legacy = material_fingerprint(pack(), [], "NORMAL")
    assert isinstance(legacy, str) and len(legacy) == 64
