"""The news analyst's reading list: one disposition per news item (transparency-v2 §3.6).

For every news item an agent read, code (not a model) says what happened to it:
  - `read_by`: every role whose input listed the item (the news analyst reads `news_detail`; every
    role whose desk listed the headline read the headline);
  - `cited_by`: every card, advocate claim or rebuttal, strongest-opposing fact, macro driver, PM or
    control deviation and decisive fact whose evidence contains the id;
  - `disposition`: `card` when a news-analyst card cites it, else `cited` when anyone cites it,
    else `not_cited`; `used` is True for the first two;
  - `why`: one fixed-template sentence (no model writes it). A later triage (news.md v2, T4) adds
    the analyst's own verdict and reason code for items it did not use.
Every read item appears exactly once, in reading order (newest first). The private viewer reads
the capture (`reads_from_inputs`); the redaction layer and cycles without a capture rebuild the same
list from the fact pack (`reads_from_pack`). Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from council.deliberation.capture import section_parts
from council.models.inputs import CycleInputs, LicensedInputs

Disposition = Literal["card", "cited", "not_cited"]
CitationKind = Literal["card", "claim", "rebuttal", "strongest", "driver", "deviation", "decisive"]
SPEAKERS = ("bull_open", "bear", "bull_rebuttal")


@dataclass(frozen=True)
class NewsRead:
    """One news item as the agents saw it."""

    id: str
    source: str = ""
    licence: str = "public"
    symbols: str = ""
    age: str = ""
    title: str = ""
    summary: str = ""
    available: bool = True          # False once a licensed text was purged


@dataclass(frozen=True)
class Citation:
    by: str                         # "K:news:1", "bear:c1", "bear:rebuttal:c2", "pm:0:decisive", ...
    kind: CitationKind
    role: str
    ids: tuple[str, ...]


@dataclass(frozen=True)
class Reading:
    id: str
    source: str
    licence: str
    title: str
    summary: str
    symbols: str
    age: str
    available: bool
    read_by: tuple[str, ...]
    cited_by: tuple[str, ...]
    cards: tuple[str, ...]
    disposition: Disposition
    used: bool
    why: str
    triage: tuple[str, str, str] | None = None   # (verdict, code, why) from the analyst, when present
    link: str = ""   # a public-domain item's source link (private record `extras.news_fetch`); never a broker item's


# ------------------------------------------------------------------------------- what was read
def reads_from_inputs(
    inputs: CycleInputs, licensed: LicensedInputs | None = None
) -> tuple[list[NewsRead], dict[str, tuple[str, ...]]]:
    """The news items in a private capture (reading order) and the roles that read each."""
    fields: dict[str, dict[str, Any]] = {}
    readers: dict[str, list[str]] = {}
    for call in inputs.calls:
        for key in call.sections:
            section = inputs.sections.get(key)
            if section is None:
                continue
            for text, idx, available in section_parts(section, licensed):
                if idx is None:
                    continue
                item = section.items[idx]
                if item.kind != "news":
                    continue
                row = fields.setdefault(item.ref, {"id": item.ref, "available": True})
                if item.field in ("symbols", "age", "title", "summary") and item.field not in row:
                    row[item.field] = text
                    if not available:
                        row["available"] = False
                if item.field == "id" and item.sources and "source" not in row:
                    row["source"] = item.sources[0]
                if item.field == "title":
                    row["licence"] = item.licence
                roles = readers.setdefault(item.ref, [])
                if call.role not in roles:
                    roles.append(call.role)
    reads = [NewsRead(**row) for row in fields.values()]
    return reads, {k: tuple(v) for k, v in readers.items()}


def reads_from_pack(
    pack: Any, roles: Sequence[str]
) -> tuple[list[NewsRead], dict[str, tuple[str, ...]]]:
    """The same as `reads_from_inputs`, rebuilt from the fact pack when there is no capture (the
    redaction layer, an old cycle). `roles`: the roles that made a model call, in call order. Every
    desk lists the same newest headlines as `news_detail` (`desk.shown_news`), so each shown item
    was read by every such role; only the news analyst read the summaries."""
    from council.deliberation.desk import (
        SUMMARY_CHARS,
        TITLE_CHARS,
        clean,
        news_age,
        news_licence,
        news_source,
        shown_news,
    )

    readers = tuple(dict.fromkeys(roles))
    news_read = "news" in readers
    reads = [
        NewsRead(
            id=item.id, source=news_source(item), licence=news_licence(item),
            symbols=",".join(item.symbols) if item.symbols else "-", age=news_age(pack, item),
            title=clean(item.title, TITLE_CHARS),
            summary=clean(item.summary, SUMMARY_CHARS) if item.summary and news_read else "",
        )
        for item in shown_news(pack)
    ]
    return reads, {r.id: readers for r in reads}


# ----------------------------------------------------------------------------- who cited what
def _ids(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Iterable):
        return tuple(str(v) for v in value if v)
    return ()


def citations_from_record(record: Mapping[str, Any]) -> list[Citation]:
    """Every citation in a cycle record (the ledger's JSON of `CycleRecord`, or the same keys
    built from a `CouncilResult`: cards, macro, debate, pm, single_agent)."""
    out: list[Citation] = []
    for card in record.get("cards") or []:
        out.append(Citation(str(card.get("card_id", "")), "card", str(card.get("role", "")),
                            _ids(card.get("evidence_ids"))))
    macro = record.get("macro") or {}
    for i, drv in enumerate(macro.get("drivers") or [], start=1):
        out.append(Citation(f"macro:driver:{i}", "driver", "macro", _ids(drv.get("evidence_ids"))))
    debate = record.get("debate") or {}
    for speaker in SPEAKERS:
        case = debate.get(speaker)
        if not case:
            continue
        for claim in case.get("claims") or []:
            out.append(Citation(f"{speaker}:{claim.get('claim_id')}", "claim", speaker,
                                _ids(claim.get("evidence_ids"))))
        out.append(Citation(f"{speaker}:strongest", "strongest", speaker,
                            _ids(case.get("strongest_opposing_fact_id"))))
        for reb in case.get("rebuttals") or []:
            out.append(Citation(f"{speaker}:rebuttal:{reb.get('claim_id')}", "rebuttal", speaker,
                                _ids(reb.get("evidence_ids"))))
    for role in ("pm", "single_agent"):
        for rep in record.get(role) or []:
            decision = rep.get("decision") or {}
            n = rep.get("replicate", 0)
            for dev in decision.get("deviations") or []:
                out.append(Citation(f"{role}:{n}:deviation:{dev.get('symbol')}", "deviation", role,
                                    _ids(dev.get("evidence_ids"))))
            fact = decision.get("decisive_fact") or {}
            if fact.get("evidence_id"):
                out.append(Citation(f"{role}:{n}:decisive", "decisive", role, _ids(fact.get("evidence_id"))))
    return [c for c in out if c.ids]


def citations_from_models(
    *, cards: Sequence[Any] = (), macro: Any = None, debate: Any = None, pm: Sequence[Any] = (),
    single_agent: Sequence[Any] = (),
) -> list[Citation]:
    """The same as `citations_from_record`, from the council's pydantic models."""
    def dump(v: Any) -> Any:
        return v.model_dump(mode="json") if hasattr(v, "model_dump") else v

    return citations_from_record({
        "cards": [dump(c) for c in cards], "macro": dump(macro) if macro is not None else None,
        "debate": dump(debate) if debate is not None else None,
        "pm": [dump(r) for r in pm], "single_agent": [dump(r) for r in single_agent],
    })


