"""M5-D1 capability gates (m5-readiness §5): fail closed until a smoke fill (ledger cross-check) or
an operator-written readiness record proves them; consulted only with a connected broker."""

from __future__ import annotations

import json
import sqlite3
import stat
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from council.broker.eligibility import parse_eligibility_row, required_capabilities, resolve_vehicle
from council.broker.fake import eligibility_row, leverage_config
from council.operator import capabilities as caps_mod
from council.operator import readiness
from council.operator.capabilities import (
    CAPABILITIES,
    Capabilities,
    CapabilityError,
    load,
    write_capability,
    write_mirror_check,
)

pytestmark = pytest.mark.capability_gates

NOW = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
OK = {"assert_operator": lambda: None, "assert_release": lambda: None}
SHA = "a" * 40


def _refuse() -> None:
    raise RuntimeError("not the operator terminal")


def _smoke_ledger(state, decision_id="smoke-s6", *, kind="smoke", state_="completed", step="S6",
                  leg_state="filled"):
    """The ledger rows M5-D2 writes (kind 'smoke', target_json.smoke_step, filled legs)."""
    con = sqlite3.connect(state / "ledger.sqlite3")
    con.execute("CREATE TABLE IF NOT EXISTS decisions (decision_id TEXT PRIMARY KEY, kind TEXT, "
                "state TEXT, target_json TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS legs (decision_id TEXT, seq INTEGER, state TEXT)")
    con.execute("INSERT INTO decisions VALUES (?, ?, ?, ?)",
                (decision_id, kind, state_, json.dumps({"smoke_step": step})))
    con.execute("INSERT INTO legs VALUES (?, 1, ?)", (decision_id, leg_state))
    con.commit()
    con.close()


def _prove(state, cap, step, decision_id, *, mirror=True):
    write_capability(cap, decision_id=decision_id, step=step, state_dir=state, now=NOW, **OK)
    if mirror:
        for item in caps_mod.MIRROR_ITEMS:
            write_mirror_check(item, decision_id=decision_id, state_dir=state, now=NOW, **OK)


# ------------------------------------------------------------------------------ records
def test_a_missing_file_makes_every_capability_false(tmp_path):
    caps = load(tmp_path, code_flag=lambda: True)
    assert caps.verified == frozenset() and not caps.unproven
    assert caps.flags() == [f"capability_missing:{c}" for c in CAPABILITIES]
    with pytest.raises(CapabilityError):
        caps.has("teleport")


def test_writes_refuse_outside_operator_context_and_unknown_items(tmp_path):
    with pytest.raises(RuntimeError):
        write_capability("cfd_short", decision_id="d", step="S6", state_dir=tmp_path,
                         assert_operator=_refuse, assert_release=lambda: None)
    with pytest.raises(Exception):  # noqa: B017 - the real guard: COUNCIL_ROLE=dev, no TTY
        write_mirror_check("mirror-copied", decision_id="d", state_dir=tmp_path)
    with pytest.raises(CapabilityError):
        write_capability("teleport", decision_id="d", step="S1", state_dir=tmp_path, **OK)
    with pytest.raises(CapabilityError):
        write_capability("cfd_short", decision_id="d", step="S1", state_dir=tmp_path, **OK)
    with pytest.raises(CapabilityError):
        write_mirror_check("mirror-happy", decision_id="d", state_dir=tmp_path, **OK)
    assert not caps_mod.path_for(tmp_path).exists()


