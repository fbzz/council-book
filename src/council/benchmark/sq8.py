"""The SQ-8 paper benchmark (design swing-book.md rev 2, §6.2, §6.4, §8.2; SW-6).

The mechanical stock-sleeve rule that failed its pre-registered gate, tracked on paper and never
traded. Label on every public output: `LABEL`.

- **Rule**: exactly the adopted cell (SQ-8: sector score, sector quotas, 8 names, equal weight, no
  overlay). The quarterly selection is `council.stocks.rank.rank` over the FROZEN modules
  (`stocks.pit`, `stocks.score`, `stocks.sectors`); the book's order decision at each close is the
  FROZEN `council.reference.sleeve.pending_trades` (drift deadband + never borrow), with the unit and
  the drift threshold from `council.reference.sleeve.unit_weight` / `drift_threshold`. Nothing here
  re-implements the rule; the parameters come from the adoption record (`stocks.adopted`).
- **Paper book**: its own capital (share 1.0 = the study's stand-alone sleeve), 100 at inception,
  the selection decided at a rank close D targets from D and trades at the next close (the study's
  lag 1). `step` is the study simulator's per-close loop (`simulate_budgeted` in
  scripts/stock_sleeve_study.py) made incremental so the watch can run it once a day; a test pins it
  to the study's simulator on the same inputs (parity).
- **Costs**: per traded weight `per_side` plus `fixed` per traded line. The default is the public
  declared cost (1.25% of the traded notional per leg, `policy/swing.yaml public_record`), so the
  swing book and the benchmark are compared net of the same declared cost (§8.2).
- **Matched index** (§6.4 a): per swing trade, side x beta_60d x its FF12 sector ETF's return over
  the same window, net of the same declared round trip. **Index hold** (§6.4 b): the swing budget
  held in SPX from the start, one entry leg, no later fees.

`run_benchmark_rank` is the retargeted `council stocks rank`: it ranks with the frozen rule and
writes the benchmark's quarterly selection to `state_dir/benchmark/sq8/<quarter>/` (no policy file,
no broker gate, no trading path).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from council.reference import sleeve as sleeve_rule
from council.reference.report import assert_public_safe

LABEL = "paper benchmark — the mechanical rule that failed its pre-registered gate; tracked, never traded"
CELL = "SQ-8"
NAMES = 8
DECLARED_COST_PCT_PER_LEG = 1.25          # policy/swing.yaml public_record (percent of the position)
INDEX_BUDGET = 0.48                       # the swing budget, as a share of NAV (§3.2)
INCEPTION = 100.0
BENCHMARK_DIR = Path("benchmark") / "sq8"
BOOK_FILE = "book.json"
SELECTION_FILE = "selection.json"
PUBLIC_FILE = "public.md"
_EPS = 1e-12                              # as the study simulator: a smaller trade is no trade


class BenchmarkError(ValueError):
    """The benchmark cannot be computed from what it was given (fail closed: no row recorded)."""


# ------------------------------------------------------------------------------------ parameters
@dataclass(frozen=True)
class SQ8Params:
    names: int = NAMES
    share: float = 1.0                    # the paper sleeve's own capital
    deadband_level: float = 0.25
    min_share: float = 0.04               # min_nav_share / sleeve share_of_nav (the study's run_sleeve)
    per_side: float = DECLARED_COST_PCT_PER_LEG / 100.0
    fixed: float = 0.0

    @property
    def unit(self) -> float:
        return sleeve_rule.unit_weight(self.share, self.names)

    @property
    def threshold(self) -> float:
        return sleeve_rule.drift_threshold(self.unit, self.deadband_level, self.min_share)

    @classmethod
    def from_adopted(cls, rule: Any | None = None, *, declared_cost_pct_per_leg: float = DECLARED_COST_PCT_PER_LEG,
                     fixed: float = 0.0) -> SQ8Params:
        """The adopted rule's constants (`council.stocks.adopted`, fails closed on any disagreement
        with the tagged variants file), with the book on its own capital as the study ran it."""
        if rule is None:
            from council.stocks import adopted

            rule = adopted.adopted_rule()
        if rule.cell != CELL or rule.overlay != "none":
            raise BenchmarkError(f"the adopted rule is {rule.cell}/{rule.overlay}, not {CELL}/none")
        return cls(names=int(rule.names), share=1.0, deadband_level=float(rule.deadband_level),
                   min_share=float(rule.deadband_min_nav_share) / float(rule.sleeve_share),
                   per_side=float(declared_cost_pct_per_leg) / 100.0, fixed=float(fixed))


# ------------------------------------------------------------------------------------ the paper book
@dataclass
class PaperSleeve:
    """The benchmark's state after a close: weights of its own capital, the level each line was last
    traded at, the orders decided at that close (executed at the next), and the NAV (1.0 = start)."""

    nav: float = 1.0
    held: dict[str, float] = field(default_factory=dict)
    held_level: dict[str, float] = field(default_factory=dict)
    pending: dict[str, tuple[float, float]] = field(default_factory=dict)
    selection: tuple[str, ...] = ()
    selection_asof: str | None = None
    day: str | None = None
    peak: float = 1.0

    def to_json(self) -> dict[str, Any]:
        return {"nav": self.nav, "held": self.held, "held_level": self.held_level,
                "pending": {k: list(v) for k, v in self.pending.items()}, "selection": list(self.selection),
                "selection_asof": self.selection_asof, "day": self.day, "peak": self.peak}

    @classmethod
    def from_json(cls, doc: Mapping[str, Any]) -> PaperSleeve:
        return cls(nav=float(doc["nav"]), held={k: float(v) for k, v in doc["held"].items()},
                   held_level={k: float(v) for k, v in doc["held_level"].items()},
                   pending={k: (float(v[0]), float(v[1])) for k, v in doc["pending"].items()},
                   selection=tuple(doc["selection"]), selection_asof=doc.get("selection_asof"),
                   day=doc.get("day"), peak=float(doc.get("peak", doc["nav"])))


@dataclass(frozen=True)
class StepResult:
    day: str
    ret: float                            # the day's net return of the paper sleeve (fraction)
    cost: float                           # cost charged at this close, fraction of NAV
    traded: tuple[str, ...]               # lines traded at this close
    ordered: tuple[str, ...]              # lines ordered at this close (traded at the next)


def _ret(returns: Mapping[str, float], key: str) -> float:
    r = returns.get(key)
    return float(r) if r is not None and math.isfinite(float(r)) else 0.0


def step(book: PaperSleeve, params: SQ8Params, day: str, returns: Mapping[str, float], *,
         selection: Sequence[str] | None = None, selection_asof: str | None = None) -> StepResult:
    """One close of the paper sleeve, in the study simulator's order: grow the held weights by the
    day's returns (a missing or non-finite return is 0, as the study's carried-forward closes), execute
    the orders decided at the previous close and charge their cost, then decide this close's orders
    with the frozen `pending_trades`. A new `selection` (decided at this close) replaces the targets
    from this close on. Mutates `book`."""
    if book.day is not None and day <= book.day:
        raise BenchmarkError(f"benchmark day {day} is not after {book.day}")
    if selection is not None:
        chosen = list(selection)
        sleeve_rule.sleeve_targets(chosen, params.share, params.names, 1.0)   # refuses duplicates / > N
        book.selection, book.selection_asof = tuple(chosen), selection_asof or day
    nav0 = book.nav
    first = book.day is None
    lines = sorted(set(book.held) | set(book.pending) | set(book.selection))
    held = np.array([book.held.get(k, 0.0) for k in lines], dtype=float)
    held_level = np.array([book.held_level.get(k, 0.0) for k in lines], dtype=float)
    if not first:
        rets = np.array([_ret(returns, k) for k in lines], dtype=float)
        growth = 1.0 + float(held @ rets)
        if growth <= 0.0:
            raise BenchmarkError(f"paper sleeve wiped out on {day}")
        held = held * (1.0 + rets) / growth
        book.nav *= growth
    cost = 0.0
    traded: list[str] = []
    if book.pending:
        pend = np.array([k in book.pending for k in lines], dtype=bool)
        new = held.copy()
        for j, k in enumerate(lines):
            if pend[j]:
                new[j] = book.pending[k][0]
                held_level[j] = book.pending[k][1]
        dw = new - held
        moved = np.abs(dw) > _EPS
        cost = float(np.abs(dw).sum() * params.per_side + params.fixed * int(moved.sum()))
        traded = [k for j, k in enumerate(lines) if moved[j]]
        held = new
        book.nav *= 1.0 - cost
    unit = params.unit
    target = np.array([unit if k in book.selection else 0.0 for k in lines], dtype=float)
    level = np.array([1.0 if k in book.selection else 0.0 for k in lines], dtype=float)
    orders = sleeve_rule.pending_trades(held, held_level, target, level, params.threshold,
                                        budget_mask=np.ones(len(lines), dtype=bool), budget=params.share)
    book.pending = {k: (float(target[j]), float(level[j])) for j, k in enumerate(lines) if orders[j]}
    book.held = {k: float(held[j]) for j, k in enumerate(lines) if abs(held[j]) > _EPS or held_level[j] != 0.0}
    book.held_level = {k: float(held_level[j]) for j, k in enumerate(lines) if k in book.held}
    book.day = day
    book.peak = max(book.peak, book.nav)
    return StepResult(day=day, ret=book.nav / nav0 - 1.0, cost=cost, traded=tuple(traded),
                      ordered=tuple(book.pending))


def simulate(calendar: Sequence[Any], decisions: Mapping[Any, Sequence[str]], returns: pd.DataFrame,
             params: SQ8Params) -> tuple[pd.Series, pd.DataFrame]:
    """The paper sleeve over a calendar (NAV from 1.0, weights after each close): each decision
    applies from the first calendar row on or after its date, as the study's `sleeve_plan`."""
    rows = pd.DatetimeIndex(calendar)
    by_row: dict[int, tuple[Any, Sequence[str]]] = {}
    for d in sorted(decisions):
        i0 = int(rows.searchsorted(pd.Timestamp(d)))
        if i0 < len(rows):
            by_row[i0] = (d, decisions[d])            # a later decision on the same row wins, as the plan
    book = PaperSleeve()
    navs, weights = [], []
    for i, t in enumerate(rows):
        rets = returns.loc[t].to_dict() if (i > 0 and t in returns.index) else {}
        sel = by_row.get(i)
        step(book, params, t.strftime("%Y-%m-%d"), rets, selection=None if sel is None else list(sel[1]),
             selection_asof=None if sel is None else str(pd.Timestamp(sel[0]).date()))
        navs.append(book.nav)
        weights.append(dict(book.held))
    return pd.Series(navs, index=rows, name="sq8"), pd.DataFrame(weights, index=rows).fillna(0.0)


