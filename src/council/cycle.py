"""The 4-hour council cycle. Runs unattended (launchd) with a READ-ONLY broker token, or on demand.

Order of operations (each step fails closed):
  preflight (lock, slot, invariants, disk) → broker snapshot + NAV/kill (if connected) → history →
  fact pack (percent-only, available_at ≤ slot) → reference book → officers (vol/event cards) →
  bands → council (analysts, debate, PM ×3, audit, medoid) → risk engine → plan (if connected) →
  ledger → seal + publish → proposal → notify.

This module never imports the broker writer and never places an order: a proposal only becomes an
order when the human operator approves it in the operator terminal (council.operator.approve).

Market hours: every leg is stamped with its vehicle's session and its own deadline
(`stamp_sessions`: min(next slot - 5 min, session close - 10 min), early closes and FX breaks
included). A rebalance stays approvable until its latest leg's deadline (the approval drops expired
or closed legs by the drop rule); flatten and compliance are valid until the next slot - 5 min.
Each decision records the policy SHA it was made under. Orders the broker holds for a closed
market (waiting_for_market) are resolved read-only before the broker snapshot, and a flatten never
adds a second close for a position whose close the broker already holds.

Smoke tickets (M5-D2, K20): while a smoke ticket is pending or in flight, or a smoke-opened position
is open (`ledger.smoke_active`), a live cycle seals no rebalance or compliance proposal (flag
`smoke_open`) and publishes its ops row only: no cycle document, status or book (they would carry
the smoke weight). A HALTED / FLAT cycle still seals its flatten (which supersedes a pending ticket),
publishing the ops row only; that flatten keeps no public cycle link, so its execution report stays
private (M5-N). Meanwhile the NAV state and the lifetime peaks are never initialised or
raised; after the token-day tickets they restart once, before the first live proposal.

Costs and trade size (WP-E): the cycle prices the $1 fixed fee as a PRIVATE scalar of NAV from the
snapshot's equity and the operator's mirror ratio (`runtime.cycle_trade_economics`; missing →
the policy's assumed ratio and the flag `mirror_ratio_missing`), sets it on the real non-crypto cost
quotes, gives the engine the held reference levels (`risk.held_levels`), the real-dollar trade
floor and discretionary-only trailing budgets, stamps each leg with its origin, reference level
and fee, arms the planner's gap guard on stock lines, and feeds the kill switch the real account's
cumulative extra fee drag. None of these numbers is published or shown to the council.

Corporate actions (design §3.6): with a stock sleeve, the cycle start checks the broker snapshot for
positions no line owns that no open leg of ours created (a spin-off or stock-for-stock credit) and
for positions on retired vehicles (`stocks.corporate.detect`). Each raises a satellite-scoped R20
blocker (public-safe codes: the stock sleeve is held, the core trades) and an URGENT alert to the
operator naming the instrument and the command (private: the alert is never published). A failing
check holds the satellite too (`corporate_action_check_failed`).

What each agent saw (transparency-v2 §2.2, T1/T1v): the council gets a private input sink, so every
model call's exact input (its sections, earlier turns, news items, instruction tail) is recorded
before it is sent, and a call that times out keeps its input; the capture is written to
`state_dir/calls/` (broker-licensed item texts apart in `state_dir/licensed/calls/`), never inside the
repository, and read only from the operator terminal (`council inputs <cycle>`). Capture never stops
a cycle (`inputs_capture_error:<type>`). The first cycle of each UTC day purges licensed copies that
would pass 7 days before the next daily run (`council.operator.purge`; a failure is `purge_error:*`).

Why each line moved (transparency-v2 §4, T5a): the record keeps the structured drops, the medoid's
fall-back lines, the bands before the analysts' cards, the lines with new evidence, the claim-to-line
tags and the publishable values of the cited evidence (`record_trail`), so `council why <cycle>`
can walk every line from the reference to execution. It never stops a cycle (`trail_record_error:*`).

News (T3b): the news role reads the public-domain items and, with a connected Agent Portfolio and the
switch on, the broker's feed (`council.context.news_sources`); their flags (`news_source_error:*`,
`news_broker_feed:off`, ...) join the record, and the private record keeps each public item's link.
The final leak scan before a publish also guards the pack's licensed texts (feed text) and the
private NAV figures; the material-change fingerprint is keyed by the private install key.

Live data path (WP-G, design §11): stock lines take their history from the dedicated stock source
(`facts.market.history_source`; their states use its availability rule and label); returns are
aligned on the core lines' calendar, so a short-history stock never shortens the core's covariance
window; the stock lines' fundamentals facts enter the pack; the material-change rule is per line
(`runtime.material_fingerprints`: a discretionary change needs new evidence on its own line or
market-wide, and session admission is not evidence); and the plan reads eligibility and rates only
for the vehicles of the current lines and the instruments of held positions.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from council import clock
from council.context import broker_expected
from council.invariants import check_policy
from council.models.cycle import CycleRecord, Debate
from council.models.plan import Plan
from council.models.risk import Band, RiskDecision, changed_lines
from council.operator import urgent
from council.operator.urgent import KEYCHAIN_UNAVAILABLE, broker_error_kind
from council.policy import LineSpec, Universe
from council.runtime import (
    CycleContext,
    LockBusy,
    consume_fingerprints,
    cost_hints,
    cycle_trade_economics,
    disk_ok,
    engine_quotes,
    fingerprints_digest,
    first_cycle_of_utc_day,
    floor_cost_quotes,
    instance_lock,
    material_changes,
    material_fingerprint,
    material_fingerprints,
    window,
)
from council.swing.sources import (
    SwingSources,  # noqa: F401 - re-exported (the swing stage's inputs)
)

REDACT_ERROR = "redact_error"             # flag prefix and URGENT kind when public_cycle raises (V11)


def redact_failed(rec: CycleRecord) -> bool:
    return any(f.startswith(REDACT_ERROR + ":") for f in rec.flags)


SMOKE_BASELINE_RESET_KEY = "smoke_baseline_reset"   # = ledger.db.SMOKE_BASELINE_RESET_KEY
SMOKE_OPEN_FLAG = "smoke_open"            # K20: no proposal sealed while a smoke ticket is active (M5-D2)
REAL_PEAK_KEY = "real_adjusted_peak"      # ledger runtime: lifetime peak of the real-adjusted equity (D19)


@dataclass
class CycleOutcome:
    cycle_id: str
    status: str
    basis: str | None = None
    decision_id: str | None = None
    decision_state: str | None = None
    legs: int = 0
    commit_sha: str | None = None
    published: bool = False
    flags: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------------- entry
def run_cycle(ctx: CycleContext, *, force: bool = False) -> CycleOutcome:
    return asyncio.run(run_cycle_async(ctx, force=force))


async def run_cycle_async(ctx: CycleContext, *, force: bool = False) -> CycleOutcome:
    now = ctx.clock()
    info = clock.classify(now)
    cycle_id = info.cycle_id
    try:
        with instance_lock(state_dir=ctx.state_dir):
            if ctx.ledger.cycle_exists(cycle_id) and not force:
                return CycleOutcome(cycle_id=cycle_id, status="already_done")
            if info.status == "missed" and not force:
                rec = _bare_record(ctx, info, now, status="missed")
                ctx.ledger.record_cycle(rec)
                published, sha = _publish_ops_only(ctx, rec)
                return CycleOutcome(cycle_id=cycle_id, status="missed", published=published, commit_sha=sha)
            if not disk_ok(ctx.state_dir):
                rec = _bare_record(ctx, info, now, status="skipped_disk")
                ctx.ledger.record_cycle(rec)
                return CycleOutcome(cycle_id=cycle_id, status="skipped_disk")
            return await _run(ctx, info, now)
    except LockBusy:
        return CycleOutcome(cycle_id=cycle_id, status="skipped_overlap")


LAUNCHD_LABEL_PREFIX = "com.fbzz.council"
RUNNER_LAUNCHD = "runner:launchd"


def runner_flags(env: Any = None) -> list[str]:
    """`runner:launchd` when launchd started this process (its job label is in XPC_SERVICE_NAME)."""
    import os

    env = os.environ if env is None else env
    return [RUNNER_LAUNCHD] if str(env.get("XPC_SERVICE_NAME", "")).startswith(LAUNCHD_LABEL_PREFIX) else []


def _bare_record(ctx: CycleContext, info: clock.SlotInfo, now: datetime, *, status: str) -> CycleRecord:
    return CycleRecord(
        cycle_id=info.cycle_id, slot=info.slot, started_at=now, finished_at=now, status=status,  # type: ignore[arg-type]
        late_by_s=int(info.late_by.total_seconds()), mode=ctx.settings.mode, policy_sha=ctx.policy.sha256,
        model=ctx.settings.ollama_model, flags=runner_flags(),
    )


# ------------------------------------------------------------------------------------ core
async def _run(ctx: CycleContext, info: clock.SlotInfo, now: datetime) -> CycleOutcome:
    from council.deliberation.officers import event_cards, vol_cards
    from council.facts.features import market_states
    from council.facts.market import history_sources
    from council.facts.pack import build_fact_pack
    from council.facts.returns import returns_matrix
    from council.reference.book import build_reference
    from council.risk.authority import compute_bands
    from council.risk.churn import event_block
    from council.risk.costs import passes_cost_gate

    policy, ledger = ctx.policy, ctx.ledger
    lines: list[LineSpec] = list(policy.universe.lines)
    slot, cycle_id = info.slot, info.cycle_id
    started = time.monotonic()
    flags: list[str] = []
    check_policy(policy)

    expired = ledger.expire_stale(now)
    if expired:
        flags.append(f"expired:{len(expired)}")
    flags += daily_purge(ctx, now)

    # ---- broker snapshot, NAV and kill switch (only once the Agent Portfolio is connected)
    snapshot, kill_state, nav = None, "NORMAL", None
    corporate_blockers: list[str] = []
    if ctx.sources.broker is None and broker_expected(ctx):
        # G4: onboarded live account without a broker (e.g. a locked Keychain): never AWAITING_ACCOUNT
        return _skip_broker(ctx, info, now, flags, KEYCHAIN_UNAVAILABLE)
    if ctx.sources.broker is not None:
        try:
            _settle_held_orders(ctx, now)   # before the snapshot, so a filled hold is not counted twice
            snapshot, kill_state, nav = _snapshot_and_kill(ctx, now)
        except Exception as exc:  # any broker read failure: skip safely, alert once per 4 h
            return _skip_broker(ctx, info, now, flags, f"broker_error:{broker_error_kind(exc)}")
        corporate_blockers = corporate_actions(ctx, snapshot, cycle_id)
    smoke = ledger.smoke_active([p.position_id for p in snapshot.positions]) if snapshot is not None else []
    # M5-D1 capability gates: consulted only with a connected broker (None = nothing changes)
    caps = _capabilities(ctx, now) if snapshot is not None else None
    # PRIVATE: the fixed fee and the real trade floor as NAV shares (never published or prompted)
    econ = cycle_trade_economics(policy, ctx.state_dir, snapshot.equity_usd if snapshot is not None else None)

    # ---- history → states → pack
    history, hist_flags = ctx.sources.history(slot)
    flags += hist_flags
    raw_states = market_states(policy, history, now=slot, sources=history_sources(policy) or None)
    returns = returns_matrix(history, master=core_lines(policy))
    ev_start, ev_end = window(slot)
    events, ev_flags = ctx.sources.events(ev_start, ev_end)
    flags += ev_flags
    # news flags stay out of the pack: a skew drop depends on an item dated after the slot
    news_flags: list[str] = []
    news, news_extras = gather_news(ctx, slot, news_flags)
    run_macro = first_cycle_of_utc_day(ledger.get_runtime("last_macro_day"), slot)
    macro: dict[str, Any] = {}
    if ctx.sources.macro is not None:
        macro, m_flags = ctx.sources.macro(slot)
        flags += m_flags
    fundamentals: list[Any] = []
    if ctx.sources.fundamentals is not None and policy.universe.stock_lines():
        try:
            fundamentals, f_flags = ctx.sources.fundamentals(slot)
        except Exception as exc:  # stock-only evidence: its failure never stops the core's cycle
            fundamentals, f_flags = [], [f"fundamentals_error:{type(exc).__name__}"]
        flags += f_flags
    quotes = floor_cost_quotes(policy, quoted_at=slot, fee_bps=econ.fee_nav_bps)
    cost_facts = _cost_facts(quotes, slot)
    pack = build_fact_pack(cycle_id=cycle_id, slot=slot, now=now, policy=policy, states=raw_states,
                           news=news, events=events, macro=macro, cost_facts=cost_facts,
                           quality_flags=flags, fundamental_facts=fundamentals)
    states = pack.states
    flags += record_stock_sigma_4h(ledger, policy, states)

    # ---- reference book
    ref_flags: list[str] = []
    ref = build_reference(cycle_id=cycle_id, lines=lines, states=states, returns=returns,
                          policy=policy, flags=ref_flags)
    unit = {s: e.unit_weight for s, e in ref.entries.items()}
    ref_levels = {s: e.level_ref for s, e in ref.entries.items()}
    current_levels = _current_levels(snapshot, unit)

    # ---- officers and bands
    code_cards = vol_cards(pack, policy) + event_cards(pack, slot, policy)
    event_blocked = {ln.symbol for ln in lines if event_block(ln, pack.events, slot, policy)}
    lever_ok, short_ok = set(), set()
    for ln in lines:
        st = states.get(ln.symbol)
        sigma = st.sigma_ann if st is not None else None
        if not ln.council_deviations or not sigma:
            continue
        q_lev = quotes.get((ln.symbol, "long", 2))
        if q_lev and passes_cost_gate(q_lev, sigma, q_lev_class(ln, q_lev), policy, toward_reference=False)[0]:
            lever_ok.add(ln.symbol)
        q_short = quotes.get((ln.symbol, "short", 1))
        if q_short and ln.shortable and passes_cost_gate(q_short, sigma, q_lev_class(ln, q_short), policy,
                                                         toward_reference=False)[0]:
            short_ok.add(ln.symbol)
    if caps is not None:                # §5: no short band / leverage extension until proven
        if not caps.has("cfd_short"):
            short_ok = set()
        if not caps.has("cfd_leverage"):
            lever_ok = set()

    def bands_fn(cards: list) -> dict[str, Band]:
        return compute_bands(lines=lines, ref=ref_levels, states=states, cards=cards,
                             current_levels=current_levels, kill_state=kill_state,
                             event_blocked=event_blocked, lever_ok=lever_ok, short_ok=short_ok,
                             policy=policy)

    bands = bands_fn(code_cards)

    # ---- council
    rec = CycleRecord(
        cycle_id=cycle_id, slot=slot, started_at=now, status=info.status, mode=ctx.settings.mode,
        late_by_s=int(info.late_by.total_seconds()), input_hash=pack.input_hash,
        policy_sha=policy.sha256, prompt_manifest_sha=_manifest_sha(ctx), model=ctx.settings.ollama_model,
        kill_state=kill_state if kill_state in ("NORMAL", "WARN", "HALTED", "FLAT") else "NORMAL",
        reference=ref, bands=bands, cards=list(code_cards), flags=runner_flags() + list(flags) + ref_flags + news_flags,
    )
    if snapshot is not None:            # connected: say when the fee uses an assumed mirror ratio
        rec.flags += [f for f in econ.flags if f.startswith("mirror_ratio")]
    if caps is not None:                # codes only: capability_missing:<cap> / capability_unproven:<cap>
        rec.flags += [f for f in caps.flags() if f not in rec.flags]
    try:                                # PRIVATE (ledger): counts and public items only; never stops a cycle
        rec.extras["news_fetch"] = news_record(pack, news_extras)
    except Exception as exc:
        rec.flags.append(f"news_record_error:{type(exc).__name__}")
    rec.model_digest = await _digest(ctx)
    sink = _input_sink()
    basis, levels, raw = await _council(ctx, rec, pack=pack, ref=ref, bands=bands, bands_fn=bands_fn,
                                   code_cards=code_cards, current_levels=current_levels,
                                   quotes=quotes, run_macro=run_macro, kill_state=kill_state,
                                   ref_levels=ref_levels, now=now, started=started, sink=sink)
    # write what each agent saw right away: a later failure in this cycle must not lose it
    rec.flags += _write_calls(ctx, cycle_id, raw, sink)
    if run_macro and ctx.sources.macro is not None:
        ledger.set_runtime("last_macro_day", slot.date().isoformat())

    # ---- swing book (swing-book.md §1.1): never raises; paper-only while SWING_BOOK_LIVE is False
    swing = await run_swing(ctx, rec, snapshot=snapshot, kill_state=kill_state, nav=nav, slot=slot, now=now,
                            econ=econ, sink=sink)
    rec.flags += [f for f in swing.flags if f not in rec.flags]
    # the swing roles' calls join the cycle's calls (the site counts them; `_calls` publishes role,
    # status, latency and tokens only); the canary's stay private (ledger only, H11)
    rec.calls = [*rec.calls, *swing.calls]
    for call in swing.private_calls:
        try:
            ledger.record_role_call(cycle_id, call)
        except Exception as exc:  # noqa: BLE001 - a record failure never stops the cycle
            rec.flags.append(f"swing_call_record_error:{type(exc).__name__}")
    if swing.calls and sink is not None:     # re-capture: the swing roles' inputs join the core's
        try:
            from council.deliberation.capture import write_cycle_inputs

            rec.flags += [f for f in write_cycle_inputs(ctx.state_dir, sink, cycle_id=cycle_id,
                                                        captured_at=ctx.clock()) if f not in rec.flags]
        except Exception as exc:  # noqa: BLE001 - capture never stops a cycle
            rec.flags.append(f"inputs_capture_error:{type(exc).__name__}")
    if swing.live and swing.slot_ok and ledger.get_runtime(SWING_LIVE_SINCE_KEY) is None:
        ledger.set_runtime(SWING_LIVE_SINCE_KEY, slot.astimezone(clock.NEW_YORK).date().isoformat(), now=now)

    # ---- risk engine
    fps, material_changed = _material(ledger, pack, rec.cards, kill_state)
    rec.material_fingerprint = fingerprints_digest(fps)
    # PRIVATE (M5-N): the broker minimum reaches the engine, so a line below it holds as R11 rather
    # than reaching the planner's size skip; read once and reused by the plan
    rows = _eligibility_rows(ctx, snapshot, rec.flags)
    broker_min = broker_min_shares(policy, rows, snapshot.equity_usd if snapshot is not None else None)
    rec.flags += size_floor_binding_flags(policy, broker_min, econ.copy_floor_share if econ is not None else 0.0)
    decision = _evaluate(ctx, rec, levels=levels, ref_levels=ref_levels, bands=rec.bands or bands,
                         states=states, snapshot=snapshot, unit=unit, kill_state=kill_state,
                         quotes=quotes, pack=pack, material_changed=material_changed, basis=basis,
                         slot=slot, returns=returns, nav=nav, econ=econ, extra_blockers=corporate_blockers,
                         extra_lines=swing.lines, broker_min_share=broker_min)
    rec.risk = decision

    # ---- plan (connected account only)
    plan = None
    if snapshot is not None and ctx.sources.broker is not None:
        try:
            plan = _plan(ctx, decision, snapshot=snapshot, states=states, kill_state=kill_state,
                         ref_levels=ref_levels, unit=unit, econ=econ, history=history, caps=caps, rows=rows,
                         swing_orders=swing_orders_after_engine(swing, decision) if swing.live else None)
            if plan is not None:
                plan = stamp_swing_legs(stamp_sessions(plan, policy.universe, asof=slot), slot)
        except Exception as exc:  # planning failure never trades; it is published as a flag
            import logging

            logging.getLogger("council.cycle").exception("planning failed")
            rec.flags.append(f"plan_failed:{type(exc).__name__}")
    rec.plan = plan

    # ---- ledger + decision
    rec.finished_at = ctx.clock()
    decision_id = None
    valid_until: datetime | None = None
    if plan is not None and plan.legs:
        kind = "flatten" if kill_state in ("HALTED", "FLAT") else ("compliance" if decision.compliance and not _discretionary(decision) else "rebalance")
    else:
        kind = None
    # K20 (M5-D2): while a smoke ticket is pending or in flight, or a smoke position is open, no
    # rebalance or compliance proposal is sealed and only the ops row is published (the cycle
    # document, status and book would carry the smoke position's weight, M-2). HALTED / FLAT still
    # seal their flatten, which supersedes a pending ticket; only the ops row is published and the
    # flatten keeps no public cycle link (its execution report would carry the smoke weight).
    smoke_hold = bool(smoke) and kill_state not in ("HALTED", "FLAT")
    if smoke_hold:
        rec.flags.append(SMOKE_OPEN_FLAG)
        kind = None
    if kind is not None and plan is not None:
        decision_id = f"{cycle_id}-{kind}-{uuid.uuid4().hex[:6]}"
        valid_until = decision_valid_until(plan, kind, slot)
        ledger.create_decision(
            decision_id=decision_id, kind=kind, valid_until=valid_until,
            # a flatten sealed while a smoke position is open keeps no public cycle link: its
            # execution report would carry the smoke leg's weight (and so the NAV); like a watch
            # flatten it waits for its own public type (M5-N)
            cycle_id=None if smoke else cycle_id,
            target={"final_w": decision.final_w, "base_w": decision.base_w,
                    "nav_usd": snapshot.equity_usd if snapshot else None},
            plan=plan, state="awaiting_publication", now=now, policy_sha=policy.sha256)
        ledger.insert_legs(decision_id, plan.legs)
        rec.decision_id, rec.decision_state = decision_id, "awaiting_publication"
        if swing.live:
            rec.flags += record_swing_decision(ledger, swing, plan, decision_id, cycle_id, now)
    else:
        rec.decision_state = "reviewed_no_action"
    rec.flags += record_trail(rec, pack, code_bands=bands, material_changed=material_changed, lines=lines)
    ledger.record_cycle(rec)
    for call in rec.calls:
        ledger.record_role_call(cycle_id, call)

    # ---- seal + publish
    if smoke:   # a HALTED flatten too: the cycle document would carry the smoke weight (M-2)
        published, sha = _publish_ops_only(ctx, rec)
    else:
        published, sha = _seal_and_publish(ctx, rec, pack, reveal_now=decision_id is None,
                                           snapshot=snapshot, smoke_open=bool(smoke))
    state = rec.decision_state
    if redact_failed(rec):
        # no commitment exists: the decision never becomes `proposed` (approve refuses it) and
        # expires with the slot; the ops row is published without the sealed document
        _publish_ops_only(ctx, rec)
        ledger.record_cycle(rec)
    elif decision_id is not None:
        risk_up = plan is not None and plan.risk_increasing
        if published or not risk_up:
            ledger.transition(decision_id, "proposed", "sealed and published" if published else
                              "risk-reducing: publication not required")
            state = "proposed"
            if sha:
                ledger.set_published_commit(decision_id, sha)
            # evidence is consumed when a proposal issues, only by the lines that could act on it
            ledger.set_material_fingerprints(consume_fingerprints(
                fps, ledger.get_material_fingerprints(),
                evidence_lines(policy, pack, engine_blockers(ctx, corporate_blockers))))
            _notify_proposal(ctx, rec, plan, urgent=kill_state in ("HALTED", "FLAT"),
                             valid_until=valid_until)
        else:
            rec.flags.append("publish_failed_risk_increasing_held")
    ledger.set_runtime("last_cycle", {"cycle_id": cycle_id, "at": ctx.clock().isoformat()})
    return CycleOutcome(cycle_id=cycle_id, status=rec.status, basis=decision.basis, decision_id=decision_id,
                        decision_state=state, legs=len(plan.legs) if plan else 0, commit_sha=sha,
                        published=published, flags=rec.flags)


def _skip_broker(ctx: CycleContext, info: clock.SlotInfo, now: datetime, flags: list[str],
                 code: str) -> CycleOutcome:
    """M5-A: the broker is required but unreadable. The cycle ends `skipped_broker` with a fixed
    code flag, publishes only its ops row (the public status is left untouched, so it never flips to
    AWAITING_ACCOUNT), and sends one URGENT per code per 4 h. No deliberation, no plan, no order."""
    rec = _bare_record(ctx, info, now, status="skipped_broker")
    rec.flags = [*rec.flags, *flags, code]      # keep runner:launchd from _bare_record
    rec.decision_state = "reviewed_no_action"
    rec.finished_at = ctx.clock()
    ctx.ledger.record_cycle(rec)
    published, sha = _publish_ops_only(ctx, rec)
    urgent.send_once(ctx, code, f"council {info.cycle_id}",
                     f"cycle skipped: {code}. Check the Keychain and the READ token "
                     "(council doctor --live-read in the operator terminal).", now)
    return CycleOutcome(cycle_id=info.cycle_id, status="skipped_broker", published=published,
                        commit_sha=sha, flags=rec.flags)


def _settle_held_orders(ctx: CycleContext, now: datetime) -> None:
    """Resolve orders the broker holds for a closed market (the watch's read-only lookups) BEFORE
    the snapshot: a hold that filled since the last watch run would otherwise be counted twice, in
    the snapshot and in ledger.pending_open_weights. A failure only leaves them waiting (the
    engine then counts them as held, the conservative side)."""
    from council import watch

    try:
        alerts = watch._resolve_waiting(ctx, now)
    except Exception as exc:  # never stops the cycle
        alerts = [f"waiting_check_error:{type(exc).__name__}"]
    for alert in alerts:
        watch._alert(ctx, alert)


CORPORATE_CHECK_FAILED = "corporate_action_check_failed"
STOCK_SIGMA_4H_KEY = "stock_sigma_4h"     # ledger runtime, PRIVATE: {stock line: 4-hour sigma}
US_SESSION_HOURS = 6.5


def stock_sigma_4h(policy: Any, states: Any) -> dict[str, float]:
    """{stock line: its 4-hour sigma, relative}: the daily sigma (`risk.stops.sigma_daily_of`)
    scaled by sqrt(4 / 6.5), four of the US session's six and a half hours (design §3.6's band for a
    vanished stock position). A line without volatility is left out: the watch then counts a vanished
    position on it as a stop hit (ambiguous -> conservative)."""
    import math

    from council.risk.stops import sigma_daily_of

    out: dict[str, float] = {}
    for line in policy.universe.stock_lines():
        state = states.get(line.symbol)
        sigma = sigma_daily_of(state, line.asset_class) if state is not None else None
        if sigma is not None and math.isfinite(sigma) and sigma > 0:
            out[line.symbol] = round(float(sigma) * math.sqrt(4.0 / US_SESSION_HOURS), 6)
    return out


def record_stock_sigma_4h(ledger: Any, policy: Any, states: Any) -> list[str]:
    """Store `stock_sigma_4h` for the watch (only when the policy has stock lines). Never raises: a
    failure costs only the watch's stop-hit band (without a stored sigma a vanished stock position
    counts as a stop hit, the conservative reading) and returns the flag
    `stock_sigma_record_error:<type>` for the cycle record."""
    try:
        if policy.universe.stock_lines():
            ledger.set_runtime(STOCK_SIGMA_4H_KEY, stock_sigma_4h(policy, states))
    except Exception as exc:  # stock-only bookkeeping: its failure never stops the core's cycle
        return [f"stock_sigma_record_error:{type(exc).__name__}"]
    return []


def corporate_actions(ctx: CycleContext, snapshot: Any, cycle_id: str) -> list[str]:
    """Design §3.6 detection at the cycle start, on the broker snapshot: positions no line owns that
    no open leg of ours created (pending corporate actions) and positions on retired vehicles. Returns
    their satellite-scoped R20 blockers (codes only, no identifier: blocker strings reach the public
    record) and sends each URGENT alert (which names the instrument and the command) through the
    notifier. Runs only when the policy has a stock sleeve. A check that fails holds the satellite
    (`satellite:corporate_action_check_failed`) and alerts; it never stops the cycle."""
    from council.ledger.states import SATELLITE_BLOCKER_PREFIX
    from council.stocks import corporate

    if not corporate.sleeve_active(ctx.policy):
        return []
    try:
        found = corporate.detect(list(snapshot.positions), ctx.policy,
                                 opened=corporate.opened_position_ids(ctx.ledger))
    except Exception as exc:  # fail closed: hold the satellite, keep the core cycle
        _urgent(ctx, cycle_id, f"URGENT {CORPORATE_CHECK_FAILED}: the corporate-action check failed "
                               f"({type(exc).__name__}); the stock sleeve is held. Run `council stocks status`")
        return [f"{SATELLITE_BLOCKER_PREFIX}{CORPORATE_CHECK_FAILED}"]
    for alert in found.alerts:
        _urgent(ctx, cycle_id, alert, fallback="URGENT corporate action pending: the stock sleeve is held. "
                                               "Run `council stocks status`")
    return list(found.blockers)


def _urgent(ctx: CycleContext, cycle_id: str, message: str, *, fallback: str | None = None) -> None:
    """One URGENT operator alert (private ntfy; never published). The notifier refuses a message its
    leak scan flags (e.g. a 7+ digit instrument id): then the fixed `fallback` text goes instead.
    Never raises."""
    if ctx.notifier is None:
        return
    body = message.removeprefix("URGENT ").strip()
    for text in (body, (fallback or "").removeprefix("URGENT ").strip()):
        if not text:
            continue
        try:
            ctx.notifier.send(f"council {cycle_id}", text, priority="urgent")
            return
        except Exception:  # try the fallback; an alert failure never stops the cycle
            continue


def core_lines(policy: Any) -> list[str]:
    """The lines whose common dates form the returns calendar: every line outside the satellite."""
    return [ln.symbol for ln in policy.universe.lines if ln.sleeve != "satellite"]


def _material(ledger: Any, pack: Any, cards: list, kill_state: str) -> tuple[dict[str, str], dict[str, bool]]:
    """(per-line fingerprints, {line: new material evidence since the last issued proposal}). A
    ledger that stored only the legacy single fingerprint answers once through it."""
    fps = material_fingerprints(pack, cards, kill_state)
    stored = ledger.get_material_fingerprints()
    legacy_equal = None
    if stored is None:
        legacy = ledger.get_runtime("last_material_fingerprint")
        if legacy:
            legacy_equal = material_fingerprint(pack, cards, kill_state) == legacy
    return fps, material_changes(fps, stored, legacy_equal=legacy_equal)


def evidence_lines(policy: Any, pack: Any, blockers: list[str]) -> list[str]:
    """The lines that could act on their material evidence this cycle, so a proposal issued now
    consumes it (`runtime.consume_fingerprints`): admitted by the pack (usable, fresh data and an
    open session), minus every line under a whole-book blocker, minus the satellite under a
    satellite-scoped one."""
    from council.ledger.states import SATELLITE_BLOCKER_PREFIX, SWING_BLOCKER_PREFIX

    blockers = [b for b in blockers if not str(b).startswith(SWING_BLOCKER_PREFIX)]  # swing entries only
    scoped = [str(b).startswith(SATELLITE_BLOCKER_PREFIX) for b in blockers]
    if not all(scoped):
        return []
    satellite_held = any(scoped)
    by_line = policy.universe.by_symbol()
    return [s for s in pack.admitted
            if not (satellite_held and s in by_line and by_line[s].sleeve == "satellite")]


def plan_instrument_ids(policy: Any, imap: Any, snapshot: Any) -> list[int]:
    """The instruments a plan reads eligibility and rates for (design §11.5): the vehicles of the
    current lines that the instrument map resolves, plus the instruments of held positions (unmapped
    and retired ones included, so a flatten can price them). Never the whole append-only map."""
    ids: set[int] = set()
    for line in policy.universe.lines:
        for vehicle in (*line.vehicles.long, *line.vehicles.short):
            iid = imap.get(vehicle.symbol)
            if iid is not None:
                ids.add(int(iid))
    for position in getattr(snapshot, "positions", None) or ():
        ids.add(int(position.instrument_id))
    return sorted(ids)


def without_held_closes(plan: Plan | None, ledger: Any) -> Plan | None:
    """A flatten gets no second close for a position whose full close the broker already holds
    for a closed market (waiting_for_market): the held close fills when the market opens."""
    from council.ledger.states import WAITING_STATE

    if plan is None:
        return None
    held = {r.position_id for r in ledger.legs_in_states([WAITING_STATE])
            if r.kind == "close" and r.position_id is not None}
    if not held:
        return plan
    legs = [leg for leg in plan.legs
            if not (leg.kind in ("close", "partial_close") and leg.position_id in held)]
    return plan.model_copy(update={"legs": legs})


def stamp_sessions(plan: Plan, universe: Universe, *, asof: datetime) -> Plan:
    """Each leg with its vehicle's trading session and its own deadline, min(next slot - 5 min,
    session close - 10 min) from `asof` (the cycle's slot; the watch passes the time it makes a
    standing flatten). A leg on a line the policy does not know (an unmapped position) gets no
    session and the slot deadline: the broker decides, and status 11 is handled."""
    by_line = universe.by_symbol()
    legs = []
    for leg in plan.legs:
        spec = by_line.get(leg.line or "")
        session = clock.vehicle_session(spec, leg.symbol) if spec is not None else None
        legs.append(leg.model_copy(update={"session": session,
                                           "valid_until": clock.leg_valid_until(session, asof)}))
    return plan.model_copy(update={"legs": legs})


def decision_valid_until(plan: Plan | None, kind: str, slot: datetime) -> datetime:
    """A rebalance is approvable while any leg is (its latest leg deadline, never past the next
    slot - 5 min); flatten and compliance are never clipped below the next slot - 5 min."""
    limit = clock.proposal_valid_until(slot)
    if kind != "rebalance" or plan is None:
        return limit
    latest = plan.latest_valid_until()
    return limit if latest is None else min(limit, latest)


def q_lev_class(line: LineSpec, quote: Any) -> str:
    from council.runtime import vehicle_asset_class

    return vehicle_asset_class(line, quote.settlement)


def _discretionary(decision: RiskDecision) -> bool:
    return decision.basis in ("council", "council_partial_reference")


def _manifest_sha(ctx: CycleContext) -> str:
    try:
        return ctx.registry.manifest_sha()
    except Exception:
        return ""


async def _digest(ctx: CycleContext) -> str:
    fn = getattr(ctx.gateway, "model_digest", None)
    if fn is None:
        return ""
    try:
        return await fn()
    except Exception:
        return ""


def _cost_facts(quotes: dict, slot: datetime) -> list:
    try:
        from council.facts.pack import cost_facts_from_quotes
    except ImportError:  # older pack module
        return []
    per_line = {line: q for (line, direction, lev), q in quotes.items() if direction == "long" and lev == 1}
    return cost_facts_from_quotes(per_line, slot=slot)


def _current_levels(snapshot: Any, unit: dict[str, float]) -> dict[str, float]:
    if snapshot is None:
        return {s: 0.0 for s in unit}
    from council.risk.exposure import levels_from_weights

    safe_unit = {s: (u if u > 0 else 1e-9) for s, u in unit.items()}
    levels = levels_from_weights(snapshot.signed_w, safe_unit)
    return {s: levels.get(s, 0.0) for s in unit}


# ------------------------------------------------------------------------------- snapshot
def _snapshot_and_kill(ctx: CycleContext, now: datetime) -> tuple[Any, str, Any]:
    from council.broker.instruments import InstrumentMap
    from council.execution.planner import vehicle_to_line
    from council.risk import killswitch
    from council.risk.exposure import snapshot_from_pnl
    from council.risk.nav import NavState, update_nav

    ledger, policy = ctx.ledger, ctx.policy
    payload = ctx.sources.broker.pnl()  # type: ignore[union-attr]
    imap = InstrumentMap.load(ctx.state_dir / "instruments.json")
    from council.swing.book import swing_vehicle_map

    smap = swing_vehicle_map(ledger, policy)      # swing positions are swing lines, not UNMAPPED (§1.9)
    snapshot = snapshot_from_pnl(payload, vehicle_by_instrument=smap.merged_symbols(imap.symbols_by_id()),
                                 line_by_vehicle=smap.merged_lines(vehicle_to_line(policy.universe)), now=now)
    # M-3 (M5-D2): while a smoke ticket is pending, in flight or holds a position, the NAV state and
    # the lifetime peaks are read but never initialised or raised; after the token-day tickets they
    # restart once, at the first smoke-free read before any live proposal
    smoke = ledger.smoke_active([p.position_id for p in snapshot.positions])
    reset = not smoke and ledger.smoke_baseline_due()
    raw_nav = None if reset else ledger.get_runtime("nav_state")
    nav = update_nav(NavState.model_validate(raw_nav) if raw_nav else None, snapshot.equity_usd, now)
    if not smoke:
        ledger.set_runtime("nav_state", nav.model_dump(mode="json"))
    if reset:
        ledger.set_runtime(SMOKE_BASELINE_RESET_KEY, now.isoformat())
    reads = [(datetime.fromisoformat(t), float(e)) for t, e in ledger.get_runtime("equity_reads", [])]
    reads = [r for r in reads if now - r[0] <= timedelta(hours=6)][-20:] + [(now, snapshot.equity_usd)]
    ledger.set_runtime("equity_reads", [[t.isoformat(), e] for t, e in reads])
    prev = ledger.get_runtime("kill_state", "NORMAL")
    real_peak = None if reset else ledger.get_runtime(REAL_PEAK_KEY)
    kd = killswitch.evaluate(nav=nav, equity_reads=reads, prev_state=prev,
                             has_positions=bool(snapshot.positions), policy=policy,
                             real_drag=ledger.real_fee_drag(),       # D19: the real account's fee drag
                             real_peak=float(real_peak) if isinstance(real_peak, int | float) else None)
    if kd.real_peak is not None and not smoke:   # PRIVATE: lifetime peak of the real-adjusted equity
        ledger.set_runtime(REAL_PEAK_KEY, kd.real_peak)
    ledger.set_runtime("kill_state", kd.state)
    ledger.add_equity_mark(now, snapshot.equity_usd, credit_usd=snapshot.credit_usd,
                           source="smoke" if smoke else "cycle")
    return snapshot, kd.state, nav


# -------------------------------------------------------------------------------- council
async def _council(ctx: CycleContext, rec: CycleRecord, *, pack, ref, bands, bands_fn, code_cards,
                   current_levels, quotes, run_macro, kill_state, ref_levels, now, started,
                   sink=None) -> tuple[str, dict[str, float], dict[str, str]]:
    from council.deliberation.council import run_council
    from council.risk.authority import enforce_authority

    policy = ctx.policy
    if kill_state in ("HALTED", "FLAT"):
        rec.flags.append("council_skipped_halted")
        return "halted", {s: 0.0 for s in ref_levels}, {}
    movable = [s for s in pack.admitted
               if policy.universe.by_symbol()[s].council_deviations and bands.get(s)
               and bands[s].hi - bands[s].lo > 1e-9]
    if not movable:
        rec.flags.append("council_skipped_no_movable_lines")
        levels, _ = enforce_authority(dict(ref_levels), bands)
        return "code_only", levels, {}
    remaining = max(30.0, ctx.budget_s - (time.monotonic() - started) - 120.0)
    call_log: list = []     # filled stage by stage, so a council timeout keeps the calls that ran
    try:
        result = await asyncio.wait_for(
            run_council(gw=ctx.gateway, reg=ctx.registry, pack=pack, ref=ref, bands=bands,
                        current_levels=current_levels, cost_hints=cost_hints(quotes),
                        lines=list(policy.universe.lines), policy=policy, enforce=enforce_authority,
                        now=pack.slot, run_single_agent=ctx.run_single_agent, run_macro=run_macro,
                        **_council_extras(code_cards, bands_fn, call_log, sink)),
            timeout=remaining)
    except TimeoutError:
        rec.flags.append("council_timeout")
        rec.calls = list(call_log)
        levels, _ = enforce_authority(dict(ref_levels), bands)
        return "council_unavailable", levels, {}
    rec.cards = list(result.cards)
    rec.macro = result.macro
    rec.debate = result.debate or Debate()
    rec.pm = list(result.pm)
    rec.medoid_replicate = result.aggregate.medoid_index
    rec.agreement = dict(result.aggregate.agreement)
    rec.single_agent = list(result.single_agent)
    if result.single_agent_aggregate is not None:
        rec.single_agent_levels = dict(result.single_agent_aggregate.levels)
    rec.calls = list(result.calls)
    rec.dropped_cards = list(result.dropped)
    rec.drops = list(getattr(result, "drops", None) or [])           # the trail's structured drops
    rec.fallback_lines = list(result.aggregate.per_line_fallback)    # medoid lines back at the reference
    rec.desk_sha = result.desk_sha
    rec.flags += list(result.flags)
    if getattr(result, "bands", None):
        rec.bands = dict(result.bands)
    if result.enforce_notes:
        rec.extras["enforce_notes"] = result.enforce_notes
    raw = dict(getattr(result, "raw", {}) or {})   # PRIVATE: written gzipped to state_dir only
    return result.basis, dict(result.aggregate.levels), raw


def _council_extras(code_cards, bands_fn, call_log: list | None = None, sink: Any = None) -> dict[str, Any]:
    """Pass the new run_council parameters only when the installed council supports them."""
    import inspect

    from council.deliberation.council import run_council

    params = inspect.signature(run_council).parameters
    out: dict[str, Any] = {}
    if "code_cards" in params:
        out["code_cards"] = list(code_cards)
    if "bands_fn" in params:
        out["bands_fn"] = bands_fn
    if "call_log" in params and call_log is not None:
        out["call_log"] = call_log
    if "input_sink" in params and sink is not None:
        out["input_sink"] = sink
    return out


# ------------------------------------------------------------------------------- risk
def _evaluate(ctx: CycleContext, rec: CycleRecord, *, levels, ref_levels, bands, states, snapshot, unit,
              kill_state, quotes, pack, material_changed, basis, slot, returns, nav,
              econ=None, extra_blockers=(), extra_lines=(), broker_min_share=None) -> RiskDecision:
    from council.risk.engine import RiskEngine
    from council.risk.held_levels import ledger_held_levels

    ledger, policy = ctx.ledger, ctx.policy
    last_change = ledger.last_changes()
    # R13 turnover and the R14 30-day budget count discretionary legs only (legacy legs count)
    disc = {"kinds": ("rebalance",), "origins": ("discretionary",)}
    turnover_7d = ledger.turnover_since(slot - timedelta(days=7), **disc)
    turnover_30d = ledger.turnover_since(slot - timedelta(days=30), **disc)
    cost_30d = ledger.cost_bps_since(slot - timedelta(days=30), **disc)
    fee_30d = ledger.fee_bps_since(slot - timedelta(days=30), **disc)
    stop_hits = ledger.stop_hits_since(slot - timedelta(days=14), universe=policy.universe)
    pending = ledger.pending_open_weights()      # orders the broker holds until their market opens
    held = ledger_held_levels(ledger, policy.universe.lines,
                              current_w=snapshot.signed_w if snapshot is not None else None,
                              units=unit, now=slot, persist=snapshot is not None)
    vol_fn = _vol_fn(policy, returns, states)
    return RiskEngine(policy).evaluate(
        levels=levels, ref=ref_levels, bands=bands, states=states, snapshot=snapshot,
        unit_weights=unit, kill_state=kill_state, cost_quotes=engine_quotes(quotes), events=pack.events,
        last_change=last_change, turnover_7d=float(turnover_7d), material_changed=material_changed,
        basis=basis, now=slot, ex_ante_vol_fn=vol_fn,
        turnover_30d=float(turnover_30d), cost_30d_bps=float(cost_30d),
        stop_hits=stop_hits, blockers=engine_blockers(ctx, extra_blockers),
        nav_drawdown=nav.drawdown if nav is not None else None,
        pending_w=pending, held_levels=held,
        copy_min_share=econ.copy_floor_share if econ is not None else 0.0,
        broker_min_share=broker_min_share or None,
        cost_30d_fee_bps=float(fee_30d),
        extra_lines=extra_lines,          # swing-book §1.1: the pinned swing lines (none until run_swing)
    )


def _eligibility_rows(ctx: CycleContext, snapshot: Any, flags: list[str]) -> list[Any] | None:
    """The broker's eligibility rows for the plan's instruments, read once per cycle (connected
    account only). A failed read leaves None: the plan reads them again and fails closed there."""
    from council.broker.instruments import InstrumentMap

    broker = ctx.sources.broker
    if snapshot is None or broker is None:
        return None
    try:
        imap = InstrumentMap.load(ctx.state_dir / "instruments.json")
        return list(broker.eligibility(instrument_ids=plan_instrument_ids(ctx.policy, imap, snapshot)))
    except Exception as exc:  # the engine then runs without the broker minimum; the plan re-reads
        flags.append(f"broker_minimum_unread:{type(exc).__name__}")
        return None


def broker_min_shares(policy: Any, rows: Iterable[Any] | None, nav_usd: float | None) -> dict[str, float]:
    """PRIVATE: each line's broker minimum position exposure as a NAV share, the lowest over the
    line's vehicles the broker returned (M5-N; never published or prompted)."""
    if not rows or not nav_usd or nav_usd <= 0:
        return {}
    by_symbol = {r.symbol: r for r in rows}
    out: dict[str, float] = {}
    for line in policy.universe.lines:
        mins = [float(getattr(by_symbol[v.symbol], "min_position_exposure", 0.0) or 0.0)
                for v in (*line.vehicles.long, *line.vehicles.short) if v.symbol in by_symbol]
        if mins and min(mins) > 0:
            out[line.symbol] = min(mins) / float(nav_usd)
    return out


