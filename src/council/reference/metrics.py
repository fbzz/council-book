"""Backtest metrics. All figures are fractions (0.12 = 12%); nothing here is in currency.

Definitions (daily net returns r, NAV index starting at 1.0, 252 trading days a year):
- CAGR = (NAV_end / NAV_start)^(1/years) - 1, years = daily returns / 252.
- Ann vol = std(r) x sqrt(252). Sharpe = mean(r) / std(r) x sqrt(252), rf = 0.
- Max drawdown = min(NAV / running peak - 1). Calmar = CAGR / |max drawdown|.
- Turnover/yr = sum of |traded weight| / years. Cost drag/yr = sum of costs (fraction of NAV) / years.
- Rolling 12-month drawdown share = share of 252-day windows whose within-window max drawdown is
  at or below the threshold (-25%).
- Soft-kill drill: an event is the first close of a drawdown episode at or below warn_at (or
  halt_at) x LIFETIME peak; the episode ends, and the drill re-arms, only at a new lifetime peak.
  The book is NOT stopped; the drill reports what the mechanical book did over the next 60 trading
  days, plus how often NAV crossed the line and how many days it spent below it in the episode.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from council.reference.backtest import SimResult
from council.reference.signals import TREND_STATES

DAYS_PER_YEAR = 252


def _years(n_returns: int) -> float:
    return n_returns / DAYS_PER_YEAR


def max_drawdown(nav: pd.Series) -> float:
    """min(NAV / running peak - 1); 0.0 for a never-falling path."""
    values = nav.to_numpy(dtype=float)
    if values.size == 0:
        return 0.0
    return float(np.min(values / np.maximum.accumulate(values) - 1.0))


def rolling_drawdown_share(nav: pd.Series, window: int = DAYS_PER_YEAR, threshold: float = -0.25) -> float | None:
    """Share of rolling `window`-return windows with a within-window max drawdown <= threshold.

    None when the path is shorter than one window."""
    values = nav.to_numpy(dtype=float)
    if values.size <= window:
        return None
    hits = 0
    total = 0
    for end in range(window, values.size):
        seg = values[end - window : end + 1]
        dd = float(np.min(seg / np.maximum.accumulate(seg) - 1.0))
        hits += dd <= threshold
        total += 1
    return hits / total


def performance(sim: SimResult) -> dict[str, float | None]:
    """Headline metrics of one simulated book (see the module docstring)."""
    r = sim.returns.iloc[1:]
    n = len(r)
    years = _years(n)
    nav = sim.nav
    total = float(nav.iloc[-1] / nav.iloc[0] - 1.0) if len(nav) else 0.0
    cagr = (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 and total > -1.0 else None
    std = float(r.std(ddof=1)) if n > 1 else 0.0
    vol = std * math.sqrt(DAYS_PER_YEAR)
    sharpe = float(r.mean()) / std * math.sqrt(DAYS_PER_YEAR) if std > 0 else None
    mdd = max_drawdown(nav)
    calmar = cagr / abs(mdd) if cagr is not None and mdd < 0 else None
    return {
        "total_return": total,
        "cagr": cagr,
        "ann_vol": vol,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "calmar": calmar,
        "turnover_per_year": float(sim.turnover.sum()) / years if years > 0 else None,
        "cost_drag_per_year": float(sim.costs.sum()) / years if years > 0 else None,
        "mean_gross": float(sim.gross.mean()) if len(sim.gross) else 0.0,
        "dd25_window_share": rolling_drawdown_share(nav),
        "years": years,
    }


def trend_stats(trend: pd.DataFrame) -> dict[str, dict[str, float | None]]:
    """Per line: trend flips per year (changes between consecutive known states, over the years
    with a known state) and the share of days in each state, including `missing`."""
    out: dict[str, dict[str, float | None]] = {}
    for sym in trend.columns:
        col = [v if v in TREND_STATES else None for v in trend[sym].tolist()]
        n = len(col)
        known = [v for v in col if v is not None]
        flips = sum(1 for a, b in zip(col[:-1], col[1:], strict=True) if a is not None and b is not None and a != b)
        known_years = _years(len(known))
        row: dict[str, float | None] = {
            "flips_per_year": flips / known_years if known_years > 0 else None,
        }
        for state in TREND_STATES:
            row[state] = sum(1 for v in col if v == state) / n if n else None
        row["missing"] = sum(1 for v in col if v is None) / n if n else None
        out[str(sym)] = row
    return out


def soft_kill_drill(
    sim: SimResult,
    *,
    warn_at: float = 0.80,
    halt_at: float = 0.75,
    horizon: int = 60,
) -> list[dict[str, Any]]:
    """WARN / HALT episodes against the lifetime peak and the book's next `horizon` trading days.

    Each event: date, kind, drawdown at the event, gross at the event, mean gross, turnover and
    return over the next `horizon` days, the worst further fall from the event NAV, whether NAV
    recovered above the line within the horizon, and, for the whole episode (until a new peak),
    the number of downward crossings of the line and the days spent below it."""
    nav = sim.nav.to_numpy(dtype=float)
    gross = sim.gross.to_numpy(dtype=float)
    turnover = sim.turnover.to_numpy(dtype=float)
    peak = np.maximum.accumulate(nav)
    at_peak = nav >= peak
    events: list[dict[str, Any]] = []
    for kind, line in (("WARN", warn_at), ("HALT", halt_at)):
        below = nav <= line * peak
        i = 0
        while i < len(nav):
            if not below[i]:
                i += 1
                continue
            episode_end = i
            while episode_end + 1 < len(nav) and not at_peak[episode_end + 1]:
                episode_end += 1
            ep = below[i : episode_end + 1]
            crossings = 1 + int(np.sum(ep[1:] & ~ep[:-1]))
            end = min(len(nav) - 1, i + horizon)
            window = slice(i + 1, end + 1)
            observed = end > i
            events.append(
                {
                    "date": sim.nav.index[i].date(),
                    "kind": kind,
                    "drawdown": float(nav[i] / peak[i] - 1.0),
                    "gross_at_event": float(gross[i]),
                    "days_observed": int(end - i),
                    "mean_gross_next": float(gross[window].mean()) if observed else None,
                    "turnover_next": float(turnover[window].sum()) if observed else None,
                    "return_next": float(nav[end] / nav[i] - 1.0),
                    "worst_further": float(np.min(nav[i : end + 1]) / nav[i] - 1.0),
                    "recovered_within": bool(np.any(~below[window])) if observed else False,
                    "crossings_in_episode": crossings,
                    "days_below_in_episode": int(np.sum(ep)),
                }
            )
            i = episode_end + 1
    events.sort(key=lambda e: (e["date"], e["kind"]))
    return events
