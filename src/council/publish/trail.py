"""The per-line decision trail: why each line moved, or why it did not (transparency-v2 §4.4).

For every line that moved, that someone asked to move, or that code could have moved, the trail
walks the cycle's stages in order and says, in fixed words, what each stage did to the line:

   1 Reference     the studied rule's trend and level, and the weight it gives the line
   2 Officers      the code volatility and event cards on the line
   3 Analysts      the news / macro cards on the line, and the drafts code dropped
   4 Band          what the manager was allowed to do (and, when the analysts' cards changed it,
                   the band before them); asks outside it get the counterfactual reason
   5 Debate        each advocate's ask on the line and the claims tagged to it, with evidence values
   6 Manager       how many of the attempts deviated, the decisive fact, whom it sided with, which
                   claims about the line it set aside, and its reason for no change
   7 Auditor       reverted deviations and band clips
   8 Medoid        the attempt chosen and the agreement, or the fall back to the reference
   9 Risk engine   the engine's trace (T5b) or, until then, its notes, mapped through
                   `trace_rules` so a size floor or a broker cost never shows as a number
  10 Plan          the leg, or why no order was made
  11 Human         approved / rejected / expired / pending
  12 Execution     the fill state and the weight achieved

Outcomes: changed, changed_partly, reference_trade, not_taken_by_manager, not_allowed_by_band,
reverted_by_auditor, no_agreement, held_by_engine, too_small (every R11 hold and every size skip,
with the same words), not_ordered, rejected, expired, not_filled, pending; `held` for the lines
nobody asked about (one sentence). The "why not" names the first gate that failed, in the order
manager, auditor, band, medoid, engine, plan, human, execution; the counterfactual band check runs
whether or not the manager tried.

Two sources, one builder:
  - `trails(doc, execution, ops)`: the PUBLIC cycle document (plus its execution file and ops row).
    Agent-safe on a revealed cycle; works on every journal written so far (old documents without
    the newer fields fall back to `hold_reasons`, labelled "from engine notes").
  - `record_trails(rec, ...)`: the PRIVATE ledger record, which also holds what is not published
    yet (structured drops, the bands before the analysts' cards, the medoid's fall-back lines, the
    lines with new evidence, claim-to-line tags). Operator only (`council why`).
Both go through the same public mappings (`trace_rules` for engine notes and plan skips), so the
words never carry an R11 subtype, a size floor, a fee or a broker-derived R15 value. Words come
from fixed templates; model text appears only in quotes, attributed to its agent.

Pure: no I/O. `council.operator.why` loads the documents and prints `render_text`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from council.deliberation.debate import evidence_lines
from council.publish import labels, trace_rules

EPS = 1e-6
X_TOL = 0.0015            # weights are published at 0.001 x: smaller differences are rounding
QUOTE_MAX = 160

Status = Literal["info", "passed", "changed", "blocked"]
Outcome = Literal[
    "changed", "changed_partly", "reference_trade", "not_taken_by_manager", "not_allowed_by_band",
    "reverted_by_auditor", "no_agreement", "held_by_engine", "too_small", "not_ordered",
    "rejected", "expired", "not_filled", "pending", "held",
]
OUTCOMES: tuple[str, ...] = (
    "changed", "changed_partly", "reference_trade", "not_taken_by_manager", "not_allowed_by_band",
    "reverted_by_auditor", "no_agreement", "held_by_engine", "too_small", "not_ordered",
    "rejected", "expired", "not_filled", "pending", "held",
)
STAGES: dict[str, int] = {
    "reference": 1, "officers": 2, "analysts": 3, "band": 4, "debate": 5, "manager": 6,
    "auditor": 7, "medoid": 8, "risk": 9, "plan": 10, "human": 11, "execution": 12,
}
_STAGE_LABEL = {
    "reference": "Reference", "band": "Band", "debate": "Debate", "manager": "Manager",
    "auditor": "Auditor", "medoid": "Medoid", "risk": "Risk engine", "plan": "Plan",
    "human": "Human", "execution": "Execution",
}
_ACTOR_LABEL = {
    "vol": "Vol officer", "event": "Event officer", "news": "News", "macro": "Macro",
    "filings": "Filings", "sector": "Sector",
}
SPEAKERS = {"bull_open": "bull", "bear": "bear", "bull_rebuttal": "bull reply"}
_ADVOCATE_ACTOR = {"bull_open": "bull", "bear": "bear", "bull_rebuttal": "bull"}
PENDING_STATES = frozenset({"awaiting_publication", "proposed"})
APPROVED_STATES = frozenset({
    "approved", "executing", "completed", "completed_partial", "blocked", "execution_unknown",
    "waiting_for_market",
})
FILLED = frozenset({"filled"})
PARTLY = frozenset({"partially_filled", "rejected_partial"})
NOT_FILLED = frozenset({"rejected", "skipped"})
REHEARSAL_PLAN = "rehearsal: no broker account, nothing is ordered"


# ---------------------------------------------------------------------------------- output
@dataclass(frozen=True)
class TrailStep:
    stage: str
    actor: str
    status: Status
    words: str
    before_x: float | None = None
    after_x: float | None = None
    refs: tuple[str, ...] = ()
    anchor: str = ""

    @property
    def number(self) -> int:
        return STAGES[self.stage]

    @property
    def label(self) -> str:
        if self.stage in ("officers", "analysts"):
            return _ACTOR_LABEL.get(self.actor, self.actor.capitalize())
        return _STAGE_LABEL[self.stage]


@dataclass(frozen=True)
class LineTrail:
    line: str
    outcome: Outcome
    stopped_at: str | None
    asked_by: tuple[str, ...]
    steps: tuple[TrailStep, ...]
    headline: str
    why_not: str = ""
    base_x: float = 0.0
    final_x: float = 0.0

    @property
    def anchor(self) -> str:
        return f"trail-{self.line.lower()}"


# ----------------------------------------------------------------------- normalised input
@dataclass(frozen=True)
class CardView:
    card_id: str
    role: str
    card_type: str
    scope: tuple[str, ...]
    direction: str
    qualifying: bool
    corroborated_by: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    filing_only: bool = False


@dataclass(frozen=True)
class BandView:
    trend: str | None
    ref_level: float
    lo: float
    hi: float
    reasons: tuple[str, ...] = ()
    qualifying_cards: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaimView:
    speaker: str
    claim_id: str
    text: str
    evidence: tuple[str, ...]
    lines: tuple[str, ...]


@dataclass(frozen=True)
class RebuttalView:
    claim_id: str                  # the bull claim it answers
    verdict: str
    text: str
    evidence: tuple[str, ...]
    lines: tuple[str, ...]


@dataclass(frozen=True)
class AdvocateView:
    speaker: str                   # bull_open | bear | bull_rebuttal
    proposal: Mapping[str, float]
    claims: tuple[ClaimView, ...] = ()
    rebuttals: tuple[RebuttalView, ...] = ()
    concessions: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeviationView:
    line: str
    level: float
    direction: str
    reason: str = ""
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplicateView:
    replicate: int
    valid: bool
    levels: Mapping[str, float]
    deviations: tuple[DeviationView, ...] = ()
    decisive_text: str = ""
    decisive_id: str = ""
    sided_with: str | None = None
    dismissed: tuple[tuple[str, str], ...] = ()
    no_change_reason: str = ""
    violations: tuple[str, ...] = ()
    reverted: tuple[str, ...] = ()


@dataclass(frozen=True)
class DropView:
    role: str
    what: str
    index: int
    code: str
    ids: tuple[str, ...] = ()
    target: str = ""
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class TraceView:
    stage: str
    code: str
    before_x: float | None = None
    after_x: float | None = None
    value: float | None = None
    limit: float | None = None


@dataclass(frozen=True)
class LegView:
    kind: str
    direction: str
    before_x: float
    after_x: float
    settlement: str | None = None
    leverage: int = 1
    cost_bp: float = 0.0
    origin: str | None = None      # reference | discretionary | None (not recorded: inferred)


@dataclass(frozen=True)
class FillView:
    state: str
    target_x: float | None = None
    filled_x: float | None = None


@dataclass(frozen=True)
class TrailInput:
    cycle_id: str
    live: bool
    lines: tuple[str, ...]
    basis: str | None = None
    kill_state: str = "NORMAL"
    names: Mapping[str, str] = field(default_factory=dict)
    ref_trend: Mapping[str, str | None] = field(default_factory=dict)
    ref_level: Mapping[str, float] = field(default_factory=dict)
    ref_x: Mapping[str, float] = field(default_factory=dict)
    values: Mapping[str, str] = field(default_factory=dict)       # evidence id -> display value
    sources: Mapping[str, str] = field(default_factory=dict)      # news id -> publisher
    cards: tuple[CardView, ...] = ()
    bands: Mapping[str, BandView] = field(default_factory=dict)
    code_bands: Mapping[str, BandView] = field(default_factory=dict)
    drops: tuple[DropView, ...] = ()
    advocates: tuple[AdvocateView, ...] = ()
    replicates: tuple[ReplicateView, ...] = ()
    medoid: int | None = None
    agreement: Mapping[str, float] = field(default_factory=dict)  # share 0..1
    fallback: frozenset[str] | None = None                        # None: inferred
    council_levels: Mapping[str, float] = field(default_factory=dict)
    base_x: Mapping[str, float] = field(default_factory=dict)
    banded_x: Mapping[str, float] = field(default_factory=dict)
    final_x: Mapping[str, float] = field(default_factory=dict)
    has_risk: bool = False
    notes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)   # public form, per line
    trace: Mapping[str, tuple[TraceView, ...]] = field(default_factory=dict)
    material: frozenset[str] | None = None
    legs: Mapping[str, tuple[LegView, ...]] = field(default_factory=dict)
    skipped: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # public skip codes
    plan_present: bool = False
    plan_failed: bool = False
    decision_state: str | None = None
    human: str = "none"
    reason: str = ""
    approved_slot: str = ""
    execution_state: str | None = None
    fills: Mapping[str, tuple[FillView, ...]] = field(default_factory=dict)
    achieved_x: Mapping[str, float] = field(default_factory=dict)


# ------------------------------------------------------------------------------ formatting
def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{float(x) * 100:.1f}%"


def lvl(v: float) -> str:
    return f"{float(v):.2f}"


def quote(text: str | None, limit: int = QUOTE_MAX) -> str:
    t = " ".join(str(text or "").split())
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return f"“{t}”"


def ask_words(level: float, ref: float, current: float | None = None) -> str:
    """"cut to 0.50", "add to 0.75", "lever to 1.25", "short at -0.25", "hold 1.00"."""
    if abs(level - ref) <= EPS:
        return f"hold {lvl(level)}"
    if level < -EPS and ref > -EPS:
        return f"short at {lvl(level)}"
    if level < ref:
        return f"cut to {lvl(level)}"
    if current is not None and current < -EPS and level <= EPS:
        return f"cover to {lvl(level)}"
    if level > 1.0 + EPS:
        return f"lever to {lvl(level)}"
    return f"add to {lvl(level)}"


def ask_noun(level: float, ref: float, current: float | None = None) -> str:
    """The ask as a noun phrase: "a cut to 0.50", "a short at -0.25", "an add to 0.75"."""
    words = ask_words(level, ref, current)
    verb, _, rest = words.partition(" ")
    return {"cut": f"a cut {rest}", "short": f"a short {rest}", "cover": f"a cover {rest}",
            "lever": f"leverage {rest}", "add": f"an add {rest}", "hold": f"holding {rest}"}.get(verb, words)


def fact_display(value: Any, unit: str | None) -> str | None:
    """A publishable fact value as trail text ("+4.1%", "2.31x", "up")."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return value
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return {
        "pct": f"{v:+.1f}%", "x": f"{v:.2f}x", "ratio": f"{v:.2f}", "bps": f"{v:.1f} bp",
        "bps_day": f"{v:.2f} bp/day", "days": f"{v:g} days", "hours": f"{v:.1f}h",
        "sigma": f"{v:+.2f}σ",
    }.get(unit or "", f"{v:g}")


