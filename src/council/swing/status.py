"""`council swing status` (design swing-book.md rev 2, §7.1; SW-6): the operator's one-screen view.

Private (operator terminal only): open trades (side, sessions held, distance to stop/target %, time
stop date, P&L % actual and declared), `open_tp_missing` trades, pending/waiting ideas, the S3 weekly
counter and the S12/S15/S17 lines the caller supplies, the Skeptic test, the pause rule, swing-scoped
blockers and the benchmark lines. Reads the ledger only through its public read APIs; writes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from council.benchmark import sq8
from council.swing import metrics, paper
from council.swing.models import OPEN_STATES
from council.swing.rules import sessions_until

WAITING_IDEA_STATUSES = ("wait", "proposed", "pending", "missed")
DECLARED_COST_PCT_PER_LEG = 1.25


@dataclass(frozen=True)
class OpenTradeLine:
    trade_id: str
    ticker: str
    side: str
    state: str
    sessions_held: int | None
    to_stop_pct: float | None
    to_target_pct: float | None
    time_stop_date: str | None
    pnl_actual_pct: float | None          # gross price move x side (fees are in the private ledger)
    pnl_declared_pct: float | None        # net of the declared cost on both legs

    def line(self) -> str:
        def f(x: float | None, nd: int = 1) -> str:
            return "n/a" if x is None else f"{x:.{nd}f}%"

        return (f"{self.trade_id} {self.ticker} {self.side} [{self.state}] held {self.sessions_held if self.sessions_held is not None else 'n/a'} "
                f"sessions; to stop {f(self.to_stop_pct)}, to target {f(self.to_target_pct)}; time stop "
                f"{self.time_stop_date or 'n/a'}; P&L actual {f(self.pnl_actual_pct, 2)}, declared {f(self.pnl_declared_pct, 2)}")


@dataclass
class SwingStatus:
    today: date
    open_trades: list[OpenTradeLine] = field(default_factory=list)
    tp_missing: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    entries_7d: int = 0
    counters: dict[str, Any] = field(default_factory=dict)
    skeptic: metrics.SkepticTest | None = None
    pause: metrics.PauseDecision | None = None
    funnel: dict[str, metrics.Interval] = field(default_factory=dict)
    benchmark: dict[str, float | None] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        """SW-8 accept: nothing needs the operator (no blocker, no missing TP, no pause)."""
        return not self.blockers and not self.tp_missing and not (self.pause and self.pause.pause)

    def lines(self) -> list[str]:
        out = [f"swing status {self.today.isoformat()}: {'CLEAN' if self.clean else 'NEEDS ATTENTION'}"]
        out.append(f"open trades: {len(self.open_trades)}")
        out += [f"  {t.line()}" for t in self.open_trades]
        out += [f"open_tp_missing: {t}" for t in self.tp_missing]
        out.append(f"waiting / pending ideas: {', '.join(self.waiting) or 'none'}")
        out.append(f"S3 entries in the last 7 days: {self.entries_7d}")
        out += [f"{k}: {v}" for k, v in sorted(self.counters.items())]
        out += [f"blocker: {b}" for b in self.blockers] or ["blockers (swing scope): none"]
        if self.skeptic is not None:
            s = self.skeptic
            out.append(f"skeptic test: {s.status} (n={s.n}, rejected {_r(s.rejected_mean)}, passed "
                       f"{_r(s.passed_mean)}, wait {_r(s.wait_mean)})")
        if self.pause is not None:
            p = self.pause
            out.append(f"pause rule: {'PAUSE' if p.pause else ('ok' if p.applies else 'not yet')} "
                       f"({p.n} closed{'; ' + ', '.join(p.reasons) if p.reasons else ''})")
        for g, iv in self.funnel.items():
            out.append(f"paper {g}: n={iv.n} mean {_r(iv.mean)} [{_r(iv.low)}, {_r(iv.high)}]")
        for k, v in self.benchmark.items():
            out.append(f"benchmark {k}: {'n/a' if v is None else f'{100 * v:.1f}%'}")
        out.append(f"({sq8.LABEL})")
        return out


def _r(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2f}R"


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return 100.0 * abs(a / b - 1.0)


def trade_line(t: Any, today: date, mark: float | None, *,
               declared_cost_pct_per_leg: float = DECLARED_COST_PCT_PER_LEG) -> OpenTradeLine:
    sign = 1.0 if t.side == "long" else -1.0
    held = sessions_until(t.opened_at.date(), today) if t.opened_at is not None else None
    actual = declared = None
    if mark is not None and t.open_rate:
        actual = 100.0 * sign * (mark / t.open_rate - 1.0)
        declared = actual - 2.0 * declared_cost_pct_per_leg
    return OpenTradeLine(trade_id=t.trade_id, ticker=t.ticker, side=t.side, state=t.state, sessions_held=held,
                         to_stop_pct=_pct(mark, t.sl_rate), to_target_pct=_pct(t.tp_rate, mark),
                         time_stop_date=t.time_stop_date, pnl_actual_pct=actual, pnl_declared_pct=declared)


def _closed_trades(rows: Sequence[Any]) -> list[metrics.ClosedTrade]:
    """Closed trades carrying their percent-only outcome in `detail` (written by the watch):
    r_declared, net_ret, size_nav, beta, sector_etf_ret, exit_kind, days_held."""
    out = []
    for t in rows:
        d = t.detail or {}
        if "r_declared" not in d or "net_ret" not in d:
            continue
        out.append(metrics.ClosedTrade(
            r_declared=float(d["r_declared"]), net_ret=float(d["net_ret"]), size_nav=float(d.get("size_nav", 0.0)),
            side=t.side, beta=d.get("beta"), sector_etf_ret=d.get("sector_etf_ret"),
            exit_kind=str(d.get("exit_kind", t.state.removeprefix("closed_"))), days_held=int(d.get("days_held", 0))))
    return out


_VERDICT_OF_GROUP = {"skeptic_rejected": "reject", "skeptic_wait": "wait", "skeptic_wait_debated": "wait"}


def swing_status(ledger: Any, *, today: date, now: datetime | None = None,
                 marks: Mapping[str, float] | None = None, counters: Mapping[str, Any] | None = None,
                 declared_cost_pct_per_leg: float = DECLARED_COST_PCT_PER_LEG,
                 resamples: int = metrics.BOOTSTRAP_RESAMPLES) -> SwingStatus:
    """Build the status from the ledger (read-only). `marks` = last price per ticker (private);
    `counters` = S12/S15/S17 lines the caller computed (fee bps, brake, kill state)."""
    marks = marks or {}
    st = SwingStatus(today=today, counters=dict(counters or {}))
    trades = ledger.swing_trades()
    for t in trades:
        if t.state in OPEN_STATES:
            st.open_trades.append(trade_line(t, today, marks.get(t.ticker),
                                             declared_cost_pct_per_leg=declared_cost_pct_per_leg))
        if t.state == "open_tp_missing":
            st.tp_missing.append(t.trade_id)
    week_ago = (now.date() if now else today) - timedelta(days=7)
    st.entries_7d = sum(1 for t in trades if t.opened_at is not None and t.opened_at.date() > week_ago)
    st.waiting = [f"{i['idea_id']} {i['ticker']} {i['side']} ({i['status']})" for i in ledger.swing_ideas()
                  if i["status"] in WAITING_IDEA_STATUSES]
    st.blockers = list(ledger.swing_blockers())

    rows = ledger.paper_trades()
    outcomes = paper.closed_outcomes(rows)
    st.funnel = metrics.funnel(outcomes, resamples=resamples)
    verdicts = []
    for o in outcomes:
        g = o["group"]
        v = o.get("skeptic_verdict") or _VERDICT_OF_GROUP.get(
            str(g), "pass" if g in ("executed", "pm_passed", "missed") else None)
        if v is not None:
            verdicts.append({"verdict": v, "r_declared": o["r_declared"]})
    st.skeptic = metrics.skeptic_test(verdicts)
    st.pause = metrics.pause_rule(_closed_trades([t for t in trades if t.state.startswith("closed_")]),
                                  declared_cost_pct_per_leg=declared_cost_pct_per_leg)
    days = ledger.benchmark_days()
    st.benchmark = {"sq8_cumulative": sq8.cumulative(days, "sq8_ret"),
                    "matched_index_cumulative": sq8.cumulative(days, "matched_idx_ret"),
                    "index_hold_cumulative": sq8.cumulative(days, "idx_hold_ret")}
    return st
