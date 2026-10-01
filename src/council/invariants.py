"""The user's hard limits, hard-coded. Policy files may be STRICTER, never looser.

These are asserted at startup (cycle, watch, approve) and by tests. Changing them requires editing
code AND a tagged policy change — a deliberate, reviewable act, not a config tweak.
"""

from __future__ import annotations

from council.policy import Policy
from council.swing.policy import SwingPolicy

GROSS_HARD_MAX = 2.0            # gross exposure <= 2.0x NAV
HALT_AT_PEAK_FRACTION = 0.75    # kill switch: halt at -25% from the LIFETIME peak
STOP_LOSS_ON_EVERY_OPEN = True  # every open order carries a broker-side stop-loss
HUMAN_APPROVAL_REQUIRED = True  # no code path places an order without an operator approval
NEVER_TOUCH_MAIN_ACCOUNT = True # only Agent Portfolio tokens are ever loaded
# Single stocks (split for the swing book, swing-book.md rev 2 §3.7): a stock LONG is only ever real
# shares at 1x; a stock SHORT is only ever a 1x CFD with a broker stop at most
# SWING_MAX_SHORT_STOP_PCT away. Universe (satellite) stock lines stay real, long-only and unlevered.
STOCK_LONGS_REAL_1X = True
STOCK_SHORTS_CFD_1X_WITH_STOP = True
STOCKS_REAL_LONG_1X = STOCK_LONGS_REAL_1X   # the pre-swing name, kept for readers of older records
STOCK_SLEEVE_LIVE = False       # a committed stock-sleeve.yaml stays OUT of every runtime policy
                                # (live, dry run, rehearsal, approval) until the go-live commit
                                # flips this with a CHANGELOG policy entry
# The swing book (swing-book.md rev 2). SWING_BOOK_LIVE gates live swing entries: False means the
# swing stage produces nothing in live mode (rehearsal and paper tracking may still run it). It flips
# only in the go-live commit, with a CHANGELOG policy entry, after the smoke tickets.
SWING_BOOK_LIVE = False
# Code ceilings for `policy/swing.yaml` (the policy may be stricter, never looser).
SWING_MAX_OPEN = 6              # open swing trades (proposed-but-unexecuted entries count)
SWING_MAX_SHORT = 2             # open swing shorts
SWING_MAX_SIZE_NAV = 0.08       # one trade's size, fraction of NAV
SWING_MAX_NEW_7D = 6            # new swing entries per rolling 7 calendar days (the user's ceiling)
SWING_MAX_LONG_LOSS_NAV = 0.008   # planned loss at the stop, long
SWING_MAX_SHORT_LOSS_NAV = 0.005  # planned loss at the stop, short
SWING_MAX_LONG_STOP_PCT = 0.12
SWING_MAX_SHORT_STOP_PCT = 0.08
SWING_MAX_OPEN_RISK_NAV = 0.04  # sum of size x stop x gap multiplier (gap multiplier >= 1.5)
SWING_MIN_GAP_MULT = 1.5
SWING_MAX_TOTAL_SESSIONS = 20   # time stop including the one extension
SWING_MIN_NET_RR = 1.2          # net reward/risk floor (the policy may demand more)
SWING_MAX_ENTRY_VALID_MIN = 60  # an approved entry is stale after this many minutes
SWING_MAX_LLM_CALLS_PER_SLOT = 12   # 1 Scout + 5 Skeptic + bull + bear + 3 PM + 1 spare (user 2026-10-01)
SWING_MIN_DECLARED_COST_PCT_PER_LEG = 1.25   # public R is net of at least this cost per leg
SWING_MAX_BUDGET_PCT = 50       # S18: the council's swing budget, percent of NAV (user 2026-10-01)
# S15 brake and S17 drawdown scaling are protections the swing book ADDS (design §3.7 "stricter than
# today"): the policy may trigger them earlier or scale harder, never switch them off by moving the
# threshold out of reach.
SWING_BRAKE_MIN_PNL_NAV = -0.05          # the 30-day net brake fires at a loss no deeper than this
SWING_BRAKE_MIN_WINDOW_DAYS = 30         # ... measured over at least this many days
SWING_DD_SCALE_MIN_FROM_PEAK = -0.10     # drawdown scaling starts no later than -10% from the peak
SWING_DD_SCALE_MAX_SIZE_NAV = 0.04       # ... and then sizes at most 4% of NAV
SWING_DD_SCALE_MAX_OPEN = 3              # ... with at most 3 open swing trades
# eToro Licensed Content: the broker's news feed text (transparency-v2 §3.0; the user's decision of
# 2026-09-26: the feed serves the operator's personal use with their own account). The news role may
# read the feed whenever an Agent Portfolio is connected; `policy/council.yaml` `news.broker_feed:
# false` can only turn it off. Feed text is NEVER published (the public record may carry item ids,
# counts, times, instruments and the agents' own paraphrase only), and every private copy is purged
# within LICENSED_RETENTION_DAYS (`council purge-licensed`; the first cycle of each UTC day runs it).
BROKER_FEED_ENABLED = True      # the code ceiling: False stops every feed request, whatever the policy
LICENSED_RETENTION_DAYS = 7     # private copies of licensed text are kept at most this many days


