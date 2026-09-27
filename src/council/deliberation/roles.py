"""LLM analysts (news, macro). They write card DRAFTS; code checks them and assigns card IDs.

Rules:
  - A card draft is kept only if every evidence ID is in the pack (`pack.evidence_ids()`, minus
    anything stamped available after the slot), every
    scope symbol is an admitted line, and its card_type is one the role may write (news:
    news_material/news_context; macro: macro_context). Anything else is dropped with a reason.
  - Code assigns `K:<role>:<n>` to kept cards, in the order the model wrote them.
  - A `news_material` card is qualifying only if a code `vol_shock` card on a shared line exists
    within the last 24h; `corroborated_by` lists those vol cards. `news_context` never qualifies.
    The council passes only THIS cycle's vol cards (card IDs are per cycle; no carry-forward in
    v1), so in practice the vol card must be live at the slot.
  - Filing metadata never unlocks a cut (transparency-v2 T-D17): the model sees an SEC 8-K / 6-K
    item's form and item codes only, never the filing's content, so a `news_material` card whose
    cited news items are all SEC filing items is kept but never qualifying, whatever corroborates
    it; the note `filing_metadata_only` records why. One non-SEC news item among its evidence (a
    Federal Reserve release, a broker feed item) lets the usual corroboration rule decide.
  - Macro drivers citing unknown IDs are dropped; sleeve tilts for unknown sleeves are dropped.
  - Every drop is recorded twice: as the old free-text note (`dropped`, unchanged wording) and as a
    structured `models.drops.Drop` (`drops`: role, what, index, code, ids, lines, the draft), which
    the per-line decision trail shows next to the line it concerned (transparency-v2 §4.3).
  - Inputs (transparency-v2 §2.3): news reads `desk.code.*`, `sep.news`, `news_detail`,
    `tail.news`; macro reads `desk.code.*`, `tail.macro`. `sink` records each exact input.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from council.deliberation.capture import InputSink
from council.deliberation.common import admissible_ids, call_role
from council.deliberation.desk import news_detail_section
from council.deliberation.segments import Segmented, literal, raw
from council.llm.gateway import Gateway
from council.llm.prompts import PromptRegistry
from council.models.cards import CardDraft, EvidenceCard, MacroAnalystOutput, NewsAnalystOutput
from council.models.cycle import RoleCall
from council.models.drops import Drop, drop_from_problem
from council.models.facts import FactPack
from council.policy import LineSpec, Policy

NEWS_TYPES = frozenset({"news_material", "news_context"})
MACRO_TYPES = frozenset({"macro_context"})
CORROBORATION_WINDOW = timedelta(hours=24)
FILING_SOURCE = "sec"
FILING_ONLY_NOTE = "filing_metadata_only (cannot unlock a cut)"
TAIL_NEWS = "\nWrite the news cards now. Reply with the JSON object only."
TAIL_MACRO = "\nDescribe the regime now. Reply with the JSON object only."


class NewsRun(NamedTuple):
    cards: list[EvidenceCard]
    calls: list[RoleCall]
    dropped: list[str]
    raw: str
    drops: tuple[Drop, ...] = ()


class MacroRun(NamedTuple):
    output: MacroAnalystOutput | None
    cards: list[EvidenceCard]
    calls: list[RoleCall]
    dropped: list[str]
    raw: str
    drops: tuple[Drop, ...] = ()


class DraftCheck(NamedTuple):
    cards: list[EvidenceCard]
    dropped: list[str]             # the free-text notes (unchanged wording)
    drops: list[Drop]              # the same drops, structured


def _desk(desk_text: str | None, desk_sections: Sequence[Segmented] | None) -> list[Segmented]:
    return list(desk_sections) if desk_sections is not None else [raw("desk", desk_text or "")]


def admitted_lines(pack: FactPack, lines: Sequence[LineSpec]) -> set[str]:
    return {ln.symbol for ln in lines} & set(pack.admitted)


def draft_problem(
    draft: CardDraft, *, pack_ids: set[str], scope_ok: set[str], allowed_types: frozenset[str]
) -> str | None:
    """Why a draft must be dropped, or None if it is acceptable."""
    if draft.card_type not in allowed_types:
        return f"type_not_allowed {draft.card_type}"
    unknown_scope = [s for s in draft.scope if s not in scope_ok]
    if unknown_scope:
        return f"scope_not_admitted {','.join(unknown_scope)}"
    unknown_ids = [e for e in draft.evidence_ids if e not in pack_ids]
    if unknown_ids:
        return f"unknown_evidence {','.join(unknown_ids)}"
    return None


def corroborating_vol_cards(
    draft: CardDraft,
    corroborators: Sequence[tuple[EvidenceCard, datetime]],
    now: datetime,
) -> list[str]:
    """IDs of code vol_shock cards sharing a line with `draft`, issued within 24h before `now`."""
    scope = set(draft.scope)
    out = []
    for card, at in corroborators:
        if card.card_type != "vol_shock" or card.role != "vol":
            continue
        if not (timedelta(0) <= now - at <= CORROBORATION_WINDOW):
            continue
        if scope & set(card.scope) and card.card_id not in out:
            out.append(card.card_id)
    return out


def filing_metadata_only(draft: CardDraft, pack: FactPack) -> bool:
    """True when every news item the draft cites is an SEC filing item (8-K / 6-K metadata), i.e.
    the card's news evidence is metadata whose content the model never saw (T-D17)."""
    by_id = {n.id: n for n in pack.news}
    cited = [by_id[e] for e in draft.evidence_ids if e in by_id]
    return bool(cited) and all(n.source == FILING_SOURCE for n in cited)


