"""SW-2b: swing-book ledger wiring (design swing-book.md rev 2, SW-2 acceptance, §4.5, H9).

Trade states follow swing.models.TRANSITIONS; a trade is created from a FILLED leg whatever its
decision's state; one trade may hold several positions; a closed trade is immutable; a swing
entry_unknown blocks swing entries only; the 7-day purge scrubs feed text from swing rows by origin
cycle; the v4 -> v5 migration keeps every row (on a copy of a real-shaped ledger in tmp_path)."""

from __future__ import annotations

import itertools
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from council.cycle import evidence_lines
from council.ledger.db import SCHEMA_VERSION, Ledger, LedgerError
from council.ledger.purge import scrub_swing_rows
from council.ledger.states import (
    SWING_BLOCKER_PREFIX,
    TRADE_STATES,
    TRADE_TERMINAL_STATES,
    TRADE_TRANSITIONS,
    IllegalTransition,
    blocker_scope_of,
    can_transition_trade,
    check_trade_transition,
)
from council.swing.models import TRANSITIONS
from tests.execution.helpers import open_leg

T0 = datetime(2026, 10, 5, 14, 40, tzinfo=UTC)
SWING_TABLES = ("swing_ideas", "swing_trades", "swing_events", "benchmark_days", "paper_trades")


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "state" / "ledger.sqlite3", clock=lambda: T0)


def _decision(ledger: Ledger, decision_id: str = "d1", *, cycle_id: str = "c-2026-10-05") -> None:
    ledger.create_decision(decision_id=decision_id, kind="rebalance", valid_until=T0 + timedelta(hours=1),
                           cycle_id=cycle_id)
    ledger.transition(decision_id, "approved", "ok", actor="operator")
    ledger.transition(decision_id, "executing", "go", actor="executor")


def _swing_leg(ledger: Ledger, decision_id: str, seq: int, trade_id: str | None, *, symbol: str = "SPX500",
               direction: str = "long") -> None:
    ledger.insert_legs(decision_id, [open_leg(seq, symbol, direction=direction).model_copy(
        update={"line": "SWING_X"})])
    detail = {"sleeve": "swing", "tp_rate": 110.0, "time_stop_date": "2026-10-16", "ticker": "NVDA"}
    if trade_id:
        detail["swing_trade_id"] = trade_id
    ledger.update_leg(decision_id, seq, state="submitting", request_id=f"r-{decision_id}-{seq}", detail=detail)


def _fill(ledger: Ledger, decision_id: str, seq: int, *, state: str = "filled", ids=(11,), via=()) -> None:
    for step in via:
        ledger.update_leg(decision_id, seq, state=step)
    ledger.update_leg(decision_id, seq, state=state, position_ids=list(ids), resolved_at=T0)


# ------------------------------------------------------------------------------ states
def test_the_ledger_table_is_the_models_table_and_illegal_pairs_raise():
    assert TRADE_TRANSITIONS is TRANSITIONS and frozenset(TRANSITIONS) == TRADE_STATES
    for src, dst in itertools.product(sorted(TRADE_STATES), repeat=2):
        if can_transition_trade(src, dst):
            assert check_trade_transition(src, dst) == dst
        else:
            with pytest.raises(IllegalTransition):
                check_trade_transition(src, dst)
    with pytest.raises(IllegalTransition):
        check_trade_transition("open", "bogus")


def test_the_sql_state_check_lists_exactly_the_trade_states(ledger):
    sql = sqlite3.connect(ledger.path).execute(
        "SELECT sql FROM sqlite_master WHERE name = 'swing_trades'").fetchone()[0]
    listed = sql.split("state IN (", 1)[1].split(")", 1)[0]
    assert {s.strip(" '") for s in listed.split(",")} == TRADE_STATES


