"""Execute an APPROVED plan against the broker, leg by leg, failing closed.

Rules (each one has a chaos test against broker/fake.py):
- The decision must be `approved`. It moves to `executing`, then to exactly one of completed,
  completed_partial, blocked or execution_unknown.
- Every leg is persisted as `submitting` with request_id = uuid5(NAMESPACE, "decision:seq:attempt")
  BEFORE its HTTP call, so a crash between submit and persist can always be looked up.
- One limiter (18 per 60 s) paces every write. A definite 429 pauses the limiter for Retry-After
  and retries with attempt + 1, i.e. a NEW request id (the 429 proves nothing was placed). Any
  other definite rejection is final for that leg.
- An ambiguous write (timeout, transport error, 5xx) is NEVER resubmitted. Opens are looked up by
  referenceId and closes confirmed by re-reading the portfolio, for up to 60 s: found → continue;
  not found → the leg is unknown, the decision becomes execution_unknown and nothing else is sent.
- Opens are polled on orders:lookup at the poll schedule (default 1/2/4/8/15/30/60 s = 120 s):
  3 filled; 5 partially filled once 60 s have passed; 4/7/8 rejected; 9/10 rejected_partial;
  still in flight (or never found) at the end → unknown.
- Order: closes (plan order) → stop-loss modifications → refresh equity → opens (plan order).
  Open units are re-derived from fresh equity (approved units × equity_now / NAV at approval) but
  never above approved units × 1.02. An open whose dependencies did not fill is skipped.
- After every open fill, exposure (filled units × fill price, and the broker-reported exposure)
  must be within risk.approval.post_fill_exposure_tolerance of the intended exposure (units sent
  × planned price); otherwise the decision is BLOCKED and nothing else is sent.
- The first rejected open, or any rejected close, stops the remaining opens → completed_partial.
- Final state: unknown leg → execution_unknown; blocked → blocked; unreadable portfolio, missing
  stop-loss, unknown position or a broken fill → blocked; any rejected/skipped/partial leg →
  completed_partial; drift ≤ risk.reconcile.drift_max → completed; otherwise blocked.
- `resume` does lookups and reconcile ONLY and never sends an order. A leg that provably never
  reached the broker (clean not-found) is marked skipped: it needs a fresh proposal and approval.
- One executor at a time: an exclusive lock file in the private state dir.
"""

from __future__ import annotations

import fcntl
import math
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

from pydantic import Field

from council.broker.http import (
    RETRY_AFTER_DEFAULT_S,
    AmbiguousWriteError,
    BrokerError,
    DefiniteRejection,
)
from council.broker.instruments import InstrumentMap
from council.broker.parsing import (
    STATUS_FAILED,
    STATUS_FAILED_AFTER_PARTIAL,
    STATUS_FILLED,
    STATUS_IN_FLIGHT,
    STATUS_PARTIALLY_FILLED,
    OrderStatus,
    PortfolioRead,
    as_int,
    close_order_id,
    parse_close_order,
    parse_order_status,
    parse_pnl,
    pick,
    snapshot_from_portfolio,
)
from council.clock import utcnow
from council.execution.planner import vehicle_to_line
from council.execution.ratelimit import TokenBucket
from council.execution.reconcile import ExpectedPosition, ReconcileResult, reconcile
from council.ledger.db import Ledger, LegRow
from council.ledger.states import LEG_ACTIVE_STATES, can_transition
from council.models.common import Direction, Strict
from council.models.cycle import DecisionState
from council.models.plan import Leg, Plan
from council.paths import state_dir
from council.policy import Policy, default_policy

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "council-book/execution/request-id/v1")
DEFAULT_POLL_SCHEDULE: tuple[float, ...] = (1, 2, 4, 8, 15, 30, 60)
AMBIGUITY_WINDOW_S = 60.0
PARTIAL_WINDOW_S = 60.0
MAX_WRITE_ATTEMPTS = 3
UNITS_REFRESH_CAP = 1.02
CLOSE_UNITS_TOLERANCE = 0.01
LOCK_FILE = "exec.lock"

T = TypeVar("T")


def leg_request_id(decision_id: str, seq: int, attempt: int) -> str:
    """Deterministic x-request-id / idempotency key for one attempt of one leg."""
    return str(uuid.uuid5(NAMESPACE, f"{decision_id}:{seq}:{attempt}"))


class ExecutionError(RuntimeError):
    pass


class ExecutionLocked(ExecutionError):  # noqa: N818 - reads as a state
    pass


