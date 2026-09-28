"""The 15-minute watch job. READ-ONLY: it never places an order and never builds an Executor.

  - expires stale proposals (atomic),
  - reveals sealed cycles once their decision is terminal (exact sealed bytes + salt),
  - publishes execution records written by the operator terminal (the operator never runs git),
  - publishes the weightless ops row of each smoke ticket (M5-D2, `journal/ops/smoke.jsonl`) when
    its public state changes, and moves a ticket from awaiting_publication to proposed once its
    row reached the remote; a smoke execution is never published otherwise (no book or status
    file here either). A vanished smoke position is never recorded as a stop hit (M-3),
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
  - swing book (swing-book.md rev 2, §1.8, §1.9; SW-5): FLAGS ONLY, it never creates an exit. Each
    swing position's observation keeps its stop and take-profit rates and the last bid (long) or
    ask (short). A swing position that left the book is classified from the broker's closed-trade
    record (`closed_trade_route`, once proven) by its actual close rate: `closed_target`,
    `closed_stop` or `closed_external`; no record -> `closed_unclassified` with an URGENT operator
    alert, never a guessed "stop hit" and never an R4d cool-off. A long closed at its take-profit
    raises no URGENT alert. The trade's percent-only outcome goes into its detail first
    (`trade_outcome_detail`). Open trades raise `swing_events` flags once a day each:
    `time_stop_due`, `earnings_exit_due` (a confirmed date in the trade's detail),
    `target_reached_unplaced` (the target never reached the broker and the price is through it);
    the cycle turns them into exit legs at the next swing slot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from council.context import broker_expected
from council.operator import urgent
from council.operator.urgent import KEYCHAIN_UNAVAILABLE, broker_error_kind
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
    ops_rows_published: list[str] = field(default_factory=list)
    smoke_rows_published: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    broker_error: str | None = None     # fixed code when the broker checks could not run
    urgent: list[str] = field(default_factory=list)   # fixed codes of URGENT conditions this run

    @property
    def fail_code(self) -> str | None:
        """The healthcheck `/fail` body: the first URGENT code, or None for a good run."""
        if self.urgent:
            return self.urgent[0]
        if any(a.startswith("URGENT") for a in self.alerts):
            return "urgent_alert"
        return None


def run_watch(ctx: CycleContext, *, ping_client: Any | None = None) -> WatchOutcome:
    """One watch run, then the dead-man switch ping (M5-E2): a success ping at the end of every
    run, `/fail` with a fixed code when the run raised an URGENT condition or an exception (the
    exception is re-raised after the ping). An overlapping run skips the ping: the run holding the
    lock pings. Only a live runner pings (a rehearsal or dry run never feeds the real switch)."""
    try:
        with instance_lock(state_dir=ctx.state_dir):
            out = _run(ctx)
    except LockBusy:
        return WatchOutcome(status="skipped_overlap")
    except Exception as exc:
        _ping(ctx, f"watch_exception:{type(exc).__name__.lower()}", client=ping_client)
        raise
    _ping(ctx, out.fail_code, client=ping_client)
    return out


def _ping(ctx: CycleContext, fail_code: str | None, *, client: Any | None = None) -> None:
    """Healthcheck ping plus the `ops.healthcheck` runtime record. Never raises."""
    settings = ctx.settings
    if getattr(settings, "mode", "") != "live" or not getattr(settings, "healthcheck_url", None):
        return
    try:
        from council.operator import healthcheck
        from council.operator.release import is_marked_sandbox

        if is_marked_sandbox(ctx.state_dir):     # a rehearsal never feeds the real dead-man switch
            return

        result = healthcheck.ping(settings.healthcheck_url, fail_code=fail_code, client=client)
        healthcheck.record(ctx.ledger, result, now=ctx.clock())
    except Exception:  # noqa: BLE001 - the ping never stops the watch
        return


def _run(ctx: CycleContext) -> WatchOutcome:
    ledger, now = ctx.ledger, ctx.clock()
    out = WatchOutcome(status="ok", expired=list(ledger.expire_stale(now)))
    out.alerts += _resolve_waiting(ctx, now)          # without a broker only the timeout applies
    files: dict[str, bytes] = {}
    out.revealed = _reveals(ctx, files)
    unpublished: list[str] = []
    out.executions_published = _executions(ctx, files, unpublished)
    out.ops_rows_published = _ops_rows(ctx, files, unpublished)
    smoke_rows = _smoke_rows(ctx, files, unpublished)
    out.smoke_rows_published = sorted(smoke_rows)
    out.urgent += [code.split(":")[0] for code in dict.fromkeys(unpublished)]
    for code in dict.fromkeys(unpublished):          # a bad record never stops the loop; retried
        what = "a public ops row" if code.startswith("ops_row") else "an execution report"
        urgent.send_once(ctx, code, "council watch", f"{what} is unpublished ({code})", now)
    if files and ctx.publisher is not None:
        try:
            result = ctx.publisher.publish(files, f"watch {now.strftime('%Y-%m-%dT%H%MZ')}: reveals/executions")
            _smoke_published(ctx, smoke_rows, getattr(result, "commit_sha", None), now)
            ledger.set_runtime("revealed", sorted(set(ledger.get_runtime("revealed", [])) | set(out.revealed)))
            ledger.set_runtime("executions_published",
                               sorted(set(ledger.get_runtime("executions_published", []))
                                      | set(out.executions_published)))
            if out.ops_rows_published:              # cleared only after a successful publish
                done = set(out.ops_rows_published)
                ledger.set_runtime(OPS_ROWS_PENDING, [c for c in ledger.get_runtime(OPS_ROWS_PENDING, [])
                                                      if c not in done])
        except Exception as exc:
            out.alerts.append(f"publish_error:{type(exc).__name__}")
    if ctx.sources.broker is not None:
        try:
            out.alerts += _broker_checks(ctx, now)
        except Exception as exc:  # the heartbeat and the other alerts still run
            code = f"broker_error:{broker_error_kind(exc)}"
            out.broker_error = code
            out.urgent.append(code)
            urgent.send_once(ctx, code, "council watch",
                             f"broker checks failed ({code}): stops and the kill switch are unchecked "
                             "until the READ token works (council doctor --live-read)", now)
        if out.broker_error is None:
            out.alerts += _token_expiry(ctx, now)
    elif broker_expected(ctx):                      # G4: onboarded, live, but no broker
        out.broker_error = KEYCHAIN_UNAVAILABLE
        out.urgent.append(KEYCHAIN_UNAVAILABLE)
        urgent.send_once(ctx, KEYCHAIN_UNAVAILABLE, "council watch",
                         "no READ token (Keychain locked or item missing): stops and the kill switch "
                         "are unchecked", now)
    out.alerts += _heartbeat(ctx, now)
    for flag in _daily_backup(ctx, now):             # rate-limited: a failing backup retries every run
        out.urgent.append(BACKUP_ERROR)
        urgent.send_once(ctx, BACKUP_ERROR, "council watch", f"ledger backup failed ({flag})", now)
    for flag in _licensed_sweep(ctx, now):           # once per UTC day (M5-M); flags only
        out.alerts.append(flag)
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
def _executions(ctx: CycleContext, files: dict[str, bytes], unpublished: list[str] | None = None) -> list[str]:
    """Publish each finished execution report. Every record is guarded: one that cannot be made
    public is flagged `execution_unpublished:<type>` and retried next run, never stopping the loop.
    A report without a cycle (a watch flatten, keyed by `decision_ref`) waits for its own public
    document type (M5-N)."""
    from council.publish import journal, redact

    ledger = ctx.ledger
    done = set(ledger.get_runtime("executions_published", []))
    published: list[str] = []
    for key in ledger.get_runtime("execution_reports", []):
        if key in done:
            continue
        try:
            payload = ledger.get_runtime(f"exec_report:{key}")
            if not payload or not hasattr(redact, "public_execution"):
                continue
            if _decision_state(ledger, key) == WAITING_STATE:     # published once resolved
                continue
            if payload.get("cycle_id") is None:                   # G14: no public type yet (M5-N)
                continue
            from council.execution.executor import ExecutionReport
            from council.models.plan import Plan

            report = ExecutionReport.model_validate(payload["report"])
            plan = Plan.model_validate(payload["plan"]) if payload.get("plan") else None
            public = redact.public_execution(
                report, cycle_id=payload["cycle_id"], lines=ctx.policy.universe, nav_usd=payload["nav_usd"],
                plan=plan, approved_at=_dt(payload.get("approved_at")),
                completed_at=_dt(payload.get("completed_at")))
            record_files = journal.execution_files(public)
        except Exception as exc:  # noqa: BLE001 - one bad record never stops the watch
            if unpublished is not None:
                unpublished.append(f"execution_unpublished:{type(exc).__name__}")
            continue
        files.update(record_files)
        published.append(key)
    return published


# ---------------------------------------------------------------------------- ops rows
OPS_ROWS_PENDING = "ops_rows_pending"   # = council.operator.approve.OPS_ROWS_PENDING (not imported:
                                        # approve.py is the operator's execution module; a test pins it)


def _ops_rows(ctx: CycleContext, files: dict[str, bytes], unpublished: list[str] | None = None) -> list[str]:
    """Republish the public ops row of each cycle `ops review` / `resume-exec` finalised (queued
    under `ops_rows_pending`: the operator never runs git). A cycle whose record cannot be made
    public is flagged `ops_row_unpublished:<type>` and stays queued; the caller clears the
    published ids only after the publish succeeds."""
    from council.models.cycle import CycleRecord
    from council.publish import journal, redact

    pending = list(dict.fromkeys(ctx.ledger.get_runtime(OPS_ROWS_PENDING, []) or []))
    if not pending:
        return []
    rows, done = [], []
    for cycle_id in pending:
        try:
            raw = ctx.ledger.get_cycle(cycle_id)
            if not raw:
                continue                                   # no record: stays queued, visible
            rows.append(redact.public_ops_row(CycleRecord.model_validate(raw)))
            done.append(cycle_id)
        except Exception as exc:  # noqa: BLE001 - one bad record never stops the watch
            if unpublished is not None:
                unpublished.append(f"ops_row_unpublished:{type(exc).__name__}")
    if rows:
        try:
            existing = files.get(journal.OPS_PATH) or _read_existing(ctx, journal.OPS_PATH)
            files.update(journal.ops_files(existing, rows))
        except Exception as exc:  # noqa: BLE001
            if unpublished is not None:
                unpublished.append(f"ops_row_unpublished:{type(exc).__name__}")
            return []
    return done


# ---------------------------------------------------------------------------- smoke rows
SMOKE_ROWS_PUBLISHED = "smoke_rows_published"   # = operator.smoke.ROWS_PUBLISHED: {id: public state}


def _smoke_rows(ctx: CycleContext, files: dict[str, bytes], unpublished: list[str] | None = None) -> dict[str, str]:
    """M5-D2 (§8.4): the weightless public row of every smoke ticket whose public state changed
    (proposed → completed | blocked | rejected), in `journal/ops/smoke.jsonl`. Returns {id: state}
    of the rows added to `files`; `_smoke_published` records them after a successful publish."""
    from council.publish import smoke_row

    ledger = ctx.ledger
    done = dict(ledger.get_runtime(SMOKE_ROWS_PUBLISHED, {}) or {})
    rows, states = [], {}
    for d in ledger.decisions(limit=1000):
        if d.kind != "smoke" or not d.commitment_sha:
            continue
        try:
            executed = any(r.submitted_at is not None for r in ledger.legs(d.decision_id))
            state = smoke_row.public_smoke_state(d.state, previous=done.get(d.decision_id), executed=executed)
            if state is None or done.get(d.decision_id) == state:
                continue
            rows.append(smoke_row.PublicSmokeRow(
                id=d.decision_id, step=str((d.target or {}).get("smoke_step", "")), state=state,
                commitment=d.commitment_sha))
            states[d.decision_id] = state
        except Exception as exc:  # noqa: BLE001 - one bad record never stops the watch
            if unpublished is not None:
                unpublished.append(f"ops_row_unpublished:{type(exc).__name__}")
    if not rows:
        return {}
    try:
        existing = files.get(smoke_row.SMOKE_PATH) or _read_existing(ctx, smoke_row.SMOKE_PATH)
        files.update(smoke_row.smoke_files(existing, rows))
    except Exception as exc:  # noqa: BLE001
        if unpublished is not None:
            unpublished.append(f"ops_row_unpublished:{type(exc).__name__}")
        return {}
    return states


def _smoke_published(ctx: CycleContext, states: dict[str, str], commit_sha: str | None, now: datetime) -> None:
    """After a successful publish: remember each row's public state, and move a ticket whose
    `proposed` row reached the remote from awaiting_publication to proposed (its published commit
    is what `approve` re-checks). Without a commit (a dry run) nothing becomes approvable."""
    if not states:
        return
    ledger = ctx.ledger
    done = dict(ledger.get_runtime(SMOKE_ROWS_PUBLISHED, {}) or {})
    for decision_id, state in states.items():
        if state == "proposed" and not commit_sha:
            continue
        done[decision_id] = state
        if state != "proposed":
            continue
        d = ledger.get_decision(decision_id)
        if d.state == "awaiting_publication":
            ledger.set_published_commit(decision_id, commit_sha, now=now)
            ledger.transition(decision_id, "proposed", "smoke ops row published", now=now)
    ledger.set_runtime(SMOKE_ROWS_PUBLISHED, done, now=now)


# ------------------------------------------------------------------------ broker checks
def _broker_checks(ctx: CycleContext, now: datetime) -> list[str]:
    from council.cycle import _snapshot_and_kill

    alerts: list[str] = []
    ledger = ctx.ledger
    prev = ledger.get_runtime("kill_state", "NORMAL")
    snapshot, kill_state, _ = _snapshot_and_kill(ctx, now)
    alerts += _stop_hits(ctx, snapshot, now)
    try:
        alerts += swing_flags(ctx.ledger, ctx.policy, snapshot, now)
    except Exception as exc:  # noqa: BLE001 - a flag failure never stops the watch
        alerts.append(f"swing_flags_error:{type(exc).__name__}")
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


BACKUP_ERROR = "backup_error"


def _daily_backup(ctx: CycleContext, now: datetime) -> list[str]:
    """Once per UTC day, a private ledger backup (`ops.backup`). Never raises; returns the fixed
    failure flags. The caller turns a failure into one URGENT per 4 h (a failed backup is retried
    on every run, so an unthrottled alert would fire every run) and a healthcheck `/fail`."""
    try:
        from council.operator import licensed
        from council.ops import backup

        return list(backup.maybe_daily_backup(ctx.state_dir, now,
                                              licensed_check=licensed.backup_check(ctx.state_dir)))
    except Exception as exc:  # noqa: BLE001
        return [f"{BACKUP_ERROR}:{type(exc).__name__}"]


def _licensed_sweep(ctx: CycleContext, now: datetime) -> list[str]:
    """Once per UTC day, delete licensed payloads before they reach the 7-day retention
    (`council.operator.licensed.sweep`, shared with the cycle hook: whichever runs first that day
    writes the receipt). Never raises; returns `purge_error:*` flags."""
    try:
        from council.operator import licensed

        return list(licensed.sweep(ctx.state_dir, now))
    except Exception as exc:  # noqa: BLE001
        return [f"purge_error:{type(exc).__name__}"]


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
    ours |= set(ledger.smoke_positions())       # M-3: a smoke position never feeds R4d cool-off
    raw_sigma = ledger.get_runtime(STOCK_SIGMA_4H_KEY, {})
    sigma = {str(k): _positive(v) for k, v in raw_sigma.items()} if isinstance(raw_sigma, dict) else {}
    from council.swing.book import swing_vehicle_map

    vanished = corporate.classify_vanished_positions(seen, live, ours, ctx.policy, sigma_4h=sigma,
                                                     swing_map=swing_vehicle_map(ledger, ctx.policy))
    alerts = []
    for v in vanished:
        if v.outcome == corporate.SWING_CLOSED:   # never a core R4d stop hit
            alerts += _swing_closed(ctx, v, live, now)
            continue
        if v.outcome == corporate.STOP_HIT:
            ledger.record_stop_hit(line=v.line, symbol=v.symbol, position_id=v.position_id, at=now)
            alerts.append(f"URGENT stop-loss hit on {v.line}")
    alerts += corporate.vanished_alerts(vanished)
    ledger.set_runtime("watch_positions", {str(pid): observation(p) for pid, p in live.items()})
    return alerts


def observation(p: Any) -> dict[str, Any]:
    """The PRIVATE per-position observation: stop and take-profit rates, and the quote a close would
    fill at: the bid for a long, the ask for a short (`close_rate` is the side's closing quote)."""
    out = {"symbol": p.symbol, "sl_rate": p.sl_rate, "bid": p.close_rate if p.is_buy else None}
    if getattr(p, "tp_rate", None):
        out["tp_rate"] = p.tp_rate
    if not p.is_buy:
        out["ask"] = p.close_rate
    return out


# ------------------------------------------------------------------------------ swing book (SW-5b)
SWING_CLOSE_TOL = 0.005        # a close within 0.5% of the stop / target rate is that exit
DECLARED_COST_PCT_PER_LEG = 1.25


def closed_trade_route_ok(state_dir: Any) -> bool:
    """The closed-trade READ route is used only once its smoke step proved it (`closed_trade_route`)."""
    try:
        from council.operator import capabilities

        return capabilities.load(state_dir).has("closed_trade_route")
    except Exception:  # noqa: BLE001 - unproven = unused
        return False


def classify_swing_close(side: str, sl_rate: float | None, tp_rate: float | None,
                         record: Any) -> tuple[str, float | None]:
    """(state, close rate) from the broker's closed-trade record: at / through the take-profit ->
    `closed_target`, at / through the stop -> `closed_stop`, anything else (manual, liquidation,
    corporate action) -> `closed_external`. No record or no close rate -> `closed_unclassified`."""
    rate = record.get("closeRate") if isinstance(record, dict) else None
    if isinstance(rate, bool) or not isinstance(rate, int | float) or not rate > 0:
        return "closed_unclassified", None
    sign = 1.0 if side == "long" else -1.0
    if tp_rate and (rate - tp_rate) * sign >= -SWING_CLOSE_TOL * tp_rate:
        return "closed_target", float(rate)
    if sl_rate and (rate - sl_rate) * sign <= SWING_CLOSE_TOL * sl_rate:
        return "closed_stop", float(rate)
    return "closed_external", float(rate)


def trade_outcome_detail(trade: Any, close_rate: float | None, exit_kind: str, now: datetime, *,
                         sector_etf_ret: float | None = None,
                         declared_cost_pct_per_leg: float = DECLARED_COST_PCT_PER_LEG) -> dict[str, Any]:
    """A closed trade's percent-only outcome for its detail (numbers and short codes only):
    r_declared, net_ret, size_nav, beta, sector_etf_ret, exit_kind, days_held. Without a close rate
    only the codes and counts are kept (the metrics then leave the trade out)."""
    from council.swing.rules import sessions_until

    d = dict(trade.detail or {})
    held = sessions_until(trade.opened_at.date(), now.date()) if trade.opened_at is not None else 0
    out: dict[str, Any] = {"exit_kind": exit_kind, "days_held": int(held),
                           "size_nav": _num(d.get("size_nav")), "beta": _num(d.get("beta")),
                           "sector_etf_ret": _num(sector_etf_ret)}
    if close_rate and trade.open_rate:
        sign = 1.0 if trade.side == "long" else -1.0
        net = sign * (close_rate / trade.open_rate - 1.0) - 2.0 * declared_cost_pct_per_leg / 100.0
        stop = _num(d.get("stop_pct"))
        if stop is None and trade.sl_rate:
            stop = abs(trade.open_rate - trade.sl_rate) / trade.open_rate
        out["net_ret"] = round(net, 6)
        if stop:
            out["r_declared"] = round(net / stop, 6)
    return out


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def record_swing_close(ledger: Any, read: Any, trade_id: str, position_id: int | None, *, now: datetime,
                       state_dir: Any = None, route_ok: bool | None = None,
                       sector_bars: Any = None) -> str | None:
    """Classify and record one swing trade the broker closed (its SL / TP, or outside them). The
    closed-trade record is read only when `closed_trade_route` is proven. Returns the new state, or
    None when the trade is unknown, already closed, or still holds another live position."""
    from council.operator.smoke import closed_trade_record
    from council.swing.models import TERMINAL_STATES

    trade = ledger.swing_trade(trade_id) if trade_id else None
    if trade is None or trade.state in TERMINAL_STATES or trade.state in ("proposed", "entry_executing"):
        return None
    ok = closed_trade_route_ok(state_dir) if route_ok is None else route_ok
    record = closed_trade_record(read, position_id) if ok else None
    state, rate = classify_swing_close(trade.side, trade.sl_rate, trade.tp_rate,
                                       dict(record) if record is not None else None)
    from council.swing.exits import sector_etf_return

    sector = sector_etf_return(trade.detail or {}, trade.opened_at, now, sector_bars)
    ledger.update_swing_trade(trade_id, detail=trade_outcome_detail(trade, rate, state.removeprefix("closed_"),
                                                                    now, sector_etf_ret=sector), now=now)
    ledger.transition_swing_trade(trade_id, state, close_rate=rate, now=now,
                                  reason=f"broker_close:{state.removeprefix('closed_')}")
    return state


def _swing_closed(ctx: CycleContext, v: Any, live: dict[int, Any], now: datetime) -> list[str]:
    """One vanished swing position: record its trade's broker close (once every position of the
    trade is gone) and alert. Only an unclassified or external close is URGENT."""
    from council.swing.models import ACTIVE_STATES

    ledger = ctx.ledger
    try:
        trades = [t for t in ledger.swing_trades(states=sorted(ACTIVE_STATES)) if v.position_id in t.position_ids]
    except Exception:  # noqa: BLE001 - an old ledger without swing tables
        trades = []
    if not trades:
        return [f"URGENT swing position closed at the broker without a trade record: {v.line}"]
    trade = trades[0]
    if any(pid in live for pid in trade.position_ids):
        return [f"swing position partly closed at the broker: {v.line}"]
    try:
        swing_src = getattr(ctx.sources, "swing", None)
        state = record_swing_close(ledger, ctx.sources.broker, trade.trade_id, v.position_id, now=now,
                                   state_dir=ctx.state_dir, route_ok=closed_trade_route_ok(ctx.state_dir),
                                   sector_bars=getattr(swing_src, "daily_bars", None))
    except Exception as exc:  # noqa: BLE001 - never a guessed outcome: the operator checks
        return [f"URGENT swing_close_error:{v.line}:{type(exc).__name__}"]
    if state == "closed_target":
        return [f"swing trade closed at its take-profit: {v.line}"]
    if state == "closed_stop":
        return [f"swing stop-loss hit (pre-approved): {v.line}"]
    if state == "closed_external":
        return [f"URGENT swing position closed outside its stop and target: {v.line} (check the broker)"]
    return [f"URGENT swing_close_unclassified:{v.line}: no closed-trade record; check the broker"]


FLAG_KINDS = ("time_stop_due", "earnings_exit_due", "target_reached_unplaced")


def swing_flags(ledger: Any, policy: Any, snapshot: Any, now: datetime) -> list[str]:
    """The watch's swing flags (§1.8): one `swing_events` row per trade, kind and US day. FLAGS ONLY:
    the cycle creates the exits at the next swing slot."""
    from datetime import date as _date

    from council.clock import NEW_YORK
    from council.swing.rules import exit_due
    from council.swing.slots import season_of

    try:
        trades = ledger.swing_trades(states=["open", "open_tp_missing", "partial"])
    except Exception:  # noqa: BLE001 - no swing tables: nothing to flag
        return []
    if not trades:
        return []
    sp = policy.swing
    today = now.astimezone(NEW_YORK).date()
    slots = sp.slots.summer_utc if season_of(now) == "summer" else sp.slots.winter_utc
    marks = {}
    for p in snapshot.positions:
        marks[p.position_id] = p.close_rate          # bid for a long, ask for a short
    seen = {(e["trade_id"], e["kind"], str(e["created_at"])[:10]) for e in ledger.swing_events()
            if e["kind"] in FLAG_KINDS}
    alerts: list[str] = []

    def flag(t: Any, kind: str, reason: str) -> None:
        key = (t.trade_id, kind, now.date().isoformat())
        if key in seen:
            return
        ledger.add_swing_event(kind, trade_id=t.trade_id, reason=reason, now=now)
        seen.add(key)
        alerts.append(f"swing {kind}: {t.trade_id}")

    for t in trades:
        detail = t.detail or {}
        if t.time_stop_date:
            due, _ = exit_due(_date.fromisoformat(t.time_stop_date), today, slots_per_session=len(slots),
                              lead_slots=0)
            if due:
                flag(t, "time_stop_due", "time_stop")
            earn = detail.get("earnings_next")
            if isinstance(earn, str) and detail.get("earnings_confirmed") is True:
                e_due, when = exit_due(_date.fromisoformat(t.time_stop_date), today, slots_per_session=len(slots),
                                       lead_slots=int(sp.earnings.exit_proposal_lead_slots),
                                       earnings_next=_date.fromisoformat(earn), earnings_confirmed=True,
                                       exit_before_sessions=int(sp.earnings.exit_before_sessions))
                if e_due and when < _date.fromisoformat(t.time_stop_date):
                    flag(t, "earnings_exit_due", "earnings")
        unplaced = t.state == "open_tp_missing" or detail.get("tp_mode") == "none"
        if unplaced and t.tp_rate:
            sign = 1.0 if t.side == "long" else -1.0
            px = [marks[pid] for pid in t.position_ids if marks.get(pid)]
            if px and any((m - t.tp_rate) * sign >= 0 for m in px):
                flag(t, "target_reached_unplaced", "target_watched")
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
                if refit and not _sl_modify_verified(ctx):
                    # M5-D1: stop changes are unproven (S2), so the fix is a close, never a modify_sl
                    close = refit.replace("sl_refit_needed", "sl_refit_close_needed", 1)
                    notes += [refit, close, "capability_missing:sl_modify"]
                    alerts.append(f"URGENT {close}: the stop-loss no longer protects the fill and stop "
                                  "changes are unverified (capability_missing:sl_modify); check it in "
                                  "the broker and propose a close")
                elif refit:
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


def _sl_modify_verified(ctx: CycleContext) -> bool:
    """The M5-D1 `sl_modify` gate (S2 proven). Only reached with a connected broker; never raises."""
    try:
        from council.operator import capabilities

        return capabilities.load(ctx.state_dir).has("sl_modify")
    except Exception:  # noqa: BLE001 - an unreadable gate is a false gate
        return False


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
    from council.swing.book import swing_vehicle_map

    smoke_ids: set[int] = set()
    try:            # smoke positions are expected, never unknown (S7 / S8 on a stock no line owns)
        smoke_ids = set(ctx.ledger.smoke_positions())
        if rows and getattr(ctx.ledger.get_decision(rows[0].decision_id), "kind", None) == "smoke":
            smoke_ids |= {int(p) for r in rows if r.kind == "open" for p in r.position_ids}
    except Exception:  # noqa: BLE001 - without the smoke view the plain reconcile applies (fail closed)
        smoke_ids = set()
    rec = reconcile(snapshot_from_portfolio(port, now), targets, expected, ctx.policy,
                    swing_map=swing_vehicle_map(ctx.ledger, ctx.policy), smoke_position_ids=smoke_ids)
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
    decision_id = flatten_ref(now)
    if _decision_state(ctx.ledger, decision_id) is not None:      # same minute: keep ids unique
        decision_id = f"{decision_id}-{uuid.uuid4().hex[:6]}"
    base_w = dict(snapshot.signed_w)
    for line, w in ctx.ledger.pending_open_weights().items():   # held opens count as held (as at approval)
        base_w[line] = base_w.get(line, 0.0) + w
    ctx.ledger.create_decision(decision_id=decision_id, kind="flatten",
                               valid_until=clock.proposal_valid_until(slot), cycle_id=None,
                               target={"final_w": {}, "base_w": base_w,
                                       "nav_usd": snapshot.equity_usd, "decision_ref": decision_id},
                               plan=plan, state="proposed", now=now, policy_sha=ctx.policy.sha256)
    ctx.ledger.insert_legs(decision_id, plan.legs)
    return decision_id


def flatten_ref(now: datetime) -> str:
    """G14: a watch flatten has no cycle; it is keyed `<minute>-flatten` (e.g.
    `2026-10-01T1447Z-flatten`), carried as `decision_ref` into the execution report."""
    return f"{now.astimezone(UTC).strftime('%Y-%m-%dT%H%MZ')}-flatten"


# ------------------------------------------------------------------------ token expiry
TOKEN_EXPIRY_KEY = "token_expiry_checked_day"
TOKEN_EXPIRY_WARN = timedelta(days=14)


def _token_expiry(ctx: CycleContext, now: datetime) -> list[str]:
    """Once per UTC day: read the Agent Portfolio metadata and warn when a token expires within
    TOKEN_EXPIRY_WARN (URGENT within 3 days). Read-only; a failure is one rate-limited alert."""
    ledger, day = ctx.ledger, now.astimezone(UTC).date().isoformat()
    if ledger.get_runtime(TOKEN_EXPIRY_KEY) == day:
        return []
    try:
        meta = ctx.sources.broker.agent_portfolios()
    except Exception as exc:  # noqa: BLE001 - retried next run; the alert is rate-limited
        code = f"broker_error:{broker_error_kind(exc)}"
        urgent.send_once(ctx, f"token_expiry:{code}", "council watch",
                         f"token-expiry check failed ({code})", now)
        return []
    ledger.set_runtime(TOKEN_EXPIRY_KEY, day)        # only a completed check counts for the day
    soonest = min(_expiries(meta), default=None)
    if soonest is None:
        return []
    left = soonest - now
    if left <= timedelta(days=3):
        return [f"URGENT the Agent Portfolio token expires in {max(0, left.days)} day(s): renew it"]
    if left <= TOKEN_EXPIRY_WARN:
        return [f"the Agent Portfolio token expires in {left.days} days: plan its renewal"]
    return []


def _expiries(meta: Any) -> list[datetime]:
    """Every parseable expiry timestamp in the metadata (keys containing 'expir'), as UTC."""
    found: list[datetime] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if "expir" in str(key).lower() and isinstance(value, str):
                    try:
                        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    found.append(at if at.tzinfo else at.replace(tzinfo=UTC))
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(meta)
    return found
