"""SW-4b: the S15 swing brake, the Skeptic canary pause, the operator lift, the canary event provider
and the URGENT after unapproved time-stop exits (swing-book.md rev 2, §3.1 S15, §1.5, §3.1 S9(c))."""

# ruff: noqa: F811 - the execution fixtures are imported by name and requested as parameters

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from council import cli
from council.cycle import note_unapproved_exit, run_swing, settle_swing, swing_exit_urgent
from council.publish import swing_brake_row as sbr
from council.publish.leakscan import scan_bytes
from council.swing import brake as B
from council.swing import canary as C
from council.swing import canary_set as CS
from council.swing import sources as ss
from tests.cli.operator_sim import simulate_operator
from tests.execution.conftest import fclock, ledger  # noqa: F401
from tests.swing.test_sw5b_cycle import decide, exit_leg, open_trade
from tests.swing.test_sw5c_sources import ctx_for

NOW = datetime(2026, 10, 7, 19, 0, tzinfo=UTC)
MONDAY = datetime(2026, 10, 5, 10, 40, tzinfo=UTC)
SWING_SLOT = datetime(2026, 9, 29, 14, 40, tzinfo=UTC)


def trade(tid="trade:a", *, side="long", state="closed_stop", size=0.08, open_rate=100.0, close_rate=None,
          closed_at=None, **detail):
    return SimpleNamespace(trade_id=tid, side=side, state=state, detail={"size_nav": size, **detail},
                           open_rate=open_rate, close_rate=close_rate, closed_at=closed_at)


def figure(trades, marks=None, **kw):
    kw.setdefault("declared_cost_pct_per_leg", 1.25)
    return B.s15_pnl_nav(trades, marks or {}, now=NOW, window_days=30, **kw)


# ------------------------------------------------------------------------------ S15 figure
def test_s15_figure_counts_realised_and_open_net_of_the_larger_cost():
    closed = trade(close_rate=90.0, closed_at=NOW - timedelta(days=3))            # -10% gross
    old = trade("trade:old", close_rate=50.0, closed_at=NOW - timedelta(days=31))  # outside the window
    short = trade("trade:s", side="short", state="open", open_rate=100.0)
    got = figure([closed, old, short], {"trade:s": 104.0})
    assert got == pytest.approx(0.08 * (-0.10 - 0.025) + 0.08 * (-0.04 - 0.025))
    # a private round trip above the declared cost (fixed fees at a small size) is the one charged
    got = figure([closed], costs={"trade:a": 4.0})
    assert got == pytest.approx(0.08 * (-0.10 - 0.04))
    # the watch's outcome (net of the declared cost) stands in for missing rates
    fallback = trade(close_rate=None, closed_at=NOW - timedelta(days=1), net_ret=-0.125)
    assert figure([fallback]) == pytest.approx(0.08 * (-0.10 - 0.025))


def test_s15_figure_is_unknown_without_a_mark_and_ignores_losses_before_a_lift():
    assert figure([trade(state="open")]) is None                                  # fail closed
    assert figure([trade(close_rate=None, closed_at=NOW)]) is None
    closed = trade(close_rate=30.0, closed_at=NOW - timedelta(days=5))
    assert figure([closed]) < -0.05
    assert figure([closed], since=NOW - timedelta(days=2)) == 0.0                 # reviewed at the lift
    assert figure([trade(state="proposed", size=0.08)]) == 0.0                    # never opened


# ------------------------------------------------------------------------------ S15 latch
def test_s15_engages_at_the_boundary_and_stays_on_until_lifted():
    st, ev, _ = B.step(B.empty_state(), pnl_nav=-0.0499, threshold=-0.05, verdicts=[], now=NOW)
    assert not B.is_on(st, "s15") and ev == []
    st, ev, flags = B.step(st, pnl_nav=-0.05, threshold=-0.05, verdicts=[], now=NOW)
    assert B.is_on(st, "s15") and [(e.brake, e.event, e.cause) for e in ev] == [("s15", "engaged", "s15_net_loss")]
    assert "swing_paused:s15" in flags
    st, ev, _ = B.step(st, pnl_nav=0.02, threshold=-0.05, verdicts=[], now=NOW + timedelta(days=1))
    assert B.is_on(st, "s15") and ev == []                                        # only the operator lifts
    st2, ev, flags = B.step(st, pnl_nav=None, threshold=-0.05, verdicts=[], now=NOW)
    assert B.is_on(st2, "s15") and st2["s15"]["unknown"] and "swing_brake_unknown" in flags


