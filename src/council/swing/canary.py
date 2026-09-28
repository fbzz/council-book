"""Keeping the Skeptic honest over time (design swing-book.md rev 2, §1.5; SW-3).

- WEEKLY CANARY: at the Monday 10:40 UTC slot (no market needed, never a US swing slot, so it never
  raises a swing slot's budget), the Skeptic gets one planted idea built by code from a real past
  event: the catalyst is >= 5 sessions old and the stock already moved >= 3 sigma in the idea's
  direction since. The idea carries `canary=True`, a code-side flag the model never sees (its input is
  rendered by the same blind `skeptic_input` as a real idea). Expected: `wait` or `reject` with
  `priced_in` mostly or fully; a `pass` is a MISSED canary. A canary ref can never reach the PM,
  the planner or the public idea list (`roles.guard_no_canary`, H11). Cost: 1 call a week.
- PASS-RATE ALARM: > 60% `pass` over the trailing 20 verdicts, or no `reject` in the trailing 10
  -> flag `skeptic_pass_rate`.
- BRAKE: two alarms within 30 days, or two missed canaries in a row, pause new entries until the
  operator reviews the prompt.

Pure: no I/O, no clock of its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from council.stocks.universe import try_normalise_id
from council.swing.facts import FactCard
from council.swing.models import ScoutIdea
from council.swing.roles import CatalystMeta, SwingIdea, VerdictOutcome

CANARY_WEEKDAY = 0                       # Monday
CANARY_SLOT = (10, 40)                   # UTC
MIN_AGE_SESSIONS = 5
MIN_MOVE_SIGMA = 3.0
PASS_RATE_WINDOW, PASS_RATE_MAX = 20, 0.60
REJECT_WINDOW = 10
ALARM_WINDOW_DAYS = 30
CANARY_REF = "idea:1"                     # indistinguishable from a real ref; the flag is code-side


class CanaryError(ValueError):
    """The past event does not qualify as a canary."""


@dataclass(frozen=True)
class PastEvent:
    """A real past event: the ticker, the direction it moved, the catalyst ids that caused it (all
    admitted), a code-written factual claim built from the item's official titles, and the fact card
    computed at the canary slot."""

    ticker: str
    side: str
    catalysts: tuple[CatalystMeta, ...]
    claim: str
    card: FactCard

    @property
    def catalyst_ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.catalysts)


def canary_due(slot: datetime) -> bool:
    return slot.weekday() == CANARY_WEEKDAY and (slot.hour, slot.minute) == CANARY_SLOT


def qualifies(event: PastEvent) -> str | None:
    """None when the event is a valid canary, else the reason."""
    f = event.card.fields
    age = f.get("news_age_sessions")
    sig = f.get("move_since_news_close_sigma")
    if not event.card.ok:
        return "card_not_ok"
    if not event.catalysts:
        return "no_catalyst"
    if not isinstance(age, int) or age < MIN_AGE_SESSIONS:
        return "catalyst_too_recent"
    if sig is None:
        return "no_move_sigma"
    directional = float(sig) if event.side == "long" else -float(sig)
    if directional < MIN_MOVE_SIGMA:
        return "move_too_small"
    return None


def build_canary(event: PastEvent) -> SwingIdea:
    """The planted idea. Only the fields the blind Skeptic reads carry content; the rest are fixed
    placeholders that the Skeptic never sees (and no later stage ever receives)."""
    reason = qualifies(event)
    if reason is not None:
        raise CanaryError(reason)
    line = try_normalise_id(event.ticker)
    if line is None or line != event.card.line_id:
        raise CanaryError("ticker_card_mismatch")
    idea = ScoutIdea(ticker=event.ticker, side=event.side, setup="news_continuation",  # type: ignore[arg-type]
                     catalyst_ids=list(event.catalyst_ids), catalyst_claim=event.claim,
                     thesis="canary", why_not_priced_in="canary", entry="now", stop_pct=0.05,
                     target_pct=0.1, time_stop_days=5, invalidation="canary")
    return SwingIdea(ref=CANARY_REF, idea=idea, line_id=line, canary=True, card=event.card)


def grade_canary(outcome: VerdictOutcome) -> str:
    """`caught` or `missed` (a failed call counts as missed: the check did not happen)."""
    v = outcome.verdict
    if v is None:
        return "missed"
    said_ok = v.verdict in ("wait", "reject") and v.priced_in in ("mostly", "fully")
    return "caught" if said_ok else "missed"


def pass_rate_alarm(verdicts: Sequence[str]) -> bool:
    """`verdicts`: the model's own verdicts (before code rules), oldest first, canaries excluded."""
    tail20 = list(verdicts)[-PASS_RATE_WINDOW:]
    if len(tail20) == PASS_RATE_WINDOW and sum(v == "pass" for v in tail20) / PASS_RATE_WINDOW > PASS_RATE_MAX:
        return True
    tail10 = list(verdicts)[-REJECT_WINDOW:]
    return len(tail10) == REJECT_WINDOW and "reject" not in tail10


def brake_engaged(alarms: Sequence[datetime], canary_grades: Sequence[str], *, now: datetime) -> bool:
    """Two alarms in 30 days, or the last two canaries missed."""
    recent = [a for a in alarms if now - a <= timedelta(days=ALARM_WINDOW_DAYS)]
    return len(recent) >= 2 or list(canary_grades)[-2:] == ["missed", "missed"]
