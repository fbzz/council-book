"""Churn and timing rules: deadband (R11), minimum hold (R12), churn budgets (R13), event block
(R16), anti-chase (R17) and the re-entry cool-off after a stop hit (R4d).

Rules:
- R11 deadband, discretionary legs: a change executes only if |d level| >= deadband.level (crypto:
  level_crypto) AND |d weight| >= max(deadband.min_nav_share, the broker minimum as a NAV share).
  A close to zero skips the size floor (it still needs the level step and the cost gate); on a
  stock line it skips the level step too.
- R11, reference-origin legs (the move goes toward the mechanical rule's target): the studied rule,
  `council.reference.sleeve.pending_trades` (imported, never copied): the line trades when its
  reference level differs from the level it last traded at (`risk.held_levels`), or when
  |target - held| >= max(deadband level x unit, min_nav_share), or (sleeve lines) when the
  never-borrow trim applies; a stock line whose rule target is 0 exits in full whatever its drift
  (a corporate-action credit, a residual). The size floor is then max(the real-dollar trade floor
  as a NAV share, the broker minimum); a close to zero skips it.
- R12 minimum hold: no discretionary change within min_hold_days of the line's last change
  (crypto 7d, others 3d) and no sign flip within no_flip_hours. Moves toward the reference are
  exempt when min_hold_days.toward_reference_exempt is true.
- R13: the cycle's risk increase sum(|w| increases; a flip counts its full new leg) must stay
  <= churn.cycle_increase_max; trailing discretionary turnover <= turnover_7d_max / _30d_max.
- R16: no ADDS on index/ETF/FX/commodity/crypto/stock lines from macro_before_h before to
  macro_after_h after a scheduled macro event (FOMC/CPI/NFP/PCE). Earnings (design §10, D12) are
  per symbol: an `earnings` event blocks adds only on the stock line it names, never market-wide,
  from earnings_before_h before the report until the later of earnings_after_h after it and the
  availability of the first completed daily bar after it (the reaction; `reaction_bar_at`). An
  ESTIMATED report date (`source == EARNINGS_ESTIMATE_SOURCE`) is a window: the report may come on
  any US trading day within earnings_estimate_window_days of it, so the block runs from
  earnings_before_h before the first of those days to the reaction after the last. R16 never forces
  a sale (the engine and the bands clip increases only), whatever the line's origin.
- R17: no new or larger long after a completed daily move above +anti_chase_sigma sigmas, and no
  new or larger short after one below -anti_chase_sigma.
- R4d: after a stop hit, no increase on that line for reentry_cooloff_days (crypto 7, else 3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta

import numpy as np

from council import clock
from council.data.bars import TIINGO_AVAILABLE_HOURS
from council.models.facts import EventItem, MarketState
from council.policy import LineSpec, Policy
from council.reference import sleeve as sleeve_rule
from council.risk.config import EventBlockConfig, risk_limits

MACRO_KINDS: frozenset[str] = frozenset({"fomc", "cpi", "nfp", "pce"})
MACRO_SENSITIVE_CLASSES: frozenset[str] = frozenset({"index", "etf", "fx", "commodity", "crypto", "stock"})
EARNINGS_KINDS: frozenset[str] = frozenset({"earnings"})
EARNINGS_SENSITIVE_CLASSES: frozenset[str] = frozenset({"stock"})
EARNINGS_ESTIMATE_SOURCE = "sec_estimate"      # an estimated report date (council.stocks.earnings)
_EPS = 1e-9


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime; council code uses aware UTC datetimes only")
    return ts


def deadband_level(crypto: bool, policy: Policy) -> float:
    db = risk_limits(policy).deadband
    return db.level_crypto if crypto else db.level


def deadband_ok(
    delta_level: float,
    delta_w: float,
    *,
    to_zero: bool,
    crypto: bool,
    min_share: float,
    policy: Policy,
    stock: bool = False,
) -> bool:
    """R11 for one discretionary line: level step and (unless closing to zero) notional floor. A
    stock line closing to zero skips both (a full exit of real shares)."""
    db = risk_limits(policy).deadband
    if to_zero and stock:
        return True
    if abs(delta_level) < deadband_level(crypto, policy) - _EPS:
        return False
    if to_zero:
        return True
    return abs(delta_w) >= max(db.min_nav_share, min_share) - _EPS


def reference_pending(
    lines: Sequence[str],
    *,
    held_w: Mapping[str, float],
    held_level: Mapping[str, float],
    target_w: Mapping[str, float],
    target_level: Mapping[str, float],
    unit: Mapping[str, float],
    crypto_lines: Iterable[str],
    policy: Policy,
    budget_lines: Iterable[str] = (),
    budget: float = float("inf"),
) -> set[str]:
    """R11 for reference-origin legs: the lines the studied rule orders to their target now.

    The decision is `council.reference.sleeve.pending_trades` (the frozen function the stock-sleeve
    study simulated), with threshold max(deadband level x unit, min_nav_share) per line (crypto:
    level_crypto) and the never-borrow budget over `budget_lines` (the stock sleeve).

    Plus one live-only exit (design §3.5, §17.2 #3-#4): a budgeted (stock) line whose rule target is
    0 and that still holds a position is ordered flat whatever its drift. In the study that state
    cannot arise (a position there always carries the non-zero level it was bought at, so a zero
    target is a level change), but live it does: shares credited by a corporate action, what a
    partial stop or fill leaves behind. Without it a residual below the drift threshold would never
    be sold, so its retiring line could never be pruned."""
    names = list(lines)
    if not names:
        return set()
    db = risk_limits(policy).deadband
    crypto = set(crypto_lines)
    budgeted = set(budget_lines)

    def arr(values: Mapping[str, float]) -> np.ndarray:
        return np.array([float(values.get(s, 0.0)) for s in names], dtype=float)

    threshold = np.array([
        sleeve_rule.drift_threshold(float(unit.get(s, 0.0)), deadband_level(s in crypto, policy),
                                    db.min_nav_share)
        for s in names
    ], dtype=float)
    mask = np.array([s in budgeted for s in names], dtype=bool)
    held, target = arr(held_w), arr(target_w)
    pending = sleeve_rule.pending_trades(
        held, arr(held_level), target, arr(target_level), threshold,
        budget_mask=mask if mask.any() else None, budget=budget,
    )
    exits = mask & (np.abs(target) <= _EPS) & (np.abs(held) > _EPS)
    pending = pending | exits
    return {s for s, flag in zip(names, pending, strict=True) if bool(flag)}


def reference_size_ok(delta_w: float, *, to_zero: bool, min_share: float) -> bool:
    """R11 size floor of a reference-origin leg: |dw| >= max(real-dollar floor, broker minimum) as
    NAV shares (`min_share`); a close to zero skips it."""
    return to_zero or abs(delta_w) >= min_share - _EPS


def apply_deadband(
    target_levels: Mapping[str, float],
    current_levels: Mapping[str, float],
    unit_weights: Mapping[str, float],
    nav_min_share: float | Mapping[str, float],
    policy: Policy,
    *,
    crypto_lines: Iterable[str],
) -> tuple[dict[str, float], list[str]]:
    """Hold every line whose change fails R11 at its current level. Returns (levels, held).

    `nav_min_share` is the broker minimum as a share of NAV: one number for every line, or a
    mapping per line (missing lines use 0, i.e. only the policy floor)."""
    crypto = set(crypto_lines)
    out: dict[str, float] = {}
    held: list[str] = []
    for line, target in target_levels.items():
        current = float(current_levels.get(line, 0.0))
        if abs(target - current) <= _EPS:
            out[line] = target
            continue
        unit = float(unit_weights.get(line, 0.0))
        share = (
            float(nav_min_share.get(line, 0.0))
            if isinstance(nav_min_share, Mapping)
            else float(nav_min_share)
        )
        ok = deadband_ok(
            target - current,
            (target - current) * unit,
            to_zero=abs(target) <= _EPS,
            crypto=line in crypto,
            min_share=share,
            policy=policy,
        )
        if ok:
            out[line] = target
        else:
            out[line] = current
            held.append(line)
    return out, sorted(held)


def min_hold_days(asset_class: str, policy: Policy) -> float:
    mh = risk_limits(policy).min_hold_days
    return mh.crypto if asset_class == "crypto" else mh.default


def min_hold_ok(
    line: str,
    last_change_at: datetime | None,
    now: datetime,
    *,
    sign_flip: bool,
    toward_reference: bool,
    asset_class: str,
    policy: Policy,
    reversal: bool = True,
) -> bool:
    """R12 for one line. `reversal=False` (the change continues the last change's direction)
    skips the day count; the 24h no-flip rule always applies to discretionary flips."""
    mh = risk_limits(policy).min_hold_days
    if toward_reference and mh.toward_reference_exempt:
        return True
    if last_change_at is None:
        return True
    age = _aware(now) - _aware(last_change_at)
    if sign_flip and age < timedelta(hours=mh.no_flip_hours):
        return False
    return not (reversal and age < timedelta(days=min_hold_days(asset_class, policy)))


def anti_chase_block(
    state: MarketState | None, increasing_long: bool, increasing_short: bool, policy: Policy
) -> bool:
    """R17: True when this increase chases the last completed daily move."""
    if state is None or state.ret1d_sigma is None:
        return False
    limit = risk_limits(policy).anti_chase_sigma
    if increasing_long and state.ret1d_sigma > limit:
        return True
    return bool(increasing_short and state.ret1d_sigma < -limit)


def _us_trading_day(day: date) -> bool:
    return clock.session_hours("us", day, closing=True) is not None


def shift_us_trading_days(day: date, n: int) -> date:
    """The n-th US trading day after `day` (before it when n < 0); `day` itself when n == 0.
    Holidays and weekends do not count; past the exchange calendars every weekday counts (the
    `closing=True` approximation of `clock.session_hours`)."""
    step = 1 if n > 0 else -1
    out, left = day, abs(int(n))
    while left:
        out += timedelta(days=step)
        if _us_trading_day(out):
            left -= 1
    return out


def reaction_bar_at(ts: datetime) -> datetime:
    """When the first completed daily bar that can show the market's reaction to news at `ts` is
    usable: the end-of-day availability time (`council.data.bars`, 20:00 New York on the bar's
    date) of the first US session that closes after `ts`. A report after Friday's close is
    reacted to on Monday, so its bar arrives Monday evening, not 30 hours later."""
    close = clock.session_close_after("us", _aware(ts))
    assert close is not None                                   # the US session always closes
    day = close.astimezone(clock.NEW_YORK).date()
    return datetime.combine(day, time(TIINGO_AVAILABLE_HOURS), tzinfo=clock.NEW_YORK).astimezone(UTC)


def _window(event: EventItem, cfg: EventBlockConfig) -> tuple[datetime, datetime] | None:
    at = _aware(event.at_utc)
    if event.kind in MACRO_KINDS:
        return at - timedelta(hours=cfg.macro_before_h), at + timedelta(hours=cfg.macro_after_h)
    if event.kind not in EARNINGS_KINDS:
        return None
    if event.source == EARNINGS_ESTIMATE_SOURCE:
        day = at.astimezone(clock.NEW_YORK).date()
        first = shift_us_trading_days(day, -cfg.earnings_estimate_window_days)
        last = shift_us_trading_days(day, cfg.earnings_estimate_window_days)
        earliest = datetime.combine(first, time(0), tzinfo=clock.NEW_YORK).astimezone(UTC)
        latest = datetime.combine(last + timedelta(days=1), time(0), tzinfo=clock.NEW_YORK).astimezone(UTC)
    else:
        earliest = latest = at
    end = max(latest + timedelta(hours=cfg.earnings_after_h), reaction_bar_at(latest))
    return earliest - timedelta(hours=cfg.earnings_before_h), end


def event_window(event: EventItem, policy: Policy) -> tuple[datetime, datetime] | None:
    """R16's no-add window [start, end] (UTC) of one event, or None for a kind R16 ignores. Macro:
    [at - macro_before_h, at + macro_after_h]. Earnings at a known time (a report that occurred, a
    confirmed date): [at - earnings_before_h, max(at + earnings_after_h, reaction_bar_at(at))]. An
    estimated date E: from earnings_before_h before 00:00 New York of the US trading day
    earnings_estimate_window_days before E, to the same end computed for a report at the very end
    of the trading day that many days after E."""
    return _window(event, risk_limits(policy).event_block)


def event_applies(line: LineSpec, event: EventItem) -> bool:
    """Whether R16 reads `event` for `line`: a macro event on a macro-sensitive line (every such
    line when the event names none); an earnings event only on the stock line it names."""
    if event.kind in MACRO_KINDS:
        return line.asset_class in MACRO_SENSITIVE_CLASSES and (not event.symbols or line.symbol in event.symbols)
    if event.kind in EARNINGS_KINDS:
        return line.asset_class in EARNINGS_SENSITIVE_CLASSES and line.symbol in event.symbols
    return False


def event_block(
    line: LineSpec, events: Iterable[EventItem], now: datetime, policy: Policy
) -> bool:
    """R16: True when an add on this line falls inside the window of an event that applies to it
    (a macro event, or an earnings event on this stock line)."""
    cfg = risk_limits(policy).event_block
    now = _aware(now)
    for event in events:
        if not event_applies(line, event):
            continue
        window = _window(event, cfg)
        if window is not None and window[0] <= now <= window[1]:
            return True
    return False


def reentry_blocked(
    stop_hit_at: datetime | None, now: datetime, asset_class: str, policy: Policy
) -> bool:
    """R4d: True while the post-stop cool-off is running."""
    if stop_hit_at is None:
        return False
    days = risk_limits(policy).reentry_cooloff_days
    cooloff = days["crypto"] if asset_class == "crypto" else days["default"]
    return _aware(now) - _aware(stop_hit_at) < timedelta(days=cooloff)


def line_increase(before: float, after: float) -> float:
    """Risk added on one line: a flip counts its whole new leg, otherwise |after| - |before|."""
    if before * after < 0:
        return abs(after)
    return max(abs(after) - abs(before), 0.0)


def cycle_increase(before: Mapping[str, float], after: Mapping[str, float]) -> float:
    lines = set(before) | set(after)
    return sum(line_increase(before.get(s, 0.0), after.get(s, 0.0)) for s in lines)


def cycle_increase_ok(increase: float, policy: Policy) -> bool:
    return increase <= risk_limits(policy).churn.cycle_increase_max + _EPS


def turnover_ok(prior: float, this_cycle: float, window: str, policy: Policy) -> bool:
    """R13: trailing discretionary turnover plus this cycle's stays within the window budget."""
    churn = risk_limits(policy).churn
    limit = {"7d": churn.turnover_7d_max, "30d": churn.turnover_30d_max}[window]
    return prior + this_cycle <= limit + _EPS
