"""R3 soft kill switch: NORMAL -> WARN -> HALTED -> FLAT, measured from the LIFETIME peak.

Rules:
- WARN when the latest equity <= killswitch.warn_at x peak: no new risk (no |w| increases).
- HALT only when equity <= killswitch.halt_at x peak on at least `confirm_reads` reads that are
  each >= `confirm_gap_s` apart, all inside the trailing run of breaching reads (one read above
  the halt line resets the confirmation). A single unconfirmed breach is WARN.
- HALTED is latched: code proposes an urgent flatten (a human still approves it). HALTED with no
  positions is FLAT. Only `resume(prev, reason)` with a non-empty reason leaves HALTED/FLAT.
- Resuming never resets the peak: the next evaluation measures from the same lifetime peak.
"""

from __future__ import annotations

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
) -> KillDecision:
    """Kill state for this cycle (rules in the module docstring)."""
    cfg = risk_limits(policy).killswitch
    for ts, _ in equity_reads:
        if ts.tzinfo is None:
            raise ValueError("naive datetime in equity reads")
    reads = list(equity_reads) or [(nav.updated_at, nav.last)]
    latest_ts, latest = max(reads, key=lambda r: r[0])
    peak = max([nav.peak, *(eq for _, eq in reads)])
    drawdown = max(0.0, 1.0 - latest / peak)
    info = {"peak": peak, "equity": latest, "drawdown": drawdown}

    if prev_state in LATCHED:
        state: KillState = "HALTED" if has_positions else "FLAT"
        return KillDecision(state=state, reason="latched until an operator resume", **info)

    halt_line = cfg.halt_at * peak
    confirmed = confirmed_breach_reads(reads, halt_line, cfg.confirm_gap_s)
    if confirmed >= cfg.confirm_reads:
        state = "HALTED" if has_positions else "FLAT"
        reason = (
            f"equity {latest / peak:.1%} of lifetime peak <= halt {cfg.halt_at:.0%} "
            f"on {confirmed} reads"
        )
        return KillDecision(state=state, reason=reason, confirmed_reads=confirmed, **info)
    if latest <= cfg.warn_at * peak:
        pending = " (halt breach awaiting confirmation)" if latest <= halt_line else ""
        reason = f"equity {latest / peak:.1%} of lifetime peak <= warn {cfg.warn_at:.0%}{pending}"
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