def size_floor_binding_flags(policy: Any, broker_min: Mapping[str, float], copy_min_share: float) -> list[str]:
    """The private P2 flags: `size_floor_binding:<line>` for each line whose size floor (the real
    trade floor or the broker minimum) exceeds the public deadband share at the current NAV. They
    bound the NAV, so `redact.public_flags` drops them."""
    threshold = float(policy.risk["deadband"]["min_nav_share"])
    lines = broker_min.keys() if broker_min else ()
    if copy_min_share > threshold + 1e-12:
        lines = policy.universe.symbols()
    return [f"size_floor_binding:{s}" for s in sorted(lines)
            if max(copy_min_share, float(broker_min.get(s, 0.0))) > threshold + 1e-12]


def engine_blockers(ctx: CycleContext, extra: Iterable[str] = ()) -> list[str]:
    """R20 inputs: the ledger's blockers, those found while loading policy (e.g.
    `sleeve_policy_untagged`, `stock_eligibility_unchecked`) and `extra` (the cycle start's
    corporate-action blockers, already "satellite:<code>"), without repeats. A satellite-scoped one is
    passed as "satellite:<code>" and holds only the stock sleeve; anything else holds every line."""
    from council.ledger.states import SATELLITE_BLOCKER_PREFIX

    out = list(ctx.ledger.blockers())
    for blocker in getattr(ctx, "policy_blockers", ()) or ():
        code = str(getattr(blocker, "code", blocker))
        satellite = getattr(blocker, "scope", "all") == "satellite"
        out.append(f"{SATELLITE_BLOCKER_PREFIX}{code}" if satellite else code)
    out += [str(b) for b in extra]
    return list(dict.fromkeys(out))


