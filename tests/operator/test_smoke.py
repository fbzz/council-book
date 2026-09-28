"""M5-D2 smoke tickets (m5-readiness §8): S1–S6 end to end on FakeEtoro with the operator simulated
through the REAL guard function (an operator environment, both TTYs, no agent ancestor), every
approval re-check, the size and cap rules, the refusal cases, verify's automatic checks and their
red paths, the flatten supersede (V17), the kind=smoke isolation from the council's book, and the
weightless public row."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from council.broker.etoro_read import EtoroReadClient
from council.broker.fake import FakeClock, FakeEtoro, eligibility_row, leverage_config
from council.broker.instruments import InstrumentMap
from council.context import hold_reference_stub
from council.ledger.db import Ledger, PriorityConflict
from council.llm.prompts import PromptRegistry
from council.llm.stub import StubGateway
from council.operator import capabilities, guards
from council.operator import smoke as sm
from council.operator.approve import ApprovalDeps, ApprovalRefused, approve
from council.policy import Policy
from council.publish import smoke_row
from council.publish.gitops import Publisher
from council.publish.leakscan import scan_bytes
from council.publish.public_models import CYCLE_ID_PATTERN
from council.runtime import CycleContext, Sources
from council.settings import Settings
from council.watch import run_watch

pytestmark = pytest.mark.capability_gates                 # the real, fail-closed capability load

NOW = datetime(2026, 10, 1, 9, 5, tzinfo=UTC)          # Thursday: LSE, FX and crypto open
WEEKEND = datetime(2026, 10, 3, 11, 0, tzinfo=UTC)     # Saturday: LSE closed
INSTRUMENTS = {   # symbol: (instrument id, mid price, settlement)
    "CNDX.L": (301, 900.0, "real"), "SGLN.L": (302, 40.0, "real"), "BTC": (305, 60000.0, "real"),
    "EURUSD": (308, 1.1, "cfd"), "AAPL": (320, 200.0, "real"),
}


def operator_guard() -> None:
    """The real guard function, fed an operator's terminal."""
    guards.assert_operator_context(env={"COUNCIL_ROLE": "operator"}, stdin_isatty=True,
                                   stdout_isatty=True, ancestors=["zsh", "login", "Terminal"])