def test_an_illegal_ledger_transition_raises_and_writes_nothing(ledger):
    ledger.create_swing_trade("trade:t1", ticker="NVDA", side="long")
    with pytest.raises(IllegalTransition):
        ledger.transition_swing_trade("trade:t1", "open")          # proposed -> open skips entry
    assert ledger.swing_trade("trade:t1").state == "proposed"
    assert [e["to_state"] for e in ledger.swing_events("trade:t1")] == ["proposed"]
    with pytest.raises(LedgerError):
        ledger.create_swing_trade("t2", ticker="NVDA", side="long")   # id must be "trade:..."


# ------------------------------------------------------------------------------ fills
@pytest.mark.parametrize("end", ["blocked", "execution_unknown", "waiting_for_market", "resumed"])
def test_a_filled_leg_creates_its_trade_whatever_the_decision_state(ledger, end):
    _decision(ledger)
    _swing_leg(ledger, "d1", 1, "trade:t1")
    if end == "resumed":                                          # unknown, then resolved by resume
        ledger.update_leg("d1", 1, state="unknown")
        ledger.transition("d1", "execution_unknown", "timeout", actor="executor")
        ledger.transition("d1", "completed", "resume", actor="executor")
    else:
        ledger.transition("d1", end, "x", actor="executor")
    _fill(ledger, "d1", 1)
    trade = ledger.record_swing_fill("d1", 1)
    assert (trade.trade_id, trade.state, trade.side, trade.ticker) == ("trade:t1", "open", "long", "NVDA")
    assert trade.decision_id == "d1" and trade.entry_seq == 1 and trade.origin_cycle == "c-2026-10-05"
    assert trade.position_ids == [11] and trade.tp_rate == 110.0 and trade.sl_rate is not None


def test_one_trade_with_two_positions_and_a_repeat_fill_is_idempotent(ledger):
    _decision(ledger)
    ledger.create_swing_trade("trade:t1", ticker="NVDA", side="long", decision_id="d1", entry_seq=1)
    _swing_leg(ledger, "d1", 1, None)                             # linked by (decision, seq) only
    _fill(ledger, "d1", 1, ids=(11, 12))
    trade = ledger.record_swing_fill("d1", 1)
    assert trade.state == "open" and trade.position_ids == [11, 12]
    assert [e["to_state"] for e in ledger.swing_events("trade:t1")] == ["proposed", "entry_executing", "open"]
    again = ledger.record_swing_fill("d1", 1)
    assert again.state == "open" and again.position_ids == [11, 12]
    assert len(ledger.swing_events("trade:t1")) == 3
    assert ledger.update_swing_trade("trade:t1", position_ids=[12, 13]).position_ids == [11, 12, 13]


def test_a_partial_fill_makes_a_partial_trade_and_unfilled_or_non_swing_legs_refuse(ledger):
    _decision(ledger)
    _swing_leg(ledger, "d1", 1, "trade:t1", direction="short")
    with pytest.raises(LedgerError):
        ledger.record_swing_fill("d1", 1)                          # still submitting
    _fill(ledger, "d1", 1, state="partially_filled")
    trade = ledger.record_swing_fill("d1", 1)
    assert trade.state == "partial" and trade.side == "short"
    _decision(ledger, "d2")
    ledger.insert_legs("d2", [open_leg(1, "GOLD")])
    ledger.update_leg("d2", 1, state="submitting", request_id="r-core")
    _fill(ledger, "d2", 1)
    with pytest.raises(LedgerError, match="not a swing leg"):
        ledger.record_swing_fill("d2", 1)


