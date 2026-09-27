"""The per-line decision trail (transparency-v2 §4.4, T5a-core): one fixture per outcome.

The builder (`council.publish.trail`) walks each line from the reference to execution in fixed
words and names the first gate that stopped a change someone asked for. The same builder runs over
the PRIVATE ledger record (`record_trails`) and over the PUBLIC cycle document (`trails`), and its
engine and plan wording goes through `trace_rules`, so no size floor, fee or broker-derived number
is ever printed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from council.models.cycle import CycleRecord
from council.models.risk import RiskDecision, TraceStep
from council.publish import trail
from tests.fixtures import trail_records as tr

JOURNAL = Path(__file__).resolve().parents[1] / "fixtures" / "site_journal" / "journal"


def by_line(trails: list[trail.LineTrail]) -> dict[str, trail.LineTrail]:
    return {t.line: t for t in trails}


def stages(t: trail.LineTrail) -> list[str]:
    return [s.stage for s in t.steps]


def words(t: trail.LineTrail, stage: str) -> str:
    return " | ".join(s.words for s in t.steps if s.stage == stage)


# ------------------------------------------------------------------------ the four named fixtures
def test_cut_executed_walks_every_stage_to_the_fill():
    rec = tr.cut_executed()
    got = by_line(trail.record_trails(rec, names=tr.NAMES, decision_state="completed",
                                      leg_states={"SEMIS": ["filled"]}, achieved_x={"SEMIS": 0.067},
                                      approved_at="2026-10-01T15:02:00Z"))
    semis = got["SEMIS"]
    assert semis.outcome == "changed" and semis.stopped_at is None
    assert semis.asked_by == ("bear", "pm")
    assert stages(semis) == ["reference", "officers", "band", "debate", "manager", "auditor", "medoid",
                             "risk", "plan", "human", "execution"]
    assert semis.headline.startswith("SEMIS · Semiconductors — cut, 13.4% → 6.7%")
    assert "vs SMA50 +2.0%" in words(semis, "reference") and "level 1.00 · 13.4%" in words(semis, "reference")
    assert "K:vol:1 volatility shock" in words(semis, "officers") and "qualifying" in words(semis, "officers")
    assert "V:SEMIS:ewma5_60 = 2.31x" in words(semis, "debate")          # evidence values in claims
    assert words(semis, "manager").startswith("3 of 3 attempts: cut to 0.50")
    assert "decisive V:SEMIS:ewma5_60 = 2.31x" in words(semis, "manager")
    assert "sided with the bear" in words(semis, "manager")
    assert words(semis, "auditor") == "accepted; inside the band"
    assert "partial close" in words(semis, "plan") and "council change" in words(semis, "plan")
    assert words(semis, "human") == "approved (slot 14:40 UTC)"
    assert words(semis, "execution") == "filled; achieved 6.7%"
    assert got["NDX"].outcome == "held" and got["NDX"].steps == ()
    assert got["NDX"].headline.endswith("held the reference; nobody asked")


@pytest.mark.parametrize("note,outcome,expect", [
    ("SEMIS: R12 minimum hold", "held_by_engine", "minimum holding period not over (R12)"),
    ("SEMIS: MC no new material evidence", "held_by_engine", "no new evidence since the last change (MC)"),
    ("SEMIS: R15 SR_be 0.32 above 0.20", "held_by_engine", "net-of-cost gate (R15)"),     # old free text
    ("SEMIS: R15", "held_by_engine", "net-of-cost gate (R15)"),                         # new code-only form
    ("SEMIS: R11 below the minimum trade size", "too_small", "too small to trade (R11)"),
    ("SEMIS: R11 deadband (level step -0.50)", "too_small", "too small to trade (R11)"),
    ("SEMIS: deadband", "too_small", "too small to trade (R11)"),
    ("SEMIS: R11", "too_small", "too small to trade (R11)"),
])
def test_cut_held_by_risk_names_the_rule(note, outcome, expect):
    semis = by_line(trail.record_trails(tr.cut_held_by_risk(note)))["SEMIS"]
    assert semis.outcome == outcome and semis.stopped_at == "risk"
    assert expect in words(semis, "risk") and "(from engine notes)" in words(semis, "risk")
    assert semis.why_not == f"the manager asked for a cut to 0.50; not done: {expect}"
    assert semis.headline.endswith("the manager asked for a cut to 0.50; held")


def test_an_r11_hold_reads_the_same_whatever_its_private_subtype():
    """The size floor must not be told apart from the deadband (it would bound the NAV)."""
    texts = set()
    for note in ("SEMIS: R11 below the minimum trade size", "SEMIS: R11 deadband (level step -0.50)",
                 "SEMIS: R11 reference rule (level unchanged, drift below the threshold)", "SEMIS: R11"):
        texts.add(trail.render_text(trail.record_trails(tr.cut_held_by_risk(note))))
    assert len(texts) == 1
    text = texts.pop()
    assert "too small to trade (R11)" in text
    assert not re.search(r"minimum|floor|broker|reference rule|level step", text)


def test_an_r15_value_shows_only_for_a_line_priced_from_the_policy_floor():
    held = tr.cut_held_by_risk("SEMIS: R15 SR_be 0.32 above 0.20")
    assert "0.32" not in trail.render_text(trail.record_trails(held))
    shown = held.model_copy(update={"value_lines": ["SEMIS"]})
    assert "SR_be 0.32 above 0.20 (R15; policy cost floor)" in trail.render_text(trail.record_trails(shown))


def test_bear_asked_but_the_manager_held_with_the_counterfactual_band():
    got = by_line(trail.record_trails(tr.bear_asked_pm_held(), names=tr.NAMES))
    ndx = got["NDX"]
    assert ndx.outcome == "not_taken_by_manager" and ndx.stopped_at == "manager"
    assert ndx.asked_by == ("bear",)
    assert ndx.headline.startswith("NDX · Nasdaq-100 — the bear asked for a cut to 0.50; not done")
    assert ndx.why_not == "the bear asked for a cut to 0.50; not done: the manager did not take it up"
    band = words(ndx, "band")
    assert band.startswith("fixed at 1.00 — trend up")
    assert ("code would not have allowed a cut to 0.50: a cut in an uptrend needs a volatility-shock card "
            "or a corroborated material news card on the line") in band
    assert "bear: cut to 0.50 · c2 “The Nasdaq-100 fell over ten days.” [F:NDX:mom10d = -3.4%]" in words(ndx, "debate")
    manager = words(ndx, "manager")
    assert manager.startswith("0 of 3 attempts deviated")
    assert "attempt 1 set aside bear:c2: “A ten-day dip inside an uptrend.”" in manager
    assert "no change because “Trends intact.”" in manager
    # the unused SEMIS unlock is a trail too (the design's third reason for a trail)
    assert got["SEMIS"].outcome == "not_taken_by_manager"
    assert "allowed a cut that nobody used" in words(got["SEMIS"], "band")


def test_rehearsal_lines_end_at_the_plan_not_ordered():
    got = by_line(trail.record_trails(tr.rehearsal()))
    for line in ("NDX", "SEMIS", "GOLD"):
        t = got[line]
        assert t.outcome == "not_ordered" and t.stopped_at == "plan", line
        assert t.steps[-1].stage == "plan" and t.steps[-1].words == trail.REHEARSAL_PLAN
        assert "on paper; not ordered" in t.headline
        assert t.why_not == trail.REHEARSAL_PLAN          # the move is the reference's, not the bear's
    assert got["OIL"].outcome == "held"


# ------------------------------------------------------------------------ the other outcomes
def test_news_card_unlock_shows_the_band_before_the_cards_and_the_dropped_draft():
    semis = by_line(trail.record_trails(tr.news_unlock()))["SEMIS"]
    analysts = words(semis, "analysts")
    assert "K:news:1 material news, risk down, corroborated by K:vol:1 — qualifying" in analysts
    assert "P:1a2b3c4d (Federal Reserve Board release)" in analysts
    assert ("code dropped the news analyst's card draft 2: it cited P:deadbeef, which was not in its pack"
            in analysts)
    assert "(before the analysts' cards: fixed at 1.00)" in words(semis, "band")
    assert semis.outcome == "pending" and semis.stopped_at == "human"


def test_band_clip_is_not_allowed_by_band():
    d = tr.decision({"NDX": 0.5})
    rec = tr.record(pm=tr.replicates([d, d, d], enforced=[dict(tr.REF_LEVELS)] * 3),
                    risk_decision=tr.risk(raw=dict(tr.REF_LEVELS)))
    ndx = by_line(trail.record_trails(rec))["NDX"]
    assert ndx.outcome == "not_allowed_by_band" and ndx.stopped_at == "band"
    assert "attempt 0 asked 0.50, band allows 1.00" in words(ndx, "auditor")
    assert "a cut in an uptrend needs" in ndx.why_not


def test_auditor_revert():
    d = tr.decision({"SEMIS": 0.5})
    rec = tr.record(pm=tr.replicates([d, d, d], reverted=[["SEMIS"]] * 3))
    semis = by_line(trail.record_trails(rec))["SEMIS"]
    assert semis.outcome == "reverted_by_auditor" and semis.stopped_at == "auditor"
    assert "attempt 0: reverted (unknown_evidence)" in words(semis, "auditor")


def test_medoid_fallback_is_no_agreement():
    cut, hold = tr.decision({"SEMIS": 0.5}), tr.decision(sided="reference")
    rec = tr.record(pm=tr.replicates([cut, hold, hold]), fallback_lines=["SEMIS"],
                    agreement={"SEMIS": 1 / 3})
    semis = by_line(trail.record_trails(rec))["SEMIS"]
    assert semis.outcome == "no_agreement" and semis.stopped_at == "medoid"
    assert "only 1 of 3 attempts agreed on its move → back to the reference 1.00" in words(semis, "medoid")


def test_medoid_fallback_is_inferred_for_older_records():
    cut, hold = tr.decision({"SEMIS": 0.5}), tr.decision(sided="reference")
    rec = tr.record(pm=tr.replicates([cut, hold, hold]), agreement={"SEMIS": 1 / 3})
    assert by_line(trail.record_trails(rec))["SEMIS"].outcome == "no_agreement"


@pytest.mark.parametrize("skip,outcome,expect", [
    ("SEMIS: below_broker_minimum", "too_small", "too small to trade (R11)"),
    ("SEMIS: below_real_minimum", "too_small", "too small to trade (R11)"),
    ("SEMIS: no_quote", "not_ordered", "not ordered: no quote"),
    ("SEMIS: stop_outside_broker_bounds (widened 3.1)", "not_ordered", "not ordered: stop outside broker bounds"),
])
def test_plan_skips(skip, outcome, expect):
    rec = tr.cut_executed().model_copy(update={"plan": tr.plan(skipped=[skip]), "decision_id": None,
                                               "decision_state": "reviewed_no_action"})
    semis = by_line(trail.record_trails(rec))["SEMIS"]
    assert semis.outcome == outcome and semis.stopped_at == "plan"
    assert words(semis, "plan") == expect
    assert "3.1" not in trail.render_text([semis])


@pytest.mark.parametrize("state,reason,outcome,stopped", [
    ("rejected", "Wait one cycle.", "rejected", "human"),
    ("expired", None, "expired", "human"),
    ("superseded", None, "expired", "human"),
    ("awaiting_publication", None, "pending", "human"),
])
def test_human_outcomes(state, reason, outcome, stopped):
    semis = by_line(trail.record_trails(tr.cut_executed(), decision_state=state, reason=reason))["SEMIS"]
    assert semis.outcome == outcome and semis.stopped_at == stopped
    assert semis.steps[-1].stage == "human"
    if reason:
        assert f"rejected: “{reason}”" in words(semis, "human")


@pytest.mark.parametrize("states,outcome", [
    (["rejected"], "not_filled"), (["skipped"], "not_filled"), (["partially_filled"], "changed_partly"),
    (["submitted"], "pending"),
])
def test_execution_outcomes(states, outcome):
    semis = by_line(trail.record_trails(tr.cut_executed(), decision_state="completed",
                                        leg_states={"SEMIS": states}))["SEMIS"]
    assert semis.outcome == outcome
    assert semis.steps[-1].stage == "execution"


def test_engine_limited_change_is_changed_partly():
    rec = tr.cut_executed()
    risk = rec.risk.model_copy(update={"final_w": {**rec.risk.final_w, "SEMIS": 0.1},
                                       "hold_reasons": ["SEMIS: limited by R5 line cap"]})
    semis = by_line(trail.record_trails(rec.model_copy(update={"risk": risk}), decision_state="completed",
                                        leg_states={"SEMIS": ["filled"]}))["SEMIS"]
    assert semis.outcome == "changed_partly" and "limited by R5 line cap" in words(semis, "risk")


def test_reference_trade():
    base = {**tr.BOOK, "GOLD": 0.06}          # the book holds GOLD at half size; the rule wants 0.75
    rec = tr.record(risk_decision=tr.risk(raw=dict(tr.REF_LEVELS), base=base),
                    the_plan=tr.plan({"GOLD": (0.06, 0.09)}), decision_state="completed")
    rec.plan.legs[0].origin = "reference"
    gold = by_line(trail.record_trails(rec, decision_state="completed", leg_states={"GOLD": ["filled"]}))["GOLD"]
    assert gold.outcome == "reference_trade" and gold.asked_by == ("reference",)
    assert "(the reference rule)" in gold.headline and "reference trade" in words(gold, "plan")


def test_a_reference_move_the_engine_held_names_the_reference_rule():
    base = {**tr.BOOK, "GOLD": 0.0}
    rec = tr.record(risk_decision=tr.risk(raw=dict(tr.REF_LEVELS), base=base, final=base,
                                          notes=["GOLD: R14 cycle cost budget"]))
    gold = by_line(trail.record_trails(rec))["GOLD"]
    assert gold.outcome == "held_by_engine" and gold.asked_by == ("reference",)
    assert gold.why_not.startswith("the reference rule asked for 9.0% (from 0.0%); not done: trimmed")


def test_line_trace_is_used_when_the_engine_records_it():
    rec = tr.cut_held_by_risk()
    risk = rec.risk.model_copy(update={"hold_reasons": [], "line_trace": {"SEMIS": [
        TraceStep(stage="line_filter", code="R11 below the minimum trade size", before_w=0.067, after_w=0.134,
                  value=0.0021, limit=0.005, inputs=("size_floor",))]}})
    semis = by_line(trail.record_trails(rec.model_copy(update={"risk": risk})))["SEMIS"]
    assert semis.outcome == "too_small"
    text = words(semis, "risk")
    assert "too small to trade (R11)" in text and "0.0021" not in text and "0.005" not in text
    assert "(from engine notes)" not in text


def test_every_outcome_word_is_in_the_vocabulary():
    seen = set()
    for rec, kw in [(tr.cut_executed(), {"decision_state": "completed", "leg_states": {"SEMIS": ["filled"]}}),
                    (tr.cut_held_by_risk(), {}), (tr.bear_asked_pm_held(), {}), (tr.rehearsal(), {}),
                    (tr.news_unlock(), {})]:
        seen |= {t.outcome for t in trail.record_trails(rec, **kw)}
    assert seen <= set(trail.OUTCOMES)


def test_old_records_without_the_trail_fields_still_build():
    raw = json.loads(tr.cut_executed().model_dump_json())
    for name in ("drops", "code_bands", "fallback_lines", "material_lines", "claim_lines", "evidence_values",
                 "value_lines"):
        raw.pop(name)
    raw["risk"].pop("line_trace")
    rec = CycleRecord.model_validate(raw)
    assert rec.drops == [] and rec.risk.line_trace == {}
    semis = by_line(trail.record_trails(rec))["SEMIS"]
    assert semis.outcome == "pending" and "V:SEMIS:ewma5_60" in words(semis, "debate")


def test_trace_step_model_is_additive():
    assert RiskDecision.model_fields["line_trace"].default_factory() == {}
    step = TraceStep(stage="final", code="R10", before_w=0.1, after_w=0.2, inputs=("policy",))
    assert step.inputs == ("policy",)


# ------------------------------------------------------------------------ the public journal
def _journal_cycles():
    ops = {}
    for text in (JOURNAL / "ops" / "cycles.jsonl").read_text().splitlines():
        row = json.loads(text)
        ops[row["cycle_id"]] = row
    for path in sorted((JOURNAL / "cycles").rglob("*Z.json")):
        cycle_id = path.stem
        ex_path = JOURNAL / "executions" / cycle_id[:4] / cycle_id[5:7] / f"{cycle_id}.json"
        yield (cycle_id, json.loads(path.read_text()),
               json.loads(ex_path.read_text()) if ex_path.exists() else None, ops.get(cycle_id))


def test_every_changed_line_of_the_fixture_journal_has_a_trail_that_reaches_the_plan():
    count = 0
    for cycle_id, doc, execution, ops in _journal_cycles():
        got = by_line(trail.trails(doc, execution, ops))
        risk = doc["risk"]
        changed = {s for s in set(risk["base_x"]) | set(risk["final_x"])
                   if abs(risk["base_x"].get(s, 0.0) - risk["final_x"].get(s, 0.0)) > 1e-9}
        for line in changed:
            t = got[line]
            assert t.outcome != "held", (cycle_id, line)
            assert "plan" in stages(t) or t.outcome in ("too_small", "not_ordered"), (cycle_id, line)
            count += 1
    assert count >= 20


def test_fixture_journal_outcomes():
    cycles = {cid: by_line(trail.trails(doc, ex, ops)) for cid, doc, ex, ops in _journal_cycles()}
    done = cycles["2026-09-25T1440Z"]
    assert done["SEMIS"].outcome == "changed" and done["GBPUSD"].outcome == "changed"
    assert done["NVDA"].outcome == "reference_trade"
    assert words(done["SEMIS"], "execution").startswith("filled")
    rejected = cycles["2026-09-24T1840Z"]
    assert rejected["SEMIS"].outcome == "rejected" and rejected["OIL"].outcome == "rejected"
    assert "Oil short into the OPEC+ meeting" in words(rejected["OIL"], "human")
    calm = cycles["2026-09-25T0640Z"]
    assert calm["OIL"].outcome == "too_small" and calm["PLTR"].outcome == "held_by_engine"
    assert all(t.outcome == "not_ordered" for t in cycles["2026-09-24T0640Z"].values()
               if t.outcome != "held" and t.line not in ("OIL",))


def test_public_trail_never_prints_licensed_or_private_text():
    from tests.fixtures.make_site_journal import canaries

    text = "".join(trail.render_text(trail.trails(doc, ex, ops)) for _cid, doc, ex, ops in _journal_cycles())
    for canary in canaries():
        assert str(canary) not in text


def test_summary_is_compact():
    rows = trail.summary(trail.record_trails(tr.cut_held_by_risk()))
    assert rows == [{"line": "SEMIS", "outcome": "held_by_engine", "stopped_at": "risk",
                     "asked_by": ["bear", "pm"]}]


def test_evidence_lines_tags_claims_to_lines():
    from council.deliberation.debate import claim_lines, evidence_lines

    lines = {"NDX", "SEMIS", "GOLD"}
    assert evidence_lines(["F:NDX:mom10d", "K:vol:1", "M:DGS10@2026-09-30", "P:1a2b3c4d"], lines=lines,
                          item_lines={"P:1a2b3c4d": ["GOLD", "XYZ"]}, card_scope={"K:vol:1": ["SEMIS", "market"]}
                          ) == ["NDX", "SEMIS", "GOLD"]
    tags = claim_lines(tr.debate(bear_proposal={"NDX": 0.5}), lines=lines, card_scope={"K:vol:1": ["SEMIS"]})
    assert tags == {"bull_open:c1": ["NDX"], "bear:c1": ["SEMIS"], "bear:c2": ["NDX"],
                    "bear:rebuttal:c1": ["NDX"]}
