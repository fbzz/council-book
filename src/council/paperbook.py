"""The paper book: a persisted paper broker for `council cycle --paper` (paper runs only).

A paper run has no broker token, so before this module every paper cycle took the current book as
FLAT and the paper portfolio never built. The paper book closes that gap:

- **State**: `<state>/paper/book.json` (the paper run's own state dir) plus an append-only
  `book_ledger.jsonl` of every paper fill, mark and exit, both 0600. Amounts there are PRIVATE
  (they scale with the funded NAV); nothing in this module's public outputs carries one.
- **Snapshot**: `snapshot()` is an `ExposureSnapshot` exactly like a broker snapshot - equity,
  signed weights per core line and per swing line (`SW_<ticker>`), gross / net - with no broker
  positions (no position ids exist on paper) and the flag `paper_book`. The cycle hands it to the
  engine, the council's current levels and the swing stage, so the paper run exercises the whole
  book (S18 core rescale, the swing budget, whole-book engine limits).
- **Funding**: the start NAV is the private `funded_real_nav_usd` (`state_dir/account/swing.json`,
  or the base state dir's); missing -> the policy's assumed virtual NAV (flag
  `paper_book_assumed_nav`). Paper returns are NAV-invariant, so only the % return is public.
- **Execution** (after each paper cycle; no approval: paper): the decision's core target weights
  are traded at the slot's reference prices (the cycle's last completed closes) with the DECLARED
  policy cost per side (`per_side_bps` + the private fixed fee as bps of NAV, the engine's R14
  model), and the surviving swing entries open at the paper reference already used for paper
  tracking with the declared 1.25% per leg (`swing.paper`).
- **Marks** (each paper cycle start): core lines at the last completed close; open swing trades run
  `swing.paper.evaluate` on the completed daily bars (gap/stop/target/time-stop conventions) and are
  otherwise marked at the last close.

`paper_book_public(state)` is the percent-only view the public paper record and the site render as
"the portfolio" (weights %, swing trades with side / setup / stop % / target % / days held / return %
net of the declared cost, the core / swing / cash split and the paper return % since start).

Live is untouched: `SWING_BOOK_LIVE` keeps its meaning and a live or broker-connected context never
loads a paper book (`is_paper_run`).
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

BOOK_FILE = "book.json"
LEDGER_FILE = "book_ledger.jsonl"
PAPER_STATE_DIR = "paper"
EPS = 1e-9
VERSION = 1


def is_paper_run(ctx: Any) -> bool:
    """The same test as `cycle.swing_wide_of`: not live, no publisher, no broker, no notifier, the
    state dir named `paper`."""
    return (getattr(getattr(ctx, "settings", None), "mode", "live") != "live" and ctx.publisher is None
            and getattr(ctx.sources, "broker", None) is None and getattr(ctx, "notifier", None) is None
            and Path(ctx.state_dir).name == PAPER_STATE_DIR)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _finite(x: Any) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


@dataclass
class PaperSwing:
    trade_id: str
    ticker: str
    side: str
    line: str
    notional: float                       # PRIVATE (position size at entry)
    entry_ref: float                      # PRIVATE
    stop_pct: float                       # fraction
    target_pct: float
    entry_day: str
    time_stop_day: str
    setup: str | None = None
    last_price: float | None = None       # PRIVATE
    status: str = "open"
    exit_reason: str | None = None
    exit_day: str | None = None
    net_ret: float | None = None          # fraction of the position, net of both declared legs
    days_held: int = 0
    opened_cycle: str | None = None
    sigma_daily: float | None = None
    beta: float | None = None

    @property
    def sign(self) -> float:
        return 1.0 if self.side == "long" else -1.0

    def pnl(self) -> float:
        """Unrealised P&L at the last mark (the entry cost was already paid in cash)."""
        px = self.last_price if self.last_price else self.entry_ref
        return self.notional * self.sign * (px / self.entry_ref - 1.0)

    def value(self) -> float:
        """Signed exposure at the last mark."""
        px = self.last_price if self.last_price else self.entry_ref
        return self.sign * self.notional * px / self.entry_ref


@dataclass
class PaperBook:
    state_dir: Path
    started_at: str | None = None
    start_nav: float = 0.0                # PRIVATE
    cash: float = 0.0                     # PRIVATE
    core: dict[str, dict[str, float]] = field(default_factory=dict)   # line -> {qty, price} PRIVATE
    swing: dict[str, PaperSwing] = field(default_factory=dict)
    last_cycle: str | None = None
    marked_at: str | None = None
    funding: str = "funded"
    declared_swing_pct_per_leg: float = 1.25
    peak_nav: float = 0.0                 # PRIVATE (the paper lifetime peak, for S17 / the engine)
    build_phase: dict[str, Any] | None = None   # the initial-build phase (`cycle.build_phase_open`)

    # ------------------------------------------------------------------ persistence
    @classmethod
    def path_of(cls, state_dir: Path) -> Path:
        return Path(state_dir) / BOOK_FILE

    @classmethod
    def load(cls, state_dir: Path) -> PaperBook | None:
        path = cls.path_of(state_dir)
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        book = cls(state_dir=Path(state_dir), started_at=raw.get("started_at"),
                   start_nav=float(raw.get("start_nav") or 0.0), cash=float(raw.get("cash") or 0.0),
                   core={k: {"qty": float(v["qty"]), "price": float(v["price"])}
                         for k, v in (raw.get("core") or {}).items()},
                   swing={k: PaperSwing(**v) for k, v in (raw.get("swing") or {}).items()},
                   last_cycle=raw.get("last_cycle"), marked_at=raw.get("marked_at"),
                   funding=raw.get("funding") or "funded",
                   declared_swing_pct_per_leg=float(raw.get("declared_swing_pct_per_leg") or 1.25),
                   peak_nav=float(raw.get("peak_nav") or raw.get("start_nav") or 0.0),
                   build_phase=raw.get("build_phase") if isinstance(raw.get("build_phase"), dict) else None)
        return book

    @classmethod
    def start(cls, state_dir: Path, nav: float, *, at: datetime, funding: str = "funded",
              declared_swing_pct_per_leg: float = 1.25) -> PaperBook:
        if not (_finite(nav) and nav > 0):
            raise ValueError("the paper book needs a positive start NAV")
        book = cls(state_dir=Path(state_dir), started_at=at.isoformat(), start_nav=float(nav), cash=float(nav),
                   peak_nav=float(nav),
                   funding=funding, declared_swing_pct_per_leg=declared_swing_pct_per_leg)
        book.log({"kind": "start", "at": at.isoformat(), "funding": funding})
        return book

    def to_json(self) -> dict[str, Any]:
        return {"version": VERSION, "started_at": self.started_at, "start_nav": self.start_nav, "cash": self.cash,
                "core": self.core, "swing": {k: vars(v) for k, v in self.swing.items()},
                "last_cycle": self.last_cycle, "marked_at": self.marked_at, "funding": self.funding,
                "declared_swing_pct_per_leg": self.declared_swing_pct_per_leg, "peak_nav": self.peak_nav,
                "build_phase": self.build_phase}

    def drawdown(self) -> float:
        """Fractional distance below the paper peak (0 at the peak), like `risk.nav.NavState`; the
        peak is raised to the current equity first."""
        eq = self.equity()
        self.peak_nav = max(self.peak_nav, eq)
        return max(0.0, 1.0 - eq / self.peak_nav) if self.peak_nav > 0 else 0.0

    def save(self) -> Path:
        self.drawdown()
        path = self.path_of(self.state_dir)
        _write_private(path, json.dumps(self.to_json(), sort_keys=True, indent=1))
        return path

    def log(self, row: Mapping[str, Any]) -> None:
        """Append one private ledger row (0600)."""
        path = Path(self.state_dir) / LEDGER_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(dict(row), sort_keys=True, default=str) + "\n")
        os.chmod(path, 0o600)

    # ------------------------------------------------------------------ book arithmetic
    def open_swing(self) -> list[PaperSwing]:
        return [t for t in self.swing.values() if t.status == "open"]

    def equity(self) -> float:
        core = sum(p["qty"] * p["price"] for p in self.core.values())
        return self.cash + core + sum(t.pnl() for t in self.open_swing())

    def signed_w(self) -> dict[str, float]:
        eq = self.equity()
        if eq <= 0:
            return {}
        out = {s: p["qty"] * p["price"] / eq for s, p in self.core.items() if abs(p["qty"]) > EPS}
        for t in self.open_swing():
            out[t.line] = out.get(t.line, 0.0) + t.value() / eq
        return out

    def is_empty(self) -> bool:
        return not any(abs(p["qty"]) > EPS for p in self.core.values()) and not self.open_swing()

    def swing_exposure_nav(self) -> float:
        """Sized exposure of the open paper swing trades as a NAV fraction (|size at entry|)."""
        eq = self.equity()
        return sum(abs(t.notional) for t in self.open_swing()) / eq if eq > 0 else 0.0

    def snapshot(self, now: datetime) -> Any:
        from council.models.broker import ExposureSnapshot

        w = self.signed_w()
        return ExposureSnapshot(taken_at=now, equity_usd=self.equity(), credit_usd=max(self.cash, 0.0),
                                positions=[], signed_w=w, gross=sum(abs(v) for v in w.values()),
                                net=sum(w.values()), margin_use=0.0, flags=["paper_book"])

    # ------------------------------------------------------------------ marks
    def mark_core(self, prices: Mapping[str, float], at: datetime) -> list[str]:
        flags = []
        for s, p in self.core.items():
            px = prices.get(s)
            if _finite(px) and px > 0:
                p["price"] = float(px)
            elif abs(p["qty"]) > EPS:
                flags.append(f"paper_book_no_mark:{s}")
        self.marked_at = at.isoformat()
        return flags

    def settle_swing(self, bars_by_ticker: Mapping[str, Any], last_close: Callable[[str], float | None],
                     at: datetime) -> list[str]:
        """Exit the open swing trades whose stop / target / time stop fired on the completed daily
        bars (`swing.paper.evaluate`), mark the rest at the last close."""
        from council.swing import paper

        flags = []
        for t in self.open_swing():
            idea = paper.PaperIdea(ref=t.trade_id, ticker=t.ticker, side=t.side, group="executed",
                                   entry_day=date.fromisoformat(t.entry_day), entry_ref=t.entry_ref,
                                   stop_pct=t.stop_pct, target_pct=t.target_pct,
                                   time_stop_day=date.fromisoformat(t.time_stop_day))
            bars = bars_by_ticker.get(t.ticker)
            out = paper.evaluate(idea, bars, declared_cost_pct_per_leg=self.declared_swing_pct_per_leg) \
                if bars is not None else None
            if out is not None:
                exit_px = t.entry_ref * (1.0 + t.sign * out.gross_ret)
                self.close_swing(t.trade_id, exit_px, out.exit_reason, out.exit_day.isoformat(), out.days_held)
                flags.append(f"paper_swing_exit:{out.exit_reason}")
                continue
            px = last_close(t.ticker)
            if _finite(px) and px > 0:
                t.last_price = float(px)
                if bars is not None and not getattr(bars, "empty", True):
                    t.days_held = sum(1 for d in paper._bar_days(bars) if d > idea.entry_day)
        return flags

    # ------------------------------------------------------------------ fills
    def trade_core(self, targets: Mapping[str, float], prices: Mapping[str, float],
                   cost_bps: Callable[[str, float, float], float], *, at: datetime,
                   cycle_id: str) -> tuple[int, list[str]]:
        """Move each core line to its target weight at `prices`, paying `cost_bps(line, before,
        after)`: the leg's declared cost in bps of NAV (|dw| x per side + the fixed fee, the
        engine's R14 leg cost). Returns (legs filled, flags)."""
        eq = self.equity()
        if eq <= 0:
            return 0, ["paper_book_no_equity"]
        cur = self.signed_w()
        legs, flags = 0, []
        for s, tw in sorted(targets.items()):
            before = cur.get(s, 0.0)
            if abs(tw - before) <= 1e-6:
                continue
            px = prices.get(s) or (self.core.get(s) or {}).get("price")
            if not (_finite(px) and px > 0):
                flags.append(f"paper_book_no_price:{s}")
                continue
            delta = (tw - before) * eq
            cost = eq * max(float(cost_bps(s, before, tw)), 0.0) / 1e4
            pos = self.core.setdefault(s, {"qty": 0.0, "price": float(px)})
            pos["price"] = float(px)
            pos["qty"] += delta / float(px)
            self.cash -= delta + cost
            legs += 1
            self.log({"kind": "core_fill", "at": at.isoformat(), "cycle": cycle_id, "line": s,
                      "before_w": round(before, 6), "after_w": round(tw, 6), "cost": cost})
        return legs, flags

    def enter_swing(self, *, trade_id: str, ticker: str, side: str, line: str, size_nav: float, entry_ref: float,
                    stop_pct: float, target_pct: float, entry_day: str, time_stop_day: str,
                    setup: str | None, at: datetime, cycle_id: str, sigma_daily: float | None = None,
                    beta: float | None = None) -> bool:
        if trade_id in self.swing or not (_finite(entry_ref) and entry_ref > 0) or not size_nav > 0:
            return False
        notional = float(size_nav) * self.equity()
        self.cash -= notional * self.declared_swing_pct_per_leg / 100.0
        self.swing[trade_id] = PaperSwing(trade_id=trade_id, ticker=ticker, side=side, line=line, notional=notional,
                                          entry_ref=float(entry_ref), stop_pct=float(stop_pct),
                                          target_pct=float(target_pct), entry_day=entry_day,
                                          time_stop_day=time_stop_day, setup=setup, last_price=float(entry_ref),
                                          opened_cycle=cycle_id, sigma_daily=sigma_daily, beta=beta)
        self.log({"kind": "swing_entry", "at": at.isoformat(), "cycle": cycle_id, "trade": trade_id,
                  "ticker": ticker, "side": side, "size_nav": round(float(size_nav), 6)})
        return True

    def close_swing(self, trade_id: str, exit_px: float, reason: str, day: str, days_held: int) -> None:
        t = self.swing[trade_id]
        gross = t.sign * (exit_px / t.entry_ref - 1.0)
        leg = self.declared_swing_pct_per_leg / 100.0
        self.cash += t.notional * (gross - leg)
        t.status, t.exit_reason, t.exit_day, t.days_held = "closed", reason, day, int(days_held)
        t.last_price, t.net_ret = float(exit_px), gross - 2.0 * leg
        self.log({"kind": "swing_exit", "trade": trade_id, "reason": reason, "day": day,
                  "net_pct": round(100.0 * t.net_ret, 4)})

    # ------------------------------------------------------------------ public view
    def public(self) -> dict[str, Any]:
        """Percent-only (module docstring): no amount, unit, price or position id."""
        eq = self.equity()
        w = self.signed_w()
        core_lines = {s: round(100.0 * v, 2) for s, v in sorted(w.items()) if s in self.core and abs(v) > 5e-5}
        swing_w = {t.line: 0.0 for t in self.open_swing()}
        for t in self.open_swing():
            swing_w[t.line] += t.value() / eq if eq > 0 else 0.0
        leg = 2.0 * self.declared_swing_pct_per_leg / 100.0
        trades = []
        for t in sorted(self.swing.values(), key=lambda x: (x.entry_day, x.trade_id)):
            if t.status == "open":
                px = t.last_price or t.entry_ref
                ret = t.sign * (px / t.entry_ref - 1.0) - leg
            else:
                ret = t.net_ret or 0.0
            trades.append({
                "ticker": t.ticker, "side": t.side, "setup": t.setup, "status": t.status,
                "weight_pct": round(100.0 * t.value() / eq, 2) if t.status == "open" and eq > 0 else 0.0,
                "stop_pct": round(100.0 * t.stop_pct, 2), "target_pct": round(100.0 * t.target_pct, 2),
                "entry_day": t.entry_day, "days_held": int(t.days_held), "exit_reason": t.exit_reason,
                "return_net_pct": round(100.0 * ret, 2)})
        core_gross = sum(abs(v) for v in core_lines.values())
        swing_gross = sum(abs(v) for v in swing_w.values()) * 100.0
        return {
            "version": VERSION,
            "started_at": self.started_at, "marked_at": self.marked_at, "last_cycle": self.last_cycle,
            "paper_return_pct": round(100.0 * (eq / self.start_nav - 1.0), 2) if self.start_nav > 0 else 0.0,
            "core_weights_pct": core_lines,
            "swing_trades": trades,
            "split_pct": {"core": round(core_gross, 2), "swing": round(swing_gross, 2),
                          "cash": round(100.0 * self.cash / eq, 2) if eq > 0 else 0.0},
            "cost_basis": {"core": "policy cost model per side", "swing_pct_per_leg": self.declared_swing_pct_per_leg},
            "funding": self.funding,
        }


