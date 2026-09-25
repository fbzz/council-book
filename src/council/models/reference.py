from __future__ import annotations

from pydantic import Field

from council.models.common import Strict, TrendState


class ReferenceEntry(Strict):
    symbol: str
    sleeve: str
    asset_class: str
    in_reference: bool
    trend: TrendState | None
    level_ref: float                  # 1.0 / 0.5 / 0.25 (0 for overlay lines)
    unit_weight: float = Field(ge=0)  # weight of NAV at level 1.0 = base_weight * vol cap * book scale
    weight_ref: float                 # level_ref * unit_weight (after truncation)
    sigma_ann: float | None
    stop_distance: float | None = None


class ReferenceBook(Strict):
    cycle_id: str
    entries: dict[str, ReferenceEntry]
    k: float                          # book scale applied to every unit weight (<= 1)
    target_vol: float
    ex_ante_vol: float
    gross: float
    truncations: list[str] = Field(default_factory=list)

    def weights(self) -> dict[str, float]:
        return {s: e.weight_ref for s, e in self.entries.items()}
