"""NAV invariance of the core public record (m5-readiness M5-N; transparency-v2 §4.2, T-D10, X3).

- (a) forced: the same decisions at two funding levels, with private notes that differ in
  NAV-revealing ways (a size-floor R11 at one level, a deadband R11 at the other; an R15 SR_be
  priced from a broker what-if; fee-bearing codes; amounts, units and fees scaled by the NAV)
  give byte-identical public cycle, book, status, ops and execution files.
- (b) unforced, two funding levels both above the P2 threshold (no line's size floor exceeds the
  public deadband share `deadband.min_nav_share`): the real engine and planner give
  byte-identical public files.
- (c) a funding level below the threshold: the documents differ only on the lines whose size
  floor binds (flagged like the private `size_floor_binding:<line>`), each held line reads the
  bare `LINE: R11` (the same words as a genuine deadband hold), and no differing string carries a
  number, "minimum", "floor" or "broker".
- (d) smoke tickets do not exist yet (M5-D2 adds them and extends this file's cases).

Residuals this file does not claim to close (stated in transparency-v2 X3 and D18): whether a
small change happened at all still bounds the NAV coarsely; the fixed fee in bps of NAV can hold
a risk-increasing discretionary leg under R14 or R15_fee at a small NAV. The scenario below uses
legs the fee cannot hold (cuts and a reference-origin add) so that the size floor is what is
tested. Every amount here is synthetic.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from council.broker.eligibility import VehicleChoice, parse_eligibility, select_config
from council.broker.fake import eligibility_row
from council.execution.planner import build_plan, changed_targets
from council.facts.pack import cost_facts_from_quotes
from council.models.broker import ExposureSnapshot, Position, Quote
from council.models.cycle import CycleRecord
from council.models.facts import Fact, FactPack
from council.models.plan import Leg, Plan
from council.models.reference import ReferenceBook, ReferenceEntry
from council.models.risk import Band, RiskCheck, RiskDecision
from council.publish import commit_reveal
from council.publish.redact import (
    public_book,
    public_cycle,
    public_execution,
    public_ops_row,
    public_status,
)
from council.risk.costs import trade_economics
from council.runtime import engine_quotes, floor_cost_quotes
from tests.risk.helpers import NOW, default_ref, default_units, run, states_for

CYCLE_ID = "2026-10-01T1440Z"
SLOT = NOW
SHA = "ab" * 32
PRICE = 50.0                         # every vehicle: bid = ask, so unit rounding stays exact
VEHICLES = {"SEMIS": ("SMH.L", 201), "GOLD": ("SGLN.L", 202), "SPX": ("CSPX.L", 203), "NDX": ("EQQQ.L", 204)}
# The broker's minimum position exposure per line (USD): GOLD's and SPX's vehicles have a large one,
# so their size floor binds below 4,000 USD of NAV; the others never bind here.
BROKER_MIN_USD = {"GOLD": 40.0, "SPX": 80.0}
OTHER_MIN_USD = 5.0


# ------------------------------------------------------------------------------ the scenario
def _scenario(policy):
    units = default_units(policy)
    ref = default_ref(policy, "up")
    states = {s: st.model_copy(update={"history_source": f"{'binance' if s in ('BTC', 'ETH') else 'tiingo'}:{s}"})
              for s, st in states_for(policy, "up", GOLD={"sigma_ann": 0.40}).items()}
    current = {s: ref[s] * units[s] for s in units}
    current["SPX"] = 0.5 * units["SPX"]            # the held reference level lags: a reference-origin add
    held = {s: ref[s] for s in units} | {"SPX": 0.5}
    levels = dict(ref) | {"SEMIS": 0.5, "GOLD": 0.75, "ETH": 0.75}   # two discretionary cuts, one too small
    bands = {s: Band(symbol=s, trend="up", ref_level=ref[s], lo=ref[s], hi=ref[s]) for s in units}
    for s in ("SEMIS", "GOLD", "ETH"):
        bands[s] = Band(symbol=s, trend="up", ref_level=ref[s], lo=ref[s] - 0.5, hi=ref[s],
                        reasons=["uptrend: cut allowed by a qualifying card"], qualifying_cards=["K:vol:1"])
    return SimpleNamespace(units=units, ref=ref, states=states, current=current, held=held, levels=levels,
                           bands=bands)


def _min_share(nav: float) -> dict[str, float]:
    return {s: BROKER_MIN_USD.get(s, OTHER_MIN_USD) / nav for s in ("NDX", "SEMIS", "SPX", "GOLD", "BTC", "ETH",
                                                                    "OIL", "EURUSD", "GBPUSD")}


def size_floor_binding(policy, nav: float) -> set[str]:
    """The private P2 flag: lines whose size floor exceeds the public deadband share at this NAV."""
    econ = trade_economics(policy, virtual_nav_usd=nav, mirror_ratio=1.0)
    threshold = float(policy.risk["deadband"]["min_nav_share"])
    return {s for s, share in _min_share(nav).items() if max(share, econ.copy_floor_share) > threshold}


def _positions(sc, nav: float) -> list[Position]:
    out = []
    for i, (line, (symbol, iid)) in enumerate(sorted(VEHICLES.items()), start=1):
        exposure = sc.current[line] * nav
        out.append(Position(position_id=900 + i, instrument_id=iid, symbol=symbol, is_buy=True, units=exposure / PRICE,
                            open_rate=PRICE * 0.98, amount=exposure, sl_rate=PRICE * 0.9, settlement="real",
                            opened_at=SLOT - timedelta(days=30), exposure_usd=exposure, close_rate=PRICE))
    return out


def _plan(policy, sc, decision: RiskDecision, nav: float, econ) -> Plan:
    rows = {r.symbol: r for r in parse_eligibility({"eligibilities": [
        eligibility_row(symbol, iid, min_position_exposure=BROKER_MIN_USD.get(line, OTHER_MIN_USD))
        for line, (symbol, iid) in VEHICLES.items()]}, SLOT)}
    by_line = {line: rows[symbol] for line, (symbol, _) in VEHICLES.items()}

    def vehicle_for(line: str, direction: str, leverage: int) -> VehicleChoice | None:
        row = by_line.get(line)
        config = select_config(row, direction, leverage) if row is not None else None
        if config is None:
            return None
        return VehicleChoice(symbol=row.symbol, instrument_id=row.instrument_id, settlement="cfd",
                             leverage=leverage, config=config)

    snap = ExposureSnapshot(taken_at=SLOT, equity_usd=nav, credit_usd=nav, positions=_positions(sc, nav),
                            signed_w={}, gross=0.0, net=0.0, margin_use=0.0)
    quotes = {symbol: Quote(symbol=symbol, instrument_id=iid, bid=PRICE, ask=PRICE, at=SLOT)
              for symbol, iid in VEHICLES.values()}
    return build_plan(
        snapshot=snap, target_w=changed_targets(decision), vehicle_for=vehicle_for, quotes=quotes,
        stop_distance={line: 0.1 for line in VEHICLES}, leverage_for={}, eligibility=rows,
        cost_bps=lambda line, sym, d, lev: (5.0, 0.0, econ.fee_nav_bps), nav_usd=nav, policy=policy,
        economics=econ,
    )


def _reference(sc) -> ReferenceBook:
    return ReferenceBook(
        cycle_id=CYCLE_ID, k=1.0, target_vol=0.22, ex_ante_vol=0.18, gross=0.9,
        entries={s: ReferenceEntry(symbol=s, sleeve="core", asset_class="index", in_reference=sc.ref[s] > 0,
                                   trend="up", level_ref=sc.ref[s], unit_weight=u, weight_ref=sc.ref[s] * u,
                                   sigma_ann=0.2, stop_distance=0.1)
                 for s, u in sc.units.items()},
    )


def _pack(sc, cost_facts: list[Fact]) -> FactPack:
    return FactPack(cycle_id=CYCLE_ID, slot=SLOT, created_at=SLOT, admitted=list(sc.units), states=sc.states,
                    facts=cost_facts)


def _record(sc, decision: RiskDecision, plan: Plan, **extra: Any) -> CycleRecord:
    return CycleRecord(
        cycle_id=CYCLE_ID, slot=SLOT, started_at=SLOT + timedelta(minutes=3), finished_at=SLOT + timedelta(minutes=9),
        status="on_time", mode="live", input_hash=SHA, policy_sha=SHA, model="deepseek-v4.1-flash:cloud",
        reference=_reference(sc), bands=sc.bands, risk=decision, plan=plan, decision_state="completed",
        approved_at=SLOT + timedelta(minutes=20), material_fingerprint="cd" * 32, flags=[], **extra,
    )


def _report(plan: Plan, final_w: dict[str, float]) -> SimpleNamespace:
    legs = [SimpleNamespace(seq=leg.seq, kind=leg.kind, symbol=leg.symbol, line=leg.line, state="filled",
                            units_requested=leg.units, units_filled=leg.units,
                            fill_price=(leg.amount_usd * leg.leverage / leg.units) if leg.units else None)
            for leg in plan.legs]
    return SimpleNamespace(final_state="completed", legs=legs, reconcile=SimpleNamespace(drift=0.0, achieved_w=final_w))


def public_files(policy, sc, record: CycleRecord, pack: FactPack, nav: float, positions: list[Position]) -> dict[str, Any]:
    """Every public document of the cycle, as JSON objects (canonical bytes are compared too)."""
    lines = policy.universe
    doc = public_cycle(record, pack, lines=lines)
    return {
        "cycle": doc,
        "book": public_book(CYCLE_ID, record.risk.final_w, lines=lines, reference_weights=_reference(sc).weights(),
                            positions=positions, pack=pack),
        "status": public_status("LIVE", last_cycle_id=CYCLE_ID, last_cycle_at=SLOT),
        "ops": public_ops_row(record),
        "execution": public_execution(_report(record.plan, record.risk.final_w), cycle_id=CYCLE_ID, lines=lines,
                                      nav_usd=nav, plan=record.plan, approved_at=record.approved_at,
                                      completed_at=record.approved_at + timedelta(minutes=5)),
    }


def unforced_at(policy, nav: float, *, engine_broker_min: bool = True) -> tuple[dict[str, Any], RiskDecision]:
    """The public files at `nav`. `engine_broker_min=False` runs the engine the way `cycle.py` does
    today (no `broker_min_share`), so the broker minimum is met only by the planner's skip."""
    sc = _scenario(policy)
    econ = trade_economics(policy, virtual_nav_usd=nav, mirror_ratio=1.0)
    quotes = floor_cost_quotes(policy, quoted_at=SLOT, fee_bps=econ.fee_nav_bps)
    decision = run(policy, levels=sc.levels, ref=sc.ref, bands=sc.bands, states=sc.states, current=sc.current,
                   unit_weights=sc.units, cost_quotes=engine_quotes(quotes), copy_min_share=econ.copy_floor_share,
                   broker_min_share=_min_share(nav) if engine_broker_min else None, held_levels=sc.held)
    plan = _plan(policy, sc, decision, nav, econ)
    per_line = {line: q for (line, direction, lev), q in quotes.items() if direction == "long" and lev == 1}
    pack = _pack(sc, cost_facts_from_quotes(per_line, slot=SLOT))
    return public_files(policy, sc, _record(sc, decision, plan), pack, nav, _positions(sc, nav)), decision


