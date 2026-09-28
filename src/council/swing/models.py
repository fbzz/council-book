"""Swing-book models (design swing-book.md rev 2, §1.3 Scout, §1.5 Skeptic, §1.7 PM, SW-2 states).

Pure data: the role outputs as strict pydantic models (``extra="forbid"``, length caps) and the swing
trade state machine as a transition table. Nothing here touches the ledger, a broker or an LLM; the
ledger wiring (``swing_trades`` rows, the ``swing:`` blocker scope) is SW-2b, and the code rules that
turn a decoded output into an accepted one (H3/H4/H4b/H6/H8/H10, the §1.5 verdict rules) are
``swing/roles.py`` (SW-3).

Schema bounds vs code rules: the bounds below are what a model output must satisfy to *decode*
(a violation is a validation failure, retried then dropped). Whether an id was admitted this slot,
whether a stop clears 1 x ATR (S5) or a target the volatility cap (S6), and the net reward/risk
floor are code rules applied after decoding against the fact card and ``policy/swing.yaml``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from council.models.common import Strict
from council.models.pm import DecisiveFact, Dismissal
from council.swing.policy import Setup

__all__ = [
    "ACTIVE_STATES", "CLOSED_STATES", "IDEA_ACTIONS", "OPEN_STATES", "SWING_BLOCKING_STATES",
    "TERMINAL_STATES", "TRADE_ACTIONS", "TRADE_STATES", "TRANSITIONS",
    "AggregatedSwingAction", "Idea", "SwingBearCase", "SwingCase", "SwingClaim", "SwingRebuttal", "IllegalTransition", "PricedIn", "ScoutIdea", "ScoutOutput",
    "Setup", "SkepticReason", "SkepticVerdict", "SwingAction", "SwingPMDecision", "TradeState",
    "Verdict", "can_transition", "check_transition", "is_idea_ref", "is_trade_ref",
]

# ---------------------------------------------------------------------------------------------
# Shared field types
# ---------------------------------------------------------------------------------------------

# Exact US symbol as the Scout writes it ("NVDA", "BRK.B", "BF-B"); resolution to an eToro
# instrument and an SEC registrant is H1/H2 in swing/resolve.py, never the model's memory.
Ticker = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9]{0,5}([.\-][A-Z]{1,2})?$")]
# Catalyst ids come from the slot's reading list / movers screen (P:, N:, S:, M:). Admission
# (H3) and "about the ticker" (H4) are checked in code, so one bad id drops one idea, not the output.
CatalystId = Annotated[str, StringConstraints(pattern=r"^[PNSM]:\S{1,80}$")]
# Any cited evidence id (fact card, reading list, core pack); unknown ids are stripped by H6.
EvidenceId = Annotated[str, StringConstraints(pattern=r"^[A-Z]{1,2}:\S{1,100}$")]
IdeaRef = Annotated[str, StringConstraints(pattern=r"^idea:[A-Za-z0-9_\-]{1,40}$")]
TradeRef = Annotated[str, StringConstraints(pattern=r"^trade:[A-Za-z0-9_\-]{1,64}$")]
SwingRef = Annotated[str, StringConstraints(pattern=r"^(idea:[A-Za-z0-9_\-]{1,40}|trade:[A-Za-z0-9_\-]{1,64})$")]
Side = Literal["long", "short"]

StopPct = Annotated[float, Field(ge=0.02, le=0.12)]      # S5 outer bounds (short <= 0.08, validator)
TargetPct = Annotated[float, Field(ge=0.03, le=0.30)]    # S6 outer bounds
TimeStopDays = Annotated[int, Field(strict=True, ge=3, le=15)]   # S7, US trading days
SHORT_MAX_STOP_PCT = 0.08


def _text(max_len: int, min_len: int = 1):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=min_len,
                                            max_length=max_len)]


def is_idea_ref(ref: str) -> bool:
    return ref.startswith("idea:")


def is_trade_ref(ref: str) -> bool:
    return ref.startswith("trade:")


# ---------------------------------------------------------------------------------------------
# ② Scout (§1.3)
# ---------------------------------------------------------------------------------------------

class ScoutIdea(Strict):
    ticker: Ticker
    side: Side
    setup: Setup                                     # paper-only setups are dropped by code (SB16)
    catalyst_ids: list[CatalystId] = Field(min_length=1, max_length=4)
    catalyst_claim: _text(120)                       # FACTUAL; the only Scout text the Skeptic sees
    thesis: _text(400)
    why_not_priced_in: _text(240)                    # never shown to the Skeptic
    entry: Literal["now", "on_pullback", "on_break"]  # v1 executes "now" only (§4.2)
    stop_pct: StopPct                                # distance from entry
    target_pct: TargetPct                            # distance from entry
    time_stop_days: TimeStopDays
    invalidation: _text(160)

    @model_validator(mode="after")
    def _short_stop_cap(self) -> ScoutIdea:
        if self.side == "short" and self.stop_pct > SHORT_MAX_STOP_PCT:
            raise ValueError(f"short stop_pct above {SHORT_MAX_STOP_PCT}")
        if len(set(self.catalyst_ids)) != len(self.catalyst_ids):
            raise ValueError("duplicate catalyst id")
        return self


Idea = ScoutIdea


class ScoutOutput(Strict):
    ideas: list[ScoutIdea] = Field(default_factory=list, max_length=5)   # best first; 0 is normal
    passed: list[Ticker] = Field(default_factory=list, max_length=10)    # audit only


# ---------------------------------------------------------------------------------------------
# ④ Skeptic (§1.5) - blind: its input never carries thesis / why_not_priced_in / setup / levels
# ---------------------------------------------------------------------------------------------

Verdict = Literal["pass", "wait", "reject"]
PricedIn = Literal["no", "partly", "mostly", "fully"]


class SkepticReason(Strict):
    text: _text(200)
    evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=4)


class SkepticVerdict(Strict):
    idea_ref: IdeaRef
    catalyst_supports_claim: bool                    # Q0 (H4b); false -> catalyst_misread
    claim_supports_side: bool                        # Q0; false -> catalyst_misread
    verdict: Verdict
    priced_in: PricedIn                              # Q1 already happened / priced in
    news_status: Literal["new", "follow_up", "stale", "restated"]   # Q2 old news
    regime: Literal["supports", "neutral", "against"]               # Q4 bigger picture
    crowding: Literal["low", "medium", "high", "unknown"]           # Q5 ("unknown" is not "low")
    reasons: list[SkepticReason] = Field(min_length=2, max_length=5)
    what_would_change_my_mind: _text(160)
    second_order: _text(160) | None = None           # Q3; a suggestion only (next slot, via the gate)

    def evidence_ids(self) -> list[str]:
        """Every id cited across the reasons, first-seen order, no duplicates."""
        seen: dict[str, None] = {}
        for reason in self.reasons:
            for eid in reason.evidence_ids:
                seen.setdefault(eid, None)
        return list(seen)


# ---------------------------------------------------------------------------------------------
# ⑤ Swing debate (§1.6): one bull call, one bear call (the bear sees the bull); no rebuttal
# ---------------------------------------------------------------------------------------------
class SwingClaim(Strict):
    """A debate claim scoped to one ref (the design's `Claim` with `scope` = the idea or trade)."""

    claim_id: Annotated[str, StringConstraints(pattern=r"^c\d{1,2}$")]
    ref: SwingRef                                    # H8: closed set, checked in roles.py
    text: _text(300)
    evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=6)


