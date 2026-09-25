"""Code officers: deterministic evidence cards from the fact pack (no LLM).

Rules:
  - Vol officer: a `vol_shock` card for every admitted line whose EWMA5/EWMA60 volatility ratio is
    >= `risk.vol_breaker.card_ratio`. It is QUALIFYING for cuts (listed in
    `risk.authority.qualifying_cut_cards`).
  - Event officer: an `event_binary` card for every binary scheduled event whose block window
    [event - macro_before_h, event + macro_after_h] contains `now`. It is NOT qualifying: it only
    blocks adds (R16) and never justifies a cut or forces a sell. Market-wide events cover every
    admitted line; a card's scope holds at most `MAX_SCOPE` lines (= `CardDraft.scope` max_length,
    9 = the whole v1 universe), so only a wider universe splits an event into several cards.
  - Card IDs are `K:vol:<n>` / `K:event:<n>`, numbered in universe line order (events: by time),
    so any module that calls these functions with the same pack and `now` gets the same IDs.
    IDs are PER CYCLE (no cycle namespace): a card from an earlier cycle must never be mixed
    with this cycle's cards. The orchestrator builds the code cards once, with `now = slot`, and
    passes them to both `compute_bands` and `run_council(code_cards=...)`.
  - No vol-card hysteresis in v1: cards are rebuilt from the current pack every cycle, so a
    `vol_shock` card exists exactly while EWMA5/EWMA60 >= `card_ratio` (2.0). Nothing carries a
    card forward, so the 1.5 expiry ratio below never extends a card's life inside the council.
  - Expiry (`card_expired` / `card_expires_at`, for callers that keep cards across cycles): an
    event card expires `macro_after_h` after the event; a vol card once the ratio is back under
    `CARD_EXPIRY_RATIO` (1.5); any other card after `horizon_days`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from council.deliberation.common import admissible_ids
from council.models.cards import CardDraft, EvidenceCard
from council.models.facts import FactPack, MarketState
from council.policy import Policy

CARD_EXPIRY_RATIO = 1.5
VOL_CARD_HORIZON_DAYS = 5


def _scope_max_length() -> int:
    """`CardDraft.scope` max_length, so officer cards and analyst drafts share one scope limit."""
    for meta in CardDraft.model_fields["scope"].metadata:
        limit = getattr(meta, "max_length", None)
        if limit is not None:
            return int(limit)
    raise RuntimeError("CardDraft.scope has no max_length")


MAX_SCOPE = _scope_max_length()


def _state(pack: FactPack, symbol: str, policy: Policy) -> MarketState | None:
    st = pack.states.get(symbol)
    if st is None:
        line = policy.universe.by_symbol().get(symbol)
        if line is not None:
            st = pack.states.get(line.signal.ticker)
    return st


def vol_fact_id(pack: FactPack, symbol: str) -> str:
    """The pack's volatility-shock fact ID for a line. Preference: `V:<sym>:ewma5_60` (the facts
    module's ID), then any `V:<sym>:*ewma*`, then any `V:<sym>:*`; the canonical ID when the
    pack has none."""
    ids = admissible_ids(pack)
    canonical = f"V:{symbol}:ewma5_60"
    if canonical in ids:
        return canonical
    vol_ids = sorted(i for i in ids if i.startswith(f"V:{symbol}:"))
    ewma = [i for i in vol_ids if "ewma" in i]
    if ewma:
        return ewma[0]
    return vol_ids[0] if vol_ids else canonical


def expiry_ratio(policy: Policy) -> float:
    return float(policy.risk.get("vol_breaker", {}).get("card_expiry_ratio", CARD_EXPIRY_RATIO))


def vol_cards(pack: FactPack, policy: Policy) -> list[EvidenceCard]:
    """`vol_shock` cards: EWMA5/EWMA60 >= card_ratio on an admitted line (qualifying for cuts)."""
    threshold = float(policy.risk["vol_breaker"]["card_ratio"])
    qualifying = "vol_shock" in policy.risk["authority"]["qualifying_cut_cards"]
    admitted = set(pack.admitted)
    cards: list[EvidenceCard] = []
    for line in policy.universe.lines:
        if line.symbol not in admitted:
            continue
        st = _state(pack, line.symbol, policy)
        if st is None or st.ewma5_60_ratio is None or st.ewma5_60_ratio < threshold:
            continue
        cards.append(
            EvidenceCard(
                card_id=f"K:vol:{len(cards) + 1}",
                role="vol",
                scope=[line.symbol],
                card_type="vol_shock",
                direction="risk_down",
                claim=(
                    f"{line.symbol} EWMA5/EWMA60 volatility ratio {st.ewma5_60_ratio:.2f}x, at or "
                    f"above the {threshold:.2f}x card threshold."
                ),
                evidence_ids=[vol_fact_id(pack, line.symbol)],
                horizon_days=VOL_CARD_HORIZON_DAYS,
                falsifier=f"Ratio back under {expiry_ratio(policy):.2f}x.",
                novel=True,
                qualifying=qualifying,
            )
        )
    return cards


def _chunks(items: Sequence[str], size: int) -> list[list[str]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def event_cards(pack: FactPack, now: datetime, policy: Policy) -> list[EvidenceCard]:
    """`event_binary` cards for binary events whose block window contains `now` (never qualifying)."""
    block = policy.risk["event_block"]
    before = timedelta(hours=float(block["macro_before_h"]))
    after = timedelta(hours=float(block["macro_after_h"]))
    admitted = [ln.symbol for ln in policy.universe.lines if ln.symbol in set(pack.admitted)]
    cards: list[EvidenceCard] = []
    for ev in sorted(pack.events, key=lambda e: (e.at_utc, e.id)):
        if not ev.binary or not (ev.at_utc - before <= now <= ev.at_utc + after):
            continue
        scope = [s for s in admitted if not ev.symbols or s in ev.symbols]
        if not scope:
            continue
        hours = (ev.at_utc - now).total_seconds() / 3600
        for chunk in _chunks(scope, MAX_SCOPE):
            cards.append(
                EvidenceCard(
                    card_id=f"K:event:{len(cards) + 1}",
                    role="event",
                    scope=chunk,
                    card_type="event_binary",
                    direction="neutral",
                    claim=(
                        f"{ev.kind.upper()} at {ev.at_utc:%Y-%m-%d %H:%M} UTC ({hours:+.1f}h): "
                        f"binary event; adds blocked from {block['macro_before_h']}h before to "
                        f"{block['macro_after_h']}h after."
                    ),
                    evidence_ids=[ev.id],
                    horizon_days=1,
                    falsifier="The event passes; the block lifts after the window.",
                    novel=True,
                    qualifying=False,
                )
            )
    return cards


def card_expires_at(
    card: EvidenceCard, *, created_at: datetime, pack: FactPack, policy: Policy
) -> datetime | None:
    """Time-based expiry. None for vol cards (their expiry depends on the vol ratio)."""
    if card.card_type == "vol_shock":
        return None
    if card.card_type == "event_binary":
        after = timedelta(hours=float(policy.risk["event_block"]["macro_after_h"]))
        events = {e.id: e for e in pack.events}
        for eid in card.evidence_ids:
            if eid in events:
                return events[eid].at_utc + after
        return created_at  # the event has left the calendar: already expired
    return created_at + timedelta(days=int(card.horizon_days))


def card_expired(
    card: EvidenceCard, *, now: datetime, created_at: datetime, pack: FactPack, policy: Policy
) -> bool:
    """True once a card no longer justifies anything (see the module rules). A vol card whose
    ratio is unknown in the current pack is NOT expired: expiry needs positive evidence."""
    if card.card_type == "vol_shock":
        limit = expiry_ratio(policy)
        ratios = []
        for sym in card.scope:
            st = _state(pack, sym, policy)
            if st is None or st.ewma5_60_ratio is None:
                return False
            ratios.append(st.ewma5_60_ratio)
        return all(r < limit for r in ratios)
    expires = card_expires_at(card, created_at=created_at, pack=pack, policy=policy)
    return expires is not None and now >= expires
