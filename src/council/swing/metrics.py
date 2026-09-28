"""Pre-registered forward metrics and the pre-declared review rules (design swing-book.md rev 2,
§8.2, §8.3; SW-6). Pure functions over percent-only trade records; no ledger, no network.

§8.2 metrics: hit rate, expectancy with a bootstrap 90% interval (10,000 resamples), payoff, swing
contribution (bps of NAV, net of the declared cost), vs matched index (side x beta_60d x the FF12
sector ETF, same window, same declared cost), the funnel by paper group, the exit mix.

§8.3 rules, exactly as pre-declared:
- **Pause**: at the first review with >= 20 closed trades, new entries pause if mean `r_declared`
  <= 0 **or** the swing contribution trails its matched index. Either failure is enough.
- **Review due**: at 30 closed trades or 90 calendar days after go-live, whichever first, then every
  further 30 trades.
- **Skeptic test**: once >= 40 ideas have a Skeptic pass or reject and a finished paper outcome, if
  rejected ideas beat passed ones by >= 0.3 R (mean paper `r_declared`) the veto becomes advisory;
  if passed beat rejected by >= 0.3 R that is the Skeptic's evidence of value. `wait` is its own
  group and never enters the test.
- **Setup promotion** (SB16): >= 30 paper ideas, mean > 0 and bootstrap lower bound > -0.1 R.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_LEVEL = 0.90
BOOTSTRAP_SEED = 20260928
PAUSE_MIN_TRADES = 20
REVIEW_TRADES = 30
REVIEW_DAYS = 90
SKEPTIC_MIN_IDEAS = 40
SKEPTIC_MARGIN_R = 0.3
PROMOTION_MIN_IDEAS = 30
PROMOTION_LOWER_R = -0.1
EXIT_KINDS = ("stop", "target", "time", "discretionary", "external")
_TOL = 1e-12


@dataclass(frozen=True)
class ClosedTrade:
    """One closed swing trade, percent-only (fractions of the position; size as share of NAV)."""

    r_declared: float
    net_ret: float                        # net of the declared cost, fraction of the position
    size_nav: float                       # share of NAV at entry
    side: str
    beta: float | None = None             # beta_60d vs the sector ETF line
    sector_etf_ret: float | None = None   # the FF12 sector ETF over the same window
    exit_kind: str = "stop"
    days_held: int = 0


@dataclass(frozen=True)
class Interval:
    mean: float | None
    low: float | None
    high: float | None
    n: int


def bootstrap_mean(values: Sequence[float], *, resamples: int = BOOTSTRAP_RESAMPLES,
                   level: float = BOOTSTRAP_LEVEL, seed: int = BOOTSTRAP_SEED) -> Interval:
    """Mean with a percentile bootstrap interval (seeded: the same inputs give the same interval)."""
    x = np.asarray([float(v) for v in values], dtype=float)
    if x.size == 0:
        return Interval(None, None, None, 0)
    if not np.isfinite(x).all():
        raise ValueError("bootstrap values must be finite")
    rng = np.random.default_rng(seed)
    means = x[rng.integers(0, x.size, size=(resamples, x.size))].mean(axis=1)
    a = (1.0 - level) / 2.0
    return Interval(float(x.mean()), float(np.quantile(means, a)), float(np.quantile(means, 1 - a)), int(x.size))


def hit_rate(rs: Sequence[float]) -> float | None:
    return sum(1 for r in rs if r > 0) / len(rs) if rs else None


def payoff(rs: Sequence[float]) -> float | None:
    """Mean win R / mean |loss R| (None without both a win and a loss)."""
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r < 0]
    if not wins or not losses:
        return None
    return float(np.mean(wins) / np.mean(losses))


def _sign(side: str) -> float:
    return 1.0 if side == "long" else -1.0


def matched_ret(t: ClosedTrade, *, declared_rt: float) -> float | None:
    """side x beta x sector ETF over the trade's window, net of the declared round trip."""
    if t.beta is None or t.sector_etf_ret is None:
        return None
    return _sign(t.side) * t.beta * t.sector_etf_ret - declared_rt


def contribution_bps(trades: Iterable[ClosedTrade]) -> float:
    """Swing contribution: sum of size x net return, in bps of NAV (declared cost)."""
    return float(sum(t.size_nav * t.net_ret for t in trades) * 1e4)


def matched_contribution_bps(trades: Iterable[ClosedTrade], *, declared_rt: float) -> float | None:
    total, any_ = 0.0, False
    for t in trades:
        m = matched_ret(t, declared_rt=declared_rt)
        if m is None:
            return None                       # an unmatched trade: the comparison is not computable
        total += t.size_nav * m
        any_ = True
    return total * 1e4 if any_ else None


def vs_matched(trades: Sequence[ClosedTrade], *, declared_rt: float) -> float | None:
    """Mean over trades of (trade net return - matched net return)."""
    diffs = []
    for t in trades:
        m = matched_ret(t, declared_rt=declared_rt)
        if m is None:
            return None
        diffs.append(t.net_ret - m)
    return float(np.mean(diffs)) if diffs else None


def exit_mix(trades: Sequence[ClosedTrade]) -> dict[str, float]:
    if not trades:
        return {}
    c = Counter(t.exit_kind for t in trades)
    return {k: c.get(k, 0) / len(trades) for k in EXIT_KINDS}


@dataclass
class TradeSummary:
    n: int
    hit_rate: float | None
    expectancy: Interval
    payoff: float | None
    contribution_bps: float
    matched_contribution_bps: float | None
    vs_matched: float | None
    exit_mix: dict[str, float]
    avg_days_held: float | None


