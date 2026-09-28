"""Onboarding smoke tickets (m5-readiness §8, M5-D2): `council smoke propose <step> [--preview]`,
`council smoke verify <decision>` and `council smoke status`.

Rules (every one is tested):
- Operator only. Each command runs the operator guards first (COUNCIL_ROLE=operator, both TTYs, no
  agent / CI variable or ancestor); the CLI also release-pins them. They read the broker with the
  READ token only. This module imports no broker writer at load time; `--preview` imports only the
  writer's pure body builders, inside the operator terminal, to print the exact request.
- A smoke ticket is a decision of kind `smoke`, id `<UTC minute>-smoke-<step>` (never matches the
  cycle-id pattern), priority 0 under the normal supersede rule: any other decision (a flatten
  first) supersedes a pending smoke ticket; a smoke ticket supersedes nothing. It is created
  `awaiting_publication`; the watch publishes its weightless ops row (`publish.smoke_row`) and only
  then moves it to `proposed`. Agents can never execute it: it goes through `council approve` like
  any proposal, with every re-check (published commitment, drift, price guard, hard gross,
  market-hours drops); only the policy-SHA check is exempt, as for flatten.
- `propose` refuses when: another decision is pending or in flight; a blocker exists; the kill switch
  is not NORMAL (a close / partial close also runs under WARN / RESUMED); a council launchd job is loaded; the step's market is closed; its prerequisite is
  missing (S2/S3/S4 need S1's position open with a stop-loss; S5x/S6x/... their open's position);
  the step's own position is already open.
- Size (opens): max(broker minimum, 1.2 x the real copy floor) as exposure, in units rounded UP
  (whole units when the instrument requires them). A step whose position a later step partially
  closes (S1, S7) is sized twice that, so both halves clear the minimum and the floor. Above 1% of
  NAV the step is refused (`smoke_min_above_cap`); the cap is never raised and the capability that
  step proves stays false.
- Legs come from `execution.planner.build_smoke_plan` (the planner's own leg and stop-loss
  construction). The private plan is committed as sha256(salt ‖ canonical plan); the salt and the
  canonical bytes stay in `state_dir/salts/smoke/<id>.json` (0600; a subdirectory, so the cycle
  reveal loop never reads it). Nothing about the plan is published.
- `verify` checks a completed ticket automatically: every sent leg terminal within 60 s; units
  sent = units filled; the stop-loss present at the requested rate (± SL_TOLERANCE); `totalFees`
  seen on the agent position (`fee_virtual_seen`, reported); reconcile drift 0 (each position the
  ticket leaves has the expected units and stop, each closed one is gone). All green → the step's
  capability is written to `capabilities.json` (it still needs the mirror attestations); the manual
  checklist is printed either way.
- `status` lists the steps, the tickets, their states and whether a smoke ticket is pending or a
  smoke position is open (K20), and the K14 / K20 / S3 codes.
Output is value-free: codes, steps, symbols and percentages of NAV; never an amount, an id of the
broker or a token.

Swing-book steps (swing-book.md rev 2, SW-5; Track S, operator-approved like S1-S6):
- S7 opens a REAL stock long with its stop-loss AND a take-profit in the open body. All green proves
  `stock_fractional` and `stock_real_long`; the take-profit is checked separately on the live
  position: kept -> `tp_on_open` and `tp_min_pct` (the design's `tp_on_open_or_patch`); not kept ->
  reported, and the optional S7t PATCHes the take-profit (resending the stop), proving `tp_min_pct`
  (the planner then uses the ledgered `modify_tp` leg). S7p partially closes, S7x fully closes and
  proves `closed_trade_route` when the closed-trade record is readable with its close rate.
- S8 opens a 1x stock CFD SHORT with a stop-loss and a take-profit (`stock_cfd_short`,
  `cfd_short_mirror`: the mirror attestations of S8 are the mirror's short behaviour). S8x closes it
  after at least one overnight (refused on the same US session) and proves `stock_short_carry` when
  the closed-trade record reports the carry fee (its sign is then known) and `closed_trade_route`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from council import clock
from council.clock import utcnow

SMOKE_KIND = "smoke"
STEP_KEY = "smoke_step"                  # = operator.capabilities.SMOKE_STEP_KEY
SIZE_FLOOR_MULTIPLE = 1.2                # size = max(broker minimum, 1.2 x real copy floor)
NAV_CAP = 0.01                           # a smoke position never exceeds 1% of NAV
TERMINAL_WITHIN = timedelta(seconds=60)
SL_TOLERANCE = 0.005                     # relative tolerance of the stop-loss rate
UNITS_TOLERANCE = 1e-6                   # relative tolerance of units sent vs filled
SL_TIGHTEN = 0.8                         # S2 moves the stop to 80% of its current distance
ROWS_PUBLISHED = "smoke_rows_published"  # ledger runtime: {decision id: public state} (the watch)
SALTS_REL = ("salts", "smoke")
LIVE_JOBS = ("com.fbzz.council.cycle", "com.fbzz.council.watch")
REDUCING_ACTIONS = ("close", "partial_close")
IN_FLIGHT = ("approved", "executing", "execution_unknown", "waiting_for_market")
MANUAL_CHECKS = (
    "main account: the mirror copied the trade        -> council-op ops attest mirror-copied --decision {id}",
    "main account: the mirror's stop-loss is the same  -> council-op ops attest mirror-sl-equal --decision {id}",
    "main account: where the $1 fee was charged        -> council-op ops attest fee-charged-on=<levels>",
)


class SmokeRefused(RuntimeError):
    """A smoke command refused; the message starts with a fixed code."""


@dataclass(frozen=True)
class Step:
    code: str
    action: str                          # open | modify_sl | partial_close | close
    session: str                         # the step's market session (lse, us, crypto, fx24x5)
    candidates: tuple[str, ...] = ()     # opens: vehicle symbols, the cheapest resolved one wins
    settlement: str = "real"
    direction: str = "long"
    leverage: int = 1
    stop_distance: float = 0.10          # opens: requested stop distance (fitted to broker bounds)
    host: str | None = None              # the open step whose position this step acts on
    split: int = 1                       # 2 when a later step partially closes the position
    proves: str | None = None            # the capability verify writes
    optional: bool = False
    track: str = "C"                     # C = token day, S = Track S day
    tp_distance: float | None = None     # opens / set_tp: a take-profit this far from the price
    also_proves: tuple[str, ...] = ()    # further capabilities (each with its own check, see verify)
    overnight: bool = False              # closes: refused on the host open's US session

    @property
    def is_open(self) -> bool:
        return self.action == "open"


_UCITS = ("SGLN.L", "CNDX.L")
_STOCKS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL")
STEPS: dict[str, Step] = {s.code: s for s in (
    Step("S1", "open", "lse", _UCITS, stop_distance=0.10, split=2, proves="real_etf"),
    Step("S2", "modify_sl", "lse", host="S1", proves="sl_modify"),
    Step("S3", "partial_close", "lse", host="S1", proves="partial_close"),
    Step("S4", "close", "lse", host="S1"),
    Step("S5", "open", "crypto", ("BTC",), stop_distance=0.25, proves="crypto_real"),
    Step("S5x", "close", "crypto", host="S5"),
    Step("S6", "open", "fx24x5", ("EURUSD",), settlement="cfd", direction="short", stop_distance=0.03,
         proves="cfd_short"),
    Step("S6x", "close", "fx24x5", host="S6"),
    Step("S6a", "open", "fx24x5", ("EURUSD",), settlement="cfd", stop_distance=0.03, proves="cfd_long",
         optional=True),
    Step("S6ax", "close", "fx24x5", host="S6a", optional=True),
    Step("S6b", "open", "fx24x5", ("EURUSD",), settlement="cfd", leverage=2, stop_distance=0.03,
         proves="cfd_leverage", optional=True),
    Step("S6bx", "close", "fx24x5", host="S6b", optional=True),
    Step("S7", "open", "us", _STOCKS, stop_distance=0.10, split=2, proves="stock_fractional", track="S",
         tp_distance=0.25, also_proves=("stock_real_long", "tp_on_open", "tp_min_pct")),
    Step("S7t", "set_tp", "us", host="S7", track="S", tp_distance=0.25, proves="tp_min_pct", optional=True),
    Step("S7p", "partial_close", "us", host="S7", track="S"),
    Step("S7x", "close", "us", host="S7", track="S", proves="closed_trade_route"),
    Step("S8", "open", "us", _STOCKS, settlement="cfd", direction="short", stop_distance=0.06, track="S",
         tp_distance=0.15, proves="stock_cfd_short", also_proves=("cfd_short_mirror",)),
    Step("S8x", "close", "us", host="S8", track="S", overnight=True, proves="stock_short_carry",
         also_proves=("closed_trade_route",)),
)}
# capabilities a step writes only when their own (non-blocking) check reports them seen
CONDITIONAL_PROOF = {"tp_on_open": "tp_on_open_kept", "closed_trade_route": "closed_trade_record_seen",
                     "stock_short_carry": "short_carry_seen"}
S7_TP_CHECK = "tp_on_open_kept"
TOKEN_DAY_STEPS = tuple(c for c, s in STEPS.items() if s.track == "C" and not s.optional)


@dataclass
class SmokeDeps:
    ledger: Any
    policy: Any
    read: Any                                          # EtoroReadClient (READ token); None for status
    state_dir: Path
    print_fn: Callable[[str], None] = print
    now_fn: Callable[[], datetime] = utcnow
    guard_fn: Callable[[], None] | None = None         # default: the real operator guards
    jobs_loaded: Callable[[], set[str]] | None = None  # default: `launchctl list` (read-only)
    release_fn: Callable[[], None] | None = None       # capabilities.json release check (tests inject)


@dataclass
class Ticket:
    step: str
    decision_id: str | None
    plan: Any
    requests: list[dict[str, Any]] = field(default_factory=list)
    nav_share: float = 0.0
    base_w: dict[str, float] = field(default_factory=dict)


# ================================================================================ guards
def run_guards(deps: SmokeDeps) -> None:
    if deps.guard_fn is not None:
        deps.guard_fn()
        return
    from council.operator import guards

    guards.assert_current_process_is_operator()


def _jobs_loaded(deps: SmokeDeps) -> set[str]:
    if deps.jobs_loaded is not None:
        return set(deps.jobs_loaded())
    from council.operator import readiness

    return set(readiness.Probes.default().launchd_loaded())


def step_of(code: str) -> Step:
    step = STEPS.get(code)
    if step is None:
        raise SmokeRefused(f"unknown_step: {code} (one of {', '.join(STEPS)})")
    return step


def smoke_id(now: datetime, step: str) -> str:
    return f"{clock._as_utc(now):%Y-%m-%dT%H%M}Z-smoke-{step}"


def smoke_decisions(ledger: Any, limit: int = 1000) -> list[Any]:
    return [d for d in ledger.decisions(limit=limit) if d.kind == SMOKE_KIND]


def refusals(deps: SmokeDeps, step: Step) -> list[str]:
    """Every ledger / machine reason `propose` refuses (empty = may proceed to the broker reads)."""
    ledger = deps.ledger
    out: list[str] = []
    ledger.expire_stale(deps.now_fn())
    pending = [d.decision_id for d in ledger.pending()]
    if pending:
        out.append(f"decision_pending:{pending[0]}")
    busy = [d.decision_id for d in ledger.decisions(states=IN_FLIGHT, limit=50)]
    if busy:
        out.append(f"decision_in_flight:{busy[0]}")
    if ledger.blockers():
        out.append("blocker_present")
    kill = str(ledger.get_runtime("kill_state", "NORMAL"))
    # a close / partial close only reduces: allowed under WARN / RESUMED (else an open smoke
    # position would hold every live proposal with no way out short of a HALT); HALTED / FLAT
    # leave it to the flatten
    reducing = step.action in REDUCING_ACTIONS and kill not in ("HALTED", "FLAT")
    if kill != "NORMAL" and not reducing:
        out.append(f"kill_switch_{kill.lower()}")
    if set(LIVE_JOBS) & _jobs_loaded(deps):
        out.append("launchd_jobs_loaded")
    return out


# ================================================================================ positions
def step_positions(ledger: Any) -> dict[str, list[tuple[int, str]]]:
    """{open step: [(position id, decision id)]} of the smoke positions still open (ledger view)."""
    by_decision: dict[str, str] = {}
    for d in smoke_decisions(ledger):
        by_decision[d.decision_id] = str((d.target or {}).get(STEP_KEY, ""))
    out: dict[str, list[tuple[int, str]]] = {}
    for pid, decision_id in ledger.smoke_positions().items():
        out.setdefault(by_decision.get(decision_id, ""), []).append((pid, decision_id))
    return out


def _host_position(deps: SmokeDeps, step: Step, snapshot: Any) -> Any:
    assert step.host is not None
    live = {p.position_id: p for p in snapshot.positions}
    held = [live[pid] for pid, _d in step_positions(deps.ledger).get(step.host, []) if pid in live]
    if len(held) != 1:
        raise SmokeRefused(f"prerequisite_missing: {step.code} needs {step.host}'s position open "
                           f"({len(held)} found)")
    pos = held[0]
    if step.action != "close" and (not pos.sl_rate or pos.sl_rate <= 0):   # a close never needs one
        raise SmokeRefused(f"prerequisite_missing: {step.host}'s position has no stop-loss")
    return pos


# ================================================================================ broker reads
@dataclass
class _Reads:
    snapshot: Any
    nav: float
    rows: dict[str, Any]                 # symbol -> EligibilityRow
    raw: dict[str, Mapping[str, Any]]    # symbol -> raw eligibility row (currency, price unit)
    quotes: dict[str, Any]
    found: dict[str, int]


def _read(deps: SmokeDeps, symbols: Sequence[str], held_ids: Sequence[int], now: datetime) -> _Reads:
    from council.broker.instruments import InstrumentMap
    from council.broker.parsing import parse_rates
    from council.operator.onboarding import eligibility_batch

    if deps.read is None:
        raise SmokeRefused("no_read_client: the READ token is needed")
    imap = InstrumentMap.load(deps.state_dir / "instruments.json")
    snapshot = swing_aware_snapshot(deps, imap, deps.read.pnl(), now=now)
    nav = float(snapshot.equity_usd)
    if not (math.isfinite(nav) and nav > 0):
        raise SmokeRefused("nav_unavailable")
    batch = eligibility_batch(deps.read, list(symbols), held_ids, now=now)
    rows = {row.symbol: row for row in batch.rows.values()}
    raw = {str(r.get("symbol")): r for r in batch.raw_by_symbol.values()}
    found = {row.symbol: row.instrument_id for row in rows.values()}
    by_id = {iid: sym for sym, iid in found.items()}
    quotes = parse_rates(deps.read.rates(sorted(set(found.values()))), by_id.get) if found else {}
    return _Reads(snapshot, nav, rows, raw, quotes, found)


def swing_aware_snapshot(deps: SmokeDeps, imap: Any, payload: Any, *, now: datetime) -> Any:
    """The READ snapshot with the runtime swing map merged in (swing-book §1.9): a Track S stock that
    no universe line owns, but a swing trade does, keeps its symbol and line (not UNMAPPED_<id>)."""
    from council.execution.planner import vehicle_to_line
    from council.risk.exposure import snapshot_from_pnl
    from council.swing.book import swing_vehicle_map

    smap = swing_vehicle_map(deps.ledger, deps.policy)
    return snapshot_from_pnl(payload, vehicle_by_instrument=smap.merged_symbols(imap.symbols_by_id()),
                             line_by_vehicle=smap.merged_lines(vehicle_to_line(deps.policy.universe)), now=now)


def _economics(deps: SmokeDeps, nav: float) -> Any:
    from council.runtime import cycle_trade_economics

    return cycle_trade_economics(deps.policy, deps.state_dir, nav)


# ================================================================================ sizing
@dataclass(frozen=True)
class Sized:
    symbol: str
    choice: Any
    units: float
    exposure: float
    price: float


def size_open(step: Step, rows: Mapping[str, Any], quotes: Mapping[str, Any], *, nav: float,
              min_amount_usd: float) -> Sized:
    """The cheapest candidate's minimum-size open (see the module rules). Raises SmokeRefused
    `smoke_min_above_cap` when even the cheapest one exceeds 1% of NAV, or `no_candidate` when none
    resolves with a usable config and quote."""
    from council.broker.eligibility import VehicleChoice, select_config, whole_units_only
    from council.execution.planner import _ceil_units, bid_ask

    best: Sized | None = None
    for symbol in step.candidates:
        row = rows.get(symbol)
        quote = bid_ask(quotes.get(symbol))
        if row is None or quote is None:
            continue
        config = select_config(row, step.direction, step.leverage, step.settlement)  # type: ignore[arg-type]
        if config is None:
            continue
        price = quote[1] if step.direction == "long" else quote[0]
        broker_min = max(float(row.min_position_exposure), float(config.min_position_amount) * step.leverage)
        size = step.split * max(broker_min, SIZE_FLOOR_MULTIPLE * min_amount_usd * step.leverage)
        units = _ceil_units(size / price, whole_units_only(row))
        exposure = units * price
        choice = VehicleChoice(symbol=symbol, instrument_id=row.instrument_id,
                               settlement=config.settlement, leverage=step.leverage, config=config)
        if best is None or exposure < best.exposure:
            best = Sized(symbol, choice, units, exposure, price)
    if best is None:
        raise SmokeRefused(f"no_candidate: none of {', '.join(step.candidates)} resolved with a "
                           f"{step.settlement} {step.direction} x{step.leverage} config and a quote")
    if best.exposure > NAV_CAP * nav * (1 + 1e-9):
        raise SmokeRefused(f"smoke_min_above_cap: {step.code} needs {best.exposure / nav:.2%} of NAV "
                           f"(cap {NAV_CAP:.0%}); the cap is never raised and {step.proves or 'the step'} "
                           "stays unproven")
    return best


# ================================================================================ propose
def build_ticket(step_code: str, deps: SmokeDeps) -> tuple[Ticket, _Reads, Step]:
    """Guards, refusals, fresh reads and the plan (nothing is written)."""
    from council.execution.planner import SmokeIntent, SmokePlanError, build_smoke_plan

    run_guards(deps)
    step = step_of(step_code)
    now = deps.now_fn()
    problems = refusals(deps, step)
    if problems:
        raise SmokeRefused("; ".join(problems))
    closing = step.action in ("close", "partial_close")
    if not clock.session_open(step.session, now, closing=closing) or \
            not clock.session_open(step.session, now + clock.APPROVAL_CLOSE_MARGIN, closing=closing):
        raise SmokeRefused(f"market_closed: {step.code} runs in the {step.session} session")
    held_ids: list[int] = []
    symbols: list[str] = list(step.candidates)
    reads: _Reads
    if step.is_open:
        reads = _read(deps, symbols, held_ids, now)
        live_ids = {p.position_id for p in reads.snapshot.positions}
        if any(pid in live_ids for pid, _d in step_positions(deps.ledger).get(step.code, [])):
            raise SmokeRefused(f"step_position_open: {step.code}'s position is still open")
        econ = _economics(deps, reads.nav)
        sized = size_open(step, reads.rows, reads.quotes, nav=reads.nav, min_amount_usd=econ.min_amount_usd)
        intents = [SmokeIntent("open", f"smoke {step.code}: minimum-size open with a stop-loss",
                               choice=sized.choice, units=sized.units, stop_distance=step.stop_distance)]
    else:
        from council.broker.instruments import InstrumentMap

        if deps.read is None:
            raise SmokeRefused("no_read_client: the READ token is needed")
        imap = InstrumentMap.load(deps.state_dir / "instruments.json")
        snap = swing_aware_snapshot(deps, imap, deps.read.pnl(), now=now)
        pos = _host_position(deps, step, snap)
        if step.overnight:
            _refuse_same_session(deps, step, pos.position_id, now)
        # by its symbol too, so the rows and quotes carry the position's name (a Track S stock that
        # no universe line owns would otherwise come back as UNMAPPED_<id>)
        named = [] if pos.symbol.startswith("UNMAPPED_") else [pos.symbol]
        reads = _read(deps, named, [pos.instrument_id], now)
        econ = _economics(deps, reads.nav)
        live = {p.position_id: p for p in reads.snapshot.positions}
        pos = live.get(pos.position_id, pos)
        if step.action == "set_tp":
            plan = _set_tp_plan(step, pos, reads, deps)
            return _ticket(step, plan, reads, deps, now), reads, step
        intents = [_host_intent(step, pos, reads, econ)]
    try:
        plan = build_smoke_plan(intents=intents, snapshot=reads.snapshot, quotes=reads.quotes,
                                eligibility=reads.rows, nav_usd=reads.nav, policy=deps.policy,
                                economics=econ)
    except SmokePlanError as exc:
        raise SmokeRefused(f"smoke_plan_refused: {exc}") from exc
    if step.is_open and step.tp_distance is not None:
        plan = with_take_profit(plan, step.tp_distance)
    return _ticket(step, plan, reads, deps, now), reads, step


def _ticket(step: Step, plan: Any, reads: _Reads, deps: SmokeDeps, now: datetime) -> Ticket:
    from council.operator.approve import current_book

    by_line = deps.policy.universe.by_symbol()
    legs = []
    for leg in plan.legs:
        spec = by_line.get(leg.line or "")
        session = clock.vehicle_session(spec, leg.symbol) if spec is not None else step.session
        legs.append(leg.model_copy(update={"session": session,
                                           "valid_until": clock.leg_valid_until(session, now)}))
    plan = plan.model_copy(update={"legs": legs})
    exposure = sum(abs(leg.weight_after - leg.weight_before) for leg in plan.legs if leg.kind == "open")
    ticket = Ticket(step.code, None, plan, nav_share=exposure)
    base = current_book(reads.snapshot.signed_w, deps.ledger.pending_open_weights())
    ticket.requests = [request_preview(leg, reads.raw.get(leg.symbol)) for leg in plan.legs]
    ticket.base_w = base
    return ticket


def with_take_profit(plan: Any, distance: float) -> Any:
    """S7 / S8: every open leg carries a take-profit `distance` from its planned price, sent in the
    open body (`tp_mode="body"`); the stop-loss is unchanged."""
    legs = []
    for leg in plan.legs:
        if leg.kind == "open" and leg.units and leg.amount_usd:
            price = leg.amount_usd / leg.units
            rate = price * (1 + distance) if leg.direction == "long" else price * (1 - distance)
            leg = leg.model_copy(update={"tp_rate": rate, "tp_mode": "body"})
        legs.append(leg)
    return plan.model_copy(update={"legs": legs})


def _set_tp_plan(step: Step, pos: Any, reads: _Reads, deps: SmokeDeps) -> Any:
    """S7t: one `set_tp` leg on S7's position: the take-profit by PATCH, the current stop resent."""
    from council.execution.planner import bid_ask, smoke_line
    from council.models.plan import Leg, Plan

    quote = bid_ask(reads.quotes.get(pos.symbol))
    if quote is None:
        raise SmokeRefused(f"no_quote: {pos.symbol}")
    ref = quote[0] if pos.is_buy else quote[1]
    dist = float(step.tp_distance or 0.0)
    rate = ref * (1 + dist) if pos.is_buy else ref * (1 - dist)
    snap = reads.snapshot
    exposure = abs(float(getattr(pos, "exposure_usd", 0.0) or pos.units * ref))
    w = (exposure if pos.is_buy else -exposure) / reads.nav
    line = smoke_line(deps.policy.universe, pos.symbol, pos.instrument_id)
    leg = Leg(seq=1, kind="set_tp", symbol=pos.symbol, line=line, instrument_id=pos.instrument_id,
              direction="long" if pos.is_buy else "short", settlement=pos.settlement, leverage=pos.leverage,
              weight_before=w, weight_after=w, risk_increasing=False, units=pos.units,
              reason=f"smoke {step.code}: take-profit by PATCH on {step.host}'s position (stop resent)",
              position_id=pos.position_id, sl_rate=pos.sl_rate, tp_rate=rate)
    return Plan(legs=[leg], gross_before=snap.gross, gross_after=snap.gross, net_before=snap.net,
                net_after=snap.net, cost_bps_nav=0.0, carry_bps_day_nav=0.0)


