"""Ledger: schema, decision transitions, priority/supersession, atomic expiry, legs, blockers,
operator-only approval, cycle queries and runtime state."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from council.ledger.db import (
    ApprovalRefused,
    InvalidTransition,
    Ledger,
    LedgerError,
    PriorityConflict,
)
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


def _step(ledger, decision_id, state, reason="step"):
    """Transition as the operator would for approvals (only the operator may approve)."""
    actor = "operator" if state == "approved" else "system"
    return ledger.transition(decision_id, state, reason, actor=actor)


# ------------------------------------------------------------------------------ schema
def test_migrate_creates_wal_schema(ledger):
    assert ledger.journal_mode() == "wal"
    conn = sqlite3.connect(ledger.path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "cycles", "role_calls", "decisions", "decision_events", "legs", "positions_observed",
        "broker_events", "equity_marks", "runtime_state",
    } <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert ledger.migrate() == 2                     # idempotent
    columns = {r[1] for r in conn.execute("PRAGMA table_info(decision_events)")}
    assert {"actor", "process_role"} <= columns


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
        _step(ledger, "d1", state, f"to {state}")
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
        _step(ledger, "d1", state)
    with pytest.raises(InvalidTransition):
        ledger.transition("d1", bad, "illegal", actor="operator")


def test_unknown_decision_and_state(ledger, fclock):
    with pytest.raises(LedgerError):
        ledger.transition("nope", "approved", "x", actor="operator")
    _new(ledger, fclock)
    with pytest.raises(InvalidTransition):
        ledger.transition("d1", "exploded", "x")


def test_execution_unknown_resolves_or_is_reviewed(ledger, fclock):
    _new(ledger, fclock)
    for state in ("approved", "executing", "execution_unknown", "completed_partial"):
        _step(ledger, "d1", state)
    _new(ledger, fclock, "d2")
    for state in ("approved", "executing", "blocked", "reviewed_no_action"):
        _step(ledger, "d2", state)


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
    _step(ledger, "r1", "approved", "ok")
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
    _step(ledger, "r1", "approved", "approved before expiry")        # approved: not pending
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
        "created_at": ledger.events("a")[-1]["created_at"], "actor": "system", "process_role": "dev",
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
    _step(ledger, "d1", "approved", "ok")
    _step(ledger, "d1", "executing", "go")
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


# ------------------------------------------------------------------------------ operator-only approval
@pytest.mark.parametrize("actor", ["system", "runner", "executor", "watch", "Operator", "operator "])
def test_only_the_operator_actor_may_approve(ledger, fclock, actor):
    _new(ledger, fclock)
    with pytest.raises(ApprovalRefused):
        ledger.transition("d1", "approved", "sneaky", actor=actor)
    with pytest.raises(ApprovalRefused):
        ledger.transition("d1", "approved", "default actor")          # default is "system"
    assert ledger.get_decision("d1").state == "proposed"
    assert [e["to_state"] for e in ledger.events("d1")] == ["proposed"]   # nothing logged


def test_events_record_actor_and_process_role(ledger, fclock, monkeypatch):
    _new(ledger, fclock)
    monkeypatch.setenv("COUNCIL_ROLE", "operator")
    ledger.transition("d1", "approved", "looks right", actor="operator")
    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    ledger.transition("d1", "executing", "go", actor="executor")
    ledger.note("d1", "still going", actor="executor")
    events = ledger.events("d1")
    assert [(e["to_state"], e["actor"], e["process_role"]) for e in events] == [
        ("proposed", "system", "dev"), ("approved", "operator", "operator"),
        ("executing", "executor", "dev"), ("executing", "executor", "dev"),
    ]
    with pytest.raises(LedgerError):
        ledger.transition("d1", "completed", "x", actor="")


def test_migrates_a_v1_ledger_in_place(tmp_path, fclock):
    path = tmp_path / "state" / "old.sqlite3"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE decisions (decision_id TEXT PRIMARY KEY, cycle_id TEXT, kind TEXT NOT NULL,
            priority INTEGER NOT NULL, state TEXT NOT NULL, prev_state TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, valid_until TEXT NOT NULL, target_json TEXT NOT NULL DEFAULT '{}',
            plan_json TEXT, commitment_sha TEXT, published_commit TEXT, superseded_by TEXT);
        CREATE TABLE decision_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL REFERENCES decisions (decision_id), from_state TEXT,
            to_state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        INSERT INTO decisions VALUES ('old', NULL, 'rebalance', 1, 'proposed', NULL,
            '2026-10-01T14:40:00.000000Z', '2026-10-01T14:40:00.000000Z', '2026-10-01T18:40:00.000000Z',
            '{}', NULL, NULL, NULL, NULL);
        INSERT INTO decision_events (decision_id, from_state, to_state, reason, created_at)
            VALUES ('old', NULL, 'proposed', 'created', '2026-10-01T14:40:00.000000Z');
        PRAGMA user_version = 1;
    """)
    conn.close()
    ledger = Ledger(path, clock=fclock.now)
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 2
    (legacy,) = ledger.events("old")
    assert legacy["actor"] == "unrecorded" and legacy["process_role"] is None
    ledger.transition("old", "approved", "ok", actor="operator")
    assert ledger.events("old")[-1]["actor"] == "operator"
    assert ledger.migrate() == 2


