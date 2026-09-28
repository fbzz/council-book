"""Operator-terminal commands that can move money: approve and flatten. Reject is here too, and the
incident commands that never send an order: `resume_exec` (lookups only), `resolve_waiting`,
`review_blocked` and `resume_kill_switch` (m5-readiness §9.1-§9.2).

Order of operations for `approve` (any failure stops before a single order is sent):
  1. process guards (TTY, COUNCIL_ROLE=operator, no CI/agent env, no agent ancestor process)
  2. decision state: proposed, not expired, not superseded; HALTED allows flatten/compliance only
  3. risk-increasing plans need their sealed commitment published (commit recorded)
  3a. market hours: a leg past its own deadline, or whose market is closed now or closes within
     clock.APPROVAL_CLOSE_MARGIN (5 min), is DROPPED and printed with the reason. Flatten and
     compliance may drop any leg. A rebalance applies the drop rule: if a dropped leg reduces
     risk, every risk-increasing leg is dropped too; an open whose dependency was dropped is
     dropped; a dropped re-open (same line, same direction as the close it waits for: a re-opened
     remainder or a vehicle switch) drops that close too, so a trim never becomes a full exit.
     Repeated until nothing changes. What remains is either the plan minus opens or the pre-trade
     book plus closes, both inside the limits the engine checked. Nothing left → refused, with
     every leg's reason.
  3b. a rebalance must have been made under the current policy (stored policy SHA; the cycle
     record's for decisions written before it was stored). Flatten and compliance are exempt:
     they only reduce and must never be stranded by a policy commit. A smoke ticket (M5-D2) is
     exempt too, but needs its published ops row (step 3, every step) and a NORMAL kill switch
     (a close-only smoke ticket also under WARN / RESUMED).
  4. fresh READ snapshot: every position a kept leg closes still exists; line drift <= policy.
     The current book is the snapshot PLUS the opens the broker still holds for a closed market
     (ledger.pending_open_weights), exactly as the engine counted it in the proposal's base.
  5. fresh quotes: opens refused when the price moved beyond the guard since the proposal
  6. post-trade gross (kept legs, held opens included) with fresh equity <= the hard-coded 2.0x
  7. screen with every kept leg (percent and x) and every dropped one, then a typed nonce
  7a. the clock is read again after the nonce: refused if the decision expired meanwhile or if the
     market-hours drops at that moment differ from the ones on the screen (nothing is sent)
  8. transition to approved (actor=operator), unlock the WRITE keychain, read the token, lock it
  9. execute (closes → confirm → opens with a stop-loss on every open), store the report for the
     watch job to publish (the operator never runs git)
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from council import clock
from council.clock import utcnow
from council.invariants import GROSS_HARD_MAX


class ApprovalRefused(RuntimeError):
    pass


@dataclass
class ApprovalDeps:
    ledger: Any
    policy: Any
    read: Any                                             # EtoroReadClient (READ token)
    write_factory: Callable[[], Any]                      # builds EtoroWriteClient after unlock
    state_dir: Any
    input_fn: Callable[[str], str] = input
    print_fn: Callable[[str], None] = print
    now_fn: Callable[[], datetime] = utcnow
    guard_fn: Callable[[], None] | None = None            # default: real process guards
    executor_kwargs: Mapping[str, Any] | None = None
    journal_dir: Any = None                               # M5-K why screen; default paths.JOURNAL_DIR


def run_guards(deps: ApprovalDeps) -> None:
    if deps.guard_fn is not None:
        deps.guard_fn()
        return
    from council.operator import guards

    guards.assert_current_process_is_operator()      # env, both TTYs, ancestor processes


def _transition(ledger: Any, decision_id: str, state: str, reason: str) -> None:
    ledger.transition(decision_id, state, reason, actor="operator")


def approve(decision_id: str, deps: ApprovalDeps) -> Any:
    from council.broker.instruments import InstrumentMap
    from council.execution.planner import vehicle_to_line
    from council.models.plan import Plan
    from council.risk.exposure import snapshot_from_pnl

    run_guards(deps)
    ledger, policy, now = deps.ledger, deps.policy, deps.now_fn()
    ledger.expire_stale(now)
    d = ledger.get_decision(decision_id)
    if d.state != "proposed":
        raise ApprovalRefused(f"decision is {d.state}, not proposed")
    if d.valid_until is not None and _as_dt(d.valid_until) <= now:
        raise ApprovalRefused("proposal expired")
    plan = Plan.model_validate(_json(d.plan_json if hasattr(d, "plan_json") else d.plan))
    kill_state = ledger.get_runtime("kill_state", "NORMAL")
    if kill_state in ("HALTED", "FLAT") and d.kind not in ("flatten", "compliance"):
        raise ApprovalRefused(f"kill switch {kill_state}: only flatten or compliance may be approved")
    if plan.risk_increasing and not getattr(d, "published_commit", None):
        raise ApprovalRefused("risk-increasing plan whose commitment was never published")
    if d.kind == "smoke":
        # M5-D2: a smoke ticket needs its weightless ops row published (every step, risk-reducing
        # ones too) and a NORMAL kill switch; a flatten supersedes it anyway
        if not getattr(d, "published_commit", None):
            raise ApprovalRefused("smoke ticket whose ops row was never published")
        reducing = all(leg.kind in ("close", "partial_close") for leg in plan.legs)
        if kill_state != "NORMAL" and not reducing:   # a smoke close stays approvable under WARN
            raise ApprovalRefused(f"kill switch {kill_state}: smoke tickets need NORMAL")
    dropped = market_hours_drops(plan, d.kind, policy, now)
    kept = [leg for leg in plan.legs if leg.seq not in dropped]
    if not kept:
        reasons = "; ".join(f"leg {seq}: {why}" for seq, why in sorted(dropped.items()))
        raise ApprovalRefused(f"nothing left to execute: {reasons}")
    if d.kind == "rebalance":
        _policy_check(d, ledger, policy)
    kept_plan = plan.model_copy(update={"legs": kept})

    imap = InstrumentMap.load(deps.state_dir / "instruments.json")
    snap = snapshot_from_pnl(deps.read.pnl(), vehicle_by_instrument=imap.symbols_by_id(),
                             line_by_vehicle=vehicle_to_line(policy.universe), now=now)
    live_ids = {p.position_id for p in snap.positions}
    for leg in kept:
        if leg.kind in ("close", "partial_close", "modify_sl") and leg.position_id not in live_ids:
            raise ApprovalRefused(f"leg {leg.seq}: position no longer exists (stop hit or manual change)")
    target = _json(d.target_json if hasattr(d, "target_json") else d.target) or {}
    base = target.get("base_w", {})
    current = current_book(snap.signed_w, ledger.pending_open_weights())
    drift = sum(abs(current.get(s, 0.0) - base.get(s, 0.0)) for s in set(current) | set(base))
    if drift > float(policy.risk["approval"]["drift_l1_max"]) + 1e-9:
        raise ApprovalRefused(f"book drifted {drift:.3f} since the proposal")
    _price_guard(kept_plan, deps, imap, policy)
    gross = _gross_after(current, plan, kept)
    if gross > GROSS_HARD_MAX + 1e-9:
        raise ApprovalRefused(f"post-trade gross {gross:.2f}x above the hard {GROSS_HARD_MAX:.1f}x")

    _screen(kept_plan, deps, drift=drift, gross=gross, deadline=_as_dt(d.valid_until) if d.valid_until else None)
    why_screen(d, kept_plan, deps)
    for seq, why in sorted(dropped.items()):
        deps.print_fn(f"dropped leg {seq}: {why}")
    from council.operator.guards import new_nonce, typed_nonce_confirm

    nonce = new_nonce()
    if not typed_nonce_confirm(deps.input_fn, nonce):
        raise ApprovalRefused("nonce mismatch: nothing was sent")
    _recheck_after_nonce(d, plan, policy, dropped, deps.now_fn())
    _transition(ledger, decision_id, "approved", "operator approved in the operator terminal")

    from council.execution.executor import Executor
    from council.execution.ratelimit import TokenBucket

    write = deps.write_factory()
    kwargs = dict(deps.executor_kwargs or {})
    executor = Executor(write, deps.read, ledger, TokenBucket(), policy=policy,
                        symbol_for=imap.symbol_for, **kwargs)
    report = executor.execute(decision_id, plan, nav_usd=snap.equity_usd, dropped=dropped)
    keys = list(ledger.get_runtime("execution_reports", []))
    ledger.set_runtime(f"exec_report:{decision_id}", {
        "report": report.model_dump(mode="json"), "cycle_id": d.cycle_id,
        "decision_ref": d.cycle_id or (d.target or {}).get("decision_ref") or decision_id,
        "nav_usd": snap.equity_usd,
        "plan": plan.model_dump(mode="json"), "approved_at": now.isoformat(),
        "completed_at": deps.now_fn().isoformat()})
    ledger.set_runtime("execution_reports", [*keys, decision_id])
    deps.print_fn(f"execution finished: {report.final_state} ({report.writes_sent} writes)")
    return report


def resolve_waiting(decision_id: str, outcome: str, deps: ApprovalDeps) -> None:
    """`council ops resolve`: the operator checked the broker and records what happened to the
    orders a decision still has waiting for their market (`filled` or `cancelled`). Ledger only:
    nothing is sent. The decision (waiting_for_market or blocked) becomes reviewed_no_action."""
    if outcome not in ("filled", "cancelled"):
        raise ApprovalRefused("outcome must be 'filled' or 'cancelled'")
    run_guards(deps)
    ledger, now = deps.ledger, deps.now_fn()
    d = ledger.get_decision(decision_id)
    if d.state not in ("waiting_for_market", "blocked"):
        raise ApprovalRefused(f"decision is {d.state}: nothing waits for its market")
    waiting = [r for r in ledger.legs(decision_id) if r.state == "waiting_for_market"]
    if not waiting:
        raise ApprovalRefused("no leg of this decision waits for its market")
    for row in waiting:
        if outcome == "filled":
            ledger.update_leg(decision_id, row.seq, state="filled", resolved_at=now, now=now,
                              detail={"resolved_by": "operator"})
        else:
            ledger.update_leg(decision_id, row.seq, state="skipped", resolved_at=now, now=now,
                              error="cancelled at the broker (recorded by the operator)")
    _transition(ledger, decision_id, "reviewed_no_action",
                f"operator recorded {len(waiting)} held order(s) as {outcome} after checking the broker")
    deps.print_fn(f"{decision_id}: {len(waiting)} held order(s) recorded as {outcome}; "
                  "the next cycle reads the book from the broker")


REVIEWABLE_STATES = ("blocked", "execution_unknown")
OPS_ROWS_PENDING = "ops_rows_pending"       # cycle ids whose public ops row the watch republishes
KILL_RESUMES = "kill_resumes"               # private log of operator resumes of the kill switch


def resume_exec(decision_id: str, deps: ApprovalDeps) -> Any:
    """`council resume-exec`: recover an executing, execution_unknown or blocked decision with
    lookups and reconcile ONLY. The executor is built without a write client and runs its resume
    with a `NoWriteClient`, so nothing can be sent; a leg that provably never reached the broker
    becomes skipped (a fresh proposal and approval are needed). The resumed report replaces the
    stored one and is queued again for the watch to publish."""
    from council.broker.instruments import InstrumentMap
    from council.execution.executor import RESUMABLE_STATES, Executor
    from council.execution.ratelimit import TokenBucket

    run_guards(deps)
    ledger = deps.ledger
    d = ledger.get_decision(decision_id)
    if d.state not in RESUMABLE_STATES:
        raise ApprovalRefused(f"decision is {d.state}: nothing to resume (resume-exec takes "
                              "executing, execution_unknown or blocked)")
    if deps.read is None:
        raise ApprovalRefused("no READ client: resume-exec needs the READ token for its lookups")
    kwargs = dict(deps.executor_kwargs or {})
    if "symbol_for" not in kwargs:
        kwargs["symbol_for"] = InstrumentMap.load(deps.state_dir / "instruments.json").symbol_for
    executor = Executor(None, deps.read, ledger, TokenBucket(), policy=deps.policy, **kwargs)
    report = executor.resume(decision_id)
    _store_report(ledger, decision_id, d, report, deps)
    deps.print_fn(f"resume finished: {report.final_state} ({report.writes_sent} writes; lookups only)")
    if report.final_state in REVIEWABLE_STATES:
        deps.print_fn("still held: check the broker, then `council-op ops review "
                      f"{decision_id} --reason ...` (or resume-exec again later)")
    return report


def _store_report(ledger: Any, decision_id: str, d: Any, report: Any, deps: ApprovalDeps) -> None:
    """Replace the stored execution report with the resumed one and let the watch publish it again
    (the watch publishes a report once; a resumed outcome supersedes the earlier record)."""
    key = f"exec_report:{decision_id}"
    payload = dict(ledger.get_runtime(key) or {})
    payload.update({"report": report.model_dump(mode="json"), "cycle_id": payload.get("cycle_id") or d.cycle_id,
                    "decision_ref": payload.get("decision_ref") or d.cycle_id
                    or (d.target or {}).get("decision_ref") or decision_id,
                    "resumed_at": deps.now_fn().isoformat(), "completed_at": deps.now_fn().isoformat()})
    if "nav_usd" not in payload:                   # no approval report (e.g. a crash before storing it)
        before = getattr(report, "equity_before", None)
        if before is None:
            return                                  # nothing to size the public record with: keep private
        payload["nav_usd"] = before
    ledger.set_runtime(key, payload)
    keys = list(ledger.get_runtime("execution_reports", []))
    if decision_id not in keys:
        ledger.set_runtime("execution_reports", [*keys, decision_id])
    done = list(ledger.get_runtime("executions_published", []))
    if decision_id in done:
        ledger.set_runtime("executions_published", [k for k in done if k != decision_id])


def review_blocked(decision_id: str, reason: str, deps: ApprovalDeps) -> None:
    """`council ops review`: after checking the broker, the operator clears a blocked (or
    execution_unknown) decision that has no active and no waiting leg. Ledger only: nothing is
    sent. The decision becomes reviewed_no_action with the reason, and the reason is queued for
    publication on the cycle's public ops row (it must carry no amount, id, link, path or e-mail)."""
    text = " ".join((reason or "").split())
    if not text:
        raise ApprovalRefused("a reason is required (it is published)")
    run_guards(deps)
    ledger, now = deps.ledger, deps.now_fn()
    d = ledger.get_decision(decision_id)
    if d.state not in REVIEWABLE_STATES:
        raise ApprovalRefused(f"decision is {d.state}: ops review clears only blocked or execution_unknown")
    from council.ledger.states import LEG_ACTIVE_STATES, WAITING_STATE

    legs = ledger.legs(decision_id)
    active = [r.seq for r in legs if r.state in LEG_ACTIVE_STATES]
    if active:
        raise ApprovalRefused(f"leg(s) {active} still active at the broker: run resume-exec first")
    waiting = [r.seq for r in legs if r.state == WAITING_STATE]
    if waiting:
        raise ApprovalRefused(f"leg(s) {waiting} wait for their market: use ops resolve")
    public = _public_reason(text)
    _transition(ledger, decision_id, "reviewed_no_action", f"operator review: {public}")
    _queue_public_outcome(ledger, d, "reviewed_no_action", f"operator review: {public}", now)
    deps.print_fn(f"{decision_id}: reviewed (no action); the next cycle runs and the watch publishes "
                  "the reason")