class InvariantViolation(RuntimeError):
    pass


def check_policy(policy: Policy) -> None:
    risk = policy.risk
    if float(risk["gross"]["hard_max"]) > GROSS_HARD_MAX:
        raise InvariantViolation("risk.gross.hard_max exceeds the hard-coded 2.0x limit")
    if float(risk["gross"]["proposal_max"]) > float(risk["gross"]["hard_max"]):
        raise InvariantViolation("proposal gross above hard gross")
    if float(risk["killswitch"]["halt_at"]) < HALT_AT_PEAK_FRACTION:
        raise InvariantViolation("kill switch halts later than -25% from peak")
    if risk["killswitch"].get("peak", "lifetime") != "lifetime":
        raise InvariantViolation("kill switch must measure from the lifetime peak")
    if policy.universe.reference_gross_max > 1.0:
        raise InvariantViolation("reference book may not lever")
    if STOCK_LONGS_REAL_1X:
        _check_stocks_real_long_1x(policy)
    if policy.swing is not None:
        check_swing_policy(policy.swing)
        # Q-S9: "other" promises the Skeptic a different model from the Scout (the council model);
        # the same-model fallback must say so ("same"), because the public record shows the family.
        llm = policy.swing.llm
        scout = policy.council.get("model")
        if llm.skeptic_model_family == "other" and llm.skeptic_model == scout:
            raise InvariantViolation(
                f"policy/swing.yaml: skeptic_model_family 'other' but the Skeptic model {scout!r} is "
                "the Scout's (council.yaml model)")
        if llm.skeptic_model_family == "same" and llm.skeptic_model != scout:
            raise InvariantViolation(
                "policy/swing.yaml: skeptic_model_family 'same' but the Skeptic model differs from the "
                "Scout's (council.yaml model)")


def _check_stocks_real_long_1x(policy: Policy) -> None:
    """Repeats the `Universe` rule in code the policy files cannot relax (a policy object built with
    `model_copy` skips validators): a stock line is a satellite line of real shares, long only."""
    if float((policy.risk.get("leverage_caps") or {}).get("stock", 1)) != 1.0:
        raise InvariantViolation("risk.leverage_caps.stock must be 1: single stocks are never levered")
    for line in policy.universe.lines:
        if line.asset_class != "stock":
            continue
        if line.sleeve != "satellite":
            raise InvariantViolation(f"stock line {line.symbol} outside the satellite sleeve")
        if line.vehicles.short:
            raise InvariantViolation(f"stock line {line.symbol} has a short vehicle")
        if not line.vehicles.long or any(v.settlement != "real" for v in line.vehicles.long):
            raise InvariantViolation(f"stock line {line.symbol} has a vehicle that is not real shares")