def check_drafts(
    drafts: Sequence[CardDraft],
    *,
    role: str,
    pack: FactPack,
    lines: Sequence[LineSpec],
    allowed_types: frozenset[str],
    qualifying_types: Sequence[str],
    corroborators: Sequence[tuple[EvidenceCard, datetime]],
    now: datetime,
) -> DraftCheck:
    """Check drafts, assign IDs, set corroboration/qualifying. Returns the kept cards, the drop
    notes (with the filing-metadata labels, as before) and the structured drops."""
    pack_ids = admissible_ids(pack)
    scope_ok = admitted_lines(pack, lines)
    cards: list[EvidenceCard] = []
    dropped: list[str] = []
    drops: list[Drop] = []
    for i, draft in enumerate(drafts, start=1):
        problem = draft_problem(
            draft, pack_ids=pack_ids, scope_ok=scope_ok, allowed_types=allowed_types
        )
        if problem is not None:
            dropped.append(f"{role} draft {i}: {problem}")
            drops.append(drop_from_problem(problem, role=role, index=i, draft=draft))
            continue
        corroborated: list[str] = []
        qualifying = False
        if draft.card_type == "news_material":
            corroborated = corroborating_vol_cards(draft, corroborators, now)
            filing_only = filing_metadata_only(draft, pack)
            qualifying = bool(corroborated) and "news_material" in qualifying_types and not filing_only
            if filing_only:
                dropped.append(f"{role} card K:{role}:{len(cards) + 1}: {FILING_ONLY_NOTE}")
        cards.append(
            EvidenceCard(
                **draft.model_dump(),
                card_id=f"K:{role}:{len(cards) + 1}",
                role=role,
                corroborated_by=corroborated,
                qualifying=qualifying,
            )
        )
    return DraftCheck(cards, dropped, drops)


def accept_drafts(
    drafts: Sequence[CardDraft],
    *,
    role: str,
    pack: FactPack,
    lines: Sequence[LineSpec],
    allowed_types: frozenset[str],
    qualifying_types: Sequence[str],
    corroborators: Sequence[tuple[EvidenceCard, datetime]],
    now: datetime,
) -> tuple[list[EvidenceCard], list[str]]:
    """Check drafts, assign IDs, set corroboration/qualifying. Returns (cards, drop reasons)."""
    checked = check_drafts(drafts, role=role, pack=pack, lines=lines, allowed_types=allowed_types,
                           qualifying_types=qualifying_types, corroborators=corroborators, now=now)
    return checked.cards, checked.dropped


