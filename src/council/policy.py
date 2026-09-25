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
Sleeve = Literal["core", "crypto", "overlay", "satellite"]
HistorySource = Literal["etoro", "tiingo", "binance"]


class Signal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: HistorySource
    ticker: str


class Vehicle(BaseModel):
    """A tradable instrument candidate for a line. Resolved against broker eligibility at onboarding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    settlement: Literal["real", "cfd"]


class Vehicles(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    long: list[Vehicle]
    short: list[Vehicle] = Field(default_factory=list)


class LineSpec(BaseModel):
    """An exposure line: what the council reasons about (e.g. Nasdaq-100), not a broker symbol."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    name: str
    asset_class: AssetClass
    sleeve: Sleeve
    in_reference: bool
    base_weight: float = Field(gt=0, le=1.0)
    council_deviations: bool = True
    signal: Signal
    vehicles: Vehicles

    @property
    def shortable(self) -> bool:
        return bool(self.vehicles.short)


class Universe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    reference_gross_max: float = Field(gt=0, le=1.0)
    lines: list[LineSpec]
    controls: dict[str, list[str]] = Field(default_factory=dict)

    def by_symbol(self) -> dict[str, LineSpec]:
        return {line.symbol: line for line in self.lines}

    def symbols(self) -> list[str]:
        return [line.symbol for line in self.lines]


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
