"""Churn and timing rules: deadband (R11), minimum hold (R12), churn budgets (R13), event block
(R16), anti-chase (R17) and the re-entry cool-off after a stop hit (R4d).

Rules:
- R11 deadband: a change executes only if |d level| >= deadband.level (crypto: level_crypto) AND
  |d weight| >= max(deadband.min_nav_share, the broker minimum as a NAV share). A close to zero
  skips the size floor (it still needs the level step and the cost gate).
- R12 minimum hold: no discretionary change within min_hold_days of the line's last change
  (crypto 7d, others 3d) and no sign flip within no_flip_hours. Moves toward the reference are
  exempt when min_hold_days.toward_reference_exempt is true.
- R13: the cycle's risk increase sum(|w| increases; a flip counts its full new leg) must stay
  <= churn.cycle_increase_max; trailing discretionary turnover <= turnover_7d_max / _30d_max.
- R16: no ADDS on index/ETF/FX/commodity/crypto lines from macro_before_h before to macro_after_h
  after a scheduled macro event (FOMC/CPI/NFP/PCE). It never forces a sale.
- R17: no new or larger long after a completed daily move above +anti_chase_sigma sigmas, and no
  new or larger short after one below -anti_chase_sigma.
- R4d: after a stop hit, no increase on that line for reentry_cooloff_days (crypto 7, else 3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from council.models.facts import EventItem, MarketState
from council.policy import LineSpec, Policy
from council.risk.config import risk_limits

MACRO_KINDS: frozenset[str] = frozenset({"fomc", "cpi", "nfp", "pce"})
MACRO_SENSITIVE_CLASSES: frozenset[str] = frozenset({"index", "etf", "fx", "commodity", "crypto"})
_EPS = 1e-9


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime; council code uses aware UTC datetimes only")
    return ts


def deadband_level(crypto: bool, policy: Policy) -> float:
    db = risk_limits(policy).deadband
    return db.level_crypto if crypto else db.level


def deadband_ok(
    delta_level: float,
    delta_w: float,
    *,
    to_zero: bool,
    crypto: bool,
    min_share: float,
    policy: Policy,
) -> bool:
    """R11 for one line: level step and (unless closing to zero) notional floor."""
    db = risk_limits(policy).deadband
    if abs(delta_level) < deadband_level(crypto, policy) - _EPS:
        return False
    if to_zero:
        return True
    return abs(delta_w) >= max(db.min_nav_share, min_share) - _EPS


def apply_deadband(
    target_levels: Mapping[str, float],
    current_levels: Mapping[str, float],
    unit_weights: Mapping[str, float],
    nav_min_share: float | Mapping[str, float],
    policy: Policy,
    *,
    crypto_lines: Iterable[str],
) -> tuple[dict[str, float], list[str]]:
    """Hold every line whose change fails R11 at its current level. Returns (levels, held).

    `nav_min_share` is the broker minimum as a share of NAV: one number for every line, or a
    mapping per line (missing lines use 0, i.e. only the policy floor)."""
    crypto = set(crypto_lines)
    out: dict[str, float] = {}
    held: list[str] = []
    for line, target in target_levels.items():
        current = float(current_levels.get(line, 0.0))
        if abs(target - current) <= _EPS:
            out[line] = target
            continue
        unit = float(unit_weights.get(line, 0.0))
        share = (
            float(nav_min_share.get(line, 0.0))
            if isinstance(nav_min_share, Mapping)
            else float(nav_min_share)
        )
        ok = deadband_ok(
            target - current,
            (target - current) * unit,
            to_zero=abs(target) <= _EPS,
            crypto=line in crypto,
            min_share=share,
            policy=policy,
        )
        if ok:
            out[line] = target
        else:
            out[line] = current
            held.append(line)
    return out, sorted(held)


def min_hold_days(asset_class: str, policy: Policy) -> float:
    mh = risk_limits(policy).min_hold_days
    return mh.crypto if asset_class == "crypto" else mh.default


def min_hold_ok(
    line: str,
    last_change_at: datetime | None,
    now: datetime,
    *,
    sign_flip: bool,
    toward_reference: bool,
    asset_class: str,
    policy: Policy,
    reversal: bool = True,
) -> bool:
    """R12 for one line. `reversal=False` (the change continues the last change's direction)
    skips the day count; the 24h no-flip rule always applies to discretionary flips."""
    mh = risk_limits(policy).min_hold_days
    if toward_reference and mh.toward_reference_exempt:
        return True
    if last_change_at is None:
        return True
    age = _aware(now) - _aware(last_change_at)
    if sign_flip and age < timedelta(hours=mh.no_flip_hours):
        return False
    return not (reversal and age < timedelta(days=min_hold_days(asset_class, policy)))


def anti_chase_block(
    state: MarketState | None, increasing_long: bool, increasing_short: bool, policy: Policy
) -> bool:
    """R17: True when this increase chases the last completed daily move."""
    if state is None or state.ret1d_sigma is None:
        return False
    limit = risk_limits(policy).anti_chase_sigma
    if increasing_long and state.ret1d_sigma > limit:
        return True
    return bool(increasing_short and state.ret1d_sigma < -limit)


def event_block(
    line: LineSpec, events: Iterable[EventItem], now: datetime, policy: Policy
) -> bool:
    """R16: True when an add on this line falls inside a macro event window."""
    if line.asset_class not in MACRO_SENSITIVE_CLASSES:
        return False
    cfg = risk_limits(policy).event_block
    now = _aware(now)
    for event in events:
        if event.kind not in MACRO_KINDS:
            continue
        if event.symbols and line.symbol not in event.symbols:
            continue
        start = event.at_utc - timedelta(hours=cfg.macro_before_h)
        end = event.at_utc + timedelta(hours=cfg.macro_after_h)
        if start <= now <= end:
            return True
    return False


def reentry_blocked(
    stop_hit_at: datetime | None, now: datetime, asset_class: str, policy: Policy
) -> bool:
    """R4d: True while the post-stop cool-off is running."""
    if stop_hit_at is None:
        return False
    days = risk_limits(policy).reentry_cooloff_days
    cooloff = days["crypto"] if asset_class == "crypto" else days["default"]
    return _aware(now) - _aware(stop_hit_at) < timedelta(days=cooloff)


def line_increase(before: float, after: float) -> float:
    """Risk added on one line: a flip counts its whole new leg, otherwise |after| - |before|."""
    if before * after < 0:
        return abs(after)
    return max(abs(after) - abs(before), 0.0)


def cycle_increase(before: Mapping[str, float], after: Mapping[str, float]) -> float:
    lines = set(before) | set(after)
    return sum(line_increase(before.get(s, 0.0), after.get(s, 0.0)) for s in lines)


def cycle_increase_ok(increase: float, policy: Policy) -> bool:
    return increase <= risk_limits(policy).churn.cycle_increase_max + _EPS


def turnover_ok(prior: float, this_cycle: float, window: str, policy: Policy) -> bool:
    """R13: trailing discretionary turnover plus this cycle's stays within the window budget."""
    churn = risk_limits(policy).churn
    limit = {"7d": churn.turnover_7d_max, "30d": churn.turnover_30d_max}[window]
    return prior + this_cycle <= limit + _EPS