def test_a_record_without_a_completed_smoke_fill_is_unproven(tmp_path):
    _prove(tmp_path, "cfd_short", "S6", "smoke-s6")
    path = caps_mod.path_for(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    caps = load(tmp_path, code_flag=lambda: False)
    assert not caps.has("cfd_short") and "cfd_short" in caps.unproven
    assert "capability_unproven:cfd_short" in caps.flags()


@pytest.mark.parametrize("kind,state_,step,leg", [
    ("rebalance", "completed", "S6", "filled"),     # not a smoke decision
    ("smoke", "blocked", "S6", "filled"),           # not completed
    ("smoke", "completed", "S1", "filled"),         # another step
    ("smoke", "completed", "S6", "skipped"),        # no fill
])
def test_the_ledger_cross_check_needs_kind_state_step_and_fills(tmp_path, kind, state_, step, leg):
    _smoke_ledger(tmp_path, kind=kind, state_=state_, step=step, leg_state=leg)
    _prove(tmp_path, "cfd_short", "S6", "smoke-s6")
    assert not load(tmp_path, code_flag=lambda: False).has("cfd_short")


def test_a_completed_smoke_fill_plus_both_mirror_checks_proves_it(tmp_path):
    _smoke_ledger(tmp_path)
    _prove(tmp_path, "cfd_short", "S6", "smoke-s6", mirror=False)
    caps = load(tmp_path, code_flag=lambda: False)
    assert not caps.has("cfd_short") and caps.notes["cfd_short"] == "mirror_unattested"
    for item in caps_mod.MIRROR_ITEMS:
        write_mirror_check(item, decision_id="smoke-s6", state_dir=tmp_path, now=NOW, **OK)
    caps = load(tmp_path, code_flag=lambda: False)
    assert caps.has("cfd_short") and not caps.unproven
    assert "capability_missing:cfd_short" not in caps.flags()


def test_a_tampered_file_reads_as_all_false(tmp_path):
    path = caps_mod.path_for(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": 1, "capabilities": {"cfd_short": {"verified": True}}}))
    assert load(tmp_path, code_flag=lambda: True).verified == frozenset()


def test_evidence_capabilities_need_operator_written_green_records(tmp_path):
    assert not load(tmp_path, code_flag=lambda: True).has("rates_entitled")
    readiness.write_record("live-read", head=SHA, state_dir=tmp_path, now=NOW,
                           gates={"K7": {"state": "green", "code": "rates_ok"},
                                  "K17": {"state": "green", "code": "cancel_ok"},
                                  "K19": {"state": "red", "code": "unit_unknown"}}, **OK)
    readiness.write_record("attest", head=SHA, state_dir=tmp_path, now=NOW,
                           attested={"terms-version": True}, **OK)
    caps = load(tmp_path, code_flag=lambda: False)
    assert caps.has("rates_entitled") and caps.has("terms_version")
    assert not caps.has("price_units")            # red gate
    assert not caps.has("cancel_route")           # needs the code flip too
    assert load(tmp_path, code_flag=lambda: True).has("cancel_route")
    with pytest.raises(RuntimeError):             # an agent cannot write the record the gates read
        readiness.write_record("live-read", head=SHA, state_dir=tmp_path,
                               gates={"K19": {"state": "green", "code": "units_ok"}},
                               assert_operator=_refuse, assert_release=lambda: None)


# ------------------------------------------------------------------------------ vehicle classes
def test_required_capabilities_by_vehicle_class(policy):
    by = policy.universe.by_symbol()
    assert required_capabilities(by["NDX"], "real", "long", 1) == ("real_etf",)
    assert required_capabilities(by["BTC"], "real", "long", 1) == ("crypto_real",)
    assert required_capabilities(by["NDX"], "cfd", "long", 1) == ("cfd_long",)
    assert required_capabilities(by["NDX"], "cfd", "short", 1) == ("cfd_short",)
    assert required_capabilities(by["NDX"], "cfd", "long", 2) == ("cfd_long", "cfd_leverage")


def test_resolve_vehicle_excludes_unverified_classes(policy):
    line = policy.universe.by_symbol()["NDX"]
    rows = {}
    for i, v in enumerate(line.vehicles.long):
        configs = ([leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,))]
                   if v.settlement == "real" else [leverage_config(direction="LONG")])
        rows[v.symbol] = parse_eligibility_row(eligibility_row(v.symbol, 300 + i, configs=configs), NOW)
    cost = lambda vehicle, row, config: 1.0 if vehicle.settlement == "real" else 0.5  # noqa: E731
    assert resolve_vehicle(line, "long", 1, rows, cost).settlement == "cfd"          # no gate
    assert resolve_vehicle(line, "long", 1, rows, cost, capabilities=set()) is None
    assert resolve_vehicle(line, "long", 1, rows, cost, capabilities={"real_etf"}).settlement == "real"
    assert resolve_vehicle(line, "long", 1, rows, cost, capabilities={"cfd_long"}).settlement == "cfd"