def test_s15_twice_in_60_days_is_flagged():
    st, _, _ = B.step(B.empty_state(), pnl_nav=-0.06, threshold=-0.05, verdicts=[], now=NOW)
    st, _ = B.lift(st, "s15", "reviewed the losing trades", NOW + timedelta(days=1))
    st, _, flags = B.step(st, pnl_nav=-0.07, threshold=-0.05, verdicts=[], now=NOW + timedelta(days=59))
    assert B.FLAG_TWICE in flags
    st, _ = B.lift(st, "s15", "reviewed again", NOW + timedelta(days=60))
    st, _, flags = B.step(st, pnl_nav=-0.07, threshold=-0.05, verdicts=[], now=NOW + timedelta(days=125))
    assert B.FLAG_TWICE not in flags


# ------------------------------------------------------------------------------ canary pause
def test_two_missed_canaries_in_a_row_pause_entries_and_a_catch_resets_the_streak():
    st = B.note_canary(B.note_canary(B.empty_state(), "missed"), "caught")
    st = B.note_canary(st, "missed")
    st, ev, _ = B.step(st, pnl_nav=0.0, threshold=-0.05, verdicts=[], now=NOW)
    assert not B.is_on(st, "canary") and ev == []
    st, ev, flags = B.step(B.note_canary(st, "missed"), pnl_nav=0.0, threshold=-0.05, verdicts=[], now=NOW)
    assert B.is_on(st, "canary") and ev[0].cause == "missed_canaries" and "swing_paused:canary" in flags


@pytest.mark.parametrize(("gap_days", "paused"), [(29, True), (31, False)])
def test_two_pass_rate_alarms_within_30_days_pause_entries(gap_days, paused):
    alarm, calm = ["pass"] * 20, ["pass"] * 10 + ["reject", "wait"] * 5
    st, _, flags = B.step(B.empty_state(), pnl_nav=0.0, threshold=-0.05, verdicts=alarm, now=NOW)
    assert "skeptic_pass_rate" in flags and len(st["canary"]["alarms"]) == 1
    st, _, flags = B.step(st, pnl_nav=0.0, threshold=-0.05, verdicts=alarm, now=NOW + timedelta(days=1))
    assert "skeptic_pass_rate" not in flags and len(st["canary"]["alarms"]) == 1   # one alarm per episode
    st, _, _ = B.step(st, pnl_nav=0.0, threshold=-0.05, verdicts=calm, now=NOW + timedelta(days=2))
    st, ev, _ = B.step(st, pnl_nav=0.0, threshold=-0.05, verdicts=alarm, now=NOW + timedelta(days=gap_days))
    assert B.is_on(st, "canary") is paused
    assert [e.cause for e in ev] == (["pass_rate_alarms"] if paused else [])


# ------------------------------------------------------------------------------ lift
@pytest.mark.parametrize("reason", ["", "   ", "lost $500 so paused", "see https://example.invalid/x",
                                    "account 123456789 reviewed", "x" * 181])
def test_a_lift_refuses_an_unpublishable_reason(reason):
    st, _, _ = B.step(B.empty_state(), pnl_nav=-0.06, threshold=-0.05, verdicts=[], now=NOW)
    with pytest.raises(B.BrakeError):
        B.lift(st, "s15", reason, NOW)