def _refuse_same_session(deps: SmokeDeps, step: Step, position_id: int, now: datetime) -> None:
    """S8x: the short must have been held over at least one US overnight."""
    host = dict(step_positions(deps.ledger).get(step.host or "", []))
    decision_id = host.get(position_id)
    opened = None
    if decision_id is not None:
        opened = next((r.resolved_at for r in deps.ledger.legs(decision_id)
                       if r.kind == "open" and r.resolved_at is not None), None)
    if opened is None or opened.astimezone(clock.NEW_YORK).date() >= now.astimezone(clock.NEW_YORK).date():
        raise SmokeRefused(f"held_overnight_missing: {step.code} closes {step.host}'s short only after "
                           "one US overnight")


def _host_intent(step: Step, pos: Any, reads: _Reads, econ: Any) -> Any:
    from council.execution.planner import SmokeIntent, _floor_units, bid_ask

    row = reads.rows.get(pos.symbol)
    if step.action == "close":
        return SmokeIntent("close", f"smoke {step.code}: full close of {step.host}'s position", position=pos)
    if step.action == "modify_sl":
        quote = bid_ask(reads.quotes.get(pos.symbol))
        if quote is None:
            raise SmokeRefused(f"no_quote: {pos.symbol}")
        ref = quote[1] if pos.is_buy else quote[0]
        current = abs(1.0 - float(pos.sl_rate) / ref)
        return SmokeIntent("modify_sl", f"smoke {step.code}: move {step.host}'s stop-loss",
                           position=pos, stop_distance=max(current * SL_TIGHTEN, 1e-4))
    whole = bool(row is not None and "whole" in row.units_quantity_type.lower())
    half = _floor_units(pos.units / 2.0, whole)
    unit_value = (pos.exposure_usd / pos.units) if getattr(pos, "exposure_usd", None) and pos.units else pos.open_rate
    closed, remainder = half * unit_value, (pos.units - half) * unit_value
    min_exposure = float(row.min_position_exposure) if row is not None else 0.0
    if half <= 0 or remainder < min_exposure or closed / max(pos.leverage, 1) < econ.min_amount_usd * (1 - 1e-9):
        raise SmokeRefused("smoke_partial_below_minimum: both halves must clear the broker minimum "
                           "and the real copy floor")
    return SmokeIntent("partial_close", f"smoke {step.code}: partial close of {step.host}'s position",
                       position=pos, units=half)


