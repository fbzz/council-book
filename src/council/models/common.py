from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

TrendState = Literal["up", "mixed", "down"]
Direction = Literal["long", "short"]
Settlement = Literal["real", "cfd", "realFutures", "marginTrade"]
LEVEL_GRID: tuple[float, ...] = (-0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5)
# Levels are multiples of a line's unit weight. Above 1.0 = leverage extension (uptrend only, cost-gated).

# Evidence-ID prefixes. The auditor rejects any cited ID that is not in the cycle's pack.
#   F:<sym>:<field>      market fact            N:<sha8>             news item
#   S:<accession>#p<n>   SEC filing sentence    M:<series>@<date>    macro value
#   E:<event_id>         scheduled event        C:<sym>:<field>      cost fact
#   V:<sym>:<field>      volatility fact        K:<role>:<n>         evidence card (code-assigned)
EVIDENCE_PREFIXES = ("F:", "N:", "S:", "M:", "E:", "C:", "V:", "K:")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def snap_level(value: float) -> float:
    """Snap an arbitrary level to the nearest grid value (ties go toward zero)."""
    return min(LEVEL_GRID, key=lambda g: (abs(g - value), abs(g)))
