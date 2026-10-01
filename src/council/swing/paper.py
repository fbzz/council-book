"""One paper convention for every swing idea group (design swing-book.md rev 2, §8.2; SW-6).

Every idea is paper-tracked the same way, executed trades included, so the groups compare like with
like; real fills are measured separately as slippage against the same reference (`slippage`).

- **Entry**: the slot-time reference price (the SIP minute-bar close at the decision's seal time),
  on the slot's session. The entry session's daily bar is never used for exits (part of it happened
  before the entry); exits start on the next completed daily bar.
- **Exits** on completed daily bars with the idea's stop, target and time stop:
  - a **gap through the stop** books at that session's **open** (not at the stop);
  - an open beyond the target books at the open (the same rule on the favourable side);
  - a bar touching **both** stop and target books the **stop**;
  - otherwise the stop, then the target; at the close of the time-stop session, the close.
- **Cost**: the declared cost on every leg (1.25% of the position per leg, `policy/swing.yaml
  public_record`), so `r_declared` = (net return) / (planned stop distance).

Groups (§8.2 funnel value): executed, pm_passed, skeptic_rejected, skeptic_wait,
skeptic_wait_debated (a supported Skeptic wait the debate + PM heard and did not enter), code_dropped
(eligible names only), paper_only (paper-only setups), missed. Percent-only outputs: the reference
price is private (it stays in the ledger row, never in a public record).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

import pandas as pd

DECLARED_COST_PCT_PER_LEG = 1.25
GROUPS = ("executed", "pm_passed", "skeptic_rejected", "skeptic_wait", "skeptic_wait_debated", "code_dropped",
          "paper_only", "missed")
Group = Literal["executed", "pm_passed", "skeptic_rejected", "skeptic_wait", "skeptic_wait_debated", "code_dropped",
                "paper_only",
                "missed"]
ExitReason = Literal["stop", "stop_gap", "target", "target_gap", "time"]


class PaperError(ValueError):
    """A paper idea cannot be tracked as given (a missing reference, a bad group or distance)."""


@dataclass(frozen=True)
class PaperIdea:
    ref: str                              # idea:<id> or trade:<id>
    ticker: str
    side: str                             # long | short
    group: str
    entry_day: date                       # the slot's US session
    entry_ref: float                      # slot-time reference price (private)
    stop_pct: float                       # fraction, distance from entry
    target_pct: float
    time_stop_day: date                   # the last session held; exit at its close

    def __post_init__(self) -> None:
        if self.group not in GROUPS:
            raise PaperError(f"unknown paper group {self.group!r}")
        if self.side not in ("long", "short"):
            raise PaperError(f"unknown side {self.side!r}")
        if not (math.isfinite(self.entry_ref) and self.entry_ref > 0):
            raise PaperError("the slot-time reference price is missing")
        if not (0 < self.stop_pct < 1 and self.target_pct > 0):
            raise PaperError("stop and target distances must be positive fractions")
        if self.time_stop_day < self.entry_day:
            raise PaperError("the time stop is before the entry")

    @property
    def sign(self) -> float:
        return 1.0 if self.side == "long" else -1.0

    @property
    def stop_price(self) -> float:
        return self.entry_ref * (1.0 - self.sign * self.stop_pct)

    @property
    def target_price(self) -> float:
        return self.entry_ref * (1.0 + self.sign * self.target_pct)


@dataclass(frozen=True)
class PaperOutcome:
    ref: str
    group: str
    side: str
    exit_day: date
    exit_reason: str
    gross_ret: float                      # side x (exit / entry - 1), fraction of the position
    net_ret: float                        # gross minus the declared cost on both legs
    r_declared: float                     # net_ret / stop_pct
    days_held: int                        # sessions after the entry session up to the exit

    def public(self) -> dict[str, Any]:
        """Percent-only view (no price)."""
        return {"ref": self.ref, "group": self.group, "side": self.side, "exit_day": self.exit_day.isoformat(),
                "exit_reason": self.exit_reason, "gross_pct": round(100 * self.gross_ret, 4),
                "net_pct": round(100 * self.net_ret, 4), "r_declared": round(self.r_declared, 4),
                "days_held": self.days_held}


def _bar_days(bars: pd.DataFrame) -> list[date]:
    out = []
    for t in bars.index:
        ts = pd.Timestamp(t)
        out.append((ts.tz_convert("UTC") if ts.tzinfo else ts).date())
    return out


def outcome_of(idea: PaperIdea, exit_day: date, exit_price: float, reason: str, days_held: int, *,
               declared_cost_pct_per_leg: float = DECLARED_COST_PCT_PER_LEG) -> PaperOutcome:
    gross = idea.sign * (exit_price / idea.entry_ref - 1.0)
    net = gross - 2.0 * declared_cost_pct_per_leg / 100.0
    return PaperOutcome(ref=idea.ref, group=idea.group, side=idea.side, exit_day=exit_day, exit_reason=reason,
                        gross_ret=gross, net_ret=net, r_declared=net / idea.stop_pct, days_held=days_held)


def evaluate(idea: PaperIdea, bars: pd.DataFrame, *,
             declared_cost_pct_per_leg: float = DECLARED_COST_PCT_PER_LEG) -> PaperOutcome | None:
    """The paper outcome on completed daily bars (columns open/high/low/close, one row per session),
    or None while the idea is still open (no exit and the time-stop session not yet completed)."""
    if bars is None or bars.empty:
        return None
    stop, target = idea.stop_price, idea.target_price
    held = 0
    for day, (_, bar) in zip(_bar_days(bars), bars.iterrows(), strict=True):
        if day <= idea.entry_day:
            continue
        held += 1
        o, h, lo, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
        if idea.side == "long":
            gap_stop, gap_target = o <= stop, o >= target
            hit_stop, hit_target = lo <= stop, h >= target
        else:
            gap_stop, gap_target = o >= stop, o <= target
            hit_stop, hit_target = h >= stop, lo <= target
        found: tuple[float, str] | None = None
        if gap_stop:
            found = (o, "stop_gap")
        elif gap_target:
            found = (o, "target_gap")
        elif hit_stop:                                   # also when the bar touches both
            found = (stop, "stop")
        elif hit_target:
            found = (target, "target")
        elif day >= idea.time_stop_day:
            found = (c, "time")
        if found is not None:
            return outcome_of(idea, day, found[0], found[1], held,
                              declared_cost_pct_per_leg=declared_cost_pct_per_leg)
    return None


def mark(idea: PaperIdea, last_close: float, *,
         declared_cost_pct_per_leg: float = DECLARED_COST_PCT_PER_LEG) -> dict[str, float]:
    """An open paper idea marked at a close: return net of both declared legs, R, distances (%)."""
    gross = idea.sign * (last_close / idea.entry_ref - 1.0)
    net = gross - 2.0 * declared_cost_pct_per_leg / 100.0
    return {"gross_pct": 100 * gross, "net_pct": 100 * net, "r_declared": net / idea.stop_pct,
            "to_stop_pct": 100 * abs(last_close / idea.stop_price - 1.0),
            "to_target_pct": 100 * abs(idea.target_price / last_close - 1.0)}


def slippage(fill: float, ref: float, side: str, stop_pct: float, *, leg: str = "entry") -> dict[str, float]:
    """Private: a real fill against the slot-time reference, in % (positive = worse than the
    reference) and in R of the planned stop distance."""
    if not (ref > 0 and fill > 0 and stop_pct > 0):
        raise PaperError("slippage needs positive prices and stop distance")
    sign = 1.0 if side == "long" else -1.0
    worse = sign * (fill / ref - 1.0) if leg == "entry" else -sign * (fill / ref - 1.0)
    return {"pct": 100.0 * worse, "r": worse / stop_pct}


# ------------------------------------------------------------------------------------ ledger glue
def paper_id(ref: str, group: str) -> str:
    return f"paper:{ref}:{group}"


def track(ledger: Any, idea: PaperIdea, *, origin_cycle: str, opened_at: datetime,
          skeptic_verdict: str | None = None, drop_code: str | None = None) -> str:
    """Record a paper idea through the ledger's public API (`Ledger.add_paper_trade`). `drop_code`:
    the idea's seal-time drop code (a code only; None when it was not dropped)."""
    pid = paper_id(idea.ref, idea.group)
    ledger.add_paper_trade(
        pid, origin_cycle=origin_cycle, ticker=idea.ticker, side=idea.side, opened_at=opened_at,
        idea_id=idea.ref if idea.ref.startswith("idea:") else None, entry_ref=idea.entry_ref,
        stop_pct=idea.stop_pct, target_pct=idea.target_pct, time_stop_date=idea.time_stop_day.isoformat(),
        record={"group": idea.group, "ref": idea.ref, "entry_day": idea.entry_day.isoformat(),
                "skeptic_verdict": skeptic_verdict, "drop_code": drop_code})
    return pid