async def run_news(
    *,
    gw: Gateway,
    reg: PromptRegistry,
    pack: FactPack,
    desk_text: str | None = None,
    ctx: Mapping[str, Any],
    lines: Sequence[LineSpec],
    policy: Policy,
    corroborators: Sequence[tuple[EvidenceCard, datetime]],
    now: datetime,
    seed: int = 42,
    num_predict: int = 1200,
    desk_sections: Sequence[Segmented] | None = None,
    sink: InputSink | None = None,
    attempt: int = 0,
) -> NewsRun:
    """One news-analyst call -> checked evidence cards."""
    sections = [*_desk(desk_text, desk_sections), literal("sep.news", "\n"),
                news_detail_section(pack), literal("tail.news", TAIL_NEWS, "tail")]
    res = await call_role(
        gw, reg, role="news", ctx=ctx, sections=sections, schema=NewsAnalystOutput,
        seed=seed, num_predict=num_predict, sink=sink, attempt=attempt,
    )
    if not isinstance(res.parsed, NewsAnalystOutput):
        return NewsRun([], [res.call], [], res.raw)
    checked = check_drafts(
        res.parsed.cards,
        role="news",
        pack=pack,
        lines=lines,
        allowed_types=NEWS_TYPES,
        qualifying_types=policy.risk["authority"]["qualifying_cut_cards"],
        corroborators=corroborators,
        now=now,
    )
    return NewsRun(checked.cards, [res.call], checked.dropped, res.raw, tuple(checked.drops))


async def run_macro(
    *,
    gw: Gateway,
    reg: PromptRegistry,
    pack: FactPack,
    desk_text: str | None = None,
    ctx: Mapping[str, Any],
    lines: Sequence[LineSpec],
    policy: Policy,
    now: datetime,
    seed: int = 42,
    num_predict: int = 700,
    desk_sections: Sequence[Segmented] | None = None,
    sink: InputSink | None = None,
    attempt: int = 0,
) -> MacroRun:
    """One macro-analyst call -> a cleaned MacroAnalystOutput plus its checked cards."""
    sections = [*_desk(desk_text, desk_sections), literal("tail.macro", TAIL_MACRO, "tail")]
    res = await call_role(
        gw, reg, role="macro", ctx=ctx, sections=sections, schema=MacroAnalystOutput,
        seed=seed, num_predict=num_predict, sink=sink, attempt=attempt,
    )
    if not isinstance(res.parsed, MacroAnalystOutput):
        return MacroRun(None, [], [res.call], [], res.raw)
    out = res.parsed
    pack_ids = admissible_ids(pack)
    dropped: list[str] = []
    drops: list[Drop] = []
    drivers = []
    for i, drv in enumerate(out.drivers, start=1):
        unknown = [e for e in drv.evidence_ids if e not in pack_ids]
        if unknown:
            dropped.append(f"macro driver {i}: unknown_evidence {','.join(unknown)}")
            drops.append(Drop(role="macro", what="macro_driver", index=i, code="unknown_evidence",
                              ids=unknown))
        else:
            drivers.append(drv)
    sleeves = {ln.sleeve for ln in lines}
    tilts = {}
    for i, (sleeve, tilt) in enumerate(out.sleeve_tilts.items(), start=1):
        if sleeve in sleeves:
            tilts[sleeve] = tilt
        else:
            dropped.append(f"macro tilt: unknown_sleeve {sleeve}")
            drops.append(Drop(role="macro", what="macro_tilt", index=i, code="unknown_sleeve",
                              target=str(sleeve)))
    checked = check_drafts(
        out.cards,
        role="macro",
        pack=pack,
        lines=lines,
        allowed_types=MACRO_TYPES,
        qualifying_types=policy.risk["authority"]["qualifying_cut_cards"],
        corroborators=(),
        now=now,
    )
    cards = checked.cards
    dropped += checked.dropped
    drops += checked.drops
    kept_drafts = [CardDraft(**c.model_dump(include=set(CardDraft.model_fields))) for c in cards]
    clean = out.model_copy(update={"drivers": drivers, "sleeve_tilts": tilts, "cards": kept_drafts})
    return MacroRun(clean, cards, [res.call], dropped, res.raw, tuple(drops))