def test_a_closed_trade_is_immutable_even_to_raw_sql(ledger):
    _decision(ledger)
    _swing_leg(ledger, "d1", 1, "trade:t1")
    _fill(ledger, "d1", 1)
    ledger.record_swing_fill("d1", 1)
    ledger.transition_swing_trade("trade:t1", "exit_pending")
    closed = ledger.transition_swing_trade("trade:t1", "closed_time", close_rate=104.0)
    assert closed.state == "closed_time" and closed.closed_at is not None and closed.close_rate == 104.0
    for dst in sorted(TRADE_STATES):
        with pytest.raises(IllegalTransition):
            ledger.transition_swing_trade("trade:t1", dst)
    with pytest.raises(IllegalTransition):
        ledger.update_swing_trade("trade:t1", tp_rate=1.0)
    with pytest.raises(IllegalTransition):
        ledger.record_swing_fill("d1", 1)
    conn = sqlite3.connect(ledger.path)
    for sql in ("UPDATE swing_trades SET state = 'open'", "DELETE FROM swing_trades"):
        with pytest.raises(sqlite3.DatabaseError, match="terminal"):
            conn.execute(sql)
    assert ledger.swing_trade("trade:t1").state == "closed_time"
    assert "missed" in TRADE_TERMINAL_STATES


# ------------------------------------------------------------------------------ blocker scope
def test_a_swing_entry_unknown_blocks_swing_entries_only(ledger):
    ledger.create_swing_trade("trade:t1", ticker="NVDA", side="long")
    ledger.transition_swing_trade("trade:t1", "entry_executing")
    ledger.transition_swing_trade("trade:t1", "entry_unknown")
    assert ledger.blockers() == ["swing:trade:t1"] == ledger.swing_blockers()
    assert ledger.swing_entries_blocked()
    assert [blocker_scope_of(b) for b in ledger.blockers()] == ["swing"]     # never "all"
    policy = SimpleNamespace(universe=SimpleNamespace(by_symbol=dict))
    pack = SimpleNamespace(admitted=["NDX", "SPX"])
    assert evidence_lines(policy, pack, ledger.blockers()) == ["NDX", "SPX"]  # the core still acts
    ledger.transition_swing_trade("trade:t1", "open")
    assert ledger.blockers() == [] and not ledger.swing_entries_blocked()


def test_a_decision_is_swing_scoped_only_while_every_unresolved_leg_is_swing(ledger):
    _decision(ledger)
    ledger.insert_legs("d1", [open_leg(1, "GOLD"), open_leg(2, "SPX500").model_copy(update={"line": "SW"})])
    ledger.update_leg("d1", 1, state="submitting", request_id="r1")
    ledger.update_leg("d1", 2, state="submitting", request_id="r2", detail={"sleeve": "swing"})
    ledger.update_leg("d1", 2, state="unknown")
    ledger.update_leg("d1", 1, state="unknown")
    ledger.transition("d1", "execution_unknown", "timeout", actor="executor")
    with pytest.raises(LedgerError, match="non-swing"):
        ledger.set_blocker_scope("d1", "swing")
    assert ledger.blockers() == ["d1"]                             # a core unknown halts everything
    _fill(ledger, "d1", 1)                                         # the core leg resolves
    ledger.set_blocker_scope("d1", "swing")
    assert ledger.blockers() == [f"{SWING_BLOCKER_PREFIX}d1"] and ledger.swing_entries_blocked()


def test_a_swing_scope_with_nothing_unresolved_holds_the_whole_book(ledger):
    """Review fix: a blocked decision whose every leg resolved (e.g. a core post-fill exposure
    mismatch) is not a swing hold, whatever scope was recorded while a swing leg was waiting."""
    _decision(ledger)
    _swing_leg(ledger, "d1", 1, "trade:t1")
    ledger.update_leg("d1", 1, state="submitted")
    ledger.update_leg("d1", 1, state="waiting_for_market")
    ledger.set_blocker_scope("d1", "swing")
    ledger.transition("d1", "waiting_for_market", "held", actor="executor")
    assert ledger.blockers() == ["swing:d1"]
    ledger.transition("d1", "blocked", "timeout", actor="watch")
    assert ledger.blockers() == ["swing:d1"]                       # the swing leg still waits
    _fill(ledger, "d1", 1)
    assert ledger.blockers() == ["d1"]                             # resolved: the whole book is held
    with pytest.raises(LedgerError, match="non-swing"):
        ledger.set_blocker_scope("d1", "swing")