# ------------------------------------------------------------------------------ leg lines
def test_insert_legs_takes_the_line_from_the_leg_then_the_universe(ledger, fclock):
    _new(ledger, fclock)
    legs = [
        open_leg(1, "NSDQ100").model_copy(update={"line": "NDX", "whole_units": True, "cost_bps_nav": 0.5}),
        open_leg(2, "SPX500"),                                       # no line: universe map
        open_leg(3, "GOLD").model_copy(update={"line": "GOLD"}),
        open_leg(4, "EURUSD").model_copy(update={"symbol": "UNMAPPED_77"}),
    ]
    ledger.insert_legs("d1", legs, line_of={"NSDQ100": "WRONG"})     # Leg.line wins over line_of
    rows = {r.seq: r for r in ledger.legs("d1")}
    assert [rows[i].line for i in (1, 2, 3, 4)] == ["NDX", "SPX", "GOLD", "UNMAPPED_77"]
    assert rows[1].detail["whole_units"] is True and rows[1].detail["cost_bps_nav"] == 0.5
    assert rows[2].detail["whole_units"] is False


def test_insert_legs_is_idempotent_only_for_the_same_planned_legs(ledger, fclock):
    _new(ledger, fclock)
    legs = [open_leg(1, "SPX500"), open_leg(2, "GOLD", depends_on=(1,))]
    ledger.insert_legs("d1", legs)
    ledger.insert_legs("d1", list(reversed(legs)))                   # same legs: a no-op
    assert [r.seq for r in ledger.legs("d1")] == [1, 2]
    for changed in (
        [legs[0]],                                                   # fewer legs
        [legs[0], open_leg(2, "GOLD", units=11.0, depends_on=(1,))],
        [legs[0], open_leg(2, "GOLD")],                              # dependency dropped
        [legs[0].model_copy(update={"line": "NDX"}), legs[1]],
    ):
        with pytest.raises(LedgerError):
            ledger.insert_legs("d1", changed)
    ledger.update_leg("d1", 1, state="submitting", request_id="r-1")
    with pytest.raises(LedgerError):
        ledger.insert_legs("d1", legs)                               # no longer all planned


def test_insert_legs_refuses_a_symbol_without_a_line(ledger, fclock):
    _new(ledger, fclock)
    stray = open_leg(1, "SPX500").model_copy(update={"symbol": "FOO.L"})
    with pytest.raises(LedgerError):
        ledger.insert_legs("d1", [stray])
    assert ledger.legs("d1") == []                                   # nothing written
    ledger.insert_legs("d1", [stray], line_of=lambda s: "SPX" if s == "FOO.L" else None)
    assert ledger.get_leg("d1", 1).line == "SPX"


# ------------------------------------------------------------------------------ cycle queries
def _leg(seq, kind, symbol, before, after, *, cost=0.0):
    from council.models.plan import Leg

    return Leg(
        seq=seq, kind=kind, symbol=symbol, direction="long", weight_before=before,
        weight_after=after, risk_increasing=kind == "open", cost_bps_nav=cost, units=10.0,
        instrument_id=1, position_id=1 if kind != "open" else None,
    )


def _resolve(ledger, decision_id, seq, state, fclock, **detail):
    ledger.update_leg(decision_id, seq, state="submitting", request_id=f"{decision_id}-{seq}")
    ledger.update_leg(decision_id, seq, state=state, resolved_at=fclock.now(), detail=detail)


def _executing(ledger, fclock, decision_id, kind="rebalance"):
    _new(ledger, fclock, decision_id, kind=kind)
    _step(ledger, decision_id, "approved")
    _step(ledger, decision_id, "executing")


