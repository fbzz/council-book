"""Swing PM aggregation (design swing-book.md rev 2, §1.7; SW-3). Same spirit as
`deliberation/aggregate.py`: a real vote of the replicates, never an average of intentions.

- An ENTRY needs `enter` from a strict majority of the replicates that ran (2 of 3; after the
  budget drop to one replicate, 1 of 1 - the Skeptic's `pass`, required for any idea to reach the
  PM, is then the second key). Its stop / target / time stop are the medians of the agreeing
  replicates (each clipped again later by `swing/rules.py`, S5-S7).
- An EXIT of an open trade needs the same majority; otherwise hold.
- A replicate that failed validation, or said nothing about a ref, counts as `pass` / `hold`.
- Entries are ordered by agreeing votes, then the Scout's order, and capped at `max_entries` (the
  room S1-S4 leave); an entry beyond the cap becomes `pass` with the flag `no_room`.

Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import median

from council.swing.models import AggregatedSwingAction, SwingAction


@dataclass
class SwingAggregate:
    actions: list[AggregatedSwingAction] = field(default_factory=list)   # one per ref, input order
    flags: list[str] = field(default_factory=list)
    ranked: list[str] = field(default_factory=list)                        # entry refs, priority order

    def entries(self) -> list[AggregatedSwingAction]:
        """Entries in priority order (agreeing votes, then the Scout's order)."""
        by_ref = {a.ref: a for a in self.actions if a.action == "enter"}
        return [by_ref[r] for r in self.ranked if r in by_ref]

    def exits(self) -> list[AggregatedSwingAction]:
        return [a for a in self.actions if a.action == "exit"]


def _median(values: Sequence[float | int | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return float(median(vals)) if vals else None


def aggregate_actions(
    replicates: Sequence[list[SwingAction] | None],
    *,
    idea_refs: Sequence[str],
    trade_refs: Sequence[str],
    max_entries: int | None = None,
) -> SwingAggregate:
    """`replicates`: each replicate's accepted actions, or None for a failed one. `idea_refs` in the
    Scout's order."""
    n = len(replicates)
    if n < 1:
        raise ValueError("at least one PM replicate is required")
    failed = sum(1 for r in replicates if r is None)
    by_ref: dict[str, list[SwingAction]] = {}
    for rep in replicates:
        for a in rep or []:
            by_ref.setdefault(a.ref, []).append(a)
    out = SwingAggregate()
    need = n // 2 + 1

    entries: list[tuple[int, int, AggregatedSwingAction]] = []
    for order, ref in enumerate(idea_refs):
        agree = [a for a in by_ref.get(ref, []) if a.action == "enter"]
        if len(agree) >= need:
            ts = _median([a.time_stop_days for a in agree])
            agg = AggregatedSwingAction(
                ref=ref, action="enter", votes_for=len(agree), replicates=n, failed_replicates=failed,
                stop_pct=_median([a.stop_pct for a in agree]), target_pct=_median([a.target_pct for a in agree]),
                time_stop_days=int(ts) if ts is not None else None)
            entries.append((-len(agree), order, agg))
        else:
            out.actions.append(AggregatedSwingAction(ref=ref, action="pass", votes_for=n - len(agree),
                                                     replicates=n, failed_replicates=failed))
    entries.sort(key=lambda t: (t[0], t[1]))
    room = len(entries) if max_entries is None else max(0, max_entries)
    for i, (_, _, agg) in enumerate(entries):
        if i < room:
            out.actions.append(agg)
            out.ranked.append(agg.ref)
        else:
            out.flags.append(f"no_room:{agg.ref}")
            out.actions.append(AggregatedSwingAction(ref=agg.ref, action="pass", votes_for=n - agg.votes_for,
                                                     replicates=n, failed_replicates=failed))
    for ref in trade_refs:
        agree = [a for a in by_ref.get(ref, []) if a.action == "exit"]
        if len(agree) >= need:
            out.actions.append(AggregatedSwingAction(ref=ref, action="exit", votes_for=len(agree),
                                                     replicates=n, failed_replicates=failed))
        else:
            out.actions.append(AggregatedSwingAction(ref=ref, action="hold", votes_for=n - len(agree),
                                                     replicates=n, failed_replicates=failed))
    order = {r: i for i, r in enumerate([*idea_refs, *trade_refs])}
    out.actions.sort(key=lambda a: order[a.ref])
    return out