# ------------------------------------------------------------------------------ connected cycle
def _broker(tmp_path):
    """tests/integration/test_cycle_costs.py's connected fake (one real/CFD row per vehicle)."""
    from council.broker.etoro_read import EtoroReadClient
    from council.broker.fake import FakeClock, FakeEtoro
    from council.broker.instruments import InstrumentMap
    from tests.integration.test_end_to_end import API_KEY, READ_KEY, VEHICLES, WRITE_KEY
    from tests.integration.test_end_to_end import NOW as E2E_NOW

    fclock = FakeClock(start=E2E_NOW)
    fake = FakeEtoro(clock=fclock.now, credit=10_000.0, write_user_keys={WRITE_KEY})
    for sym, (iid, px, settlement) in VEHICLES.items():
        if settlement == "real":
            configs = [leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,))]
        else:
            configs = [leverage_config(direction="LONG"), leverage_config(direction="SHORT")]
        fake.add_instrument(sym, iid, bid=px * 0.9995, ask=px * 1.0005,
                            row=eligibility_row(sym, iid, configs=configs))
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    InstrumentMap({}, path=state_dir / "instruments.json").merged(
        {s: v[0] for s, v in VEHICLES.items()}, E2E_NOW).save()
    return EtoroReadClient(API_KEY, READ_KEY, transport=fake.transport(), sleep=fclock.sleep)


def _run(tmp_path, monkeypatch, verified=None):
    from council import cycle
    from council.risk import authority
    from tests.integration.test_end_to_end import _ctx

    seen = {}
    real = getattr(authority.compute_bands, "real", authority.compute_bands)   # never chain spies

    def spy(**kw):
        seen["short_ok"], seen["lever_ok"] = set(kw["short_ok"]), set(kw["lever_ok"])
        return real(**kw)

    spy.real = real

    monkeypatch.setattr(authority, "compute_bands", spy)
    if verified is not None:
        monkeypatch.setattr(caps_mod, "load", lambda *a, **k: Capabilities(verified=frozenset(verified)))
    ctx = _ctx(tmp_path, broker=_broker(tmp_path))
    out = cycle.run_cycle(ctx)
    legs = ctx.ledger.legs(out.decision_id) if out.decision_id else []
    return out, legs, seen


def test_connected_cycle_without_capabilities_plans_nothing(tmp_path, monkeypatch):
    out, legs, seen = _run(tmp_path, monkeypatch)
    assert legs == [] and out.decision_state == "reviewed_no_action"
    for cap in CAPABILITIES:
        assert f"capability_missing:{cap}" in out.flags
    assert seen["short_ok"] == set() and seen["lever_ok"] == set()


def test_real_etf_alone_trades_core_ucits_and_keeps_overlays_flat(tmp_path, monkeypatch):
    out, legs, _ = _run(tmp_path, monkeypatch, {"real_etf", "rates_entitled", "price_units"})
    assert legs, out.flags
    assert {r.settlement for r in legs} == {"real"}
    assert {r.direction for r in legs} == {"long"} and {r.leverage for r in legs} == {1}
    assert not {r.line for r in legs} & {"BTC", "ETH", "OIL", "EURUSD", "GBPUSD"}
    assert "capability_missing:cfd_long" in out.flags and "capability_missing:real_etf" not in out.flags


def test_attesting_cfd_short_restores_short_ok(tmp_path, monkeypatch):
    from council.risk import costs

    monkeypatch.setattr(costs, "passes_cost_gate", lambda *a, **k: (True, 0.0, 0.0))  # every line qualifies
    base = {"real_etf", "rates_entitled", "price_units", "cfd_long"}
    _, _, without = _run(tmp_path / "a", monkeypatch, base)
    _, _, ungated = _run(tmp_path / "b", monkeypatch, set(CAPABILITIES))
    _, _, with_short = _run(tmp_path / "c", monkeypatch, base | {"cfd_short"})
    assert ungated["short_ok"]
    assert without["short_ok"] == set() and without["lever_ok"] == set()
    assert with_short["short_ok"] == ungated["short_ok"] and with_short["lever_ok"] == set()


def test_nothing_changes_before_the_token(tmp_path, monkeypatch):
    from council import cycle
    from tests.integration.test_end_to_end import _ctx

    called = []
    monkeypatch.setattr(caps_mod, "load", lambda *a, **k: called.append(1) or caps_mod.NONE)
    out = cycle.run_cycle(_ctx(tmp_path))
    assert not called and not any(f.startswith("capability_") for f in out.flags)


