"""The swing book's two pauses (design swing-book.md rev 2, §3.1 S15 and §1.5; SW-4b).

- S15 BRAKE (`brake_on`): realised + open swing P&L over the trailing `brake.window_days`, net of
  ALL costs, as a fraction of the NAV. Each trade's cost is the larger of the declared public cost
  (`public_record.declared_cost_pct_per_leg`, both legs) and its private round trip priced at entry
  (`swing.costs.round_trip`, fixed fees included, kept under the runtime key `swing_trade_costs`);
  an open trade is charged its whole round trip. At or below `brake.pnl_nav` the brake engages and
  new swing entries drop `S15:brake_on` until the operator lifts it; exits always continue. An open
  trade without a mark makes the figure unknown: entries drop `S15:brake_unknown` (fail closed) and
  the latched state is left as it was. After a lift, trades closed before the lift no longer count
  (open trades always count in full), so a lift is not undone by the loss it was reviewed for.
- CANARY PAUSE (`brake_engaged`): two missed weekly canaries in a row, or two Skeptic pass-rate
  alarms (`canary.pass_rate_alarm`, counted on its false -> true edge) within 30 days. New swing
  entries drop `S15:brake_engaged` until the operator lifts it after reviewing the prompt.
- LIFT: `council swing brake --lift [--canary] --reason "..."` (operator terminal only). The reason
  is published on a public row (`publish.swing_brake_row`) and must survive the public text rules
  unchanged; engaging a pause also queues a public row (a fixed cause code, no number).
- Stop-at-once (§8.3): the S15 brake engaging twice within 60 days adds the flag
  `swing_brake_twice_60d` (the operator stops the book; code does not).

The state lives under the runtime key `swing_brake`; queued public rows under
`swing_brake_rows_pending` (the watch publishes and clears them). The P&L figure is private: it is
never stored, printed or published, only its consequence.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from council.swing.canary import ALARM_WINDOW_DAYS, pass_rate_alarm
from council.swing.models import CLOSED_STATES, OPEN_STATES

BRAKE_KEY = "swing_brake"
ROWS_PENDING_KEY = "swing_brake_rows_pending"
TRADE_COSTS_KEY = "swing_trade_costs"          # private: {trade id: round-trip cost, percent}
BRAKES = ("s15", "canary")
TWICE_WINDOW_DAYS = 60
MISSED_IN_A_ROW = 2
ALARMS_TO_PAUSE = 2
KEEP_LIFTS = 50
KEEP_COSTS = 400
REASON_MAX = 180
FLAG_TWICE = "swing_brake_twice_60d"


class BrakeError(ValueError):
    """A lift that cannot happen (no such pause on, or a reason that cannot be published)."""


@dataclass(frozen=True)
class BrakeEvent:
    brake: str                    # s15 | canary
    event: str                    # engaged | lifted
    at: datetime
    cause: str | None = None      # engaged: s15_net_loss | missed_canaries | pass_rate_alarms
    reason: str | None = None     # lifted: the operator's published words

    def row(self) -> dict[str, Any]:
        from council.publish.swing_brake_row import row_id

        out: dict[str, Any] = {"id": row_id(self.at.strftime("%Y-%m-%dT%H%MZ"), self.brake, self.event),
                               "brake": self.brake, "event": self.event}
        if self.cause:
            out["cause"] = self.cause
        if self.reason:
            out["reason"] = self.reason
        return out


# ------------------------------------------------------------------------------------ state
def empty_state() -> dict[str, Any]:
    return {"s15": {"on": False, "since": None, "fired": [], "lifted_at": None, "unknown": False},
            "canary": {"on": False, "since": None, "cause": None, "lifted_at": None, "missed_streak": 0,
                       "alarms": [], "alarm_active": False},
            "lifts": []}


def normalise(raw: Any) -> dict[str, Any]:
    """A stored state merged over the empty one (an old or partial record never raises)."""
    out = empty_state()
    if isinstance(raw, Mapping):
        for part in BRAKES:
            if isinstance(raw.get(part), Mapping):
                out[part].update({k: v for k, v in raw[part].items() if k in out[part]})
        if isinstance(raw.get("lifts"), list):
            out["lifts"] = list(raw["lifts"])[-KEEP_LIFTS:]
    return out


def load(ledger: Any) -> dict[str, Any]:
    try:
        return normalise(ledger.get_runtime(BRAKE_KEY))
    except Exception:  # noqa: BLE001 - an old ledger: no pause recorded
        return empty_state()


def save(ledger: Any, state: Mapping[str, Any], now: datetime) -> None:
    ledger.set_runtime(BRAKE_KEY, dict(state), now=now)


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def is_on(state: Mapping[str, Any], brake: str) -> bool:
    return bool((state.get(brake) or {}).get("on"))


# ------------------------------------------------------------------------------------ S15 figure
def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def s15_pnl_nav(trades: Iterable[Any], marks: Mapping[str, float], *, now: datetime, window_days: int,
                declared_cost_pct_per_leg: float, costs: Mapping[str, float] | None = None,
                since: datetime | None = None) -> float | None:
    """Realised + open swing P&L as a fraction of the NAV, net of all costs (module rules), or None
    when an open trade has no mark or entry rate, or a closed trade in the window has no result.
    `marks` = {trade id: current rate} (private)."""
    start = now - timedelta(days=window_days)
    if since is not None and since > start:
        start = since
    declared_rt = 2.0 * declared_cost_pct_per_leg / 100.0
    costs = costs or {}
    total = 0.0
    for t in trades:
        d = t.detail or {}
        size = _num(d.get("size_nav"))
        if size is None or size <= 0:
            continue
        sign = 1.0 if t.side == "long" else -1.0
        private = _num(costs.get(t.trade_id))
        cost_rt = max(declared_rt, (private or 0.0) / 100.0)
        if t.state in CLOSED_STATES:
            if t.closed_at is None or t.closed_at < start:
                continue
            if t.close_rate and t.open_rate:
                gross = sign * (float(t.close_rate) / float(t.open_rate) - 1.0)
            elif _num(d.get("net_ret")) is not None:
                gross = float(d["net_ret"]) + declared_rt      # the outcome is net of the declared cost
            else:
                return None
            total += size * (gross - cost_rt)
        elif t.state in OPEN_STATES:
            mark = _num(marks.get(t.trade_id))
            if not t.open_rate or mark is None or mark <= 0:
                return None
            total += size * (sign * (mark / float(t.open_rate) - 1.0) - cost_rt)
    return total


# ------------------------------------------------------------------------------------ step
def step(state: Mapping[str, Any], *, pnl_nav: float | None, threshold: float, verdicts: Sequence[str],
         now: datetime) -> tuple[dict[str, Any], list[BrakeEvent], list[str]]:
    """One evaluation (pure). Returns the new state, the pause events that engaged now, and flags."""
    st = normalise(copy.deepcopy(dict(state)))
    events: list[BrakeEvent] = []
    flags: list[str] = []
    s15 = st["s15"]
    s15["unknown"] = pnl_nav is None
    if pnl_nav is None:
        flags.append("swing_brake_unknown")
    elif not s15["on"] and pnl_nav <= threshold:
        s15.update(on=True, since=now.isoformat())
        s15["fired"] = [f for f in s15["fired"] if (_dt(f) or now) >= now - timedelta(days=TWICE_WINDOW_DAYS)]
        s15["fired"].append(now.isoformat())
        events.append(BrakeEvent("s15", "engaged", now, cause="s15_net_loss"))
    if s15["on"] and sum(1 for f in s15["fired"]
                         if (_dt(f) or now) >= now - timedelta(days=TWICE_WINDOW_DAYS)) >= 2:
        flags.append(FLAG_TWICE)

    can = st["canary"]
    alarm = bool(pass_rate_alarm(verdicts))
    if alarm and not can["alarm_active"]:
        can["alarms"] = [*can["alarms"], now.isoformat()]
        flags.append("skeptic_pass_rate")
    can["alarm_active"] = alarm
    can["alarms"] = [a for a in can["alarms"] if (_dt(a) or now) >= now - timedelta(days=ALARM_WINDOW_DAYS)]
    if not can["on"]:
        cause = None
        if int(can["missed_streak"] or 0) >= MISSED_IN_A_ROW:
            cause = "missed_canaries"
        elif len(can["alarms"]) >= ALARMS_TO_PAUSE:
            cause = "pass_rate_alarms"
        if cause is not None:
            can.update(on=True, since=now.isoformat(), cause=cause)
            events.append(BrakeEvent("canary", "engaged", now, cause=cause))
    for part in BRAKES:
        if st[part]["on"]:
            flags.append(f"swing_paused:{part}")
    return st, events, flags


def note_canary(state: Mapping[str, Any], grade: str) -> dict[str, Any]:
    """Count consecutive missed canaries (a caught one resets the streak)."""
    st = normalise(copy.deepcopy(dict(state)))
    if grade == "missed":
        st["canary"]["missed_streak"] = int(st["canary"]["missed_streak"] or 0) + 1
    elif grade == "caught":
        st["canary"]["missed_streak"] = 0
    return st


def public_reason(text: str) -> str:
    """The lift reason exactly as it will be published, or BrakeError when publishing would change it
    (an amount, a long number, an id, a link, a path or an e-mail address) or it is empty/too long."""
    from council.publish.redact import clean_text, literal_ok

    words = " ".join((text or "").split())
    if not words:
        raise BrakeError("a reason is required (it is published)")
    if len(words) > REASON_MAX or not literal_ok(words) or clean_text(words, REASON_MAX) != words:
        raise BrakeError(f"the reason is published: at most {REASON_MAX} characters and no amounts, ids, "
                         "number runs, links, paths or e-mail addresses")
    return words


def lift(state: Mapping[str, Any], brake: str, reason: str, now: datetime) -> tuple[dict[str, Any], BrakeEvent]:
    """The operator's lift of one pause (pure). Raises BrakeError when it is not on."""
    if brake not in BRAKES:
        raise BrakeError(f"unknown pause {brake!r}")
    words = public_reason(reason)
    st = normalise(copy.deepcopy(dict(state)))
    if not st[brake]["on"]:
        raise BrakeError(f"the {'canary pause' if brake == 'canary' else 'S15 brake'} is not on")
    st[brake].update(on=False, since=None, lifted_at=now.isoformat())
    if brake == "canary":
        # the reviewed evidence no longer counts; a still-active alarm must clear and fire again
        st["canary"].update(cause=None, missed_streak=0, alarms=[])
    st["lifts"] = [*st["lifts"], {"at": now.isoformat(), "brake": brake, "reason": words}][-KEEP_LIFTS:]
    return st, BrakeEvent(brake, "lifted", now, reason=words)


