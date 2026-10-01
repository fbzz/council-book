"""Allow-listed public documents. Everything in `journal/` is CONSTRUCTED from these models.

Rules (each one is tested):
- `extra="forbid"` and frozen: an unknown field is a bug, never silently published.
- Units live in the field name: `_x` (multiple of NAV), `_pct` (percent), `_bp` (basis points).
  No field holds a dollar amount, a unit count, a price, or a broker/account/order/position id.
- `cycle_id` (e.g. `2026-10-01T1440Z`) is the only key. Timestamps are UTC.
- Evidence is referenced by typed refs. Broker feed items (`N:`) are ids only: licensed text is
  never republished. Public-domain news items (`P:`: SEC filing notices, Federal Reserve Board,
  BLS, BEA, Treasury and EIA releases) are cited by id with their publisher. FRED values appear
  only for series flagged publishable.
- Additive evolution: a field added after documents were first sealed is optional and listed in
  the model's `OMIT_WHEN_DEFAULT`; it is left out of the output while it holds its default, so a
  document sealed before the field existed still re-serialises to exactly its sealed bytes.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Annotated, Any, ClassVar, Literal, get_args

from pydantic import (
    AfterValidator,
    AwareDatetime,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from council.models.common import Frozen

# ------------------------------------------------------------------------------------------ types
CYCLE_ID_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{4}Z$"
# 1-12 characters; "_" only inside a single-stock id (BRK.B is written BRK_B); one-letter tickers
# (V, F, T, C) are valid lines.
LINE_PATTERN = r"^[A-Z0-9](?:[A-Z0-9_]{0,10}[A-Z0-9])?$"
SHA_PATTERN = r"^([0-9a-f]{8,64})?$"          # empty when unknown
HEX64_PATTERN = r"^[0-9a-f]{64}$"

# A decision without a cycle: a watch flatten or an onboarding smoke ticket (m5-readiness M5-N)
DECISION_REF_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{4}Z-(flatten|smoke-S[1-7][a-z]{0,2})$"
FillTolerance = Literal["within_tolerance", "outside_tolerance"]

CycleId = Annotated[str, Field(pattern=CYCLE_ID_PATTERN)]
Line = Annotated[str, Field(pattern=LINE_PATTERN)]
Sha = Annotated[str, Field(pattern=SHA_PATTERN)]
Hex64 = Annotated[str, Field(pattern=HEX64_PATTERN)]
RoleName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
ModelName = Annotated[str, Field(pattern=r"^[A-Za-z0-9:._/-]{0,80}$")]
ShortText = Annotated[str, Field(max_length=160)]
Code = Annotated[str, Field(max_length=120)]   # code-generated reason codes and flags
# Model text: each public cap is the private schema's cap plus ~10% slack, so the cleaner's
# placeholders ("[amount removed]") never force a cut of text the model was allowed to write.
Argument = Annotated[str, Field(max_length=1600)]
ClaimText = Annotated[str, Field(max_length=330)]
RebuttalText = Annotated[str, Field(max_length=270)]
Concession = Annotated[str, Field(max_length=300)]
ReasonText = Annotated[str, Field(max_length=180)]      # PM deviation reason, dismissal "why"
FactText = Annotated[str, Field(max_length=220)]        # decisive fact, card claim, macro driver
CardId = Annotated[str, Field(pattern=r"^K:[a-z_]+:\d+$")]
BROKER_NEWS_ID_PATTERN = r"^N:[0-9a-f]{8}$"    # a broker feed item: licensed, id only
PUBLIC_NEWS_ID_PATTERN = r"^P:[0-9a-f]{8}$"    # a public-domain news item (sha256 of public inputs)
# Every evidence-id form: F/V/C/E/S/K ids, broker feed items (N:<8 hex>), public-domain news items
# (P:<8 hex>) and FRED series (M:...).
EVIDENCE_ID_PATTERN = (
    r"^(?:[FVCESK]:[A-Za-z0-9_.:@#+-]{1,80}|N:[0-9a-f]{8}|P:[0-9a-f]{8}"
    r"|M:[A-Z0-9_]{1,32}(?:\.[a-z0-9_]{1,16})?(?:@\d{4}-\d{2}-\d{2})?)$"
)
EvidenceId = Annotated[str, Field(pattern=EVIDENCE_ID_PATTERN)]
StateWord = Annotated[str, Field(pattern=r"^[a-z][a-z_]{0,15}$")]   # e.g. a trend state: "up"
SleeveName = Annotated[str, Field(pattern=r"^[a-z][a-z_]{0,15}$")]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


def _utc(ts: datetime) -> datetime:
    return ts.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_utc)]

# Exposure as a multiple of NAV; the hard invariant is |gross| <= 2.0, the band is wider so a
# breach is published rather than hidden by a validation error.
X = Annotated[float, Field(ge=-5.0, le=5.0, allow_inf_nan=False)]
Level = Annotated[float, Field(ge=-3.0, le=3.0, allow_inf_nan=False)]
Pct = Annotated[float, Field(ge=-1000.0, le=1000.0, allow_inf_nan=False)]
Bp = Annotated[float, Field(ge=-10000.0, le=10000.0, allow_inf_nan=False)]
IndexValue = Annotated[float, Field(gt=0.0, le=100000.0, allow_inf_nan=False)]

TrendState = Literal["up", "mixed", "down"]
StatusState = Literal["AWAITING_ACCOUNT", "LIVE", "WARN", "HALTED", "FLAT"]
KillState = Literal["NORMAL", "WARN", "HALTED", "FLAT", "RESUMED"]
HumanOutcome = Literal["pending", "approved", "rejected", "expired", "superseded", "no_action", "none"]
CardType = Literal[
    "news_material", "news_context", "filing_material", "filing_context",
    "macro_context", "event_binary", "vol_shock", "sector_rank",
]
CardDirection = Literal["risk_up", "risk_down", "neutral"]
DecisionState = Literal[
    "awaiting_publication", "proposed", "approved", "executing", "completed", "completed_partial",
    "rejected", "expired", "superseded", "blocked", "execution_unknown", "reviewed_no_action",
]
CycleStatus = Literal[
    "on_time", "late", "missed", "skipped_overlap", "skipped_disk", "skipped_broker",
    "aborted", "halted", "dry_run",
]
Basis = Literal[
    "council", "council_partial_reference", "fallback_parse", "fallback_disagreement",
    "council_unavailable", "halted", "code_only",
]
CallStatus = Literal["ok", "parse_fail", "timeout", "transport", "cached", "skipped", "invalid"]
LegKind = Literal["open", "close", "partial_close", "modify_sl"]
LegState = Literal[
    "planned", "submitting", "submitted", "in_flight", "filled", "partially_filled",
    "rejected", "rejected_partial", "unknown", "skipped",
]
Direction = Literal["long", "short"]
Settlement = Literal["real", "cfd", "realFutures", "marginTrade"]
AssetClass = Literal["stock", "etf", "crypto", "index", "commodity", "fx"]
Session = Literal["us", "lse", "fx24x5", "crypto"]     # when the line's preferred vehicle trades
MacroRegime = Literal["risk_on", "neutral", "risk_off"]
# Why a call did not simply succeed, as a fixed code (the private error text is never read out):
# corrected = valid only after the one correction turn; not_json / schema = the reply could not be
# read / failed the schema even after the correction; correction_failed = the correction call
# itself failed; call_budget / council_unavailable = skipped before it ran.
ErrorKind = Literal[
    "corrected", "timeout", "not_json", "schema", "correction_failed", "http_429", "http_4xx",
    "http_5xx", "server_error", "bad_response", "connection", "internal_error", "call_budget",
    "council_unavailable", "skipped", "invalid",
]
FactKind = Literal["market", "vol", "cost", "macro", "event", "news", "filing", "fundamental"]
FactUnit = Literal["pct", "x", "ratio", "bps", "bps_day", "days", "hours", "sigma", "state"]
# The U.S. federal public-domain publishers whose items the record may cite (docs/data-rights.md):
# SEC filing metadata, the Federal Reserve Board, BLS, BEA, Treasury and EIA.
PublicNewsSource = Literal["sec", "fed_board", "bls", "bea", "treasury", "eia"]
PUBLIC_NEWS_SOURCES: frozenset[str] = frozenset(get_args(PublicNewsSource))
FactSource = Literal[
    "tiingo", "binance", "broker", "fred", "clock", "policy", "calendar", "broker_feed", "filing",
    "unknown", "sec", "fed_board", "bls", "bea", "treasury", "eia", "rss",
]
# Why a fact's value is not shown (docs/data-rights.md): a licensed FRED-hosted series, a value
# derived from broker data beyond the coarse states the record may show, or an unknown source.
Withheld = Literal["licensed_series", "not_publishable", "broker_data", "unknown_source"]


class PublicModel(Frozen):
    """Base for every public document: extra fields forbidden, instances immutable.

    `OMIT_WHEN_DEFAULT` names fields added after v1 documents were sealed: they are omitted from
    the output while they hold their default (see the module docstring)."""

    OMIT_WHEN_DEFAULT: ClassVar[frozenset[str]] = frozenset()

    @model_serializer(mode="wrap")
    def _omit_new_defaults(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        omit = type(self).OMIT_WHEN_DEFAULT
        if omit and isinstance(data, dict):
            fields = type(self).model_fields
            for name in omit:
                if name in data and getattr(self, name) == fields[name].get_default(call_default_factory=True):
                    del data[name]
        return data


# ---------------------------------------------------------------------------------- evidence refs
class BrokerFeedRef(PublicModel):
    """A broker news/feed item. Id only: the licensed text is never republished."""

    kind: Literal["broker_feed"] = "broker_feed"
    id: str = Field(pattern=BROKER_NEWS_ID_PATTERN)


class PublicNewsRef(PublicModel):
    """A public-domain news item, cited by its `P:` id (a hash of the publisher and the item's
    public key, so it hides nothing). `source` names the publisher when the item passed the public
    test (a `P:` id, a public publisher and a public-domain licence); it is absent when the cited
    id could not be checked against the cycle's pack."""

    OMIT_WHEN_DEFAULT = frozenset({"source"})

    kind: Literal["public_news"] = "public_news"
    id: str = Field(pattern=PUBLIC_NEWS_ID_PATTERN)
    source: PublicNewsSource | None = None


