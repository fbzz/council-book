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
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from council import clock
from council.invariants import check_policy
from council.models.cycle import CycleRecord, Debate
from council.models.plan import Plan
from council.models.risk import Band, RiskDecision, changed_lines
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


def _bare_record(ctx: CycleContext, info: clock.SlotInfo, now: datetime, *, status: str) -> CycleRecord:
    return CycleRecord(
        cycle_id=info.cycle_id, slot=info.slot, started_at=now, finished_at=now, status=status,  # type: ignore[arg-type]
        late_by_s=int(info.late_by.total_seconds()), mode=ctx.settings.mode, policy_sha=ctx.policy.sha256,
        model=ctx.settings.ollama_model,
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
    if ctx.sources.broker is not None:
        _settle_held_orders(ctx, now)       # before the snapshot, so a filled hold is not counted twice
        snapshot, kill_state, nav = _snapshot_and_kill(ctx, now)
        corporate_blockers = corporate_actions(ctx, snapshot, cycle_id)
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
        reference=ref, bands=bands, cards=list(code_cards), flags=list(flags) + ref_flags + news_flags,
    )
    if snapshot is not None:            # connected: say when the fee uses an assumed mirror ratio
        rec.flags += [f for f in econ.flags if f.startswith("mirror_ratio")]
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

    # ---- risk engine
    fps, material_changed = _material(ledger, pack, rec.cards, kill_state)
    rec.material_fingerprint = fingerprints_digest(fps)
    decision = _evaluate(ctx, rec, levels=levels, ref_levels=ref_levels, bands=rec.bands or bands,
                         states=states, snapshot=snapshot, unit=unit, kill_state=kill_state,
                         quotes=quotes, pack=pack, material_changed=material_changed, basis=basis,
                         slot=slot, returns=returns, nav=nav, econ=econ, extra_blockers=corporate_blockers)
    rec.risk = decision

    # ---- plan (connected account only)
    plan = None
    if snapshot is not None and ctx.sources.broker is not None:
        try:
            plan = _plan(ctx, decision, snapshot=snapshot, states=states, kill_state=kill_state,
                         ref_levels=ref_levels, unit=unit, econ=econ, history=history)
            if plan is not None:
                plan = stamp_sessions(plan, policy.universe, asof=slot)
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
        decision_id = f"{cycle_id}-{kind}-{uuid.uuid4().hex[:6]}"
        valid_until = decision_valid_until(plan, kind, slot)
        ledger.create_decision(
            decision_id=decision_id, kind=kind, valid_until=valid_until,
            cycle_id=cycle_id,
            target={"final_w": decision.final_w, "base_w": decision.base_w,
                    "nav_usd": snapshot.equity_usd if snapshot else None},
            plan=plan, state="awaiting_publication", now=now, policy_sha=policy.sha256)
        ledger.insert_legs(decision_id, plan.legs)
        rec.decision_id, rec.decision_state = decision_id, "awaiting_publication"
    else:
        rec.decision_state = "reviewed_no_action"
    rec.flags += record_trail(rec, pack, code_bands=bands, material_changed=material_changed, lines=lines)
    ledger.record_cycle(rec)
    for call in rec.calls:
        ledger.record_role_call(cycle_id, call)

    # ---- seal + publish
    published, sha = _seal_and_publish(ctx, rec, pack, reveal_now=decision_id is None, snapshot=snapshot)
    state = rec.decision_state
    if decision_id is not None:
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
    from council.ledger.states import SATELLITE_BLOCKER_PREFIX

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
    snapshot = snapshot_from_pnl(payload, vehicle_by_instrument=imap.symbols_by_id(),
                                 line_by_vehicle=vehicle_to_line(policy.universe), now=now)
    raw_nav = ledger.get_runtime("nav_state")
    nav = update_nav(NavState.model_validate(raw_nav) if raw_nav else None, snapshot.equity_usd, now)
    ledger.set_runtime("nav_state", nav.model_dump(mode="json"))
    reads = [(datetime.fromisoformat(t), float(e)) for t, e in ledger.get_runtime("equity_reads", [])]
    reads = [r for r in reads if now - r[0] <= timedelta(hours=6)][-20:] + [(now, snapshot.equity_usd)]
    ledger.set_runtime("equity_reads", [[t.isoformat(), e] for t, e in reads])
    prev = ledger.get_runtime("kill_state", "NORMAL")
    real_peak = ledger.get_runtime(REAL_PEAK_KEY)
    kd = killswitch.evaluate(nav=nav, equity_reads=reads, prev_state=prev,
                             has_positions=bool(snapshot.positions), policy=policy,
                             real_drag=ledger.real_fee_drag(),       # D19: the real account's fee drag
                             real_peak=float(real_peak) if isinstance(real_peak, int | float) else None)
    if kd.real_peak is not None:        # PRIVATE: lifetime peak of the real-adjusted equity
        ledger.set_runtime(REAL_PEAK_KEY, kd.real_peak)
    ledger.set_runtime("kill_state", kd.state)
    ledger.add_equity_mark(now, snapshot.equity_usd, credit_usd=snapshot.credit_usd, source="cycle")
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
              econ=None, extra_blockers=()) -> RiskDecision:
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
        cost_30d_fee_bps=float(fee_30d),
    )


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
          ref_levels=None, unit=None, econ=None, history=None):
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
    rows = broker.eligibility(instrument_ids=ids)  # type: ignore[union-attr]
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
        return resolve_vehicle(by_line[line], direction, leverage, rows_by_symbol, expected_cost)

    settlement_of = {v.symbol: v.settlement for ln in by_line.values()
                     for v in (*ln.vehicles.long, *ln.vehicles.short)}

    fee = econ.fee_nav_bps if econ is not None else 0.0

    def cost_bps(line: str, vehicle, direction: str, leverage: int):
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
                      gap_ref=gap_references(policy, changed, states, history))


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


def _seal_and_publish(ctx: CycleContext, rec: CycleRecord, pack, *, reveal_now: bool, snapshot) -> tuple[bool, str | None]:
    """Seal the public cycle document, store the exact sealed bytes + salt privately, and publish
    the commitment (plus the reveal when there is nothing to hide, i.e. no proposal)."""
    from council.publish import commit_reveal, journal, redact

    if ctx.publisher is None:
        return False, None
    lines = ctx.policy.universe
    public = redact.public_cycle(rec, pack, lines=lines, install_key=cycle_install_key(ctx, rec))
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
    files.update(journal.status_files(redact.public_status(
        state, last_cycle_id=rec.cycle_id, last_cycle_at=rec.slot, kill_state=rec.kill_state)))
    if snapshot is not None and rec.risk is not None:
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
    files = journal.ops_files(_existing(ctx, journal.OPS_PATH), [redact.public_ops_row(rec)])
    try:
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
