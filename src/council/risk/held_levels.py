"""Held reference levels: the level at which the mechanical rule last traded each line.

The studied rule (`council.reference.sleeve.pending_trades`, as in `reference.backtest.simulate`)
orders a line when its target level differs from the level it was last traded at. Live, that
"held level" is the only state the rule needs (design R2/§5.3). Rules:
- A FILLED reference-origin leg (filled, partially filled or partly rejected; never `modify_sl`)
  sets its line's held level to the reference level it traded toward (`Leg.ref_level`).
- A broker stop-loss hit on the line, or a filled close of a kill-switch flatten, later than that
  fill sets it to 0 (the position is gone; after the R4d cool-off the rule re-buys it).
- Discretionary legs never change it (the council is not the rule).
- Migration, for a line with neither: from the current book, once. An in-reference line takes its
  current weight / unit weight snapped to the level grid; any other line (overlay, shortlist,
  retiring) 0, the only level the rule gives it. The migrated levels are stored in the ledger's
  runtime state (`held_levels_migration`) the first time a connected cycle sees the line, so later
  unit-weight changes (the vol cap) are never mistaken for level changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from council.models.common import snap_level
from council.policy import LineSpec

MIGRATION_KEY = "held_levels_migration"
_EPS = 1e-12


def migrated_level(line: LineSpec, weight: float, unit: float) -> float:
    """The held level a line migrates to from its current weight (module rules)."""
    if not line.in_reference or unit <= _EPS:
        return 0.0
    return snap_level(float(weight) / unit)


def migrated_levels(lines: Iterable[LineSpec], current_w: Mapping[str, float],
                    units: Mapping[str, float]) -> dict[str, float]:
    return {ln.symbol: migrated_level(ln, float(current_w.get(ln.symbol, 0.0)), float(units.get(ln.symbol, 0.0)))
            for ln in lines}


def resolve_held_levels(
    lines: Iterable[str],
    *,
    fills: Mapping[str, tuple[float, datetime]],
    resets: Mapping[str, datetime],
    migrated: Mapping[str, float],
) -> dict[str, float]:
    """Held level per line: the latest reference fill's level, 0 when a reset (stop hit, flatten
    close) is later than that fill, else the migrated level (0 when there is none)."""
    out: dict[str, float] = {}
    for s in lines:
        fill = fills.get(s)
        reset = resets.get(s)
        if fill is not None and (reset is None or fill[1] >= reset):
            out[s] = float(fill[0])
        elif reset is not None:
            out[s] = 0.0
        else:
            out[s] = float(migrated.get(s, 0.0))
    return out


def ledger_held_levels(
    ledger: Any,
    lines: Iterable[LineSpec],
    *,
    current_w: Mapping[str, float] | None,
    units: Mapping[str, float],
    now: datetime,
    persist: bool,
) -> dict[str, float]:
    """The cycle's held levels from the ledger (duck-typed: `reference_fills`, `level_resets`,
    `get_runtime`, `set_runtime`). `current_w` is the snapshot book (None: no broker, the book is
    flat). With `persist`, lines migrated for the first time are stored (connected cycles only)."""
    specs = list(lines)
    record = ledger.get_runtime(MIGRATION_KEY) or {}
    stored = dict(record.get("levels") or {}) if isinstance(record, dict) else {}
    fills = ledger.reference_fills()
    resets = ledger.level_resets()
    fresh = {
        ln.symbol: migrated_level(ln, float((current_w or {}).get(ln.symbol, 0.0)),
                                  float(units.get(ln.symbol, 0.0)))
        for ln in specs
        if ln.symbol not in stored and ln.symbol not in fills and ln.symbol not in resets
    }
    migrated = {**{k: float(v) for k, v in stored.items()}, **fresh}
    if persist and fresh and current_w is not None:
        ledger.set_runtime(MIGRATION_KEY, {"levels": dict(sorted(migrated.items())),
                                           "updated_at": now.isoformat()})
    return resolve_held_levels([ln.symbol for ln in specs], fills=fills, resets=resets, migrated=migrated)