def check_swing_policy(swing: SwingPolicy) -> None:
    """`policy/swing.yaml` against the code ceilings above: every number may be stricter, never
    looser. Raises `InvariantViolation` listing every breach."""
    s = swing
    looser: list[str] = []

    def above(name: str, value: float, ceiling: float) -> None:
        if value > ceiling + 1e-12:
            looser.append(f"{name} {value} above the code ceiling {ceiling}")

    def below(name: str, value: float, floor: float) -> None:
        if value < floor - 1e-12:
            looser.append(f"{name} {value} below the code floor {floor}")

    above("capacity.max_open", s.capacity.max_open, SWING_MAX_OPEN)
    above("capacity.max_short", s.capacity.max_short, SWING_MAX_SHORT)
    above("capacity.max_new_7d", s.capacity.max_new_7d, SWING_MAX_NEW_7D)
    above("capacity.max_open_risk_nav", s.capacity.max_open_risk_nav, SWING_MAX_OPEN_RISK_NAV)
    below("capacity.open_risk_gap_mult", s.capacity.open_risk_gap_mult, SWING_MIN_GAP_MULT)
    above("size.target_nav", s.size.target_nav, SWING_MAX_SIZE_NAV)
    above("size.max_loss_nav_at_stop", s.size.max_loss_nav_at_stop, SWING_MAX_LONG_LOSS_NAV)
    above("size.short_max_loss_nav_at_stop", s.size.short_max_loss_nav_at_stop, SWING_MAX_SHORT_LOSS_NAV)
    above("size.short_si_unknown_mult", s.size.short_si_unknown_mult, 1.0)
    above("stops.max_long_pct", s.stops.max_long_pct, SWING_MAX_LONG_STOP_PCT)
    above("stops.max_short_pct", s.stops.max_short_pct, SWING_MAX_SHORT_STOP_PCT)
    above("time_stop.max_total_sessions", s.time_stop.max_total_sessions, SWING_MAX_TOTAL_SESSIONS)
    below("targets.min_net_rr", s.targets.min_net_rr, SWING_MIN_NET_RR)
    above("entry_guard.valid_minutes", s.entry_guard.valid_minutes, SWING_MAX_ENTRY_VALID_MIN)
    above("llm.max_calls_per_slot", s.llm.max_calls_per_slot, SWING_MAX_LLM_CALLS_PER_SLOT)
    below("public_record.declared_cost_pct_per_leg", s.public_record.declared_cost_pct_per_leg,
          SWING_MIN_DECLARED_COST_PCT_PER_LEG)
    above("budget.max_pct", s.budget.max_pct, SWING_MAX_BUDGET_PCT)
    above("budget.default_pct", s.budget.default_pct, SWING_MAX_BUDGET_PCT)
    below("brake.pnl_nav", s.brake.pnl_nav, SWING_BRAKE_MIN_PNL_NAV)
    below("brake.window_days", s.brake.window_days, SWING_BRAKE_MIN_WINDOW_DAYS)
    below("drawdown_scale.from_peak", s.drawdown_scale.from_peak, SWING_DD_SCALE_MIN_FROM_PEAK)
    above("drawdown_scale.size_nav", s.drawdown_scale.size_nav, SWING_DD_SCALE_MAX_SIZE_NAV)
    above("drawdown_scale.max_open", s.drawdown_scale.max_open, SWING_DD_SCALE_MAX_OPEN)
    if not STOCK_SHORTS_CFD_1X_WITH_STOP and s.capacity.max_short > 0:
        looser.append("stock shorts are disabled in code (STOCK_SHORTS_CFD_1X_WITH_STOP is False)")
    if looser:
        raise InvariantViolation("policy/swing.yaml is looser than the code: " + "; ".join(looser))