def _verb(before: float, after: float) -> str:
    if abs(before) <= EPS and abs(after) > EPS:
        return "opened"
    if abs(after) <= EPS < abs(before):
        return "closed"
    if before * after < 0:
        return "flipped"
    return "cut" if abs(after) < abs(before) else "added"


def _join(parts: Iterable[str], sep: str = " · ") -> str:
    return sep.join(p for p in parts if p)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# ------------------------------------------------------------------------ engine notes
_NOTE_WORDS: dict[str, str] = {
    "R10 no band: hold current": "no band: held at the current level (R10)",
    "R12 minimum hold": "minimum holding period not over (R12)",
    "MC no new material evidence": "no new evidence since the last change (MC)",
    "R15 no cost quote": "no cost quote, so no net-of-cost check (R15)",
    "R15 no volatility": "no volatility, so no net-of-cost check (R15)",
    "R2 net floor": "trimmed to keep the book's net exposure above its floor (R2)",
    "R2 net ceiling": "trimmed to keep the book's net exposure below its ceiling (R2)",
    "R13 cycle increase budget": "trimmed: the cycle's budget for adding risk is used (R13)",
    "R13 7-day turnover budget": "trimmed: the 7-day turnover budget is used (R13)",
    "R13 30-day turnover budget": "trimmed: the 30-day turnover budget is used (R13)",
    "R14 cycle cost budget": "trimmed: the cycle's cost budget is used (R14)",
    "R14 30-day cost budget": "trimmed: the 30-day cost budget is used (R14)",
    "R14 carry budget": "trimmed: the overnight carry budget is used (R14)",
    "R21 too many legs": "trimmed: more legs than a proposal may carry (R21)",
    "R21 too many legs in total": "trimmed: more legs in total than allowed (R21)",
    "scaled to fit aggregate limits (R1/R2/R5/R7/R8)":
        "scaled to fit the book's aggregate limits (R1/R2/R5/R7/R8)",
    "no broker snapshot: current book taken as flat": "no broker snapshot: the book counted as flat",
}
_R10_CLIP = re.compile(r"^R10 level ([+-]\d+\.\d+) outside band \[([+-]\d+\.\d+), ([+-]\d+\.\d+)\]$")
_R10_CAP = re.compile(r"^R10 deviation beyond the (\d+) allowed per cycle$")
_R15_VALUE = re.compile(r"^R15 SR_be (\d+\.\d+) above (\d+\.\d+)$")
TOO_SMALL = "too small to trade (R11)"


def note_code(note: str) -> str:
    """The rule code of one public engine note ("R11", "R12", "MC", "limited", ...)."""
    n = " ".join(note.split())
    if n == "deadband":
        return trace_rules.R11
    if n.startswith("limited by "):
        return "limited"
    if n.startswith("scaled to fit"):
        return "scaled"
    return trace_rules.public_code(n)


def note_words(note: str) -> str:
    """Fixed words for one engine note already in its public form."""
    n = " ".join(note.split())
    code = note_code(n)
    if code == trace_rules.R11:
        return TOO_SMALL
    if n in _NOTE_WORDS:
        return _NOTE_WORDS[n]
    m = _R10_CLIP.match(n)
    if m:
        return f"asked {m.group(1)}, band allows {m.group(2)} to {m.group(3)} (R10)"
    m = _R10_CAP.match(n)
    if m:
        return f"more deviations than the {m.group(1)} allowed per cycle: this one went back (R10)"
    m = _R15_VALUE.match(n)
    if m:
        return f"net-of-cost gate: SR_be {m.group(1)} above {m.group(2)} (R15; policy cost floor)"
    if code == "R15":
        return "net-of-cost gate (R15)"
    if code in ("R15_fee", "R14_fee"):
        return f"fixed-fee cost check ({code})"
    if code == "limited":
        return f"limited by {n[len('limited by '):]}"
    if code == trace_rules.HELD:
        return "held by the engine"
    return f"held by the engine ({code})"


def skip_words(code: str) -> str:
    """Fixed words for one public plan-skip reason ("R11", "not_ordered", "no_quote", ...)."""
    if code == trace_rules.R11:
        return TOO_SMALL
    if code == trace_rules.NOT_ORDERED:
        return "not ordered"
    return "not ordered: " + code.replace("_", " ")


def _split_note(note: str) -> tuple[str | None, str]:
    head, sep, rest = note.partition(": ")
    if sep and re.fullmatch(r"[A-Z0-9](?:[A-Z0-9_.]{0,22}[A-Z0-9])?", head):
        return head, rest
    return None, note


def notes_by_line(notes: Iterable[str]) -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for note in notes:
        line, rest = _split_note(" ".join(str(note).split()))
        if line is not None:
            out.setdefault(line, []).append(rest)
    return {k: tuple(v) for k, v in out.items()}


def skips_by_line(skipped: Iterable[str]) -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for raw in skipped:
        public = trace_rules.public_plan_skip(raw)
        line, rest = _split_note(public)
        if line is None:
            head, sep, tail = public.partition(": ")
            line, rest = (head, tail) if sep else (None, public)
        if line is not None:
            out.setdefault(line, []).append(rest)
    return {k: tuple(v) for k, v in out.items()}