class SwingRebuttal(Strict):
    claim_id: Annotated[str, StringConstraints(pattern=r"^c\d{1,2}$")]   # the BULL's claim id
    verdict: Literal["concede", "refute"]
    text: _text(240)
    evidence_ids: list[EvidenceId] = Field(default_factory=list, max_length=4)


class SwingCase(Strict):
    """Swing bull output (and the base of the bear's)."""

    argument: _text(1200)
    claims: list[SwingClaim] = Field(default_factory=list, max_length=8)
    strongest_opposing_fact_id: EvidenceId

    @model_validator(mode="after")
    def _unique_claims(self) -> SwingCase:
        ids = [c.claim_id for c in self.claims]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate claim_id")
        return self


class SwingBearCase(SwingCase):
    rebuttals: list[SwingRebuttal] = Field(default_factory=list, max_length=8)


# ---------------------------------------------------------------------------------------------
# ⑥ Swing PM (§1.7) and its 2-of-3 aggregate
# ---------------------------------------------------------------------------------------------

IDEA_ACTIONS = frozenset({"enter", "pass"})
TRADE_ACTIONS = frozenset({"hold", "exit"})
SwingActionKind = Literal["enter", "pass", "hold", "exit"]


class SwingAction(Strict):
    ref: SwingRef                                    # H8: closed set, checked in roles.py
    action: SwingActionKind
    stop_pct: StopPct | None = None                  # entries may tighten, never widen past S5
    target_pct: TargetPct | None = None
    time_stop_days: TimeStopDays | None = None
    evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=6)
    reason: _text(300)

    @model_validator(mode="after")
    def _action_fits_ref(self) -> SwingAction:
        allowed = IDEA_ACTIONS if is_idea_ref(self.ref) else TRADE_ACTIONS
        if self.action not in allowed:
            raise ValueError(f"action {self.action!r} not valid for {self.ref.split(':')[0]} ref")
        if self.action in ("pass", "exit") and (
                self.stop_pct is not None or self.target_pct is not None
                or self.time_stop_days is not None):
            raise ValueError(f"{self.action!r} carries no levels")
        return self


class SwingPMDecision(Strict):
    actions: list[SwingAction] = Field(default_factory=list, max_length=12)  # <= 5 ideas + 6 trades
    decisive_fact: DecisiveFact
    dismissed: list[Dismissal] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _one_action_per_ref(self) -> SwingPMDecision:
        refs = [a.ref for a in self.actions]
        if len(set(refs)) != len(refs):
            raise ValueError("more than one action for the same ref")
        return self