def test_a_planned_unmarked_leg_keeps_a_decision_whole_book(ledger):
    _decision(ledger)
    ledger.insert_legs("d1", [open_leg(1, "GOLD"), open_leg(2, "SPX500").model_copy(update={"line": "SW"})])
    ledger.update_leg("d1", 2, state="submitting", request_id="r2", detail={"sleeve": "swing"})
    ledger.update_leg("d1", 2, state="unknown")                    # leg 1 (core) never left `planned`
    ledger.transition("d1", "execution_unknown", "died", actor="executor")
    with pytest.raises(LedgerError, match="non-swing"):
        ledger.set_blocker_scope("d1", "swing")
    assert ledger.blockers() == ["d1"]


def test_a_fill_must_match_the_trade_it_names(ledger):
    ledger.create_swing_trade("trade:t1", ticker="NVDA", side="short")
    _decision(ledger)
    _swing_leg(ledger, "d1", 1, "trade:t1", direction="long")
    _fill(ledger, "d1", 1)
    with pytest.raises(LedgerError, match="does not match"):
        ledger.record_swing_fill("d1", 1)
    assert ledger.swing_trade("trade:t1").state == "proposed"


def test_trade_detail_refuses_free_text_because_a_closed_trade_cannot_be_scrubbed(ledger):
    with pytest.raises(LedgerError, match="free text"):
        ledger.create_swing_trade("trade:t1", ticker="NVDA", side="long",
                                  detail={"why": "Company beats estimates, raises guidance"})
    ledger.create_swing_trade("trade:t1", ticker="NVDA", side="long",
                              detail={"leg_ids": ["d1:1"], "r_net": 1.4, "tp_at_broker": False})
    with pytest.raises(LedgerError, match="free text"):
        ledger.update_swing_trade("trade:t1", detail={"note": "a sentence copied from a headline"})
    assert ledger.swing_trade("trade:t1").detail == {"leg_ids": ["d1:1"], "r_net": 1.4,
                                                      "tp_at_broker": False}


def test_trade_events_are_keyed_for_the_purge_by_trade_and_by_writing_cycle(ledger):
    feed = "an exact copied feed headline text"
    ledger.create_swing_trade("trade:t1", ticker="NVDA", side="long", origin_cycle="c1")
    ledger.add_swing_event("time_stop_due", trade_id="trade:t1", reason=feed)   # trade id only
    ledger.transition_swing_trade("trade:t1", "missed", reason=feed, cycle_id="c3")
    with pytest.raises(LedgerError):
        ledger.add_swing_event("note", trade_id="trade:nope", reason="x")
    with pytest.raises(LedgerError, match="purge"):
        ledger.add_swing_event("note", reason=feed)                 # text nobody could scrub
    hits = lambda text: feed in text   # noqa: E731
    assert scrub_swing_rows(ledger, "c1", hits, "[x]") == 2        # the flag, and c3's (via trade)
    assert [e["reason"] for e in ledger.swing_events("trade:t1")] == ["created", "[x]", "[x]"]
    assert [e["origin_cycle"] for e in ledger.swing_events("trade:t1")] == ["c1", "c1", "c3"]


def test_benchmark_detail_never_carries_prices_units_or_ids(ledger):
    for detail in ({"spy_price": 512.3}, {"legs": [{"units": 3}]}, {"x": {"position_id": 7}}):
        with pytest.raises(LedgerError, match="private"):
            ledger.record_benchmark_day("2026-10-05", sq8_ret=0.0, matched_idx_ret=0.0, idx_hold_ret=0.0,
                                        detail=detail)
    ledger.record_benchmark_day("2026-10-05", sq8_ret=0.0, matched_idx_ret=0.0, idx_hold_ret=0.0,
                                detail={"sq8_index": 100.0, "sq8_drawdown": 0.0, "names": ["NVDA"],
                                        "traded": [], "cost": 0.0})


