"""What if we had bought every idea? (`council paper whatif`, paper state only.)

Every idea the swing council saw is in the paper ledger (`paper_trades`, one row per idea and
group). This module re-walks each one on completed daily bars with the one paper convention
(`council.swing.paper.evaluate`: entry at the slot reference, exits from the next completed session,
a gap through the stop books at the open, a bar touching stop and target books the stop, the time
stop books the close) and the declared cost on both legs, then aggregates by group, by drop code
and by Skeptic verdict.

- **Open ideas** are marked at the last completed close from the entry session on (`paper.mark`;
  the entry session's close marks, it never exits); they count in the averages
  as marks, and the open/resolved counts say how many.
- **Reference**: ideas whose reference was the previous close (the cycle flag
  `paper_reference_last_close`, or `ref_source` in the row) are flagged `prior_close`.
- **Dedupe**: the same ticker + side + slot (origin cycle) across replays counts once (the first row).
- **Corporate actions**: a split or spin-off makes adjusted bars disagree with the slot reference.
  An idea is `corporate_action` (listed, excluded from every average) when the reference lies far
  outside the entry session's adjusted range (the adjustment mismatch), or when a one-day move beyond
  +-40% coincides with an 8-K item 2.01 / 3.03 / 5.03 or a known corporate-action flag.
- Percent only: nothing here returns a price (the reference stays in the private row).
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from council.swing import paper

BIG_MOVE = 0.40                    # a one-day move beyond +-40% (close to close, or the open gap)
ADJ_MISMATCH = 0.25                # reference > 25% outside the entry session's adjusted range
CA_8K_ITEMS = ("2.01", "3.03", "5.03")
EVENT_WINDOW_DAYS = 3              # an event up to 3 calendar days before the move (or the same day)
KNOWN_EVENTS_FILE = "swing/corporate_actions.json"   # private, operator-kept: {ticker: [{day, code}]}
STATUSES = ("open", "stop", "target", "time", "corporate_action")


@dataclass(frozen=True)
class WhatIf:
    """One idea's what-if outcome (percent only; `paper_id`/`ref` are private ledger ids)."""

    paper_id: str
    ref: str
    cycle_id: str
    ticker: str
    side: str
    group: str
    drop_code: str | None
    skeptic_verdict: str | None
    ref_source: str                 # slot | prior_close
    entry_day: date
    status: str                     # open | stop | target | time | corporate_action
    exit_reason: str | None         # the paper exit reason (stop_gap, target, time, ...)
    days_held: int
    gross_pct: float | None
    net_pct: float | None
    corporate_action: str | None = None

    @property
    def priced(self) -> bool:
        return self.status != "corporate_action" and self.net_pct is not None


# ------------------------------------------------------------------------------------ inputs
def dedupe(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """One row per ticker + side + slot (origin cycle): the first (opened, created, id)."""
    keep: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for r in sorted(rows, key=lambda r: (str(r.get("opened_at") or ""), str(r.get("created_at") or ""),
                                         str(r.get("paper_id") or ""))):
        key = (str(r.get("ticker") or "").upper(), str(r.get("side") or ""), str(r.get("origin_cycle") or ""))
        keep.setdefault(key, r)
    return list(keep.values())


def ref_source(row: Mapping[str, Any], cycle_flags: Mapping[str, Sequence[str]] | None = None) -> str:
    """`prior_close` when the row says so, or (older rows) when its cycle used the last close for
    any idea (flag `paper_reference_last_close`: per-idea source unknown, so flagged conservatively)."""
    rec = row.get("record") or {}
    src = rec.get("ref_source")
    if src in ("slot", "prior_close"):
        return str(src)
    flags = (cycle_flags or {}).get(str(row.get("origin_cycle") or "")) or ()
    return "prior_close" if "paper_reference_last_close" in flags else "slot"


def normalise_code(code: str | None) -> str | None:
    """A drop code for grouping: a trailing count becomes N (not_best_3 -> not_best_N)."""
    if not code:
        return None
    return re.sub(r"_\d+$", "_N", str(code))


def load_known_events(state_dir: Path) -> dict[str, list[tuple[date, str]]]:
    """The private known corporate-action flags (`<paper state>/swing/corporate_actions.json`),
    {ticker: [(day, code)]}; {} when absent or unreadable."""
    path = Path(state_dir) / KNOWN_EVENTS_FILE
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, list[tuple[date, str]]] = {}
    for tk, items in (raw.items() if isinstance(raw, dict) else []):
        for it in items if isinstance(items, list) else []:
            try:
                out.setdefault(str(tk).upper(), []).append((date.fromisoformat(str(it["day"])), str(it.get("code") or "flag")))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def is_ca_event(code: str) -> bool:
    """An 8-K item 2.01 / 3.03 / 5.03, or any other known corporate-action flag."""
    c = str(code)
    if c.upper().startswith("8-K") or "item" in c.lower():
        return any(item in c for item in CA_8K_ITEMS)
    return bool(c)