def _bytes(files: dict[str, Any]) -> dict[str, bytes]:
    return {name: commit_reveal.canonical_json(doc) for name, doc in files.items()}


def _json(files: dict[str, Any]) -> dict[str, Any]:
    return {name: json.loads(b) for name, b in _bytes(files).items()}


# ------------------------------------------------------------------------------ (a) forced
FORCED_NOTES = {
    # small NAV: the size floor holds GOLD; a broker what-if priced SPX's R15
    2_000.0: ["GOLD: R11 below the minimum trade size", "SPX: R15 SR_be 0.41 above 0.20",
              "NDX: R15_fee net-of-cost gate (fixed fee)", "ETH: R11 deadband (level step -0.25)"],
    # large NAV: the same decisions for other private reasons
    20_000.0: ["GOLD: R11 reference rule (level unchanged, drift below the threshold)",
               "SPX: R15 SR_be 0.33 above 0.20", "NDX: R15_fee net-of-cost gate (fixed fee)",
               "ETH: R11 deadband (level step -0.25)"],
}


def forced_at(policy, nav: float) -> dict[str, Any]:
    sc = _scenario(policy)
    base = dict(sc.current)
    final = dict(base) | {"SEMIS": 0.075}
    fee = 1e4 * 2.0 / nav
    decision = RiskDecision(
        raw_levels=sc.levels, banded_levels=sc.levels, base_w=base, proposed_w=final | {"GOLD": 0.09}, final_w=final,
        checks=[RiskCheck(rule_id="R11", name="deadband", passed=True, value=0.0, limit="level 0.25/0.5 crypto, 2% NAV"),
                RiskCheck(rule_id="R14", name="cycle_cost_bps", passed=True, value="R14_fee", limit=40.0),
                RiskCheck(rule_id="R15", name="net_of_cost_gate", passed=True, value=0.41 if nav < 10_000 else 0.33,
                          limit="reference 0.3, council 0.2")],
        gross=sum(abs(v) for v in final.values()), net=sum(final.values()), margin_use=0.3, stop_budget_used=0.05,
        stop_budget_limit=0.2, carry_bps_day=0.0, ex_ante_vol=0.15, basis="council", hold_reasons=FORCED_NOTES[nav],
    )
    units = 0.075 * nav / PRICE
    plan = Plan(
        legs=[Leg(seq=1, kind="partial_close", symbol="SMH.L", line="SEMIS", instrument_id=201, direction="long",
                  settlement="real", leverage=1, weight_before=0.15, weight_after=0.075, stop_distance=0.1,
                  sl_margin_pct=10.0, cost_bps_nav=0.375, risk_increasing=False, reason="SEMIS: reduce",
                  amount_usd=0.075 * nav, units=units, sl_rate=PRICE * 0.9, position_id=902, fee_bps_nav=fee,
                  fee_drag=1.0 / nav)],
        gross_before=sum(abs(v) for v in base.values()), gross_after=sum(abs(v) for v in final.values()),
        net_before=sum(base.values()), net_after=sum(final.values()), cost_bps_nav=0.375, carry_bps_day_nav=0.0,
        skipped=[], fee_bps_nav=fee,
    )
    whatif = [Fact(id=f"C:{s}:per_side_bps", kind="cost", symbol=s, value=6.1, unit="bps", available_at=SLOT,
                   source="costs:whatif") for s in sc.units]
    return public_files(policy, sc, _record(sc, decision, plan), _pack(sc, whatif), nav, _positions(sc, nav))


