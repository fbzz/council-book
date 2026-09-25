"""Ledger: schema, decision transitions, priority/supersession, atomic expiry, legs, blockers."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from council.ledger.db import InvalidTransition, Ledger, LedgerError, PriorityConflict
from council.ledger.states import (
    ALLOWED_TRANSITIONS,
    DECISION_STATES,
    LEG_STATES,
    LEG_TRANSITIONS,
    PRIORITY,
    TERMINAL_STATES,
)
from council.models.broker import Position
from council.models.cycle import CycleRecord, RoleCall
from council.paths import REPO_ROOT
from tests.execution.helpers import open_leg


def _new(ledger, fclock, decision_id="d1", kind="rebalance", ttl_h=4.0, **kw):
    return ledger.create_decision(
        decision_id=decision_id, kind=kind, valid_until=fclock.now() + timedelta(hours=ttl_h), **kw
    )


# ------------------------------------------------------------------------------ schema
def test_migrate_creates_wal_schema(ledger):
    assert ledger.journal_mode() == "wal"
    conn = sqlite3.connect(ledger.path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "cycles", "role_calls", "decisions", "decision_events", "legs", "positions_observed",
        "broker_events", "equity_marks", "runtime_state",
    } <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert ledger.migrate() == 1                     # idempotent


def test_ledger_refuses_a_path_inside_the_repo():
    with pytest.raises(RuntimeError):
        Ledger(REPO_ROOT / "ledger.sqlite3")


def test_state_machines_cover_every_model_state():
    assert set(ALLOWED_TRANSITIONS) == DECISION_STATES
    assert set(LEG_TRANSITIONS) == LEG_STATES
    assert all(not ALLOWED_TRANSITIONS[s] for s in TERMINAL_STATES)


def test_priority_matches_policy_order(policy):
    order = policy.risk["priority"]
    assert sorted(PRIORITY, key=PRIORITY.get, reverse=True) == order


# ------------------------------------------------------------------------------ transitions
def test_allowed_transition_path_and_events(ledger, fclock):
    _new(ledger, fclock)
    for state in ("approved", "executing", "completed"):
        ledger.transition("d1", state, f"to {state}")
    assert ledger.get_decision("d1").state == "completed"
    assert [e["to_state"] for e in ledger.events("d1")] == ["proposed", "approved", "executing", "completed"]


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        ((), "executing"),                          # proposed cannot execute without approval
        ((), "completed"),
        (("approved",), "completed"),               # must execute first
        (("approved", "executing", "completed"), "executing"),   # terminal never moves
        (("rejected",), "approved"),
        (("approved", "executing", "blocked"), "completed"),     # blocked needs operator review
    ],
)
def test_forbidden_transitions_raise(ledger, fclock, path, bad):
    _new(ledger, fclock)
    for state in path:
        ledger.transition("d1", state, "step")
    with pytest.raises(InvalidTransition):
        ledger.transition("d1", bad, "illegal")


def test_unknown_decision_and_state(ledger, fclock):
    with pytest.raises(LedgerError):
        ledger.transition("nope", "approved", "x")
    _new(ledger, fclock)
    with pytest.raises(InvalidTransition):
        ledger.transition("d1", "exploded", "x")


def test_execution_unknown_resolves_or_is_reviewed(ledger, fclock):
    _new(ledger, fclock)
    for state in ("approved", "executing", "execution_unknown", "completed_partial"):
        ledger.transition("d1", state, "step")
    _new(ledger, fclock, "d2")
    for state in ("approved", "executing", "blocked", "reviewed_no_action"):
        ledger.transition("d2", state, "step")


# ------------------------------------------------------------------------------ priority
def test_new_rebalance_supersedes_older_pending_rebalance(ledger, fclock):
    _new(ledger, fclock, "r1")
    assert _new(ledger, fclock, "r2") == ["r1"]
    old = ledger.get_decision("r1")
    assert old.state == "superseded" and old.superseded_by == "r2"
    assert [d.decision_id for d in ledger.pending()] == ["r2"]


def test_rebalance_never_supersedes_pending_flatten(ledger, fclock):
    _new(ledger, fclock, "f1", kind="flatten")
    with pytest.raises(PriorityConflict):
        _new(ledger, fclock, "r1")
    with pytest.raises(PriorityConflict):
        _new(ledger, fclock, "c1", kind="compliance")
    assert ledger.get_decision("f1").state == "proposed"
    with pytest.raises(LedgerError):
        ledger.get_decision("r1")                    # nothing written


def test_compliance_supersedes_rebalance_but_not_flatten(ledger, fclock):
    _new(ledger, fclock, "r1")
    assert _new(ledger, fclock, "c1", kind="compliance") == ["r1"]
    with pytest.raises(PriorityConflict):
        _new(ledger, fclock, "r2")
    assert sorted(_new(ledger, fclock, "f1", kind="flatten")) == ["c1"]
    assert [d.decision_id for d in ledger.pending()] == ["f1"]


def test_flatten_supersedes_older_flatten(ledger, fclock):
    _new(ledger, fclock, "f1", kind="flatten")
    assert _new(ledger, fclock, "f2", kind="flatten") == ["f1"]


def test_approved_decisions_are_never_superseded(ledger, fclock):
    _new(ledger, fclock, "r1")
    ledger.transition("r1", "approved", "ok")
    assert _new(ledger, fclock, "r2") == []
    assert ledger.get_decision("r1").state == "approved"


def test_unknown_kind_and_non_pending_start_refused(ledger, fclock):
    with pytest.raises(LedgerError):
        _new(ledger, fclock, "x", kind="yolo")
    with pytest.raises(LedgerError):
        _new(ledger, fclock, "x", state="approved")


# ------------------------------------------------------------------------------ expiry
def test_expire_stale_expires_only_past_pending(ledger, fclock):
    _new(ledger, fclock, "r1", ttl_h=1)
    ledger.transition("r1", "approved", "approved before expiry")     # approved: not pending
    _new(ledger, fclock, "c1", kind="compliance", ttl_h=1)
    fclock.advance(3600)                                              # at both valid_untils
    assert ledger.expire_stale() == ["c1"]
    assert ledger.get_decision("r1").state == "approved"
    _new(ledger, fclock, "f1", kind="flatten", ttl_h=3)
    fclock.advance(3600)
    assert ledger.expire_stale() == []                                # f1 still valid
    assert [d.decision_id for d in ledger.pending()] == ["f1"]


def test_expire_stale_boundary_and_events(ledger, fclock):
    _new(ledger, fclock, "a", ttl_h=1)
    fclock.advance(3599)
    assert ledger.expire_stale() == []                                # 1 s before: stays
    fclock.advance(1)
    assert ledger.expire_stale() == ["a"]                             # at valid_until: expires
    assert ledger.get_decision("a").state == "expired"
    assert ledger.events("a")[-1] == {
        "from_state": "proposed", "to_state": "expired", "reason": "valid_until passed",
        "created_at": ledger.events("a")[-1]["created_at"],
    }
    assert ledger.expire_stale() == []


def test_expire_stale_is_a_single_update_returning(ledger, fclock, monkeypatch):
    statements: list[str] = []
    original = ledger._connect

    def traced():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(ledger, "_connect", traced)
    _new(ledger, fclock, "a", ttl_h=1)
    _new(ledger, fclock, "b", kind="compliance", ttl_h=1)
    fclock.advance(7200)
    statements.clear()
    assert ledger.expire_stale() == ["b"]
    updates = [s for s in statements if s.lstrip().upper().startswith("UPDATE")]
    assert len(updates) == 1 and "RETURNING" in updates[0]


# ------------------------------------------------------------------------------ blockers
def test_has_blocker_tracks_blocked_unknown_and_unknown_legs(ledger, fclock):
    assert not ledger.has_blocker()
    _new(ledger, fclock, "d1")
    ledger.transition("d1", "approved", "ok")
    ledger.transition("d1", "executing", "go")
    ledger.insert_legs("d1", [open_leg(1, "SPX500")])
    ledger.update_leg("d1", 1, state="submitting", request_id="r-1")
    ledger.update_leg("d1", 1, state="unknown")
    assert ledger.blockers() == ["d1"]                                # leg unknown
    ledger.transition("d1", "blocked", "post-fill mismatch")
    assert ledger.has_blocker()
    ledger.transition("d1", "reviewed_no_action", "operator adopted")
    assert ledger.blockers() == ["d1"]                                # the unknown leg still counts
    ledger.update_leg("d1", 1, state="filled")
    assert not ledger.has_blocker()


# ------------------------------------------------------------------------------ legs
def test_leg_rows_follow_the_leg_state_machine(ledger, fclock):
    _new(ledger, fclock)
    ledger.insert_legs("d1", [open_leg(1, "SPX500"), open_leg(2, "GOLD")], line_of={"SPX500": "SPX"})
    leg = ledger.get_leg("d1", 1)
    assert (leg.state, leg.line, leg.vehicle_symbol) == ("planned", "SPX", "SPX500")
    assert leg.detail["weight_after"] == pytest.approx(0.1001)
    with pytest.raises(InvalidTransition):
        ledger.update_leg("d1", 1, state="filled")                   # planned -> filled skips submit
    ledger.update_leg("d1", 1, state="submitting", request_id="r-1", attempt=0, submitted_at=fclock.now())
    ledger.update_leg("d1", 1, state="submitting", request_id="r-2", attempt=1)   # new attempt
    ledger.update_leg("d1", 1, state="submitted", order_id=5001, detail={"a": 1})
    ledger.update_leg("d1", 1, state="filled", position_ids=[1001], detail={"b": 2})
    leg = ledger.get_leg("d1", 1)
    assert leg.state == "filled" and leg.request_id == "r-2" and leg.position_ids == [1001]
    assert leg.detail["a"] == 1 and leg.detail["b"] == 2              # detail merges
    with pytest.raises(InvalidTransition):
        ledger.update_leg("d1", 1, state="unknown")                  # terminal never moves
    with pytest.raises(LedgerError):
        ledger.update_leg("d1", 2, state="submitting", request_id="r-2")   # request id is unique
    with pytest.raises(LedgerError):
        ledger.update_leg("d1", 2, nonsense=1)
    assert ledger.leg_by_request_id("r-2").seq == 1
    with pytest.raises(LedgerError):
        ledger.insert_legs("d1", [open_leg(3, "SPX500")])            # legs are written once


# ------------------------------------------------------------------------------ misc tables
def test_cycles_role_calls_runtime_and_marks(ledger, fclock, slot):
    record = CycleRecord(
        cycle_id="2026-10-01T1440Z", slot=slot, started_at=slot, status="on_time", mode="stub",
        policy_sha="x" * 64, model="stub",
    )
    assert not ledger.cycle_exists(record.cycle_id)
    ledger.record_cycle(record)
    ledger.record_cycle(record.model_copy(update={"status": "late"}))
    assert ledger.cycle_exists(record.cycle_id)
    assert ledger.get_cycle(record.cycle_id)["status"] == "late"
    call = RoleCall(role="pm", prompt_id="pm", prompt_sha="s", input_hash="h", status="ok")
    ledger.record_role_call(record.cycle_id, call)
    assert ledger.role_calls(record.cycle_id)[0]["role"] == "pm"

    ledger.set_runtime("kill_state", {"state": "NORMAL"})
    assert ledger.get_runtime("kill_state") == {"state": "NORMAL"}
    assert ledger.get_runtime("missing", 7) == 7

    ledger.add_equity_mark(fclock.now(), 100.0, source="t")
    fclock.advance(60)
    ledger.add_equity_mark(fclock.now(), 101.0, flow_usd=5.0)
    assert ledger.latest_equity_mark()["equity_usd"] == 101.0
    assert len(ledger.equity_marks(since=fclock.now())) == 1


def test_broker_events_redact_credentials(ledger, fclock):
    ledger.record_broker_event(kind="write_accepted", decision_id="d1", seq=1, request_id="r",
                               payload={"token": "secret-value", "orderId": 1, "nested": {"x-user-key": "k"}})
    (event,) = ledger.broker_events("d1")
    assert event["payload"]["token"] == "[REDACTED]"
    assert event["payload"]["nested"]["x-user-key"] == "[REDACTED]"
    assert event["payload"]["orderId"] == 1


def test_positions_observed_roundtrip(ledger, fclock):
    p = Position(position_id=1, instrument_id=101, symbol="SPX500", is_buy=True, units=1.0,
                 open_rate=100.0, amount=100.0, sl_rate=90.0)
    ledger.record_positions(fclock.now(), [p], decision_id="d1", source="pre")
    (obs,) = ledger.positions_observed("d1")
    assert obs["positions"][0]["symbol"] == "SPX500"


def test_commitment_plan_and_publication_fields(ledger, fclock):
    _new(ledger, fclock, target={"SPX": 0.1})
    ledger.set_commitment("d1", "abc")
    ledger.set_published_commit("d1", "def")
    ledger.set_plan("d1", {"legs": []})
    row = ledger.get_decision("d1")
    assert (row.commitment_sha, row.published_commit, row.plan, row.target) == ("abc", "def", {"legs": []}, {"SPX": 0.1})
    with pytest.raises(LedgerError):
        ledger.set_commitment("nope", "x")
