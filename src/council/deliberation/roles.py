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
  - Macro drivers citing unknown IDs are dropped; sleeve tilts for unknown sleeves are dropped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from council.deliberation.common import admissible_ids, call_role
from council.deliberation.desk import news_detail
from council.llm.gateway import Gateway
from council.llm.prompts import PromptRegistry
from council.models.cards import CardDraft, EvidenceCard, MacroAnalystOutput, NewsAnalystOutput
from council.models.cycle import RoleCall
from council.models.facts import FactPack
from council.policy import LineSpec, Policy

NEWS_TYPES = frozenset({"news_material", "news_context"})
MACRO_TYPES = frozenset({"macro_context"})
CORROBORATION_WINDOW = timedelta(hours=24)


class NewsRun(NamedTuple):
    cards: list[EvidenceCard]
    calls: list[RoleCall]
    dropped: list[str]
    raw: str


class MacroRun(NamedTuple):
    output: MacroAnalystOutput | None
    cards: list[EvidenceCard]
    calls: list[RoleCall]
    dropped: list[str]
    raw: str


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
    pack_ids = admissible_ids(pack)
    scope_ok = admitted_lines(pack, lines)
    cards: list[EvidenceCard] = []
    dropped: list[str] = []
    for i, draft in enumerate(drafts, start=1):
        problem = draft_problem(
            draft, pack_ids=pack_ids, scope_ok=scope_ok, allowed_types=allowed_types
        )
        if problem is not None:
            dropped.append(f"{role} draft {i}: {problem}")
            continue
        corroborated: list[str] = []
        qualifying = False
        if draft.card_type == "news_material":
            corroborated = corroborating_vol_cards(draft, corroborators, now)
            qualifying = bool(corroborated) and "news_material" in qualifying_types
        cards.append(
            EvidenceCard(
                **draft.model_dump(),
                card_id=f"K:{role}:{len(cards) + 1}",
                role=role,
                corroborated_by=corroborated,
                qualifying=qualifying,
            )
        )
    return cards, dropped


async def run_news(
    *,
    gw: Gateway,
    reg: PromptRegistry,
    pack: FactPack,
    desk_text: str,
    ctx: Mapping[str, Any],
    lines: Sequence[LineSpec],
    policy: Policy,
    corroborators: Sequence[tuple[EvidenceCard, datetime]],
    now: datetime,
    seed: int = 42,
    num_predict: int = 1200,
) -> NewsRun:
    """One news-analyst call -> checked evidence cards."""
    user = (
        f"{desk_text}\n{news_detail(pack)}\n"
        "Write the news cards now. Reply with the JSON object only."
    )
    res = await call_role(
        gw, reg, role="news", ctx=ctx, user=user, schema=NewsAnalystOutput,
        seed=seed, num_predict=num_predict,
    )
    if not isinstance(res.parsed, NewsAnalystOutput):
        return NewsRun([], [res.call], [], res.raw)
    cards, dropped = accept_drafts(
        res.parsed.cards,
        role="news",
        pack=pack,
        lines=lines,
        allowed_types=NEWS_TYPES,
        qualifying_types=policy.risk["authority"]["qualifying_cut_cards"],
        corroborators=corroborators,
        now=now,
    )
    return NewsRun(cards, [res.call], dropped, res.raw)


async def run_macro(
    *,
    gw: Gateway,
    reg: PromptRegistry,
    pack: FactPack,
    desk_text: str,
    ctx: Mapping[str, Any],
    lines: Sequence[LineSpec],
    policy: Policy,
    now: datetime,
    seed: int = 42,
    num_predict: int = 700,
) -> MacroRun:
    """One macro-analyst call -> a cleaned MacroAnalystOutput plus its checked cards."""
    user = f"{desk_text}\nDescribe the regime now. Reply with the JSON object only."
    res = await call_role(
        gw, reg, role="macro", ctx=ctx, user=user, schema=MacroAnalystOutput,
        seed=seed, num_predict=num_predict,
    )
    if not isinstance(res.parsed, MacroAnalystOutput):
        return MacroRun(None, [], [res.call], [], res.raw)
    out = res.parsed
    pack_ids = admissible_ids(pack)
    dropped: list[str] = []
    drivers = []
    for i, drv in enumerate(out.drivers, start=1):
        unknown = [e for e in drv.evidence_ids if e not in pack_ids]
        if unknown:
            dropped.append(f"macro driver {i}: unknown_evidence {','.join(unknown)}")
        else:
            drivers.append(drv)
    sleeves = {ln.sleeve for ln in lines}
    tilts = {}
    for sleeve, tilt in out.sleeve_tilts.items():
        if sleeve in sleeves:
            tilts[sleeve] = tilt
        else:
            dropped.append(f"macro tilt: unknown_sleeve {sleeve}")
    cards, card_drops = accept_drafts(
        out.cards,
        role="macro",
        pack=pack,
        lines=lines,
        allowed_types=MACRO_TYPES,
        qualifying_types=policy.risk["authority"]["qualifying_cut_cards"],
        corroborators=(),
        now=now,
    )
    dropped += card_drops
    kept_drafts = [CardDraft(**c.model_dump(include=set(CardDraft.model_fields))) for c in cards]
    clean = out.model_copy(update={"drivers": drivers, "sleeve_tilts": tilts, "cards": kept_drafts})
    return MacroRun(clean, cards, [res.call], dropped, res.raw)