def request_preview(leg: Any, raw_row: Mapping[str, Any] | None) -> dict[str, Any]:
    """The exact write request the executor would send for this leg (no headers), plus the
    instrument's currency and price unit. Imports only the writer's pure body builders."""
    from council.broker import etoro_write as w
    from council.broker.instruments import unit_of

    currency, unit = unit_of(raw_row) if raw_row else (None, None)
    info = {"leg": leg.seq, "symbol": leg.symbol, "currency": currency or "unknown",
            "price_unit": unit or "unknown", "direction": leg.direction, "leverage": leg.leverage,
            "units": leg.units, "sl_rate": leg.sl_rate, "sl_distance": leg.stop_distance}
    if leg.kind == "open":
        body = w.build_open_body(instrument_id=int(leg.instrument_id), settlement=str(leg.settlement),
                                 transaction="buy" if leg.direction == "long" else "sellShort",
                                 leverage=leg.leverage, units=float(leg.units), stop_loss_rate=leg.sl_rate)
        return {**info, "method": "POST", "path": w.OPEN_ORDER_PATH, "body": body}
    if leg.kind in ("close", "partial_close"):
        body = w.build_close_body(instrument_id=int(leg.instrument_id),
                                  units_to_deduct=leg.units if leg.kind == "partial_close" else None)
        return {**info, "method": "POST", "path": w.CLOSE_POSITION_PATH, "body": body}
    body = w.build_patch_body(stop_loss_rate=float(leg.sl_rate))
    return {**info, "method": "PATCH", "path": w.POSITION_PATH, "body": body}