def test_a_lift_refuses_a_pause_that_is_off_and_publishes_a_code_free_row():
    with pytest.raises(B.BrakeError, match="not on"):
        B.lift(B.empty_state(), "canary", "prompt reviewed", NOW)
    st = B.note_canary(B.note_canary(B.empty_state(), "missed"), "missed")
    st, engaged, _ = B.step(st, pnl_nav=-0.2, threshold=-0.05, verdicts=["pass"] * 20, now=NOW)
    st, lifted = B.lift(st, "canary", "prompt revision recorded after review", NOW + timedelta(hours=2))
    assert not B.is_on(st, "canary") and B.is_on(st, "s15")                       # one pause at a time
    assert st["canary"]["missed_streak"] == 0 and st["canary"]["alarms"] == []
    st, ev, _ = B.step(st, pnl_nav=-0.2, threshold=-0.05, verdicts=["pass"] * 20, now=NOW + timedelta(hours=3))
    assert not B.is_on(st, "canary") and ev == []            # a still-active alarm does not re-pause at once
    rows = [sbr.PublicSwingBrakeRow.model_validate(e.row()) for e in [*engaged, lifted]]
    assert {r.id for r in rows} >= {"2026-10-07T1900Z-swing-brake-s15-engaged",
                                    "2026-10-07T2100Z-swing-brake-canary-lifted"}
    files = sbr.swing_brake_files(None, rows)
    raw = files[sbr.SWING_BRAKE_PATH]
    assert scan_bytes(sbr.SWING_BRAKE_PATH, raw, canaries=[2000.0]) == []
    lines = [json.loads(x) for x in raw.decode().splitlines()]
    assert all(set(x) <= {"id", "type", "brake", "event", "cause", "reason"} for x in lines)
    assert sbr.swing_brake_files(raw, rows)[sbr.SWING_BRAKE_PATH] == raw          # idempotent


def test_the_brake_command_is_operator_only_and_lifts_through_the_ledger(monkeypatch, tmp_path):
    from council.ledger.db import Ledger

    assert cli.OPERATOR_COMMANDS["swing brake"] is True
    res = CliRunner().invoke(cli.app, ["swing", "brake", "--lift", "--reason", "reviewed"])
    assert res.exit_code == 2 and "operator command" in res.output
    lg = Ledger(tmp_path / "ledger.sqlite3")
    lg.migrate()
    st, _, _ = B.step(B.empty_state(), pnl_nav=-0.06, threshold=-0.05, verdicts=[], now=NOW)
    B.save(lg, st, NOW)
    monkeypatch.setattr(cli, "_ledger_only", lambda: (tmp_path, lg))
    simulate_operator(monkeypatch)
    res = CliRunner().invoke(cli.app, ["swing", "brake"])
    assert res.exit_code == 0 and "S15 brake: ON" in res.output
    res = CliRunner().invoke(cli.app, ["swing", "brake", "--lift", "--canary", "--reason", "reviewed"])
    assert res.exit_code == 2 and "not on" in res.output
    res = CliRunner().invoke(cli.app, ["swing", "brake", "--lift", "--reason", "lost $900"])
    assert res.exit_code == 2 and B.is_on(B.load(lg), "s15")
    res = CliRunner().invoke(cli.app, ["swing", "brake", "--lift", "--reason", "losing trades reviewed"])
    assert res.exit_code == 0, res.output
    assert not B.is_on(B.load(lg), "s15")
    pending = lg.get_runtime(B.ROWS_PENDING_KEY)
    assert pending[-1]["event"] == "lifted" and pending[-1]["reason"] == "losing trades reviewed"


def test_the_deny_rules_cover_the_brake_command_identically():
    from council.paths import REPO_ROOT

    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())["permissions"]["deny"]
    rules = json.loads((REPO_ROOT / "ops" / "claude" / "deny-rules.json").read_text())
    rules = rules["deny"] if isinstance(rules, dict) else rules
    for line in ("council swing brake --lift", "uv run council swing brake --lift --canary"):
        for deny in (settings, rules):
            assert any(r.startswith("Bash(") and line.startswith(r[5:].removesuffix(":*)") + " ") for r in deny), line