class AggregatedSwingAction(Strict):
    """One ref after aggregating the PM replicates (swing/aggregate.py): an entry or an exit needs
    a strict majority of the replicates that ran (``votes_for >= 2`` of 3; with the budget drop to a
    single replicate, 1 of 1, and the Skeptic's pass is then the second key, §5); levels are the
    agreeing replicates' medians, clipped later by code."""

    ref: SwingRef
    action: SwingActionKind
    votes_for: int = Field(strict=True, ge=0, le=3)  # replicates that chose `action`
    replicates: int = Field(default=3, strict=True, ge=1, le=3)
    failed_replicates: int = Field(default=0, strict=True, ge=0, le=3)   # counted as pass/hold
    stop_pct: StopPct | None = None
    target_pct: TargetPct | None = None
    time_stop_days: TimeStopDays | None = None

    @model_validator(mode="after")
    def _consistent(self) -> AggregatedSwingAction:
        allowed = IDEA_ACTIONS if is_idea_ref(self.ref) else TRADE_ACTIONS
        if self.action not in allowed:
            raise ValueError(f"action {self.action!r} not valid for {self.ref.split(':')[0]} ref")
        if self.votes_for > self.replicates or self.failed_replicates > self.replicates:
            raise ValueError("votes exceed replicates")
        if self.action in ("enter", "exit") and 2 * self.votes_for <= self.replicates:
            raise ValueError(f"{self.action!r} needs a strict majority of the replicates (2 of 3)")
        return self


# ---------------------------------------------------------------------------------------------
# Trade state machine (SW-2; pure data - ledger wiring in SW-2b)
# ---------------------------------------------------------------------------------------------

TradeState = Literal[
    "proposed", "entry_executing",
    "open", "open_tp_missing", "partial", "entry_unknown", "missed",
    "exit_pending",
    "closed_stop", "closed_target", "closed_time", "closed_exit", "closed_halt",
    "closed_external", "closed_unclassified",
]
TRADE_STATES: tuple[str, ...] = TradeState.__args__  # type: ignore[attr-defined]

# Positions exist at the broker (the stop is in force).
OPEN_STATES = frozenset({"open", "open_tp_missing", "partial", "exit_pending"})
CLOSED_STATES = frozenset({"closed_stop", "closed_target", "closed_time", "closed_exit",
                           "closed_halt", "closed_external", "closed_unclassified"})
TERMINAL_STATES = CLOSED_STATES | {"missed"}        # immutable once reached
ACTIVE_STATES = frozenset(TRADE_STATES) - TERMINAL_STATES   # count toward S2/S3 capacity
# A swing trade in one of these halts NEW SWING ENTRIES only (the `swing:` blocker scope), never
# the core book.
SWING_BLOCKING_STATES = frozenset({"entry_unknown", "open_tp_missing"})

# Broker-side closes (SL/TP hit, manual, liquidation) are classified from the broker's
# closed-position record; missing data -> closed_unclassified, never a guessed stop hit.
_BROKER_CLOSES = frozenset({"closed_stop", "closed_target", "closed_external",
                            "closed_unclassified"})
# Closes of our own approved exit legs.
_EXIT_CLOSES = frozenset({"closed_time", "closed_exit", "closed_halt"})

TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"entry_executing", "missed"}),
    "entry_executing": frozenset({"open", "open_tp_missing", "partial", "entry_unknown", "missed"}),
    # resolved later by `resume` / the waiting-for-market path, from leg state
    "entry_unknown": frozenset({"open", "open_tp_missing", "partial", "missed",
                                "closed_external", "closed_unclassified"}),
    "open": frozenset({"open_tp_missing", "exit_pending"}) | _BROKER_CLOSES,
    "partial": frozenset({"open_tp_missing", "exit_pending"}) | _BROKER_CLOSES,
    "open_tp_missing": frozenset({"open", "partial", "exit_pending"}) | _BROKER_CLOSES,
    "exit_pending": _EXIT_CLOSES | _BROKER_CLOSES,
    **{state: frozenset() for state in TERMINAL_STATES},
}


class IllegalTransition(ValueError):
    """A swing trade state change the table does not allow (closed trades are immutable)."""


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, frozenset())


def check_transition(src: str, dst: str) -> str:
    """Return ``dst`` when ``src -> dst`` is legal, else raise :class:`IllegalTransition`."""
    if src not in TRANSITIONS or dst not in TRANSITIONS:
        raise IllegalTransition(f"unknown swing trade state: {src!r} -> {dst!r}")
    if src in TERMINAL_STATES:
        raise IllegalTransition(f"swing trade is terminal ({src}); it cannot become {dst}")
    if not can_transition(src, dst):
        raise IllegalTransition(f"illegal swing trade transition {src} -> {dst}")
    return dst
