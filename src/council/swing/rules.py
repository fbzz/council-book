"""Swing risk rules S1-S17 (design swing-book.md rev 2, §3; SW-4). Pure: no I/O, no broker, no LLM.

Every number comes from `policy/swing.yaml` (`Policy.swing`), each bounded again here by the code
ceilings in `council.invariants` (the policy may be stricter, never looser). The rules run twice:
as a pre-screen after the fact card (`screen_entry`, one idea against the current book) and as the
final pass on the PM's entries (`final_pass`, in PM order, each accepted entry joining the book the
next one is checked against). Exits are never refused.

Units: every size, stop, target and sigma is a FRACTION (0.05 = 5%); the round-trip cost callback
returns PERCENT of the position (`swing.costs.RoundTrip.total_pct`), like `costs.econ_ok`.

Reason codes (public; the numbers behind them are not):

| Rule | Code(s) |
|---|---|
| S1 size | `stop_too_wide_for_size` |
| S2 capacity | `max_open`, `max_short`, `already_open` (one swing trade per ticker: a second entry would stack past the size and loss caps) |
| S3 weekly cap | `weekly_cap` |
| S4 open risk | `open_risk` |
| S5 stops | `stop_missing`, `stop_out_of_range`, `stop_inside_atr` (a stop inside 1 ATR is widened when the wider stop still passes S1/S6), `atr_unknown` (no ATR: the 1-ATR check cannot run, so the entry drops; fail closed) |
| S6 targets | `target_missing`, `vol_unknown`, `vol_too_high`, `target_too_small`, `target_beyond_vol`, `cost_unavailable` |
| S7 time stop | `time_stop_out_of_range` |
| S8 liquidity | `illiquid`, `price_too_low` |
| S9 earnings | `earnings_window`, `earnings_window_estimated`, `post_earnings_wait` |
| S10 correlation | `bucket_full`, `swing_net_beta` |
| S11 chase | `chased` (2-3 sigma is the Skeptic's `wait` prior, flag `chase_prior_wait`) |
| S12 fees | reported only while `fees.mode: report` (flag `s12_over_budget`); `fee_budget` under `enforce` |
| S13 shorts | `short_new_listing`, `short_crowded`, `short_takeover_target`, `short_into_flush`, `short_squeeze_risk`, `short_si_unknown` (flag: half size) |
| S14 cool-off | `cooloff` |
| S15 brake | `swing_brake` |
| S16 entry guard | at approval: `swing_entry_ran`, `swing_entry_stopped`, `expired` (`entry_guard`) |
| S17 drawdown | flag `drawdown_scaled`; WARN / HALTED / FLAT -> `kill_state`; an unknown drawdown -> `drawdown_unknown` (fail closed: no entry) |
| other | `setup_paper_only`, `swing_blocker`, `vehicle_owned_by_core` |
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Literal

from council import invariants as inv
from council.clock import session_hours
from council.stocks.universe import try_normalise_id
from council.swing import costs as swing_costs
from council.swing.policy import SwingPolicy

Side = Literal["long", "short"]
EPS = 1e-12
MAX_SCAN_DAYS = 400
ENTRY_BLOCKING_KILL_STATES = frozenset({"WARN", "HALTED", "FLAT"})

# Rule id of each drop code (the trace and the public record carry `<rule>:<code>`).
RULE_OF: dict[str, str] = {
    "stop_too_wide_for_size": "S1",
    "max_open": "S2", "max_short": "S2", "already_open": "S2",
    "weekly_cap": "S3",
    "open_risk": "S4",
    "stop_missing": "S5", "stop_out_of_range": "S5", "stop_inside_atr": "S5",
    "atr_unknown": "S5",
    "target_missing": "S6", "vol_unknown": "S6", "vol_too_high": "S6", "target_too_small": "S6",
    "target_beyond_vol": "S6", "cost_unavailable": "S6",
    "time_stop_out_of_range": "S7",
    "illiquid": "S8", "price_too_low": "S8",
    "earnings_window": "S9", "earnings_window_estimated": "S9", "post_earnings_wait": "S9",
    "bucket_full": "S10", "swing_net_beta": "S10",
    "chased": "S11",
    "fee_budget": "S12",
    "short_new_listing": "S13", "short_crowded": "S13", "short_takeover_target": "S13",
    "short_into_flush": "S13", "short_squeeze_risk": "S13",
    "cooloff": "S14",
    "swing_brake": "S15",
    "swing_entry_ran": "S16", "swing_entry_stopped": "S16", "expired": "S16",
    "kill_state": "S17", "drawdown_unknown": "S17",
    "setup_paper_only": "SB16", "swing_blocker": "R20", "vehicle_owned_by_core": "S0",
}
SWING_DROP_CODES: frozenset[str] = frozenset(RULE_OF)


def public_code(code: str) -> str:
    """`<rule>:<code>` for the public record (no number, ever)."""
    return f"{RULE_OF.get(code, 'S0')}:{code}"


# ------------------------------------------------------------------------------------ calendar
def is_session(day: date) -> bool:
    return session_hours("us", day, closing=True) is not None


def add_sessions(day: date, n: int) -> date:
    """The date of the n-th US session after `day` (n >= 1)."""
    d, k = day, 0
    for _ in range(MAX_SCAN_DAYS):
        d += timedelta(days=1)
        if is_session(d):
            k += 1
            if k >= n:
                return d
    raise ValueError("session scan window exceeded")


def sessions_until(day: date, until: date) -> int:
    """US sessions d with day < d <= until (0 when until <= day)."""
    n, d = 0, day
    if until <= day:
        return 0
    for _ in range(MAX_SCAN_DAYS):
        d += timedelta(days=1)
        if d > until:
            return n
        if is_session(d):
            n += 1
    raise ValueError("session scan window exceeded")


# ------------------------------------------------------------------------------------ limits
@dataclass(frozen=True)
class Limits:
    """The policy numbers the rules use, each clamped to the code ceiling."""

    target_nav: float
    min_nav: float
    long_loss: float
    short_loss: float
    si_unknown_mult: float
    max_open: int
    max_short: int
    max_new_7d: int
    max_open_risk: float
    gap_mult: float
    stop_min: float
    stop_max_long: float
    stop_max_short: float
    atr_mult: float
    min_cost_mult: float
    min_net_rr: float
    target_max: float
    vol_mult: float
    sigma_max: float
    ts_min: int
    ts_max: int


def limits(sp: SwingPolicy) -> Limits:
    return Limits(
        target_nav=min(sp.size.target_nav, inv.SWING_MAX_SIZE_NAV),
        min_nav=sp.size.min_nav,
        long_loss=min(sp.size.max_loss_nav_at_stop, inv.SWING_MAX_LONG_LOSS_NAV),
        short_loss=min(sp.size.short_max_loss_nav_at_stop, inv.SWING_MAX_SHORT_LOSS_NAV),
        si_unknown_mult=min(sp.size.short_si_unknown_mult, 1.0),
        max_open=min(sp.capacity.max_open, inv.SWING_MAX_OPEN),
        max_short=min(sp.capacity.max_short, inv.SWING_MAX_SHORT),
        max_new_7d=min(sp.capacity.max_new_7d, inv.SWING_MAX_NEW_7D),
        max_open_risk=min(sp.capacity.max_open_risk_nav, inv.SWING_MAX_OPEN_RISK_NAV),
        gap_mult=max(sp.capacity.open_risk_gap_mult, inv.SWING_MIN_GAP_MULT),
        stop_min=sp.stops.min_pct,
        stop_max_long=min(sp.stops.max_long_pct, inv.SWING_MAX_LONG_STOP_PCT),
        stop_max_short=min(sp.stops.max_short_pct, inv.SWING_MAX_SHORT_STOP_PCT),
        atr_mult=sp.stops.min_atr_mult,
        min_cost_mult=sp.targets.min_cost_mult,
        min_net_rr=max(sp.targets.min_net_rr, inv.SWING_MIN_NET_RR),
        target_max=sp.targets.max_pct,
        vol_mult=sp.targets.max_vol_mult,
        sigma_max=sp.targets.max_sigma_daily,
        ts_min=sp.time_stop.min_sessions,
        ts_max=min(sp.time_stop.max_sessions, inv.SWING_MAX_TOTAL_SESSIONS),
    )


# ------------------------------------------------------------------------------------ inputs
@dataclass(frozen=True)
class Candidate:
    """One proposed entry, with the facts the rules need (fractions; None = unknown)."""

    ref: str
    ticker: str
    side: Side
    setup: str | None = None
    stop_pct: float | None = None
    target_pct: float | None = None
    time_stop_days: int | None = None
    sigma_daily: float | None = None
    atr_pct: float | None = None
    adv_usd: float | None = None
    price: float | None = None
    px_ge_10: bool | None = None                                   # the card's percent-only price flag
    beta_60d: float | None = None
    sector: str | None = None
    corr_open: Mapping[str, float] = field(default_factory=dict)   # {open trade ref: 60d rho}
    corr_core: Mapping[str, float] = field(default_factory=dict)   # {core line: 60d rho}
    short_interest_pct_float: float | None = None                  # percent (20 = 20%)
    listing_days: int | None = None
    earnings_next: date | None = None
    earnings_confirmed: bool = False
    last_report_at: datetime | None = None
    move_since_news_sigma: float | None = None                     # signed (up > 0)
    last_session_sigma: float | None = None                        # signed
    ret_20d: float | None = None
    from_52w_high: float | None = None                             # 0.02 = 2% below the high
    vol_ratio_last: float | None = None
    takeover_target: bool = False
    vehicle_owned_by_core: bool = False


@dataclass(frozen=True)
class BookTrade:
    """An open swing trade, or a proposed-unexecuted entry (counts until it expires)."""

    ref: str
    ticker: str
    side: Side
    size_nav: float
    stop_pct: float
    sector: str | None = None
    beta_60d: float | None = None
    buckets: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RecentExit:
    ticker: str
    day: date
    by_stop: bool


@dataclass(frozen=True)
class BookState:
    today: date
    now: datetime
    kill_state: str = "NORMAL"
    trades: Sequence[BookTrade] = ()
    entries_7d: int = 0                      # executed + approved in flight, rolling 7 days
    drawdown_from_peak: float | None = None  # real-adjusted, negative (D19)
    brake_on: bool = False                   # S15, lifted only by the operator
    blockers: Sequence[str] = ()             # swing-scope ledger blockers
    recent_exits: Sequence[RecentExit] = ()
    core_overweight: frozenset[str] = frozenset()   # core lines held above their reference weight
    fee_30d_nav_bps: float = 0.0             # S12 (private)


CostFn = Callable[[Side, float, int], float | None]    # (side, size_nav, sessions) -> percent


@dataclass(frozen=True)
class Verdict:
    ref: str
    ok: bool
    code: str | None = None
    size_nav: float = 0.0
    stop_pct: float | None = None
    target_pct: float | None = None
    time_stop_days: int | None = None
    time_stop_date: date | None = None
    cost_rt_pct: float | None = field(default=None, repr=False)   # private
    buckets: frozenset[str] = frozenset()
    flags: tuple[str, ...] = ()

    @property
    def rule(self) -> str | None:
        return RULE_OF.get(self.code) if self.code else None

    def book_trade(self, c: Candidate) -> BookTrade:
        return BookTrade(ref=c.ref, ticker=c.ticker, side=c.side, size_nav=self.size_nav,
                         stop_pct=float(self.stop_pct or 0.0), sector=c.sector, beta_60d=c.beta_60d,
                         buckets=self.buckets)


def _drop(c: Candidate, code: str, flags: Iterable[str] = ()) -> Verdict:
    return Verdict(ref=c.ref, ok=False, code=code, flags=tuple(flags))


def _tid(ticker: str) -> str:
    """The ticker's line id (BRK.B == BRK_B), so one name is one swing line."""
    return try_normalise_id(ticker) or ticker.strip().upper()


