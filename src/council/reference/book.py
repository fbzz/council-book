"""Mechanical reference book: trend level x vol-capped unit weight, scaled to the gross and ex-ante
vol limits, then clipped to the risk caps. Never short, never levered.

Rules (policy keys in brackets):
- Level: an in-reference line takes its trend level [reference.trend.levels: up 1.0, mixed 0.5,
  down 0.25]. A line without a trend state holds 0.0 and is flagged. Overlay lines hold 0.0.
- Unit: base_weight x min(1, line_cap_ratio / vol_ratio_1y) [reference.vol.line_cap_ratio].
  Volatility only shrinks a line, never levers it. A line without a vol ratio gets
  base_weight x 0.5 and is flagged.
- Scale: w = level x unit. k_gross = min(1, reference_gross_max / sum|w|)
  [universe.reference_gross_max]; then k_vol = min(1, ex_ante_vol_hard / ex-ante vol)
  [reference.book.ex_ante_vol_hard, and risk.ex_ante_vol_hard if stricter]. k = k_gross x k_vol.
- Caps: risk.caps.line, crypto_total, fx_total and equity_beta_cluster, by clipping. A group over
  its cap scales its members pro rata. If clipping raised ex-ante vol (a removed hedge), the book is
  scaled down once more. Every reduction is recorded in `truncations`, in percent.

`states` and `returns` are keyed by line symbol (NDX); the line's signal ticker (QQQ) is accepted
as a fallback key. `returns` are daily simple returns on one common calendar.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from council.models.facts import MarketState
from council.models.reference import ReferenceBook, ReferenceEntry
from council.policy import LineSpec, Policy

MISSING_VOL_HAIRCUT = 0.5      # unit multiplier when a line has no vol ratio
COVARIANCE_LOOKBACK_SPANS = 5  # rows older than 5 spans carry < 0.5% of the EWMA weight
_EPS = 1e-12


def _flag(flags: list[str] | None, text: str) -> None:
    if flags is not None:
        flags.append(text)


def _state(line: LineSpec, states: Mapping[str, MarketState]) -> MarketState | None:
    """The line's state, keyed by line symbol, else by its signal ticker."""
    state = states.get(line.symbol)
    if state is None:
        state = states.get(line.signal.ticker)
    return state


def _pct(x: float) -> str:
    return f"{100.0 * x:.2f}%"


def trend_level_table(policy: Policy) -> dict[str, float]:
    """Trend state -> reference level. Rule: 0 <= down <= mixed <= up <= 1 (never short/levered)."""
    raw = policy.reference["trend"]["levels"]
    table = {state: float(raw[state]) for state in ("up", "mixed", "down")}
    if not 0.0 <= table["down"] <= table["mixed"] <= table["up"] <= 1.0:
        raise ValueError(f"reference.trend.levels must satisfy 0 <= down <= mixed <= up <= 1: {table}")
    return table


def reference_levels(
    lines: Sequence[LineSpec],
    states: Mapping[str, MarketState],
    policy: Policy,
    flags: list[str] | None = None,
) -> dict[str, float]:
    """Reference level per line. Rule: in-reference lines take the trend level; a missing trend state
    gives 0.0 plus a flag; overlay (council-only) lines are always 0.0."""
    table = trend_level_table(policy)
    out: dict[str, float] = {}
    for line in lines:
        if not line.in_reference:
            out[line.symbol] = 0.0
            continue
        state = _state(line, states)
        trend = state.trend if state is not None else None
        if trend is None:
            out[line.symbol] = 0.0
            _flag(flags, f"{line.symbol}: no trend state, level 0")
            continue
        out[line.symbol] = table[trend]
    return out


def _vol_ratio(state: MarketState | None) -> float | None:
    if state is None or state.vol_ratio_1y is None:
        return None
    ratio = float(state.vol_ratio_1y)
    if not math.isfinite(ratio) or ratio <= 0.0:
        return None
    return ratio