def _vol_fn(policy, returns, states):
    try:
        from council.reference.book import book_covariance, vol_fn
    except ImportError:
        return None
    cov = book_covariance(returns, list(policy.universe.lines), states, policy)
    return vol_fn(cov)


# ------------------------------------------------------------------------------- plan
def _plan(ctx: CycleContext, decision: RiskDecision, *, snapshot, states, kill_state,
          ref_levels=None, unit=None, econ=None, history=None, caps=None, rows=None, swing_orders=None):
    from council.broker.eligibility import resolve_vehicle
    from council.broker.instruments import InstrumentMap
    from council.broker.parsing import parse_rates
    from council.execution.planner import build_plan
    from council.risk.costs import carry_bps_day, fee_applies, per_side_bps
    from council.risk.engine import leg_origins
    from council.risk.stops import catastrophe_stop_distance
    from council.runtime import vehicle_asset_class

    policy, broker = ctx.policy, ctx.sources.broker
    by_line = policy.universe.by_symbol()
    imap = InstrumentMap.load(ctx.state_dir / "instruments.json")
    ids = plan_instrument_ids(policy, imap, snapshot)
    orders = list(swing_orders or [])
    swing_ids = {int(o.instrument_id) for o in orders if o.instrument_id is not None}
    ids = sorted(set(ids) | swing_ids)
    if rows is None:
        rows = broker.eligibility(instrument_ids=ids)  # type: ignore[union-attr]
    else:
        missing = sorted(swing_ids - {r.instrument_id for r in rows})
        if missing:                                  # swing lines the core's eligibility read did not cover
            rows = [*rows, *broker.eligibility(instrument_ids=missing)]  # type: ignore[union-attr]
    rows_by_symbol = {r.symbol: r for r in rows}
    quotes = parse_rates(broker.rates(ids), imap.symbol_for)  # type: ignore[union-attr]
    nav_usd = snapshot.equity_usd

    if kill_state in ("HALTED", "FLAT"):
        from council.execution.planner import build_flatten_plan

        flatten = build_flatten_plan(snapshot=snapshot, quotes=quotes, eligibility=rows_by_symbol,
                                     nav_usd=nav_usd, policy=policy, symbol_for=imap.symbol_for)
        return without_held_closes(flatten, ctx.ledger)

    def expected_cost(vehicle, row, config):
        cls = vehicle_asset_class(by_line[_line_of(policy, vehicle.symbol)], vehicle.settlement)
        side = per_side_bps(vehicle.settlement, cls, None, None, policy)
        lev = max(config.leverage_values) if config.leverage_values else 1
        return 2 * side + 20 * carry_bps_day(config.direction, vehicle.settlement, lev, cls, None, policy)

    def vehicle_for(line: str, direction: str, leverage: int):
        return resolve_vehicle(by_line[line], direction, leverage, rows_by_symbol, expected_cost,
                               capabilities=caps.verified if caps is not None else None)

    settlement_of = {v.symbol: v.settlement for ln in by_line.values()
                     for v in (*ln.vehicles.long, *ln.vehicles.short)}

    fee = econ.fee_nav_bps if econ is not None else 0.0

    swing_cost = _swing_leg_cost(policy, fee)

    def cost_bps(line: str, vehicle, direction: str, leverage: int):
        if line not in by_line and swing_cost is not None and line.startswith("SW_"):
            return swing_cost(direction)            # swing legs: swing/costs.py floors, never zero
        symbol = vehicle if isinstance(vehicle, str) else vehicle.symbol
        settlement = settlement_of.get(symbol, "cfd") if leverage == 1 and direction == "long" else "cfd"
        cls = vehicle_asset_class(by_line[line], settlement)
        return (per_side_bps(settlement, cls, None, None, policy),
                carry_bps_day(direction, settlement, leverage, cls, None, policy),
                fee if fee_applies(settlement, cls) else 0.0)     # PRIVATE fixed fee, bps of NAV

    changed = changed_lines(decision)
    target = {s: decision.final_w.get(s, 0.0) for s in changed}
    stop = {s: catastrophe_stop_distance(states[s], by_line[s], policy) for s in changed if s in states}
    # leverage 2 only for the leverage extension (|level| > 1, cost-gated in the bands)
    lev = {s: (2 if abs(decision.banded_levels.get(s, 0.0)) > 1.0 + 1e-9 else 1) for s in changed}
    levels = dict(ref_levels or {})
    ref_w = {s: float(levels.get(s, 0.0)) * float((unit or {}).get(s, 0.0)) for s in by_line}
    origin = leg_origins(decision.base_w, decision.final_w, ref_w) if ref_levels is not None else None
    return build_plan(snapshot=snapshot, target_w=target, vehicle_for=vehicle_for, quotes=quotes,
                      stop_distance=stop, leverage_for=lev, eligibility=rows_by_symbol,
                      cost_bps=cost_bps, nav_usd=nav_usd, policy=policy, origin=origin,
                      ref_levels=levels or None, economics=econ,
                      gap_ref=gap_references(policy, changed, states, history), capabilities=caps,
                      swing=orders or None, swing_map=_swing_map(ctx) if orders else None)


