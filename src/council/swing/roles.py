"""Code rules on the swing roles' outputs (design swing-book.md rev 2, §1.3-§1.7, §2; SW-3).

A decoded model output is never used as is. These functions turn it into an ACCEPTED output:

- `accept_ideas` (Scout): H3 every catalyst id was admitted this slot (reading list or movers
  screen, `available_at < slot`); H4 each catalyst is ABOUT the ticker, unless the setup is
  `second_order` (then a market- or sector-wide item may carry it); H10 a ticker rejected in the
  last 5 sessions needs a catalyst newer than the rejection; one idea per ticker (the first); a
  paper-only setup
  (SB16) leaves the LLM path here, so it costs no call (it is still paper-tracked, SW-6).
- `accept_verdict` (Skeptic): H8 the ref is the one idea it was shown; H6 unknown ids are
  stripped and a reason left with no id is dropped; then the §1.5 rules, in order:
  misread -> drop `catalyst_misread`; `priced_in: fully` -> reject (`skeptic_incoherent` if it said
  pass); `mostly` + pass -> wait; `stale|restated` + pass -> wait; the sigma prior (a pass after a
  >= prior-sigma move in the trade's direction, >= 1 session after the news, must cite a fact the
  move does not contain: a non-price card field) -> wait; reject drops; wait parks; pass goes on.
  No verdict (two validation failures) -> drop `skeptic_failed`.
- `accept_case` (bull / bear) and `accept_actions` (PM): H8 closed refs, H6 ids; a PM action left
  with no id becomes pass (idea) / hold (trade). A canary ref reaching the PM raises (H11).

Pure: no I/O, no clock, no LLM, no ledger.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from council.stocks.universe import try_normalise_id
from council.swing.facts import FactCard
from council.swing.models import (
    ScoutIdea,
    ScoutOutput,
    SkepticReason,
    SkepticVerdict,
    SwingAction,
    SwingBearCase,
    SwingCase,
    SwingPMDecision,
    is_idea_ref,
)

REPITCH_SESSIONS = 5
WAIT_EXPIRY_SESSIONS = 3

# Card fields that describe the market's REACTION to the news: a pass under the sigma prior must
# cite something else (a fact the move does not already contain).
REACTION_FIELDS = frozenset({
    "news_age_sessions", "gap_pct", "move_since_news_close_pct", "move_since_news_close_sigma",
    "move_since_news_live_pct", "move_since_news_live_sigma", "move_today_live_pct",
    "move_today_live_sigma", "vol_ratio_last", "vol_ratio_since", "rel_move_since_pct",
    "sector_move_since_pct", "spx_move_since_pct", "ndx_move_since_pct",
})
# Card fields that are NOT derived from the price or the volume tape (fundamentals, the earnings
# calendar, positioning): the only card facts a pass under the sigma prior may lean on. Anything
# price-derived (trend, 52-week distances, momentum, vol, beta, correlation) is already in the move.
NON_PRICE_FIELDS = frozenset({
    "rev_yoy", "rev_accel", "gm_chg", "om_chg", "filing_age_d", "earnings_next", "earnings_confirmed",
    "earnings_last_sessions_ago", "short_interest_pct_float", "days_to_cover",
})
# Close-layer fields whose bucket change makes a parked `wait` idea material again.
BUCKET_FIELDS = ("trend", "adv_bucket", "crowding", "earnings_confirmed")


class CanaryLeak(AssertionError):
    """A canary idea reached a stage it must never reach (H11)."""


@dataclass(frozen=True)
class Drop:
    ref: str
    ticker: str
    code: str
    detail: str = ""


@dataclass(frozen=True)
class CatalystMeta:
    """What code knows about one admissible catalyst id of this slot."""

    id: str
    available_at: datetime
    symbols: frozenset[str] = frozenset()      # line ids the item is tagged with; empty = market-wide
    title: str = ""
    form: str | None = None
    items: tuple[str, ...] = ()

    @property
    def market_wide(self) -> bool:
        return not self.symbols


def catalyst_index(reading: Iterable[Any], screen_rows: Iterable[Mapping[str, Any]] = (), *,
                   slot: datetime, screen_available_at: datetime | None = None) -> dict[str, CatalystMeta]:
    """Admissible catalyst ids of this slot: reading-list items (`NewsItem`) and movers-screen rows
    (`{"id": "M:<line>:<list>", "line_id": ...}`), only those available before the slot."""
    out: dict[str, CatalystMeta] = {}
    for item in reading:
        if item.available_at >= slot:
            continue
        syms = frozenset(s for s in (try_normalise_id(x) for x in item.symbols) if s)
        out[item.id] = CatalystMeta(id=item.id, available_at=item.available_at, symbols=syms,
                                    title=item.title, form=getattr(item, "form", None),
                                    items=tuple(getattr(item, "items", ()) or ()))
    if screen_available_at is not None and screen_available_at < slot:
        for row in screen_rows:
            out[str(row["id"])] = CatalystMeta(id=str(row["id"]), available_at=screen_available_at,
                                               symbols=frozenset({str(row["line_id"])}),
                                               title=f"movers screen: {str(row['id']).rsplit(':', 1)[-1]}")
    return out


@dataclass
class SwingIdea:
    """One Scout idea as it moves through the gate. `canary` is a code-side flag the model never sees."""

    ref: str
    idea: ScoutIdea
    line_id: str
    canary: bool = False
    card: FactCard | None = None
    verdict: VerdictOutcome | None = None

    @property
    def ticker(self) -> str:
        return self.idea.ticker


@dataclass
class IdeaCheck:
    accepted: list[SwingIdea] = field(default_factory=list)
    paper_only: list[SwingIdea] = field(default_factory=list)   # no call spent; paper-tracked (SW-6)
    drops: list[Drop] = field(default_factory=list)


def accept_ideas(
    out: ScoutOutput | None,
    *,
    catalysts: Mapping[str, CatalystMeta],
    setups_live: Sequence[str],
    setups_paper_only: Sequence[str],
    recent_rejections: Mapping[str, datetime] | None = None,
) -> IdeaCheck:
    """H3 / H4 / H10 / SB16 over the Scout's ideas, in the Scout's order. `recent_rejections` maps a
    line id to its latest Skeptic/PM rejection within the last REPITCH_SESSIONS sessions."""
    check = IdeaCheck()
    if out is None:
        return check
    seen: set[str] = set()
    for k, idea in enumerate(out.ideas, start=1):
        ref = f"idea:{k}"
        line = try_normalise_id(idea.ticker)
        if line is None:
            check.drops.append(Drop(ref, idea.ticker, "unresolved_symbol"))
            continue
        if line in seen:
            check.drops.append(Drop(ref, idea.ticker, "duplicate_idea"))
            continue
        seen.add(line)
        metas = [catalysts.get(cid) for cid in idea.catalyst_ids]
        missing = [cid for cid, m in zip(idea.catalyst_ids, metas, strict=True) if m is None]
        if missing:                                                       # H3: never from memory
            check.drops.append(Drop(ref, idea.ticker, "catalyst_not_admitted", ",".join(missing)))
            continue
        known = [m for m in metas if m is not None]
        # H4: about the ticker; a market-wide item may support an on-ticker catalyst. A
        # `second_order` idea is carried by another name's or the market's news by definition.
        on = [m for m in known if line in m.symbols]
        off = [] if idea.setup == "second_order" else (
            [m.id for m in known if line not in m.symbols and not m.market_wide] or
            ([] if on else [m.id for m in known]))
        if off:
            check.drops.append(Drop(ref, idea.ticker, "catalyst_off_ticker", ",".join(off)))
            continue
        rejected_at = (recent_rejections or {}).get(line)
        if rejected_at is not None and not any(m.available_at > rejected_at for m in known):   # H10
            check.drops.append(Drop(ref, idea.ticker, "repitch_no_new_news"))
            continue
        swing = SwingIdea(ref=ref, idea=idea, line_id=line)
        if idea.setup in setups_paper_only:
            check.paper_only.append(swing)
        elif idea.setup in setups_live:
            check.accepted.append(swing)
        else:
            check.drops.append(Drop(ref, idea.ticker, "setup_not_allowed", idea.setup))
    return check


# ------------------------------------------------------------------------------------ Skeptic
Status = Literal["pass", "wait", "reject", "drop"]


@dataclass(frozen=True)
class VerdictOutcome:
    status: Status
    code: str | None                       # the drop / park code (public)
    verdict: SkepticVerdict | None         # the cleaned verdict (ids stripped), None on failure
    flags: tuple[str, ...] = ()
    said: str | None = None                # what the model said before the code rules


def is_reaction_id(eid: str, line_id: str) -> bool:
    if eid.startswith("M:"):
        return True
    prefix = f"X:{line_id}:"
    return eid.startswith(prefix) and eid[len(prefix):] in REACTION_FIELDS


def is_independent_id(eid: str, line_id: str, catalyst_ids: Iterable[str]) -> bool:
    """A fact the market move does not already contain (the sigma-prior escape, §1.5): a non-price
    card field of this line. Never a catalyst id, a movers-screen row, a market-context item (the
    Skeptic's only other P:/N: ids are market-wide context), a core-desk market row (F:/V:/C:) or a
    price-derived field."""
    if eid in set(catalyst_ids):
        return False
    prefix = f"X:{line_id}:"
    return eid.startswith(prefix) and eid[len(prefix):] in NON_PRICE_FIELDS


def directional_sigma(card: FactCard | None, side: str) -> float | None:
    """The move since the news in sigma, signed so that positive = in the trade's direction; the
    live value first (it is what the market shows now), else the completed-close value."""
    if card is None:
        return None
    raw = card.fields.get("move_since_news_live_sigma")
    if raw is None:
        raw = card.fields.get("move_since_news_close_sigma")
    if raw is None:
        return None
    return float(raw) if side == "long" else -float(raw)


def sigma_prior_applies(card: FactCard | None, side: str, *, prior_wait_sigma: float) -> bool:
    sig = directional_sigma(card, side)
    age = card.fields.get("news_age_sessions") if card is not None else None
    return sig is not None and sig >= prior_wait_sigma and isinstance(age, int) and age >= 1


def clean_reasons(reasons: Sequence[SkepticReason], admissible: set[str]) -> list[SkepticReason]:
    out: list[SkepticReason] = []
    for r in reasons:
        ids = [e for e in r.evidence_ids if e in admissible]
        if ids:
            out.append(r.model_copy(update={"evidence_ids": ids}))
    return out


def accept_verdict(
    v: SkepticVerdict | None,
    *,
    idea: SwingIdea,
    admissible: set[str],
    prior_wait_sigma: float,
) -> VerdictOutcome:
    """The §1.5 code rules, in the design's order. `admissible` = the ids of the Skeptic's input."""
    if v is None:
        return VerdictOutcome("drop", "skeptic_failed", None)
    said = v.verdict
    if v.idea_ref != idea.ref:                                              # H8
        return VerdictOutcome("drop", "skeptic_failed", None, ("unknown_ref",), said)
    flags: list[str] = []
    reasons = clean_reasons(v.reasons, admissible)                          # H6
    if len(reasons) != len(v.reasons):
        flags.append("skeptic_ids_stripped")
    if not reasons:
        return VerdictOutcome("drop", "skeptic_failed", None, (*flags, "no_cited_reason"), said)
    clean = v.model_copy(update={"reasons": reasons})
    if not v.catalyst_supports_claim or not v.claim_supports_side:          # H4b
        return VerdictOutcome("drop", "catalyst_misread", clean, tuple(flags), said)
    verdict = v.verdict
    if v.priced_in == "fully":
        if verdict == "pass":
            flags.append("skeptic_incoherent")
        verdict = "reject"
    if verdict == "pass" and v.priced_in == "mostly":
        verdict = "wait"
        flags.append("skeptic_mostly_wait")
    if verdict == "pass" and v.news_status in ("stale", "restated"):
        verdict = "wait"
        flags.append("skeptic_stale_wait")
    if verdict == "pass" and sigma_prior_applies(idea.card, idea.idea.side, prior_wait_sigma=prior_wait_sigma):
        cited = {e for r in reasons for e in r.evidence_ids}
        independent = [e for e in cited if is_independent_id(e, idea.line_id, idea.idea.catalyst_ids)]
        if not independent:
            verdict = "wait"
            flags.append("skeptic_prior_wait")
    if verdict == "reject":
        return VerdictOutcome("reject", "skeptic_reject", clean, tuple(flags), said)
    if verdict == "wait":
        return VerdictOutcome("wait", "skeptic_wait", clean, tuple(flags), said)
    return VerdictOutcome("pass", None, clean, tuple(flags), said)


def wait_disposition(parked_card: FactCard, parked_catalysts: Iterable[str], new_card: FactCard | None,
                     new_catalysts: Iterable[str], *, sessions_parked: int) -> Literal["return", "keep", "expire"]:
    """A parked `wait` idea returns only if its fact card changed materially (a new admitted
    catalyst, or a close-layer field moving a bucket), else it expires after 3 sessions."""
    if new_card is not None and new_card.ok:
        if set(new_catalysts) - set(parked_catalysts):
            return "return"
        for key in BUCKET_FIELDS:
            if parked_card.fields.get(key) != new_card.fields.get(key):
                return "return"
        old = parked_card.fields.get("move_since_news_close_sigma")
        new = new_card.fields.get("move_since_news_close_sigma")
        if old is not None and new is not None and int(float(old)) != int(float(new)):
            return "return"
    return "expire" if sessions_parked >= WAIT_EXPIRY_SESSIONS else "keep"


# ------------------------------------------------------------------------------ debate and PM
def guard_no_canary(ideas: Iterable[SwingIdea]) -> None:
    """H11: raise if any canary idea is about to reach the debate, the PM or the planner."""
    leaked = [i.ref for i in ideas if i.canary]
    if leaked:
        raise CanaryLeak(f"canary idea(s) {leaked} reached a stage past the Skeptic")


def accept_case(case: SwingCase | None, *, refs: set[str], admissible: set[str]) -> tuple[SwingCase | None, int]:
    """H8/H6 on a bull or bear case: claims on unknown refs are dropped, unknown ids stripped, a
    claim left with no id dropped. Returns (clean case or None, number of claims dropped)."""
    if case is None:
        return None, 0
    kept = []
    for c in case.claims:
        ids = [e for e in c.evidence_ids if e in admissible]
        if c.ref in refs and ids:
            kept.append(c.model_copy(update={"evidence_ids": ids}))
    update: dict[str, Any] = {"claims": kept}
    if isinstance(case, SwingBearCase):
        update["rebuttals"] = [r.model_copy(update={"evidence_ids": [e for e in r.evidence_ids if e in admissible]})
                               for r in case.rebuttals]
    return case.model_copy(update=update), len(case.claims) - len(kept)


def accept_actions(
    decision: SwingPMDecision | None,
    *,
    idea_refs: set[str],
    trade_refs: set[str],
    admissible: set[str],
    canary_refs: set[str] = frozenset(),       # type: ignore[assignment]
) -> list[SwingAction] | None:
    """One PM replicate's accepted actions, or None when it failed validation (counts as
    pass/hold in the aggregate). H11: a canary ref here raises. H8: unknown refs dropped. H6: ids
    stripped; an action left with no id becomes pass (idea) / hold (trade), without levels."""
    if decision is None:
        return None
    out: list[SwingAction] = []
    for a in decision.actions:
        if a.ref in canary_refs:
            raise CanaryLeak(f"canary ref {a.ref} in a PM decision")
        if a.ref not in (idea_refs | trade_refs):
            continue
        ids = [e for e in a.evidence_ids if e in admissible]
        if not ids:
            fallback = "pass" if is_idea_ref(a.ref) else "hold"
            out.append(SwingAction.model_construct(ref=a.ref, action=fallback, stop_pct=None, target_pct=None,
                                                   time_stop_days=None, evidence_ids=list(a.evidence_ids),
                                                   reason=a.reason))
            continue
        out.append(a.model_copy(update={"evidence_ids": ids}))
    return out
