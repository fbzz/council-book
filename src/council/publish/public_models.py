"""Allow-listed public documents. Everything in `journal/` is CONSTRUCTED from these models.

Rules (each one is tested):
- `extra="forbid"` and frozen: an unknown field is a bug, never silently published.
- Units live in the field name: `_x` (multiple of NAV), `_pct` (percent), `_bp` (basis points).
  No field holds a dollar amount, a unit count, a price, or a broker/account/order/position id.
- `cycle_id` (e.g. `2026-10-01T1440Z`) is the only key. Timestamps are UTC.
- Evidence is referenced by typed refs. Broker feed items are ids only: licensed text is never
  republished. FRED values appear only for series flagged publishable.
- Additive evolution: a field added after documents were first sealed is optional and listed in
  the model's `OMIT_WHEN_DEFAULT`; it is left out of the output while it holds its default, so a
  document sealed before the field existed still re-serialises to exactly its sealed bytes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, ClassVar, Literal

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
# Every evidence-id form: F/V/C/E/S/K ids, broker feed items (N:<8 hex>) and FRED series (M:...).
EVIDENCE_ID_PATTERN = (
    r"^(?:[FVCESK]:[A-Za-z0-9_.:@#+-]{1,80}|N:[0-9a-f]{8}"
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
FactSource = Literal[
    "tiingo", "binance", "broker", "fred", "clock", "policy", "calendar", "broker_feed", "filing",
    "unknown",
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
    id: str = Field(pattern=r"^N:[0-9a-f]{8}$")


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


EvidenceRef = Annotated[BrokerFeedRef | FredRef | IdRef, Field(discriminator="kind")]


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


class PublicCycleV1(PublicModel):
    """One council cycle, as revealed after its decision is final.

    Added after the first cycles were sealed (omitted while empty): `macro`, the macro analyst's
    output; `facts`, the evidence table of the pack the agents saw."""

    OMIT_WHEN_DEFAULT = frozenset({"macro", "facts"})

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


class PublicExecution(PublicModel):
    """The FINAL outcome of an executed decision (the cycle itself was sealed before approval)."""

    schema_id: Literal["council-book/execution/v1"] = "council-book/execution/v1"
    cycle_id: CycleId
    decision_state: DecisionState
    approved_slot: UtcDatetime | None = None
    completed_slot: UtcDatetime | None = None
    fills: list[PublicFill] = Field(default_factory=list, max_length=64)
    achieved_x: dict[Line, X] = Field(default_factory=dict)   # line weights after reconcile
    achieved_drift_x: X | None = None
    cost_bp_total: Bp | None = None
    flags: list[Code] = Field(default_factory=list)


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
    PublicPerformancePoint, PublicExecution, PublicIncident,
)