def _public_reason(text: str) -> str:
    """The reason exactly as it will be published, or ApprovalRefused if publishing would change
    it (an amount, a long number, an id, a link, a path or an e-mail address)."""
    from council.publish.redact import clean_text

    cleaned = clean_text(text, 180)
    if cleaned != text[:180].strip() or len(text) > 180:
        raise ApprovalRefused("the reason is published: at most 180 characters and no amounts, ids, "
                              "links, paths or e-mail addresses")
    return cleaned


def _queue_public_outcome(ledger: Any, d: Any, state: str, reason: str, now: datetime) -> None:
    """Record the final outcome on the private cycle record and queue its public ops row for the
    watch (the operator never runs git)."""
    if not d.cycle_id:
        return
    rec = ledger.get_cycle(d.cycle_id)
    if rec:
        rec["decision_state"] = state
        rec["decision_reason"] = reason[:200]
        ledger.record_cycle(rec, now=now)
    pending = list(ledger.get_runtime(OPS_ROWS_PENDING, []))
    if d.cycle_id not in pending:
        ledger.set_runtime(OPS_ROWS_PENDING, [*pending, d.cycle_id], now=now)


def resume_kill_switch(reason: str, deps: ApprovalDeps) -> str:
    """`council resume --reason`: leave HALTED or FLAT after recovery. Refused without a reason and
    from NORMAL or WARN. The lifetime peak and the recent equity reads are untouched, so the next
    watch or cycle re-halts while equity is still below the halt line."""
    from council.risk import killswitch

    text = " ".join((reason or "").split())
    if not text:
        raise ApprovalRefused("a reason is required")
    run_guards(deps)
    ledger, now = deps.ledger, deps.now_fn()
    prev = str(ledger.get_runtime("kill_state", "NORMAL"))
    try:
        decision = killswitch.resume(prev, text)
    except ValueError as exc:
        raise ApprovalRefused(str(exc)) from exc
    ledger.set_runtime("kill_state", decision.state, now=now)
    log = list(ledger.get_runtime(KILL_RESUMES, []))
    ledger.set_runtime(KILL_RESUMES, [*log, {"at": now.isoformat(), "from": prev, "reason": text[:200]}], now=now)
    deps.print_fn(f"kill switch {prev} -> {decision.state}; the lifetime peak is unchanged, so the next "
                  "check halts again while equity is below the halt line")
    return decision.state


