"""The 4-hour council cycle. Runs unattended (launchd) with a READ-ONLY broker token, or on demand.

Order of operations (each step fails closed):
  preflight (lock, slot, invariants, disk) → broker snapshot + NAV/kill (if connected) → history →
  fact pack (percent-only, available_at ≤ slot) → reference book → officers (vol/event cards) →
  bands → council (analysts, debate, PM ×3, audit, medoid) → risk engine → plan (if connected) →
  ledger → seal + publish → proposal → notify.

This module never imports the broker writer and never places an order: a proposal only becomes an
order when the human operator approves it in the operator terminal (council.operator.approve).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from council import clock
from council.invariants import check_policy
from council.models.cycle import CycleRecord, Debate
from council.models.risk import Band, RiskDecision, changed_lines
from council.policy import LineSpec
from council.runtime import (
    CycleContext,
    LockBusy,
    cost_hints,
    disk_ok,
    engine_quotes,
    first_cycle_of_utc_day,
    floor_cost_quotes,
    instance_lock,
    material_fingerprint,
    window,
)


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

    # ---- broker snapshot, NAV and kill switch (only once the Agent Portfolio is connected)
    snapshot, kill_state, nav = None, "NORMAL", None
    if ctx.sources.broker is not None:
        snapshot, kill_state, nav = _snapshot_and_kill(ctx, now)

    # ---- history → states → pack
    history, hist_flags = ctx.sources.history(slot)
    flags += hist_flags
    raw_states = market_states(policy, history, now=slot)
    returns = returns_matrix(history)
    ev_start, ev_end = window(slot)
    events, ev_flags = ctx.sources.events(ev_start, ev_end)
    flags += ev_flags
    news = ctx.sources.news(slot) if ctx.sources.news is not None else []
    run_macro = first_cycle_of_utc_day(ledger.get_runtime("last_macro_day"), slot)
    macro: dict[str, Any] = {}
    if ctx.sources.macro is not None:
        macro, m_flags = ctx.sources.macro(slot)
        flags += m_flags
    quotes = floor_cost_quotes(policy, quoted_at=slot)
    cost_facts = _cost_facts(quotes, slot)
    pack = build_fact_pack(cycle_id=cycle_id, slot=slot, now=now, policy=policy, states=raw_states,
                           news=news, events=events, macro=macro, cost_facts=cost_facts,
                           quality_flags=flags)
    states = pack.states

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
        reference=ref, bands=bands, cards=list(code_cards), flags=list(flags) + ref_flags,
    )
    rec.model_digest = await _digest(ctx)
    basis, levels, raw = await _council(ctx, rec, pack=pack, ref=ref, bands=bands, bands_fn=bands_fn,
                                   code_cards=code_cards, current_levels=current_levels,
                                   quotes=quotes, run_macro=run_macro, kill_state=kill_state,
                                   ref_levels=ref_levels, now=now, started=started)
    if run_macro and ctx.sources.macro is not None:
        ledger.set_runtime("last_macro_day", slot.date().isoformat())

    # ---- risk engine
    fp = material_fingerprint(pack, rec.cards, kill_state)
    rec.material_fingerprint = fp
    material_changed = fp != ledger.get_runtime("last_material_fingerprint")
    decision = _evaluate(ctx, rec, levels=levels, ref_levels=ref_levels, bands=rec.bands or bands,
                         states=states, snapshot=snapshot, unit=unit, kill_state=kill_state,
                         quotes=quotes, pack=pack, material_changed=material_changed, basis=basis,
                         slot=slot, returns=returns, nav=nav)
    rec.risk = decision

    # ---- plan (connected account only)
    plan = None
    if snapshot is not None and ctx.sources.broker is not None:
        try:
            plan = _plan(ctx, decision, snapshot=snapshot, states=states, kill_state=kill_state)
        except Exception as exc:  # planning failure never trades; it is published as a flag
            import logging

            logging.getLogger("council.cycle").exception("planning failed")
            rec.flags.append(f"plan_failed:{type(exc).__name__}")
    rec.plan = plan

    # ---- ledger + decision
    rec.finished_at = ctx.clock()
    decision_id = None
    if plan is not None and plan.legs:
        kind = "flatten" if kill_state in ("HALTED", "FLAT") else ("compliance" if decision.compliance and not _discretionary(decision) else "rebalance")
        decision_id = f"{cycle_id}-{kind}-{uuid.uuid4().hex[:6]}"
        ledger.create_decision(
            decision_id=decision_id, kind=kind, valid_until=clock.proposal_valid_until(slot),
            cycle_id=cycle_id,
            target={"final_w": decision.final_w, "base_w": decision.base_w,
                    "nav_usd": snapshot.equity_usd if snapshot else None},
            plan=plan, state="awaiting_publication", now=now)
        ledger.insert_legs(decision_id, plan.legs)
        rec.decision_id, rec.decision_state = decision_id, "awaiting_publication"
    else:
        rec.decision_state = "reviewed_no_action"
    ledger.record_cycle(rec)
    for call in rec.calls:
        ledger.record_role_call(cycle_id, call)
    _write_transcript(ctx, cycle_id, raw)

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
            if material_changed:
                ledger.set_runtime("last_material_fingerprint", fp)
            _notify_proposal(ctx, rec, plan, urgent=kill_state in ("HALTED", "FLAT"))
        else:
            rec.flags.append("publish_failed_risk_increasing_held")
    ledger.set_runtime("last_cycle", {"cycle_id": cycle_id, "at": ctx.clock().isoformat()})
    return CycleOutcome(cycle_id=cycle_id, status=rec.status, basis=decision.basis, decision_id=decision_id,
                        decision_state=state, legs=len(plan.legs) if plan else 0, commit_sha=sha,
                        published=published, flags=rec.flags)


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
    kd = killswitch.evaluate(nav=nav, equity_reads=reads, prev_state=prev,
                             has_positions=bool(snapshot.positions), policy=policy)
    ledger.set_runtime("kill_state", kd.state)
    ledger.add_equity_mark(now, snapshot.equity_usd, credit_usd=snapshot.credit_usd, source="cycle")
    return snapshot, kd.state, nav


# -------------------------------------------------------------------------------- council
async def _council(ctx: CycleContext, rec: CycleRecord, *, pack, ref, bands, bands_fn, code_cards,
                   current_levels, quotes, run_macro, kill_state, ref_levels, now, started) -> tuple[str, dict[str, float], dict[str, str]]:
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
                        **_council_extras(code_cards, bands_fn, call_log)),
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
    rec.desk_sha = result.desk_sha
    rec.flags += list(result.flags)
    if getattr(result, "bands", None):
        rec.bands = dict(result.bands)
    if result.enforce_notes:
        rec.extras["enforce_notes"] = result.enforce_notes
    raw = dict(getattr(result, "raw", {}) or {})   # PRIVATE: written gzipped to state_dir only
    return result.basis, dict(result.aggregate.levels), raw


def _council_extras(code_cards, bands_fn, call_log: list | None = None) -> dict[str, Any]:
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
    return out


# ------------------------------------------------------------------------------- risk
def _evaluate(ctx: CycleContext, rec: CycleRecord, *, levels, ref_levels, bands, states, snapshot, unit,
              kill_state, quotes, pack, material_changed, basis, slot, returns, nav) -> RiskDecision:
    from council.risk.engine import RiskEngine

    ledger, policy = ctx.ledger, ctx.policy
    last_change = ledger.last_changes()
    turnover_7d = ledger.turnover_since(slot - timedelta(days=7), kinds=("rebalance",))
    turnover_30d = ledger.turnover_since(slot - timedelta(days=30), kinds=("rebalance",))
    cost_30d = ledger.cost_bps_since(slot - timedelta(days=30), kinds=("rebalance",))
    stop_hits = ledger.stop_hits_since(slot - timedelta(days=14), universe=policy.universe)
    vol_fn = _vol_fn(policy, returns, states)
    return RiskEngine(policy).evaluate(
        levels=levels, ref=ref_levels, bands=bands, states=states, snapshot=snapshot,
        unit_weights=unit, kill_state=kill_state, cost_quotes=engine_quotes(quotes), events=pack.events,
        last_change=last_change, turnover_7d=float(turnover_7d), material_changed=material_changed,
        basis=basis, now=slot, ex_ante_vol_fn=vol_fn,
        turnover_30d=float(turnover_30d), cost_30d_bps=float(cost_30d),
        stop_hits=stop_hits, blockers=ledger.blockers(),
        nav_drawdown=nav.drawdown if nav is not None else None,
    )


def _vol_fn(policy, returns, states):
    try:
        from council.reference.book import book_covariance, vol_fn
    except ImportError:
        return None
    cov = book_covariance(returns, list(policy.universe.lines), states, policy)
    return vol_fn(cov)


# ------------------------------------------------------------------------------- plan
def _plan(ctx: CycleContext, decision: RiskDecision, *, snapshot, states, kill_state):
    from council.broker.eligibility import resolve_vehicle
    from council.broker.instruments import InstrumentMap
    from council.broker.parsing import parse_rates
    from council.execution.planner import build_plan
    from council.risk.costs import carry_bps_day, per_side_bps
    from council.risk.stops import catastrophe_stop_distance
    from council.runtime import vehicle_asset_class

    policy, broker = ctx.policy, ctx.sources.broker
    by_line = policy.universe.by_symbol()
    imap = InstrumentMap.load(ctx.state_dir / "instruments.json")
    ids = imap.ids()
    rows = broker.eligibility(instrument_ids=sorted(ids.values()))  # type: ignore[union-attr]
    rows_by_symbol = {r.symbol: r for r in rows}
    quotes = parse_rates(broker.rates(sorted(ids.values())), imap.symbol_for)  # type: ignore[union-attr]
    nav_usd = snapshot.equity_usd

    if kill_state in ("HALTED", "FLAT"):
        from council.execution.planner import build_flatten_plan

        return build_flatten_plan(snapshot=snapshot, quotes=quotes, eligibility=rows_by_symbol,
                                  nav_usd=nav_usd, policy=policy, symbol_for=imap.symbol_for)

    def expected_cost(vehicle, row, config):
        cls = vehicle_asset_class(by_line[_line_of(policy, vehicle.symbol)], vehicle.settlement)
        side = per_side_bps(vehicle.settlement, cls, None, None, policy)
        lev = max(config.leverage_values) if config.leverage_values else 1
        return 2 * side + 20 * carry_bps_day(config.direction, vehicle.settlement, lev, cls, None, policy)

    def vehicle_for(line: str, direction: str, leverage: int):
        return resolve_vehicle(by_line[line], direction, leverage, rows_by_symbol, expected_cost)

    settlement_of = {v.symbol: v.settlement for ln in by_line.values()
                     for v in (*ln.vehicles.long, *ln.vehicles.short)}

    def cost_bps(line: str, vehicle, direction: str, leverage: int):
        symbol = vehicle if isinstance(vehicle, str) else vehicle.symbol
        settlement = settlement_of.get(symbol, "cfd") if leverage == 1 and direction == "long" else "cfd"
        cls = vehicle_asset_class(by_line[line], settlement)
        return (per_side_bps(settlement, cls, None, None, policy),
                carry_bps_day(direction, settlement, leverage, cls, None, policy))

    changed = changed_lines(decision)
    target = {s: decision.final_w.get(s, 0.0) for s in changed}
    stop = {s: catastrophe_stop_distance(states[s], by_line[s], policy) for s in changed if s in states}
    # leverage 2 only for the leverage extension (|level| > 1, cost-gated in the bands)
    lev = {s: (2 if abs(decision.banded_levels.get(s, 0.0)) > 1.0 + 1e-9 else 1) for s in changed}
    return build_plan(snapshot=snapshot, target_w=target, vehicle_for=vehicle_for, quotes=quotes,
                      stop_distance=stop, leverage_for=lev, eligibility=rows_by_symbol,
                      cost_bps=cost_bps, nav_usd=nav_usd, policy=policy)


def _line_of(policy, vehicle_symbol: str) -> str:
    from council.execution.planner import vehicle_to_line

    return vehicle_to_line(policy.universe)[vehicle_symbol]


# ----------------------------------------------------------------------------- publish
def _write_transcript(ctx: CycleContext, cycle_id: str, raw: dict[str, str]) -> None:
    """Private, gzipped: raw model output never reaches the ledger record, journal/ or the site."""
    d = ctx.state_dir / "transcripts"
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / f"{cycle_id}.json.gz", "wt") as fh:
        json.dump({"cycle_id": cycle_id, "raw": raw}, fh)


def _seal_and_publish(ctx: CycleContext, rec: CycleRecord, pack, *, reveal_now: bool, snapshot) -> tuple[bool, str | None]:
    """Seal the public cycle document, store the exact sealed bytes + salt privately, and publish
    the commitment (plus the reveal when there is nothing to hide, i.e. no proposal)."""
    from council.publish import commit_reveal, journal, redact

    if ctx.publisher is None:
        return False, None
    lines = ctx.policy.universe
    public = redact.public_cycle(rec, pack, lines=lines)
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


def _notify_proposal(ctx: CycleContext, rec: CycleRecord, plan, *, urgent: bool) -> None:
    if ctx.notifier is None or plan is None or rec.risk is None:
        return
    risk = rec.risk
    body = (f"{len(plan.legs)} legs, gross {plan.gross_before:.2f}x → {plan.gross_after:.2f}x, "
            f"cost {plan.cost_bps_nav:.1f} bp, valid until "
            f"{clock.proposal_valid_until(rec.slot).strftime('%H:%MZ')} (basis {risk.basis})")
    try:
        ctx.notifier.send(f"council {rec.cycle_id}", body, priority="urgent" if urgent else "default")
    except Exception as exc:
        rec.flags.append(f"notify_error:{type(exc).__name__}")