# ------------------------------------------------------------------------------ builder
class _Line:
    """Everything the templates need about one line."""

    def __init__(self, inp: TrailInput, line: str):
        self.inp, self.line = inp, line
        self.band = inp.bands.get(line)
        self.ref_level = (self.band.ref_level if self.band is not None
                          else float(inp.ref_level.get(line, 0.0)))
        self.ref_x = inp.ref_x.get(line)
        self.base = float(inp.base_x.get(line, 0.0))
        self.final = float(inp.final_x.get(line, self.base))
        self.legs = inp.legs.get(line, ())
        self.skips = inp.skipped.get(line, ())
        self.notes = inp.notes.get(line, ())
        self.trace = inp.trace.get(line, ())
        self.changed = inp.has_risk and abs(self.final - self.base) > EPS
        self.asks = [(adv.speaker, float(adv.proposal[line])) for adv in inp.advocates
                     if line in adv.proposal and abs(float(adv.proposal[line]) - self.ref_level) > EPS]
        self.tried = [(rep, dev) for rep in inp.replicates for dev in rep.deviations if dev.line == line]
        self.valid_tried = [(rep, dev) for rep, dev in self.tried if rep.valid]
        self.valid = [rep for rep in inp.replicates if rep.valid]
        self.medoid = next((r for r in inp.replicates if r.replicate == inp.medoid), None)
        self.council_level = float(inp.council_levels.get(line, self.ref_level))
        if inp.fallback is not None:
            self.fallback = line in inp.fallback
        else:
            medoid_level = self.medoid.levels.get(line) if self.medoid is not None else None
            self.fallback = medoid_level is not None and abs(float(medoid_level) - self.council_level) > EPS
        self.unlock_unused = bool(
            self.band is not None and self.band.qualifying_cards
            and self.band.lo < self.ref_level - EPS and not self.tried)

    @property
    def reference_asked(self) -> bool:
        """The reference weight differs from the book and nobody else asked: a reference move the
        engine held (a changed line's move is the reference's own when the manager did not ask)."""
        return (not self.changed and not self.tried and not self.asks and self.ref_x is not None
                and abs(self.ref_x - self.base) > X_TOL and bool(self.notes or self.trace_changes))

    @property
    def has_trail(self) -> bool:
        return bool(self.changed or self.legs or self.asks or self.tried or self.unlock_unused
                    or self.notes or self.skips or self.trace_changes)

    @property
    def trace_changes(self) -> bool:
        return any(t.before_x is not None and t.after_x is not None and abs(t.after_x - t.before_x) > EPS
                   for t in self.trace)

    @property
    def title(self) -> str:
        name = self.inp.names.get(self.line)
        return f"{self.line} · {name}" if name and name != self.line else self.line

    # ------------------------------------------------------------------ evidence words
    def ev(self, eid: str) -> str:
        value = self.inp.values.get(eid)
        if value is not None:
            return f"{eid} = {value}"
        source = self.inp.sources.get(eid)
        if eid.startswith("P:") and source:
            return f"{eid} ({labels.news_label(eid, source)})"
        return eid

    def evs(self, ids: Iterable[str]) -> str:
        return ", ".join(self.ev(e) for e in ids)


def _reference_step(x: _Line) -> TrailStep:
    inp, line = x.inp, x.line
    trend = inp.ref_trend.get(line, x.band.trend if x.band is not None else None)
    dist = [f"vs SMA{n} {inp.values[f'F:{line}:dist_sma{n}']}" for n in (50, 200)
            if f"F:{line}:dist_sma{n}" in inp.values]
    head = f"trend {trend}" if trend else "no trend state"
    if dist:
        head += f" ({', '.join(dist)})"
    weight = f" · {pct(x.ref_x)}" if x.ref_x is not None else ""
    return TrailStep("reference", "reference", "info", f"{head} → level {lvl(x.ref_level)}{weight}",
                     after_x=x.ref_x, refs=tuple(f"F:{line}:dist_sma{n}" for n in (50, 200)
                                                 if f"F:{line}:dist_sma{n}" in inp.values))


_CARD_TYPE = {
    "vol_shock": "volatility shock", "event_binary": "scheduled event", "news_material": "material news",
    "news_context": "news context", "macro_context": "macro context", "filing_material": "material filing",
    "filing_context": "filing context", "sector_rank": "sector rank",
}


def _card_words(x: _Line, card: CardView) -> tuple[str, Status]:
    what = f"{card.card_id} {_CARD_TYPE.get(card.card_type, card.card_type)}, {card.direction.replace('_', ' ')}"
    if card.corroborated_by:
        what += f", corroborated by {', '.join(card.corroborated_by)}"
    if card.qualifying:
        verdict, status = "qualifying: allows a cut in an uptrend", "changed"
    elif card.filing_only:
        verdict, status = "not qualifying: filing metadata only (cannot unlock a cut)", "info"
    elif card.card_type == "news_material" and not card.corroborated_by:
        verdict, status = "not qualifying: no volatility card on the line corroborates it", "info"
    elif card.card_type == "news_material":
        verdict, status = "not qualifying under the policy's qualifying card types", "info"
    elif card.card_type == "event_binary":
        verdict, status = "blocks adds around the event", "info"
    elif card.card_type == "vol_shock":
        verdict, status = "not qualifying on its own (it can corroborate a news card)", "info"
    else:
        verdict, status = "context only (never qualifying)", "info"
    cites = f" · cites {x.evs(card.evidence)}" if card.evidence else ""
    return f"{what} — {verdict}{cites}", status


_DROP_WHAT = {
    "card_draft": "card draft", "macro_driver": "driver", "macro_tilt": "sleeve tilt",
    "proposal_entry": "proposal entry", "rebuttal": "rebuttal", "triage": "triage entry",
    "deviation": "deviation",
}
_ROLE_NAME = {"news": "news analyst", "macro": "macro analyst", "bull_open": "bull",
              "bear": "bear", "bull_rebuttal": "bull reply", "pm": "manager"}


def drop_words(drop: DropView) -> str:
    """"code dropped the news analyst's card draft 2: it cited P:…, which was not in its pack"."""
    who = _ROLE_NAME.get(drop.role, drop.role)
    what = f"code dropped the {who}'s {_DROP_WHAT.get(drop.what, drop.what)} {drop.index}"
    why = {
        "unknown_evidence": (f"it cited {', '.join(drop.ids)}, which "
                             f"{'was' if len(drop.ids) == 1 else 'were'} not in its pack"),
        "scope_not_admitted": f"its scope {drop.target} is not an admitted line this cycle",
        "type_not_allowed": f"the {who} may not write a {drop.target} card",
        "non_admitted_line": f"{drop.target} is not an admitted line this cycle",
        "unknown_sleeve": f"{drop.target} is not a sleeve",
        "unknown_bull_claim": f"it answers {drop.target}, a claim the bull never made",
    }.get(drop.code, drop.code.replace("_", " "))
    return f"{what}: {why}"


def _officer_steps(x: _Line) -> list[TrailStep]:
    steps = []
    band_event = x.band is not None and any("event" in r for r in x.band.reasons)
    for card in x.inp.cards:
        if card.role not in ("vol", "event"):
            continue
        if x.line in card.scope or (card.role == "event" and band_event and "market" in card.scope):
            words, status = _card_words(x, card)
            steps.append(TrailStep("officers", card.role, status, words, refs=(card.card_id,),
                                   anchor=card.card_id))
    return steps


def _analyst_steps(x: _Line) -> list[TrailStep]:
    steps = []
    for card in x.inp.cards:
        if card.role in ("vol", "event") or x.line not in card.scope:
            continue
        words, status = _card_words(x, card)
        steps.append(TrailStep("analysts", card.role, status, words, refs=(card.card_id,),
                               anchor=card.card_id))
    for drop in x.inp.drops:
        if drop.role in ("news", "macro", "filings", "sector") and x.line in drop.lines:
            steps.append(TrailStep("analysts", drop.role, "blocked", drop_words(drop), refs=drop.ids))
    return steps


def band_range(band: BandView) -> str:
    if abs(band.hi - band.lo) <= EPS:
        return f"fixed at {lvl(band.lo)}"
    return f"{lvl(band.lo)} to {lvl(band.hi)}"