def reject(decision_id: str, reason: str, deps: ApprovalDeps) -> None:
    if not reason.strip():
        raise ApprovalRefused("a reason is required (it is published)")
    run_guards(deps)
    _transition(deps.ledger, decision_id, "rejected", reason.strip()[:200])


def write_client_factory(settings: Any) -> Callable[[], Any]:
    """Unlock the separate write keychain (the user types its password at the OS prompt), read the
    token, lock the keychain again immediately, then build the writer (lazy import)."""

    def factory() -> Any:
        from council.operator import keychain

        keychain.unlock_write_keychain()
        try:
            token = keychain.read_secret(keychain.WRITE_SERVICE, keychain=keychain.write_keychain_path())
            api_key = keychain.read_secret(keychain.API_KEY_SERVICE)
        finally:
            keychain.lock_write_keychain()
        from council.broker.etoro_write import EtoroWriteClient

        return EtoroWriteClient(api_key, token, base_url=settings.etoro_base_url)

    return factory


# ------------------------------------------------------------------------------ market hours
def leg_session(leg: Any, policy: Any) -> str | None:
    """The session stamped on the leg, else derived from the policy (legs written before stamping);
    None when the line is unknown (an unmapped position)."""
    if getattr(leg, "session", None):
        return str(leg.session)
    spec = policy.universe.by_symbol().get(leg.line or "")
    return clock.vehicle_session(spec, leg.symbol) if spec is not None else None