def test_forced_decisions_publish_byte_identical_files_at_two_funding_levels(policy):
    small, large = forced_at(policy, 2_000.0), forced_at(policy, 20_000.0)
    assert _bytes(small) == _bytes(large)
    holds = small["cycle"].risk.hold_reasons
    assert holds == ["GOLD: R11", "SPX: R15", "NDX: R15_fee", "ETH: R11"]
    r15 = next(c for c in small["cycle"].risk.checks if c.rule_id == "R15")
    assert r15.value is None                                  # the largest SR_be came from a broker what-if
    text = b"".join(_bytes(small).values()).decode()
    for word in ("minimum", "SR_be", "fixed fee", "reference rule", "level step"):
        assert word not in text


# ------------------------------------------------------------------------------ (b) and (c) unforced
HIGH, HIGHER, LOW = 5_000.0, 50_000.0, 1_000.0


def test_the_p2_flag_separates_the_funding_levels(policy):
    assert size_floor_binding(policy, HIGH) == size_floor_binding(policy, HIGHER) == set()
    assert size_floor_binding(policy, LOW) == {"GOLD", "SPX"}


def test_unforced_above_the_threshold_publishes_byte_identical_files(policy):
    high, d_high = unforced_at(policy, HIGH)
    higher, d_higher = unforced_at(policy, HIGHER)
    assert _bytes(high) == _bytes(higher)
    # the scenario really trades: two cuts and a reference-origin add, one genuine deadband hold
    moved = {s for s in d_high.final_w if abs(d_high.final_w[s] - d_high.base_w[s]) > 1e-9}
    assert moved == {"SEMIS", "GOLD", "SPX"} and {leg.line for leg in high["cycle"].plan.legs} == moved
    assert high["cycle"].risk.hold_reasons == ["ETH: R11"]
    assert [r for r in d_high.hold_reasons if r.startswith("ETH")] == ["ETH: R11 deadband (level step -0.25)"]