def unit_weights(
    lines: Sequence[LineSpec],
    states: Mapping[str, MarketState],
    policy: Policy,
    flags: list[str] | None = None,
) -> dict[str, float]:
    """Unit weight (weight of NAV at level 1.0) per line.

    Rule: unit = base_weight x min(1, line_cap_ratio / vol_ratio_1y). Vol is a CAP, never a lever:
    a calm line keeps its base weight. A missing, non-finite or non-positive vol ratio gives
    base_weight x 0.5 plus a flag."""
    cap_ratio = float(policy.reference["vol"].get("line_cap_ratio", 1.0))
    if cap_ratio <= 0.0:
        raise ValueError("reference.vol.line_cap_ratio must be positive")
    out: dict[str, float] = {}
    for line in lines:
        ratio = _vol_ratio(_state(line, states))
        if ratio is None:
            out[line.symbol] = line.base_weight * MISSING_VOL_HAIRCUT
            _flag(flags, f"{line.symbol}: no vol ratio, unit halved")
        else:
            out[line.symbol] = line.base_weight * min(1.0, cap_ratio / ratio)
    return out


def ewma_covariance(returns: pd.DataFrame, span: int = 90, ann: int = 252) -> pd.DataFrame:
    """Zero-mean EWMA covariance of daily returns (RiskMetrics convention), annualised by `ann`.

    Row weights are proportional to (1 - a)^age with a = 2 / (span + 1), newest row heaviest.
    Pairwise-complete: each pair uses only the rows where both returns are finite, with the weights
    renormalised over those rows. A pair with no common row is NaN."""
    if span < 1:
        raise ValueError("span must be >= 1")
    cols = [str(c) for c in returns.columns]
    x = returns.to_numpy(dtype=float, copy=True)
    if x.shape[0] == 0:
        return pd.DataFrame(np.nan, index=cols, columns=cols)
    alpha = 2.0 / (span + 1.0)
    decay = (1.0 - alpha) ** np.arange(x.shape[0] - 1, -1, -1, dtype=float)
    finite = np.isfinite(x)
    x0 = np.where(finite, x, 0.0)
    mask = finite.astype(float)
    num = (x0 * decay[:, None]).T @ x0
    den = (mask * decay[:, None]).T @ mask
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = np.where(den > 0.0, num / den, np.nan) * float(ann)
    return pd.DataFrame(cov, index=cols, columns=cols)


def ex_ante_vol(weights: Mapping[str, float], cov: pd.DataFrame) -> float:
    """Ex-ante annualised vol sqrt(w' C w) over the non-zero weights.

    Rule: a weighted line missing from `cov`, or with a non-finite entry, raises; risk is never
    silently treated as zero."""
    syms = [s for s, v in weights.items() if abs(float(v)) > _EPS]
    if not syms:
        return 0.0
    missing = [s for s in syms if s not in cov.index or s not in cov.columns]
    if missing:
        raise KeyError(f"covariance missing weighted lines: {missing}")
    sub = cov.loc[syms, syms].to_numpy(dtype=float)
    if not np.isfinite(sub).all():
        raise ValueError(f"non-finite covariance among weighted lines: {syms}")
    w = np.array([float(weights[s]) for s in syms])
    return float(math.sqrt(max(0.0, float(w @ sub @ w))))


def _align_returns(returns: pd.DataFrame | None, lines: Sequence[LineSpec]) -> pd.DataFrame:
    """Columns renamed to line symbols (signal tickers accepted), other columns dropped."""
    if returns is None or returns.empty:
        return pd.DataFrame(columns=[line.symbol for line in lines], dtype=float)
    cols: dict[str, pd.Series] = {}
    for line in lines:
        if line.symbol in returns.columns:
            cols[line.symbol] = returns[line.symbol]
        elif line.signal.ticker in returns.columns:
            cols[line.symbol] = returns[line.signal.ticker]
    return pd.DataFrame(cols, index=returns.index, dtype=float)