def drawdown(book: PaperSleeve) -> float:
    return book.nav / book.peak - 1.0 if book.peak > 0 else 0.0


# ------------------------------------------------------------------------------ the two index lines
def side_sign(side: str) -> float:
    if side not in ("long", "short"):
        raise BenchmarkError(f"unknown side {side!r}")
    return 1.0 if side == "long" else -1.0


def matched_trade_return(side: str, beta: float, sector_etf_return: float, *,
                         declared_cost_pct_per_leg: float = DECLARED_COST_PCT_PER_LEG) -> float:
    """§6.4 (a): side x beta_60d x the trade's FF12 sector ETF return over the same holding window,
    net of the same declared round trip (two legs), as a fraction of the position."""
    for v in (beta, sector_etf_return):
        if not math.isfinite(float(v)):
            raise BenchmarkError("matched index inputs must be finite")
    return side_sign(side) * float(beta) * float(sector_etf_return) - 2.0 * declared_cost_pct_per_leg / 100.0


@dataclass(frozen=True)
class MatchedLeg:
    """One open swing trade on one day, for the matched-index curve (all percent-only)."""

    side: str
    beta: float
    etf_ret: float                        # the sector ETF's return that day (fraction)
    size: float                           # the trade's share of the swing capital
    entry_day: bool = False
    exit_day: bool = False