def _swing_map(ctx: CycleContext) -> Any:
    from council.swing.book import swing_vehicle_map

    return swing_vehicle_map(ctx.ledger, ctx.policy)


def _swing_leg_cost(policy: Any, fee_nav_bps: float) -> Any:
    """The planner's cost callback for a swing line (`swing.costs`): (per-side bps of the position's
    NAV share, carry bps/day, the PRIVATE fixed fee in bps of NAV). A long is real shares (the fee
    applies, no carry); a short is a 1x stock CFD (the short carry floor, no fixed fee). None when the
    cost config is unreadable (the planner must then not price a swing leg at zero: see the report)."""
    try:
        from council.swing.costs import CostConfig

        cfg = CostConfig.from_policy(policy)
    except Exception:  # noqa: BLE001
        return None

    def cost(direction: str) -> tuple[float, float, float]:
        if direction == "long":
            return cfg.spread_floor_bps, 0.0, fee_nav_bps
        return cfg.spread_floor_bps, cfg.short_carry_bps_day_floor, 0.0

    return cost


def _capabilities(ctx: CycleContext, now: datetime):
    """The M5-D1 gates from the private state dir; an unproven record alerts once (URGENT)."""
    from council.operator import capabilities

    caps = capabilities.load(ctx.state_dir)
    capabilities.alert_unproven(ctx, caps, now)
    return caps


def gap_references(policy, lines, states, history) -> dict[str, tuple[float, float] | None]:
    """D20 inputs for the planner's gap guard: for each STOCK line among `lines`, the pack's last
    completed close and its daily sigma; None when either is missing (the open is then skipped)."""
    from council.risk.stops import sigma_daily_of

    by_line = policy.universe.by_symbol()
    out: dict[str, tuple[float, float] | None] = {}
    for s in lines:
        spec = by_line.get(s)
        if spec is None or spec.asset_class != "stock":
            continue
        frame = (history or {}).get(s)
        state = states.get(s)
        last = None
        if frame is not None and "close" in frame and not frame["close"].dropna().empty:
            last = float(frame["close"].dropna().iloc[-1])
        sigma = sigma_daily_of(state, spec.asset_class) if state is not None else None
        out[s] = (last, float(sigma)) if last is not None and sigma is not None else None
    return out


