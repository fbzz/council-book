"""The 15-minute watch job. READ-ONLY: it never places an order and never builds an Executor.

  - expires stale proposals (atomic),
  - reveals sealed cycles once their decision is terminal (exact sealed bytes + salt),
  - publishes execution records written by the operator terminal (the operator never runs git),
  - kill switch: on a fresh HALT it creates a standing flatten PROPOSAL and sends an urgent alert,
  - checks that every open position carries a stop-loss and that the cycle heartbeat is fresh,
  - records a vanished position as a broker stop-loss hit (R4d cool-off). With a stock sleeve, a
    vanished STOCK position is classified first (`stocks.corporate.classify_vanished_positions`):
    a stop hit only when its last observed bid was at or below its stop, or above it by at most
    2 x the line's 4-hour sigma (stored by the last cycle), else `vanished_not_stop` (a cash
    takeover?): URGENT, no cool-off. Missing data counts as a stop hit,
  - resolves orders the broker holds until their market opens (waiting_for_market), read-only:
    filled → reconcile → completed / completed_partial; cancelled or rejected → skipped legs →
    completed_partial; a partial fill keeps waiting for its remainder until the leg's deadline,
    then settles as partially filled. A fill is checked against the units sent and the broker's
    exposure against units × the average fill price, never against the planned price (a held order
    fills at the next open, where a gap is normal). Only a unit or exposure mismatch, a missing
    stop-loss, an unknown position or a broken expected position blocks; drift from targets that
    may be hours old is recorded as a reason. A fill whose stop-loss sits at or through the fill
    price, or much closer than the class floor, raises an URGENT `sl_refit_needed:<line>` alert.
    With a stock sleeve, that reconcile takes pending corporate actions out
    (`stocks.corporate.reconcile_corporate`): a position no line owns that no leg of ours opened
    holds only the stock sleeve (at the next cycle's start), and a credited line without a stop is
    a warning; both are recorded as reasons.
    Still held one hour after the next full session closes → blocked with an URGENT alert (the
    operator checks the broker and runs `council ops resolve`). The timeout applies on every run,
    also when the broker read fails or no broker is connected. A timed-out hold keeps its blocker
    scope (a stock-only hold still holds only the satellite); a broken fill holds the whole book.
    The execution record of a waiting decision is published only once it is resolved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from council.runtime import CycleContext, LockBusy, instance_lock

TERMINAL = {"completed", "completed_partial", "rejected", "expired", "superseded",
            "reviewed_no_action", "blocked", "execution_unknown"}
HEARTBEAT_MAX = timedelta(hours=5)
WAITING_STATE = "waiting_for_market"
SL_REFIT_FLOOR_SHARE = 0.8     # refit when the stop is closer than 80% of the class floor


@dataclass
class WatchOutcome:
    status: str
    expired: list[str] = field(default_factory=list)
    revealed: list[str] = field(default_factory=list)
    executions_published: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)


def run_watch(ctx: CycleContext) -> WatchOutcome:
    try:
        with instance_lock(state_dir=ctx.state_dir):
            return _run(ctx)
    except LockBusy:
        return WatchOutcome(status="skipped_overlap")


def _run(ctx: CycleContext) -> WatchOutcome:
    ledger, now = ctx.ledger, ctx.clock()
    out = WatchOutcome(status="ok", expired=list(ledger.expire_stale(now)))
    out.alerts += _resolve_waiting(ctx, now)          # without a broker only the timeout applies
    files: dict[str, bytes] = {}
    out.revealed = _reveals(ctx, files)
    out.executions_published = _executions(ctx, files)
    if files and ctx.publisher is not None:
        try:
            ctx.publisher.publish(files, f"watch {now.strftime('%Y-%m-%dT%H%MZ')}: reveals/executions")
            ledger.set_runtime("revealed", sorted(set(ledger.get_runtime("revealed", [])) | set(out.revealed)))
            ledger.set_runtime("executions_published",
                               sorted(set(ledger.get_runtime("executions_published", []))
                                      | set(out.executions_published)))
        except Exception as exc:
            out.alerts.append(f"publish_error:{type(exc).__name__}")
    if ctx.sources.broker is not None:
        out.alerts += _broker_checks(ctx, now)
    out.alerts += _heartbeat(ctx, now)
    for alert in out.alerts:
        _alert(ctx, alert)
    return out


# ------------------------------------------------------------------------------- reveals
def _reveals(ctx: CycleContext, files: dict[str, bytes]) -> list[str]:
    from council.publish import journal
    from council.publish.public_models import PublicCommitment

    ledger = ctx.ledger
    done = set(ledger.get_runtime("revealed", []))
    salts = ctx.state_dir / "salts"
    revealed: list[str] = []
    if not salts.exists():
        return revealed
    for f in sorted(salts.glob("*.json")):
        cycle_id = f.stem
        if cycle_id in done:
            continue
        state = _cycle_decision_state(ledger, cycle_id)
        if state is not None and state not in TERMINAL:
            continue
        data = json.loads(f.read_text())
        sealed = bytes.fromhex(data["sealed_hex"])
        commitment_path = ctx.publisher and _read_existing(ctx, journal.commitment_path(cycle_id))
        if not commitment_path:
            continue
        commitment = PublicCommitment.model_validate_json(commitment_path)
        files.update(journal.reveal_files(sealed, data["salt"], commitment))
        revealed.append(cycle_id)
    return revealed


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _cycle_decision_state(ledger: Any, cycle_id: str) -> str | None:
    rec = ledger.get_cycle(cycle_id)
    if not rec:
        return None
    decision_id = rec.get("decision_id")
    if not decision_id:
        return rec.get("decision_state") or "reviewed_no_action"
    try:
        return ledger.get_decision(decision_id).state
    except Exception:
        return None


def _read_existing(ctx: CycleContext, rel: str) -> bytes | None:
    for root in (getattr(ctx.publisher, "dry_run_dir", None), getattr(ctx.publisher, "clone_dir", None)):
        if root is not None and (root / rel).exists():
            return (root / rel).read_bytes()
    return None


# ---------------------------------------------------------------------------- executions
def _executions(ctx: CycleContext, files: dict[str, bytes]) -> list[str]:
    from council.publish import journal, redact

    ledger = ctx.ledger
    done = set(ledger.get_runtime("executions_published", []))
    published: list[str] = []
    for key in ledger.get_runtime("execution_reports", []):
        if key in done:
            continue
        payload = ledger.get_runtime(f"exec_report:{key}")
        if not payload or not hasattr(redact, "public_execution"):
            continue
        if _decision_state(ledger, key) == WAITING_STATE:     # published once resolved
            continue
        from council.execution.executor import ExecutionReport
        from council.models.plan import Plan

        report = ExecutionReport.model_validate(payload["report"])
        plan = Plan.model_validate(payload["plan"]) if payload.get("plan") else None
        public = redact.public_execution(
            report, cycle_id=payload["cycle_id"], lines=ctx.policy.universe, nav_usd=payload["nav_usd"],
            plan=plan, approved_at=_dt(payload.get("approved_at")), completed_at=_dt(payload.get("completed_at")))
        files.update(journal.execution_files(public))
        published.append(key)
    return published


# ------------------------------------------------------------------------ broker checks
def _broker_checks(ctx: CycleContext, now: datetime) -> list[str]:
    from council.cycle import _snapshot_and_kill

    alerts: list[str] = []
    ledger = ctx.ledger
    prev = ledger.get_runtime("kill_state", "NORMAL")
    snapshot, kill_state, _ = _snapshot_and_kill(ctx, now)
    alerts += _stop_hits(ctx, snapshot, now)
    missing = [p.symbol for p in snapshot.positions if p.sl_rate in (None, 0)]
    if missing:
        alerts.append(f"URGENT positions without a stop-loss: {', '.join(sorted(set(missing)))}")
    if snapshot.gross > float(ctx.policy.risk["gross"]["hard_max"]) + 1e-9:
        alerts.append(f"URGENT gross {snapshot.gross:.2f}x above the hard limit")
    if kill_state in ("HALTED", "FLAT") and prev not in ("HALTED", "FLAT"):
        decision_id = _flatten_proposal(ctx, snapshot, now)
        alerts.append("URGENT kill switch HALTED: flatten proposal "
                      f"{decision_id or '(no open positions)'} awaits your approval")
    elif kill_state == "WARN" and prev == "NORMAL":
        alerts.append("WARN drawdown below 80% of the lifetime peak: no new risk")
    return alerts


def _heartbeat(ctx: CycleContext, now: datetime) -> list[str]:
    last = ctx.ledger.get_runtime("last_cycle")
    if not last:
        return []
    at = datetime.fromisoformat(last["at"])
    if now - at > HEARTBEAT_MAX:
        return [f"URGENT no completed cycle since {last['cycle_id']}"]
    return []


ALERT_WITHHELD = "a watch alert was withheld by the notification leak scan: run `council status`"


def _alert_texts(body: str) -> list[str]:
    """The alert body, then the same body with instrument keys and long ids scrubbed (the notifier
    refuses `UNMAPPED_<id>` and 7+ digit runs), then a fixed text: an URGENT alert must arrive even
    when its details cannot."""
    import re

    scrubbed = re.sub(r"(?i)UNMAPPED_[0-9A-Za-z]+", "an unmapped instrument", body)
    scrubbed = re.sub(r"(?<![\w.])\d{7,}(?!\w)", "[id]", scrubbed)
    return list(dict.fromkeys([body, scrubbed, ALERT_WITHHELD]))


def _alert(ctx: CycleContext, message: str) -> None:
    """Send one watch alert; a text the notifier refuses is retried scrubbed, then as a fixed text
    (never raises: an alert failure never stops the watch)."""
    if ctx.notifier is None:
        return
    urgent = message.startswith("URGENT")
    for text in _alert_texts(message.removeprefix("URGENT ").strip()):
        try:
            ctx.notifier.send("council watch", text, priority="urgent" if urgent else "default")
            return
        except Exception:  # noqa: BLE001 - try the next, safer text
            continue


# ---------------------------------------------------------------------- stops and flatten
def _stop_hits(ctx: CycleContext, snapshot: Any, now: datetime) -> list[str]:
    """Positions we expected (last observation) that vanished without one of our closes
    (`stocks.corporate.classify_vanished_positions`). A core line's is a broker stop-loss hit:
    recorded (R4d re-entry cool-off) with an URGENT alert. A stock line's is a stop hit only when
    its last observed bid was at or below its stop rate, or above it by at most 2 x the line's
    4-hour sigma (`cycle.STOCK_SIGMA_4H_KEY`, stored by the last cycle); otherwise it is
    `vanished_not_stop` (a cash takeover?): an URGENT alert, no cool-off. Missing data counts as a
    stop hit. The observations (symbol, stop rate, last bid) are PRIVATE ledger state."""
    from council.cycle import STOCK_SIGMA_4H_KEY
    from council.stocks import corporate

    ledger = ctx.ledger
    live = {p.position_id: p for p in snapshot.positions}
    seen = _observations(ledger.get_runtime("watch_positions", {}) or {})
    ours = {row.position_id for row in ledger.legs_in_states(
                ["filled", "partially_filled", "submitted", "in_flight", WAITING_STATE])
            if row.kind in ("close", "partial_close") and row.position_id is not None}
    raw_sigma = ledger.get_runtime(STOCK_SIGMA_4H_KEY, {})
    sigma = {str(k): _positive(v) for k, v in raw_sigma.items()} if isinstance(raw_sigma, dict) else {}
    vanished = corporate.classify_vanished_positions(seen, live, ours, ctx.policy, sigma_4h=sigma)
    alerts = []
    for v in vanished:
        if v.outcome == corporate.STOP_HIT:
            ledger.record_stop_hit(line=v.line, symbol=v.symbol, position_id=v.position_id, at=now)
            alerts.append(f"URGENT stop-loss hit on {v.line}")
    alerts += corporate.vanished_alerts(vanished)
    ledger.set_runtime("watch_positions", {
        str(pid): {"symbol": p.symbol, "sl_rate": p.sl_rate, "bid": p.close_rate if p.is_buy else None}
        for pid, p in live.items()})
    return alerts


def _observations(raw: Any) -> dict[int, Any]:
    """{position id: corporate.Observation} from the stored `watch_positions` (a bare symbol, as
    stored before the stock sleeve, reads as an observation without a stop rate or bid)."""
    from council.stocks.corporate import Observation

    out: dict[int, Any] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        try:
            pid = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, str):
            out[pid] = Observation(symbol=value)
        elif isinstance(value, dict) and isinstance(value.get("symbol"), str):
            out[pid] = Observation(symbol=value["symbol"], sl_rate=_positive(value.get("sl_rate")),
                                   bid=_positive(value.get("bid")))
    return out


def _positive(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) and value > 0 else None


def _decision_state(ledger: Any, decision_id: str) -> str | None:
    try:
        return ledger.get_decision(decision_id).state
    except Exception:
        return None


# ------------------------------------------------------------------ waiting_for_market
def _resolve_waiting(ctx: CycleContext, now: datetime) -> list[str]:
    """Look up every order the broker holds for a closed market (read-only) and settle it. Without
    a broker, or when a lookup fails, the decision stays waiting but its timeout still applies."""
    alerts: list[str] = []
    read = ctx.sources.broker
    for d in ctx.ledger.decisions(states=[WAITING_STATE], limit=50):
        if read is None:
            alerts += _timeout_if_due(ctx.ledger, d.decision_id, now)
            continue
        try:
            alerts += _resolve_one(ctx, d.decision_id, now)
        except Exception as exc:  # a failed read leaves the decision waiting; the timeout still applies
            alerts.append(f"waiting_check_error:{d.decision_id}:{type(exc).__name__}")
            try:
                alerts += _timeout_if_due(ctx.ledger, d.decision_id, now)
            except Exception as err:
                alerts.append(f"URGENT waiting_timeout_error:{d.decision_id}:{type(err).__name__}")
    return alerts


def _leg_deadline(row: Any) -> datetime | None:
    raw = row.detail.get("waiting_deadline")
    return datetime.fromisoformat(raw) if raw else None


def _timeout_if_due(ledger: Any, decision_id: str, now: datetime) -> list[str]:
    """A decision whose waiting legs are past their deadline (one hour after the next full session
    closed; a waiting leg without a deadline fails closed at once) → blocked, the stored execution
    record refreshed, and an URGENT alert asking for `council ops resolve`. The blocker scope is
    kept: a timed-out stock-only hold still holds only the satellite. Returns the alerts."""
    if _decision_state(ledger, decision_id) != WAITING_STATE:
        return []
    still = [r for r in ledger.legs(decision_id) if r.state == WAITING_STATE]
    if not still:
        return []
    deadlines = [d for d in (_leg_deadline(r) for r in still) if d is not None]
    if len(deadlines) == len(still) and now <= min(deadlines):
        return []
    ledger.transition(decision_id, "blocked", "waiting_for_market unresolved one hour after "
                      "the next full session closed", actor="watch", now=now)
    _refresh_report(ledger, decision_id, "blocked", ["order still held by the broker"])
    return [f"URGENT {decision_id}: an order is still held by the broker after the session close; "
            f"check the broker, then run `council ops resolve {decision_id} --filled|--cancelled`"]


def _resolve_one(ctx: CycleContext, decision_id: str, now: datetime) -> list[str]:
    from council.broker.instruments import InstrumentMap
    from council.broker.parsing import (
        STATUS_FAILED,
        STATUS_FAILED_AFTER_PARTIAL,
        STATUS_FILLED,
        STATUS_PARTIALLY_FILLED,
        parse_close_order,
        parse_order_status,
        parse_pnl,
    )

    ledger, read = ctx.ledger, ctx.sources.broker
    imap = InstrumentMap.load(ctx.state_dir / "instruments.json")
    port = parse_pnl(read.pnl(), imap.symbol_for)  # type: ignore[union-attr]
    tol = float(ctx.policy.risk["approval"]["post_fill_exposure_tolerance"])
    alerts: list[str] = []
    problems: list[str] = []        # block the decision
    notes: list[str] = []           # recorded, never block
    for row in ledger.legs(decision_id):
        if row.state != WAITING_STATE:
            continue
        if row.kind == "open":
            payload = (read.order_lookup(reference_id=row.request_id) if row.request_id  # type: ignore[union-attr]
                       else read.order_lookup(order_id=row.order_id) if row.order_id else None)  # type: ignore[union-attr]
            if payload is None:
                continue
            st = parse_order_status(payload)
            sid = st.status_id
            fill = {"units_filled": st.filled_units, "fill_price": st.avg_price,
                    "broker_exposure": st.broker_exposure_usd}
            common = {"order_id": st.order_id or row.order_id, "position_ids": st.position_ids,
                      "broker_status": f"{sid}:{st.status_name}", "resolved_at": now,
                      "detail": {**fill, "resolved_by": "watch"}}
            deadline = _leg_deadline(row)
            partial_final = sid == STATUS_PARTIALLY_FILLED and (deadline is None or now > deadline)
            if sid == STATUS_PARTIALLY_FILLED and not partial_final:
                # the remainder may still fill in this session: record the progress, keep waiting
                ledger.update_leg(decision_id, row.seq, now=now, order_id=st.order_id or row.order_id,
                                  position_ids=st.position_ids, broker_status=f"{sid}:{st.status_name}",
                                  detail={**fill, "partial_seen_at": now.isoformat()})
                continue
            if sid == STATUS_FILLED or partial_final:
                mismatch = _fill_mismatch(row, st, tol, full=sid == STATUS_FILLED)
                state = "filled" if sid == STATUS_FILLED else "partially_filled"
                ledger.update_leg(decision_id, row.seq, state=state, error=mismatch, now=now, **common)
                if mismatch:
                    problems.append(f"{row.line}: {mismatch}")
                refit = _sl_refit(ctx, row, st)
                if refit:
                    notes.append(refit)
                    alerts.append(f"URGENT {refit}: the stop-loss no longer protects the fill; "
                                  "check it in the broker and propose a fix")
            elif sid in STATUS_FAILED:
                ledger.update_leg(decision_id, row.seq, state="skipped", now=now,
                                  error=f"cancelled or rejected at the broker (status {sid})", **common)
            elif sid in STATUS_FAILED_AFTER_PARTIAL:
                ledger.update_leg(decision_id, row.seq, state="rejected_partial", now=now,
                                  error=f"cancelled after a partial fill (status {sid})", **common)
        elif row.kind in ("close", "partial_close"):
            info = read.close_order_info(row.order_id) if row.order_id else None  # type: ignore[union-attr]
            status = parse_close_order(info) if info is not None else None
            if status is not None and status.failed:
                ledger.update_leg(decision_id, row.seq, state="skipped", resolved_at=now, now=now,
                                  error="close cancelled or rejected at the broker")
            elif status is not None and status.waiting_for_market:
                continue
            elif row.position_id is not None and _close_done(port, row):
                ledger.update_leg(decision_id, row.seq, state="filled", resolved_at=now, now=now,
                                  position_ids=[row.position_id], detail={"resolved_by": "watch"})
    rows = ledger.legs(decision_id)
    if any(r.state == WAITING_STATE for r in rows):
        return alerts + _timeout_if_due(ledger, decision_id, now)
    fresh = parse_pnl(read.pnl(), imap.symbol_for)  # type: ignore[union-attr]  # after the fills
    final, reasons = _final_after_wait(ctx, rows, fresh, now, problems, notes)
    if final == "blocked":
        ledger.set_blocker_scope(decision_id, "all", now=now)     # a broken fill holds every line
    ledger.transition(decision_id, final, "; ".join(reasons) or f"market opened: {final}",
                      actor="watch", now=now)
    _refresh_report(ledger, decision_id, final, reasons)
    alerts.append(("URGENT " if final == "blocked" else "") + f"{decision_id}: held order resolved → {final}")
    return alerts


def _fill_mismatch(row: Any, st: Any, tol: float, *, full: bool) -> str | None:
    """The post-fill check for an order that filled after its market opened. The planned price is
    NOT used: a held order fills at the next open and a gap there is normal (the stop-loss question
    is `_sl_refit`'s). A full fill must match the units sent, a partial fill must not exceed them,
    and the broker's exposure must match units filled × the average fill price."""
    sent = row.detail.get("units_sent") or row.units
    price, filled = st.avg_price, st.filled_units
    if not sent or not price or filled <= 0:
        return "fill unverifiable (units or fill price missing)"
    sent = float(sent)
    if full and abs(filled / sent - 1) > tol:
        return "filled units differ from the units sent"
    if not full and filled > sent * (1 + tol):
        return "filled units exceed the units sent"
    broker = st.broker_exposure_usd
    if broker is not None and abs(broker / (filled * float(price)) - 1) > tol:
        return "broker-reported exposure differs from units × fill price"
    return None


def _sl_refit(ctx: CycleContext, row: Any, st: Any) -> str | None:
    """`sl_refit_needed:<line>` when the fill left the stop at or through the fill price, or
    closer than SL_REFIT_FLOOR_SHARE of the class floor (a gap at the open)."""
    from council.risk.config import risk_limits
    from council.risk.stops import floor_key

    price, sl = st.avg_price, st.sl_rate or row.sl_rate
    spec = ctx.policy.universe.by_symbol().get(row.line)
    if not price or not sl or spec is None:
        return None
    long = row.direction == "long"
    distance = (price - sl) / price if long else (sl - price) / price
    floor = risk_limits(ctx.policy).catastrophe_stop.floors.get(floor_key(spec))
    if distance <= 0 or (floor is not None and distance < SL_REFIT_FLOOR_SHARE * floor):
        return f"sl_refit_needed:{row.line}"
    return None


def _close_done(port: Any, row: Any) -> bool:
    pos = port.position(row.position_id)
    if pos is None:
        return True
    before, deduct = row.detail.get("position_units_before"), row.detail.get("units_to_deduct")
    return (deduct is not None and before is not None
            and float(before) - pos.units >= float(deduct) * 0.99)


def _final_after_wait(ctx: CycleContext, rows: list[Any], port: Any, now: datetime,
                      problems: list[str], notes: list[str] | None = None) -> tuple[str, list[str]]:
    """blocked only for a unit/exposure mismatch (`problems`), a missing stop-loss, an unknown
    position or a broken expected position. Drift from the approval's targets (which may be hours
    or days old by the time a held order fills) is a reason, never a block: the next cycle trades
    it. A gap at the open is `sl_refit_needed` (in `notes`), also never a block."""
    from council.broker.parsing import snapshot_from_portfolio
    from council.execution.reconcile import ExpectedPosition, reconcile

    reasons = [*problems, *(notes or [])]
    if problems:
        return "blocked", reasons
    expected = [
        ExpectedPosition(position_id=pid, symbol=r.vehicle_symbol, direction=r.direction,
                         leverage=r.leverage, sl_rate=r.sl_rate)
        for r in rows if r.kind == "open" and r.state in ("filled", "partially_filled", "rejected_partial")
        for pid in r.position_ids
    ]
    targets = {r.line: float(r.detail["weight_after"]) for r in rows if r.detail.get("weight_after") is not None}
    rec = reconcile(snapshot_from_portfolio(port, now), targets, expected, ctx.policy)
    rec, corporate_notes = _corporate_reconcile(ctx, rec, port)
    reasons += corporate_notes
    if not rec.protected:
        reasons += [*(f"missing stop-loss: {s}" for s in rec.missing_sl),
                    *(f"unknown position: {s}" for s in rec.unknown_positions), *rec.issues]
        return "blocked", reasons
    if not rec.drift_ok:
        reasons.append("drift above reconcile.drift_max against the approval's targets (recorded, not a "
                       "block: the order filled after its market opened)")
    if any(r.state in ("rejected", "skipped", "partially_filled", "rejected_partial") for r in rows):
        return "completed_partial", reasons
    return "completed", reasons


def _corporate_reconcile(ctx: CycleContext, rec: Any, port: Any) -> tuple[Any, list[str]]:
    """(the reconcile with pending corporate actions taken out, reasons to record) via
    `stocks.corporate.reconcile_corporate`, only while the policy has a stock sleeve (a core-only
    book keeps today's rule: an unknown position blocks). A failure of the corporate step keeps the
    plain reconcile (fail closed: an unknown position still blocks) with a note."""
    from council.stocks import corporate

    if not corporate.sleeve_active(ctx.policy):
        return rec, []
    try:
        fixed, warnings, pending = corporate.reconcile_corporate(
            rec, list(port.positions), ctx.policy, opened=corporate.opened_position_ids(ctx.ledger))
    except Exception as exc:  # noqa: BLE001 - the held order still resolves on the plain reconcile
        return rec, [f"corporate-action reconcile unavailable ({type(exc).__name__}); the plain reconcile applies"]
    notes = [f"warning {w}: a credited position without a stop-loss (sold at the next US session)"
             for w in warnings]
    if pending:
        notes.append("corporate action pending: an unknown position no leg of ours opened; the stock sleeve "
                     "is held until `council stocks adopt`")
    return fixed, notes


def _refresh_report(ledger: Any, decision_id: str, final: str, reasons: list[str]) -> None:
    """Rewrite the stored execution record with the resolved legs, so the watch publishes the
    outcome (never the waiting state)."""
    payload = ledger.get_runtime(f"exec_report:{decision_id}")
    if not payload:
        return
    report = dict(payload["report"])
    by_seq = {r.seq: r for r in ledger.legs(decision_id)}
    legs = []
    for leg in report.get("legs", []):
        row = by_seq.get(leg.get("seq"))
        if row is not None:
            leg = {**leg, "state": row.state, "order_id": row.order_id, "position_ids": row.position_ids,
                   "units_filled": row.detail.get("units_filled", leg.get("units_filled")),
                   "fill_price": row.detail.get("fill_price", leg.get("fill_price")),
                   "error": row.error or ""}
        legs.append(leg)
    report.update({"legs": legs, "final_state": final,
                   "reasons": [*report.get("reasons", []), *reasons]})
    ledger.set_runtime(f"exec_report:{decision_id}", {**payload, "report": report})


def _flatten_proposal(ctx: CycleContext, snapshot: Any, now: datetime) -> str | None:
    """HALT: a flatten PROPOSAL (closes only; the human still approves). Never executes. A position
    whose full close the broker already holds gets no second close (`cycle._plan`)."""
    import uuid

    from council import clock
    from council.cycle import _plan, stamp_sessions

    if not snapshot.positions:
        return None
    plan = _plan(ctx, None, snapshot=snapshot, states={}, kill_state="HALTED")
    if plan is None or not plan.legs:
        return None
    plan = stamp_sessions(plan, ctx.policy.universe, asof=now)
    slot = clock.slot_at_or_before(now)
    decision_id = f"{clock.cycle_id_for(slot)}-flatten-{uuid.uuid4().hex[:6]}"
    base_w = dict(snapshot.signed_w)
    for line, w in ctx.ledger.pending_open_weights().items():   # held opens count as held (as at approval)
        base_w[line] = base_w.get(line, 0.0) + w
    ctx.ledger.create_decision(decision_id=decision_id, kind="flatten",
                               valid_until=clock.proposal_valid_until(slot), cycle_id=None,
                               target={"final_w": {}, "base_w": base_w,
                                       "nav_usd": snapshot.equity_usd},
                               plan=plan, state="proposed", now=now, policy_sha=ctx.policy.sha256)
    ctx.ledger.insert_legs(decision_id, plan.legs)
    return decision_id