def test_last_change_turnover_and_cost_from_resolved_legs(ledger, fclock):
    t0 = fclock.now()
    _executing(ledger, fclock, "d1")
    ledger.insert_legs("d1", [
        _leg(1, "close", "NSDQ100", 0.2, 0.0, cost=1.0),
        _leg(2, "open", "SPX500", 0.0, 0.1, cost=0.5),
        _leg(3, "open", "GOLD", 0.0, 0.1, cost=0.4),
        _leg(4, "open", "EURUSD", 0.0, -0.1, cost=0.3),
        _leg(5, "modify_sl", "SPX500", 0.1, 0.1),
        _leg(6, "open", "GBPUSD", 0.0, 0.05, cost=0.2),
    ])
    fclock.advance(60)
    _resolve(ledger, "d1", 1, "filled", fclock)
    t_ndx = fclock.now()
    fclock.advance(60)
    _resolve(ledger, "d1", 2, "filled", fclock)
    t_spx = fclock.now()
    _resolve(ledger, "d1", 3, "partially_filled", fclock, units_sent=10.0, units_filled=5.0)
    _resolve(ledger, "d1", 4, "rejected", fclock)                  # nothing moved
    fclock.advance(60)
    _resolve(ledger, "d1", 5, "filled", fclock)                    # stop change: not exposure
    ledger.update_leg("d1", 6, state="skipped")                    # never sent
    assert ledger.last_change("NDX") == t_ndx and ledger.last_change("SPX") == t_spx
    assert ledger.last_change("EURUSD") is None and ledger.last_change("GBPUSD") is None
    assert ledger.last_changes() == {"NDX": t_ndx, "SPX": t_spx, "GOLD": t_spx}
    assert ledger.turnover_since(t0) == pytest.approx(0.2 + 0.1 + 0.05)
    assert ledger.turnover_since(t_spx) == pytest.approx(0.1 + 0.05)
    assert ledger.turnover_since(fclock.now() + timedelta(seconds=1)) == 0.0
    assert ledger.turnover_since(t0, kinds=("rebalance",)) == pytest.approx(0.35)
    assert ledger.turnover_since(t0, kinds=("flatten", "compliance")) == 0.0
    assert ledger.cost_bps_since(t0) == pytest.approx(1.0 + 0.5 + 0.2)


def test_partial_fill_without_fill_detail_counts_in_full(ledger, fclock):
    t0 = fclock.now()
    _executing(ledger, fclock, "d1")
    ledger.insert_legs("d1", [_leg(1, "open", "SPX500", 0.0, 0.1)])
    _resolve(ledger, "d1", 1, "rejected_partial", fclock)
    assert ledger.turnover_since(t0) == pytest.approx(0.1)          # conservative for R13
    assert ledger.last_change("SPX") == fclock.now()


def test_stop_hits_since_latest_per_line(ledger, fclock):
    t0 = fclock.now()
    ledger.record_stop_hit(line="SPX", symbol="SPX500", position_id=1, at=t0)
    ledger.record_broker_event(kind="stop_hit", payload={"symbol": "NSDQ100"}, now=t0 + timedelta(hours=1))
    ledger.record_broker_event(kind="stop_hit", payload={"symbol": "UNMAPPED_5"}, now=t0 + timedelta(hours=1))
    ledger.record_broker_event(kind="stop_hit", payload={}, now=t0 + timedelta(hours=1))   # unusable
    ledger.record_broker_event(kind="write_accepted", payload={"line": "GOLD"}, now=t0 + timedelta(hours=1))
    ledger.record_stop_hit(line="SPX", at=t0 + timedelta(hours=2))
    assert ledger.stop_hits_since(t0) == {
        "SPX": t0 + timedelta(hours=2), "NDX": t0 + timedelta(hours=1),
        "UNMAPPED_5": t0 + timedelta(hours=1),
    }
    assert ledger.stop_hits_since(t0 + timedelta(minutes=90)) == {"SPX": t0 + timedelta(hours=2)}
    with pytest.raises(LedgerError):
        ledger.record_stop_hit(line="")


# ------------------------------------------------------------------------------ runtime state
def test_nav_state_roundtrip(ledger, fclock):
    from council.risk.nav import start_nav, update_nav

    assert ledger.get_nav_state() is None
    state = update_nav(start_nav(10_000.0, fclock.now()), 10_500.0, fclock.now() + timedelta(hours=4))
    ledger.set_nav_state(state)
    assert ledger.get_nav_state() == state
    ledger.set_nav_state(state.model_dump(mode="json"))              # a mapping is validated too
    assert ledger.get_nav_state().peak == 10_500.0


def test_kill_state_roundtrip_and_validation(ledger, fclock):
    assert ledger.get_kill_state() == "NORMAL" and ledger.get_kill_state_record() is None
    ledger.set_kill_state("HALTED", reason="equity below halt line")
    assert ledger.get_kill_state() == "HALTED"
    record = ledger.get_kill_state_record()
    assert record["reason"] == "equity below halt line" and record["at"]
    with pytest.raises(LedgerError):
        ledger.set_kill_state("PANIC")
    ledger.set_runtime("kill_state", {"state": "WARN"})               # bare legacy shape
    assert ledger.get_kill_state() == "WARN"
    ledger.set_runtime("kill_state", "BOGUS")
    with pytest.raises(LedgerError):
        ledger.get_kill_state()


def test_material_fingerprint_roundtrip(ledger):
    assert ledger.get_material_fingerprint() is None
    ledger.set_material_fingerprint("sha256:abc")
    assert ledger.get_material_fingerprint() == "sha256:abc"
    assert ledger.get_runtime("last_material_fingerprint") == "sha256:abc"
    with pytest.raises(LedgerError):
        ledger.set_material_fingerprint("")
