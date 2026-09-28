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
- Order: closes (plan order) → stop-loss / take-profit modifications of existing positions
  (`modify_sl`, `set_tp`) → refresh equity → core opens → swing opens → the swing opens'
  `modify_tp` legs (swing-book §4.4, §4.6).
  Open units are re-derived from fresh equity (approved units × equity_now / NAV at approval) but
  never above approved units × 1.02. An open whose dependencies did not fill is skipped.
- After every open fill, exposure (filled units × fill price, and the broker-reported exposure)
  must be within risk.approval.post_fill_exposure_tolerance of the intended exposure (units sent
  × planned price); otherwise the decision is BLOCKED and nothing else is sent.
- The first rejected open, or any rejected close, stops the remaining opens → completed_partial.
  Scoped by sleeve (swing-book §4.6): a rejected (or held) SWING open stops only the later swing
  opens and skips its own `modify_tp`; a rejected core open or close stops every later open.
- Swing take-profit (swing-book §4.4, SW-5): an entry with `tp_mode="body"` sends takeProfitRate in
  the open body and needs no PATCH. A `modify_tp` / `set_tp` leg is ledgered `submitting` with its
  request id before its PATCH like every leg; the PATCH always resends the position's CURRENT
  stop-loss with the take-profit (the stop is never re-anchored to the fill), and both rates are
  re-read. A modify_sl PATCH resends the position's current take-profit. Every filled swing entry
  creates or advances its swing trade (`ledger.record_swing_fill`) at once and again at the finish;
  at the finish a filled entry whose target should be at the broker but whose positions do not carry
  it (a rejected or unconfirmed PATCH, a crash between the fill and the PATCH, a body TP the broker
  dropped) moves its trade to `open_tp_missing` (a swing-scoped blocker; the next swing slot plans a
  `set_tp`); a filled `set_tp` moves it back. A confirmed take-profit is checked again by reconcile.
  An execution_unknown or waiting decision whose every unresolved leg is a swing leg gets the
  `swing` blocker scope (new swing entries halt, the core does not). The post-execution reconcile
  maps live swing positions to their swing lines (`swing.book.swing_vehicle_map`).
- Market hours: legs the approval dropped (`dropped`: seq → reason) are marked skipped before
  anything is sent. Every other leg's session is re-checked just before its write: closed →
  skipped `market_closed` (a close skipped this way also stops every open, the drop rule).
- Broker status 11 (WaitingForMarket) is not "in flight". While the leg's session is open by our
  calendar (a trading halt, say) polling continues through 11 for the normal window; once the
  session is closed, or the window ends with the order still held, the order is held for its
  market. If the write client offers `cancel_order` AND declares `CANCEL_ROUTE_VERIFIED` (both
  added only once the M5 route check verifies the cancel route) the order is cancelled and
  re-looked-up: a confirmed cancel → rejected `cancelled_market_closed`. `resume` never cancels
  (lookups only). Otherwise the leg becomes `waiting_for_market`, the remaining opens stop, and the
  decision ends `waiting_for_market` (a blocker scoped to the satellite sleeve when every waiting
  leg is a stock order, else to the whole book) that the watch resolves read-only. A close order
  that reports status 11 is handled the same way (no cancel).
- Final state: unknown leg → execution_unknown; blocked → blocked; unreadable portfolio, missing
  stop-loss, unknown position or a broken fill → blocked; a waiting leg → waiting_for_market;
  any rejected/skipped/partial leg → completed_partial; drift ≤ risk.reconcile.drift_max →
  completed; otherwise blocked.