def matched_index_day(legs: Iterable[MatchedLeg], *,
                      declared_cost_pct_per_leg: float = DECLARED_COST_PCT_PER_LEG) -> float:
    """The matched-index curve's return on the swing capital for one day: every open trade replaced
    by side x beta x its sector ETF, charged the declared cost on its entry and exit days."""
    leg_cost = declared_cost_pct_per_leg / 100.0
    total = 0.0
    for leg in legs:
        total += leg.size * side_sign(leg.side) * leg.beta * leg.etf_ret
        total -= leg.size * leg_cost * (int(leg.entry_day) + int(leg.exit_day))
    return total


def index_hold_day(spx_ret: float | None, *, first_day: bool,
                   declared_cost_pct_per_leg: float = DECLARED_COST_PCT_PER_LEG) -> float | None:
    """§6.4 (b): the swing budget held in SPX from the start; one entry leg on the first day, no
    later fees. Return on the swing capital (x INDEX_BUDGET for NAV)."""
    if first_day:
        return -declared_cost_pct_per_leg / 100.0
    if spx_ret is None or not math.isfinite(float(spx_ret)):
        return None
    return float(spx_ret)


# -------------------------------------------------------------------------------- the daily record
def benchmark_row(book: PaperSleeve, result: StepResult, *, matched_idx_ret: float | None,
                  idx_hold_ret: float | None) -> dict[str, Any]:
    """The `benchmark_days` row for one close (fractions; public-safe detail: index level, drawdown,
    the current names — SEC public data — and what traded)."""
    return {"day": result.day, "sq8_ret": result.ret, "matched_idx_ret": matched_idx_ret,
            "idx_hold_ret": idx_hold_ret,
            "detail": {"sq8_index": round(INCEPTION * book.nav, 6), "sq8_drawdown": round(drawdown(book), 6),
                       "names": list(book.selection), "selection_asof": book.selection_asof,
                       "traded": list(result.traded), "cost": result.cost}}


