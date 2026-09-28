"""Round-trip cost of one swing trade, in percent of the position (design swing-book.md rev 2, §3.3).

PRIVATE: `cost_rt_pct` is never prompted and never published (it encodes the NAV). The desk and the
public record see only the label from `econ_label` ("econ: ok" / "econ: target too small").

    cost_rt_pct = max(2 x fee / (size x real_nav), fee_ref_pct)          # $1 on open and on close
                + 2 x fee / (size x virtual_nav)   [while the fee is charged on the virtual book]
                + spread_open + spread_close                              # per-side bps
                + carry (shorts: overnight x nights + weekend multiplier x weekends, to the time stop)

- `fee_ref_pct` is the fee term at 0.75 x the FUNDED real NAV (private,
  `state_dir/account/swing.json`), so the fee term is NAV-invariant at every NAV where an entry can
  be decided (WARN blocks new entries below 0.8 x the lifetime peak >= funded): the same pass/fail
  pattern at $1.5k, $2k and $20k (D18). No account file -> no cost (the caller drops the idea).
- Spread: the broker cost what-if per side when given, never below the `costs.yaml` floor
  (`stock_real`, 10 bps a side); the slippage buffer is not added (the entry guard bounds the run).
- Carry: `council.risk.costs.carry_bps_day` for a 1x stock CFD short (the what-if wins when higher;
  always priced as a debit until the smoke ticket proves the sign). Longs are real shares: 0.
  Nights are counted on the US exchange calendar from the entry session to the time stop; a night
  that spans a weekend or holiday is charged `weekend_multiplier` nights, as the broker does.
- `declared_rt_pct`: the public record's declared cost (policy `public_record`, 1.25 % a leg).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from council import paths
from council.clock import session_hours

Side = Literal["long", "short"]
ACCOUNT_FILE = Path("account") / "swing.json"
FEE_REF_FRACTION = 0.75
STOCK_SPREAD_FLOOR_KEY = "stock_real"
MAX_CARRY_SCAN_DAYS = 80           # calendar days scanned for the time stop (20 sessions fit easily)


class CostUnavailable(ValueError):
    """A required input (funded NAV, a positive size or NAV) is missing: no cost, no entry."""


@dataclass(frozen=True, repr=False)
class SwingAccount:
    """The private funding figure. Never printed."""

    funded_real_nav_usd: float = field(repr=False)

    def __repr__(self) -> str:
        return "SwingAccount(<private>)"


def load_account(state_dir: Path | None = None) -> SwingAccount | None:
    """`state_dir/account/swing.json` {"funded_real_nav_usd": <positive number>}, or None when it is
    missing, unreadable or not positive (fail closed: no cost, no swing entry)."""
    path = (state_dir if state_dir is not None else paths.state_dir()) / ACCOUNT_FILE
    try:
        raw = json.loads(path.read_text())
        value = raw.get("funded_real_nav_usd") if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        return None
    return SwingAccount(float(value))


@dataclass(frozen=True)
class CostConfig:
    """The public cost floors (`policy/costs.yaml`)."""

    fee_usd: float
    charged_on: tuple[str, ...]
    spread_floor_bps: float
    short_carry_bps_day_floor: float
    weekend_multiplier: float

    @classmethod
    def from_policy(cls, policy: Any) -> CostConfig:
        from council.risk.config import cost_floors
        from council.risk.costs import carry_bps_day

        cfg = cost_floors(policy)
        return cls(
            fee_usd=max(float(cfg.fixed_commission_usd.get("real", 0.0)), 0.0),
            charged_on=tuple(dict.fromkeys(cfg.fixed_commission_charged_on)),
            spread_floor_bps=float(cfg.per_side_bps[STOCK_SPREAD_FLOOR_KEY]),
            short_carry_bps_day_floor=carry_bps_day("short", "cfd", 1, "stock", None, policy),
            weekend_multiplier=float(cfg.weekend_multiplier),
        )


@dataclass(frozen=True)
class RoundTrip:
    """Components in percent of the position. PRIVATE (repr keeps only the total)."""

    fee_pct: float = field(repr=False)
    virtual_fee_pct: float = field(repr=False)
    spread_pct: float = field(repr=False)
    carry_pct: float = field(repr=False)
    fee_ref_used: bool = field(repr=False)

    @property
    def total_pct(self) -> float:
        return self.fee_pct + self.virtual_fee_pct + self.spread_pct + self.carry_pct


def carry_nights(entry: date, sessions: int, *, weekend_multiplier: float) -> float:
    """Charged nights from the entry session to the close of the `sessions`-th session after it,
    on the US calendar: each overnight counts 1, one that skips a non-session day counts
    `weekend_multiplier`."""
    if sessions <= 0:
        return 0.0
    days: list[date] = []
    day = entry
    for _ in range(MAX_CARRY_SCAN_DAYS):
        day = day + timedelta(days=1)
        if session_hours("us", day, closing=True) is not None:
            days.append(day)
            if len(days) == sessions:
                break
    else:
        raise CostUnavailable("time stop beyond the carry scan window")
    total, prev = 0.0, entry
    for d in days:
        total += weekend_multiplier if (d - prev).days > 1 else 1.0
        prev = d
    return total


def round_trip(
    side: Side,
    *,
    size_nav: float,
    real_nav_usd: float,
    virtual_nav_usd: float,
    account: SwingAccount | None,
    cfg: CostConfig,
    entry_day: date,
    time_stop_sessions: int,
    spread_bps_side: float | None = None,
    carry_bps_day: float | None = None,
) -> RoundTrip:
    """The round-trip cost of one swing trade (module rules). Raises CostUnavailable."""
    if account is None:
        raise CostUnavailable("no funded NAV (state_dir/account/swing.json)")
    for name, value in (("size_nav", size_nav), ("real_nav_usd", real_nav_usd),
                        ("virtual_nav_usd", virtual_nav_usd)):
        if not math.isfinite(value) or value <= 0:
            raise CostUnavailable(f"{name} must be positive")
    fee = cfg.fee_usd
    actual = 2.0 * fee / (size_nav * real_nav_usd) * 100.0
    ref = 2.0 * fee / (size_nav * FEE_REF_FRACTION * account.funded_real_nav_usd) * 100.0
    virtual = 2.0 * fee / (size_nav * virtual_nav_usd) * 100.0 if "virtual" in cfg.charged_on else 0.0
    spread_side = max(float(spread_bps_side or 0.0), cfg.spread_floor_bps)
    spread = 2.0 * spread_side / 100.0
    carry = 0.0
    if side == "short":
        rate = max(float(carry_bps_day or 0.0), cfg.short_carry_bps_day_floor)
        nights = carry_nights(entry_day, time_stop_sessions, weekend_multiplier=cfg.weekend_multiplier)
        carry = rate * nights / 100.0
    return RoundTrip(fee_pct=max(actual, ref), virtual_fee_pct=virtual, spread_pct=spread,
                     carry_pct=carry, fee_ref_used=ref >= actual)


def econ_ok(cost_pct: float, stop_pct: float, target_pct: float, *, min_cost_mult: float,
            min_net_rr: float) -> bool:
    """S6's cost terms (all in percent): target >= min_cost_mult x cost and
    (target - cost) / (stop + cost) >= min_net_rr."""
    if target_pct < min_cost_mult * cost_pct - 1e-12:
        return False
    return (target_pct - cost_pct) >= min_net_rr * (stop_pct + cost_pct) - 1e-12


def econ_label(ok: bool) -> str:
    """The only cost text the desk sees (never a number)."""
    return "econ: ok" if ok else "econ: target too small"


def declared_rt_pct(declared_cost_pct_per_leg: float) -> float:
    """The public record's declared round trip (two legs), percent of the position."""
    return 2.0 * float(declared_cost_pct_per_leg)