def canonical_plan(decision_id: str, step: str, plan: Any) -> bytes:
    from council.publish.commit_reveal import canonical_json

    return canonical_json({"decision_id": decision_id, "step": step, "plan": plan.model_dump(mode="json")})


def commitment_of(salt: str, canonical: bytes) -> str:
    return hashlib.sha256(bytes.fromhex(salt) + canonical).hexdigest()


def _save_salt(state_dir: Path, decision_id: str, salt: str, canonical: bytes) -> Path:
    from council import paths

    directory = state_dir.joinpath(*SALTS_REL)
    paths.assert_outside_repo(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{decision_id}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"decision_id": decision_id, "salt": salt, "canonical_hex": canonical.hex()}, fh)
    return path


def propose(step_code: str, deps: SmokeDeps, *, preview: bool = False) -> Ticket:
    """`council smoke propose <step> [--preview]` (see the module rules)."""
    ticket, reads, step = build_ticket(step_code, deps)
    p = deps.print_fn
    p(f"smoke {step.code}: {step.action} · {step.session} session · "
      f"{ticket.nav_share:.3%} of NAV exposure (cap {NAV_CAP:.0%})")
    for req in ticket.requests:
        p(f"  leg {req['leg']}: {req['method']} {req['path']}  {req['symbol']} "
          f"({req['currency']}, {req['price_unit']}) {req['direction']} x{req['leverage']}")
        if preview:
            p("    " + json.dumps(req["body"], sort_keys=True))
            if req.get("sl_distance"):
                p(f"    stop-loss distance {float(req['sl_distance']):.2%}")
    if preview:
        p("preview only: nothing was written")
        return ticket
    from council.broker.instruments import InstrumentMap

    now = deps.now_fn()
    imap = InstrumentMap.load(deps.state_dir / "instruments.json")
    wanted = {leg.symbol: int(leg.instrument_id) for leg in ticket.plan.legs if leg.instrument_id}
    missing = {s: i for s, i in wanted.items() if imap.get(s) != i}
    if missing:                        # append-only: an identity change raises, nothing written
        imap.merged(missing, now).save()
    decision_id = smoke_id(now, step.code)
    salt = secrets.token_hex(16)
    canonical = canonical_plan(decision_id, step.code, ticket.plan)
    commitment = commitment_of(salt, canonical)
    stamps = [leg.valid_until for leg in ticket.plan.legs if leg.valid_until is not None]
    valid_until = min(stamps) if stamps else clock.proposal_valid_until(clock.slot_at_or_before(now))
    _save_salt(deps.state_dir, decision_id, salt, canonical)
    deps.ledger.create_decision(
        decision_id=decision_id, kind=SMOKE_KIND, valid_until=valid_until,
        target={STEP_KEY: step.code, "base_w": ticket.base_w, "decision_ref": decision_id},
        plan=ticket.plan, state="awaiting_publication", commitment_sha=commitment,
        actor="operator", now=now, policy_sha=deps.policy.sha256)
    deps.ledger.insert_legs(decision_id, ticket.plan.legs, now=now)
    ticket.decision_id = decision_id
    p(f"{decision_id}: awaiting publication; the watch publishes its weightless ops row, then "
      f"approve it in this terminal (council-op approve {decision_id}) before {valid_until:%H:%MZ}")
    return ticket