def record_day(ledger: Any, row: Mapping[str, Any]) -> None:
    """Write one row through the ledger's public API (`Ledger.record_benchmark_day`)."""
    ledger.record_benchmark_day(row["day"], sq8_ret=row["sq8_ret"], matched_idx_ret=row["matched_idx_ret"],
                                idx_hold_ret=row["idx_hold_ret"], detail=row["detail"])


def cumulative(rows: Iterable[Mapping[str, Any]], column: str) -> float | None:
    """Compounded return of one benchmark column over the recorded days (None when never recorded)."""
    nav, seen = 1.0, False
    for r in rows:
        v = r.get(column)
        if v is not None:
            nav *= 1.0 + float(v)
            seen = True
    return nav - 1.0 if seen else None


# ------------------------------------------------------------------------------ state files
def bench_dir(state_dir: Path) -> Path:
    return Path(state_dir) / BENCHMARK_DIR


def load_book(state_dir: Path) -> PaperSleeve | None:
    path = bench_dir(state_dir) / BOOK_FILE
    return PaperSleeve.from_json(json.loads(path.read_text())) if path.exists() else None


def save_book(state_dir: Path, book: PaperSleeve) -> Path:
    path = bench_dir(state_dir) / BOOK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(book.to_json(), sort_keys=True, indent=1))
    tmp.replace(path)
    return path


def load_selections(state_dir: Path) -> dict[str, list[str]]:
    """Every recorded quarterly selection: {rank date: symbols, best first}."""
    out: dict[str, list[str]] = {}
    root = bench_dir(state_dir)
    if not root.is_dir():
        return out
    for path in sorted(root.glob(f"*/{SELECTION_FILE}")):
        doc = json.loads(path.read_text())
        out[doc["asof"]] = list(doc["selected"])
    return dict(sorted(out.items()))


def latest_selection(state_dir: Path, *, on_or_before: str | None = None) -> tuple[str, list[str]] | None:
    sel = {d: s for d, s in load_selections(state_dir).items() if on_or_before is None or d <= on_or_before}
    if not sel:
        return None
    d = max(sel)
    return d, sel[d]


def previous_keys(state_dir: Path, asof: date) -> list[str]:
    """The rank keys of the latest selection before `asof` (the hold buffer's `held`)."""
    root = bench_dir(state_dir)
    docs = [json.loads(p.read_text()) for p in sorted(root.glob(f"*/{SELECTION_FILE}"))] if root.is_dir() else []
    docs = [d for d in docs if d["asof"] < asof.isoformat()]
    if not docs:
        return []
    doc = max(docs, key=lambda d: d["asof"])
    return list(doc.get("keys") or doc["selected"])


