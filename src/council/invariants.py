"""The user's hard limits, hard-coded. Policy files may be STRICTER, never looser.

These are asserted at startup (cycle, watch, approve) and by tests. Changing them requires editing
code AND a tagged policy change — a deliberate, reviewable act, not a config tweak.
"""

from __future__ import annotations

from council.policy import Policy

GROSS_HARD_MAX = 2.0            # gross exposure <= 2.0x NAV
HALT_AT_PEAK_FRACTION = 0.75    # kill switch: halt at -25% from the LIFETIME peak
STOP_LOSS_ON_EVERY_OPEN = True  # every open order carries a broker-side stop-loss
HUMAN_APPROVAL_REQUIRED = True  # no code path places an order without an operator approval
NEVER_TOUCH_MAIN_ACCOUNT = True # only Agent Portfolio tokens are ever loaded
STOCKS_REAL_LONG_1X = True      # single stocks: real shares, long only, never levered
STOCK_SLEEVE_LIVE = False       # a committed stock-sleeve.yaml stays OUT of every runtime policy
                                # (live, dry run, rehearsal, approval) until the go-live commit
                                # flips this with a CHANGELOG policy entry
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
    if STOCKS_REAL_LONG_1X:
        _check_stocks_real_long_1x(policy)


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