def _sign(side: str) -> float:
    return 1.0 if side == "long" else -1.0


# ------------------------------------------------------------------------------------ sizing
def size_for(side: Side, stop_pct: float, lim: Limits, *, si_unknown: bool = False,
             drawdown_scaled: float | None = None) -> float:
    """S1 (+ S17): the nominal size shrunk so the loss at the stop stays within the cap; shorts
    smaller; unknown short interest halves a short; the drawdown scale caps the size."""
    loss = lim.long_loss if side == "long" else lim.short_loss
    size = min(lim.target_nav, loss / stop_pct)
    if side == "short" and si_unknown:
        size *= lim.si_unknown_mult
    if drawdown_scaled is not None:
        size = min(size, drawdown_scaled)
    return size


def drawdown_scaled(book: BookState, sp: SwingPolicy) -> bool:
    dd = book.drawdown_from_peak
    return dd is not None and dd <= sp.drawdown_scale.from_peak + EPS


def open_risk(trades: Iterable[BookTrade], gap_mult: float) -> float:
    return sum(t.size_nav * t.stop_pct * gap_mult for t in trades)


def net_beta(trades: Iterable[BookTrade]) -> float:
    return sum(t.size_nav * (t.beta_60d if t.beta_60d is not None else 1.0) * _sign(t.side)
               for t in trades)


