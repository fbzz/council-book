"""The swing book as runtime risk lines, and the runtime vehicle -> line map (swing-book.md rev 2,
§1.9 and §3.2; SW-4).

- `SwingLine`: one swing trade as an ALL-OR-NOTHING runtime line of the risk engine
  (`RiskEngine.evaluate(extra_lines=...)`). Its weight is pinned: `+-size` for an entry, 0 for an
  exit, the current weight for a hold. The engine never scales it; when a whole-book limit binds,
  whole swing ENTRIES are removed (last in PM order first) before any core line shrinks. Exits and
  holds are never removed.
- `assert_vehicle`: a swing long is real shares at 1x; a swing short is a 1x CFD with a stop at most
  `SWING_MAX_SHORT_STOP_PCT` away. A stock CFD long, a real short or any leverage above 1 raises
  (`invariants._check_stocks_real_long_1x` only covers the universe, so runtime lines need this).
- `SwingVehicleMap` / `swing_vehicle_map`: built from the open and in-flight `swing_trades` rows, so
  reconcile, approval, the watch and `stocks.corporate.classify_vanished_positions` see a swing
  position as a swing line, never as an unknown (UNMAPPED) position.

Pure except `swing_vehicle_map`, which only reads the ledger. Never imports the broker writer.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from council import invariants as inv
from council.broker.parsing import unmapped_symbol
from council.stocks.universe import try_normalise_id
from council.swing.models import ACTIVE_STATES

LINE_PREFIX = "SW_"
MAX_LINE_LEN = 12                     # publish.public_models.LINE_PATTERN
SIGMA_ANN_DAYS = 252
Action = Literal["enter", "hold", "exit"]


class SwingVehicleError(ValueError):
    """A runtime swing line on a forbidden vehicle (stock CFD long, real short, leverage > 1)."""


def line_id(ticker: str) -> str:
    """`SW_<ticker line id>`; raises ValueError when the ticker cannot form a line id."""
    tid = try_normalise_id(ticker)
    if tid is None:
        raise ValueError("unusable ticker for a swing line")
    lid = f"{LINE_PREFIX}{tid}"
    if len(lid) > MAX_LINE_LEN:
        raise ValueError("ticker too long for a swing line id")
    return lid


def is_swing_line(line: str) -> bool:
    return line.startswith(LINE_PREFIX)


def assert_vehicle(side: str, settlement: str, leverage: int, stop_pct: float | None) -> None:
    if leverage != 1:
        raise SwingVehicleError("swing lines are unlevered (leverage 1)")
    if side == "long":
        if not inv.STOCK_LONGS_REAL_1X or settlement != "real":
            raise SwingVehicleError("a swing long is real shares only (no stock CFD long)")
        return
    if side != "short":
        raise SwingVehicleError(f"unknown side {side!r}")
    if not inv.STOCK_SHORTS_CFD_1X_WITH_STOP or settlement != "cfd":
        raise SwingVehicleError("a swing short is a 1x CFD only")
    if stop_pct is None or not 0 < stop_pct <= inv.SWING_MAX_SHORT_STOP_PCT + 1e-12:
        raise SwingVehicleError("a swing short needs a stop at most 8% away")


@dataclass(frozen=True)
class SwingLine:
    """One swing trade as a pinned runtime line. Weights are signed NAV shares."""

    line_id: str
    ref: str                      # idea:<id> (entry) or trade:<id> (hold / exit)
    ticker: str
    side: Literal["long", "short"]
    action: Action
    pinned_w: float
    settlement: Literal["real", "cfd"]
    leverage: int = 1
    stop_pct: float | None = None
    sigma_ann: float | None = None     # annualised volatility (Alpaca returns); None blocks an entry
    beta_60d: float | None = None
    vehicle: str | None = None         # broker symbol (InstrumentMap key)

    def __post_init__(self) -> None:
        assert_vehicle(self.side, self.settlement, self.leverage, self.stop_pct)
        if not is_swing_line(self.line_id):
            raise ValueError("swing line ids start with SW_")
        if not math.isfinite(self.pinned_w):
            raise ValueError("pinned weight must be finite")
        if self.action == "exit" and abs(self.pinned_w) > 0:
            raise ValueError("an exit pins the line at 0")
        if self.action == "enter" and (self.pinned_w > 0) != (self.side == "long"):
            raise ValueError("an entry's sign must match its side")
        if self.action == "enter":
            _assert_entry_caps(self.side, abs(self.pinned_w), self.stop_pct)


def _assert_entry_caps(side: str, size: float, stop_pct: float | None) -> None:
    """Defence in depth behind S1/S5 (§3.6): an entry line carries its stop, stays within the code
    size ceiling, the long stop ceiling and the planned loss at the stop (0.8% / 0.5% NAV)."""
    tol = 1e-9
    if size > inv.SWING_MAX_SIZE_NAV + tol:
        raise ValueError("a swing entry above the code size ceiling")
    if stop_pct is None or not math.isfinite(stop_pct) or stop_pct <= 0:
        raise ValueError("a swing entry needs a stop (S5)")
    if side == "long" and stop_pct > inv.SWING_MAX_LONG_STOP_PCT + tol:
        raise ValueError("a swing long's stop beyond the code ceiling")
    loss = inv.SWING_MAX_LONG_LOSS_NAV if side == "long" else inv.SWING_MAX_SHORT_LOSS_NAV
    if size * stop_pct > loss + tol:
        raise ValueError("a swing entry's planned loss at the stop above the code ceiling")


def entry_line(ref: str, ticker: str, side: str, size_nav: float, *, stop_pct: float,
               sigma_daily: float | None, beta_60d: float | None = None, vehicle: str | None = None) -> SwingLine:
    sign = 1.0 if side == "long" else -1.0
    return SwingLine(line_id=line_id(ticker), ref=ref, ticker=ticker, side=side,  # type: ignore[arg-type]
                     action="enter", pinned_w=sign * size_nav,
                     settlement="real" if side == "long" else "cfd", stop_pct=stop_pct,
                     sigma_ann=None if sigma_daily is None else sigma_daily * math.sqrt(SIGMA_ANN_DAYS),
                     beta_60d=beta_60d, vehicle=vehicle)


def open_line(ref: str, ticker: str, side: str, current_w: float, *, exit_: bool = False,
              stop_pct: float | None = None, sigma_daily: float | None = None,
              beta_60d: float | None = None, vehicle: str | None = None) -> SwingLine:
    """A hold (pinned at its current weight) or an exit (pinned at 0) of an open trade."""
    return SwingLine(line_id=line_id(ticker), ref=ref, ticker=ticker, side=side,  # type: ignore[arg-type]
                     action="exit" if exit_ else "hold", pinned_w=0.0 if exit_ else float(current_w),
                     settlement="real" if side == "long" else "cfd",
                     stop_pct=stop_pct,
                     sigma_ann=None if sigma_daily is None else sigma_daily * math.sqrt(SIGMA_ANN_DAYS),
                     beta_60d=beta_60d, vehicle=vehicle)


def pinned_targets(lines: Iterable[SwingLine]) -> dict[str, float]:
    return {ln.line_id: ln.pinned_w for ln in lines}


# ------------------------------------------------------------------------------ runtime map
@dataclass(frozen=True)
class SwingVehicleMap:
    """{instrument id: vehicle symbol} and {vehicle symbol: swing line id} of the live swing book."""

    symbols_by_id: Mapping[int, str] = field(default_factory=dict)
    line_by_vehicle: Mapping[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.line_by_vehicle)

    def merged_symbols(self, base: Mapping[int, str]) -> dict[int, str]:
        """`base` wins: a universe instrument is never re-labelled as a swing one."""
        return {**dict(self.symbols_by_id), **dict(base)}

    def merged_lines(self, base: Mapping[str, str]) -> dict[str, str]:
        return {**dict(self.line_by_vehicle), **dict(base)}

    def owner(self, symbol: str) -> str | None:
        return self.line_by_vehicle.get(symbol)


def build_vehicle_map(rows: Iterable[Any], *, core_vehicles: Iterable[str] = ()) -> SwingVehicleMap:
    """From swing trade rows (`ledger.SwingTradeRow`-like: ticker, instrument_id, state). Only the
    active states count; a vehicle a core/universe line already owns is left to that line."""
    owned = set(core_vehicles)
    by_id: dict[int, str] = {}
    by_vehicle: dict[str, str] = {}
    for r in rows:
        if getattr(r, "state", None) not in ACTIVE_STATES:
            continue
        tid = try_normalise_id(r.ticker)
        if tid is None or tid in owned:
            continue
        try:
            lid = line_id(tid)
        except ValueError:
            continue
        by_vehicle[tid] = lid
        by_vehicle[lid] = lid
        if r.instrument_id is not None:
            by_id[int(r.instrument_id)] = tid
            # A portfolio parsed with the universe map only names a swing position
            # UNMAPPED_<id> (`broker.parsing.unmapped_symbol`): it is still this swing line.
            by_vehicle[unmapped_symbol(int(r.instrument_id))] = lid
    return SwingVehicleMap(symbols_by_id=by_id, line_by_vehicle=by_vehicle)


def swing_vehicle_map(ledger: Any, policy: Any = None) -> SwingVehicleMap:
    """The runtime map from the ledger's open and in-flight swing trades (empty on any error, so a
    missing table never turns into a crash; an empty map keeps today's behaviour)."""
    core: set[str] = set()
    if policy is not None:
        from council.execution.planner import vehicle_to_line

        core = set(vehicle_to_line(policy.universe))
    try:
        rows = ledger.swing_trades(states=sorted(ACTIVE_STATES))
    except Exception:  # noqa: BLE001 - an old ledger without swing tables: no swing positions
        return SwingVehicleMap()
    return build_vehicle_map(rows, core_vehicles=core)


def approval_drift(current: Mapping[str, float], base: Mapping[str, float],
                   smap: SwingVehicleMap) -> float:
    """L1 drift of the book since the proposal (`approve.py`, `drift_l1_max`). A swing line whose
    trade the broker already closed (SL/TP, so it is gone from the book and from the live map) is left
    out (§3.2: a closed 8% swing trade alone would otherwise refuse the whole decision); every other
    swing line counts normally."""
    gone = {s for s in base if is_swing_line(s) and abs(current.get(s, 0.0)) <= 0 and not smap.owner(s)}
    return sum(abs(current.get(s, 0.0) - base.get(s, 0.0)) for s in (set(current) | set(base)) - gone)


def summary(lines: Sequence[SwingLine]) -> dict[str, float]:
    """Public-safe aggregates of the pinned swing book (weights are public, percent of NAV)."""
    longs = sum(ln.pinned_w for ln in lines if ln.pinned_w > 0)
    shorts = -sum(ln.pinned_w for ln in lines if ln.pinned_w < 0)
    beta = sum(ln.pinned_w * (ln.beta_60d if ln.beta_60d is not None else 1.0) for ln in lines)
    return {"gross": longs + shorts, "net": longs - shorts, "net_beta": beta}
