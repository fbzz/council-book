"""The debate: bull opening -> bear (sees the bull) -> bull rebuttal (sees the bear).

Rules:
  - Advocates have no authority; code keeps their JSON for the PM and the record.
  - Proposals are normalised: symbols that are not admitted lines are dropped, levels are snapped
    to the grid. Bear rebuttals must name one of the bull's claim_ids; others are dropped.
  - If the bull opening fails, the bear still argues its own case. If the bear fails, the bull
    rebuttal is skipped (there is nothing to answer), saving a call.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from council.deliberation.common import call_role
from council.llm.gateway import Gateway
from council.llm.prompts import PromptRegistry
from council.models.common import snap_level
from council.models.cycle import Debate, RoleCall
from council.models.debate import AdvocateCase, BearCase
from council.models.facts import FactPack
from council.policy import LineSpec


class DebateRun(NamedTuple):
    debate: Debate
    calls: list[RoleCall]
    notes: list[str]
    raw: dict[str, str]


def normalise_case[C: AdvocateCase](
    case: C, *, role: str, admitted: set[str], notes: list[str]
) -> C:
    """Drop proposal entries for non-admitted symbols and snap proposal levels to the grid."""
    proposal: dict[str, float] = {}
    for sym, level in case.proposal.items():
        if sym not in admitted:
            notes.append(f"{role}: proposal for non-admitted {sym} dropped")
            continue
        proposal[sym] = snap_level(float(level))
    return case.model_copy(update={"proposal": proposal})


def format_case(label: str, case: AdvocateCase | None, *, prefix: str = "", rebut_prefix: str = "") -> str:
    """Compact text rendering of an advocate case (claim IDs optionally prefixed by speaker)."""
    if case is None:
        return f"[{label}] unavailable (the call failed)"
    out = [f"[{label}] {case.argument}"]
    prop = ", ".join(f"{s} {lvl:+.2f}" for s, lvl in case.proposal.items()) or "reference everywhere"
    out.append(f"  proposal: {prop}")
    for c in case.claims:
        out.append(f"  claim {prefix}{c.claim_id}: {c.text} [{' '.join(c.evidence_ids)}]")
    out.append(f"  strongest opposing fact: {case.strongest_opposing_fact_id}")
    if case.concessions:
        out.append("  concessions: " + " | ".join(case.concessions))
    if isinstance(case, BearCase):
        for r in case.rebuttals:
            ids = f" [{' '.join(r.evidence_ids)}]" if r.evidence_ids else ""
            out.append(f"  rebuttal of {rebut_prefix}{r.claim_id}: {r.verdict} - {r.text}{ids}")
    return "\n".join(out)


def transcript(debate: Debate) -> str:
    """The PM's view of the debate. Claim IDs are labelled `bull_open:c1`, `bear:c2`, ..."""
    parts = [
        "DEBATE TRANSCRIPT (advocates with ASSIGNED opposite biases; advocacy, not forecasts)",
        format_case("BULL opening", debate.bull_open, prefix="bull_open:"),
        format_case("BEAR reply", debate.bear, prefix="bear:", rebut_prefix="bull_open:"),
    ]
    if debate.bull_rebuttal is not None or debate.bear is not None:
        parts.append(format_case("BULL rebuttal", debate.bull_rebuttal, prefix="bull_rebuttal:"))
    return "\n\n".join(parts) + "\n"


def claim_ids(debate: Debate) -> set[str]:
    """Every claim label the PM may dismiss."""
    out: set[str] = set()
    for label, case in (
        ("bull_open", debate.bull_open), ("bear", debate.bear), ("bull_rebuttal", debate.bull_rebuttal)
    ):
        if case is not None:
            out |= {f"{label}:{c.claim_id}" for c in case.claims}
    return out


async def run_debate(
    *,
    gw: Gateway,
    reg: PromptRegistry,
    desk_text: str,
    ctx: Mapping[str, Any],
    pack: FactPack,
    lines: Sequence[LineSpec],
    bull_seed: int = 42,
    bear_seed: int = 43,
    bull_num_predict: int = 900,
    bear_num_predict: int = 900,
) -> DebateRun:
    """Run the three debate turns sequentially."""
    admitted = {ln.symbol for ln in lines} & set(pack.admitted)
    notes: list[str] = []
    calls: list[RoleCall] = []
    raw: dict[str, str] = {}

    res = await call_role(
        gw, reg, role="bull_open", ctx=ctx,
        user=f"{desk_text}\nOpen the debate with the bull case. Reply with the JSON object only.",
        schema=AdvocateCase, seed=bull_seed, num_predict=bull_num_predict,
    )
    calls.append(res.call)
    raw["bull_open"] = res.raw
    bull = (
        normalise_case(res.parsed, role="bull_open", admitted=admitted, notes=notes)
        if isinstance(res.parsed, AdvocateCase)
        else None
    )

    res = await call_role(
        gw, reg, role="bear", ctx=ctx,
        user=(
            f"{desk_text}\nBULL OPENING\n{format_case('BULL opening', bull)}\n\n"
            "Reply with the bear case. Reply with the JSON object only."
        ),
        schema=BearCase, seed=bear_seed, num_predict=bear_num_predict,
    )
    calls.append(res.call)
    raw["bear"] = res.raw
    bear: BearCase | None = None
    if isinstance(res.parsed, BearCase):
        bear = normalise_case(res.parsed, role="bear", admitted=admitted, notes=notes)
        bull_claims = {c.claim_id for c in bull.claims} if bull is not None else set()
        kept = []
        for r in bear.rebuttals:
            if r.claim_id in bull_claims:
                kept.append(r)
            else:
                notes.append(f"bear: rebuttal of unknown bull claim {r.claim_id} dropped")
        bear = bear.model_copy(update={"rebuttals": kept})

    rebuttal: AdvocateCase | None = None
    if bear is not None:
        res = await call_role(
            gw, reg, role="bull_rebuttal", ctx=ctx,
            user=(
                f"{desk_text}\nYOUR OPENING\n{format_case('BULL opening', bull)}\n\n"
                f"BEAR REPLY\n{format_case('BEAR reply', bear)}\n\n"
                "Answer the bear. Reply with the JSON object only."
            ),
            schema=AdvocateCase, seed=bull_seed, num_predict=bull_num_predict,
        )
        calls.append(res.call)
        raw["bull_rebuttal"] = res.raw
        if isinstance(res.parsed, AdvocateCase):
            rebuttal = normalise_case(res.parsed, role="bull_rebuttal", admitted=admitted, notes=notes)

    return DebateRun(Debate(bull_open=bull, bear=bear, bull_rebuttal=rebuttal), calls, notes, raw)