# ================================================================================ verify
@dataclass
class Check:
    code: str
    ok: bool
    detail: str = ""


def _close(a: float | None, b: float | None, rel: float) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= rel * max(abs(float(a)), abs(float(b)), 1e-12)


def _raw_positions(payload: Any) -> dict[int, Mapping[str, Any]]:
    port = payload.get("clientPortfolio", payload) if isinstance(payload, Mapping) else {}
    out: dict[int, Mapping[str, Any]] = {}
    for raw in (port.get("positions") or []) if isinstance(port, Mapping) else []:
        if not isinstance(raw, Mapping):
            continue
        low = {str(k).lower(): v for k, v in raw.items()}
        pid = low.get("positionid")
        if isinstance(pid, int | float) and not isinstance(pid, bool):
            out[int(pid)] = low
    return out


def automatic_checks(decision_id: str, deps: SmokeDeps) -> list[Check]:
    """verify's automatic checks for one smoke decision (READ only)."""
    from council.broker.instruments import InstrumentMap

    ledger = deps.ledger
    d = ledger.get_decision(decision_id)
    if d.kind != SMOKE_KIND:
        raise SmokeRefused(f"not_a_smoke_decision: {decision_id} is {d.kind}")
    checks = [Check("decision_completed", d.state == "completed", d.state)]
    legs = ledger.legs(decision_id)
    sent = [r for r in legs if r.submitted_at is not None]
    checks.append(Check("legs_sent", bool(sent), f"{len(sent)}/{len(legs)}"))
    for r in sent:
        within = r.resolved_at is not None and r.resolved_at - r.submitted_at <= TERMINAL_WITHIN
        checks.append(Check(f"leg{r.seq}_terminal_60s", within, r.state))
    if deps.read is None:
        raise SmokeRefused("no_read_client: the READ token is needed")
    payload = deps.read.pnl()
    imap = InstrumentMap.load(deps.state_dir / "instruments.json")
    snap = swing_aware_snapshot(deps, imap, payload, now=deps.now_fn())
    live = {p.position_id: p for p in snap.positions}
    raw = _raw_positions(payload)
    planned = _planned_legs(d)
    for r in legs:
        if r.kind == "open":
            sent_units = r.detail.get("units_sent")
            filled = r.detail.get("units_filled")
            checks.append(Check(f"leg{r.seq}_units_filled", _close(sent_units, filled, UNITS_TOLERANCE)
                                and r.state == "filled", "units sent vs filled"))
            pid = r.position_ids[0] if r.position_ids else None
            pos = live.get(pid) if pid is not None else None
            checks.append(Check(f"leg{r.seq}_position_id", pid is not None, "fill reported a position"))
            ours = ledger.smoke_positions().get(pid or -1) == decision_id
            # the stop-loss and units can only be verified on the live position: a ticket whose
            # position is gone (stop hit, closed first) never proves its capability
            checks.append(Check(f"leg{r.seq}_position_still_open", ours and pos is not None,
                                "verify right after the fill"))
            if ours:
                checks.append(Check(f"leg{r.seq}_position_present", pos is not None, "reconcile"))
                checks.append(Check(f"leg{r.seq}_sl_at_request", pos is not None
                                    and _close(pos.sl_rate, r.sl_rate, SL_TOLERANCE), "stop-loss rate"))
                if pos is not None:
                    checks.append(Check(f"leg{r.seq}_units_reconciled", _close(pos.units, filled, UNITS_TOLERANCE),
                                        "reconcile drift"))
                fees = raw.get(pid or -1, {}).get("totalfees")
                seen = isinstance(fees, int | float) and not isinstance(fees, bool) and fees != 0
                checks.append(Check("fee_virtual_seen" if seen else "fee_virtual_not_seen", True, "reported"))
                tp = getattr(planned.get(r.seq), "tp_rate", None)
                if tp:                  # S7 / S8: reported; a kept take-profit proves tp_on_open
                    kept = pos is not None and _close(getattr(pos, "tp_rate", None), tp, SL_TOLERANCE)
                    checks.append(Check(S7_TP_CHECK if kept else "tp_on_open_not_kept", True,
                                        "take-profit on the position" if kept else "reported: S7t PATCHes it"))
                    checks.append(Check(f"leg{r.seq}_tp_min_respected", kept or r.state == "filled",
                                        "the open with a take-profit was accepted"))
        elif r.kind == "modify_sl":
            pos = live.get(r.position_id or -1)
            checks.append(Check(f"leg{r.seq}_sl_moved", r.state == "filled" and pos is not None
                                and _close(pos.sl_rate, r.sl_rate, SL_TOLERANCE), "stop-loss rate"))
        elif r.kind == "partial_close":
            pos = live.get(r.position_id or -1)
            before = r.detail.get("position_units_before")
            expect = None if before is None or r.units is None else float(before) - float(r.units)
            checks.append(Check(f"leg{r.seq}_remainder_units", r.state == "filled" and pos is not None
                                and _close(pos.units, expect, UNITS_TOLERANCE), "reconcile drift"))
            checks.append(Check(f"leg{r.seq}_sl_kept", pos is not None and bool(pos.sl_rate), "stop-loss"))
        elif r.kind == "set_tp":
            pos = live.get(r.position_id or -1)
            tp = getattr(planned.get(r.seq), "tp_rate", None)
            checks.append(Check(f"leg{r.seq}_tp_patched", r.state == "filled" and pos is not None
                                and _close(getattr(pos, "tp_rate", None), tp, SL_TOLERANCE), "take-profit rate"))
            checks.append(Check(f"leg{r.seq}_sl_resent", pos is not None
                                and _close(pos.sl_rate, r.sl_rate, SL_TOLERANCE), "stop-loss kept"))
        elif r.kind == "close":
            checks.append(Check(f"leg{r.seq}_flat", r.state == "filled" and (r.position_id not in live),
                                "reconcile drift"))
            checks += _closed_trade_checks(deps, r.position_id)
    return checks