def resolve_dir(state: Path) -> Path:
    """`state` may be the paper state dir itself or the base state dir that contains `paper/`."""
    state = Path(state)
    if (state / BOOK_FILE).exists() or state.name == PAPER_STATE_DIR:
        return state
    return state / PAPER_STATE_DIR


def paper_book_public(state: Path) -> dict[str, Any]:
    """The percent-only paper portfolio for the public paper record / site (module docstring).
    `{}` when no paper book exists yet."""
    book = PaperBook.load(resolve_dir(state))
    return book.public() if book is not None else {}


def start_nav(state_dir: Path, policy: Any) -> tuple[float, str]:
    """The private start NAV: the funded NAV (paper dir, then the base state dir), else the policy's
    assumed virtual NAV ("assumed")."""
    from council.swing.costs import load_account

    for d in (Path(state_dir), Path(state_dir).parent):
        acct = load_account(d)
        if acct is not None:
            return float(acct.funded_real_nav_usd), "funded"
    from council.risk.config import cost_floors

    return float(cost_floors(policy).assumed.virtual_nav_usd), "assumed"


def last_closes(history: Mapping[str, Any], lines: Iterable[str]) -> dict[str, float]:
    """{line: the last completed close} from the cycle's history (the slot's reference prices)."""
    out: dict[str, float] = {}
    for s in lines:
        bars = history.get(s)
        if bars is None or getattr(bars, "empty", True) or "close" not in bars:
            continue
        px = float(bars["close"].iloc[-1])
        if math.isfinite(px) and px > 0:
            out[s] = px
    return out