class WriteClient(Protocol):
    def open_order(
        self, *, request_id: str, instrument_id: int, transaction: Any, settlement: str,
        leverage: int, units: float, stop_loss_rate: float | None, stop_loss_type: str = "fixed",
    ) -> dict[str, Any]: ...

    def close_position(
        self, *, request_id: str, position_id: int, instrument_id: int,
        units_to_deduct: float | None = None,
    ) -> dict[str, Any]: ...

    def patch_stop_loss(
        self, *, request_id: str, position_id: int, stop_loss_rate: float
    ) -> dict[str, Any]: ...


class ReadClient(Protocol):
    def pnl(self) -> dict[str, Any]: ...

    def order_lookup(
        self, reference_id: str | None = None, order_id: int | None = None
    ) -> dict[str, Any] | None: ...

    def close_order_info(self, order_id: int) -> dict[str, Any] | None: ...


class LegResult(Strict):
    """PRIVATE: order/position ids and units."""

    seq: int
    kind: str
    symbol: str
    line: str
    state: str
    attempts: int = 0
    order_id: int | None = None
    position_ids: list[int] = Field(default_factory=list)
    units_requested: float | None = None
    units_filled: float | None = None
    fill_price: float | None = None
    error: str = ""


class ExecutionReport(Strict):
    """PRIVATE execution summary (USD equity, ids). Publishing derives percentages from it."""

    decision_id: str
    final_state: DecisionState
    legs: list[LegResult]
    reasons: list[str] = Field(default_factory=list)
    reconcile: ReconcileResult | None = None
    equity_before: float | None = None
    equity_after: float | None = None
    writes_sent: int = 0


@dataclass(frozen=True)
class _LegCtx:
    seq: int
    kind: str
    symbol: str
    line: str
    instrument_id: int | None
    direction: Direction
    settlement: str | None
    leverage: int
    units: float | None
    amount_usd: float | None
    sl_rate: float | None
    position_id: int | None
    depends_on: tuple[int, ...]

    @property
    def planned_price(self) -> float | None:
        if self.units and self.amount_usd and self.units > 0 and self.amount_usd > 0:
            return self.amount_usd / self.units
        return None

    @classmethod
    def from_leg(cls, leg: Leg, line: str) -> _LegCtx:
        return cls(
            seq=leg.seq, kind=leg.kind, symbol=leg.symbol, line=line,
            instrument_id=leg.instrument_id, direction=leg.direction, settlement=leg.settlement,
            leverage=leg.leverage, units=leg.units, amount_usd=leg.amount_usd,
            sl_rate=leg.sl_rate, position_id=leg.position_id, depends_on=tuple(leg.depends_on),
        )

    @classmethod
    def from_row(cls, row: LegRow) -> _LegCtx:
        return cls(
            seq=row.seq, kind=row.kind, symbol=row.vehicle_symbol, line=row.line,
            instrument_id=row.instrument_id, direction=row.direction,  # type: ignore[arg-type]
            settlement=row.settlement, leverage=row.leverage, units=row.units,
            amount_usd=row.amount_usd, sl_rate=row.sl_rate, position_id=row.position_id,
            depends_on=tuple(row.depends_on),
        )


@dataclass
class _Run:
    decision_id: str
    legs: list[_LegCtx]
    before: PortfolioRead | None = None
    reasons: list[str] = field(default_factory=list)
    expected: list[ExpectedPosition] = field(default_factory=list)
    unknown: bool = False
    blocked: bool = False
    partial: bool = False
    stop_opens: bool = False
    writes: int = 0

    def reason(self, text: str) -> None:
        if text not in self.reasons:
            self.reasons.append(text)


@dataclass(frozen=True)
class _Sent:
    outcome: Literal["accepted", "rejected", "ambiguous", "invalid"]
    request_id: str
    submitted_at: datetime
    payload: dict[str, Any] | None = None
    error: str = ""


@dataclass(frozen=True)
class _Found:
    status: OrderStatus | None
    clean: bool          # every lookup answered: a None status is a definitive "not found"