def agent_guard() -> None:
    """The real guard function, fed a coding agent's shell."""
    guards.assert_operator_context(env={"COUNCIL_ROLE": "operator", "CLAUDECODE": "1"}, stdin_isatty=False,
                                   stdout_isatty=False, ancestors=["zsh", "claude"])


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _remote_clone(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    clone = tmp_path / "state" / "publisher-clone"
    clone.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", str(bare), str(clone), cwd=tmp_path)
    _git("config", "user.name", "council-publisher", cwd=clone)
    _git("config", "user.email", "18754232+fbzz@users.noreply.github.com", cwd=clone)
    (clone / "journal").mkdir()
    (clone / "journal" / ".keep").write_text("")
    _git("add", "journal/.keep", cwd=clone)
    _git("commit", "-q", "-m", "init", cwd=clone)
    _git("push", "-q", "-u", "origin", "main", cwd=clone)
    return clone


class World:
    def __init__(self, tmp_path: Path, *, credit: float = 10_000.0, start: datetime = NOW) -> None:
        self.clock = FakeClock(start=start)
        self.app, self.read_tok, self.write_tok = (f"fake-{n}-{uuid.uuid4().hex[:6]}" for n in ("a", "r", "w"))
        self.fake = FakeEtoro(clock=self.clock.now, credit=credit, api_key=self.app,
                              user_keys=(self.read_tok, self.write_tok), write_user_keys={self.write_tok})
        for sym, (iid, px, settlement) in INSTRUMENTS.items():
            if settlement == "real":
                configs = [leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,))]
            else:
                configs = [leverage_config(direction="LONG"), leverage_config(direction="SHORT")]
            self.fake.add_instrument(sym, iid, bid=px * 0.9995, ask=px * 1.0005,
                                     row=eligibility_row(sym, iid, configs=configs, currency="USD"))
        self.state = tmp_path / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        InstrumentMap({}, path=self.state / "instruments.json").merged(
            {s: v[0] for s, v in INSTRUMENTS.items() if s != "AAPL"}, start).save()
        self.ledger = Ledger(self.state / "ledger.sqlite3", clock=self.clock.now)
        self.policy = Policy.load(include_sleeve=False)
        self.read = EtoroReadClient(self.app, self.read_tok, transport=self.fake.transport(), sleep=self.clock.sleep)
        self.clone = _remote_clone(tmp_path)
        self.ctx = CycleContext(
            policy=self.policy, settings=Settings(role="dev", mode="stub"), ledger=self.ledger,
            gateway=StubGateway(hold_reference_stub()), registry=PromptRegistry(),
            sources=Sources(history=lambda slot: ({}, []), events=lambda s, e: ([], []), broker=self.read),
            publisher=Publisher(self.clone, push=True), clock=self.clock.now, state_dir=self.state)
        self.out: list[str] = []
        self.jobs: set[str] = set()

    def deps(self, *, guard=operator_guard) -> sm.SmokeDeps:
        return sm.SmokeDeps(ledger=self.ledger, policy=self.policy, read=self.read, state_dir=self.state,
                            print_fn=self.out.append, now_fn=self.clock.now, guard_fn=guard,
                            jobs_loaded=lambda: set(self.jobs), release_fn=lambda: None)

    def approval(self, *, guard=operator_guard) -> ApprovalDeps:
        import importlib

        etoro_write = importlib.import_module("council.broker.etoro_write")

        def answer(prompt: str) -> str:
            return re.search(r"Type (\S+) to approve", prompt).group(1)

        return ApprovalDeps(
            ledger=self.ledger, policy=self.policy, read=self.read,
            write_factory=lambda: etoro_write.EtoroWriteClient(self.app, self.write_tok, transport=self.fake.transport()),
            state_dir=self.state, input_fn=answer, print_fn=self.out.append, now_fn=self.clock.now,
            guard_fn=guard, executor_kwargs={"clock": self.clock.now, "sleep": self.clock.sleep,
                                             "_skip_guard_for_tests": True})

    def rows(self) -> list[dict]:
        path = self.clone / smoke_row.SMOKE_PATH
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_step(self, step: str) -> str:
        ticket = sm.propose(step, self.deps())
        decision_id = ticket.decision_id
        assert self.ledger.get_decision(decision_id).state == "awaiting_publication"
        with pytest.raises(ApprovalRefused):                  # not approvable before its row is public
            approve(decision_id, self.approval())
        run_watch(self.ctx)
        d = self.ledger.get_decision(decision_id)
        assert d.state == "proposed" and d.published_commit, self.out
        report = approve(decision_id, self.approval())
        assert report.final_state == "completed", (report.reasons, self.out)
        self.clock.advance(5)
        run_watch(self.ctx)
        checks = sm.verify(decision_id, self.deps())
        assert all(c.ok for c in checks), [c for c in checks if not c.ok]
        self.clock.advance(60)
        return decision_id


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    return World(tmp_path)


def _nav(world: World) -> float:
    return world.fake.equity()


