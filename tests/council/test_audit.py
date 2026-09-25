"""Auditor: one pass and one fail case per rule."""

from __future__ import annotations

import pytest

from council.deliberation.audit import audit, direction_matches
from council.deliberation.officers import vol_cards
from council.models.pm import PMDecision

from .factories import REF_LEVELS, build_pack, pm_decision, with_late_evidence


def dev(symbol, level, direction, ids=("F:NDX:trend",)):
    return {"symbol": symbol, "level": level, "direction": direction, "evidence_ids": list(ids),
            "reason": "r"}


def run(decision, *, policy, lines, bands, current, pack=None, cards=None):
    pack = pack or build_pack()
    cards = vol_cards(pack, policy) if cards is None else cards
    parsed = None if decision is None else PMDecision.model_validate(decision)
    return audit(parsed, pack=pack, cards=cards, bands=bands, ref_levels=REF_LEVELS,
                 current_levels=current, lines=lines, policy=policy)


def test_clean_decision_is_valid(policy, lines, bands, current):
    res = run(pm_decision([dev("SEMIS", 0.5, "cut", ["K:vol:1"])]), policy=policy, lines=lines,
              bands=bands, current=current)
    assert res.valid and res.violations == [] and res.reverted == []
    assert res.levels == {**REF_LEVELS, "SEMIS": 0.5}


def test_no_decision_is_invalid(policy, lines, bands, current):
    res = run(None, policy=policy, lines=lines, bands=bands, current=current)
    assert not res.valid and res.levels == REF_LEVELS and res.violations == ["no_decision"]


def test_no_deviation_is_valid(policy, lines, bands, current):
    res = run(pm_decision(None, fact="F:NDX:trend"), policy=policy, lines=lines, bands=bands,
              current=current)
    assert res.valid and res.levels == REF_LEVELS


@pytest.mark.parametrize(
    ("symbol", "reason"),
    [("BTC", "reference_only"), ("XYZ", "unknown_line")],
)
def test_symbol_rules(policy, lines, bands, current, symbol, reason):
    decision = pm_decision([dev(symbol, 0.5, "cut"), dev("SEMIS", 0.5, "cut", ["K:vol:1"]),
                            dev("GOLD", 0.75, "add")])
    res = run(decision, policy=policy, lines=lines, bands=bands, current=current)
    assert res.reverted == [symbol] and res.violations == [f"{symbol}: {reason}"]
    assert res.valid  # 1 of 3 reverted
    assert symbol not in res.levels or res.levels[symbol] == REF_LEVELS[symbol]


def test_not_admitted(policy, lines, bands, current):
    pack = build_pack(admitted=["NDX", "SEMIS"])
    res = run(pm_decision([dev("GOLD", 0.75, "add")]), pack=pack, policy=policy, lines=lines,
              bands=bands, current=current)
    assert res.reverted == ["GOLD"] and "GOLD: not_admitted" in res.violations and not res.valid


def test_max_deviations_from_policy(policy, lines, bands, current):
    risk = {**policy.risk, "authority": {**policy.risk["authority"], "max_deviations_per_cycle": 2}}
    strict = policy.model_copy(update={"risk": risk})
    decision = pm_decision([dev("SEMIS", 0.5, "cut", ["K:vol:1"]), dev("GOLD", 0.75, "add"),
                            dev("GBPUSD", 0.25, "add")])
    res = run(decision, policy=strict, lines=lines, bands=bands, current=current)
    assert res.reverted == ["GBPUSD"] and "GBPUSD: over_max_deviations" in res.violations
    ok = run(decision, policy=policy, lines=lines, bands=bands, current=current)
    assert ok.reverted == [] and ok.levels["GBPUSD"] == 0.25


def test_duplicate_line_reverted(policy, lines, bands, current):
    decision = pm_decision([dev("GOLD", 0.75, "add"), dev("GOLD", 0.25, "cut"),
                            dev("SEMIS", 0.5, "cut", ["K:vol:1"])])
    res = run(decision, policy=policy, lines=lines, bands=bands, current=current)
    assert res.reverted == ["GOLD"] and "GOLD: duplicate" in res.violations
    assert res.levels["GOLD"] == 0.75