def summarize(trades: Sequence[ClosedTrade], *, declared_cost_pct_per_leg: float = 1.25,
              resamples: int = BOOTSTRAP_RESAMPLES) -> TradeSummary:
    rt = 2.0 * declared_cost_pct_per_leg / 100.0
    rs = [t.r_declared for t in trades]
    return TradeSummary(
        n=len(trades), hit_rate=hit_rate(rs), expectancy=bootstrap_mean(rs, resamples=resamples),
        payoff=payoff(rs), contribution_bps=contribution_bps(trades),
        matched_contribution_bps=matched_contribution_bps(trades, declared_rt=rt),
        vs_matched=vs_matched(trades, declared_rt=rt), exit_mix=exit_mix(trades),
        avg_days_held=float(np.mean([t.days_held for t in trades])) if trades else None)


def funnel(outcomes: Iterable[Mapping[str, object]], *, resamples: int = BOOTSTRAP_RESAMPLES) -> dict[str, Interval]:
    """Mean paper `r_declared` with its bootstrap interval, per paper group."""
    by: dict[str, list[float]] = {}
    for o in outcomes:
        by.setdefault(str(o["group"]), []).append(float(o["r_declared"]))  # type: ignore[arg-type]
    return {g: bootstrap_mean(v, resamples=resamples) for g, v in sorted(by.items())}


# ------------------------------------------------------------------------------ pre-declared rules
@dataclass(frozen=True)
class PauseDecision:
    applies: bool                         # False below PAUSE_MIN_TRADES closed trades
    pause: bool
    reasons: tuple[str, ...] = ()
    n: int = 0


def pause_rule(trades: Sequence[ClosedTrade], *, declared_cost_pct_per_leg: float = 1.25) -> PauseDecision:
    """§8.3: with >= 20 closed trades, pause new entries if mean r_declared <= 0 OR the swing
    contribution trails its matched index (same declared cost). Either alone is enough. An
    uncomputable matched line (a trade without beta or ETF return) fails closed: it pauses."""
    n = len(trades)
    if n < PAUSE_MIN_TRADES:
        return PauseDecision(applies=False, pause=False, n=n)
    reasons = []
    if float(np.mean([t.r_declared for t in trades])) <= 0.0:
        reasons.append("mean_r_declared_le_0")
    rt = 2.0 * declared_cost_pct_per_leg / 100.0
    matched = matched_contribution_bps(trades, declared_rt=rt)
    if matched is None:
        reasons.append("matched_index_unavailable")
    elif contribution_bps(trades) < matched - _TOL:
        reasons.append("trails_matched_index")
    return PauseDecision(applies=True, pause=bool(reasons), reasons=tuple(reasons), n=n)


def review_due(n_closed: int, golive: date, today: date, *, last_review_n: int | None = None,
               reviewed_day90: bool = False) -> bool:
    """§8.3: the first review at 30 closed trades or 90 calendar days after go-live (whichever
    first), then every further 30 trades."""
    if last_review_n is None:
        return n_closed >= REVIEW_TRADES or ((today - golive).days >= REVIEW_DAYS and not reviewed_day90)
    return n_closed >= last_review_n + REVIEW_TRADES


@dataclass(frozen=True)
class SkepticTest:
    status: str                           # insufficient | advisory | skeptic_value | inconclusive
    n: int
    rejected_mean: float | None
    passed_mean: float | None
    wait_mean: float | None = None

    @property
    def advisory(self) -> bool:
        return self.status == "advisory"


def skeptic_test(outcomes: Iterable[Mapping[str, object]]) -> SkepticTest:
    """§8.3 Skeptic test over finished paper outcomes, each {"verdict": pass|reject|wait,
    "r_declared": R}. Only pass and reject count toward the 40; wait is reported on its own."""
    by: dict[str, list[float]] = {"pass": [], "reject": [], "wait": []}
    for o in outcomes:
        v = str(o["verdict"])
        if v in by:
            by[v].append(float(o["r_declared"]))  # type: ignore[arg-type]
    n = len(by["pass"]) + len(by["reject"])
    mean = {k: (float(np.mean(v)) if v else None) for k, v in by.items()}
    if n < SKEPTIC_MIN_IDEAS or not by["pass"] or not by["reject"]:
        status = "insufficient"
    elif mean["reject"] - mean["pass"] >= SKEPTIC_MARGIN_R - _TOL:  # type: ignore[operator]
        status = "advisory"
    elif mean["pass"] - mean["reject"] >= SKEPTIC_MARGIN_R - _TOL:  # type: ignore[operator]
        status = "skeptic_value"
    else:
        status = "inconclusive"
    return SkepticTest(status=status, n=n, rejected_mean=mean["reject"], passed_mean=mean["pass"],
                       wait_mean=mean["wait"])


@dataclass(frozen=True)
class Promotion:
    eligible: bool
    n: int
    interval: Interval = field(default_factory=lambda: Interval(None, None, None, 0))


def setup_promotion(rs: Sequence[float], *, resamples: int = BOOTSTRAP_RESAMPLES) -> Promotion:
    """SB16: a paper-only setup may be promoted (by a recorded prereg revision) after >= 30 paper
    ideas with mean r_declared > 0 and a bootstrap lower bound > -0.1 R."""
    iv = bootstrap_mean(rs, resamples=resamples)
    ok = (iv.n >= PROMOTION_MIN_IDEAS and iv.mean is not None and iv.mean > 0
          and iv.low is not None and iv.low > PROMOTION_LOWER_R)
    return Promotion(eligible=bool(ok), n=iv.n, interval=iv)


def standard_error(rs: Sequence[float]) -> float | None:
    """The honest power line (§8.3): s.d. / sqrt(n)."""
    if len(rs) < 2:
        return None
    return float(np.std(rs, ddof=1) / math.sqrt(len(rs)))