def _complete_covariance(
    cov: pd.DataFrame,
    symbols: Sequence[str],
    lines: Mapping[str, LineSpec],
    states: Mapping[str, MarketState],
    notes: list[str],
) -> pd.DataFrame:
    """Covariance over `symbols` with conservative gap filling.

    Rule: a missing variance uses the state's sigma_ann squared, else the largest known variance;
    a missing covariance assumes correlation 1 (no diversification credit). Each fill is noted."""
    var: dict[str, float] = {}
    for s in symbols:
        v = float(cov.at[s, s]) if s in cov.index and s in cov.columns else math.nan
        if math.isfinite(v) and v > 0.0:
            var[s] = v
            continue
        state = _state(lines[s], states)
        sigma = state.sigma_ann if state is not None else None
        if sigma is not None and math.isfinite(sigma) and sigma > 0.0:
            var[s] = float(sigma) ** 2
            notes.append(f"{s}: no return history, variance from sigma_ann")
    known = [v for v in var.values()]
    for s in symbols:
        if s not in var:
            if not known:
                raise ValueError(f"no volatility information for weighted lines {list(symbols)}")
            var[s] = max(known)
            notes.append(f"{s}: no volatility information, largest known variance used")
    out = pd.DataFrame(np.nan, index=list(symbols), columns=list(symbols), dtype=float)
    for a in symbols:
        for b in symbols:
            if a == b:
                out.at[a, b] = var[a]
                continue
            c = (
                float(cov.at[a, b])
                if a in cov.index and b in cov.columns
                else math.nan
            )
            if not math.isfinite(c):
                c = math.sqrt(var[a] * var[b])
                if a < b:
                    notes.append(f"{a}/{b}: no common history, correlation 1 assumed")
            out.at[a, b] = c
    return out


def _vol_hard(policy: Policy) -> float:
    """The stricter of reference.book.ex_ante_vol_hard and risk.ex_ante_vol_hard."""
    limits = [float(policy.reference["book"]["ex_ante_vol_hard"])]
    if "ex_ante_vol_hard" in policy.risk:
        limits.append(float(policy.risk["ex_ante_vol_hard"]))
    return min(limits)


def apply_caps(
    weights: Mapping[str, float],
    lines: Sequence[LineSpec],
    policy: Policy,
    notes: list[str] | None = None,
) -> dict[str, float]:
    """Clip weights to risk.caps. Rule: |w| <= line cap; each group (crypto_total over crypto
    lines, fx_total over fx lines, equity_beta_cluster over its members) with sum|w| above its cap
    scales its members by cap / sum|w|. Caps only ever reduce |w|."""
    caps = policy.risk.get("caps", {})
    out = {s: float(v) for s, v in weights.items()}
    for sym, cap in caps.get("line", {}).items():
        cap = float(cap)
        if sym in out and abs(out[sym]) > cap + _EPS:
            _flag(notes, f"{sym}: line cap {_pct(cap)} (from {_pct(abs(out[sym]))})")
            out[sym] = math.copysign(cap, out[sym])
    groups: list[tuple[str, list[str], float]] = []
    if "crypto_total" in caps:
        members = [ln.symbol for ln in lines if ln.asset_class == "crypto"]
        groups.append(("crypto_total", members, float(caps["crypto_total"])))
    if "fx_total" in caps:
        members = [ln.symbol for ln in lines if ln.asset_class == "fx"]
        groups.append(("fx_total", members, float(caps["fx_total"])))
    cluster = caps.get("equity_beta_cluster")
    if cluster:
        groups.append(("equity_beta_cluster", [str(m) for m in cluster["members"]], float(cluster["max"])))
    for name, members, cap in groups:
        total = sum(abs(out.get(m, 0.0)) for m in members)
        if total > cap + _EPS:
            factor = cap / total
            for m in members:
                if m in out:
                    out[m] *= factor
            _flag(notes, f"{name}: {_pct(total)} above cap {_pct(cap)}, members scaled to the cap")
    return out