# ------------------------------------------------------------------------------------ ledger glue
def queue_rows(ledger: Any, events: Iterable[BrakeEvent], now: datetime) -> None:
    rows = [e.row() for e in events]
    if not rows:
        return
    pending = list(ledger.get_runtime(ROWS_PENDING_KEY, []) or [])
    ids = {r.get("id") for r in pending if isinstance(r, Mapping)}
    ledger.set_runtime(ROWS_PENDING_KEY, [*pending, *[r for r in rows if r["id"] not in ids]], now=now)


def record_trade_cost(ledger: Any, trade_id: str, cost_rt_pct: float | None, now: datetime) -> None:
    """Keep one entry's private round-trip cost (percent of the position) for the S15 figure."""
    if _num(cost_rt_pct) is None:
        return
    costs = dict(ledger.get_runtime(TRADE_COSTS_KEY, {}) or {})
    costs[trade_id] = round(float(cost_rt_pct), 6)  # type: ignore[arg-type]
    if len(costs) > KEEP_COSTS:
        costs = dict(list(costs.items())[-KEEP_COSTS:])
    ledger.set_runtime(TRADE_COSTS_KEY, costs, now=now)


def trade_costs(ledger: Any) -> dict[str, float]:
    try:
        raw = ledger.get_runtime(TRADE_COSTS_KEY, {}) or {}
    except Exception:  # noqa: BLE001
        return {}
    return {str(k): float(v) for k, v in raw.items() if _num(v) is not None}


def lift_in_ledger(ledger: Any, brake: str, reason: str, now: datetime) -> BrakeEvent:
    """`council swing brake --lift`: lift, save, and queue the public row. Raises BrakeError."""
    st, event = lift(load(ledger), brake, reason, now)
    save(ledger, st, now)
    queue_rows(ledger, [event], now)
    return event


def status_lines(state: Mapping[str, Any]) -> list[str]:
    """Private terminal lines (codes and dates only; the P&L figure is never shown)."""
    st = normalise(state)
    s15, can = st["s15"], st["canary"]
    lines = [f"S15 brake: {'ON since ' + str(s15['since'])[:16] if s15['on'] else 'off'}"
             + (" (figure unknown: entries held)" if s15["unknown"] else ""),
             f"canary pause: {'ON since ' + str(can['since'])[:16] + ' (' + str(can['cause']) + ')' if can['on'] else 'off'}"
             f"; missed canaries in a row {int(can['missed_streak'] or 0)}; pass-rate alarms (30d) {len(can['alarms'])}"]
    for lf in st["lifts"][-3:]:
        lines.append(f"lifted {lf.get('brake')} at {str(lf.get('at'))[:16]}: {lf.get('reason')}")
    return lines