class FredRef(PublicModel):
    """A FRED macro value. `value` (and its `unit`) is present only when the series is publishable.

    `M:DGS10@2026-09-24` -> series DGS10; `M:DGS10.chg20@2026-09-24` -> series DGS10, measure chg20
    (the 20-observation change)."""

    kind: Literal["fred"] = "fred"
    series: str = Field(pattern=r"^[A-Z0-9_]{1,32}$")
    measure: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,16}$")
    as_of: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    publishable: bool
    value: float | None = Field(default=None, allow_inf_nan=False)
    unit: Literal["pct", "bps", "x", "ratio"] | None = None

    @model_validator(mode="after")
    def _value_only_if_publishable(self) -> FredRef:
        if (self.value is not None or self.unit is not None) and not self.publishable:
            raise ValueError("a non-publishable FRED series may not carry a value")
        return self


class IdRef(PublicModel):
    """Code-generated facts (market, volatility, cost), public events, filings and cards: id only."""

    kind: Literal["market", "vol", "cost", "event", "filing", "card"]
    id: str = Field(pattern=r"^[FVCESK]:[A-Za-z0-9_.:@#+-]{1,80}$")


EvidenceRef = Annotated[BrokerFeedRef | FredRef | IdRef | PublicNewsRef, Field(discriminator="kind")]


