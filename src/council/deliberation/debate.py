"""The debate: bull opening -> bear (sees the bull) -> bull rebuttal (sees the bear).

Rules:
  - Advocates have no authority; code keeps their JSON for the PM and the record.
  - Proposals are normalised: symbols that are not admitted lines are dropped, levels are snapped
    to the grid. Bear rebuttals must name one of the bull's claim_ids; others are dropped.
  - If the bull opening fails, the bear still argues its own case. If the bear fails, the bull
    rebuttal is skipped (there is nothing to answer), saving a call.
  - Each turn's user message is built from sections (transparency-v2 §2.3): the full desk, the
    earlier turns as `case.<speaker>.plain` (the transcript for the PM is `transcript`), then the
    role's instruction tail. The text is byte-identical to the f-strings it replaced.
  - What code cut is recorded twice: the old notes (`DebateRun.notes`, unchanged wording) and the
    structured drops (`DebateRun.drops`: a proposal entry for a line that is not admitted is
    `non_admitted_line`, a rebuttal of a claim the bull never made is `unknown_bull_claim`).
  - Claim-to-line tags (transparency-v2 §4.1, `claim_lines`): code, not the model, says which lines
    a claim is about, from its evidence ids: `F:`/`V:`/`C:` ids name their line, a card id its scope,
    a news item, event or filing the lines of its symbols; a macro value names none. A bear rebuttal
    is also about the lines of the bull claim it answers. The decision trail uses the tags to show a
    claim, and the manager's dismissal of it, under the line it argued about.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, NamedTuple

from council.deliberation.capture import InputSink
from council.deliberation.common import call_role
from council.deliberation.segments import Segmented, TextBuilder, literal, raw
from council.llm.gateway import Gateway
from council.llm.prompts import PromptRegistry
from council.models.common import snap_level
from council.models.cycle import Debate, RoleCall
from council.models.debate import AdvocateCase, BearCase
from council.models.drops import Drop
from council.models.facts import FactPack
from council.policy import LineSpec

TRANSCRIPT_HEADER = (
    "DEBATE TRANSCRIPT (advocates with ASSIGNED opposite biases; advocacy, not forecasts)"
)
TAIL_BULL_OPEN = "\nOpen the debate with the bull case. Reply with the JSON object only."
TAIL_BEAR = "\n\nReply with the bear case. Reply with the JSON object only."
TAIL_BULL_REBUTTAL = "\n\nAnswer the bear. Reply with the JSON object only."


class DebateRun(NamedTuple):
    debate: Debate
    calls: list[RoleCall]
    notes: list[str]
    raw: dict[str, str]
    drops: tuple[Drop, ...] = ()


def normalise_case[C: AdvocateCase](
    case: C, *, role: str, admitted: set[str], notes: list[str], drops: list[Drop] | None = None,
) -> C:
    """Drop proposal entries for non-admitted symbols and snap proposal levels to the grid.
    Each dropped entry adds a note and, when `drops` is given, a structured `Drop`."""
    proposal: dict[str, float] = {}
    for i, (sym, level) in enumerate(case.proposal.items(), start=1):
        if sym not in admitted:
            notes.append(f"{role}: proposal for non-admitted {sym} dropped")
            if drops is not None:
                drops.append(Drop(role=role, what="proposal_entry", index=i, code="non_admitted_line",
                                  target=str(sym), lines=[str(sym)]))
            continue
        proposal[sym] = snap_level(float(level))
    return case.model_copy(update={"proposal": proposal})


# ------------------------------------------------------------------------- claim-to-line tags
def evidence_lines(
    evidence_ids: Iterable[str],
    *,
    lines: Collection[str],
    item_lines: Mapping[str, Iterable[str]] | None = None,
    card_scope: Mapping[str, Iterable[str]] | None = None,
) -> list[str]:
    """The lines a list of evidence ids is about, in first-seen order (pure): `item_lines` first
    (news, events, filings: id -> symbols), then `F:<line>:…`, `V:<line>:…`, `C:<line>:…`, then a
    card id's scope. Anything that is not one of `lines` is ignored ("market", a macro series)."""
    items = item_lines or {}
    scopes = card_scope or {}
    out: list[str] = []
    for eid in evidence_ids:
        prefix, _, rest = str(eid).partition(":")
        if eid in items:
            candidates: Iterable[str] = items[eid]
        elif prefix in ("F", "V", "C"):
            candidates = (rest.split(":", 1)[0],)
        elif prefix == "K":
            candidates = scopes.get(eid, ())
        else:
            candidates = ()
        for line in candidates:
            if line in lines and line not in out:
                out.append(line)
    return out


def pack_item_lines(pack: FactPack | None) -> dict[str, list[str]]:
    """id -> symbols of the pack's news items, events and filing sentences."""
    if pack is None:
        return {}
    out: dict[str, list[str]] = {n.id: list(n.symbols) for n in pack.news}
    out.update({e.id: list(e.symbols) for e in pack.events})
    for filing in pack.filings:
        out.update({sentence.id: [filing.symbol] for sentence in filing.sentences})
    return out


