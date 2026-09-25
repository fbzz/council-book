"""Decision and leg state machines. The ledger refuses any transition not listed here.

Decision rules:
- Pending = awaiting_publication | proposed: only these can be approved, expire or be superseded.
- An approved decision either starts executing or is expired/blocked/rejected by the approval
  re-checks; execution ends completed | completed_partial | blocked | execution_unknown.
- execution_unknown is resolved only by `resume` (lookups + reconcile, never a new order) or by an
  operator review; blocked is cleared only by an operator review (reviewed_no_action).
- Terminal states never move again.
Priority: flatten > compliance > rebalance (policy risk.priority); a lower priority never
supersedes a pending higher one.
"""

from __future__ import annotations

from typing import Literal, get_args

from council.models.cycle import DecisionState
from council.models.plan import LegState

DECISION_STATES: frozenset[str] = frozenset(get_args(DecisionState))
LEG_STATES: frozenset[str] = frozenset(get_args(LegState))

PENDING_STATES: frozenset[str] = frozenset({"awaiting_publication", "proposed"})
BLOCKER_STATES: frozenset[str] = frozenset({"blocked", "execution_unknown"})
TERMINAL_STATES: frozenset[str] = frozenset(
    {"completed", "completed_partial", "rejected", "expired", "superseded", "reviewed_no_action"}
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "awaiting_publication": frozenset({"proposed", "rejected", "expired", "superseded", "blocked"}),
    "proposed": frozenset(
        {"approved", "rejected", "expired", "superseded", "blocked", "reviewed_no_action"}
    ),
    "approved": frozenset({"executing", "expired", "rejected", "blocked"}),
    "executing": frozenset({"completed", "completed_partial", "blocked", "execution_unknown"}),
    "execution_unknown": frozenset(
        {"completed", "completed_partial", "blocked", "reviewed_no_action"}
    ),
    "blocked": frozenset({"reviewed_no_action"}),
    **{state: frozenset() for state in TERMINAL_STATES},
}

DecisionKind = Literal["rebalance", "flatten", "compliance"]
PRIORITY: dict[str, int] = {"flatten": 3, "compliance": 2, "rebalance": 1}

LEG_ACTIVE_STATES: frozenset[str] = frozenset({"submitting", "submitted", "in_flight", "unknown"})
LEG_TERMINAL_STATES: frozenset[str] = frozenset(
    {"filled", "partially_filled", "rejected", "rejected_partial", "skipped"}
)
_RESOLUTIONS = frozenset({"filled", "partially_filled", "rejected", "rejected_partial"})

LEG_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"submitting", "skipped"}),
    # submitting -> submitting = a new attempt after a definite 429
    "submitting": frozenset({"submitting", "submitted", "in_flight", "unknown", "skipped"} | _RESOLUTIONS),
    "submitted": frozenset({"in_flight", "unknown"} | _RESOLUTIONS),
    "in_flight": frozenset({"in_flight", "unknown"} | _RESOLUTIONS),
    "unknown": frozenset({"submitted", "in_flight", "skipped"} | _RESOLUTIONS),
    **{state: frozenset() for state in LEG_TERMINAL_STATES},
}


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


def can_transition_leg(from_state: str, to_state: str) -> bool:
    return to_state in LEG_TRANSITIONS.get(from_state, frozenset())