def counterfactual(band: BandView, level: float) -> str:
    """Why code would not allow `level` on this band (the counterfactual band check)."""
    reasons = " ".join(band.reasons).lower()
    if "reference-only" in reasons:
        return "a reference-only line: the council may not move it"
    if "frozen" in reasons or "no trend" in reasons:
        return "the line's data is frozen or has no trend: it holds its current level"
    if "halted" in reasons or "flatten" in reasons:
        return "the kill switch has the book flattening"
    if level > band.hi + EPS and ("no adds" in reasons or "event window" in reasons or "warn" in reasons):
        return "no adds while " + ("the drawdown warning holds" if "warn" in reasons else "an event window is open")
    if band.trend == "up" and level < band.lo - EPS:
        if not band.qualifying_cards:
            return ("a cut in an uptrend needs a volatility-shock card or a corroborated material news card "
                    "on the line")
        return f"the qualifying card allows a cut down to {lvl(band.lo)} only"
    if band.trend == "up" and level > band.hi + EPS:
        return ("above the reference in an uptrend needs the leverage extension, which must pass the cost gate"
                if abs(band.hi - band.ref_level) <= EPS else f"the band stops at {lvl(band.hi)}")
    if level < min(band.lo, 0.0) - EPS or (level < -EPS and band.lo >= -EPS):
        return "a short needs a shortable line whose short passes the cost gate"
    return f"outside the band {band_range(band)}"


def _band_step(x: _Line) -> TrailStep | None:
    band = x.band
    if band is None:
        return None
    words = band_range(band)
    if band.reasons:
        words += " — " + "; ".join(band.reasons)
    before = x.inp.code_bands.get(x.line)
    if before is not None and (abs(before.lo - band.lo) > EPS or abs(before.hi - band.hi) > EPS):
        words += f" (before the analysts' cards: {band_range(before)})"
    asked = [(who, lv) for who, lv in x.asks] + [(f"attempt {rep.replicate}", float(dev.level))
                                                 for rep, dev in x.tried]
    outside: list[tuple[str, str]] = []
    for _who, level in asked:
        if level < band.lo - EPS or level > band.hi + EPS:
            words_ask = ask_noun(level, x.ref_level)
            if words_ask not in [o[0] for o in outside]:
                outside.append((words_ask, counterfactual(band, level)))
    for words_ask, why in outside:
        words += f"\n→ code would not have allowed {words_ask}: {why}"
    if x.unlock_unused:
        words += f"\n→ a qualifying card ({', '.join(band.qualifying_cards)}) allowed a cut that nobody used"
    return TrailStep("band", "audit", "blocked" if outside else "info", words,
                     refs=tuple(band.qualifying_cards))


def _debate_steps(x: _Line) -> list[TrailStep]:
    steps = []
    for adv in x.inp.advocates:
        who = SPEAKERS.get(adv.speaker, adv.speaker)
        parts: list[str] = []
        refs: list[str] = []
        if x.line in adv.proposal:
            parts.append(ask_words(float(adv.proposal[x.line]), x.ref_level))
        claims = [c for c in adv.claims if x.line in c.lines]
        rebuttals = [r for r in adv.rebuttals if x.line in r.lines]
        if not parts and (claims or rebuttals):
            parts.append("reference" if not adv.proposal else "no ask on this line")
        for c in claims:
            parts.append(f"{c.claim_id} {quote(c.text)} [{x.evs(c.evidence)}]")
            refs.append(f"{adv.speaker}:{c.claim_id}")
        for r in rebuttals:
            ev = f" [{x.evs(r.evidence)}]" if r.evidence else ""
            parts.append(f"{r.verdict}s bull:{r.claim_id} {quote(r.text)}{ev}")
            refs.append(f"{adv.speaker}:rebuttal:{r.claim_id}")
        for d in x.inp.drops:
            if d.role == adv.speaker and x.line in d.lines:
                parts.append(drop_words(d))
        if parts:
            asked = x.line in adv.proposal and abs(float(adv.proposal[x.line]) - x.ref_level) > EPS
            steps.append(TrailStep("debate", _ADVOCATE_ACTOR.get(adv.speaker, adv.speaker),
                                   "changed" if asked else "info", f"{who}: " + _join(parts),
                                   refs=tuple(refs), anchor=f"debate-{adv.speaker}"))
    return steps


def _claims_about(x: _Line) -> set[str]:
    """The claim labels a dismissal may use for claims tagged to this line: `bear:c2` always, the
    bare `c2` only when every advocate claim with that id is tagged to the line (a bare id that
    could name another speaker's claim about another line is not attributed)."""
    out: set[str] = set()
    tagged: dict[str, list[bool]] = {}
    for adv in x.inp.advocates:
        for c in adv.claims:
            on_line = x.line in c.lines
            tagged.setdefault(c.claim_id, []).append(on_line)
            if on_line:
                out.add(f"{adv.speaker}:{c.claim_id}")
                if adv.speaker == "bull_open":
                    out.add(f"bull:{c.claim_id}")
    out |= {cid for cid, flags in tagged.items() if flags and all(flags)}
    return out


def _manager_step(x: _Line) -> TrailStep | None:
    inp = x.inp
    basis = inp.basis or ""
    if basis == "council_unavailable":
        return TrailStep("manager", "pm", "blocked", "the council was unavailable (outage): every line at its reference")
    if basis == "fallback_parse":
        return TrailStep("manager", "pm", "blocked",
                         "fewer than 2 readable attempts: every line at its reference")
    if basis == "halted":
        return TrailStep("manager", "pm", "blocked", f"kill switch {inp.kill_state}: the council did not meet")
    if basis == "code_only":
        return TrailStep("manager", "pm", "info", "no line could move this cycle: the council did not meet")
    if not inp.replicates:
        return None
    n = len(inp.replicates)
    k = len({rep.replicate for rep, _ in x.tried})
    parts: list[str] = []
    if k:
        levels: dict[str, int] = {}
        for _rep, dev in x.tried:
            key = ask_words(float(dev.level), x.ref_level)
            levels[key] = levels.get(key, 0) + 1
        groups = [f"{words} ({count})" if len(levels) > 1 else words for words, count in levels.items()]
        parts.append(f"{k} of {n} attempts: " + ", ".join(groups))
        decider = next((rep for rep, _ in x.tried if rep.replicate == inp.medoid), x.tried[0][0])
        if decider.decisive_id:
            parts.append(f"decisive {x.ev(decider.decisive_id)}")
        if decider.sided_with:
            parts.append(f"sided with the {decider.sided_with}")
        reasons = [dev.reason for rep, dev in x.tried if rep.replicate == decider.replicate and dev.reason]
        if reasons:
            parts.append(f"attempt {decider.replicate}: {quote(reasons[0])}")
    else:
        parts.append(f"0 of {n} attempts deviated")
        about = _claims_about(x)
        for rep in inp.replicates:
            for claim_id, why in rep.dismissed:
                if claim_id in about:
                    parts.append(f"attempt {rep.replicate} set aside {claim_id}: {quote(why)}")
        chooser = x.medoid or (x.valid[0] if x.valid else None)
        if chooser is not None and chooser.no_change_reason and not chooser.deviations:
            parts.append(f"no change because {quote(chooser.no_change_reason)}")
    invalid = sum(1 for rep in inp.replicates if not rep.valid)
    if invalid:
        parts.append(f"({_plural(invalid, 'attempt')} invalid)")
    status: Status = "changed" if x.valid_tried else "info"
    return TrailStep("manager", "pm", status, _join(parts), anchor="pm")


def _auditor_step(x: _Line) -> TrailStep | None:
    parts: list[str] = []
    blocked = False
    for rep, dev in x.tried:
        if x.line in rep.reverted:
            why = next((v.split(": ", 1)[1] for v in rep.violations if v.startswith(f"{x.line}: ")), "")
            parts.append(f"attempt {rep.replicate}: reverted" + (f" ({why.split(' ', 1)[0]})" if why else ""))
            blocked = True
            continue
        if not rep.valid:
            parts.append(f"attempt {rep.replicate}: the whole attempt was invalid")
            blocked = True
            continue
        enforced = rep.levels.get(x.line)
        if enforced is not None and abs(float(enforced) - float(dev.level)) > EPS:
            parts.append(f"attempt {rep.replicate} asked {lvl(dev.level)}, band allows {lvl(enforced)}")
            blocked = blocked or abs(float(enforced) - x.ref_level) <= EPS
    if not parts and x.valid_tried:
        parts.append("accepted; inside the band")
    if not parts:
        return None
    return TrailStep("auditor", "audit", "blocked" if blocked else "passed", _join(parts))