def _planned_legs(d: Any) -> dict[int, Any]:
    """{seq: planned Leg} of the ticket's private plan ({} when unreadable)."""
    from council.models.plan import Plan

    try:
        raw = d.plan_json if hasattr(d, "plan_json") else d.plan
        plan = Plan.model_validate(json.loads(raw) if isinstance(raw, str) else raw)
    except Exception:  # noqa: BLE001 - a display aid: missing plan -> no take-profit checks
        return {}
    return {leg.seq: leg for leg in plan.legs}


def closed_trade_record(read: Any, position_id: int | None) -> Mapping[str, Any] | None:
    """The broker's closed-trade record of a position, through the READ client's `closed_trade`
    route when it has one (not modelled until `closed_trade_route` is proven); None otherwise."""
    fn = getattr(read, "closed_trade", None)
    if fn is None or position_id is None:
        return None
    try:
        rec = fn(position_id)
    except Exception:  # noqa: BLE001 - a read failure is "not seen", never a crash
        return None
    return rec if isinstance(rec, Mapping) else None


def _closed_trade_checks(deps: SmokeDeps, position_id: int | None) -> list[Check]:
    """Reported (non-blocking): the closed-trade record with its close rate (-> closed_trade_route)
    and, for a short held overnight, a numeric carry fee (-> stock_short_carry)."""
    rec = closed_trade_record(deps.read, position_id)
    rate = rec.get("closeRate") if rec else None
    seen = isinstance(rate, int | float) and not isinstance(rate, bool) and rate > 0
    out = [Check("closed_trade_record_seen" if seen else "closed_trade_record_missing", True, "reported")]
    carry = None
    for key in ("overnightFees", "overnightFee", "totalFees"):
        value = (rec or {}).get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
            carry = value
            break
    out.append(Check("short_carry_seen" if carry is not None else "short_carry_missing", True, "reported"))
    return out