def _line_of(policy, vehicle_symbol: str) -> str:
    from council.execution.planner import vehicle_to_line

    return vehicle_to_line(policy.universe)[vehicle_symbol]


# ------------------------------------------------------------------------------ news
def gather_news(ctx: CycleContext, slot: datetime, flags: list[str]) -> tuple[list[Any], dict[str, Any]]:
    """The cycle's news items and the private fetch report. `ctx.sources.news(slot)` returns a
    `NewsFetch` (items, flags, per-source reports; `council.context.news_sources`) or, from an older
    or test source, a bare list. Its flags are appended to `flags` (the caller keeps them out of the
    fact pack: a skew drop depends on an item dated after the slot). Never raises: a source function
    that raises becomes `news_source_error:sources:<type>` and the cycle runs without news."""
    if ctx.sources.news is None:
        return [], {}
    try:
        fetched = ctx.sources.news(slot)
    except Exception as exc:  # news never stops the cycle
        flags.append(f"news_source_error:sources:{type(exc).__name__}")
        return [], {}
    if isinstance(fetched, list | tuple):
        return list(fetched), {}
    flags += [f for f in getattr(fetched, "flags", ()) if f not in flags]
    reports = {name: rep.model_dump(mode="json") if hasattr(rep, "model_dump") else dict(rep)
               for name, rep in (getattr(fetched, "sources", None) or {}).items()}
    return list(getattr(fetched, "items", ()) or ()), {"sources": reports}


def news_record(pack: Any, extras: dict[str, Any]) -> dict[str, Any]:
    """PRIVATE (the ledger's `extras["news_fetch"]`): what each source did (counts, error codes) and,
    for every PUBLIC-DOMAIN item the pack admitted, its source, link, times, form and item codes, so
    the operator's reading list can show where an item came from. Broker feed items are only
    counted: the ledger never holds feed text or feed metadata (eToro Licensed Content)."""
    public, broker = [], 0
    for item in pack.news:
        if item.source == "etoro_feed" or not str(item.id).startswith("P:"):
            broker += 1
            continue
        row: dict[str, Any] = {"id": item.id, "source": item.source, "licence": item.licence,
                               "published_at": item.published_at.isoformat(),
                               "available_at": item.available_at.isoformat()}
        if item.link:
            row["link"] = item.link
        if item.form:
            row["form"], row["items"] = item.form, list(item.items)
        public.append(row)
    return {"sources": dict(extras.get("sources") or {}), "public_items": public, "broker_items": broker}


# ------------------------------------------------------------------- the decision trail
def record_trail(rec: CycleRecord, pack: Any, *, code_bands: dict[str, Band],
                 material_changed: dict[str, bool] | None, lines: list[LineSpec]) -> list[str]:
    """Keep, in the private record, what the per-line decision trail needs and the record did not
    hold (transparency-v2 §4, T5a): the bands before the analysts' cards where they differ from the
    final bands, the lines with new material evidence, the claim-to-line tags, the publishable
    values of the evidence ids the trail cites (`publish.trail.evidence_values`: never a licensed or
    broker-derived value), the lines whose R15 value may be shown, and a summary of the trail itself
    (`extras["trail"]`: outcome, where it stopped, who asked, per line). `council why` rebuilds the
    full trail from the record and the ledger's later decision and execution state. Never raises:
    a failure is the flag `trail_record_error:<type>`."""
    try:
        from council.deliberation.debate import claim_lines, pack_item_lines
        from council.publish import trail

        final = rec.bands or code_bands
        rec.code_bands = {s: b for s, b in code_bands.items()
                          if s in final and (abs(b.lo - final[s].lo) > 1e-9 or abs(b.hi - final[s].hi) > 1e-9)}
        rec.material_lines = sorted(s for s, changed in (material_changed or {}).items() if changed)
        rec.claim_lines = claim_lines(rec.debate, lines={ln.symbol for ln in lines},
                                      item_lines=pack_item_lines(pack),
                                      card_scope={c.card_id: list(c.scope) for c in rec.cards})
        rec.evidence_values = trail.evidence_values(pack, lines, trail.cited_ids(rec))
        rec.value_lines = trail.value_lines(pack, lines)
        rec.extras["trail"] = trail.summary(trail.record_trails(rec))
    except Exception as exc:  # the trail is a record of the decision, never a condition for it
        return [f"trail_record_error:{type(exc).__name__}"]
    return []


# ----------------------------------------------------------------------- private capture
def daily_purge(ctx: CycleContext, now: datetime) -> list[str]:
    """The once-a-day purge of licensed private copies (first cycle of each UTC day; transparency
    T1v, `council.operator.purge.maybe_daily_purge`). Never raises; returns flags."""
    try:
        from council.operator.purge import maybe_daily_purge

        return list(maybe_daily_purge(ctx.state_dir, now))
    except Exception as exc:  # best effort: the cycle goes on
        return [f"purge_error:{type(exc).__name__}"]


def _input_sink() -> Any:
    """A fresh private input sink for the council (None when capture cannot even start)."""
    try:
        from council.deliberation.capture import InputSink

        return InputSink()
    except Exception:  # capture never stops a cycle
        return None


def _write_calls(ctx: CycleContext, cycle_id: str, raw: dict[str, str], sink: Any) -> list[str]:
    """Write the private capture of every model call (`state_dir/calls/`, licensed item texts in
    `state_dir/licensed/calls/`) and, for one release, the old raw transcript. Returns flags
    (`inputs_capture_error:<type>`); never raises."""
    flags: list[str] = []
    if sink is not None and (sink.calls or sink.flags):
        try:
            from council.deliberation.capture import write_cycle_inputs

            flags += write_cycle_inputs(ctx.state_dir, sink, cycle_id=cycle_id, captured_at=ctx.clock())
        except Exception as exc:
            flags.append(f"inputs_capture_error:{type(exc).__name__}")
    try:
        _write_transcript(ctx, cycle_id, raw)
    except Exception as exc:  # the raw transcript is a private convenience
        flags.append(f"transcript_error:{type(exc).__name__}")
    return list(dict.fromkeys(flags))


# ----------------------------------------------------------------------------- publish
def _write_transcript(ctx: CycleContext, cycle_id: str, raw: dict[str, str]) -> None:
    """Private, gzipped: raw model output never reaches the ledger record, journal/ or the site."""
    from council.deliberation.capture import write_private

    path = ctx.state_dir / "transcripts" / f"{cycle_id}.json.gz"
    write_private(ctx.state_dir, path, gzip.compress(json.dumps({"cycle_id": cycle_id, "raw": raw}).encode(),
                                                     mtime=0))


NAV_CANARY_MIN = 10_000.0     # smaller figures collide with years, slot times and hex digests


def publish_canaries(ctx: CycleContext, snapshot: Any) -> list[str | float]:
    """Private values the final leak scan refuses in any published byte: the context's canaries,
    `COUNCIL_LEAK_CANARIES`, and the NAV and mirror figures (snapshot equity, real funding, virtual
    NAV) of at least NAV_CANARY_MIN. Smaller figures are left to the redaction layer: as 4-digit
    whole numbers they would match years, slot times (1440) and digest hex, and block every publish."""
    import math

    from council.publish.leakscan import env_canaries

    values: list[Any] = [getattr(snapshot, "equity_usd", None)]
    try:
        from council.operator.mirror import load_mirror

        mirror = load_mirror(ctx.state_dir)
        if mirror is not None:
            values += [mirror.funding_usd, mirror.virtual_nav_usd]
    except Exception:  # no mirror file (or unreadable): only the other canaries
        pass
    out: list[str | float] = [*ctx.canaries, *env_canaries()]
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= NAV_CANARY_MIN:
            out.append(round(number, 2))
    return out


def arm_leak_scan(publisher: Any, *, licensed: list[str], canaries: list[str | float]) -> None:
    """Give the publisher's final (git-level) leak scan this cycle's licensed texts (the pack's
    broker feed items) and private canaries, on top of whatever it was built with."""
    base = getattr(publisher, "_council_base_scan", None)
    if base is None:
        base = (list(getattr(publisher, "canaries", None) or []),
                list(getattr(publisher, "licensed_texts", None) or []))
        try:
            publisher._council_base_scan = base
        except (AttributeError, TypeError):  # a publisher that refuses new attributes keeps its lists
            return
    publisher.canaries = [*base[0], *canaries]
    publisher.licensed_texts = [*base[1], *licensed]


def cycle_install_key(ctx: CycleContext, rec: CycleRecord) -> bytes:
    """The private install key (keys the material-change fingerprint). If it cannot be loaded or
    created, a random key for this cycle only (flag `install_key_error:<type>`): the fingerprint then
    matches no other cycle's, which is safe; an unkeyed one could be brute-forced."""
    try:
        from council.publish import install_key

        return install_key.load_or_create(ctx.state_dir)
    except Exception as exc:
        import secrets

        rec.flags.append(f"install_key_error:{type(exc).__name__}")
        return secrets.token_bytes(32)


def _seal_and_publish(ctx: CycleContext, rec: CycleRecord, pack, *, reveal_now: bool, snapshot,
                      smoke_open: bool = False) -> tuple[bool, str | None]:
    """Seal the public cycle document, store the exact sealed bytes + salt privately, and publish
    the commitment (plus the reveal when there is nothing to hide, i.e. no proposal). While a smoke
    ticket is active (`smoke_open`) no status or book file is published: they would carry the smoke
    position's weight (M-2)."""
    from council.publish import commit_reveal, journal, redact

    if ctx.publisher is None:
        return False, None
    lines = ctx.policy.universe
    try:
        public = redact.public_cycle(rec, pack, lines=lines, install_key=cycle_install_key(ctx, rec),
                                     **swing_seal_inputs(ctx, rec))
    except Exception as exc:  # V11: never a crash; no commitment, so the decision cannot be approved
        code = f"{REDACT_ERROR}:{type(exc).__name__}"
        rec.flags.append(code)
        urgent.send_once(ctx, REDACT_ERROR, f"council {rec.cycle_id}",
                         f"cycle not sealed ({code}); its decision cannot be approved.", ctx.clock())
        return False, None
    arm_leak_scan(ctx.publisher, licensed=redact.licensed_texts(pack),
                  canaries=publish_canaries(ctx, snapshot))
    commitment, salt, sealed = _seal(commit_reveal, public, rec, ctx)
    # private, 0600; a forced re-run may replace a seal only outside live mode (a published live
    # commitment is never re-sealed)
    commit_reveal.save_sealed(commit_reveal.SealedCycle.build(commitment, salt, sealed),
                              ctx.state_dir / "salts", replace=ctx.settings.mode != "live")
    files: dict[str, bytes] = {}
    files.update(journal.commitment_files(commitment))
    if reveal_now:
        files.update(_reveal_files(journal, commit_reveal, commitment, salt, sealed, public))
    ops_path = journal.OPS_PATH
    files.update(journal.ops_files(_existing(ctx, ops_path), [redact.public_ops_row(rec)]))
    state = "AWAITING_ACCOUNT" if ctx.sources.broker is None else (
        {"WARN": "WARN", "HALTED": "HALTED", "FLAT": "FLAT"}.get(rec.kill_state, "LIVE"))
    if not smoke_open:
        files.update(journal.status_files(redact.public_status(
            state, last_cycle_id=rec.cycle_id, last_cycle_at=rec.slot, kill_state=rec.kill_state)))
    if not smoke_open and getattr(ctx.sources, "swing", None) is not None:
        files.update(swing_book_files(ctx, rec))
    if snapshot is not None and rec.risk is not None and not smoke_open:
        ref_w = rec.reference.weights() if rec.reference else None
        files.update(journal.book_files(redact.public_book(
            rec.cycle_id, rec.risk.base_w or snapshot.signed_w, lines=lines, reference_weights=ref_w,
            kill_state=rec.kill_state, positions=snapshot.positions, pack=pack)))
    try:
        result = ctx.publisher.publish(files, f"cycle {rec.cycle_id}: {rec.decision_state}")
    except Exception as exc:
        rec.flags.append(f"publish_error:{type(exc).__name__}")
        return False, None
    if rec.decision_id and result.commit_sha:
        ctx.ledger.set_commitment(rec.decision_id, commitment.commitment_sha256)
    return bool(result.pushed or result.dry_run), result.commit_sha


def swing_seal_inputs(ctx: CycleContext, rec: CycleRecord) -> dict[str, Any]:
    """`public_cycle`'s swing arguments for a swing slot (`rec.extras["swing"]`): the licensed feed
    texts of this cycle and of every carried idea's origin cycles (unreadable -> withheld, fail
    closed), the open swing trades and the Skeptic-health line. {} without a swing record."""
    record = rec.extras.get("swing") if isinstance(rec.extras, Mapping) else None
    if not isinstance(record, Mapping):
        return {}
    from council.swing.models import OPEN_STATES
    from council.swing.record import origin_texts

    origins = [c for row in record.get("ideas") or [] for c in (row.get("carried_from") or [])
               if isinstance(c, str)]
    out: dict[str, Any] = {"swing_texts": {}, "swing_trades": [], "swing_health": None}
    try:
        out["swing_texts"] = origin_texts(ctx.state_dir, [rec.cycle_id, *origins])
    except Exception as exc:  # noqa: BLE001 - no texts: every quoted text is withheld
        rec.flags.append(f"swing_texts_error:{type(exc).__name__}")
    try:
        out["swing_trades"] = ctx.ledger.swing_trades(states=sorted(OPEN_STATES))
        out["swing_health"] = swing_health(ctx.ledger)
    except Exception as exc:  # noqa: BLE001
        rec.flags.append(f"swing_seal_error:{type(exc).__name__}")
    return out