# ------------------------------------------------------------------------------ purge
def test_scrub_swing_rows_by_origin_and_carry_cycle(ledger):
    feed = "an exact copied feed headline text"
    ledger.add_swing_idea("idea:a", origin_cycle="c1", ticker="NVDA", side="long", status="wait",
                          record={"thesis": feed, "claim": "fine", "n": 3})
    ledger.update_swing_idea("idea:a", carry_cycle="c2")
    ledger.add_swing_idea("idea:b", origin_cycle="c9", ticker="AMD", side="short", status="scout",
                          record={"thesis": feed})
    ledger.add_swing_event("time_stop_due", idea_id="idea:a", origin_cycle="c2", reason=feed,
                           payload={"note": feed})
    ledger.add_paper_trade("p1", idea_id="idea:a", origin_cycle="c1", ticker="NVDA", side="long",
                           opened_at=T0, record={"why": [feed]})
    ledger.create_swing_trade("trade:t1", ticker="NVDA", side="long", idea_id="idea:a", origin_cycle="c1")
    hits = lambda text: feed in text   # noqa: E731
    assert scrub_swing_rows(ledger, "c2", hits, "[x]", dry_run=True) == 4
    assert ledger.swing_idea("idea:a")["record"]["thesis"] == feed
    assert scrub_swing_rows(ledger, "c2", hits, "[x]") == 4        # idea via carry, its event and paper
    assert ledger.swing_idea("idea:a")["record"] == {"thesis": "[x]", "claim": "fine", "n": 3}
    (event,) = [e for e in ledger.swing_events() if e["kind"] == "time_stop_due"]
    assert event["reason"] == "[x]" and event["payload"] == {"note": "[x]"}
    assert ledger.paper_trades()[0]["record"] == {"why": ["[x]"]}
    assert ledger.swing_idea("idea:b")["record"]["thesis"] == feed   # another cycle: untouched
    assert scrub_swing_rows(ledger, "c2", hits, "[x]") == 0


def test_the_licensed_purge_removes_feed_text_from_swing_rows_after_seven_days(tmp_path):
    from council import paths
    from council.ledger.db import LEDGER_FILE
    from council.operator.purge import REPLY_PLACEHOLDER, purge_licensed
    from tests.operator.conftest import CANARY_TITLE, capture_cycle

    root = paths.state_dir()
    root.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 10, 20, 10, 40, tzinfo=UTC)
    six = capture_cycle(root, slot=now - timedelta(days=6), canary=True)
    eight = capture_cycle(root, slot=now - timedelta(days=8), canary=True)
    ledger = Ledger(root / LEDGER_FILE)
    for cap, idea in ((six, "idea:six"), (eight, "idea:eight")):
        ledger.add_swing_idea(idea, origin_cycle=cap.cycle_id, ticker="NVDA", side="long", status="wait",
                              record={"thesis": CANARY_TITLE, "catalyst_ids": ["N:0000beef"]})
        ledger.add_swing_event("note", idea_id=idea, origin_cycle=cap.cycle_id, reason=CANARY_TITLE)
    purge_licensed(root, now=now, older_than_days=7, write_receipt=False)
    old, kept = ledger.swing_idea("idea:eight"), ledger.swing_idea("idea:six")
    assert old["record"] == {"thesis": REPLY_PLACEHOLDER, "catalyst_ids": ["N:0000beef"]}
    assert kept["record"]["thesis"] == CANARY_TITLE                # day six: inside the retention
    reasons = {e["idea_id"]: e["reason"] for e in ledger.swing_events()}
    assert reasons == {"idea:eight": REPLY_PLACEHOLDER, "idea:six": CANARY_TITLE}
    purge_licensed(root, now=now + timedelta(days=2), write_receipt=False)
    assert ledger.swing_idea("idea:six")["record"]["thesis"] == REPLY_PLACEHOLDER


