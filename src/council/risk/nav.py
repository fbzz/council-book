"""NAV index and the LIFETIME peak that the kill switch measures from.

Rules:
- NAV is the Agent Portfolio's equity as read from the broker (no cash-flow machinery in v1).
- nav_index = equity / first_equity x 100 (the public, percent-only series).
- The peak is the LIFETIME peak: it only ever rises, and nothing (not even a kill-switch resume)
  resets it. A peak reset is a tagged policy change, never a runtime action.
- An out-of-order (older) read still counts toward the peak but never replaces the latest value.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from council.models.common import Frozen


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime; council code uses aware UTC datetimes only")
    return ts


class NavState(Frozen):
    first_equity: float = Field(gt=0)
    peak: float = Field(gt=0)
    last: float
    updated_at: datetime

    @property
    def nav_index(self) -> float:
        return self.last / self.first_equity * 100.0

    @property
    def peak_index(self) -> float:
        return self.peak / self.first_equity * 100.0

    @property
    def drawdown(self) -> float:
        """Fractional distance below the lifetime peak (0 at the peak, 0.25 at -25%)."""
        return max(0.0, 1.0 - self.last / self.peak)


def start_nav(equity: float, now: datetime) -> NavState:
    """First observation: index 100, peak = equity."""
    if equity <= 0:
        raise ValueError("NAV must start from positive equity")
    return NavState(first_equity=equity, peak=equity, last=equity, updated_at=_aware(now))


def update_nav(state: NavState | None, equity: float, now: datetime) -> NavState:
    """Fold one equity read into the state (lifetime peak = running max of every read)."""
    _aware(now)
    if state is None:
        return start_nav(equity, now)
    if equity <= 0:
        raise ValueError("equity must be positive")
    peak = max(state.peak, equity)
    if now < state.updated_at:
        return state.model_copy(update={"peak": peak})
    return state.model_copy(update={"peak": peak, "last": equity, "updated_at": now})


def nav_index(state: NavState, equity: float | None = None) -> float:
    """equity / first_equity x 100 (defaults to the last read)."""
    value = state.last if equity is None else equity
    return value / state.first_equity * 100.0
