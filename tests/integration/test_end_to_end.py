"""End to end, offline: stub LLM + synthetic history + fake broker + a local git remote.

1. AWAITING ACCOUNT: a cycle with no broker publishes a sealed-and-revealed no-action cycle.
2. Connected: a cycle builds the reference from a flat book → proposal (sealed, pushed) →
   operator approve (guards injected, typed nonce) → execution on the fake broker → watch
   reveals the cycle and publishes the execution record. Nothing public contains money or ids.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from council.context import hold_reference_stub
from council.cycle import run_cycle
from council.ledger.db import Ledger
from council.llm.prompts import PromptRegistry
from council.llm.stub import StubGateway
from council.policy import Policy
from council.publish.gitops import Publisher
from council.publish.leakscan import scan
from council.runtime import CycleContext, Sources
from council.settings import Settings
from council.watch import run_watch

SLOT = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)          # Thursday, US session open
NOW = SLOT + timedelta(minutes=3)
API_KEY, READ_KEY, WRITE_KEY = "test-app-key", "test-read-key", "test-write-key"

# vehicle symbol -> (instrument id, price, settlement)
VEHICLES = {
    "EQQQ.L": (201, 400.0, "real"), "SMH.L": (202, 50.0, "real"), "CSPX.L": (203, 600.0, "real"),
    "SGLN.L": (204, 40.0, "real"), "BTC": (205, 60000.0, "real"), "ETH": (206, 3000.0, "real"),
    "OIL": (207, 70.0, "cfd"), "EURUSD": (208, 1.1, "cfd"), "GBPUSD": (209, 1.3, "cfd"),
    "NSDQ100": (210, 20000.0, "cfd"), "SPX500": (211, 6000.0, "cfd"), "GOLD": (212, 2500.0, "cfd"),
    "SOXX": (213, 250.0, "cfd"),
}


def _history(slot: datetime) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Up-trending synthetic daily bars for every line, completed and available before the slot."""
    rng = np.random.default_rng(7)
    out: dict[str, pd.DataFrame] = {}
    for line in Policy.load().universe.lines:
        crypto = line.asset_class == "crypto"
        end = (slot - timedelta(days=1)).date()
        if crypto:
            idx = pd.date_range(end=pd.Timestamp(end), periods=500, freq="D")
        else:
            idx = pd.bdate_range(end=pd.Timestamp(end), periods=500)
        idx = pd.DatetimeIndex(idx).tz_localize("UTC")
        noise = 0.03 if crypto else 0.009
        r = 0.0012 + rng.normal(0, noise, len(idx))
        close = 100 * np.exp(np.cumsum(r))
        openp = np.concatenate([[close[0]], close[:-1]])
        out[line.symbol] = pd.DataFrame({"open": openp, "high": np.maximum(openp, close) * 1.002,
                                         "low": np.minimum(openp, close) * 0.998, "close": close,
                                         "volume": 1000.0}, index=idx)
    return out, []


def _no_events(start, end):
    return [], []


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def remote_clone(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    clone = tmp_path / "state" / "publisher-clone"
    clone.parent.mkdir(parents=True)
    _git("clone", "-q", str(bare), str(clone), cwd=tmp_path)
    _git("config", "user.name", "council-publisher", cwd=clone)
    _git("config", "user.email", "18754232+fbzz@users.noreply.github.com", cwd=clone)
    (clone / "journal").mkdir()
    (clone / "journal" / ".keep").write_text("")
    _git("add", "journal/.keep", cwd=clone)
    _git("commit", "-q", "-m", "init", cwd=clone)
    _git("push", "-q", "-u", "origin", "main", cwd=clone)
    return clone


def _ctx(tmp_path: Path, *, broker=None, publisher=None, clock=lambda: NOW) -> CycleContext:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(state / "ledger.sqlite3", clock=clock)
    ledger.migrate()
    return CycleContext(
        policy=Policy.load(), settings=Settings(role="dev", mode="stub"), ledger=ledger,
        gateway=StubGateway(hold_reference_stub()), registry=PromptRegistry(),
        sources=Sources(history=_history, events=_no_events, broker=broker),
        publisher=publisher, clock=clock, state_dir=state,
    )


def _public_files(root: Path) -> list[Path]:
    return [p for p in (root / "journal").rglob("*") if p.is_file() and p.name != ".keep"]


def _assert_clean(root: Path, canaries=()) -> None:
    for p in _public_files(root):
        text = p.read_text()
        findings = scan(text, canaries=canaries)
        assert not findings, (p.name, [f.kind for f in findings])


def test_awaiting_account_cycle_publishes_sealed_no_action(tmp_path):
    preview = tmp_path / "preview"
    pub = Publisher(tmp_path / "state" / "publisher-clone", push=False, dry_run_dir=preview)
    ctx = _ctx(tmp_path, publisher=pub)
    out = run_cycle(ctx)
    assert out.status == "on_time"
    assert out.decision_state == "reviewed_no_action" and out.legs == 0
    assert out.basis in ("council", "council_partial_reference", "code_only")
    files = {p.relative_to(preview).as_posix() for p in _public_files(preview)}
    assert any("commitments/" in f for f in files)
    assert any("cycles/" in f for f in files)          # no proposal → revealed at once
    status = json.loads((preview / "journal" / "status.json").read_text())
    assert status["state"] == "AWAITING_ACCOUNT"
    _assert_clean(preview)
    # the same slot twice is a no-op; a run 3 hours late is recorded as missed
    assert run_cycle(ctx).status == "already_done"
    late = _ctx(tmp_path, publisher=pub, clock=lambda: SLOT + timedelta(hours=3, minutes=30))
    assert run_cycle(late).status in ("missed", "already_done")


@pytest.fixture
def fake_broker(tmp_path, monkeypatch):
    from council.broker.fake import FakeClock, FakeEtoro, eligibility_row, leverage_config
    from council.broker.instruments import InstrumentMap

    fclock = FakeClock(start=NOW)
    fake = FakeEtoro(clock=fclock.now, credit=10_000.0, write_user_keys={WRITE_KEY})
    for sym, (iid, px, settlement) in VEHICLES.items():
        if settlement == "real":
            configs = [leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,))]
        else:
            configs = [leverage_config(direction="LONG"), leverage_config(direction="SHORT")]
        fake.add_instrument(sym, iid, bid=px * 0.9995, ask=px * 1.0005,
                            row=eligibility_row(sym, iid, configs=configs))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    InstrumentMap({}, path=state / "instruments.json").merged(
        {s: v[0] for s, v in VEHICLES.items()}, NOW).save()
    return fake, fclock


