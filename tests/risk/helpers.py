"""Risk-area helpers: synthetic market states, cheap cost quotes, snapshots and policy variants.
Nothing here touches the network, a broker or an LLM; all numbers are synthetic."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from council.models.broker import CostQuote, ExposureSnapshot
from council.models.facts import MarketState
from council.policy import Policy
from council.risk.authority import compute_bands
from council.risk.engine import RiskEngine

NOW = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
SIGMA = {"NDX": 0.22, "SEMIS": 0.35, "SPX": 0.17, "GOLD": 0.15, "BTC": 0.55, "ETH": 0.75,
         "OIL": 0.35, "EURUSD": 0.07, "GBPUSD": 0.08}


def override(policy: Policy, section: str, updates: Mapping[str, Any]) -> Policy:
    """Copy of `policy` with dotted-path updates applied to one policy dict (risk, costs...)."""
    data = copy.deepcopy(getattr(policy, section))
    for path, value in updates.items():
        node = data
        keys = path.split(".")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return policy.model_copy(update={section: data})


# Budgets that would otherwise interfere with a test of one specific limit.
LOOSE = {
    "churn.cycle_increase_max": 10.0,
    "churn.turnover_7d_max": 50.0,
    "churn.turnover_30d_max": 50.0,
    "cost_budget.cycle_max_bps": 1e6,
    "cost_budget.discretionary_30d_max_bps": 1e6,
    "cost_budget.carry_proposal_max_bps_day": 1e6,
    "ex_ante_vol_hard": 10.0,
    "margin_use_max": 10.0,
    "caps.equity_beta_cluster.max": 10.0,
    "caps.crypto_total": 10.0,
    "caps.fx_total": 10.0,
    "proposal.max_legs": 50,
    "authority.max_deviations_per_cycle": 9,
}


def loose(policy: Policy, **extra: Any) -> Policy:
    updates = dict(LOOSE)
    updates.update({k.replace("__", "."): v for k, v in extra.items()})
    return override(policy, "risk", updates)


def state(symbol: str, asset_class: str, *, trend: str = "up", **kw: Any) -> MarketState:
    sigma = kw.pop("sigma_ann", SIGMA.get(symbol, 0.2))
    days = 365 if asset_class == "crypto" else 252
    fields: dict[str, Any] = {
        "symbol": symbol, "asset_class": asset_class, "trend": trend, "sigma_ann": sigma,
        "sigma_daily": None if sigma is None else sigma / math.sqrt(days),
        "ewma5_60_ratio": 1.0, "ret1d_sigma": 0.0,
        "market_open": True, "data_age_h": 2.0,
    }
    fields.update(kw)
    return MarketState(**fields)


def states_for(policy: Policy, trend: str | Mapping[str, str] = "up", **per_line: Any
               ) -> dict[str, MarketState]:
    out = {}
    for line in policy.universe.lines:
        t = trend if isinstance(trend, str) else trend.get(line.symbol, "up")
        out[line.symbol] = state(line.symbol, line.asset_class, trend=t,
                                 **per_line.get(line.symbol, {}))
    return out


def quote(symbol: str, direction: str = "long", *, per_side: float = 1.0, carry: float = 0.0,
          at: datetime = NOW, leverage: int = 1) -> CostQuote:
    return CostQuote(symbol=symbol, direction=direction, settlement="cfd", leverage=leverage,
                     per_side_bps=per_side, what_if_bps=None, carry_bps_day=carry, quoted_at=at)


def quotes_for(policy: Policy, *, per_side: float = 1.0, carry: float = 0.0,
               **per_line: Mapping[str, float]) -> dict[object, CostQuote]:
    out: dict[object, CostQuote] = {}
    for line in policy.universe.lines:
        opts = {"per_side": per_side, "carry": carry, **per_line.get(line.symbol, {})}
        for direction in ("long", "short"):
            out[(line.symbol, direction)] = quote(line.symbol, direction, **opts)
    return out


def snapshot(weights: Mapping[str, float], *, hedged: list[str] | None = None) -> ExposureSnapshot:
    """A synthetic snapshot on unit equity (weights are shares of NAV)."""
    return ExposureSnapshot(
        taken_at=NOW, equity_usd=1.0, credit_usd=1.0, positions=[],
        signed_w=dict(weights), gross=sum(abs(v) for v in weights.values()),
        net=sum(weights.values()), margin_use=0.0,
        unmapped=sorted(s for s in weights if s.startswith("UNMAPPED_")), hedged=hedged or [],
    )


def default_units(policy: Policy) -> dict[str, float]:
    return {line.symbol: line.base_weight for line in policy.universe.lines}


def default_ref(policy: Policy, trend: str = "up") -> dict[str, float]:
    level = {"up": 1.0, "mixed": 0.5, "down": 0.25}[trend]
    return {line.symbol: (level if line.in_reference else 0.0) for line in policy.universe.lines}


def run(policy: Policy, **kw: Any):
    """Run the engine with sensible synthetic defaults; any argument can be overridden.

    Extra keys: trend (for states/ref), current (weights -> snapshot), lever_ok, short_ok,
    event_blocked, cards, band_kill_state (kill state used for bands)."""
    trend = kw.pop("trend", "up")
    states = kw.pop("states", None) or states_for(policy, trend)
    units = kw.pop("unit_weights", None) or default_units(policy)
    ref = kw.pop("ref", None) or default_ref(policy, trend if isinstance(trend, str) else "up")
    current = kw.pop("current", None)
    snap = kw.pop("snapshot", None)
    if snap is None and current is not None:
        snap = snapshot(current, hedged=kw.pop("hedged", None))
    kill_state = kw.pop("kill_state", "NORMAL")
    cur_levels = {s: (w / units[s] if units.get(s) else 0.0)
                  for s, w in (snap.signed_w.items() if snap else []) if s in units}
    bands = kw.pop("bands", None) or compute_bands(
        lines=policy.universe, ref=ref, states=states, cards=kw.pop("cards", []),
        current_levels=cur_levels, kill_state=kw.pop("band_kill_state", kill_state),
        event_blocked=kw.pop("event_blocked", set()), lever_ok=kw.pop("lever_ok", set()),
        short_ok=kw.pop("short_ok", set()), policy=policy,
    )
    args: dict[str, Any] = {
        "levels": dict(ref), "ref": ref, "bands": bands, "states": states, "snapshot": snap,
        "unit_weights": units, "kill_state": kill_state, "cost_quotes": quotes_for(policy),
        "events": [], "last_change": {}, "turnover_7d": 0.0, "material_changed": True,
        "basis": "council", "now": NOW,
    }
    args.update(kw)
    return RiskEngine(policy).evaluate(**args)


def row(decision, rule_id: str, name: str | None = None):
    """The RiskCheck row with this id (and name)."""
    for check in decision.checks:
        if check.rule_id == rule_id and (name is None or check.name == name):
            return check
    raise KeyError(f"{rule_id}/{name} not in {[c.rule_id + '/' + c.name for c in decision.checks]}")