def verify(decision_id: str, deps: SmokeDeps) -> list[Check]:
    """`council smoke verify <decision>`: automatic checks, the manual checklist, and (all green)
    the step's capability in capabilities.json."""
    from council.operator import capabilities

    run_guards(deps)
    checks = automatic_checks(decision_id, deps)
    p = deps.print_fn
    for c in checks:
        p(f"{'ok ' if c.ok else 'BAD'}  {c.code}" + (f"  ({c.detail})" if c.detail else ""))
    d = deps.ledger.get_decision(decision_id)
    step = step_of(str((d.target or {}).get(STEP_KEY, "")))
    ok = all(c.ok for c in checks)
    seen = {c.code for c in checks}
    proven = [cap for cap in (step.proves, *step.also_proves) if cap
              and (cap not in CONDITIONAL_PROOF or CONDITIONAL_PROOF[cap] in seen)]
    if step.code == "S7" and S7_TP_CHECK not in seen:
        proven = [cap for cap in proven if cap != "tp_min_pct"]     # S7t proves it by PATCH
    if ok:
        for cap in proven:
            capabilities.write_capability(cap, decision_id=decision_id, step=step.code,
                                          state_dir=deps.state_dir, now=deps.now_fn(),
                                          assert_operator=deps.guard_fn, assert_release=deps.release_fn)
            p(f"capability {cap}: automatic checks recorded; it counts once both mirror checks "
              "are attested")
        for cap in (step.proves, *step.also_proves):
            if cap and cap not in proven:
                p(f"capability {cap}: not seen on this ticket, stays unproven")
    elif not ok:
        p(f"automatic checks failed: {step.proves or step.code} stays unproven")
    p("manual checklist (main account):")
    for line in MANUAL_CHECKS:
        p("  " + line.format(id=decision_id))
    return checks