# ------------------------------------------------------------------------------------ cycle parts
class PublicReferenceLine(PublicModel):
    """`day_change_pct`: the line's last completed daily return from its signal history, in %
    (derived; None when the history is broker candles, which the record does not republish)."""

    OMIT_WHEN_DEFAULT = frozenset({"day_change_pct"})

    trend: TrendState | None
    level_ref: Level
    weight_ref_x: X
    day_change_pct: Pct | None = None


class PublicCard(PublicModel):
    card_id: str = Field(pattern=r"^K:[a-z_]+:\d+$")
    role: RoleName
    card_type: CardType
    scope: list[Annotated[str, Field(max_length=24)]] = Field(max_length=6)
    direction: CardDirection
    claim: FactText
    horizon_days: int = Field(ge=1, le=60)
    falsifier: str = Field(default="", max_length=180)
    qualifying: bool = False
    corroborated_by: list[str] = Field(default_factory=list, max_length=8)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=8)


class PublicClaim(PublicModel):
    claim_id: str = Field(pattern=r"^c\d+$")
    text: ClaimText
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=6)


class PublicRebuttal(PublicModel):
    claim_id: str = Field(max_length=16)
    verdict: Literal["concede", "refute"]
    text: RebuttalText
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=4)


class PublicAdvocate(PublicModel):
    argument: Argument
    proposal_levels: dict[Line, Level] = Field(default_factory=dict)
    claims: list[PublicClaim] = Field(default_factory=list, max_length=6)
    concessions: list[Concession] = Field(default_factory=list, max_length=4)
    strongest_opposing: EvidenceRef | None = None
    rebuttals: list[PublicRebuttal] = Field(default_factory=list, max_length=6)


class PublicDebate(PublicModel):
    bull: PublicAdvocate | None = None
    bear: PublicAdvocate | None = None
    rebuttal: PublicAdvocate | None = None


class PublicDeviation(PublicModel):
    line: Line
    level: Level
    direction: Literal["cut", "add", "short", "cover", "lever"]
    reason: ReasonText
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=6)


class PublicDecisiveFact(PublicModel):
    text: FactText
    evidence: EvidenceRef | None = None


class PublicDismissal(PublicModel):
    claim_id: str = Field(max_length=16)
    why: ReasonText


class PublicPMReplicate(PublicModel):
    replicate: int = Field(ge=0, le=16)
    valid: bool
    levels: dict[Line, Level] = Field(default_factory=dict)
    deviations: list[PublicDeviation] = Field(default_factory=list, max_length=3)
    decisive_fact: PublicDecisiveFact | None = None
    sided_with: Literal["bull", "bear", "neither", "reference"] | None = None
    dismissed: list[PublicDismissal] = Field(default_factory=list, max_length=6)
    no_change_reason: str = Field(default="", max_length=220)
    violations: list[Code] = Field(default_factory=list)
    reverted: list[Code] = Field(default_factory=list)


class PublicPM(PublicModel):
    """PM replicates, the medoid and the aggregate levels this block produced.

    `levels`: the aggregate (council: the levels handed to the risk engine; single-agent control:
    its own aggregate). `agreement_pct[line]`: share of valid replicates in the medoid's action
    class (up / hold / down) for that line, in percent: the agreement that decided the line."""

    replicates: list[PublicPMReplicate] = Field(default_factory=list, max_length=8)
    medoid: int | None = None
    levels: dict[Line, Level] = Field(default_factory=dict)
    agreement_pct: dict[Line, Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)]] = Field(
        default_factory=dict)
    valid_replicates: int = Field(default=0, ge=0)


class PublicMacroDriver(PublicModel):
    text: FactText
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=6)


class PublicMacro(PublicModel):
    """The macro analyst's output (it runs on the first cycle of each UTC day): the regime, up to
    four short drivers with their evidence, a tilt per sleeve (-1 lean less, 0 neutral, +1 lean
    more; context only, code never acts on it) and the ids of the cards it wrote (in `cards`)."""

    regime: MacroRegime
    drivers: list[PublicMacroDriver] = Field(default_factory=list, max_length=4)
    sleeve_tilts: dict[SleeveName, Literal[-1, 0, 1]] = Field(default_factory=dict)
    cards: list[CardId] = Field(default_factory=list, max_length=8)


