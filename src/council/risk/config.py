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


INITIAL_BUILD_EXEMPTABLE: frozenset[str] = frozenset({"R13", "R14", "R15"})


class InitialBuildConfig(Frozen):
    """The initial funding allowance: on the ONE build cycle of an empty book (a paper book's first
    build, a live account's funding day) the listed churn/cost rules do not apply. Only R13 (churn),
    R14 (cycle and 30-day cost; the carry budget still applies) and R15 (net-of-cost gate) may be
    listed (`invariants.check_policy`); every other rule (gross, net, margin, R7 reserve, vol, R21 ...)
    applies. Empty = no allowance."""

    exempt: tuple[str, ...] = ()


class EventBlockConfig(Frozen):
    """R16: hours before/after a scheduled macro event during which adds are blocked; for a stock's
    earnings, the hours before/after the report and the half-width, in US trading days, of the
    window around an ESTIMATED report date (`council.risk.churn.event_window`)."""

    macro_before_h: float = Field(ge=0)
    macro_after_h: float = Field(ge=0)
    earnings_before_h: float = Field(ge=0)
    earnings_after_h: float = Field(ge=0)
    earnings_estimate_window_days: int = Field(ge=0, le=30)


class FreshnessConfig(Frozen):
    """R18: data and quote freshness, and the frozen share of reference weight tolerated."""

    daily_bar_max_h: float = Field(gt=0)
    four_hour_bar_max_h: float = Field(gt=0)
    quote_max_s: float = Field(gt=0)
    frozen_reference_share_max: float = Field(ge=0, le=1)


class ProposalConfig(Frozen):
    """R21: proposal shape. `max_legs` counts risk-increasing and discretionary legs; risk-reducing
    reference-origin legs do not count, and `max_legs_total` bounds every leg of a plan."""

    max_legs: int = Field(ge=1)
    max_legs_total: int = Field(ge=1)

    @property
    def total_cap(self) -> int:
        """The hard total, never below `max_legs` (a policy that raises `max_legs` raises it too)."""
        return max(self.max_legs_total, self.max_legs)


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
    initial_build: InitialBuildConfig = InitialBuildConfig()


class OvernightConfig(Frozen):
    """Annual overnight rates. Carry = rate / 365 per calendar day on exposure; never income."""

    benchmark_rate: float = Field(ge=0)
    long_cfd_markup: float = Field(ge=0)
    short_cfd_markup: float = Field(ge=0)
    index_commodity_fx: float = Field(ge=0)
    crypto_cfd: float = Field(ge=0)


class MinTradeConfig(Frozen):
    """The hard trade-size floor in REAL dollars: `copy_min_multiple` x the copy minimum."""

    copy_min_real_usd: float = Field(gt=0)
    copy_min_multiple: float = Field(ge=1)

    @property
    def floor_real_usd(self) -> float:
        return self.copy_min_real_usd * self.copy_min_multiple


class AssumedAccount(Frozen):
    """Conservative stand-ins when the account figures are unknown: the virtual NAV before the
    broker is connected, the mirror ratio (real / virtual) while `mirror.json` is missing."""

    virtual_nav_usd: float = Field(gt=0)
    mirror_ratio: float = Field(gt=0, le=10)


class CostFloors(BaseModel):
    """costs.yaml: per-side floors (bps), fixed commissions (USD), where the fixed commission is
    charged, the real-dollar trade floor, assumed account figures, slippage buffer, carry rates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    per_side_bps: dict[str, float]
    fixed_commission_usd: dict[str, float]
    fixed_commission_charged_on: tuple[Literal["virtual", "mirror"], ...] = Field(min_length=1)
    min_trade: MinTradeConfig
    assumed: AssumedAccount
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
