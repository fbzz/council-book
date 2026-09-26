"""R3 soft kill switch: NORMAL -> WARN -> HALTED -> FLAT, measured from the LIFETIME peak.

Rules:
- WARN when the latest equity <= killswitch.warn_at x peak: no new risk (no |w| increases).
- HALT only when equity <= killswitch.halt_at x peak on at least `confirm_reads` reads that are
  each >= `confirm_gap_s` apart, all inside the trailing run of breaching reads (one read above
  the halt line resets the confirmation). A single unconfirmed breach is WARN.
- HALTED is latched: code proposes an urgent flatten (a human still approves it). HALTED with no
  positions is FLAT. Only `resume(prev, reason)` with a non-empty reason leaves HALTED/FLAT.
- Resuming never resets the peak: the next evaluation measures from the same lifetime peak.
- Real-adjusted (design D19): `real_drag` is the real account's cumulative extra fixed-fee drag
  (`Ledger.real_fee_drag`, private). WARN and HALT use the worse of the virtual drawdown (from the
  virtual lifetime peak) and the drawdown of the real-adjusted equity, virtual equity x
  (1 - real_drag), from ITS OWN lifetime peak `real_peak` (stored by the caller; the virtual peak
  when none is stored yet, which is exact while no drag has accrued). Measuring the adjusted
  equity from the virtual peak instead would turn the cumulative drag into a permanent drawdown
  that eventually trips WARN and HALT at an all-time high. The switch never trips later than the
  virtual rule; it trips earlier when the real account's extra fees deepen a drawdown.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from council.models.common import Frozen
from council.policy import Policy
from council.risk.config import risk_limits
from council.risk.nav import NavState

KillState = Literal["NORMAL", "WARN", "HALTED", "FLAT"]
LATCHED: frozenset[str] = frozenset({"HALTED", "FLAT"})


class KillDecision(Frozen):
    state: KillState
    reason: str
    peak: float | None = None
    equity: float | None = None
    drawdown: float | None = None
    confirmed_reads: int = 0
    real_peak: float | None = None      # PRIVATE: lifetime peak of the real-adjusted equity (store it)


def allows_increase(state: str) -> bool:
    """Only NORMAL may add risk; WARN is reduce-only; HALTED/FLAT flatten."""
    return state == "NORMAL"


def confirmed_breach_reads(
    reads: Sequence[tuple[datetime, float]], threshold: float, gap_s: float
) -> int:
    """Count breaching reads that confirm each other: walk back from the latest read through the
    trailing run of reads <= threshold, keeping reads at least `gap_s` older than the last kept."""
    ordered = sorted(reads, key=lambda r: r[0])
    count = 0
    last_kept: datetime | None = None
    for ts, equity in reversed(ordered):
        if equity > threshold:
            break
        if last_kept is None or (last_kept - ts).total_seconds() >= gap_s:
            count += 1
            last_kept = ts
    return count


def evaluate(
    *,
    nav: NavState,
    equity_reads: Sequence[tuple[datetime, float]],
    prev_state: str,
    has_positions: bool,
    policy: Policy,
    real_drag: float = 0.0,
    real_peak: float | None = None,
) -> KillDecision:
    """Kill state for this cycle (rules in the module docstring). `real_peak` is the stored
    lifetime peak of the real-adjusted equity (None: not stored yet); the decision's `real_peak`
    is the updated value for the caller to store."""
    cfg = risk_limits(policy).killswitch
    for ts, _ in equity_reads:
        if ts.tzinfo is None:
            raise ValueError("naive datetime in equity reads")
    raw = list(equity_reads) or [(nav.updated_at, nav.last)]
    peak = max([nav.peak, *(eq for _, eq in raw)])
    drag = float(real_drag) if math.isfinite(float(real_drag)) else 0.0
    keep = 1.0 - min(max(drag, 0.0), 0.999999)
    stored = float(real_peak) if real_peak is not None and math.isfinite(float(real_peak)) else 0.0
    # the adjusted series never exceeds the virtual one, so its peak is at most the virtual peak
    start = min(stored, peak) if stored > 0 else peak
    r_peak = max([start, *(eq * keep for _, eq in raw)])
    # per read, the worse of the two ratios to their own lifetime peaks
    ratios = [(ts, min(eq / peak, eq * keep / r_peak)) for ts, eq in raw]
    _, latest_eq = max(raw, key=lambda r: r[0])
    ratio = min(latest_eq / peak, latest_eq * keep / r_peak)
    adjusted = latest_eq * keep / r_peak < latest_eq / peak - 1e-12
    info = {"peak": peak, "equity": latest_eq, "drawdown": max(0.0, 1.0 - ratio), "real_peak": r_peak}
    label = "real-adjusted " if adjusted else ""

    if prev_state in LATCHED:
        state: KillState = "HALTED" if has_positions else "FLAT"
        return KillDecision(state=state, reason="latched until an operator resume", **info)

    confirmed = confirmed_breach_reads(ratios, cfg.halt_at, cfg.confirm_gap_s)
    if confirmed >= cfg.confirm_reads:
        state = "HALTED" if has_positions else "FLAT"
        reason = (f"{label}equity {ratio:.1%} of lifetime peak <= halt {cfg.halt_at:.0%} "
                  f"on {confirmed} reads")
        return KillDecision(state=state, reason=reason, confirmed_reads=confirmed, **info)
    if ratio <= cfg.warn_at:
        pending = " (halt breach awaiting confirmation)" if ratio <= cfg.halt_at else ""
        reason = f"{label}equity {ratio:.1%} of lifetime peak <= warn {cfg.warn_at:.0%}{pending}"
        return KillDecision(state="WARN", reason=reason, confirmed_reads=confirmed, **info)
    return KillDecision(state="NORMAL", reason="within limits", **info)


def resume(prev: str, reason: str) -> KillDecision:
    """Operator resume from HALTED/FLAT. Requires a reason; the lifetime peak is untouched, so the
    next `evaluate` re-halts immediately if equity is still below the halt line."""
    if prev not in LATCHED:
        raise ValueError(f"nothing to resume from state {prev}")
    if not reason or not reason.strip():
        raise ValueError("resume requires a non-empty reason")
    return KillDecision(state="NORMAL", reason=f"resumed by operator: {reason.strip()}")