def _medoid_step(x: _Line) -> TrailStep | None:
    inp = x.inp
    if inp.medoid is None or not (x.tried or x.fallback):
        return None
    n = len(x.valid)
    share = inp.agreement.get(x.line)
    agree = f"agreement {share * 100:.0f}%" if share is not None else ""
    if x.fallback:
        k = round((share or 0.0) * n) if n else 0
        words = (f"attempt {inp.medoid}; only {k} of {n} attempts agreed on its move → back to the "
                 f"reference {lvl(x.ref_level)}")
        return TrailStep("medoid", "pm", "blocked", words)
    medoid_level = x.medoid.levels.get(x.line) if x.medoid is not None else None
    if medoid_level is not None and abs(float(medoid_level) - x.ref_level) <= EPS and x.valid_tried:
        k = len({rep.replicate for rep, _ in x.valid_tried})
        return TrailStep("medoid", "pm", "blocked",
                         _join([f"attempt {inp.medoid} held; {k} of {n} attempts asked for a change", agree]))
    return TrailStep("medoid", "pm", "passed", _join([f"attempt {inp.medoid}", agree]))


def _risk_step(x: _Line) -> TrailStep | None:
    inp = x.inp
    if not inp.has_risk:
        return None
    parts: list[str] = []
    status: Status = "passed"
    if x.trace:
        for t in x.trace:
            words = note_words(t.code) if t.code not in trace_rules.BOX_CODES else f"limited by {t.code}"
            if t.value is not None and t.limit is not None:
                words = f"{t.code} {t.value:.2f} vs {t.limit:.2f}"
            if t.before_x is not None and t.after_x is not None and abs(t.after_x - t.before_x) > EPS:
                words += f" ({pct(t.before_x)} → {pct(t.after_x)})"
                status = "changed"
            parts.append(words)
    elif x.notes:
        parts += [note_words(n) for n in x.notes]
        status = "blocked" if not x.changed else "changed"
    if x.inp.material is not None and x.changed and x.line in x.inp.material:
        parts.insert(0, "new evidence on the line (MC passed)")
    if x.changed:
        parts.append(("passed " if not parts else "") + f"→ {pct(x.final)} (from {pct(x.base)})")
    elif not parts:
        parts.append(f"no change: stays at {pct(x.final)}")
        status = "info"
    else:
        parts.append(f"→ stays at {pct(x.final)}")
    words = _join(parts)
    if x.notes and not x.trace:
        words += " (from engine notes)"
    return TrailStep("risk", "risk", status, words, before_x=x.base, after_x=x.final)


_LEG_KIND = {"open": "open", "close": "close", "partial_close": "partial close", "modify_sl": "stop change"}


def _origin(x: _Line, leg: LegView) -> str:
    if leg.origin == "reference":
        return "reference trade"
    if leg.origin == "discretionary":
        return "council change"
    if x.ref_x is not None and abs(leg.after_x - x.ref_x) < abs(leg.before_x - x.ref_x) - EPS and not x.valid_tried:
        return "reference trade (origin inferred)"
    return "council change (origin inferred)"


def _plan_step(x: _Line) -> TrailStep | None:
    inp = x.inp
    if x.legs:
        words = []
        for leg in x.legs:
            bits = [_LEG_KIND.get(leg.kind, leg.kind), leg.direction]
            if leg.settlement:
                bits.append(leg.settlement)
            if leg.leverage and leg.leverage > 1:
                bits.append(f"x{leg.leverage}")
            words.append(f"{' '.join(bits)}, {pct(leg.before_x)} → {pct(leg.after_x)}, "
                         f"cost {leg.cost_bp:.1f} bp · {_origin(x, leg)}")
        suffix = "" if inp.live else " · rehearsal: nothing is sent"
        return TrailStep("plan", "costs", "changed", _join(words) + suffix,
                         before_x=x.legs[0].before_x, after_x=x.legs[-1].after_x)
    if x.skips:
        return TrailStep("plan", "costs", "blocked", _join(skip_words(s) for s in x.skips))
    if not x.changed:
        return None
    if not inp.live:
        return TrailStep("plan", "costs", "blocked", REHEARSAL_PLAN)
    if inp.plan_failed:
        return TrailStep("plan", "costs", "blocked", "planning failed: nothing is ordered")
    if not inp.plan_present:
        return TrailStep("plan", "costs", "blocked", "no plan: no broker account connected")
    return TrailStep("plan", "costs", "blocked", "no leg for this line")


def _human(inp: TrailInput) -> tuple[str, Status, str]:
    """(kind, status, words): kind is approved | rejected | expired | pending | none."""
    state, human = inp.decision_state or "", inp.human or "none"
    reason = f": {quote(inp.reason)}" if inp.reason else ""
    if state == "rejected" or human == "rejected":
        return "rejected", "blocked", f"rejected{reason}"
    if state == "expired" or human == "expired":
        return "expired", "blocked", "expired before the operator decided"
    if state == "superseded" or human == "superseded":
        return "expired", "blocked", "superseded by a later proposal"
    if state in APPROVED_STATES or human == "approved":
        slot = f" (slot {inp.approved_slot})" if inp.approved_slot else ""
        return "approved", "passed", f"approved{slot}{reason}"
    if state in PENDING_STATES or human == "pending":
        return "pending", "info", "awaiting the operator's decision"
    return "none", "info", "no decision recorded"


def _execution(x: _Line) -> tuple[str, Status, str]:
    """(kind, status, words): kind is filled | partly | not_filled | pending."""
    inp = x.inp
    fills = inp.fills.get(x.line, ())
    achieved = inp.achieved_x.get(x.line)
    got = f"; achieved {pct(achieved)}" if achieved is not None else ""
    if fills:
        states = {f.state for f in fills}
        if states <= FILLED:
            return "filled", "passed", f"filled{got}"
        if states & (FILLED | PARTLY):
            return "partly", "changed", f"partly filled ({', '.join(sorted(states))}){got}"
        if states <= NOT_FILLED:
            return "not_filled", "blocked", f"not filled ({', '.join(sorted(states))})"
        return "pending", "info", f"not resolved yet ({', '.join(sorted(states))})"
    state = inp.execution_state or inp.decision_state or ""
    if state in ("blocked", "execution_unknown"):
        return "pending", "blocked", f"execution {state.replace('_', ' ')}: operator review"
    if state == "waiting_for_market":
        return "pending", "info", "waiting for the market to open"
    if state in ("completed", "completed_partial", "reviewed_no_action"):
        return "not_filled", "blocked", "no fill recorded for this line"
    return "pending", "info", "not executed yet"


def _headline(x: _Line, outcome: str, asker: tuple[str, str] | None, stopped: str | None = None) -> str:
    move = f"{_verb(x.base, x.final)}, {pct(x.base)} → {pct(x.final)}"
    if outcome == "pending" and stopped == "execution":      # approved; the fill is not resolved
        return f"{x.title} — {move} approved; execution not resolved"
    if outcome in ("changed", "changed_partly"):
        return f"{x.title} — {move}" + (" (partly)" if outcome == "changed_partly" else "")
    if outcome == "reference_trade":
        return f"{x.title} — {move} (the reference rule)"
    if outcome == "pending":
        return f"{x.title} — {move} proposed; awaiting the operator"
    if outcome in ("rejected", "expired", "not_filled"):
        verdict = {"rejected": "rejected by the operator", "expired": "not approved in time",
                   "not_filled": "not filled"}[outcome]
        return f"{x.title} — {move} proposed; {verdict}"
    if outcome == "not_ordered" and x.changed and not x.legs:
        return f"{x.title} — {move} on paper; not ordered"
    if asker is not None:
        who, what = asker
        verdict = "held" if outcome in ("too_small", "held_by_engine") else "not done"
        return f"{x.title} — {who} asked for {what}; {verdict}"
    if outcome == "not_taken_by_manager" and x.unlock_unused:
        return f"{x.title} — a qualifying card allowed a cut; nobody used it"
    if outcome == "too_small":
        return f"{x.title} — too small to trade; held at {pct(x.final)}"
    if outcome == "held_by_engine":
        return f"{x.title} — held by the engine at {pct(x.final)}"
    return f"{x.title} — held at {pct(x.final)}"


def _asker(x: _Line) -> tuple[str, str] | None:
    """(who asked, what they asked for): the manager first, then an advocate, then the reference
    rule when its weight differs from the book and the engine held the move."""
    if x.tried:
        _rep, dev = next(((r, d) for r, d in x.tried if r.replicate == x.inp.medoid), x.tried[0])
        return "the manager", ask_noun(float(dev.level), x.ref_level)
    for speaker, level in x.asks:
        return f"the {SPEAKERS.get(speaker, speaker)}", ask_noun(level, x.ref_level)
    if x.reference_asked:
        return "the reference rule", f"{pct(x.ref_x)} (from {pct(x.base)})"
    return None