# ------------------------------------------------------------------------------ benchmark, paper
def test_benchmark_days_upsert_and_paper_trades_close_once(ledger):
    ledger.record_benchmark_day("2026-10-05", sq8_ret=0.01, matched_idx_ret=0.002, idx_hold_ret=None)
    ledger.record_benchmark_day("2026-10-05", sq8_ret=0.02, matched_idx_ret=0.003, idx_hold_ret=0.001)
    assert [(d["day"], d["sq8_ret"]) for d in ledger.benchmark_days()] == [("2026-10-05", 0.02)]
    with pytest.raises(LedgerError):
        ledger.record_benchmark_day("2026-10-06", sq8_ret=float("nan"), matched_idx_ret=0, idx_hold_ret=0)
    ledger.add_paper_trade("p1", origin_cycle="c1", ticker="NVDA", side="long", opened_at=T0)
    ledger.close_paper_trade("p1", exit_reason="target", ret_pct=4.5, closed_at=T0 + timedelta(days=3))
    assert ledger.paper_trades(status="closed")[0]["ret_pct"] == 4.5
    with pytest.raises(LedgerError):
        ledger.close_paper_trade("p1", exit_reason="stop", ret_pct=-1.0, closed_at=T0)


# ------------------------------------------------------------------------------ migration
def _real_shaped_v4(path) -> dict[str, list[tuple]]:
    """A v4 ledger built with the current code (API-written rows), then the v5 objects dropped."""
    ledger = Ledger(path, clock=lambda: T0)
    ledger.record_cycle({"cycle_id": "c1", "slot": "us_open", "status": "sealed", "cards": []})
    _decision(ledger, "d1")
    ledger.insert_legs("d1", [open_leg(1, "GOLD"), open_leg(2, "SPX500")])
    ledger.update_leg("d1", 1, state="submitting", request_id="r1")
    ledger.update_leg("d1", 1, state="filled", position_ids=[7], resolved_at=T0)
    ledger.transition("d1", "completed_partial", "done", actor="executor")
    ledger.create_decision(decision_id="s1", kind="smoke", valid_until=T0 + timedelta(hours=1))
    ledger.add_equity_mark(T0, 2000.0)
    ledger.set_runtime("kill_state", {"state": "NORMAL"})
    conn = sqlite3.connect(path)
    for name in ("swing_trades_terminal_no_update", "swing_trades_terminal_no_delete"):
        conn.execute(f"DROP TRIGGER {name}")
    for table in SWING_TABLES:
        conn.execute(f"DROP TABLE {table}")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                         "AND name NOT LIKE 'sqlite_%'")]
    rows = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}
    conn.close()
    return rows


def test_migration_v4_to_v5_keeps_every_row_on_a_copy(tmp_path):
    original = tmp_path / "state" / "ledger.sqlite3"
    before = _real_shaped_v4(original)
    assert sum(len(v) for v in before.values()) >= 8
    copy = tmp_path / "state" / "copy" / "ledger.sqlite3"
    copy.parent.mkdir(parents=True)
    shutil.copy(original, copy)
    ledger = Ledger(copy, clock=lambda: T0)
    conn = sqlite3.connect(copy)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
    for table, rows in before.items():
        assert conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() == rows, table
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert set(SWING_TABLES) <= names and "swing_trades_terminal_no_update" in names
    assert sqlite3.connect(original).execute("PRAGMA user_version").fetchone()[0] == 4   # untouched
    assert ledger.get_decision("d1").state == "completed_partial" and ledger.migrate() == 5
    ledger.create_swing_trade("trade:t1", ticker="NVDA", side="long")
    assert ledger.swing_trades(states=["proposed"])[0].trade_id == "trade:t1"