def idea_from_row(row: Mapping[str, Any]) -> PaperIdea:
    rec = row.get("record") or {}
    return PaperIdea(ref=str(rec.get("ref") or row.get("idea_id") or row["paper_id"]), ticker=row["ticker"],
                     side=row["side"], group=str(rec.get("group")),
                     entry_day=date.fromisoformat(rec["entry_day"]), entry_ref=float(row["entry_ref"]),
                     stop_pct=float(row["stop_pct"]), target_pct=float(row["target_pct"]),
                     time_stop_day=date.fromisoformat(row["time_stop_date"]))


def settle(ledger: Any, bars_by_ticker: Mapping[str, pd.DataFrame], *,
           declared_cost_pct_per_leg: float = DECLARED_COST_PCT_PER_LEG) -> list[PaperOutcome]:
    """Close every open paper idea whose outcome is known on the completed bars given (the watch,
    once a day after the US close; code only). Returns the outcomes closed now."""
    closed = []
    for row in ledger.paper_trades(status="open"):
        idea = idea_from_row(row)
        out = evaluate(idea, bars_by_ticker.get(idea.ticker, pd.DataFrame()),
                       declared_cost_pct_per_leg=declared_cost_pct_per_leg)
        if out is None:
            continue
        ledger.close_paper_trade(row["paper_id"], exit_reason=out.exit_reason, ret_pct=100.0 * out.net_ret,
                                 closed_at=datetime(out.exit_day.year, out.exit_day.month, out.exit_day.day,
                                                    20, 0, tzinfo=UTC))
        closed.append(out)
    return closed


def closed_outcomes(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Closed paper rows as {group, r_declared, net_ret, exit_reason} (ret_pct is net, in %)."""
    out = []
    for r in rows:
        if r.get("status") != "closed" or r.get("ret_pct") is None or not r.get("stop_pct"):
            continue
        net = float(r["ret_pct"]) / 100.0
        rec = r.get("record") or {}
        out.append({"group": rec.get("group"), "skeptic_verdict": rec.get("skeptic_verdict"), "net_ret": net,
                    "r_declared": net / float(r["stop_pct"]), "exit_reason": r.get("exit_reason")})
    return out