def _engine_outcome(x: _Line) -> Outcome | None:
    codes = [note_code(n) for n in x.notes] + [trace_rules.public_code(t.code) for t in x.trace
                                                 if t.before_x is not None and t.after_x is not None
                                                 and abs(t.after_x - t.before_x) > EPS]
    if not codes:
        return None
    return "too_small" if trace_rules.R11 in codes else "held_by_engine"


def line_trail(inp: TrailInput, line: str) -> LineTrail:
    """The trail of one line (see the module docstring)."""
    x = _Line(inp, line)
    asked_by = tuple(dict.fromkeys(
        [_ADVOCATE_ACTOR.get(s, s) for s, _ in x.asks] + (["pm"] if x.valid_tried else [])))
    if not x.has_trail:
        at_ref = x.ref_x is None or abs(x.base - x.ref_x) <= X_TOL
        words = "held the reference; nobody asked" if at_ref else f"held at {pct(x.base)}; nobody asked"
        return LineTrail(line, "held", None, (), (), f"{x.title} — {words}", base_x=x.base, final_x=x.final)
    if (x.changed and not x.valid_tried) or x.reference_asked:
        asked_by = (*asked_by, "reference")

    band = _band_step(x)
    steps: list[TrailStep] = [_reference_step(x), *_officer_steps(x), *_analyst_steps(x),
                              *([band] if band is not None else []), *_debate_steps(x)]
    steps += [s for s in (_manager_step(x), _auditor_step(x), _medoid_step(x), _risk_step(x), _plan_step(x))
              if s is not None]

    outcome: Outcome
    stopped: str | None = None
    why = ""
    if x.legs:
        kind, h_status, h_words = _human(inp)
        steps.append(TrailStep("human", "human", h_status, h_words))
        if kind == "approved":
            e_kind, e_status, e_words = _execution(x)
            steps.append(TrailStep("execution", "costs", e_status, e_words, after_x=inp.achieved_x.get(line)))
            if e_kind == "not_filled":
                outcome, stopped, why = "not_filled", "execution", e_words
            elif e_kind == "pending":
                outcome, stopped, why = "pending", "execution", e_words
            elif asked_by == ("reference",) or not x.valid_tried:
                outcome = "reference_trade"
            else:
                target = inp.banded_x.get(line)
                partial = e_kind == "partly" or (target is not None and abs(x.final - target) > X_TOL)
                outcome = "changed_partly" if partial else "changed"
        elif kind == "pending":
            outcome, stopped, why = "pending", "human", h_words
        elif kind in ("rejected", "expired"):
            outcome, stopped, why = kind, "human", h_words
        else:
            outcome, stopped, why = "pending", "human", h_words
    elif x.changed:
        stopped = "plan"
        if trace_rules.R11 in x.skips:
            outcome, why = "too_small", TOO_SMALL
        else:
            outcome = "not_ordered"
            why = next((s.words for s in steps if s.stage == "plan"), "not ordered")
    elif x.skips:
        stopped = "plan"
        outcome = "too_small" if trace_rules.R11 in x.skips else "not_ordered"
        why = TOO_SMALL if outcome == "too_small" else _join(skip_words(s) for s in x.skips)
    else:
        outcome, stopped, why = _why_not(x)

    asker = _asker(x)
    if x.changed or x.legs:
        # the change that stopped at the plan, the human or the execution is the book's own move:
        # the manager's when an attempt asked for it, else the reference rule's
        asker = _asker(x) if x.valid_tried else None
    if why and asker is not None and outcome not in ("changed", "changed_partly", "reference_trade"):
        who, what = asker
        why = f"{who} asked for {what}; not done: {why}"
    return LineTrail(line, outcome, stopped, asked_by, tuple(steps), _headline(x, outcome, asker, stopped),
                     why_not=why, base_x=x.base, final_x=x.final)


def _why_not(x: _Line) -> tuple[Outcome, str | None, str]:
    """The first gate that failed for a line that did not change (design §4.4)."""
    engine = _engine_outcome(x)
    if not x.tried:
        if x.asks or x.unlock_unused:
            words = "the manager did not take it up"
            return "not_taken_by_manager", "manager", words
        if engine is not None:
            return engine, "risk", _join(note_words(n) for n in x.notes) or "the engine held it"
        return "held", None, "the line already sat at the level asked"
    if all(x.line in rep.reverted or not rep.valid for rep, _ in x.tried):
        return "reverted_by_auditor", "auditor", "the auditor reverted every attempt's deviation"
    kept = [(rep, dev) for rep, dev in x.valid_tried if x.line not in rep.reverted]
    if kept and all(abs(float(rep.levels.get(x.line, x.ref_level)) - x.ref_level) <= EPS for rep, _ in kept):
        band = x.band
        why = counterfactual(band, float(kept[0][1].level)) if band is not None else "outside the band"
        return "not_allowed_by_band", "band", f"the band clipped it back: {why}"
    medoid_level = x.medoid.levels.get(x.line) if x.medoid is not None else None
    if x.fallback or (medoid_level is not None and abs(float(medoid_level) - x.ref_level) <= EPS):
        n = len(x.valid)
        k = len({rep.replicate for rep, _ in kept})
        return "no_agreement", "medoid", f"only {k} of {n} attempts asked for it"
    if engine is not None:
        return engine, "risk", _join(note_words(n) for n in x.notes) or "the engine held it"
    return "held_by_engine", "risk", "the engine held it"


def build(inp: TrailInput) -> list[LineTrail]:
    """One trail per line: the lines `names` lists first, in its (the policy's) order, then the
    rest in the input's order (stored JSON sorts its keys, so the policy order is the stable one)."""
    ranked = {line: i for i, line in enumerate(inp.names)}
    order = sorted(inp.lines, key=lambda line: (ranked.get(line, len(ranked)), inp.lines.index(line)))
    return [line_trail(inp, line) for line in order]


# ------------------------------------------------------------------------------ rendering
_WIDTH = 100


def render_trail(trail: LineTrail) -> list[str]:
    head = trail.headline
    tag = f"outcome: {trail.outcome}"
    pad = max(2, _WIDTH - len(head) - len(tag))
    out = [f"{head}{' ' * pad}{tag}"]
    if trail.why_not:
        out.append(f"   why not: {trail.why_not}")
    for step in trail.steps:
        first, *rest = step.words.split("\n")
        out.append(f"{step.number:>2} {step.label:<13}{first}")
        out += [f"{'':16}{more}" for more in rest]
    return out


def render_text(trails: Sequence[LineTrail], *, header: str = "", line: str | None = None,
                show_held: bool = True) -> str:
    """The trails as terminal text: one block per line with a trail, then one sentence for the
    lines nobody asked about. `line` keeps one line only (its held sentence when it has no trail)."""
    chosen = [t for t in trails if line is None or t.line == line]
    out: list[str] = [header] if header else []
    moved = [t for t in chosen if t.outcome != "held"]
    held = [t for t in chosen if t.outcome == "held"]
    for t in moved:
        out += ["", *render_trail(t)]
    if held and (show_held or line is not None):
        out.append("")
        if line is not None:
            out.append(held[0].headline)
        else:
            out.append("Held the reference, nobody asked: " + ", ".join(t.line for t in held))
    return "\n".join(out).lstrip("\n") + "\n"


def summary(trails: Sequence[LineTrail]) -> list[dict[str, Any]]:
    """A compact record of the trails (outcome, where it stopped, who asked) for the ledger."""
    return [{"line": t.line, "outcome": t.outcome, "stopped_at": t.stopped_at,
             "asked_by": list(t.asked_by)} for t in trails if t.outcome != "held"]


# ------------------------------------------------------------------------------ public source
def ref_id(ref: Any) -> str:
    """The evidence id of a public evidence ref (a FRED ref is rebuilt as `M:series[.measure]@date`)."""
    kind = getattr(ref, "kind", None) if not isinstance(ref, Mapping) else ref.get("kind")
    get = (lambda k: ref.get(k)) if isinstance(ref, Mapping) else (lambda k: getattr(ref, k, None))
    if kind == "fred":
        eid = f"M:{get('series')}"
        if get("measure"):
            eid += f".{get('measure')}"
        if get("as_of"):
            eid += f"@{get('as_of')}"
        return eid
    return str(get("id") or "")


def _ids(refs: Iterable[Any]) -> tuple[str, ...]:
    return tuple(i for i in (ref_id(r) for r in refs or ()) if i)