# ------------------------------------------------------------------------------------ bar walk
def _rows(bars: pd.DataFrame) -> list[tuple[date, float, float, float, float]]:
    days = paper._bar_days(bars)
    return [(d, float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"]))
            for d, (_, b) in zip(days, bars.iterrows(), strict=True)]


def corporate_action(idea: paper.PaperIdea, bars: pd.DataFrame | None, *,
                     events: Sequence[tuple[date, str]] = (), until: date | None = None) -> str | None:
    """A code when the idea's window holds a corporate-action artefact, else None:
    `adjustment_mismatch` (the slot reference far outside both the entry session's adjusted bar and
    the session before it, the prior close's) or `big_move:<event code>`."""
    if bars is None or bars.empty:
        return None
    rows = _rows(bars)
    base = [r for r in rows if r[0] <= idea.entry_day]
    ref = idea.entry_ref
    if base and all(ref > hi * (1.0 + ADJ_MISMATCH) or ref < lo * (1.0 - ADJ_MISMATCH)
                    for _, _, hi, lo, _ in base[-2:]):
        return "adjustment_mismatch"
    # From the slot reference on, including the entry session itself (a prior-close reference sees a
    # spin-off or split on the entry day). A move beyond BIG_MOVE is a corporate-action artefact:
    # named by its 8-K event when one is known, else `big_move:unverified` (excluded either way, and
    # listed, so a real crash is never silently averaged as a corporate action without being shown).
    prev = ref if ref and ref > 0 else (base[-1][4] if base else None)
    for d, o, _, _, c in rows:
        if d < idea.entry_day:
            continue
        if until is not None and d > until:
            break
        if prev is not None and prev > 0:
            move = max(abs(c / prev - 1.0), abs(o / prev - 1.0))
            if move > BIG_MOVE:
                hit = next((code for ed, code in events
                            if d - timedelta(days=EVENT_WINDOW_DAYS) <= ed <= d and is_ca_event(code)), None)
                return f"big_move:{hit or 'unverified'}"
        prev = c
    return None


def _status(reason: str) -> str:
    return reason.split("_", 1)[0]            # stop_gap -> stop, target_gap -> target


def evaluate_row(row: Mapping[str, Any], bars: pd.DataFrame | None, *, source: str = "slot",
                 events: Sequence[tuple[date, str]] = (),
                 declared_cost_pct_per_leg: float = paper.DECLARED_COST_PCT_PER_LEG) -> WhatIf:
    """One paper row walked on its completed daily bars."""
    idea = paper.idea_from_row(row)
    rec = row.get("record") or {}
    base = {"paper_id": str(row.get("paper_id") or ""), "ref": idea.ref, "cycle_id": str(row.get("origin_cycle") or ""),
            "ticker": idea.ticker, "side": idea.side, "group": idea.group,
            "drop_code": rec.get("drop_code"), "skeptic_verdict": rec.get("skeptic_verdict"),
            "ref_source": source, "entry_day": idea.entry_day}
    b = bars if bars is not None else pd.DataFrame()
    out = paper.evaluate(idea, b, declared_cost_pct_per_leg=declared_cost_pct_per_leg) if not b.empty else None
    ca = corporate_action(idea, b, events=events, until=out.exit_day if out is not None else None)
    if ca is not None:
        return WhatIf(**base, status="corporate_action", exit_reason=None, days_held=0, gross_pct=None,
                      net_pct=None, corporate_action=ca)
    if out is not None:
        return WhatIf(**base, status=_status(out.exit_reason), exit_reason=out.exit_reason, days_held=out.days_held,
                      gross_pct=100.0 * out.gross_ret, net_pct=100.0 * out.net_ret)
    # open: marked at the last completed close from the entry session on (the entry session's close
    # counts for the mark, never for an exit); days held = completed sessions after the entry session
    seen = [r for r in (_rows(b) if not b.empty else []) if r[0] >= idea.entry_day]
    if not seen:
        return WhatIf(**base, status="open", exit_reason=None, days_held=0, gross_pct=None, net_pct=None)
    m = paper.mark(idea, seen[-1][4], declared_cost_pct_per_leg=declared_cost_pct_per_leg)
    return WhatIf(**base, status="open", exit_reason=None, days_held=sum(1 for r in seen if r[0] > idea.entry_day),
                  gross_pct=m["gross_pct"], net_pct=m["net_pct"])


def whatif(rows: Iterable[Mapping[str, Any]], bars_by_ticker: Mapping[str, pd.DataFrame], *,
           cycle_flags: Mapping[str, Sequence[str]] | None = None,
           events: Mapping[str, Sequence[tuple[date, str]]] | None = None,
           declared_cost_pct_per_leg: float = paper.DECLARED_COST_PCT_PER_LEG) -> list[WhatIf]:
    """Every deduped paper row's what-if outcome, oldest first. A row that cannot be tracked
    (missing reference, bad distances) is skipped."""
    out = []
    for row in dedupe(rows):
        tk = str(row.get("ticker") or "")
        try:
            out.append(evaluate_row(row, bars_by_ticker.get(tk), source=ref_source(row, cycle_flags),
                                    events=(events or {}).get(tk.upper(), ()),
                                    declared_cost_pct_per_leg=declared_cost_pct_per_leg))
        except (paper.PaperError, KeyError, TypeError, ValueError):
            continue
    return out


# ------------------------------------------------------------------------------------ aggregates
def aggregate(items: Sequence[WhatIf], key: str) -> list[dict[str, Any]]:
    """Per value of `key` (group | drop_code | skeptic_verdict): n priced, mean / median net %,
    mean gross %, hit rate % (net > 0), open / resolved / excluded (corporate action) counts.
    Open ideas count at their mark."""
    buckets: dict[str, list[WhatIf]] = {}
    for it in items:
        k = getattr(it, key)
        k = normalise_code(k) if key == "drop_code" else k
        buckets.setdefault(str(k) if k else "none", []).append(it)
    out = []
    for k, xs in buckets.items():
        priced = [x for x in xs if x.priced]
        nets = [float(x.net_pct) for x in priced]                      # type: ignore[arg-type]
        gross = [float(x.gross_pct) for x in priced if x.gross_pct is not None]
        out.append({
            "key": k, "n": len(priced),
            "mean_net_pct": round(statistics.fmean(nets), 2) if nets else None,
            "median_net_pct": round(statistics.median(nets), 2) if nets else None,
            "mean_gross_pct": round(statistics.fmean(gross), 2) if gross else None,
            "hit_rate_pct": round(100.0 * sum(1 for v in nets if v > 0) / len(nets), 1) if nets else None,
            "open": sum(1 for x in xs if x.status == "open"),
            "resolved": sum(1 for x in xs if x.status in ("stop", "target", "time")),
            "excluded": sum(1 for x in xs if x.status == "corporate_action"),
        })
    return sorted(out, key=lambda a: (-a["n"] - a["excluded"], a["key"]))


def summary(items: Sequence[WhatIf]) -> dict[str, Any]:
    return {"ideas": len(items), "groups": aggregate(items, "group"), "drop_codes": aggregate(items, "drop_code"),
            "verdicts": aggregate(items, "skeptic_verdict"),
            "corporate_actions": [{"ticker": x.ticker, "side": x.side, "cycle_id": x.cycle_id,
                                   "code": x.corporate_action} for x in items if x.status == "corporate_action"]}


def as_dict(item: WhatIf) -> dict[str, Any]:
    d = asdict(item)
    d["entry_day"] = item.entry_day.isoformat()
    for k in ("gross_pct", "net_pct"):
        if d[k] is not None:
            d[k] = round(float(d[k]), 2) if math.isfinite(d[k]) else None
    return d


# ------------------------------------------------------------------------------------ glue
def cycle_flags_of(ledger: Any, cycle_ids: Iterable[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for cid in set(cycle_ids):
        try:
            rec = ledger.get_cycle(cid) or {}
        except Exception:  # noqa: BLE001 - no record: no flag
            rec = {}
        out[cid] = [str(f) for f in rec.get("flags") or []]
    return out


def first_entry_day(rows: Sequence[Mapping[str, Any]]) -> date | None:
    days = []
    for r in rows:
        try:
            days.append(date.fromisoformat((r.get("record") or {})["entry_day"]))
        except (KeyError, TypeError, ValueError):
            continue
    return min(days, default=None)


def run(ledger: Any, daily_bars: Any, *, state_dir: Path | None = None,
        declared_cost_pct_per_leg: float = paper.DECLARED_COST_PCT_PER_LEG) -> tuple[list[WhatIf], date | None]:
    """Read every paper row, fetch completed daily bars (`daily_bars(tickers, since_day)` ->
    {ticker: bars}) and walk them. Returns (outcomes, the last completed session in the bars)."""
    rows = dedupe(ledger.paper_trades())
    if not rows:
        return [], None
    since = first_entry_day(rows) or date.today()
    bars = daily_bars(sorted({str(r["ticker"]) for r in rows}), since) or {}
    return whatif(rows, bars, cycle_flags=cycle_flags_of(ledger, (str(r.get("origin_cycle")) for r in rows)),
                  events=load_known_events(state_dir) if state_dir is not None else {},
                  declared_cost_pct_per_leg=declared_cost_pct_per_leg), last_session(bars)


def last_session(bars_by_ticker: Mapping[str, pd.DataFrame]) -> date | None:
    days = [paper._bar_days(b)[-1] for b in bars_by_ticker.values() if b is not None and not b.empty]
    return max(days, default=None)


def private_canaries(rows: Iterable[Mapping[str, Any]]) -> list[float]:
    """The slot reference prices, as leak-scan canaries (never written)."""
    return sorted({round(float(r["entry_ref"]), 2) for r in rows
                   if isinstance(r.get("entry_ref"), int | float) and float(r["entry_ref"]) >= 50.0})