# ================================================================================ end to end
def test_s1_to_s6_pass_every_approval_recheck_on_fake_etoro(world):
    ids = {}
    for step in ("S1", "S2", "S3", "S4", "S5", "S5x", "S6", "S6x"):
        ids[step] = world.run_step(step)
        if step in ("S1", "S2", "S3", "S5", "S6"):
            assert world.ledger.smoke_active(), step          # a smoke position is open (K20 red)
        if step in ("S4", "S5x", "S6x"):
            assert world.ledger.smoke_active() == [], step
    assert not world.fake.positions, "every smoke position was closed again"
    data = capabilities.read_file(world.state)["capabilities"]
    assert {c: data[c]["step"] for c in data} == {
        "real_etf": "S1", "sl_modify": "S2", "partial_close": "S3", "crypto_real": "S5", "cfd_short": "S6"}
    for step in ("S1", "S6"):
        assert capabilities.smoke_proof(world.state, ids[step], step)
    # every public row is weightless and completed; ids never look like cycle ids
    rows = world.rows()
    assert {r["id"] for r in rows} == set(ids.values())
    assert all(set(r) == {"id", "type", "step", "state", "commitment"} for r in rows)
    assert all(r["state"] == "completed" and r["type"] == "smoke_test" for r in rows)
    assert not any(re.match(CYCLE_ID_PATTERN, i) for i in ids.values())
    raw = (world.clone / smoke_row.SMOKE_PATH).read_bytes()
    assert scan_bytes(smoke_row.SMOKE_PATH, raw, canaries=[10_000.0, _nav(world)]) == []
    assert not (world.clone / "journal" / "status.json").exists()
    assert not (world.clone / "journal" / "book").exists()
    # M-3: no smoke fill feeds the council's book queries
    since = NOW - timedelta(days=30)
    assert world.ledger.last_changes() == {}
    assert world.ledger.turnover_since(since) == 0.0
    assert world.ledger.cost_bps_since(since) == 0.0 and world.ledger.real_fee_drag() == 0.0
    assert world.ledger.level_resets() == {}
    # the commitment is sha256(salt ‖ canonical private plan), kept privately for audit
    salt_file = json.loads((world.state / "salts" / "smoke" / f"{ids['S1']}.json").read_text())
    expect = sm.commitment_of(salt_file["salt"], bytes.fromhex(salt_file["canonical_hex"]))
    assert world.ledger.get_decision(ids["S1"]).commitment_sha == expect
    assert not list((world.state / "salts").glob("*.json")), "never in the cycle reveal loop"


def test_size_is_max_of_broker_minimum_and_floor_and_s1_is_doubled_for_the_split(world):
    econ_floor = 1.2 * 30.0                     # 1.2 x the assumed real copy floor in virtual dollars
    t1 = sm.build_ticket("S1", world.deps())[0]
    t5 = sm.build_ticket("S5", world.deps())[0]
    exposure = lambda t: t.plan.legs[0].amount_usd                      # noqa: E731
    assert exposure(t1) == pytest.approx(2 * econ_floor, rel=1e-3)
    assert exposure(t5) == pytest.approx(econ_floor, rel=1e-3)
    leg = t1.plan.legs[0]
    assert leg.kind == "open" and leg.sl_rate and leg.stop_distance and leg.risk_increasing
    assert leg.symbol in ("SGLN.L", "CNDX.L") and leg.units * (leg.amount_usd / leg.units) >= 2 * econ_floor - 1e-6
    assert world.ledger.decisions() == [], "build_ticket writes nothing"


def test_a_minimum_above_one_percent_of_nav_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    small = World(tmp_path, credit=2_000.0)
    with pytest.raises(sm.SmokeRefused, match="smoke_min_above_cap"):
        sm.propose("S1", small.deps())
    assert small.ledger.decisions() == []
    assert not (small.state / "salts").exists()
    assert "real_etf" not in capabilities.read_file(small.state)["capabilities"]


def test_preview_prints_the_exact_request_and_writes_nothing(world):
    before = (world.state / "instruments.json").read_bytes()
    ticket = sm.propose("S6", world.deps(), preview=True)
    assert ticket.decision_id is None
    body = next(line for line in world.out if "stopLossRate" in line)
    sent = json.loads(body.strip())
    assert sent["transaction"] == "sellShort" and sent["settlementType"] == "cfd" and sent["units"] > 0
    assert any("(USD, USD)" in line for line in world.out)
    assert any("stop-loss distance" in line for line in world.out)
    assert world.ledger.decisions() == [] and not (world.state / "salts").exists()
    assert (world.state / "instruments.json").read_bytes() == before
    assert not any(k in "\n".join(world.out) for k in (world.read_tok, world.write_tok, world.app))


