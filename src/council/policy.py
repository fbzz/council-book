"""Typed access to the frozen policy files. Every cycle records the policy SHA-256."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from council.paths import POLICY_DIR

AssetClass = Literal["stock", "etf", "crypto", "index", "commodity", "fx"]
Sleeve = Literal["core", "crypto", "satellite", "index", "commodity", "fx"]
HistorySource = Literal["etoro", "tiingo", "binance"]


class InstrumentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    asset_class: AssetClass
    sleeve: Sleeve
    in_reference: bool
    budget_pct: float = Field(gt=0, le=20)
    history: HistorySource
    proxy: str
    peer_group: str | None = None


class SatelliteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    k: int = Field(ge=0, le=5)
    budget_pct_per_name: float
    benchmark: str
    peer_groups: dict[str, list[str]]


class Universe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    instruments: list[InstrumentSpec]
    satellite: SatelliteSpec

    def by_symbol(self) -> dict[str, InstrumentSpec]:
        return {i.symbol: i for i in self.instruments}

    def satellite_candidates(self) -> list[InstrumentSpec]:
        out: list[InstrumentSpec] = []
        for group, symbols in self.satellite.peer_groups.items():
            for sym in symbols:
                out.append(
                    InstrumentSpec(
                        symbol=sym,
                        asset_class="stock",
                        sleeve="satellite",
                        in_reference=True,
                        budget_pct=self.satellite.budget_pct_per_name,
                        history="tiingo",
                        proxy=sym,
                        peer_group=group,
                    )
                )
        return out


def _load(name: str, directory: Path | None = None) -> dict[str, Any]:
    path = (directory or POLICY_DIR) / name
    with path.open() as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


class Policy(BaseModel):
    """All policy files, parsed. `risk`, `reference`, `costs`, `council` stay dicts on purpose:
    the modules that own them validate the parts they use, and tests assert every number."""

    model_config = ConfigDict(frozen=True)

    universe: Universe
    risk: dict[str, Any]
    reference: dict[str, Any]
    costs: dict[str, Any]
    council: dict[str, Any]
    calendar: dict[str, Any]
    sha256: str

    @classmethod
    def load(cls, directory: Path | None = None) -> Policy:
        directory = directory or POLICY_DIR
        return cls(
            universe=Universe.model_validate(_load("universe.yaml", directory)),
            risk=_load("risk.yaml", directory),
            reference=_load("reference.yaml", directory),
            costs=_load("costs.yaml", directory),
            council=_load("council.yaml", directory),
            calendar=_load("calendar-2026.yaml", directory),
            sha256=policy_sha256(directory),
        )


def policy_sha256(directory: Path | None = None) -> str:
    """Hash of every policy file (sorted by name) — changes whenever any number changes."""
    directory = directory or POLICY_DIR
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.yaml")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@lru_cache(maxsize=1)
def default_policy() -> Policy:
    return Policy.load()