class Executor:
    def __init__(
        self,
        write: WriteClient | None,
        read: ReadClient,
        ledger: Ledger,
        limiter: TokenBucket,
        *,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        poll_schedule: Sequence[float] = DEFAULT_POLL_SCHEDULE,
        policy: Policy | None = None,
        symbol_for: Mapping[int, str] | Callable[[int], str | None] | None = None,
        whole_units: Mapping[str, bool] | None = None,
        lock_path: Path | None = None,
    ) -> None:
        self.write = write
        self.read = read
        self.ledger = ledger
        self.limiter = limiter
        self.clock = clock
        self.sleep = sleep
        self.poll_schedule = tuple(float(s) for s in poll_schedule)
        self.policy = policy or default_policy()
        self._symbol_source = symbol_for
        self._whole_units = dict(whole_units or {})
        self._symbols: dict[int, str] = {}
        self._symbol_fn: Callable[[int], str | None] | None = None
        self.lock_path = lock_path
        self._v2l = vehicle_to_line(self.policy.universe)
        self._exposure_tol = float(self.policy.risk["approval"]["post_fill_exposure_tolerance"])
        self._sl_tol = float(self.policy.risk["reconcile"]["sl_rate_tolerance"])

    # ================================================================== public
    def execute(self, decision_id: str, plan: Plan, *, nav_usd: float) -> ExecutionReport:
        """Run an approved plan. `nav_usd` is the equity the plan was sized with."""
        if self.write is None:
            raise ExecutionError("execute needs a write client")
        if not (math.isfinite(nav_usd) and nav_usd > 0):
            raise ValueError("nav_usd must be positive")
        seqs = [leg.seq for leg in plan.legs]
        if len(set(seqs)) != len(seqs):
            raise ValueError("plan legs must have unique seq numbers")
        decision = self.ledger.get_decision(decision_id)
        if decision.state != "approved":
            raise ExecutionError(f"{decision_id} is {decision.state}, not approved")
        ordered = sorted(plan.legs, key=lambda leg: leg.seq)
        legs = [_LegCtx.from_leg(leg, self._line(leg.symbol)) for leg in ordered]
        with self._lock():
            self._load_symbols(legs)
            before = self._portfolio()            # a failing read changes nothing
            run = _Run(decision_id, legs, before=before)
            now = self.clock()
            self.ledger.record_positions(now, before.positions, decision_id=decision_id, source="pre_execution")
            self.ledger.add_equity_mark(now, before.equity_usd, credit_usd=before.credit_usd, source="pre_execution")
            self.ledger.transition(decision_id, "executing", "approved plan: execution started", now=now)
            try:
                self.ledger.insert_legs(decision_id, ordered, line_of=self._line, now=now)
                self._run_closes(run)
                self._run_modifies(run)
                self._run_opens(run, nav_usd)
                return self._finish(run, skip_reason="not sent: execution stopped earlier")
            except Exception as exc:
                self._fail_closed(run, exc)
                raise

    def resume(self, decision_id: str) -> ExecutionReport:
        """Recover an interrupted or unknown execution with lookups and reconcile only."""
        decision = self.ledger.get_decision(decision_id)
        if decision.state not in ("executing", "execution_unknown"):
            raise ExecutionError(f"{decision_id} is {decision.state}; nothing to resume")
        rows = self.ledger.legs(decision_id)
        legs = [_LegCtx.from_row(r) for r in rows]
        with self._lock():
            self._load_symbols(legs)
            run = _Run(decision_id, legs, before=self._portfolio())
            try:
                for row, leg in zip(rows, legs, strict=True):
                    if row.state in LEG_ACTIVE_STATES:
                        self._recover(run, leg, row)
                    else:
                        self._account_existing(run, leg, row)
                return self._finish(
                    run, skip_reason="not sent before the interruption; needs a fresh approval"
                )
            except Exception as exc:
                self._fail_closed(run, exc)
                raise

    # ================================================================== phases
    def _run_closes(self, run: _Run) -> None:
        for leg in run.legs:
            if leg.kind in ("close", "partial_close"):
                if run.unknown or run.blocked:
                    return
                self._close_leg(run, leg)

    def _run_modifies(self, run: _Run) -> None:
        for leg in run.legs:
            if leg.kind == "modify_sl":
                if run.unknown or run.blocked:
                    return
                self._modify_leg(run, leg)

    def _run_opens(self, run: _Run, nav_usd: float) -> None:
        opens = [leg for leg in run.legs if leg.kind == "open"]
        if not opens or run.unknown or run.blocked or run.stop_opens:
            return
        try:
            fresh = self._portfolio()
        except BrokerError:
            run.reason("equity refresh failed; opens not sent")
            run.stop_opens = True
            return
        self.ledger.add_equity_mark(self.clock(), fresh.equity_usd, credit_usd=fresh.credit_usd, source="pre_opens")
        scale = fresh.equity_usd / nav_usd
        for leg in opens:
            if run.unknown or run.blocked or run.stop_opens:
                return
            if any(self.ledger.get_leg(run.decision_id, dep).state != "filled" for dep in leg.depends_on):
                self._skip(run, leg, "dependency did not fill")
                continue
            if not (leg.sl_rate and leg.sl_rate > 0) or not leg.units or leg.instrument_id is None or not leg.settlement:
                self._skip(run, leg, "invalid open leg (stop-loss, units, instrument or settlement missing)")
                run.stop_opens = True
                continue
            units = self._rederive_units(leg.units, scale, whole=self._whole_units.get(leg.symbol, False))
            if units <= 0:
                self._skip(run, leg, "no units left after the equity refresh")
                continue
            self._open_leg(run, leg, units)

    # ================================================================== closes
    def _close_leg(self, run: _Run, leg: _LegCtx) -> None:
        if leg.position_id is None or leg.instrument_id is None:
            self._skip(run, leg, "close leg without a position")
            run.stop_opens = True
            return
        pos = run.before.position(leg.position_id) if run.before else None
        if pos is None:
            self._skip(run, leg, "position no longer open")
            run.stop_opens = True
            run.reason(f"{leg.symbol}: position set changed; opens stopped")
            return
        units_before = pos.units
        deduct = (
            leg.units
            if leg.kind == "partial_close" and leg.units and leg.units < units_before * (1 - 1e-9)
            else None
        )
        position_id, instrument_id = leg.position_id, leg.instrument_id
        sent = self._submit(
            run, leg, {"position_units_before": units_before, "units_to_deduct": deduct},
            lambda rid: self.write.close_position(  # type: ignore[union-attr]
                request_id=rid, position_id=position_id, instrument_id=instrument_id,
                units_to_deduct=deduct,
            ),
        )
        if sent.outcome in ("rejected", "invalid"):
            self._resolve(run, leg, "rejected", error=sent.error)
            run.partial = run.stop_opens = True
            run.reason(f"{leg.symbol}: close rejected; opens stopped")
            return
        order_id = None
        if sent.outcome == "accepted":
            order_id = close_order_id(sent.payload)
            self._leg(run, leg, "submitted", order_id=order_id)
            verdict = self._await_close(position_id, units_before, deduct, order_id, self._window(), immediate=False)
        else:
            verdict = self._await_close(position_id, units_before, deduct, None, AMBIGUITY_WINDOW_S, immediate=True)
        self._settle_close(run, leg, verdict, order_id)

    def _settle_close(self, run: _Run, leg: _LegCtx, verdict: str, order_id: int | None) -> None:
        if verdict == "filled":
            self._resolve(run, leg, "filled", order_id=order_id, position_ids=[leg.position_id])
        elif verdict == "rejected":
            self._resolve(run, leg, "rejected", order_id=order_id, error="close order failed")
            run.partial = run.stop_opens = True
            run.reason(f"{leg.symbol}: close failed at the broker; opens stopped")
        else:
            self._resolve(run, leg, "unknown", order_id=order_id, error="close not confirmed")
            run.unknown = True
            run.reason(f"{leg.symbol}: close outcome unknown")

    def _await_close(
        self, position_id: int, units_before: float, deduct: float | None,
        order_id: int | None, window: float, *, immediate: bool,
    ) -> str:
        """'filled' once the portfolio shows the position gone (or reduced by the deducted
        units); 'rejected' if the close order reports an error; else 'unconfirmed'."""

        def check() -> str | None:
            if order_id is not None:
                try:
                    info = self.read.close_order_info(order_id)
                except BrokerError:
                    info = None
                if info is not None and parse_close_order(info).failed:
                    return "rejected"
            try:
                port = self._portfolio()
            except BrokerError:
                return None
            pos = port.position(position_id)
            if pos is None:
                return "filled"
            if deduct is not None and units_before - pos.units >= deduct * (1 - CLOSE_UNITS_TOLERANCE):
                return "filled"
            return None

        return self._poll(check, window, immediate=immediate) or "unconfirmed"

    # ================================================================== stop-loss PATCH
    def _modify_leg(self, run: _Run, leg: _LegCtx) -> None:
        pos = run.before.position(leg.position_id) if (run.before and leg.position_id) else None
        if pos is None or not leg.sl_rate:
            self._skip(run, leg, "stop-loss leg without an open position or a rate")
            return
        position_id, rate = pos.position_id, float(leg.sl_rate)
        sent = self._submit(
            run, leg, {},
            lambda rid: self.write.patch_stop_loss(  # type: ignore[union-attr]
                request_id=rid, position_id=position_id, stop_loss_rate=rate
            ),
        )
        if sent.outcome in ("rejected", "invalid"):
            self._resolve(run, leg, "rejected", error=sent.error)
            run.partial = True
            run.reason(f"{leg.symbol}: stop-loss change rejected")
            return
        if sent.outcome == "accepted":
            self._leg(run, leg, "submitted")
        window = self._window() if sent.outcome == "accepted" else AMBIGUITY_WINDOW_S
        if self._await_sl(position_id, rate, window, immediate=sent.outcome == "ambiguous"):
            self._resolve(run, leg, "filled", position_ids=[position_id])
            run.expected.append(ExpectedPosition(
                position_id=position_id, symbol=pos.symbol,
                direction="long" if pos.is_buy else "short", leverage=pos.leverage, sl_rate=rate,
            ))
        else:
            self._resolve(run, leg, "unknown", error="stop-loss change not confirmed")
            run.unknown = True
            run.reason(f"{leg.symbol}: stop-loss change unknown")

    def _await_sl(self, position_id: int, rate: float, window: float, *, immediate: bool) -> bool:
        def check() -> bool | None:
            try:
                pos = self._portfolio().position(position_id)
            except BrokerError:
                return None
            if pos is not None and pos.sl_rate and abs(pos.sl_rate / rate - 1) <= self._sl_tol:
                return True
            return None

        return bool(self._poll(check, window, immediate=immediate))

    # ================================================================== opens
    def _open_leg(self, run: _Run, leg: _LegCtx, units: float) -> None:
        planned_price = leg.planned_price
        instrument_id, settlement, sl_rate = leg.instrument_id, leg.settlement, leg.sl_rate
        transaction = "buy" if leg.direction == "long" else "sellShort"
        sent = self._submit(
            run, leg, {"units_sent": units, "planned_price": planned_price},
            lambda rid: self.write.open_order(  # type: ignore[union-attr]
                request_id=rid, instrument_id=instrument_id, transaction=transaction,
                settlement=settlement, leverage=leg.leverage, units=units, stop_loss_rate=sl_rate,
            ),
        )
        if sent.outcome in ("rejected", "invalid"):
            self._resolve(run, leg, "rejected", error=sent.error)
            run.partial = run.stop_opens = True
            run.reason(f"{leg.symbol}: open rejected; remaining opens stopped")
            return
        first: OrderStatus | None = None
        if sent.outcome == "ambiguous":
            found = self._find_open(sent.request_id, AMBIGUITY_WINDOW_S)
            if found.status is None:
                self._resolve(run, leg, "unknown", error="ambiguous write not found by referenceId")
                run.unknown = True
                run.reason(f"{leg.symbol}: open outcome unknown")
                return
            first = found.status
            self._leg(run, leg, "submitted", order_id=first.order_id)
        else:
            self._leg(run, leg, "submitted", order_id=as_int(pick(sent.payload, "orderId", "orderID")))
        status = self._poll_open(run, leg, sent.request_id, sent.submitted_at, first)
        self._settle_open(run, leg, status, units, planned_price)

    def _find_open(self, request_id: str, window: float) -> _Found:
        clean = True

        def check() -> OrderStatus | None:
            nonlocal clean
            try:
                payload = self.read.order_lookup(reference_id=request_id)
            except BrokerError:
                clean = False
                return None
            return parse_order_status(payload) if payload is not None else None

        status = self._poll(check, window, immediate=True)
        return _Found(status, clean)

    def _poll_open(
        self, run: _Run, leg: _LegCtx, request_id: str, submitted_at: datetime,
        first: OrderStatus | None,
    ) -> OrderStatus | None:
        last = first

        def terminal(status: OrderStatus | None) -> bool:
            if status is None:
                return False
            sid = status.status_id
            if sid == STATUS_FILLED or sid in STATUS_FAILED or sid in STATUS_FAILED_AFTER_PARTIAL:
                return True
            elapsed = (self.clock() - submitted_at).total_seconds()
            return sid == STATUS_PARTIALLY_FILLED and elapsed >= PARTIAL_WINDOW_S

        if terminal(last):
            return last

        def check() -> OrderStatus | None:
            nonlocal last
            try:
                payload = self.read.order_lookup(reference_id=request_id)
            except BrokerError:
                return None
            if payload is None:
                return None
            last = parse_order_status(payload)
            if last.status_id in STATUS_IN_FLIGHT:
                self._leg(run, leg, "in_flight", broker_status=f"{last.status_id}:{last.status_name}")
            return last if terminal(last) else None

        self._poll(check, self._window(), immediate=False)
        return last

    def _settle_open(
        self, run: _Run, leg: _LegCtx, status: OrderStatus | None, units: float,
        planned_price: float | None,
    ) -> None:
        if status is None:
            self._resolve(run, leg, "unknown", error="accepted order never found by lookup")
            run.unknown = True
            run.reason(f"{leg.symbol}: open outcome unknown")
            return
        sid = status.status_id
        common: dict[str, Any] = {
            "order_id": status.order_id,
            "position_ids": status.position_ids,
            "broker_status": f"{sid}:{status.status_name}",
            "detail": {
                "units_filled": status.filled_units,
                "fill_price": status.avg_price,
                "broker_exposure": status.broker_exposure_usd,
            },
        }
        if sid in (STATUS_FILLED, STATUS_PARTIALLY_FILLED):
            full = sid == STATUS_FILLED
            self._expect(run, leg, status)
            mismatch = self._exposure_mismatch(status, units, planned_price, full=full)
            self._resolve(run, leg, "filled" if full else "partially_filled", error=mismatch, **common)
            if not full:
                run.partial = True
                run.reason(f"{leg.symbol}: open partially filled")
            if mismatch:
                run.blocked = True
                run.reason(f"{leg.symbol}: post-fill exposure check failed ({mismatch})")
        elif sid in STATUS_FAILED:
            self._resolve(run, leg, "rejected", error=status.error_message or "rejected", **common)
            run.partial = run.stop_opens = True
            run.reason(f"{leg.symbol}: open rejected; remaining opens stopped")
        elif sid in STATUS_FAILED_AFTER_PARTIAL:
            self._expect(run, leg, status)
            self._resolve(run, leg, "rejected_partial", error=status.error_message or "rejected after a partial fill", **common)
            run.partial = run.stop_opens = True
            run.reason(f"{leg.symbol}: open rejected after a partial fill; remaining opens stopped")
        else:
            self._resolve(run, leg, "unknown", error=f"still in flight (status {sid})", **common)
            run.unknown = True
            run.reason(f"{leg.symbol}: open still in flight at the end of the poll window")

    def _exposure_mismatch(
        self, status: OrderStatus, units: float, planned_price: float | None, *, full: bool
    ) -> str | None:
        """Post-fill check. Full fill: units × fill price and the broker exposure vs units sent ×
        planned price. Partial fill: fill price vs planned price (and broker exposure per unit)."""
        tol = self._exposure_tol
        price = status.avg_price
        filled = status.filled_units
        if not planned_price or not price or filled <= 0:
            return "fill exposure unverifiable"
        intended = (units if full else filled) * planned_price
        actual = filled * price
        if abs(actual / intended - 1) > tol:
            return "filled exposure outside tolerance"
        broker = status.broker_exposure_usd
        if broker is not None and abs(broker / intended - 1) > tol:
            return "broker-reported exposure outside tolerance"
        return None

    def _expect(self, run: _Run, leg: _LegCtx, status: OrderStatus) -> None:
        for pid in status.position_ids:
            run.expected.append(ExpectedPosition(
                position_id=pid, symbol=leg.symbol, direction=leg.direction,
                leverage=leg.leverage, sl_rate=leg.sl_rate,
            ))

    # ================================================================== resume
    def _recover(self, run: _Run, leg: _LegCtx, row: LegRow) -> None:
        if leg.kind == "open":
            found = self._find_open(row.request_id, AMBIGUITY_WINDOW_S) if row.request_id else _Found(None, True)
            if found.status is None:
                if found.clean and row.order_id is None and row.state in ("submitting", "unknown"):
                    self._resolve(run, leg, "skipped", error="not found at the broker: never placed; needs a fresh approval")
                    run.partial = True
                else:
                    self._resolve(run, leg, "unknown", error="still not found by lookup")
                    run.unknown = True
                    run.reason(f"{leg.symbol}: open outcome still unknown")
                return
            if row.state == "submitting" or row.order_id is None:
                self._leg(run, leg, "submitted", order_id=found.status.order_id)
            submitted_at = row.submitted_at or self.clock()
            status = self._poll_open(run, leg, row.request_id, submitted_at, found.status)  # type: ignore[arg-type]
            units = float(row.detail.get("units_sent") or row.units or 0.0)
            self._settle_open(run, leg, status, units, row.detail.get("planned_price") or leg.planned_price)
        elif leg.kind in ("close", "partial_close"):
            units_before = row.detail.get("position_units_before")
            if leg.position_id is None or units_before is None:
                self._resolve(run, leg, "unknown", error="close cannot be verified")
                run.unknown = True
                return
            verdict = self._await_close(
                leg.position_id, float(units_before), row.detail.get("units_to_deduct"),
                row.order_id, AMBIGUITY_WINDOW_S, immediate=True,
            )
            if verdict == "unconfirmed" and row.order_id is None and row.state in ("submitting", "unknown"):
                try:
                    pending = leg.position_id in self._portfolio().pending_close_position_ids
                except BrokerError:
                    pending = True
                if not pending:
                    self._resolve(run, leg, "skipped", error="close never executed; position intact")
                    run.partial = run.stop_opens = True
                    return
            self._settle_close(run, leg, verdict, row.order_id)
        else:
            ok = bool(leg.position_id and leg.sl_rate) and self._await_sl(
                leg.position_id, float(leg.sl_rate), AMBIGUITY_WINDOW_S, immediate=True  # type: ignore[arg-type]
            )
            if ok:
                self._resolve(run, leg, "filled", position_ids=[leg.position_id])
            elif row.state in ("submitting", "unknown"):
                self._resolve(run, leg, "skipped", error="stop-loss change not applied")
                run.partial = True
            else:
                self._resolve(run, leg, "unknown", error="stop-loss change not confirmed")
                run.unknown = True

    def _account_existing(self, run: _Run, leg: _LegCtx, row: LegRow) -> None:
        """Carry the outcome of legs that were already terminal into the resumed run."""
        if row.state in ("rejected", "skipped", "partially_filled", "rejected_partial"):
            run.partial = True
        if leg.kind == "open" and row.state in ("filled", "partially_filled", "rejected_partial"):
            for pid in row.position_ids:
                run.expected.append(ExpectedPosition(
                    position_id=pid, symbol=leg.symbol, direction=leg.direction,
                    leverage=leg.leverage, sl_rate=leg.sl_rate,
                ))
        if row.error and "exposure" in row.error:
            run.blocked = True
            run.reason(f"{leg.symbol}: post-fill exposure check failed earlier")

    # ================================================================== finish
    def _finish(self, run: _Run, *, skip_reason: str) -> ExecutionReport:
        now = self.clock()
        for row in self.ledger.legs(run.decision_id):
            if row.state == "planned":
                self.ledger.update_leg(run.decision_id, row.seq, state="skipped", error=skip_reason, resolved_at=now, now=now)
                run.partial = True
        rec: ReconcileResult | None = None
        after: PortfolioRead | None = None
        try:
            after = self._portfolio()
            snapshot = snapshot_from_portfolio(after, now)
            rec = reconcile(snapshot, self._targets(run.decision_id), run.expected, self.policy)
            self.ledger.record_positions(now, after.positions, decision_id=run.decision_id, source="post_execution")
            self.ledger.add_equity_mark(now, after.equity_usd, credit_usd=after.credit_usd, source="post_execution")
        except (BrokerError, ValueError) as exc:
            run.reason(f"post-execution reconcile unavailable ({type(exc).__name__})")
        final = self._final_state(run, rec)
        current = self.ledger.get_decision(run.decision_id).state
        reason = "; ".join(run.reasons) or final
        if current != final and can_transition(current, final):
            self.ledger.transition(run.decision_id, final, reason, now=now)
        else:
            self.ledger.note(run.decision_id, f"resume: still {current}: {reason}", now=now)
            final = current  # type: ignore[assignment]
        return self._report(run, final, rec, after)  # type: ignore[arg-type]

    def _final_state(self, run: _Run, rec: ReconcileResult | None) -> str:
        if run.unknown:
            return "execution_unknown"
        if run.blocked:
            return "blocked"
        if rec is None:
            return "blocked"
        if not rec.protected:
            if rec.missing_sl:
                run.reason("missing stop-loss: " + ", ".join(rec.missing_sl))
            if rec.unknown_positions:
                run.reason("unknown positions: " + ", ".join(rec.unknown_positions))
            for issue in rec.issues:
                run.reason(issue)
            return "blocked"
        if run.partial:
            return "completed_partial"
        if rec.drift_ok:
            return "completed"
        run.reason("drift above reconcile.drift_max")
        return "blocked"

    def _targets(self, decision_id: str) -> dict[str, float]:
        targets: dict[str, float] = {}
        for row in self.ledger.legs(decision_id):
            after = row.detail.get("weight_after")
            if after is not None:
                targets[row.line] = float(after)
        return targets

    def _report(self, run: _Run, final: DecisionState, rec: ReconcileResult | None, after: PortfolioRead | None) -> ExecutionReport:
        legs = [
            LegResult(
                seq=r.seq, kind=r.kind, symbol=r.vehicle_symbol, line=r.line, state=r.state,
                attempts=r.attempt + 1 if r.request_id else 0, order_id=r.order_id,
                position_ids=r.position_ids,
                units_requested=r.detail.get("units_sent", r.units),
                units_filled=r.detail.get("units_filled"), fill_price=r.detail.get("fill_price"),
                error=r.error or "",
            )
            for r in self.ledger.legs(run.decision_id)
        ]
        return ExecutionReport(
            decision_id=run.decision_id, final_state=final, legs=legs, reasons=run.reasons,
            reconcile=rec, equity_before=run.before.equity_usd if run.before else None,
            equity_after=after.equity_usd if after else None, writes_sent=run.writes,
        )

    def _fail_closed(self, run: _Run, exc: Exception) -> None:
        """Unexpected error mid-execution: in-flight legs become unknown, the decision
        execution_unknown. Best effort; the original exception is re-raised by the caller."""
        label = f"executor error: {type(exc).__name__}"
        try:
            for row in self.ledger.legs(run.decision_id):
                if row.state in ("submitting", "submitted", "in_flight"):
                    self.ledger.update_leg(run.decision_id, row.seq, state="unknown", error=label)
            state = self.ledger.get_decision(run.decision_id).state
            if can_transition(state, "execution_unknown"):
                self.ledger.transition(run.decision_id, "execution_unknown", label)
        except Exception:  # pragma: no cover - never mask the original failure
            pass

    # ================================================================== plumbing
    def _submit(
        self, run: _Run, leg: _LegCtx, detail: dict[str, Any], send: Callable[[str], dict[str, Any]]
    ) -> _Sent:
        """Persist `submitting` + request id, then send exactly once per attempt. Only a definite
        429 earns another attempt (new request id), after the limiter pauses for Retry-After."""
        attempt = 0
        while True:
            request_id = leg_request_id(run.decision_id, leg.seq, attempt)
            now = self.clock()
            self._leg(run, leg, "submitting", request_id=request_id, attempt=attempt, submitted_at=now, detail=detail)
            self.limiter.acquire()
            run.writes += 1
            try:
                payload = send(request_id)
            except DefiniteRejection as exc:
                self._event(run, leg, "write_rejected", request_id, exc.status, {"body": exc.body})
                if exc.status == 429 and attempt + 1 < MAX_WRITE_ATTEMPTS:
                    self.limiter.pause(exc.retry_after if exc.retry_after is not None else RETRY_AFTER_DEFAULT_S)
                    attempt += 1
                    continue
                return _Sent("rejected", request_id, now, error=f"HTTP {exc.status}")
            except AmbiguousWriteError as exc:
                self._event(run, leg, "write_ambiguous", request_id, exc.status, {"error": str(exc)})
                return _Sent("ambiguous", request_id, now, error=str(exc))
            except ValueError as exc:  # the client refused to build the request: nothing was sent
                self._event(run, leg, "write_invalid", request_id, None, {"error": str(exc)})
                return _Sent("invalid", request_id, now, error=f"invalid request: {exc}")
            self._event(run, leg, "write_accepted", request_id, None, payload)
            return _Sent("accepted", request_id, now, payload=payload)

    def _poll(self, check: Callable[[], T | None], window: float, *, immediate: bool) -> T | None:
        if immediate:
            out = check()
            if out is not None:
                return out
        for delay in self._schedule(window):
            self.sleep(delay)
            out = check()
            if out is not None:
                return out
        return None

    def _schedule(self, window: float) -> list[float]:
        out: list[float] = []
        total = 0.0
        for delay in self.poll_schedule:
            if total + delay > window + 1e-9:
                break
            out.append(delay)
            total += delay
        return out

    def _window(self) -> float:
        return sum(self.poll_schedule)

    @staticmethod
    def _rederive_units(approved: float, scale: float, *, whole: bool = False) -> float:
        """min(approved × equity_now / NAV_at_approval, approved × 1.02), floored (to whole
        units when the instrument requires them, else to 6 decimals)."""
        units = min(approved * scale, approved * UNITS_REFRESH_CAP)
        if whole:
            return float(math.floor(units + 1e-9))
        return math.floor(units * 1_000_000 + 1e-6) / 1_000_000

    def _leg(self, run: _Run, leg: _LegCtx, state: str, **fields: Any) -> None:
        self.ledger.update_leg(run.decision_id, leg.seq, state=state, now=self.clock(), **fields)

    def _resolve(self, run: _Run, leg: _LegCtx, state: str, **fields: Any) -> None:
        if fields.get("error") is None:
            fields.pop("error", None)
        self._leg(run, leg, state, resolved_at=self.clock(), **fields)

    def _skip(self, run: _Run, leg: _LegCtx, reason: str) -> None:
        self._resolve(run, leg, "skipped", error=reason)
        run.partial = True
        run.reason(f"{leg.symbol}: {reason}")

    def _event(self, run: _Run, leg: _LegCtx, kind: str, request_id: str, status: int | None, payload: Any) -> None:
        self.ledger.record_broker_event(
            kind=kind, decision_id=run.decision_id, seq=leg.seq, request_id=request_id,
            http_status=status, payload=payload, now=self.clock(),
        )

    def _line(self, symbol: str) -> str:
        return self._v2l.get(symbol, symbol)

    def _load_symbols(self, legs: Sequence[_LegCtx]) -> None:
        source = self._symbol_source
        if source is None:
            try:
                base: dict[int, str] = InstrumentMap.load().symbols_by_id()
            except (OSError, ValueError):
                base = {}
            self._symbol_fn = None
        elif isinstance(source, Mapping):
            base, self._symbol_fn = dict(source), None
        else:
            base, self._symbol_fn = {}, source
        for leg in legs:
            if leg.instrument_id is not None:
                base.setdefault(leg.instrument_id, leg.symbol)
        self._symbols = base

    def _symbol(self, instrument_id: int) -> str | None:
        found = self._symbols.get(instrument_id)
        if found is None and self._symbol_fn is not None:
            found = self._symbol_fn(instrument_id)
        return found

    def _portfolio(self) -> PortfolioRead:
        return parse_pnl(self.read.pnl(), self._symbol)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        path = self.lock_path or state_dir() / LOCK_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ExecutionLocked("another executor holds the execution lock") from exc
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