- Corporate actions at reconcile (design §3.6, `stocks.corporate.reconcile_corporate`): a position no
  line owns that no open leg of ours created (spin-off shares before `council stocks adopt`) is not
  an unknown position and its missing stop does not block: it is a pending corporate action, which
  holds only the stock sleeve (the next cycle's start raises `satellite:corporate_action_pending`).
  A credited line's position without a stop is the warning `credited_no_sl:<line>`. Both are
  recorded as reasons; every other unknown position or missing stop still blocks.
- `resume` does lookups and reconcile ONLY and never sends an order: for its duration the write
  client is a `NoWriteClient` whatever the executor was built with, and `_submit` refuses before any
  leg state changes (`WriteRefused`). A leg that provably never reached the broker (clean
  not-found) is marked skipped: it needs a fresh proposal and approval. `resume` accepts an
  executing, execution_unknown or blocked decision; a blocked one only gets its lookups and
  reconcile noted (blocked is cleared only by an operator review, `council ops review`).
- One executor at a time: an exclusive lock file in the private state dir.
- Constructing an executor WITH a write client runs `operator.guards.assert_operator_context` on
  the real process context (env, TTYs, ancestor processes) and raises GuardError outside a human
  operator's terminal. Only tests may pass `_skip_guard_for_tests=True`. Without a write client
  (resume) there is no guard and no way to send an order.
- A leg's line is `Leg.line` (else the vehicle map); open units are floored to whole units when
  `Leg.whole_units` says so. Decision events are logged with actor "executor".
"""

from __future__ import annotations

import fcntl
import math
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, NoReturn, Protocol, TypeVar

from pydantic import Field

from council import clock as market_clock
from council.broker.http import (
    RETRY_AFTER_DEFAULT_S,
    AmbiguousWriteError,
    BrokerError,
    DefiniteRejection,
)
from council.broker.instruments import InstrumentMap
from council.broker.parsing import (
    STATUS_CANCELED,
    STATUS_FAILED,
    STATUS_FAILED_AFTER_PARTIAL,
    STATUS_FILLED,
    STATUS_IN_FLIGHT,
    STATUS_PARTIALLY_FILLED,
    STATUS_WAITING_FOR_MARKET,
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
from council.ledger.db import Ledger, LedgerError, LegRow
from council.ledger.states import LEG_ACTIVE_STATES, WAITING_STATE, can_transition
from council.models.common import Direction, Strict
from council.models.cycle import DecisionState
from council.models.plan import Leg, Plan
from council.operator import guards
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
ACTOR = "executor"
CANCEL_ATTEMPT = 90          # request-id attempt number reserved for a leg's cancel request
WAITING_GRACE_H = 1.0        # a held order unresolved 1 h after the next full session closes → blocked
MARKET_CLOSED = "market_closed"
CANCELLED_MARKET_CLOSED = "cancelled_market_closed"
RESUMABLE_STATES = frozenset({"executing", "execution_unknown", "blocked"})

T = TypeVar("T")


class _NoPortfolio(Exception):  # noqa: N818 - internal flow marker
    """The post-execution portfolio read failed (already recorded as a reason)."""


def _row_is_swing(row: LegRow) -> bool:
    return row.detail.get("sleeve") == "swing" or bool(row.detail.get("swing_trade_id"))


def leg_request_id(decision_id: str, seq: int, attempt: int) -> str:
    """Deterministic x-request-id / idempotency key for one attempt of one leg."""
    return str(uuid.uuid5(NAMESPACE, f"{decision_id}:{seq}:{attempt}"))


class ExecutionError(RuntimeError):
    pass


class ExecutionLocked(ExecutionError):  # noqa: N818 - reads as a state
    pass


class WriteRefused(ExecutionError):  # noqa: N818 - reads as a refusal
    """A write was attempted while the executor runs lookups only (resume)."""


class NoWriteClient:
    """The write client `resume` runs with: every write raises `WriteRefused`, so a recovery path
    that tried to send, modify or cancel an order fails closed instead. It never declares the
    cancel route verified."""

    CANCEL_ROUTE_VERIFIED = False

    def open_order(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        raise WriteRefused("resume is lookups only: open_order refused, nothing was sent")

    def close_position(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        raise WriteRefused("resume is lookups only: close_position refused, nothing was sent")

    def patch_stop_loss(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        raise WriteRefused("resume is lookups only: patch_stop_loss refused, nothing was sent")

    def cancel_order(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        raise WriteRefused("resume is lookups only: cancel_order refused, nothing was sent")


class WriteClient(Protocol):
    def open_order(
        self, *, request_id: str, instrument_id: int, transaction: Any, settlement: str,
        leverage: int, units: float, stop_loss_rate: float | None, stop_loss_type: str = "fixed",
        take_profit_rate: float | None = None,
    ) -> dict[str, Any]: ...

    def close_position(
        self, *, request_id: str, position_id: int, instrument_id: int,
        units_to_deduct: float | None = None,
    ) -> dict[str, Any]: ...

    def patch_stop_loss(
        self, *, request_id: str, position_id: int, stop_loss_rate: float,
        take_profit_rate: float | None = None,
    ) -> dict[str, Any]: ...

    # Optional, and absent from EtoroWriteClient until the M5 route check verifies the cancel route;
    # used only when the client also declares CANCEL_ROUTE_VERIFIED = True:
    # def cancel_order(self, *, request_id: str, order_id: int) -> dict[str, Any]: ...


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
    whole_units: bool = False
    session: str | None = None
    sleeve: str | None = None
    swing_trade_id: str | None = None
    tp_rate: float | None = None
    tp_mode: str | None = None

    @property
    def is_swing(self) -> bool:
        return self.sleeve == "swing" or bool(self.swing_trade_id)

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
            whole_units=leg.whole_units, session=leg.session,
            sleeve="swing" if leg.is_swing else None, swing_trade_id=leg.swing_trade_id,
            tp_rate=leg.tp_rate, tp_mode=leg.tp_mode,
        )

    @classmethod
    def from_row(cls, row: LegRow) -> _LegCtx:
        return cls(
            seq=row.seq, kind=row.kind, symbol=row.vehicle_symbol, line=row.line,
            instrument_id=row.instrument_id, direction=row.direction,  # type: ignore[arg-type]
            settlement=row.settlement, leverage=row.leverage, units=row.units,
            amount_usd=row.amount_usd, sl_rate=row.sl_rate, position_id=row.position_id,
            depends_on=tuple(row.depends_on), whole_units=bool(row.detail.get("whole_units", False)),
            session=row.detail.get("session"),
            sleeve="swing" if (row.detail.get("sleeve") == "swing" or row.detail.get("swing_trade_id")) else None,
            swing_trade_id=row.detail.get("swing_trade_id"), tp_rate=row.detail.get("tp_rate"),
            tp_mode=row.detail.get("tp_mode"),
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
    stop_swing: bool = False        # a swing open failed: later SWING opens stop, core ones do not
    waiting: bool = False
    writes: int = 0
    allow_cancel: bool = True       # False in resume: lookups only, never a write

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
        _skip_guard_for_tests: bool = False,
    ) -> None:
        if write is not None and not _skip_guard_for_tests:
            guards.assert_operator_context(
                env=os.environ,
                stdin_isatty=bool(sys.stdin is not None and sys.stdin.isatty()),
                stdout_isatty=bool(sys.stdout is not None and sys.stdout.isatty()),
                ancestors=guards.process_ancestors(),
            )
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
    def execute(
        self, decision_id: str, plan: Plan, *, nav_usd: float, dropped: Mapping[int, str] | None = None,
    ) -> ExecutionReport:
        """Run an approved plan. `nav_usd` is the equity the plan was sized with; `dropped` maps
        the seq of every leg the approval dropped (market hours) to its reason: those legs are
        marked skipped and never sent."""
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
        dropped = dict(dropped or {})
        unknown_seqs = sorted(set(dropped) - set(seqs))
        if unknown_seqs:
            raise ValueError(f"dropped legs not in the plan: {unknown_seqs}")
        legs = [_LegCtx.from_leg(leg, leg.line or self._line(leg.symbol)) for leg in ordered
                if leg.seq not in dropped]
        with self._lock():
            self._load_symbols(legs)
            before = self._portfolio()            # a failing read changes nothing
            run = _Run(decision_id, legs, before=before)
            now = self.clock()
            self.ledger.record_positions(now, before.positions, decision_id=decision_id, source="pre_execution")
            self.ledger.add_equity_mark(now, before.equity_usd, credit_usd=before.credit_usd, source="pre_execution")
            # legs first (idempotent when the cycle already wrote the same ones): a mismatch
            # raises here, with nothing sent and the decision still `approved`
            self.ledger.insert_legs(decision_id, ordered, line_of=self._line, now=now)
            self.ledger.transition(decision_id, "executing", "approved plan: execution started", actor=ACTOR, now=now)
            try:
                for seq, why in sorted(dropped.items()):
                    self.ledger.update_leg(decision_id, seq, state="skipped", resolved_at=now, now=now,
                                           error=f"dropped at approval: {why}")
                    run.partial = True
                self._run_closes(run)
                self._run_modifies(run)
                self._run_opens(run, nav_usd)
                self._run_tp_legs(run)
                return self._finish(run, skip_reason="not sent: execution stopped earlier")
            except Exception as exc:
                self._fail_closed(run, exc)
                raise

    def resume(self, decision_id: str) -> ExecutionReport:
        """Recover an interrupted or unknown execution with lookups and reconcile only."""
        decision = self.ledger.get_decision(decision_id)
        if decision.state not in RESUMABLE_STATES:
            raise ExecutionError(f"{decision_id} is {decision.state}; nothing to resume")
        rows = self.ledger.legs(decision_id)
        legs = [_LegCtx.from_row(r) for r in rows]
        writer, self.write = self.write, NoWriteClient()     # lookups only, whatever we were built with
        try:
            with self._lock():
                self._load_symbols(legs)
                run = _Run(decision_id, legs, before=self._portfolio(), allow_cancel=False)
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
        finally:
            self.write = writer

    # ================================================================== phases
    def _run_closes(self, run: _Run) -> None:
        for leg in run.legs:
            if leg.kind in ("close", "partial_close"):
                if run.unknown or run.blocked:
                    return
                self._close_leg(run, leg)

    def _run_modifies(self, run: _Run) -> None:
        for leg in run.legs:
            if leg.kind in ("modify_sl", "set_tp"):
                if run.unknown or run.blocked:
                    return
                if leg.kind == "set_tp":
                    self._tp_leg(run, leg)
                else:
                    self._modify_leg(run, leg)

    def _run_tp_legs(self, run: _Run) -> None:
        """The swing opens' take-profit PATCHes, after every open."""
        for leg in run.legs:
            if leg.kind == "modify_tp":
                if run.unknown or run.blocked:
                    return
                self._tp_leg(run, leg)

    @staticmethod
    def _stop_after(run: _Run, leg: _LegCtx) -> None:
        """A failed open stops the later opens of its sleeve: a swing open only the swing ones, a
        core open every one (core and swing)."""
        if leg.is_swing:
            run.stop_swing = True
        else:
            run.stop_opens = True

    def _run_opens(self, run: _Run, nav_usd: float) -> None:
        # core opens first, then swing opens (plan order inside each sleeve)
        opens = sorted((leg for leg in run.legs if leg.kind == "open"), key=lambda leg: leg.is_swing)
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
            if leg.is_swing and run.stop_swing:
                continue                     # left planned: skipped at the finish
            if any(self.ledger.get_leg(run.decision_id, dep).state != "filled" for dep in leg.depends_on):
                self._skip(run, leg, "dependency did not fill")
                continue
            if not (leg.sl_rate and leg.sl_rate > 0) or not leg.units or leg.instrument_id is None or not leg.settlement:
                self._skip(run, leg, "invalid open leg (stop-loss, units, instrument or settlement missing)")
                self._stop_after(run, leg)
                continue
            if self._market_closed(leg):
                self._skip(run, leg, MARKET_CLOSED)
                continue
            whole = leg.whole_units or self._whole_units.get(leg.symbol, False)
            units = self._rederive_units(leg.units, scale, whole=whole)
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
        if self._market_closed(leg):
            self._skip(run, leg, MARKET_CLOSED)
            run.stop_opens = True            # the drop rule: a risk-reducing leg did not go
            run.reason(f"{leg.symbol}: market closed; opens stopped")
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
            verdict = self._await_close(position_id, units_before, deduct, order_id, self._window(),
                                        immediate=False, leg=leg)
        else:
            verdict = self._await_close(position_id, units_before, deduct, None, AMBIGUITY_WINDOW_S,
                                        immediate=True, leg=leg)
        self._settle_close(run, leg, verdict, order_id)

    def _settle_close(self, run: _Run, leg: _LegCtx, verdict: str, order_id: int | None) -> None:
        if verdict == "waiting":
            self._wait(run, leg, order_id=order_id, broker_status=f"{STATUS_WAITING_FOR_MARKET}:WaitingForMarket")
            return
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
        order_id: int | None, window: float, *, immediate: bool, leg: _LegCtx | None = None,
    ) -> str:
        """'filled' once the portfolio shows the position gone (or reduced by the deducted
        units); 'rejected' if the close order reports an error; 'waiting' when the broker holds
        it for a closed market (at once when the leg's session is closed, else only if it is still
        held at the end of the window); else 'unconfirmed'."""
        held = False

        def check() -> str | None:
            nonlocal held
            if order_id is not None:
                try:
                    info = self.read.close_order_info(order_id)
                except BrokerError:
                    info = None
                if info is not None and parse_close_order(info).failed:
                    return "rejected"
                if info is not None:
                    held = parse_close_order(info).waiting_for_market
                    if held and (leg is None or self._market_closed(leg)):
                        return "waiting"
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

        return self._poll(check, window, immediate=immediate) or ("waiting" if held else "unconfirmed")

    # ================================================================== stop-loss PATCH
    def _modify_leg(self, run: _Run, leg: _LegCtx) -> None:
        pos = run.before.position(leg.position_id) if (run.before and leg.position_id) else None
        if pos is None or not leg.sl_rate:
            self._skip(run, leg, "stop-loss leg without an open position or a rate")
            return
        if self._market_closed(leg):
            self._skip(run, leg, MARKET_CLOSED)
            return
        position_id, rate = pos.position_id, float(leg.sl_rate)
        keep_tp = {"take_profit_rate": float(pos.tp_rate)} if pos.tp_rate else {}   # resend the current TP
        sent = self._submit(
            run, leg, {},
            lambda rid: self.write.patch_stop_loss(  # type: ignore[union-attr]
                request_id=rid, position_id=position_id, stop_loss_rate=rate, **keep_tp
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

    # ================================================================== take-profit PATCH (swing)
    def _tp_leg(self, run: _Run, leg: _LegCtx) -> None:
        """`modify_tp` (after its swing open) or `set_tp` (an open trade): PATCH the approved
        take-profit together with the position's CURRENT stop-loss, then re-read both rates."""
        if leg.kind == "modify_tp":
            deps = [self.ledger.get_leg(run.decision_id, dep) for dep in leg.depends_on]
            if not deps or any(d.state not in ("filled", "partially_filled", "rejected_partial") for d in deps):
                self._skip(run, leg, "entry did not fill; no take-profit to set")
                return
            pids = sorted({pid for d in deps for pid in d.position_ids})
        else:
            pids = [leg.position_id] if leg.position_id else []
        if len(pids) != 1 or not leg.tp_rate:
            self._skip(run, leg, "take-profit not sent: needs exactly one position and a rate")
            return
        if self._market_closed(leg):
            self._skip(run, leg, MARKET_CLOSED)
            return
        position_id, tp = pids[0], float(leg.tp_rate)
        try:
            pos = self._portfolio().position(position_id)
        except BrokerError:
            self._skip(run, leg, "portfolio unreadable; take-profit not sent")
            return
        if pos is None:
            self._skip(run, leg, "position no longer open")
            return
        if pos.tp_rate and abs(pos.tp_rate / tp - 1) <= self._sl_tol:
            self._resolve(run, leg, "filled", position_ids=[position_id], detail={"tp_already_set": True})
            return
        sl = float(pos.sl_rate) if pos.sl_rate and pos.sl_rate > 0 else float(leg.sl_rate or 0.0)
        if sl <= 0:
            self._skip(run, leg, "take-profit not sent: no stop-loss to resend with it")
            return
        sent = self._submit(
            run, leg, {"position_id": position_id, "sl_rate_sent": sl, "tp_rate_sent": tp},
            lambda rid: self.write.patch_stop_loss(  # type: ignore[union-attr]
                request_id=rid, position_id=position_id, stop_loss_rate=sl, take_profit_rate=tp,
            ),
        )
        if sent.outcome in ("rejected", "invalid"):
            self._resolve(run, leg, "rejected", error=sent.error)
            run.partial = True
            run.reason(f"{leg.symbol}: take-profit change rejected (open_tp_missing)")
            return
        if sent.outcome == "accepted":
            self._leg(run, leg, "submitted")
        window = self._window() if sent.outcome == "accepted" else AMBIGUITY_WINDOW_S
        if self._await_sl_tp(position_id, sl, tp, window, immediate=sent.outcome == "ambiguous"):
            self._resolve(run, leg, "filled", position_ids=[position_id])
        else:
            self._resolve(run, leg, "unknown", error="take-profit change not confirmed")
            run.unknown = True
            run.reason(f"{leg.symbol}: take-profit change unknown (open_tp_missing)")

    def _await_sl_tp(self, position_id: int, sl: float | None, tp: float, window: float, *,
                     immediate: bool) -> bool:
        def check() -> bool | None:
            try:
                pos = self._portfolio().position(position_id)
            except BrokerError:
                return None
            if pos is None or not pos.tp_rate or abs(pos.tp_rate / tp - 1) > self._sl_tol:
                return None
            if sl and not (pos.sl_rate and abs(pos.sl_rate / sl - 1) <= self._sl_tol):
                return None
            return True

        return bool(self._poll(check, window, immediate=immediate))

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
        # the take-profit rides in the open body only behind the tp_on_open gate (the planner's mode)
        tp = {"take_profit_rate": float(leg.tp_rate)} if leg.tp_mode == "body" and leg.tp_rate else {}
        sent = self._submit(
            run, leg, {"units_sent": units, "planned_price": planned_price},
            lambda rid: self.write.open_order(  # type: ignore[union-attr]
                request_id=rid, instrument_id=instrument_id, transaction=transaction,
                settlement=settlement, leverage=leg.leverage, units=units, stop_loss_rate=sl_rate, **tp,
            ),
        )
        if sent.outcome in ("rejected", "invalid"):
            self._resolve(run, leg, "rejected", error=sent.error)
            run.partial = True
            self._stop_after(run, leg)
            run.reason(f"{leg.symbol}: open rejected; remaining {self._scope_word(leg)}opens stopped")
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
            if sid == STATUS_WAITING_FOR_MARKET:
                # held until the market opens: stop polling once the session is closed by our
                # calendar; while it is open (a halt) keep polling through the normal window
                return self._market_closed(leg)
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
            self._record_swing_fill(run, leg)
            if not full:
                run.partial = True
                run.reason(f"{leg.symbol}: open partially filled")
            if mismatch:
                run.blocked = True
                run.reason(f"{leg.symbol}: post-fill exposure check failed ({mismatch})")
        elif sid in STATUS_FAILED:
            self._resolve(run, leg, "rejected", error=status.error_message or "rejected", **common)
            run.partial = True
            self._stop_after(run, leg)
            run.reason(f"{leg.symbol}: open rejected; remaining {self._scope_word(leg)}opens stopped")
        elif sid in STATUS_FAILED_AFTER_PARTIAL:
            self._expect(run, leg, status)
            self._resolve(run, leg, "rejected_partial", error=status.error_message or "rejected after a partial fill", **common)
            self._record_swing_fill(run, leg)
            run.partial = True
            self._stop_after(run, leg)
            run.reason(f"{leg.symbol}: open rejected after a partial fill; remaining {self._scope_word(leg)}opens stopped")
        elif sid == STATUS_WAITING_FOR_MARKET:
            after = self._cancel_waiting(run, leg, status)
            if after is not None and after.status_id != STATUS_WAITING_FOR_MARKET:
                if after.status_id == STATUS_CANCELED:
                    self._resolve(run, leg, "rejected", error=CANCELLED_MARKET_CLOSED,
                                  order_id=after.order_id, broker_status=f"{after.status_id}:{after.status_name}")
                    run.partial = True
                    self._stop_after(run, leg)
                    run.reason(f"{leg.symbol}: market closed; order cancelled; remaining {self._scope_word(leg)}opens stopped")
                    return
                self._settle_open(run, leg, after, units, planned_price)
                return
            self._wait(run, leg, order_id=status.order_id, broker_status=f"{sid}:{status.status_name}")
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

    @staticmethod
    def _scope_word(leg: _LegCtx) -> str:
        return "swing " if leg.is_swing else ""

    def _record_swing_fill(self, run: _Run, leg: _LegCtx) -> None:
        """A filled swing entry creates or advances its trade at once (leg state decides, §4.5)."""
        if not leg.is_swing or leg.kind != "open":
            return
        from council.swing.models import IllegalTransition

        try:
            trade = self.ledger.record_swing_fill(run.decision_id, leg.seq, actor=ACTOR, now=self.clock())
            # the approved rates live on the trade (a later set_tp re-sends this target)
            missing = {k: v for k, v in (("sl_rate", leg.sl_rate), ("tp_rate", leg.tp_rate))
                       if v is not None and getattr(trade, k) is None}
            if missing:
                self.ledger.update_swing_trade(trade.trade_id, now=self.clock(), **missing)
        except (LedgerError, IllegalTransition) as exc:
            run.reason(f"{leg.symbol}: swing trade not recorded ({type(exc).__name__})")

    def _expect(self, run: _Run, leg: _LegCtx, status: OrderStatus) -> None:
        for pid in status.position_ids:
            run.expected.append(ExpectedPosition(
                position_id=pid, symbol=leg.symbol, direction=leg.direction,
                leverage=leg.leverage, sl_rate=leg.sl_rate,
            ))

    # ================================================================== market hours
    def _session(self, leg: _LegCtx) -> str | None:
        if leg.session:
            return leg.session
        spec = self.policy.universe.by_symbol().get(leg.line)
        return market_clock.vehicle_session(spec, leg.symbol) if spec is not None else None

    def _market_closed(self, leg: _LegCtx) -> bool:
        """Pre-send check: is the leg's session closed right now? A leg without a known session
        (an unmapped position) is sent: the broker decides, and status 11 is handled."""
        session = self._session(leg)
        if session is None:
            return False
        closing = leg.kind in ("close", "partial_close")
        return not market_clock.session_open(session, self.clock(), closing=closing)

    def _cancel_waiting(self, run: _Run, leg: _LegCtx, status: OrderStatus) -> OrderStatus | None:
        """Cancel an order held for a closed market, when the write client has a verified cancel
        route, and look it up again. None when no cancel was possible or nothing was learned."""
        if not run.allow_cancel or self.write is None or status.order_id is None:
            return None
        cancel = getattr(self.write, "cancel_order", None)
        if not callable(cancel) or getattr(self.write, "CANCEL_ROUTE_VERIFIED", False) is not True:
            return None
        request_id = leg_request_id(run.decision_id, leg.seq, CANCEL_ATTEMPT)
        self.limiter.acquire()
        run.writes += 1
        order_id = status.order_id
        try:
            payload = cancel(request_id=request_id, order_id=order_id)
            self._event(run, leg, "cancel_accepted", request_id, None, payload)
        except DefiniteRejection as exc:
            self._event(run, leg, "cancel_rejected", request_id, exc.status, {"body": exc.body})
        except AmbiguousWriteError as exc:
            self._event(run, leg, "cancel_ambiguous", request_id, exc.status, {"error": str(exc)})
        except ValueError as exc:
            self._event(run, leg, "cancel_invalid", request_id, None, {"error": str(exc)})
            return None
        found: OrderStatus | None = None

        def check() -> OrderStatus | None:
            nonlocal found
            try:
                payload = self.read.order_lookup(order_id=order_id)
            except BrokerError:
                return None
            if payload is None:
                return None
            found = parse_order_status(payload)
            settled = found.status_id not in STATUS_IN_FLIGHT and found.status_id != STATUS_WAITING_FOR_MARKET
            return found if settled else None

        self._poll(check, self._window(), immediate=True)
        return found

    def _wait(self, run: _Run, leg: _LegCtx, *, order_id: int | None, broker_status: str) -> None:
        """The broker holds the order until the market opens: waiting_for_market, not unknown."""
        now = self.clock()
        session = self._session(leg)
        close = market_clock.next_session_close(session, now) if session else None
        deadline = close + timedelta(hours=WAITING_GRACE_H) if close else now + timedelta(hours=24)
        self._leg(run, leg, WAITING_STATE, order_id=order_id, broker_status=broker_status,
                  detail={"waiting_since": now.isoformat(), "waiting_deadline": deadline.isoformat()})
        run.waiting = True
        if leg.kind == "open":
            self._stop_after(run, leg)
        else:
            run.stop_opens = True
        run.reason(f"{leg.symbol}: order held by the broker until its market opens; remaining "
                   f"{self._scope_word(leg) if leg.kind == 'open' else ''}opens stopped")

    def _blocker_scope(self, decision_id: str) -> str:
        """`swing` when every waiting leg is a swing leg, `satellite` when every waiting leg is a
        stock order on a satellite line, else `all`."""
        specs = self.policy.universe.by_symbol()
        waiting = [r for r in self.ledger.legs(decision_id) if r.state == WAITING_STATE]
        if waiting and all(_row_is_swing(r) for r in waiting):
            return "swing"
        stock = all(
            (spec := specs.get(r.line)) is not None and spec.asset_class == "stock" and spec.sleeve == "satellite"
            for r in waiting
        )
        return "satellite" if waiting and stock else "all"

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
                row.order_id, AMBIGUITY_WINDOW_S, immediate=True, leg=leg,
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
        elif leg.kind in ("modify_tp", "set_tp"):
            pid = row.detail.get("position_id") or leg.position_id
            tp = row.detail.get("tp_rate_sent") or leg.tp_rate
            sl = row.detail.get("sl_rate_sent") or leg.sl_rate
            ok = bool(pid and tp) and self._await_sl_tp(int(pid), sl, float(tp), AMBIGUITY_WINDOW_S,  # type: ignore[arg-type]
                                                         immediate=True)
            if ok:
                self._resolve(run, leg, "filled", position_ids=[int(pid)])  # type: ignore[arg-type]
            elif row.state in ("submitting", "unknown"):
                self._resolve(run, leg, "skipped", error="take-profit change not applied (open_tp_missing)")
                run.partial = True
            else:
                self._resolve(run, leg, "unknown", error="take-profit change not confirmed")
                run.unknown = True
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
        """Carry the outcome of legs that were already terminal (or waiting) into the resumed run."""
        if row.state == WAITING_STATE:
            run.waiting = run.stop_opens = True
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
        except BrokerError as exc:
            run.reason(f"post-execution reconcile unavailable ({type(exc).__name__})")
        self._settle_swing(run, after)
        try:
            if after is None:
                raise _NoPortfolio
            snapshot = snapshot_from_portfolio(after, now)
            rec = reconcile(snapshot, self._targets(run.decision_id), run.expected, self.policy,
                            swing_map=self._swing_map())
            rec = self._corporate_reconcile(run, rec, after.positions)
            self.ledger.record_positions(now, after.positions, decision_id=run.decision_id, source="post_execution")
            self.ledger.add_equity_mark(now, after.equity_usd, credit_usd=after.credit_usd, source="post_execution")
        except _NoPortfolio:
            pass
        except (BrokerError, ValueError) as exc:
            run.reason(f"post-execution reconcile unavailable ({type(exc).__name__})")
        final = self._final_state(run, rec)
        current = self.ledger.get_decision(run.decision_id).state
        reason = "; ".join(run.reasons) or final
        if current != final and can_transition(current, final):
            # the scope is set only with the move to waiting: a resumed BLOCKED decision that stays
            # blocked keeps its full blocker (a satellite scope would free the core cycle without
            # the operator review that alone clears blocked)
            if final == WAITING_STATE:
                self._scope(run.decision_id, self._blocker_scope(run.decision_id), now)
            elif final == "execution_unknown" and self._only_swing_unresolved(run.decision_id):
                self._scope(run.decision_id, "swing", now)
            self.ledger.transition(run.decision_id, final, reason, actor=ACTOR, now=now)
        else:
            self.ledger.note(run.decision_id, f"resume: still {current}: {reason}", actor=ACTOR, now=now)
            final = current  # type: ignore[assignment]
        return self._report(run, final, rec, after)  # type: ignore[arg-type]

    def _scope(self, decision_id: str, scope: str, now: datetime) -> None:
        """Set a blocker scope; a `swing` scope the ledger refuses (an unresolved core leg) falls
        back to the whole book."""
        try:
            self.ledger.set_blocker_scope(decision_id, scope, now=now)
        except LedgerError:
            self.ledger.set_blocker_scope(decision_id, "all", now=now)

    def _only_swing_unresolved(self, decision_id: str) -> bool:
        unresolved = [r for r in self.ledger.legs(decision_id)
                      if r.state in LEG_ACTIVE_STATES | {WAITING_STATE, "planned"}]
        return bool(unresolved) and all(_row_is_swing(r) for r in unresolved)

    def _swing_map(self) -> Any:
        from council.swing.book import swing_vehicle_map

        return swing_vehicle_map(self.ledger, self.policy)

    def _settle_swing(self, run: _Run, after: PortfolioRead | None) -> None:
        """Swing trades from leg state (§4.5) and the take-profit check (§4.4): every filled swing
        entry records its trade; one whose target should be at the broker but whose live positions
        do not carry it moves to `open_tp_missing`; a filled `set_tp` moves its trade back. A
        confirmed take-profit is added to the reconcile's expectations."""
        from council.swing.models import IllegalTransition

        rows = [r for r in self.ledger.legs(run.decision_id) if _row_is_swing(r)]
        for row in rows:
            leg = _LegCtx.from_row(row)
            try:
                if row.kind == "open" and row.state in ("filled", "partially_filled", "rejected_partial"):
                    trade = self.ledger.record_swing_fill(run.decision_id, row.seq, actor=ACTOR, now=self.clock())
                    if row.detail.get("tp_mode") not in ("body", "patch") or not leg.tp_rate:
                        continue
                    tp = float(leg.tp_rate)
                    live = [] if after is None else [p for pid in row.position_ids
                                                     if (p := after.position(pid)) is not None]
                    if after is not None and not live:
                        continue            # already closed at the broker: the watch classifies it
                    if live and all(p.tp_rate and abs(p.tp_rate / tp - 1) <= self._sl_tol for p in live):
                        confirmed = {p.position_id for p in live}
                        run.expected = [e.model_copy(update={"tp_rate": tp}) if e.position_id in confirmed else e
                                        for e in run.expected]
                    elif trade.state in ("open", "partial"):
                        self.ledger.transition_swing_trade(
                            trade.trade_id, "open_tp_missing", actor=ACTOR, now=self.clock(),
                            reason=f"take-profit not at the broker after {run.decision_id}:{row.seq}",
                            cycle_id=self.ledger.get_decision(run.decision_id).cycle_id)
                        run.reason(f"{row.vehicle_symbol}: take-profit missing at the broker (open_tp_missing)")
                elif row.kind == "set_tp" and row.state == "filled" and leg.swing_trade_id:
                    trade = self.ledger.swing_trade(leg.swing_trade_id)
                    if trade is not None and trade.state == "open_tp_missing":
                        self.ledger.transition_swing_trade(
                            trade.trade_id, self._resumed_state(trade), actor=ACTOR, now=self.clock(),
                            reason=f"take-profit set by {run.decision_id}:{row.seq}",
                            cycle_id=self.ledger.get_decision(run.decision_id).cycle_id)
            except (LedgerError, IllegalTransition) as exc:
                run.reason(f"{row.vehicle_symbol}: swing trade update failed ({type(exc).__name__})")

    def _resumed_state(self, trade: Any) -> str:
        """`partial` when the trade's entry leg filled only in part, else `open`."""
        try:
            if trade.decision_id and trade.entry_seq is not None:
                state = self.ledger.get_leg(trade.decision_id, int(trade.entry_seq)).state
                return "open" if state == "filled" else "partial"
        except LedgerError:
            pass
        return "open"

    def _corporate_reconcile(self, run: _Run, rec: ReconcileResult, positions: Sequence[Any]) -> ReconcileResult:
        """The reconcile `_final_state` judges, with corporate actions taken out
        (`stocks.corporate.reconcile_corporate`): a pending action (a position no line owns that no
        open leg of ours created) is not an unknown position and its missing stop does not block, and
        a credited line without a stop is a warning. Both become reasons; the pending action holds
        only the stock sleeve, at the next cycle's start. Every other problem stays. Without a stock
        sleeve nothing changes (no cycle check would hold anything, so an unknown position blocks).
        A failure of the corporate step keeps the plain reconcile (fail closed: an unknown position
        still blocks) and records why; it never breaks the post-trade finish."""
        from council.stocks import corporate

        if not corporate.sleeve_active(self.policy):
            return rec
        try:
            fixed, warnings, blockers = corporate.reconcile_corporate(
                rec, list(positions), self.policy, opened=corporate.opened_position_ids(self.ledger))
        except Exception as exc:  # noqa: BLE001 - after orders were sent, never crash the finish
            run.reason(f"corporate-action reconcile unavailable ({type(exc).__name__}); the plain "
                       "reconcile applies")
            return rec
        for warning in warnings:
            run.reason(f"warning {warning}: a credited position without a stop-loss (sold at the next US session)")
        if blockers:
            run.reason("corporate action pending: an unknown position no leg of ours opened; the stock "
                       "sleeve is held until `council stocks adopt`")
        return fixed

    def _final_state(self, run: _Run, rec: ReconcileResult | None) -> str:
        """The decision's final state from the run and the post-execution reconcile (after
        `_corporate_reconcile`: pending corporate actions and credited positions do not block)."""
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
        if run.waiting:
            return WAITING_STATE
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
                self.ledger.transition(run.decision_id, "execution_unknown", label, actor=ACTOR)
        except Exception:  # pragma: no cover - never mask the original failure
            pass

    # ================================================================== plumbing
    def _submit(
        self, run: _Run, leg: _LegCtx, detail: dict[str, Any], send: Callable[[str], dict[str, Any]]
    ) -> _Sent:
        """Persist `submitting` + request id, then send exactly once per attempt. Only a definite
        429 earns another attempt (new request id), after the limiter pauses for Retry-After."""
        if isinstance(self.write, NoWriteClient):
            raise WriteRefused(f"{leg.symbol}: resume is lookups only; nothing was sent")
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
