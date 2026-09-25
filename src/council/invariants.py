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
