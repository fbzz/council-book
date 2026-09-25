"""R10 council authority: the bands (in units of a line's unit weight) the PM may move inside.

Rules (risk.yaml `authority`):
- Core lines by trend:
  UP    -> [ref - cut, ref (+ leverage_extension if the line is in lever_ok)], where
           cut = up.cut_with_qualifying_card only if a qualifying card exists (type in
           qualifying_cut_cards, card.qualifying true, the line in its scope), else 0;
  MIXED -> [mixed.lo, mixed.hi];  DOWN -> [down.lo if the line is in short_ok else 0, down.hi].
  A core band always contains the reference level.
- Overlay (council-only, reference 0) lines follow `overlay` by trend; negative bounds only when the
  line is in short_ok.
- council_deviations = false -> [ref, ref].
- WARN or an event block -> no adds: the band is clipped into [min(0, current), max(0, current)]
  (for a long or flat line this is hi = min(hi, current); for a short it stops the short growing).
- A frozen line, or one without a trend state -> [current, current].
- HALTED/FLAT -> [0, 0] (flatten).
- enforce_authority clips levels into their bands and reports every line it moved.
- expired_cuts: a line cut below its reference by a card whose expiry has passed returns to
  the reference (code proposes it; the cost gate still applies).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from council.models.cards import EvidenceCard
from council.models.common import LEVEL_GRID, Frozen
from council.models.facts import MarketState
from council.models.reference import ReferenceBook
from council.models.risk import Band
from council.policy import LineSpec, Policy, Universe
from council.risk.config import risk_limits

_EPS = 1e-9
MAX_LEVEL = max(LEVEL_GRID)


def reference_levels(book: ReferenceBook) -> dict[str, float]:
    return {symbol: entry.level_ref for symbol, entry in book.entries.items()}


def qualifying_cut_cards(line: str, cards: Iterable[EvidenceCard], policy: Policy) -> list[str]:
    """IDs of cards that allow an uptrend cut on `line`."""
    allowed = set(risk_limits(policy).authority.qualifying_cut_cards)
    return sorted(
        card.card_id
        for card in cards
        if card.card_type in allowed and card.qualifying and line in card.scope
    )


def no_add_interval(current: float) -> tuple[float, float]:
    """Levels reachable without adding risk: toward zero, never through it."""
    return min(0.0, current), max(0.0, current)


def restrict(lo: float, hi: float, a: float, b: float) -> tuple[float, float]:
    """Clip an interval into [a, b]. The result is never empty: if the two are disjoint it
    collapses to the point of [a, b] nearest the original interval."""
    new_lo = min(max(lo, a), b)
    new_hi = min(max(hi, a), b)
    return new_lo, max(new_lo, new_hi)


def _core_band(
    ref: float,
    trend: str,
    q_cards: list[str],
    lever_ok: bool,
    short_ok: bool,
    policy: Policy,
) -> tuple[float, float, list[str]]:
    auth = risk_limits(policy).authority
    reasons: list[str] = [f"trend {trend}"]
    if trend == "up":
        cut = auth.up.cut_with_qualifying_card if q_cards else 0.0
        lo = max(ref - cut, 0.0)
        hi = ref
        if cut:
            reasons.append("qualifying card allows a cut")
        if lever_ok:
            hi = min(ref + auth.up.leverage_extension, MAX_LEVEL)
            reasons.append("leverage extension passes the cost gate")
    elif trend == "mixed":
        lo, hi = auth.mixed.lo, auth.mixed.hi
    else:
        lo = auth.down.lo if short_ok else max(auth.down.lo, 0.0)
        hi = auth.down.hi
        if short_ok and lo < 0:
            reasons.append("short allowed")
    if ref < lo - _EPS or ref > hi + _EPS:
        reasons.append("widened to include the reference")
    return min(lo, ref), max(hi, ref), reasons


def _overlay_band(trend: str, short_ok: bool, policy: Policy) -> tuple[float, float, list[str]]:
    overlay = risk_limits(policy).authority.overlay
    lo, hi = {"up": overlay.up, "mixed": overlay.mixed, "down": overlay.down}[trend]
    reasons = [f"overlay trend {trend}"]
    if not short_ok:
        lo = max(lo, 0.0)
        hi = max(hi, lo)
    elif lo < 0:
        reasons.append("short allowed")
    return lo, hi, reasons


def compute_bands(
    *,
    lines: Sequence[LineSpec] | Universe,
    ref: Mapping[str, float],
    states: Mapping[str, MarketState],
    cards: Iterable[EvidenceCard],
    current_levels: Mapping[str, float],
    kill_state: str,
    event_blocked: set[str],
    lever_ok: set[str],
    short_ok: set[str],
    policy: Policy,
) -> dict[str, Band]:
    """The band of every line (rules in the module docstring)."""
    specs = lines.lines if isinstance(lines, Universe) else list(lines)
    cards = list(cards)
    bands: dict[str, Band] = {}
    for spec in specs:
        s = spec.symbol
        ref_level = float(ref.get(s, 0.0))
        current = float(current_levels.get(s, 0.0))
        state = states.get(s)
        trend = state.trend if state is not None else None
        q_cards = qualifying_cut_cards(s, cards, policy)

        if kill_state in ("HALTED", "FLAT"):
            bands[s] = Band(symbol=s, trend=trend, ref_level=ref_level, lo=0.0, hi=0.0,
                            reasons=["kill switch halted: flatten"])
            continue
        if state is None or state.frozen or trend is None:
            why = "frozen" if state is not None and state.frozen else "no trend state"
            bands[s] = Band(symbol=s, trend=trend, ref_level=ref_level, lo=current, hi=current,
                            reasons=[f"{why}: hold current"])
            continue

        if not spec.council_deviations:
            lo, hi, reasons = ref_level, ref_level, ["reference-only line"]
        elif spec.sleeve == "overlay" or not spec.in_reference:
            lo, hi, reasons = _overlay_band(trend, s in short_ok, policy)
        else:
            lo, hi, reasons = _core_band(
                ref_level, trend, q_cards, s in lever_ok, s in short_ok, policy
            )

        if kill_state == "WARN" or s in event_blocked:
            lo, hi = restrict(lo, hi, *no_add_interval(current))
            reasons.append("WARN: no adds" if kill_state == "WARN" else "event window: no adds")
        bands[s] = Band(symbol=s, trend=trend, ref_level=ref_level, lo=lo, hi=hi,
                        reasons=reasons, qualifying_cards=q_cards)
    return bands


def enforce_authority(
    levels: Mapping[str, float], bands: Mapping[str, Band]
) -> tuple[dict[str, float], list[str]]:
    """Clip each level into its band. Lines missing from `levels` start at the band's reference;
    lines without a band are dropped. Returns (clipped levels, lines moved or dropped)."""
    clipped: dict[str, float] = {}
    reverted: list[str] = []
    for s, band in bands.items():
        raw = float(levels.get(s, band.ref_level))
        value = min(max(raw, band.lo), band.hi)
        clipped[s] = value
        if abs(value - raw) > _EPS:
            reverted.append(s)
    reverted.extend(s for s in levels if s not in bands)
    return clipped, sorted(reverted)


class CutRecord(Frozen):
    """A card-justified cut below the reference and when its card stops justifying it."""

    symbol: str
    card_id: str
    cut_at: datetime
    expires_at: datetime


def expired_cuts(
    current_levels: Mapping[str, float],
    ref_levels: Mapping[str, float],
    cut_log: Iterable[CutRecord],
    now: datetime,
) -> set[str]:
    """Lines still below their reference whose every logged cut card has expired."""
    if now.tzinfo is None:
        raise ValueError("naive datetime; council code uses aware UTC datetimes only")
    by_line: dict[str, list[CutRecord]] = {}
    for record in cut_log:
        by_line.setdefault(record.symbol, []).append(record)
    out: set[str] = set()
    for s, records in by_line.items():
        ref = float(ref_levels.get(s, 0.0))
        cur = float(current_levels.get(s, 0.0))
        if abs(cur) >= abs(ref) - _EPS:
            continue
        if all(r.expires_at <= now for r in records):
            out.add(s)
    return out
