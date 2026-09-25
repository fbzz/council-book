"""R4 catastrophe stops. Every open carries a fixed broker-side stop-loss; there is no stop budget
ladder (stop-at-risk is reported only; the user chose the soft kill switch instead).

Rules:
- Distance d = min(cap, max(class floor, sigma_mult x sigma_daily x sqrt(horizon_days))).
  Floors by asset class (`catastrophe_stop.floors`); the GOLD line uses `commodity_gold`;
  single stocks (v1.1) use the ETF floor.
- eToro's SL% is a percent of MARGIN: SL% = d x L x 100. It must lie inside
  [min_sl_pct + buffer, max_sl_pct - buffer]. Below the minimum the stop is widened to it;
  above the maximum there is no valid stop, so the open is not allowed (None).
- Stop rate: long = ask x (1 - d); short = bid x (1 + d).
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from council.models.broker import LeverageConfig
from council.models.facts import MarketState
from council.policy import LineSpec, Policy
from council.risk.config import CatastropheStopConfig, risk_limits

GOLD_LINES: frozenset[str] = frozenset({"GOLD"})
DEFAULT_SL_BUFFER_PP = 0.5  # mirrors risk.yaml catastrophe_stop.margin_pct_buffer_pp (tested)
_ANNUALISATION = {"crypto": 365.0}
_DEFAULT_ANNUALISATION = 252.0


def floor_key(line: LineSpec) -> str:
    if line.symbol in GOLD_LINES:
        return "commodity_gold"
    if line.asset_class == "stock":
        return "etf"
    return line.asset_class


def sigma_daily_of(state: MarketState, asset_class: str) -> float | None:
    """Daily sigma from the state; falls back to sigma_ann / sqrt(annualisation days)."""
    if state.sigma_daily is not None and state.sigma_daily > 0:
        return state.sigma_daily
    if state.sigma_ann is not None and state.sigma_ann > 0:
        days = _ANNUALISATION.get(asset_class, _DEFAULT_ANNUALISATION)
        return state.sigma_ann / math.sqrt(days)
    return None


def catastrophe_stop_distance(
    state: MarketState, line: LineSpec, policy: Policy, *, cfg: CatastropheStopConfig | None = None
) -> float:
    """d = min(cap, max(floor, sigma_mult x sigma_daily x sqrt(horizon))). Raises ValueError if
    the state carries no volatility at all (no open may happen without a stop). `cfg` is the
    policy's already-validated catastrophe_stop section (saves re-parsing risk.yaml in loops)."""
    cfg = cfg if cfg is not None else risk_limits(policy).catastrophe_stop
    key = floor_key(line)
    if key not in cfg.floors:
        raise ValueError(f"no catastrophe-stop floor for {key}")
    sigma = sigma_daily_of(state, line.asset_class)
    if sigma is None:
        raise ValueError(f"{line.symbol}: no volatility for a catastrophe stop")
    raw = cfg.sigma_mult * sigma * math.sqrt(cfg.horizon_days)
    return min(cfg.cap, max(cfg.floors[key], raw))


def fit_to_eligibility(
    d: float, leverage: int, lev_cfg: LeverageConfig, *, buffer_pp: float = DEFAULT_SL_BUFFER_PP
) -> float | None:
    """Fit a price-distance stop into the broker's SL% bounds (percent of margin).

    Returns the (possibly widened) distance, or None when no valid stop exists: SL editing is
    disabled, the buffered range is empty, or d x L x 100 exceeds max_sl_pct - buffer."""
    if d <= 0 or leverage < 1:
        raise ValueError("stop distance and leverage must be positive")
    if not (lev_cfg.allow_edit_stop_loss and lev_cfg.allow_sl_tp):
        return None
    lo = lev_cfg.min_sl_pct + buffer_pp
    hi = lev_cfg.max_sl_pct - buffer_pp
    if lo > hi:
        return None
    pct = d * leverage * 100.0
    if pct > hi + 1e-12:
        return None
    if pct < lo:
        return lo / (leverage * 100.0)
    return d


def sl_margin_pct(d: float, leverage: int) -> float:
    """The eToro SL% for a price distance: d x L x 100."""
    return d * leverage * 100.0


def stop_loss_rate(direction: str, bid: float, ask: float, d: float) -> float:
    """Fixed stop rate: long = ask x (1 - d), short = bid x (1 + d)."""
    if bid <= 0 or ask <= 0 or bid > ask:
        raise ValueError("need 0 < bid <= ask")
    if direction == "long":
        if not 0 < d < 1:
            raise ValueError("long stop distance must be in (0, 1)")
        return ask * (1.0 - d)
    if direction == "short":
        if d <= 0:
            raise ValueError("short stop distance must be positive")
        return bid * (1.0 + d)
    raise ValueError(f"unknown direction {direction!r}")


def stop_at_risk(
    weights: Mapping[str, float],
    distances: Mapping[str, float],
    *,
    default_distance: float = 1.0,
) -> float:
    """Reporting only: sum(|w| x d), the NAV share lost if every stop fired at its level.
    Lines without a known distance count at `default_distance` (1.0 = the whole position)."""
    return float(
        sum(abs(w) * distances.get(line, default_distance) for line, w in weights.items())
    )
