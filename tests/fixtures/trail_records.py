"""Synthetic PRIVATE cycle records for the per-line decision trail tests (transparency-v2 T5a).

Four lines, percent-only, no private money values:
  NDX    uptrend, reference 1.00 x 0.30; band fixed at 1.00 (no qualifying card)
  SEMIS  uptrend, reference 1.00 x 0.134; a code volatility card qualifies a cut: band 0.50-1.00
  GOLD   mixed, reference 0.75 x 0.12; band 0.00-1.00
  OIL    downtrend overlay, reference 0.00 x 0.10; band -0.50-0.00 (short allowed)

`record(...)` builds a live cycle whose book sits at the reference; the scenario builders below
change one thing each (one fixture per outcome). Pure: no I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime

from council.models.cards import CardDraft, EvidenceCard
from council.models.cycle import CycleRecord, Debate, PMReplicate
from council.models.debate import AdvocateCase, BearCase, Claim, Rebuttal
from council.models.drops import Drop
from council.models.plan import Leg, Plan
from council.models.pm import DecisiveFact, Deviation, Dismissal, PMDecision
from council.models.reference import ReferenceBook, ReferenceEntry
from council.models.risk import Band, RiskDecision

SLOT = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
CYCLE = "2026-10-01T1440Z"
DECISION = f"{CYCLE}-rebalance-abc123"
# line: (trend, reference level, unit weight, band lo, band hi, band reasons)
LINES: dict[str, tuple[str, float, float, float, float, list[str]]] = {
    "NDX": ("up", 1.0, 0.30, 1.0, 1.0, ["trend up"]),
    "SEMIS": ("up", 1.0, 0.134, 0.5, 1.0, ["trend up", "qualifying card allows a cut"]),
    "GOLD": ("mixed", 0.75, 0.12, 0.0, 1.0, ["trend mixed"]),
    "OIL": ("down", 0.0, 0.10, -0.5, 0.0, ["overlay trend down", "short allowed"]),
}
NAMES = {"NDX": "Nasdaq-100", "SEMIS": "Semiconductors", "GOLD": "Gold", "OIL": "Crude oil"}
BOOK = {s: round(level * unit, 6) for s, (_t, level, unit, *_r) in LINES.items()}
VALUES = {
    "F:NDX:dist_sma50": "+4.1%", "F:NDX:dist_sma200": "+12.3%", "F:NDX:mom10d": "-3.4%",
    "F:SEMIS:dist_sma50": "+2.0%", "F:SEMIS:dist_sma200": "+18.5%", "V:SEMIS:ewma5_60": "2.31x",
    "F:GOLD:trend": "mixed", "F:OIL:trend": "down",
}


def reference() -> ReferenceBook:
    entries = {
        s: ReferenceEntry(symbol=s, sleeve="core" if s != "OIL" else "overlay", asset_class="index",
                          in_reference=s != "OIL", trend=trend, level_ref=level, unit_weight=unit,
                          weight_ref=round(level * unit, 6), sigma_ann=0.2)
        for s, (trend, level, unit, *_rest) in LINES.items()
    }
    return ReferenceBook(cycle_id=CYCLE, entries=entries, k=1.0, target_vol=0.12, ex_ante_vol=0.11, gross=0.52)


def bands(**override: tuple[float, float, list[str], list[str]]) -> dict[str, Band]:
    out = {}
    for s, (trend, level, _unit, lo, hi, reasons) in LINES.items():
        cards = ["K:vol:1"] if s == "SEMIS" else []
        if s in override:
            lo, hi, reasons, cards = override[s]
        out[s] = Band(symbol=s, trend=trend, ref_level=level, lo=lo, hi=hi, reasons=list(reasons),
                      qualifying_cards=list(cards))
    return out


def vol_card() -> EvidenceCard:
    return EvidenceCard(scope=["SEMIS"], card_type="vol_shock", direction="risk_down",
                        claim="Semiconductor five-day volatility is 2.31 times its sixty-day level.",
                        evidence_ids=["V:SEMIS:ewma5_60"], horizon_days=5, card_id="K:vol:1", role="vol",
                        qualifying=True)


def debate(*, bear_proposal: dict[str, float] | None = None) -> Debate:
    bull = AdvocateCase(
        argument="Hold the reference.", proposal={}, strongest_opposing_fact_id="V:SEMIS:ewma5_60",
        claims=[Claim(claim_id="c1", text="The Nasdaq-100 trend is intact.", evidence_ids=["F:NDX:dist_sma50"])],
    )
    bear = BearCase(
        argument="Cut semiconductors.", proposal=dict(bear_proposal if bear_proposal is not None else {"SEMIS": 0.5}),
        strongest_opposing_fact_id="F:NDX:dist_sma50",
        claims=[Claim(claim_id="c1", text="A qualifying volatility card covers semiconductors.",
                      evidence_ids=["K:vol:1", "V:SEMIS:ewma5_60"]),
                Claim(claim_id="c2", text="The Nasdaq-100 fell over ten days.", evidence_ids=["F:NDX:mom10d"])],
        rebuttals=[Rebuttal(claim_id="c1", verdict="refute", text="Ten days of weakness inside an uptrend.",
                            evidence_ids=["F:NDX:mom10d"])],
    )
    rebuttal = AdvocateCase(argument="Concede semiconductors.", proposal={}, strongest_opposing_fact_id="K:vol:1",
                            claims=[], concessions=["The semiconductor card is real."])
    return Debate(bull_open=bull, bear=bear, bull_rebuttal=rebuttal)


def decision(deviations: dict[str, float] | None = None, *, sided: str = "bear",
             dismissed: list[tuple[str, str]] | None = None, reason: str = "") -> PMDecision:
    devs = []
    for sym, level in (deviations or {}).items():
        ref = LINES[sym][1]
        direction = "short" if level < 0 else ("cut" if level < ref else "add")
        devs.append(Deviation(symbol=sym, level=level, direction=direction, evidence_ids=["V:SEMIS:ewma5_60"],
                              reason="Qualifying volatility card; half size."))
    return PMDecision(
        deviations=devs, decisive_fact=DecisiveFact(text="Volatility shock.", evidence_id="V:SEMIS:ewma5_60"),
        sided_with=sided, dismissed=[Dismissal(claim_id=c, why=w) for c, w in (dismissed or [])],
        no_change_reason=reason,
    )


def replicates(decisions: list[PMDecision], *, enforced: list[dict[str, float]] | None = None,
               reverted: list[list[str]] | None = None, valid: list[bool] | None = None) -> list[PMReplicate]:
    ref = {s: v[1] for s, v in LINES.items()}
    out = []
    for i, d in enumerate(decisions):
        rev = (reverted or [[] for _ in decisions])[i]
        levels = (enforced[i] if enforced is not None else
                  {s: (ref[s] if s in rev else lv) for s, lv in d.levels(ref).items()})
        out.append(PMReplicate(replicate=i, seed=42 + i, decision=d,
                               valid=(valid or [True] * len(decisions))[i],
                               audit_violations=[f"{s}: unknown_evidence F:XYZ:mom10d" for s in rev],
                               reverted=rev, enforced_levels=levels))
    return out


def risk(*, raw: dict[str, float], final: dict[str, float] | None = None, base: dict[str, float] | None = None,
         notes: list[str] | None = None, basis: str = "council") -> RiskDecision:
    unit = {s: v[2] for s, v in LINES.items()}
    base_w = dict(BOOK if base is None else base)
    target = {s: round(raw[s] * unit[s], 6) for s in raw}
    final_w = dict(final if final is not None else target)
    return RiskDecision(raw_levels=dict(raw), banded_levels=dict(raw), base_w=base_w, proposed_w=dict(final_w),
                        final_w=final_w, checks=[], gross=sum(abs(v) for v in final_w.values()),
                        net=sum(final_w.values()), margin_use=0.0, stop_budget_used=0.0, stop_budget_limit=0.25,
                        carry_bps_day=0.0, ex_ante_vol=0.11, basis=basis, hold_reasons=list(notes or []))


def plan(legs: dict[str, tuple[float, float]] | None = None, *, skipped: list[str] | None = None) -> Plan:
    out = []
    for seq, (line, (before, after)) in enumerate((legs or {}).items(), start=1):
        kind = "open" if abs(before) < 1e-9 else ("close" if abs(after) < 1e-9 else "partial_close")
        out.append(Leg(seq=seq, kind=kind, symbol=f"{line}.V", line=line, direction="short" if after < 0 else "long",
                       settlement="real", leverage=1, weight_before=before, weight_after=after,
                       cost_bps_nav=0.5, risk_increasing=abs(after) > abs(before), origin="discretionary"))
    return Plan(legs=out, gross_before=0.5, gross_after=0.45, net_before=0.5, net_after=0.45,
                cost_bps_nav=0.5 * len(out), carry_bps_day_nav=0.0, skipped=list(skipped or []))


REF_LEVELS = {s: v[1] for s, v in LINES.items()}


def record(*, mode: str = "live", pm: list[PMReplicate] | None = None, risk_decision: RiskDecision | None = None,
           the_plan: Plan | None = None, decision_state: str = "reviewed_no_action",
           decision_reason: str = "", the_debate: Debate | None = None, the_bands: dict[str, Band] | None = None,
           cards: list[EvidenceCard] | None = None, **extra) -> CycleRecord:
    reps = pm if pm is not None else replicates([decision(sided="reference", reason="Nothing new.")] * 3)
    extra.setdefault("agreement", {s: 1.0 for s in LINES})
    return CycleRecord(
        cycle_id=CYCLE, slot=SLOT, started_at=SLOT, status="on_time", mode=mode, policy_sha="0" * 64,
        model="stub", reference=reference(), cards=cards if cards is not None else [vol_card()],
        bands=the_bands if the_bands is not None else bands(),
        debate=the_debate if the_debate is not None else debate(), pm=reps, medoid_replicate=0,
        risk=risk_decision if risk_decision is not None else risk(raw=dict(REF_LEVELS)),
        plan=the_plan, decision_id=DECISION if the_plan is not None and the_plan.legs else None,
        decision_state=decision_state, decision_reason=decision_reason,
        evidence_values=dict(VALUES), **extra,
    )


# ------------------------------------------------------------------------------ scenarios
def cut_executed(**kw) -> CycleRecord:
    """The manager cut SEMIS to 0.50 (3 of 3), the engine passed it, the plan has a leg; the ledger
    later says completed and the leg filled."""
    d = decision({"SEMIS": 0.5})
    raw = {**REF_LEVELS, "SEMIS": 0.5}
    return record(pm=replicates([d, d, d]), risk_decision=risk(raw=raw),
                  the_plan=plan({"SEMIS": (0.134, 0.067)}), decision_state="awaiting_publication", **kw)


def cut_held_by_risk(note: str = "SEMIS: R12 minimum hold") -> CycleRecord:
    """The manager cut SEMIS to 0.50; the risk engine held it (the note names the rule)."""
    d = decision({"SEMIS": 0.5})
    raw = {**REF_LEVELS, "SEMIS": 0.5}
    return record(pm=replicates([d, d, d]), risk_decision=risk(raw=raw, final=dict(BOOK), notes=[note]),
                  the_plan=plan())


def bear_asked_pm_held() -> CycleRecord:
    """The bear asked for NDX at 0.50 in an uptrend with no qualifying card; no attempt deviated and
    one set aside the bear's NDX claim."""
    hold = decision(sided="reference", dismissed=[("bear:c2", "A ten-day dip inside an uptrend.")],
                    reason="Trends intact.")
    plain = decision(sided="reference", reason="Trends intact.")
    return record(the_debate=debate(bear_proposal={"NDX": 0.5}), pm=replicates([plain, hold, plain]),
                  claim_lines={"bull_open:c1": ["NDX"], "bear:c1": ["SEMIS"], "bear:c2": ["NDX"],
                               "bear:rebuttal:c1": ["NDX"]})


