"""Public execution record: percentages, bps and states from the PRIVATE ExecutionReport."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from council.execution.executor import ExecutionReport, LegResult
from council.execution.reconcile import ReconcileResult
from council.models.plan import Leg, Plan
from council.publish import journal, leakscan
from council.publish.public_models import PublicExecution
from council.publish.redact import public_execution
from tests.publish.conftest import (
    CANARIES,
    CYCLE_ID,
    PRIVATE_AMOUNT,
    PRIVATE_DECISION_ID,
    PRIVATE_INSTRUMENT_ID,
    PRIVATE_NAV,
    PRIVATE_POSITION_ID,
    PRIVATE_SL_RATE,
    PRIVATE_UNITS,
)

NAV = 2000.0
OPEN_PRICE = 50.0
OPEN_UNITS = 4.0              # 4 x 50 = 200 USD = 0.10 x NAV
FILL_PRICE = 50.1             # 20 bp adverse on a long


def _plan() -> Plan:
    return Plan(
        legs=[
            Leg(seq=1, kind="partial_close", symbol="SMH.L", line="SEMIS", instrument_id=PRIVATE_INSTRUMENT_ID,
                direction="long", settlement="real", leverage=1, weight_before=0.15, weight_after=0.075,
                cost_bps_nav=0.6, risk_increasing=False, amount_usd=PRIVATE_AMOUNT, units=PRIVATE_UNITS,
                position_id=PRIVATE_POSITION_ID),
            Leg(seq=2, kind="open", symbol="BTC", line="BTC", instrument_id=100000, direction="long",
                settlement="real", leverage=1, weight_before=0.03, weight_after=0.13, stop_distance=0.2,
                cost_bps_nav=1.5, risk_increasing=True, amount_usd=OPEN_UNITS * OPEN_PRICE, units=OPEN_UNITS,
                sl_rate=PRIVATE_SL_RATE),
            Leg(seq=3, kind="open", symbol="EURUSD", line="EURUSD", direction="short", settlement="cfd",
                leverage=1, weight_before=0.0, weight_after=-0.05, cost_bps_nav=0.4, risk_increasing=True,
                amount_usd=100.0, units=90.0),
        ],
        gross_before=0.93, gross_after=1.03, net_before=0.93, net_after=0.93, cost_bps_nav=2.5,
        carry_bps_day_nav=0.4,
    )


def _report(**overrides) -> ExecutionReport:
    legs = [
        LegResult(seq=1, kind="partial_close", symbol="SMH.L", line="SEMIS", state="filled", attempts=1,
                  order_id=PRIVATE_POSITION_ID + 1, position_ids=[PRIVATE_POSITION_ID], units_requested=PRIVATE_UNITS),
        LegResult(seq=2, kind="open", symbol="BTC", line="BTC", state="filled", attempts=1,
                  order_id=PRIVATE_POSITION_ID + 2, position_ids=[PRIVATE_POSITION_ID + 3],
                  units_requested=OPEN_UNITS, units_filled=OPEN_UNITS, fill_price=FILL_PRICE),
        LegResult(seq=3, kind="open", symbol="EURUSD", line="EURUSD", state="rejected", attempts=1,
                  units_requested=90.0, error=f"insufficient funds: {PRIVATE_NAV} USD available"),
        LegResult(seq=4, kind="open", symbol="XYZ", line=f"UNMAPPED_{PRIVATE_INSTRUMENT_ID}", state="skipped"),
    ]
    base = dict(
        decision_id=PRIVATE_DECISION_ID, final_state="completed_partial", legs=legs,
        reasons=[f"EURUSD: open rejected; remaining opens stopped ({PRIVATE_NAV})"],
        reconcile=ReconcileResult(ok=True, drift=0.0512, drift_max=0.1,
                                  achieved_w={"SEMIS": 0.0751, "BTC": 0.1302, "NDX": 0.35}),
        equity_before=PRIVATE_NAV, equity_after=PRIVATE_NAV + 1, writes_sent=3,
    )
    return ExecutionReport(**(base | overrides))


@pytest.fixture
def execution(policy) -> PublicExecution:
    return public_execution(
        _report(), cycle_id=CYCLE_ID, lines=policy.universe, nav_usd=NAV, plan=_plan(),
        approved_at=datetime(2026, 10, 1, 15, 7, 13, tzinfo=UTC), completed_at=datetime(2026, 10, 1, 15, 9, tzinfo=UTC),
    )


def test_fills_are_weights_bps_and_states(execution):
    by_seq = {f.seq: f for f in execution.fills}
    assert sorted(by_seq) == [1, 2, 3]                                   # the unmapped leg is dropped
    close, btc, eur = by_seq[1], by_seq[2], by_seq[3]
    assert (close.line, close.kind, close.direction, close.state) == ("SEMIS", "partial_close", "long", "filled")
    assert close.weight_target_x == -0.075 and close.weight_filled_x is None   # a close is confirmed, not measured
    assert (btc.weight_target_x, btc.weight_filled_x) == (0.1, 0.1)
    assert btc.slippage_bp == 20.0 and btc.cost_bp == 1.5 and btc.exposure_error_pct == 0.0
    assert (eur.direction, eur.state, eur.weight_target_x, eur.weight_filled_x) == ("short", "rejected", -0.05, None)


def test_execution_summary(execution):
    assert execution.decision_state == "completed_partial"
    assert execution.approved_slot == datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
    assert execution.completed_slot == datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
    assert execution.achieved_x == {"NDX": 0.35, "SEMIS": 0.075, "BTC": 0.13}
    assert execution.achieved_drift_x == 0.051
    assert execution.cost_bp_total == 2.1                              # executed legs only (0.6 + 1.5)
    assert execution.flags == ["unmapped_symbols_dropped:1"]


def test_short_slippage_is_signed_adverse(policy):
    report = _report(legs=[LegResult(seq=3, kind="open", symbol="EURUSD", line="EURUSD", state="filled",
                                     units_requested=90.0, units_filled=90.0, fill_price=100.0 / 90.0 * 0.999)])
    fill = public_execution(report, cycle_id=CYCLE_ID, lines=policy.universe, nav_usd=NAV, plan=_plan()).fills[0]
    assert fill.slippage_bp == 10.0                                    # sold 10 bp below plan: adverse
    assert fill.weight_filled_x == -0.05


def test_no_private_value_reaches_the_execution_record(execution):
    text = json.dumps(execution.model_dump(mode="json"))
    for private in (str(PRIVATE_NAV), str(PRIVATE_AMOUNT), str(PRIVATE_UNITS), str(PRIVATE_SL_RATE),
                    str(PRIVATE_POSITION_ID), str(PRIVATE_INSTRUMENT_ID), PRIVATE_DECISION_ID, str(NAV),
                    str(FILL_PRICE), "insufficient", "UNMAPPED_", "order", "position"):
        assert private not in text, private
    assert leakscan.scan(execution, canaries=[*CANARIES, NAV, FILL_PRICE]) == []


def test_without_the_plan_only_measured_fields_are_missing(policy):
    doc = public_execution(_report(), cycle_id=CYCLE_ID, lines=policy.universe, nav_usd=NAV)
    assert "plan_missing" in doc.flags and doc.cost_bp_total is None
    assert all(f.direction is None and f.weight_target_x is None and f.slippage_bp is None for f in doc.fills)
    assert [f.state for f in doc.fills] == ["filled", "filled", "rejected"]


def test_unknown_states_fail_closed_and_bad_nav_is_refused(policy):
    legs = [LegResult(seq=2, kind="open", symbol="BTC", line="BTC", state="weird"),
            LegResult(seq=5, kind="teleport", symbol="BTC", line="BTC", state="filled")]
    doc = public_execution(_report(legs=legs), cycle_id=CYCLE_ID, lines=policy.universe, nav_usd=NAV, plan=_plan())
    assert [(f.seq, f.state) for f in doc.fills] == [(2, "unknown")]
    assert {"leg_state_unrecognised:1", "leg_kind_unrecognised:1"} <= set(doc.flags)
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            public_execution(_report(), cycle_id=CYCLE_ID, lines=policy.universe, nav_usd=bad)


def test_execution_journal_file(execution):
    files = journal.execution_files(execution)
    data = files[journal.execution_path(CYCLE_ID)]
    assert journal.execution_path(CYCLE_ID) == "journal/executions/2026/10/2026-10-01T1440Z.json"
    assert PublicExecution.model_validate_json(data) == execution
    assert leakscan.scan_bytes("x.json", data, canaries=CANARIES) == []