def test_levels_snapped_to_grid(policy, lines, bands, current):
    res = run(pm_decision([dev("GOLD", 0.8, "add")]), policy=policy, lines=lines, bands=bands,
              current=current)
    assert res.levels["GOLD"] == 0.75 and res.valid


def test_unknown_evidence_reverted_but_card_ids_count(policy, lines, bands, current):
    bad = run(pm_decision([dev("GOLD", 0.75, "add", ["F:GOLD:made_up"])]), policy=policy,
              lines=lines, bands=bands, current=current)
    assert bad.reverted == ["GOLD"] and "unknown_evidence F:GOLD:made_up" in bad.violations[0]
    good = run(pm_decision([dev("SEMIS", 0.5, "cut", ["K:vol:1", "N:1a2b3c4d"])]), policy=policy,
               lines=lines, bands=bands, current=current)
    assert good.reverted == []
    no_cards = run(pm_decision([dev("SEMIS", 0.5, "cut", ["K:vol:1"])]), policy=policy,
                   lines=lines, bands=bands, current=current, cards=[])
    assert no_cards.reverted == ["SEMIS"]


@pytest.mark.parametrize(
    ("direction", "level", "ref", "cur", "ok"),
    [
        ("cut", 0.5, 1.0, 1.0, True), ("cut", 1.0, 1.0, 1.0, False),
        ("add", 0.75, 0.5, 0.5, True), ("add", 0.5, 0.5, 0.5, False),
        ("short", -0.25, 0.0, 0.0, True), ("short", 0.0, 0.0, 0.0, False),
        ("cover", 0.0, 0.0, -0.25, True), ("cover", -0.25, 0.0, -0.5, True),
        ("cover", 0.25, 0.0, -0.25, False), ("cover", 0.0, 0.0, 0.0, False),
        ("lever", 1.25, 1.0, 1.0, True), ("lever", 1.0, 1.0, 0.5, False),
        ("sideways", 1.0, 1.0, 1.0, False),
    ],
)
def test_direction_matches(direction, level, ref, cur, ok):
    assert direction_matches(direction, level, ref, cur) is ok


def test_direction_mismatch_reverted(policy, lines, bands, current):
    res = run(pm_decision([dev("GOLD", 0.75, "cut")]), policy=policy, lines=lines, bands=bands,
              current=current)
    assert res.reverted == ["GOLD"] and "direction_mismatch cut" in res.violations[0]
    cover = run(pm_decision([dev("OIL", 0.0, "cover")]), policy=policy, lines=lines, bands=bands,
                current=current)
    assert cover.reverted == [] and cover.levels["OIL"] == 0.0


def test_invalid_decisive_fact(policy, lines, bands, current):
    res = run(pm_decision(None, fact="F:NOPE:x"), policy=policy, lines=lines, bands=bands,
              current=current)
    assert not res.valid and res.violations == ["decisive_fact: unknown_evidence F:NOPE:x"]


@pytest.mark.parametrize(
    ("deviations", "valid"),
    [
        ([dev("GOLD", 0.75, "cut")], False),                                   # 1 of 1
        ([dev("GOLD", 0.75, "cut"), dev("GBPUSD", 0.25, "add")], False),      # 1 of 2
        ([dev("GOLD", 0.75, "cut"), dev("GBPUSD", 0.25, "add"),
          dev("SEMIS", 0.5, "cut", ["K:vol:1"])], True),                        # 1 of 3
    ],
)
def test_half_reverted_invalidates_replicate(policy, lines, bands, current, deviations, valid):
    res = run(pm_decision(deviations), policy=policy, lines=lines, bands=bands, current=current)
    assert res.valid is valid


def test_late_evidence_is_not_citable(policy, lines, bands, current):
    pack = with_late_evidence(build_pack())
    res = run(pm_decision([dev("GOLD", 0.75, "add", ["F:NDX:late_close"])]), pack=pack,
              policy=policy, lines=lines, bands=bands, current=current)
    assert res.reverted == ["GOLD"] and "unknown_evidence F:NDX:late_close" in res.violations[0]