# ================================================================================ refusals
def _pending_rebalance(world: World, state: str = "proposed") -> str:
    world.ledger.create_decision(decision_id="2026-10-01T0640Z-rebalance-aaaaaa", kind="rebalance",
                                 valid_until=NOW + timedelta(hours=1), state=state)
    return "2026-10-01T0640Z-rebalance-aaaaaa"


@pytest.mark.parametrize("case, code", [
    ("pending", "decision_pending"), ("blocker", "blocker_present"), ("warn", "kill_switch_warn"),
    ("halted", "kill_switch_halted"), ("jobs", "launchd_jobs_loaded"), ("prereq", "prerequisite_missing"),
    ("unknown", "unknown_step"),
])
def test_propose_refusals(world, case, code):
    step = "S1"
    if case == "pending":
        _pending_rebalance(world)
    elif case == "blocker":
        did = _pending_rebalance(world)
        world.ledger.transition(did, "blocked", "test")
    elif case in ("warn", "halted"):
        world.ledger.set_runtime("kill_state", case.upper() if case == "warn" else "HALTED")
    elif case == "jobs":
        world.jobs = {"com.fbzz.council.watch"}
    elif case == "prereq":
        step = "S2"
    else:
        step = "S9"
    with pytest.raises(sm.SmokeRefused, match=code):
        sm.propose(step, world.deps())
    assert [d for d in world.ledger.decisions() if d.kind == "smoke"] == []


def test_an_agent_context_is_refused_by_the_real_guards(world):
    with pytest.raises(guards.GuardError, match="CLAUDECODE is set"):
        sm.propose("S1", world.deps(guard=agent_guard))
    with pytest.raises(guards.GuardError):
        sm.status(world.deps(guard=agent_guard))
    assert world.ledger.decisions() == []


def test_a_closed_market_and_an_open_step_position_are_refused(tmp_path, monkeypatch, world):
    with pytest.raises(sm.SmokeRefused, match="market_closed"):
        sm.propose("S1", sm.SmokeDeps(**{**world.deps().__dict__, "now_fn": lambda: WEEKEND}))
    world.run_step("S5")
    with pytest.raises(sm.SmokeRefused, match="step_position_open"):
        sm.propose("S5", world.deps())


def test_the_agents_can_never_approve_a_smoke_ticket(world):
    ticket = sm.propose("S5", world.deps())
    run_watch(world.ctx)
    with pytest.raises(guards.GuardError):
        approve(ticket.decision_id, world.approval(guard=agent_guard))
    assert world.ledger.get_decision(ticket.decision_id).state == "proposed"
    assert not world.fake.positions


# ================================================================================ supersede
def test_a_flatten_supersedes_a_pending_smoke_ticket_v17(world):
    ticket = sm.propose("S5", world.deps())
    run_watch(world.ctx)
    world.ledger.create_decision(decision_id="flatten-watch-1", kind="flatten",
                                 valid_until=NOW + timedelta(hours=1))
    assert world.ledger.get_decision(ticket.decision_id).state == "superseded"
    with pytest.raises(ApprovalRefused, match="superseded"):
        approve(ticket.decision_id, world.approval())
    run_watch(world.ctx)
    assert {r["id"]: r["state"] for r in world.rows()}[ticket.decision_id] == "rejected"


def test_a_smoke_ticket_supersedes_nothing(world):
    _pending_rebalance(world)
    with pytest.raises(PriorityConflict):
        world.ledger.create_decision(decision_id="2026-10-01T0905Z-smoke-S1", kind="smoke",
                                     valid_until=NOW + timedelta(hours=1))


def test_approval_needs_a_normal_kill_switch_for_a_smoke_ticket(world):
    ticket = sm.propose("S5", world.deps())
    run_watch(world.ctx)
    world.ledger.set_runtime("kill_state", "WARN")
    with pytest.raises(ApprovalRefused, match="NORMAL"):
        approve(ticket.decision_id, world.approval())


# ================================================================================ verify
def _only_leg(world: World, decision_id: str):
    (leg,) = world.ledger.legs(decision_id)
    return leg