class PublicFact(PublicModel):
    """One fact of the pack the agents saw this cycle, looked up by its evidence id.

    `value` is present only where docs/data-rights.md allows it: derived percentages, ratios and
    states from Tiingo / Binance history, the clock and the public cost policy; FRED values only
    for publishable series; broker-candle facts only as coarse states (trend, market open) and
    volatility ratios. Otherwise `withheld` says why. News and filing items are ids only.
    A news row's `source` is its publisher: `broker_feed` for an `N:` item (and never for a `P:`
    item), a public publisher (sec, fed_board, ...) for a `P:` item that passed the public test,
    else `unknown`.
    `as_of`: when the value became available to the council (always at or before the slot).
    Rounding by unit: pct / sigma 0.01, ratio / x 0.001, bps / hours 0.1, bps_day 0.01."""

    OMIT_WHEN_DEFAULT = frozenset({"line", "value", "unit", "as_of", "source", "withheld"})

    id: EvidenceId
    kind: FactKind
    label: str = Field(max_length=80)
    line: Line | None = None
    value: bool | FiniteFloat | StateWord | None = None
    unit: FactUnit | None = None
    as_of: UtcDatetime | None = None
    source: FactSource | None = None
    withheld: Withheld | None = None

    @model_validator(mode="after")
    def _value_rules(self) -> PublicFact:
        if self.value is not None and self.withheld is not None:
            raise ValueError("a withheld fact may not carry a value")
        if self.kind in ("news", "filing") and self.value is not None:
            raise ValueError("news and filing items are ids only")
        if self.id.startswith("N:") and self.source not in (None, "broker_feed", "rss"):
            raise ValueError("a licensed news item is labelled broker_feed or rss")
        if self.id.startswith("P:") and self.source not in (None, "unknown", *PUBLIC_NEWS_SOURCES):
            raise ValueError("a public news item is labelled with its public publisher")
        if isinstance(self.value, float) and abs(self.value) > 1_000_000:
            raise ValueError("fact value out of range")
        return self


class PublicBand(PublicModel):
    trend: TrendState | None
    ref_level: Level
    lo: Level
    hi: Level
    reasons: list[Code] = Field(default_factory=list)
    qualifying_cards: list[str] = Field(default_factory=list)


class PublicCheck(PublicModel):
    rule_id: str = Field(pattern=r"^[A-Z][A-Za-z0-9_]{0,15}$")
    name: str = Field(max_length=80)
    passed: bool
    value: float | str | None = None
    limit: float | str | None = None
    kind: Literal["policy", "execution"] = "policy"


class PublicRisk(PublicModel):
    base_x: dict[Line, X] = Field(default_factory=dict)       # the book the cycle started from
    raw_levels: dict[Line, Level] = Field(default_factory=dict)
    banded_levels: dict[Line, Level] = Field(default_factory=dict)
    raw_x: dict[Line, X] = Field(default_factory=dict)
    banded_x: dict[Line, X] = Field(default_factory=dict)
    proposed_x: dict[Line, X] = Field(default_factory=dict)
    final_x: dict[Line, X] = Field(default_factory=dict)
    checks: list[PublicCheck] = Field(default_factory=list)
    gross_x: X
    net_x: X
    margin_use_pct: Pct
    stop_at_risk_pct: Pct
    carry_bp_day: Bp
    ex_ante_vol_pct: Pct
    hold_reasons: list[Code] = Field(default_factory=list)
    compliance: list[Code] = Field(default_factory=list)


class PublicLeg(PublicModel):
    seq: int = Field(ge=0, le=64)
    kind: LegKind
    line: Line
    direction: Direction
    settlement: Settlement | None = None
    leverage: int = Field(ge=1, le=10)
    weight_before_x: X
    weight_after_x: X
    stop_distance_pct: Pct | None = None
    cost_bp: Bp = 0.0
    risk_increasing: bool


class PublicPlan(PublicModel):
    legs: list[PublicLeg] = Field(default_factory=list, max_length=64)
    cost_bp_total: Bp = 0.0
    carry_bp_day: Bp = 0.0
    gross_before_x: X = 0.0
    gross_after_x: X = 0.0
    net_before_x: X = 0.0
    net_after_x: X = 0.0
    skipped: list[Code] = Field(default_factory=list)


class PublicDecision(PublicModel):
    state: DecisionState | None = None
    human_outcome: HumanOutcome = "none"
    reason: str = Field(default="", max_length=200)
    approved_slot: UtcDatetime | None = None       # approval time rounded DOWN to its slot


class PublicCall(PublicModel):
    """One language-model call. `error_kind` is a fixed code for why it did not simply succeed
    (see `ErrorKind`); it is absent for a clean call. The private error text is never read out."""

    OMIT_WHEN_DEFAULT = frozenset({"error_kind"})

    role: RoleName
    replicate: int = Field(ge=0, le=16)
    status: CallStatus
    latency_ms: int = Field(ge=0, le=3_600_000)
    tokens_in: int = Field(ge=0, le=1_000_000)
    tokens_out: int = Field(ge=0, le=1_000_000)
    prompt_id: str = Field(max_length=64)
    prompt_sha: Sha
    error_kind: ErrorKind | None = None


# ------------------------------------------------------------------------------------ swing book
# Swing-book ids (swing-book.md rev 2, §7.2): a fact-card field `X:<line>:<field>`, a public-domain
# news item `P:`, a broker feed item `N:` (id only), an SEC filing `S:`, a movers-screen row `M:` and
# the core desk's F/V/C/E/K ids. The id names a field, never its value.
SWING_EVIDENCE_ID_PATTERN = (
    r"^(?:X:[A-Z0-9](?:[A-Z0-9_]{0,10}[A-Z0-9])?:[a-z0-9_]{1,48}|N:[0-9a-f]{8}|P:[0-9a-f]{8}"
    r"|S:[A-Za-z0-9_.:@#+-]{1,80}|M:[A-Za-z0-9_.:@-]{1,60}|[FVCEK]:[A-Za-z0-9_.:@#+-]{1,80})$"
)
SwingEvidenceId = Annotated[str, Field(pattern=SWING_EVIDENCE_ID_PATTERN)]
SwingSide = Literal["long", "short"]
SwingStage = Literal["dropped_by_code", "skeptic", "waiting", "debate", "pm", "risk", "planned",
                     "approved", "executed", "missed", "expired"]