def _day(value: Any) -> Any:
    from datetime import date as _date

    if isinstance(value, datetime):
        return value.astimezone(clock.NEW_YORK).date() if value.tzinfo else value.date()
    if isinstance(value, _date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date() if value else None
    except ValueError:
        return None


def swing_book_files(ctx: CycleContext, rec: CycleRecord) -> dict[str, bytes]:
    """`journal/swing/latest.json`: the swing page's document from the ledger's swing trades, paper
    trades and SQ-8 benchmark days (percent-only; `public_swing_book`). A failure is a flag and no
    file (the page keeps its last version)."""
    from council import invariants
    from council.publish import journal, redact

    try:
        ledger = ctx.ledger
        paper_rows = ledger.paper_trades()
        opened = [d for d in (_day(r.get("opened_at")) for r in paper_rows) if d is not None]
        book = redact.public_swing_book(
            list(ledger.swing_trades()), as_of=rec.slot, paper_rows=paper_rows,
            benchmark_days=ledger.benchmark_days(), health=swing_health(ledger),
            live=bool(invariants.SWING_BOOK_LIVE) and ctx.sources.broker is not None,
            live_since=_day(ledger.get_runtime(SWING_LIVE_SINCE_KEY)),
            paper_since=min(opened) if opened else None,
            today=rec.slot.astimezone(clock.NEW_YORK).date())
        return journal.swing_files(book)
    except Exception as exc:  # noqa: BLE001 - the swing page never stops a publish
        rec.flags.append(f"swing_book_error:{type(exc).__name__}")
        return {}


def _seal(commit_reveal, public, rec, ctx):
    return commit_reveal.seal_bytes(public, sealed_at=ctx.clock(), code_commit=ctx.code_commit)


def _reveal_files(journal, commit_reveal, commitment, salt, sealed, public) -> dict[str, bytes]:
    """Reveal the EXACT sealed bytes (never a rebuilt document)."""
    return journal.reveal_files(sealed, salt, commitment)


def _existing(ctx: CycleContext, rel: str) -> bytes | None:
    clone = getattr(ctx.publisher, "clone_dir", None)
    dry = getattr(ctx.publisher, "dry_run_dir", None)
    for root in (dry, clone):
        if root is None:
            continue
        p = root / rel
        if p.exists():
            return p.read_bytes()
    return None


def _publish_ops_only(ctx: CycleContext, rec: CycleRecord) -> tuple[bool, str | None]:
    from council.publish import journal, redact

    if ctx.publisher is None:
        return False, None
    try:   # the ops row is built by the same redact module that may have just raised (V11)
        files = journal.ops_files(_existing(ctx, journal.OPS_PATH), [redact.public_ops_row(rec)])
        result = ctx.publisher.publish(files, f"cycle {rec.cycle_id}: {rec.status}")
    except Exception:
        return False, None
    return bool(result.pushed or result.dry_run), result.commit_sha


def _notify_proposal(ctx: CycleContext, rec: CycleRecord, plan, *, urgent: bool,
                     valid_until: datetime | None = None) -> None:
    if ctx.notifier is None or plan is None or rec.risk is None:
        return
    risk = rec.risk
    deadline = valid_until or clock.proposal_valid_until(rec.slot)
    body = (f"{len(plan.legs)} legs, gross {plan.gross_before:.2f}x → {plan.gross_after:.2f}x, "
            f"cost {plan.cost_bps_nav:.1f} bp, valid until "
            f"{deadline.strftime('%H:%MZ')} (basis {risk.basis})")
    try:
        ctx.notifier.send(f"council {rec.cycle_id}", body, priority="urgent" if urgent else "default")
    except Exception as exc:
        rec.flags.append(f"notify_error:{type(exc).__name__}")


# ------------------------------------------------------------------------------ swing book (SW-5b)
SWING_DAILY_KEY = "swing_daily_last"          # runtime: the last US day whose paper / SQ-8 step ran
SWING_EXIT_FLAGS = {"time_stop_due": "time", "earnings_exit_due": "earnings",
                    "target_reached_unplaced": "target"}
SWING_ENTRY_VALID = timedelta(minutes=60)     # S16: a swing entry leg's own approval window


@dataclass
class SwingEntry:
    trade_id: str
    idea_id: str
    ticker: str
    side: str
    instrument_id: int | None
    size_nav: float
    stop_pct: float
    target_pct: float
    time_stop_date: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class SwingRun:
    """What the swing stage hands the core cycle. `lines` / `orders` are empty unless live."""

    live: bool = False
    slot_ok: bool = False
    lines: list[Any] = field(default_factory=list)
    orders: list[Any] = field(default_factory=list)
    entries: list[SwingEntry] = field(default_factory=list)
    exits: dict[str, str] = field(default_factory=dict)       # trade id -> exit kind
    flags: list[str] = field(default_factory=list)
    calls: list[Any] = field(default_factory=list)
    private_calls: list[Any] = field(default_factory=list)    # the weekly canary (H11: ledger only)
    paper: list[str] = field(default_factory=list)            # paper ids tracked this slot


def _swing_short(cycle_id: str) -> str:
    return "".join(ch for ch in cycle_id if ch.isalnum())[:20]


async def run_swing(ctx: CycleContext, rec: CycleRecord, *, snapshot: Any, kill_state: str, nav: Any,
                    slot: datetime, now: datetime, econ: Any = None, sink: Any = None) -> SwingRun:
    """The swing stage of one cycle (swing-book.md rev 2, §1.1). Never raises (`CanaryLeak`, a code
    bug, excepted): a failure becomes `swing_error:<stage>:<type>` and the core cycle continues.

    Every cycle: settle last slots' trades (a rejected / expired entry -> `missed`, a rejected /
    expired exit -> back to open, a filled exit -> its closed state with the outcome detail), and once
    per US day `paper.settle` + the SQ-8 step. At a swing slot: code exits (time stop, earnings,
    a watched target: created HERE, never by the watch, from the watch's flags and the time stop),
    the swing council, the S-rules final pass, paper tracking of every idea group, and - only when
    `invariants.SWING_BOOK_LIVE` is True and a broker is connected - the pinned swing lines and the
    planner's `SwingOrder`s (entries, exits, `set_tp` for `open_tp_missing`). With the switch off the
    swing book is PAPER-ONLY: no live leg, nothing to approve."""
    from council import invariants
    from council.swing.roles import CanaryLeak

    out = SwingRun()
    stage = "settle"
    try:
        out.flags += settle_swing(ctx.ledger, now)
        stage = "daily"
        out.flags += swing_daily(ctx, slot, now)
        src = getattr(ctx.sources, "swing", None)
        if src is not None and getattr(src, "prepare", None) is not None and not src.unavailable:
            stage = "prepare"
            out.flags += src.prepare(slot, now)          # the after-close screen, once per session
        stage = "canary"
        await _swing_canary(ctx, out, slot=slot, now=now)
        stage = "slot"
        from council.swing.slots import is_swing_slot

        if not is_swing_slot(slot, ctx.policy.swing.slots):
            return out
        out.slot_ok = True
        out.live = bool(invariants.SWING_BOOK_LIVE) and snapshot is not None and ctx.sources.broker is not None
        stage = "exits"
        out.exits = swing_code_exits(ctx.ledger, ctx.policy, slot)
        stage = "council"
        await _swing_council(ctx, rec, out, snapshot=snapshot, kill_state=kill_state, nav=nav, slot=slot,
                             now=now, econ=econ, sink=sink)
        if out.live:
            stage = "lines"
            _swing_lines_and_orders(ctx, out, snapshot=snapshot)
    except CanaryLeak:
        raise
    except Exception as exc:  # noqa: BLE001 - the swing book never stops the core cycle
        import logging

        logging.getLogger("council.cycle").exception("swing stage failed")
        out.flags.append(f"swing_error:{stage}:{type(exc).__name__}")
        out.lines, out.orders, out.entries = [], [], []
    finally:
        src = getattr(ctx.sources, "swing", None)
        if src is not None and hasattr(src, "drain"):
            out.flags += [f for f in src.drain() if f not in out.flags]
    if not out.live:
        out.lines, out.orders = [], []
    return out


async def _swing_canary(ctx: CycleContext, out: SwingRun, *, slot: datetime, now: datetime) -> None:
    """The weekly Skeptic canary (swing-book §2, H11) when the sources can plant a past event: ONE
    Skeptic call, graded by code, the grade kept in the runtime health record. Its call goes to
    the ledger only (never `rec.calls`, never the capture): a canary reaches no public record but
    the health line."""
    from council.swing.canary import canary_due
    from council.swing.council import run_canary

    src = getattr(ctx.sources, "swing", None)
    event_fn = getattr(src, "canary_event", None)
    if event_fn is None or getattr(src, "unavailable", ()) or not canary_due(slot):
        return
    event = event_fn(slot)
    if event is None:
        out.flags.append("swing_canary_no_event")
        return
    res = await run_canary(ctx.gateway, ctx.registry, ctx.policy, event, slot=slot,
                           skeptic_gw=src.skeptic_gateway, sink=None)
    out.private_calls += list(res.calls)
    out.flags += [f for f in res.flags if f not in out.flags]
    record_canary_grade(ctx.ledger, res.grade, now)


def settle_swing(ledger: Any, now: datetime) -> list[str]:
    """Trades whose decision is over: a `proposed` entry whose decision never executed -> `missed`
    (its idea -> `pending`); an `exit_pending` trade whose exit was rejected / expired -> back to
    open (never stranded); one whose exit leg filled -> `closed_<kind>` with its outcome detail."""
    from council.swing.models import IllegalTransition

    flags: list[str] = []
    dead = ("rejected", "expired", "superseded", "reviewed_no_action")
    try:
        trades = ledger.swing_trades(states=["proposed", "exit_pending"])
    except Exception:  # noqa: BLE001 - an old ledger: nothing to settle
        return flags
    try:
        expire_stale_ideas(ledger, now)
    except Exception as exc:  # noqa: BLE001 - an idea housekeeping failure never stops settling
        flags.append(f"swing_idea_expiry_error:{type(exc).__name__}")
    for t in trades:
        try:
            if t.state == "proposed" and t.decision_id:
                d = ledger.get_decision(t.decision_id)
                leg = ledger.get_leg(t.decision_id, int(t.entry_seq)) if t.entry_seq is not None else None
                sent = leg is not None and leg.state not in ("planned", "skipped")
                if d.state in dead or (d.state in ("completed", "completed_partial") and not sent):
                    ledger.transition_swing_trade(t.trade_id, "missed", reason=f"entry_{d.state}"[:64],
                                                  cycle_id=d.cycle_id, now=now)
                    if t.idea_id and ledger.swing_idea(t.idea_id) is not None:
                        ledger.update_swing_idea(t.idea_id, status="pending", carry_cycle=d.cycle_id, now=now)
            elif t.state == "exit_pending":
                flags += _settle_exit(ledger, t, now, dead)
        except (IllegalTransition, Exception) as exc:  # noqa: BLE001 - one bad row never stops the rest
            flags.append(f"swing_settle_error:{type(exc).__name__}")
    return flags


def _settle_exit(ledger: Any, t: Any, now: datetime, dead: tuple[str, ...]) -> list[str]:
    """The fallback of the executor's own close (`swing.exits.close_filled_exit`, idempotent): a
    filled exit closes the trade; a rejected / expired exit returns it to where it was."""
    from council.swing.exits import close_filled_exit

    detail = t.detail or {}
    decision_id = detail.get("exit_decision")
    if not isinstance(decision_id, str):
        return []
    if close_filled_exit(ledger, t.trade_id, decision_id, now) is not None:
        return []
    d = ledger.get_decision(decision_id)
    legs = [r for r in ledger.legs(decision_id) if r.kind == "close" and (r.detail or {}).get("swing_trade_id") == t.trade_id]
    filled = [r for r in legs if r.state == "filled"]
    back = str(detail.get("pre_exit_state") or "open")
    if d.state in dead or (d.state in ("completed", "completed_partial", "blocked") and not filled
                           and all(r.state in ("planned", "skipped", "rejected") for r in legs)):
        ledger.transition_swing_trade(t.trade_id, back, reason=f"exit_{d.state}"[:64], cycle_id=d.cycle_id, now=now)
    return []


def swing_code_exits(ledger: Any, policy: Any, slot: datetime) -> dict[str, str]:
    """{trade id: exit kind} of the exits code makes at this swing slot, no LLM: the time stop
    (computed here from the trade's date, whether or not the watch flagged it), and the watch's
    `earnings_exit_due` / `target_reached_unplaced` flags since the trade's last state change."""
    from datetime import date as _date

    from council.clock import NEW_YORK
    from council.swing.rules import exit_due
    from council.swing.slots import season_of

    trades = ledger.swing_trades(states=["open", "open_tp_missing", "partial"])
    if not trades:
        return {}
    sp = policy.swing
    today = slot.astimezone(NEW_YORK).date()
    per_session = len(sp.slots.summer_utc if season_of(slot) == "summer" else sp.slots.winter_utc)
    out: dict[str, str] = {}
    events = ledger.swing_events()
    for t in trades:
        if t.time_stop_date:
            due, _ = exit_due(_date.fromisoformat(t.time_stop_date), today, slots_per_session=per_session,
                              lead_slots=0)
            if due:
                out[t.trade_id] = "time"
                continue
        for e in events:
            if e["trade_id"] == t.trade_id and e["kind"] in SWING_EXIT_FLAGS and \
                    str(e["created_at"]) >= t.updated_at.isoformat()[:10]:
                out[t.trade_id] = SWING_EXIT_FLAGS[e["kind"]]
                break
    return out


def _open_trade_views(ledger: Any, snapshot: Any, slot: datetime, exits: Mapping[str, str]) -> list[Any]:
    from council.clock import NEW_YORK
    from council.swing.council import OpenTrade
    from council.swing.rules import sessions_until

    marks = {p.position_id: p.close_rate for p in (snapshot.positions if snapshot is not None else [])}
    today = slot.astimezone(NEW_YORK).date()
    views = []
    for t in ledger.swing_trades(states=["open", "open_tp_missing", "partial"]):
        px = next((marks[pid] for pid in t.position_ids if marks.get(pid)), None)
        to_stop = abs(px / t.sl_rate - 1) * 100 if px and t.sl_rate else None
        to_target = abs(t.tp_rate / px - 1) * 100 if px and t.tp_rate else None
        held = sessions_until(t.opened_at.date(), today) if t.opened_at is not None else 0
        triggers: tuple[str, ...] = ()
        if t.trade_id in exits:
            triggers = (f"code_exit:{exits[t.trade_id]}",)
        elif (to_stop is not None and to_target is not None and t.sl_rate and t.open_rate
              and min(to_stop, to_target) <= 0.25 * abs(t.open_rate / t.sl_rate - 1) * 100):
            triggers = ("near_stop_or_target",)
        views.append(OpenTrade(ref=t.trade_id, ticker=t.ticker, side=t.side, days_held=int(held),
                               to_stop_pct=to_stop, to_target_pct=to_target, triggers=triggers))
    return views


def _book_state(ledger: Any, policy: Any, *, kill_state: str, nav: Any, slot: datetime, now: datetime) -> Any:
    from council.clock import NEW_YORK
    from council.swing import rules as R
    from council.swing.models import ACTIVE_STATES

    trades = []
    entries_7d = 0
    week_ago = now - timedelta(days=7)
    for t in ledger.swing_trades(states=sorted(ACTIVE_STATES)):
        d = t.detail or {}
        stop = d.get("stop_pct")
        size = d.get("size_nav")
        if isinstance(stop, int | float) and isinstance(size, int | float):
            trades.append(R.BookTrade(ref=t.trade_id, ticker=t.ticker, side=t.side, size_nav=float(size),
                                      stop_pct=float(stop), sector=d.get("sector") if isinstance(d.get("sector"), str) else None,
                                      beta_60d=d.get("beta") if isinstance(d.get("beta"), int | float) else None))
        if t.created_at >= week_ago and t.state != "proposed":
            entries_7d += 1
    dd = getattr(nav, "drawdown", None) if nav is not None else None
    return R.BookState(today=slot.astimezone(NEW_YORK).date(), now=now, kill_state=kill_state, trades=trades,
                       entries_7d=entries_7d, drawdown_from_peak=dd if isinstance(dd, int | float) else None,
                       blockers=list(ledger.swing_blockers()))


def _swing_cost_fn(ctx: CycleContext, snapshot: Any, slot: datetime) -> Any:
    """S6's round-trip cost callback (`swing.costs.round_trip`, percent of the position). No funded
    NAV, no snapshot or any failure -> None (the rules drop the entry `cost_unavailable`)."""
    from council.clock import NEW_YORK
    from council.swing import costs as sc

    account = sc.load_account(ctx.state_dir)
    try:
        cfg = sc.CostConfig.from_policy(ctx.policy)
    except Exception:  # noqa: BLE001 - no cost config: no entry
        cfg = None

    def cost(side: str, size: float, sessions: int) -> float | None:
        if cfg is None or (snapshot is None and account is None):
            return None
        try:
            # no snapshot = a paper run (never live): the funded NAV stands in (NAV-invariant, D18)
            nav = float(snapshot.equity_usd) if snapshot is not None else float(account.funded_real_nav_usd)  # type: ignore[union-attr]
            return sc.round_trip(side, size_nav=size, real_nav_usd=nav, virtual_nav_usd=nav,  # type: ignore[arg-type]
                                 account=account, cfg=cfg, entry_day=slot.astimezone(NEW_YORK).date(),
                                 time_stop_sessions=sessions).total_pct
        except sc.CostUnavailable:
            return None

    return cost


def _swing_ledger_idea(ledger: Any, cycle_id: str, idea: Any, status: str, now: datetime) -> str:
    """The ledger id of one Scout idea: a pending idea on the same ticker and side is carried
    forward (its record rewritten, `carry_cycle` = this cycle), else a new id."""
    rec = {"ref": idea.ref, "setup": idea.idea.setup, "stop_pct": idea.idea.stop_pct,
           "target_pct": idea.idea.target_pct, "time_stop_days": idea.idea.time_stop_days}
    for row in ledger.swing_ideas(status="pending"):
        if row["ticker"] == idea.ticker and row["side"] == idea.idea.side:
            ledger.update_swing_idea(row["idea_id"], status=status, record=rec, carry_cycle=cycle_id, now=now)
            return str(row["idea_id"])
    iid = f"idea:{_swing_short(cycle_id)}_{idea.ref.split(':', 1)[1]}"
    if ledger.swing_idea(iid) is None:
        ledger.add_swing_idea(iid, origin_cycle=cycle_id, ticker=idea.ticker, side=idea.idea.side,
                              status=status, setup=idea.idea.setup, record=rec, now=now)
    else:
        ledger.update_swing_idea(iid, status=status, record=rec, now=now)
    return iid


REPROPOSAL_SESSIONS = 3          # §4.3: at most `entry_guard.max_reproposals` re-proposals within 3 sessions


def _idea_sessions(row: Mapping[str, Any], now: datetime) -> int:
    from council.clock import NEW_YORK
    from council.swing.rules import sessions_until

    created = row.get("created_at")
    try:
        born = created if isinstance(created, datetime) else datetime.fromisoformat(str(created))
    except ValueError:
        return REPROPOSAL_SESSIONS + 1          # unreadable: treat as too old (fail closed)
    if born.tzinfo is None:
        born = born.replace(tzinfo=UTC)
    return sessions_until(born.astimezone(NEW_YORK).date(), now.astimezone(NEW_YORK).date())


def reproposal_refused(ledger: Any, policy: Any, ticker: str, side: str, now: datetime) -> bool:
    """§4.3: a missed entry returns to `pending`; the Scout may re-propose it at most
    `entry_guard.max_reproposals` times within REPROPOSAL_SESSIONS sessions of the first proposal,
    then it is `expired`. True (and the pending idea -> `expired`) when this re-proposal is over the
    limit. A ticker/side with no pending idea is a fresh idea (False)."""
    limit = int(policy.swing.entry_guard.max_reproposals)
    for row in ledger.swing_ideas(status="pending"):
        if row["ticker"] != ticker or row["side"] != side:
            continue
        carried = [c for c in row.get("carry_cycles") or [] if c != row.get("origin_cycle")]
        if len(carried) >= limit or _idea_sessions(row, now) > REPROPOSAL_SESSIONS:
            ledger.update_swing_idea(row["idea_id"], status="expired", now=now)
            return True
        return False
    return False


def expire_stale_ideas(ledger: Any, now: datetime) -> list[str]:
    """Pending ideas older than REPROPOSAL_SESSIONS sessions -> `expired` (never re-proposed)."""
    out = []
    for row in ledger.swing_ideas(status="pending"):
        if _idea_sessions(row, now) > REPROPOSAL_SESSIONS:
            ledger.update_swing_idea(row["idea_id"], status="expired", now=now)
            out.append(str(row["idea_id"]))
    return out


async def _swing_council(ctx: CycleContext, rec: CycleRecord, out: SwingRun, *, snapshot: Any, kill_state: str,
                         nav: Any, slot: datetime, now: datetime, econ: Any, sink: Any) -> None:
    from council.broker.instruments import InstrumentMap
    from council.swing import rules as R
    from council.swing.council import run_swing_stage

    ledger, policy = ctx.ledger, ctx.policy
    src = getattr(ctx.sources, "swing", None)
    if src is None:
        out.flags.append("swing_sources_missing")
        return
    if getattr(src, "unavailable", ()):             # fail closed: a missing credential, no council
        out.flags += [f for f in src.unavailable if f not in out.flags]
        return
    views = _open_trade_views(ledger, snapshot, slot, out.exits)
    inputs = src.inputs(slot, views, list(out.exits))
    result = await run_swing_stage(ctx.gateway, ctx.registry, policy, inputs, gate=src.gate,
                                   skeptic_gw=src.skeptic_gateway, sink=sink)
    out.flags += [f for f in result.flags if f not in out.flags]
    out.calls = list(result.calls)
    for ref in result.exits():                   # PM exits of open trades (code exits already in)
        if ref.startswith("trade:") and ref not in out.exits:
            out.exits[ref] = "exit"
    if snapshot is None and not out.live:
        # a paper run without a broker (SW-5c): the S-rules see a flat book at its peak and the
        # funded NAV of the account file (costs are NAV-invariant, D18); nothing here can trade
        nav = SimpleNamespace(drawdown=0.0)
        out.flags.append("swing_paper_assumed_book")
    book = _book_state(ledger, policy, kill_state=kill_state, nav=nav, slot=slot, now=now)
    cost_fn = _swing_cost_fn(ctx, snapshot, slot)
    imap = InstrumentMap.load(ctx.state_dir / "instruments.json")
    cands, aggs = [], {}
    for agg in result.entries():
        idea = result.ideas.get(agg.ref)
        if idea is None or idea.card is None:
            continue
        extras = dict(src.candidate_extras(idea) or {}) if src.candidate_extras else {}
        cands.append(R.candidate_from_card(idea.card, ref=agg.ref, ticker=idea.ticker, setup=idea.idea.setup,
                                           stop_pct=agg.stop_pct, target_pct=agg.target_pct,
                                           time_stop_days=agg.time_stop_days, **extras))
        aggs[agg.ref] = agg
    accepted, rule_dropped = R.final_pass(cands, book, policy.swing, cost_fn)
    rule_codes: dict[str, str] = {}
    for v in rule_dropped:
        code = R.public_code(v.code or "unknown")
        out.flags.append(f"swing_drop:{code}")
        rule_codes[v.ref] = code
    ok = {v.ref: v for v in accepted}
    cycle_id = rec.cycle_id
    short = _swing_short(cycle_id)
    idea_ids: dict[str, str] = {}
    for k, (ref, idea) in enumerate(sorted(result.ideas.items()), start=1):
        v = ok.get(ref)
        if v is not None and reproposal_refused(ledger, policy, idea.ticker, idea.idea.side, now):
            v = None                                    # §4.3: over the re-proposal limit -> expired
            out.flags.append(REPROPOSAL_FLAG)
            rule_codes[ref] = REPROPOSAL_CODE
        status = "accepted" if v is not None else "dropped"
        idea_id = _swing_ledger_idea(ledger, cycle_id, idea, status, now)
        idea_ids[ref] = idea_id
        verdict = idea.verdict.verdict if idea.verdict is not None else None
        if v is not None:
            agg = aggs[ref]
            card = idea.card.fields if idea.card is not None else {}
            beta = card.get("beta_60d")
            sigma = card.get("sigma_daily")
            entry = SwingEntry(
                trade_id=f"trade:{short}_{k}", idea_id=idea_id, ticker=idea.ticker, side=idea.idea.side,
                instrument_id=imap.get(idea.line_id), size_nav=float(v.size_nav), stop_pct=float(v.stop_pct or 0),
                target_pct=float(v.target_pct or 0),
                time_stop_date=v.time_stop_date.isoformat() if v.time_stop_date else "",
                detail={"size_nav": round(float(v.size_nav), 6), "stop_pct": round(float(v.stop_pct or 0), 6),
                        "target_pct": round(float(v.target_pct or 0), 6),
                        "beta": float(beta) if isinstance(beta, int | float) else None,
                        "sigma_daily": float(sigma) / 100.0 if isinstance(sigma, int | float) else None,
                        "sector_etf": card.get("sector_etf") if isinstance(card.get("sector_etf"), str) else None,
                        "skeptic": verdict.verdict if verdict is not None else None,
                        "priced_in": verdict.priced_in if verdict is not None else None,
                        "regime": verdict.regime if verdict is not None else None,
                        "votes_for": agg.votes_for, "replicates": agg.replicates,
                        "live": out.live})
            out.entries.append(entry)
        _paper_track(ctx, out, result, ref, idea, idea_id, v, slot=slot, now=now, cycle_id=cycle_id,
                     skeptic=verdict.verdict if verdict is not None else None)
    try:                                # PRIVATE (ledger): the slot's full chain (`swing.record`)
        rec.extras["swing"] = _swing_slot_record(ctx, out, result, inputs, accepted=[r for r in ok if r not in rule_codes],
                                                 rule_codes=rule_codes, idea_ids=idea_ids, cycle_id=cycle_id)
    except Exception as exc:  # noqa: BLE001 - a record failure never stops the cycle; nothing is published
        out.flags.append(f"swing_record_error:{type(exc).__name__}")
    out.flags += record_skeptic_verdicts(ledger, result, now)


REPROPOSAL_FLAG = "swing_drop:reproposal_limit"
REPROPOSAL_CODE = "reproposal_limit"
SKEPTIC_HEALTH_KEY = "swing_skeptic_health"   # runtime: canary grades + the Skeptic's own verdicts
SKEPTIC_HEALTH_KEEP = 200
SWING_LIVE_SINCE_KEY = "swing_live_since"     # runtime: the US day of the first live swing slot


def _swing_slot_record(ctx: CycleContext, out: SwingRun, result: Any, inputs: Any, *, accepted: list[str],
                       rule_codes: Mapping[str, str], idea_ids: Mapping[str, str], cycle_id: str) -> dict[str, Any]:
    """`swing.record.swing_record` for this slot: the catalyst index the council used, the ledger
    idea ids, each carried idea's earlier cycles (origin + carry cycles, this one excluded) and the
    public links of the reading list's public-domain items."""
    from council.swing.record import swing_record
    from council.swing.roles import catalyst_index

    catalysts = catalyst_index(inputs.reading, inputs.screen_rows, slot=inputs.slot,
                               screen_available_at=inputs.screen_available_at)
    carried: dict[str, list[str]] = {}
    for ref, iid in idea_ids.items():
        row = ctx.ledger.swing_idea(iid) or {}
        cycles = [row.get("origin_cycle"), *(row.get("carry_cycles") or [])]
        earlier = [c for c in dict.fromkeys(cycles) if isinstance(c, str) and c and c != cycle_id]
        if earlier:
            carried[ref] = earlier
    links = {str(i.id): str(i.link) for i in inputs.reading
             if str(getattr(i, "id", "")).startswith("P:") and getattr(i, "link", None)}
    return swing_record(result, catalysts=catalysts, live=out.live, accepted=accepted, rule_codes=rule_codes,
                        idea_ids=idea_ids, carried_from=carried, links=links,
                        live_setups=ctx.policy.swing.setups_live)


def _health_state(ledger: Any) -> dict[str, list[str]]:
    raw = ledger.get_runtime(SKEPTIC_HEALTH_KEY) or {}
    return {"canary_grades": [str(g) for g in raw.get("canary_grades") or []],
            "verdicts": [str(v) for v in raw.get("verdicts") or []]}


def record_skeptic_verdicts(ledger: Any, result: Any, now: datetime) -> list[str]:
    """Append the Skeptic's OWN verdicts of this slot (the model's word before any code rule;
    canaries never) to the runtime health record. Returns flags; never raises."""
    try:
        own = []
        for ref in sorted(result.ideas, key=lambda r: int(r.split(":")[1]) if r.split(":")[-1].isdigit() else 0):
            idea = result.ideas[ref]
            if getattr(idea, "canary", False) or idea.verdict is None or idea.verdict.verdict is None:
                continue
            word = getattr(idea.verdict.verdict, "verdict", None)
            if word in ("pass", "wait", "reject"):
                own.append(word)
        if not own:
            return []
        state = _health_state(ledger)
        state["verdicts"] = (state["verdicts"] + own)[-SKEPTIC_HEALTH_KEEP:]
        ledger.set_runtime(SKEPTIC_HEALTH_KEY, state, now=now)
        return []
    except Exception as exc:  # noqa: BLE001 - measurement never stops a cycle
        return [f"skeptic_health_error:{type(exc).__name__}"]


def record_canary_grade(ledger: Any, grade: str, now: datetime) -> None:
    """Append one canary grade (`caught` / `missed`) to the runtime health record."""
    if grade not in ("caught", "missed"):
        return
    state = _health_state(ledger)
    state["canary_grades"] = (state["canary_grades"] + [grade])[-SKEPTIC_HEALTH_KEEP:]
    ledger.set_runtime(SKEPTIC_HEALTH_KEY, state, now=now)


def swing_health(ledger: Any) -> Any:
    """The public Skeptic-health line from the runtime record (`public_skeptic_health`)."""
    from council.publish.redact import public_skeptic_health

    state = _health_state(ledger)
    return public_skeptic_health(state["canary_grades"], state["verdicts"])


def _paper_group(result: Any, ref: str, idea: Any, accepted: bool, live: bool) -> str:
    if idea in result.paper_only:
        return "paper_only"
    if accepted:
        return "executed" if live else "missed"
    outcome = next((o for o in result.outcomes if o.ref == ref), None)
    if outcome is not None and outcome.stage == "skeptic":
        status = idea.verdict.status if idea.verdict is not None else None
        return "skeptic_wait" if status == "wait" else "skeptic_rejected"
    if outcome is not None and outcome.stage == "pm" and outcome.code is not None:
        return "pm_passed"              # the PM passed; a PM entry the S-rules dropped is code_dropped
    return "code_dropped"


def _paper_track(ctx: CycleContext, out: SwingRun, result: Any, ref: str, idea: Any, idea_id: str, v: Any, *,
                 slot: datetime, now: datetime, cycle_id: str, skeptic: str | None) -> None:
    """SB12: every idea group is paper-tracked with the Skeptic's verdict (none without a reference
    price: flag `paper_no_reference`)."""
    from council.clock import NEW_YORK
    from council.swing import paper
    from council.swing.rules import add_sessions

    src = getattr(ctx.sources, "swing", None)
    price = src.reference_price(idea.ticker) if src is not None and src.reference_price else None
    if not isinstance(price, int | float) or not price > 0:
        out.flags.append("paper_no_reference")
        return
    group = _paper_group(result, ref, idea, v is not None, out.live)
    stop = float(v.stop_pct) if v is not None else float(idea.idea.stop_pct)
    target = float(v.target_pct) if v is not None else float(idea.idea.target_pct)
    days = int(v.time_stop_days) if v is not None and v.time_stop_days else int(idea.idea.time_stop_days)
    day = slot.astimezone(NEW_YORK).date()
    row = ctx.ledger.swing_idea(idea_id) or {}
    carried = len(row.get("carry_cycles") or [])
    ref = idea_id if not carried else f"{idea_id}-r{carried}"      # a re-proposal is its own paper row
    p = paper.PaperIdea(ref=ref, ticker=idea.ticker, side=idea.idea.side, group=group, entry_day=day,
                        entry_ref=float(price), stop_pct=stop, target_pct=target,
                        time_stop_day=add_sessions(day, days))
    try:
        out.paper.append(paper.track(ctx.ledger, p, origin_cycle=cycle_id, opened_at=now,
                                     skeptic_verdict=skeptic))
    except Exception as exc:  # noqa: BLE001 - a duplicate paper row (re-run slot) is not an error
        out.flags.append(f"paper_track_error:{type(exc).__name__}")


def _swing_lines_and_orders(ctx: CycleContext, out: SwingRun, *, snapshot: Any) -> None:
    """The pinned runtime lines (entries, exits, holds) and the planner's orders."""
    from council.execution.planner import SwingOrder, set_tp_orders
    from council.swing import book as B

    ledger = ctx.ledger
    signed = dict(snapshot.signed_w) if snapshot is not None else {}
    open_trades = ledger.swing_trades(states=["open", "open_tp_missing", "partial"])
    for t in open_trades:
        d = t.detail or {}
        line = B.line_id(t.ticker)
        exit_kind = out.exits.get(t.trade_id)
        out.lines.append(B.open_line(t.trade_id, t.ticker, t.side, signed.get(line, 0.0), exit_=exit_kind is not None,
                                     stop_pct=d.get("stop_pct") if isinstance(d.get("stop_pct"), int | float) else None,
                                     sigma_daily=d.get("sigma_daily") if isinstance(d.get("sigma_daily"), int | float) else None,
                                     beta_60d=d.get("beta") if isinstance(d.get("beta"), int | float) else None,
                                     vehicle=t.ticker))
        if exit_kind is not None:
            out.orders.append(SwingOrder(line=line, trade_id=t.trade_id, side=t.side, action="exit",  # type: ignore[arg-type]
                                         symbol=t.ticker, instrument_id=t.instrument_id,
                                         reason=f"{line}: swing exit ({exit_kind})"))
    out.orders += [o for o in set_tp_orders(open_trades) if o.trade_id not in out.exits]
    for e in out.entries:
        if e.instrument_id is None:
            out.flags.append(f"swing_drop:{e.ticker}:no_instrument")
            continue
        line = B.line_id(e.ticker)
        out.lines.append(B.entry_line(e.trade_id, e.ticker, e.side, e.size_nav, stop_pct=e.stop_pct,
                                      sigma_daily=e.detail.get("sigma_daily"), beta_60d=e.detail.get("beta"),
                                      vehicle=e.ticker))
        out.orders.append(SwingOrder(line=line, trade_id=e.trade_id, side=e.side, action="enter",  # type: ignore[arg-type]
                                     symbol=e.ticker, instrument_id=e.instrument_id, size_nav=e.size_nav,
                                     stop_pct=e.stop_pct, target_pct=e.target_pct, time_stop_date=e.time_stop_date,
                                     reason=f"{line}: swing {e.side} entry"))


def swing_orders_after_engine(out: SwingRun, decision: RiskDecision) -> list[Any]:
    """The orders whose line survived the engine (a whole-book limit removes whole swing entries;
    exits and holds are never removed)."""
    removed = {note.split(":", 1)[0] for note in getattr(decision, "hold_reasons", []) or []
               if "swing_book_limit" in note}
    return [o for o in out.orders if not (o.action == "enter" and o.line in removed)]


def record_swing_decision(ledger: Any, out: SwingRun, plan: Plan, decision_id: str, cycle_id: str,
                          now: datetime) -> list[str]:
    """After the decision exists: a `proposed` trade per planned swing entry (decision id + entry
    seq, its approved rates and percent-only detail), and `exit_pending` for each planned exit."""
    flags: list[str] = []
    by_trade = {e.trade_id: e for e in out.entries}
    for leg in plan.legs:
        if not leg.is_swing or not leg.swing_trade_id:
            continue
        try:
            if leg.kind == "open" and leg.swing_trade_id in by_trade:
                e = by_trade[leg.swing_trade_id]
                detail = {k: v for k, v in e.detail.items() if v is not None}
                detail["tp_mode"] = leg.tp_mode or "none"
                ledger.create_swing_trade(e.trade_id, ticker=e.ticker, side=e.side, idea_id=e.idea_id,
                                          origin_cycle=cycle_id, decision_id=decision_id, entry_seq=leg.seq,
                                          instrument_id=leg.instrument_id, sl_rate=leg.sl_rate,
                                          tp_rate=leg.tp_rate, time_stop_date=e.time_stop_date,
                                          detail=detail, now=now)
                ledger.update_swing_idea(e.idea_id, status="proposed", carry_cycle=cycle_id, now=now)
            elif leg.kind in ("close", "partial_close"):
                t = ledger.swing_trade(leg.swing_trade_id)
                if t is not None and t.state in ("open", "open_tp_missing", "partial"):
                    kind = out.exits.get(t.trade_id, "exit")
                    ledger.update_swing_trade(t.trade_id, detail={"exit_decision": decision_id, "exit_kind": kind,
                                                                  "pre_exit_state": t.state}, now=now)
                    ledger.transition_swing_trade(t.trade_id, "exit_pending", reason=f"exit_proposed:{kind}",
                                                  cycle_id=cycle_id, now=now)
        except Exception as exc:  # noqa: BLE001 - the approval refuses a leg whose trade is missing
            flags.append(f"swing_record_error:{type(exc).__name__}")
    return flags


def stamp_swing_legs(plan: Plan, slot: datetime) -> Plan:
    """Swing legs trade in the US session. An entry's own deadline is min(slot + 60 min, US close -
    10 min, next slot - 5 min) (S16); exits and take-profit legs keep the session deadline."""
    legs = []
    for leg in plan.legs:
        if leg.is_swing:
            until = clock.leg_valid_until("us", slot)
            if leg.kind == "open":
                until = min(until, slot + SWING_ENTRY_VALID)
            leg = leg.model_copy(update={"session": "us", "valid_until": until})
        legs.append(leg)
    return plan.model_copy(update={"legs": legs})


def swing_daily(ctx: CycleContext, slot: datetime, now: datetime) -> list[str]:
    """Once per US day (the first cycle after it): `paper.settle` on the completed daily bars and
    the SQ-8 paper benchmark step + `record_day`. Missing sources -> a flag, nothing recorded."""
    from council.clock import NEW_YORK

    ledger = ctx.ledger
    day = (slot.astimezone(NEW_YORK) - timedelta(days=1)).date()        # the last completed close
    if ledger.get_runtime(SWING_DAILY_KEY) == day.isoformat():
        return []
    src = getattr(ctx.sources, "swing", None)
    if src is None:
        return []
    flags: list[str] = []
    try:
        from council.swing import paper

        open_rows = ledger.paper_trades(status="open")
        if open_rows and src.daily_bars is not None:
            bars = src.daily_bars(sorted({r["ticker"] for r in open_rows}), day)
            paper.settle(ledger, bars or {})
    except Exception as exc:  # noqa: BLE001 - measurement never stops a cycle
        flags.append(f"paper_settle_error:{type(exc).__name__}")
    try:
        flags += _sq8_day(ctx, src, day)
    except Exception as exc:  # noqa: BLE001
        flags.append(f"sq8_error:{type(exc).__name__}")
    ledger.set_runtime(SWING_DAILY_KEY, day.isoformat(), now=now)
    return flags


def _sq8_day(ctx: CycleContext, src: Any, day: Any) -> list[str]:
    from council.benchmark import sq8
    from council.swing.rules import is_session

    if src.benchmark_returns is None or not is_session(day):
        return []
    rets = src.benchmark_returns(day)
    if rets is None:
        return ["sq8_no_returns"]
    book = sq8.load_book(ctx.state_dir) or sq8.PaperSleeve()
    if book.day is not None and book.day >= day.isoformat():
        return []
    params = sq8.SQ8Params.from_adopted()
    sel = sq8.latest_selection(ctx.state_dir, on_or_before=day.isoformat())
    selection = sel[1] if sel is not None and tuple(sel[1]) != tuple(book.selection) else None
    first = book.day is None
    result = sq8.step(book, params, day.isoformat(), rets, selection=selection,
                      selection_asof=sel[0] if selection is not None and sel is not None else None)
    legs = src.matched_legs(day) if src.matched_legs is not None else None
    matched = sq8.matched_index_day(legs) if legs is not None else None
    hold = sq8.index_hold_day(rets.get("SPX"), first_day=first)
    sq8.record_day(ctx.ledger, sq8.benchmark_row(book, result, matched_idx_ret=matched, idx_hold_ret=hold))
    sq8.save_book(ctx.state_dir, book)
    return []