# ------------------------------------------------------------------------------------ S9
def earnings_cap(c: Candidate, book: BookState, sp: SwingPolicy) -> tuple[int | None, str | None]:
    """(max time-stop sessions allowed by S9, drop code). None = no cap."""
    e = sp.earnings
    if c.last_report_at is not None and book.now - c.last_report_at < timedelta(hours=e.post_report_wait_h):
        return None, "post_earnings_wait"
    if c.earnings_next is None:
        return None, None
    if c.earnings_confirmed:
        n = sessions_until(book.today, c.earnings_next)
        if n <= e.no_entry_within_sessions:
            return None, "earnings_window"
        return n - e.exit_before_sessions, None
    window_start = c.earnings_next - timedelta(days=e.estimated_window_days)
    return sessions_until(book.today, window_start - timedelta(days=1)), None


# ------------------------------------------------------------------------------------ one idea
def screen_entry(c: Candidate, book: BookState, sp: SwingPolicy, cost_fn: CostFn) -> Verdict:
    """Every per-idea rule, then the book rules against `book.trades`. First failure wins."""
    lim = limits(sp)
    flags: list[str] = []
    if book.kill_state != "NORMAL":      # WARN / HALTED / FLAT, and any unknown state (fail closed)
        return _drop(c, "kill_state")
    if book.blockers:
        return _drop(c, "swing_blocker")
    if book.brake_on:
        return _drop(c, "swing_brake")
    dd = book.drawdown_from_peak      # S17 cannot scale what it cannot see: no entry (fail closed)
    if dd is None or not math.isfinite(dd):
        return _drop(c, "drawdown_unknown")
    if c.vehicle_owned_by_core:
        return _drop(c, "vehicle_owned_by_core")
    if c.setup is not None and c.setup not in sp.setups_live:
        return _drop(c, "setup_paper_only")
    # S14 cool-off
    for x in book.recent_exits:
        if _tid(x.ticker) == _tid(c.ticker):
            wait = sp.cooloff.after_stop_sessions if x.by_stop else sp.cooloff.after_exit_sessions
            if sessions_until(x.day, book.today) < wait:
                return _drop(c, "cooloff")
    # S8 liquidity
    min_adv = sp.liquidity.short_min_adv_usd if c.side == "short" else sp.liquidity.min_adv_usd
    if c.adv_usd is None or c.adv_usd < min_adv:
        return _drop(c, "illiquid")
    if c.price is not None:
        price_ok = c.price >= sp.liquidity.min_price_usd
    else:   # the fact card carries only `px_ge_10`; it answers the rule only at a $10 floor
        price_ok = bool(c.px_ge_10) and sp.liquidity.min_price_usd <= 10.0
    if not price_ok:
        return _drop(c, "price_too_low")
    # S11 chase
    if c.move_since_news_sigma is not None:
        directed = c.move_since_news_sigma * _sign(c.side)
        if directed > sp.chase.max_move_since_news_sigma + EPS:
            return _drop(c, "chased")
        if directed >= sp.chase.prior_wait_sigma:
            flags.append("chase_prior_wait")
    # S13 shorts
    si_unknown = False
    if c.side == "short":
        sh = sp.shorts
        if c.listing_days is None or c.listing_days < sh.min_listing_days:
            return _drop(c, "short_new_listing")
        if c.short_interest_pct_float is None:
            si_unknown = True
            flags.append("short_si_unknown")
        elif c.short_interest_pct_float > sh.max_si_pct_float:
            return _drop(c, "short_crowded")
        if c.takeover_target:
            return _drop(c, "short_takeover_target")
        if c.last_session_sigma is not None and c.last_session_sigma <= -sh.no_short_after_down_sigma:
            return _drop(c, "short_into_flush")
        if c.ret_20d is not None and c.ret_20d > sh.no_short_ret20_above:
            return _drop(c, "short_squeeze_risk")
        if (c.from_52w_high is not None and c.from_52w_high <= sh.near_high_pct
                and c.vol_ratio_last is not None and c.vol_ratio_last > sh.near_high_vol_ratio):
            return _drop(c, "short_squeeze_risk")
    # S6 volatility (needed by S5 and the target ceiling)
    if c.sigma_daily is None or not math.isfinite(c.sigma_daily) or c.sigma_daily <= 0:
        return _drop(c, "vol_unknown")
    if c.sigma_daily > lim.sigma_max + EPS:
        return _drop(c, "vol_too_high")
    # S7 time stop, S9 earnings cut
    days = lim.ts_max if c.time_stop_days is None else int(c.time_stop_days)
    if not lim.ts_min <= days <= lim.ts_max:
        return _drop(c, "time_stop_out_of_range")
    cap, code = earnings_cap(c, book, sp)
    if code:
        return _drop(c, code)
    if cap is not None and cap < days:
        if cap < lim.ts_min:
            return _drop(c, "earnings_window" if c.earnings_confirmed else "earnings_window_estimated")
        days = cap
        flags.append("time_stop_cut_earnings")
    # S5 stop
    if c.stop_pct is None:
        return _drop(c, "stop_missing")
    if c.target_pct is None:
        return _drop(c, "target_missing")
    stop_max = lim.stop_max_long if c.side == "long" else lim.stop_max_short
    stop = float(c.stop_pct)
    if not lim.stop_min - EPS <= stop <= stop_max + EPS:
        return _drop(c, "stop_out_of_range")
    widened = False
    if c.atr_pct is None or not math.isfinite(c.atr_pct) or c.atr_pct <= 0:
        return _drop(c, "atr_unknown")     # the 1-ATR floor is never skipped (fail closed)
    if stop < lim.atr_mult * c.atr_pct - EPS:
        stop = lim.atr_mult * c.atr_pct
        if stop > stop_max + EPS:
            return _drop(c, "stop_inside_atr")
        widened = True
        flags.append("stop_widened_to_atr")
    # S1 size (+ S17)
    scaled = drawdown_scaled(book, sp)
    if scaled:
        flags.append("drawdown_scaled")
    size = size_for(c.side, stop, lim, si_unknown=si_unknown,
                    drawdown_scaled=sp.drawdown_scale.size_nav if scaled else None)
    if size < lim.min_nav - EPS:
        return _drop(c, "stop_inside_atr" if widened else "stop_too_wide_for_size", flags)
    # S6 target
    cost = cost_fn(c.side, size, days)
    if cost is None or not math.isfinite(cost) or cost < 0:
        return _drop(c, "cost_unavailable", flags)
    ceiling = min(lim.target_max, lim.vol_mult * c.sigma_daily * math.sqrt(days))
    target = float(c.target_pct)
    clipped = target > ceiling + EPS
    if clipped:
        target = ceiling
        flags.append("target_clipped_to_vol")
    if not swing_costs.econ_ok(cost, stop * 100.0, target * 100.0, min_cost_mult=lim.min_cost_mult,
                               min_net_rr=lim.min_net_rr):
        code = "stop_inside_atr" if widened else ("target_beyond_vol" if clipped else "target_too_small")
        return _drop(c, code, flags)
    # S12 fee budget (reported unless enforced)
    if book.fee_30d_nav_bps + cost * size * 100.0 > sp.fees.budget_30d_nav_bps + EPS:
        if sp.fees.mode == "enforce":
            return _drop(c, "fee_budget", flags)
        flags.append("s12_over_budget")
    v = Verdict(ref=c.ref, ok=True, size_nav=size, stop_pct=stop, target_pct=target,
                time_stop_days=days, time_stop_date=add_sessions(book.today, days), cost_rt_pct=cost,
                buckets=buckets_for(c, book, sp), flags=tuple(flags))
    code = book_check(c, v, book, sp)
    return v if code is None else _drop(c, code, flags)