# ------------------------------------------------------------------------------ in the cycle
def _run_at(ctx, slot):
    return asyncio.run(run_swing(ctx, SimpleNamespace(cycle_id=slot.strftime("%Y-%m-%dT%H%MZ"), extras={}),
                                 snapshot=None, kill_state="NORMAL", nav=None, slot=slot, now=slot))


@pytest.mark.parametrize(("brake", "code"), [("s15", "S15:brake_on"), ("canary", "S15:brake_engaged")])
def test_a_pause_drops_new_swing_entries_in_the_cycle(policy, tmp_path, brake, code):
    src = ss.fixture_swing_sources(skeptic_gateway=ss.fixture_skeptic_gateway(policy))
    ctx = ctx_for(policy, tmp_path, src, account=True)
    free = _run_at(ctx, SWING_SLOT)
    assert free.entries and not [f for f in free.flags if "S15" in f], free.flags
    st = B.empty_state()
    st[brake]["on"] = True
    B.save(ctx.ledger, st, SWING_SLOT)
    ctx2 = ctx_for(policy, tmp_path / "b", ss.fixture_swing_sources(skeptic_gateway=ss.fixture_skeptic_gateway(policy)),
                   account=True)
    B.save(ctx2.ledger, st, SWING_SLOT)
    out = _run_at(ctx2, SWING_SLOT)
    assert not out.entries and f"swing_drop:{code}" in out.flags and f"swing_paused:{brake}" in out.flags


def _canary_reply(event, verdict, priced_in):
    line = event.card.line_id
    return {"idea_ref": "idea:1", "catalyst_supports_claim": True, "claim_supports_side": True, "verdict": verdict,
            "priced_in": priced_in, "news_status": "follow_up", "regime": "neutral", "crowding": "unknown",
            "reasons": [{"text": "The move since the filing is large.",
                         "evidence_ids": [f"X:{line}:move_since_news_close_sigma"]},
                        {"text": "The filing is several sessions old.", "evidence_ids": [event.catalyst_ids[0]]}],
            "what_would_change_my_mind": "A new filing.", "second_order": None}


def test_the_monday_canary_grades_the_skeptic_and_two_misses_pause_entries(policy, tmp_path):
    from council.cycle import SKEPTIC_HEALTH_KEY
    from council.llm.stub import StubGateway

    model = str(policy.swing.llm.skeptic_model)
    src = ss.fixture_swing_sources(skeptic_gateway=None)
    src.canary_event = CS.provider(tmp_path / "state", src.flags)
    ctx = ctx_for(policy, tmp_path, src)
    ev1 = src.canary_event(MONDAY)
    src.skeptic_gateway = StubGateway(responses={"skeptic": _canary_reply(ev1, "reject", "fully")}, model=model)
    out = _run_at(ctx, MONDAY)
    assert "swing_canary_no_event" not in out.flags and len(out.private_calls) == 1 and not out.calls
    assert ctx.ledger.get_runtime(SKEPTIC_HEALTH_KEY)["canary_grades"] == ["caught"]
    for week in (1, 2):
        slot = MONDAY + timedelta(days=7 * week)
        ev = src.canary_event(slot)
        src.skeptic_gateway = StubGateway(responses={"skeptic": _canary_reply(ev, "pass", "partly")}, model=model)
        out = _run_at(ctx, slot)
    assert ctx.ledger.get_runtime(SKEPTIC_HEALTH_KEY)["canary_grades"] == ["caught", "missed", "missed"]
    assert B.is_on(B.load(ctx.ledger), "canary") and "swing_paused:canary" in out.flags
    assert ctx.ledger.get_runtime(B.ROWS_PENDING_KEY)[-1]["cause"] == "missed_canaries"