# ================================================================================ status
@dataclass
class Status:
    rows: list[tuple[str, str, str]]           # (step, decision id or "-", state)
    active: list[str]
    gates: dict[str, dict[str, str]]


def status(deps: SmokeDeps) -> Status:
    """`council smoke status`: every step's latest ticket, K20 (no pending smoke ticket, no open
    smoke position), K14 (S1–S6 capabilities verified and cross-checked) and S3 (S7)."""
    from council.operator import capabilities

    run_guards(deps)
    live = None
    if deps.read is not None:
        from council.broker.instruments import InstrumentMap

        imap = InstrumentMap.load(deps.state_dir / "instruments.json")
        snap = swing_aware_snapshot(deps, imap, deps.read.pnl(), now=deps.now_fn())
        live = [p.position_id for p in snap.positions]
    latest: dict[str, Any] = {}
    for d in sorted(smoke_decisions(deps.ledger), key=lambda d: d.created_at):
        latest[str((d.target or {}).get(STEP_KEY, ""))] = d
    rows = [(code, latest[code].decision_id if code in latest else "-",
             latest[code].state if code in latest else "not run") for code in STEPS]
    active = deps.ledger.smoke_active(live)
    caps = capabilities.load(deps.state_dir)
    needed = ("real_etf", "sl_modify", "partial_close", "crypto_real", "cfd_short")
    missing = [c for c in needed if not caps.has(c)]
    gates = {
        "K20": {"state": "red" if active else "green", "code": active[0].split(":")[0] if active else "no_smoke_open"},
        "K14": {"state": "green" if not missing else "red",
                "code": "smoke_s1_s6_verified" if not missing else f"capability_missing:{missing[0]}"},
        "S3": {"state": "green" if caps.has("stock_fractional") else "amber",
               "code": "stock_fractional_verified" if caps.has("stock_fractional") else "s7_not_run"},
    }
    swing_missing = [c for c in capabilities.SWING_CAPABILITIES if not caps.has(c)]
    gates["SW"] = {"state": "green" if not swing_missing else "amber",   # S7/S7t/S7x/S8/S8x (swing book)
                   "code": "swing_capabilities_verified" if not swing_missing
                   else f"capability_missing:{swing_missing[0]}"}
    p = deps.print_fn
    for code, did, state in rows:
        p(f"{code:<5} {state:<20} {did}")
    for gate, g in gates.items():
        p(f"{g['state']:<6} {gate:<4} {g['code']}")
    return Status(rows, active, gates)