def buckets_for(c: Candidate, book: BookState, sp: SwingPolicy) -> frozenset[str]:
    """S10 buckets: the FF12 sector, each same-side open trade with rho >= same_bet_corr, and a
    core line (rho >= same_bet_corr) the core holds above its reference weight (longs only)."""
    thr = sp.correlation.same_bet_corr
    out = {f"trade:{c.ref}"}
    if c.sector:
        out.add(f"sector:{c.sector}")
    sides = {t.ref: t.side for t in book.trades}
    for ref, rho in c.corr_open.items():
        if rho >= thr - EPS and sides.get(ref) == c.side:
            out.add(f"trade:{ref}")
    if c.side == "long":
        for line, rho in c.corr_core.items():
            if rho >= thr - EPS and line in book.core_overweight:
                out.add(f"core:{line}")
    return frozenset(out)


def _members(bucket: str, book: BookState) -> int:
    n = sum(1 for t in book.trades if bucket in t.buckets or bucket == f"trade:{t.ref}"
            or (t.sector is not None and bucket == f"sector:{t.sector}"))
    if bucket.startswith("core:") and bucket[5:] in book.core_overweight:
        n += 1                            # the core's overweight line is itself one member
    return n


def book_check(c: Candidate, v: Verdict, book: BookState, sp: SwingPolicy) -> str | None:
    """S2, S3, S4, S10 (+ S17's open cap) of one accepted entry against the book."""
    lim = limits(sp)
    trades = list(book.trades)
    max_open = lim.max_open
    if drawdown_scaled(book, sp):
        max_open = min(max_open, sp.drawdown_scale.max_open)
    if any(_tid(t.ticker) == _tid(c.ticker) or t.ref == c.ref for t in trades):
        return "already_open"
    if len(trades) + 1 > max_open:
        return "max_open"
    if c.side == "short" and sum(1 for t in trades if t.side == "short") + 1 > lim.max_short:
        return "max_short"
    if book.entries_7d + 1 > lim.max_new_7d:
        return "weekly_cap"
    mine = v.book_trade(c)
    if open_risk([*trades, mine], lim.gap_mult) > lim.max_open_risk + EPS:
        return "open_risk"
    for b in v.buckets:
        if b == f"trade:{c.ref}":
            continue
        if _members(b, book) + 1 > sp.correlation.max_open_per_bucket:
            return "bucket_full"
    if abs(net_beta([*trades, mine])) > sp.correlation.max_swing_net_beta + EPS:
        return "swing_net_beta"
    return None