SwingTradeState = Literal["open", "open_tp_missing", "exit_pending", "closed_stop", "closed_target",
                          "closed_time", "closed_exit", "closed_halt", "closed_external"]
PaperGroup = Literal["executed", "pm_passed", "skeptic_rejected", "skeptic_wait", "skeptic_wait_debated", "code_dropped",
                     "paper_only", "missed"]
SwingText = Annotated[str, Field(max_length=440)]      # thesis (400) + slack
SwingClaimText = Annotated[str, Field(max_length=135)]  # catalyst claim (120) + slack
SwingFactValue = bool | FiniteFloat | Annotated[str, Field(max_length=40)]


class PublicSwingCatalyst(PublicModel):
    """One catalyst chip. `N:` (a broker feed item): the id only, never its title; an `N:` RSS
    headline (`licensed_news`): the id and its feed label, never its text. `P:` (public
    domain): the title and its publisher's link. `S:`: the filing's form and item codes (SEC
    metadata). `M:`: a movers-screen row, id only."""

    OMIT_WHEN_DEFAULT = frozenset({"title", "link", "source", "form", "items"})

    id: SwingEvidenceId
    kind: Literal["broker_feed", "licensed_news", "public_news", "filing", "screen"]
    title: str | None = Field(default=None, max_length=160)
    link: str | None = Field(default=None, max_length=300, pattern=r"^https://[A-Za-z0-9.-]+\.gov/\S*$")
    source: str | None = Field(default=None, max_length=24)
    form: str | None = Field(default=None, max_length=16)
    items: list[Annotated[str, Field(max_length=8)]] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _feed_is_id_only(self) -> PublicSwingCatalyst:
        if self.kind == "broker_feed" and (self.title or self.link or self.form or self.items):
            raise ValueError("a broker feed catalyst is published by id only")
        if self.kind == "broker_feed" and not self.id.startswith("N:"):
            raise ValueError("a broker feed catalyst has an N: id")
        if self.kind == "licensed_news" and (self.title or self.link or self.form or self.items
                                             or not self.id.startswith("N:")
                                             or not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", self.source or "")):
            raise ValueError("a licensed RSS catalyst is an N: id and its feed label only")
        if self.kind != "licensed_news" and self.id.startswith("N:") and self.source:
            raise ValueError("a broker feed catalyst carries no source")
        return self


class PublicSwingReason(PublicModel):
    text: ReasonText
    evidence: list[SwingEvidenceId] = Field(default_factory=list, max_length=4)


class PublicSkepticVerdict(PublicModel):
    """The Skeptic's verdict on one idea (it judged blind: no thesis, no setup, no levels).
    `verdict` is the code's final word; `said` what the model itself wrote when code overrode it.
    `discounted` is the role's `priced_in` answer (a key naming prices is refused by the leak scan)."""

    OMIT_WHEN_DEFAULT = frozenset({"said", "code_override", "second_order"})

    ref: str = Field(pattern=r"^idea:[0-9]{1,2}$")
    verdict: Literal["pass", "wait", "reject", "failed"]
    said: Literal["pass", "wait", "reject"] | None = None
    discounted: Literal["no", "partly", "mostly", "fully", "unknown"] = "unknown"   # the role's `priced_in`
    news_status: Literal["new", "follow_up", "stale", "restated", "unknown"] = "unknown"
    regime: Literal["supports", "neutral", "against", "unknown"] = "unknown"
    crowding: Literal["low", "medium", "high", "unknown"] = "unknown"
    catalyst_supports_claim: bool | None = None
    claim_supports_side: bool | None = None
    code_override: Code | None = None
    model_family: Annotated[str, Field(pattern=r"^[a-z0-9_.-]{0,32}$")] = ""
    same_family: bool = False
    reasons: list[PublicSwingReason] = Field(default_factory=list, max_length=5)
    what_would_change_my_mind: ReasonText = ""
    second_order: ReasonText | None = None


class PublicSwingVotes(PublicModel):
    """The swing PM's replicates on one idea: `enter` of `replicates` (2 of 3 enters)."""

    enter: int = Field(ge=0, le=3)
    replicates: int = Field(ge=0, le=3)
    failed: int = Field(default=0, ge=0, le=3)


class PublicSwingIdea(PublicModel):
    """One Scout idea and how far it got. Distances are % of the entry (never a price); `facts`
    holds completed-bar fields only, `facts_withheld` says why a field shows no value
    (`broker_data` for the private live layer, `unknown_source` until the Alpaca row is widened).
    `carried_from`: earlier cycles the idea came from (a wait, a missed entry, a re-proposal);
    `text_withheld`: its model text overlapped licensed feed text of one of them (or could not be
    checked) and is not shown."""

    OMIT_WHEN_DEFAULT = frozenset({"drop_code", "verdict", "votes", "carried_from", "text_withheld",
                                   "facts_withheld", "flags"})

    ref: str = Field(pattern=r"^idea:[0-9]{1,2}$")
    ticker: Line
    side: SwingSide
    setup: Annotated[str, Field(pattern=r"^[a-z][a-z_]{0,31}$")]
    live_setup: bool
    catalysts: list[PublicSwingCatalyst] = Field(default_factory=list, max_length=4)
    catalyst_claim: SwingClaimText = ""
    thesis: SwingText = ""
    stop_pct: Pct
    target_pct: Pct
    time_stop_days: int = Field(ge=0, le=30)
    facts: dict[Annotated[str, Field(pattern=r"^[a-z0-9_]{1,48}$")], SwingFactValue] = Field(default_factory=dict)
    facts_withheld: dict[Annotated[str, Field(pattern=r"^[a-z0-9_]{1,48}$")], Withheld] = Field(default_factory=dict)
    stage_reached: SwingStage
    drop_code: Code | None = None
    verdict: PublicSkepticVerdict | None = None
    votes: PublicSwingVotes | None = None
    carried_from: list[CycleId] = Field(default_factory=list, max_length=8)
    text_withheld: bool = False
    flags: list[Code] = Field(default_factory=list, max_length=8)


class PublicSwingClaim(PublicModel):
    claim_id: str = Field(pattern=r"^c\d{1,2}$")
    ref: str = Field(pattern=r"^(idea:[0-9]{1,2}|trade:[A-Za-z0-9_\-]{1,64})$")
    text: ClaimText
    evidence: list[SwingEvidenceId] = Field(default_factory=list, max_length=6)


class PublicSwingRebuttal(PublicModel):
    claim_id: str = Field(pattern=r"^c\d{1,2}$")
    verdict: Literal["concede", "refute"]
    text: RebuttalText
    evidence: list[SwingEvidenceId] = Field(default_factory=list, max_length=4)


class PublicSwingCase(PublicModel):
    argument: Argument = ""
    claims: list[PublicSwingClaim] = Field(default_factory=list, max_length=8)
    rebuttals: list[PublicSwingRebuttal] = Field(default_factory=list, max_length=8)


class PublicSwingTrade(PublicModel):
    """A swing trade, percent-only. `weight_x`: its size at entry as a multiple of NAV;
    `stop_pct` / `target_pct`: planned distances from the entry; results are net of the DECLARED
    cost (1.25% of the position per leg, §7.3), never of the actual one."""

    OMIT_WHEN_DEFAULT = frozenset({"closed_cycle", "exit_kind", "r_declared", "net_declared_pct",
                                   "contribution_declared_bp", "live"})

    trade_id: str = Field(pattern=r"^trade:[A-Za-z0-9_\-]{1,64}$")
    ticker: Line
    side: SwingSide
    weight_x: X
    opened_cycle: CycleId | None = None
    closed_cycle: CycleId | None = None
    days_held: int = Field(ge=0, le=400)
    stop_pct: Pct
    target_pct: Pct
    tp_at_broker: bool
    time_stop_date: date | None = None
    state: SwingTradeState
    exit_kind: Literal["stop", "target", "time", "exit", "halt", "external"] | None = None
    r_declared: Annotated[float, Field(ge=-50.0, le=50.0, allow_inf_nan=False)] | None = None
    net_declared_pct: Pct | None = None
    contribution_declared_bp: Bp | None = None
    live: bool = True


class PublicSkepticHealth(PublicModel):
    """The Skeptic's health line: the weekly canary (a past event whose move was already in the
    price: `pass` misses it), the pass rate over its last 20 verdicts and its rejects in the last 10."""

    canary_last: Literal["caught", "missed", "none_yet"] = "none_yet"
    canaries_caught_total: int = Field(default=0, ge=0)
    canaries_missed_total: int = Field(default=0, ge=0)
    pass_share_20_pct: Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)] | None = None
    rejects_last_10: int = Field(default=0, ge=0, le=10)
    alarm: bool = False


