"""The 15-minute watch job. READ-ONLY: it never places an order and never builds an Executor.

  - expires stale proposals (atomic),
  - reveals sealed cycles once their decision is terminal (exact sealed bytes + salt),
  - publishes execution records written by the operator terminal (the operator never runs git),
  - kill switch: on a fresh HALT it creates a standing flatten PROPOSAL and sends an urgent alert,
  - checks that every open position carries a stop-loss and that the cycle heartbeat is fresh.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from council.runtime import CycleContext, LockBusy, instance_lock

TERMINAL = {"completed", "completed_partial", "rejected", "expired", "superseded",
            "reviewed_no_action", "blocked", "execution_unknown"}
HEARTBEAT_MAX = timedelta(hours=5)


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


def _alert(ctx: CycleContext, message: str) -> None:
    if ctx.notifier is None:
        return
    urgent = message.startswith("URGENT")
    with contextlib.suppress(Exception):
        ctx.notifier.send("council watch", message.removeprefix("URGENT ").strip(),
                          priority="urgent" if urgent else "default")


# ---------------------------------------------------------------------- stops and flatten
def _stop_hits(ctx: CycleContext, snapshot: Any, now: datetime) -> list[str]:
    """Positions we expected (last observation) that vanished without one of our closes are
    broker stop-loss hits: record them (R4d re-entry cool-off) and alert."""
    from council.execution.planner import vehicle_to_line

    ledger = ctx.ledger
    live = {p.position_id: p.symbol for p in snapshot.positions}
    seen = {int(k): v for k, v in (ledger.get_runtime("watch_positions", {}) or {}).items()}
    ours = {row.position_id for row in ledger.legs_in_states(["filled", "partially_filled", "submitted", "in_flight"])
            if row.kind in ("close", "partial_close") and row.position_id is not None}
    v2l = vehicle_to_line(ctx.policy.universe)
    alerts = []
    for pid, symbol in seen.items():
        if pid not in live and pid not in ours:
            line = v2l.get(symbol, symbol)
            ledger.record_stop_hit(line=line, symbol=symbol, position_id=pid, at=now)
            alerts.append(f"URGENT stop-loss hit on {line}")
    ledger.set_runtime("watch_positions", {str(k): v for k, v in live.items()})
    return alerts


def _flatten_proposal(ctx: CycleContext, snapshot: Any, now: datetime) -> str | None:
    """HALT: a flatten PROPOSAL (closes only; the human still approves). Never executes."""
    import uuid

    from council import clock
    from council.cycle import _plan

    if not snapshot.positions:
        return None
    plan = _plan(ctx, None, snapshot=snapshot, states={}, kill_state="HALTED")
    if plan is None or not plan.legs:
        return None
    slot = clock.slot_at_or_before(now)
    decision_id = f"{clock.cycle_id_for(slot)}-flatten-{uuid.uuid4().hex[:6]}"
    ctx.ledger.create_decision(decision_id=decision_id, kind="flatten",
                               valid_until=clock.proposal_valid_until(slot), cycle_id=None,
                               target={"final_w": {}, "base_w": dict(snapshot.signed_w),
                                       "nav_usd": snapshot.equity_usd},
                               plan=plan, state="proposed", now=now)
    ctx.ledger.insert_legs(decision_id, plan.legs)
    return decision_id
