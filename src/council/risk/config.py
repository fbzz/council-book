"""Typed, validated views of `policy/risk.yaml` and `policy/costs.yaml`.

`council.policy.Policy` keeps these files as plain dicts on purpose: the module that owns them
validates the parts it uses. Every section the risk engine reads is parsed here with
`extra="forbid"`, so a renamed or misspelt key fails loudly instead of silently disabling a rule.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from council.models.common import Frozen
from council.policy import Policy


class GrossLimits(Frozen):
    """R1: gross exposure sum(|w|) at proposal, hard at approval, and the de-risk target."""

    proposal_max: float = Field(gt=0)
    hard_max: float = Field(gt=0)
    watch_derisk_to: float = Field(gt=0)


class NetLimits(Frozen):
    """R2: net exposure bounds and the short-gross cap."""

    min: float
    max: float
    short_gross_max: float = Field(ge=0)


class KillSwitchConfig(Frozen):
    """R3: soft kill. WARN at warn_at x peak, HALT at halt_at x peak (confirmed)."""

    warn_at: float = Field(gt=0, lt=1)
    halt_at: float = Field(gt=0, lt=1)
    confirm_reads: int = Field(ge=1)
    confirm_gap_s: float = Field(ge=0)
    peak: Literal["lifetime"]


class CatastropheStopConfig(Frozen):
    """R4: every open carries a stop at d = clamp(max(floor, sigma_mult*sigma_d*sqrt(h)), cap)."""

    sigma_mult: float = Field(gt=0)
    horizon_days: int = Field(ge=1)
    floors: dict[str, float]
    cap: float = Field(gt=0, lt=1)
    margin_pct_buffer_pp: float = Field(ge=0)
    stop_at_risk_reporting_only: bool = True


class ClusterCap(Frozen):
    members: list[str]
    max: float = Field(gt=0)


class CapsConfig(Frozen):
    """R5: |w| caps per line and per group (crypto, FX, equity-beta cluster)."""

    line: dict[str, float]
    crypto_total: float = Field(gt=0)
    fx_total: float = Field(gt=0)
    equity_beta_cluster: ClusterCap


class VolBreakerConfig(Frozen):
    """R9: EWMA5/EWMA60 ratios that block increases."""

    instrument_ratio: float = Field(gt=0)
    book_ratio: float = Field(gt=0)
    card_ratio: float = Field(gt=0)


class UpBand(Frozen):
    cut_with_qualifying_card: float = Field(ge=0)
    leverage_extension: float = Field(ge=0)


class LevelBounds(Frozen):
    lo: float
    hi: float


class OverlayBands(Frozen):
    up: tuple[float, float]
    mixed: tuple[float, float]
    down: tuple[float, float]


class AuthorityConfig(Frozen):
    """R10: council authority bands, in units of the line's unit weight."""

    up: UpBand
    mixed: LevelBounds
    down: LevelBounds
    overlay: OverlayBands
    qualifying_cut_cards: list[str]
    max_deviations_per_cycle: int = Field(ge=0)
    card_expiry_returns_to_reference: bool = True


class DeadbandConfig(Frozen):
    """R11: minimum level change (crypto wider) and minimum notional as a share of NAV."""

    level: float = Field(ge=0)
    level_crypto: float = Field(ge=0)
    min_nav_share: float = Field(ge=0)


class MinHoldConfig(Frozen):
    """R12: minimum days between discretionary changes; no sign flip within no_flip_hours."""

    crypto: float = Field(ge=0)
    default: float = Field(ge=0)
    no_flip_hours: float = Field(ge=0)
    toward_reference_exempt: bool = True


class ChurnConfig(Frozen):
    """R13: per-cycle risk increase and trailing discretionary turnover."""

    cycle_increase_max: float = Field(ge=0)
    turnover_7d_max: float = Field(ge=0)
    turnover_30d_max: float = Field(ge=0)


class CostBudgetConfig(Frozen):
    """R14: cost per cycle and per 30 days (bps of NAV); carry (bps of NAV per day)."""

    cycle_max_bps: float = Field(ge=0)
    discretionary_30d_max_bps: float = Field(ge=0)
    carry_proposal_max_bps_day: float = Field(ge=0)
    carry_watch_max_bps_day: float = Field(ge=0)


class CostGateConfig(Frozen):
    """R15: break-even Sharpe thresholds and the hold horizon used to amortise round trips."""

    reference_max_srbe: float = Field(gt=0)
    council_max_srbe: float = Field(gt=0)
    hold_days: dict[str, float]
    # Moves toward the reference ride a trend state that lasts months, so their round trip is
    # amortised over a longer expected hold than a discretionary council deviation.
    reference_hold_days: dict[str, float] | None = None


class EventBlockConfig(Frozen):
    """R16: hours before/after a macro event during which adds are blocked."""

    macro_before_h: float = Field(ge=0)
    macro_after_h: float = Field(ge=0)


class FreshnessConfig(Frozen):
    """R18: data and quote freshness, and the frozen share of reference weight tolerated."""

    daily_bar_max_h: float = Field(gt=0)
    four_hour_bar_max_h: float = Field(gt=0)
    quote_max_s: float = Field(gt=0)
    frozen_reference_share_max: float = Field(ge=0, le=1)


class ProposalConfig(Frozen):
    """R21: proposal shape."""

    max_legs: int = Field(ge=1)


class RiskLimits(BaseModel):
    """The sections of risk.yaml the engine enforces (approval/reconcile belong to execution)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    version: int
    gross: GrossLimits
    net: NetLimits
    killswitch: KillSwitchConfig
    catastrophe_stop: CatastropheStopConfig
    reentry_cooloff_days: dict[str, float]
    caps: CapsConfig
    leverage_caps: dict[str, int]
    margin_use_max: float = Field(gt=0)
    ex_ante_vol_hard: float = Field(gt=0)
    vol_breaker: VolBreakerConfig
    authority: AuthorityConfig
    deadband: DeadbandConfig
    min_hold_days: MinHoldConfig
    churn: ChurnConfig
    cost_budget: CostBudgetConfig
    net_of_cost_gate: CostGateConfig
    event_block: EventBlockConfig
    anti_chase_sigma: float = Field(gt=0)
    freshness: FreshnessConfig
    material_change_required: bool = True
    proposal: ProposalConfig


class OvernightConfig(Frozen):
    """Annual overnight rates. Carry = rate / 365 per calendar day on exposure; never income."""

    benchmark_rate: float = Field(ge=0)
    long_cfd_markup: float = Field(ge=0)
    short_cfd_markup: float = Field(ge=0)
    index_commodity_fx: float = Field(ge=0)
    crypto_cfd: float = Field(ge=0)


class CostFloors(BaseModel):
    """costs.yaml: per-side floors (bps), fixed commissions (USD), slippage buffer, carry rates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    per_side_bps: dict[str, float]
    fixed_commission_usd: dict[str, float]
    slippage_buffer_bps: float = Field(ge=0)
    overnight_annual: OvernightConfig
    weekend_multiplier: float = Field(ge=1)


def risk_limits(policy: Policy) -> RiskLimits:
    """Parse and validate the risk sections of a policy (cheap; not cached because tests build
    variant policies with `model_copy` that keep the same SHA)."""
    return RiskLimits.model_validate(policy.risk)


def cost_floors(policy: Policy) -> CostFloors:
    """Parse and validate costs.yaml."""
    return CostFloors.model_validate(policy.costs)
