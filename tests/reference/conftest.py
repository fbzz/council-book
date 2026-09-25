"""Helpers for the reference-book tests: states, policy variants, the script module."""

from __future__ import annotations

import copy
import importlib.util
import sys
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd
import pytest

from council.models.facts import MarketState
from council.paths import REPO_ROOT
from council.policy import Policy
from council.reference.backtest import CostModel, SimResult


def state(symbol: str, trend: str | None = "up", vol_ratio: float | None = 1.0, sigma: float | None = 0.2,
          asset_class: str = "etf") -> MarketState:
    return MarketState(symbol=symbol, asset_class=asset_class, trend=trend, vol_ratio_1y=vol_ratio, sigma_ann=sigma)


def all_states(policy: Policy, **overrides: dict[str, Any]) -> dict[str, MarketState]:
    """Every line up with vol ratio 1; `overrides[SYM]` replaces fields for one line."""
    out = {}
    for line in policy.universe.lines:
        fields = {"trend": "up", "vol_ratio": 1.0, "sigma": 0.2, **overrides.get(line.symbol, {})}
        out[line.symbol] = state(line.symbol, asset_class=line.asset_class, **fields)
    return out


def variant(policy: Policy, *, risk: Callable[[dict], None] | None = None,
            reference: Callable[[dict], None] | None = None, gross_max: float | None = None) -> Policy:
    """A deep-copied policy with in-place edits applied to its risk/reference dicts."""
    risk_d = copy.deepcopy(policy.risk)
    ref_d = copy.deepcopy(policy.reference)
    if risk:
        risk(risk_d)
    if reference:
        reference(ref_d)
    update: dict[str, Any] = {"risk": risk_d, "reference": ref_d}
    if gross_max is not None:
        update["universe"] = policy.universe.model_copy(update={"reference_gross_max": gross_max})
    return policy.model_copy(update=update)


def gaussian_returns(columns: list[str], *, n: int = 400, daily_sd: float = 0.01, seed: int = 1,
                     start: str = "2020-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame(rng.standard_normal((n, len(columns))) * daily_sd, index=idx, columns=columns)


def sim_from_nav(nav: list[float], gross: list[float] | None = None, start: str = "2020-01-01") -> SimResult:
    """A SimResult shell around a hand-written NAV path (single column X)."""
    idx = pd.bdate_range(start, periods=len(nav))
    g = gross if gross is not None else [1.0] * len(nav)
    nav_s = pd.Series(nav, index=idx, dtype=float)
    return SimResult(
        name="t",
        nav=nav_s,
        returns=nav_s.pct_change().fillna(0.0),
        weights=pd.DataFrame({"X": g}, index=idx, dtype=float),
        trades=pd.DataFrame({"X": [0.0] * len(nav)}, index=idx),
        costs=pd.Series(0.0, index=idx),
        orders=pd.DataFrame({"X": [np.nan] * len(nav)}, index=idx),
        cost_model=CostModel(per_side={"X": 0.0}, fixed={"X": 0.0}, classes={"X": "none"}),
    )


@pytest.fixture(scope="session")
def script_module():
    """scripts/backtest_reference.py loaded as a module (scripts/ is not a package)."""
    path = REPO_ROOT / "scripts" / "backtest_reference.py"
    spec = importlib.util.spec_from_file_location("backtest_reference_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
