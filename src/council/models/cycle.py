"""The private cycle record: what the orchestrator assembles, the ledger stores, the exporter reads.

The per-line decision trail (transparency-v2 §4, `council.publish.trail`) is rebuilt from this
record on demand (`council why`). The fields after `dropped_cards` keep what the record did not
otherwise hold: the structured drops, the bands before the analysts' cards (only lines where they
differ from `bands`), the lines whose medoid fell back to the reference, the lines with new material
evidence, the lines each advocate claim is about, the publishable values of the evidence ids the
trail cites and the lines whose R15 value may be shown. All default empty, so older ledger records
load unchanged; all are PRIVATE (a later package publishes the public subset)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from council.models.cards import EvidenceCard, MacroAnalystOutput, SectorAnalystOutput
from council.models.common import Strict
from council.models.debate import AdvocateCase, BearCase
from council.models.drops import Drop
from council.models.plan import Plan
from council.models.pm import PMDecision
from council.models.reference import ReferenceBook
from council.models.risk import Band, RiskDecision

CycleStatus = Literal[
    "on_time", "late", "missed", "skipped_overlap", "skipped_disk", "skipped_broker",
    "aborted", "halted", "dry_run",
]
DecisionState = Literal[
    "awaiting_publication", "proposed", "approved", "executing", "completed", "completed_partial",
    "rejected", "expired", "superseded", "blocked", "execution_unknown", "reviewed_no_action",
    "waiting_for_market",
]
CallStatus = Literal["ok", "parse_fail", "timeout", "transport", "cached", "skipped", "invalid"]


class RoleCall(Strict):
    role: str
    replicate: int = 0
    seed: int | None = None
    think: bool = False
    prompt_id: str
    prompt_sha: str
    input_hash: str
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    status: CallStatus
    error: str = ""


class PMReplicate(Strict):
    replicate: int
    seed: int
    decision: PMDecision | None
    valid: bool
    audit_violations: list[str] = Field(default_factory=list)
    reverted: list[str] = Field(default_factory=list)
    enforced_levels: dict[str, float] = Field(default_factory=dict)


class Debate(Strict):
    bull_open: AdvocateCase | None = None
    bear: BearCase | None = None
    bull_rebuttal: AdvocateCase | None = None


class CycleRecord(Strict):
    cycle_id: str
    slot: datetime
    started_at: datetime
    finished_at: datetime | None = None
    status: CycleStatus
    late_by_s: int = 0
    mode: str
    input_hash: str = ""
    policy_sha: str
    prompt_manifest_sha: str = ""
    model: str
    model_digest: str = ""
    think: bool = False
    why_we_met: list[str] = Field(default_factory=list)
    kill_state: Literal["NORMAL", "WARN", "HALTED", "FLAT", "RESUMED"] = "NORMAL"
    reference: ReferenceBook | None = None
    cards: list[EvidenceCard] = Field(default_factory=list)
    macro: MacroAnalystOutput | None = None
    sector: list[SectorAnalystOutput] = Field(default_factory=list)
    bands: dict[str, Band] = Field(default_factory=dict)
    debate: Debate = Field(default_factory=Debate)
    pm: list[PMReplicate] = Field(default_factory=list)
    medoid_replicate: int | None = None
    agreement: dict[str, float] = Field(default_factory=dict)   # per line: share of valid replicates agreeing with the medoid's action
    single_agent: list[PMReplicate] = Field(default_factory=list)   # control C10
    single_agent_levels: dict[str, float] = Field(default_factory=dict)
    desk_sha: str = ""
    dropped_cards: list[str] = Field(default_factory=list)
    drops: list[Drop] = Field(default_factory=list)                      # dropped_cards, structured
    code_bands: dict[str, Band] = Field(default_factory=dict)            # before the analysts' cards
    fallback_lines: list[str] = Field(default_factory=list)              # medoid -> reference
    material_lines: list[str] = Field(default_factory=list)              # new material evidence (MC)
    claim_lines: dict[str, list[str]] = Field(default_factory=dict)      # "bear:c1" -> lines
    evidence_values: dict[str, str] = Field(default_factory=dict)        # cited id -> public value
    value_lines: list[str] = Field(default_factory=list)                 # R15 value may be shown
    risk: RiskDecision | None = None
    plan: Plan | None = None
    decision_id: str | None = None
    decision_state: DecisionState | None = None
    decision_reason: str = ""                      # operator's approve/reject reason (published)
    approved_at: datetime | None = None            # published rounded down to its slot
    material_fingerprint: str = ""
    calls: list[RoleCall] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)