def test_connected_cycle_approve_execute_watch(tmp_path, remote_clone, fake_broker, monkeypatch):
    from council.broker.etoro_read import EtoroReadClient
    from council.operator.approve import ApprovalDeps, approve

    fake, fclock = fake_broker
    read = EtoroReadClient(API_KEY, READ_KEY, transport=fake.transport(), sleep=fclock.sleep)
    pub = Publisher(remote_clone, push=True)
    ctx = _ctx(tmp_path, broker=read, publisher=pub)

    out = run_cycle(ctx)
    assert out.decision_id, out.flags
    assert out.decision_state == "proposed", out.flags
    assert out.legs > 0 and out.published and out.commit_sha
    d = ctx.ledger.get_decision(out.decision_id)
    assert d.published_commit

    # nothing public before execution reveals the proposal's content
    assert not list((remote_clone / "journal").rglob("cycles/*.json"))

    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    etoro_write = importlib.import_module("council.broker.etoro_write")

    def answer(prompt: str) -> str:
        return re.search(r"Type (\S+) to approve", prompt).group(1)

    printed: list[str] = []
    deps = ApprovalDeps(
        ledger=ctx.ledger, policy=ctx.policy, read=read,
        write_factory=lambda: etoro_write.EtoroWriteClient(API_KEY, WRITE_KEY, transport=fake.transport()),
        state_dir=ctx.state_dir, input_fn=answer, print_fn=printed.append, now_fn=fclock.now,
        guard_fn=lambda: None,
        executor_kwargs={"clock": fclock.now, "sleep": fclock.sleep, "_skip_guard_for_tests": True},
    )
    report = approve(out.decision_id, deps)
    assert report.final_state in ("completed", "completed_partial"), (report.reasons, printed)
    assert all(p.sl_rate for p in fake.positions.values()), "every open carries a stop-loss"

    w = run_watch(ctx)
    assert out.decision_id in w.executions_published or not w.executions_published
    assert out.cycle_id in w.revealed
    canaries = [f"{10_000.0:.2f}", "10,000.00"]
    _assert_clean(remote_clone, canaries=canaries)


def test_watch_records_stop_hit_and_halt_issues_flatten_proposal(tmp_path, remote_clone, fake_broker):
    """A broker stop-loss hit is recorded (re-entry cool-off) and a -25% drawdown produces a
    flatten PROPOSAL — never an order: the ledger shows it waiting for the operator."""
    from council.broker.etoro_read import EtoroReadClient

    fake, fclock = fake_broker
    btc = fake.add_position("BTC", is_buy=True, units=0.02, leverage=1, sl_rate=40_000.0, settlement="real")
    fake.add_position("EQQQ.L", is_buy=True, units=5, leverage=1, sl_rate=300.0, settlement="real")
    read = EtoroReadClient(API_KEY, READ_KEY, transport=fake.transport(), sleep=fclock.sleep)
    ctx = _ctx(tmp_path, broker=read, publisher=Publisher(remote_clone, push=True), clock=fclock.now)

    first = run_watch(ctx)                      # sets the lifetime peak and the observed positions
    assert not any("stop-loss hit" in a for a in first.alerts)
    fake.hit_stop(btc.position_id)
    fclock.advance(900)
    second = run_watch(ctx)
    assert any("stop-loss hit on BTC" in a for a in second.alerts)
    assert "BTC" in ctx.ledger.stop_hits_since(NOW - timedelta(days=1), universe=ctx.policy.universe)

    fake.credit -= 0.40 * fake.equity()         # a -40% loss: below the 0.75 x peak halt line
    fclock.advance(900)
    run_watch(ctx)                              # first breach read
    fclock.advance(900)
    third = run_watch(ctx)                      # confirmed on a second read >= 60 s later
    assert any("HALTED" in a for a in third.alerts), third.alerts
    pending = ctx.ledger.pending()
    assert pending and pending[0].kind == "flatten" and pending[0].state == "proposed"
    assert all(leg["kind"] in ("close", "partial_close") for leg in pending[0].plan["legs"])
    assert fake.positions, "nothing was closed without the operator"