def render_public(quarter: str, asof: str, selected: Sequence[str], previous: Sequence[str],
                  sectors: Mapping[str, str], *, cumulative_ret: float | None = None,
                  dd: float | None = None, params: SQ8Params | None = None) -> str:
    """The percent-only public page: label, current names and equal weights, the quarter's changes."""
    p = params or SQ8Params()
    w = f"{100.0 * p.unit:.1f}%"
    lines = [f"# SQ-8 paper benchmark, {quarter}", "", f"_{LABEL}_", "",
             f"Rank date {asof}. Rule {CELL}: sector score, sector quotas, {p.names} names, equal weight, "
             "no overlay. Returns net of the declared cost per leg.", ""]
    if cumulative_ret is not None:
        lines.append(f"Cumulative return since the start: {100.0 * cumulative_ret:.1f}%.")
    if dd is not None:
        lines.append(f"Current drawdown: {100.0 * dd:.1f}%.")
    lines += ["", "| Name | Sector | Weight of the paper sleeve |", "|---|---|---|"]
    lines += [f"| {s} | {sectors.get(s, 'n/a')} | {w} |" for s in selected]
    ins = [s for s in selected if s not in set(previous)]
    outs = [s for s in previous if s not in set(selected)]
    lines += ["", f"In this quarter: {', '.join(ins) or 'none'}.", f"Out this quarter: {', '.join(outs) or 'none'}.", ""]
    text = "\n".join(lines)
    assert_public_safe(text)
    return text


def write_selection(state_dir: Path, asof: date, selected: Sequence[str], sectors: Mapping[str, str], *,
                    kept: Sequence[str] = (), keys: Sequence[str] | None = None,
                    params: SQ8Params | None = None) -> dict[str, Path]:
    """Write the quarter's selection (private JSON, symbols and sectors only) and its public page."""
    from council.stocks.sleeve_file import quarter_of

    quarter = quarter_of(asof)
    prev = latest_selection(state_dir, on_or_before=(asof.isoformat()))
    previous = prev[1] if prev is not None and prev[0] != asof.isoformat() else []
    out = bench_dir(state_dir) / quarter
    out.mkdir(parents=True, exist_ok=True)
    doc = {"cell": CELL, "quarter": quarter, "asof": asof.isoformat(), "selected": list(selected),
           "kept": list(kept), "keys": list(keys if keys is not None else selected), "sectors": {s: sectors.get(s) for s in selected},
           "weight": (params or SQ8Params()).unit, "label": LABEL}
    (out / SELECTION_FILE).write_text(json.dumps(doc, indent=1, sort_keys=True))
    (out / PUBLIC_FILE).write_text(render_public(quarter, asof.isoformat(), selected, previous, sectors,
                                                 params=params))
    return {"selection": out / SELECTION_FILE, "public": out / PUBLIC_FILE}


@dataclass(frozen=True)
class BenchmarkRank:
    quarter: str
    asof: date
    selected: tuple[str, ...]
    kept: tuple[str, ...]
    files: dict[str, Path]

    def report_lines(self) -> list[str]:
        return [f"SQ-8 paper benchmark rank {self.quarter} as of {self.asof} ({LABEL})",
                f"selected: {', '.join(self.selected) or 'none'}",
                f"kept by the hold buffer: {', '.join(self.kept) or 'none'}",
                *(f"file: {p}" for p in self.files.values())]


def run_benchmark_rank(asof: date, build_inputs: Callable[..., Any], *, state_dir: Path,
                       ai_symbols: Sequence[str] = (), config: Any | None = None,
                       rank_fn: Callable[..., Any] | None = None) -> BenchmarkRank:
    """The retargeted `council stocks rank` (§6.2): rank with the frozen rule, holding the previous
    benchmark selection, and write the benchmark's quarterly selection. `build_inputs(asof,
    ai_symbols, config)` -> (RankInputs, sources), as `stocks.commands.RankServices.build_inputs`."""
    from dataclasses import replace

    from council.stocks import rank as rank_mod

    cfg = config if config is not None else rank_mod.RankConfig.selected()
    inputs, _sources = build_inputs(asof, tuple(ai_symbols), cfg)
    inputs = replace(inputs, held=tuple(previous_keys(state_dir, asof)))
    result = (rank_fn or rank_mod.rank)(asof, inputs, cfg)
    sectors = result.eligible["sector"].astype(str).to_dict() if not result.eligible.empty else {}
    symbols = {k: str(result.eligible.at[k, "symbol"]) for k in result.selected} if not result.eligible.empty else {}
    selected = [symbols.get(k, k) for k in result.selected]
    files = write_selection(state_dir, asof, selected, {symbols.get(k, k): sectors.get(k, "n/a") for k in result.selected},
                            kept=[symbols.get(k, k) for k in result.kept], keys=list(result.selected))
    from council.stocks.sleeve_file import quarter_of

    return BenchmarkRank(quarter=quarter_of(asof), asof=asof, selected=tuple(selected),
                         kept=tuple(symbols.get(k, k) for k in result.kept), files=files)