def final_pass(cands: Sequence[Candidate], book: BookState, sp: SwingPolicy,
               cost_fn: CostFn) -> tuple[list[Verdict], list[Verdict]]:
    """The PM's entries in PM order: each passes every rule against the book INCLUDING the entries
    accepted before it. Returns (accepted, dropped)."""
    ok: list[Verdict] = []
    dropped: list[Verdict] = []
    cur = book
    for c in cands:
        v = screen_entry(c, cur, sp, cost_fn)
        if v.ok:
            ok.append(v)
            cur = replace(cur, trades=[*cur.trades, v.book_trade(c)], entries_7d=cur.entries_7d + 1,
                          fee_30d_nav_bps=cur.fee_30d_nav_bps + float(v.cost_rt_pct or 0) * v.size_nav * 100)
        else:
            dropped.append(v)
    return ok, dropped


def _frac(fields: Mapping[str, object], key: str) -> float | None:
    v = fields.get(key)
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(float(v)):
        return None
    return float(v) / 100.0


def candidate_from_card(card: object, *, ref: str, ticker: str, setup: str | None,
                        stop_pct: float | None, target_pct: float | None, time_stop_days: int | None,
                        sector: str | None = None, corr_open: Mapping[str, float] | None = None,
                        corr_core: Mapping[str, float] | None = None, listing_days: int | None = None,
                        last_report_at: datetime | None = None, takeover_target: bool = False,
                        last_session_sigma: float | None = None,
                        vehicle_owned_by_core: bool = False) -> Candidate:
    """A `Candidate` from a `swing.facts.FactCard` (its *_pct / sigma fields are percent) plus the
    PM's levels (fractions) and what code knows beyond the card."""
    f: Mapping[str, object] = getattr(card, "fields", {}) or {}
    side = getattr(card, "side", "long")
    adv = f.get("adv_usd_20d")
    beta = f.get("beta_60d")
    si = f.get("short_interest_pct_float")
    msn = f.get("move_since_news_live_sigma")
    if msn is None:
        msn = f.get("move_since_news_close_sigma")
    high = _frac(f, "dist_52w_high_pct")
    earn = f.get("earnings_next")
    vr = f.get("vol_ratio_last")
    return Candidate(
        ref=ref, ticker=ticker, side=side, setup=setup, stop_pct=stop_pct, target_pct=target_pct,  # type: ignore[arg-type]
        time_stop_days=time_stop_days, sigma_daily=_frac(f, "sigma_daily"), atr_pct=_frac(f, "atr14_pct"),
        adv_usd=float(adv) if isinstance(adv, int | float) else None,
        px_ge_10=f.get("px_ge_10") if isinstance(f.get("px_ge_10"), bool) else None,  # type: ignore[arg-type]
        beta_60d=float(beta) if isinstance(beta, int | float) else None, sector=sector,
        corr_open=dict(corr_open or {}), corr_core=dict(corr_core or {}),
        short_interest_pct_float=float(si) if isinstance(si, int | float) else None,
        listing_days=listing_days,
        earnings_next=date.fromisoformat(earn) if isinstance(earn, str) else None,
        earnings_confirmed=bool(f.get("earnings_confirmed")), last_report_at=last_report_at,
        move_since_news_sigma=float(msn) if isinstance(msn, int | float) else None,
        last_session_sigma=last_session_sigma, ret_20d=_frac(f, "ret_20d"),
        from_52w_high=-high if high is not None else None,
        vol_ratio_last=float(vr) if isinstance(vr, int | float) else None,
        takeover_target=takeover_target, vehicle_owned_by_core=vehicle_owned_by_core)


