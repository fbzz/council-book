"""Time-weighted return (TWR) index from equity marks. Descriptive only.

Rules:
- index_0 = base (100). index_t = index_{t-1} * (E_t - F_t) / E_{t-1}, where F_t is the net
  external flow (deposit > 0, withdrawal < 0) that arrived after mark t-1 and is included in E_t.
  Flows therefore neither create nor hide returns.
- Marks must be strictly increasing in time; equity must stay positive.
- Drawdown is measured on the index against its running (lifetime) peak, as a fraction <= 0.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Mark:
    at: datetime
    equity: float
    flow: float = 0.0     # external flow since the previous mark, already included in `equity`


def _as_mark(m: Mark | tuple) -> Mark:
    if isinstance(m, Mark):
        return m
    if len(m) == 2:
        return Mark(at=m[0], equity=float(m[1]))
    return Mark(at=m[0], equity=float(m[1]), flow=float(m[2]))


def nav_index(marks: Sequence[Mark | tuple], base: float = 100.0) -> list[tuple[datetime, float]]:
    """Base-`base` TWR index at each mark."""
    items = [_as_mark(m) for m in marks]
    if not items:
        return []
    out: list[tuple[datetime, float]] = []
    prev: Mark | None = None
    level = base
    for mark in items:
        if mark.equity <= 0:
            raise ValueError("equity marks must be positive")
        if prev is not None:
            if mark.at <= prev.at:
                raise ValueError("marks must be strictly increasing in time")
            level *= (mark.equity - mark.flow) / prev.equity
        out.append((mark.at, level))
        prev = mark
    return out


def drawdown(index_values: Sequence[float]) -> list[float]:
    """Fractional drawdown from the running peak (0 at a new high, -0.25 = 25% below the peak)."""
    peak = float("-inf")
    out = []
    for v in index_values:
        peak = max(peak, v)
        out.append(v / peak - 1.0 if peak > 0 else 0.0)
    return out


def period_returns(index_values: Sequence[float]) -> list[float]:
    return [b / a - 1.0 for a, b in zip(index_values, index_values[1:], strict=False)]


def peak_fraction(index_values: Sequence[float]) -> float:
    """Current index as a fraction of its lifetime peak (1.0 at a high). Kill-switch reporting."""
    if not index_values:
        return 1.0
    return index_values[-1] / max(index_values)