def claim_lines(
    debate: Debate,
    *,
    lines: Collection[str],
    item_lines: Mapping[str, Iterable[str]] | None = None,
    card_scope: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, list[str]]:
    """Code tags from each advocate claim to the lines its evidence is about: keys
    `bull_open:c1`, `bear:c2`, `bull_rebuttal:c1`, and `bear:rebuttal:c1` for the bear's answer to
    the bull's claim c1 (its own evidence plus the lines of the claim it answers). Claims whose
    evidence names no line are left out."""
    def tag(ids: Iterable[str]) -> list[str]:
        return evidence_lines(ids, lines=lines, item_lines=item_lines, card_scope=card_scope)

    out: dict[str, list[str]] = {}
    for speaker, case in (("bull_open", debate.bull_open), ("bear", debate.bear),
                          ("bull_rebuttal", debate.bull_rebuttal)):
        if case is None:
            continue
        for claim in case.claims:
            found = tag(claim.evidence_ids)
            if found:
                out[f"{speaker}:{claim.claim_id}"] = found
    if debate.bear is not None:
        for r in debate.bear.rebuttals:
            found = tag(r.evidence_ids)
            for line in out.get(f"bull_open:{r.claim_id}", []):
                if line not in found:
                    found.append(line)
            if found:
                out[f"bear:rebuttal:{r.claim_id}"] = found
    return out


def _speaker(label: str, prefix: str) -> str:
    return prefix.rstrip(":") or label.split()[0].lower()


def case_into(
    b: TextBuilder, label: str, case: AdvocateCase | None, *, prefix: str = "", rebut_prefix: str = "",
    speaker: str | None = None,
) -> TextBuilder:
    """Append the compact rendering of an advocate case: every model value is an item whose ref
    names its speaker (`bull_open:c1`, `bear:rebuttal:c2`, ...); labels and prefixes are literals."""
    if case is None:
        return b.lit(f"[{label}] unavailable (the call failed)")
    who = speaker or _speaker(label, prefix)
    src = ("council",)
    b.lit(f"[{label}] ").item("claim", f"{who}:argument", case.argument, sources=src, field="argument")
    b.lit("\n  proposal: ")
    if case.proposal:
        for i, (sym, lvl) in enumerate(case.proposal.items()):
            if i:
                b.lit(", ")
            b.item("text", f"{who}:proposal:{sym}", f"{sym} {lvl:+.2f}", sources=src, line=sym,
                   field="proposal")
    else:
        b.lit("reference everywhere")
    for c in case.claims:
        ref = f"{who}:{c.claim_id}"
        b.lit(f"\n  claim {prefix}").item("claim", ref, c.claim_id, sources=src, field="id")
        b.lit(": ").item("claim", ref, c.text, sources=src, field="text")
        b.lit(" [").item("text", ref, " ".join(c.evidence_ids), sources=src, field="evidence").lit("]")
    b.lit("\n  strongest opposing fact: ")
    b.item("text", f"{who}:strongest", case.strongest_opposing_fact_id, sources=src,
           field="strongest_opposing_fact")
    if case.concessions:
        b.lit("\n  concessions: ")
        for i, text in enumerate(case.concessions):
            if i:
                b.lit(" | ")
            b.item("claim", f"{who}:concession:{i + 1}", text, sources=src, field="concession")
    if isinstance(case, BearCase):
        for r in case.rebuttals:
            ref = f"{who}:rebuttal:{r.claim_id}"
            b.lit(f"\n  rebuttal of {rebut_prefix}").item("claim", ref, r.claim_id, sources=src, field="id")
            b.lit(": ").item("claim", ref, r.verdict, sources=src, field="verdict")
            b.lit(" - ").item("claim", ref, r.text, sources=src, field="text")
            if r.evidence_ids:
                b.lit(" [").item("text", ref, " ".join(r.evidence_ids), sources=src, field="evidence")
                b.lit("]")
    return b


def case_section(
    label: str, case: AdvocateCase | None, *, key: str, prefix: str = "", rebut_prefix: str = "",
    speaker: str | None = None,
) -> Segmented:
    """One advocate case as a section (e.g. `case.bull_open.plain`)."""
    b = TextBuilder(key, "case")
    return case_into(b, label, case, prefix=prefix, rebut_prefix=rebut_prefix, speaker=speaker).build()


def format_case(label: str, case: AdvocateCase | None, *, prefix: str = "", rebut_prefix: str = "") -> str:
    """Compact text rendering of an advocate case (claim IDs optionally prefixed by speaker)."""
    b = TextBuilder("case", "case")
    return case_into(b, label, case, prefix=prefix, rebut_prefix=rebut_prefix).raw_text()