def _closed_reason(leg: Any, session: str | None, now: datetime) -> str | None:
    """Why this leg cannot be sent now, or None. A close past the calendars gets weekday hours."""
    if leg.valid_until is not None and _as_dt(leg.valid_until) <= now:
        return f"past its deadline {_as_dt(leg.valid_until):%H:%MZ}"
    if session is None:
        return None if leg.kind in ("close", "partial_close") else "unknown market session"
    closing = leg.kind in ("close", "partial_close")
    if not clock.session_open(session, now, closing=closing):
        missing = clock.calendar_missing(session, now)
        return f"{session} market closed" + (f" ({missing})" if missing else "")
    soon = now + clock.APPROVAL_CLOSE_MARGIN
    if not clock.session_open(session, soon, closing=closing):
        minutes = int(clock.APPROVAL_CLOSE_MARGIN.total_seconds() // 60)
        return f"{session} market closes within {minutes} min"
    return None


def market_hours_drops(plan: Any, kind: str, policy: Any, now: datetime) -> dict[int, str]:
    """{seq: reason} for every leg that must not be sent at `now` (see step 3a). The drop rule, the
    dependency rule and (rebalance) the re-open rule are applied until nothing changes."""
    dropped: dict[int, str] = {}
    for leg in plan.legs:
        why = _closed_reason(leg, leg_session(leg, policy), now)
        if why is not None:
            dropped[leg.seq] = why
    by_seq = {leg.seq: leg for leg in plan.legs}
    changed = True

    def drop(leg: Any, why: str) -> None:
        nonlocal changed
        if leg.seq not in dropped:
            dropped[leg.seq] = why
            changed = True

    while changed:
        changed = False
        if kind == "rebalance" and any(not by_seq[seq].risk_increasing for seq in dropped):
            for leg in plan.legs:
                if leg.risk_increasing:
                    drop(leg, "drop rule: a risk-reducing leg of this plan was dropped")
        for leg in plan.legs:
            if any(dep in dropped for dep in leg.depends_on):
                drop(leg, "its dependency was dropped")
        if kind == "rebalance":
            for leg in plan.legs:
                if leg.kind == "open" and leg.seq in dropped:
                    for close in _same_side_closes(leg, by_seq):
                        drop(close, f"its re-open (leg {leg.seq}) was dropped: the line keeps its position")
    return dropped


def _same_side_closes(open_leg: Any, by_seq: Mapping[int, Any]) -> list[Any]:
    """The closes an open waits for on the SAME line in the SAME direction: the open re-opens a
    remainder or switches vehicle, so the close alone would turn a trim into a full exit. A flip
    (the open takes the other side) is not one: closing alone is then a plain reduction."""
    line = open_leg.line or open_leg.symbol
    out = []
    for dep in open_leg.depends_on:
        close = by_seq.get(dep)
        if (close is not None and close.kind in ("close", "partial_close")
                and (close.line or close.symbol) == line and close.direction == open_leg.direction):
            out.append(close)
    return out


def _recheck_after_nonce(d: Any, plan: Any, policy: Any, dropped: Mapping[int, str], now: datetime) -> None:
    """Step 7a: the operator may take minutes to type the nonce. Nothing is sent if the decision
    expired meanwhile or the market-hours drops at `now` differ from the ones on the screen (a leg
    reached its deadline, or its market's close came within the margin)."""
    if d.valid_until is not None and _as_dt(d.valid_until) <= now:
        raise ApprovalRefused("proposal expired while the nonce was typed: nothing was sent")
    again = market_hours_drops(plan, d.kind, policy, now)
    if again != dict(dropped):
        changed = sorted(set(again) ^ set(dropped))
        raise ApprovalRefused(f"market hours changed while the nonce was typed (legs {changed}): "
                              "nothing was sent; run approve again to see the new screen")


def current_book(signed_w: Mapping[str, float], pending: Mapping[str, float]) -> dict[str, float]:
    """The book the engine sees: the broker snapshot plus the opens the broker still holds for a
    closed market (ledger.pending_open_weights), summed by line."""
    out = {line: float(w) for line, w in signed_w.items()}
    for line, w in pending.items():
        out[line] = out.get(line, 0.0) + float(w)
    return out


def _policy_check(d: Any, ledger: Any, policy: Any) -> None:
    """3b: a rebalance must have been made under the policy that is live now."""
    made_under = getattr(d, "policy_sha", None)
    if not made_under and d.cycle_id:
        record = ledger.get_cycle(d.cycle_id) or {}
        made_under = record.get("policy_sha")
    if not made_under:
        raise ApprovalRefused("rebalance without a recorded policy SHA: wait for a fresh proposal")
    if made_under != policy.sha256:
        raise ApprovalRefused(f"policy changed since the proposal (made under {made_under[:12]}, "
                              f"now {policy.sha256[:12]}): wait for a fresh proposal")


def _gross_after(current: Mapping[str, float], plan: Any, kept: list[Any]) -> float:
    """Post-trade gross: a line whose legs are all kept lands on its planned weight; a line with
    dropped legs moves only by the kept legs' changes."""
    after = dict(current)
    kept_seqs = {leg.seq for leg in kept}
    by_line: dict[str, list[Any]] = {}
    for leg in plan.legs:
        by_line.setdefault(leg.line or leg.symbol, []).append(leg)
    for line, legs in by_line.items():
        if all(leg.seq in kept_seqs for leg in legs):
            after[line] = legs[-1].weight_after
        else:
            after[line] = after.get(line, 0.0) + sum(
                leg.weight_after - leg.weight_before for leg in legs if leg.seq in kept_seqs)
    return sum(abs(w) for w in after.values())


# ------------------------------------------------------------------------------ helpers
def _price_guard(plan: Any, deps: ApprovalDeps, imap: Any, policy: Any) -> None:
    from council.broker.parsing import parse_rates

    opens = [leg for leg in plan.legs if leg.kind == "open" and leg.units and leg.amount_usd]
    if not opens:
        return
    ids = [i for i in (imap.get(leg.symbol) for leg in opens) if i is not None]
    quotes = parse_rates(deps.read.rates(ids), imap.symbol_for)
    for leg in opens:
        q = quotes.get(leg.symbol)
        if q is None:
            raise ApprovalRefused(f"no fresh quote for {leg.symbol}")
        planned = leg.amount_usd / leg.units
        now_px = q.ask if leg.direction == "long" else q.bid
        move = abs(math.log(now_px / planned))
        # guard: 1.5 x a 4-hour sigma, approximated from the leg's stop distance (>= 3%)
        limit = max(0.03, float(policy.risk["approval"]["open_price_guard_sigma4h"])
                    * (leg.stop_distance or 0.1) / (3.0 * math.sqrt(5) * math.sqrt(6)))
        if move > limit:
            raise ApprovalRefused(f"{leg.symbol} moved {move:.2%} since the proposal (guard {limit:.2%})")


def _screen(plan: Any, deps: ApprovalDeps, *, drift: float, gross: float,
            deadline: datetime | None = None) -> None:
    p = deps.print_fn
    p("leg  kind           line     vehicle   dir    lev  w before → after   stop   cost bp  risk  market  approve by")
    for leg in plan.legs:
        until = f"{_as_dt(leg.valid_until):%H:%MZ}" if leg.valid_until else "-"
        p(f"{leg.seq:>3}  {leg.kind:<13}  {(leg.line or ''):<7}  {leg.symbol:<8}  {leg.direction:<5}  "
          f"{leg.leverage:>3}  {leg.weight_before:+.3f} → {leg.weight_after:+.3f}   "
          f"{(leg.stop_distance or 0):.1%}  {leg.cost_bps_nav:>6.1f}  {'UP' if leg.risk_increasing else 'down':<4}  "
          f"{(leg.session or '-'):<6}  {until}")
    p(f"gross after {gross:.2f}x · drift since proposal {drift:.3f} · cost {plan.cost_bps_nav:.1f} bp of NAV")
    if deadline is not None:
        p(f"approval deadline {deadline:%Y-%m-%d %H:%MZ} (a leg past its own time above is dropped)")


def why_screen(d: Any, plan: Any, deps: ApprovalDeps) -> list[str]:
    """M5-K: per line with a leg, why it moved (the public trail over the sealed document) and its
    legs in percent and x. A display aid only: it never refuses and never raises; a leg without a
    trail is flagged so the operator can reject."""
    from council.operator.why import decision_why

    try:
        missing = decision_why(d, plan, state_dir=deps.state_dir, journal_dir=deps.journal_dir,
                               echo=deps.print_fn)
    except Exception as exc:
        deps.print_fn(f"WARNING: why trail unavailable ({type(exc).__name__}); reject unless you know why "
                      "each line moves")
        return []
    if missing:
        deps.print_fn(f"WARNING: no trail for {', '.join(missing)}; reject unless you know why it moves")
    return missing


def _json(value: Any) -> Any:
    import json

    if value is None or isinstance(value, dict | list):
        return value
    return json.loads(value)


def _as_dt(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