# ------------------------------------------------------------------------------ canary provider
def test_every_builtin_event_is_a_valid_canary_on_every_monday_of_a_year():
    slot = MONDAY
    seen = set()
    for _ in range(53):
        ev = CS.pick(list(CS.BUILTIN_EVENTS), slot)
        assert ev is not None and C.qualifies(ev) is None
        idea = C.build_canary(ev)
        assert idea.canary and ev.catalysts[0].available_at < slot
        seen.add((ev.ticker, ev.side, ev.catalyst_ids[0]))
        slot += timedelta(days=7)
    assert len(seen) == len(CS.BUILTIN_EVENTS)                                    # the rotation visits all
    for rec in CS.BUILTIN_EVENTS:
        ev = CS.build_event(rec, MONDAY)
        assert ev is not None and C.qualifies(ev) is None, rec["ticker"]
        assert ev.catalyst_ids[0].startswith("P:") and ev.card.fields["news_age_sessions"] >= C.MIN_AGE_SESSIONS


def test_a_private_set_is_preferred_and_an_invalid_one_falls_back_with_a_flag(tmp_path):
    rec = dict(CS.BUILTIN_EVENTS[0], ticker="AAPL", sector_etf="XLK")
    (tmp_path / "swing").mkdir()
    (tmp_path / "swing" / "canary_events.json").write_text(json.dumps([rec]))
    flags: list[str] = []
    ev = CS.provider(tmp_path, flags)(MONDAY)
    assert ev.ticker == "AAPL" and flags == []
    (tmp_path / "swing" / "canary_events.json").write_text(json.dumps([{"ticker": "AAPL"}]))
    ev = CS.provider(tmp_path, flags)(MONDAY)
    assert ev is not None and ev.ticker != "AAPL" and flags == [CS.FLAG_INVALID]
    too_small = dict(rec, move_sigma=2.9)
    assert CS.pick([too_small], MONDAY) is None                                   # -> swing_canary_no_event


def test_real_sources_carry_the_canary_provider(policy, tmp_path):
    src = ss.real_swing_sources(policy, state_dir=tmp_path, keys_loader=lambda: None,
                                sec_user_agent=lambda: "test agent test@example.invalid")
    assert src.canary_event is not None and src.canary_event(MONDAY) is not None


# ------------------------------------------------------------------------------ URGENT exits
class Notes:
    def __init__(self):
        self.sent = []

    def send(self, title, body, priority="default"):
        self.sent.append((title, body, priority))


def test_three_unapproved_time_stop_exits_send_one_urgent_per_trade(ledger, fclock, policy):
    open_trade(ledger)
    notes = Notes()
    ctx = SimpleNamespace(ledger=ledger, policy=policy, notifier=notes)
    assert policy.swing.earnings.urgent_after_unapproved_slots == 3
    for n in (1, 2):
        did = decide(ledger, fclock, exit_leg())
        ledger.update_swing_trade("trade:t0", detail={"exit_decision": did, "exit_kind": "time",
                                                      "pre_exit_state": "open"})
        ledger.transition_swing_trade("trade:t0", "exit_pending", cycle_id="c")
        ledger.transition(did, "rejected", "operator", actor="operator")
        settle_swing(ledger, fclock.now())
        assert ledger.swing_trade("trade:t0").state == "open"
        assert swing_exit_urgent(ctx, fclock.now()) == [] and notes.sent == [], n
    assert note_unapproved_exit(ledger, "trade:t0", fclock.now()) == 3
    assert swing_exit_urgent(ctx, fclock.now()) == ["swing_exit_unapproved"]
    assert len(notes.sent) == 1 and notes.sent[0][2] == "urgent" and "NVDA" in notes.sent[0][1]
    assert swing_exit_urgent(ctx, fclock.now()) == [] and len(notes.sent) == 1   # deduplicated per trade


def test_a_non_time_exit_is_not_counted(ledger, fclock):
    open_trade(ledger)
    did = decide(ledger, fclock, exit_leg())
    ledger.update_swing_trade("trade:t0", detail={"exit_decision": did, "exit_kind": "exit", "pre_exit_state": "open"})
    ledger.transition_swing_trade("trade:t0", "exit_pending", cycle_id="c")
    ledger.transition(did, "rejected", "operator", actor="operator")
    settle_swing(ledger, fclock.now())
    assert not ledger.get_runtime("swing_exit_misses")