class PublicSwingSection(PublicModel):
    """The swing book's part of one cycle (a swing slot), sealed with the cycle."""

    OMIT_WHEN_DEFAULT = frozenset({"bull", "bear", "trades", "health", "flags"})

    live: bool
    ideas: list[PublicSwingIdea] = Field(default_factory=list, max_length=5)
    bull: PublicSwingCase | None = None
    bear: PublicSwingCase | None = None
    trades: list[PublicSwingTrade] = Field(default_factory=list, max_length=12)
    health: PublicSkepticHealth | None = None
    declared_cost_pct_per_leg: Annotated[float, Field(ge=0.0, le=10.0)] = 1.25
    flags: list[Code] = Field(default_factory=list)


class PublicInterval(PublicModel):
    mean: FiniteFloat | None = None
    low: FiniteFloat | None = None
    high: FiniteFloat | None = None
    n: int = Field(default=0, ge=0)


class PublicFunnelGroup(PublicModel):
    """One idea group with its PAPER outcome at the slot-time reference and the declared cost."""

    group: PaperGroup
    ideas: int = Field(ge=0)
    closed: int = Field(ge=0)
    r_declared: PublicInterval = Field(default_factory=PublicInterval)
    hit_rate_pct: Pct | None = None


class PublicSwingMetrics(PublicModel):
    """The pre-registered forward metrics (§8.2) over closed live trades, net of the declared cost."""

    n_closed: int = Field(default=0, ge=0)
    hit_rate_pct: Pct | None = None
    expectancy_r: PublicInterval = Field(default_factory=PublicInterval)
    payoff: FiniteFloat | None = None
    contribution_declared_bp: Bp = 0.0
    matched_contribution_declared_bp: Bp | None = None
    vs_matched_pct: Pct | None = None
    exit_mix_pct: dict[Literal["stop", "target", "time", "discretionary", "external"], Pct] = Field(
        default_factory=dict)
    avg_days_held: FiniteFloat | None = None
    standard_error_r: FiniteFloat | None = None


class PublicBenchmarkPoint(PublicModel):
    """Base-100 indices: the SQ-8 mechanical rule (PAPER), the matched index (beta x sector ETF over
    the swing trades' windows) and the index held."""

    day: date
    sq8: IndexValue | None = None
    matched_index: IndexValue | None = None
    index_hold: IndexValue | None = None