def transcript_section(debate: Debate) -> Segmented:
    """The PM's view of the debate as one section (key `transcript`)."""
    b = TextBuilder("transcript", "transcript").lit(TRANSCRIPT_HEADER).lit("\n\n")
    case_into(b, "BULL opening", debate.bull_open, prefix="bull_open:", speaker="bull_open")
    b.lit("\n\n")
    case_into(b, "BEAR reply", debate.bear, prefix="bear:", rebut_prefix="bull_open:", speaker="bear")
    if debate.bull_rebuttal is not None or debate.bear is not None:
        b.lit("\n\n")
        case_into(b, "BULL rebuttal", debate.bull_rebuttal, prefix="bull_rebuttal:",
                  speaker="bull_rebuttal")
    return b.lit("\n").build()


def transcript(debate: Debate) -> str:
    """The PM's view of the debate. Claim IDs are labelled `bull_open:c1`, `bear:c2`, ..."""
    return transcript_section(debate).text()


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
    desk_text: str | None = None,
    ctx: Mapping[str, Any],
    pack: FactPack,
    lines: Sequence[LineSpec],
    bull_seed: int = 42,
    bear_seed: int = 43,
    bull_num_predict: int = 900,
    bear_num_predict: int = 900,
    desk_sections: Sequence[Segmented] | None = None,
    sink: InputSink | None = None,
    attempt: int = 0,
) -> DebateRun:
    """Run the three debate turns sequentially. `desk_sections` (the full desk) wins over
    `desk_text`; `sink` records each turn's exact input before the call is made."""
    admitted = {ln.symbol for ln in lines} & set(pack.admitted)
    notes: list[str] = []
    drops: list[Drop] = []
    calls: list[RoleCall] = []
    raw_out: dict[str, str] = {}
    desk = list(desk_sections) if desk_sections is not None else [raw("desk", desk_text or "")]

    res = await call_role(
        gw, reg, role="bull_open", ctx=ctx,
        sections=[*desk, literal("tail.bull_open", TAIL_BULL_OPEN, "tail")],
        schema=AdvocateCase, seed=bull_seed, num_predict=bull_num_predict, sink=sink, attempt=attempt,
    )
    calls.append(res.call)
    raw_out["bull_open"] = res.raw
    bull = (
        normalise_case(res.parsed, role="bull_open", admitted=admitted, notes=notes, drops=drops)
        if isinstance(res.parsed, AdvocateCase)
        else None
    )
    bull_plain = case_section("BULL opening", bull, key="case.bull_open.plain", speaker="bull_open")

    res = await call_role(
        gw, reg, role="bear", ctx=ctx,
        sections=[*desk, literal("lit.bear.bull_opening", "\nBULL OPENING\n"), bull_plain,
                  literal("tail.bear", TAIL_BEAR, "tail")],
        schema=BearCase, seed=bear_seed, num_predict=bear_num_predict, sink=sink, attempt=attempt,
    )
    calls.append(res.call)
    raw_out["bear"] = res.raw
    bear: BearCase | None = None
    if isinstance(res.parsed, BearCase):
        bear = normalise_case(res.parsed, role="bear", admitted=admitted, notes=notes, drops=drops)
        bull_claims = {c.claim_id for c in bull.claims} if bull is not None else set()
        kept = []
        for i, r in enumerate(bear.rebuttals, start=1):
            if r.claim_id in bull_claims:
                kept.append(r)
            else:
                notes.append(f"bear: rebuttal of unknown bull claim {r.claim_id} dropped")
                drops.append(Drop(role="bear", what="rebuttal", index=i, code="unknown_bull_claim",
                                  ids=list(r.evidence_ids), target=r.claim_id,
                                  lines=evidence_lines(r.evidence_ids, lines=admitted)))
        bear = bear.model_copy(update={"rebuttals": kept})

    rebuttal: AdvocateCase | None = None
    if bear is not None:
        res = await call_role(
            gw, reg, role="bull_rebuttal", ctx=ctx,
            sections=[
                *desk, literal("lit.bull_rebuttal.your_opening", "\nYOUR OPENING\n"), bull_plain,
                literal("lit.bull_rebuttal.bear_reply", "\n\nBEAR REPLY\n"),
                case_section("BEAR reply", bear, key="case.bear.plain", speaker="bear"),
                literal("tail.bull_rebuttal", TAIL_BULL_REBUTTAL, "tail"),
            ],
            schema=AdvocateCase, seed=bull_seed, num_predict=bull_num_predict, sink=sink,
            attempt=attempt,
        )
        calls.append(res.call)
        raw_out["bull_rebuttal"] = res.raw
        if isinstance(res.parsed, AdvocateCase):
            rebuttal = normalise_case(res.parsed, role="bull_rebuttal", admitted=admitted, notes=notes,
                                      drops=drops)

    return DebateRun(Debate(bull_open=bull, bear=bear, bull_rebuttal=rebuttal), calls, notes, raw_out,
                     tuple(drops))