def build_reference(
    *,
    cycle_id: str,
    lines: Sequence[LineSpec],
    states: Mapping[str, MarketState],
    returns: pd.DataFrame | None,
    policy: Policy,
    flags: list[str] | None = None,
) -> ReferenceBook:
    """Build the reference book for one cycle (see the module docstring for every rule).

    Data-quality defaults (missing trend or vol) are recorded in `truncations` because they reduce
    the reference, and are also appended to `flags` when given. The result is checked: no short
    weight, gross <= reference_gross_max, ex-ante vol <= the hard limit (up to rounding)."""
    line_map = {line.symbol: line for line in lines}
    notes: list[str] = []
    levels = reference_levels(lines, states, policy, notes)
    units = unit_weights(lines, states, policy, notes)
    raw = {s: levels[s] * units[s] for s in line_map}
    if any(v < 0.0 for v in raw.values()):
        raise ValueError("reference weights must be non-negative (never short)")

    gross_max = float(policy.universe.reference_gross_max)
    gross_raw = sum(abs(v) for v in raw.values())
    k_gross = gross_max / gross_raw if gross_raw > gross_max * (1.0 + 1e-9) else 1.0
    if k_gross < 1.0:
        notes.append(f"book: gross {_pct(gross_raw)} above {_pct(gross_max)}, scaled to the limit")

    weighted = [s for s, v in raw.items() if v > _EPS]
    span = int(policy.reference["book"]["covariance_ewma_days"])
    ann = int(policy.reference["vol"]["annualisation_days"]["default"])
    vol_hard = _vol_hard(policy)
    cov = pd.DataFrame(dtype=float)
    k_vol = 1.0
    if weighted:
        aligned = _align_returns(returns, lines)
        if len(aligned) > COVARIANCE_LOOKBACK_SPANS * span:
            aligned = aligned.iloc[-COVARIANCE_LOOKBACK_SPANS * span :]
        cov = _complete_covariance(ewma_covariance(aligned, span, ann), weighted, line_map, states, notes)
        vol_pre = ex_ante_vol({s: raw[s] * k_gross for s in weighted}, cov)
        if vol_pre > vol_hard:
            k_vol = vol_hard / vol_pre
            notes.append(f"book: ex-ante vol {_pct(vol_pre)} above {_pct(vol_hard)}, scaled to the limit")
    k = k_gross * k_vol

    weights = apply_caps({s: raw[s] * k for s in line_map}, lines, policy, notes)
    vol_post = ex_ante_vol({s: weights[s] for s in weighted}, cov) if weighted else 0.0
    if vol_post > vol_hard * (1.0 + 1e-9):
        k_post = vol_hard / vol_post
        weights = {s: v * k_post for s, v in weights.items()}
        k *= k_post
        vol_post = vol_hard
        notes.append("book: capping raised ex-ante vol, scaled back to the limit")

    gross = sum(abs(v) for v in weights.values())
    if any(v < -_EPS for v in weights.values()):
        raise AssertionError("reference book went short")
    if gross > gross_max + 1e-9:
        raise AssertionError(f"reference book gross {gross} above {gross_max}")

    entries: dict[str, ReferenceEntry] = {}
    for line in lines:
        state = _state(line, states)
        entries[line.symbol] = ReferenceEntry(
            symbol=line.symbol,
            sleeve=line.sleeve,
            asset_class=line.asset_class,
            in_reference=line.in_reference,
            trend=state.trend if state is not None else None,
            level_ref=levels[line.symbol],
            unit_weight=units[line.symbol] * k,
            weight_ref=max(0.0, weights[line.symbol]),
            sigma_ann=state.sigma_ann if state is not None else None,
        )
    if flags is not None:
        flags.extend(notes)
    return ReferenceBook(
        cycle_id=cycle_id,
        entries=entries,
        k=k,
        target_vol=float(policy.reference["book"]["ex_ante_vol_target"]),
        ex_ante_vol=vol_post,
        gross=gross,
        truncations=notes,
    )