class PublicSwingBook(PublicModel):
    """The swing page's document (journal/swing/latest.json)."""

    schema_id: Literal["council-book/swing/v1"] = "council-book/swing/v1"
    as_of: UtcDatetime
    live: bool = False
    live_since: date | None = None
    paper_since: date | None = None
    declared_cost_pct_per_leg: Annotated[float, Field(ge=0.0, le=10.0)] = 1.25
    open_trades: list[PublicSwingTrade] = Field(default_factory=list, max_length=12)
    closed_trades: list[PublicSwingTrade] = Field(default_factory=list, max_length=2000)
    metrics: PublicSwingMetrics = Field(default_factory=PublicSwingMetrics)
    funnel: list[PublicFunnelGroup] = Field(default_factory=list, max_length=8)
    benchmarks: list[PublicBenchmarkPoint] = Field(default_factory=list, max_length=4000)
    health: PublicSkepticHealth = Field(default_factory=PublicSkepticHealth)
    flags: list[Code] = Field(default_factory=list)


class PublicCycleV1(PublicModel):
    """One council cycle, as revealed after its decision is final.

    Added after the first cycles were sealed (omitted while empty): `macro`, the macro analyst's
    output; `facts`, the evidence table of the pack the agents saw; `swing`, the swing book's part
    of a swing slot."""

    OMIT_WHEN_DEFAULT = frozenset({"macro", "facts", "swing"})

    schema_id: Literal["council-book/cycle/v1"] = "council-book/cycle/v1"
    cycle_id: CycleId
    slot: UtcDatetime
    mode: Literal["live", "rehearsal"] = "live"     # rehearsal = no broker account, nothing traded
    status: CycleStatus
    late_by_min: int = Field(ge=0, le=100_000)
    input_hash: Sha = ""
    policy_sha: Sha
    prompt_manifest_sha: Sha = ""
    model: ModelName
    model_digest: ModelName = ""
    think: bool = False
    why_we_met: list[Code] = Field(default_factory=list)
    kill_state: KillState = "NORMAL"
    reference: dict[Line, PublicReferenceLine] = Field(default_factory=dict)
    cards: list[PublicCard] = Field(default_factory=list)
    macro: PublicMacro | None = None
    debate: PublicDebate = Field(default_factory=PublicDebate)
    pm: PublicPM = Field(default_factory=PublicPM)
    single_agent: PublicPM | None = None           # control: one agent, no debate
    material_fingerprint: Sha = ""                 # short hash of the material-change fingerprint
    basis: Basis | None = None
    bands: dict[Line, PublicBand] = Field(default_factory=dict)
    risk: PublicRisk | None = None
    plan: PublicPlan | None = None
    decision: PublicDecision = Field(default_factory=PublicDecision)
    calls: list[PublicCall] = Field(default_factory=list)
    flags: list[Code] = Field(default_factory=list)
    facts: list[PublicFact] = Field(default_factory=list, max_length=4000)
    swing: PublicSwingSection | None = None


# ----------------------------------------------------------------------------- commit and reveal
COMMIT_ALGO = "sha256(salt||canonical_json)"


class PublicCommitment(PublicModel):
    """Sealed BEFORE a proposal can be approved; the cycle itself is revealed later."""

    schema_id: Literal["council-book/commitment/v1"] = "council-book/commitment/v1"
    cycle_id: CycleId
    commitment_sha256: Hex64
    algo: Literal["sha256(salt||canonical_json)"] = COMMIT_ALGO
    sealed_at: UtcDatetime
    code_commit: Annotated[str, Field(pattern=r"^([0-9a-f]{7,40})?$")] = ""
    prompt_manifest_sha: Sha = ""
    policy_sha: Sha = ""
    model_digest: ModelName = ""


class PublicReveal(PublicModel):
    """The salt that opens a commitment: sha256(bytes.fromhex(salt) + canonical_json(cycle))."""

    schema_id: Literal["council-book/reveal/v1"] = "council-book/reveal/v1"
    cycle_id: CycleId
    salt: Hex64
    commitment_sha256: Hex64
    algo: Literal["sha256(salt||canonical_json)"] = COMMIT_ALGO


# ------------------------------------------------------------------------------- other documents
class PublicStatus(PublicModel):
    schema_id: Literal["council-book/status/v1"] = "council-book/status/v1"
    state: StatusState = "AWAITING_ACCOUNT"
    last_cycle_id: CycleId | None = None
    last_cycle_at: UtcDatetime | None = None      # slot time of the last published cycle
    kill_state: KillState = "NORMAL"
    note: str = Field(default="", max_length=200)


class PublicBookLine(PublicModel):
    """One line of the book. Descriptive fields (added later, omitted when unknown):
    `name`, `asset_class`, `session` come from the public policy; `settlement` and `leverage` are
    those of the line's largest open position; `pnl_since_open_pct` is the book's own P/L on the
    line's open positions in % of the amount invested (price return since open x leverage,
    weighted by invested amount, in the instrument's currency); `day_change_pct` is the line's
    last completed daily return from its Tiingo / Binance history (never from broker candles)."""

    OMIT_WHEN_DEFAULT = frozenset({
        "name", "asset_class", "session", "settlement", "leverage", "pnl_since_open_pct",
        "day_change_pct",
    })

    direction: Literal["long", "short", "flat"]
    weight_x: X
    level: Level | None = None
    reference_weight_x: X | None = None
    name: str | None = Field(default=None, max_length=48)
    asset_class: AssetClass | None = None
    session: Session | None = None
    settlement: Settlement | None = None
    leverage: int | None = Field(default=None, ge=1, le=10)
    pnl_since_open_pct: Pct | None = None
    day_change_pct: Pct | None = None


