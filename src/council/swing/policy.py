"""Typed, strict view of `policy/swing.yaml` (design swing-book.md rev 2, §3).

Every section forbids unknown keys, numbers are strict (a YAML string or bool is refused), and the
cross-field rules below keep the file internally consistent. The code ceilings the file may never
loosen live in `council.invariants.check_swing_policy`; this module has no imports from the rest of
the package so `council.policy` can load it.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

SWING_FILE = "swing.yaml"

Frac = Annotated[float, Field(gt=0, le=1)]
PosInt = Annotated[int, Field(strict=True, ge=1)]
NonNegInt = Annotated[int, Field(strict=True, ge=0)]
PosFloat = Annotated[float, Field(gt=0)]
SlotHHMM = Annotated[StrictStr, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Setup = Literal["news_continuation", "post_earnings_drift", "second_order",
                "gap_fade", "breakout", "mean_reversion", "event_run_up"]
_MODEL = re.compile(r"^[a-z0-9][a-z0-9._\-]*(:[a-z0-9._\-]+)?$")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Size(_Section):
    target_nav: Frac
    min_nav: Frac
    max_loss_nav_at_stop: Frac
    short_max_loss_nav_at_stop: Frac
    short_si_unknown_mult: Frac

    @model_validator(mode="after")
    def _order(self) -> Size:
        if self.min_nav > self.target_nav:
            raise ValueError("size.min_nav above size.target_nav")
        if self.short_max_loss_nav_at_stop > self.max_loss_nav_at_stop:
            raise ValueError("a short may not risk more than a long at its stop")
        return self


class Capacity(_Section):
    max_open: PosInt
    max_short: NonNegInt
    max_new_7d: PosInt
    max_open_risk_nav: Frac
    open_risk_gap_mult: Annotated[float, Field(ge=1.0)]

    @model_validator(mode="after")
    def _order(self) -> Capacity:
        if self.max_short > self.max_open:
            raise ValueError("capacity.max_short above capacity.max_open")
        return self


class Stops(_Section):
    min_pct: Frac
    max_long_pct: Frac
    max_short_pct: Frac
    min_atr_mult: PosFloat

    @model_validator(mode="after")
    def _order(self) -> Stops:
        if not self.min_pct < min(self.max_long_pct, self.max_short_pct):
            raise ValueError("stops.min_pct must be below both stop maxima")
        return self


class Targets(_Section):
    min_cost_mult: Annotated[float, Field(ge=1.0)]
    min_net_rr: PosFloat
    max_pct: Frac
    max_vol_mult: PosFloat
    max_sigma_daily: Frac


class TimeStop(_Section):
    max_sessions: PosInt
    min_sessions: PosInt
    max_extension_sessions: NonNegInt
    max_total_sessions: PosInt

    @model_validator(mode="after")
    def _order(self) -> TimeStop:
        if self.min_sessions > self.max_sessions:
            raise ValueError("time_stop.min_sessions above max_sessions")
        if self.max_sessions + self.max_extension_sessions > self.max_total_sessions:
            raise ValueError("time_stop: max_sessions + max_extension_sessions exceeds max_total_sessions")
        return self


class Liquidity(_Section):
    min_adv_usd: PosInt
    min_price_usd: PosFloat
    short_min_adv_usd: PosInt

    @model_validator(mode="after")
    def _order(self) -> Liquidity:
        if self.short_min_adv_usd < self.min_adv_usd:
            raise ValueError("liquidity: shorts need at least the long ADV floor")
        return self


class Earnings(_Section):
    no_entry_within_sessions: PosInt
    exit_before_sessions: PosInt
    post_report_wait_h: PosInt
    estimated_window_days: PosInt
    exit_proposal_lead_slots: PosInt
    urgent_after_unapproved_slots: PosInt
    exit_mode: Literal["proposal"]   # Q-S5: an automated pre-earnings write is not expressible


class Correlation(_Section):
    max_open_per_bucket: PosInt
    same_bet_corr: Frac
    max_swing_net_beta: Frac


class Chase(_Section):
    max_move_since_news_sigma: PosFloat
    prior_wait_sigma: PosFloat

    @model_validator(mode="after")
    def _order(self) -> Chase:
        if self.prior_wait_sigma > self.max_move_since_news_sigma:
            raise ValueError("chase.prior_wait_sigma above the hard chase drop")
        return self


class Fees(_Section):
    mode: Literal["report", "enforce"]
    budget_30d_nav_bps: PosFloat
    min_closed_for_ratio: PosInt
    max_fee_to_gross: Frac


class Shorts(_Section):
    min_listing_days: PosInt
    max_si_pct_float: PosFloat
    no_short_after_down_sigma: PosFloat
    no_short_ret20_above: PosFloat
    near_high_pct: Frac
    near_high_vol_ratio: PosFloat
    takeover_lookback_days: PosInt


class Cooloff(_Section):
    after_stop_sessions: NonNegInt
    after_exit_sessions: NonNegInt


class Brake(_Section):
    window_days: PosInt
    pnl_nav: Annotated[float, Field(lt=0, ge=-1)]
    net_of_all_costs: Literal[True]


class EntryGuard(_Section):
    valid_minutes: PosInt
    max_run_stop_frac: Frac
    max_run_pct: Frac
    max_reproposals: NonNegInt


class DrawdownScale(_Section):
    from_peak: Annotated[float, Field(lt=0, gt=-1)]
    size_nav: Frac
    max_open: PosInt


class Slots(_Section):
    summer_utc: Annotated[list[SlotHHMM], Field(min_length=1)]
    winter_utc: Annotated[list[SlotHHMM], Field(min_length=1)]
    min_session_age_min: PosInt
    min_to_close_min: PosInt

    @model_validator(mode="after")
    def _unique(self) -> Slots:
        for name in ("summer_utc", "winter_utc"):
            slots = getattr(self, name)
            if len(set(slots)) != len(slots) or slots != sorted(slots):
                raise ValueError(f"slots.{name} must be sorted and unique")
        return self


class Llm(_Section):
    max_calls_per_slot: PosInt
    deadline_s: PosInt
    max_skeptic_calls: PosInt
    pm_replicates: PosInt
    pm_entry_votes: PosInt
    skeptic_model: StrictStr
    skeptic_model_family: Literal["other", "same"]

    @model_validator(mode="after")
    def _order(self) -> Llm:
        if not _MODEL.match(self.skeptic_model):
            raise ValueError(f"llm.skeptic_model {self.skeptic_model!r} is not a model tag")
        if self.pm_entry_votes > self.pm_replicates or 2 * self.pm_entry_votes <= self.pm_replicates:
            raise ValueError("llm.pm_entry_votes must be a strict majority of pm_replicates")
        # Scout 1 + Skeptic calls + bull 1 + bear 1 + PM replicates must fit the slot cap
        if 3 + self.max_skeptic_calls + self.pm_replicates > self.max_calls_per_slot:
            raise ValueError("llm: the role calls of one slot exceed max_calls_per_slot")
        return self


class Budget(_Section):
    idle: Literal["cash", "spx"]


class PublicRecord(_Section):
    declared_cost_pct_per_leg: Annotated[float, Field(gt=0, le=10)]   # percent of the position
    percent_only: Literal[True]


class Tracking(_Section):
    paper_track_every_idea: Literal[True]
    paper_run_before_live: StrictBool


class SwingPolicy(_Section):
    """`policy/swing.yaml`, parsed. Frozen; `Policy.swing` holds one when the file exists."""

    version: Literal[1]
    size: Size
    capacity: Capacity
    stops: Stops
    targets: Targets
    time_stop: TimeStop
    liquidity: Liquidity
    earnings: Earnings
    correlation: Correlation
    chase: Chase
    fees: Fees
    shorts: Shorts
    cooloff: Cooloff
    brake: Brake
    entry_guard: EntryGuard
    drawdown_scale: DrawdownScale
    slots: Slots
    llm: Llm
    budget: Budget
    public_record: PublicRecord
    tracking: Tracking
    setups_live: Annotated[list[Setup], Field(min_length=1)]
    setups_paper_only: list[Setup]

    @model_validator(mode="after")
    def _cross(self) -> SwingPolicy:
        errors: list[str] = []
        if set(self.setups_live) & set(self.setups_paper_only):
            errors.append("a setup is both live and paper-only")
        if len(set(self.setups_live)) != len(self.setups_live):
            errors.append("setups_live has duplicates")
        if self.drawdown_scale.size_nav > self.size.target_nav:
            errors.append("drawdown_scale.size_nav above size.target_nav")
        if self.drawdown_scale.size_nav < self.size.min_nav:
            errors.append("drawdown_scale.size_nav below size.min_nav (every scaled entry would drop)")
        if self.drawdown_scale.max_open > self.capacity.max_open:
            errors.append("drawdown_scale.max_open above capacity.max_open")
        if self.correlation.max_open_per_bucket > self.capacity.max_open:
            errors.append("correlation.max_open_per_bucket above capacity.max_open")
        if errors:
            raise ValueError("invalid swing policy: " + "; ".join(errors))
        return self