# ------------------------------------------------------------------------------ planner
def test_planner_skips_a_trim_without_partial_close_but_a_full_exit_works(policy):
    from tests.execution.test_planner import build, pos

    older, newer = pos(1, "SPX500", 10, age_h=48), pos(2, "SPX500", 10, age_h=1)
    gated = Capabilities(verified=frozenset({"cfd_long", "rates_entitled", "price_units"}))
    plan = build(policy, {"SPX": 0.15}, older, newer, capabilities=gated)
    assert plan.legs == [] and "SPX: capability_missing:partial_close" in plan.skipped
    (leg,) = build(policy, {"SPX": 0.15}, older, newer,
                   capabilities=Capabilities(verified=gated.verified | {"partial_close"})).legs
    assert leg.kind == "partial_close"
    exit_plan = build(policy, {"SPX": 0.0}, older, newer, capabilities=gated)
    assert {leg.kind for leg in exit_plan.legs} == {"close"} and len(exit_plan.legs) == 2


def test_planner_plans_no_open_without_rates_or_units(policy):
    from tests.execution.test_planner import build

    plan = build(policy, {"SPX": 0.15}, capabilities=Capabilities(verified=frozenset({"cfd_long", "real_etf"})))
    assert plan.legs == [] and "SPX: capability_missing:rates_entitled" in plan.skipped
    assert build(policy, {"SPX": 0.15}, capabilities=Capabilities.all_verified()).legs


def test_a_trim_never_becomes_a_full_exit_when_the_reopen_is_gated(policy):
    """Review fix: a broker that refuses partial closes trims by close + re-open; when the re-open
    lacks a capability the trim waits instead of fully exiting the line."""
    from tests.execution.test_planner import build, pos, rows

    elig = rows(SPX500=eligibility_row("SPX500", 101, allow_partial_close=False))
    no_units = Capabilities(verified=frozenset({"cfd_long", "partial_close", "rates_entitled"}))
    plan = build(policy, {"SPX": 0.06}, pos(1, "SPX500", 10), eligibility=elig, capabilities=no_units)
    assert plan.legs == [] and "SPX: capability_missing:price_units" in plan.skipped
    kinds = [leg.kind for leg in build(policy, {"SPX": 0.06}, pos(1, "SPX500", 10), eligibility=elig,
                                       capabilities=Capabilities.all_verified()).legs]
    assert kinds == ["close", "open"]


def test_an_open_for_a_line_outside_the_universe_fails_closed(policy):
    from council.execution.planner import _Builder

    b = _Builder(policy=policy, nav=10_000.0, vehicle_for=lambda *a: None, quotes={}, stop_distance={},
                 leverage_for={}, eligibility={}, cost_bps=lambda *a: (0, 0),
                 capabilities=Capabilities.all_verified())
    assert b.vehicle_gaps("NOPE", "cfd", "long", 1) == ["unknown_line"]


# ------------------------------------------------------------------------------ CLI
def test_ops_attest_and_capabilities_are_operator_only_and_pinned():
    from council.cli import OPERATOR_COMMANDS, app

    assert OPERATOR_COMMANDS["ops attest"] is True and OPERATOR_COMMANDS["ops capabilities"] is True
    runner = CliRunner()
    for args in (["ops", "attest", "terms-version"], ["ops", "capabilities"]):
        result = runner.invoke(app, args)
        assert result.exit_code != 0 and "refused" in result.output.lower()


def test_ops_attest_refuses_unknown_items(monkeypatch):
    from council import cli

    monkeypatch.setattr(cli, "require_operator", lambda *a, **k: None)
    runner = CliRunner()
    for args, needle in ((["teleport"], "unknown attestation"),
                         (["fee-charged-on"], "fee-charged-on takes"),
                         (["mirror-copied"], "--decision"),
                         (["etoro-licence"], "--ref")):
        result = runner.invoke(cli.app, ["ops", "attest", *args])
        assert result.exit_code != 0 and needle in result.output, result.output


def test_fee_location_gate_compares_with_costs_yaml(monkeypatch):
    from council import cli

    assert cli._fee_location_gate("virtual,mirror")["state"] == "green"
    assert cli._fee_location_gate("mirror")["state"] == "amber"
