"""Decision and leg state machines. The ledger refuses any transition not listed here.

Decision rules:
- Pending = awaiting_publication | proposed: only these can be approved, expire or be superseded.
- An approved decision either starts executing or is expired/blocked/rejected by the approval
  re-checks; execution ends completed | completed_partial | blocked | execution_unknown.
- execution_unknown is resolved only by `resume` (lookups + reconcile, never a new order) or by an
  operator review; blocked is cleared only by an operator review (reviewed_no_action).
- waiting_for_market: an order the broker holds until its market opens (status 11). The watch
  resolves it read-only (filled → completed/completed_partial; cancelled/rejected → skipped legs,
  completed_partial); unresolved one hour after the next full session closes it becomes blocked. The
  operator may record the outcome (`council ops resolve`, → reviewed_no_action).
Blockers: blocked, execution_unknown and any unknown leg hold every line (scope `all`); a
waiting_for_market decision holds every line too unless every waiting leg is a stock order, which
holds only the satellite sleeve (scope `satellite`; `blockers()` reports it as "satellite:<id>").
Such a hold that times out to blocked keeps its satellite scope; a broken fill resets it to `all`.
A decision whose every unresolved leg is a swing leg may carry scope `swing` (swing-book design
§4.5): it is reported as "swing:<id>" and halts NEW SWING ENTRIES only, never the core or satellite;
a swing trade in `entry_unknown` / `open_tp_missing` is reported as "swing:trade:<id>" the same way.
A core-scoped unknown still halts everything.
Swing trades (SW-2b): `swing_trades.state` moves only along `council.swing.models.TRANSITIONS`
(`check_trade_transition` raises `IllegalTransition`); a closed or missed trade never moves again.
- Terminal states never move again.
Priority: flatten > compliance > rebalance > smoke (policy risk.priority); a lower priority never
supersedes a pending higher one. A `smoke` ticket (m5-readiness §8, operator-proposed onboarding
test) has priority 0 under the same rule: any other decision (a flatten first of all) supersedes a
pending smoke ticket, and a smoke ticket supersedes nothing but an older smoke ticket.
"""

from __future__ import annotations

from typing import Literal, get_args

from council.models.cycle import DecisionState
from council.models.plan import LegState
from council.swing.models import CLOSED_STATES as TRADE_CLOSED_STATES  # noqa: F401 (re-export)
from council.swing.models import OPEN_STATES as TRADE_OPEN_STATES  # noqa: F401 (re-export)
from council.swing.models import SWING_BLOCKING_STATES, IllegalTransition  # noqa: F401 (re-export)
from council.swing.models import TERMINAL_STATES as TRADE_TERMINAL_STATES
from council.swing.models import TRADE_STATES as _TRADE_STATES
from council.swing.models import TRANSITIONS as TRADE_TRANSITIONS
from council.swing.models import check_transition as _check_trade_transition

DECISION_STATES: frozenset[str] = frozenset(get_args(DecisionState))
LEG_STATES: frozenset[str] = frozenset(get_args(LegState))

PENDING_STATES: frozenset[str] = frozenset({"awaiting_publication", "proposed"})
BLOCKER_STATES: frozenset[str] = frozenset({"blocked", "execution_unknown"})
WAITING_STATE = "waiting_for_market"
BLOCKER_SCOPES: frozenset[str] = frozenset({"all", "satellite", "swing"})
SATELLITE_BLOCKER_PREFIX = "satellite:"   # a blocker id that holds only the satellite sleeve
SWING_BLOCKER_PREFIX = "swing:"           # a blocker id that holds only new swing entries
TERMINAL_STATES: frozenset[str] = frozenset(
    {"completed", "completed_partial", "rejected", "expired", "superseded", "reviewed_no_action"}
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "awaiting_publication": frozenset({"proposed", "rejected", "expired", "superseded", "blocked"}),
    "proposed": frozenset(
        {"approved", "rejected", "expired", "superseded", "blocked", "reviewed_no_action"}
    ),
    "approved": frozenset({"executing", "expired", "rejected", "blocked"}),
    "executing": frozenset(
        {"completed", "completed_partial", "blocked", "execution_unknown", WAITING_STATE}
    ),
    "execution_unknown": frozenset(
        {"completed", "completed_partial", "blocked", "reviewed_no_action", WAITING_STATE}
    ),
    WAITING_STATE: frozenset({"completed", "completed_partial", "blocked", "reviewed_no_action"}),
    "blocked": frozenset({"reviewed_no_action"}),
    **{state: frozenset() for state in TERMINAL_STATES},
}

DecisionKind = Literal["rebalance", "flatten", "compliance", "smoke"]
PRIORITY: dict[str, int] = {"flatten": 3, "compliance": 2, "rebalance": 1, "smoke": 0}
SMOKE_KIND = "smoke"

LEG_ACTIVE_STATES: frozenset[str] = frozenset({"submitting", "submitted", "in_flight", "unknown"})
LEG_TERMINAL_STATES: frozenset[str] = frozenset(
    {"filled", "partially_filled", "rejected", "rejected_partial", "skipped"}
)
_RESOLUTIONS = frozenset({"filled", "partially_filled", "rejected", "rejected_partial"})

LEG_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"submitting", "skipped"}),
    # submitting -> submitting = a new attempt after a definite 429
    "submitting": frozenset({"submitting", "submitted", "in_flight", "unknown", "skipped"} | _RESOLUTIONS),
    "submitted": frozenset({"in_flight", "unknown", WAITING_STATE} | _RESOLUTIONS),
    "in_flight": frozenset({"in_flight", "unknown", WAITING_STATE} | _RESOLUTIONS),
    "unknown": frozenset({"submitted", "in_flight", "skipped", WAITING_STATE} | _RESOLUTIONS),
    # a held order: filled/rejected when the market opens, skipped when cancelled (nothing filled)
    WAITING_STATE: frozenset({"unknown", "skipped"} | _RESOLUTIONS),
    **{state: frozenset() for state in LEG_TERMINAL_STATES},
}


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


def can_transition_leg(from_state: str, to_state: str) -> bool:
    return to_state in LEG_TRANSITIONS.get(from_state, frozenset())


# ---------------------------------------------------------------- blocker scopes
def blocker_scope_of(blocker: str) -> str:
    """`swing` for "swing:<id>", `satellite` for "satellite:<id>", else `all`."""
    text = str(blocker)
    if text.startswith(SWING_BLOCKER_PREFIX):
        return "swing"
    if text.startswith(SATELLITE_BLOCKER_PREFIX):
        return "satellite"
    return "all"


# ---------------------------------------------------------------- swing trades (SW-2b)
TRADE_STATES: frozenset[str] = frozenset(_TRADE_STATES)
TRADE_ACTIVE_STATES: frozenset[str] = TRADE_STATES - TRADE_TERMINAL_STATES


def can_transition_trade(from_state: str, to_state: str) -> bool:
    return to_state in TRADE_TRANSITIONS.get(from_state, frozenset())


def check_trade_transition(from_state: str, to_state: str) -> str:
    """Return `to_state` when legal, else raise `IllegalTransition` (terminal trades never move)."""
    return _check_trade_transition(from_state, to_state)