def _slot_words(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%H:%M UTC")
    if isinstance(value, str) and "T" in value:
        return value.split("T", 1)[1][:5] + " UTC"
    return ""


def trails(c: Any, execution: Any = None, ops: Any = None, *,
           names: Mapping[str, str] | None = None) -> list[LineTrail]:
    """The trails of a PUBLIC cycle document (`PublicCycleV1` or its JSON), with its execution file
    and ops row when there are any (they carry the final decision)."""
    return build(public_input(c, execution, ops, names=names))


def public_input(c: Any, execution: Any = None, ops: Any = None, *,
                 names: Mapping[str, str] | None = None) -> TrailInput:
    from council.publish.public_models import PublicCycleV1, PublicExecution, PublicOpsRow

    doc = c if isinstance(c, PublicCycleV1) else PublicCycleV1.model_validate(c)
    ex = (execution if isinstance(execution, PublicExecution) or execution is None
          else PublicExecution.model_validate(execution))
    row = ops if isinstance(ops, PublicOpsRow) or ops is None else PublicOpsRow.model_validate(ops)
    risk = doc.risk
    order: list[str] = []
    for keys in (doc.reference, doc.bands, risk.base_x if risk else {}, risk.final_x if risk else {},
                 [leg.line for leg in doc.plan.legs] if doc.plan else []):
        for k in keys:
            if k not in order:
                order.append(k)
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    item_lines: dict[str, list[str]] = {}
    for f in doc.facts:
        shown = fact_display(f.value, f.unit)
        if shown is not None:
            values[f.id] = shown
        if f.kind == "news" and f.source:
            sources[f.id] = f.source
        if f.kind in ("news", "event", "filing") and f.line:
            item_lines[f.id] = [f.line]
    cards = []
    for card in doc.cards:
        news = [r for r in card.evidence if getattr(r, "kind", "") in ("public_news", "broker_feed")]
        filing_only = (card.card_type == "news_material" and bool(news)
                       and all(getattr(r, "kind", "") == "public_news" and getattr(r, "source", None) == "sec"
                               for r in news))
        cards.append(CardView(card.card_id, card.role, card.card_type, tuple(card.scope), card.direction,
                              card.qualifying, tuple(card.corroborated_by), _ids(card.evidence), filing_only))
    scopes = {c.card_id: c.scope for c in cards}
    known = set(order)

    def tag(ids: Iterable[str]) -> tuple[str, ...]:
        return tuple(evidence_lines(ids, lines=known, item_lines=item_lines, card_scope=scopes))

    advocates = []
    bull_claim_lines: dict[str, tuple[str, ...]] = {}
    for speaker, adv in (("bull_open", doc.debate.bull), ("bear", doc.debate.bear),
                         ("bull_rebuttal", doc.debate.rebuttal)):
        if adv is None:
            continue
        claims = tuple(ClaimView(speaker, cl.claim_id, cl.text, _ids(cl.evidence), tag(_ids(cl.evidence)))
                       for cl in adv.claims)
        if speaker == "bull_open":
            bull_claim_lines = {cl.claim_id: cl.lines for cl in claims}
        rebuttals = tuple(
            RebuttalView(r.claim_id, r.verdict, r.text, _ids(r.evidence),
                         tuple(dict.fromkeys(tag(_ids(r.evidence)) + bull_claim_lines.get(r.claim_id, ()))))
            for r in adv.rebuttals)
        advocates.append(AdvocateView(speaker, dict(adv.proposal_levels), claims, rebuttals,
                                      tuple(adv.concessions)))
    replicates = tuple(
        ReplicateView(
            r.replicate, r.valid, dict(r.levels),
            tuple(DeviationView(d.line, d.level, d.direction, d.reason, _ids(d.evidence)) for d in r.deviations),
            r.decisive_fact.text if r.decisive_fact else "",
            ref_id(r.decisive_fact.evidence) if r.decisive_fact and r.decisive_fact.evidence else "",
            r.sided_with, tuple((d.claim_id, d.why) for d in r.dismissed), r.no_change_reason,
            tuple(r.violations), tuple(r.reverted))
        for r in doc.pm.replicates)
    legs: dict[str, list[LegView]] = {}
    skipped: dict[str, tuple[str, ...]] = {}
    if doc.plan is not None:
        for leg in doc.plan.legs:
            legs.setdefault(leg.line, []).append(LegView(
                leg.kind, leg.direction, leg.weight_before_x, leg.weight_after_x, leg.settlement,
                leg.leverage, leg.cost_bp))
        skipped = skips_by_line(doc.plan.skipped)
    fills: dict[str, list[FillView]] = {}
    if ex is not None:
        for fill in ex.fills:
            fills.setdefault(fill.line, []).append(FillView(fill.state, fill.weight_target_x, fill.weight_filled_x))
    decision = doc.decision
    state = row.decision_state if row is not None else decision.state
    human = row.human_outcome if row is not None else decision.human_outcome
    reason = (row.decision_reason if row is not None else decision.reason) or ""
    approved = (row.approved_slot if row is not None else None) or decision.approved_slot
    if ex is not None:
        state = ex.decision_state
        approved = approved or ex.approved_slot
    return TrailInput(
        cycle_id=doc.cycle_id, live=doc.mode == "live", lines=tuple(order), basis=doc.basis,
        kill_state=doc.kill_state, names=dict(names or {}),
        ref_trend={k: v.trend for k, v in doc.reference.items()},
        ref_level={k: v.level_ref for k, v in doc.reference.items()},
        ref_x={k: v.weight_ref_x for k, v in doc.reference.items()},
        values=values, sources=sources, cards=tuple(cards),
        bands={k: BandView(b.trend, b.ref_level, b.lo, b.hi, tuple(b.reasons), tuple(b.qualifying_cards))
               for k, b in doc.bands.items()},
        advocates=tuple(advocates), replicates=replicates, medoid=doc.pm.medoid,
        agreement={k: v / 100.0 for k, v in doc.pm.agreement_pct.items()},
        council_levels=dict(risk.raw_levels) if risk else dict(doc.pm.levels),
        base_x=dict(risk.base_x) if risk else {}, banded_x=dict(risk.banded_x) if risk else {},
        final_x=dict(risk.final_x) if risk else {}, has_risk=risk is not None,
        notes=notes_by_line(risk.hold_reasons) if risk else {},
        legs={k: tuple(v) for k, v in legs.items()}, skipped=skipped, plan_present=doc.plan is not None,
        plan_failed=any(f.startswith("plan_failed") for f in doc.flags),
        decision_state=state, human=human, reason=reason, approved_slot=_slot_words(approved),
        execution_state=ex.decision_state if ex is not None else None,
        fills={k: tuple(v) for k, v in fills.items()},
        achieved_x=dict(ex.achieved_x) if ex is not None else {},
    )


# ------------------------------------------------------------------------------ private source
def record_trails(rec: Any, *, names: Mapping[str, str] | None = None, decision_state: str | None = None,
                  reason: str | None = None, approved_at: Any = None,
                  leg_states: Mapping[str, Sequence[str]] | None = None,
                  achieved_x: Mapping[str, float] | None = None) -> list[LineTrail]:
    """The trails of a PRIVATE `CycleRecord` (operator only). The decision and execution arguments
    come from the ledger's current state (the record itself is written at the cycle's end)."""
    return build(record_input(rec, names=names, decision_state=decision_state, reason=reason,
                              approved_at=approved_at, leg_states=leg_states, achieved_x=achieved_x))


def _filing_only_cards(dropped: Iterable[str]) -> set[str]:
    out = set()
    for note in dropped:
        m = re.search(r"card (K:[a-z_]+:\d+): filing_metadata_only", note)
        if m:
            out.add(m.group(1))
    return out


def record_input(rec: Any, *, names: Mapping[str, str] | None = None, decision_state: str | None = None,
                 reason: str | None = None, approved_at: Any = None,
                 leg_states: Mapping[str, Sequence[str]] | None = None,
                 achieved_x: Mapping[str, float] | None = None) -> TrailInput:
    from council.models.common import snap_level

    risk = rec.risk
    entries = rec.reference.entries if rec.reference is not None else {}
    order: list[str] = []
    for keys in (entries, rec.bands, risk.base_w if risk else {}, risk.final_w if risk else {},
                 [leg.line for leg in rec.plan.legs if leg.line] if rec.plan else []):
        for k in keys:
            if k not in order:
                order.append(k)
    known = set(order)
    filing_only = _filing_only_cards(rec.dropped_cards)
    cards = tuple(CardView(c.card_id, c.role, c.card_type, tuple(c.scope), c.direction, c.qualifying,
                           tuple(c.corroborated_by), tuple(c.evidence_ids), c.card_id in filing_only)
                  for c in rec.cards)
    scopes = {c.card_id: c.scope for c in cards}
    recorded = dict(rec.claim_lines or {})

    def tag(key: str, ids: Iterable[str]) -> tuple[str, ...]:
        if key in recorded:
            return tuple(recorded[key])
        return tuple(evidence_lines(ids, lines=known, card_scope=scopes))

    advocates = []
    for speaker, case in (("bull_open", rec.debate.bull_open), ("bear", rec.debate.bear),
                          ("bull_rebuttal", rec.debate.bull_rebuttal)):
        if case is None:
            continue
        claims = tuple(ClaimView(speaker, c.claim_id, c.text, tuple(c.evidence_ids),
                                 tag(f"{speaker}:{c.claim_id}", c.evidence_ids)) for c in case.claims)
        rebuttals = tuple(
            RebuttalView(r.claim_id, r.verdict, r.text, tuple(r.evidence_ids),
                         tag(f"bear:rebuttal:{r.claim_id}", r.evidence_ids))
            for r in getattr(case, "rebuttals", ()) or ())
        advocates.append(AdvocateView(speaker, dict(case.proposal), claims, rebuttals, tuple(case.concessions)))
    ref_levels = {s: e.level_ref for s, e in entries.items()}
    reps = []
    for rep in rec.pm:
        d = rep.decision
        levels = dict(rep.enforced_levels) or (d.levels(ref_levels) if d is not None else {})
        reps.append(ReplicateView(
            rep.replicate, rep.valid, levels,
            tuple(DeviationView(dev.symbol, snap_level(float(dev.level)), dev.direction, dev.reason,
                                tuple(dev.evidence_ids)) for dev in (d.deviations if d else [])),
            d.decisive_fact.text if d else "", d.decisive_fact.evidence_id if d else "",
            d.sided_with if d else None, tuple((x.claim_id, x.why) for x in (d.dismissed if d else [])),
            d.no_change_reason if d else "", tuple(rep.audit_violations), tuple(rep.reverted)))
    unit = {s: e.unit_weight for s, e in entries.items()}
    value_lines = frozenset(rec.value_lines or ())
    notes = notes_by_line(trace_rules.public_hold_reason(n, value_lines=value_lines)
                          for n in (risk.hold_reasons if risk else []))
    trace: dict[str, tuple[TraceView, ...]] = {}
    for line, steps in ((risk.line_trace or {}) if risk else {}).items():
        views = []
        for st in steps:
            code, value, limit = trace_rules.public_step_values(st.code, st.value, st.limit, st.inputs)
            views.append(TraceView(st.stage, code, st.before_w, st.after_w, value, limit))
        trace[line] = tuple(views)
    legs: dict[str, list[LegView]] = {}
    skipped: dict[str, tuple[str, ...]] = {}
    if rec.plan is not None:
        for leg in rec.plan.legs:
            if leg.line:
                legs.setdefault(leg.line, []).append(LegView(
                    leg.kind, leg.direction, leg.weight_before, leg.weight_after, leg.settlement,
                    leg.leverage, leg.cost_bps_nav, leg.origin))
        skipped = skips_by_line(rec.plan.skipped)
    fills = {line: tuple(FillView(s) for s in states) for line, states in (leg_states or {}).items()}
    news = ((rec.extras or {}).get("news_fetch") or {}).get("public_items") or []
    sources = {str(i.get("id")): str(i.get("source")) for i in news if isinstance(i, Mapping) and i.get("source")}
    state = decision_state if decision_state is not None else rec.decision_state
    return TrailInput(
        cycle_id=rec.cycle_id, live=rec.mode == "live", lines=tuple(order),
        basis=risk.basis if risk else None, kill_state=rec.kill_state, names=dict(names or {}),
        ref_trend={s: e.trend for s, e in entries.items()}, ref_level=ref_levels,
        ref_x={s: e.weight_ref for s, e in entries.items()},
        values=dict(rec.evidence_values or {}), sources=sources, cards=cards,
        bands={s: BandView(b.trend, b.ref_level, b.lo, b.hi, tuple(b.reasons), tuple(b.qualifying_cards))
               for s, b in rec.bands.items()},
        code_bands={s: BandView(b.trend, b.ref_level, b.lo, b.hi, tuple(b.reasons), tuple(b.qualifying_cards))
                    for s, b in (rec.code_bands or {}).items()},
        drops=tuple(DropView(d.role, d.what, d.index, d.code, tuple(d.ids), d.target, tuple(d.lines))
                    for d in rec.drops),
        advocates=tuple(advocates), replicates=tuple(reps), medoid=rec.medoid_replicate,
        agreement=dict(rec.agreement),
        # recorded since the trail hook (its summary marks the record); inferred for older records
        fallback=(frozenset(rec.fallback_lines)
                  if rec.fallback_lines or "trail" in (rec.extras or {}) else None),
        council_levels=dict(risk.raw_levels) if risk else {},
        base_x=dict(risk.base_w) if risk else {},
        banded_x={s: float(v) * unit.get(s, 0.0) for s, v in (risk.banded_levels if risk else {}).items()},
        final_x=dict(risk.final_w) if risk else {}, has_risk=risk is not None, notes=notes, trace=trace,
        material=frozenset(rec.material_lines), legs={k: tuple(v) for k, v in legs.items()},
        skipped=skipped, plan_present=rec.plan is not None,
        plan_failed=any(str(f).startswith("plan_failed") for f in rec.flags),
        decision_state=state, human=_human_of(state),
        reason=reason if reason is not None else (rec.decision_reason or ""),
        approved_slot=_slot_words(_slot(approved_at if approved_at is not None else rec.approved_at)),
        execution_state=state, fills=fills, achieved_x=dict(achieved_x or {}),
    )


_HUMAN_OF = {
    "awaiting_publication": "pending", "proposed": "pending", "approved": "approved",
    "executing": "approved", "completed": "approved", "completed_partial": "approved",
    "blocked": "approved", "execution_unknown": "approved", "waiting_for_market": "approved",
    "rejected": "rejected", "expired": "expired", "superseded": "superseded",
    "reviewed_no_action": "no_action",
}


def _human_of(state: str | None) -> str:
    return _HUMAN_OF.get(state or "", "none")


def _slot(value: Any) -> datetime | None:
    """A timestamp rounded down to its 4-hour slot (the public record's approval precision)."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    from council import clock

    return clock.slot_at_or_before(value)


# ------------------------------------------------------------------ cycle-time helpers (private)
def cited_ids(rec: Any) -> list[str]:
    """Every evidence id the trail may print for a record: card, claim, rebuttal, deviation and
    decisive-fact evidence, plus each line's SMA distances."""
    ids: list[str] = []

    def add(values: Iterable[str]) -> None:
        for v in values:
            if v and v not in ids:
                ids.append(v)

    for card in rec.cards:
        add(card.evidence_ids)
    for case in (rec.debate.bull_open, rec.debate.bear, rec.debate.bull_rebuttal):
        if case is None:
            continue
        for claim in case.claims:
            add(claim.evidence_ids)
        for r in getattr(case, "rebuttals", ()) or ():
            add(r.evidence_ids)
    for rep in rec.pm:
        if rep.decision is not None:
            add([rep.decision.decisive_fact.evidence_id])
            for dev in rep.decision.deviations:
                add(dev.evidence_ids)
    entries = rec.reference.entries if rec.reference is not None else {}
    for line in entries:
        add([f"F:{line}:dist_sma50", f"F:{line}:dist_sma200"])
    return ids


def evidence_values(pack: Any, lines: Any, ids: Iterable[str]) -> dict[str, str]:
    """{evidence id: display value} for `ids`, from the facts table the public record would carry
    (`redact` decides what may carry a value: docs/data-rights.md), so a licensed or broker-derived
    value is never stored for the trail. Values only; no text."""
    from council.publish import redact

    table, _dropped, _mismatch = redact._facts(pack, redact.LineMap(lines))
    wanted = set(ids)
    out: dict[str, str] = {}
    for fact in table:
        if fact.id in wanted:
            shown = fact_display(fact.value, fact.unit)
            if shown is not None:
                out[fact.id] = shown
    return out


def value_lines(pack: Any, lines: Any) -> list[str]:
    """The lines whose R15 SR_be the trail may show: every input public (`redact.value_lines`)."""
    from council.publish import redact

    shown, _ = redact.value_lines(pack, redact.LineMap(lines))
    return sorted(shown)