@pytest.mark.parametrize("tamper, bad", [
    ("sl", "leg1_sl_at_request"), ("units", "leg1_units_reconciled"), ("slow", "leg1_terminal_60s"),
    ("fill", "leg1_units_filled"),
])
def test_verify_red_paths_never_write_the_capability(world, tamper, bad):
    ticket = sm.propose("S5", world.deps())
    run_watch(world.ctx)
    approve(ticket.decision_id, world.approval())
    leg = _only_leg(world, ticket.decision_id)
    pid = leg.position_ids[0]
    con = sqlite3.connect(world.state / "ledger.sqlite3")
    if tamper == "sl":
        world.fake.positions[pid].sl_rate = world.fake.positions[pid].sl_rate * 0.9
    elif tamper == "units":
        world.fake.positions[pid].units *= 0.5
    elif tamper == "slow":
        con.execute("UPDATE legs SET resolved_at = ? WHERE leg_id = ?",
                    ((leg.submitted_at + timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), leg.leg_id))
    else:
        detail = dict(leg.detail, units_filled=float(leg.detail["units_sent"]) * 0.5)
        con.execute("UPDATE legs SET detail_json = ? WHERE leg_id = ?", (json.dumps(detail), leg.leg_id))
    con.commit()
    con.close()
    checks = sm.verify(ticket.decision_id, world.deps())
    assert bad in [c.code for c in checks if not c.ok]
    assert "crypto_real" not in capabilities.read_file(world.state)["capabilities"]
    assert any("mirror-copied" in line for line in world.out), "the manual checklist is printed anyway"


def test_verify_refuses_an_unfinished_or_foreign_decision(world):
    ticket = sm.propose("S5", world.deps())
    checks = sm.verify(ticket.decision_id, world.deps())
    assert "decision_completed" in [c.code for c in checks if not c.ok]
    did = _pending_rebalance(world, state="awaiting_publication")
    with pytest.raises(sm.SmokeRefused, match="not_a_smoke_decision"):
        sm.verify(did, world.deps())


# ================================================================================ isolation
def test_status_reports_k20_red_while_open_and_green_after(world):
    world.run_step("S5")
    st = sm.status(world.deps())
    assert st.gates["K20"]["state"] == "red" and st.active
    world.run_step("S5x")
    st = sm.status(world.deps())
    assert st.gates["K20"]["state"] == "green" and st.gates["K14"]["state"] == "red"


def test_a_stopped_smoke_position_is_not_a_council_stop_hit(world):
    world.run_step("S5")
    (pid,) = world.ledger.smoke_positions()
    run_watch(world.ctx)                        # the watch observes the position
    world.fake.hit_stop(pid)
    run_watch(world.ctx)
    assert world.ledger.stop_hits_since(NOW - timedelta(days=1)) == {}
    assert world.ledger.smoke_active([p for p in world.fake.positions]) == []


def test_nav_baseline_is_frozen_while_smoke_is_active_and_restarts_once_after(world):
    run_watch(world.ctx)
    first = world.ledger.get_runtime("nav_state")
    assert first is not None
    world.run_step("S5")
    assert world.ledger.get_runtime("nav_state") == first, "no NAV write while a smoke position is open"
    world.run_step("S5x")
    run_watch(world.ctx)
    assert world.ledger.get_runtime("smoke_baseline_reset")
    assert world.ledger.get_runtime("nav_state") != first
    assert world.ledger.smoke_baseline_due() is False