# ------------------------------------------------------------------------------------ S16
def entry_guard(*, side: Side, planned_rate: float, stop_rate: float, live_rate: float,
                proposed_at: datetime, now: datetime, sp: SwingPolicy) -> str | None:
    """S16 at approval (§4.3): `expired`, `swing_entry_stopped` or `swing_entry_ran`, else None."""
    valid = min(sp.entry_guard.valid_minutes, inv.SWING_MAX_ENTRY_VALID_MIN)
    if now - proposed_at > timedelta(minutes=valid):
        return "expired"
    s = _sign(side)
    if (live_rate - stop_rate) * s <= 0:
        return "swing_entry_stopped"
    stop_dist = abs(planned_rate - stop_rate) / planned_rate
    run = (live_rate - planned_rate) / planned_rate * s
    if run > min(sp.entry_guard.max_run_stop_frac * stop_dist, sp.entry_guard.max_run_pct) + EPS:
        return "swing_entry_ran"
    return None


# ------------------------------------------------------------------------------------ code exits
def exit_due(time_stop_date: date, today: date, *, slots_per_session: int, lead_slots: int,
             earnings_next: date | None = None, earnings_confirmed: bool = False,
             exit_before_sessions: int = 1) -> tuple[bool, date]:
    """(exit proposal due now, effective time-stop date). The exit is proposed from `lead_slots`
    swing slots before its due slot (the last slot of the time-stop date); a confirmed earnings date
    inside the trade pulls the time stop to `exit_before_sessions` before it (S7/S9c)."""
    due = time_stop_date
    if earnings_next is not None and earnings_confirmed:
        n = sessions_until(today, earnings_next)
        pulled = today if n <= exit_before_sessions else add_sessions(today, n - exit_before_sessions)
        due = min(due, pulled)
    slots_left = sessions_until(today, due) * max(slots_per_session, 1)
    return slots_left <= lead_slots, due
