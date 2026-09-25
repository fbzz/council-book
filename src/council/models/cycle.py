"""The private cycle record: what the orchestrator assembles, the ledger stores, the exporter reads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from council.models.cards import EvidenceCard, MacroAnalystOutput, SectorAnalystOutput
from council.models.common import Strict
from council.models.debate import AdvocateCase, BearCase
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
    kill_state: str = "NORMAL"
    reference: ReferenceBook | None = None
    cards: list[EvidenceCard] = Field(default_factory=list)
    macro: MacroAnalystOutput | None = None
    sector: list[SectorAnalystOutput] = Field(default_factory=list)
    bands: dict[str, Band] = Field(default_factory=dict)
    debate: Debate = Field(default_factory=Debate)
    pm: list[PMReplicate] = Field(default_factory=list)
    medoid_replicate: int | None = None
    agreement: float | None = None
    single_agent: list[PMReplicate] = Field(default_factory=list)   # control C10
    single_agent_levels: dict[str, float] = Field(default_factory=dict)
    desk_sha: str = ""
    dropped_cards: list[str] = Field(default_factory=list)
    risk: RiskDecision | None = None
    plan: Plan | None = None
    decision_id: str | None = None
    decision_state: DecisionState | None = None
    calls: list[RoleCall] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)