class PublicBook(PublicModel):
    """The book after the last REVEALED cycle (lagged by the reveal), by exposure line."""

    schema_id: Literal["council-book/book/v1"] = "council-book/book/v1"
    as_of_cycle_id: CycleId
    lines: dict[Line, PublicBookLine] = Field(default_factory=dict)
    gross_x: X
    net_x: X
    cash_x: X
    kill_state: KillState = "NORMAL"


class PublicOpsRow(PublicModel):
    """One row per cycle, upserted by cycle_id. The cycle document is sealed BEFORE the human
    decision, so this row (and the execution file) carries the FINAL decision outcome."""

    cycle_id: CycleId
    slot: UtcDatetime
    status: CycleStatus
    late_by_min: int = Field(ge=0, le=100_000)
    duration_s: int | None = Field(default=None, ge=0, le=86_400)
    calls: int = Field(ge=0)
    calls_ok: int = Field(ge=0)
    parse_fail: int = Field(ge=0)
    timeouts: int = Field(ge=0)
    basis: Basis | None = None
    legs: int = Field(ge=0)
    decision_state: DecisionState | None = None
    human_outcome: HumanOutcome = "none"
    decision_reason: str = Field(default="", max_length=200)
    approved_slot: UtcDatetime | None = None       # approval time rounded DOWN to its slot
    model: ModelName = ""
    model_digest: ModelName = ""
    flags: list[Code] = Field(default_factory=list)


class PublicPerformancePoint(PublicModel):
    """Base-100 indices: C0 as executed, C1 as proposed, C2 reference, C2x exposure-matched
    reference, C3 hold, C4 buy-and-hold SPY / BTC. Descriptive only."""

    as_of: date
    cycle_id: CycleId | None = None
    c0: IndexValue | None = None
    c1: IndexValue | None = None
    c2: IndexValue | None = None
    c2x: IndexValue | None = None
    c3: IndexValue | None = None
    c4_spy: IndexValue | None = None
    c4_btc: IndexValue | None = None
    drawdown_pct: Pct | None = None
    segment: str = Field(default="", max_length=40)


class PublicFill(PublicModel):
    """One executed leg. Weights are signed changes of the LINE's weight (x NAV at approval):
    `weight_target_x` is the approved change, `weight_filled_x` the measured one (opens only:
    filled size at the fill price; a close is confirmed by the portfolio, not measured).
    `slippage_bp`: fill vs planned price, positive = adverse. `cost_bp`: the leg's estimated cost
    in bps of NAV. Fields are None when the private report cannot support them."""

    seq: int = Field(ge=0, le=64)
    kind: LegKind
    line: Line
    direction: Direction | None = None
    settlement: Settlement | None = None
    leverage: int | None = Field(default=None, ge=1, le=10)
    state: LegState
    weight_target_x: X | None = None
    weight_filled_x: X | None = None
    exposure_error_pct: Pct | None = None
    slippage_bp: Bp | None = None
    cost_bp: Bp | None = None
    # M5-N: a measured fill within the planned tolerance of its target publishes as the target
    # (whole-unit rounding would otherwise bound the NAV); outside it, the exact value
    fill: FillTolerance | None = None

    OMIT_WHEN_DEFAULT: ClassVar[frozenset[str]] = frozenset({"fill"})


class PublicExecution(PublicModel):
    """The FINAL outcome of an executed decision (the cycle itself was sealed before approval)."""

    schema_id: Literal["council-book/execution/v1"] = "council-book/execution/v1"
    cycle_id: CycleId | None = None
    # M5-N (G14/G34): a decision without a cycle (a watch flatten, a smoke ticket) is keyed by its
    # own id, which never matches the cycle-id pattern
    decision_ref: Annotated[str, Field(pattern=DECISION_REF_PATTERN)] | None = None
    decision_state: DecisionState
    approved_slot: UtcDatetime | None = None
    completed_slot: UtcDatetime | None = None
    fills: list[PublicFill] = Field(default_factory=list, max_length=64)
    achieved_x: dict[Line, X] = Field(default_factory=dict)   # line weights after reconcile
    achieved_drift_x: X | None = None
    cost_bp_total: Bp | None = None
    flags: list[Code] = Field(default_factory=list)

    OMIT_WHEN_DEFAULT: ClassVar[frozenset[str]] = frozenset({"cycle_id", "decision_ref"})

    @model_validator(mode="after")
    def _one_key(self) -> PublicExecution:
        if (self.cycle_id is None) == (self.decision_ref is None):
            raise ValueError("an execution names exactly one of cycle_id and decision_ref")
        return self

    @property
    def key(self) -> str:
        """The id the execution file is named after."""
        return self.cycle_id or self.decision_ref or ""


class PublicIncident(PublicModel):
    incident_id: str = Field(pattern=r"^INC-\d{4}$")
    opened_slot: UtcDatetime
    severity: Literal["low", "medium", "high"]
    status: Literal["open", "resolved"] = "open"
    title: str = Field(max_length=120)
    summary: str = Field(max_length=1000)
    cycles: list[CycleId] = Field(default_factory=list)
    withheld: bool = False


PUBLIC_MODELS: tuple[type[PublicModel], ...] = (
    PublicCycleV1, PublicCommitment, PublicReveal, PublicStatus, PublicBook, PublicOpsRow,
    PublicPerformancePoint, PublicExecution, PublicIncident, PublicSwingBook,
)
