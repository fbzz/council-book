"""Operator-terminal commands that can move money: approve and flatten. Reject is here too.

Order of operations for `approve` (any failure stops before a single order is sent):
  1. process guards (TTY, COUNCIL_ROLE=operator, no CI/agent env, no agent ancestor process)
  2. decision state: proposed, not expired, not superseded; HALTED allows flatten/compliance only
  3. risk-increasing plans need their sealed commitment published (commit recorded)
  4. fresh READ snapshot: every position the plan closes still exists; line drift <= policy
  5. fresh quotes: opens refused when the price moved beyond the guard since the proposal
  6. post-trade gross with fresh equity <= the hard-coded 2.0x
  7. screen with every leg (percent and x), then a typed nonce
  8. transition to approved (actor=operator), unlock the WRITE keychain, read the token, lock it
  9. execute (closes → confirm → opens with a stop-loss on every open), store the report for the
     watch job to publish (the operator never runs git)
"""

from __future__ import annotations

import math
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

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


def run_guards(deps: ApprovalDeps) -> None:
    if deps.guard_fn is not None:
        deps.guard_fn()
        return
    from council.operator.guards import assert_operator_context, process_ancestors

    assert_operator_context(env=os.environ, stdin_isatty=sys.stdin.isatty(),
                            stdout_isatty=sys.stdout.isatty(), ancestors=process_ancestors())


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

    imap = InstrumentMap.load(deps.state_dir / "instruments.json")
    snap = snapshot_from_pnl(deps.read.pnl(), vehicle_by_instrument=imap.symbols_by_id(),
                             line_by_vehicle=vehicle_to_line(policy.universe), now=now)
    live_ids = {p.position_id for p in snap.positions}
    for leg in plan.legs:
        if leg.kind in ("close", "partial_close", "modify_sl") and leg.position_id not in live_ids:
            raise ApprovalRefused(f"leg {leg.seq}: position no longer exists (stop hit or manual change)")
    target = _json(d.target_json if hasattr(d, "target_json") else d.target) or {}
    base = target.get("base_w", {})
    drift = sum(abs(snap.signed_w.get(s, 0.0) - base.get(s, 0.0)) for s in set(snap.signed_w) | set(base))
    if drift > float(policy.risk["approval"]["drift_l1_max"]) + 1e-9:
        raise ApprovalRefused(f"book drifted {drift:.3f} since the proposal")
    _price_guard(plan, deps, imap, policy)
    after = dict(snap.signed_w)
    for leg in plan.legs:
        line = leg.line or leg.symbol
        after[line] = leg.weight_after
    gross = sum(abs(w) for w in after.values())
    if gross > GROSS_HARD_MAX + 1e-9:
        raise ApprovalRefused(f"post-trade gross {gross:.2f}x above the hard {GROSS_HARD_MAX:.1f}x")

    _screen(plan, deps, drift=drift, gross=gross)
    from council.operator.guards import new_nonce, typed_nonce_confirm

    nonce = new_nonce()
    if not typed_nonce_confirm(deps.input_fn, nonce):
        raise ApprovalRefused("nonce mismatch: nothing was sent")
    _transition(ledger, decision_id, "approved", "operator approved in the operator terminal")

    from council.execution.executor import Executor
    from council.execution.ratelimit import TokenBucket

    write = deps.write_factory()
    kwargs = dict(deps.executor_kwargs or {})
    executor = Executor(write, deps.read, ledger, TokenBucket(), policy=policy,
                        symbol_for=imap.symbol_for, **kwargs)
    report = executor.execute(decision_id, plan, nav_usd=snap.equity_usd)
    keys = list(ledger.get_runtime("execution_reports", []))
    ledger.set_runtime(f"exec_report:{decision_id}", {
        "report": report.model_dump(mode="json"), "cycle_id": d.cycle_id, "nav_usd": snap.equity_usd,
        "plan": plan.model_dump(mode="json"), "approved_at": now.isoformat(),
        "completed_at": deps.now_fn().isoformat()})
    ledger.set_runtime("execution_reports", [*keys, decision_id])
    deps.print_fn(f"execution finished: {report.final_state} ({report.writes_sent} writes)")
    return report


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


def _screen(plan: Any, deps: ApprovalDeps, *, drift: float, gross: float) -> None:
    p = deps.print_fn
    p("leg  kind           line     vehicle   dir    lev  w before → after   stop   cost bp  risk")
    for leg in plan.legs:
        p(f"{leg.seq:>3}  {leg.kind:<13}  {(leg.line or ''):<7}  {leg.symbol:<8}  {leg.direction:<5}  "
          f"{leg.leverage:>3}  {leg.weight_before:+.3f} → {leg.weight_after:+.3f}   "
          f"{(leg.stop_distance or 0):.1%}  {leg.cost_bps_nav:>6.1f}  {'UP' if leg.risk_increasing else 'down'}")
    p(f"gross after {gross:.2f}x · drift since proposal {drift:.3f} · cost {plan.cost_bps_nav:.1f} bp of NAV")


def _json(value: Any) -> Any:
    import json

    if value is None or isinstance(value, dict | list):
        return value
    return json.loads(value)


def _as_dt(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