def rehearsal() -> CycleRecord:
    """No broker account: the book counts as flat, the target is the reference, no plan."""
    return record(mode="dry_run", risk_decision=risk(raw=dict(REF_LEVELS), base={},
                                                     notes=["no broker snapshot: current book taken as flat"]))


def news_unlock() -> CycleRecord:
    """A corroborated news card unlocked the SEMIS cut: before the analysts' cards the band was
    fixed at 1.00. A second news draft was dropped (it cited an id the pack did not have)."""
    news = EvidenceCard(scope=["SEMIS"], card_type="news_material", direction="risk_down",
                        claim="A Federal Reserve release tightened chip export financing.",
                        evidence_ids=["P:1a2b3c4d"], horizon_days=5, card_id="K:news:1", role="news",
                        corroborated_by=["K:vol:1"], qualifying=True)
    draft = CardDraft(scope=["SEMIS"], card_type="news_material", direction="risk_down", claim="Invented.",
                      evidence_ids=["P:deadbeef"], horizon_days=5)
    d = decision({"SEMIS": 0.5})
    code = Band(symbol="SEMIS", trend="up", ref_level=1.0, lo=1.0, hi=1.0, reasons=["trend up"])
    unlocked = bands(SEMIS=(0.5, 1.0, ["trend up", "qualifying card allows a cut"], ["K:news:1"]))
    return record(cards=[vol_card().model_copy(update={"qualifying": False}), news], the_bands=unlocked,
                  pm=replicates([d, d, d]),
                  risk_decision=risk(raw={**REF_LEVELS, "SEMIS": 0.5}), the_plan=plan({"SEMIS": (0.134, 0.067)}),
                  decision_state="awaiting_publication", code_bands={"SEMIS": code},
                  drops=[Drop(role="news", what="card_draft", index=2, code="unknown_evidence",
                              ids=["P:deadbeef"], lines=["SEMIS"], draft=draft)],
                  extras={"news_fetch": {"public_items": [{"id": "P:1a2b3c4d", "source": "fed_board"}]}})