# ------------------------------------------------------------------------------- dispositions
def _why(disposition: Disposition, cards: Sequence[str], cited: Sequence[str], read_by: Sequence[str],
         triage: tuple[str, str, str] | None) -> str:
    if disposition == "card":
        others = [c for c in cited if c not in cards]
        text = f"made into {'card' if len(cards) == 1 else 'cards'} {', '.join(cards)}"
        return text + (f"; also cited by {', '.join(others)}" if others else "")
    if disposition == "cited":
        return f"no card; cited by {', '.join(cited)}"
    readers = ", ".join(read_by) if read_by else "no agent"
    text = f"read by {readers}; no card, claim or decision cited it"
    if triage is not None:
        verdict, code, reason = triage
        text += f"; news analyst: {verdict}" + (f" ({code})" if code else "") + (f": {reason}" if reason else "")
    return text


def reading_list(
    items: Sequence[NewsRead],
    read_by: Mapping[str, Sequence[str]],
    citations: Sequence[Citation],
    triage: Mapping[str, tuple[str, str, str]] | None = None,
) -> list[Reading]:
    """One `Reading` per distinct item id, in the order given."""
    out: list[Reading] = []
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        cited = tuple(dict.fromkeys(c.by for c in citations if item.id in c.ids))
        cards = tuple(dict.fromkeys(c.by for c in citations
                                    if c.kind == "card" and c.role == "news" and item.id in c.ids))
        disposition: Disposition = "card" if cards else "cited" if cited else "not_cited"
        roles = tuple(read_by.get(item.id, ()))
        tri = (triage or {}).get(item.id)
        out.append(Reading(
            id=item.id, source=item.source, licence=item.licence, title=item.title,
            summary=item.summary, symbols=item.symbols, age=item.age, available=item.available,
            read_by=roles, cited_by=cited, cards=cards, disposition=disposition,
            used=disposition != "not_cited", why=_why(disposition, cards, cited, roles, tri),
            triage=tri,
        ))
    return out


def counts(readings: Sequence[Reading]) -> dict[str, int]:
    """{"read": n, "card": n, "cited": n, "not_cited": n} for a summary line."""
    out = {"read": len(readings), "card": 0, "cited": 0, "not_cited": 0}
    for r in readings:
        out[r.disposition] += 1
    return out


def public_links(record: Mapping[str, Any] | None) -> dict[str, str]:
    """{P: item id: link} from the private ledger record's `extras["news_fetch"]["public_items"]`
    (the cycle writes a link only for public-domain items; broker feed items are only counted there,
    so no broker link can come back). Empty for an older record or a malformed one."""
    try:
        rows = ((record or {}).get("extras") or {}).get("news_fetch", {}).get("public_items") or []
    except AttributeError:
        return {}
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        item_id, link = str(row.get("id") or ""), str(row.get("link") or "")
        if item_id.startswith("P:") and link.startswith(("https://", "http://")):
            out[item_id] = link
    return out


def with_links(readings: Sequence[Reading], links: Mapping[str, str]) -> list[Reading]:
    """The readings with each public-domain item's link filled in (others unchanged)."""
    from dataclasses import replace

    return [replace(r, link=links[r.id]) if r.id in links and r.id.startswith("P:") else r
            for r in readings]