def test_the_public_row_is_nav_invariant_apart_from_the_random_commitment(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    rows = []
    for credit in (10_000.0, 25_000.0):
        w = World(tmp_path / f"nav{int(credit)}", credit=credit)
        w.run_step("S5")
        (row,) = w.rows()
        rows.append({k: v for k, v in row.items() if k != "commitment"})
        raw = (w.clone / smoke_row.SMOKE_PATH).read_bytes()
        assert scan_bytes(smoke_row.SMOKE_PATH, raw, canaries=[credit, w.fake.equity()]) == []
    assert rows[0] == rows[1]


# ================================================================================ ledger
def test_a_v3_ledger_admits_the_smoke_kind_and_keeps_its_rows(tmp_path):
    path = tmp_path / "state" / "ledger.sqlite3"
    path.parent.mkdir(parents=True)
    ledger = Ledger(path)
    ledger.create_decision(decision_id="d-old", kind="rebalance", valid_until=NOW + timedelta(hours=1))
    ledger.transition("d-old", "rejected", "test")
    con = sqlite3.connect(path)
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='decisions'").fetchone()[0]
    con.execute("PRAGMA writable_schema=ON")
    con.execute("UPDATE sqlite_master SET sql=? WHERE name='decisions'",
                (sql.replace(", 'smoke'", ""),))
    con.execute("PRAGMA writable_schema=OFF")
    con.execute("PRAGMA user_version=3")
    con.commit()
    con.close()
    again = Ledger(path)
    assert again.get_decision("d-old").kind == "rebalance"
    again.create_decision(decision_id="2026-10-01T0905Z-smoke-S1", kind="smoke",
                          valid_until=NOW + timedelta(hours=1))
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 4


def test_smoke_ids_never_match_the_cycle_pattern():
    for step in sm.STEPS:
        sid = sm.smoke_id(NOW, step)
        assert not re.match(CYCLE_ID_PATTERN, sid) and smoke_row.is_smoke_id(sid)
    assert set(capabilities.SMOKE_STEPS["cfd_short"]) <= set(sm.STEPS)
    assert all(s.proves in capabilities.SMOKE_STEPS for s in sm.STEPS.values() if s.proves)
    assert all(s.code in capabilities.SMOKE_STEPS[s.proves] for s in sm.STEPS.values() if s.proves)


# ================================================================================ CLI
def test_smoke_commands_are_pinned_operator_commands_and_refuse_outside_the_terminal(monkeypatch):
    from typer.testing import CliRunner

    from council import cli

    assert {k: cli.OPERATOR_COMMANDS[k] for k in ("smoke propose", "smoke verify", "smoke status")} == {
        "smoke propose": True, "smoke verify": True, "smoke status": True}
    monkeypatch.setenv("COUNCIL_ROLE", "operator")
    monkeypatch.setenv("CLAUDECODE", "1")
    for args in (["smoke", "propose", "S1"], ["smoke", "verify", "x"], ["smoke", "status"]):
        result = CliRunner().invoke(cli.app, args)
        assert result.exit_code == 2 and "refused" in result.output



# ================================================================================ live cycle (K20)
from ..integration import test_end_to_end as e2e  # noqa: E402
from ..integration.test_end_to_end import fake_broker, remote_clone  # noqa: E402,F401


def test_a_live_cycle_seals_no_proposal_and_publishes_only_the_ops_row_while_a_ticket_is_pending(
        tmp_path, remote_clone, fake_broker):  # noqa: F811
    from council.cycle import SMOKE_OPEN_FLAG, run_cycle

    fake, fclock = fake_broker
    read = EtoroReadClient(e2e.API_KEY, e2e.READ_KEY, transport=fake.transport(), sleep=fclock.sleep)
    ctx = e2e._ctx(tmp_path, broker=read, publisher=Publisher(remote_clone, push=True))
    ticket = "2026-10-01T1440Z-smoke-S5"
    ctx.ledger.create_decision(decision_id=ticket, kind="smoke", valid_until=e2e.NOW + timedelta(hours=1))
    out = run_cycle(ctx)
    assert SMOKE_OPEN_FLAG in out.flags and out.decision_id is None
    assert ctx.ledger.get_decision(ticket).state == "proposed", "a live cycle never supersedes it"
    journal = remote_clone / "journal"
    assert (journal / "ops" / "cycles.jsonl").exists()
    assert not (journal / "status.json").exists() and not (journal / "book").exists()
    assert not list(journal.rglob("commitments/**/*.json")) and not list(journal.rglob("cycles/**/*.json"))


# ================================================================================ review fixes
def test_a_smoke_close_runs_under_warn_so_an_open_smoke_position_never_deadlocks(world):
    world.run_step("S5")
    world.ledger.set_runtime("kill_state", "WARN")
    with pytest.raises(sm.SmokeRefused, match="kill_switch_warn"):
        sm.propose("S6", world.deps())                # opens still need NORMAL
    ticket = sm.propose("S5x", world.deps())
    run_watch(world.ctx)
    world.ledger.set_runtime("kill_state", "WARN")    # the watch re-evaluated it; keep WARN
    report = approve(ticket.decision_id, world.approval())
    assert report.final_state == "completed", report.reasons
    assert not world.fake.positions
    world.ledger.set_runtime("kill_state", "HALTED")
    world.clock.advance(60)
    with pytest.raises(sm.SmokeRefused, match="kill_switch_halted"):
        sm.propose("S5x", world.deps())               # HALTED leaves it to the flatten


def test_a_smoke_close_needs_no_stop_loss(world):
    world.run_step("S5")
    (pid,) = world.ledger.smoke_positions()
    world.fake.positions[pid].sl_rate = 0.0
    ticket = sm.propose("S5x", world.deps())
    assert ticket.decision_id


def test_a_smoke_position_closed_elsewhere_frees_its_step(world):
    world.run_step("S5")
    (pid,) = world.ledger.smoke_positions()
    world.fake.hit_stop(pid)
    world.clock.advance(5)
    ticket = sm.propose("S5", world.deps())           # the broker no longer holds it
    assert ticket.decision_id


def test_verify_is_red_when_the_open_fill_reported_no_position(world):
    ticket = sm.propose("S5", world.deps())
    run_watch(world.ctx)
    approve(ticket.decision_id, world.approval())
    leg = _only_leg(world, ticket.decision_id)
    con = sqlite3.connect(world.state / "ledger.sqlite3")
    con.execute("UPDATE legs SET position_ids_json = '[]' WHERE leg_id = ?", (leg.leg_id,))
    con.commit()
    con.close()
    checks = sm.verify(ticket.decision_id, world.deps())
    assert "leg1_position_id" in [c.code for c in checks if not c.ok]
    assert "crypto_real" not in capabilities.read_file(world.state)["capabilities"]


def test_a_halted_cycle_with_a_smoke_ticket_publishes_only_the_ops_row_and_no_cycle_link(
        tmp_path, remote_clone, fake_broker):  # noqa: F811
    """M-2 (review): a HALTED cycle's document and its flatten's execution report would carry the
    smoke weight (and so the NAV): only the ops row is published, the flatten has no cycle link."""
    from council.cycle import run_cycle

    fake, fclock = fake_broker
    read = EtoroReadClient(e2e.API_KEY, e2e.READ_KEY, transport=fake.transport(), sleep=fclock.sleep)
    ctx = e2e._ctx(tmp_path, broker=read, publisher=Publisher(remote_clone, push=True))
    ticket = "2026-10-01T1440Z-smoke-S5"
    ctx.ledger.create_decision(decision_id=ticket, kind="smoke", valid_until=e2e.NOW + timedelta(hours=1))
    ctx.ledger.set_runtime("kill_state", "HALTED")
    out = run_cycle(ctx)
    journal = remote_clone / "journal"
    assert (journal / "ops" / "cycles.jsonl").exists()
    assert not (journal / "status.json").exists() and not (journal / "book").exists()
    assert not list(journal.rglob("commitments/**/*.json")) and not list(journal.rglob("cycles/**/*.json"))
    if out.decision_id is not None:
        assert ctx.ledger.get_decision(out.decision_id).cycle_id is None


def test_a_smoke_execution_is_never_published_by_the_watch(world):
    """M5-D2/M5-N: the smoke legs and fills stay private; only the weightless ops row is public."""
    ticket = world.run_step("S1")
    world.clock.advance(900)
    run_watch(world.ctx)
    executions = world.clone / "journal" / "executions"
    assert not (executions.exists() and any(executions.rglob("*.json")))
    assert ticket not in world.ledger.get_runtime("executions_published", [])