def _leaves(a: Any, b: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Differing leaves of two JSON trees (lists by index, dicts by key)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            out += _leaves(a.get(key), b.get(key), f"{path}.{key}" if path else key)
        return out
    if isinstance(a, list) and isinstance(b, list):
        out = []
        for i in range(max(len(a), len(b))):
            out += _leaves(a[i] if i < len(a) else None, b[i] if i < len(b) else None, f"{path}[{i}]")
        return out
    return [] if a == b else [(path, a, b)]


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


AGGREGATES = {
    "cycle": {"risk.gross_x", "risk.net_x", "risk.margin_use_pct", "risk.stop_at_risk_pct", "risk.carry_bp_day",
              "risk.ex_ante_vol_pct", "plan.cost_bp_total", "plan.carry_bp_day", "plan.gross_after_x",
              "plan.net_after_x"},
    "book": {"gross_x", "net_x", "cash_x"},
    "ops": {"legs"},
    "execution": {"cost_bp_total", "achieved_drift_x"},
}
# Check rows whose value is a sum or a count over the whole book (gross, net, stop-at-risk, group and
# margin totals, ex-ante vol, cost budgets, legs): they follow the flagged lines' weights. Every other
# check value (R11's violation count, R15's SR_be, R12, R13, R16...R20, MC) must stay equal.
AGGREGATE_CHECKS = frozenset({"R1", "R2", "R4", "R5", "R6", "R7", "R8", "R14", "R21"})


def _normalised(name: str, doc: dict[str, Any], flagged: set[str]) -> dict[str, Any]:
    """The document with the parts that may differ removed: the flagged lines' weights, legs,
    fills and `LINE: R11` holds, the derived aggregates and the check values of aggregate rules."""
    doc = json.loads(json.dumps(doc))
    for dotted in AGGREGATES.get(name, ()):
        node = doc
        *head, last = dotted.split(".")
        for key in head:
            node = node.get(key) or {}
        node.pop(last, None)
    if name == "cycle":
        risk, plan = doc["risk"], doc["plan"]
        for key in ("final_x",):
            risk[key] = {k: v for k, v in risk[key].items() if k not in flagged}
        risk["hold_reasons"] = [r for r in risk["hold_reasons"] if r not in {f"{s}: R11" for s in flagged}]
        for check in risk["checks"]:
            if check["rule_id"] in AGGREGATE_CHECKS:
                check.pop("value", None)
        plan["legs"] = [{k: v for k, v in leg.items() if k != "seq"} for leg in plan["legs"] if leg["line"] not in flagged]
    elif name == "book":
        doc["lines"] = {k: v for k, v in doc["lines"].items() if k not in flagged}
    elif name == "execution":
        doc["fills"] = [{k: v for k, v in f.items() if k != "seq"} for f in doc["fills"] if f["line"] not in flagged]
        doc["achieved_x"] = {k: v for k, v in doc["achieved_x"].items() if k not in flagged}
    return doc


def test_unforced_below_the_threshold_differs_only_on_flagged_lines(policy):
    high, _ = unforced_at(policy, HIGH)
    low, d_low = unforced_at(policy, LOW)
    flagged = size_floor_binding(policy, LOW)
    a, b = _json(high), _json(low)
    for name in a:
        assert _normalised(name, a[name], flagged) == _normalised(name, b[name], flagged), name
    # the private notes name the size floor; the public ones do not, and read like a deadband hold
    assert "SPX: R11 below the minimum trade size" in d_low.hold_reasons
    assert "GOLD: R11 deadband (level step -0.25)" in d_low.hold_reasons
    assert low["cycle"].risk.hold_reasons == ["SPX: R11", "GOLD: R11", "ETH: R11"]
    assert set(low["cycle"].risk.hold_reasons) - set(high["cycle"].risk.hold_reasons) == {"SPX: R11", "GOLD: R11"}
    # the check rows keep their rules, names, limits and verdicts
    for x, y in zip(a["cycle"]["risk"]["checks"], b["cycle"]["risk"]["checks"], strict=True):
        assert {k: v for k, v in x.items() if k != "value"} == {k: v for k, v in y.items() if k != "value"}
    # no differing string carries a number (beyond the rule code) or names the floor
    for name in a:
        for _path, x, y in _leaves(a[name], b[name]):
            for s in _strings(x) + _strings(y):
                bare = re.sub(r"\bR\d{1,2}[a-z]?\b", "", s)
                assert not re.search(r"\d", bare), (name, s)
                assert not re.search(r"(?i)minimum|floor|broker", s), (name, s)


# The planner's channel: `cycle.py` does not pass `broker_min_share` to the engine yet, so the
# broker's minimum on an OPEN leg is met by the planner, whose skip note is published as written.
# SPX's vehicle gets a 300 USD minimum here so that at 2,000 USD the fee (D18) does not hold SPX first.
PLANNER_NAV = 2_000.0


def _planner_channel(policy, monkeypatch) -> tuple[dict[str, Any], RiskDecision]:
    monkeypatch.setitem(BROKER_MIN_USD, "SPX", 300.0)
    return unforced_at(policy, PLANNER_NAV, engine_broker_min=False)


def test_the_scenario_reaches_the_planner_skip_when_the_engine_lacks_the_broker_minimum(policy, monkeypatch):
    """Pins that the xfail below exercises the real planner path (not a test artefact)."""
    files, decision = _planner_channel(policy, monkeypatch)
    assert files["cycle"].plan.skipped and all(s.startswith("SPX: ") for s in files["cycle"].plan.skipped)
    assert not any(r.startswith("SPX") for r in decision.hold_reasons)


@pytest.mark.xfail(strict=True, reason=(
    "open M5-N item (token-day): `redact._plan` publishes planner size skips as written "
    "(`SPX: below_broker_minimum`), which bounds the NAV. Closing it needs `trace_rules.public_plan_skip` "
    "wired into `redact._plan` together with the site's words for `R11` "
    "(tests/publish/test_site.py::test_internal_codes_read_as_words), or cycle.py passing "
    "`broker_min_share` so the engine holds the line under R11 first"))
def test_no_public_plan_skip_names_the_broker_minimum(policy, monkeypatch):
    files, _ = _planner_channel(policy, monkeypatch)
    text = commit_reveal.canonical_json(files["cycle"]).decode()
    assert not re.search(r"(?i)minimum|below_real|below_broker", text)


@pytest.mark.parametrize("nav", [LOW, HIGH])
def test_a_size_hold_and_a_deadband_hold_publish_the_same_text(policy, nav):
    files, decision = unforced_at(policy, nav)
    public = files["cycle"].risk.hold_reasons
    assert all(re.fullmatch(r"[A-Z0-9_]+: R11", r) for r in public), public
    assert len(public) == len(decision.hold_reasons)
